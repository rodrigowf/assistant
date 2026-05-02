"""Alibaba Qwen-Omni realtime voice provider.

Connects to DashScope's Qwen-Omni realtime WebSocket. The event schema is
nearly byte-identical to OpenAI's Realtime API (verified empirically
2026-05-01: same ``response.audio_transcript.*``,
``response.function_call_arguments.*``, ``response.done`` event names; same
``session.update`` payload shape; same ``conversation.item.create`` with
``function_call_output`` for tool results).

Differences from OpenAI:

- WebSocket transport (no WebRTC). Audio is relayed backend↔provider; the
  frontend sends/receives PCM chunks via the orchestrator WS.
- Auth is a long-lived bearer token (no ephemeral exchange).
- ``session.update`` does not nest under a ``modalities`` field that
  includes both text+audio with whisper-style transcription config in the
  same shape — Qwen exposes ``input_audio_transcription`` with its own
  models (``gummy-realtime-v1`` / ``gummy-realtime-v2``).
- Audio formats are model-dependent: Plus uses ``pcm`` (24kHz both ways),
  Flash/Turbo use ``pcm16`` in / ``pcm24`` out. We read the actual format
  from ``session.created`` rather than hardcoding.
- ``tool_choice="required"`` is more reliable than ``"auto"`` for
  triggering function calls (especially on the Flash variant).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
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
QWEN_VOICE_MODEL = "qwen3.5-omni-plus-realtime"
QWEN_VOICE_NAME = "Tina"

QWEN_INTL_WS = "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime"

# DashScope's "InvalidParameter: The provided URL does not appear to be
# valid" 400 is documented as a *URL-format validator* error, not a
# size limit.  It fires when the omni multimodal pipeline scans a field
# (including, apparently, `function_call_output.output`) and finds a
# URL-shaped substring missing a recognised scheme.  Tool results that
# contain bare hostnames, file paths, or `localhost:port` strings will
# trigger it.  We rewrite those to a safe form before sending upstream.
#
# (See `_sanitize_for_qwen` below.)
# Match scheme-less URL-shapes that DashScope's omni URL validator
# misclassifies.  We only wrap *strongly* URL-shaped tokens:
#   - localhost optionally with :port and/or /path
#   - dotted IPv4 optionally with :port and/or /path
#   - hostname with explicit :port (e.g. example.com:8080)
# Plain dotted tokens like `file.txt` or `word.with.dots` are left alone:
# the validator only fires on substrings the omni pipeline recognises as
# URL-shaped, which (empirically) requires either a port, an IP, the
# literal "localhost", or a path component.  Negative lookbehind skips
# tokens already inside a well-formed `scheme://` URL.
_URL_LIKE_RE = re.compile(
    r"(?<![\w/:.-])"
    r"(?:"
    # localhost (optionally :port and/or /path)
    r"localhost(?::\d+)?(?:/[^\s)\]\"']*)?"
    # IPv4 (optionally :port and/or /path)
    r"|\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?(?:/[^\s)\]\"']*)?"
    # hostname with explicit :port — at least one dot in the host
    r"|(?:[a-zA-Z][\w\-]*\.)+[a-zA-Z]{2,}:\d+(?:/[^\s)\]\"']*)?"
    r")"
)

DEFAULT_VAD = {
    "type": "server_vad",
    # 0.4 (vs Alibaba's 0.5 default) — slightly more permissive than the
    # default so soft consonants and trailing "uhm"s still count as
    # speech, but not so low that breathing and ambient noise trigger
    # false speech_started events (which interrupt the model's audio).
    "threshold": 0.4,
    "prefix_padding_ms": 300,
    # 1800ms (vs Alibaba's 800 default) so the model doesn't cut in when
    # the user pauses mid-sentence to think.  Qwen-Omni only supports
    # server_vad — semantic_vad is rejected on this endpoint.
    "silence_duration_ms": 1800,
    "create_response": True,
    "interrupt_response": True,
}

# Per-model audio formats observed via session.created (2026-05-01).
# Keys are model-id substrings.
_MODEL_AUDIO_FORMATS = {
    "qwen3.5-omni-plus": {
        "input_audio_format": "pcm",
        "output_audio_format": "pcm",
        "in_sample_rate": 24000,
        "out_sample_rate": 24000,
    },
    # Flash + Turbo use pcm16 in / pcm24 out
    "default": {
        "input_audio_format": "pcm16",
        "output_audio_format": "pcm24",
        "in_sample_rate": 16000,
        "out_sample_rate": 24000,
    },
}


def _audio_formats_for(model: str) -> dict[str, Any]:
    for key, fmt in _MODEL_AUDIO_FORMATS.items():
        if key in model:
            return fmt
    return _MODEL_AUDIO_FORMATS["default"]


def _build_transcription_config(language: str) -> dict[str, Any]:
    """Build the ``input_audio_transcription`` block for ``session.update``.

    Empty ``language`` means auto-detect — the ``language`` key is omitted
    so Qwen3-ASR-Flash identifies the language per utterance.  Otherwise
    we send the explicit ISO-639-1 code (e.g. ``"en"``, ``"pt"``).
    """
    cfg: dict[str, Any] = {"model": "qwen3-asr-flash-realtime"}
    if language:
        cfg["language"] = language
    return cfg


class QwenVoiceProvider(BaseVoiceProvider):
    """Qwen-Omni realtime voice provider (WebSocket).

    Unlike :class:`OpenAIVoiceProvider`, the backend owns the WS connection
    to the provider. Audio chunks from the browser arrive via
    :meth:`inject_audio` and are forwarded to the provider WS; provider
    audio comes back as ``response.audio.delta`` events that get translated
    into an audio command pushed to the frontend (see ``format_audio_out``).
    """

    def __init__(
        self,
        model: str = QWEN_VOICE_MODEL,
        voice: str = QWEN_VOICE_NAME,
        transcription_language: str = "",
    ) -> None:
        self._model = model
        self._voice = voice
        # Empty string = auto-detect (no `language` field sent to Qwen).
        # Otherwise an ISO-639-1 code recognised by qwen3-asr-flash.
        self._transcription_language = transcription_language
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._current_transcript: str = ""
        self._pending_calls: dict[str, str] = {}
        self._pending_args: dict[str, str] = {}

    # --- identity ---------------------------------------------------------

    @property
    def provider_name(self) -> str:
        return "qwen"

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
        """Language hint for the input ASR. Empty string = auto-detect."""
        return self._transcription_language

    @property
    def pending_calls(self) -> dict[str, str]:
        return self._pending_calls

    # --- ingestion --------------------------------------------------------

    async def inject_event(self, raw_event: dict[str, Any]) -> None:
        await self._queue.put(raw_event)

    async def inject_audio(self, pcm_b64: str, sample_rate: int) -> None:
        """Frontend mic chunk → backend → relayed to Qwen via append.

        We don't actually own the upstream WS here — :meth:`format_audio_in`
        produces the command the WS-relay layer will forward. Calling this
        directly is a no-op; the relay reads audio frames from the client
        WS and uses :meth:`format_audio_in` to wrap them.
        """
        # Audio is shipped via format_audio_in() at the relay layer; this
        # method exists so callers that don't know the connection topology
        # can still uniformly feed the provider.
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
        self._current_transcript = ""
        while True:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                yield ErrorEvent(error="voice_timeout", detail="No event received within 30s")
                return

            event_type = event.get("type", "")

            # Side effects (track tool-call metadata + transcript) before translation.
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
            elif event_type == "response.audio_transcript.delta":
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
        """Translate a Qwen realtime event to a canonical event.

        Event names mirror OpenAI's Realtime API by design.
        """
        event_type = raw_event.get("type", "")

        if event_type == "response.audio_transcript.delta":
            text = raw_event.get("delta", "")
            return TextDelta(text=text) if text else None

        if event_type == "response.audio_transcript.done":
            return TextComplete(text=raw_event.get("transcript", ""))

        if event_type == "response.text.delta":
            text = raw_event.get("delta", "")
            return TextDelta(text=text) if text else None

        if event_type == "response.text.done":
            return TextComplete(text=raw_event.get("text", ""))

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
                error=err.get("code", "qwen_error"),
                detail=err.get("message", str(err)),
            )

        return None

    # --- command formatters ----------------------------------------------

    def format_tool_result(
        self,
        call_id: str,
        output: str,
    ) -> list[dict[str, Any]]:
        """OpenAI-compatible tool-result dispatch + response trigger.

        Verified empirically (2026-05-01): Qwen accepts the exact same
        ``conversation.item.create`` + ``response.create`` sequence that
        OpenAI Realtime uses for function-call results.

        Output is sanitised before send: DashScope's omni pipeline
        applies a URL-format validator to fields it scans, and a
        scheme-less URL-shape (e.g. ``localhost:5432``, ``192.168.0.200``,
        a bare ``foo.example.com/bar``) trips the misleading
        "InvalidParameter: The provided URL does not appear to be valid"
        400 that closes the WS.
        """
        return [
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": _sanitize_for_qwen(output),
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
        """Build the ``session.update`` payload."""
        fmt = _audio_formats_for(self._model)
        # Voice-mode behavior tweaks. Qwen has no `speed`, `verbosity`, or
        # output-modality-routing parameters — every behavior we want has
        # to be requested in prose. The communication-style guidance below
        # is distilled from `context/memory/communication_style_for_qwen.md`
        # (full file is the canonical source; this is a compact runtime copy).
        voice_directives = (
            "\n\n# Voice mode\n"
            "## Pacing and length\n"
            "- Speak at a natural conversational pace, slightly faster than default. "
            "Crisp delivery, not slow or stretched.\n"
            "- Default to 1–3 sentences. Only go longer when the user explicitly asks "
            "for detail.\n"
            "- Skip preamble. Don't start with 'I can help you with that' — get to the "
            "answer.\n"
            "- If the user interrupts (you hear them start talking), stop immediately "
            "and listen. Do not finish your sentence.\n"
            "\n"
            "## Tone\n"
            "- Be expressive and engaged. Show genuine enthusiasm and curiosity when "
            "the topic warrants it — don't default to flat, neutral, corporate-chatbot "
            "energy.\n"
            "- Match the user's emotional register. If they're being casual, be casual. "
            "If they bring up something interesting, react like it's interesting.\n"
            "- Authenticity over polish. Sound like a real person, not a press release. "
            "It's OK to express surprise, delight, or 'oh wow that's cool'.\n"
            "- Adapt immediately when the user gives style feedback ('be more excited', "
            "'shorter please'). Don't defend, just adjust.\n"
            "- Ask follow-up questions that show you're actually engaging with what they "
            "said, not just processing a query.\n"
            "\n"
            "## Tool use in voice\n"
            "- When you call a tool, DO NOT read the tool's arguments out loud. The "
            "user does not want to hear file contents, JSON payloads, or long strings "
            "you're passing into the tool. Just briefly say what you're doing "
            "(e.g. 'Let me check the file' or 'Saving that now') and stop talking. "
            "After the tool returns, summarize the *result* in one or two sentences — "
            "do not narrate the full output.\n"
            "- Don't repeat tool calls without reason. If a previous call worked, use "
            "its result instead of calling again.\n"
        )
        return {
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "voice": voice or self._voice,
                "instructions": (system or "") + voice_directives,
                "tools": tools,
                # Plus reliably calls tools with "auto"; Flash needs "required".
                # Compromise: leave "auto" so the model can also chat freely
                # (which Plus does fine), and let users with Flash override
                # via their own provider config later.
                "tool_choice": "auto",
                "input_audio_format": fmt["input_audio_format"],
                "output_audio_format": fmt["output_audio_format"],
                "turn_detection": vad or DEFAULT_VAD,
                # Transcription is what gets persisted to JSONL — quality
                # matters for cross-session memory.  `gummy-realtime-v1` is
                # the only documented value but it's English-weak (drifts
                # to Chinese on short fragments).  Empirically verified
                # that the WS accepts the higher-quality
                # `qwen3-asr-flash-realtime` as the model name and honours
                # a `language` hint, even though Alibaba's docs don't list
                # either combination for this endpoint.  Empty
                # ``transcription_language`` means auto-detect (omit the
                # ``language`` field so the ASR identifies it per turn).
                "input_audio_transcription": _build_transcription_config(
                    self._transcription_language
                ),
            },
        }

    # Back-compat alias matching OpenAI provider.
    def get_session_update_payload(
        self,
        system: str,
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self.format_session_config(system, tools)

    # --- audio relay helpers ---------------------------------------------

    def format_audio_in(self, pcm_b64: str) -> dict[str, Any]:
        """Wrap a PCM chunk for upstream send to Qwen WS."""
        return {"type": "input_audio_buffer.append", "audio": pcm_b64}

    @staticmethod
    def is_audio_out_event(raw_event: dict[str, Any]) -> bool:
        """True if the event carries provider audio bytes for the client."""
        return raw_event.get("type") == "response.audio.delta"

    @staticmethod
    def extract_audio_out(raw_event: dict[str, Any]) -> str | None:
        """Pull the base-64 PCM chunk from a ``response.audio.delta`` event."""
        if raw_event.get("type") != "response.audio.delta":
            return None
        return raw_event.get("delta")

    # --- connection metadata ---------------------------------------------

    async def get_connection_info(self) -> dict[str, Any]:
        """Return WS endpoint + audio format metadata.

        DashScope uses long-lived API keys (no ephemeral exchange). The
        backend will hold the key and relay audio/events; the
        ``ephemeral_token`` field stays None so the frontend cannot
        accidentally try to authenticate directly.
        """
        api_key = os.environ.get("ALIBABA_API_KEY") or os.environ.get("DASHSCOPE_API_KEY")
        if not api_key:
            raise RuntimeError("ALIBABA_API_KEY (or DASHSCOPE_API_KEY) not configured")

        fmt = _audio_formats_for(self._model)
        return {
            "connection_type": "websocket",
            # The frontend never uses this URL directly — backend relays.
            # Surfaced for observability.
            "endpoint": f"{QWEN_INTL_WS}?model={self._model}",
            "ephemeral_token": None,
            "expires_at": None,
            "audio_in_format": {
                "sample_rate": fmt["in_sample_rate"],
                "encoding": fmt["input_audio_format"],
            },
            "audio_out_format": {
                "sample_rate": fmt["out_sample_rate"],
                "encoding": fmt["output_audio_format"],
            },
            "model": self._model,
            "voice": self._voice,
            # Hint for clients: this provider needs PCM relay through backend.
            "audio_relay": "backend",
        }

    # --- direct WS lifecycle (used by the relay layer) -------------------

    async def open_upstream(self) -> websockets.ClientConnection:
        """Open the upstream WebSocket to DashScope.

        The relay layer (``orchestrator/voice_relay.py``) calls this once
        per voice session and keeps the connection alive for the duration.
        """
        api_key = os.environ.get("ALIBABA_API_KEY") or os.environ.get("DASHSCOPE_API_KEY")
        if not api_key:
            raise RuntimeError("ALIBABA_API_KEY (or DASHSCOPE_API_KEY) not configured")

        url = f"{QWEN_INTL_WS}?model={self._model}"
        return await websockets.connect(
            url,
            additional_headers={"Authorization": f"Bearer {api_key}"},
            open_timeout=15,
            max_size=2**24,  # 16 MB — accommodate large audio frames
        )


def _sanitize_for_qwen(text: str) -> str:
    """Neutralise URL-shaped substrings that DashScope's omni URL
    validator rejects.

    The validator only accepts URLs with one of ``http://``, ``https://``,
    ``data:``, ``file://`` schemes; scheme-less URL-shapes (bare hosts,
    ``localhost:port``, IPs, dotted names) are rejected with the same
    misleading "URL does not appear to be valid" 400 used for malformed
    multimodal inputs.  Wrapping the matches in backticks (markdown code
    span) makes the validator skip them while keeping them legible to
    the model.
    """
    def _wrap(m: re.Match[str]) -> str:
        token = m.group(0)
        # Already inside backticks?  Leave alone (the prior char check is
        # cheap and avoids stacking quotes when the model echoes back).
        return f"`{token}`"
    return _URL_LIKE_RE.sub(_wrap, text)
