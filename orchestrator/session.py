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
import enum
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
from orchestrator.providers.voice_base import BaseVoiceProvider
from orchestrator.audio_recorder import AudioRecorder, is_recording_enabled
# Provider classes are imported lazily inside the methods that need them so
# this module remains importable on machines where the corresponding SDK
# (``anthropic`` for Claude, ``openai`` for GPT/Qwen/Gemini) isn't installed.
# Missing SDKs surface as a 400 at config-save time and a friendly runtime
# error at send time — never as a backend that won't boot.
from orchestrator.runner import BackgroundAgentRunner, Notification, NotificationQueue
from orchestrator import summary_cache
from orchestrator.token_budget import (
    RECENT_VERBATIM_TOKENS,
    estimate_message_tokens,
    split_by_token_budget,
    summary_target_word_range,
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

# Fallback when ``summarizer_model`` is unset in assistant_config.json.
# A frontier model with a long context, picked because the user is more likely
# to have an OpenAI key configured than an Anthropic one in this environment.
# Override globally by editing ``summarizer_model`` in assistant_config.json.
DEFAULT_SUMMARIZER_MODEL = "gpt-5.1"


class VoiceLifecycle(enum.Enum):
    """Voice session lifecycle states — guarded by ``OrchestratorSession._voice_lock``.

    All transitions go through :meth:`OrchestratorSession._transition_voice_state`.
    Only the transitions in :data:`_VALID_VOICE_TRANSITIONS` are allowed; anything
    else is a bug and raises.

    IDLE
        No voice session active (text mode, or voice never started, or already
        finished cleaning up).
    STARTING
        ``start_voice_relay`` is running. Upstream handshake in flight.
    ACTIVE
        Relay is up and pumping events. Normal operating state.
    ENDING
        ``end_voice`` is tearing down. Late events are dropped. New
        ``voice_start`` for the same ``local_id`` must wait for ``ENDED``.
    ENDED
        Terminal. The session is cleaned up. A fresh start uses a new
        ``OrchestratorSession`` instance.
    """

    IDLE = "idle"
    STARTING = "starting"
    ACTIVE = "active"
    ENDING = "ending"
    ENDED = "ended"


# Source → set of allowed targets. Used to validate every transition so a
# stray double-call (e.g. concurrent agent end_voice + user stop) becomes a
# silent no-op instead of a corrupting double-teardown.
_VALID_VOICE_TRANSITIONS: dict[VoiceLifecycle, set[VoiceLifecycle]] = {
    VoiceLifecycle.IDLE: {VoiceLifecycle.STARTING, VoiceLifecycle.ENDING},
    VoiceLifecycle.STARTING: {VoiceLifecycle.ACTIVE, VoiceLifecycle.ENDING},
    VoiceLifecycle.ACTIVE: {VoiceLifecycle.ENDING},
    VoiceLifecycle.ENDING: {VoiceLifecycle.ENDED},
    VoiceLifecycle.ENDED: set(),  # terminal
}

# Best-effort timeout for the provider's graceful_shutdown_frames send.
# If the upstream WS is already wedged we don't want teardown to block
# forever; the WS close that follows will free the resource anyway.
_GRACEFUL_SHUTDOWN_TIMEOUT_S = 0.5


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
        voice_endpoint: str | None = None,
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
        # Backend selector for the "google" voice provider (AI Studio vs
        # Vertex). Ignored by other providers. ``None`` means "use the
        # registry default" (currently Vertex).
        self._voice_endpoint: str | None = voice_endpoint
        self._voice_relay = None  # Set lazily for websocket providers
        self._history_summary: str | None = None
        self._audio_recorder: AudioRecorder | None = None  # Set in start() if recording enabled

        # Voice lifecycle state machine. ``IDLE`` for both text and
        # not-yet-started voice; the route layer flips us to ``STARTING``
        # before calling ``start_voice_relay``. ``_voice_lock`` guards every
        # transition; ``_voice_ended`` is set when state == ENDED so callers
        # waiting on a tear-down can piggyback on the in-flight one without
        # racing.
        self._voice_state: VoiceLifecycle = VoiceLifecycle.IDLE
        self._voice_lock: asyncio.Lock = asyncio.Lock()
        self._voice_ended: asyncio.Event = asyncio.Event()
        # Last reason the session ended (or None). Useful for logging /
        # exposing through the broadcast.
        self._voice_end_reason: str | None = None

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

        # Injection window — set by the listen_recording tool while it's
        # pumping past audio into the live voice WS.  See the is_injecting
        # property for the full list of behaviours this gates.
        self._injection_until: float = 0.0
        self._injection_active: bool = False
        self._injection_watchdog: asyncio.Task[None] | None = None

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
    def voice_state(self) -> VoiceLifecycle:
        """Current voice-lifecycle state. See :class:`VoiceLifecycle`."""
        return self._voice_state

    @property
    def voice_is_ending(self) -> bool:
        """True while :meth:`end_voice` is tearing the session down.

        Used by the pool/route layer to block a fresh ``voice_start`` for
        the same ``local_id`` so we don't reconnect into a dying relay.
        """
        return self._voice_state == VoiceLifecycle.ENDING

    @property
    def voice_provider(self) -> BaseVoiceProvider | None:
        """The active voice provider instance, or None outside a voice session.

        Exposed so callers (notably the WebSocket route's voice-event
        filter) can consult provider-specific hooks like
        ``accepts_upstream_event`` without reaching through the private
        attribute.
        """
        return self._voice_provider

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
        import orchestrator.tools.assistant_config  # noqa: F401
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
                endpoint=self._voice_endpoint,
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
        """Create a provider instance based on current config.

        Imports the underlying SDK lazily so a Qwen-only deployment can
        still load this module — the SDK is only required at the moment
        we actually need to talk to its API.
        """
        if self._config.provider == Provider.OPENAI:
            try:
                from orchestrator.providers.openai_text import OpenAITextProvider
            except ImportError as e:
                raise RuntimeError(
                    "OpenAI provider requested but `openai` package is not installed. "
                    "Install it with: pip install -r requirements-openai.txt"
                ) from e
            return OpenAITextProvider(
                model=self._config.model,
                max_tokens=self._config.max_tokens,
            )
        try:
            from orchestrator.providers.anthropic import AnthropicProvider
        except ImportError as e:
            raise RuntimeError(
                "Anthropic provider requested but `anthropic` package is not installed. "
                "Install it with: pip install -r requirements-anthropic.txt"
            ) from e
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
            voice_provider_id=self._voice_provider.provider_name,
        )
        tools = registry.get_openai_definitions()
        return self._voice_provider.get_session_update_payload(system, tools)

    async def _build_history_for_prompt(
        self,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Load history fresh from JSONL, clip tool results, split by token
        budget, and summarize the older prefix.

        The summary of the older prefix is the expensive part — a 15-25s
        LLM call on long sessions. We consult ``summary_cache`` first
        (sibling ``.summary.json`` file keyed on JSONL size+mtime). On
        cache hit we skip the LLM call entirely. On miss we compute
        synchronously (so the caller still gets a correct prompt) and
        write the result back so the next call is fast.

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
            target_words = summary_target_word_range(len(older), prefix_tokens)

            cached = summary_cache.read(self._jsonl_path)
            if cached is not None:
                # Hit. The cache key is (jsonl_size, jsonl_mtime_ns) so
                # any new turn appended to the JSONL invalidates this.
                # We don't re-verify the input slice — the splitter is
                # deterministic given file content.
                logger.info(
                    "history summary cache HIT for %s (input_messages=%d, "
                    "summary_chars=%d, generated_at=%s)",
                    self._jsonl_path.name, cached.input_message_count,
                    len(cached.summary_text), cached.generated_at,
                )
                summary = cached.summary_text
            else:
                logger.info(
                    "history summary cache MISS for %s — recomputing "
                    "(input_messages=%d, target_words=%s)",
                    self._jsonl_path.name, len(older), target_words,
                )
                summary = await self._summarize_history(
                    older, target_words=target_words
                )
                # Write through. Failures are logged inside summary_cache
                # and never propagate — at worst the next call recomputes.
                if summary:
                    summary_cache.write(
                        self._jsonl_path,
                        summary_text=summary,
                        input_message_count=len(older),
                        summary_target_words=target_words,
                        summarizer_model=getattr(
                            self._config, "summarizer_model", None
                        ),
                    )

        self._history_summary = summary
        return recent, summary

    async def refresh_summary_cache_if_stale(self) -> bool:
        """Compute and persist the history summary for the current JSONL
        state, but only if the cache is missing or stale.

        Safe to call from anywhere — short-circuits cheaply on cache hit.
        Returns True if a new summary was written, False otherwise.

        Used by:
        - Session stop (write the summary the next reopen will need)
        - Background safety nets (boot warmup, history reopen)
        """
        if self._jsonl_path is None or not self._jsonl_path.is_file():
            return False
        if summary_cache.is_fresh(self._jsonl_path):
            return False

        try:
            history = HistoryLoader(self._jsonl_path).load()
        except Exception:  # noqa: BLE001
            logger.exception("summary refresh: history load failed")
            return False
        if not history:
            return False

        clipped = truncate_tool_results(history)
        older, _ = split_by_token_budget(clipped, RECENT_VERBATIM_TOKENS)
        if not older:
            return False

        prefix_tokens = sum(estimate_message_tokens(m) for m in older)
        target_words = summary_target_word_range(len(older), prefix_tokens)
        try:
            summary = await self._summarize_history(
                older, target_words=target_words
            )
        except Exception:  # noqa: BLE001
            logger.exception("summary refresh: _summarize_history failed")
            return False
        if not summary:
            return False

        summary_cache.write(
            self._jsonl_path,
            summary_text=summary,
            input_message_count=len(older),
            summary_target_words=target_words,
            summarizer_model=getattr(self._config, "summarizer_model", None),
        )
        return True

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
        session_update: dict[str, Any] | None = None,
    ) -> None:
        """Open the upstream provider WS for non-WebRTC voice providers.

        The handler layer wires the two callbacks so audio chunks become
        ``voice_audio_out`` payloads on the orchestrator broadcast and
        provider events are mirrored to subscribers.

        ``session_update`` may be passed in if the caller already built it
        (e.g. to send to the frontend in the same handler) — saves a second
        round-trip through the LLM-backed history summarizer, which on the
        Jetson costs enough to blow the frontend's 10s start timeout.

        Drives the lifecycle from IDLE → STARTING → ACTIVE. Bails early
        with no side effects if the session is already past STARTING
        (i.e. a teardown raced ahead).
        """
        if not self.needs_voice_relay:
            return

        # Move IDLE → STARTING under the lock. If the session has already
        # progressed beyond STARTING (concurrent end_voice, or a redundant
        # rebuild from _attach_voice_payload's reconnect path), bail out
        # without touching the relay.
        async with self._voice_lock:
            if self._voice_state == VoiceLifecycle.STARTING:
                # Another caller is already opening the relay; let it win.
                return
            if self._voice_state == VoiceLifecycle.ACTIVE:
                # Already up — idempotent.
                return
            if self._voice_state in (VoiceLifecycle.ENDING, VoiceLifecycle.ENDED):
                logger.info(
                    "start_voice_relay skipped — session %s is %s",
                    self._local_id, self._voice_state.value,
                )
                return
            self._set_voice_state_unlocked(VoiceLifecycle.STARTING)

        from orchestrator.voice_relay import VoiceRelay
        if session_update is None:
            session_update = await self.get_session_update()
        if session_update is None:
            raise RuntimeError("Voice provider did not produce a session.update payload")

        async def _rebuild_session_update() -> dict[str, Any]:
            # Used by the relay to recover from DashScope's misleading
            # "InvalidParameter" close mid-session.  Rebuilds with the
            # current history so the model picks up where it left off.
            payload = await self.get_session_update()
            if payload is None:
                raise RuntimeError("rebuild_session_update returned None")
            return payload

        relay = VoiceRelay(
            self._voice_provider,
            on_audio_out=on_audio_out,
            on_event_for_frontend=on_event_for_frontend,
            session_id=self._local_id,
            rebuild_session_update=_rebuild_session_update,
        )
        try:
            await relay.start(session_update)
        except Exception:
            # Relay failed to open. Roll the state forward to ENDED so a
            # retry creates a fresh session instead of seeing STARTING.
            async with self._voice_lock:
                if self._voice_state == VoiceLifecycle.STARTING:
                    self._set_voice_state_unlocked(VoiceLifecycle.ENDING)
                    self._set_voice_state_unlocked(VoiceLifecycle.ENDED)
                    self._voice_end_reason = "start_failed"
                    self._voice_ended.set()
            raise

        async with self._voice_lock:
            self._voice_relay = relay
            # If a teardown snuck in between relay.start() and here, honour
            # it: don't transition to ACTIVE, let the ENDING path catch up.
            if self._voice_state == VoiceLifecycle.STARTING:
                self._set_voice_state_unlocked(VoiceLifecycle.ACTIVE)

    async def stop_voice_relay(self) -> None:
        """Low-level relay teardown — closes the upstream WS only.

        Does NOT touch the lifecycle state machine. Used for intra-session
        relay rebuilds (e.g. ``_attach_voice_payload`` detecting a dead
        relay on reconnect) and as a building block for :meth:`end_voice`.

        For the full session-level teardown — provider release, audio
        recorder stop, broadcast of ``voice_ended`` — call :meth:`end_voice`.
        """
        if self._voice_relay is not None:
            await self._voice_relay.stop()
            self._voice_relay = None

    def _set_voice_state_unlocked(self, target: VoiceLifecycle) -> None:
        """Internal: transition state. Caller MUST hold ``_voice_lock``.

        Validates against :data:`_VALID_VOICE_TRANSITIONS`. Invalid
        transitions raise — they indicate a bug, not a recoverable race.
        """
        current = self._voice_state
        if target == current:
            return
        if target not in _VALID_VOICE_TRANSITIONS[current]:
            raise RuntimeError(
                f"Invalid voice transition {current.value} → {target.value} "
                f"for session {self._local_id}"
            )
        logger.info(
            "voice_state session=%s %s → %s",
            self._local_id, current.value, target.value,
        )
        self._voice_state = target

    async def end_voice(self, reason: str) -> None:
        """Tear the voice session down cleanly. The ONE canonical end path.

        Idempotent: a second call observes ENDED and returns immediately.
        Concurrent calls piggy-back: the second caller awaits the first's
        :attr:`_voice_ended` event and then returns.

        Sequence:
            1. Acquire lock; short-circuit if already ENDING/ENDED.
            2. Transition → ENDING.
            3. Broadcast ``voice_ending`` so the UI can show a transient state.
            4. Best-effort send of the provider's ``graceful_shutdown_frames``
               (Qwen: commit; Gemini: activityEnd; OpenAI: nothing).
            5. Close the relay (cancels drain/keepalive, closes upstream WS).
            6. Release the audio recorder and clear the provider.
            7. Transition → ENDED; broadcast ``voice_ended``; set the event.

        :param reason: One of ``user_stop``, ``agent_end``, ``client_disconnect``,
            ``error``, ``shutdown`` — surfaced in the broadcast for telemetry.
        """
        # Fast path: no voice session ever started — nothing to tear down,
        # no broadcasts, no state churn. Text-mode ``stop()`` calls land
        # here and exit immediately.
        if self._voice_state == VoiceLifecycle.IDLE:
            return
        # Already ended — second caller observes the terminal state.
        if self._voice_state == VoiceLifecycle.ENDED:
            return
        # Piggy-back on an in-flight teardown without re-running it.
        if self._voice_state == VoiceLifecycle.ENDING:
            await self._voice_ended.wait()
            return

        async with self._voice_lock:
            # Re-check under the lock.
            if self._voice_state == VoiceLifecycle.ENDED:
                return
            if self._voice_state == VoiceLifecycle.ENDING:
                # Released the lock to a concurrent caller mid-teardown;
                # wait for it to finish outside the lock below.
                pass
            else:
                self._voice_end_reason = reason
                self._set_voice_state_unlocked(VoiceLifecycle.ENDING)

        if self._voice_state != VoiceLifecycle.ENDING:
            # Concurrent finalisation happened — just wait for it.
            await self._voice_ended.wait()
            return

        await self._broadcast_voice_lifecycle("voice_ending", reason)

        # 1. Best-effort graceful shutdown frames (provider-specific). The
        #    relay does the actual send so it can pace through the provider's
        #    normal write path. Bounded by _GRACEFUL_SHUTDOWN_TIMEOUT_S.
        if self._voice_relay is not None and self._voice_provider is not None:
            try:
                frames = self._voice_provider.graceful_shutdown_frames()
            except Exception:  # noqa: BLE001
                logger.exception(
                    "graceful_shutdown_frames raised for provider %s",
                    self._voice_provider.provider_name,
                )
                frames = []
            if frames:
                try:
                    await asyncio.wait_for(
                        self._voice_relay.send_shutdown_frames(frames),
                        timeout=_GRACEFUL_SHUTDOWN_TIMEOUT_S,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "graceful_shutdown_frames timed out after %.1fs for session %s",
                        _GRACEFUL_SHUTDOWN_TIMEOUT_S, self._local_id,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "graceful_shutdown_frames send failed for session %s",
                        self._local_id,
                    )

        # 2. Close the relay (cancels drain/keepalive, closes upstream WS).
        try:
            await self.stop_voice_relay()
        except Exception:  # noqa: BLE001
            logger.exception("stop_voice_relay raised during end_voice")

        # 3. Release the audio recorder.
        if self._audio_recorder is not None:
            try:
                self._audio_recorder.stop()
            except Exception:  # noqa: BLE001
                logger.exception("audio recorder stop raised during end_voice")
            self._audio_recorder = None

        # 4. Release the provider handle. The session object itself stays;
        #    callers that want the whole session gone follow up with
        #    pool.stop_orchestrator().
        self._voice_provider = None

        async with self._voice_lock:
            if self._voice_state == VoiceLifecycle.ENDING:
                self._set_voice_state_unlocked(VoiceLifecycle.ENDED)
        self._voice_ended.set()

        await self._broadcast_voice_lifecycle("voice_ended", reason)

    async def _broadcast_voice_lifecycle(self, event_type: str, reason: str) -> None:
        """Push a ``voice_ending`` / ``voice_ended`` event to all subscribers.

        Swallow errors: the lifecycle must not depend on the broadcast
        succeeding (subscribers may already be gone if the trigger was
        ``client_disconnect``).
        """
        pool = self._context.get("pool")
        if pool is None:
            return
        try:
            await pool.broadcast_orchestrator({
                "type": event_type,
                "reason": reason,
                "session_id": self._local_id,
            })
        except Exception:  # noqa: BLE001
            logger.exception(
                "broadcast of %s failed for session %s",
                event_type, self._local_id,
            )

    async def send_voice_audio_in(self, pcm_b64: str) -> None:
        """Forward a frontend mic chunk upstream (WS providers only).

        While an injection window is active (``listen_recording`` is
        replaying past audio into the WS), do NOT also write the chunk into
        the audio recorder — the injected bytes are not the user's
        microphone, and persisting them would corrupt the recording with
        material that already exists elsewhere.
        """
        if self._voice_relay is None:
            return
        if (
            not self.is_injecting
            and self._audio_recorder is not None
            and self._audio_recorder.is_recording
        ):
            self._audio_recorder.write_user_audio(pcm_b64)
        await self._voice_relay.send_audio(pcm_b64)

    @property
    def is_injecting(self) -> bool:
        """True while listen_recording is replaying past audio into the WS.

        Three call sites consult this flag, all backend-side:
        - ``send_voice_audio_in`` skips writing the chunk into the audio
          recorder (the bytes are a replay, not the user's mic).
        - ``process_voice_event`` skips persisting voice_transcription /
          voice_interrupted JSONL entries fired by the provider's VAD/ASR
          chewing on the injected audio.
        - The route-layer mirror (``_on_event_for_frontend``) skips
          forwarding the corresponding ``input_audio_buffer.*`` and
          ``transcription.completed`` events to the frontend, so the UI
          never tries to barge-in on a replay it didn't actually hear.
        """
        return self._injection_active

    def extend_injection_window(self, seconds: float) -> None:
        """Mark the injection window as active for at least ``seconds`` more.

        Idempotent and safe to call repeatedly: the deadline only ever
        moves forward.  A single watchdog task watches the deadline and
        clears the flag when it expires; calling
        ``extend_injection_window`` again from the tool keeps pushing the
        deadline out, so a long playback (or one with a generous
        ASR-completion grace window) stays suppressed.

        Must be called from inside the asyncio loop.
        """
        loop = asyncio.get_running_loop()
        new_deadline = loop.time() + max(0.1, seconds)
        if new_deadline > self._injection_until:
            self._injection_until = new_deadline
        if not self._injection_active:
            self._injection_active = True
        if self._injection_watchdog is None or self._injection_watchdog.done():
            self._injection_watchdog = asyncio.create_task(
                self._injection_watchdog_loop(),
                name="voice-injection-watchdog",
            )

    async def _injection_watchdog_loop(self) -> None:
        """Sleep until the injection deadline, then clear the flag.

        Re-checks the deadline on each wake so an extension done while we
        were sleeping just reschedules the next check rather than ending
        suppression early.
        """
        try:
            loop = asyncio.get_running_loop()
            while True:
                remaining = self._injection_until - loop.time()
                if remaining <= 0:
                    break
                await asyncio.sleep(remaining)
            self._injection_active = False
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("injection watchdog crashed")
            # Fail closed: clear the flag so suppression doesn't leak.
            self._injection_active = False

    def record_assistant_audio(self, pcm_b64: str) -> None:
        """Record assistant audio chunk (called from route layer for WS providers)."""
        if self._audio_recorder is not None and self._audio_recorder.is_recording:
            self._audio_recorder.write_assistant_audio(pcm_b64)

    def _flush_pending_user_transcript(self, transcript: str) -> None:
        """Write the buffered user transcript as a single JSONL entry.

        Used by the Gemini Live event path where ``inputTranscription``
        arrives as token-level deltas across many events. The buffer is
        cleared after writing.
        """
        segment = None
        if self._audio_recorder is not None and self._audio_recorder.is_recording:
            segment = self._audio_recorder.mark_user_turn_end(transcript)
        if segment:
            content = (
                f"[voice, recording: {self._local_id} "
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
        self._pending_user_transcript = None

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

        # --- Gemini Live event shape -----------------------------------
        # Gemini Live doesn't carry a top-level ``type`` field — it uses
        # camelCase top-level keys (``setupComplete``, ``serverContent``,
        # ``toolCall``, ``toolResponse``) and nests transcription /
        # turn-complete / interruption signals under ``serverContent``.
        # Translate to the same persistence behaviour the OpenAI / Qwen
        # branches below provide.
        if not event_type and self._voice_provider.provider_name == "google":
            sc = event.get("serverContent") or {}
            input_t = sc.get("inputTranscription") if isinstance(sc, dict) else None
            output_t = sc.get("outputTranscription") if isinstance(sc, dict) else None
            tool_call = event.get("toolCall")

            # User speech transcript — Gemini Live streams inputTranscription
            # as token-level deltas (one event per word/fragment). Buffer and
            # persist a single JSONL entry on turn boundary, mirroring the
            # assistant-side accumulation below. Without this every fragment
            # became its own "[voice] my" / "[voice] friend" user turn,
            # corrupting both UI and history.
            if isinstance(input_t, dict) and not self.is_injecting:
                delta = input_t.get("text", "")
                if delta:
                    staged = getattr(self, "_pending_user_transcript", None) or ""
                    self._pending_user_transcript = staged + delta

            # Assistant transcript delta — accumulate; persist on turnComplete.
            # Flushing the *user* transcript here too: when the model starts
            # replying, the user's turn is by definition over, so the buffered
            # user fragments form one coherent utterance.
            if isinstance(output_t, dict):
                # Flush staged user transcript on the first output delta of
                # this turn (model started speaking → user turn ended).
                staged_user = getattr(self, "_pending_user_transcript", None)
                if staged_user and not self.is_injecting:
                    self._flush_pending_user_transcript(staged_user)
                delta = output_t.get("text", "")
                if delta:
                    staged = getattr(self, "_pending_assistant_transcript", None) or ""
                    self._pending_assistant_transcript = staged + delta

            # Turn complete — persist staged transcripts.
            if isinstance(sc, dict) and sc.get("turnComplete"):
                # Failsafe flush of user transcript: covers turns where the
                # model produced no text output (audio-only modality) so the
                # outputTranscription branch above never fired.
                staged_user = getattr(self, "_pending_user_transcript", None)
                if staged_user and not self.is_injecting:
                    self._flush_pending_user_transcript(staged_user)
                staged = getattr(self, "_pending_assistant_transcript", None)
                if staged:
                    segment = None
                    if self._audio_recorder is not None and self._audio_recorder.is_recording:
                        segment = self._audio_recorder.mark_assistant_turn_end(staged)
                    if segment:
                        content = (
                            f"[voice, recording: {self._local_id} "
                            f"{segment['start_ms']}-{segment['end_ms']}ms] {staged}"
                        )
                    else:
                        content = staged
                    entry = {
                        "type": "assistant",
                        "message": {"role": "assistant", "content": content},
                        "source": "voice_response",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    if segment:
                        entry["audio_segment"] = segment
                    self._writer.append(entry)
                self._pending_assistant_transcript = None

            # Interrupted — mark in JSONL like OpenAI's speech_started.
            if isinstance(sc, dict) and sc.get("interrupted") and not self.is_injecting:
                self._writer.append({
                    "type": "voice_interrupted",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

            # Tool call — execute synchronously, ship the toolResponse back.
            if isinstance(tool_call, dict):
                for call in tool_call.get("functionCalls", []):
                    call_id = call.get("id", "")
                    name = call.get("name", "")
                    args = call.get("args", {}) or {}
                    if not (call_id and name):
                        continue
                    result = await registry.execute(name, args, self._context)
                    self._writer.append({
                        "type": "tool_use",
                        "tool_call_id": call_id,
                        "tool_name": name,
                        "tool_input": args,
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
                    commands.extend(self._voice_provider.format_tool_result(call_id, result))

            return commands

        # --- OpenAI / Qwen event shape (uses top-level ``type``) -------

        # User speech transcript — arrives when Whisper transcription completes.
        # Skip while listen_recording is replaying past audio into the WS:
        # the provider's ASR is transcribing those bytes and firing this
        # event for each fragment, and persisting them as user turns would
        # corrupt history with phantom messages that the user never spoke.
        if (
            event_type == "conversation.item.input_audio_transcription.completed"
            and not self.is_injecting
        ):
            transcript = event.get("transcript", "")
            if transcript:
                # Build message content with optional audio segment reference
                segment = None
                if self._audio_recorder is not None and self._audio_recorder.is_recording:
                    segment = self._audio_recorder.mark_user_turn_end(transcript)

                if segment:
                    # Include audio reference in the message text itself.
                    # Wall-clock range — pass straight to listen_recording.
                    content = (
                        f"[voice, recording: {self._local_id} "
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
        # GA OpenAI gpt-realtime emits ``response.output_audio_transcript.done``;
        # legacy beta models and Qwen still emit ``response.audio_transcript.done``.
        elif event_type in (
            "response.output_audio_transcript.done",
            "response.audio_transcript.done",
        ):
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
                    # Include audio reference in the message text itself.
                    # Wall-clock range — pass straight to listen_recording.
                    content = (
                        f"[voice, recording: {self._local_id} "
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

        # Barge-in interruption — mark in JSONL.  While injecting past
        # audio via listen_recording, the provider's VAD fires this event
        # for every chunk it detects in the replay; those are not real
        # interruptions and must not be persisted.
        elif event_type == "input_audio_buffer.speech_started":
            if not self.is_injecting:
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

        # Audio mode is OpenAI-only — import lazily so a Qwen-only install
        # can still import this module.
        try:
            from orchestrator.providers.openai_text import (
                OpenAITextProvider, create_audio_message,
            )
        except ImportError as e:
            raise RuntimeError(
                "Audio mode requires the `openai` package. "
                "Install it with: pip install -r requirements-openai.txt"
            ) from e

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

    async def stop(self, reason: str = "shutdown") -> None:
        """Clean up the session.

        Cancels every in-flight background-agent turn (with SDK interrupts so
        the bundled ``claude`` subprocesses actually stop) and unsubscribes
        the wake callback so notifications fired during shutdown go nowhere.

        Voice teardown is delegated to :meth:`end_voice` so the canonical
        lifecycle path runs (graceful shutdown frames, broadcasts, state
        machine). Idempotent — calling ``stop`` after ``end_voice`` is fine.

        Also kicks off a fire-and-forget history-summary refresh: the
        conversation just ended, so this is the cheapest moment to pay
        for the LLM call — by the time the user comes back to this
        session, the cache will be warm and ``voice_start`` won't have
        to block on summarisation.

        :param reason: Forwarded to :meth:`end_voice` if voice is active.
            Defaults to ``shutdown``; the pool override path uses ``user_stop``.
        """
        self._notifications.set_wake_callback(None)
        if self._runner is not None:
            try:
                await self._runner.cancel_all()
            except Exception:  # noqa: BLE001
                logger.exception("BackgroundAgentRunner.cancel_all failed during stop")
        # Funnel voice teardown through the canonical path so the state
        # machine and broadcasts stay coherent. ``end_voice`` is idempotent
        # and handles the no-voice case (state == IDLE) implicitly.
        if self._voice:
            try:
                await self.end_voice(reason)
            except Exception:  # noqa: BLE001
                logger.exception("end_voice failed during session stop")
        # Spawn the cache-refresh as a detached task. We don't await
        # because the session is shutting down and the JSONL is already
        # written; the refresh just makes the next reopen faster.
        if self._jsonl_path is not None and self._jsonl_path.is_file():
            asyncio.create_task(
                self.refresh_summary_cache_if_stale(),
                name=f"summary-refresh-{self._local_id}",
            )
        # Cancel the injection watchdog if it's still scheduled
        if self._injection_watchdog is not None and not self._injection_watchdog.done():
            self._injection_watchdog.cancel()
            try:
                await self._injection_watchdog
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._injection_watchdog = None
        self._injection_active = False
        self._injection_until = 0.0
        # Audio recorder + voice_provider were already released by end_voice
        # for voice sessions; clear here for non-voice sessions too (no-op
        # if end_voice already ran).
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
        target_words: tuple[int, int] | None = None,
    ) -> str:
        """Summarize older conversation messages into a rich digest.

        The digest is what the voice agent reads at the start of a session to
        remember the earlier conversation, so it must be both comprehensive
        (every major arc) and faithful (every user utterance represented in
        short form). The API call is *uncapped* — the model writes as much as
        it needs.  ``target_words`` is a soft steering range (min, max)
        injected into the system prompt to nudge the model toward an
        appropriate length for this conversation size; the model is explicitly
        told it may go over rather than drop information.

        The summarizer model is configurable via ``summarizer_model`` in
        ``assistant_config.json`` so a fast frontier model can compress the
        transcript even when the active text/voice model is a realtime model
        that can't reliably hold the role of summarizer.
        """
        if not messages:
            return ""

        # Build a transcript for the summarizer. Generous per-message clips
        # so long-form turns survive into the digest input.
        lines: list[str] = []
        for msg in messages:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            label = "USER" if role == "user" else "ASSISTANT"
            if isinstance(content, str):
                lines.append(f"{label}: {content.strip()[:4000]}")
            elif isinstance(content, list):
                parts: list[str] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        parts.append(block.get("text", "").strip()[:4000])
                    elif btype == "tool_use":
                        parts.append(f"[tool: {block.get('name', '?')}]")
                    elif btype == "tool_result":
                        rc = block.get("content", "")
                        if isinstance(rc, list):
                            rc = " ".join(
                                b.get("text", "") for b in rc
                                if isinstance(b, dict) and b.get("type") == "text"
                            )
                        parts.append(f"[tool result: {str(rc)[:1200]}]")
                if parts:
                    lines.append(f"{label}: {' '.join(parts)}")

        transcript = "\n".join(lines)

        if target_words is None:
            # Fallback when called without a pre-computed range (e.g. the
            # manual /compact path).  Compute on the fly from the transcript.
            from orchestrator.token_budget import (
                estimate_tokens as _est,
                summary_target_word_range as _range,
            )
            target_words = _range(len(messages), _est(transcript))

        min_words, max_words = target_words
        length_hint = (
            f"## Target length\n"
            f"Aim for roughly **{min_words}–{max_words} words**. This is a "
            f"steering range, not a hard limit — if covering every user "
            f"message and every topic faithfully needs more space, USE MORE. "
            f"Never drop content to fit a target. If the conversation is "
            f"short and the range feels too long, write less.\n\n"
        )

        system = (
            "You are a conversation summarizer. You receive a transcript of a "
            "past conversation between a user and an AI assistant, wrapped in "
            "<transcript>...</transcript> tags, and you produce a structured "
            "digest the assistant will read later to remember the conversation.\n\n"
            "Hard rules:\n"
            "- DO NOT continue the conversation.\n"
            "- DO NOT roleplay as the assistant or address the user.\n"
            "- DO NOT answer any questions in the transcript — they're history.\n"
            "- DO NOT omit any user message or topic to save space — go over "
            "the length target if you have to.\n"
            "- Output only the digest, in the format below.\n\n"
            + length_hint +
            "Format — include every section that has any content, in this order:\n\n"
            "## Conversation arc\n"
            "A short narrative (3–8 sentences) describing how the conversation "
            "unfolded chronologically — what was discussed first, how it "
            "evolved, what the current focus is. Capture the *feel* of the "
            "conversation (exploratory, debugging, deep technical dive, casual "
            "catch-up, etc.).\n\n"
            "## Topics covered\n"
            "Every distinct topic that came up, in roughly chronological order. "
            "One bullet per topic with a 1–2 sentence description of what was "
            "discussed and any outcome. Don't merge unrelated topics.\n\n"
            "## Every user message (short)\n"
            "A faithful list of every user utterance from the transcript, in "
            "order, in short form. One bullet per user line. Compress long "
            "messages to their essential ask/statement (≤25 words) but DO NOT "
            "drop any — even greetings, asides, and one-word replies. This "
            "section is what lets the assistant reconstruct the conversation "
            "beat-by-beat. Use the user's own wording when possible.\n\n"
            "## Decisions & conclusions\n"
            "Concrete choices made, trade-offs accepted, conclusions reached. "
            "One bullet each, specific.\n\n"
            "## Open threads\n"
            "Unresolved questions, things the user wanted to come back to, "
            "work in progress. One bullet each.\n\n"
            "## Key entities\n"
            "Files, sessions, projects, people, tools, URLs, model names, "
            "etc. that matter for continuity. One bullet each with a brief "
            "note on what role they played.\n\n"
            "## User preferences & context\n"
            "How the user wants the assistant to behave, tone, constraints, "
            "stated preferences, personal context revealed.\n\n"
            "Style:\n"
            "- Be factual and specific. Use concrete names, not generic phrases.\n"
            "- Omit filler but keep the user's voice.\n"
            "- Write in the same language(s) the conversation used.\n"
            "- If a section has no content, omit it entirely (don't write 'N/A').\n\n"
            "## Handling voice transcription errors\n"
            "Parts of the transcript come from voice ASR and may contain "
            "mistranscriptions of technical terms — model names, product names, "
            "library names, project names, code identifiers. Before reifying any "
            "unfamiliar proper noun as a real entity:\n"
            "1. **Try to recover the intended word from context** — earlier or "
            "later in the same transcript the user often uses the correct term, "
            "or a sibling term that makes the intent obvious (e.g. an "
            "ASR-rendered 'Quanticore' near repeated mentions of 'Qwen' and "
            "'local models' almost certainly means 'Qwen Code'). When you're "
            "confident from context, use the corrected term and add a short "
            "parenthetical note like `Qwen Code (ASR rendered as \"Quanticore\")`.\n"
            "2. **If context doesn't resolve it**, flag the term with `(?ASR)` "
            "rather than treating it as authoritative — e.g. "
            "`Foobarinator (?ASR) — referenced as a local model runner`. Don't "
            "invent a description for an entity you can't verify; say it was "
            "mentioned and you couldn't recover the canonical name.\n"
            "Do this in the Key entities section and anywhere else a malformed "
            "term would otherwise propagate as fact."
        )

        user_msg = (
            "Summarize the transcript below according to your instructions.\n\n"
            f"<transcript>\n{transcript}\n</transcript>\n\n"
            "Produce only the structured digest. Do not greet, do not reply, "
            "do not continue the conversation. Cover every user message in the "
            '"Every user message (short)" section — do not skip any. If you '
            "need more than the target length to do that faithfully, use it."
        )

        model, provider = self._resolve_summarizer_model()
        try:
            if provider == Provider.OPENAI:
                import openai
                client = openai.AsyncOpenAI()
                # GPT-5 family and reasoning models (o1/o3/o4) reject custom
                # ``temperature``; everything else takes it.  Both
                # ``max_tokens`` and ``max_completion_tokens`` are omitted on
                # purpose so the model writes as much as the digest requires.
                mid = model.lower()
                is_reasoning = (
                    mid.startswith("gpt-5")
                    or mid.startswith("o1")
                    or mid.startswith("o3")
                    or mid.startswith("o4")
                )
                kwargs: dict[str, Any] = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_msg},
                    ],
                }
                if not is_reasoning:
                    kwargs["temperature"] = 0.3
                response = await client.chat.completions.create(**kwargs)
                choice = response.choices[0] if response.choices else None
                text = (choice.message.content or "") if choice else ""
                if choice and choice.finish_reason == "length":
                    logger.warning(
                        "Summarizer hit the model's own output ceiling "
                        "(model=%s, completion_tokens=%s) — digest may be "
                        "incomplete. Consider switching to a model with a "
                        "larger output cap.",
                        model,
                        getattr(response.usage, "completion_tokens", "?"),
                    )
                return text
            else:
                import anthropic
                client = anthropic.AsyncAnthropic()
                # Anthropic requires ``max_tokens``; pass the largest value
                # the SDK will accept so it doesn't cap us short.  Sonnet 4.6
                # supports up to 64k output tokens.
                response = await client.messages.create(
                    model=model,
                    max_tokens=64_000,
                    system=system,
                    messages=[{"role": "user", "content": user_msg}],
                )
                if response.stop_reason == "max_tokens":
                    logger.warning(
                        "Summarizer hit Anthropic's max_tokens cap of 64k "
                        "(model=%s) — digest may be incomplete.",
                        model,
                    )
                return response.content[0].text if response.content else ""
        except Exception as e:
            logger.warning("Failed to summarize history with %s/%s: %s", provider.value, model, e)
            return ""

    def _resolve_summarizer_model(self) -> tuple[str, Provider]:
        """Return (model_id, provider) for the history summarizer.

        Reads ``summarizer_model`` from ``assistant_config.json`` on every
        call so the user can change it in the Config UI without restarting
        the orchestrator. Falls back to ``DEFAULT_SUMMARIZER_MODEL`` when
        unset, and ultimately to the active text-mode model when the chosen
        summarizer model id can't be classified at all.
        """
        from orchestrator.config import _infer_model_info, get_model_info

        # Prefer the dedicated summarizer model from the live config file.
        configured: str | None = None
        try:
            from api.routes.config import _load_config as _load_app_config
            configured = (_load_app_config().get("summarizer_model") or "").strip() or None
        except Exception:
            configured = None

        candidate = configured or DEFAULT_SUMMARIZER_MODEL
        info = get_model_info(candidate) or _infer_model_info(candidate)
        if info is not None:
            return candidate, info.provider
        # Last resort: reuse the orchestrator's active model.
        return self._config.model, self._config.provider

    def _get_jsonl_path(self) -> Path:
        """Get the JSONL file path for this session.

        Uses context/ directly for portability.
        """
        sessions_dir = get_sessions_dir()
        sessions_dir.mkdir(parents=True, exist_ok=True)
        return sessions_dir / f"{self.jsonl_id}.jsonl"
