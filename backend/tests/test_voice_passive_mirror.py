"""Live OpenAI (WebRTC) voice transcripts reach the other devices (VT-2).

With WebRTC the owner device talks to OpenAI directly and mirrors every
data-channel event to the backend as ``voice_event``. The backend persisted
them but never forwarded them, so a passive device (iPad watching a call
made from the phone) saw only the tool cards. ``_handle_voice_event`` now
re-broadcasts the transcript events to everyone but the sender.
"""

from __future__ import annotations

from pathlib import Path

from api.routes.orchestrator import _handle_voice_event
from orchestrator.config import OrchestratorConfig
from orchestrator.providers.openai_voice import OpenAIVoiceProvider
from orchestrator.providers.qwen_voice import QwenVoiceProvider
from orchestrator.session import OrchestratorSession


def _make_session(tmp_path: Path, provider) -> OrchestratorSession:
    config = OrchestratorConfig(
        project_dir=str(tmp_path),
        memory_path=str(tmp_path / "mem.md"),
    )
    session = OrchestratorSession(config=config, context={}, voice=True)
    session._voice_provider = provider
    return session


class _Pool:
    """SessionPool stand-in recording broadcasts and their ``exclude``."""

    def __init__(self) -> None:
        self.broadcasts: list[tuple[dict, object]] = []

    async def broadcast_orchestrator(self, payload: dict, *, exclude=None) -> None:
        self.broadcasts.append((payload, exclude))

    def voice_events(self) -> list[tuple[dict, object]]:
        return [(p["event"], ex) for p, ex in self.broadcasts if p.get("type") == "voice_event"]


OWNER = object()  # stands in for the owner's WebSocket


async def test_webrtc_transcripts_reach_other_devices(tmp_path):
    session = _make_session(tmp_path, OpenAIVoiceProvider())
    pool = _Pool()
    turn = [
        {"type": "input_audio_buffer.speech_started"},
        {"type": "conversation.item.input_audio_transcription.completed", "item_id": "i1", "transcript": "what time is it"},
        {"type": "response.created", "response": {"id": "r1"}},
        {"type": "response.output_audio_transcript.delta", "delta": "It is "},
        {"type": "response.output_audio_transcript.delta", "delta": "noon."},
        {"type": "response.output_audio_transcript.done", "transcript": "It is noon."},
        {"type": "response.done", "response": {"id": "r1", "status": "completed"}},
    ]
    for ev in turn:
        await _handle_voice_event(pool, session, ev, sender=OWNER)

    mirrored = pool.voice_events()
    assert [ev["type"] for ev, _ in mirrored] == [
        "input_audio_buffer.speech_started",
        "conversation.item.input_audio_transcription.completed",
        "response.output_audio_transcript.delta",
        "response.output_audio_transcript.delta",
        "response.output_audio_transcript.done",
        "response.done",
    ]
    assert mirrored[1][0]["transcript"] == "what time is it"
    # The owner renders from its own data channel: never echoed back to it.
    assert all(ex is OWNER for _, ex in mirrored)


async def test_webrtc_outbound_and_control_events_are_not_mirrored(tmp_path):
    session = _make_session(tmp_path, OpenAIVoiceProvider())
    pool = _Pool()
    for ev in (
        {"type": "session.update", "session": {}},
        {"type": "response.create"},
        {"type": "conversation.item.create", "item": {"type": "message"}},
        {"type": "session.updated", "session": {}},
        {"type": "input_audio_buffer.speech_stopped"},
    ):
        await _handle_voice_event(pool, session, ev, sender=OWNER)
    assert pool.voice_events() == []


async def test_webrtc_injected_user_audio_is_not_mirrored(tmp_path):
    session = _make_session(tmp_path, OpenAIVoiceProvider())
    session._injection_active = True
    pool = _Pool()
    await _handle_voice_event(pool, session, {"type": "input_audio_buffer.speech_started"}, sender=OWNER)
    await _handle_voice_event(
        pool, session,
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": "replayed"},
        sender=OWNER,
    )
    assert pool.voice_events() == []


async def test_ws_provider_events_are_not_double_broadcast(tmp_path):
    """WS providers broadcast from the relay; the client path must not add a copy."""
    session = _make_session(tmp_path, QwenVoiceProvider())
    assert session.needs_voice_relay
    pool = _Pool()
    await _handle_voice_event(
        pool, session, {"type": "response.audio_transcript.delta", "delta": "hi"}, sender=OWNER,
    )
    assert pool.voice_events() == []
