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
        self._resume_id = session_id
        self._local_id = local_id  # Stable ID from frontend, if provided
        self._session_id: str | None = None
        self._agent: OrchestratorAgent | None = None
        self._jsonl_path: Path | None = None
        self._voice = voice
        self._voice_provider = None  # Set in start() if voice=True

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def is_voice(self) -> bool:
        return self._voice

    async def start(self) -> str:
        """Initialize the session. Returns the session ID."""
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

        # Create agent
        self._agent = OrchestratorAgent(
            config=self._config,
            registry=registry,
            provider=provider,
            context=self._context,
        )

        # Determine session ID — use local_id if provided, resume_id if
        # resuming, otherwise generate a new UUID
        if self._local_id:
            self._session_id = self._local_id
        elif self._resume_id:
            self._session_id = self._resume_id
        else:
            self._session_id = str(uuid.uuid4())

        self._jsonl_path = self._get_jsonl_path()

        # If resuming, load history from JSONL
        if self._resume_id and self._jsonl_path.is_file():
            history = self._load_history()
            self._agent.history = history
        else:
            # Write session metadata as first line
            self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            meta: dict[str, Any] = {
                "type": "orchestrator_meta",
                "orchestrator": True,
                "session_id": self._session_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            if self._voice:
                from orchestrator.providers.openai_voice import VOICE_MODEL, VOICE_NAME
                meta["voice"] = True
                meta["openai_model"] = VOICE_MODEL
                meta["voice_name"] = VOICE_NAME
            self._append_jsonl(meta)

        return self._session_id

    def get_session_update(self) -> dict[str, Any] | None:
        """Return the OpenAI session.update payload for voice mode.

        The caller (WebSocket handler) should send this back to the frontend
        as a voice_command so the frontend can forward it to OpenAI via the
        data channel.
        """
        if not self._voice or self._voice_provider is None:
            return None

        from orchestrator.prompt import build_system_prompt
        system = build_system_prompt(self._config, self._context)
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
                self._append_jsonl({
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
                        self._append_jsonl({
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
                self._append_jsonl({
                    "type": "tool_use",
                    "tool_call_id": call_id,
                    "tool_name": name,
                    "tool_input": tool_input,
                    "source": "voice",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                self._append_jsonl({
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
                self._append_jsonl({
                    "type": "assistant",
                    "message": {"role": "assistant", "content": transcript},
                    "source": "voice_response",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

        # Barge-in interruption — mark in JSONL
        elif event_type == "input_audio_buffer.speech_started":
            self._append_jsonl({
                "type": "voice_interrupted",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

        return commands

    async def send(self, prompt: str) -> AsyncIterator[OrchestratorEvent]:
        """Send a message and yield events. Persists to JSONL. (Text mode only)"""
        if self._agent is None:
            raise RuntimeError("Session not started")

        # Persist user message
        self._append_jsonl({
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
                self._append_jsonl({
                    "type": "tool_use",
                    "tool_call_id": event.tool_call_id,
                    "tool_name": event.tool_name,
                    "tool_input": event.tool_input,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            elif isinstance(event, ToolResultEvent):
                self._append_jsonl({
                    "type": "tool_result",
                    "tool_call_id": event.tool_call_id,
                    "output": event.output,
                    "is_error": event.is_error,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            yield event

        # Persist assistant text response
        if assistant_text_parts:
            self._append_jsonl({
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
        return sessions_dir / f"{self._session_id}.jsonl"

    def _append_jsonl(self, data: dict[str, Any]) -> None:
        """Append a line to the session's JSONL file."""
        if self._jsonl_path is None:
            return
        try:
            with open(self._jsonl_path, "a") as f:
                f.write(json.dumps(data) + "\n")
        except Exception as e:
            logger.warning("Failed to write to JSONL: %s", e)

    def _load_history(self) -> list[dict[str, Any]]:
        """Load conversation history from the JSONL file.

        Reconstructs the full message history including tool calls and results.
        Tool results are stored as separate JSONL entries and grouped into a
        single user message (as required by the Anthropic API).
        """
        if self._jsonl_path is None or not self._jsonl_path.is_file():
            return []

        history: list[dict[str, Any]] = []
        # Buffer to accumulate tool_result entries between assistant messages
        pending_tool_results: list[dict[str, Any]] = []
        try:
            with open(self._jsonl_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    msg_type = obj.get("type")
                    if msg_type == "tool_result":
                        pending_tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": obj.get("tool_call_id", ""),
                            "content": obj.get("output", ""),
                            **({"is_error": True} if obj.get("is_error") else {}),
                        })
                    elif msg_type in ("user", "assistant"):
                        # Flush any buffered tool results as a user message first
                        if pending_tool_results:
                            history.append({
                                "role": "user",
                                "content": pending_tool_results,
                            })
                            pending_tool_results = []
                        msg = obj.get("message", {})
                        history.append({
                            "role": msg.get("role", msg_type),
                            "content": msg.get("content", ""),
                        })

            # Flush any trailing tool results
            if pending_tool_results:
                history.append({"role": "user", "content": pending_tool_results})

        except Exception as e:
            logger.warning("Failed to load history: %s", e)

        return history
