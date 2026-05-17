"""Tests for api/routes/chat.py — WebSocket chat endpoint."""

from unittest.mock import AsyncMock, MagicMock, patch

import orjson
import pytest
from starlette.testclient import TestClient

from api.app import create_app
from manager.types import TextComplete, TextDelta, TurnComplete


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_session_manager(session_id="test-123", events=None):
    """Create a mock SessionManager that yields given events on send()."""
    sm = MagicMock()
    sm.start = AsyncMock(return_value=session_id)
    sm.stop = AsyncMock()
    sm.interrupt = AsyncMock()
    sm.session_id = session_id
    sm.local_id = session_id
    sm.sdk_session_id = "sdk-" + session_id

    send_events = events or [
        TextDelta(text="Hello"),
        TextComplete(text="Hello world"),
        TurnComplete(cost=0.01, num_turns=1, session_id="sdk-" + session_id),
    ]

    async def _send(text):
        for event in send_events:
            yield event

    sm.send = _send

    async def _command(text):
        for event in send_events:
            yield event

    sm.command = _command

    # Used by chat.py to forward typed-text-as-permission-deny.  Default to
    # "no pending permissions" so the typical send path doesn't try to
    # resolve anything.
    sm.pending_permission_ids = MagicMock(return_value=[])

    return sm


def _make_pool(mock_sm, session_id="test-123"):
    """Create a mock SessionPool that wraps a mock SessionManager.

    The mock pool broadcasts serialized events to all subscribers (like the
    real pool).  start_turn spawns a session-owned task; cancel_turn awaits
    it.  This mirrors the real pool's behavior and lets tests exercise the
    "WS becomes a pure observer" contract.
    """
    import asyncio
    from api.serializers import serialize_event

    pool = MagicMock()
    subscribers: dict[str, set] = {}
    turn_tasks: dict[str, asyncio.Task] = {}

    pool.has = MagicMock(return_value=False)
    pool.create = AsyncMock(return_value=session_id)
    pool.get = MagicMock(return_value=mock_sm)
    pool.interrupt = AsyncMock()
    pool.resolve_session_permission = AsyncMock(return_value=True)

    def _subscribe(sid, ws):
        subscribers.setdefault(sid, set()).add(ws)
    pool.subscribe = MagicMock(side_effect=_subscribe)

    def _unsubscribe(sid, ws):
        if sid in subscribers:
            subscribers[sid].discard(ws)
    pool.unsubscribe = MagicMock(side_effect=_unsubscribe)

    async def _broadcast(sid, payload):
        # Snapshot to tolerate concurrent sub/unsub during iteration.
        for ws in tuple(subscribers.get(sid, set())):
            try:
                await ws.send_bytes(orjson.dumps(payload))
            except Exception:
                pass

    async def _drive(sid, text, source_ws):
        async for event in mock_sm.send(text):
            payload = serialize_event(event)
            for ws in tuple(subscribers.get(sid, set())):
                if ws is source_ws and payload.get("type") == "user_message":
                    continue
                try:
                    await ws.send_bytes(orjson.dumps(payload))
                except Exception:
                    pass

    async def _cancel_turn(sid):
        task = turn_tasks.get(sid)
        if task is None or task.done():
            return False
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        return True
    pool.cancel_turn = AsyncMock(side_effect=_cancel_turn)

    async def _start_turn(sid, text, *, source_ws=None):
        await _cancel_turn(sid)
        turn_tasks[sid] = asyncio.create_task(_drive(sid, text, source_ws))
    pool.start_turn = AsyncMock(side_effect=_start_turn)

    pool.has_active_turn = MagicMock(
        side_effect=lambda sid: sid in turn_tasks and not turn_tasks[sid].done()
    )

    return pool


@pytest.fixture
def sync_client():
    app = create_app()
    with TestClient(app) as client:
        yield client


@pytest.fixture
def pool_client():
    """Client with a pre-configured mock pool."""
    app = create_app()
    mock_sm = _mock_session_manager()
    pool = _make_pool(mock_sm)
    with TestClient(app) as client:
        # Set mock pool AFTER lifespan starts (lifespan creates a real pool)
        app.state.pool = pool
        yield client, pool, mock_sm


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestWebSocketChat:
    def test_start_and_send(self, pool_client):
        client, pool, mock_sm = pool_client

        with client.websocket_connect("/api/sessions/chat") as ws:
            # Start session with a local_id
            ws.send_text(orjson.dumps({"type": "start", "local_id": "my-local-1"}).decode())
            # First response is "connecting" status
            resp = orjson.loads(ws.receive_bytes())
            assert resp["type"] == "status"
            assert resp["status"] == "connecting"
            # Then session_started with the local_id
            resp = orjson.loads(ws.receive_bytes())
            assert resp["type"] == "session_started"
            assert resp["session_id"] == "test-123"

            # Send message
            ws.send_text(orjson.dumps({"type": "send", "text": "Hi"}).decode())

            events = []
            for _ in range(3):
                events.append(orjson.loads(ws.receive_bytes()))

            assert events[0] == {"type": "text_delta", "text": "Hello"}
            assert events[1] == {"type": "text_complete", "text": "Hello world"}
            assert events[2]["type"] == "turn_complete"
            assert events[2]["cost"] == 0.01

            # Stop
            ws.send_text(orjson.dumps({"type": "stop"}).decode())
            resp = orjson.loads(ws.receive_bytes())
            assert resp["type"] == "session_stopped"

    def test_send_before_start(self, sync_client):
        with sync_client.websocket_connect("/api/sessions/chat") as ws:
            ws.send_text(orjson.dumps({"type": "send", "text": "Hi"}).decode())
            resp = orjson.loads(ws.receive_bytes())
            assert resp["type"] == "error"
            assert resp["error"] == "not_started"

    def test_interrupt(self, pool_client):
        client, pool, _ = pool_client

        with client.websocket_connect("/api/sessions/chat") as ws:
            ws.send_text(orjson.dumps({"type": "start"}).decode())
            ws.receive_bytes()  # connecting status
            ws.receive_bytes()  # session_started

            ws.send_text(orjson.dumps({"type": "interrupt"}).decode())
            resp = orjson.loads(ws.receive_bytes())
            assert resp["type"] == "status"
            assert resp["status"] == "interrupted"
            # Interrupt now goes through cancel_turn (which internally
            # sends the SDK interrupt and awaits the in-flight task).
            pool.cancel_turn.assert_awaited_with("test-123")

    def test_command(self, pool_client):
        client, pool, _ = pool_client

        with client.websocket_connect("/api/sessions/chat") as ws:
            ws.send_text(orjson.dumps({"type": "start"}).decode())
            ws.receive_bytes()  # connecting status
            ws.receive_bytes()  # session_started

            ws.send_text(orjson.dumps({"type": "command", "text": "/compact"}).decode())
            events = []
            for _ in range(3):
                events.append(orjson.loads(ws.receive_bytes()))
            assert events[0]["type"] == "text_delta"

    def test_invalid_json(self, sync_client):
        with sync_client.websocket_connect("/api/sessions/chat") as ws:
            ws.send_text("not json at all")
            resp = orjson.loads(ws.receive_bytes())
            assert resp["type"] == "error"
            assert resp["error"] == "invalid_json"

    def test_unknown_type(self, sync_client):
        with sync_client.websocket_connect("/api/sessions/chat") as ws:
            ws.send_text(orjson.dumps({"type": "bogus"}).decode())
            resp = orjson.loads(ws.receive_bytes())
            assert resp["type"] == "error"
            assert resp["error"] == "unknown_type"

    def test_start_failure(self, pool_client):
        client, pool, _ = pool_client
        pool.create = AsyncMock(side_effect=RuntimeError("connection failed"))

        with client.websocket_connect("/api/sessions/chat") as ws:
            ws.send_text(orjson.dumps({"type": "start"}).decode())
            # First we get "connecting" status
            resp = orjson.loads(ws.receive_bytes())
            assert resp["type"] == "status"
            assert resp["status"] == "connecting"
            # Then the error
            resp = orjson.loads(ws.receive_bytes())
            assert resp["type"] == "error"
            assert resp["error"] == "start_failed"

    def test_disconnect_does_not_cancel_turn(self, pool_client):
        """Page reload (WS disconnect) must NOT cancel the in-flight turn.

        This is the central invariant the session-owned turn refactor was
        meant to deliver: the turn lifetime belongs to the pool, not to
        the WebSocket that initiated it.  We verify by:
          1. Starting a turn whose iterator yields slowly.
          2. Disconnecting the WS *before* the turn finishes.
          3. Asserting the pool's start_turn task is still alive.
        """
        import asyncio
        import time
        from manager.types import TextDelta, TextComplete, TurnComplete

        # Slow event stream — 50ms gap so we can disconnect mid-turn.
        slow_events = [
            TextDelta(text="part1"),
            TextDelta(text="part2"),
            TextDelta(text="part3"),
            TextComplete(text="part1part2part3"),
            TurnComplete(cost=0.01, num_turns=1, session_id="sdk-test-123"),
        ]

        async def _slow_send(text):
            for ev in slow_events:
                await asyncio.sleep(0.05)
                yield ev

        client, pool, mock_sm = pool_client
        mock_sm.send = _slow_send

        with client.websocket_connect("/api/sessions/chat") as ws:
            ws.send_text(orjson.dumps({"type": "start", "local_id": "sticky-1"}).decode())
            ws.receive_bytes()  # connecting
            ws.receive_bytes()  # session_started
            ws.send_text(orjson.dumps({"type": "send", "text": "go"}).decode())
            # Receive only the first delta then bail — disconnect mid-turn.
            first = orjson.loads(ws.receive_bytes())
            assert first["type"] == "text_delta"
            assert first["text"] == "part1"
            # WS context manager exit will close the connection (= page reload).

        # Turn task must still be running after the WS closes.  We give the
        # pool's mock a brief moment to register the next polling tick.
        time.sleep(0.05)
        # has_active_turn returns True iff the spawned task hasn't finished.
        assert pool.has_active_turn("test-123") is True

        # Drain the turn so pytest doesn't leave a dangling task.  Wait
        # synchronously until the slow generator finishes.
        deadline = time.monotonic() + 2.0
        while pool.has_active_turn("test-123") and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pool.has_active_turn("test-123") is False, \
            "turn should complete within 2s of slow-event budget"

    def test_resume_session(self, pool_client):
        client, pool, _ = pool_client
        mock_sm = _mock_session_manager(session_id="local-456")
        pool.create = AsyncMock(return_value="local-456")
        pool.get = MagicMock(return_value=mock_sm)

        with client.websocket_connect("/api/sessions/chat") as ws:
            ws.send_text(orjson.dumps({
                "type": "start",
                "local_id": "local-456",
                "resume_sdk_id": "old-sdk-123",
                "fork": True,
            }).decode())
            # First we get "connecting" status
            resp = orjson.loads(ws.receive_bytes())
            assert resp["type"] == "status"
            assert resp["status"] == "connecting"
            # Then session_started with the local_id
            resp = orjson.loads(ws.receive_bytes())
            assert resp["type"] == "session_started"
            assert resp["session_id"] == "local-456"

            # Verify pool.create was called with local_id and resume args
            pool.create.assert_awaited_once()
            call_kwargs = pool.create.call_args
            assert call_kwargs.kwargs.get("local_id") == "local-456"
            assert call_kwargs.kwargs.get("resume_sdk_id") == "old-sdk-123"
            assert call_kwargs.kwargs.get("fork") is True
