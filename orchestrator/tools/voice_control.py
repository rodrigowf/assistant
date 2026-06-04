"""Voice control tools — let the voice assistant end its own session."""

from __future__ import annotations

import logging

from orchestrator.tools import registry

logger = logging.getLogger(__name__)


@registry.register(
    name="end_voice_session",
    description=(
        "End the current realtime voice conversation. Use this when the user asks to "
        "stop, hang up, end the call, or when the conversation is naturally complete "
        "and the user doesn't need anything else. After calling this tool the voice "
        "session will disconnect cleanly."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "farewell_message": {
                "type": "string",
                "description": "Optional short message to say before ending (e.g. 'Goodbye!'). "
                               "The assistant should speak this aloud before the tool is called.",
            },
        },
        "required": [],
    },
)
async def end_voice_session(context: dict, farewell_message: str = "") -> str:
    """End the current voice connection while keeping the orchestrator
    session alive in the pool for re-arming.

    Awaits :meth:`OrchestratorSession.end_voice` directly (no
    fire-and-forget, no sleep). The teardown closes the upstream
    provider WS, releases the audio recorder, clears the voice provider,
    and broadcasts ``voice_ending`` / ``voice_ended``.

    The orchestrator session itself stays in the pool. This is
    intentional: the tab — its JSONL, agent state, background work — is
    the durable thing; the voice connection is one ephemeral mode of
    interacting with it. When the user calls the wake word again, the
    route handler finds the same session and re-arms voice via
    ``restart_voice()`` on it (same JSONL, no resume dance, no fresh
    session).

    The farewell line is spoken by the model **before** this tool fires
    — the system prompt instructs the assistant to say goodbye and then
    call ``end_voice_session``.
    """
    pool = context.get("pool")
    if pool is None:
        return "Error: no session pool available."

    session = pool.get_orchestrator()
    if session is None:
        return "Voice session already ended."

    try:
        await session.end_voice("agent_end")
    except Exception as e:  # noqa: BLE001
        logger.exception("end_voice failed during agent-initiated stop")
        return f"Voice session ended with error: {e}"

    return "Voice session ended."
