"""OpenAI Realtime Voice provider for the orchestrator agent.

This provider does NOT call the model directly. Instead, the frontend establishes
a WebRTC connection to OpenAI Realtime API and mirrors all data channel events
to the backend via the orchestrator WebSocket. Those events are injected here
via inject_event() and translated into the standard OrchestratorEvent stream.

Architecture:
    Frontend (WebRTC data channel) → orchestrator WS → inject_event() → create_message()
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from orchestrator.types import (
    ErrorEvent,
    OrchestratorEvent,
    TextDelta,
    TextComplete,
    ToolUseStart,
    TurnComplete,
    VoiceInterrupted,
)

logger = logging.getLogger(__name__)

# Model and voice constants
VOICE_MODEL = "gpt-realtime"
VOICE_NAME = "cedar"


class OpenAIVoiceProvider:
    """Event-queue based provider that processes mirrored OpenAI Realtime events.

    Usage::

        provider = OpenAIVoiceProvider()

        # In WebSocket handler, when a voice_event arrives:
        await provider.inject_event(event_dict)

        # In agent loop, iterate over translated events:
        async for orchestrator_event in provider.create_message(messages, tools, system):
            ...
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        # Track the current partial transcript for interruption handling
        self._current_transcript: str = ""
        # Map call_id → tool_name for active tool calls
        self._pending_calls: dict[str, str] = {}
        # Accumulate function call arguments per call_id
        self._pending_args: dict[str, str] = {}

    async def inject_event(self, event: dict[str, Any]) -> None:
        """Inject an OpenAI Realtime event from the frontend mirror."""
        await self._queue.put(event)

    @property
    def pending_calls(self) -> dict[str, str]:
        """Map of call_id → tool_name for calls awaiting results."""
        return self._pending_calls

    async def create_message(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
    ) -> AsyncIterator[OrchestratorEvent]:
        """Process queued realtime events and yield OrchestratorEvents.

        Runs until response.done or an error event is received.
        """
        self._current_transcript = ""

        while True:
            try:
                # Use a timeout so we don't block forever if connection drops
                event = await asyncio.wait_for(self._queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                yield ErrorEvent(error="voice_timeout", detail="No event received within 30s")
                return

            event_type = event.get("type", "")
            translated = self._translate(event)

            if translated is not None:
                yield translated

            # Track transcript for interruption context
            if event_type == "response.audio_transcript.delta":
                self._current_transcript += event.get("delta", "")

            # End of turn
            if event_type == "response.done":
                self._current_transcript = ""
                return

            if event_type == "error":
                return

    def _translate(self, event: dict[str, Any]) -> OrchestratorEvent | None:
        """Translate a single OpenAI Realtime event to an OrchestratorEvent."""
        event_type = event.get("type", "")

        # Streaming transcript tokens → TextDelta
        if event_type == "response.audio_transcript.delta":
            text = event.get("delta", "")
            if text:
                return TextDelta(text=text)

        # Complete transcript → TextComplete
        elif event_type == "response.audio_transcript.done":
            text = event.get("transcript", "")
            return TextComplete(text=text)

        # Tool call: name arrives in response.output_item.added for function items
        elif event_type == "response.output_item.added":
            item = event.get("item", {})
            if item.get("type") == "function_call":
                call_id = item.get("call_id", "")
                name = item.get("name", "")
                if call_id and name:
                    self._pending_calls[call_id] = name
                    self._pending_args[call_id] = ""

        # Accumulate function call arguments
        elif event_type == "response.function_call_arguments.delta":
            call_id = event.get("call_id", "")
            if call_id in self._pending_args:
                self._pending_args[call_id] += event.get("delta", "")

        # Tool call ready to execute
        elif event_type == "response.function_call_arguments.done":
            call_id = event.get("call_id", "")
            args_str = event.get("arguments", self._pending_args.get(call_id, "{}"))
            name = self._pending_calls.get(call_id, event.get("name", ""))

            try:
                import json
                tool_input = json.loads(args_str) if args_str else {}
            except Exception:
                tool_input = {}

            if call_id and name:
                return ToolUseStart(
                    tool_call_id=call_id,
                    tool_name=name,
                    tool_input=tool_input,
                )

        # Turn complete
        elif event_type == "response.done":
            usage = event.get("response", {}).get("usage", {})
            return TurnComplete(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
            )

        # User barge-in (server VAD detected speech during assistant output)
        elif event_type == "input_audio_buffer.speech_started":
            partial = self._current_transcript
            self._current_transcript = ""
            return VoiceInterrupted(partial_text=partial)

        # Error from OpenAI
        elif event_type == "error":
            err = event.get("error", {})
            return ErrorEvent(
                error=err.get("code", "openai_error"),
                detail=err.get("message", str(err)),
            )

        return None

    def get_session_update_payload(
        self,
        system: str,
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Build the session.update payload to configure OpenAI Realtime session.

        This is sent by the backend to the frontend, which forwards it to OpenAI
        via the WebRTC data channel.
        """
        return {
            "type": "session.update",
            "session": {
                "model": VOICE_MODEL,
                "voice": VOICE_NAME,
                "instructions": system,
                "tools": tools,
                "tool_choice": "auto",
                "modalities": ["text", "audio"],
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.5,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 800,
                },
                "input_audio_transcription": {
                    "model": "whisper-1",
                },
            },
        }
