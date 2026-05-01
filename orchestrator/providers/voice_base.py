"""Abstract base for realtime voice providers.

A voice provider mediates between the canonical OrchestratorEvent stream and
a provider-specific realtime API (OpenAI Realtime over WebRTC, Gemini Live
over WebSocket, Qwen-Omni over WebSocket, etc.).

Two operating modes:

- ``connection_type == "webrtc"``: Audio streams browser↔provider directly.
  The backend only relays JSON events (mirrored from the data channel via
  ``inject_event``) and emits commands the frontend forwards back.
- ``connection_type == "websocket"``: Audio also flows backend↔provider.
  The frontend captures PCM and ships it to the backend, which forwards it
  to the provider via ``inject_audio``; provider audio is yielded as
  ``VoiceAudioDelta`` events.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from orchestrator.types import OrchestratorEvent


class BaseVoiceProvider(ABC):
    """Provider-agnostic contract for realtime voice backends.

    Subclasses must satisfy ``ModelProvider`` (so the agent loop can drive
    them) plus the additional voice-specific methods below.
    """

    # --- identity ---------------------------------------------------------

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Short identifier (``"openai"``, ``"google"``, ``"qwen"``, ...)."""

    @property
    @abstractmethod
    def connection_type(self) -> str:
        """Either ``"webrtc"`` or ``"websocket"``."""

    @property
    @abstractmethod
    def model(self) -> str:
        """Model id for this provider instance."""

    @property
    @abstractmethod
    def voice(self) -> str:
        """Voice/speaker id this provider was configured with."""

    # --- ingestion --------------------------------------------------------

    @abstractmethod
    async def inject_event(self, raw_event: dict[str, Any]) -> None:
        """Feed a raw provider event (mirrored from the frontend) into the queue."""

    async def inject_audio(self, pcm_b64: str, sample_rate: int) -> None:
        """Feed an inbound audio chunk. WebSocket providers must override.

        WebRTC providers leave the default raise — audio bypasses the backend.
        """
        raise NotImplementedError(
            f"{self.provider_name} uses {self.connection_type}; audio bypasses the backend"
        )

    # --- streaming (ModelProvider protocol) -------------------------------

    @abstractmethod
    def create_message(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
    ) -> AsyncIterator[OrchestratorEvent]:
        """Yield canonical events from queued provider events.

        Returns until a turn-complete or error event is observed.
        """

    @abstractmethod
    def translate_event(self, raw_event: dict[str, Any]) -> OrchestratorEvent | None:
        """Translate a single raw provider event to a canonical event.

        Pure function for unit testing. Returns ``None`` for events that
        don't map to anything user-visible.
        """

    # --- command formatters ----------------------------------------------

    @abstractmethod
    def format_tool_result(self, call_id: str, output: str) -> list[dict[str, Any]]:
        """Build the provider commands that submit a tool result and ask for
        the next response. Returned commands are sent back to the frontend
        as ``voice_command`` payloads to forward to the provider.
        """

    @abstractmethod
    def format_session_config(
        self,
        system: str,
        tools: list[dict[str, Any]],
        voice: str | None = None,
        vad: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build the session-configuration command (``session.update`` for
        OpenAI/Qwen, ``BidiGenerateContentSetup`` for Gemini).
        """

    # --- connection metadata ---------------------------------------------

    @abstractmethod
    async def get_connection_info(self) -> dict[str, Any]:
        """Return the metadata the frontend needs to establish the provider
        connection. Shape::

            {
                "connection_type": "webrtc" | "websocket",
                "endpoint": "https://..." | "wss://...",
                "ephemeral_token": str | None,
                "expires_at": int | None,
                "audio_in_format": {"sample_rate": int, "encoding": "pcm16"},
                "audio_out_format": {"sample_rate": int, "encoding": "pcm16"},
                "model": str,
                "voice": str,
            }
        """
