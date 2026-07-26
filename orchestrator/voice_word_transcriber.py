"""Live word-level transcription of the assistant's outgoing voice audio.

Feeds the PCM the backend already broadcasts as ``voice_audio_out`` through
a Vosk KaldiRecognizer and emits ``voice_word_out`` events over the same
orchestrator broadcast channel — one per recognised word, tagged with its
audio-relative start-time and a monotonic per-response index.

The observer (``context/public/avatar-pipeline/observer.html``) uses these
to align its emoji-driven expressions to the actual moment each word is
spoken, rather than to the transcript-arrival timing (which streams much
faster than playback).

**Activation is fully opt-in.** No model is loaded and no CPU is spent
until a subscriber explicitly asks for word transcription on their voice
session (via the observer's ``enable_word_transcription`` WS message).
When the last subscriber goes away the recognizer is torn down. The
normal application flow — chat, voice, tool calls — never touches this
module at all if nobody subscribes.

Design:
- One :class:`VoiceWordTranscriber` per subscribed voice session, held
  in the module-level :data:`_TRANSCRIBERS` registry. Reference-counted
  by :data:`_SUBSCRIBERS`.
- Model is a shared :class:`Model` singleton, loaded once on first
  subscribe across the whole process (~1–2 s on Jetson Nano).
- Vosk runs on CPU. It's ~5–10× faster than real time on Jetson Nano's
  Cortex-A57 cores, so a single core covers a live conversation with
  margin. No GPU needed.
- Vosk's native sample rate for the small model is 16 kHz. Voice
  providers (Gemini, OpenAI, Qwen) emit 24 kHz. We downsample before
  feeding using linear interpolation — the small model is robust to
  this and higher-fidelity resamplers aren't warranted for a
  cue-detection use case.
- Recognizer state is per-response: :meth:`reset` at the top of every
  new assistant utterance so word indices start from 0.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
from typing import Callable, Awaitable, Optional

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model singleton
# ---------------------------------------------------------------------------

_MODEL_PATH = os.environ.get(
    "VOSK_MODEL_PATH",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "context", "models", "vosk-en-small",
    ),
)

_MODEL = None
_MODEL_LOCK = threading.Lock()


def _get_model():
    """Load Vosk model on first use, then cache. Thread-safe."""
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL
        try:
            from vosk import Model, SetLogLevel
        except ImportError:
            logger.warning(
                "vosk not installed; live word transcription disabled. "
                "Install with `pip install vosk` in the venv."
            )
            return None
        if not os.path.isdir(_MODEL_PATH):
            logger.warning(
                "Vosk model not found at %s; live word transcription disabled. "
                "Download with: cd context/models && curl -sL "
                "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip -o m.zip "
                "&& unzip m.zip && mv vosk-model-small-en-us-0.15 vosk-en-small && rm m.zip",
                _MODEL_PATH,
            )
            return None
        SetLogLevel(-1)  # silence Vosk's stderr chatter
        logger.info("Loading Vosk model from %s ...", _MODEL_PATH)
        _MODEL = Model(_MODEL_PATH)
        logger.info("Vosk model loaded.")
        return _MODEL


# ---------------------------------------------------------------------------
# Per-session transcriber
# ---------------------------------------------------------------------------

# Vosk's internal sample rate. The model was trained on 16 kHz, so we
# downsample from the 24 kHz voice providers emit.
_VOSK_SR = 16000

# Callback signature — an async fn that broadcasts the word-out event.
WordCallback = Callable[[dict], Awaitable[None]]


class VoiceWordTranscriber:
    """Streaming word-level transcriber for one voice session.

    Parameters
    ----------
    source_sample_rate
        Sample rate of the incoming PCM (typically 24000). Downsampled
        to Vosk's 16 kHz internally.
    on_word
        Async callback invoked with the event dict for every recognised
        word (via partial or final results).
    """

    def __init__(self, *, source_sample_rate: int = 24000, on_word: WordCallback) -> None:
        self._on_word = on_word
        self._src_sr = source_sample_rate
        self._recognizer = None
        self._word_index = 0
        self._seen_word_starts: set[float] = set()  # dedupe partial-vs-final
        self._enabled = True
        self._model = _get_model()
        if self._model is None:
            self._enabled = False
            return
        try:
            from vosk import KaldiRecognizer
        except ImportError:
            self._enabled = False
            return
        self._recognizer = KaldiRecognizer(self._model, _VOSK_SR)
        self._recognizer.SetWords(True)
        # We rely on partial results for low latency; full results
        # arrive at silence boundaries.
        self._recognizer.SetPartialWords(True)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def reset(self) -> None:
        """Reset for a new assistant utterance. Call at response start."""
        if not self._enabled:
            return
        # Vosk doesn't expose an explicit reset — recreate the recognizer.
        # Cheap: ~10 ms on Jetson Nano.
        try:
            from vosk import KaldiRecognizer
        except ImportError:
            return
        self._recognizer = KaldiRecognizer(self._model, _VOSK_SR)
        self._recognizer.SetWords(True)
        self._recognizer.SetPartialWords(True)
        self._word_index = 0
        self._seen_word_starts.clear()

    def _downsample_24k_to_16k(self, pcm_i16: np.ndarray) -> bytes:
        """24 kHz → 16 kHz by decimation with linear interpolation.

        Uses a fractional-step index instead of dropping every 3rd sample
        (which would introduce audible aliasing). Vosk's small model is
        robust to this simple resampler.
        """
        if self._src_sr == _VOSK_SR:
            return pcm_i16.astype(np.int16).tobytes()
        n_out = int(len(pcm_i16) * _VOSK_SR / self._src_sr)
        if n_out <= 0:
            return b""
        idx_f = np.linspace(0, len(pcm_i16) - 1, n_out)
        idx0 = np.floor(idx_f).astype(np.int64)
        idx1 = np.clip(idx0 + 1, 0, len(pcm_i16) - 1)
        frac = (idx_f - idx0).astype(np.float32)
        s0 = pcm_i16[idx0].astype(np.float32)
        s1 = pcm_i16[idx1].astype(np.float32)
        out = (s0 * (1.0 - frac) + s1 * frac).astype(np.int16)
        return out.tobytes()

    async def feed_pcm_b64(self, audio_b64: str) -> list[dict]:
        """Feed one base64-encoded PCM16 chunk. Returns any new word
        events emitted (also delivered via the callback).
        """
        if not self._enabled or self._recognizer is None:
            return []
        try:
            raw = base64.b64decode(audio_b64)
        except Exception:  # noqa: BLE001
            return []
        pcm = np.frombuffer(raw, dtype=np.int16)
        if pcm.size == 0:
            return []
        pcm16 = self._downsample_24k_to_16k(pcm)
        events: list[dict] = []
        try:
            is_final = self._recognizer.AcceptWaveform(pcm16)
        except Exception:  # noqa: BLE001
            logger.exception("Vosk AcceptWaveform failed")
            return []
        if is_final:
            payload = self._recognizer.Result()
        else:
            payload = self._recognizer.PartialResult()
        try:
            parsed = json.loads(payload)
        except Exception:  # noqa: BLE001
            return []
        # Vosk emits "result" (final, with word timings) or "partial_result"
        # (words-so-far with timings when SetPartialWords is on).
        words = parsed.get("result") or parsed.get("partial_result") or []
        for w in words:
            start = w.get("start")
            word = w.get("word") or ""
            if start is None or not word:
                continue
            # Dedupe — partial results include already-emitted words.
            key = round(start * 1000)
            if key in self._seen_word_starts:
                continue
            self._seen_word_starts.add(key)
            ev = {
                "type": "voice_word_out",
                "word": word,
                "start_ms": int(start * 1000),
                "end_ms": int(w.get("end", start) * 1000),
                "index": self._word_index,
            }
            self._word_index += 1
            events.append(ev)
            try:
                await self._on_word(ev)
            except Exception:  # noqa: BLE001
                logger.exception("voice_word_out callback failed")
        return events

    def maybe_reset_for_event(self, event: dict) -> bool:
        """Reset the recognizer if this provider event marks end-of-turn.

        Recognised end-of-turn shapes:
          * OpenAI / Qwen: ``response.done``
          * Gemini Live:   ``serverContent.turnComplete`` /
                            ``serverContent.interrupted``

        Returns True if reset was performed.
        """
        if not self._enabled:
            return False
        if event.get("type") == "response.done":
            self.reset()
            return True
        sc = event.get("serverContent") if isinstance(event, dict) else None
        if isinstance(sc, dict) and (sc.get("turnComplete") or sc.get("interrupted")):
            self.reset()
            return True
        return False

    def close(self) -> None:
        self._recognizer = None
        self._enabled = False


# ---------------------------------------------------------------------------
# Subscriber-gated activation
# ---------------------------------------------------------------------------
# The transcriber is opt-in per voice session. The route layer wires
# subscribe() when a WS client sends ``enable_word_transcription`` and
# unsubscribe() on ``disable_word_transcription`` or WS close. In
# between, ``feed_if_active`` / ``reset_if_active`` are cheap no-ops when
# nobody's watching.

_TRANSCRIBERS: dict[str, VoiceWordTranscriber] = {}
_SUBSCRIBERS: dict[str, set[int]] = {}


def subscribe(session_id: str, client_id: int, on_word: WordCallback) -> bool:
    """Register a subscriber for a voice session's word stream.

    On the FIRST subscriber for the session, instantiates the
    transcriber (which lazy-loads the shared Vosk model). Subsequent
    subscribers just increment the ref count.

    Returns True if this was the first subscriber (i.e. the transcriber
    was just created).
    """
    subs = _SUBSCRIBERS.setdefault(session_id, set())
    first = not subs
    subs.add(client_id)
    if first and session_id not in _TRANSCRIBERS:
        _TRANSCRIBERS[session_id] = VoiceWordTranscriber(
            source_sample_rate=24000, on_word=on_word
        )
    return first


def unsubscribe(session_id: str, client_id: int) -> bool:
    """Unregister a subscriber. Tears down the transcriber on the last
    unsubscribe. Safe to call for unknown ids (no-op).

    Returns True if this was the last subscriber (transcriber torn down).
    """
    subs = _SUBSCRIBERS.get(session_id)
    if not subs:
        return False
    subs.discard(client_id)
    if subs:
        return False
    _SUBSCRIBERS.pop(session_id, None)
    t = _TRANSCRIBERS.pop(session_id, None)
    if t is not None:
        t.close()
    return True


def is_active(session_id: str) -> bool:
    return session_id in _TRANSCRIBERS


async def feed_if_active(session_id: str, audio_b64: str) -> None:
    """Feed one base64 PCM chunk iff someone is subscribed for this
    session. Zero cost when inactive."""
    t = _TRANSCRIBERS.get(session_id)
    if t is not None and t.enabled:
        await t.feed_pcm_b64(audio_b64)


def reset_if_active_for_event(session_id: str, event: dict) -> None:
    """End-of-turn detection — no-op when transcriber isn't active."""
    t = _TRANSCRIBERS.get(session_id)
    if t is not None and t.enabled:
        t.maybe_reset_for_event(event)
