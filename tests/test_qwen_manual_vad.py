"""Tests for the QWEN_MANUAL_VAD=1 wiring in qwen_voice.py and voice_relay.py.

When ``QWEN_MANUAL_VAD=1`` is set, the Qwen provider must emit
``turn_detection: None`` in its ``session.update`` so DashScope's server
VAD is disabled, and the relay must run our local Silero VAD against
every mic chunk, emit synthetic ``input_audio_buffer.speech_started /
speech_stopped`` events on the frontend channel, and send
``input_audio_buffer.commit`` + ``response.create`` upstream when the
user finishes a turn.

These tests cover the seams between the three pieces — the full
end-to-end pipeline (real WebSocket → DashScope → audio) is exercised
manually by running the Jetson backend with the env var set.
"""

from __future__ import annotations

import base64
import wave
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.providers.qwen_voice import DEFAULT_VAD, QwenVoiceProvider

_FIXTURE_DIR = Path(__file__).parent / "fixtures"
_SPEECH_WAV = _FIXTURE_DIR / "voice_speech_24k.wav"


def _load_speech_pcm() -> bytes:
    if not _SPEECH_WAV.exists():
        pytest.skip(f"speech fixture missing: {_SPEECH_WAV}")
    with wave.open(str(_SPEECH_WAV)) as w:
        return w.readframes(w.getnframes())


# --- Qwen provider config -----------------------------------------------------


def test_qwen_emits_server_vad_by_default(monkeypatch):
    """Without QWEN_MANUAL_VAD=1, format_session_config keeps DEFAULT_VAD."""
    monkeypatch.delenv("QWEN_MANUAL_VAD", raising=False)
    provider = QwenVoiceProvider(model="qwen3.5-omni-plus-realtime")
    payload = provider.format_session_config(system="hi", tools=[])
    assert payload["session"]["turn_detection"] == DEFAULT_VAD


def test_qwen_disables_server_vad_when_manual_mode_on(monkeypatch):
    """With QWEN_MANUAL_VAD=1, turn_detection goes to None."""
    monkeypatch.setenv("QWEN_MANUAL_VAD", "1")
    provider = QwenVoiceProvider(model="qwen3.5-omni-plus-realtime")
    payload = provider.format_session_config(system="hi", tools=[])
    assert payload["session"]["turn_detection"] is None


def test_qwen_explicit_vad_overrides_manual_mode(monkeypatch):
    """A caller-provided vad= argument always wins (listen_recording path)."""
    monkeypatch.setenv("QWEN_MANUAL_VAD", "1")
    provider = QwenVoiceProvider(model="qwen3.5-omni-plus-realtime")
    explicit = {"type": "server_vad", "threshold": 0.5}
    payload = provider.format_session_config(system="hi", tools=[], vad=explicit)
    assert payload["session"]["turn_detection"] == explicit


def test_qwen_exposes_audio_in_sample_rate():
    """The relay reads provider.audio_in_sample_rate when wiring up VAD."""
    provider = QwenVoiceProvider(model="qwen3.5-omni-plus-realtime")
    assert provider.audio_in_sample_rate == 24000


# --- Relay manual-VAD path ----------------------------------------------------


@pytest.mark.asyncio
async def test_relay_manual_vad_emits_frontend_events_and_commits(monkeypatch):
    """A real speech-then-silence input drives the relay's manual-VAD path:

    - frontend gets synthetic speech_started + speech_stopped events
    - upstream gets input_audio_buffer.commit + response.create
    """
    monkeypatch.setenv("QWEN_MANUAL_VAD", "1")

    from orchestrator.voice_relay import VoiceRelay

    frontend_events: list[dict] = []

    async def on_event(ev):
        frontend_events.append(ev)

    provider = QwenVoiceProvider(model="qwen3.5-omni-plus-realtime")
    relay = VoiceRelay(
        provider,
        on_audio_out=AsyncMock(),
        on_event_for_frontend=on_event,
        session_id="t-manual-vad",
    )
    # Don't open a real WS — install a fake send_event we can spy on
    # and force the VAD instance into existence.
    upstream_events: list[dict] = []

    async def fake_send_event(ev):
        upstream_events.append(ev)

    relay.send_event = fake_send_event  # type: ignore[method-assign]
    # Stub send_audio's WS send so we don't crash on no-WS — the real
    # upstream call isn't what we're testing. We replicate just enough
    # to run _run_manual_vad.
    from orchestrator import voice_vad
    relay._manual_vad = voice_vad.VoiceVAD(
        input_sample_rate=24000,
        min_silence_duration_ms=600,
        min_speech_duration_ms=100,
    )

    pcm = _load_speech_pcm()
    # Feed chunks at the real cadence (960B = 20ms at 24kHz mono PCM16).
    chunk_size = 960
    for i in range(0, len(pcm), chunk_size):
        await relay._run_manual_vad(base64.b64encode(pcm[i:i + chunk_size]).decode())

    frontend_kinds = [e["type"] for e in frontend_events]
    upstream_kinds = [e["type"] for e in upstream_events]

    assert "input_audio_buffer.speech_started" in frontend_kinds, frontend_kinds
    assert "input_audio_buffer.speech_stopped" in frontend_kinds, frontend_kinds
    # Exactly one commit + response.create pair (one utterance).
    assert upstream_kinds.count("input_audio_buffer.commit") == 1, upstream_kinds
    assert upstream_kinds.count("response.create") == 1, upstream_kinds
    # And they happen in the right order.
    assert upstream_kinds.index("input_audio_buffer.commit") < upstream_kinds.index("response.create")


@pytest.mark.asyncio
async def test_relay_skips_manual_vad_when_env_off(monkeypatch):
    """Without QWEN_MANUAL_VAD=1, the relay never instantiates VoiceVAD."""
    monkeypatch.delenv("QWEN_MANUAL_VAD", raising=False)
    from orchestrator.voice_relay import VoiceRelay

    provider = QwenVoiceProvider(model="qwen3.5-omni-plus-realtime")
    relay = VoiceRelay(
        provider,
        on_audio_out=AsyncMock(),
        on_event_for_frontend=AsyncMock(),
        session_id="t-no-manual",
    )
    # Simulate start()'s VAD init block by hand (without opening a WS).
    from orchestrator import voice_vad
    in_sr = getattr(relay._provider, "audio_in_sample_rate", None)
    if voice_vad.is_enabled() and in_sr is not None:
        relay._manual_vad = voice_vad.VoiceVAD(input_sample_rate=in_sr)
    assert relay._manual_vad is None
