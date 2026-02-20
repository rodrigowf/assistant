"""WebSocket orchestrator endpoint — streams orchestrator agent events."""

from __future__ import annotations

import logging
from pathlib import Path

import orjson
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.connections import OrchestratorConnectionManager
from api.pool import SessionPool
from api.serializers import serialize_orchestrator_event
from orchestrator.config import OrchestratorConfig
from orchestrator.session import OrchestratorSession

logger = logging.getLogger(__name__)
router = APIRouter(tags=["orchestrator"])


@router.websocket("/api/orchestrator/chat")
async def orchestrator_ws(ws: WebSocket):
    await ws.accept()

    ocm: OrchestratorConnectionManager = ws.app.state.orchestrator_connections
    pool: SessionPool = ws.app.state.pool
    session: OrchestratorSession | None = None
    session_id: str | None = None

    # Register this WS as a pool watcher so it gets agent_session_opened notifications
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
                session, session_id = await _handle_start(ws, ocm, msg, voice=False)
                if session is None:
                    continue

            elif msg_type == "voice_start":
                session, session_id = await _handle_start(ws, ocm, msg, voice=True)
                if session is None:
                    continue

            elif msg_type == "send":
                if session is None:
                    await ws.send_bytes(orjson.dumps({
                        "type": "error", "error": "not_started",
                        "detail": "Send a 'start' message first",
                    }))
                    continue
                await _handle_send(ocm, session, msg.get("text", ""))

            elif msg_type == "voice_event":
                # Frontend mirrors an OpenAI Realtime data channel event to us
                if session is None or not session.is_voice:
                    await ws.send_bytes(orjson.dumps({
                        "type": "error", "error": "not_voice_session",
                        "detail": "No active voice session",
                    }))
                    continue
                await _handle_voice_event(ocm, session, msg.get("event", {}))

            elif msg_type == "interrupt":
                if session is not None:
                    await session.interrupt()
                    await ocm.broadcast({"type": "status", "status": "interrupted"})

            elif msg_type == "stop":
                if session_id:
                    await ocm.disconnect()
                session = None
                session_id = None
                await ws.send_bytes(orjson.dumps({"type": "session_stopped"}))

            else:
                await ws.send_bytes(orjson.dumps({
                    "type": "error", "error": "unknown_type",
                    "detail": f"Unknown message type: {msg_type!r}",
                }))

    except WebSocketDisconnect:
        logger.info("Orchestrator WS disconnected (client closed)")
    except Exception:
        logger.exception("Orchestrator WS error")
    finally:
        logger.info("Orchestrator WS cleanup: subscriber_count=%d session_id=%s", ocm.subscriber_count, session_id)
        pool.unwatch(ws)
        ocm.unsubscribe(ws)
        # Session keeps running headlessly — reconnected when the browser reopens.


async def _handle_start(
    ws: WebSocket,
    ocm: OrchestratorConnectionManager,
    msg: dict,
    voice: bool = False,
) -> tuple[OrchestratorSession | None, str | None]:
    """Start, resume, or reconnect to an orchestrator session.

    The frontend sends ``local_id`` (stable tab UUID) and optionally
    ``session_id`` (orchestrator session ID for resuming from history).
    """
    local_id = msg.get("local_id")
    resume_id = msg.get("resume_sdk_id") or msg.get("session_id")

    logger.info("_handle_start: local_id=%s resume_id=%s ocm.is_active=%s ocm.session_id=%s", local_id, resume_id, ocm.is_active, ocm.session_id)

    # Reconnect / multi-device: another browser is subscribing to the same
    # already-active orchestrator session.
    if ocm.is_active and local_id and ocm.session_id == local_id:
        if ocm.subscribe(local_id, ws):
            session = ocm.get_session()
            await ws.send_bytes(orjson.dumps({
                "type": "session_started",
                "session_id": local_id,
                "voice": getattr(session, "is_voice", False),
            }))
            return session, local_id

    # Check if a *different* orchestrator is already active
    if ocm.is_active:
        await ws.send_bytes(orjson.dumps({
            "type": "error", "error": "orchestrator_active",
            "detail": "An orchestrator session is already active. Stop it first.",
        }))
        return None, None

    config = OrchestratorConfig.load()
    project_dir = config.project_dir

    # Build context with references to shared app state
    context: dict = {
        "connections": ws.app.state.connections,
        "store": ws.app.state.store,
        "manager_config": ws.app.state.config,
        "pool": ws.app.state.pool,
        "project_dir": project_dir,
        "index_dir": str(Path(project_dir) / "index" / "chroma"),
    }

    session = OrchestratorSession(
        config=config,
        context=context,
        session_id=resume_id,
        local_id=local_id,
        voice=voice,
    )

    try:
        await ws.send_bytes(orjson.dumps({
            "type": "status", "status": "connecting",
        }))
        session_id = await session.start()
    except Exception as e:
        logger.exception("Orchestrator session start failed")
        await ws.send_bytes(orjson.dumps({
            "type": "error", "error": "start_failed",
            "detail": str(e),
        }))
        return None, None

    if not ocm.connect(session_id, ws, session):
        await session.stop()
        await ws.send_bytes(orjson.dumps({
            "type": "error", "error": "orchestrator_active",
            "detail": "Another orchestrator connected in the meantime.",
        }))
        return None, None

    started_payload: dict = {
        "type": "session_started",
        "session_id": session_id,
        "voice": voice,
    }

    # For voice sessions, include the session.update payload for the frontend
    # to forward to OpenAI via the data channel
    if voice:
        session_update = session.get_session_update()
        if session_update:
            started_payload["voice_session_update"] = session_update

    await ws.send_bytes(orjson.dumps(started_payload))
    return session, session_id


async def _handle_send(
    ocm: OrchestratorConnectionManager, session: OrchestratorSession, text: str,
) -> None:
    """Stream orchestrator events to all subscribed WebSockets."""
    try:
        await ocm.broadcast({"type": "status", "status": "streaming"})
        async for event in session.send(text):
            payload = serialize_orchestrator_event(event)
            await ocm.broadcast(payload)
        await ocm.broadcast({"type": "status", "status": "idle"})
    except Exception as e:
        logger.exception("Orchestrator send failed")
        await ocm.broadcast({"type": "error", "error": "send_failed", "detail": str(e)})


async def _handle_voice_event(
    ocm: OrchestratorConnectionManager, session: OrchestratorSession, event: dict,
) -> None:
    """Process a mirrored OpenAI Realtime event and send back any voice commands."""
    try:
        commands = await session.process_voice_event(event)
        for cmd in commands:
            # Send each command as a voice_command for the frontend to forward to OpenAI
            await ocm.broadcast({"type": "voice_command", "command": cmd})
    except Exception as e:
        logger.exception("Voice event processing failed")
        await ocm.broadcast({"type": "error", "error": "voice_event_failed", "detail": str(e)})
