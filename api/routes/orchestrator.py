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
from pathlib import Path

import orjson
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.pool import SessionPool
from api.serializers import serialize_orchestrator_event
from orchestrator.config import OrchestratorConfig, get_available_models
from orchestrator.providers.discovery import list_orchestrator_models
from orchestrator.session import OrchestratorSession

logger = logging.getLogger(__name__)
router = APIRouter(tags=["orchestrator"])


@router.websocket("/api/orchestrator/chat")
async def orchestrator_ws(ws: WebSocket):
    await ws.accept()

    pool: SessionPool = ws.app.state.pool
    session: OrchestratorSession | None = None
    subscribed = False  # True once this ws is registered in pool._orchestrator_subs

    # Register as a watcher so this ws receives agent_session_opened/closed events
    pool.watch(ws)

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = orjson.loads(raw)
            except (orjson.JSONDecodeError, ValueError):
                await ws.send_bytes(orjson.dumps({
                    "type": "error", "error": "invalid_json",
                }))
                continue

            msg_type = msg.get("type", "")

            if msg_type == "start":
                session, subscribed = await _handle_start(ws, pool, msg, voice=False)

            elif msg_type == "voice_start":
                session, subscribed = await _handle_start(ws, pool, msg, voice=True)

            elif msg_type == "send":
                if session is None:
                    await ws.send_bytes(orjson.dumps({
                        "type": "error", "error": "not_started",
                        "detail": "Send a 'start' message first",
                    }))
                    continue
                await _handle_send(pool, session, msg.get("text", ""))

            elif msg_type == "send_audio":
                if session is None:
                    await ws.send_bytes(orjson.dumps({
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
                    await ws.send_bytes(orjson.dumps({
                        "type": "error", "error": "not_started",
                        "detail": "Send a 'start' message first",
                    }))
                    continue
                await _handle_set_model(pool, session, msg.get("model", ""))

            elif msg_type == "get_model":
                if session is None:
                    await ws.send_bytes(orjson.dumps({
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
                    await ws.send_bytes(orjson.dumps({
                        "type": "error", "error": "not_voice_session",
                        "detail": "No active voice session",
                    }))
                    continue
                await _handle_voice_event(pool, session, msg.get("event", {}))

            elif msg_type == "voice_audio_in":
                # WS-provider mic chunk from the browser → forward upstream.
                if session is None or not session.is_voice:
                    await ws.send_bytes(orjson.dumps({
                        "type": "error", "error": "not_voice_session",
                        "detail": "No active voice session",
                    }))
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

            elif msg_type == "compact":
                if session is None:
                    await ws.send_bytes(orjson.dumps({
                        "type": "error", "error": "not_started",
                        "detail": "Send a 'start' message first",
                    }))
                    continue
                await _handle_compact(pool, session)

            elif msg_type == "interrupt":
                if session is not None:
                    await session.interrupt()
                    await pool.broadcast_orchestrator({"type": "status", "status": "interrupted"})

            elif msg_type == "stop":
                await pool.stop_orchestrator()
                session = None
                subscribed = False
                await ws.send_bytes(orjson.dumps({"type": "session_stopped"}))

            else:
                await ws.send_bytes(orjson.dumps({
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
        # Orchestrator session keeps running headlessly until explicitly stopped.


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

    if voice:
        logger.info(
            "voice_session start_requested local_id=%s resume_id=%s provider=%s model=%s voice=%s lang=%s",
            local_id, resume_id, voice_provider_req, voice_model_req, voice_name_req, voice_lang_req,
        )

    # --- Reconnect: an orchestrator with this local_id is already running ---
    if pool.has_orchestrator() and local_id and pool.orchestrator_id == local_id:
        session = pool.get_orchestrator()
        current_voice = getattr(session, "is_voice", False)

        if voice and not current_voice:
            # Text→voice transition: stop text session, fall through to create voice
            await pool.stop_orchestrator()
        elif not voice and current_voice:
            # Text WS reconnecting while voice is active — subscribe without
            # disrupting the voice session (the text WS auto-connects on mount).
            pool.subscribe_orchestrator(local_id, ws)
            reconnect_payload: dict = {
                "type": "session_started",
                "session_id": local_id,
                "voice": current_voice,
                "model_info": session.get_model_info(),
            }
            await _attach_voice_payload(reconnect_payload, session)
            await ws.send_bytes(orjson.dumps(reconnect_payload))
            return session, True
        else:
            # Same mode — just reconnect
            pool.subscribe_orchestrator(local_id, ws)
            reconnect_payload = {
                "type": "session_started",
                "session_id": local_id,
                "voice": current_voice,
                "model_info": session.get_model_info(),
            }
            if current_voice:
                await _attach_voice_payload(reconnect_payload, session)
            await ws.send_bytes(orjson.dumps(reconnect_payload))
            return session, True

    # --- A different orchestrator is already active ---
    if pool.has_orchestrator():
        await ws.send_bytes(orjson.dumps({
            "type": "error", "error": "orchestrator_active",
            "detail": "An orchestrator session is already active. Stop it first.",
        }))
        return None, False

    # --- Start a new (or resumed) orchestrator session ---
    config = OrchestratorConfig.load()
    project_dir = config.project_dir

    # If voice mode and any of provider/model/voice/language missing from
    # the start message, fall back to what's saved in assistant_config.json.
    if voice and (
        voice_provider_req is None
        or voice_model_req is None
        or voice_name_req is None
        or voice_lang_req is None
    ):
        try:
            from api.routes.config import _load_config as _load_app_config
            app_cfg = _load_app_config()
            voice_provider_req = voice_provider_req or app_cfg.get("default_voice_provider")
            voice_model_req = voice_model_req or app_cfg.get("default_voice_model")
            voice_name_req = voice_name_req or app_cfg.get("default_voice_name")
            if voice_lang_req is None:
                voice_lang_req = app_cfg.get("default_voice_transcription_language")
        except Exception:
            logger.exception("Failed to load voice defaults from assistant_config.json")

    context: dict = {
        "store": ws.app.state.store,
        "manager_config": ws.app.state.config,
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
    )

    await ws.send_bytes(orjson.dumps({"type": "status", "status": "connecting"}))
    try:
        session_id = await session.start()
    except Exception as e:
        logger.exception("Orchestrator session start failed")
        await ws.send_bytes(orjson.dumps({
            "type": "error", "error": "start_failed", "detail": str(e),
        }))
        return None, False

    pool.set_orchestrator(session_id, session)
    pool.subscribe_orchestrator(session_id, ws)

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

    started_payload: dict = {
        "type": "session_started",
        "session_id": session_id,
        "voice": voice,
        "model_info": session.get_model_info(),
    }
    if voice:
        await _attach_voice_payload(started_payload, session)

    await ws.send_bytes(orjson.dumps(started_payload))
    return session, True


async def _attach_voice_payload(
    payload: dict,
    session: OrchestratorSession,
) -> None:
    """Mutate ``payload`` to include voice provider metadata and the
    provider-specific session.update + connection info, and start the
    backend relay for WebSocket providers if it isn't already running.

    Errors fetching the ephemeral token or starting the relay are
    swallowed and reported back as ``voice_connection_error`` so the
    frontend can surface them; the session itself stays alive.
    """
    payload["voice_provider"] = session.voice_provider_id
    payload["voice_model"] = session.voice_model_id
    payload["voice_name"] = session.voice_name_id
    payload["voice_transcription_language"] = session.voice_transcription_language

    session_update = await session.get_session_update()
    if session_update:
        payload["voice_session_update"] = session_update

    provider_obj = getattr(session, "_voice_provider", None)
    if provider_obj is not None:
        try:
            payload["voice_connection_info"] = await provider_obj.get_connection_info()
        except Exception as e:
            logger.warning("Voice connection info fetch failed: %s", e)
            payload["voice_connection_error"] = str(e)

    # Start the backend relay for WS providers (Qwen / Gemini / locals).
    # Idempotent: skipped if already running, no-op for WebRTC providers.
    if session.needs_voice_relay and getattr(session, "_voice_relay", None) is None:
        try:
            pool = session._context.get("pool")
            if pool is not None:
                async def _on_audio_out(b64: str) -> None:
                    await pool.broadcast_orchestrator({
                        "type": "voice_audio_out",
                        "audio": b64,
                    })

                async def _on_event_for_frontend(event: dict) -> None:
                    # Mirror provider events to the frontend so the UI can
                    # reflect transcripts, status, etc.
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
                    # All other events: run orchestrator-side processing
                    # (persists transcripts, etc.).  inject=False because
                    # the relay already pushed the event into the provider
                    # queue before invoking this callback.
                    try:
                        commands = await session.process_voice_event(event, inject=False)
                        await _dispatch_voice_commands(pool, session, commands)
                    except Exception:  # noqa: BLE001
                        logger.exception("Voice event processing failed in relay drain")

                await session.start_voice_relay(_on_audio_out, _on_event_for_frontend)
        except Exception as e:
            logger.exception("Failed to start voice relay")
            payload["voice_connection_error"] = str(e)


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
    await ws.send_bytes(orjson.dumps({
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
    await ws.send_bytes(orjson.dumps({
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
        if session.needs_voice_relay:
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
                    await pool.broadcast_orchestrator({
                        "type": "tool_result",
                        "tool_use_id": item.get("call_id", ""),
                        "output": item.get("output", ""),
                        "is_error": False,
                    })

        await _dispatch_voice_commands(pool, session, commands)
    except Exception as e:
        logger.exception("Voice tool call execution failed")
        await pool.broadcast_orchestrator({"type": "error", "error": "voice_event_failed", "detail": str(e)})
