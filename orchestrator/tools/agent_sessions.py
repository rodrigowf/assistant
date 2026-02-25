"""Agent session tools — control Claude Code instances from the orchestrator."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from orchestrator.tools import registry

logger = logging.getLogger(__name__)


def _get_mcp_configs(mcp_names: list[str]) -> dict[str, dict]:
    """Load MCP configurations for the given names from .claude.json.

    Returns a dict mapping MCP name to its configuration, only for
    names that exist in the config.
    """
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        config_path = Path(config_dir) / ".claude.json"
    else:
        project_root = Path(__file__).resolve().parent.parent.parent
        config_path = project_root / ".claude_config" / ".claude.json"

    if not config_path.is_file():
        logger.warning("Claude config not found at %s", config_path)
        return {}

    try:
        with open(config_path) as f:
            config = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error("Failed to load Claude config: %s", e)
        return {}

    # Get project-specific MCP servers
    project_root = Path(__file__).resolve().parent.parent.parent
    project_dir = str(project_root)
    projects = config.get("projects", {})
    project_config = projects.get(project_dir, {})
    available_mcps = project_config.get("mcpServers", {})

    # Return only the requested MCPs that exist
    result = {}
    for name in mcp_names:
        if name in available_mcps:
            result[name] = available_mcps[name]
        else:
            logger.warning("MCP server %r not found in config", name)

    return result


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


@registry.register(
    name="open_agent_session",
    description=(
        "Start a new Claude Code agent session or resume a past one from history. "
        "To resume a past session, pass its sdk_session_id (returned by list_agent_sessions "
        "or list_history). Omit all parameters to start a fresh session. "
        "You can specify which MCP servers to load for extended capabilities. "
        "Returns the session_id to use with send_to_agent_session and close_agent_session."
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
                    "Examples: ['obs'], ['youtube'], ['chrome-devtools', 'ubuntu-desktop-control']. "
                    "If not specified, the session starts with no MCPs (default Claude Code tools only)."
                ),
            },
        },
    },
)
async def open_agent_session(
    context: dict[str, Any],
    resume_sdk_id: str = "",
    mcp_servers: list[str] | None = None,
) -> str:
    from manager.config import ManagerConfig

    pool = context["pool"]
    store = context["store"]
    config = context.get("manager_config") or ManagerConfig.load()
    sdk_id = resume_sdk_id if resume_sdk_id else None

    # Validate that the session actually exists before trying to resume
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

    # Build MCP servers dict if names provided
    mcp_servers_config: dict[str, dict] | None = None
    if mcp_servers:
        mcp_servers_config = _get_mcp_configs(mcp_servers)
        if not mcp_servers_config:
            return json.dumps({
                "error": f"No valid MCP servers found from: {mcp_servers}. "
                "Check available MCPs in the system prompt."
            })

    try:
        local_id = await pool.create(config, resume_sdk_id=sdk_id, mcp_servers=mcp_servers_config)
    except Exception as e:
        return json.dumps({"error": f"Failed to start session: {e}"})

    result = {"session_id": local_id, "status": "started"}
    if mcp_servers_config:
        result["mcp_servers"] = list(mcp_servers_config.keys())
    return json.dumps(result)


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
    description="Read recent messages from a Claude Code session's history.",
    input_schema={
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "The session ID to read.",
            },
            "max_messages": {
                "type": "integer",
                "description": "Maximum number of messages to return (default: 20).",
            },
        },
        "required": ["session_id"],
    },
)
async def read_agent_session(
    context: dict[str, Any], session_id: str, max_messages: int = 20
) -> str:
    pool = context["pool"]
    store = context["store"]

    # session_id is the local_id; look up the SDK session ID for JSONL store
    sm = pool.get(session_id)
    sdk_id = sm.sdk_session_id if sm else session_id

    previews = store.get_preview(sdk_id, max_messages=max_messages)
    if not previews:
        return json.dumps({"error": f"No messages found for session {session_id}"})

    messages = []
    for p in previews:
        messages.append({
            "role": p.role,
            "text": p.text,
            "timestamp": p.timestamp.isoformat() if p.timestamp else None,
        })
    return json.dumps({"session_id": session_id, "messages": messages})


@registry.register(
    name="send_to_agent_session",
    description=(
        "Send a message to an active Claude Code agent session and wait for the response. "
        "Returns the agent's text response. Progress events are streamed to the orchestrator "
        "so the frontend stays updated during long-running agent tasks."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "The session ID to send to.",
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
    import asyncio
    from api.serializers import serialize_event
    from manager.types import TextComplete, TurnComplete, TextDelta, ToolUse, ToolResult

    pool = context["pool"]

    if not pool.has(session_id):
        return json.dumps({"error": f"No active session with ID {session_id}"})

    texts: list[str] = []
    cost = 0.0
    turns = 0

    async def _collect() -> None:
        nonlocal cost, turns
        async for event in pool.send(session_id, message):
            # Forward significant events to the orchestrator WebSocket as nested events
            # This ensures the frontend sees progress during long-running agent tasks
            if isinstance(event, (TextDelta, TextComplete, ToolUse, ToolResult)):
                try:
                    serialized = serialize_event(event)
                    await pool.broadcast_orchestrator({
                        "type": "nested_session_event",
                        "session_id": session_id,
                        "event_type": serialized.get("type", "unknown"),
                        "event_data": serialized,
                    })
                except Exception as e:
                    logger.debug("Failed to broadcast nested event: %s", e)

            if isinstance(event, TextComplete):
                texts.append(event.text)
            elif isinstance(event, TurnComplete):
                cost = event.cost or 0.0
                turns = event.num_turns

    try:
        # pool.send() acquires the per-session lock and broadcasts events
        # to all WebSocket subscribers (e.g., the frontend agent tab).
        # We also forward events to the orchestrator WebSocket as nested events
        # so the frontend can show progress during agent execution.
        # Timeout prevents the orchestrator from hanging indefinitely if
        # the SDK subprocess dies or the lock is held too long.
        await asyncio.wait_for(_collect(), timeout=300.0)
    except asyncio.TimeoutError:
        return json.dumps({
            "error": f"Timed out waiting for response from session {session_id}. "
            "The session may be busy with another request or unresponsive."
        })
    except Exception as e:
        return json.dumps({"error": f"Failed to send message: {e}"})

    return json.dumps({
        "session_id": session_id,
        "response": "\n".join(texts),
        "cost": cost,
        "turns": turns,
    })


@registry.register(
    name="interrupt_agent_session",
    description=(
        "Interrupt an actively executing Claude Code agent session. "
        "Use this to stop an agent that is running undesired actions or taking too long. "
        "The agent will stop processing immediately."
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
    name="list_history",
    description=(
        "List all past conversation sessions. Each session has a 'type' field: "
        "'agent' sessions can be resumed with open_agent_session, "
        "'orchestrator' sessions CANNOT be resumed as agent sessions."
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
