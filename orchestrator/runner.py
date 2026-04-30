"""Background agent-turn lifecycle for the orchestrator.

Owns asyncio tasks that drive ``pool.send()`` for fire-and-forget agent
calls (the orchestrator's ``send_to_agent_session`` tool), buffers per-turn
events for ``peek_agent_session``, and pushes structured ``Notification``
records onto a queue the orchestrator drains at the start of each turn.

The runner does **not** import from ``orchestrator.session`` or
``orchestrator.agent`` — it depends only on ``SessionPool``, ``SessionStore``,
and the manager's event types — so it stays unit-testable with simple
mock pools.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from manager.types import (
    PermissionRequest,
    PermissionResolved,
    TextComplete,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnComplete,
)

if TYPE_CHECKING:
    from api.pool import SessionPool
    from manager.store import SessionStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgentTurnEvent:
    """A single buffered event from an in-flight agent turn.

    Buffered in a bounded ring so peek_agent_session can return recent
    activity without holding the entire turn in memory.  ``seq`` is
    monotonic per (session_id, turn_id) so the LLM can resume incrementally
    using ``since_seq``.
    """

    seq: int
    kind: str  # "text" | "tool_use" | "tool_result" | "permission_request" | "permission_resolved"
    payload: dict[str, Any]
    timestamp: float


@dataclass(slots=True)
class _TurnRecord:
    """Internal mutable state for one in-flight (or recently finished) turn."""

    turn_id: str
    session_id: str
    session_title: str | None
    origin_tool_use_id: str | None
    started_at: float  # time.monotonic
    started_at_wall: float  # time.time, for surfacing to the LLM
    status: str = "running"
    task: asyncio.Task | None = None
    finished: bool = False
    cost: float = 0.0
    turns: int = 0
    error: str | None = None
    events: deque[AgentTurnEvent] = field(default_factory=deque)
    next_seq: int = 1
    last_assistant_text: str = ""
    pending_permission_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentTurnHandle:
    """Public, read-only view of an in-flight turn.

    Returned by :meth:`BackgroundAgentRunner.spawn` and used by the prompt
    builder to render the active-sessions section.  Frozen so consumers
    can't accidentally mutate runner state.
    """

    turn_id: str
    session_id: str
    session_title: str | None
    origin_tool_use_id: str | None
    started_at: float  # wall-clock seconds since epoch
    elapsed_seconds: float
    status: str
    pending_permission_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Notification:
    """A completed-turn notification destined for the orchestrator's LLM.

    Pushed by ``_drive`` exactly once per turn (success/failure/timeout/
    cancel) and drained by the orchestrator session before each LLM call.
    """

    notification_id: str
    turn_id: str
    session_id: str
    session_title: str | None
    origin_tool_use_id: str | None
    status: str  # "succeeded" | "failed" | "cancelled" | "timeout"
    cost: float
    turns: int
    duration_seconds: float
    error: str | None  # set when status != "succeeded"


# ---------------------------------------------------------------------------
# NotificationQueue
# ---------------------------------------------------------------------------


class NotificationQueue:
    """Append-only queue of :class:`Notification`, drained on idle.

    Decouples background runners from the orchestrator's turn lifecycle.
    A single async ``wake_callback`` is invoked best-effort after every
    push; listeners decide whether to start a new orchestrator turn or
    just let the next user prompt pick it up.
    """

    def __init__(self) -> None:
        self._items: list[Notification] = []
        self._wake_cb: Callable[[], Awaitable[None]] | None = None

    def push(self, n: Notification) -> None:
        self._items.append(n)
        cb = self._wake_cb
        if cb is None:
            return
        # Fire-and-forget so a slow callback can't block the pushing task.
        # The callback is responsible for its own error handling.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("NotificationQueue.push: no running loop, skipping wake")
            return
        loop.create_task(_safe_invoke(cb), name="notification-wake")

    def drain(self) -> list[Notification]:
        out = self._items
        self._items = []
        return out

    def has_pending(self) -> bool:
        return bool(self._items)

    def pending_count(self) -> int:
        return len(self._items)

    def set_wake_callback(self, cb: Callable[[], Awaitable[None]] | None) -> None:
        self._wake_cb = cb


async def _safe_invoke(cb: Callable[[], Awaitable[None]]) -> None:
    try:
        await cb()
    except Exception:  # noqa: BLE001
        logger.exception("NotificationQueue wake callback raised")


# ---------------------------------------------------------------------------
# BackgroundAgentRunner
# ---------------------------------------------------------------------------


class BackgroundAgentRunner:
    """Lifecycle manager for fire-and-forget agent turns.

    One instance per :class:`OrchestratorSession`.  Tools call
    :meth:`spawn` to dispatch a turn; the runner drives ``pool.send()`` to
    completion in a background task and pushes a :class:`Notification`
    onto the shared queue when done.  The orchestrator drains the queue
    on its next turn and renders each notification as a structured
    ``[SESSION xxx event: ...]`` line.

    Two concurrent ``spawn()`` calls for the same ``session_id`` serialize
    naturally inside ``pool.send()``'s per-session lock — no extra lock
    here.  Across distinct sessions, turns run in parallel.
    """

    def __init__(
        self,
        pool: SessionPool,
        store: SessionStore,
        notifications: NotificationQueue,
        *,
        peek_buffer_size: int = 200,
        default_timeout: float = 600.0,
    ) -> None:
        self._pool = pool
        self._store = store
        self._notifications = notifications
        self._peek_buffer_size = peek_buffer_size
        self._default_timeout = default_timeout
        # turn_id → record
        self._turns: dict[str, _TurnRecord] = {}
        # session_id → last turn_id (for peek without explicit turn_id)
        self._latest_by_session: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def notifications(self) -> NotificationQueue:
        return self._notifications

    async def spawn(
        self,
        session_id: str,
        message: str,
        *,
        origin_tool_use_id: str | None = None,
        timeout: float | None = None,
    ) -> AgentTurnHandle:
        """Validate, generate a ``turn_id``, spawn the driver task, return a handle.

        Raises ``ValueError`` if the pool has no live session with that ID.
        Returns within microseconds — does not await the agent's response.
        """
        if not self._pool.has(session_id):
            raise ValueError(f"No active session with ID {session_id}")

        title = self._resolve_title(session_id)
        turn_id = str(uuid.uuid4())
        now_mono = time.monotonic()
        now_wall = time.time()
        record = _TurnRecord(
            turn_id=turn_id,
            session_id=session_id,
            session_title=title,
            origin_tool_use_id=origin_tool_use_id,
            started_at=now_mono,
            started_at_wall=now_wall,
        )
        self._turns[turn_id] = record
        self._latest_by_session[session_id] = turn_id

        record.task = asyncio.create_task(
            self._drive(record, message, timeout or self._default_timeout),
            name=f"runner-{session_id[:8]}-{turn_id[:8]}",
        )
        return self._handle(record)

    def peek(
        self,
        session_id: str,
        *,
        turn_id: str | None = None,
        since_seq: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Snapshot of buffered events for the most recent (or specified) turn.

        Returns a dict with keys ``session_id``, ``turn_id``, ``status``,
        ``finished``, ``next_seq``, ``last_assistant_text``, ``events`` —
        suitable for direct JSON serialization by the tool layer.
        """
        if turn_id is None:
            turn_id = self._latest_by_session.get(session_id)
        if turn_id is None or turn_id not in self._turns:
            return {
                "session_id": session_id,
                "turn_id": turn_id,
                "status": "unknown",
                "finished": True,
                "next_seq": 0,
                "last_assistant_text": "",
                "events": [],
                "error": "no in-flight or buffered turn for that session/turn_id",
            }
        record = self._turns[turn_id]
        events = [e for e in record.events if e.seq > since_seq][:limit]
        return {
            "session_id": record.session_id,
            "turn_id": record.turn_id,
            "session_title": record.session_title,
            "status": record.status,
            "finished": record.finished,
            "next_seq": record.next_seq,
            "last_assistant_text": record.last_assistant_text,
            "events": [
                {
                    "seq": e.seq,
                    "kind": e.kind,
                    "payload": e.payload,
                    "timestamp": e.timestamp,
                }
                for e in events
            ],
        }

    def list_in_flight(self) -> list[AgentTurnHandle]:
        """Snapshot of currently running turns. Used by the prompt builder."""
        return [self._handle(r) for r in self._turns.values() if not r.finished]

    async def cancel(self, turn_id: str) -> bool:
        """Cancel a single turn. Returns True if a task was found and cancelled.

        Also calls ``pool.interrupt(session_id)`` so the SDK actually stops
        — cancelling our coroutine alone leaves the bundled ``claude``
        subprocess running.
        """
        record = self._turns.get(turn_id)
        if record is None or record.finished or record.task is None:
            return False
        # Best-effort SDK interrupt
        try:
            await self._pool.interrupt(record.session_id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to interrupt SDK for session %s during cancel(%s)",
                record.session_id, turn_id,
            )
        record.task.cancel()
        return True

    async def cancel_all(self) -> None:
        """Cancel every in-flight task. Called from OrchestratorSession.stop().

        After all tasks complete, finalise any record whose task was cancelled
        before its body ran (asyncio can cancel-before-start, in which case
        ``_drive``'s own finally never executes).  This guarantees one
        Notification per turn even in shutdown races.
        """
        records = [r for r in self._turns.values() if not r.finished and r.task is not None]
        if not records:
            return

        # Best-effort SDK interrupts (one per distinct session_id)
        seen: set[str] = set()
        for r in records:
            if r.session_id in seen:
                continue
            seen.add(r.session_id)
            try:
                await self._pool.interrupt(r.session_id)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Failed to interrupt SDK for session %s during cancel_all",
                    r.session_id,
                )

        for r in records:
            assert r.task is not None  # for type-checkers
            r.task.cancel()
        await asyncio.gather(
            *(r.task for r in records if r.task is not None),
            return_exceptions=True,
        )

        # Safety net for cancel-before-start: any record still un-finalised
        # had its task killed before _drive could run its own except/finally.
        for r in records:
            if not r.finished:
                self._finalise(r, status="cancelled", error="cancelled before start")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _handle(self, r: _TurnRecord) -> AgentTurnHandle:
        elapsed = time.monotonic() - r.started_at
        return AgentTurnHandle(
            turn_id=r.turn_id,
            session_id=r.session_id,
            session_title=r.session_title,
            origin_tool_use_id=r.origin_tool_use_id,
            started_at=r.started_at_wall,
            elapsed_seconds=elapsed,
            status=r.status,
            pending_permission_ids=r.pending_permission_ids,
        )

    def _resolve_title(self, session_id: str) -> str | None:
        """Best-effort lookup of the session's display title."""
        sm = self._pool.get(session_id)
        if sm is None:
            return None
        sdk_id = sm.sdk_session_id
        if not sdk_id:
            return None
        try:
            info = self._store.get_session_info(sdk_id)
        except Exception:  # noqa: BLE001
            return None
        return info.title if info is not None else None

    def _buffer(self, record: _TurnRecord, kind: str, payload: dict[str, Any]) -> None:
        """Append an event to the per-turn ring buffer (bounded)."""
        seq = record.next_seq
        record.next_seq += 1
        evt = AgentTurnEvent(seq=seq, kind=kind, payload=payload, timestamp=time.monotonic())
        record.events.append(evt)
        if len(record.events) > self._peek_buffer_size:
            record.events.popleft()

    async def _drive(
        self,
        record: _TurnRecord,
        message: str,
        timeout: float,
    ) -> None:
        """Task body. Iterate ``pool.send``, buffer events, push Notification.

        Always finalises in a ``try/except/finally`` so a Notification is
        emitted exactly once per turn — whether the turn succeeded, failed,
        timed out, or got cancelled.  Without this, a cancelled turn would
        leak into the runner's records forever (the queue would never see
        a closing notification).
        """
        sm = self._pool.get(record.session_id)
        if sm is None:
            self._finalise(record, status="failed", error=f"session {record.session_id} not in pool")
            return

        async def _consume() -> None:
            async for event in self._pool.send(record.session_id, message):
                # Track pending permissions for the prompt-builder snapshot
                if hasattr(sm, "pending_permission_ids"):
                    record.pending_permission_ids = tuple(sm.pending_permission_ids())

                if isinstance(event, TextDelta):
                    self._buffer(record, "text_delta", {"text": event.text})
                elif isinstance(event, TextComplete):
                    record.last_assistant_text = event.text
                    self._buffer(record, "text", {"text": event.text})
                elif isinstance(event, ToolUse):
                    self._buffer(record, "tool_use", {
                        "tool_use_id": event.tool_use_id,
                        "tool_name": event.tool_name,
                        "tool_input": event.tool_input,
                    })
                elif isinstance(event, ToolResult):
                    self._buffer(record, "tool_result", {
                        "tool_use_id": event.tool_use_id,
                        "output_excerpt": _excerpt(event.output, 800),
                        "is_error": event.is_error,
                    })
                elif isinstance(event, PermissionRequest):
                    self._buffer(record, "permission_request", {
                        "request_id": event.request_id,
                        "tool_name": event.tool_name,
                        "tool_input": event.tool_input,
                    })
                elif isinstance(event, PermissionResolved):
                    self._buffer(record, "permission_resolved", {
                        "request_id": event.request_id,
                        "decision": event.decision,
                        "responder": event.responder,
                        "message": event.message,
                    })
                elif isinstance(event, TurnComplete):
                    record.cost = event.cost or 0.0
                    record.turns = event.num_turns or 0

        # The try/except below sets a specific status; the outer finally is a
        # safety net for the case where the task is cancelled BEFORE the body
        # runs at all (asyncio can do that — cancel-before-start) so _finalise
        # would otherwise never run and a Notification would leak.
        try:
            try:
                await asyncio.wait_for(_consume(), timeout=timeout)
            except asyncio.TimeoutError:
                try:
                    await self._pool.interrupt(record.session_id)
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to interrupt session %s after timeout", record.session_id)
                self._finalise(record, status="timeout", error=f"turn exceeded {timeout:.0f}s")
            except asyncio.CancelledError:
                self._finalise(record, status="cancelled", error="cancelled by orchestrator")
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("BackgroundAgentRunner._drive failed for turn %s", record.turn_id)
                self._finalise(record, status="failed", error=str(exc))
            else:
                self._finalise(record, status="succeeded", error=None)
        finally:
            # Backstop: if the task was cancelled before the body executed (or
            # any other path missed _finalise), still emit a notification.
            if not record.finished:
                self._finalise(record, status="cancelled", error="cancelled before start")

    def _finalise(
        self,
        record: _TurnRecord,
        *,
        status: str,
        error: str | None,
    ) -> None:
        """Mark a turn finished and push its Notification. Idempotent."""
        if record.finished:
            return
        record.finished = True
        record.status = status
        record.error = error
        duration = time.monotonic() - record.started_at
        notification = Notification(
            notification_id=str(uuid.uuid4()),
            turn_id=record.turn_id,
            session_id=record.session_id,
            session_title=record.session_title,
            origin_tool_use_id=record.origin_tool_use_id,
            status=status,
            cost=record.cost,
            turns=record.turns,
            duration_seconds=duration,
            error=error,
        )
        try:
            self._notifications.push(notification)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to push notification for turn %s", record.turn_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _excerpt(text: str, limit: int) -> str:
    """Trim a tool output for buffering — full text is in the agent's JSONL."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [+{len(text) - limit} chars]"


__all__ = [
    "AgentTurnEvent",
    "AgentTurnHandle",
    "BackgroundAgentRunner",
    "Notification",
    "NotificationQueue",
]
