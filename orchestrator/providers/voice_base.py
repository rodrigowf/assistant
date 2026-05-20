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

WebSocket providers ALSO implement the relay hooks at the bottom of this
file: ``format_audio_in``, ``extract_audio_out`` (required), plus the
opt-in hooks ``is_recoverable_error``, ``should_gate_event``,
``on_inbound_event``, ``build_keepalive_chunk`` (default no-ops on the
base class). The ``VoiceRelay`` in ``orchestrator/voice_relay.py`` calls
these hooks instead of branching on the provider name — every Qwen-
specific quirk that used to be hardcoded in the relay now lives behind
one of these hooks on ``QwenVoiceProvider``.
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

    # --- relay hooks (websocket providers only) --------------------------

    def format_audio_in(self, pcm_b64: str) -> dict[str, Any]:
        """Wrap a base64-PCM chunk in the upstream provider's frame shape.

        Required for websocket providers (Qwen, Gemini Live). WebRTC
        providers don't need this since audio bypasses the backend; the
        default raises so misconfigured providers fail loudly.
        """
        raise NotImplementedError(
            f"{self.provider_name} is a websocket provider but did not "
            f"override format_audio_in()"
        )

    @property
    def audio_in_sample_rate(self) -> int | None:
        """Sample rate (Hz) of PCM the frontend ships in.

        Optional — only websocket providers that want client-side VAD
        (orchestrator/voice_vad.py) need to declare this. Returns None on
        the base; providers that know their rate override.
        """
        return None

    @classmethod
    def extract_audio_out(cls, raw_event: dict[str, Any]) -> str | None:
        """Return the base64-PCM audio payload from a provider event, or None.

        Required for websocket providers. The base implementation returns
        None — WebRTC providers (audio bypasses the backend) and any
        provider that yields a non-audio event hit the base.
        """
        return None

    def is_recoverable_error(self, exc: BaseException) -> bool:
        """Decide whether a drain-loop exception is worth a transparent reconnect.

        Default: ``False``. Providers that know their upstream sometimes
        closes with a misleading error (e.g. Qwen-Omni's
        ``InvalidParameter: The provided URL does not appear to be valid``
        boilerplate) override this to inspect ``str(exc)`` and return
        ``True``, letting the relay reopen the WS with a fresh
        ``session.update``.
        """
        return False

    def should_gate_event(self, event: dict[str, Any]) -> bool:
        """Decide whether an outbound control event must be deferred.

        Default: ``False`` (always send immediately). Providers can
        override to express in-flight constraints — e.g. Qwen-Omni
        rejects ``response.create`` while another response is active
        and closes the WS, so it gates that event until ``response.done``
        clears the active-response flag (tracked by
        :meth:`on_inbound_event`).

        When ``True`` is returned, the relay holds the event in a
        per-relay deferred queue and re-tries the most recent one after
        :meth:`on_inbound_event` reports that the gate has lifted (see
        :meth:`gate_cleared`).
        """
        return False

    def on_inbound_event(self, event: dict[str, Any]) -> None:
        """Hook called for every inbound provider event before fan-out.

        Lets the provider mutate its own gating/state machine in response
        to upstream signals — e.g. Qwen tracks ``response.created`` /
        ``response.done`` to know whether another ``response.create`` is
        safe to send. Default: no-op.

        Implementations should be cheap and side-effect-only; the relay
        passes every inbound event through this hook in order.
        """

    def gate_cleared(self) -> bool:
        """Return True if a previously-gated event can now be sent.

        Called by the relay after :meth:`on_inbound_event` runs, before
        the relay decides whether to drain its deferred-event queue.
        Default: ``True`` (no gating, so the queue can drain freely —
        though if ``should_gate_event`` never returned True there's
        nothing in the queue to begin with).
        """
        return True

    def should_close_after_event(self, event: dict[str, Any]) -> bool:
        """Decide whether the relay should proactively close the upstream WS.

        Default: ``False``. Providers can return ``True`` for inbound
        signals that *require* the client to close the connection per
        protocol — Gemini Live's ``goAway`` is the motivating case:
        Google's docs say the client must close after receiving the
        signal, and a passively-dropped connection produces a misleading
        ``1008 policy violation`` close that the user sees as a red
        error banner.

        Called by the relay right after :meth:`on_inbound_event`. When
        True, the relay closes the upstream WS with a clean 1000; the
        drain loop then enters the recoverable-error path and, if
        :meth:`is_recoverable_error` agrees, the existing reconnect
        machinery reopens with a fresh ``session.update`` (which lets
        Gemini's session-resumption handle restore in-memory context).
        """
        return False

    def build_keepalive_chunk(self) -> str | None:
        """Return a base64-PCM silent chunk to keep the upstream warm, or None.

        Default: ``None`` (no keepalive). Providers that need it (Qwen-
        Omni's ASR pipeline times out after ~3-5 min of silence) override
        to return a short silent PCM chunk, which the relay wraps with
        :meth:`format_audio_in` and ships every 30s of audio silence.

        Returning ``None`` tells the relay not to spawn the keepalive task.
        """
        return None

    @property
    def handshake_direction(self) -> str:
        """Order of the WS handshake — ``"server_first"`` or ``"client_first"``.

        - ``"server_first"`` (default, used by OpenAI / Qwen): the server
          greets with a ``session.created`` frame, THEN the client sends
          its ``session.update``.  The relay drains the first inbound
          frame before sending the config.
        - ``"client_first"`` (used by Gemini Live): the client sends the
          ``setup`` payload as the very first frame; the server then
          replies with ``setupComplete``.  The relay sends the config
          immediately and lets the drain loop handle the ack inline.
        """
        return "server_first"
