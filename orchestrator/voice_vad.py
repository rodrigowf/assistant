"""Client-side streaming VAD for Qwen voice manual mode.

DashScope's server VAD force-commits long utterances mid-speech, splitting
one continuous user turn into two `conversation.item` entries and triggering
a phantom `response.create` between them. To avoid that we disable server
VAD (`turn_detection: None` in `session.update`) and run our own endpoint
detection on the PCM stream before forwarding to DashScope.

This module wraps the Silero VAD v5 ONNX model directly via onnxruntime —
the upstream `silero-vad` pypi package transitively requires torch +
torchaudio (~600MB) for tooling we don't need. The model itself is
vendored at `vendor/silero_vad_v5.onnx` (2.3MB, MIT).

Public API:

    vad = VoiceVAD(input_sample_rate=24000)
    for event in vad.feed_pcm16(audio_bytes):
        if event.kind == "speech_started":
            ...
        elif event.kind == "speech_stopped":
            ...

`feed_pcm16` is a generator: it consumes any-size PCM16 byte buffers,
internally resamples to 16 kHz, slices into the 512-sample (32 ms) windows
Silero expects, runs each through the model, and yields state-transition
events when the smoothed speech probability crosses the threshold.

The detection logic mirrors Silero's official `VADIterator`:
- `threshold` (default 0.5) — speech is "on" when prob crosses up; "off"
  when prob falls below `threshold - 0.15` (hysteresis to avoid chatter).
- `min_silence_duration_ms` (default 1200) — once below the off-threshold,
  wait this long before emitting `speech_stopped`. This is the analogue of
  the upstream `silence_duration_ms` we used to send to DashScope.
- `speech_pad_ms` (default 300) — informational only here; pre-roll
  trimming is the relay's job (it knows where the audio buffer started).
- `min_speech_duration_ms` (default 250) — suppress micro-blips (a single
  cough triggering one above-threshold window doesn't cause `speech_started`
  → `speech_stopped` events).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import onnxruntime as ort

logger = logging.getLogger(__name__)

# Silero VAD ONNX — vendored from the official PyPI wheel (MIT license).
# We use the `silero_vad_half.onnx` quantized variant: 1.3 MB, 16 kHz
# only, no `sr` input parameter. The full `silero_vad.onnx` (2.3 MB,
# multi-SR) returns probabilities that don't react to real speech in our
# onnxruntime version — likely a graph/opset mismatch we couldn't isolate
# in 30 minutes of bisection. The quantized variant works correctly and
# accuracy is documented as "comparable" for VAD-grade thresholding.
_MODEL_PATH = Path(__file__).resolve().parent.parent / "vendor" / "silero_vad_v5.onnx"

# Silero wants 512-sample windows at 16 kHz (32 ms).
_SILERO_SAMPLE_RATE = 16000
_SILERO_WINDOW_SAMPLES = 512  # 32 ms at 16 kHz
_SILERO_WINDOW_MS = 32  # exact: 512 / 16000 * 1000


@dataclass(frozen=True)
class VADEvent:
    """A speech state transition."""

    kind: Literal["speech_started", "speech_stopped"]
    # Total PCM16 bytes consumed by the VAD up to and including the
    # window that caused this transition. Useful for the caller to map
    # back to wall-clock time or upstream buffer offsets.
    bytes_consumed: int


class VoiceVAD:
    """Stateful streaming VAD over PCM16 input.

    One instance per voice session — the Silero model carries hidden state
    across calls, so a fresh instance is needed for each new audio stream.

    Args:
        input_sample_rate: sample rate of PCM16 bytes fed in. Anything other
            than 16000 is decimated/upsampled to 16 kHz via linear
            interpolation before the model. Qwen-Omni uses 24000.
        threshold: Silero speech-probability threshold for the speech-on
            transition. The speech-off transition uses `threshold - 0.15`
            (hysteresis).
        min_silence_duration_ms: how long the probability must stay below
            the off-threshold before we emit `speech_stopped`.
        min_speech_duration_ms: how long the probability must stay above
            the on-threshold before we emit `speech_started`. Prevents
            single-cough false positives.
    """

    def __init__(
        self,
        input_sample_rate: int = 24000,
        threshold: float = 0.5,
        min_silence_duration_ms: int = 1200,
        min_speech_duration_ms: int = 250,
    ) -> None:
        if not _MODEL_PATH.exists():
            raise FileNotFoundError(
                f"Silero VAD model not found at {_MODEL_PATH}. "
                "Run scripts/download-silero-vad.sh or re-clone."
            )

        self._input_sample_rate = input_sample_rate
        self._threshold_on = threshold
        self._threshold_off = max(0.0, threshold - 0.15)
        self._min_silence_windows = max(1, min_silence_duration_ms // _SILERO_WINDOW_MS)
        self._min_speech_windows = max(1, min_speech_duration_ms // _SILERO_WINDOW_MS)

        # ONNX session — single CPU thread is plenty (<1 ms per window).
        sess_opts = ort.SessionOptions()
        sess_opts.intra_op_num_threads = 1
        sess_opts.inter_op_num_threads = 1
        self._sess = ort.InferenceSession(
            str(_MODEL_PATH),
            sess_options=sess_opts,
            providers=["CPUExecutionProvider"],
        )
        # Silero hidden state — (2, batch=1, 128) float32, zeros at start.
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        # The "half" Silero variant we vendor is fixed at 16 kHz and does
        # NOT accept an `sr` input. The full v5 model does. Inspect the
        # ONNX graph to decide which kwargs to pass per call.
        self._input_names = {i.name for i in self._sess.get_inputs()}
        self._needs_sr = "sr" in self._input_names
        if self._needs_sr:
            self._sr_input = np.array(_SILERO_SAMPLE_RATE, dtype=np.int64)

        # Detection state machine.
        self._is_speech = False
        # Number of consecutive above/below-threshold windows we've seen
        # since the last state transition. Used to enforce min_speech /
        # min_silence_ms before emitting an event.
        self._consec_above = 0
        self._consec_below = 0

        # Carry buffer: bytes from the previous feed_pcm16 call that didn't
        # fill a complete 16k window get prepended to the next call.
        self._carry_input_pcm = b""
        self._bytes_consumed = 0

        # Cheap log so we can confirm initialisation in the per-session
        # voice log.
        logger.info(
            "VoiceVAD init: in_sr=%dHz threshold=%.2f (on)/%.2f (off) "
            "min_silence_ms=%d min_speech_ms=%d",
            input_sample_rate, self._threshold_on, self._threshold_off,
            min_silence_duration_ms, min_speech_duration_ms,
        )

    def feed_pcm16(self, pcm_bytes: bytes) -> Iterator[VADEvent]:
        """Feed PCM16 little-endian audio; yield VAD events at transitions.

        The caller is expected to pass chunks at the input sample rate (no
        per-chunk size constraint — any byte count works). Returns a
        generator of zero or more `VADEvent`s.
        """
        if not pcm_bytes:
            return

        # Prepend any carry-over from the previous call.
        pcm = self._carry_input_pcm + pcm_bytes
        # Round down to whole samples (2 bytes each).
        n_samples_in = len(pcm) // 2
        if n_samples_in == 0:
            self._carry_input_pcm = pcm
            return

        usable_bytes = n_samples_in * 2
        self._carry_input_pcm = pcm[usable_bytes:]

        # int16 → float32 in [-1, 1].
        audio_in = np.frombuffer(pcm[:usable_bytes], dtype=np.int16).astype(np.float32) / 32768.0

        # Resample to 16 kHz if needed. Cheap linear interpolation — Silero
        # is robust enough that a fancier kernel buys nothing for VAD.
        if self._input_sample_rate == _SILERO_SAMPLE_RATE:
            audio_16k = audio_in
        else:
            ratio = _SILERO_SAMPLE_RATE / self._input_sample_rate
            n_out = int(len(audio_in) * ratio)
            if n_out == 0:
                # Not enough samples to produce a single 16k sample; carry
                # everything to the next call. (Happens only with tiny
                # sub-millisecond feeds.)
                return
            # np.interp: x_new at uniform spacing in input grid.
            x_old = np.arange(len(audio_in), dtype=np.float32)
            x_new = np.linspace(0.0, len(audio_in) - 1, num=n_out, dtype=np.float32)
            audio_16k = np.interp(x_new, x_old, audio_in).astype(np.float32)

        # Window into 512-sample frames. Anything left over goes back to
        # the carry buffer at the INPUT sample rate (we re-resample on
        # the next call so the carry stays simple).
        n_windows = len(audio_16k) // _SILERO_WINDOW_SAMPLES
        leftover_samples_16k = len(audio_16k) - n_windows * _SILERO_WINDOW_SAMPLES
        if leftover_samples_16k > 0:
            # Convert leftover-16k back to input-sr leftover by counting
            # the equivalent samples in the original.
            leftover_in_samples = int(leftover_samples_16k * self._input_sample_rate / _SILERO_SAMPLE_RATE)
            # Stash that many input samples (from the tail) back in the
            # input carry. We already moved them out of self._carry_input_pcm
            # above, so re-stash here.
            keep_bytes = leftover_in_samples * 2
            if keep_bytes > 0 and keep_bytes <= usable_bytes:
                self._carry_input_pcm = pcm[usable_bytes - keep_bytes:usable_bytes] + self._carry_input_pcm
                # Consumed bytes excludes what we stashed.
                consumed_this_call = usable_bytes - keep_bytes
            else:
                consumed_this_call = usable_bytes
        else:
            consumed_this_call = usable_bytes

        # Run each window through the model and update the state machine.
        for w in range(n_windows):
            window = audio_16k[w * _SILERO_WINDOW_SAMPLES:(w + 1) * _SILERO_WINDOW_SAMPLES]
            prob = self._infer_window(window)
            # Track how much input has been "consumed" up through this
            # window so the emitted event carries accurate byte offsets.
            # Approximate: assume each window consumes uniform fraction
            # of consumed_this_call.
            window_bytes = consumed_this_call // n_windows if n_windows else 0
            self._bytes_consumed += window_bytes

            event = self._update_state(prob)
            if event is not None:
                yield event

    def _infer_window(self, window: np.ndarray) -> float:
        """Run one 512-sample window through Silero; return P(speech)."""
        x = window.reshape(1, _SILERO_WINDOW_SAMPLES)
        feed: dict[str, np.ndarray] = {"input": x, "state": self._state}
        if self._needs_sr:
            feed["sr"] = self._sr_input
        out, self._state = self._sess.run(None, feed)
        return float(out[0, 0])

    def _update_state(self, prob: float) -> VADEvent | None:
        """Update the speech/silence state machine; return an event or None."""
        if not self._is_speech:
            # We're in silence — looking for speech onset.
            if prob >= self._threshold_on:
                self._consec_above += 1
                self._consec_below = 0
                if self._consec_above >= self._min_speech_windows:
                    self._is_speech = True
                    self._consec_above = 0
                    return VADEvent(kind="speech_started", bytes_consumed=self._bytes_consumed)
            else:
                self._consec_above = 0
        else:
            # We're in speech — looking for silence onset.
            if prob < self._threshold_off:
                self._consec_below += 1
                self._consec_above = 0
                if self._consec_below >= self._min_silence_windows:
                    self._is_speech = False
                    self._consec_below = 0
                    return VADEvent(kind="speech_stopped", bytes_consumed=self._bytes_consumed)
            else:
                self._consec_below = 0
        return None

    @property
    def is_speech(self) -> bool:
        """Whether the VAD currently considers the user to be speaking."""
        return self._is_speech


def is_enabled_for(provider_name: str) -> bool:
    """Manual-VAD opt-out per provider.

    Each provider has its own env switch (``QWEN_MANUAL_VAD``,
    ``GEMINI_MANUAL_VAD``, …). All default to **on** because every
    realtime voice service we've shipped has shown a force-commit /
    early-end-of-turn pathology on long utterances:

    - **Qwen / DashScope (2026-05-20)**: server VAD force-commits
      utterances at ~30–40s of continuous audio, splitting one turn
      into two ``conversation.item`` entries.
    - **Gemini Live (2026-06-03)**: server VAD ends turns
      mid-sentence on natural breathing pauses even with
      ``endOfSpeechSensitivity: LOW`` and ``silenceDurationMs: 2500``.

    Opt back into server VAD per-provider:
    ``export QWEN_MANUAL_VAD=0`` or ``export GEMINI_MANUAL_VAD=0``.

    Unknown provider names default to True (opt-in via override only
    matters for the providers we've actually wired hooks for).
    """
    env_var = {
        "qwen": "QWEN_MANUAL_VAD",
        "google": "GEMINI_MANUAL_VAD",
    }.get(provider_name)
    if env_var is None:
        return True
    return os.environ.get(env_var, "1") != "0"


def is_enabled() -> bool:
    """Back-compat shim — equivalent to ``is_enabled_for("qwen")``.

    Existing callers (and tests) that don't yet route through the
    per-provider gate keep the old Qwen-only behaviour.
    """
    return is_enabled_for("qwen")
