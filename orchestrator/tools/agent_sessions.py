"""Agent session tools — control Claude Code instances from the orchestrator."""

from __future__ import annotations

import json
import logging
from typing import Any

from orchestrator.tools import registry
from utils.mcp_config import get_mcp_configs, load_available_mcps

logger = logging.getLogger(__name__)


@registry.register(
    name="list_agent_sessions",
    description=(
        "List all currently active Claude Code agent sessions with their status. "
        "Each session has a session_id (use with send_to_agent_session/close_agent_session) "
        "and a sdk_session_id (use with open_agent_session to resume after closing)."
    ),
    input_schema={
        "type": "object",
        "properties": {},
    },
)
async def list_agent_sessions(context: dict[str, Any]) -> str:
    pool = context["pool"]
    store = context["store"]
    sessions = pool.list_sessions()

    # Enrich with history data from the store (message count, title).
    # The store uses SDK session IDs (JSONL filenames).
    for s in sessions:
        sdk_id = s.get("sdk_session_id")
        if sdk_id:
            info = store.get_session_info(sdk_id)
            if info:
                s["message_count"] = info.message_count
                s["title"] = info.title

    return json.dumps({"sessions": sessions, "count": len(sessions)})


def _open_agent_session_schema(static: dict[str, Any]) -> dict[str, Any]:
    """Inject the live MCP list as an ``enum`` on the ``mcp_servers`` items.

    Both Anthropic and Gemini (via ``functionDeclarations``) honour
    ``enum``: passing an unknown value is rejected before the call lands,
    which prevents the hallucinated-MCP-name failure mode where the model
    confidently invokes ``open_agent_session({"mcp_servers": ["google-banana"]})``
    against a project that has no such server. We also rewrite the human
    description to list what's actually available rather than hardcoding
    examples from other deployments.
    """
    import copy

    available = sorted(load_available_mcps().keys())
    schema = copy.deepcopy(static)
    mcp_prop = schema["properties"]["mcp_servers"]
    if available:
        mcp_prop["items"] = {"type": "string", "enum": available}
        mcp_prop["description"] = (
            "Names of MCP servers to enable for this session. "
            f"Valid names: {available}. "
            "Omit or pass [] to start with default Claude Code tools only."
        )
    else:
        # No MCPs configured — keep the field but make the constraint
        # explicit so the model doesn't invent values.
        mcp_prop["items"] = {"type": "string", "enum": []}
        mcp_prop["description"] = (
            "No MCP servers are configured for this project. "
            "Omit this field or pass []."
        )
    return schema


@registry.register(
    name="open_agent_session",
    description=(
        "Start a new Claude Code agent session OR re-open a past one from history. "
        "This is the single entry point for both: omit all parameters to start fresh, "
        "or pass resume_sdk_id with any sdk_session_id from list_history / "
        "list_agent_sessions to resume that exact conversation (full context restored). "
        "You can specify which MCP servers to load for extended capabilities — "
        "valid names are listed in the system prompt's 'Available MCPs' section. "
        "Returns the session_id; you MUST use that exact session_id in the very "
        "next send_to_agent_session call rather than reusing an older one."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "resume_sdk_id": {
                "type": "string",
                "description": (
                    "The sdk_session_id of a past session to resume from history. "
                    "This is the Claude SDK session ID, NOT the session_id returned by "
                    "open_agent_session. Get it from list_agent_sessions or list_history."
                ),
            },
            "mcp_servers": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of MCP server names to enable for this session. "
                    "See the 'Available MCPs' section of the system prompt for valid names. "
                    "Omit or pass [] for default Claude Code tools only."
                ),
            },
        },
    },
    schema_builder=_open_agent_session_schema,
)
async def open_agent_session(
    context: dict[str, Any],
    resume_sdk_id: str = "",
    mcp_servers: list[str] | None = None,
) -> str:
    """Spawn an agent session honouring the live global config.

    Working directory (local or SSH), session-harness, harness model, and
    chrome flag all come from ``assistant_config.json`` via
    :func:`api.session_factory.build_session_config` — the same path the
    UI's "+" button uses.  ``mcp_servers`` is the only per-call override:
    when ``None``, the factory falls back to the global ``enabled_mcps``;
    when a list (possibly empty), it replaces the inherited value
    verbatim, matching the per-session config the UI exposes via the gear
    panel.
    """
    from api.session_factory import build_session_config

    pool = context["pool"]
    store = context["store"]
    sdk_id = resume_sdk_id if resume_sdk_id else None

    # Validate that the session actually exists before trying to resume.
    if sdk_id:
        session_info = store.get_session_info(sdk_id)
        if session_info is None:
            return json.dumps({
                "error": f"Session {sdk_id!r} not found in history. "
                "Use list_history to see available sessions."
            })
        if session_info.is_orchestrator:
            return json.dumps({
                "error": f"Session {sdk_id!r} is an orchestrator session and cannot "
                "be resumed as an agent session. Only agent sessions (type='agent') "
                "from list_history can be resumed."
            })

    # Eagerly validate any explicit MCP names against the live catalogue
    # so the model gets a precise error before pool.create is invoked.
    # The factory drops unknown names silently; here we *want* the loud
    # signal because the user-visible failure mode is "the model invented
    # a name and the tool reported success on an empty MCP set".
    if mcp_servers:
        resolved = get_mcp_configs(mcp_servers)
        missing = [n for n in mcp_servers if n not in resolved]
        if missing:
            available = sorted(load_available_mcps().keys())
            return json.dumps({
                "error": (
                    f"Unknown MCP servers: {missing}. "
                    f"Available: {available or '(none configured)'}."
                )
            })

    try:
        config, resolved_mcps, info = build_session_config(
            resume_sdk_id=sdk_id,
            mcp_override=mcp_servers,
        )
    except Exception as e:
        return json.dumps({"error": f"Failed to build session config: {e}"})

    try:
        local_id = await pool.create(
            config, resume_sdk_id=sdk_id, mcp_servers=resolved_mcps,
        )
    except Exception as e:
        return json.dumps({"error": f"Failed to start session: {e}"})

    # Mirror chat.py's persist-on-detect behaviour so a legacy resumed
    # session gets its provider pinned for future deterministic resumes.
    if sdk_id and info.get("persist_provider"):
        from api.routes.session_config import save_session_config
        try:
            save_session_config(sdk_id, {"provider": info["persist_provider"]})
        except Exception as e:
            logger.warning(
                "Failed to persist resolved provider for session %s: %s",
                sdk_id, e,
            )

    # Surface the resolved config back to the model so it can see what
    # global settings landed (working dir, provider, MCPs, ssh target).
    # Without this the model has no way to verify that its expectations
    # match reality without calling get_assistant_config separately.
    return json.dumps({
        "session_id": local_id,
        "status": "started",
        "resolved_config": {
            "working_directory": info["working_directory"],
            "project_dir": info["project_dir"],
            "ssh_host": info["ssh_host"],
            "provider": info["provider"],
            "model": info["model"],
            "chrome_extension": info["chrome_extension"],
            "mcp_servers": info["mcp_servers"],
        },
    })


@registry.register(
    name="close_agent_session",
    description="Close an active Claude Code agent session.",
    input_schema={
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "The session ID to close.",
            },
        },
        "required": ["session_id"],
    },
)
async def close_agent_session(context: dict[str, Any], session_id: str) -> str:
    pool = context["pool"]

    if not pool.has(session_id):
        return json.dumps({"error": f"No active session with ID {session_id}"})

    try:
        await pool.close(session_id)
    except Exception as e:
        logger.warning("Error closing session %s: %s", session_id, e)

    return json.dumps({"session_id": session_id, "status": "closed"})


@registry.register(
    name="read_agent_session",
    description=(
        "Read a Claude Code session's conversation, returning the last N messages "
        "(default 5; pass max_messages=null for the full transcript). The response "
        "also tells you whether a turn is actively running on this session: "
        "'live.status' is 'running' (a turn is in flight — events shows the live "
        "tail of text/tool_use/permission events) or 'idle' (no turn running — "
        "messages is the canonical record). Use this single tool whether you want "
        "the finished output of a past turn or a progress check on an in-flight one."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "The session ID to read.",
            },
            "max_messages": {
                "type": ["integer", "null"],
                "description": (
                    "How many of the most recent persisted messages to return "
                    "(default: 5). Use null to return the entire conversation."
                ),
            },
        },
        "required": ["session_id"],
    },
)
async def read_agent_session(
    context: dict[str, Any],
    session_id: str,
    max_messages: int | None = 5,
) -> str:
    pool = context["pool"]
    store = context["store"]
    runner = context.get("runner")

    # session_id is the local_id; look up the SDK session ID for JSONL store
    sm = pool.get(session_id)
    sdk_id = sm.sdk_session_id if sm else session_id

    previews = store.get_preview(sdk_id, max_messages=max_messages)
    messages = [
        {
            "role": p.role,
            "text": p.text,
            "timestamp": p.timestamp.isoformat() if p.timestamp else None,
        }
        for p in previews
    ]

    # Live status: is a background turn currently driving this session?
    # The runner's ring buffer is the single source of truth for in-flight
    # state — pool membership alone only means the session is open.
    live: dict[str, Any] = {"status": "idle"}
    if runner is not None:
        snapshot = runner.peek(session_id)
        if snapshot.get("status") == "running" and not snapshot.get("finished", True):
            live = {
                "status": "running",
                "turn_id": snapshot.get("turn_id"),
                "last_assistant_text": snapshot.get("last_assistant_text", ""),
                "events": snapshot.get("events", []),
            }

    if not messages and live["status"] == "idle":
        return json.dumps({
            "error": f"No messages found for session {session_id}",
            "live": live,
        })

    return json.dumps({
        "session_id": session_id,
        "messages": messages,
        "live": live,
    })


@registry.register(
    name="send_to_agent_session",
    description=(
        "Send a message to an active Claude Code agent session. RETURNS IMMEDIATELY "
        "with a turn_id; the agent runs in the background. The result will arrive "
        "as a structured background event ('[SESSION xxx event: turn finished]') at "
        "the start of your next turn — do NOT loop calling this tool waiting for a "
        "response. While the turn is in flight you can: spawn other agents, check "
        "progress or read persisted output via read_agent_session, answer "
        "permission requests via respond_to_agent_permission, or simply respond "
        "to the user. Use interrupt_agent_session to stop a turn early."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": (
                    "The session ID to send to. When you just called "
                    "open_agent_session, use the exact session_id it returned — "
                    "do NOT substitute a session_id from earlier in the conversation."
                ),
            },
            "message": {
                "type": "string",
                "description": "The message to send to the agent.",
            },
        },
        "required": ["session_id", "message"],
    },
)
async def send_to_agent_session(
    context: dict[str, Any], session_id: str, message: str
) -> str:
    pool = context["pool"]
    runner = context.get("runner")
    if runner is None:
        # Fallback safety: shouldn't happen — OrchestratorSession injects runner
        # into context at __init__.  If it's missing we have a wiring bug.
        return json.dumps({
            "error": "BackgroundAgentRunner not available in context — orchestrator init bug",
        })

    if not pool.has(session_id):
        return json.dumps({"error": f"No active session with ID {session_id}"})

    try:
        handle = await runner.spawn(session_id, message)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    return json.dumps({
        "turn_id": handle.turn_id,
        "session_id": session_id,
        "session_title": handle.session_title,
        "status": "running",
        "started_at": handle.started_at,
        "hint": (
            "Result arrives as a background event next turn. "
            "Call read_agent_session(session_id) at any time — "
            "it returns persisted messages plus 'live.status' (running/idle) for in-flight progress."
        ),
    })


@registry.register(
    name="interrupt_agent_session",
    description=(
        "Interrupt an actively executing Claude Code agent session. "
        "Use this to stop an agent that is running undesired actions or taking "
        "too long.  The agent will stop processing immediately, and any "
        "fire-and-forget turn driven by send_to_agent_session will produce a "
        "'cancelled' background event next turn."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "The session ID to interrupt.",
            },
        },
        "required": ["session_id"],
    },
)
async def interrupt_agent_session(context: dict[str, Any], session_id: str) -> str:
    pool = context["pool"]

    if not pool.has(session_id):
        return json.dumps({"error": f"No active session with ID {session_id}"})

    try:
        await pool.interrupt(session_id)
    except Exception as e:
        logger.warning("Error interrupting session %s: %s", session_id, e)
        return json.dumps({"error": f"Failed to interrupt session: {e}"})

    return json.dumps({"session_id": session_id, "status": "interrupted"})


@registry.register(
    name="respond_to_agent_permission",
    description=(
        "Approve or deny a pending permission request from an agent session. "
        "When an agent calls a gated tool (currently only ExitPlanMode), the SDK "
        "pauses; the user gets a per-tab modal in the agent's tab and the "
        "orchestrator gets a 'permission_request' nested event with the request_id. "
        "Typically the user answers in their tab — only intervene if the user is "
        "unavailable, you have specific reason to overrule, or the agent has "
        "asked you in chat to decide.  First answer wins; the loser's call is a "
        "no-op (this includes when the user clicks the modal before you respond)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "The agent session that issued the permission request.",
            },
            "request_id": {
                "type": "string",
                "description": "The request_id from the permission_request event.",
            },
            "decision": {
                "type": "string",
                "enum": ["allow", "deny"],
                "description": "'allow' to let the tool run, 'deny' to block it.",
            },
            "message": {
                "type": "string",
                "description": (
                    "Optional message returned to the agent. For 'deny' it "
                    "becomes the rejection reason; for 'allow' it's purely "
                    "informational and recorded in the conversation."
                ),
            },
        },
        "required": ["session_id", "request_id", "decision"],
    },
)
async def respond_to_agent_permission(
    context: dict[str, Any],
    session_id: str,
    request_id: str,
    decision: str,
    message: str | None = None,
) -> str:
    pool = context["pool"]
    if decision not in ("allow", "deny"):
        return json.dumps({"error": "decision must be 'allow' or 'deny'"})
    won = await pool.resolve_session_permission(
        session_id,
        request_id,
        decision,
        message=message,
        responder="orchestrator",
    )
    if not won:
        return json.dumps({
            "session_id": session_id,
            "request_id": request_id,
            "result": "no_op",
            "detail": "request was already answered or no longer exists",
        })
    return json.dumps({
        "session_id": session_id,
        "request_id": request_id,
        "decision": decision,
        "result": "ok",
    })


@registry.register(
    name="list_history",
    description=(
        "List all past conversation sessions (closed agents from history). "
        "Each entry has a 'session_id' (the sdk_session_id) and a 'type': "
        "pass any 'agent' entry's session_id to open_agent_session(resume_sdk_id=...) "
        "to re-open and continue that exact conversation with full context. "
        "'orchestrator' sessions CANNOT be re-opened as agent sessions."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Maximum number of sessions to return (default: 20).",
            },
        },
    },
)
async def list_history(context: dict[str, Any], limit: int = 20) -> str:
    store = context["store"]
    sessions = store.list_sessions()[:limit]
    result = []
    for s in sessions:
        result.append({
            "session_id": s.session_id,
            "title": s.title,
            "message_count": s.message_count,
            "last_activity": s.last_activity.isoformat(),
            "type": "orchestrator" if s.is_orchestrator else "agent",
        })
    return json.dumps({"sessions": result, "total": len(sessions)})
