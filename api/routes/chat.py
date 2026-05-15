"""WebSocket chat endpoint — real-time streaming via SessionManager.

Architecture: WebSockets are pure observers.  Sending a prompt spawns a
session-owned task in the pool (``pool.start_turn``) that drives the turn
to completion regardless of whether the originating WS stays connected.
A page reload merely unsubscribes from broadcasts; the next ``start``
message re-subscribes and the in-flight events flow naturally to the new
WS.  Explicit cancellation (user clicks Interrupt, or sends a new prompt
mid-turn) goes through ``pool.cancel_turn`` which sends the SDK
interrupt and awaits clean unwind.

Slash commands (``/help``, ``/compact``) intentionally bypass this model
— their output is short, single-WS, and rarely worth resuming after a
disconnect.  See ``_handle_command``.
"""

from __future__ import annotations

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
                if sm is None or session_id is None:
                    await ws.send_bytes(orjson.dumps({
                        "type": "error", "error": "not_started",
                        "detail": "Send a 'start' message first",
                    }))
                    continue
                # If a permission is pending on this session, treat the user's
                # chat as a denial with their prose as the rejection reason.
                # See the conversational-checkpoint policy in
                # manager.session._PERMISSION_GATING_PROMPT.
                text_payload = msg.get("text", "")
                if text_payload:
                    pending_ids = list(sm.pending_permission_ids())
                    for rid in pending_ids:
                        await pool.resolve_session_permission(
                            session_id,
                            rid,
                            "deny",
                            message=text_payload,
                            responder="user",
                        )
                # start_turn handles the "interrupt + new" semantics
                # internally (cancels any in-flight turn first), so we
                # don't need to call cancel_turn here.
                try:
                    await pool.start_turn(session_id, text_payload, source_ws=ws)
                except Exception as e:
                    logger.exception("start_turn failed for session %s", session_id)
                    await ws.send_bytes(orjson.dumps({
                        "type": "error", "error": "send_failed",
                        "detail": str(e),
                    }))

            elif msg_type == "command":
                if sm is None:
                    await ws.send_bytes(orjson.dumps({
                        "type": "error", "error": "not_started",
                    }))
                    continue
                # Slash commands bypass the session-owned turn model
                # deliberately — their output is single-WS by design.
                # If a chat turn is in flight, interrupt it first so the
                # SDK is free to accept the slash command.
                if session_id:
                    await pool.cancel_turn(session_id)
                await _handle_command(ws, sm, msg.get("text", ""))

            elif msg_type == "interrupt":
                if session_id is not None:
                    await pool.cancel_turn(session_id)
                    await ws.send_bytes(orjson.dumps({
                        "type": "status", "status": "interrupted",
                    }))

            elif msg_type == "compact":
                if sm is None or session_id is None:
                    await ws.send_bytes(orjson.dumps({
                        "type": "error", "error": "not_started",
                    }))
                    continue
                await pool.cancel_turn(session_id)
                await _handle_compact(ws, pool, session_id)

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
                # User explicitly detaches from this session.  Do NOT cancel
                # the in-flight turn — the user said "stop watching", not
                # "stop the agent".  Other tabs may still be observing; if
                # not, the session keeps thinking until completion or
                # explicit user delete.
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
        # Page reload, network blip, browser close — all hit this path.
        # Just unsubscribe.  The in-flight turn (if any) keeps running
        # under pool ownership and the next WS that subscribes will pick
        # up the broadcast stream.  Orphaned subprocesses are handled
        # separately by the pool's orphan reaper, not here.
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
    import asyncio

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

    # Apply the configured provider (claude | qwen).  ManagerConfig.load()
    # already honors ASSISTANT_PROVIDER env var; this overlays the UI-saved
    # value from assistant_config.json (which is what users actually edit).
    provider = assistant_cfg.get("provider")
    if provider:
        config = replace(config, provider=str(provider).lower())

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


async def _handle_compact(ws: WebSocket, pool: SessionPool, session_id: str) -> None:
    """Trigger conversation compaction, broadcasting events to all subscribers.

    Compaction is short and uses pool.compact()'s built-in broadcast, so the
    iteration runs inline here rather than as a session-owned task.  If a
    user reloads the page mid-compact, the operation completes but the new
    WS won't see the events — acceptable for an operation that's typically
    sub-second and idempotent (re-issuing /compact is safe).
    """
    try:
        async for _event in pool.compact(session_id):
            pass  # Events already broadcast by pool
    except Exception as e:
        logger.exception("compact failed for session %s", session_id)
        await ws.send_bytes(orjson.dumps({
            "type": "error", "error": "compact_failed",
            "detail": str(e),
        }))


async def _handle_command(ws: WebSocket, sm: SessionManager, text: str) -> None:
    """Stream events from sm.command() to the WebSocket.

    Slash commands are a single-WS path: the user who issued ``/help`` sees
    the help text, not other observers.  Output goes directly to ``ws`` via
    serialize_event rather than through pool broadcast.
    """
    try:
        async for event in sm.command(text):
            payload = serialize_event(event)
            await ws.send_bytes(orjson.dumps(payload))
    except Exception as e:
        logger.exception("command failed")
        await ws.send_bytes(orjson.dumps({
            "type": "error", "error": "command_failed",
            "detail": str(e),
        }))
