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
from manager.base_session import SessionDeadError
from manager.claude.session import SessionAbandoned
from manager.types import (
    SessionStatus,
    TerminationReason,
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
    # Real (unset) Event so the dead-session reaper's is_set() check
    # correctly classifies the stub as healthy.  Without this, MagicMock
    # auto-spawns a truthy attribute that the reaper would mistake for
    # a terminated receive loop.
    _alive_event = asyncio.Event()
    sm._receive_loop_done = _alive_event

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


# ---------------------------------------------------------------------------
# Resume protocol — pool-level helpers (replay_for_subscriber, _wrap_payload,
# resume_state_for) match the SessionManager-level invariants tested in
# test_session.py::TestSessionManagerResumeProtocol.  These tests exercise
# the pool's translation of those invariants into the wire payloads sent
# to the WebSocket.
# ---------------------------------------------------------------------------


def _resume_stub_session_manager(stream_id: str | None, ring: list[tuple[int, object]]):
    """Stub SessionManager with the resume-protocol surface filled in.

    The pool's resume helpers read ``stream_id``, ``_next_seq``, and
    ``replay_after``; everything else is irrelevant for these tests.
    """
    sm = MagicMock()
    sm.local_id = "rsp-session"
    sm.session_id = "rsp-session"
    sm.sdk_session_id = "sdk-rsp"
    sm.status = SessionStatus.IDLE
    sm.subprocess_pid = None
    sm.is_active = True
    sm.stream_id = stream_id
    # Pool reads ``_next_seq`` via ``getattr`` for ``resume_state_for``.
    sm._next_seq = (ring[-1][0] + 1) if ring else 0
    sm.last_yielded_seq = None

    def _replay_after(client_stream_id, after_seq):
        if client_stream_id is None or after_seq is None:
            return "ok", []
        if stream_id is None:
            return "ok", []
        if client_stream_id != stream_id:
            return "mismatch", []
        if not ring:
            if after_seq >= sm._next_seq:
                return "mismatch", []
            return "ok", []
        oldest = ring[0][0]
        latest = ring[-1][0]
        if after_seq >= latest:
            return "ok", []
        if after_seq < oldest - 1:
            return "overflow", []
        return "ok", [(s, e) for s, e in ring if s > after_seq]

    sm.replay_after = _replay_after
    return sm


def test_resume_state_for_returns_none_when_session_missing():
    pool = SessionPool()
    assert pool.resume_state_for("does-not-exist") is None


def test_resume_state_for_returns_stream_id_and_next_seq():
    pool = SessionPool()
    sm = _resume_stub_session_manager(
        stream_id="rsp-session:1700000000000",
        ring=[
            (0, TextDelta(text="a")),
            (1, TextDelta(text="b")),
        ],
    )
    _install(pool, sm)

    state = pool.resume_state_for(sm.local_id)
    assert state == {
        "stream_id": "rsp-session:1700000000000",
        "next_seq": 2,
    }


def test_resume_state_for_none_when_provider_does_not_support_protocol():
    """Providers that haven't implemented ``stream_id`` (Qwen, Gemini) get
    ``None`` — frontend treats those sessions as non-resumable and falls
    back to REST refetch automatically.
    """
    pool = SessionPool()
    sm = _resume_stub_session_manager(stream_id=None, ring=[])
    _install(pool, sm)
    assert pool.resume_state_for(sm.local_id) is None


def test_replay_for_subscriber_no_checkpoint_returns_ok_empty():
    pool = SessionPool()
    sm = _resume_stub_session_manager(
        stream_id="rsp:1", ring=[(0, TextDelta(text="x"))],
    )
    _install(pool, sm)

    status, payloads = pool.replay_for_subscriber(sm.local_id, None)
    assert status == "ok"
    assert payloads == []


def test_replay_for_subscriber_replays_missed_events_in_order():
    """Subscriber's checkpoint matches the live stream and references a
    seq older than the head — they get every newer event, in order,
    each carrying its assigned seq + stream_id in the wire payload.
    """
    pool = SessionPool()
    ring = [
        (5, TextDelta(text="alpha")),
        (6, TextDelta(text="beta")),
        (7, TextDelta(text="gamma")),
    ]
    sm = _resume_stub_session_manager(stream_id="rsp:1700", ring=ring)
    _install(pool, sm)

    status, payloads = pool.replay_for_subscriber(
        sm.local_id, {"stream_id": "rsp:1700", "seq": 5},
    )
    assert status == "ok"
    assert len(payloads) == 2
    seqs = [p["seq"] for p in payloads]
    assert seqs == [6, 7]
    assert all(p["stream_id"] == "rsp:1700" for p in payloads)
    # Wire payload carries the serialized event content too — the
    # frontend dispatches it through the same reducer as live events.
    assert payloads[0]["type"] == "text_delta"
    assert payloads[0]["text"] == "beta"


def test_replay_for_subscriber_mismatch_status_for_stale_stream_id():
    pool = SessionPool()
    sm = _resume_stub_session_manager(stream_id="rsp:NEW", ring=[(0, TextDelta(text="x"))])
    _install(pool, sm)

    status, payloads = pool.replay_for_subscriber(
        sm.local_id, {"stream_id": "rsp:OLD", "seq": 0},
    )
    assert status == "mismatch"
    assert payloads == []


def test_replay_for_subscriber_overflow_status_when_seq_too_old():
    pool = SessionPool()
    # Ring's oldest seq is 100; subscriber's checkpoint is 0 — long gone.
    ring = [(i, TextDelta(text=f"t{i}")) for i in range(100, 105)]
    sm = _resume_stub_session_manager(stream_id="rsp:1", ring=ring)
    _install(pool, sm)

    status, payloads = pool.replay_for_subscriber(
        sm.local_id, {"stream_id": "rsp:1", "seq": 0},
    )
    assert status == "overflow"
    assert payloads == []


def test_replay_for_subscriber_malformed_handshake_treated_as_no_checkpoint():
    """Belt-and-braces: a malformed resume_from dict (wrong types, missing
    keys) doesn't crash — it just behaves as if no checkpoint was sent.
    """
    pool = SessionPool()
    sm = _resume_stub_session_manager(stream_id="rsp:1", ring=[(0, TextDelta(text="x"))])
    _install(pool, sm)

    for bad in (
        {},
        {"stream_id": 42, "seq": 0},
        {"stream_id": "ok", "seq": "not-an-int"},
        {"only_seq": 5},
    ):
        status, payloads = pool.replay_for_subscriber(sm.local_id, bad)
        assert status == "ok"
        assert payloads == []


def test_replay_for_subscriber_unsupported_for_provider_without_protocol():
    """Providers (Qwen, Gemini) that haven't implemented ``replay_after``
    return ``unsupported`` — the route layer treats this the same as
    ``ok`` with no replay, preserving old behaviour for those providers.
    """
    pool = SessionPool()
    sm = MagicMock()
    sm.local_id = "qwen-session"
    sm.sdk_session_id = "sdk"
    sm.stream_id = None
    sm.status = SessionStatus.IDLE
    # Crucially: no replay_after attribute.
    del sm.replay_after
    _install(pool, sm)

    status, payloads = pool.replay_for_subscriber(
        sm.local_id, {"stream_id": "x", "seq": 0},
    )
    assert status == "unsupported"
    assert payloads == []


def test_wrap_payload_stamps_seq_and_stream_id_when_available():
    pool = SessionPool()
    sm = MagicMock()
    sm.stream_id = "rsp:42"
    sm.last_yielded_seq = 17

    out = pool._wrap_payload(sm, {"type": "text_delta", "text": "x"})
    assert out["seq"] == 17
    assert out["stream_id"] == "rsp:42"
    assert out["type"] == "text_delta"


def test_wrap_payload_no_op_when_session_lacks_protocol():
    """Qwen-style provider with no stream_id: payload passes through
    unchanged so the frontend treats it as a non-resumable broadcast.
    """
    pool = SessionPool()
    sm = MagicMock()
    sm.stream_id = None
    sm.last_yielded_seq = None
    payload = {"type": "text_delta", "text": "x"}
    out = pool._wrap_payload(sm, payload)
    # ``setdefault`` would not inject None either, but we want to be
    # explicit: no seq/stream_id at all on the wire.
    assert "seq" not in out
    assert "stream_id" not in out
    assert out is payload  # same object, no copy


# ---------------------------------------------------------------------------
# Session death recovery
#
# Symmetric to the resume-protocol tests above: the pool detects when a
# SessionManager's receive loop has exited (SSH transport closed,
# subprocess crashed, fatal SDK error) and propagates the typed
# termination reason both to WebSocket subscribers and to any in-flight
# ``send()``.
# ---------------------------------------------------------------------------


def _dead_session_manager(
    *,
    reason: TerminationReason = TerminationReason.SUBPROCESS_CRASHED,
    detail: str = "Command failed with exit code 255",
):
    """Stub SessionManager whose receive loop is already marked done.

    Mirrors the post-crash state of a real Claude session: the
    ``_receive_loop_done`` event is set and the termination metadata
    fields are populated.
    """
    sm = MagicMock()
    sm.local_id = "dead-session"
    sm.session_id = "dead-session"
    sm.sdk_session_id = "sdk-dead"
    sm.status = SessionStatus.DISCONNECTED
    sm.subprocess_pid = None
    sm.is_active = False
    sm.stream_id = None
    sm.last_yielded_seq = None
    sm.pending_permission_ids = MagicMock(return_value=[])

    # _receive_loop_done is a real asyncio.Event so the reaper's
    # is_set() check sees the truthy value MagicMock would obscure.
    done_event = asyncio.Event()
    done_event.set()
    sm._receive_loop_done = done_event

    sm._termination_reason = reason
    sm._termination_detail = detail

    async def _stop():
        return None
    sm.stop = _stop

    async def _interrupt():
        return None
    sm.interrupt = _interrupt

    sm.set_pid_callbacks = MagicMock()
    return sm


@pytest.mark.asyncio
async def test_dead_session_reaper_evicts_session_with_done_receive_loop():
    """A session whose ``_receive_loop_done`` is set gets pulled through
    ``close()`` on the next reaper sweep, with the typed reason it flagged.
    """
    pool = SessionPool()
    sm = _dead_session_manager(
        reason=TerminationReason.SUBPROCESS_CRASHED,
        detail="exit code 255",
    )
    _install(pool, sm)

    assert sm.local_id in pool._sessions
    await pool._reap_dead_sessions_once()
    assert sm.local_id not in pool._sessions


@pytest.mark.asyncio
async def test_dead_session_reaper_skips_healthy_sessions():
    """A session whose receive loop is still running is left alone."""
    pool = SessionPool()
    sm = _stub_session_manager([
        TurnComplete(cost=0.0, num_turns=1, session_id="sdk-test"),
    ])
    # Healthy: no _receive_loop_done attribute at all (the legacy
    # MagicMock from _stub_session_manager doesn't add it).  Also
    # covers the defensive ``getattr(..., None)`` path.
    _install(pool, sm)

    await pool._reap_dead_sessions_once()

    assert sm.local_id in pool._sessions


@pytest.mark.asyncio
async def test_close_with_reason_broadcasts_typed_session_terminated():
    """``close(reason=...)`` emits ``session_terminated`` BEFORE the
    legacy ``session_stopped``, so older clients still get the latter
    while new clients can act on the former.
    """
    pool = SessionPool()
    sm = _dead_session_manager(
        reason=TerminationReason.SUBPROCESS_CRASHED,
        detail="exit code 255",
    )
    _install(pool, sm)

    received: list[dict] = []
    ws = MagicMock()
    ws.client_state = __import__(
        "starlette.websockets", fromlist=["WebSocketState"],
    ).WebSocketState.CONNECTED

    async def _capture(data):
        import orjson as _orjson
        received.append(_orjson.loads(data))
    ws.send_bytes = _capture

    pool._subscribers[sm.local_id].add(ws)

    await pool.close(
        sm.local_id,
        reason=TerminationReason.SUBPROCESS_CRASHED,
        detail="exit code 255",
    )

    types_in_order = [m.get("type") for m in received]
    assert types_in_order == ["session_terminated", "session_stopped"]
    terminated = received[0]
    assert terminated["reason"] == "subprocess_crashed"
    assert terminated["detail"] == "exit code 255"
    assert terminated["sdk_session_id"] == "sdk-dead"


@pytest.mark.asyncio
async def test_close_without_reason_preserves_legacy_envelope_only():
    """Backwards-compat: callers that don't pass a reason still get
    exactly the old behaviour — ``session_stopped`` alone.
    """
    pool = SessionPool()
    sm = _dead_session_manager()
    _install(pool, sm)

    received: list[dict] = []
    ws = MagicMock()
    ws.client_state = __import__(
        "starlette.websockets", fromlist=["WebSocketState"],
    ).WebSocketState.CONNECTED

    async def _capture(data):
        import orjson as _orjson
        received.append(_orjson.loads(data))
    ws.send_bytes = _capture

    pool._subscribers[sm.local_id].add(ws)

    await pool.close(sm.local_id)

    assert [m.get("type") for m in received] == ["session_stopped"]


def _dead_send_session_manager(
    *,
    reason: TerminationReason = TerminationReason.SUBPROCESS_CRASHED,
    detail: str = "exit code 255",
):
    """Stub where ``send()`` raises ``SessionDeadError`` — modelling
    the post-receive-loop-crash state where the client tries to send
    one more message before the reaper has caught up.
    """
    sm = _dead_session_manager(reason=reason, detail=detail)
    # Override send to raise the typed error.
    async def _send(text):
        raise SessionDeadError(reason=reason, detail=detail)
        # Keep the function an async generator for type compat:
        if False:  # pragma: no cover
            yield
    sm.send = _send
    return sm


@pytest.mark.asyncio
async def test_drive_turn_catches_session_dead_error_and_closes_with_typed_event():
    """When the in-flight ``send()`` raises ``SessionDeadError``,
    ``_drive_turn`` does NOT broadcast the generic ``send_failed``.
    Instead it closes the session with the typed reason so the client
    receives ``session_terminated`` and can recover.
    """
    pool = SessionPool()
    sm = _dead_send_session_manager(
        reason=TerminationReason.SUBPROCESS_CRASHED,
        detail="exit code 255 — ssh closed",
    )
    _install(pool, sm)

    received: list[dict] = []
    ws = MagicMock()
    ws.client_state = __import__(
        "starlette.websockets", fromlist=["WebSocketState"],
    ).WebSocketState.CONNECTED

    async def _capture(data):
        import orjson as _orjson
        received.append(_orjson.loads(data))
    ws.send_bytes = _capture

    pool._subscribers[sm.local_id].add(ws)

    await pool.start_turn(sm.local_id, "hi")
    # Allow the spawned turn task to run + close path to drain.
    for _ in range(20):
        if sm.local_id not in pool._sessions:
            break
        await asyncio.sleep(0.01)

    assert sm.local_id not in pool._sessions, "dead session should be evicted"

    types_seen = [m.get("type") for m in received]
    assert "session_terminated" in types_seen, types_seen
    assert "send_failed" not in [m.get("error") for m in received]

    terminated = next(m for m in received if m["type"] == "session_terminated")
    assert terminated["reason"] == "subprocess_crashed"
    assert "ssh closed" in (terminated.get("detail") or "")
