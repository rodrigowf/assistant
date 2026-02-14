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

    send_events = events or [
        TextDelta(text="Hello"),
        TextComplete(text="Hello world"),
        TurnComplete(cost=0.01, num_turns=1, session_id=session_id),
    ]

    async def _send(text):
        for event in send_events:
            yield event

    sm.send = _send

    async def _command(text):
        for event in send_events:
            yield event

    sm.command = _command

    return sm


def _make_pool(mock_sm, session_id="test-123"):
    """Create a mock SessionPool that wraps a mock SessionManager.

    The mock pool broadcasts serialized events to all subscribers (like the
    real pool), so the test WS receives events via broadcast.
    """
    from api.serializers import serialize_event

    pool = MagicMock()
    subscribers: dict[str, set] = {}

    # has() returns False initially (no pre-existing session)
    pool.has = MagicMock(return_value=False)
    # create() returns the session_id and registers the SM
    pool.create = AsyncMock(return_value=session_id)
    # get() returns the SM after create
    pool.get = MagicMock(return_value=mock_sm)
    pool.interrupt = AsyncMock()

    def _subscribe(sid, ws):
        subscribers.setdefault(sid, set()).add(ws)
    pool.subscribe = MagicMock(side_effect=_subscribe)

    def _unsubscribe(sid, ws):
        if sid in subscribers:
            subscribers[sid].discard(ws)
    pool.unsubscribe = MagicMock(side_effect=_unsubscribe)

    # send() delegates to sm.send(), broadcasts to subscribers, and yields events
    async def _pool_send(sid, text, *, source_ws=None):
        async for event in mock_sm.send(text):
            payload = serialize_event(event)
            for ws in subscribers.get(sid, set()):
                await ws.send_bytes(orjson.dumps(payload))
            yield event

    pool.send = _pool_send

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
            # Start session
            ws.send_text(orjson.dumps({"type": "start"}).decode())
            # First response is "connecting" status
            resp = orjson.loads(ws.receive_bytes())
            assert resp["type"] == "status"
            assert resp["status"] == "connecting"
            # Then session_started
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
            pool.interrupt.assert_awaited_once_with("test-123")

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

    def test_resume_session(self, pool_client):
        client, pool, _ = pool_client
        mock_sm = _mock_session_manager(session_id="resumed-456")
        pool.create = AsyncMock(return_value="resumed-456")
        pool.get = MagicMock(return_value=mock_sm)

        with client.websocket_connect("/api/sessions/chat") as ws:
            ws.send_text(orjson.dumps({
                "type": "start", "session_id": "old-123", "fork": True,
            }).decode())
            # First we get "connecting" status
            resp = orjson.loads(ws.receive_bytes())
            assert resp["type"] == "status"
            assert resp["status"] == "connecting"
            # Then session_started
            resp = orjson.loads(ws.receive_bytes())
            assert resp["type"] == "session_started"
            assert resp["session_id"] == "resumed-456"

            # Verify pool.create was called with resume args
            pool.create.assert_awaited_once()
            call_kwargs = pool.create.call_args
            assert call_kwargs.kwargs.get("session_id") == "old-123"
            assert call_kwargs.kwargs.get("fork") is True
