"""WebSocket chat endpoint — real-time streaming via SessionManager."""

from __future__ import annotations

import asyncio
import logging
import orjson
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.pool import SessionPool
from api.serializers import serialize_event
from manager.session import SessionManager

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])


@router.websocket("/api/sessions/chat")
async def chat_ws(ws: WebSocket):
    await ws.accept()
    pool: SessionPool = ws.app.state.pool

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
                sm, session_id = await _handle_start(ws, pool, msg)
                if sm is None:
                    continue

            elif msg_type == "send":
                if sm is None:
                    await ws.send_bytes(orjson.dumps({
                        "type": "error", "error": "not_started",
                        "detail": "Send a 'start' message first",
                    }))
                    continue
                session_id = await _handle_send(ws, sm, pool, session_id, msg.get("text", ""))

            elif msg_type == "command":
                if sm is None:
                    await ws.send_bytes(orjson.dumps({
                        "type": "error", "error": "not_started",
                    }))
                    continue
                await _handle_command(ws, sm, msg.get("text", ""))

            elif msg_type == "interrupt":
                if sm is not None and session_id:
                    await pool.interrupt(session_id)
                    await ws.send_bytes(orjson.dumps({
                        "type": "status", "status": "interrupted",
                    }))

            elif msg_type == "stop":
                if session_id:
                    pool.unsubscribe(session_id, ws)
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
            pool.unsubscribe(session_id, ws)


async def _handle_start(
    ws: WebSocket, pool: SessionPool, msg: dict,
) -> tuple[SessionManager | None, str | None]:
    """Start or resume a session via the pool. Returns (sm, session_id) or (None, None)."""
    resume_id = msg.get("session_id")
    fork = msg.get("fork", False)

    # Check if this session already exists in the pool
    if resume_id and pool.has(resume_id):
        sm = pool.get(resume_id)
        pool.subscribe(resume_id, ws)
        await ws.send_bytes(orjson.dumps({
            "type": "session_started", "session_id": resume_id,
        }))
        return sm, resume_id

    # Create a new session via the pool
    from manager.config import ManagerConfig
    config = ManagerConfig.load()
    try:
        await ws.send_bytes(orjson.dumps({
            "type": "status", "status": "connecting",
        }))
        session_id = await asyncio.wait_for(
            pool.create(config, session_id=resume_id, fork=fork),
            timeout=30.0,
        )
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

    sm = pool.get(session_id)
    pool.subscribe(session_id, ws)
    await ws.send_bytes(orjson.dumps({
        "type": "session_started", "session_id": session_id,
    }))
    return sm, session_id


async def _handle_send(
    ws: WebSocket,
    sm: SessionManager,
    pool: SessionPool,
    session_id: str | None,
    text: str,
) -> str | None:
    """Stream events to the WebSocket via pool broadcast.

    Returns the (potentially updated) session_id — the pool may re-key the
    session after the first query when the SDK assigns the real ID.
    """
    from manager.types import TurnComplete

    try:
        async for event in pool.send(session_id, text, source_ws=ws):
            # Track session ID changes so the caller updates its local ref
            if isinstance(event, TurnComplete) and event.session_id:
                session_id = event.session_id
    except Exception as e:
        await ws.send_bytes(orjson.dumps({
            "type": "error", "error": "send_failed",
            "detail": str(e),
        }))
    return session_id


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
