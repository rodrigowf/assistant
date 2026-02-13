"""WebSocket chat endpoint — real-time streaming via SessionManager."""

from __future__ import annotations

import asyncio
import logging
import orjson
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.connections import ConnectionManager
from api.serializers import serialize_event
from manager.config import ManagerConfig
from manager.session import SessionManager

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])


@router.websocket("/api/sessions/chat")
async def chat_ws(ws: WebSocket):
    await ws.accept()
    cm: ConnectionManager = ws.app.state.connections

    sm: SessionManager | None = None
    session_id: str | None = None

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
                sm, session_id = await _handle_start(ws, cm, msg)
                if sm is None:
                    continue

            elif msg_type == "send":
                if sm is None:
                    await ws.send_bytes(orjson.dumps({
                        "type": "error", "error": "not_started",
                        "detail": "Send a 'start' message first",
                    }))
                    continue
                await _handle_send(ws, sm, msg.get("text", ""))

            elif msg_type == "command":
                if sm is None:
                    await ws.send_bytes(orjson.dumps({
                        "type": "error", "error": "not_started",
                    }))
                    continue
                await _handle_command(ws, sm, msg.get("text", ""))

            elif msg_type == "interrupt":
                if sm is not None:
                    await sm.interrupt()
                    await ws.send_bytes(orjson.dumps({
                        "type": "status", "status": "interrupted",
                    }))

            elif msg_type == "stop":
                if session_id:
                    await cm.disconnect(session_id)
                sm = None
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
        if session_id:
            await cm.disconnect(session_id)


async def _handle_start(
    ws: WebSocket, cm: ConnectionManager, msg: dict,
) -> tuple[SessionManager | None, str | None]:
    """Start or resume a session. Returns (sm, session_id) or (None, None)."""
    resume_id = msg.get("session_id")
    fork = msg.get("fork", False)
    config = ManagerConfig.load()

    sm = SessionManager(session_id=resume_id, fork=fork, config=config)
    try:
        # Send status to frontend while connecting
        await ws.send_bytes(orjson.dumps({
            "type": "status", "status": "connecting",
        }))
        session_id = await asyncio.wait_for(sm.start(), timeout=30.0)
    except asyncio.TimeoutError:
        logger.warning("Session start timed out after 30s")
        await ws.send_bytes(orjson.dumps({
            "type": "error", "error": "start_timeout",
            "detail": "Session start timed out. Claude Code may not be authenticated.",
        }))
        return None, None
    except Exception as e:
        logger.exception("Session start failed")
        await ws.send_bytes(orjson.dumps({
            "type": "error", "error": "start_failed",
            "detail": str(e),
        }))
        return None, None

    cm.connect(session_id, ws, sm)
    await ws.send_bytes(orjson.dumps({
        "type": "session_started", "session_id": session_id,
    }))
    return sm, session_id


async def _handle_send(ws: WebSocket, sm: SessionManager, text: str) -> None:
    """Stream events from sm.send() to the WebSocket."""
    try:
        async for event in sm.send(text):
            payload = serialize_event(event)
            await ws.send_bytes(orjson.dumps(payload))
    except Exception as e:
        await ws.send_bytes(orjson.dumps({
            "type": "error", "error": "send_failed",
            "detail": str(e),
        }))


async def _handle_command(ws: WebSocket, sm: SessionManager, text: str) -> None:
    """Stream events from sm.command() to the WebSocket."""
    try:
        async for event in sm.command(text):
            payload = serialize_event(event)
            await ws.send_bytes(orjson.dumps(payload))
    except Exception as e:
        await ws.send_bytes(orjson.dumps({
            "type": "error", "error": "command_failed",
            "detail": str(e),
        }))
