"""WebSocket orchestrator endpoint — streams orchestrator agent events."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import orjson
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.pool import SessionPool
from api.serializers import serialize_orchestrator_event
from orchestrator.config import OrchestratorConfig
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

            elif msg_type == "voice_event":
                if session is None or not session.is_voice:
                    await ws.send_bytes(orjson.dumps({
                        "type": "error", "error": "not_voice_session",
                        "detail": "No active voice session",
                    }))
                    continue
                await _handle_voice_event(pool, session, msg.get("event", {}))

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

    except WebSocketDisconnect:
        logger.info("Orchestrator WS disconnected (client closed)")
    except Exception:
        logger.exception("Orchestrator WS error")
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
            await ws.send_bytes(orjson.dumps({
                "type": "session_started",
                "session_id": local_id,
                "voice": current_voice,
            }))
            return session, True
        else:
            # Same mode — just reconnect
            pool.subscribe_orchestrator(local_id, ws)
            await ws.send_bytes(orjson.dumps({
                "type": "session_started",
                "session_id": local_id,
                "voice": current_voice,
            }))
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

    started_payload: dict = {
        "type": "session_started",
        "session_id": session_id,
        "voice": voice,
    }
    if voice:
        session_update = session.get_session_update()
        if session_update:
            started_payload["voice_session_update"] = session_update

    await ws.send_bytes(orjson.dumps(started_payload))
    return session, True


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


async def _handle_voice_event(
    pool: SessionPool, session: OrchestratorSession, event: dict,
) -> None:
    """Process a mirrored OpenAI Realtime event and send back any voice commands.

    Tool calls (response.function_call_arguments.done) are spawned as background
    tasks so the WebSocket handler can continue processing other voice events
    (transcripts, interruptions, etc.) without blocking.
    """
    try:
        event_type = event.get("type", "")

        # Tool calls are long-running — spawn as background task to avoid
        # blocking the WebSocket handler loop.
        if event_type == "response.function_call_arguments.done":
            asyncio.create_task(
                _handle_voice_tool_call(pool, session, event),
                name="voice-tool-call",
            )
            return

        commands = await session.process_voice_event(event)

        for cmd in commands:
            await pool.broadcast_orchestrator({"type": "voice_command", "command": cmd})
    except Exception as e:
        logger.exception("Voice event processing failed")
        await pool.broadcast_orchestrator({"type": "error", "error": "voice_event_failed", "detail": str(e)})


async def _handle_voice_tool_call(
    pool: SessionPool, session: OrchestratorSession, event: dict,
) -> None:
    """Execute a voice tool call in the background without blocking the WS handler."""
    try:
        import json as _json
        call_id = event.get("call_id", "")
        name = event.get("name", "")
        try:
            tool_input = _json.loads(event.get("arguments", "{}"))
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
        commands = await session.process_voice_event(event)

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

        for cmd in commands:
            await pool.broadcast_orchestrator({"type": "voice_command", "command": cmd})
    except Exception as e:
        logger.exception("Voice tool call execution failed")
        await pool.broadcast_orchestrator({"type": "error", "error": "voice_event_failed", "detail": str(e)})
