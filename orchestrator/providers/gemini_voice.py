"""Google Gemini Live realtime voice provider.

Connects to Google's `generativelanguage.googleapis.com` BidiGenerateContent
WebSocket. The protocol shape is wholly different from OpenAI / Qwen
(which mirror each other) — message names use camelCase (``setup``,
``realtimeInput``, ``serverContent``, ``toolCall``, ``toolResponse``) and
audio is shipped as ``inlineData`` parts inside ``modelTurn``.

Reference: https://ai.google.dev/api/live

Architecture vs the other providers:

- WebSocket transport (same as Qwen). The backend owns the upstream WS;
  audio flows browser → orchestrator WS → backend → Gemini WS, and back.
- Auth is an API key passed as ``?key=<KEY>`` on the WS URL. Free-tier
  dev key in ``context/.env`` is fine; productionising would route
  through Vertex AI's IAM-based auth (out of scope).
- Audio formats: 16kHz PCM in, 24kHz PCM out. The frontend's pcmPlayer
  already handles both rates — we just advertise them via
  ``get_connection_info``.
- Tool calls arrive in ``toolCall.functionCalls[]`` with an ``id`` we
  must echo back in ``toolResponse.functionResponses[].id``, plus the
  ``name`` (the orchestrator's ``format_tool_result(call_id, output)``
  doesn't pass the name through, so we track ``id → name`` internally
  during ``translate_event``).
- No DashScope-style URL-validator pathology — no payload sanitisation
  needed.

Out of scope for this provider:
- Video input (Gemini Live supports it; our frontend doesn't capture).
- Voice cloning (separate endpoint).
- Function-calling beyond the realtime audio flow (text-only Gemini is
  a separate provider class).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

import websockets

from orchestrator.providers.voice_base import BaseVoiceProvider
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

# Default model + voice (overridable via constructor).
GEMINI_VOICE_MODEL = "gemini-2.5-flash-native-audio-latest"
GEMINI_VOICE_NAME = "Puck"

# WebSocket endpoint — the API key is appended as a query param.
GEMINI_LIVE_WS = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)

# Prebuilt voice IDs Gemini Live ships today (Sept–Dec 2025 catalogue).
# The Live API doesn't expose a per-model voice list dynamically; this
# is a static catalogue used as a fallback. The dynamic-models endpoint
# attaches this same list to every Live model entry.
GEMINI_LIVE_VOICES = (
    "Puck",
    "Charon",
    "Kore",
    "Fenrir",
    "Aoede",
    "Leda",
    "Orus",
    "Zephyr",
)


class GeminiLiveVoiceProvider(BaseVoiceProvider):
    """Google Gemini Live realtime voice provider (WebSocket)."""

    def __init__(
        self,
        model: str = GEMINI_VOICE_MODEL,
        voice: str = GEMINI_VOICE_NAME,
        transcription_language: str = "",
    ) -> None:
        self._model = model
        self._voice = voice
        # Gemini Live auto-detects language from audio; this parameter
        # exists for signature parity with QwenVoiceProvider /
        # OpenAIVoiceProvider but is currently unused.
        self._transcription_language = transcription_language
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        # Tool-name lookup: Gemini Live emits ``toolCall.functionCalls[]``
        # with ``id`` and ``name`` we need to remember, because the
        # canonical ``format_tool_result(call_id, output)`` signature
        # doesn't include the tool name but Gemini's ``toolResponse``
        # demands it.
        self._pending_call_names: dict[str, str] = {}
        # Running transcript for interruption events.
        self._current_transcript: str = ""

    # --- identity ---------------------------------------------------------

    @property
    def provider_name(self) -> str:
        return "google"

    @property
    def connection_type(self) -> str:
        return "websocket"

    @property
    def model(self) -> str:
        return self._model

    @property
    def voice(self) -> str:
        return self._voice

    @property
    def transcription_language(self) -> str:
        return self._transcription_language

    @property
    def pending_calls(self) -> dict[str, str]:
        return self._pending_call_names

    # --- ingestion --------------------------------------------------------

    async def inject_event(self, raw_event: dict[str, Any]) -> None:
        await self._queue.put(raw_event)

    async def inject_audio(self, pcm_b64: str, sample_rate: int) -> None:
        """Frontend mic chunk → backend → relayed to Gemini via realtimeInput.

        The relay shapes the wire frame via :meth:`format_audio_in`; this
        method just keeps the topology-agnostic ``inject_audio`` contract.
        """
        await self.inject_event({
            "type": "_internal_audio_in_relayed",
            "audio": pcm_b64,
            "sample_rate": sample_rate,
        })

    # --- streaming --------------------------------------------------------

    async def create_message(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
    ) -> AsyncIterator[OrchestratorEvent]:
        """Drain queued provider events and yield canonical ones.

        Runs until a ``turnComplete`` or an error event is observed.
        """
        self._current_transcript = ""
        while True:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                yield ErrorEvent(error="voice_timeout", detail="No event received within 30s")
                return

            # Side effects: track tool-call names + transcript before translating.
            tool_calls = event.get("toolCall", {}).get("functionCalls", [])
            for call in tool_calls:
                cid = call.get("id", "")
                name = call.get("name", "")
                if cid and name:
                    self._pending_call_names[cid] = name

            server_content = event.get("serverContent", {})
            parts = server_content.get("modelTurn", {}).get("parts", [])
            for p in parts:
                t = p.get("text")
                if t:
                    self._current_transcript += t

            translated = self.translate_event(event)
            if translated is not None:
                yield translated

            if server_content.get("turnComplete"):
                self._current_transcript = ""
                return
            if event.get("type") == "error" or "error" in event:
                return

    def translate_event(self, raw_event: dict[str, Any]) -> OrchestratorEvent | None:
        """Translate a Gemini Live event to a canonical orchestrator event.

        Pure-ish (no transcript bookkeeping — :meth:`create_message`
        accumulates that). Returns ``None`` for events that don't map to
        anything user-visible (e.g. ``setupComplete``).
        """
        # Setup acknowledgement — nothing to surface.
        if "setupComplete" in raw_event:
            return None

        server_content = raw_event.get("serverContent")
        if server_content is not None:
            # Interrupted mid-response (user spoke over the model).
            if server_content.get("interrupted"):
                return VoiceInterrupted(partial_text=self._current_transcript)

            # Streaming text via parts[].text.
            parts = server_content.get("modelTurn", {}).get("parts", [])
            for p in parts:
                txt = p.get("text")
                if txt:
                    return TextDelta(text=txt)

            # Turn complete — emit a TurnComplete (usage isn't included
            # by the Live API in turnComplete; report zeros).
            if server_content.get("turnComplete"):
                # Some Gemini Live builds attach usage info to outputTokensDetails;
                # try opportunistically.
                usage = server_content.get("usageMetadata", {})
                return TurnComplete(
                    input_tokens=usage.get("promptTokenCount", 0),
                    output_tokens=usage.get("candidatesTokenCount", 0),
                )

        # Tool call: track id→name so format_tool_result can echo the
        # name back (Gemini's toolResponse requires it; our canonical
        # format_tool_result(call_id, output) signature doesn't pass it
        # through). Then surface the first call as ToolUseStart.
        tool_call = raw_event.get("toolCall")
        if tool_call is not None:
            calls = tool_call.get("functionCalls", [])
            first: ToolUseStart | None = None
            for call in calls:
                cid = call.get("id", "")
                name = call.get("name", "")
                args = call.get("args", {}) or {}
                if cid and name:
                    self._pending_call_names[cid] = name
                    if first is None:
                        first = ToolUseStart(
                            tool_call_id=cid,
                            tool_name=name,
                            tool_input=args,
                        )
            return first

        # Top-level error.
        if "error" in raw_event:
            err = raw_event["error"]
            if isinstance(err, dict):
                return ErrorEvent(
                    error=err.get("code", "gemini_error") if isinstance(err.get("code"), str) else "gemini_error",
                    detail=err.get("message", str(err)),
                )
            return ErrorEvent(error="gemini_error", detail=str(err))

        return None

    # --- command formatters ----------------------------------------------

    def format_tool_result(
        self,
        call_id: str,
        output: str,
    ) -> list[dict[str, Any]]:
        """Wrap a tool result in Gemini Live's ``toolResponse`` frame.

        The tool name is required by the protocol — we look it up from
        the per-session ``_pending_call_names`` map that ``translate_event``
        populated when the ``toolCall`` arrived.
        """
        name = self._pending_call_names.pop(call_id, "")
        return [{
            "toolResponse": {
                "functionResponses": [{
                    "id": call_id,
                    "name": name,
                    "response": {"output": output},
                }],
            },
        }]

    def format_session_config(
        self,
        system: str,
        tools: list[dict[str, Any]],
        voice: str | None = None,
        vad: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build the ``setup`` payload — Gemini Live's session.update equivalent.

        Sent once after the WS opens. Includes the model id, the response
        modality (we always want audio), the voice, the system prompt, and
        the available tools as function declarations.
        """
        # Tools arrive in OpenAI/Anthropic-flavoured shape (the orchestrator's
        # ToolRegistry produces both); Gemini wants {"functionDeclarations":
        # [...]}. Drop entries that don't carry a name (defensive — the
        # registry shouldn't emit such, but be permissive).
        function_declarations = []
        for t in tools or []:
            # Tool registry produces either OpenAI-style
            # {"type": "function", "function": {"name", "description", "parameters"}}
            # or Anthropic-style {"name", "description", "input_schema"}.
            if "function" in t and isinstance(t["function"], dict):
                fn = t["function"]
                function_declarations.append({
                    "name": fn.get("name"),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {}),
                })
            elif "name" in t:
                function_declarations.append({
                    "name": t.get("name"),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema") or t.get("parameters") or {},
                })

        setup: dict[str, Any] = {
            "model": f"models/{self._model}",
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {
                            "voiceName": voice or self._voice,
                        },
                    },
                },
            },
        }
        if system:
            setup["systemInstruction"] = {"parts": [{"text": system}]}
        if function_declarations:
            setup["tools"] = [{"functionDeclarations": function_declarations}]
        return {"setup": setup}

    # Back-compat alias — matches OpenAI / Qwen providers.
    def get_session_update_payload(
        self,
        system: str,
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self.format_session_config(system, tools)

    # --- relay hook overrides --------------------------------------------

    def format_audio_in(self, pcm_b64: str) -> dict[str, Any]:
        """Wrap a PCM chunk in Gemini Live's ``realtimeInput.audio`` frame."""
        return {
            "realtimeInput": {
                "audio": {
                    "data": pcm_b64,
                    "mimeType": "audio/pcm;rate=16000",
                },
            },
        }

    @classmethod
    def extract_audio_out(cls, raw_event: dict[str, Any]) -> str | None:
        """Pull base64-PCM from ``serverContent.modelTurn.parts[].inlineData.data``."""
        sc = raw_event.get("serverContent")
        if not isinstance(sc, dict):
            return None
        parts = sc.get("modelTurn", {}).get("parts", [])
        for p in parts:
            inline = p.get("inlineData") or {}
            mime = inline.get("mimeType", "")
            if mime.startswith("audio/"):
                data = inline.get("data")
                if data:
                    return data
        return None

    # No keepalive needed empirically — Gemini Live doesn't have Qwen's
    # ASR-timeout pathology. If a similar problem surfaces, override
    # build_keepalive_chunk() to return a short silent PCM chunk.
    #
    # is_recoverable_error / should_gate_event / on_inbound_event all
    # default to the base-class no-op behaviour. Gemini Live doesn't
    # reject concurrent client messages the way Qwen does, and we
    # haven't (yet) seen any close codes that benefit from automatic
    # reconnect. Add overrides here if either pattern emerges in
    # production.

    # --- connection metadata ---------------------------------------------

    async def get_connection_info(self) -> dict[str, Any]:
        """Return the metadata the frontend needs.

        Gemini Live uses long-lived API keys appended as ``?key=`` on the
        WS URL — no ephemeral exchange. The backend holds the key; the
        URL surfaced to the frontend is observability-only.
        """
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not configured")
        return {
            "connection_type": "websocket",
            "endpoint": f"{GEMINI_LIVE_WS}?model={self._model}",
            "ephemeral_token": None,
            "expires_at": None,
            "audio_in_format": {"sample_rate": 16000, "encoding": "pcm16"},
            "audio_out_format": {"sample_rate": 24000, "encoding": "pcm16"},
            "model": self._model,
            "voice": self._voice,
            "audio_relay": "backend",
        }

    # --- direct WS lifecycle (used by the relay layer) -------------------

    async def open_upstream(self) -> websockets.ClientConnection:
        """Open the upstream Gemini Live WebSocket.

        Called once per session by ``orchestrator/voice_relay.py``. The
        relay keeps the connection alive for the session's lifetime.
        """
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not configured")
        url = f"{GEMINI_LIVE_WS}?key={api_key}"
        return await websockets.connect(
            url,
            open_timeout=15,
            max_size=2**24,  # 16 MB — accommodate large audio frames
        )
