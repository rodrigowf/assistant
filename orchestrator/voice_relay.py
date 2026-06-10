"""Backend WebSocket relay for non-WebRTC voice providers.

For providers with ``connection_type == "websocket"`` (Qwen, Gemini Live,
future locals), the backend owns the upstream provider WS. The frontend
talks to the orchestrator WS only; audio flows browser → orchestrator WS →
backend → provider WS, and back.

This module wires the relay:

- :class:`VoiceRelay` opens the provider WS on start, runs a background
  task that drains messages, and exposes :meth:`send_event` /
  :meth:`send_audio` for the orchestrator handler to push frontend input
  upstream.
- Inbound provider messages get split: JSON control events go to
  ``provider.inject_event()`` (so the existing ``process_voice_event``
  pipeline persists/reacts to them); audio chunks get pushed via
  ``on_audio_out`` so the orchestrator handler can broadcast a
  ``voice_audio_out`` payload to subscribed frontends.
- WebRTC providers (OpenAI) skip the relay entirely; their audio bypasses
  the backend.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from collections import Counter, deque
from pathlib import Path
from typing import Any, Awaitable, Callable

from orchestrator.providers.voice_base import BaseVoiceProvider
from orchestrator import voice_vad
from orchestrator.voice_errors import VoiceError, VoiceErrorCategory
from orchestrator.voice_reconnect import (
    HELD_OUTBOUND_CAP,
    POLICIES,
    ReconnectReason,
    policy_for,
)
from orchestrator.voice_timeouts import VoiceTimeouts
from utils.paths import PROJECT_ROOT
from weakref import WeakSet

logger = logging.getLogger(__name__)


AudioOutCallback = Callable[[str], Awaitable[None]]
EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
SessionUpdateBuilder = Callable[[], Awaitable[dict[str, Any]]]


# How many recent non-audio frames to remember.  When the upstream WS
# dies with a misleading error from the provider, the offending frame
# is in here.  Audio chunks are excluded — they're frequent and almost
# never the cause; logging them would drown the signal.
_FRAME_HISTORY_SIZE = 24

# Sample 1-in-N audio frames into the per-session log so we can see the
# audio flow without flooding (audio inbound runs at ~50 Hz at 20ms
# chunks).
_AUDIO_LOG_SAMPLE_EVERY = 50

# Where per-session voice logs land.  One file per session_id, written
# alongside the api logs so /debug-app picks them up.
_VOICE_LOG_DIR = PROJECT_ROOT / "logs" / "voice"

# Increment F (plan §F) — the timeout constants previously declared
# here are now ``VoiceTimeouts`` fields:
#   _KEEPALIVE_INTERVAL_S         → voice_timeouts.keepalive_s
#   _MANUAL_VAD_SAFETY_COMMIT_S   → voice_timeouts.manual_vad_safety_commit_s
#   _VAD_STATE_HEARTBEAT_S        → voice_timeouts.vad_state_heartbeat_s
#   _RECONNECT_HANDSHAKE_TIMEOUT_S → voice_timeouts.reconnect_handshake_s
# Defaults equal the pre-Inc-F literals byte-for-byte
# (tests/test_voice_timeouts.py::test_defaults_equal_head_constants).


class VoiceRelay:
    """Owns one upstream WS to the voice provider for the lifetime of a session.

    The relay is created lazily by :class:`OrchestratorSession.start` when
    the provider's ``connection_type`` is ``"websocket"``. Lifecycle:

    1. ``await relay.start()`` — opens the upstream WS, sends the initial
       ``session.update``, and spawns the drain task.
    2. ``await relay.send_event(ev)`` / ``await relay.send_audio(b64)`` —
       called by the WS handler when the frontend sends control events or
       mic chunks.
    3. ``await relay.stop()`` — cancels the drain task and closes the WS.

    The relay does not block on any frontend operation — it only reaches
    upstream and yields events back via callbacks.
    """

    def __init__(
        self,
        provider: BaseVoiceProvider,
        *,
        on_audio_out: AudioOutCallback,
        on_event_for_frontend: EventCallback,
        session_id: str | None = None,
        rebuild_session_update: SessionUpdateBuilder | None = None,
        max_reconnects: int = 2,
        # Increment B — VAD tuning. None means "use VoiceVAD defaults"
        # which equal HEAD constants exactly (see
        # tests/parity/test_voice_vad_parity.py). Threaded from
        # ``assistant_config.json`` via ``OrchestratorSession``.
        vad_threshold: float | None = None,
        vad_min_silence_ms: int | None = None,
        # Increment F — central voice-pipeline timeouts. None means
        # "use VoiceTimeouts.default()" which equals HEAD constants
        # exactly (parity test pins this).
        voice_timeouts: "VoiceTimeouts | None" = None,
    ) -> None:
        if provider.connection_type != "websocket":
            raise ValueError(
                f"VoiceRelay only supports websocket providers, got {provider.connection_type!r}"
            )
        self._provider = provider
        self._on_audio_out = on_audio_out
        self._on_event_for_frontend = on_event_for_frontend
        self._session_id = session_id or "anonymous"
        # If supplied, the relay will attempt to reopen the upstream WS
        # after recoverable failures (see ``_RECONNECTABLE_ERR_SUBSTRINGS``).
        # The callback rebuilds a fresh ``session.update`` payload because
        # history may have grown since the original start().
        self._rebuild_session_update = rebuild_session_update
        self._max_reconnects = max_reconnects
        self._reconnect_count = 0
        # Separate counter for goAway-driven reconnects, used purely
        # for unique task names; goAway reconnects are NOT subject to
        # ``max_reconnects`` since they're protocol-driven by the
        # upstream provider.
        self._goaway_reconnect_count = 0

        self._ws = None  # type: ignore[assignment]
        self._drain_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()
        # Serializes every write to the upstream WS. The websockets legacy
        # protocol has a single drain waiter and asserts on concurrent
        # writers; with three send paths (control events, mic audio, our
        # silent keepalive) plus the library's own ping/pong, we MUST
        # interlock or we'll occasionally tear the connection down with
        # "Set changed size during iteration"-style asserts.
        self._send_lock = asyncio.Lock()
        # Rings of recent frames in BOTH directions.  When the upstream
        # WS dies, dumping both side-by-side shows what we sent, what
        # they sent back, and which way the close came from.
        self._sent_history: deque[dict[str, Any]] = deque(maxlen=_FRAME_HISTORY_SIZE)
        self._recv_history: deque[dict[str, Any]] = deque(maxlen=_FRAME_HISTORY_SIZE)
        # Provider-driven event gating.  Outbound frames that
        # :meth:`BaseVoiceProvider.should_gate_event` rejects are
        # parked here; we replay the most recent one each time
        # :meth:`BaseVoiceProvider.gate_cleared` reports the gate has
        # lifted.  The legacy Qwen behaviour (defer ``response.create``
        # while another response is active) is now implemented entirely
        # inside :class:`QwenVoiceProvider` via the gate-hook pair.
        self._deferred_events: list[dict[str, Any]] = []

        # Latched flag — set when the provider asked us to close the
        # upstream WS via :meth:`BaseVoiceProvider.should_close_after_event`
        # (Gemini Live's ``goAway``). The close we issue is a clean 1000,
        # which the drain loop sees as an orderly exit (no exception), so
        # we'd otherwise silently return without reconnecting. Reading
        # this flag at the drain's natural exit routes us into the
        # reconnect path the same way an unrecoverable upstream error
        # would.
        self._pending_reconnect: bool = False

        # Handshake gate. ``send_audio`` and ``send_event`` await this
        # before forwarding upstream so we never send mic/control frames
        # before the server has acknowledged ``setup`` — Gemini Live
        # enforces this strictly and force-closes with WS 1008
        # "BidiGenerateContent session expired" when violated. For
        # ``server_first`` providers (OpenAI / Qwen) the post-
        # ``session.created`` send in ``_open_and_handshake`` sets this
        # immediately; for ``client_first`` providers (Gemini Live) the
        # drain loop sets it when ``setupComplete`` arrives.
        self._handshake_complete: asyncio.Event = asyncio.Event()

        # --- Increment C: parameterised reconnect + held outbound queue ---
        # Single lock around the entire close → rebuild → handshake
        # sequence inside :meth:`_try_reconnect`. Concurrent callers
        # coalesce: the first wins the lock, the rest wait and observe
        # the outcome via ``_handshake_complete``. Closes the 2026-06-04
        # 01:11 "duplicate setup frames on reconnect race" incident.
        self._reconnect_lock: asyncio.Lock = asyncio.Lock()
        # True while inside the locked section. ``send_event`` reads this
        # to decide between "wait on handshake" (initial connect) and
        # "queue into _held_outbound" (mid-reconnect).
        self._in_reconnect: bool = False
        # Monotonic counter of completed reconnect attempts (success OR
        # fail). Used to coalesce concurrent ``_try_reconnect`` callers:
        # if the counter advanced between a caller's entry and its lock
        # acquisition, a prior caller already attempted, so this caller
        # short-circuits. Distinct from ``_reconnect_count`` which only
        # tracks recoverable-error successes against ``max_reconnects``.
        self._reconnect_attempts_completed: int = 0
        # Frames parked while mid-reconnect; flushed in order after the
        # new upstream's setupComplete. Bounded by ``HELD_OUTBOUND_CAP``
        # (drop-oldest) so a prolonged reconnect can't OOM the relay.
        self._held_outbound: list[dict[str, Any]] = []
        # Per-WS-instance idempotency guard — :meth:`_open_and_handshake`
        # records the WS here after shipping the setup frame; a second
        # call on the SAME instance raises. WeakSet so finished WSes
        # don't pin in memory.
        self._setup_sent_for_ws: WeakSet = WeakSet()

        # --- observability state ---
        self._started_at: float = 0.0  # set in start()
        self._first_audio_in_at: float | None = None
        self._first_audio_out_at: float | None = None
        self._last_send_at: float = 0.0
        self._last_recv_at: float = 0.0
        # Tracks the last REAL mic chunk separately from _last_send_at,
        # so keepalive ticks don't reset the keepalive clock.
        self._last_audio_in_at: float = 0.0
        self._audio_in_chunks = 0
        self._audio_out_chunks = 0
        self._audio_in_bytes = 0
        self._audio_out_bytes = 0
        self._sent_counts: Counter[str] = Counter()
        self._recv_counts: Counter[str] = Counter()

        # Per-session log file — opened lazily in start().  All
        # lifecycle / frame events for this session land here in one
        # place so post-mortem is "open this one file."
        self._log_file_path: Path | None = None
        self._log_file = None  # type: ignore[assignment]

        # Client-side VAD (manual mode). Set in start() when both
        # QWEN_MANUAL_VAD=1 and the provider declares a sample rate.
        # When active, send_audio runs each chunk through the VAD and
        # emits commit + response.create on detected speech_stopped
        # (and a safety commit at _MANUAL_VAD_SAFETY_COMMIT_S seconds
        # of continuous speech).
        self._manual_vad: voice_vad.VoiceVAD | None = None
        # Increment B — user-tunable VAD params threaded from
        # ``assistant_config.json``. None falls back to the VoiceVAD
        # constructor defaults (which equal the documented HEAD
        # constants — parity test pins this).
        self._vad_threshold = vad_threshold
        self._vad_min_silence_ms = vad_min_silence_ms
        # Increment F — central voice-pipeline timeouts. Default to
        # HEAD constants exactly (parity-tested).
        self._voice_timeouts: VoiceTimeouts = (
            voice_timeouts if voice_timeouts is not None else VoiceTimeouts.default()
        )
        # Timestamp of the most recent speech_started event (monotonic).
        # Used by the safety-commit watchdog.
        self._manual_vad_speech_started_at: float | None = None
        # Increment B observability: timestamp of the last
        # ``voice_vad_state`` heartbeat broadcast. While ``is_speech``
        # is True the relay emits a fresh ``state=listening`` frame
        # every ~_VAD_STATE_HEARTBEAT_S so the UI's duration clock
        # advances even if Silero never transitions.
        self._last_vad_state_emit_at: float | None = None

        # ``VOICE_DEBUG_DUMP_MIC=1`` — write every received mic byte to
        # ``logs/voice/<ts>_<sid>.mic.wav`` (16-bit PCM, mono, at the
        # provider's declared audio_in_sample_rate). Lets us listen to
        # what Silero is actually receiving when the manual-VAD path
        # silently fails to detect speech.
        self._mic_dump_path: Path | None = None
        self._mic_dump_file = None  # type: ignore[assignment]
        self._mic_dump_bytes: int = 0

    @property
    def is_running(self) -> bool:
        return self._drain_task is not None and not self._drain_task.done()

    @property
    def session_id(self) -> str:
        return self._session_id

    # --- log helpers -------------------------------------------------------

    def _now_rel(self) -> float:
        """Seconds since :meth:`start` began."""
        return time.monotonic() - self._started_at if self._started_at else 0.0

    def _open_session_log(self) -> None:
        """Create logs/voice/<session_id>.log and seed the header."""
        try:
            _VOICE_LOG_DIR.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            self._log_file_path = _VOICE_LOG_DIR / f"{ts}_{self._session_id}.log"
            self._log_file = open(self._log_file_path, "a", buffering=1, encoding="utf-8")
            self._slog(
                f"open provider={self._provider.provider_name}"
                f" model={self._provider.model}"
                f" voice={getattr(self._provider, 'voice', '?')}"
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to open voice session log file")
            self._log_file = None

    def _slog(self, msg: str) -> None:
        """Append a timestamped line to the per-session log file.

        Best-effort — failures don't propagate.  The line is also
        emitted at debug level on the main logger so it shows up in
        `api_*.log` if you crank verbosity.
        """
        line = f"[t+{self._now_rel():7.2f}s] {msg}"
        logger.debug("voice[%s] %s", self._session_id, line)
        if self._log_file is None:
            return
        try:
            self._log_file.write(line + "\n")
        except Exception:  # noqa: BLE001
            pass

    def _close_session_log(self) -> None:
        # Finalise the mic WAV (if open) before closing the text log
        # so its "mic dump closed" line lands in the same session log.
        self._close_mic_dump()
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:  # noqa: BLE001
                pass
            self._log_file = None

    def _open_mic_dump(self, sample_rate: int) -> None:
        """Open ``logs/voice/<ts>_<sid>.mic.wav`` for raw mic capture.

        Opt-in via ``VOICE_DEBUG_DUMP_MIC=1``. Writes a placeholder WAV
        header now (we'll patch the byte count in ``_close_mic_dump``
        once we know the total). Mono, 16-bit PCM, sample rate from
        the provider.
        """
        if os.environ.get("VOICE_DEBUG_DUMP_MIC") != "1":
            return
        if self._log_file_path is None:
            return
        try:
            self._mic_dump_path = self._log_file_path.with_suffix(".mic.wav")
            self._mic_dump_file = open(self._mic_dump_path, "wb")
            # 44-byte RIFF/WAVE header; fmt = PCM16 mono. Sizes are
            # placeholders — patched on close.
            self._mic_dump_file.write(self._wav_header(
                sample_rate=sample_rate, num_channels=1, bits_per_sample=16,
                data_size=0,
            ))
            self._mic_dump_bytes = 0
            self._slog(f"mic dump open: {self._mic_dump_path.name} sr={sample_rate}Hz")
        except Exception:  # noqa: BLE001
            logger.exception("Failed to open mic dump file")
            self._mic_dump_file = None

    @staticmethod
    def _wav_header(sample_rate: int, num_channels: int, bits_per_sample: int, data_size: int) -> bytes:
        import struct
        byte_rate = sample_rate * num_channels * bits_per_sample // 8
        block_align = num_channels * bits_per_sample // 8
        return (
            b"RIFF"
            + struct.pack("<I", 36 + data_size)
            + b"WAVE"
            + b"fmt "
            + struct.pack("<IHHIIHH", 16, 1, num_channels, sample_rate, byte_rate, block_align, bits_per_sample)
            + b"data"
            + struct.pack("<I", data_size)
        )

    def _write_mic_dump(self, pcm: bytes) -> None:
        if self._mic_dump_file is None:
            return
        try:
            self._mic_dump_file.write(pcm)
            self._mic_dump_bytes += len(pcm)
        except Exception:  # noqa: BLE001
            pass

    def _close_mic_dump(self) -> None:
        """Patch the WAV header with the real data size and close."""
        if self._mic_dump_file is None:
            return
        try:
            sr = getattr(self._provider, "audio_in_sample_rate", 16000) or 16000
            self._mic_dump_file.seek(0)
            self._mic_dump_file.write(self._wav_header(
                sample_rate=sr, num_channels=1, bits_per_sample=16,
                data_size=self._mic_dump_bytes,
            ))
            self._mic_dump_file.close()
            self._slog(
                f"mic dump closed: {self._mic_dump_bytes}B "
                f"(~{self._mic_dump_bytes / (sr * 2):.1f}s)"
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to finalise mic dump")
        finally:
            self._mic_dump_file = None

    async def start(self, session_config: dict[str, Any]) -> None:
        """Open the upstream WS and seed it with ``session.update``.

        ``session_config`` is the payload returned by
        :meth:`BaseVoiceProvider.format_session_config` — the relay sends
        it as the first message so the provider knows the system prompt,
        tools, voice, and VAD config before any audio arrives.
        """
        self._started_at = time.monotonic()
        self._open_session_log()
        logger.info(
            "voice_relay started session_id=%s provider=%s model=%s voice=%s",
            self._session_id,
            self._provider.provider_name,
            self._provider.model,
            getattr(self._provider, "voice", "?"),
        )

        # Let the frontend show a "preparing" indicator while the
        # upstream handshake completes. For server_first providers the
        # gate opens inside _open_and_handshake (synchronous from the
        # caller's POV); for client_first (Gemini) it opens when the
        # drain task receives setupComplete.
        await self._on_event_for_frontend({
            "type": "voice_status",
            "status": "preparing",
        })

        await self._open_and_handshake(session_config)
        self._drain_task = asyncio.create_task(self._drain(), name=f"voice-relay-{self._provider.provider_name}")

        # server_first providers complete the handshake synchronously
        # inside _open_and_handshake; announce ready here so the UI gets
        # the same signal it would for client_first via the drain loop.
        if self._handshake_complete.is_set():
            await self._on_event_for_frontend({
                "type": "voice_status",
                "status": "ready",
            })

        # Initialise client-side VAD when (a) the provider declares
        # support via ``supports_manual_vad`` (= it overrode the
        # ``manual_vad_*_frames`` hooks), (b) the per-provider env
        # toggle hasn't been flipped off, and (c) the provider tells us
        # its mic sample rate. The model load is ~50ms on a Jetson and
        # we only do it once per session. getattr-default for the rate
        # keeps test mocks that don't implement the full
        # BaseVoiceProvider surface working.
        in_sr = getattr(self._provider, "audio_in_sample_rate", None)
        supports_manual = getattr(self._provider, "supports_manual_vad", False)
        if (
            supports_manual
            and voice_vad.is_enabled_for(self._provider.provider_name)
            and in_sr is not None
        ):
            try:
                vad_kwargs: dict[str, object] = {"input_sample_rate": in_sr}
                if self._vad_threshold is not None:
                    vad_kwargs["threshold"] = self._vad_threshold
                if self._vad_min_silence_ms is not None:
                    vad_kwargs["min_silence_duration_ms"] = self._vad_min_silence_ms
                self._manual_vad = voice_vad.VoiceVAD(**vad_kwargs)  # type: ignore[arg-type]
                self._slog(
                    f"manual_vad init: in_sr={in_sr}Hz "
                    f"threshold={self._vad_threshold} "
                    f"min_silence_ms={self._vad_min_silence_ms}"
                )
            except Exception:  # noqa: BLE001
                logger.exception("manual_vad init failed; falling back to server VAD")
                self._manual_vad = None

        # Optional raw-mic capture (VOICE_DEBUG_DUMP_MIC=1). Independent
        # of manual VAD so it works even when server VAD is in use; the
        # captured WAV lets us listen to exactly what the relay (and
        # Silero, if active) is receiving.
        if in_sr is not None:
            self._open_mic_dump(in_sr)

        # Keepalive task — only if the provider opts in via
        # build_keepalive_chunk() returning a non-None chunk.  Qwen-Omni
        # needs this to keep its ASR pipeline from timing out after
        # ~3-5 min of audio silence; other providers default to None
        # and skip the task entirely.
        if self._provider.build_keepalive_chunk() is not None:
            self._keepalive_task = asyncio.create_task(
                self._keepalive_loop(),
                name=f"voice-keepalive-{self._provider.provider_name}",
            )

    async def _open_and_handshake(self, session_config: dict[str, Any]) -> None:
        """Open upstream WS, drain ``session.created``, send our config.

        Shared by :meth:`start` and :meth:`_try_reconnect` — the latter
        rebuilds the WS without restarting the drain task or the
        keepalive (those are restarted by the caller).
        """
        self._ws = await self._provider.open_upstream()
        self._slog("upstream connected")

        # Increment C: idempotency guard. If we ever end up calling
        # _open_and_handshake twice on the SAME ``_ws`` instance (the
        # 2026-06-04 race that shipped two setup frames on one socket),
        # raise before touching the wire. The single ``_reconnect_lock``
        # in :meth:`_try_reconnect` already prevents the underlying race,
        # but the guard stays as a defensive belt — cheap to check, and
        # the test in tests/test_voice_reconnect_lock_and_queue.py pins
        # that the guard fires loud rather than silently double-setup.
        if self._ws in self._setup_sent_for_ws:
            raise RuntimeError(
                "setup_already_sent: refusing duplicate setup frame on "
                f"WS instance for session {self._session_id}"
            )
        self._setup_sent_for_ws.add(self._ws)

        direction = getattr(self._provider, "handshake_direction", "server_first")

        if direction == "server_first":
            # session.created is pushed by the server unprompted — drain it
            # so the drain task starts in a clean state.
            try:
                first = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
                first_event = json.loads(first)
                self._record_recv(first_event)
                self._slog(f"recv session.created in {self._now_rel():.2f}s")
                logger.info(
                    "voice_relay session.created session_id=%s in %.2fs",
                    self._session_id, self._now_rel(),
                )
                await self._provider.inject_event(first_event)
                await self._on_event_for_frontend(first_event)
            except asyncio.TimeoutError:
                logger.warning(
                    "voice_relay no session.created within 10s session_id=%s",
                    self._session_id,
                )
                self._slog("WARN no session.created within 10s")
        # else: client_first — Gemini Live expects the setup payload to
        # be the very first frame; the server replies with setupComplete
        # which the drain loop will handle in line with subsequent frames.

        # Push our session config upstream.
        self._record_sent(session_config)
        # The session-config payload shape varies by provider — OpenAI / Qwen
        # nest under "session", Gemini Live nests under "setup".  Probe both
        # so the log is informative regardless.
        sess = session_config.get("session") or session_config.get("setup") or {}
        instr_obj = sess.get("instructions") or sess.get("systemInstruction")
        if isinstance(instr_obj, str):
            instr_size = len(instr_obj)
        elif isinstance(instr_obj, dict):
            # Gemini: {"parts": [{"text": "..."}]}
            parts = instr_obj.get("parts", [])
            instr_size = sum(len(p.get("text", "")) for p in parts if isinstance(p, dict))
        else:
            instr_size = 0
        tools_obj = sess.get("tools") or []
        if tools_obj and isinstance(tools_obj[0], dict) and "functionDeclarations" in tools_obj[0]:
            tools_count = len(tools_obj[0]["functionDeclarations"])
        else:
            tools_count = len(tools_obj)
        self._slog(
            f"send session.update instructions={instr_size}B tools={tools_count}"
            f" direction={direction}"
        )
        # Gemini debug: dump the *non-instruction, non-tool* setup fields so
        # we can confirm outputAudioTranscription / responseModalities are
        # actually in the wire bytes (the full systemInstruction + tool
        # declarations are dropped — they bloat the log without telling us
        # anything about the transcription wiring).
        if (
            self._provider.provider_name == "google"
            and os.environ.get("VOICE_DEBUG_GEMINI_BODIES") == "1"
        ):
            sess_keys = {k: v for k, v in sess.items() if k not in ("systemInstruction", "tools")}
            self._slog(f"  setup_keys={sorted(sess.keys())}")
            self._slog(f"  setup_minus_instructions_tools={json.dumps(sess_keys)[:3000]}")
        # Qwen debug: dump session keys + tool names + a slim copy of the
        # session block (instructions truncated, tools reduced to names)
        # so we can spot DashScope-side schema rejections without spamming
        # the log with 24 KB of system prompt. Opt-in via
        # VOICE_DEBUG_QWEN_BODIES=1.
        if (
            self._provider.provider_name == "qwen"
            and os.environ.get("VOICE_DEBUG_QWEN_BODIES") == "1"
        ):
            slim = dict(sess)
            instr = slim.get("instructions", "")
            slim["instructions"] = f"<truncated:{len(instr)}B>"
            tnames = []
            for t in slim.get("tools", []) or []:
                tnames.append(t.get("name") or t.get("function", {}).get("name") or "?")
            slim["tools"] = tnames
            self._slog(f"  qwen_session_keys={sorted(sess.keys())}")
            self._slog(f"  qwen_session_slim={json.dumps(slim)[:3000]}")
            # Also dump the FULL session.update payload to a sibling
            # file so we can diff against Alibaba's published example
            # without searching through the slog.
            try:
                dump = self._log_file_path.with_suffix(".session_update.json") if self._log_file_path else None
                if dump is not None:
                    dump.write_text(json.dumps(session_config, indent=2))
                    self._slog(f"  qwen_full_payload_dumped={dump}")
            except Exception:
                logger.exception("Failed to dump qwen session.update payload")
        async with self._send_lock:
            await self._ws.send(json.dumps(session_config))
        self._last_send_at = time.monotonic()

        # server_first providers (OpenAI / Qwen) were already greeted with
        # session.created above and ack the session.update they just got;
        # the gate opens immediately. client_first (Gemini Live) waits for
        # setupComplete in the drain loop before opening the gate.
        if direction == "server_first":
            self._handshake_complete.set()

    def _record_sent(self, event: dict[str, Any]) -> None:
        """Stash a sent control frame for post-mortem on upstream close.

        Stores a truncated/redacted copy — full audio bodies and overly
        long strings are clipped to keep the log readable.  Also bumps
        the per-type counter and slogs a one-line summary.
        """
        self._sent_history.append(_redact_for_log(event))
        evt_type = event.get("type", "?")
        self._sent_counts[evt_type] += 1
        size = len(json.dumps(event))
        self._slog(f"send  type={evt_type} size={size}B")

    def _record_recv(self, event: dict[str, Any]) -> None:
        """Mirror of :meth:`_record_sent` for inbound frames."""
        self._recv_history.append(_redact_for_log(event))
        evt_type = event.get("type", "?")
        self._recv_counts[evt_type] += 1
        # Don't include size here — many recv events are huge audio chunks
        # already accounted for separately.

    async def send_event(self, event: dict[str, Any]) -> None:
        """Forward a frontend control event upstream verbatim.

        The provider may gate certain event types via
        :meth:`BaseVoiceProvider.should_gate_event` — gated events are
        parked until :meth:`BaseVoiceProvider.gate_cleared` reports the
        gate has lifted (driven by upstream events through the drain
        loop).
        """
        if self._closed.is_set():
            return  # Drop silently — caller already saw an error event.

        # Increment C: if we're mid-reconnect (lock held by another
        # coroutine, handshake gate closed), park the frame on
        # ``_held_outbound`` instead of writing to a dead WS. The flush
        # at the end of :meth:`_try_reconnect` replays the queue after
        # the new ``setupComplete``.  Bounded at ``HELD_OUTBOUND_CAP``
        # (drop-oldest) so a prolonged reconnect can't OOM the relay.
        if self._in_reconnect and not self._handshake_complete.is_set():
            if len(self._held_outbound) >= HELD_OUTBOUND_CAP:
                dropped = self._held_outbound.pop(0)
                self._slog(
                    f"held_outbound drop-oldest type={dropped.get('type')} "
                    f"queue_size={HELD_OUTBOUND_CAP}"
                )
            self._held_outbound.append(event)
            return

        if self._ws is None:
            return

        # Wait for the upstream handshake to ack before forwarding
        # anything (see ``_handshake_complete`` for details). Bound the
        # wait so a botched handshake doesn't pin frontend frames forever.
        if not self._handshake_complete.is_set():
            try:
                await asyncio.wait_for(self._handshake_complete.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                self._slog(f"WARN handshake gate timeout dropping event type={event.get('type')}")
                return

        if self._provider.should_gate_event(event):
            self._slog(f"defer event type={event.get('type')} (provider gate active)")
            self._deferred_events.append(event)
            return

        self._record_sent(event)
        try:
            async with self._send_lock:
                await self._ws.send(json.dumps(event))
            self._last_send_at = time.monotonic()
        except Exception as e:  # noqa: BLE001
            # Upstream is gone; the drain task already surfaced the error.
            self._slog(f"WARN send dropped (upstream closed): type={event.get('type')} err={e}")

    async def send_shutdown_frames(self, frames: list[dict[str, Any]]) -> None:
        """Send the provider's graceful-shutdown frames just before WS close.

        Bypasses gating (we're closing anyway) but honours the send lock
        and the closed/handshake checks so we never write to a dead WS.
        Errors are swallowed: ``end_voice`` always closes the WS after
        this returns, so a flush failure just means the upstream sees
        the close without a polite goodbye.
        """
        if self._ws is None or self._closed.is_set():
            return
        if not self._handshake_complete.is_set():
            # No handshake → no point sending shutdown frames; just close.
            return
        for frame in frames:
            try:
                async with self._send_lock:
                    if self._ws is None or self._closed.is_set():
                        return
                    await self._ws.send(json.dumps(frame))
                self._last_send_at = time.monotonic()
                self._slog(f"shutdown frame sent type={frame.get('type') or list(frame.keys())[:1]}")
            except Exception as e:  # noqa: BLE001
                self._slog(f"WARN shutdown frame send failed: {e}")
                return  # upstream is gone; the close will catch up

    async def send_audio(self, pcm_b64: str) -> None:
        """Forward a frontend mic chunk upstream as a provider-specific append.

        The provider's :meth:`BaseVoiceProvider.format_audio_in` decides
        the on-the-wire shape (Qwen: ``input_audio_buffer.append``;
        Gemini Live: ``realtimeInput.audio``).
        """
        if self._ws is None or self._closed.is_set():
            return  # Drop silently after upstream close — frontend already notified.
        # Wait for the upstream handshake to ack. Gemini Live force-closes
        # with WS 1008 "BidiGenerateContent session expired" when audio
        # arrives before ``setupComplete``; dropping pre-handshake mic
        # frames is safer than triggering that close.
        if not self._handshake_complete.is_set():
            try:
                await asyncio.wait_for(self._handshake_complete.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                self._slog("WARN handshake gate timeout dropping audio_in chunk")
                return
        try:
            async with self._send_lock:
                await self._ws.send(json.dumps(self._provider.format_audio_in(pcm_b64)))
            now = time.monotonic()
            self._last_send_at = now
            self._last_audio_in_at = now
            self._audio_in_chunks += 1
            # base64 encodes 3 bytes → 4 chars; PCM byte size ≈ len * 3/4.
            self._audio_in_bytes += (len(pcm_b64) * 3) // 4
            if self._first_audio_in_at is None:
                self._first_audio_in_at = self._now_rel()
                self._slog(f"first audio_in at t+{self._first_audio_in_at:.2f}s")
                logger.info(
                    "voice_relay first audio_in session_id=%s in %.2fs",
                    self._session_id, self._first_audio_in_at,
                )
            elif self._audio_in_chunks % _AUDIO_LOG_SAMPLE_EVERY == 0:
                self._slog(
                    f"audio_in chunks={self._audio_in_chunks}"
                    f" bytes={self._audio_in_bytes}"
                )
        except Exception as e:  # noqa: BLE001
            self._slog(f"WARN audio_in dropped (upstream closed): err={e}")
            return

        # Mirror to the WAV dump (cheap, infrequent decode — only when
        # VOICE_DEBUG_DUMP_MIC=1 opened the file).
        if self._mic_dump_file is not None:
            try:
                self._write_mic_dump(base64.b64decode(pcm_b64))
            except Exception:  # noqa: BLE001
                pass

        # Manual-VAD path: feed the same chunk to our local VAD and
        # commit + response.create when the user stops speaking. We do
        # this AFTER the upstream send so a slow VAD never delays mic
        # frames reaching DashScope.
        if self._manual_vad is not None:
            try:
                await self._run_manual_vad(pcm_b64)
            except Exception:  # noqa: BLE001
                logger.exception("manual_vad processing failed; disabling for this session")
                self._manual_vad = None

    async def _run_manual_vad(self, pcm_b64: str) -> None:
        """Feed a mic chunk through the local VAD; emit events on transitions.

        Called for every ``send_audio`` chunk when manual VAD is active
        for this provider. Emits synthetic
        ``input_audio_buffer.speech_started/stopped`` events on the
        frontend channel so the UI's "thinking" state still flips, and
        sends the provider's wire-specific manual-VAD frames upstream
        (see :meth:`BaseVoiceProvider.manual_vad_start_frames` /
        ``manual_vad_stop_frames`` / ``manual_vad_safety_commit_frames``).

        Increment B adds a parallel ``voice_vad_state`` broadcast (see
        :meth:`_emit_voice_vad_state`) — additive observability that
        doesn't affect any existing event.
        """
        assert self._manual_vad is not None
        pcm = base64.b64decode(pcm_b64)
        for event in self._manual_vad.feed_pcm16(pcm):
            if event.kind == "speech_started":
                self._manual_vad_speech_started_at = time.monotonic()
                self._slog(f"manual_vad: speech_started at t+{self._now_rel():.2f}s")
                await self._on_event_for_frontend({
                    "type": "input_audio_buffer.speech_started",
                })
                # Increment B: broadcast the new typed VAD state so the
                # UI can render a duration clock + confidence indicator.
                await self._emit_voice_vad_state("listening")
                for frame in self._provider.manual_vad_start_frames():
                    await self.send_event(frame)
            elif event.kind == "speech_stopped":
                self._manual_vad_speech_started_at = None
                self._slog(f"manual_vad: speech_stopped at t+{self._now_rel():.2f}s, committing")
                await self._on_event_for_frontend({
                    "type": "input_audio_buffer.speech_stopped",
                })
                await self._emit_voice_vad_state("thinking")
                for frame in self._provider.manual_vad_stop_frames():
                    await self.send_event(frame)

        # Increment B observability heartbeat: while the VAD is still
        # in ``speech_started`` (no transition this chunk), re-emit
        # ``voice_vad_state`` every ~1s so the UI duration clock
        # advances. Driven from this method (no asyncio.Task) so we
        # don't add an extra coroutine to the relay's lifecycle.
        if self._manual_vad.is_speech and self._manual_vad_speech_started_at is not None:
            now = time.monotonic()
            last = self._last_vad_state_emit_at
            if last is None or (now - last) >= self._voice_timeouts.vad_state_heartbeat_s:
                await self._emit_voice_vad_state("listening")

        # Safety commit: some upstreams cap continuous audio in manual
        # mode (DashScope: 60s; Gemini: no documented cap but we chunk
        # defensively anyway to avoid losing the segment if the upstream
        # silently truncates). When our VAD is still saying "speech"
        # past _MANUAL_VAD_SAFETY_COMMIT_S, close the current segment
        # WITHOUT triggering a model response — the user is still
        # mid-monologue. The provider's ``manual_vad_safety_commit_frames``
        # owns the exact wire shape; if it returns an empty list (its
        # default) the safety path is a no-op for that provider.
        started_at = self._manual_vad_speech_started_at
        if (
            started_at is not None
            and (time.monotonic() - started_at) >= self._voice_timeouts.manual_vad_safety_commit_s
        ):
            safety_frames = self._provider.manual_vad_safety_commit_frames()
            if safety_frames:
                self._slog(
                    f"manual_vad: SAFETY commit (NO response) at t+{self._now_rel():.2f}s "
                    f"(speech ran {time.monotonic() - started_at:.1f}s)"
                )
                # Reset the timer so subsequent 50s windows trigger again
                # without flipping VAD state — the user is still speaking.
                self._manual_vad_speech_started_at = time.monotonic()
                for frame in safety_frames:
                    await self.send_event(frame)

    async def _emit_voice_vad_state(self, state: str) -> None:
        """Increment B: broadcast typed VAD state to the frontend.

        Payload shape (wire contract — clients switch on these keys):

            {
              "type": "voice_vad_state",
              "state": "listening" | "thinking" | "idle",
              "duration_ms": int,    // since speech_started; 0 otherwise
              "silero_prob": float | None,
            }

        Additive event — does NOT replace the existing
        ``input_audio_buffer.speech_started/stopped`` broadcasts.

        Called by :meth:`_run_manual_vad` on every state transition AND
        every ``_VAD_STATE_HEARTBEAT_S`` while the VAD remains in
        ``listening`` (so the UI clock advances even when Silero
        doesn't transition for tens of seconds).
        """
        now = time.monotonic()
        started_at = self._manual_vad_speech_started_at
        duration_ms = 0
        if started_at is not None:
            duration_ms = int((now - started_at) * 1000)
        prob: float | None = None
        if self._manual_vad is not None:
            prob = self._manual_vad.last_silero_prob
        self._last_vad_state_emit_at = now
        await self._on_event_for_frontend({
            "type": "voice_vad_state",
            "state": state,
            "duration_ms": duration_ms,
            "silero_prob": prob,
        })

    async def stop(self) -> None:
        """Cancel the drain task and close the upstream WS."""
        if self._keepalive_task is not None and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._drain_task is not None and not self._drain_task.done():
            self._drain_task.cancel()
            try:
                await self._drain_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        self._closed.set()
        # Best-effort: emit a final summary on clean stop too (the drain
        # task already does this on error close — guard against
        # double-logging via a flag).
        if not getattr(self, "_summary_logged", False):
            self._log_close_summary("clean (stop called)")
        self._close_session_log()

    def _log_close_summary(self, reason: str) -> None:
        """One-line structured summary of the entire session.

        Called on both clean and error closes.  Idempotent — second
        call is a no-op.
        """
        if getattr(self, "_summary_logged", False):
            return
        self._summary_logged = True

        duration = self._now_rel()
        last_send = (time.monotonic() - self._last_send_at) if self._last_send_at else None
        last_recv = (time.monotonic() - self._last_recv_at) if self._last_recv_at else None
        sent_d = dict(self._sent_counts)
        recv_d = dict(self._recv_counts)

        ws_close_code = None
        ws_close_reason = None
        if self._ws is not None:
            ws_close_code = getattr(self._ws, "close_code", None)
            ws_close_reason = getattr(self._ws, "close_reason", None)

        summary = (
            f"voice_session_closed session_id={self._session_id}"
            f" provider={self._provider.provider_name}"
            f" duration={duration:.1f}s"
            f" reason={reason!r}"
            f" ws_code={ws_close_code} ws_reason={ws_close_reason!r}"
            f" sent={sent_d}"
            f" recv={recv_d}"
            f" audio_in=(chunks={self._audio_in_chunks},bytes={self._audio_in_bytes})"
            f" audio_out=(chunks={self._audio_out_chunks},bytes={self._audio_out_bytes})"
            f" first_audio_in={self._first_audio_in_at}"
            f" first_audio_out={self._first_audio_out_at}"
            f" last_send={last_send}s_ago"
            f" last_recv={last_recv}s_ago"
        )
        logger.warning(summary)
        self._slog(summary)

    # --- drain loop -------------------------------------------------------

    async def _keepalive_loop(self) -> None:
        """Send a tiny silent PCM chunk every 30s of audio silence.

        Driven by the provider's
        :meth:`BaseVoiceProvider.build_keepalive_chunk` hook.  The loop
        only spawns when that hook returns a non-None chunk (see
        :meth:`start`).  Qwen-Omni uses this to keep its ASR pipeline
        from timing out after multi-minute silences; other providers
        leave the default ``None`` and the loop never runs.

        Aborted on cancellation; logs each tick at debug level.
        """
        assert self._ws is not None
        try:
            while not self._closed.is_set():
                keepalive_s = self._voice_timeouts.keepalive_s
                await asyncio.sleep(keepalive_s / 2)
                if self._closed.is_set() or self._ws is None:
                    break
                # Skip if a real audio chunk went out within the interval.
                idle = time.monotonic() - (self._last_audio_in_at or self._started_at)
                if idle < keepalive_s:
                    continue
                try:
                    chunk = self._provider.build_keepalive_chunk()
                    if chunk is None:
                        # Provider stopped wanting keepalive — exit cleanly.
                        return
                    async with self._send_lock:
                        await self._ws.send(json.dumps(self._provider.format_audio_in(chunk)))
                    self._last_send_at = time.monotonic()
                    self._slog(f"keepalive sent (idle={idle:.1f}s)")
                except Exception as e:  # noqa: BLE001
                    self._slog(f"WARN keepalive failed: {e}")
                    # Don't break — drain task will surface the close.
        except asyncio.CancelledError:
            raise

    async def _drain(self) -> None:
        """Pump upstream messages until the WS closes.

        Audio chunks (``response.audio.delta``) are forwarded to the
        frontend via ``on_audio_out``. All other JSON events are pushed
        into the provider's queue (so ``process_voice_event`` can handle
        tool execution + JSONL persistence) and also forwarded to the
        frontend so the UI can show transcripts/status.
        """
        assert self._ws is not None
        try:
            async for raw in self._ws:
                self._last_recv_at = time.monotonic()
                try:
                    event = json.loads(raw)
                except Exception:
                    logger.warning("VoiceRelay: non-JSON message dropped: %r", raw[:100])
                    self._slog(f"WARN non-JSON recv (dropped) bytes={len(raw)}")
                    continue

                # Pre-audio-filter Gemini debug: log EVERY incoming frame
                # (after stripping audio data so the log stays readable),
                # so we can see whether outputTranscription is bundled
                # alongside audio, hidden under an unknown field, or
                # simply never arriving. Opt-in via
                # VOICE_DEBUG_GEMINI_BODIES=1.
                if (
                    self._provider.provider_name == "google"
                    and os.environ.get("VOICE_DEBUG_GEMINI_BODIES") == "1"
                ):
                    self._slog(f"  raw_keys={sorted(_top_keys(event))}")
                    redacted = _redact_inline_audio(event)
                    # No truncation: we want to see every byte of the
                    # non-audio fields so an undocumented transcript path
                    # can't hide behind a cutoff.
                    self._slog(f"  raw_full={json.dumps(redacted)}")

                # Audio out → ship to frontend. The provider's
                # ``extract_audio_out`` knows the right field path.
                audio_b64 = self._provider.extract_audio_out(event)
                if audio_b64:
                    await self._on_audio_out(audio_b64)
                    self._audio_out_chunks += 1
                    self._audio_out_bytes += (len(audio_b64) * 3) // 4
                    if self._first_audio_out_at is None:
                        self._first_audio_out_at = self._now_rel()
                        self._slog(f"first audio_out at t+{self._first_audio_out_at:.2f}s")
                        logger.info(
                            "voice_relay first audio_out session_id=%s in %.2fs",
                            self._session_id, self._first_audio_out_at,
                        )
                    elif self._audio_out_chunks % _AUDIO_LOG_SAMPLE_EVERY == 0:
                        self._slog(
                            f"audio_out chunks={self._audio_out_chunks}"
                            f" bytes={self._audio_out_bytes}"
                        )
                    # Don't broadcast the audio event itself to the
                    # frontend — only the canonical audio_out payload.
                    # But still inject for any provider-internal state.
                    self._provider.on_inbound_event(event)
                    await self._provider.inject_event(event)
                    # Gemini Live bundles ``outputTranscription`` (and
                    # sometimes ``turnComplete`` / ``interrupted``) into
                    # the SAME frame as the audio chunk under
                    # ``serverContent``. If we ``continue`` here those
                    # control fields never reach the orchestrator /
                    # frontend, so the model's text reply never renders.
                    # Forward an audio-stripped copy of the event so the
                    # rest of the pipeline sees the control side.
                    control = _strip_audio_parts(event)
                    if control is not None:
                        await self._on_event_for_frontend(control)
                    continue

                # Non-audio control event — record for post-mortem,
                # bump counters, and slog.
                evt_type = event.get("type", "")
                self._record_recv(event)
                size = len(raw) if isinstance(raw, str) else len(raw or "")
                self._slog(f"recv  type={evt_type} size={size}B")

                # Gemini Live (client_first) acks the setup with
                # ``setupComplete``; open the handshake gate so deferred
                # send_audio / send_event calls can proceed.
                if not self._handshake_complete.is_set() and "setupComplete" in event:
                    self._handshake_complete.set()
                    self._slog("handshake gate opened (setupComplete)")
                    await self._on_event_for_frontend({
                        "type": "voice_status",
                        "status": "ready",
                    })
                # Gemini Live debug: log full body of control events so we
                # can see whether outputTranscription / turnComplete are
                # actually arriving. Opt-in via VOICE_DEBUG_GEMINI_BODIES=1.
                if (
                    self._provider.provider_name == "google"
                    and os.environ.get("VOICE_DEBUG_GEMINI_BODIES") == "1"
                ):
                    body = json.dumps(event)[:1000]
                    self._slog(f"  body={body}")

                # Let the provider update any internal gating state.
                self._provider.on_inbound_event(event)
                # If the gate just cleared, replay the most-recent
                # deferred event (older ones are stale — keep the queue
                # tail-only, matching the legacy Qwen "discard duplicate
                # response.create" behaviour).
                if self._deferred_events and self._provider.gate_cleared():
                    queued = self._deferred_events[-1]
                    self._deferred_events.clear()
                    await self.send_event(queued)

                # Surface upstream errors prominently — they often
                # precede a close with a more useful message than the
                # bare 1007 we see when the WS dies.
                if evt_type == "error":
                    err = event.get("error", {})
                    self._slog(f"ERR upstream error code={err.get('code')!r} msg={err.get('message')!r}")
                    logger.warning(
                        "voice_relay upstream error session_id=%s code=%s msg=%s",
                        self._session_id,
                        err.get("code"),
                        err.get("message"),
                    )

                # Control events → orchestrator pipeline + frontend mirror.
                await self._provider.inject_event(event)
                await self._on_event_for_frontend(event)

                # Some inbound signals require the client to close the
                # upstream WS per protocol — Gemini Live's ``goAway`` is
                # the motivating case. Closing here (clean 1000) makes
                # the drain loop exit naturally; the ``_pending_reconnect``
                # flag below routes us into the reconnect path instead
                # of waiting for Gemini's punitive 1008 "policy violation"
                # force-close (which the drain would catch as an error).
                if self._provider.should_close_after_event(event) and self._ws is not None:
                    # Heads-up to the frontend BEFORE we close: Gemini's
                    # goAway carries a ``timeLeft`` (~30-60s warning).
                    # The Android UI uses this to flash a banner + beep
                    # so the user knows a brief drop is coming.
                    go_away = event.get("goAway") if isinstance(event, dict) else None
                    time_left = go_away.get("timeLeft") if isinstance(go_away, dict) else None
                    await self._on_event_for_frontend({
                        "type": "voice_status",
                        "status": "reconnect_warning",
                        "time_left": time_left,
                    })
                    self._slog(f"reconnect_warning broadcast (time_left={time_left})")
                    self._slog("provider requested upstream close; closing 1000 to trigger reconnect")
                    logger.info(
                        "voice_relay provider-requested close session_id=%s",
                        self._session_id,
                    )
                    # Latch BEFORE the await so a fast clean-close + drain
                    # exit can't race us out of the loop with the flag unset.
                    self._pending_reconnect = True
                    try:
                        await self._ws.close(code=1000, reason="provider-requested close")
                    except Exception:  # noqa: BLE001
                        logger.exception("voice_relay close failed after provider request")
                    # Drain's ``async for`` exits naturally next iteration;
                    # the post-loop block reads ``_pending_reconnect``.

            # ``async for`` exited cleanly (clean close — typically the
            # one we issued in response to ``goAway``). If we asked for
            # it, route into the reconnect path the same way an
            # exception would. Otherwise the upstream simply went away
            # and we treat that as the end of the relay's life.
            if self._pending_reconnect:
                self._pending_reconnect = False
                synthetic = ConnectionError(
                    "provider-requested reconnect after goAway"
                )
                self._slog(f"DRAIN END clean (pending reconnect): {synthetic}")
                logger.info(
                    "voice_relay drain exited cleanly with pending reconnect session_id=%s",
                    self._session_id,
                )
                reconnected = await self._try_reconnect(
                    synthetic,
                    reason=ReconnectReason.PROVIDER_GOAWAY,
                )
                if reconnected:
                    self._drain_task = asyncio.create_task(
                        self._drain(),
                        name=f"voice-relay-{self._provider.provider_name}-goaway{self._goaway_reconnect_count}",
                    )
                    return
                # Fall through to the close path below: the reconnect
                # was refused (max_reconnects hit, no handle, etc.). We
                # don't have an exception object here, so synthesize the
                # close summary without one.
                self._closed.set()
                self._log_close_summary("clean close, reconnect refused")
                self._close_session_log()
                # Increment A — emit typed VoiceError envelope alongside
                # the legacy ``error`` event. No exception object here
                # (clean close), so the classifier sees None and falls
                # through to the relay's generic NETWORK fallback.
                await self._emit_voice_error_event(
                    exc=None,
                    fallback_message=(
                        f"Upstream {self._provider.provider_name} "
                        "reconnect refused after goAway"
                    ),
                )
                await self._on_event_for_frontend({
                    "type": "error",
                    "error": {
                        "code": "voice_relay_failed",
                        "message": (
                            f"Upstream {self._provider.provider_name} "
                            "reconnect refused after goAway"
                        ),
                    },
                })
                return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            # DashScope sometimes closes with a misleading 1007 + "<400>
            # InternalError.Algo.InvalidParameter: The provided URL does
            # not appear to be valid" — that boilerplate is its generic
            # validator response.  Dump BOTH sent + recv rings so we see
            # the full conversation context at the time of the close.
            logger.warning(
                "VoiceRelay drain ended for %s: %s",
                self._provider.provider_name,
                e,
            )
            self._slog(f"DRAIN END err={e}")

            sent_recent = list(self._sent_history)
            for i, frame in enumerate(sent_recent):
                offset = len(sent_recent) - i
                logger.warning(
                    "  sent[-%d] type=%s body=%s",
                    offset,
                    frame.get("type"),
                    json.dumps(frame)[:500],
                )
                self._slog(f"  sent[-{offset}] type={frame.get('type')} body={json.dumps(frame)[:500]}")

            recv_recent = list(self._recv_history)
            for i, frame in enumerate(recv_recent):
                offset = len(recv_recent) - i
                logger.warning(
                    "  recv[-%d] type=%s body=%s",
                    offset,
                    frame.get("type"),
                    json.dumps(frame)[:500],
                )
                self._slog(f"  recv[-{offset}] type={frame.get('type')} body={json.dumps(frame)[:500]}")

            # Increment A — classify the close BEFORE the reconnect
            # gate. If the classifier flags the close as
            # ``recoverable=False`` (quota / auth / model_unavailable /
            # context_full), short-circuit reconnect by zeroing
            # ``_max_reconnects`` so :meth:`_try_reconnect` refuses on
            # its first capacity check. This is authorised by plan §10
            # (2026-06-09 user decision) — quota errors are never
            # transient; retrying just delays the clear error message.
            classified = self._classify_close(e)
            if classified is not None and not classified.recoverable:
                self._max_reconnects = 0

            # Try to recover transparently before giving up — DashScope's
            # "InvalidParameter" 400 mid-session is almost always salvageable
            # by reopening with a fresh session.update.
            # Increment C — choose a ReconnectReason from the close.
            # Gemini's ``is_recoverable_error`` mutates state for the
            # stale-handle one-shot; if it just dropped a handle, route
            # via STALE_HANDLE policy (silent + handle already cleared).
            # Detect by snapshotting the handle before/after the gate.
            had_handle_before = getattr(self._provider, "_resumption_handle", None)
            recoverable_now = self._provider.is_recoverable_error(e)
            had_handle_after = getattr(self._provider, "_resumption_handle", None)
            if not recoverable_now:
                reconnected = False
                self._slog("reconnect skipped: provider classifies error as fatal")
            else:
                if (
                    had_handle_before is not None
                    and had_handle_after is None
                ):
                    reconnect_reason = ReconnectReason.STALE_HANDLE
                else:
                    reconnect_reason = ReconnectReason.RECOVERABLE_ERROR
                reconnected = await self._try_reconnect(e, reason=reconnect_reason)
            if reconnected:
                # Drain task chains into itself after a successful reconnect:
                # spin up a new drain so this one can return cleanly.  The
                # keepalive task is unaffected — it polls _ws which now
                # points at the new connection.
                self._drain_task = asyncio.create_task(
                    self._drain(),
                    name=f"voice-relay-{self._provider.provider_name}-reconnect{self._reconnect_count}",
                )
                return

            self._closed.set()
            self._log_close_summary(f"upstream drain failed: {e}")
            self._close_session_log()

            # Emit the typed VoiceError envelope first so up-to-date
            # clients render the categorised banner; the legacy
            # ``voice_relay_failed`` ``error`` event follows for
            # back-compat (older frontends + Android builds that haven't
            # been updated yet still see the generic error).
            await self._emit_voice_error_event(
                exc=e,
                fallback_message=(
                    f"Upstream {self._provider.provider_name} WS closed: {e}"
                ),
                precomputed=classified,
            )
            await self._on_event_for_frontend({
                "type": "error",
                "error": {
                    "code": "voice_relay_failed",
                    "message": f"Upstream {self._provider.provider_name} WS closed: {e}",
                },
            })

    def _classify_close(self, exc: BaseException | None) -> VoiceError | None:
        """Run the provider's ``classify_close_reason`` if it exists.

        Returns None when:
        - The provider doesn't implement the classifier (older fakes /
          providers that haven't been migrated yet).
        - The classifier itself returns None (no semantic match).

        Read-only contract: this never mutates provider state. Only
        :meth:`is_recoverable_error` does that; the relay calls both
        independently.
        """
        # ``hasattr`` check tolerates fake providers in existing tests
        # that don't subclass BaseVoiceProvider. Real providers always
        # have the method (default returns None) post-Increment-A.
        classifier = getattr(self._provider, "classify_close_reason", None)
        if classifier is None:
            return None

        ws_code: int | None = None
        ws_reason: str | None = None
        if self._ws is not None:
            ws_code = getattr(self._ws, "close_code", None)
            ws_reason = getattr(self._ws, "close_reason", None)

        try:
            return classifier(exc, ws_code, ws_reason)
        except Exception:  # noqa: BLE001
            # A classifier crash must not break the close path. Log and
            # fall through to the relay's generic NETWORK envelope.
            logger.exception(
                "voice_relay classify_close_reason raised for %s",
                self._provider.provider_name,
            )
            return None

    async def _emit_voice_error_event(
        self,
        *,
        exc: BaseException | None,
        fallback_message: str,
        precomputed: VoiceError | None = None,
    ) -> None:
        """Emit a typed ``voice_error`` event to the frontend.

        If the provider classifier returns a :class:`VoiceError`, use
        it. Otherwise synthesise a generic NETWORK envelope with
        ``recoverable=True`` (matches today's reconnect behavior for
        unclassified closes).

        Increment A wires this alongside the legacy ``error`` event so
        clients can opt into typed rendering incrementally.
        """
        err_obj = precomputed if precomputed is not None else self._classify_close(exc)

        if err_obj is None:
            ws_code: int | None = None
            ws_reason: str | None = None
            if self._ws is not None:
                ws_code = getattr(self._ws, "close_code", None)
                ws_reason = getattr(self._ws, "close_reason", None)
            err_obj = VoiceError(
                category=VoiceErrorCategory.NETWORK,
                message=fallback_message,
                recoverable=True,
                recovery_hint=None,
                provider_doc_url=None,
                raw_close_code=ws_code,
                raw_close_reason=ws_reason,
                provider=self._provider.provider_name,
            )

        try:
            await self._on_event_for_frontend(err_obj.to_event())
        except Exception:  # noqa: BLE001
            logger.exception(
                "voice_relay failed to emit voice_error for %s",
                self._provider.provider_name,
            )

    async def _try_reconnect(
        self,
        err: BaseException | None,
        *,
        reason: ReconnectReason,
    ) -> bool:
        """Attempt to transparently reopen the upstream WS.

        Three reconnect reasons (see :class:`ReconnectReason`):

        * ``PROVIDER_GOAWAY``: Gemini Live's ~10-min session limit
          warning. Uncapped — protocol-driven, not an error.
        * ``RECOVERABLE_ERROR``: transient transport close that
          ``is_recoverable_error`` flagged. Capped by ``max_reconnects``.
        * ``STALE_HANDLE``: 1008 "session expired" with a poisoned
          handle. One-shot; the provider's own
          ``_stale_handle_recovery_used`` flag guards the inner loop.

        Concurrent callers coalesce on ``_reconnect_lock``: the first
        wins the lock, the rest wait and observe the outcome via
        ``_handshake_complete``. Closes the 2026-06-04 01:11 duplicate-
        setup race (plan Bug 5).

        Returns True iff the new upstream WS is up and the handshake
        gate is open. On False the caller surfaces the error to the
        frontend and tears down.
        """
        if self._rebuild_session_update is None:
            return False

        # Snapshot the completed-attempts counter BEFORE queuing. If the
        # counter advances between this entry and our lock acquisition,
        # a prior caller already attempted a reconnect — we coalesce
        # (their outcome is our outcome). Closes the 2026-06-04 01:11
        # duplicate-setup race plus the more general "N concurrent
        # callers per close event" stress case.
        attempts_seen = self._reconnect_attempts_completed

        # Coalesce-fast-path: another reconnect for this relay is in
        # flight. Wait for it to release the lock, then report its
        # outcome.
        if self._reconnect_lock.locked():
            self._slog(
                f"reconnect coalesced: another attempt in flight (reason={reason.value})"
            )
            async with self._reconnect_lock:
                return self._handshake_complete.is_set()

        async with self._reconnect_lock:
            # Re-check after acquiring: if the completed-attempts
            # counter advanced while we were queued, a prior caller
            # already attempted. Coalesce to their outcome instead of
            # re-running.
            if self._reconnect_attempts_completed > attempts_seen:
                self._slog(
                    f"reconnect coalesced post-acquire: counter advanced "
                    f"({attempts_seen} -> {self._reconnect_attempts_completed}, "
                    f"reason={reason.value})"
                )
                return self._handshake_complete.is_set()
            self._in_reconnect = True
            try:
                return await self._do_reconnect_locked(err, reason)
            finally:
                self._in_reconnect = False
                # Bump AFTER the attempt completes (success OR fail) so
                # concurrent callers see "an attempt has happened" and
                # don't pile on. Done inside the lock so post-acquire
                # re-check is correct.
                self._reconnect_attempts_completed += 1
                # Flush AFTER the handshake (inside the lock) so a
                # second reconnect can't race the flush. ``_flush_held_outbound``
                # is a no-op when the handshake gate stayed closed
                # (failed reconnect).
                await self._flush_held_outbound()

    async def _do_reconnect_locked(
        self,
        err: BaseException | None,
        reason: ReconnectReason,
    ) -> bool:
        """The actual reconnect body, executed under ``_reconnect_lock``.

        Split out for readability — :meth:`_try_reconnect` handles
        coalescing + lock ownership, this method does the work.
        """
        policy = policy_for(reason)

        # Admission check: cap unless the policy says uncapped (0).
        # The cap VALUE is the instance's ``_max_reconnects`` (user-
        # configurable) — the policy only governs WHETHER to cap. This
        # preserves the constructor-level override that existing tests
        # rely on (``max_reconnects=1`` etc.).
        # Callers are responsible for the provider's
        # ``is_recoverable_error`` check before invoking this method
        # (the gate may mutate provider state on Gemini's stale-handle
        # path, so it must run exactly once per close).
        if policy.max_attempts > 0:
            cap = (
                policy.max_attempts
                if reason is ReconnectReason.STALE_HANDLE
                else self._max_reconnects
            )
            if self._reconnect_count >= cap:
                self._slog(
                    f"reconnect skipped: hit cap={cap} (reason={reason.value})"
                )
                return False

        # Slog + counter bookkeeping per reason.
        if reason is ReconnectReason.PROVIDER_GOAWAY:
            self._goaway_reconnect_count += 1
            self._slog(
                f"reconnect attempt #{self._goaway_reconnect_count} (goAway, uncapped)"
            )
            logger.info(
                "voice_relay reconnect (goAway #%d) session_id=%s",
                self._goaway_reconnect_count, self._session_id,
            )
        else:
            self._reconnect_count += 1
            slog_cap = (
                policy.max_attempts
                if reason is ReconnectReason.STALE_HANDLE
                else self._max_reconnects
            )
            self._slog(
                f"reconnect attempt #{self._reconnect_count}/{slog_cap} "
                f"(reason={reason.value})"
            )
            logger.warning(
                "voice_relay reconnect attempt %d/%d session_id=%s reason=%s after: %s",
                self._reconnect_count, slog_cap,
                self._session_id, reason.value, err,
            )

        # Drop the provider's resumption handle if the policy says so
        # (STALE_HANDLE only). The provider's ``is_recoverable_error``
        # already does this for the legacy gate; doing it explicitly
        # here keeps the policy authoritative for non-legacy callers.
        if policy.reset_handle and hasattr(self._provider, "_resumption_handle"):
            self._provider._resumption_handle = None
            self._slog(f"reconnect: dropped resumption handle (reason={reason.value})")

        # Tear down the old WS. We do NOT cancel keepalive — it shares
        # the relay state and will resume on the new ``self._ws``.
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None

        # Relay-internal state: clear the deferred-event queue (those
        # were tied to the old upstream context). Provider-internal
        # gating state resets itself naturally as the new session
        # delivers fresh events through :meth:`on_inbound_event`.
        self._deferred_events.clear()
        # Re-close the handshake gate so the new upstream gets a fresh
        # setup → setupComplete round trip before audio/events resume.
        self._handshake_complete.clear()
        # Tell the frontend we're cutting over right now (unless the
        # policy says this is a silent recovery — STALE_HANDLE).
        if policy.surface_to_user:
            try:
                await self._on_event_for_frontend({
                    "type": "voice_status",
                    "status": "reconnecting",
                })
            except Exception:  # noqa: BLE001
                pass

        try:
            session_config = await self._rebuild_session_update()
        except Exception as e:  # noqa: BLE001
            self._slog(f"reconnect failed: rebuild_session_update raised: {e}")
            logger.exception("voice_relay rebuild_session_update failed")
            return False

        # Bound the reconnect handshake so a stuck upstream connect
        # (observed 2026-06-04: ``websockets.connect`` hung for 40s+
        # on goAway #3 of a long session, never resolved) doesn't
        # leave the relay silently jammed.
        reconnect_timeout_s = self._voice_timeouts.reconnect_handshake_s
        try:
            await asyncio.wait_for(
                self._open_and_handshake(session_config),
                timeout=reconnect_timeout_s,
            )
        except asyncio.TimeoutError:
            self._slog(
                f"reconnect failed: open_and_handshake timed out after "
                f"{reconnect_timeout_s}s"
            )
            logger.warning(
                "voice_relay reconnect timed out session_id=%s after %ds",
                self._session_id, reconnect_timeout_s,
            )
            return False
        except Exception as e:  # noqa: BLE001
            self._slog(f"reconnect failed: open_and_handshake raised: {e}")
            logger.warning(
                "voice_relay reconnect open_and_handshake failed session_id=%s: %s",
                self._session_id, e,
            )
            return False

        self._slog(f"reconnect #{self._reconnect_count} succeeded (reason={reason.value})")
        logger.info(
            "voice_relay reconnect #%d succeeded session_id=%s reason=%s",
            self._reconnect_count, self._session_id, reason.value,
        )

        # Reset local VAD state per policy. The reconnect path drops
        # mic chunks while the upstream WS is down, and Silero's
        # recurrent state doesn't gracefully recover from a multi-
        # second discontinuity — the probability output stays flat for
        # the rest of the session and speech_started never fires again.
        if policy.reset_vad_state:
            if self._manual_vad is not None:
                self._manual_vad.reset()
                self._slog("manual_vad state reset after reconnect")
            # Also clear the safety-commit watchdog timestamp. Otherwise
            # it carries an N-seconds-ago timestamp from the pre-
            # reconnect session and the post-reconnect path computes
            # "speech ran 79.1s" and fires an immediate safety commit
            # on the fresh upstream.
            if self._manual_vad_speech_started_at is not None:
                self._manual_vad_speech_started_at = None
                self._slog("manual_vad safety-commit watchdog reset after reconnect")

        return True

    async def _flush_held_outbound(self) -> None:
        """Replay queued outbound frames on the new upstream.

        Called from :meth:`_try_reconnect` AFTER the handshake completes
        (inside the lock so a second reconnect can't race the flush).
        Skips frames whose ``should_close_after_event`` would fire on
        the new upstream — those referred to the dying connection's
        lifecycle and would re-trigger a goAway loop.
        """
        if not self._held_outbound:
            return
        # If the new handshake didn't complete (failed reconnect), drop
        # the queue — there's nothing to flush to and the relay is
        # about to surface the error.
        if not self._handshake_complete.is_set() or self._ws is None:
            count = len(self._held_outbound)
            self._held_outbound.clear()
            self._slog(f"held_outbound dropped {count} frames (handshake never completed)")
            return
        frames = list(self._held_outbound)
        self._held_outbound.clear()
        flushed = 0
        skipped = 0
        for frame in frames:
            if self._provider.should_close_after_event(frame):
                skipped += 1
                self._slog(
                    f"held_outbound skip type={frame.get('type')} "
                    "(would close new upstream)"
                )
                continue
            try:
                self._record_sent(frame)
                async with self._send_lock:
                    await self._ws.send(json.dumps(frame))
                self._last_send_at = time.monotonic()
                flushed += 1
            except Exception as e:  # noqa: BLE001
                self._slog(f"held_outbound flush failed: type={frame.get('type')} err={e}")
                # Stop flushing — upstream is sick again.
                break
        self._slog(
            f"held_outbound flushed={flushed} skipped={skipped} "
            f"total={len(frames)}"
        )


def b64_to_pcm(b64: str) -> bytes:
    """Decode a base64 PCM chunk for diagnostics."""
    return base64.b64decode(b64)


def _redact_for_log(event: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with audio + huge string fields trimmed for logging."""
    def _clip(v: Any) -> Any:
        if isinstance(v, str) and len(v) > 200:
            return v[:200] + f"...[+{len(v) - 200}]"
        if isinstance(v, dict):
            return {k: _clip(x) for k, x in v.items()}
        if isinstance(v, list):
            return [_clip(x) for x in v[:10]]
        return v
    return _clip(event)


def _top_keys(event: Any) -> list[str]:
    """Top-level keys of a frame; recurses one level into serverContent."""
    if not isinstance(event, dict):
        return []
    keys: list[str] = []
    for k, v in event.items():
        keys.append(k)
        if k == "serverContent" and isinstance(v, dict):
            for sk in v.keys():
                keys.append(f"serverContent.{sk}")
    return keys


def _strip_audio_parts(event: dict[str, Any]) -> dict[str, Any] | None:
    """Return a copy with audio-only ``modelTurn.parts`` filtered out.

    Used by the Gemini Live drain: the provider bundles
    ``outputTranscription`` (and sometimes ``turnComplete``/``interrupted``)
    into the same frame as the inline audio chunk under
    ``serverContent``. After we've shipped the audio to the frontend
    we still want to surface the rest of the event for transcription
    rendering — but without re-shipping the audio bytes. Strips
    ``inlineData`` parts (and any other ``audio/*`` mime types) from
    ``serverContent.modelTurn.parts``; drops an empty ``modelTurn`` if
    no non-audio parts remain. Returns ``None`` if the resulting
    ``serverContent`` would be empty (nothing left to surface).
    """
    if not isinstance(event, dict):
        return None
    out = dict(event)
    sc = out.get("serverContent")
    if not isinstance(sc, dict):
        return out  # No serverContent — pass through unchanged.
    sc = dict(sc)
    mt = sc.get("modelTurn")
    if isinstance(mt, dict):
        parts = mt.get("parts") or []
        kept = []
        for p in parts:
            if not isinstance(p, dict):
                kept.append(p)
                continue
            inline = p.get("inlineData")
            if isinstance(inline, dict):
                mime = inline.get("mimeType", "") or ""
                if mime.startswith("audio/"):
                    continue  # drop audio inline data
            kept.append(p)
        if kept:
            mt = dict(mt)
            mt["parts"] = kept
            sc["modelTurn"] = mt
        else:
            sc.pop("modelTurn", None)
    out["serverContent"] = sc
    # If serverContent is now empty AND the event has no other useful
    # top-level keys, suppress the forward.
    if not sc and not any(k for k in out if k != "serverContent"):
        return None
    return out


def _redact_inline_audio(event: Any) -> Any:
    """Strip ``inlineData.data`` (base64 PCM) so frames stay log-sized.

    Replaces the data field with ``<audio:NB>`` where N is the byte
    length, leaving every other field intact. Recurses into dicts and
    lists.
    """
    if isinstance(event, dict):
        out: dict[str, Any] = {}
        for k, v in event.items():
            if k == "inlineData" and isinstance(v, dict):
                inner: dict[str, Any] = {}
                for ik, iv in v.items():
                    if ik == "data" and isinstance(iv, str):
                        inner[ik] = f"<audio:{len(iv)}B>"
                    else:
                        inner[ik] = iv
                out[k] = inner
            else:
                out[k] = _redact_inline_audio(v)
        return out
    if isinstance(event, list):
        return [_redact_inline_audio(x) for x in event]
    return event
