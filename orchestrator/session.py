"""Orchestrator session — wraps OrchestratorAgent with JSONL persistence.

Supports three modes:
- Text mode (default): Uses configurable provider (Anthropic/OpenAI)
- Audio mode: Uses OpenAI for multimodal audio input
- Voice mode: Uses OpenAI Realtime for WebRTC streaming

Text and audio modes support runtime model switching. Voice mode uses a
fixed model for the session duration (WebRTC constraint).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.paths import get_sessions_dir

from orchestrator.agent import OrchestratorAgent
from orchestrator.audio_utils import convert_audio_to_wav
from orchestrator.config import (
    AVAILABLE_MODELS,
    OrchestratorConfig,
    Provider,
    get_model_info,
)
from orchestrator.persistence import HistoryLoader, HistoryWriter
from orchestrator.providers.anthropic import AnthropicProvider
from orchestrator.providers.openai_text import OpenAITextProvider, create_audio_message
from orchestrator.tools import registry
from orchestrator.types import (
    OrchestratorEvent,
    TextComplete,
    ToolResultEvent,
    ToolUseStart,
    TurnComplete,
    VoiceInterrupted,
)

logger = logging.getLogger(__name__)

# Messages to keep verbatim in the voice system prompt; older ones are summarized.
MAX_VOICE_HISTORY_MESSAGES = 20


class OrchestratorSession:
    """Manage a single orchestrator conversation with JSONL persistence.

    Supports three modes:
    - Text mode (default): Uses AnthropicProvider or OpenAITextProvider
    - Audio mode: Uses OpenAITextProvider with audio content
    - Voice mode: Uses OpenAIVoiceProvider for WebRTC streaming

    The text provider can be switched mid-conversation via set_model().

    Usage (text)::

        session = OrchestratorSession(config=config, context=context)
        session_id = await session.start()
        async for event in session.send("Hello"):
            ...
        await session.stop()

    Usage (audio)::

        session = OrchestratorSession(config=config, context=context)
        session_id = await session.start()
        async for event in session.send_audio(audio_bytes, "wav"):
            ...

    Usage (model switching)::

        success = session.set_model("gpt-4o")
        # Next send() will use the new model

    Usage (voice)::

        session = OrchestratorSession(config=config, context=context, voice=True)
        session_id = await session.start()
        session_update = session.get_session_update()  # send to frontend
        # Then for each event from frontend:
        voice_commands = await session.process_voice_event(event)
        # voice_commands is a list of dicts to send back to frontend
    """

    def __init__(
        self,
        config: OrchestratorConfig,
        context: dict[str, Any],
        session_id: str | None = None,
        local_id: str | None = None,
        voice: bool = False,
    ) -> None:
        self._config = config
        self._context = context
        self._resume_id = session_id  # Original session_id for JSONL continuity
        self._local_id = local_id or str(uuid.uuid4())
        self._agent: OrchestratorAgent | None = None
        self._jsonl_path: Path | None = None
        self._writer: HistoryWriter | None = None
        self._voice = voice
        self._voice_provider = None  # Set in start() if voice=True
        self._history_summary: str | None = None

        # Track current provider for model switching
        self._current_provider = None

    @property
    def local_id(self) -> str:
        """The pool key — stable frontend tab UUID used for reconnection."""
        return self._local_id

    @property
    def jsonl_id(self) -> str:
        """The JSONL filename stem — resume_id when resuming, else local_id."""
        return self._resume_id or self._local_id

    @property
    def is_voice(self) -> bool:
        return self._voice

    @property
    def current_model(self) -> str:
        """Get the current model ID."""
        return self._config.model

    @property
    def current_provider(self) -> str:
        """Get the current provider name."""
        return self._config.provider.value

    @property
    def supports_audio(self) -> bool:
        """Whether the current model supports audio input."""
        return self._config.supports_audio

    async def start(self) -> str:
        """Initialize the session.

        Returns the local_id (pool key). The JSONL file uses ``jsonl_id``
        which equals the original session_id when resuming so we append to
        the same history file.
        """
        # Import tools to ensure they're registered
        import orchestrator.tools.agent_sessions  # noqa: F401
        import orchestrator.tools.search  # noqa: F401
        import orchestrator.tools.files  # noqa: F401
        import orchestrator.tools.voice_control  # noqa: F401

        if self._voice:
            from orchestrator.providers.openai_voice import OpenAIVoiceProvider
            self._voice_provider = OpenAIVoiceProvider()
            provider = self._voice_provider
        else:
            provider = self._create_provider()

        self._current_provider = provider

        self._agent = OrchestratorAgent(
            config=self._config,
            registry=registry,
            provider=provider,
            context=self._context,
        )

        self._jsonl_path = self._get_jsonl_path()
        self._writer = HistoryWriter(self._jsonl_path)

        # If resuming, load history from the existing JSONL
        if self._resume_id and self._jsonl_path.is_file():
            loader = HistoryLoader(self._jsonl_path)
            history = loader.load()
            self._agent.history = history
            if self._voice and len(history) > MAX_VOICE_HISTORY_MESSAGES:
                self._history_summary = await self._summarize_history(
                    history[:-MAX_VOICE_HISTORY_MESSAGES]
                )
        else:
            # New session — write metadata as first line
            self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            meta: dict[str, Any] = {
                "type": "orchestrator_meta",
                "orchestrator": True,
                "session_id": self.jsonl_id,
                "model": self._config.model,
                "provider": self._config.provider.value,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            if self._voice:
                from orchestrator.providers.openai_voice import VOICE_MODEL, VOICE_NAME
                meta["voice"] = True
                meta["openai_model"] = VOICE_MODEL
                meta["voice_name"] = VOICE_NAME
            self._writer.append(meta)

        return self._local_id

    def _create_provider(self):
        """Create a provider instance based on current config."""
        if self._config.provider == Provider.OPENAI:
            return OpenAITextProvider(
                model=self._config.model,
                max_tokens=self._config.max_tokens,
            )
        else:
            return AnthropicProvider(
                model=self._config.model,
                max_tokens=self._config.max_tokens,
            )

    def set_model(self, model_id: str) -> bool:
        """Switch to a different model mid-conversation.

        The change takes effect on the next send() call. Cannot switch
        models during an active voice session.

        Args:
            model_id: The model identifier to switch to

        Returns:
            True if model was found and set, False otherwise
        """
        if self._voice:
            logger.warning("Cannot switch models during voice session")
            return False

        if not self._config.set_model(model_id):
            logger.warning("Unknown model: %s", model_id)
            return False

        # Create new provider
        new_provider = self._create_provider()
        self._current_provider = new_provider

        # Update the agent's provider
        if self._agent:
            self._agent.provider = new_provider

        # Log the model switch
        if self._writer:
            self._writer.append({
                "type": "model_switch",
                "model": model_id,
                "provider": self._config.provider.value,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

        logger.info("Switched to model: %s (%s)", model_id, self._config.provider.value)
        return True

    def get_model_info(self) -> dict[str, Any]:
        """Get current model information."""
        return self._config.to_dict()

    def get_session_update(self) -> dict[str, Any] | None:
        """Return the OpenAI session.update payload for voice mode.

        The caller (WebSocket handler) should send this back to the frontend
        as a voice_command so the frontend can forward it to OpenAI via the
        data channel.
        """
        if not self._voice or self._voice_provider is None:
            return None

        from orchestrator.prompt import build_system_prompt
        history = self._agent.history if self._agent else None
        system = build_system_prompt(
            self._config,
            self._context,
            history=history,
            history_summary=self._history_summary,
        )
        tools = registry.get_openai_definitions()
        return self._voice_provider.get_session_update_payload(system, tools)

    async def process_voice_event(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """Process a single mirrored OpenAI Realtime event.

        Injects the event into the VoiceProvider queue, then processes any
        ToolUseStart events synchronously. Returns a list of voice_command
        dicts to send back to the frontend (tool results + response.create).

        Transcript events (TextDelta, TextComplete) and interruptions
        (VoiceInterrupted) are persisted to JSONL here.
        """
        if not self._voice or self._voice_provider is None:
            return []

        commands: list[dict[str, Any]] = []

        await self._voice_provider.inject_event(event)

        event_type = event.get("type", "")

        # User speech transcript — arrives when Whisper transcription completes
        if event_type == "conversation.item.input_audio_transcription.completed":
            transcript = event.get("transcript", "")
            if transcript:
                self._writer.append({
                    "type": "user",
                    "message": {"role": "user", "content": f"[voice] {transcript}"},
                    "source": "voice_transcription",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

        # User text input (typed messages in voice sessions, if applicable)
        elif event_type == "conversation.item.created":
            item = event.get("item", {})
            if item.get("role") == "user":
                for c in item.get("content", []):
                    if c.get("type") == "input_text" and c.get("text"):
                        self._writer.append({
                            "type": "user",
                            "message": {"role": "user", "content": c["text"]},
                            "source": "voice_transcription",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        })
                        break

        # Tool call ready — execute and send result back
        elif event_type == "response.function_call_arguments.done":
            call_id = event.get("call_id", "")
            name = event.get("name", "")

            # Get name from pending_calls if not in event
            if not name and call_id in self._voice_provider.pending_calls:
                name = self._voice_provider.pending_calls[call_id]

            # Prefer accumulated streaming args over the done event's arguments field
            # (OpenAI may send an empty/missing arguments in the done event when
            # the args were streamed incrementally via delta events)
            args_str = (
                self._voice_provider._pending_args.get(call_id)
                or event.get("arguments", "")
                or "{}"
            )

            try:
                tool_input = json.loads(args_str) if args_str else {}
            except Exception:
                tool_input = {}

            if call_id and name:
                result = await registry.execute(name, tool_input, self._context)
                # Persist tool call + result to JSONL
                self._writer.append({
                    "type": "tool_use",
                    "tool_call_id": call_id,
                    "tool_name": name,
                    "tool_input": tool_input,
                    "source": "voice",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                self._writer.append({
                    "type": "tool_result",
                    "tool_call_id": call_id,
                    "output": result,
                    "is_error": False,
                    "source": "voice",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                commands.append({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": result,
                    },
                })
                commands.append({"type": "response.create"})

        # Assistant transcript complete — persist to JSONL
        elif event_type == "response.audio_transcript.done":
            transcript = event.get("transcript", "")
            if transcript:
                self._writer.append({
                    "type": "assistant",
                    "message": {"role": "assistant", "content": transcript},
                    "source": "voice_response",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

        # Barge-in interruption — mark in JSONL
        elif event_type == "input_audio_buffer.speech_started":
            self._writer.append({
                "type": "voice_interrupted",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

        return commands

    async def send(self, prompt: str) -> AsyncIterator[OrchestratorEvent]:
        """Send a text message and yield events. Persists to JSONL. (Text mode only)"""
        if self._agent is None:
            raise RuntimeError("Session not started")

        # Persist user message
        self._writer.append({
            "type": "user",
            "message": {"role": "user", "content": prompt},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        async for event in self._run_agent(prompt):
            yield event

    async def send_audio(
        self,
        audio_data: bytes | str,
        audio_format: str,
        text_prompt: str | None = None,
    ) -> AsyncIterator[OrchestratorEvent]:
        """Send an audio message and yield events. (Audio mode)

        The audio is sent directly to the model (GPT-4o) for transcription
        and understanding in a single pass.

        Args:
            audio_data: Raw audio bytes or base64-encoded string
            audio_format: Audio format ("wav", "mp3", "webm", "ogg")
            text_prompt: Optional accompanying text

        Yields:
            OrchestratorEvent instances
        """
        if self._agent is None:
            raise RuntimeError("Session not started")

        if not self._config.supports_audio:
            # Switch to audio-capable model automatically.
            # Note: gpt-4o does NOT support audio input - must use gpt-4o-audio-preview.
            # If _voice=True, set_model() normally refuses — force the config directly instead.
            if self._voice:
                self._config.set_model("gpt-4o-audio-preview")
                if not self._config.supports_audio:
                    raise RuntimeError("No audio-capable model available")
            elif not self.set_model("gpt-4o-audio-preview"):
                raise RuntimeError("No audio-capable model available")

        # Convert audio to OpenAI-supported format if needed (wav or mp3)
        # Browser MediaRecorder typically outputs webm, which OpenAI doesn't accept
        converted_data, converted_format = convert_audio_to_wav(audio_data, audio_format)
        logger.debug(f"Audio conversion: {audio_format} -> {converted_format}")

        # Create audio message
        audio_message = create_audio_message(converted_data, converted_format, text_prompt)

        # Persist user message (log that it was audio)
        self._writer.append({
            "type": "user",
            "message": {
                "role": "user",
                "content": f"[audio:{audio_format}] {text_prompt or '(audio message)'}",
            },
            "source": "audio_input",
            "audio_format": audio_format,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        # If the session is in voice (WebRTC) mode, the agent's provider is
        # OpenAIVoiceProvider which waits on a WebRTC event queue — sending audio
        # clips through it would time out. Temporarily swap in an audio-capable
        # text provider for this turn only.
        if self._voice:
            audio_provider = OpenAITextProvider(
                model=self._config.model,
                max_tokens=self._config.max_tokens,
            )
            saved_provider = self._agent._provider
            self._agent._provider = audio_provider
            try:
                async for event in self._run_agent(audio_message):
                    yield event
            finally:
                self._agent._provider = saved_provider
        else:
            async for event in self._run_agent(audio_message):
                yield event

    async def _run_agent(
        self,
        prompt: str | dict[str, Any],
    ) -> AsyncIterator[OrchestratorEvent]:
        """Run the agent with text or audio input and persist events."""
        # Collect assistant text for persistence; persist tool events as they arrive
        assistant_text_parts: list[str] = []

        async for event in self._agent.run(prompt):
            if isinstance(event, TextComplete):
                assistant_text_parts.append(event.text)
            elif isinstance(event, ToolUseStart):
                self._writer.append({
                    "type": "tool_use",
                    "tool_call_id": event.tool_call_id,
                    "tool_name": event.tool_name,
                    "tool_input": event.tool_input,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            elif isinstance(event, ToolResultEvent):
                self._writer.append({
                    "type": "tool_result",
                    "tool_call_id": event.tool_call_id,
                    "output": event.output,
                    "is_error": event.is_error,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            yield event

        # Persist assistant text response
        if assistant_text_parts:
            self._writer.append({
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": "\n".join(assistant_text_parts),
                },
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

    async def compact(self) -> dict[str, int]:
        """Summarize and compress the conversation history.

        Replaces the full history with a single summary message, freeing
        context window space. Returns token counts before/after.

        Returns:
            dict with "tokens_before" and "tokens_after" (estimated)
        """
        if self._agent is None:
            raise RuntimeError("Session not started")

        history = self._agent.history
        if not history:
            return {"tokens_before": 0, "tokens_after": 0}

        # Rough token estimate: ~0.75 tokens per character
        def estimate_tokens(h: list) -> int:
            import json as _json
            try:
                return int(len(_json.dumps(h)) * 0.75)
            except Exception:
                return 0

        tokens_before = estimate_tokens(history)

        summary = await self._summarize_history(history)

        if summary:
            # Replace history with a single summary message
            self._agent.history = [{
                "role": "user",
                "content": f"[Previous conversation summary]\n{summary}",
            }, {
                "role": "assistant",
                "content": "Understood. I have the context from our previous conversation.",
            }]
            # Persist the compact event
            if self._writer:
                self._writer.append({
                    "type": "compact",
                    "trigger": "manual",
                    "summary": summary,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

        tokens_after = estimate_tokens(self._agent.history)
        return {"tokens_before": tokens_before, "tokens_after": tokens_after}

    async def stop(self) -> None:
        """Clean up the session."""
        self._agent = None
        self._voice_provider = None
        self._current_provider = None

    async def interrupt(self) -> None:
        """Interrupt the current agent run."""
        if self._agent:
            await self._agent.interrupt()

    async def _summarize_history(self, messages: list[dict[str, Any]]) -> str:
        """Summarize older conversation messages using a fast Anthropic call.

        Called when history exceeds MAX_VOICE_HISTORY_MESSAGES so voice
        sessions get a concise digest of what happened earlier.
        """
        if not messages:
            return ""

        # Build a compact transcript for the summarizer
        lines: list[str] = []
        for msg in messages:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            label = "User" if role == "user" else "Assistant"
            if isinstance(content, str):
                lines.append(f"{label}: {content.strip()[:500]}")
            elif isinstance(content, list):
                parts: list[str] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        parts.append(block.get("text", "").strip()[:300])
                    elif block.get("type") == "tool_use":
                        parts.append(f"[tool: {block.get('name', '?')}]")
                if parts:
                    lines.append(f"{label}: {' '.join(parts)}")

        transcript = "\n".join(lines)

        try:
            import anthropic
            client = anthropic.AsyncAnthropic()
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{
                    "role": "user",
                    "content": (
                        "Summarize the following conversation in 3-5 concise sentences, "
                        "focusing on what was discussed, decisions made, and any important context "
                        "the assistant should remember. Be factual and brief.\n\n"
                        f"{transcript}"
                    ),
                }],
            )
            return response.content[0].text if response.content else ""
        except Exception as e:
            logger.warning("Failed to summarize history: %s", e)
            return ""

    def _get_jsonl_path(self) -> Path:
        """Get the JSONL file path for this session.

        Uses context/ directly for portability.
        """
        sessions_dir = get_sessions_dir()
        sessions_dir.mkdir(parents=True, exist_ok=True)
        return sessions_dir / f"{self.jsonl_id}.jsonl"
