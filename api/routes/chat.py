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

    async def _cancel_stream() -> bool:
        """Cancel the active stream task; return True if there was one to cancel."""
        nonlocal stream_task
        had_task = stream_task is not None and not stream_task.done()
        if had_task:
            stream_task.cancel()
            try:
                await stream_task
            except (asyncio.CancelledError, Exception):
                pass
        stream_task = None
        return had_task

    async def _interrupt_if_orphaned() -> None:
        """If this WS was the last subscriber and a turn was in flight, send the
        SDK a real interrupt so the bundled `claude` subprocess doesn't sit
        burning CPU waiting for events nobody will consume.

        Without this, a browser disconnect mid-turn (refresh, sleep, network
        blip) leaves the SDK hung — it had been streaming events to a now-dead
        consumer and never gets the signal that the turn should end.  Only
        interrupts when subscriber_count drops to zero, so other tabs watching
        the same session aren't disrupted.
        """
        if session_id and pool.subscriber_count(session_id) == 0:
            try:
                await pool.interrupt(session_id)
            except Exception:
                logger.exception("Failed to interrupt orphaned session %s", session_id)

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
                # If a permission is pending on this session, treat the user's
                # chat as a denial with their prose as the rejection reason.
                # The agent receives this as a permission_resolved with
                # responder=user, message=<chat text>; per the conversational-
                # checkpoint policy in the agent's appended system prompt, it
                # then refines its approach.  The modal in the UI closes via
                # the broadcast.  Users who actually want to approve click
                # the Approve button — the modal is the formal yes/no path.
                text_payload = msg.get("text", "")
                if session_id and text_payload:
                    pending_ids = list(sm.pending_permission_ids())
                    for rid in pending_ids:
                        await pool.resolve_session_permission(
                            session_id,
                            rid,
                            "deny",
                            message=text_payload,
                            responder="user",
                        )
                # Cancel any in-progress stream before starting a new one
                await _cancel_stream()
                stream_task = asyncio.create_task(
                    _handle_send(ws, sm, pool, session_id, text_payload)
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

            elif msg_type == "permission_response":
                # Reply to a pending can_use_tool request.  Don't gate on
                # session_id matching `sm` here — the WS still receives the
                # `permission_resolved` broadcast that closes the modal even
                # if this call lost the race to the orchestrator.
                target_id = msg.get("session_id") or session_id
                request_id = msg.get("request_id")
                decision = msg.get("decision")
                if not target_id or not request_id or decision not in ("allow", "deny"):
                    await ws.send_bytes(orjson.dumps({
                        "type": "error", "error": "invalid_permission_response",
                    }))
                    continue
                await pool.resolve_session_permission(
                    target_id,
                    request_id,
                    decision,
                    message=msg.get("message"),
                    responder="user",
                )

            elif msg_type == "stop":
                had_stream = await _cancel_stream()
                if session_id:
                    pool.unsubscribe(session_id, ws)
                    if had_stream:
                        await _interrupt_if_orphaned()
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
        had_stream = await _cancel_stream()
        if session_id:
            pool.unsubscribe(session_id, ws)
            if had_stream:
                await _interrupt_if_orphaned()


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
    assistant_cfg = _load_assistant_config()
    from dataclasses import replace

    # Apply per-session config overrides (session config takes precedence over global)
    from api.routes.session_config import load_session_config
    session_cfg = load_session_config(resume_sdk_id) if resume_sdk_id else {}

    # Resolve working directory: session-specific entry id → global active → fallback
    session_wd_id = session_cfg.get("working_directory")
    if session_wd_id:
        # Find the entry in global history by id
        history = assistant_cfg.get("working_directory_history", [])
        active_entry = next((e for e in history if e["id"] == session_wd_id), None)
    else:
        active_entry = _find_active_entry(assistant_cfg)

    if active_entry:
        config = replace(
            config,
            project_dir=active_entry["path"],
            ssh_host=active_entry.get("ssh_host") or None,
            ssh_user=active_entry.get("ssh_user") or None,
            ssh_key=active_entry.get("ssh_key") or None,
            ssh_claude_config_dir=active_entry.get("claude_config_dir") or None,
        )
    else:
        config = replace(config, project_dir=assistant_cfg.get("working_directory", config.project_dir))

    # If no per-session MCPs provided, use session-level or global enabled MCPs.
    # An empty list in enabled_mcps means "no MCPs" (opt-in); None means "use defaults".
    if mcp_servers is None:
        # Session config takes precedence over global config (None = inherit)
        raw_mcps = session_cfg.get("enabled_mcps")
        if raw_mcps is None:
            raw_mcps = assistant_cfg.get("enabled_mcps", [])
        enabled_mcps: list[str] = raw_mcps or []
        if enabled_mcps:
            # Load full MCP configs from .claude.json and filter to enabled ones
            from api.routes.mcp import _load_mcp_servers
            all_mcps = _load_mcp_servers()
            mcp_servers = {k: v for k, v in all_mcps.items() if k in enabled_mcps} or None
    # Apply chrome extension flag if enabled (session config overrides global)
    chrome = session_cfg.get("chrome_extension")
    if chrome is None:
        chrome = assistant_cfg.get("chrome_extension", False)
    if chrome:
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
    from manager.session import SessionAbandoned

    async def _stream_once() -> None:
        async for _event in pool.send(session_id, text, source_ws=ws):
            pass  # Events already broadcast by pool

    try:
        try:
            await _stream_once()
        except SessionAbandoned as exc:
            # First attempt: the upstream request never produced any events.
            # Interrupt the wedged turn and retry exactly once.
            logger.warning("Turn abandoned for session %s after %.0fs; retrying once", session_id, exc.elapsed_seconds)
            await ws.send_bytes(orjson.dumps({
                "type": "status", "status": "retrying",
                "detail": f"upstream silent for {exc.elapsed_seconds:.0f}s, retrying",
            }))
            try:
                await pool.interrupt(session_id)
            except Exception:
                logger.exception("Failed to interrupt abandoned turn for %s", session_id)
            await asyncio.sleep(1.0)
            await _stream_once()
    except asyncio.CancelledError:
        raise  # Let the task cancel propagate cleanly
    except SessionAbandoned as exc:
        # Retry also gave up — surface the failure so the user sees something.
        await ws.send_bytes(orjson.dumps({
            "type": "error", "error": "upstream_wedged",
            "detail": f"Anthropic upstream did not respond after retry ({exc.elapsed_seconds:.0f}s). Try again in a moment.",
        }))
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
