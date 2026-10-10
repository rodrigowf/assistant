"""A plain ``start`` from the voice owner's own socket keeps it the owner.

Spec 12 T-9: clients re-send ``start`` on every foreground / resync. The
backend used to answer every plain ``start`` during live voice with
``voice_initiator: false`` — including the owner's. The phone that held the
WebRTC peer then demoted itself: it dropped the ``voice_command`` frames
carrying a tool result (the model said the search was "still running") and
ignored ``voice_ended`` (the call outlived ``end_voice_session``).
2026-10-08, POCO X7 Pro.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import orjson
from starlette.testclient import TestClient

from api.app import create_app
from api.pool import SessionPool


def _fake_voice_orchestrator():
    session = MagicMock()
    session.is_voice = True
    session.voice_is_ending = False
    session.jsonl_id = "jsonl-abc"
    session.get_model_info.return_value = {"id": "test-model"}
    session.voice_config_drifts_from.return_value = None
    session.voice_provider_id = "openai"
    session.voice_model_id = "gpt-realtime"
    session.voice_name_id = "cedar"
    session.voice_transcription_language = None
    session.audio_recorder = None
    session.needs_voice_relay = False
    session._voice_relay = None
    session._voice_provider = None
    session._jsonl_path = None
    session._context = {}
    session.get_session_update = AsyncMock(return_value={"type": "session.update"})
    session.voice_owner_ws = None

    def _register(ws):
        session.voice_owner_ws = ws

    def _clear_if(ws):
        if session.voice_owner_ws is ws:
            session.voice_owner_ws = None
            return True
        return False

    session.register_voice_owner.side_effect = _register
    session.clear_voice_owner_if.side_effect = _clear_if
    session.end_voice = AsyncMock()
    return session


def _client():
    app = create_app()
    pool = SessionPool()
    session = _fake_voice_orchestrator()
    pool._orchestrator = session
    pool._orchestrator_id = "orch-1"
    app.state.pool = pool
    app.state.store = MagicMock()
    return TestClient(app), session


def _recv(ws):
    return orjson.loads(ws.receive_bytes())


def _recv_type(ws, type_):
    while True:
        frame = _recv(ws)
        if frame["type"] == type_:
            return frame


START = orjson.dumps({"type": "start", "local_id": "orch-1"}).decode()
VOICE_START = orjson.dumps({"type": "voice_start", "local_id": "orch-1"}).decode()


def test_owner_plain_start_keeps_voice_initiator_true():
    client, session = _client()
    with client.websocket_connect("/api/orchestrator/chat") as ws:
        ws.send_text(VOICE_START)
        started = _recv_type(ws, "session_started")
        assert started["voice_initiator"] is True
        _recv_type(ws, "voice_owner_active")
        owner = session.voice_owner_ws
        assert owner is not None

        # Foreground resync: the same socket re-sends a plain start.
        ws.send_text(START)
        resync = _recv_type(ws, "session_started")
        assert resync["voice"] is True
        assert resync["voice_initiator"] is True
        assert resync["voice_provider"] == "openai"
        # Nothing to re-apply on the live call.
        assert "voice_session_update" not in resync
        assert "voice_connection_info" not in resync
        # Ownership untouched.
        assert session.voice_owner_ws is owner


def test_other_device_plain_start_is_passive():
    client, session = _client()
    with client.websocket_connect("/api/orchestrator/chat") as owner_ws, \
            client.websocket_connect("/api/orchestrator/chat") as peer_ws:
        owner_ws.send_text(VOICE_START)
        assert _recv_type(owner_ws, "session_started")["voice_initiator"] is True
        owner = session.voice_owner_ws

        peer_ws.send_text(START)
        started = _recv_type(peer_ws, "session_started")
        assert started["voice"] is True
        assert started["voice_initiator"] is False
        assert session.voice_owner_ws is owner
