"""Tests for orchestrator.runner — BackgroundAgentRunner / NotificationQueue."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from manager.types import (
    PermissionRequest,
    PermissionResolved,
    SessionInfo,
    TextComplete,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnComplete,
)
from orchestrator.runner import (
    AgentTurnHandle,
    BackgroundAgentRunner,
    Notification,
    NotificationQueue,
)


# ---------------------------------------------------------------------------
# FakePool — minimal shim with the surface the runner uses.
# ---------------------------------------------------------------------------


class FakePool:
    """A SessionPool double that lets each session's send() be scripted.

    ``script`` maps session_id → list-of-events to yield (or a callable that
    returns such a list / async generator).  ``send()`` is an async generator
    so the runner's ``async for`` works as in production.
    """

    def __init__(self) -> None:
        self.sessions: dict[str, MagicMock] = {}
        self.scripts: dict[str, Any] = {}
        self.interrupts: list[str] = []
        self._send_started: dict[str, asyncio.Event] = {}
        self._block_until: dict[str, asyncio.Event] = {}

    def add_session(self, sid: str, sdk_id: str | None = "sdk-" + "x") -> None:
        sm = MagicMock()
        sm.sdk_session_id = sdk_id
        sm.pending_permission_ids = MagicMock(return_value=[])
        self.sessions[sid] = sm
        self._send_started[sid] = asyncio.Event()
        self._block_until[sid] = asyncio.Event()
        self._block_until[sid].set()  # unblocked by default

    def has(self, sid: str) -> bool:
        return sid in self.sessions

    def get(self, sid: str) -> Any:
        return self.sessions.get(sid)

    async def interrupt(self, sid: str) -> None:
        self.interrupts.append(sid)
        # Free any blocked send() so the cancel/timeout path can finish.
        if sid in self._block_until:
            self._block_until[sid].set()

    async def send(self, sid: str, message: str, *, source_ws=None):
        self._send_started[sid].set()
        await self._block_until[sid].wait()
        events = self.scripts.get(sid, [])
        for evt in events:
            yield evt

    def block_send(self, sid: str) -> None:
        self._block_until[sid].clear()

    def unblock_send(self, sid: str) -> None:
        self._block_until[sid].set()


class FakeStore:
    def __init__(self, titles: dict[str, str] | None = None) -> None:
        self.titles = titles or {}

    def get_session_info(self, sdk_id: str) -> SessionInfo | None:
        title = self.titles.get(sdk_id)
        if title is None:
            return None
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        return SessionInfo(
            session_id=sdk_id,
            started_at=now,
            last_activity=now,
            title=title,
            message_count=0,
        )


@pytest.fixture
def queue() -> NotificationQueue:
    return NotificationQueue()


@pytest.fixture
def pool() -> FakePool:
    return FakePool()


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def runner(pool: FakePool, store: FakeStore, queue: NotificationQueue) -> BackgroundAgentRunner:
    return BackgroundAgentRunner(pool, store, queue, default_timeout=2.0)


# ---------------------------------------------------------------------------
# spawn() returns immediately
# ---------------------------------------------------------------------------


async def test_spawn_returns_immediately_with_turn_id(
    runner: BackgroundAgentRunner, pool: FakePool, queue: NotificationQueue
) -> None:
    pool.add_session("s1")
    pool.block_send("s1")  # send() will block; spawn must NOT wait on it
    pool.scripts["s1"] = [TurnComplete(cost=0.0, num_turns=1)]

    t0 = time.monotonic()
    handle = await runner.spawn("s1", "do work")
    elapsed = time.monotonic() - t0

    assert isinstance(handle, AgentTurnHandle)
    assert handle.session_id == "s1"
    assert handle.turn_id  # non-empty
    assert handle.status == "running"
    assert elapsed < 0.5, f"spawn took {elapsed:.3f}s — must be near-instant"

    # Cleanup so the test loop can shut down cleanly
    await runner.cancel_all()


async def test_spawn_unknown_session_raises(runner: BackgroundAgentRunner) -> None:
    with pytest.raises(ValueError, match="No active session"):
        await runner.spawn("missing", "hi")


# ---------------------------------------------------------------------------
# Successful completion → succeeded notification
# ---------------------------------------------------------------------------


async def test_completed_turn_pushes_notification(
    runner: BackgroundAgentRunner, pool: FakePool, queue: NotificationQueue
) -> None:
    pool.add_session("s1")
    pool.scripts["s1"] = [
        TextComplete(text="hello"),
        TurnComplete(cost=0.0042, num_turns=1),
    ]

    handle = await runner.spawn("s1", "go")
    # Wait for the driver task to complete
    record_task = runner._turns[handle.turn_id].task
    assert record_task is not None
    await record_task

    pending = queue.drain()
    assert len(pending) == 1
    n = pending[0]
    assert isinstance(n, Notification)
    assert n.turn_id == handle.turn_id
    assert n.session_id == "s1"
    assert n.status == "succeeded"
    assert n.cost == pytest.approx(0.0042)
    assert n.turns == 1
    assert n.error is None
    assert n.duration_seconds >= 0.0


async def test_failed_turn_pushes_failure_notification(
    runner: BackgroundAgentRunner, pool: FakePool, queue: NotificationQueue
) -> None:
    pool.add_session("s1")

    async def boom_send(*args, **kwargs):
        # Need to make this an async generator that raises.
        if False:
            yield  # pragma: no cover
        raise RuntimeError("synthetic failure")

    pool.send = boom_send

    handle = await runner.spawn("s1", "go")
    await runner._turns[handle.turn_id].task  # type: ignore[arg-type]

    pending = queue.drain()
    assert len(pending) == 1
    assert pending[0].status == "failed"
    assert pending[0].error is not None
    assert "synthetic failure" in pending[0].error


# ---------------------------------------------------------------------------
# Timeout / cancellation
# ---------------------------------------------------------------------------


async def test_timeout_pushes_timeout_notification(
    pool: FakePool, store: FakeStore, queue: NotificationQueue
) -> None:
    pool.add_session("s1")
    pool.block_send("s1")  # never unblock except via interrupt
    runner = BackgroundAgentRunner(pool, store, queue, default_timeout=0.2)

    handle = await runner.spawn("s1", "go")
    # Driver should hit its 0.2s timeout
    await runner._turns[handle.turn_id].task  # type: ignore[arg-type]

    pending = queue.drain()
    assert len(pending) == 1
    assert pending[0].status == "timeout"
    assert pending[0].error and "exceeded" in pending[0].error
    assert "s1" in pool.interrupts


async def test_cancel_all_finalises_every_turn(
    runner: BackgroundAgentRunner, pool: FakePool, queue: NotificationQueue
) -> None:
    """cancel_all() must produce exactly one notification per in-flight turn,
    interrupt every distinct session, and leave no in-flight handles behind.

    Whether the final status is 'cancelled' or 'succeeded' depends on whether
    the pool's generator exited via cancellation or after a graceful interrupt
    yielded TurnComplete — both are acceptable end states.  What's NOT
    acceptable is a leaked task or a missing notification.
    """
    for sid in ("a", "b", "c"):
        pool.add_session(sid)
        pool.block_send(sid)

    await runner.spawn("a", "go")
    await runner.spawn("b", "go")
    await runner.spawn("c", "go")

    await runner.cancel_all()

    pending = queue.drain()
    assert len(pending) == 3
    assert {n.session_id for n in pending} == {"a", "b", "c"}
    # Each notification has a terminal status (not 'running')
    assert all(n.status in ("succeeded", "failed", "cancelled", "timeout") for n in pending)
    # Each session got an interrupt
    assert set(pool.interrupts) >= {"a", "b", "c"}
    # No more in-flight handles
    assert runner.list_in_flight() == []


async def test_cancel_propagates_when_task_blocks_through_interrupt(
    runner: BackgroundAgentRunner, store: FakeStore, queue: NotificationQueue
) -> None:
    """Verify the 'cancelled' status path specifically: a pool whose
    interrupt() does NOT free the generator forces the runner to fall back
    to task.cancel() and produce a 'cancelled' notification."""
    class StubbornPool:
        def __init__(self) -> None:
            self.sessions = {"a": MagicMock(sdk_session_id="sdk-a", pending_permission_ids=lambda: [])}
            self.interrupts: list[str] = []
        def has(self, sid): return sid in self.sessions
        def get(self, sid): return self.sessions.get(sid)
        async def interrupt(self, sid): self.interrupts.append(sid)
        async def send(self, sid, message, *, source_ws=None):
            # Pure block: never yields, never exits except by cancellation
            await asyncio.Event().wait()
            yield  # pragma: no cover

    pool = StubbornPool()
    runner = BackgroundAgentRunner(pool, store, queue, default_timeout=10.0)  # type: ignore[arg-type]

    handle = await runner.spawn("a", "go")
    # Yield so the driver actually enters pool.send()
    await asyncio.sleep(0.05)
    await runner.cancel_all()

    pending = queue.drain()
    assert len(pending) == 1
    assert pending[0].status == "cancelled"
    assert pool.interrupts == ["a"]


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


async def test_two_concurrent_spawns_different_sessions_run_parallel(
    runner: BackgroundAgentRunner, pool: FakePool, queue: NotificationQueue
) -> None:
    pool.add_session("s1")
    pool.add_session("s2")
    pool.block_send("s1")
    pool.block_send("s2")

    h1 = await runner.spawn("s1", "go")
    h2 = await runner.spawn("s2", "go")

    # Both driver tasks should have started consuming pool.send (and thus
    # be parked on the block_until event for their session) within a beat.
    await asyncio.wait_for(pool._send_started["s1"].wait(), timeout=1.0)
    await asyncio.wait_for(pool._send_started["s2"].wait(), timeout=1.0)

    # Snapshot reflects both as in-flight
    in_flight = runner.list_in_flight()
    assert {h.turn_id for h in in_flight} == {h1.turn_id, h2.turn_id}

    # Cleanup
    await runner.cancel_all()


# ---------------------------------------------------------------------------
# peek
# ---------------------------------------------------------------------------


async def test_peek_returns_buffered_text_and_tool_events(
    runner: BackgroundAgentRunner, pool: FakePool, queue: NotificationQueue
) -> None:
    pool.add_session("s1")
    pool.scripts["s1"] = [
        TextDelta(text="hel"),
        TextDelta(text="lo"),
        TextComplete(text="hello"),
        ToolUse(tool_use_id="tu_1", tool_name="Read", tool_input={"path": "x"}),
        ToolResult(tool_use_id="tu_1", output="contents", is_error=False),
        TurnComplete(cost=0.0, num_turns=1),
    ]

    handle = await runner.spawn("s1", "go")
    await runner._turns[handle.turn_id].task  # type: ignore[arg-type]

    snap = runner.peek("s1")
    assert snap["session_id"] == "s1"
    assert snap["turn_id"] == handle.turn_id
    assert snap["finished"] is True
    assert snap["last_assistant_text"] == "hello"

    kinds = [e["kind"] for e in snap["events"]]
    assert "text_delta" in kinds
    assert "text" in kinds
    assert "tool_use" in kinds
    assert "tool_result" in kinds


async def test_peek_since_seq_is_incremental(
    runner: BackgroundAgentRunner, pool: FakePool, queue: NotificationQueue
) -> None:
    pool.add_session("s1")
    pool.scripts["s1"] = [
        TextComplete(text="one"),
        TextComplete(text="two"),
        TextComplete(text="three"),
        TurnComplete(cost=0.0, num_turns=1),
    ]
    handle = await runner.spawn("s1", "go")
    await runner._turns[handle.turn_id].task  # type: ignore[arg-type]

    first = runner.peek("s1", limit=2)
    assert len(first["events"]) == 2
    last_seq = first["events"][-1]["seq"]

    second = runner.peek("s1", since_seq=last_seq)
    # Remaining events should have seq > last_seq
    assert all(e["seq"] > last_seq for e in second["events"])
    assert len(second["events"]) >= 1


def test_peek_unknown_session_returns_error_shape(
    runner: BackgroundAgentRunner,
) -> None:
    snap = runner.peek("missing")
    assert snap["status"] == "unknown"
    assert snap["events"] == []
    assert "error" in snap


# ---------------------------------------------------------------------------
# NotificationQueue
# ---------------------------------------------------------------------------


async def test_drain_returns_and_clears(queue: NotificationQueue) -> None:
    n1 = _make_notification("t1")
    n2 = _make_notification("t2")
    queue.push(n1)
    queue.push(n2)
    assert queue.has_pending() and queue.pending_count() == 2
    items = queue.drain()
    assert items == [n1, n2]
    assert not queue.has_pending()
    assert queue.drain() == []


async def test_wake_callback_fires_on_each_push(queue: NotificationQueue) -> None:
    invocations = 0
    fired = asyncio.Event()

    async def cb() -> None:
        nonlocal invocations
        invocations += 1
        fired.set()

    queue.set_wake_callback(cb)
    for i in range(3):
        queue.push(_make_notification(f"t{i}"))

    # The callback is dispatched via loop.create_task so we yield to let it run
    # All three pushes should produce three callback invocations
    for _ in range(20):
        await asyncio.sleep(0.01)
        if invocations >= 3:
            break
    assert invocations == 3


async def test_wake_callback_unset_clears(queue: NotificationQueue) -> None:
    calls = 0

    async def cb() -> None:
        nonlocal calls
        calls += 1

    queue.set_wake_callback(cb)
    queue.set_wake_callback(None)
    queue.push(_make_notification("t1"))
    await asyncio.sleep(0.05)
    assert calls == 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_notification(turn_id: str) -> Notification:
    return Notification(
        notification_id="nid-" + turn_id,
        turn_id=turn_id,
        session_id="s1",
        session_title=None,
        origin_tool_use_id=None,
        status="succeeded",
        cost=0.0,
        turns=1,
        duration_seconds=0.1,
        error=None,
    )
