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
    """End the current voice session through the canonical teardown path.

    Awaits :meth:`OrchestratorSession.end_voice` directly (no
    fire-and-forget, no sleep). The previous implementation slept 1.5s
    "to let the farewell broadcast through" — but the broadcast and the
    tear-down were both fire-and-forget, so a WS drop in the sleep
    window left the upstream relay running for an unbounded time while
    the frontend already showed "off". The state machine inside
    ``end_voice`` now handles the broadcast ordering: ``voice_ending``
    fires before the relay closes, ``voice_ended`` fires after.

    The farewell line itself is spoken by the model **before** this tool
    fires — the system prompt instructs the assistant to say goodbye and
    then call ``end_voice_session``. No sleep is needed.
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

    # After end_voice the session object is still in the pool but its
    # voice provider/recorder are gone. Drop it so a follow-up
    # voice_start for the same local_id creates a fresh session
    # instead of reattaching to the husk.
    try:
        await pool.stop_orchestrator()
    except Exception:  # noqa: BLE001
        logger.exception("pool.stop_orchestrator failed after end_voice")

    return "Voice session ended."
