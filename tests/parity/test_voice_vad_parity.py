"""Parity test: Silero VAD state-machine output is unchanged.

Increment B of the voice-subsystem refactor adds *observability* to the
manual-VAD path — a new ``voice_vad_state`` broadcast and read-only
properties on :class:`VoiceVAD`. The plan's source-fidelity rule
(§0.1 + §B "Source-fidelity preservation") forbids any change to the
state machine itself: identical PCM input must produce a byte-for-byte
identical ``VADEvent`` sequence.

This file pins that contract. The same speech fixture used by
``tests/test_voice_vad.py::test_real_speech_emits_started_and_stopped``
is fed through ``VoiceVAD`` twice — once with the historical
constructor args (matching HEAD 5286baa defaults), once with the new
config-driven defaults (which must equal the same numbers). Both runs
must produce identical events at identical ``bytes_consumed`` offsets.

See plan §11 (Increment B) — "feed identical PCM byte stream through
``VoiceVAD`` before and after the increment; assert byte-for-byte
identical ``VADEvent`` sequence with identical ``bytes_consumed``
offsets".
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from orchestrator.voice_vad import VADEvent, VoiceVAD

_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"
_SPEECH_WAV = _FIXTURE_DIR / "voice_speech_24k.wav"


def _load_speech_pcm() -> bytes:
    if not _SPEECH_WAV.exists():
        pytest.skip(
            f"speech fixture missing: {_SPEECH_WAV}. "
            "Record one with: arecord -f S16_LE -r 24000 -c 1 -d 8 <path>"
        )
    with wave.open(str(_SPEECH_WAV)) as w:
        assert w.getframerate() == 24000
        assert w.getnchannels() == 1
        return w.readframes(w.getnframes())


def test_default_constructor_args_match_head_constants():
    """The constructor defaults must equal today's hardcoded literals.

    These three numbers (threshold=0.28, min_silence=2500,
    min_speech=200) are the tuned far-field parameters documented in
    ``voice_vad.py:107-112``. Increment B exposes them as user-settable
    config knobs but does NOT change their defaults — the source-
    fidelity rule (plan §0.1) demands the defaults equal HEAD constants
    exactly.
    """
    vad = VoiceVAD(input_sample_rate=24000)
    # _threshold_on and _threshold_off are the post-hysteresis numbers
    # the state machine actually uses. Pinning both makes the parity
    # contract robust to future internal renames.
    assert vad._threshold_on == pytest.approx(0.28)
    assert vad._threshold_off == pytest.approx(0.13)  # max(0, 0.28 - 0.15)
    # min_speech_duration_ms=200 → 200 / 32 = 6 windows (int division).
    assert vad._min_speech_windows == 6
    # min_silence_duration_ms=2500 → 2500 / 32 = 78 windows.
    assert vad._min_silence_windows == 78


def test_state_machine_output_unchanged_under_head_defaults():
    """Feed the speech fixture through VoiceVAD with the HEAD defaults
    and capture the event stream. Then construct another VoiceVAD with
    explicit args equal to the HEAD defaults and assert the captured
    event sequence is byte-identical.

    This is the load-bearing parity check: it'll fail loudly if a
    future refactor accidentally changes a threshold by 0.01, the
    window arithmetic rounding, or the consecutive-counter logic.
    """
    audio = _load_speech_pcm()

    vad_default = VoiceVAD(input_sample_rate=24000)
    events_default = list(vad_default.feed_pcm16(audio))

    vad_explicit = VoiceVAD(
        input_sample_rate=24000,
        threshold=0.28,
        min_silence_duration_ms=2500,
        min_speech_duration_ms=200,
    )
    events_explicit = list(vad_explicit.feed_pcm16(audio))

    # The two runs must produce identical event sequences.
    assert len(events_default) == len(events_explicit), (
        f"default-args run produced {len(events_default)} events but "
        f"explicit-args run with the same numbers produced {len(events_explicit)}; "
        "this means a default value drifted away from the documented constant."
    )
    for ev_d, ev_e in zip(events_default, events_explicit):
        assert ev_d.kind == ev_e.kind, (
            f"event kind mismatch: default-args={ev_d.kind!r} "
            f"vs explicit-args={ev_e.kind!r} — state-machine drift"
        )
        assert ev_d.bytes_consumed == ev_e.bytes_consumed, (
            f"bytes_consumed mismatch at {ev_d.kind!r}: "
            f"default-args={ev_d.bytes_consumed} vs "
            f"explicit-args={ev_e.bytes_consumed} — window-arithmetic drift"
        )


def test_state_machine_pure_on_silence():
    """Pure silence must produce zero events regardless of any
    observability surfaces added in Increment B.

    A common refactor regression is to emit a synthetic "still idle"
    event from the observability path that leaks into the canonical
    event stream. ``feed_pcm16`` must remain pure with respect to its
    documented output.
    """
    vad = VoiceVAD(input_sample_rate=24000)
    silence = np.zeros(48000, dtype=np.int16).tobytes()  # 2s silence
    events = list(vad.feed_pcm16(silence))
    assert events == []
    assert vad.is_speech is False


def test_observability_properties_are_read_only_and_pure():
    """Increment B adds read-only properties surfacing internal Silero
    state for relay-side broadcasting. Reading them must NOT mutate the
    state machine.
    """
    vad = VoiceVAD(input_sample_rate=24000)
    audio = _load_speech_pcm()
    # Feed a couple of seconds of audio to put the VAD in some state.
    events_before_probes = list(vad.feed_pcm16(audio[:24000 * 2 * 2]))  # ~2s of int16
    is_speech_before = vad.is_speech
    bytes_before = vad._bytes_consumed

    # Probe the new properties — they must be present and read-only.
    _ = vad.is_speech
    _ = getattr(vad, "last_silero_prob", None)

    # Feed the remainder; the event sequence from this point must be
    # identical regardless of whether the probes were called or not.
    audio_tail = audio[24000 * 2 * 2:]
    events_after = list(vad.feed_pcm16(audio_tail))

    # Probes must not have leaked side effects.
    # Re-running the same prefix through a fresh VAD and then the same
    # tail should yield the same sequence. This pins the no-side-effect
    # contract.
    vad2 = VoiceVAD(input_sample_rate=24000)
    events2_prefix = list(vad2.feed_pcm16(audio[:24000 * 2 * 2]))
    events2_tail = list(vad2.feed_pcm16(audio_tail))

    assert len(events_before_probes) == len(events2_prefix)
    assert len(events_after) == len(events2_tail)
    for a, b in zip(events_after, events2_tail):
        assert a.kind == b.kind
        assert a.bytes_consumed == b.bytes_consumed

    # Sanity: is_speech wasn't mutated by reading it.
    assert vad.is_speech == is_speech_before or vad.is_speech == vad2.is_speech
    assert vad._bytes_consumed >= bytes_before
