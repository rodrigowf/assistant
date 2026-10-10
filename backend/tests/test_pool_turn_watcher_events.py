"""Turn watcher events: ``agent_turn_started`` / ``agent_turn_finished`` (spec 12 §3.7).

Device notifications ("an agent session finished") need to hear about every
agent turn on every device, but agent turn events only reach that session's
chat sockets.  ``SessionPool.send`` — the path of both chat tabs and the
orchestrator's runner — therefore tells every pool watcher (every
orchestrator socket) when a turn starts and how it ended.  These tests pin:

1. a finished turn: one started + one finished{ok} with preview, title, ids;
2. failures (``is_error``, an exception, an abandoned turn after its retry)
   end as ``error`` with the detail;
3. stops (``interrupt`` then the SDK's closing TurnComplete, ``cancel_turn``
   mid-stream) end as ``interrupted`` — clients don't notify about those;
4. the preview text helper and the store's cheap title lookup.
"""

from __future__ import annotations

import asyncio
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import orjson
import pytest
from starlette.websockets import WebSocketState

from api.pool import SessionPool, turn_preview
from manager.claude.session import SessionAbandoned
from manager.store import SessionStore
from manager.types import SessionStatus, TextComplete, TextDelta, TurnComplete

SID = "local-1"


def _watcher() -> MagicMock:
    ws = MagicMock()
    ws.client_state = WebSocketState.CONNECTED
    ws.send_bytes = AsyncMock()
    return ws


def _frames(ws: MagicMock, kind: str | None = None) -> list[dict]:
    out = [orjson.loads(c.args[0]) for c in ws.send_bytes.call_args_list]
    return [f for f in out if kind is None or f["type"] == kind]


def _session(send) -> MagicMock:
    sm = MagicMock()
    sm.local_id = SID
    sm.sdk_session_id = "sdk-1"
    sm.provider_name = "claude"
    sm.status = SessionStatus.IDLE
    sm.subprocess_pid = None
    sm._receive_loop_done = asyncio.Event()
    sm.send = send
    sm.interrupt = AsyncMock()
    return sm


def _pool(sm) -> tuple[SessionPool, MagicMock]:
    pool = SessionPool()
    pool._sessions[SID] = sm
    pool._subscribers[SID] = set()
    pool._locks[SID] = asyncio.Lock()
    pool._pending_prompts[SID] = deque()
    pool._pending_locks[SID] = asyncio.Lock()
    ws = _watcher()
    pool.watch(ws)
    return pool, ws


async def _settle(pool: SessionPool) -> None:
    """Let the fire-and-forget ``agent_turn_finished`` emissions run."""
    for _ in range(5):
        await asyncio.sleep(0)
        if pool._announce_tasks:
            await asyncio.gather(*list(pool._announce_tasks))


def _events(*evs):
    async def _send(_text):
        for ev in evs:
            yield ev
    return _send


async def _drain(pool: SessionPool, text: str = "hi") -> None:
    async for _ in pool.send(SID, text):
        pass


@pytest.mark.asyncio
async def test_finished_turn_announces_started_then_finished_ok():
    sm = _session(_events(
        TextDelta(text="Done"),
        TextComplete(text="## Done\n\nAll **12** tests pass.\n- `pytest` green"),
        TurnComplete(cost=0.01, num_turns=1, session_id="sdk-1"),
    ))
    pool, ws = _pool(sm)
    pool.title_resolver = lambda sdk: {"sdk-1": "Energy dashboard"}.get(sdk)

    await _drain(pool)
    await _settle(pool)

    types = [f["type"] for f in _frames(ws)]
    assert types == ["agent_turn_started", "agent_turn_finished"]
    started, finished = _frames(ws)
    assert started == {"type": "agent_turn_started", "session_id": SID, "sdk_session_id": "sdk-1", "provider": "claude"}
    assert finished["session_id"] == SID
    assert finished["sdk_session_id"] == "sdk-1"
    assert finished["provider"] == "claude"
    assert finished["title"] == "Energy dashboard"
    assert finished["status"] == "ok"
    assert finished["preview"] == "Done All 12 tests pass. pytest green"
    assert finished["error"] is None


@pytest.mark.asyncio
async def test_result_text_is_the_preview_when_there_is_no_text_complete():
    sm = _session(_events(TurnComplete(cost=0.0, num_turns=1, session_id="sdk-1", result="Short answer")))
    pool, ws = _pool(sm)
    await _drain(pool)
    await _settle(pool)
    (finished,) = _frames(ws, "agent_turn_finished")
    assert finished["preview"] == "Short answer"
    assert finished["title"] is None  # no resolver wired: clients fall back to their list


@pytest.mark.asyncio
async def test_error_turn_complete_is_announced_as_error():
    sm = _session(_events(TurnComplete(cost=0.0, num_turns=1, session_id="sdk-1", is_error=True, result="Credit balance is too low")))
    pool, ws = _pool(sm)
    await _drain(pool)
    await _settle(pool)
    (finished,) = _frames(ws, "agent_turn_finished")
    assert finished["status"] == "error"
    assert finished["error"] == "Credit balance is too low"


@pytest.mark.asyncio
async def test_exception_mid_turn_is_announced_as_error_once():
    async def _send(_text):
        yield TextComplete(text="Working on it")
        raise RuntimeError("subprocess died")

    pool, ws = _pool(_session(_send))
    with pytest.raises(RuntimeError):
        await _drain(pool)
    await _settle(pool)
    (finished,) = _frames(ws, "agent_turn_finished")
    assert finished["status"] == "error"
    assert finished["error"] == "subprocess died"
    assert finished["preview"] == "Working on it"


@pytest.mark.asyncio
async def test_interrupt_then_sdk_turn_complete_is_interrupted():
    """The SDK still emits a TurnComplete after an interrupt: it must not read as a finish."""
    gate = asyncio.Event()

    async def _send(_text):
        yield TextDelta(text="partial")
        await gate.wait()
        yield TurnComplete(cost=0.0, num_turns=1, session_id="sdk-1", is_error=True, result="interrupted")

    pool, ws = _pool(_session(_send))
    task = asyncio.create_task(_drain(pool))
    await asyncio.sleep(0.01)
    await pool.interrupt(SID)
    gate.set()
    await task
    await _settle(pool)
    (finished,) = _frames(ws, "agent_turn_finished")
    assert finished["status"] == "interrupted"


@pytest.mark.asyncio
async def test_cancel_turn_mid_stream_is_interrupted():
    started = asyncio.Event()

    async def _send(_text):
        yield TextDelta(text="partial")
        started.set()
        await asyncio.Event().wait()  # never finishes on its own
        yield TurnComplete(cost=0.0, num_turns=1, session_id="sdk-1")

    pool, ws = _pool(_session(_send))
    await pool.start_turn(SID, "go")
    await started.wait()
    assert await pool.cancel_turn(SID) is True
    await _settle(pool)
    (finished,) = _frames(ws, "agent_turn_finished")
    assert finished["status"] == "interrupted"


@pytest.mark.asyncio
async def test_next_turn_after_an_interrupt_finishes_normally():
    pool, ws = _pool(_session(_events(TurnComplete(cost=0.0, num_turns=1, session_id="sdk-1"))))
    await pool.interrupt(SID)  # e.g. a Stop with nothing running
    await _drain(pool)
    await _settle(pool)
    (finished,) = _frames(ws, "agent_turn_finished")
    assert finished["status"] == "ok"


@pytest.mark.asyncio
async def test_abandoned_twice_announces_one_error():
    calls = {"n": 0}

    async def _send(_text):
        calls["n"] += 1
        raise SessionAbandoned(0.5)
        yield  # pragma: no cover — makes this an async generator

    pool, ws = _pool(_session(_send))
    await pool.start_turn(SID, "go")
    await pool._turn_tasks[SID]
    await _settle(pool)
    assert calls["n"] == 2
    assert len(_frames(ws, "agent_turn_started")) == 2  # the retry is a new send
    (finished,) = _frames(ws, "agent_turn_finished")
    assert finished["status"] == "error"
    assert "did not respond" in finished["error"]


@pytest.mark.asyncio
async def test_abandoned_once_then_success_announces_ok_only():
    calls = {"n": 0}

    async def _send(_text):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SessionAbandoned(0.5)
        yield TextComplete(text="Recovered")
        yield TurnComplete(cost=0.0, num_turns=1, session_id="sdk-1")

    pool, ws = _pool(_session(_send))
    await pool.start_turn(SID, "go")
    await pool._turn_tasks[SID]
    await _settle(pool)
    (finished,) = _frames(ws, "agent_turn_finished")
    assert finished["status"] == "ok"
    assert finished["preview"] == "Recovered"


@pytest.mark.asyncio
async def test_a_failing_title_lookup_still_announces():
    def _boom(_sdk):
        raise OSError("disk")

    pool, ws = _pool(_session(_events(TurnComplete(cost=0.0, num_turns=1, session_id="sdk-1"))))
    pool.title_resolver = _boom
    await _drain(pool)
    await _settle(pool)
    (finished,) = _frames(ws, "agent_turn_finished")
    assert finished["title"] is None
    assert finished["status"] == "ok"


async def _abandon_then_hang(calls: dict):
    calls["n"] += 1
    if calls["n"] == 1:
        raise SessionAbandoned(0.5)
    await asyncio.Event().wait()  # pragma: no cover — the gap is cancelled first
    yield TurnComplete(cost=0.0, num_turns=1, session_id="sdk-1")


async def _wait_for(cond, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        assert asyncio.get_running_loop().time() < deadline, "condition never became true"
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_cancel_between_abandoned_attempt_and_retry_announces_interrupted():
    """No send() runs during the retry pause: the cancellation must still end the turn."""
    calls = {"n": 0}
    sm = _session(lambda _t: _abandon_then_hang(calls))
    pool, ws = _pool(sm)
    await pool.start_turn(SID, "go")
    await _wait_for(lambda: sm.interrupt.await_count == 1)  # in the 1 s gap now
    task = pool._turn_tasks[SID]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _settle(pool)
    assert calls["n"] == 1
    (finished,) = _frames(ws, "agent_turn_finished")
    assert finished["status"] == "interrupted"


@pytest.mark.asyncio
async def test_runner_cancel_between_abandoned_attempt_and_retry_announces_interrupted():
    from orchestrator.runner import BackgroundAgentRunner, NotificationQueue

    calls = {"n": 0}
    sm = _session(lambda _t: _abandon_then_hang(calls))
    pool, ws = _pool(sm)
    runner = BackgroundAgentRunner(pool, MagicMock(), NotificationQueue())
    handle = await runner.spawn(SID, "go")
    await _wait_for(lambda: sm.interrupt.await_count == 1)
    assert await runner.cancel(handle.turn_id) is True
    await _wait_for(lambda: runner.notifications.has_pending())
    await _settle(pool)
    assert calls["n"] == 1
    (finished,) = _frames(ws, "agent_turn_finished")
    assert finished["status"] == "interrupted"


@pytest.mark.asyncio
async def test_a_stalled_watcher_never_delays_the_turn():
    """The emissions run as their own tasks: the turn finishes while a watcher hangs."""
    sm = _session(_events(TurnComplete(cost=0.0, num_turns=1, session_id="sdk-1")))
    pool, ws = _pool(sm)
    stuck = _watcher()
    stuck.send_bytes = AsyncMock(side_effect=lambda _d: asyncio.Event().wait())
    pool.watch(stuck)
    await asyncio.wait_for(_drain(pool), timeout=1.0)
    for t in list(pool._announce_tasks):
        t.cancel()


def test_turn_preview():
    assert turn_preview(None) is None
    assert turn_preview("   \n ") is None
    assert turn_preview("> quoted\n\n# Title\n* one\n* two") == "quoted Title one two"
    long = turn_preview("word " * 100)
    assert long is not None and len(long) == 200 and long.endswith("…")


def test_store_session_title_prefers_the_custom_title(tmp_path):
    (tmp_path / "context").mkdir()  # keeps the store off the real context/
    store = SessionStore(tmp_path)
    store.set_title("sdk-1", "Renamed session")
    assert store.session_title("sdk-1") == "Renamed session"
    assert store.session_title("missing") is None
