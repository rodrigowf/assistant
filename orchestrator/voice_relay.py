"""Backend WebSocket relay for non-WebRTC voice providers.

For providers with ``connection_type == "websocket"`` (Qwen, Gemini Live,
future locals), the backend owns the upstream provider WS. The frontend
talks to the orchestrator WS only; audio flows browser → orchestrator WS →
backend → provider WS, and back.

This module wires the relay:

- :class:`VoiceRelay` opens the provider WS on start, runs a background
  task that drains messages, and exposes :meth:`send_event` /
  :meth:`send_audio` for the orchestrator handler to push frontend input
  upstream.
- Inbound provider messages get split: JSON control events go to
  ``provider.inject_event()`` (so the existing ``process_voice_event``
  pipeline persists/reacts to them); audio chunks get pushed via
  ``on_audio_out`` so the orchestrator handler can broadcast a
  ``voice_audio_out`` payload to subscribed frontends.
- WebRTC providers (OpenAI) skip the relay entirely; their audio bypasses
  the backend.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any, Awaitable, Callable

from orchestrator.providers.voice_base import BaseVoiceProvider

logger = logging.getLogger(__name__)


AudioOutCallback = Callable[[str], Awaitable[None]]
EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


class VoiceRelay:
    """Owns one upstream WS to the voice provider for the lifetime of a session.

    The relay is created lazily by :class:`OrchestratorSession.start` when
    the provider's ``connection_type`` is ``"websocket"``. Lifecycle:

    1. ``await relay.start()`` — opens the upstream WS, sends the initial
       ``session.update``, and spawns the drain task.
    2. ``await relay.send_event(ev)`` / ``await relay.send_audio(b64)`` —
       called by the WS handler when the frontend sends control events or
       mic chunks.
    3. ``await relay.stop()`` — cancels the drain task and closes the WS.

    The relay does not block on any frontend operation — it only reaches
    upstream and yields events back via callbacks.
    """

    def __init__(
        self,
        provider: BaseVoiceProvider,
        *,
        on_audio_out: AudioOutCallback,
        on_event_for_frontend: EventCallback,
    ) -> None:
        if provider.connection_type != "websocket":
            raise ValueError(
                f"VoiceRelay only supports websocket providers, got {provider.connection_type!r}"
            )
        self._provider = provider
        self._on_audio_out = on_audio_out
        self._on_event_for_frontend = on_event_for_frontend

        self._ws = None  # type: ignore[assignment]
        self._drain_task: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()

    @property
    def is_running(self) -> bool:
        return self._drain_task is not None and not self._drain_task.done()

    async def start(self, session_config: dict[str, Any]) -> None:
        """Open the upstream WS and seed it with ``session.update``.

        ``session_config`` is the payload returned by
        :meth:`BaseVoiceProvider.format_session_config` — the relay sends
        it as the first message so the provider knows the system prompt,
        tools, voice, and VAD config before any audio arrives.
        """
        self._ws = await self._provider.open_upstream()
        # session.created is pushed by the server unprompted — drain it so
        # the drain task starts in a clean state.
        try:
            first = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
            first_event = json.loads(first)
            await self._provider.inject_event(first_event)
            await self._on_event_for_frontend(first_event)
        except asyncio.TimeoutError:
            logger.warning("VoiceRelay: no session.created within 10s — proceeding anyway")

        # Push our session config upstream.
        await self._ws.send(json.dumps(session_config))

        self._drain_task = asyncio.create_task(self._drain(), name=f"voice-relay-{self._provider.provider_name}")

    async def send_event(self, event: dict[str, Any]) -> None:
        """Forward a frontend control event upstream verbatim."""
        if self._ws is None:
            raise RuntimeError("VoiceRelay not started")
        await self._ws.send(json.dumps(event))

    async def send_audio(self, pcm_b64: str) -> None:
        """Forward a frontend mic chunk upstream as a provider-specific append."""
        if self._ws is None:
            raise RuntimeError("VoiceRelay not started")
        # Each provider knows the right wrapper (Qwen: input_audio_buffer.append;
        # Gemini: BidiGenerateContentRealtimeInput.audio).
        format_audio_in = getattr(self._provider, "format_audio_in", None)
        if format_audio_in is None:
            raise RuntimeError(
                f"Provider {self._provider.provider_name} does not implement format_audio_in()"
            )
        await self._ws.send(json.dumps(format_audio_in(pcm_b64)))

    async def stop(self) -> None:
        """Cancel the drain task and close the upstream WS."""
        if self._drain_task is not None and not self._drain_task.done():
            self._drain_task.cancel()
            try:
                await self._drain_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        self._closed.set()

    # --- drain loop -------------------------------------------------------

    async def _drain(self) -> None:
        """Pump upstream messages until the WS closes.

        Audio chunks (``response.audio.delta``) are forwarded to the
        frontend via ``on_audio_out``. All other JSON events are pushed
        into the provider's queue (so ``process_voice_event`` can handle
        tool execution + JSONL persistence) and also forwarded to the
        frontend so the UI can show transcripts/status.
        """
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    event = json.loads(raw)
                except Exception:
                    logger.warning("VoiceRelay: non-JSON message dropped: %r", raw[:100])
                    continue

                # Audio out → ship to frontend. The provider-specific class
                # method picks the right field name.
                extract_audio_out = getattr(type(self._provider), "extract_audio_out", None)
                if extract_audio_out is not None:
                    audio_b64 = extract_audio_out(event)
                    if audio_b64:
                        await self._on_audio_out(audio_b64)
                        # Don't broadcast the audio event itself to the
                        # frontend — only the canonical audio_out payload.
                        # But still inject for any provider-internal state.
                        await self._provider.inject_event(event)
                        continue

                # Control events → orchestrator pipeline + frontend mirror.
                await self._provider.inject_event(event)
                await self._on_event_for_frontend(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("VoiceRelay drain failed for %s", self._provider.provider_name)
            await self._on_event_for_frontend({
                "type": "error",
                "error": {"code": "voice_relay_failed", "message": "Upstream WS error"},
            })


def b64_to_pcm(b64: str) -> bytes:
    """Decode a base64 PCM chunk for diagnostics."""
    return base64.b64decode(b64)
