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
    # Task currently streaming a response (send/compact/command), if any
    stream_task: asyncio.Task | None = None

    async def _cancel_stream() -> None:
        """Cancel the active stream task and wait for it to finish."""
        nonlocal stream_task
        if stream_task is not None and not stream_task.done():
            stream_task.cancel()
            try:
                await stream_task
            except (asyncio.CancelledError, Exception):
                pass
        stream_task = None

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
                # Cancel any in-progress stream before starting a new one
                await _cancel_stream()
                stream_task = asyncio.create_task(
                    _handle_send(ws, sm, pool, session_id, msg.get("text", ""))
                )

            elif msg_type == "command":
                if sm is None:
                    await ws.send_bytes(orjson.dumps({
                        "type": "error", "error": "not_started",
                    }))
                    continue
                await _cancel_stream()
                stream_task = asyncio.create_task(
                    _handle_command(ws, sm, msg.get("text", ""))
                )

            elif msg_type == "interrupt":
                if sm is not None and session_id:
                    # Cancel the stream task first so the SDK interrupt is effective
                    await _cancel_stream()
                    await pool.interrupt(session_id)
                    await ws.send_bytes(orjson.dumps({
                        "type": "status", "status": "interrupted",
                    }))

            elif msg_type == "compact":
                if sm is None or session_id is None:
                    await ws.send_bytes(orjson.dumps({
                        "type": "error", "error": "not_started",
                    }))
                    continue
                await _cancel_stream()
                stream_task = asyncio.create_task(
                    _handle_compact(ws, pool, session_id)
                )

            elif msg_type == "stop":
                await _cancel_stream()
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
        await _cancel_stream()
        if session_id:
            pool.unsubscribe(session_id, ws)


async def _handle_start(
    ws: WebSocket, pool: SessionPool, msg: dict,
) -> tuple[SessionManager | None, str | None]:
    """Start or resume a session via the pool. Returns (sm, session_id) or (None, None).

    The frontend sends ``local_id`` (stable tab UUID) and optionally
    ``resume_sdk_id`` (Claude Code SDK session ID for resuming from history).
    Optionally includes ``mcp_servers`` dict to specify which MCPs to load.
    """
    local_id = msg.get("local_id")
    resume_sdk_id = msg.get("resume_sdk_id") or msg.get("session_id")
    fork = msg.get("fork", False)
    mcp_servers = msg.get("mcp_servers")  # Optional: dict of MCP servers to load

    # Check if this session already exists in the pool (re-subscribing)
    if local_id and pool.has(local_id):
        sm = pool.get(local_id)
        pool.subscribe(local_id, ws)
        await ws.send_bytes(orjson.dumps({
            "type": "session_started", "session_id": local_id,
        }))
        return sm, local_id

    # Create a new session via the pool
    from manager.config import ManagerConfig
    from api.routes.config import _load_config as _load_assistant_config, _find_active_entry
    config = ManagerConfig.load()
    # Apply global assistant config overrides (working directory, MCPs)
    assistant_cfg = _load_assistant_config()
    from dataclasses import replace
    active_entry = _find_active_entry(assistant_cfg)
    if active_entry:
        config = replace(
            config,
            project_dir=active_entry["path"],
            ssh_host=active_entry.get("ssh_host") or None,
            ssh_user=active_entry.get("ssh_user") or None,
            ssh_key=active_entry.get("ssh_key") or None,
        )
    else:
        config = replace(config, project_dir=assistant_cfg.get("working_directory", config.project_dir))
    # If no per-session MCPs provided, use the globally-enabled MCPs from the config.
    # An empty list in enabled_mcps means "no MCPs" (opt-in); None means "use defaults".
    if mcp_servers is None:
        enabled_mcps: list[str] = assistant_cfg.get("enabled_mcps", [])
        if enabled_mcps:
            # Load full MCP configs from .claude.json and filter to enabled ones
            from api.routes.mcp import _load_mcp_servers
            all_mcps = _load_mcp_servers()
            mcp_servers = {k: v for k, v in all_mcps.items() if k in enabled_mcps} or None
    # Apply chrome extension flag if enabled
    if assistant_cfg.get("chrome_extension", False):
        config = replace(config, extra_args={"chrome": None})
    try:
        await ws.send_bytes(orjson.dumps({
            "type": "status", "status": "connecting",
        }))
        session_id = await asyncio.wait_for(
            pool.create(
                config,
                local_id=local_id,
                resume_sdk_id=resume_sdk_id,
                fork=fork,
                mcp_servers=mcp_servers,
            ),
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

    The session_id is the stable local_id and never changes.
    """
    try:
        async for event in pool.send(session_id, text, source_ws=ws):
            pass  # Events already broadcast by pool
    except asyncio.CancelledError:
        raise  # Let the task cancel propagate cleanly
    except Exception as e:
        await ws.send_bytes(orjson.dumps({
            "type": "error", "error": "send_failed",
            "detail": str(e),
        }))
    return session_id


async def _handle_compact(ws: WebSocket, pool: SessionPool, session_id: str) -> None:
    """Trigger conversation compaction, broadcasting events to all subscribers."""
    try:
        async for event in pool.compact(session_id):
            pass  # Events already broadcast by pool
    except asyncio.CancelledError:
        raise
    except Exception as e:
        await ws.send_bytes(orjson.dumps({
            "type": "error", "error": "compact_failed",
            "detail": str(e),
        }))


async def _handle_command(ws: WebSocket, sm: SessionManager, text: str) -> None:
    """Stream events from sm.command() to the WebSocket."""
    try:
        async for event in sm.command(text):
            payload = serialize_event(event)
            await ws.send_bytes(orjson.dumps(payload))
    except asyncio.CancelledError:
        raise
    except Exception as e:
        await ws.send_bytes(orjson.dumps({
            "type": "error", "error": "command_failed",
            "detail": str(e),
        }))
