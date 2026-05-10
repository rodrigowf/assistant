"""Tests for SessionPool.start_turn / cancel_turn — the session-owned
turn API that decouples turn lifetime from any single WebSocket.

These exercise the REAL ``SessionPool`` against a stub ``SessionManager``
so we cover the actual ``_drive_turn`` body (lock acquisition, broadcast
fan-out, error handling) — not the mock fixture used by ``test_api_chat``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from api.pool import SessionPool
from manager.session import SessionAbandoned
from manager.types import (
    SessionStatus,
    TextDelta,
    TextComplete,
    TurnComplete,
)


def _stub_session_manager(events: list, *, abandon_first: bool = False):
    """Create a stub SessionManager whose .send() yields *events*.

    If ``abandon_first`` is True, the first invocation raises
    ``SessionAbandoned`` (without yielding anything); subsequent calls
    behave normally.  Used to exercise the retry-once path.
    """
    sm = MagicMock()
    sm.local_id = "test-session"
    sm.session_id = "test-session"
    sm.sdk_session_id = "sdk-test"
    sm.status = SessionStatus.IDLE
    sm.subprocess_pid = None
    sm.is_active = True
    sm.pending_permission_ids = MagicMock(return_value=[])

    call_count = {"n": 0}

    async def _send(text):
        call_count["n"] += 1
        if abandon_first and call_count["n"] == 1:
            raise SessionAbandoned(0.5)
        for ev in events:
            yield ev

    sm.send = _send

    async def _interrupt():
        return None

    sm.interrupt = _interrupt
    return sm


def _install(pool: SessionPool, sm) -> None:
    """Inject a session into the pool without going through pool.create()
    (which would spawn a real SDK subprocess).  Mimics what create() does
    for bookkeeping minus the SessionManager.start() call."""
    sid = sm.local_id
    pool._sessions[sid] = sm
    pool._subscribers[sid] = set()
    pool._locks[sid] = asyncio.Lock()


@pytest.mark.asyncio
async def test_start_turn_returns_immediately():
    """start_turn must not block on the turn — it spawns a task and returns."""
    pool = SessionPool()
    sm = _stub_session_manager([
        TextDelta(text="hi"),
        TurnComplete(cost=0.0, num_turns=1, session_id="sdk-test"),
    ])
    _install(pool, sm)

    started = asyncio.get_event_loop().time()
    await pool.start_turn("test-session", "hello")
    elapsed = asyncio.get_event_loop().time() - started

    # Should be near-instant (microseconds).  Generous threshold to avoid
    # CI flakes; the turn itself has no awaits but task spawn isn't free.
    assert elapsed < 0.05, f"start_turn took {elapsed*1000:.1f}ms — should be instant"
    assert pool.has_active_turn("test-session") is True

    # Drain so the task finishes cleanly.
    await asyncio.sleep(0.1)
    assert pool.has_active_turn("test-session") is False


@pytest.mark.asyncio
async def test_start_turn_replaces_in_flight_turn():
    """A second start_turn while one is in flight cancels the first.

    This is the 'interrupt + new' semantics chat.py wants: a fresh prompt
    supersedes a still-running one.  The bundled CLI doesn't accept
    overlapping queries anyway, so this is the only sensible behavior.
    """
    pool = SessionPool()
    started_first = asyncio.Event()
    release_first = asyncio.Event()
    first_finished_naturally = asyncio.Event()

    async def _slow_send(text):
        if text == "first":
            started_first.set()
            await release_first.wait()  # wedge until released
            first_finished_naturally.set()  # only reaches here if NOT cancelled
            yield TurnComplete(cost=0.0, num_turns=1, session_id="sdk-test")
        else:
            yield TextDelta(text="second-output")
            yield TurnComplete(cost=0.0, num_turns=1, session_id="sdk-test")

    sm = _stub_session_manager([])
    sm.send = _slow_send
    _install(pool, sm)

    await pool.start_turn("test-session", "first")
    await started_first.wait()  # ensure the first turn entered _drive

    # Now fire a second turn — should cancel the first.
    await pool.start_turn("test-session", "second")

    # Allow the second turn to complete.
    await asyncio.sleep(0.1)

    # First was cancelled, not allowed to finish naturally.
    assert not first_finished_naturally.is_set(), \
        "first turn should have been cancelled, not allowed to finish"
    # Second turn completed.
    assert pool.has_active_turn("test-session") is False


@pytest.mark.asyncio
async def test_cancel_turn_with_no_active_turn_is_noop():
    """cancel_turn returns False when no turn is in flight."""
    pool = SessionPool()
    sm = _stub_session_manager([])
    _install(pool, sm)

    cancelled = await pool.cancel_turn("test-session")
    assert cancelled is False


@pytest.mark.asyncio
async def test_cancel_turn_awaits_task():
    """cancel_turn must not return until the turn task has fully unwound."""
    pool = SessionPool()
    in_finally = asyncio.Event()
    release = asyncio.Event()

    async def _send(text):
        try:
            await release.wait()
        finally:
            in_finally.set()
        yield TurnComplete(cost=0.0, num_turns=1, session_id="sdk-test")

    sm = _stub_session_manager([])
    sm.send = _send
    _install(pool, sm)

    await pool.start_turn("test-session", "go")
    # Give the task time to enter the wait.
    await asyncio.sleep(0.01)
    # cancel_turn should cancel the task (which triggers its finally) and
    # await its unwind.  By the time it returns, in_finally has fired.
    await pool.cancel_turn("test-session")
    assert in_finally.is_set()
    assert pool.has_active_turn("test-session") is False


@pytest.mark.asyncio
async def test_drive_turn_broadcasts_to_subscribers():
    """Events from the turn task fan out to current subscribers.

    The originating WS (passed as source_ws) is excluded from the
    user_message broadcast, but receives all subsequent events.
    """
    pool = SessionPool()
    sm = _stub_session_manager([
        TextDelta(text="hello"),
        TextComplete(text="hello"),
        TurnComplete(cost=0.01, num_turns=1, session_id="sdk-test"),
    ])
    _install(pool, sm)

    # Capture broadcast payloads — patch _broadcast_session.
    received: list[tuple[str | None, dict]] = []

    original = pool._broadcast_session

    async def _record(sid, payload, *, exclude=None):
        # Simplified: just record (exclude_marker, payload).  We pass a
        # non-WS sentinel as exclude in start_turn (None in tests), so
        # check the marker by identity.
        marker = "excluded" if exclude is not None else "broadcast"
        received.append((marker, payload))
        # Don't actually try to send — there are no real WSes here.
        return None

    pool._broadcast_session = _record

    try:
        await pool.start_turn("test-session", "hi", source_ws=None)
        # Wait for the turn to complete.
        deadline = asyncio.get_event_loop().time() + 1.0
        while pool.has_active_turn("test-session"):
            if asyncio.get_event_loop().time() > deadline:
                pytest.fail("turn did not complete within 1s")
            await asyncio.sleep(0.01)
    finally:
        pool._broadcast_session = original

    # We expect at least: user_message, text_delta, text_complete, turn_complete.
    types_seen = [p["type"] for _, p in received]
    assert "user_message" in types_seen
    assert "text_delta" in types_seen
    assert "text_complete" in types_seen
    assert "turn_complete" in types_seen


@pytest.mark.asyncio
async def test_drive_turn_retries_on_session_abandoned():
    """If sm.send raises SessionAbandoned, the driver retries once."""
    pool = SessionPool()
    sm = _stub_session_manager(
        [
            TextDelta(text="recovered"),
            TurnComplete(cost=0.0, num_turns=1, session_id="sdk-test"),
        ],
        abandon_first=True,
    )
    _install(pool, sm)

    received: list[dict] = []

    async def _record(sid, payload, *, exclude=None):
        received.append(payload)

    pool._broadcast_session = _record

    await pool.start_turn("test-session", "hi")
    # Wait for the retry path: abandon → 1s sleep → retry.
    deadline = asyncio.get_event_loop().time() + 3.0
    while pool.has_active_turn("test-session"):
        if asyncio.get_event_loop().time() > deadline:
            pytest.fail("turn did not complete within 3s (retry path stuck)")
        await asyncio.sleep(0.05)

    types_seen = [p["type"] for p in received]
    # Should see the retrying status and then the recovered turn's events.
    assert "status" in types_seen
    retrying = [p for p in received if p.get("type") == "status"]
    assert any(p.get("status") == "retrying" for p in retrying)
    assert "text_delta" in types_seen
    assert "turn_complete" in types_seen


@pytest.mark.asyncio
async def test_start_turn_unknown_session_raises():
    pool = SessionPool()
    with pytest.raises(ValueError, match="No session"):
        await pool.start_turn("nope", "hello")
