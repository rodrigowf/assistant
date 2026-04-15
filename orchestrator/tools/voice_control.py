"""Voice control tools — let the voice assistant end its own session."""

from __future__ import annotations

import asyncio
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
    """Signal the orchestrator to cleanly end the voice session."""
    pool = context.get("pool")
    if pool is None:
        return "Error: no session pool available."

    async def _stop():
        # Small delay so the tool result + any final response.create can be
        # forwarded to OpenAI before we tear down the WebRTC connection.
        await asyncio.sleep(1.5)
        await pool.broadcast_orchestrator({"type": "voice_stopped"})
        await pool.stop_orchestrator()

    asyncio.create_task(_stop(), name="end-voice-session")
    return "Voice session ending."
