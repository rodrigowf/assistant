"""WebSocket orchestrator endpoint — streams orchestrator agent events.

Supports text, audio, and voice modes with runtime model switching.

Message types (client → server):
- start: Initialize text mode
- voice_start: Initialize voice mode (WebRTC)
- send: Send text message
- send_audio: Send audio message (base64 encoded)
- set_model: Switch model mid-conversation
- voice_event: Mirrored OpenAI Realtime event (voice mode)
- interrupt: Stop current response
- stop: Close session
- get_model: Get current model info
- get_models: List available models
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from pathlib import Path

import orjson
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from api.pool import SessionPool
from api.serializers import serialize_orchestrator_event
from orchestrator import summary_cache
from orchestrator.config import OrchestratorConfig, get_available_models
from orchestrator.providers.discovery import list_orchestrator_models
from orchestrator.session import OrchestratorSession

logger = logging.getLogger(__name__)
router = APIRouter(tags=["orchestrator"])

# Per-session locks serializing concurrent ``voice_start`` handlers for the
# same ``local_id``.  Android (and other clients with reconnect-on-blip
# behaviour) can fire ``voice_start`` multiple times within ~1s when the
# socket flutters; without this guard each call independently raced
# through ``_handle_start`` → ``_attach_voice_payload`` → relay rebuild,
# opening multiple Google Live WS handshakes against the same stale
# ``sessionResumption`` handle. Google accepted the duplicate handshakes
# and then 1008'd them ~150s later ("operation aborted"), surfacing as a
# spike of 400 BadRequest in AI Studio's dashboard (observed
# 2026-06-04: three back-to-back opens, all reusing the same handle).
#
# Keyed on ``local_id``. The dict is intentionally never pruned — locks
# are tiny (~100 bytes), local_ids are UUIDs, and a long-lived backend
# accumulates O(distinct sessions/day) ≈ a handful, not enough to leak.
_VOICE_START_LOCKS: dict[str, asyncio.Lock] = {}


def _voice_start_lock_for(local_id: str) -> asyncio.Lock:
    lock = _VOICE_START_LOCKS.get(local_id)
    if lock is None:
        lock = asyncio.Lock()
        _VOICE_START_LOCKS[local_id] = lock
    return lock


async def _safe_send_bytes(ws: WebSocket, payload: bytes) -> bool:
    """Send to a client WS, swallowing the post-close race.

    Starlette's ``send`` raises ``RuntimeError: Cannot call "send" once a
    close message has been sent.`` when we try to write to a WS that
    closed mid-handler — for us that's the orchestrator-WS handler still
    in flight when okhttp closes the socket on the other end. The crash
    bubbles up through the handler, aborts whatever response we were
    building, and the route shows ``Orchestrator WS error`` in the log.

    Returns True if the bytes were sent, False if the WS was already
    closed (caller can stop trying).
    """
    if ws.client_state != WebSocketState.CONNECTED:
        return False
    try:
        await ws.send_bytes(payload)
        return True
    except RuntimeError as e:
        # The specific message Starlette raises when the WS is already
        # closing/closed. Treat as a clean "client gone" — the disconnect
        # branch will tear down voice via end_voice as usual.
        if "close message has been sent" in str(e) or "after sending close" in str(e):
            logger.debug("safe_send: WS already closed, dropping payload")
            return False
        raise
    except Exception:
        # Unknown error — log and drop. Don't crash the handler.
        logger.warning("safe_send: unexpected error, dropping payload", exc_info=True)
        return False


@router.websocket("/api/orchestrator/chat")
async def orchestrator_ws(ws: WebSocket):
    await ws.accept()

    pool: SessionPool = ws.app.state.pool
    session: OrchestratorSession | None = None
    subscribed = False  # True once this ws is registered in pool._orchestrator_subs
    # Defensive init — the finally block reads ``was_voice``
    # unconditionally, and not every loop-exit path runs through one
    # of the except handlers where ``was_voice`` is otherwise assigned.
    # Without this default we'd hit UnboundLocalError on any clean exit.
    was_voice = False
    # True only if THIS WebSocket initiated the voice session (sent
    # ``voice_start``). Passive subscribers (text-mode WebSockets that
    # joined via ``start`` while voice was already active) must NOT tear
    # down voice on disconnect — only the voice owner should.
    voice_owner = False

    # Register as a watcher so this ws receives agent_session_opened/closed events
    pool.watch(ws)

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = orjson.loads(raw)
            except (orjson.JSONDecodeError, ValueError):
                await _safe_send_bytes(ws, orjson.dumps({
                    "type": "error", "error": "invalid_json",
                }))
                continue

            msg_type = msg.get("type", "")

            # Client-initiated heartbeat / heartbeat-ack — neither side
            # needs to do anything but consume them silently. Allows the
            # client to optionally send its own pings (or echo ours back
            # as a "pong"); we just ignore both.
            if msg_type in ("ping", "pong"):
                continue

            if msg_type == "start":
                session, subscribed = await _handle_start(ws, pool, msg, voice=False)

            elif msg_type == "voice_start":
                # Serialize concurrent voice_start for the same local_id
                # so a burst of duplicate triggers from a reconnecting
                # client can't open multiple upstream WS handshakes. See
                # ``_VOICE_START_LOCKS`` docstring for the failure mode
                # this guards against. If no local_id was supplied,
                # ``_handle_start`` generates one — we accept the race
                # for that path (no clear key to lock on, and a missing
                # local_id is already a one-off "fresh tab" case where
                # duplicates are extremely unlikely).
                _vs_local_id = msg.get("local_id")
                if _vs_local_id:
                    _vs_lock = _voice_start_lock_for(_vs_local_id)
                    if _vs_lock.locked():
                        logger.info(
                            "voice_start coalesce: local_id=%s already in flight; "
                            "waiting for the in-progress handler",
                            _vs_local_id,
                        )
                    async with _vs_lock:
                        session, subscribed = await _handle_start(
                            ws, pool, msg, voice=True,
                        )
                else:
                    session, subscribed = await _handle_start(
                        ws, pool, msg, voice=True,
                    )
                if session is not None:
                    voice_owner = True

            elif msg_type == "send":
                if session is None:
                    await _safe_send_bytes(ws, orjson.dumps({
                        "type": "error", "error": "not_started",
                        "detail": "Send a 'start' message first",
                    }))
                    continue
                await _handle_send(pool, session, msg.get("text", ""))

            elif msg_type == "send_audio":
                if session is None:
                    await _safe_send_bytes(ws, orjson.dumps({
                        "type": "error", "error": "not_started",
                        "detail": "Send a 'start' message first",
                    }))
                    continue
                await _handle_send_audio(
                    pool,
                    session,
                    msg.get("audio", ""),
                    msg.get("format", "webm"),
                    msg.get("text"),
                )

            elif msg_type == "set_model":
                if session is None:
                    await _safe_send_bytes(ws, orjson.dumps({
                        "type": "error", "error": "not_started",
                        "detail": "Send a 'start' message first",
                    }))
                    continue
                await _handle_set_model(pool, session, msg.get("model", ""))

            elif msg_type == "get_model":
                if session is None:
                    await _safe_send_bytes(ws, orjson.dumps({
                        "type": "error", "error": "not_started",
                        "detail": "Send a 'start' message first",
                    }))
                    continue
                await _handle_get_model(ws, session)

            elif msg_type == "get_models":
                # List available models (doesn't require session)
                await _handle_get_models(ws)

            elif msg_type == "voice_event":
                if session is None or not session.is_voice:
                    await _safe_send_bytes(ws, orjson.dumps({
                        "type": "error", "error": "not_voice_session",
                        "detail": "No active voice session",
                    }))
                    continue
                await _handle_voice_event(pool, session, msg.get("event", {}))

            elif msg_type == "voice_audio_in":
                # WS-provider mic chunk → forward upstream.
                # Drop silently if no voice session is set up on this WS.
                # Why: when Android's WS reconnects mid-call, the local
                # mic-capture loop keeps running and starts pushing chunks
                # on the new WS before our voice_start has been processed.
                # The previous "error: not_voice_session" reply per chunk
                # caused an error storm in both directions (Android logged:
                # 28x error events before the start reply landed on a
                # single reconnect). Mic frames are inherently
                # disposable — silently dropping them while we're not
                # voice-ready is the right behaviour.
                if session is None or not session.is_voice:
                    continue
                audio_b64 = msg.get("audio", "")
                if audio_b64:
                    try:
                        await session.send_voice_audio_in(audio_b64)
                    except Exception as e:
                        logger.exception("Voice audio relay failed")
                        await pool.broadcast_orchestrator({
                            "type": "error", "error": "voice_audio_failed", "detail": str(e),
                        })

            elif msg_type == "voice_recording_chunk":
                # WebRTC recording chunk from browser — write to recorder.
                if session is None or not session.is_voice:
                    continue  # Silently drop if no session
                recorder = session.audio_recorder
                if recorder is not None and recorder.is_recording:
                    channel = msg.get("channel", "user")
                    audio_b64 = msg.get("audio", "")
                    if audio_b64:
                        if channel == "user":
                            recorder.write_user_audio(audio_b64)
                        elif channel == "assistant":
                            recorder.write_assistant_audio(audio_b64)

            elif msg_type == "voice_recording_end":
                # WebRTC recording ended — handled by session.stop()
                pass  # No action needed; the session stop handles cleanup

            elif msg_type == "compact":
                if session is None:
                    await _safe_send_bytes(ws, orjson.dumps({
                        "type": "error", "error": "not_started",
                        "detail": "Send a 'start' message first",
                    }))
                    continue
                await _handle_compact(pool, session)

            elif msg_type == "interrupt":
                if session is not None:
                    await session.interrupt()
                    await pool.broadcast_orchestrator({"type": "status", "status": "interrupted"})

            elif msg_type == "voice_stop":
                # End ONLY the voice connection — keep the orchestrator
                # session alive in the pool so the user can re-arm voice
                # later with the wake word (the tab survives). The
                # session.stop() / end_voice path closes the upstream
                # WS and releases the audio recorder; nothing leaks.
                if session is not None and session.is_voice:
                    try:
                        await session.end_voice("user_stop")
                    except Exception:  # noqa: BLE001
                        logger.exception("end_voice failed during voice_stop")
                voice_owner = False
                # No session_stopped ack here — the voice_ended broadcast
                # that end_voice fires is the ack the frontend waits for.

            elif msg_type == "stop":
                # Stop the orchestrator session entirely (close the tab).
                # For voice sessions, also tear down the voice connection
                # on the way out so the graceful shutdown frames fire
                # and tokens stop accruing immediately. The follow-up
                # pool.stop_orchestrator drops the session from the pool.
                if session is not None and session.is_voice:
                    try:
                        await session.end_voice("user_stop")
                    except Exception:  # noqa: BLE001
                        logger.exception("end_voice failed during stop")
                await pool.stop_orchestrator()
                session = None
                subscribed = False
                await _safe_send_bytes(ws, orjson.dumps({"type": "session_stopped"}))

            else:
                await _safe_send_bytes(ws, orjson.dumps({
                    "type": "error", "error": "unknown_type",
                    "detail": f"Unknown message type: {msg_type!r}",
                }))

    except WebSocketDisconnect as e:
        # Note what state we were in — a voice-mode disconnect with no
        # `stop` command tends to indicate a frontend reconnect, which
        # has consequences (the session.update fires fresh next time).
        was_voice = bool(session is not None and session.is_voice)
        logger.info(
            "Orchestrator WS disconnected (client closed) code=%s reason=%r voice_active=%s",
            getattr(e, "code", None),
            getattr(e, "reason", None),
            was_voice,
        )
    except Exception:
        was_voice = bool(session is not None and getattr(session, "is_voice", False))
        logger.exception("Orchestrator WS error voice_active=%s", was_voice)
    finally:
        pool.unwatch(ws)
        pool.unsubscribe_orchestrator(ws)
        # On client WS disconnect: tear down the voice connection ONLY
        # if this WebSocket is the voice owner (the one that sent
        # ``voice_start``). Passive subscribers (text-mode WebSockets
        # that joined via ``start`` while voice was active) must not
        # kill the voice session when they disconnect — otherwise
        # refreshing the iPad kills the Android's active voice call.
        #
        # ``end_voice`` is idempotent and handles non-voice sessions as
        # a fast no-op, so calling it unconditionally here is safe.
        if was_voice and session is not None and voice_owner:
            try:
                await session.end_voice("client_disconnect")
                logger.info(
                    "Voice connection ended on client disconnect session=%s "
                    "(orchestrator session kept in pool for re-arm)",
                    getattr(session, "local_id", "?"),
                )
            except Exception:  # noqa: BLE001
                logger.exception("end_voice on disconnect failed")


async def _handle_start(
    ws: WebSocket,
    pool: SessionPool,
    msg: dict,
    voice: bool = False,
) -> tuple[OrchestratorSession | None, bool]:
    """Start, resume, or reconnect to the orchestrator session.

    The frontend sends ``local_id`` (stable tab UUID) and optionally
    ``resume_sdk_id`` (the original session_id for the JSONL file when
    resuming from history).

    The pool is keyed by ``local_id``. The JSONL file is keyed by
    ``resume_sdk_id`` (or ``local_id`` for new sessions). This decoupling
    allows mode transitions (text↔voice) and reconnections to work correctly
    while preserving conversation history across sessions.

    Returns (session, subscribed). subscribed=True when this ws was registered.
    """
    local_id: str | None = msg.get("local_id")
    resume_id: str | None = msg.get("resume_sdk_id") or msg.get("session_id")
    voice_provider_req: str | None = msg.get("voice_provider") if voice else None
    voice_model_req: str | None = msg.get("voice_model") if voice else None
    voice_name_req: str | None = msg.get("voice_name") if voice else None
    voice_lang_req: str | None = (
        msg.get("voice_transcription_language") if voice else None
    )
    voice_endpoint_req: str | None = msg.get("voice_endpoint") if voice else None

    if voice:
        logger.info(
            "voice_session start_requested local_id=%s resume_id=%s provider=%s model=%s voice=%s lang=%s endpoint=%s",
            local_id, resume_id, voice_provider_req, voice_model_req, voice_name_req, voice_lang_req, voice_endpoint_req,
        )

    # If a prior session with this local_id is in the middle of being
    # torn down (state == ENDING), wait for it to finish before deciding
    # reconnect vs new. Skipping this is what caused the "frozen on
    # restart" bug — a fresh voice_start would hit the reconnect branch
    # and reattach to a session whose relay was already dying.
    if local_id:
        ok = await pool.await_orchestrator_stop(local_id, timeout=5.0)
        if not ok:
            logger.warning(
                "Timed out waiting for prior orchestrator %s to finish stopping",
                local_id,
            )
            await _safe_send_bytes(ws, orjson.dumps({
                "type": "error", "error": "orchestrator_stopping",
                "detail": (
                    "Previous voice session is still ending. Please retry."
                ),
            }))
            return None, False

    # --- Reconnect: an orchestrator with this local_id is already running ---
    if pool.has_orchestrator() and local_id and pool.orchestrator_id == local_id:
        session = pool.get_orchestrator()
        current_voice = getattr(session, "is_voice", False)
        skip_reconnect = False

        # Belt-and-braces: if the session's voice lifecycle has already
        # entered ENDING (the await_orchestrator_stop above would
        # normally catch this, but state can advance between the check
        # and here on a fast teardown), drop it and fall through to
        # create a fresh session.
        if voice and current_voice and getattr(session, "voice_is_ending", False):
            logger.info(
                "Voice session %s reached ENDING during start; dropping for fresh start",
                local_id,
            )
            try:
                await pool.stop_orchestrator()
            except Exception:  # noqa: BLE001
                logger.exception("stop_orchestrator during ENDING-drop failed")
            await pool.await_orchestrator_stop(local_id, timeout=5.0)
            skip_reconnect = True

        if skip_reconnect:
            pass  # fall through to "Start a new session" below
        elif voice and not current_voice:
            # Text-mode session + voice_start = re-arm voice on the SAME
            # OrchestratorSession instead of dropping and recreating it.
            # This is the canonical "wake word after end_voice_session"
            # path: the tab is the same conversation (same JSONL, same
            # agent state, same background work); only the voice
            # connection is being reconstructed.
            #
            # Equivalent to a fresh voice_start in every observable way
            # (session_started payload, voice_session_update, relay
            # boot) except the JSONL is the existing one.
            try:
                await session.restart_voice(
                    voice_provider=voice_provider_req,
                    voice_model=voice_model_req,
                    voice_name=voice_name_req,
                    voice_transcription_language=voice_lang_req,
                    voice_endpoint=voice_endpoint_req,
                )
            except Exception as e:  # noqa: BLE001
                logger.exception(
                    "restart_voice failed for session %s; falling back to fresh session",
                    local_id,
                )
                await _safe_send_bytes(ws, orjson.dumps({
                    "type": "error", "error": "voice_restart_failed",
                    "detail": str(e),
                }))
                return None, False

            pool.subscribe_orchestrator(local_id, ws)
            reconnect_payload: dict = {
                "type": "session_started",
                "session_id": local_id,
                "voice": True,
                "model_info": session.get_model_info(),
            }
            await _attach_voice_payload(
                reconnect_payload, session,
                initiator=True, initiator_ws=ws,
            )
            await _safe_send_bytes(ws, orjson.dumps(reconnect_payload))
            logger.info(
                "voice rearm complete session=%s — same orchestrator, fresh voice connection",
                local_id,
            )
            return session, True
        elif not voice and current_voice:
            # Text WS reconnecting while voice is active — subscribe without
            # disrupting the voice session (the text WS auto-connects on mount).
            # Not the initiator: another client owns the live voice
            # connection. Carry voice metadata so this client's UI can
            # reflect that voice is active, but don't let it try to open
            # its own provider transport.
            pool.subscribe_orchestrator(local_id, ws)
            reconnect_payload: dict = {
                "type": "session_started",
                "session_id": local_id,
                "voice": current_voice,
                "model_info": session.get_model_info(),
            }
            await _attach_voice_payload(
                reconnect_payload, session,
                initiator=False, initiator_ws=None,
            )
            await _safe_send_bytes(ws, orjson.dumps(reconnect_payload))
            return session, True
        else:
            # Same mode — check if the client is asking for a different
            # voice provider/model/voice/endpoint than the live session.
            # If so, we need to rebuild the relay; the existing relay's
            # provider object is bound to the original config and won't
            # honour mid-session changes. Block the swap while a turn is
            # in flight so we don't tear down upstream audio mid-reply.
            if voice and current_voice:
                drift = _voice_config_drift(
                    session,
                    voice_provider_req,
                    voice_model_req,
                    voice_name_req,
                    voice_lang_req,
                    voice_endpoint_req,
                )
                if drift:
                    if session.is_busy:
                        await _safe_send_bytes(ws, orjson.dumps({
                            "type": "error",
                            "error": "voice_config_busy",
                            "detail": (
                                "Cannot switch voice provider/model mid-turn"
                                f" ({drift}). Wait for the current response"
                                " to finish, then try again."
                            ),
                        }))
                        return None, False
                    logger.info(
                        "voice config drift on reconnect (%s) — tearing down to rebuild",
                        drift,
                    )
                    await pool.stop_orchestrator()
                    await pool.await_orchestrator_stop(local_id, timeout=5.0)
                    # Fall through to the new-session creation path below.
                else:
                    pool.subscribe_orchestrator(local_id, ws)
                    reconnect_payload = {
                        "type": "session_started",
                        "session_id": local_id,
                        "voice": current_voice,
                        "model_info": session.get_model_info(),
                    }
                    await _attach_voice_payload(
                        reconnect_payload, session,
                        initiator=True, initiator_ws=ws,
                    )
                    await _safe_send_bytes(ws, orjson.dumps(reconnect_payload))
                    return session, True
            else:
                pool.subscribe_orchestrator(local_id, ws)
                reconnect_payload = {
                    "type": "session_started",
                    "session_id": local_id,
                    "voice": current_voice,
                    "model_info": session.get_model_info(),
                }
                await _safe_send_bytes(ws, orjson.dumps(reconnect_payload))
                return session, True

    # --- A different orchestrator is already active ---
    if pool.has_orchestrator():
        await _safe_send_bytes(ws, orjson.dumps({
            "type": "error", "error": "orchestrator_active",
            "detail": "An orchestrator session is already active. Stop it first.",
        }))
        return None, False

    # --- Start a new (or resumed) orchestrator session ---
    config = OrchestratorConfig.load()
    project_dir = config.project_dir

    # If voice mode and any of provider/model/voice/language/endpoint missing
    # from the start message, fall back to what's saved in
    # assistant_config.json. Endpoint must be included in the gate, otherwise
    # clients that pass provider/model/voice/lang but omit endpoint (e.g. the
    # Android peripheral before the voice_endpoint field was added) get
    # endpoint=None → Vertex default, even when the user has configured AI
    # Studio. That breaks AI-Studio-named ids like
    # ``gemini-2.5-flash-native-audio-latest`` with a Vertex policy error.
    if voice and (
        voice_provider_req is None
        or voice_model_req is None
        or voice_name_req is None
        or voice_lang_req is None
        or voice_endpoint_req is None
    ):
        try:
            from api.routes.config import _load_config as _load_app_config
            app_cfg = _load_app_config()
            voice_provider_req = voice_provider_req or app_cfg.get("default_voice_provider")
            voice_model_req = voice_model_req or app_cfg.get("default_voice_model")
            voice_name_req = voice_name_req or app_cfg.get("default_voice_name")
            if voice_lang_req is None:
                voice_lang_req = app_cfg.get("default_voice_transcription_language")
            if voice_endpoint_req is None:
                voice_endpoint_req = app_cfg.get("default_voice_endpoint")
        except Exception:
            logger.exception("Failed to load voice defaults from assistant_config.json")

    # Note: we deliberately do NOT inject ``ws.app.state.config`` here.
    # That snapshot is built once at app startup and never refreshed, so
    # tools that used it (notably ``open_agent_session`` historically)
    # ignored later edits to ``assistant_config.json`` / ``.manager.json``
    # — the orchestrator would spawn local sessions even after the user
    # pointed the UI at an SSH host.  Tools that need a ``ManagerConfig``
    # now call ``api.session_factory.build_session_config()`` which
    # re-reads the live files on every call.
    context: dict = {
        "store": ws.app.state.store,
        "pool": pool,
        "project_dir": project_dir,
        "index_dir": str(Path(project_dir) / "index" / "chroma"),
    }

    session = OrchestratorSession(
        config=config,
        context=context,
        session_id=resume_id,
        local_id=local_id,
        voice=voice,
        voice_provider=voice_provider_req,
        voice_model=voice_model_req,
        voice_name=voice_name_req,
        voice_transcription_language=voice_lang_req,
        voice_endpoint=voice_endpoint_req,
    )

    await _safe_send_bytes(ws, orjson.dumps({"type": "status", "status": "connecting"}))
    # Timing instrumentation — when a wake-word call hits a cold orchestrator
    # session, the gap between voice_start and session_started can balloon
    # to 20+ seconds. The labelled millisecond breakdown below tells us
    # which step (session.start, voice-payload attach, prompt assembly,
    # tools build, relay open) is eating the time so we can target the
    # right fix instead of guessing. Cheap to leave on; one INFO line per
    # voice-start.
    _t0 = time.monotonic()
    def _ms_since(t: float) -> int:
        return int((time.monotonic() - t) * 1000)
    try:
        session_id = await session.start()
    except Exception as e:
        logger.exception("Orchestrator session start failed")
        await _safe_send_bytes(ws, orjson.dumps({
            "type": "error", "error": "start_failed", "detail": str(e),
        }))
        return None, False
    if voice:
        logger.info(
            "voice_start_timing local_id=%s step=session.start dt_ms=%d",
            local_id, _ms_since(_t0),
        )

    _t_pool = time.monotonic()
    pool.set_orchestrator(session_id, session)
    pool.subscribe_orchestrator(session_id, ws)
    if voice:
        logger.info(
            "voice_start_timing local_id=%s step=pool.register dt_ms=%d",
            local_id, _ms_since(_t_pool),
        )

    # Install the wake callback for background-agent notifications.  When a
    # fire-and-forget agent turn finishes while the orchestrator is idle, the
    # callback fires a synthetic empty-prompt turn so the LLM gets a chance
    # to react asynchronously.  Voice mode is skipped (notifications still
    # queue and drain on the next text/audio turn) — wiring them through the
    # OpenAI Realtime data channel is a future enhancement.
    if not voice:
        def _make_wake(_pool: SessionPool, _session: OrchestratorSession):
            async def _wake() -> None:
                if _session.is_busy:
                    return
                if not _session.notifications.has_pending():
                    return
                # Schedule, don't await — we must not block the runner's
                # _drive task that pushed the notification.  The synthetic
                # turn is a normal _handle_send with an empty prompt; the
                # session.send() body short-circuits if the queue is also
                # empty by the time it acquires the busy lock.
                asyncio.create_task(
                    _handle_send(_pool, _session, ""),
                    name="orchestrator-wake",
                )
            return _wake

        session.notifications.set_wake_callback(_make_wake(pool, session))

    # Background history-summary refresh on session reopen. The chat WS
    # `start` arrives whenever the user opens a session from history (or
    # the Android app reconnects after waking). The summarisation is the
    # critical-path cost for voice_start (see summary_cache), and here
    # we have idle time to pay for it before the user presses the mic.
    # No-op if the cache is already fresh.
    if not voice:
        asyncio.create_task(
            session.refresh_summary_cache_if_stale(),
            name=f"summary-refresh-start-{local_id}",
        )

    _t_payload = time.monotonic()
    started_payload: dict = {
        "type": "session_started",
        "session_id": session_id,
        "voice": voice,
        "model_info": session.get_model_info(),
    }
    if voice:
        await _attach_voice_payload(
            started_payload, session,
            initiator=True, initiator_ws=ws,
        )
        logger.info(
            "voice_start_timing local_id=%s step=attach_voice_payload dt_ms=%d",
            local_id, _ms_since(_t_payload),
        )

    _t_send = time.monotonic()
    await _safe_send_bytes(ws, orjson.dumps(started_payload))
    if voice:
        logger.info(
            "voice_start_timing local_id=%s step=ws.send_started dt_ms=%d payload_bytes=%d TOTAL_ms=%d",
            local_id, _ms_since(_t_send), len(orjson.dumps(started_payload)),
            _ms_since(_t0),
        )
    return session, True


def _voice_config_drift(
    session: OrchestratorSession,
    provider: str | None,
    model: str | None,
    voice_name: str | None,
    language: str | None,
    endpoint: str | None,
) -> str | None:
    """Return a short description of which voice fields the client asked
    to change vs. the live session, or ``None`` if they match.

    Fields the client didn't send (``None``) are skipped — same-mode
    reconnect WS messages frequently omit settings the client doesn't
    care about. Endpoint comparison uses the live provider's classname
    via ``endpoint_id`` for Gemini, falling back to ``_voice_endpoint``.
    """
    changed: list[str] = []
    if provider is not None and provider != session.voice_provider_id:
        changed.append(f"provider {session.voice_provider_id!r}→{provider!r}")
    if model is not None and model != session.voice_model_id:
        changed.append(f"model {session.voice_model_id!r}→{model!r}")
    if voice_name is not None and voice_name != session.voice_name_id:
        changed.append(f"voice {session.voice_name_id!r}→{voice_name!r}")
    if language is not None and language != session.voice_transcription_language:
        changed.append(
            f"language {session.voice_transcription_language!r}→{language!r}"
        )
    if endpoint is not None:
        live_endpoint: str | None = None
        provider_obj = getattr(session, "_voice_provider", None)
        if provider_obj is not None:
            live_endpoint = getattr(provider_obj, "endpoint_id", None)
        if live_endpoint is None:
            live_endpoint = getattr(session, "_voice_endpoint", None)
        if endpoint != live_endpoint:
            changed.append(f"endpoint {live_endpoint!r}→{endpoint!r}")
    return ", ".join(changed) if changed else None


async def _attach_voice_payload(
    payload: dict,
    session: OrchestratorSession,
    *,
    initiator: bool = True,
    initiator_ws: WebSocket | None = None,
) -> None:
    """Mutate ``payload`` to include voice provider metadata and the
    provider-specific session.update + connection info, and start the
    backend relay for WebSocket providers if it isn't already running.

    ``initiator`` flags whether the receiving WS is the client that
    actually requested this voice attach (vs. a text client reconnecting
    to an already-voice session). The payload carries this through as
    ``voice_initiator`` so non-initiator clients can mirror the voice
    UI without trying to spin up their own provider transport — which
    would otherwise show duplicate "preparing" / "connecting" UI on
    every device subscribed to the same orchestrator session.

    ``initiator_ws`` is the WebSocket of the initiator, used to direct
    pre-connect status events like ``voice_status: summarizing`` to
    only the initiator instead of broadcasting them (which used to
    flash the "summarizing" UI on every connected client).

    Errors fetching the ephemeral token or starting the relay are
    swallowed and reported back as ``voice_connection_error`` so the
    frontend can surface them; the session itself stays alive.
    """
    payload["voice_provider"] = session.voice_provider_id
    payload["voice_model"] = session.voice_model_id
    payload["voice_name"] = session.voice_name_id
    payload["voice_transcription_language"] = session.voice_transcription_language
    payload["voice_initiator"] = initiator
    # Tell frontend whether to record audio (relevant for WebRTC where audio bypasses backend)
    payload["voice_recording_enabled"] = session.audio_recorder is not None

    # Sub-step timing inside _attach_voice_payload — when start is slow,
    # the cost is almost always in get_session_update (system prompt +
    # tools build) or get_connection_info (ephemeral token / OAuth).
    _local_id = getattr(session, "local_id", "?")

    # If the history-summary cache is stale, get_session_update is going
    # to make a synchronous LLM call (15-25s on long sessions). Tell the
    # UI so it can show a "summarizing" yellow state instead of the
    # default "preparing" that suggests imminent readiness. Also tells
    # the user something is happening so they don't try repeatedly.
    pool_for_status = session._context.get("pool")
    # ``summarizing`` is a *pre-connect* state — only meaningful when the
    # user is waiting for the relay to come up. On a reconnect to a live
    # relay (the WS dropped briefly, Android re-attached) the relay
    # already has its session.update; summarising again would be wasted
    # work AND would broadcast a stale yellow "Preparing conversation..."
    # to a UI that's mid-call. Skip the broadcast (and the summarisation
    # remains a no-op since the cache write paths are guarded too).
    existing_relay = getattr(session, "_voice_relay", None)
    relay_is_live = existing_relay is not None and getattr(
        existing_relay, "is_running", False,
    )
    will_summarize = (
        not relay_is_live
        and session._jsonl_path is not None
        and session._jsonl_path.is_file()
        and not summary_cache.is_fresh(session._jsonl_path)
    )
    if will_summarize:
        summarizing_msg = orjson.dumps({
            "type": "voice_event",
            "event": {
                "type": "voice_status",
                "status": "summarizing",
            },
        })
        # Directed send to the initiator only — broadcasting this used
        # to flash a "Summarizing..." spinner on every other device
        # subscribed to the same orchestrator session, making it look
        # like both devices were trying to start a conversation. The
        # initiator is the one waiting on the slow LLM round-trip; no
        # other client needs to react. Falls back to broadcast if we
        # don't have an initiator handle (e.g. server-initiated rearm).
        try:
            if initiator_ws is not None:
                await _safe_send_bytes(initiator_ws, summarizing_msg)
            elif pool_for_status is not None:
                await pool_for_status.broadcast_orchestrator(orjson.loads(summarizing_msg))
        except Exception:  # noqa: BLE001
            logger.exception("voice_status:summarizing send failed")

    _t_su = time.monotonic()
    session_update = await session.get_session_update()
    logger.info(
        "voice_start_timing local_id=%s step=get_session_update dt_ms=%d bytes=%d cache_was_stale=%s",
        _local_id, int((time.monotonic() - _t_su) * 1000),
        len(orjson.dumps(session_update)) if session_update else 0,
        will_summarize,
    )
    if session_update:
        payload["voice_session_update"] = session_update

    provider_obj = getattr(session, "_voice_provider", None)
    if provider_obj is not None:
        _t_ci = time.monotonic()
        try:
            payload["voice_connection_info"] = await provider_obj.get_connection_info()
        except Exception as e:
            logger.warning("Voice connection info fetch failed: %s", e)
            payload["voice_connection_error"] = str(e)
        logger.info(
            "voice_start_timing local_id=%s step=get_connection_info dt_ms=%d",
            _local_id, int((time.monotonic() - _t_ci) * 1000),
        )

    # Start the backend relay for WS providers (Qwen / Gemini / locals).
    # Idempotent for healthy relays; rebuilds if the previous drain crashed
    # (relay object lingers but its drain task is done) so a reconnecting
    # client recovers automatically.
    existing_relay = getattr(session, "_voice_relay", None)
    if existing_relay is not None and not getattr(existing_relay, "is_running", False):
        logger.warning(
            "Voice relay for session %s is dead; tearing down and rebuilding",
            getattr(session, "local_id", "?"),
        )
        try:
            await session.stop_voice_relay()
        except Exception:  # noqa: BLE001
            logger.exception("stop_voice_relay during reconnect cleanup failed")
        existing_relay = None

    if session.needs_voice_relay and existing_relay is None:
        pool = session._context.get("pool")
        if pool is not None:
            async def _on_audio_out(b64: str) -> None:
                # Record assistant audio if recorder is active
                session.record_assistant_audio(b64)
                await pool.broadcast_orchestrator({
                    "type": "voice_audio_out",
                    "audio": b64,
                })

            # Provider events fired by audio that listen_recording injected
            # into the WS must not reach the frontend: the UI's barge-in
            # handler would respond to a speech_started by sending
            # response.cancel, killing the agent's reply mid-stream — the
            # feedback loop we're guarding against.  Backend still
            # processes them (for the writer gates in process_voice_event).
            _INJECTION_SUPPRESSED_TYPES = frozenset({
                "input_audio_buffer.speech_started",
                "input_audio_buffer.speech_stopped",
                "input_audio_buffer.committed",
                "conversation.item.input_audio_transcription.completed",
            })

            # Frontend-irrelevant events that should never be mirrored.
            # Gemini Live emits a fresh ``sessionResumptionUpdate`` every
            # ~0.5s during an active session (one observed: 1000+ in a
            # 9-minute call). The handle is purely backend state for
            # reconnect — the provider's on_inbound_event already
            # captures it. Mirroring them just clogs the orchestrator WS
            # and the Android event-dispatch pipeline, which on the A300M
            # is enough to make the conversation feel sluggish.
            def _is_frontend_irrelevant(ev: dict) -> bool:
                # Gemini shape: no top-level "type", payload under a
                # camelCase root key.
                if not ev.get("type"):
                    if "sessionResumptionUpdate" in ev:
                        return True
                return False

            async def _on_event_for_frontend(event: dict) -> None:
                # Mirror provider events to the frontend so the UI can
                # reflect transcripts, status, etc.
                if _is_frontend_irrelevant(event):
                    pass  # skip the broadcast — still flows to backend logic below
                elif not (
                    session.is_injecting
                    and event.get("type") in _INJECTION_SUPPRESSED_TYPES
                ):
                    await pool.broadcast_orchestrator({
                        "type": "voice_event",
                        "event": event,
                    })
                # Tool calls go through _handle_voice_tool_call so the
                # tool_use / tool_result broadcasts fire and execution
                # happens off the relay-drain task.
                if event.get("type") == "response.function_call_arguments.done":
                    asyncio.create_task(
                        _handle_voice_tool_call(pool, session, event, inject=False),
                        name="voice-tool-call",
                    )
                    return
                # Gemini Live tool calls — same idea but the wire shape is
                # ``toolCall.functionCalls[]`` at the top level, no
                # ``type`` field.  Route through a dedicated dispatcher so
                # tool_use / tool_result broadcasts reach the chat UI.
                if not event.get("type") and event.get("toolCall"):
                    asyncio.create_task(
                        _handle_gemini_voice_tool_call(pool, session, event),
                        name="voice-tool-call-gemini",
                    )
                    return
                # All other events: run orchestrator-side processing
                # (persists transcripts, etc.).  inject=False because
                # the relay already pushed the event into the provider
                # queue before invoking this callback.
                try:
                    commands = await session.process_voice_event(event, inject=False)
                    await _dispatch_voice_commands(pool, session, commands)
                except Exception:  # noqa: BLE001
                    logger.exception("Voice event processing failed in relay drain")

            # Kick off the relay startup in the background so we can return
            # `session_started` to the frontend before the upstream WS
            # handshake completes.  On the Jetson the upstream connect plus
            # `session.created` round-trip can run several seconds; doing it
            # inline blew the frontend's 10s start timeout (the visible
            # symptom: "Voice session did not start (no connection_info
            # from server)").  We pass the precomputed `session_update` so
            # the relay does NOT re-summarize history — that's the second
            # major cost on the Jetson.  Relay readiness is announced via
            # the provider's own `session.created`/`session.updated`
            # events, mirrored through `_on_event_for_frontend`.
            async def _bg_start_relay() -> None:
                try:
                    await session.start_voice_relay(
                        _on_audio_out,
                        _on_event_for_frontend,
                        session_update=session_update,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.exception("Failed to start voice relay (bg)")
                    await pool.broadcast_orchestrator({
                        "type": "voice_connection_error",
                        "detail": str(e),
                    })

            asyncio.create_task(_bg_start_relay(), name="voice-relay-start")


async def _handle_send(
    pool: SessionPool, session: OrchestratorSession, text: str,
) -> None:
    """Stream orchestrator events to all subscribed WebSockets."""
    try:
        await pool.broadcast_orchestrator({"type": "status", "status": "streaming"})
        async for event in session.send(text):
            payload = serialize_orchestrator_event(event)
            await pool.broadcast_orchestrator(payload)
        await pool.broadcast_orchestrator({"type": "status", "status": "idle"})
    except Exception as e:
        logger.exception("Orchestrator send failed")
        await pool.broadcast_orchestrator({"type": "error", "error": "send_failed", "detail": str(e)})


async def _handle_send_audio(
    pool: SessionPool,
    session: OrchestratorSession,
    audio_base64: str,
    audio_format: str,
    text_prompt: str | None,
) -> None:
    """Process audio input through the multimodal model."""
    try:
        # Decode base64 audio
        try:
            audio_bytes = base64.b64decode(audio_base64)
        except Exception as e:
            await pool.broadcast_orchestrator({
                "type": "error",
                "error": "invalid_audio",
                "detail": f"Invalid base64 audio data: {e}",
            })
            return

        await pool.broadcast_orchestrator({"type": "status", "status": "streaming"})

        async for event in session.send_audio(audio_bytes, audio_format, text_prompt):
            payload = serialize_orchestrator_event(event)
            await pool.broadcast_orchestrator(payload)

        await pool.broadcast_orchestrator({"type": "status", "status": "idle"})
    except Exception as e:
        logger.exception("Orchestrator send_audio failed")
        await pool.broadcast_orchestrator({
            "type": "error",
            "error": "send_audio_failed",
            "detail": str(e),
        })


async def _handle_set_model(
    pool: SessionPool,
    session: OrchestratorSession,
    model_id: str,
) -> None:
    """Switch the model for the current session."""
    if session.is_voice:
        await pool.broadcast_orchestrator({
            "type": "error",
            "error": "cannot_switch_voice",
            "detail": "Cannot switch models during voice session",
        })
        return

    if session.set_model(model_id):
        await pool.broadcast_orchestrator({
            "type": "model_changed",
            "model_info": session.get_model_info(),
        })
    else:
        await pool.broadcast_orchestrator({
            "type": "error",
            "error": "unknown_model",
            "detail": f"Unknown model: {model_id}",
        })


async def _handle_get_model(ws: WebSocket, session: OrchestratorSession) -> None:
    """Get the current model info."""
    await _safe_send_bytes(ws, orjson.dumps({
        "type": "model_info",
        "model_info": session.get_model_info(),
    }))


async def _handle_get_models(ws: WebSocket) -> None:
    """Get list of all available models (live, with static fallback)."""
    try:
        models = await list_orchestrator_models()
    except Exception:
        logger.exception("Live model discovery failed; falling back to static")
        models = get_available_models()
    await _safe_send_bytes(ws, orjson.dumps({
        "type": "models_list",
        "models": [m.to_dict() for m in models],
    }))


async def _handle_compact(
    pool: SessionPool, session: OrchestratorSession,
) -> None:
    """Compact the orchestrator conversation history."""
    try:
        await pool.broadcast_orchestrator({"type": "status", "status": "streaming"})
        result = await session.compact()
        await pool.broadcast_orchestrator({
            "type": "compact_complete",
            "trigger": "manual",
            "tokens_before": result["tokens_before"],
            "tokens_after": result["tokens_after"],
        })
        await pool.broadcast_orchestrator({"type": "status", "status": "idle"})
    except Exception as e:
        logger.exception("Orchestrator compact failed")
        await pool.broadcast_orchestrator({
            "type": "error", "error": "compact_failed", "detail": str(e),
        })


async def _handle_voice_event(
    pool: SessionPool, session: OrchestratorSession, event: dict,
) -> None:
    """Process a mirrored realtime event and send back any voice commands.

    For WebRTC providers (OpenAI), the events arrive mirrored from the
    browser's data channel; commands generated by the backend are
    broadcast as ``voice_command`` so the frontend forwards them.

    For WebSocket providers (Qwen, Gemini, future locals), this also
    accepts client-originated events to forward upstream — but inbound
    provider events arrive through the backend relay rather than the
    frontend, so this path is mostly for tool-result payloads injected by
    the orchestrator itself.

    Tool calls (response.function_call_arguments.done) are spawned as a
    background task so the WS handler can continue processing other
    voice events (transcripts, interruptions, etc.) without blocking.
    """
    try:
        event_type = event.get("type", "")

        # Log all client-originated voice events except the high-frequency
        # transcript deltas (those would flood).  This gives us a clean
        # client→backend flow timeline for crash post-mortem.
        if event_type not in (
            "response.output_audio_transcript.delta",
            "response.audio_transcript.delta",
            "response.text.delta",
            "input_audio_buffer.append",
        ):
            logger.info(
                "voice_event_in session=%s type=%s",
                session.local_id,
                event_type,
            )

        # Tool calls are long-running — spawn as background task to avoid
        # blocking the WebSocket handler loop.
        if event_type == "response.function_call_arguments.done":
            asyncio.create_task(
                _handle_voice_tool_call(pool, session, event),
                name="voice-tool-call",
            )
            return

        # For WS providers (Qwen / Gemini / locals), client-originated
        # control events need to be forwarded upstream.  WebRTC providers
        # (OpenAI) skip this path — the frontend talks to the provider
        # directly via the data channel and only mirrors events here for
        # backend persistence.
        #
        # Filter through the provider's ``accepts_upstream_event`` first:
        # if the client is still mirroring events shaped for a previously
        # active provider (e.g. OpenAI ``session.created`` / ``error``
        # leaking onto a Gemini session during a provider crossover), we
        # drop them here instead of letting the upstream WS close with a
        # fatal 1007/1008 schema-mismatch error.  Without this filter,
        # resuming any session with a different voice provider was a
        # silent kill.
        if session.needs_voice_relay:
            provider = session.voice_provider  # type: ignore[union-attr]
            if provider is not None and not provider.accepts_upstream_event(event):
                logger.info(
                    "voice_event_drop session=%s type=%s reason=provider_schema_mismatch provider=%s",
                    session.local_id,
                    event_type or "?",
                    provider.provider_name,
                )
            else:
                try:
                    await session.send_voice_event_upstream(event)
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to forward client voice event upstream")

        commands = await session.process_voice_event(event)
        await _dispatch_voice_commands(pool, session, commands)
    except Exception as e:
        logger.exception("Voice event processing failed")
        await pool.broadcast_orchestrator({"type": "error", "error": "voice_event_failed", "detail": str(e)})


async def _dispatch_voice_commands(
    pool: SessionPool,
    session: OrchestratorSession,
    commands: list,
) -> None:
    """Send provider commands upstream (WS providers) or to frontend (WebRTC).

    For WebRTC providers, the frontend owns the data channel to the
    provider, so commands need to round-trip through the orchestrator WS
    as ``voice_command`` payloads. For WS providers, the backend relay
    holds the upstream connection and sends them directly.
    """
    if session.needs_voice_relay:
        for cmd in commands:
            try:
                await session.send_voice_event_upstream(cmd)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to forward voice command upstream")
    else:
        for cmd in commands:
            await pool.broadcast_orchestrator({"type": "voice_command", "command": cmd})


def _tool_result_is_error(output: str) -> bool:
    """True when a tool's stringified JSON output carries an ``error`` field.

    Orchestrator tools (``open_agent_session``, ``send_to_agent_session``, …)
    signal failure by returning ``json.dumps({"error": "..."})`` rather than
    raising — historically the route layer always stamped ``is_error: False``
    on the broadcast, so the chat UI rendered the bubble as if the call had
    succeeded even when it hadn't. This helper flips that around so the
    user sees a red error bubble that matches Gemini's spoken (often
    misleadingly confident) follow-up.
    """
    if not output:
        return False
    try:
        parsed = orjson.loads(output)
    except (orjson.JSONDecodeError, ValueError, TypeError):
        return False
    return isinstance(parsed, dict) and "error" in parsed


async def _handle_voice_tool_call(
    pool: SessionPool, session: OrchestratorSession, event: dict,
    *,
    inject: bool = True,
) -> None:
    """Execute a voice tool call in the background without blocking the WS handler.

    ``inject=False`` when the relay drain already pushed the event into the
    provider queue (avoids double-injection for WS providers).
    """
    try:
        import json as _json
        call_id = event.get("call_id", "")
        name = event.get("name", "")

        # Fall back to pending_calls if name not in event
        # (OpenAI sends the name in response.output_item.added, not always in the done event)
        if not name and hasattr(session, "_voice_provider") and session._voice_provider:
            name = session._voice_provider.pending_calls.get(call_id, "")

        # Prefer accumulated streaming args over the event's arguments field
        pending_args = ""
        if hasattr(session, "_voice_provider") and session._voice_provider:
            pending_args = session._voice_provider._pending_args.get(call_id, "")
        try:
            tool_input = _json.loads(pending_args or event.get("arguments", "") or "{}")
        except Exception:
            tool_input = {}

        # Broadcast tool_use so the chat UI shows the tool call starting
        if call_id and name:
            await pool.broadcast_orchestrator({
                "type": "tool_use",
                "tool_use_id": call_id,
                "tool_name": name,
                "tool_input": tool_input,
            })

        # Execute the tool (this is the potentially long-running part)
        commands = await session.process_voice_event(event, inject=inject)

        # Broadcast tool_result after execution completes
        for cmd in commands:
            if cmd.get("type") == "conversation.item.create":
                item = cmd.get("item", {})
                if item.get("type") == "function_call_output":
                    output = item.get("output", "")
                    await pool.broadcast_orchestrator({
                        "type": "tool_result",
                        "tool_use_id": item.get("call_id", ""),
                        "output": output,
                        "is_error": _tool_result_is_error(output),
                    })

        await _dispatch_voice_commands(pool, session, commands)
    except Exception as e:
        logger.exception("Voice tool call execution failed")
        await pool.broadcast_orchestrator({"type": "error", "error": "voice_event_failed", "detail": str(e)})


async def _handle_gemini_voice_tool_call(
    pool: SessionPool, session: OrchestratorSession, event: dict,
) -> None:
    """Execute a Gemini Live tool call off the relay-drain task.

    Gemini's wire shape is ``toolCall.functionCalls: [{id, name, args}]``
    with no top-level ``type`` field.  We broadcast ``tool_use`` per call
    before execution, run them via ``process_voice_event`` (which writes
    JSONL + dispatches the ``toolResponse`` back upstream), then
    broadcast ``tool_result`` per call so the chat UI renders the
    output the assistant is about to talk over.
    """
    try:
        calls = (event.get("toolCall") or {}).get("functionCalls", []) or []

        for call in calls:
            call_id = call.get("id", "")
            name = call.get("name", "")
            args = call.get("args", {}) or {}
            if call_id and name:
                await pool.broadcast_orchestrator({
                    "type": "tool_use",
                    "tool_use_id": call_id,
                    "tool_name": name,
                    "tool_input": args,
                })

        commands = await session.process_voice_event(event, inject=False)

        # Gemini's tool result commands look like
        # ``{"toolResponse": {"functionResponses": [{id, name, response}]}}``.
        # Surface each as a tool_result broadcast.
        for cmd in commands:
            tr = cmd.get("toolResponse") if isinstance(cmd, dict) else None
            if not tr:
                continue
            for fr in tr.get("functionResponses", []) or []:
                output = fr.get("response", {}).get("output", "")
                await pool.broadcast_orchestrator({
                    "type": "tool_result",
                    "tool_use_id": fr.get("id", ""),
                    "output": output,
                    "is_error": _tool_result_is_error(output),
                })

        await _dispatch_voice_commands(pool, session, commands)
    except Exception as e:
        logger.exception("Gemini voice tool call execution failed")
        await pool.broadcast_orchestrator({"type": "error", "error": "voice_event_failed", "detail": str(e)})
