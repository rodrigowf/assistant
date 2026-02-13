"""Tests for api/routes/chat.py — WebSocket chat endpoint."""

from unittest.mock import AsyncMock, MagicMock, patch

import orjson
import pytest
from starlette.testclient import TestClient

from api.app import create_app
from api.routes import chat as chat_module
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


@pytest.fixture
def sync_client():
    app = create_app()
    with TestClient(app) as client:
        yield client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestWebSocketChat:
    @patch.object(chat_module, "ManagerConfig")
    @patch.object(chat_module, "SessionManager")
    def test_start_and_send(self, MockSM, MockConfig, sync_client):
        mock_sm = _mock_session_manager()
        MockSM.return_value = mock_sm
        MockConfig.load.return_value = MagicMock()

        with sync_client.websocket_connect("/api/sessions/chat") as ws:
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

    @patch.object(chat_module, "ManagerConfig")
    @patch.object(chat_module, "SessionManager")
    def test_send_before_start(self, MockSM, MockConfig, sync_client):
        with sync_client.websocket_connect("/api/sessions/chat") as ws:
            ws.send_text(orjson.dumps({"type": "send", "text": "Hi"}).decode())
            resp = orjson.loads(ws.receive_bytes())
            assert resp["type"] == "error"
            assert resp["error"] == "not_started"

    @patch.object(chat_module, "ManagerConfig")
    @patch.object(chat_module, "SessionManager")
    def test_interrupt(self, MockSM, MockConfig, sync_client):
        mock_sm = _mock_session_manager()
        MockSM.return_value = mock_sm
        MockConfig.load.return_value = MagicMock()

        with sync_client.websocket_connect("/api/sessions/chat") as ws:
            ws.send_text(orjson.dumps({"type": "start"}).decode())
            ws.receive_bytes()  # connecting status
            ws.receive_bytes()  # session_started

            ws.send_text(orjson.dumps({"type": "interrupt"}).decode())
            resp = orjson.loads(ws.receive_bytes())
            assert resp["type"] == "status"
            assert resp["status"] == "interrupted"

    @patch.object(chat_module, "ManagerConfig")
    @patch.object(chat_module, "SessionManager")
    def test_command(self, MockSM, MockConfig, sync_client):
        mock_sm = _mock_session_manager()
        MockSM.return_value = mock_sm
        MockConfig.load.return_value = MagicMock()

        with sync_client.websocket_connect("/api/sessions/chat") as ws:
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

    @patch.object(chat_module, "ManagerConfig")
    @patch.object(chat_module, "SessionManager")
    def test_start_failure(self, MockSM, MockConfig, sync_client):
        mock_sm = MagicMock()
        mock_sm.start = AsyncMock(side_effect=RuntimeError("connection failed"))
        MockSM.return_value = mock_sm
        MockConfig.load.return_value = MagicMock()

        with sync_client.websocket_connect("/api/sessions/chat") as ws:
            ws.send_text(orjson.dumps({"type": "start"}).decode())
            # First we get "connecting" status
            resp = orjson.loads(ws.receive_bytes())
            assert resp["type"] == "status"
            assert resp["status"] == "connecting"
            # Then the error
            resp = orjson.loads(ws.receive_bytes())
            assert resp["type"] == "error"
            assert resp["error"] == "start_failed"

    @patch.object(chat_module, "ManagerConfig")
    @patch.object(chat_module, "SessionManager")
    def test_resume_session(self, MockSM, MockConfig, sync_client):
        mock_sm = _mock_session_manager(session_id="resumed-456")
        MockSM.return_value = mock_sm
        MockConfig.load.return_value = MagicMock()

        with sync_client.websocket_connect("/api/sessions/chat") as ws:
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

            # Verify SessionManager was created with resume args
            MockSM.assert_called_once()
            call_kwargs = MockSM.call_args
            assert call_kwargs[1].get("fork") is True or call_kwargs[0][0] == "old-123"
