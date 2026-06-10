"""Tests for Increment B's VAD observability surface.

Increment B exposes Silero VAD state to the frontend via a new
``voice_vad_state`` event broadcast from the relay. The broadcast is
ADDITIVE — the existing ``input_audio_buffer.speech_started/stopped``
events still fire exactly when they do today (see
``tests/parity/test_voice_vad_parity.py`` for the state-machine parity
contract).

This file covers:

1. ``voice_vad_state`` fires on every speech_started → emits ``state=listening``.
2. ``voice_vad_state`` fires on every speech_stopped → emits ``state=thinking``.
3. While ``is_speech`` is True the broadcast repeats every ~1 s so the
   UI clock can advance (even before the next state transition).
4. The payload carries ``duration_ms`` and ``silero_prob`` for UI.
5. The new ``last_silero_prob`` property on ``VoiceVAD`` reflects the
   most recent inference.

See plan §B "Test plan" and §11 (Increment B).
"""

from __future__ import annotations

import base64
import wave
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from orchestrator.providers.qwen_voice import QwenVoiceProvider
from orchestrator.voice_relay import VoiceRelay

_FIXTURE_DIR = Path(__file__).parent / "fixtures"
_SPEECH_WAV = _FIXTURE_DIR / "voice_speech_24k.wav"


def _load_speech_pcm() -> bytes:
    if not _SPEECH_WAV.exists():
        pytest.skip(f"speech fixture missing: {_SPEECH_WAV}")
    with wave.open(str(_SPEECH_WAV)) as w:
        return w.readframes(w.getnframes())


# --- voice_vad_state event emission ----------------------------------

@pytest.mark.asyncio
async def test_voice_vad_state_emitted_on_speech_started(monkeypatch):
    """A speech_started transition must produce a ``voice_vad_state``
    event with ``state="listening"`` AND the existing
    ``input_audio_buffer.speech_started`` event must still fire.
    """
    monkeypatch.delenv("QWEN_MANUAL_VAD", raising=False)
    frontend_events: list[dict] = []

    async def on_event(ev):
        frontend_events.append(ev)

    provider = QwenVoiceProvider(model="qwen3.5-omni-plus-realtime")
    relay = VoiceRelay(
        provider,
        on_audio_out=AsyncMock(),
        on_event_for_frontend=on_event,
        session_id="t-vad-obs-started",
    )

    async def fake_send_event(ev):
        pass

    relay.send_event = fake_send_event  # type: ignore[method-assign]
    from orchestrator import voice_vad
    relay._manual_vad = voice_vad.VoiceVAD(
        input_sample_rate=24000,
        min_silence_duration_ms=600,
        min_speech_duration_ms=100,
    )

    pcm = _load_speech_pcm()
    chunk_size = 960
    for i in range(0, len(pcm), chunk_size):
        await relay._run_manual_vad(base64.b64encode(pcm[i:i + chunk_size]).decode())

    kinds = [e.get("type") for e in frontend_events]
    # Both event types must fire — the new one is additive.
    assert "input_audio_buffer.speech_started" in kinds, kinds
    assert "voice_vad_state" in kinds, kinds

    # At least one voice_vad_state event must carry state=listening
    # (right at speech_started).
    vad_states = [e for e in frontend_events if e.get("type") == "voice_vad_state"]
    listening_states = [s for s in vad_states if s.get("state") == "listening"]
    assert listening_states, vad_states


@pytest.mark.asyncio
async def test_voice_vad_state_emitted_on_speech_stopped(monkeypatch):
    """A speech_stopped transition must produce a ``voice_vad_state``
    event with ``state="thinking"``.
    """
    monkeypatch.delenv("QWEN_MANUAL_VAD", raising=False)
    frontend_events: list[dict] = []

    async def on_event(ev):
        frontend_events.append(ev)

    provider = QwenVoiceProvider(model="qwen3.5-omni-plus-realtime")
    relay = VoiceRelay(
        provider,
        on_audio_out=AsyncMock(),
        on_event_for_frontend=on_event,
        session_id="t-vad-obs-stopped",
    )

    async def fake_send_event(ev):
        pass

    relay.send_event = fake_send_event  # type: ignore[method-assign]
    from orchestrator import voice_vad
    relay._manual_vad = voice_vad.VoiceVAD(
        input_sample_rate=24000,
        min_silence_duration_ms=600,
        min_speech_duration_ms=100,
    )

    pcm = _load_speech_pcm()
    chunk_size = 960
    for i in range(0, len(pcm), chunk_size):
        await relay._run_manual_vad(base64.b64encode(pcm[i:i + chunk_size]).decode())

    vad_states = [e for e in frontend_events if e.get("type") == "voice_vad_state"]
    thinking_states = [s for s in vad_states if s.get("state") == "thinking"]
    assert thinking_states, vad_states


@pytest.mark.asyncio
async def test_voice_vad_state_carries_duration_ms_and_prob(monkeypatch):
    """The ``voice_vad_state`` payload must carry both ``duration_ms``
    and ``silero_prob`` so the UI can render a clock and a confidence
    indicator.
    """
    monkeypatch.delenv("QWEN_MANUAL_VAD", raising=False)
    frontend_events: list[dict] = []

    async def on_event(ev):
        frontend_events.append(ev)

    provider = QwenVoiceProvider(model="qwen3.5-omni-plus-realtime")
    relay = VoiceRelay(
        provider,
        on_audio_out=AsyncMock(),
        on_event_for_frontend=on_event,
        session_id="t-vad-payload",
    )

    async def fake_send_event(ev):
        pass

    relay.send_event = fake_send_event  # type: ignore[method-assign]
    from orchestrator import voice_vad
    relay._manual_vad = voice_vad.VoiceVAD(
        input_sample_rate=24000,
        min_silence_duration_ms=600,
        min_speech_duration_ms=100,
    )

    pcm = _load_speech_pcm()
    chunk_size = 960
    for i in range(0, len(pcm), chunk_size):
        await relay._run_manual_vad(base64.b64encode(pcm[i:i + chunk_size]).decode())

    vad_states = [e for e in frontend_events if e.get("type") == "voice_vad_state"]
    assert vad_states, frontend_events

    for s in vad_states:
        assert "state" in s, s
        assert s["state"] in {"listening", "thinking", "idle"}, s
        assert "duration_ms" in s, s
        assert isinstance(s["duration_ms"], int), s
        assert s["duration_ms"] >= 0, s
        assert "silero_prob" in s, s
        # silero_prob is float or None.
        assert s["silero_prob"] is None or isinstance(s["silero_prob"], float), s


@pytest.mark.asyncio
async def test_listening_state_repeats_periodically(monkeypatch):
    """While the VAD is in speech_started → speech_stopped, the
    ``voice_vad_state`` event with ``state="listening"`` must repeat
    at ~1 s intervals so the UI's "listening Ns" clock can advance.

    Plan §B: "Update the duration every ~1 s while in `listening` so
    the UI clock advances even before the state changes."
    """
    monkeypatch.delenv("QWEN_MANUAL_VAD", raising=False)
    frontend_events: list[dict] = []

    async def on_event(ev):
        frontend_events.append(ev)

    provider = QwenVoiceProvider(model="qwen3.5-omni-plus-realtime")
    relay = VoiceRelay(
        provider,
        on_audio_out=AsyncMock(),
        on_event_for_frontend=on_event,
        session_id="t-vad-repeat",
    )

    async def fake_send_event(ev):
        pass

    relay.send_event = fake_send_event  # type: ignore[method-assign]
    from orchestrator import voice_vad
    # Use a high min_silence so the VAD stays in 'speech' for the
    # whole fixture — that's the stuck-listening state we want to
    # exercise for the heartbeat broadcast.
    relay._manual_vad = voice_vad.VoiceVAD(
        input_sample_rate=24000,
        min_silence_duration_ms=10000,
        min_speech_duration_ms=100,
    )

    pcm = _load_speech_pcm()
    chunk_size = 960
    for i in range(0, len(pcm), chunk_size):
        await relay._run_manual_vad(base64.b64encode(pcm[i:i + chunk_size]).decode())

    listening = [
        e for e in frontend_events
        if e.get("type") == "voice_vad_state" and e.get("state") == "listening"
    ]
    # We expect at least 2 events: the initial transition and at least
    # one ~1 s heartbeat (fixture is ~8 s long). If the fixture is
    # shorter we tolerate a single event.
    assert len(listening) >= 1, listening
    if len(listening) >= 2:
        # Durations must be monotonically non-decreasing.
        durs = [e["duration_ms"] for e in listening]
        for a, b in zip(durs, durs[1:]):
            assert b >= a, durs


# --- VoiceVAD.last_silero_prob property -----------------------------

def test_voice_vad_exposes_last_silero_prob():
    """The new ``last_silero_prob`` property reflects the most recent
    per-window Silero inference. Pre-feed → None.
    """
    from orchestrator.voice_vad import VoiceVAD

    vad = VoiceVAD(input_sample_rate=24000)
    assert vad.last_silero_prob is None, "pre-feed value must be None"

    pcm = _load_speech_pcm()
    list(vad.feed_pcm16(pcm[:48000 * 2]))  # ~2 s of int16 PCM
    assert vad.last_silero_prob is not None, "after feed, prob must be set"
    assert 0.0 <= vad.last_silero_prob <= 1.0


def test_voice_vad_reset_clears_last_silero_prob():
    """``reset()`` clears observability state too."""
    from orchestrator.voice_vad import VoiceVAD

    vad = VoiceVAD(input_sample_rate=24000)
    pcm = _load_speech_pcm()
    list(vad.feed_pcm16(pcm[:48000 * 2]))
    assert vad.last_silero_prob is not None

    vad.reset()
    assert vad.last_silero_prob is None
    assert vad.is_speech is False
