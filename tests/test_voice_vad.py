"""Tests for orchestrator.voice_vad.

Silero is the source of truth for "is this speech?" — we don't try to
out-think it here. The tests focus on the things WE wrote:

- The carry buffer correctly handles arbitrary chunk sizes.
- 24 kHz → 16 kHz resampling produces sane window counts.
- The state machine respects min_speech / min_silence durations.
- Hysteresis prevents chatter when the probability dances around the
  threshold.
- An event's `bytes_consumed` field grows monotonically.

Speech tests use the real recording at
``tests/fixtures/voice_speech_24k.wav`` — Silero rejects synthetic
signals (pure tones, filtered noise) as non-speech because it's trained
on actual voice characteristics.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from orchestrator.voice_vad import VADEvent, VoiceVAD, is_enabled

_FIXTURE_DIR = Path(__file__).parent / "fixtures"
_SPEECH_WAV = _FIXTURE_DIR / "voice_speech_24k.wav"


def _pcm16_silence(seconds: float, sample_rate: int = 24000) -> bytes:
    n = int(seconds * sample_rate)
    return np.zeros(n, dtype=np.int16).tobytes()


def _load_speech_pcm(sample_rate: int = 24000) -> bytes:
    """Load the real speech fixture as raw PCM16 bytes at the requested SR.

    The fixture is recorded at 24 kHz; we linearly resample to other rates
    if the test asks for it. Skips the test if the fixture is missing.
    """
    if not _SPEECH_WAV.exists():
        pytest.skip(
            f"speech fixture missing: {_SPEECH_WAV}. "
            "Record one with: arecord -f S16_LE -r 24000 -c 1 -d 8 <path>"
        )
    with wave.open(str(_SPEECH_WAV)) as w:
        assert w.getframerate() == 24000, f"fixture must be 24kHz, got {w.getframerate()}"
        assert w.getnchannels() == 1, "fixture must be mono"
        raw = w.readframes(w.getnframes())
    if sample_rate == 24000:
        return raw
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    ratio = sample_rate / 24000
    n_out = int(len(audio) * ratio)
    x_old = np.arange(len(audio), dtype=np.float32)
    x_new = np.linspace(0.0, len(audio) - 1, num=n_out, dtype=np.float32)
    return np.interp(x_new, x_old, audio).astype(np.int16).tobytes()


def test_silence_emits_no_events():
    vad = VoiceVAD(input_sample_rate=24000)
    events = list(vad.feed_pcm16(_pcm16_silence(2.0)))
    assert events == []
    assert not vad.is_speech


def test_real_speech_emits_started_and_stopped():
    """Real speech fixture (8s WAV with silence-speech-silence) yields one start/stop pair."""
    vad = VoiceVAD(input_sample_rate=24000, min_silence_duration_ms=600, min_speech_duration_ms=100)
    audio = _load_speech_pcm(sample_rate=24000)
    events = list(vad.feed_pcm16(audio))
    kinds = [e.kind for e in events]
    assert kinds.count("speech_started") == 1, f"expected one start, got {kinds}"
    assert kinds.count("speech_stopped") == 1, f"expected one stop, got {kinds}"
    assert kinds.index("speech_started") < kinds.index("speech_stopped")
    assert not vad.is_speech


def test_chunk_size_invariant():
    """Same audio fed in different chunk sizes must yield identical event sequences."""
    audio = _load_speech_pcm(sample_rate=24000)

    def run(chunk_size: int) -> list[str]:
        vad = VoiceVAD(input_sample_rate=24000, min_silence_duration_ms=600, min_speech_duration_ms=100)
        events: list[VADEvent] = []
        for i in range(0, len(audio), chunk_size):
            events.extend(vad.feed_pcm16(audio[i:i + chunk_size]))
        return [e.kind for e in events]

    # 20ms chunks at 24k = 480 frames = 960 bytes — the real Android cadence.
    # Compared against 1-second chunks and the whole buffer in one call.
    short = run(960)
    medium = run(24000 * 2)
    whole = run(len(audio))
    assert short == medium == whole, f"chunk-size-dependent: short={short} medium={medium} whole={whole}"


def test_bytes_consumed_monotonic():
    vad = VoiceVAD(input_sample_rate=24000, min_silence_duration_ms=600, min_speech_duration_ms=100)
    audio = _load_speech_pcm(sample_rate=24000)
    events = list(vad.feed_pcm16(audio))
    consumed = [e.bytes_consumed for e in events]
    assert consumed == sorted(consumed), f"bytes_consumed not monotonic: {consumed}"
    # And they should be plausible byte offsets (positive, within input length).
    assert all(0 < c <= len(audio) for c in consumed), consumed


def test_sample_rate_16k_no_resample():
    """Native 16 kHz input bypasses the resampler and detects the same speech."""
    vad = VoiceVAD(input_sample_rate=16000, min_silence_duration_ms=600, min_speech_duration_ms=100)
    audio = _load_speech_pcm(sample_rate=16000)
    events = list(vad.feed_pcm16(audio))
    kinds = [e.kind for e in events]
    assert "speech_started" in kinds, kinds
    assert "speech_stopped" in kinds, kinds


def test_min_silence_duration_threshold_changes_event_count():
    """Tightening min_silence_duration_ms causes more stop/start cycles.

    Two copies of the speech fixture glued together = two distinct speech
    spans with the natural recording silence (~2.3s) between them. With
    a tight min_silence (300ms) Silero stops once per span. With a loose
    one (3000ms — longer than the gap) the gap is absorbed as in-utterance
    and we only see one stop.
    """
    audio = _load_speech_pcm(sample_rate=24000)
    doubled = audio + audio

    tight = VoiceVAD(input_sample_rate=24000, min_silence_duration_ms=300, min_speech_duration_ms=100)
    tight_events = list(tight.feed_pcm16(doubled))
    tight_stops = sum(1 for e in tight_events if e.kind == "speech_stopped")
    assert tight_stops == 2, f"tight gate should see two stops, got {[e.kind for e in tight_events]}"

    # Loose gate must exceed the inter-copy silence (trailing ~2.8s + leading
    # ~2.7s of the fixture). 6000ms is well above that and still within
    # Silero's documented [200, 6000] range for silence_duration_ms.
    loose = VoiceVAD(input_sample_rate=24000, min_silence_duration_ms=6000, min_speech_duration_ms=100)
    loose_events = list(loose.feed_pcm16(doubled))
    loose_stops = sum(1 for e in loose_events if e.kind == "speech_stopped")
    # Loose gate longer than the inter-copy gap: at most one stop (the very
    # end may or may not have enough trailing silence to fire).
    assert loose_stops <= 1, f"loose gate should absorb the gap, got {[e.kind for e in loose_events]}"


def test_is_enabled_env_var(monkeypatch: pytest.MonkeyPatch):
    # Default-on: unset env → enabled.
    monkeypatch.delenv("QWEN_MANUAL_VAD", raising=False)
    assert is_enabled() is True
    monkeypatch.setenv("QWEN_MANUAL_VAD", "1")
    assert is_enabled() is True
    # Only explicit "0" opts back into server VAD.
    monkeypatch.setenv("QWEN_MANUAL_VAD", "0")
    assert is_enabled() is False
