"""Orchestrator session — wraps OrchestratorAgent with JSONL persistence.

Supports three modes:
- Text mode (default): Uses configurable provider (Anthropic/OpenAI)
- Audio mode: Uses OpenAI for multimodal audio input
- Voice mode: Uses OpenAI Realtime for WebRTC streaming

Text and audio modes support runtime model switching. Voice mode uses a
fixed model for the session duration (WebRTC constraint).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
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
from orchestrator.audio_recorder import AudioRecorder, is_recording_enabled
from orchestrator.providers.anthropic import AnthropicProvider
from orchestrator.providers.openai_text import OpenAITextProvider, create_audio_message
from orchestrator.runner import BackgroundAgentRunner, Notification, NotificationQueue
from orchestrator.token_budget import (
    RECENT_VERBATIM_TOKENS,
    estimate_message_tokens,
    scale_summary_max_tokens,
    split_by_token_budget,
    truncate_tool_results,
)
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


def _render_notifications(notes: list[Notification]) -> str:
    """Render a batch of background-agent notifications as a status block.

    Status lines only (per design choice): the orchestrator must call
    read_agent_session or peek_agent_session to retrieve actual content.
    Keeps the synthetic prompt cheap and forces explicit follow-up reads.
    """
    if not notes:
        return ""
    lines: list[str] = [f"[Background events — {len(notes)} update{'s' if len(notes) != 1 else ''}]"]
    for n in notes:
        sid_short = n.session_id[:8]
        title_part = f' ("{n.session_title}")' if n.session_title else ""
        bits = [
            f"[SESSION {sid_short}{title_part}",
            f"event: turn {n.turn_id[:8]} {n.status}",
            f"duration={n.duration_seconds:.1f}s",
        ]
        if n.cost:
            bits.append(f"cost=${n.cost:.4f}")
        if n.turns:
            bits.append(f"turns={n.turns}")
        if n.error:
            bits.append(f'error="{n.error}"')
        lines.append(", ".join(bits) + "]")
    lines.append(
        "(Use read_agent_session(session_id) for persisted output, "
        "peek_agent_session(session_id, turn_id) for live in-flight events.)"
    )
    return "\n".join(lines)


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
        session_update = await session.get_session_update()  # send to frontend
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
        voice_provider: str | None = None,
        voice_model: str | None = None,
        voice_name: str | None = None,
        voice_transcription_language: str | None = None,
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
        self._voice_provider_id: str | None = voice_provider
        self._voice_model_id: str | None = voice_model
        self._voice_name: str | None = voice_name
        self._voice_transcription_language: str | None = voice_transcription_language
        self._voice_relay = None  # Set lazily for websocket providers
        self._history_summary: str | None = None
        self._audio_recorder: AudioRecorder | None = None  # Set in start() if recording enabled

        # Track current provider for model switching
        self._current_provider = None

        # Background agent runner — owns fire-and-forget agent turns spawned
        # by the send_to_agent_session tool.  Notifications drain at the top
        # of every send() call; the route layer installs a wake_callback that
        # synthesises an empty-prompt turn when one arrives while idle.
        self._notifications = NotificationQueue()
        store = context.get("store")
        pool = context.get("pool")
        if store is not None and pool is not None:
            self._runner: BackgroundAgentRunner | None = BackgroundAgentRunner(
                pool, store, self._notifications,
            )
            # Make the runner reachable from tools that get only the
            # context dict (e.g. send_to_agent_session, peek_agent_session).
            context["runner"] = self._runner
            context["notifications"] = self._notifications
        else:
            self._runner = None
        # Held while a send() / send_audio() / _run_agent is in flight.  The
        # wake callback uses is_busy to decide whether to schedule a
        # synthetic turn now or let the next user prompt drain notifications.
        self._busy_lock = asyncio.Lock()

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
    def voice_provider_id(self) -> str | None:
        """Provider id (``openai`` | ``google`` | ``qwen``) when voice mode is active."""
        if self._voice_provider is not None:
            return self._voice_provider.provider_name
        return self._voice_provider_id

    @property
    def voice_model_id(self) -> str | None:
        """Model id when voice mode is active."""
        if self._voice_provider is not None:
            return self._voice_provider.model
        return self._voice_model_id

    @property
    def voice_name_id(self) -> str | None:
        """Selected voice/speaker id when voice mode is active."""
        if self._voice_provider is not None:
            return self._voice_provider.voice
        return self._voice_name

    @property
    def voice_transcription_language(self) -> str | None:
        """Transcription language hint when voice mode is active.

        Empty string ``""`` means auto-detect (no language hint sent).
        """
        if self._voice_provider is not None:
            return getattr(self._voice_provider, "transcription_language", None)
        return self._voice_transcription_language

    @property
    def is_busy(self) -> bool:
        """True while a send/send_audio/_run_agent turn holds the busy lock.

        The wake callback (installed by the route layer) consults this to
        decide whether to fire a synthetic-prompt turn for queued
        notifications immediately or wait for the in-flight turn to finish.
        """
        return self._busy_lock.locked()

    @property
    def notifications(self) -> NotificationQueue:
        """Queue of pending background-agent notifications.

        Drained at the start of every text-mode turn; route layer installs
        a wake_callback so that a notification arriving while idle triggers
        a synthetic empty-prompt turn.
        """
        return self._notifications

    @property
    def runner(self) -> BackgroundAgentRunner | None:
        return self._runner

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
        import orchestrator.tools.audio_playback  # noqa: F401
        import orchestrator.tools.files  # noqa: F401
        import orchestrator.tools.search  # noqa: F401
        import orchestrator.tools.voice_control  # noqa: F401

        if self._voice:
            from orchestrator.providers.voice_registry import (
                instantiate_provider,
                resolve_voice_target,
            )
            provider_id, model_entry, voice_name, language = resolve_voice_target(
                self._voice_provider_id,
                self._voice_model_id,
                self._voice_name,
                self._voice_transcription_language,
            )
            self._voice_provider_id = provider_id
            self._voice_model_id = model_entry["id"]
            self._voice_name = voice_name
            self._voice_transcription_language = language
            self._voice_provider = instantiate_provider(
                provider_id, model_entry["id"], voice_name, language,
            )
            provider = self._voice_provider
        else:
            provider = self._create_provider()

        self._current_provider = provider

        # Make the session accessible from tools via context
        self._context["session"] = self

        self._agent = OrchestratorAgent(
            config=self._config,
            registry=registry,
            provider=provider,
            context=self._context,
        )

        self._jsonl_path = self._get_jsonl_path()
        self._writer = HistoryWriter(self._jsonl_path)

        # If resuming, load history from the existing JSONL.
        # For voice mode the summary is built fresh in get_session_update() on
        # every (re)connect, so we don't precompute it here.
        if self._resume_id and self._jsonl_path.is_file():
            loader = HistoryLoader(self._jsonl_path)
            history = loader.load()
            self._agent.history = history
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
            if self._voice and self._voice_provider is not None:
                meta["voice"] = True
                meta["voice_provider"] = self._voice_provider.provider_name
                meta["voice_model"] = self._voice_provider.model
                meta["voice_name"] = self._voice_provider.voice
                lang = getattr(self._voice_provider, "transcription_language", None)
                if lang is not None:
                    meta["voice_transcription_language"] = lang or "auto"
                # Legacy field — kept for back-compat with older readers.
                if self._voice_provider.provider_name == "openai":
                    meta["openai_model"] = self._voice_provider.model
            self._writer.append(meta)

        # Start audio recorder if voice mode and recording is enabled
        if self._voice and is_recording_enabled():
            self._audio_recorder = AudioRecorder(
                session_id=self._local_id,
                provider=self._voice_provider_id or "",
                model=self._voice_model_id or "",
                voice=self._voice_name or "",
            )
            self._audio_recorder.start()
            logger.info("Audio recorder started for session %s", self._local_id)

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

    async def get_session_update(self) -> dict[str, Any] | None:
        """Return the OpenAI session.update payload for voice mode.

        The caller (WebSocket handler) should send this back to the frontend
        as a voice_command so the frontend can forward it to OpenAI via the
        data channel.

        History is reloaded from the JSONL every call so that voice restarts
        (which reconnect to the same in-memory session) always see the full
        conversation, not whatever snapshot was loaded at start(). The
        summarizer is also re-run here so the digest covers recent turns.
        """
        if not self._voice or self._voice_provider is None:
            return None

        from orchestrator.prompt import build_system_prompt

        recent_messages, history_summary = await self._build_history_for_prompt()

        system = build_system_prompt(
            self._config,
            self._context,
            recent_messages=recent_messages,
            history_summary=history_summary,
        )
        tools = registry.get_openai_definitions()
        return self._voice_provider.get_session_update_payload(system, tools)

    async def _build_history_for_prompt(
        self,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Load history fresh from JSONL, clip tool results, split by token
        budget, and summarize the older prefix.

        Returns (recent_verbatim_messages, summary_or_none).
        """
        if self._jsonl_path is None or not self._jsonl_path.is_file():
            return [], None

        history = HistoryLoader(self._jsonl_path).load()
        if not history:
            return [], None

        # Clip oversized tool results before budgeting so we don't evict
        # useful conversational turns to make room for a 50KB file dump.
        clipped = truncate_tool_results(history)

        older, recent = split_by_token_budget(clipped, RECENT_VERBATIM_TOKENS)

        summary: str | None = None
        if older:
            prefix_tokens = sum(estimate_message_tokens(m) for m in older)
            summary_budget = scale_summary_max_tokens(len(older), prefix_tokens)
            summary = await self._summarize_history(
                older, max_tokens=summary_budget
            )

        self._history_summary = summary
        return recent, summary

    @property
    def needs_voice_relay(self) -> bool:
        """True for WS providers (Qwen/Gemini/locals) that relay through backend."""
        return (
            self._voice
            and self._voice_provider is not None
            and self._voice_provider.connection_type == "websocket"
        )

    async def start_voice_relay(
        self,
        on_audio_out: Callable[[str], Awaitable[None]],
        on_event_for_frontend: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Open the upstream provider WS for non-WebRTC voice providers.

        The handler layer wires the two callbacks so audio chunks become
        ``voice_audio_out`` payloads on the orchestrator broadcast and
        provider events are mirrored to subscribers.
        """
        if not self.needs_voice_relay:
            return
        from orchestrator.voice_relay import VoiceRelay
        # Lazy-build the session.update payload that seeds the upstream WS.
        session_update = await self.get_session_update()
        if session_update is None:
            raise RuntimeError("Voice provider did not produce a session.update payload")
        relay = VoiceRelay(
            self._voice_provider,
            on_audio_out=on_audio_out,
            on_event_for_frontend=on_event_for_frontend,
            session_id=self._local_id,
        )
        await relay.start(session_update)
        self._voice_relay = relay

    async def stop_voice_relay(self) -> None:
        if self._voice_relay is not None:
            await self._voice_relay.stop()
            self._voice_relay = None

    async def send_voice_audio_in(self, pcm_b64: str) -> None:
        """Forward a frontend mic chunk upstream (WS providers only)."""
        if self._voice_relay is None:
            return
        # Record user audio if recorder is active
        if self._audio_recorder is not None and self._audio_recorder.is_recording:
            self._audio_recorder.write_user_audio(pcm_b64)
        await self._voice_relay.send_audio(pcm_b64)

    def record_assistant_audio(self, pcm_b64: str) -> None:
        """Record assistant audio chunk (called from route layer for WS providers)."""
        if self._audio_recorder is not None and self._audio_recorder.is_recording:
            self._audio_recorder.write_assistant_audio(pcm_b64)

    @property
    def audio_recorder(self) -> AudioRecorder | None:
        """The audio recorder instance, if recording is active."""
        return self._audio_recorder

    async def send_voice_event_upstream(self, event: dict[str, Any]) -> None:
        """Forward a frontend control event to the upstream provider WS.

        Used for tool results and any other commands the model expects to
        receive from the client. WebRTC providers ignore this — the
        frontend sends those directly via the data channel.
        """
        if self._voice_relay is None:
            return
        await self._voice_relay.send_event(event)

    async def process_voice_event(
        self,
        event: dict[str, Any],
        *,
        inject: bool = True,
    ) -> list[dict[str, Any]]:
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

        if inject:
            await self._voice_provider.inject_event(event)

        event_type = event.get("type", "")

        # User speech transcript — arrives when Whisper transcription completes
        if event_type == "conversation.item.input_audio_transcription.completed":
            transcript = event.get("transcript", "")
            if transcript:
                # Build message content with optional audio segment reference
                segment = None
                if self._audio_recorder is not None and self._audio_recorder.is_recording:
                    segment = self._audio_recorder.mark_user_turn_end(transcript)

                if segment:
                    # Include audio reference in the message text itself
                    content = (
                        f"[voice, recording: {self._local_id} user "
                        f"{segment['start_ms']}-{segment['end_ms']}ms] {transcript}"
                    )
                else:
                    content = f"[voice] {transcript}"

                entry: dict[str, Any] = {
                    "type": "user",
                    "message": {"role": "user", "content": content},
                    "source": "voice_transcription",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                if segment:
                    entry["audio_segment"] = segment
                self._writer.append(entry)

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
                # Provider-specific command sequence to ship the result back
                # and ask for the next response.
                commands.extend(self._voice_provider.format_tool_result(call_id, result))

        # Assistant transcript complete — STAGE it; we only persist if the
        # turn ended in status="completed" (response.done below). Otherwise
        # the transcript is a fragment cut off by barge-in or response.cancel
        # and would pollute history with sentences like "Yeah, I think".
        elif event_type == "response.audio_transcript.done":
            transcript = event.get("transcript", "")
            if transcript:
                self._pending_assistant_transcript = transcript

        # Turn complete — decide whether to persist the staged transcript.
        elif event_type == "response.done":
            response = event.get("response", {})
            status = response.get("status", "completed")
            staged = getattr(self, "_pending_assistant_transcript", None)
            if staged and status == "completed":
                # Build message content with optional audio segment reference
                segment = None
                if self._audio_recorder is not None and self._audio_recorder.is_recording:
                    segment = self._audio_recorder.mark_assistant_turn_end(staged)

                if segment:
                    # Include audio reference in the message text itself
                    content = (
                        f"[voice, recording: {self._local_id} assistant "
                        f"{segment['start_ms']}-{segment['end_ms']}ms] {staged}"
                    )
                else:
                    content = staged

                entry: dict[str, Any] = {
                    "type": "assistant",
                    "message": {"role": "assistant", "content": content},
                    "source": "voice_response",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                if segment:
                    entry["audio_segment"] = segment
                self._writer.append(entry)
            self._pending_assistant_transcript = None

        # Barge-in interruption — mark in JSONL
        elif event_type == "input_audio_buffer.speech_started":
            self._writer.append({
                "type": "voice_interrupted",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

        return commands

    async def send(self, prompt: str) -> AsyncIterator[OrchestratorEvent]:
        """Send a text message and yield events. Persists to JSONL. (Text mode only)

        Drains any queued background-agent notifications first.  An empty
        prompt with no pending notifications is a spurious wake (e.g. the
        wake-callback fired between is_busy check and notification arrival)
        — short-circuit before grabbing the lock.

        The whole turn is wrapped in self._busy_lock so the route layer's
        wake_callback can reliably tell whether the orchestrator is mid-turn
        and queue notifications for the next idle window instead.
        """
        if self._agent is None:
            raise RuntimeError("Session not started")

        # Cheap pre-check: if there's nothing to do, don't even take the lock.
        # (The deeper check is also done after the lock is held, in case a
        # racing push() arrived in between.)
        if not prompt and not self._notifications.has_pending():
            return

        async with self._busy_lock:
            pending = self._notifications.drain()
            if not prompt and not pending:
                return  # racing wake; nothing to deliver
            # Persist a JSONL line for each drained notification so the run
            # is replayable (ties back to the originating tool_use_id).
            for n in pending:
                self._writer.append({
                    "type": "background_notification",
                    "notification_id": n.notification_id,
                    "turn_id": n.turn_id,
                    "session_id": n.session_id,
                    "session_title": n.session_title,
                    "origin_tool_use_id": n.origin_tool_use_id,
                    "status": n.status,
                    "cost": n.cost,
                    "turns": n.turns,
                    "duration_seconds": n.duration_seconds,
                    "error": n.error,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            # Build the actual prompt the LLM sees.  Notifications go FIRST
            # so the model reads them before anything the user typed.
            rendered = _render_notifications(pending)
            if rendered and prompt:
                effective_prompt = rendered + "\n\n" + prompt
            elif rendered:
                effective_prompt = rendered
            else:
                effective_prompt = prompt

            # Persist user message (the unmodified original; notification
            # lines are recorded separately as background_notification entries
            # above so the JSONL stays clean).
            if prompt:
                self._writer.append({
                    "type": "user",
                    "message": {"role": "user", "content": prompt},
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

            async for event in self._run_agent(effective_prompt):
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

        # Audio mode also drains notifications — prepend them to the audio's
        # accompanying text prompt so the LLM sees them first in this turn.
        async with self._busy_lock:
            pending = self._notifications.drain()
            if pending:
                for n in pending:
                    self._writer.append({
                        "type": "background_notification",
                        "notification_id": n.notification_id,
                        "turn_id": n.turn_id,
                        "session_id": n.session_id,
                        "session_title": n.session_title,
                        "origin_tool_use_id": n.origin_tool_use_id,
                        "status": n.status,
                        "cost": n.cost,
                        "turns": n.turns,
                        "duration_seconds": n.duration_seconds,
                        "error": n.error,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                rendered = _render_notifications(pending)
                text_prompt = (rendered + "\n\n" + text_prompt) if text_prompt else rendered

            async for event in self._send_audio_inner(audio_data, audio_format, text_prompt):
                yield event

    async def _send_audio_inner(
        self,
        audio_data: bytes | str,
        audio_format: str,
        text_prompt: str | None,
    ) -> AsyncIterator[OrchestratorEvent]:
        """The original send_audio body, called inside the busy_lock.

        Split out so the notification-drain wrapper above can stay readable.
        """
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
        """Clean up the session.

        Cancels every in-flight background-agent turn (with SDK interrupts so
        the bundled ``claude`` subprocesses actually stop) and unsubscribes
        the wake callback so notifications fired during shutdown go nowhere.
        """
        self._notifications.set_wake_callback(None)
        if self._runner is not None:
            try:
                await self._runner.cancel_all()
            except Exception:  # noqa: BLE001
                logger.exception("BackgroundAgentRunner.cancel_all failed during stop")
        try:
            await self.stop_voice_relay()
        except Exception:  # noqa: BLE001
            logger.exception("stop_voice_relay failed during session stop")
        # Stop audio recorder
        if self._audio_recorder is not None:
            try:
                self._audio_recorder.stop()
                logger.info("Audio recorder stopped for session %s", self._local_id)
            except Exception:  # noqa: BLE001
                logger.exception("Audio recorder stop failed during session stop")
            self._audio_recorder = None
        self._agent = None
        self._voice_provider = None
        self._current_provider = None

    async def interrupt(self) -> None:
        """Interrupt the current agent run."""
        if self._agent:
            await self._agent.interrupt()

    async def _summarize_history(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int = 2048,
    ) -> str:
        """Summarize older conversation messages using the orchestrator's current model.

        Produces a structured digest so that when voice mode resumes the
        orchestrator can pick up the thread coherently. ``max_tokens`` scales
        with the prefix size (see ``scale_summary_max_tokens``).

        Routes through whichever provider the orchestrator is currently using
        (Anthropic or OpenAI) so summarization works regardless of which API
        keys are configured in the environment.
        """
        if not messages or max_tokens <= 0:
            return ""

        # Build a transcript for the summarizer. Keep more per-message content
        # than the old 300-char clip so long-form answers survive summarization.
        lines: list[str] = []
        for msg in messages:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            label = "User" if role == "user" else "Assistant"
            if isinstance(content, str):
                lines.append(f"{label}: {content.strip()[:2000]}")
            elif isinstance(content, list):
                parts: list[str] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        parts.append(block.get("text", "").strip()[:1500])
                    elif btype == "tool_use":
                        parts.append(f"[tool: {block.get('name', '?')}]")
                    elif btype == "tool_result":
                        rc = block.get("content", "")
                        if isinstance(rc, list):
                            rc = " ".join(
                                b.get("text", "") for b in rc
                                if isinstance(b, dict) and b.get("type") == "text"
                            )
                        parts.append(f"[tool result: {str(rc)[:400]}]")
                if parts:
                    lines.append(f"{label}: {' '.join(parts)}")

        transcript = "\n".join(lines)

        instructions = (
            "Summarize the conversation below into a structured digest the "
            "assistant will read to resume the chat. Use these sections, in "
            "order, and only include ones that apply:\n"
            "- **Topics & goals**: what was being worked on, and why\n"
            "- **Decisions made**: concrete choices, trade-offs accepted\n"
            "- **Open threads**: unresolved questions, things the user wanted "
            "to come back to, work in progress\n"
            "- **Key entities**: files, sessions, projects, people, tools "
            "mentioned that matter for continuity\n"
            "- **User preferences/context expressed**: anything about how the "
            "user wants the assistant to behave, tone, constraints\n"
            "Be factual and specific — prefer concrete names over generic "
            "summaries. Omit pleasantries and filler. Write in the same "
            "language(s) the conversation used.\n\n"
            f"Conversation:\n{transcript}"
        )

        model = self._config.model
        provider = self._config.provider
        try:
            if provider == Provider.OPENAI:
                import openai
                client = openai.AsyncOpenAI()
                response = await client.chat.completions.create(
                    model=model,
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": instructions}],
                )
                choice = response.choices[0] if response.choices else None
                return (choice.message.content or "") if choice else ""
            else:
                import anthropic
                client = anthropic.AsyncAnthropic()
                response = await client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": instructions}],
                )
                return response.content[0].text if response.content else ""
        except Exception as e:
            logger.warning("Failed to summarize history with %s/%s: %s", provider.value, model, e)
            return ""

    def _get_jsonl_path(self) -> Path:
        """Get the JSONL file path for this session.

        Uses context/ directly for portability.
        """
        sessions_dir = get_sessions_dir()
        sessions_dir.mkdir(parents=True, exist_ok=True)
        return sessions_dir / f"{self.jsonl_id}.jsonl"
