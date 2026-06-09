"""OpenAI Realtime Voice provider for the orchestrator agent.

This provider does NOT call the model directly. Instead, the frontend
establishes a WebRTC connection to the OpenAI Realtime API and mirrors all
data channel events to the backend via the orchestrator WebSocket. Those
events are injected here via :meth:`inject_event` and translated into the
canonical OrchestratorEvent stream by :meth:`create_message`.

Architecture::

    Frontend (WebRTC data channel) → orchestrator WS → inject_event()
    → create_message() → canonical OrchestratorEvent stream
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from orchestrator.providers.voice_base import BaseVoiceProvider
from orchestrator.voice_errors import VoiceError, VoiceErrorCategory
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

# Defaults — kept as module-level constants for backward compatibility.
VOICE_MODEL = "gpt-realtime"
VOICE_NAME = "cedar"

# OpenAI Realtime endpoints (current as of 2026-05).
# /v1/realtime/sessions and /v1/realtime are still alive but the canonical
# replacements are /v1/realtime/client_secrets and /v1/realtime/calls.
OPENAI_CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"
OPENAI_CALLS_URL = "https://api.openai.com/v1/realtime/calls"

# Default VAD config — server-side VAD (the frontend doesn't push-to-talk).
DEFAULT_VAD = {
    "type": "server_vad",
    "threshold": 0.5,
    "prefix_padding_ms": 300,
    "silence_duration_ms": 800,
}


class OpenAIVoiceProvider(BaseVoiceProvider):
    """OpenAI Realtime voice provider (WebRTC).

    Usage::

        provider = OpenAIVoiceProvider(model="gpt-realtime", voice="cedar")
        await provider.inject_event(event_dict)            # in WS handler
        async for ev in provider.create_message(...):      # in agent loop
            ...
    """

    def __init__(
        self,
        model: str = VOICE_MODEL,
        voice: str = VOICE_NAME,
        transcription_language: str = "",
    ) -> None:
        self._model = model
        self._voice = voice
        # OpenAI Realtime supports a `language` hint on whisper-1 transcription
        # (ISO-639-1 code).  Empty string = auto-detect.  Currently this isn't
        # exposed in the UI for OpenAI sessions; the param exists for
        # signature parity with QwenVoiceProvider.
        self._transcription_language = transcription_language
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._current_transcript: str = ""
        self._pending_calls: dict[str, str] = {}        # call_id → tool_name
        self._pending_args: dict[str, str] = {}         # call_id → args_json (accumulated)

    # --- identity ---------------------------------------------------------

    @property
    def provider_name(self) -> str:
        return "openai"

    @property
    def connection_type(self) -> str:
        return "webrtc"

    @property
    def model(self) -> str:
        return self._model

    @property
    def voice(self) -> str:
        return self._voice

    @property
    def transcription_language(self) -> str:
        """Language hint for the input ASR. Empty string = auto-detect."""
        return self._transcription_language

    @property
    def pending_calls(self) -> dict[str, str]:
        """Map of call_id → tool_name for calls awaiting results."""
        return self._pending_calls

    # --- ingestion --------------------------------------------------------

    async def inject_event(self, raw_event: dict[str, Any]) -> None:
        await self._queue.put(raw_event)

    # WebRTC: audio bypasses backend; the base class default raise applies.

    # --- streaming --------------------------------------------------------

    async def create_message(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
    ) -> AsyncIterator[OrchestratorEvent]:
        """Process queued realtime events and yield canonical events.

        Runs until ``response.done`` or an ``error`` event is observed.
        """
        self._current_transcript = ""

        while True:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                yield ErrorEvent(error="voice_timeout", detail="No event received within 30s")
                return

            event_type = event.get("type", "")

            # Side-effects: track partial transcript for interruption context
            # and stash function-call metadata before translating.
            if event_type == "response.output_item.added":
                item = event.get("item", {})
                if item.get("type") == "function_call":
                    call_id = item.get("call_id", "")
                    name = item.get("name", "")
                    if call_id and name:
                        self._pending_calls[call_id] = name
                        self._pending_args[call_id] = ""

            elif event_type == "response.function_call_arguments.delta":
                call_id = event.get("call_id", "")
                if call_id in self._pending_args:
                    self._pending_args[call_id] += event.get("delta", "")

            elif event_type in (
                "response.output_audio_transcript.delta",
                "response.audio_transcript.delta",
            ):
                self._current_transcript += event.get("delta", "")

            translated = self.translate_event(event)
            if translated is not None:
                yield translated

            if event_type == "response.done":
                self._current_transcript = ""
                return
            if event_type == "error":
                return

    def translate_event(self, raw_event: dict[str, Any]) -> OrchestratorEvent | None:
        """Translate a single OpenAI Realtime event to a canonical event.

        Pure (no side effects) — :meth:`create_message` does the bookkeeping.
        """
        event_type = raw_event.get("type", "")

        # GA gpt-realtime emits ``response.output_audio_transcript.*``;
        # legacy beta gpt-4o-realtime-preview models still emit
        # ``response.audio_transcript.*``.  Accept both so the same
        # provider keeps working across model versions.
        if event_type in (
            "response.output_audio_transcript.delta",
            "response.audio_transcript.delta",
        ):
            text = raw_event.get("delta", "")
            return TextDelta(text=text) if text else None

        if event_type in (
            "response.output_audio_transcript.done",
            "response.audio_transcript.done",
        ):
            return TextComplete(text=raw_event.get("transcript", ""))

        if event_type == "response.function_call_arguments.done":
            call_id = raw_event.get("call_id", "")
            args_str = raw_event.get("arguments") or self._pending_args.get(call_id, "{}") or "{}"
            name = self._pending_calls.get(call_id, raw_event.get("name", ""))
            try:
                tool_input = json.loads(args_str) if args_str else {}
            except Exception:
                tool_input = {}
            if call_id and name:
                return ToolUseStart(
                    tool_call_id=call_id,
                    tool_name=name,
                    tool_input=tool_input,
                )
            return None

        if event_type == "response.done":
            usage = raw_event.get("response", {}).get("usage", {})
            return TurnComplete(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
            )

        if event_type == "input_audio_buffer.speech_started":
            partial = self._current_transcript
            return VoiceInterrupted(partial_text=partial)

        if event_type == "error":
            err = raw_event.get("error", {})
            return ErrorEvent(
                error=err.get("code", "openai_error"),
                detail=err.get("message", str(err)),
            )

        return None

    # --- command formatters ----------------------------------------------

    def format_tool_result(
        self,
        call_id: str,
        output: str,
    ) -> list[dict[str, Any]]:
        """Two-command sequence: submit the result, then trigger the next response."""
        return [
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                },
            },
            {"type": "response.create"},
        ]

    def format_session_config(
        self,
        system: str,
        tools: list[dict[str, Any]],
        voice: str | None = None,
        vad: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build the OpenAI ``session.update`` payload (GA Realtime schema).

        The GA ``gpt-realtime`` model rejects the legacy beta shape
        (``modalities``, flat ``voice``/``input_audio_transcription``,
        ``turn_detection``).  It uses ``output_modalities``, an ``audio``
        object split into ``input`` and ``output`` sub-objects, and the
        session itself must declare ``type: "realtime"``.  Sending the
        legacy shape causes the model to silently keep its defaults
        (no system prompt, no tools, no input transcription) — which is
        exactly what masquerades as "voice mode is isolated from my
        architecture".  Schema verified against the ``session.created``
        echo from a live connection.
        """
        transcription: dict[str, Any] = {"model": "whisper-1"}
        if self._transcription_language:
            transcription["language"] = self._transcription_language
        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": self._model,
                "instructions": system,
                "tools": tools,
                "tool_choice": "auto",
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "transcription": transcription,
                        "turn_detection": vad or DEFAULT_VAD,
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "voice": voice or self._voice,
                    },
                },
            },
        }

    # Back-compat alias used by older session code.
    def get_session_update_payload(
        self,
        system: str,
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self.format_session_config(system, tools)

    # --- connection metadata ---------------------------------------------

    async def get_connection_info(self) -> dict[str, Any]:
        """Fetch a fresh ephemeral token from OpenAI and return WebRTC info."""
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not configured")

        payload = {
            "session": {
                "type": "realtime",
                "model": self._model,
            },
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                OPENAI_CLIENT_SECRETS_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
            data = response.json()

        return {
            "connection_type": "webrtc",
            "endpoint": f"{OPENAI_CALLS_URL}?model={self._model}",
            "ephemeral_token": data.get("value"),
            "expires_at": data.get("expires_at"),
            "audio_in_format": {"sample_rate": 24000, "encoding": "pcm16"},
            "audio_out_format": {"sample_rate": 24000, "encoding": "pcm16"},
            "model": self._model,
            "voice": self._voice,
        }

    # --- error classification ------------------------------------------

    def classify_close_reason(
        self,
        exc: BaseException | None,
        close_code: int | None,
        close_reason: str | None,
    ) -> VoiceError | None:
        """Map OpenAI Realtime error shapes onto VoiceError categories.

        OpenAI uses WebRTC — the relay's drain-loop close path doesn't
        fire here in Increment A. The classifier is still wired so:

        - The ephemeral-token endpoint (``api/routes/voice.py``) can
          route ``httpx.HTTPStatusError`` bodies through it in a future
          increment.
        - Data-channel mirrored ``error`` events that surface via
          :meth:`inject_event` can be classified the same way.

        Patterns (see plan §5):

        - body contains ``insufficient_quota`` or "exceeded your current
          quota" → QUOTA_EXCEEDED
        - body contains ``rate_limit_exceeded`` or HTTP 429 → RATE_LIMIT
        - body contains "Incorrect API key" or HTTP 401 → AUTH
        - body contains ``model_not_found`` or "Unsupported model" →
          MODEL_UNAVAILABLE
        """
        text = (close_reason or "") + " " + (str(exc) if exc is not None else "")
        lower = text.lower()

        if "insufficient_quota" in text or "exceeded your current quota" in lower:
            return VoiceError(
                category=VoiceErrorCategory.QUOTA_EXCEEDED,
                message=(
                    "Your OpenAI account has exhausted its credit "
                    "(insufficient_quota)."
                ),
                recoverable=False,
                recovery_hint=(
                    "Top up at platform.openai.com/billing, then retry."
                ),
                provider_doc_url="https://platform.openai.com/billing",
                raw_close_code=close_code,
                raw_close_reason=close_reason,
                provider=self.provider_name,
            )

        if "rate_limit_exceeded" in text or close_code == 429:
            return VoiceError(
                category=VoiceErrorCategory.RATE_LIMIT,
                message="OpenAI Realtime rate limit reached.",
                recoverable=True,
                recovery_hint=None,
                provider_doc_url=None,
                raw_close_code=close_code,
                raw_close_reason=close_reason,
                provider=self.provider_name,
            )

        if (
            "incorrect api key" in lower
            or "invalid api key" in lower
            or close_code == 401
        ):
            return VoiceError(
                category=VoiceErrorCategory.AUTH,
                message="OpenAI authentication failed (Incorrect API key).",
                recoverable=False,
                recovery_hint=(
                    "Verify OPENAI_API_KEY in context/.env is current and "
                    "the account is in good standing."
                ),
                provider_doc_url="https://platform.openai.com/api-keys",
                raw_close_code=close_code,
                raw_close_reason=close_reason,
                provider=self.provider_name,
            )

        if "model_not_found" in text or "unsupported model" in lower:
            return VoiceError(
                category=VoiceErrorCategory.MODEL_UNAVAILABLE,
                message=(
                    "This OpenAI Realtime model isn't available on your "
                    "account tier."
                ),
                recoverable=False,
                recovery_hint=(
                    "Switch to gpt-realtime or check your model access at "
                    "platform.openai.com."
                ),
                provider_doc_url="https://platform.openai.com/docs/models",
                raw_close_code=close_code,
                raw_close_reason=close_reason,
                provider=self.provider_name,
            )

        return None
