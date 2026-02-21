"""Orchestrator session — wraps OrchestratorAgent with JSONL persistence."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.agent import OrchestratorAgent
from orchestrator.config import OrchestratorConfig
from orchestrator.persistence import HistoryLoader, HistoryWriter
from orchestrator.providers.anthropic import AnthropicProvider
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


def _mangle_path(project_path: str) -> str:
    """Convert an absolute path to Claude Code's mangled directory name."""
    return project_path.rstrip("/").replace("/", "-")


class OrchestratorSession:
    """Manage a single orchestrator conversation with JSONL persistence.

    Supports two modes:
    - Text mode (default): Uses AnthropicProvider, driven by session.send(text)
    - Voice mode: Uses OpenAIVoiceProvider, driven by session.process_voice_event(event)

    Usage (text)::

        session = OrchestratorSession(config=config, context=context)
        session_id = await session.start()
        async for event in session.send("Hello"):
            ...
        await session.stop()

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

        if self._voice:
            from orchestrator.providers.openai_voice import OpenAIVoiceProvider
            self._voice_provider = OpenAIVoiceProvider()
            provider = self._voice_provider
        else:
            provider = AnthropicProvider(
                model=self._config.model,
                max_tokens=self._config.max_tokens,
            )

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
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            if self._voice:
                from orchestrator.providers.openai_voice import VOICE_MODEL, VOICE_NAME
                meta["voice"] = True
                meta["openai_model"] = VOICE_MODEL
                meta["voice_name"] = VOICE_NAME
            self._writer.append(meta)

        return self._local_id

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
            args_str = event.get("arguments", "{}")
            name = event.get("name", "")

            # Get name from pending_calls if not in event
            if not name and call_id in self._voice_provider.pending_calls:
                name = self._voice_provider.pending_calls[call_id]

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
        """Send a message and yield events. Persists to JSONL. (Text mode only)"""
        if self._agent is None:
            raise RuntimeError("Session not started")

        # Persist user message
        self._writer.append({
            "type": "user",
            "message": {"role": "user", "content": prompt},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

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

    async def stop(self) -> None:
        """Clean up the session."""
        self._agent = None
        self._voice_provider = None

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
        """Get the JSONL file path for this session."""
        import os

        config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        if config_dir:
            base = Path(config_dir)
        else:
            base = Path.home() / ".claude"

        project_dir = str(Path(self._config.project_dir).resolve())
        mangled = _mangle_path(project_dir)
        sessions_dir = base / "projects" / mangled
        sessions_dir.mkdir(parents=True, exist_ok=True)
        return sessions_dir / f"{self.jsonl_id}.jsonl"
