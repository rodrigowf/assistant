"""Agent session tools — control Claude Code instances from the orchestrator."""

from __future__ import annotations

import json
import logging
from typing import Any

from orchestrator.tools import registry

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


@registry.register(
    name="open_agent_session",
    description=(
        "Start a new Claude Code agent session or resume a past one from history. "
        "To resume a past session, pass its sdk_session_id (returned by list_agent_sessions "
        "or list_history). Omit all parameters to start a fresh session. "
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
        },
    },
)
async def open_agent_session(context: dict[str, Any], resume_sdk_id: str = "") -> str:
    from manager.config import ManagerConfig

    pool = context["pool"]
    config = context.get("manager_config") or ManagerConfig.load()
    sdk_id = resume_sdk_id if resume_sdk_id else None

    try:
        local_id = await pool.create(config, resume_sdk_id=sdk_id)
    except Exception as e:
        return json.dumps({"error": f"Failed to start session: {e}"})

    return json.dumps({"session_id": local_id, "status": "started"})


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
        # to all WebSocket subscribers (e.g., the frontend agent tab).
        # session_id is the stable local_id — it never changes.
        async for event in pool.send(session_id, message):
            if isinstance(event, TextComplete):
                texts.append(event.text)
            elif isinstance(event, TurnComplete):
                cost = event.cost or 0.0
                turns = event.num_turns
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
