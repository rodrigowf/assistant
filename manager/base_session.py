"""BaseSessionManager — abstract interface for a single agent session.

Both :class:`manager.claude.session.ClaudeSessionManager` and
:class:`manager.qwen.session.QwenSessionManager` implement this contract.
The pool, the WebSocket chat handler, and the orchestrator all interact
with sessions through this interface — they should never see provider
specifics.

Subclasses own:
- Spawning and reaping their provider's subprocess
- Translating native streaming events into the normalized
  :mod:`manager.types` Event hierarchy
- Permission gating (for providers that support a popup mechanism)
- Any provider-specific quirks (Claude's stall watchdog, Qwen's
  one-shot-per-turn lifecycle, etc.)

What the base class provides:
- The dual-ID model (stable ``local_id`` + provider-supplied
  ``provider_session_id``)
- Permission state (pending Futures, gated-tool set)
- Status tracking and the common property surface used by the pool
- A standard async-context-manager protocol
"""

from __future__ import annotations

import asyncio
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING

from .config import ManagerConfig
from .types import Event, PermissionRequest, PermissionResolved, SessionStatus

if TYPE_CHECKING:
    pass


class TurnAbandoned(Exception):
    """Raised when a turn produced no events for so long the upstream
    request is considered wedged.  Subclassed by each provider to add
    provider-specific context (Claude: ``SessionAbandoned``; Qwen:
    ``QwenAbandoned``).

    Catch ``TurnAbandoned`` to handle both providers uniformly.
    """

    def __init__(self, elapsed_seconds: float) -> None:
        super().__init__(
            f"Turn produced no events after {elapsed_seconds:.0f}s "
            "(upstream request appears wedged)"
        )
        self.elapsed_seconds = elapsed_seconds


class BaseSessionManager(ABC):
    """Abstract base for all session managers.

    Concrete subclasses must implement:
        - :meth:`_run_lifecycle` — connect, idle-wait on ``_stop_requested``, disconnect
        - :meth:`send` — stream a turn's events
        - :meth:`interrupt` — cancel the in-flight turn
        - :meth:`provider_name` — provider identifier

    Optional overrides:
        - :meth:`compact` — provider-specific compaction (Claude only by default)
        - :meth:`command` — slash command (Claude only by default)
        - :meth:`subprocess_pid` property — for the pool's orphan reaper
    """

    def __init__(
        self,
        session_id: str | None = None,
        *,
        local_id: str | None = None,
        fork: bool = False,
        config: ManagerConfig | None = None,
    ) -> None:
        self._config = config or ManagerConfig.load()
        self._local_id: str = local_id or str(uuid.uuid4())
        self._resume_id: str | None = session_id  # provider-side session id to resume
        self._fork: bool = fork

        # Provider-supplied session id. Distinct from local_id: the latter
        # is stable across reconnects/restarts; this one is whatever the
        # underlying CLI hands us (Claude SDK session id, Qwen session id).
        self._provider_session_id: str | None = None

        self._status: SessionStatus = SessionStatus.DISCONNECTED
        self._cost: float = 0.0
        self._turns: int = 0

        # Lifecycle task — owns connect and disconnect from the same task.
        self._lifecycle_task: asyncio.Task[None] | None = None
        self._connect_done: asyncio.Event = asyncio.Event()
        self._connect_error: BaseException | None = None
        self._stop_requested: asyncio.Event = asyncio.Event()

        # Optional callbacks the pool installs so it can track per-turn
        # subprocess PIDs without polling.  Qwen spawns a fresh subprocess
        # for every turn; Claude has one persistent PID for the session.
        # Either provider can register/deregister PIDs through these hooks
        # so the pool's orphan reaper sees them.
        self._on_pid_spawn: "Callable[[int], None] | None" = None
        self._on_pid_exit: "Callable[[int], None] | None" = None

        # Permission gating shared state. ``_event_inbox`` is set by send()
        # so the permission callback can inject events into the live stream;
        # ``_pending_permissions`` holds the asyncio.Future per request.
        self._gated_tools: set[str] = set()
        self._pending_permissions: dict[str, asyncio.Future[tuple[str, str | None, str]]] = {}
        self._event_inbox: asyncio.Queue[Event] | None = None

    # ------------------------------------------------------------------
    # Abstract / required interface
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Canonical provider identifier — registered harness id."""

    @abstractmethod
    async def _run_lifecycle(self) -> None:
        """Subclass-owned body of the lifecycle task.

        Responsibilities:
        1. Establish the underlying connection (spawn subprocess, exchange
           init handshake, capture provider session id, etc.).
        2. If connection succeeds, set ``self._status = SessionStatus.IDLE``
           and call ``self._connect_done.set()``.
        3. If connection fails, set ``self._connect_error`` and call
           ``self._connect_done.set()``, then return.
        4. Await ``self._stop_requested.wait()`` (idle).
        5. In a ``finally``: clean up the connection / subprocess and set
           ``self._status = SessionStatus.DISCONNECTED``.
        """

    @abstractmethod
    async def send(self, prompt: str) -> AsyncIterator[Event]:
        """Send a prompt and stream typed events back."""

    @abstractmethod
    async def interrupt(self) -> None:
        """Stop the in-flight turn at the provider level."""

    # ------------------------------------------------------------------
    # Async context manager
    # ------------------------------------------------------------------

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # Lifecycle — uniform across providers
    # ------------------------------------------------------------------

    async def start(self) -> str:
        """Connect to the underlying agent and return the stable local id.

        The lifecycle task is created here and runs until :meth:`stop`.
        Implementations that need provider-specific pre-flight checks
        (e.g. an SSH reachability probe) should override
        :meth:`_pre_start_check` rather than this method.
        """
        if self._lifecycle_task is not None:
            raise RuntimeError(f"{type(self).__name__}.start() called twice")

        await self._pre_start_check()

        self._lifecycle_task = asyncio.create_task(
            self._lifecycle(), name=f"sm-lifecycle-{self._local_id}",
        )
        await self._connect_done.wait()
        if self._connect_error is not None:
            self._lifecycle_task = None
            err = self._connect_error
            self._connect_error = None
            raise err
        return self._local_id

    async def _pre_start_check(self) -> None:
        """Synchronous-ish pre-flight check. Override to raise before the
        lifecycle task is spawned (e.g. for SSH reachability)."""
        return None

    async def _lifecycle(self) -> None:
        """Wrapper around the subclass-provided ``_run_lifecycle``.

        Existence of this thin wrapper means the lifecycle-task naming and
        creation logic only lives in one place; subclasses focus on the
        actual connect/idle/disconnect dance.
        """
        await self._run_lifecycle()

    async def stop(self) -> None:
        """Request disconnect and wait for the lifecycle task to finish.

        Safe to call from any task. The lifecycle task is shielded so a
        cancellation in the caller doesn't propagate into the in-flight
        disconnect — that would tear down the SDK / subprocess in a way
        that leaks file descriptors and pins the event loop.
        """
        if self._lifecycle_task is None:
            self._status = SessionStatus.DISCONNECTED
            return
        self._stop_requested.set()
        task = self._lifecycle_task
        # Clear the slot before awaiting so concurrent stop() callers
        # short-circuit instead of racing on the same Task reference.
        self._lifecycle_task = None
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "Lifecycle task for session %s exited with error", self._local_id,
            )

    # ------------------------------------------------------------------
    # Optional capabilities (defaults: not supported)
    # ------------------------------------------------------------------

    async def compact(self) -> AsyncIterator[Event]:
        """Trigger conversation compaction. Default: not supported.

        Subclasses that support compaction should override this.
        """
        if False:  # pragma: no cover  — keeps this an async generator
            yield  # type: ignore[unreachable]
        raise NotImplementedError(
            f"{type(self).__name__} does not support compaction",
        )

    async def command(self, slash_command: str) -> AsyncIterator[Event]:
        """Send an arbitrary slash command. Default: delegate to ``send``.

        Subclasses that have a dedicated command channel may override.
        """
        async for event in self.send(slash_command):
            yield event

    # ------------------------------------------------------------------
    # Permission gating — shared resolution; injection is subclass-specific
    # ------------------------------------------------------------------

    def resolve_permission(
        self,
        request_id: str,
        decision: str,
        *,
        message: str | None = None,
        responder: str = "user",
    ) -> bool:
        """Answer a pending PermissionRequest. First call wins."""
        future = self._pending_permissions.get(request_id)
        if future is None or future.done():
            return False
        future.set_result((decision, message, responder))
        return True

    def pending_permission_ids(self) -> list[str]:
        return [rid for rid, fut in self._pending_permissions.items() if not fut.done()]

    async def _emit_permission_request(
        self,
        tool_name: str,
        tool_input: dict,
    ) -> tuple[str, str | None]:
        """Helper for subclasses: emit a ``PermissionRequest`` into the
        active stream and await the resolution. Returns ``(decision, message)``.

        Auto-allows (returns ``("allow", None)``) when there's no active
        stream — a permission popup without a UI to display it would deadlock.
        """
        inbox = self._event_inbox
        if inbox is None:
            return "allow", None

        request_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[str, str | None, str]] = loop.create_future()
        self._pending_permissions[request_id] = future

        await inbox.put(PermissionRequest(
            request_id=request_id,
            tool_name=tool_name,
            tool_input=dict(tool_input),
        ))

        try:
            decision, message, responder = await future
        finally:
            self._pending_permissions.pop(request_id, None)

        await inbox.put(PermissionResolved(
            request_id=request_id,
            decision=decision,
            responder=responder,
            message=message,
        ))
        return decision, message

    def _drain_pending_permissions(self) -> None:
        """Resolve every still-pending permission as 'deny' (stream ended).

        Called at the end of a send() so the SDK doesn't leak a future
        nothing will ever resolve.
        """
        for rid, fut in list(self._pending_permissions.items()):
            if not fut.done():
                fut.set_result(("deny", "stream ended", "system"))
            self._pending_permissions.pop(rid, None)

    # ------------------------------------------------------------------
    # Read-only properties shared by every session
    # ------------------------------------------------------------------

    @property
    def local_id(self) -> str:
        """Stable local identifier (never changes)."""
        return self._local_id

    @property
    def session_id(self) -> str:
        """Alias for local_id — the stable identifier the pool keys on."""
        return self._local_id

    @property
    def sdk_session_id(self) -> str | None:
        """Provider-supplied session id (Claude SDK id, Qwen session id).

        Named for backward compatibility — both providers expose it under
        the same attribute so the pool can key on it interchangeably.
        """
        return self._provider_session_id

    @property
    def is_active(self) -> bool:
        return self._status not in (
            SessionStatus.DISCONNECTED,
            SessionStatus.INTERRUPTED,
        )

    @property
    def status(self) -> SessionStatus:
        return self._status

    @property
    def cost(self) -> float:
        return self._cost

    @property
    def turns(self) -> int:
        return self._turns

    @property
    def is_resumed(self) -> bool:
        """True if this session was resumed from an existing one."""
        return self._resume_id is not None

    @property
    def subprocess_pid(self) -> int | None:
        """PID of the provider subprocess, if the implementation tracks it.

        Used by the pool's orphan reaper. Default: None (no tracking).
        Note: this only returns a value while a subprocess is actually
        running — for Qwen that means mid-turn.  Callers that need
        continuous tracking should register via :meth:`set_pid_callbacks`
        so they're notified at spawn and exit.
        """
        return None

    def set_pid_callbacks(
        self,
        on_spawn: Callable[[int], None] | None,
        on_exit: Callable[[int], None] | None,
    ) -> None:
        """Register callbacks the session invokes when a subprocess starts
        or exits.  The pool installs these so its orphan reaper can track
        Qwen's per-turn PIDs (and could track Claude's long-lived PID too
        if we ever needed to).

        Either callback may be ``None``; both are called best-effort and
        must not raise.  Implementations that don't fork subprocesses
        ignore the callbacks entirely.
        """
        self._on_pid_spawn = on_spawn
        self._on_pid_exit = on_exit
