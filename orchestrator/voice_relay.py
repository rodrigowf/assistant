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
from utils.paths import PROJECT_ROOT

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

# How often to send a tiny silent keepalive PCM chunk upstream when the
# user is silent.  Qwen-Omni's transcription pipeline times out after a
# few minutes of audio silence and crashes on the next real input with
# a misleading "InvalidParameter" 400.  30s comfortably stays under
# that threshold while keeping bandwidth negligible (~960B/30s).
_KEEPALIVE_INTERVAL_S = 30.0


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
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:  # noqa: BLE001
                pass
            self._log_file = None

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

        await self._open_and_handshake(session_config)
        self._drain_task = asyncio.create_task(self._drain(), name=f"voice-relay-{self._provider.provider_name}")

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
        if self._ws is None or self._closed.is_set():
            return  # Drop silently — caller already saw an error event.

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

    async def send_audio(self, pcm_b64: str) -> None:
        """Forward a frontend mic chunk upstream as a provider-specific append.

        The provider's :meth:`BaseVoiceProvider.format_audio_in` decides
        the on-the-wire shape (Qwen: ``input_audio_buffer.append``;
        Gemini Live: ``realtimeInput.audio``).
        """
        if self._ws is None or self._closed.is_set():
            return  # Drop silently after upstream close — frontend already notified.
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
                await asyncio.sleep(_KEEPALIVE_INTERVAL_S / 2)
                if self._closed.is_set() or self._ws is None:
                    break
                # Skip if a real audio chunk went out within the interval.
                idle = time.monotonic() - (self._last_audio_in_at or self._started_at)
                if idle < _KEEPALIVE_INTERVAL_S:
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

            # Try to recover transparently before giving up — DashScope's
            # "InvalidParameter" 400 mid-session is almost always salvageable
            # by reopening with a fresh session.update.
            reconnected = await self._try_reconnect(e)
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

            await self._on_event_for_frontend({
                "type": "error",
                "error": {
                    "code": "voice_relay_failed",
                    "message": f"Upstream {self._provider.provider_name} WS closed: {e}",
                },
            })

    async def _try_reconnect(self, err: BaseException) -> bool:
        """Attempt to transparently reopen the upstream WS.

        Only fires for errors the provider classifies as recoverable
        (via :meth:`BaseVoiceProvider.is_recoverable_error`) AND when
        the relay was constructed with a ``rebuild_session_update``
        callback AND we haven't exhausted :attr:`_max_reconnects`.  On
        success the new WS is wired up and ``_ws`` points at it; the
        caller restarts the drain task.

        We deliberately do NOT mirror the upstream error frame to the
        frontend on a successful reconnect — the user shouldn't see a
        red banner that immediately heals.  If reconnect fails, the
        normal error path resumes.
        """
        if self._rebuild_session_update is None:
            return False
        if self._reconnect_count >= self._max_reconnects:
            self._slog(
                f"reconnect skipped: hit max_reconnects={self._max_reconnects}"
            )
            return False
        if not self._provider.is_recoverable_error(err):
            self._slog(f"reconnect skipped: provider classifies error as fatal")
            return False

        self._reconnect_count += 1
        self._slog(
            f"reconnect attempt #{self._reconnect_count}/{self._max_reconnects}"
        )
        logger.warning(
            "voice_relay reconnect attempt %d/%d session_id=%s after: %s",
            self._reconnect_count, self._max_reconnects, self._session_id, err,
        )

        # Tear down the old WS.  We do NOT cancel keepalive — it shares
        # the relay state and will resume on the new ``self._ws``.
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None

        # Relay-internal state: clear the deferred-event queue (those
        # were tied to the old upstream context).  Provider-internal
        # gating state resets itself naturally as the new session
        # delivers fresh events through :meth:`on_inbound_event`.
        self._deferred_events.clear()

        try:
            session_config = await self._rebuild_session_update()
        except Exception as e:  # noqa: BLE001
            self._slog(f"reconnect failed: rebuild_session_update raised: {e}")
            logger.exception("voice_relay rebuild_session_update failed")
            return False

        try:
            await self._open_and_handshake(session_config)
        except Exception as e:  # noqa: BLE001
            self._slog(f"reconnect failed: open_and_handshake raised: {e}")
            logger.warning(
                "voice_relay reconnect open_and_handshake failed session_id=%s: %s",
                self._session_id, e,
            )
            return False

        self._slog(f"reconnect #{self._reconnect_count} succeeded")
        logger.info(
            "voice_relay reconnect #%d succeeded session_id=%s",
            self._reconnect_count, self._session_id,
        )
        return True


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
