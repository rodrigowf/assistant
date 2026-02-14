"""Agent session tools — control Claude Code instances from the orchestrator."""

from __future__ import annotations

import json
import logging
from typing import Any

from orchestrator.tools import registry

logger = logging.getLogger(__name__)


@registry.register(
    name="list_agent_sessions",
    description="List all currently active Claude Code agent sessions with their status.",
    input_schema={
        "type": "object",
        "properties": {},
    },
)
async def list_agent_sessions(context: dict[str, Any]) -> str:
    pool = context["pool"]
    store = context["store"]
    sessions = pool.list_sessions()

    # Enrich with history data from the store (message count, title)
    for s in sessions:
        info = store.get_session_info(s["session_id"])
        if info:
            s["message_count"] = info.message_count
            s["title"] = info.title

    return json.dumps({"sessions": sessions, "count": len(sessions)})


@registry.register(
    name="open_agent_session",
    description=(
        "Start a new Claude Code agent session or resume an existing one. "
        "Returns the session_id of the opened session."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Optional session ID to resume. Omit to start a new session.",
            },
        },
    },
)
async def open_agent_session(context: dict[str, Any], session_id: str = "") -> str:
    from manager.config import ManagerConfig

    pool = context["pool"]
    config = context.get("manager_config") or ManagerConfig.load()
    resume_id = session_id if session_id else None

    try:
        new_id = await pool.create(config, session_id=resume_id)
    except Exception as e:
        return json.dumps({"error": f"Failed to start session: {e}"})

    return json.dumps({"session_id": new_id, "status": "started"})


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
    store = context["store"]
    previews = store.get_preview(session_id, max_messages=max_messages)
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
        "Returns the agent's text response."
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
    from manager.types import TextComplete, TurnComplete

    pool = context["pool"]

    if not pool.has(session_id):
        return json.dumps({"error": f"No active session with ID {session_id}"})

    texts: list[str] = []
    cost = 0.0
    turns = 0

    try:
        # pool.send() acquires the per-session lock and broadcasts events
        # to all WebSocket subscribers (e.g., the frontend agent tab)
        async for event in pool.send(session_id, message):
            if isinstance(event, TextComplete):
                texts.append(event.text)
            elif isinstance(event, TurnComplete):
                cost = event.cost or 0.0
                turns = event.num_turns
                # Session ID may change after first query (SDK re-key)
                if event.session_id:
                    session_id = event.session_id
    except Exception as e:
        return json.dumps({"error": f"Failed to send message: {e}"})

    return json.dumps({
        "session_id": session_id,
        "response": "\n".join(texts),
        "cost": cost,
        "turns": turns,
    })


@registry.register(
    name="list_history",
    description="List all past conversation sessions (both regular and orchestrator).",
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
        })
    return json.dumps({"sessions": result, "total": len(sessions)})
