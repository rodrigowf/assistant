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
                session, session_id = await _handle_start(ws, ocm, msg)
                if session is None:
                    continue

            elif msg_type == "send":
                if session is None:
                    await ws.send_bytes(orjson.dumps({
                        "type": "error", "error": "not_started",
                        "detail": "Send a 'start' message first",
                    }))
                    continue
                await _handle_send(ws, session, msg.get("text", ""))

            elif msg_type == "interrupt":
                if session is not None:
                    await session.interrupt()
                    await ws.send_bytes(orjson.dumps({
                        "type": "status", "status": "interrupted",
                    }))

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
        pass
    finally:
        pool.unwatch(ws)
        if session_id:
            await ocm.disconnect()


async def _handle_start(
    ws: WebSocket, ocm: OrchestratorConnectionManager, msg: dict,
) -> tuple[OrchestratorSession | None, str | None]:
    """Start or resume an orchestrator session."""
    resume_id = msg.get("session_id")

    # Check if an orchestrator is already active
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

    await ws.send_bytes(orjson.dumps({
        "type": "session_started", "session_id": session_id,
    }))
    return session, session_id


async def _handle_send(
    ws: WebSocket, session: OrchestratorSession, text: str,
) -> None:
    """Stream orchestrator events to the WebSocket."""
    try:
        await ws.send_bytes(orjson.dumps({
            "type": "status", "status": "streaming",
        }))
        async for event in session.send(text):
            payload = serialize_orchestrator_event(event)
            await ws.send_bytes(orjson.dumps(payload))
        await ws.send_bytes(orjson.dumps({
            "type": "status", "status": "idle",
        }))
    except Exception as e:
        logger.exception("Orchestrator send failed")
        await ws.send_bytes(orjson.dumps({
            "type": "error", "error": "send_failed",
            "detail": str(e),
        }))
