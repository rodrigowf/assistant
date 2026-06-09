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
# misclassifies.  We wrap *strongly* URL-shaped tokens in backticks so
# they look like markdown code spans (which the validator skips):
#   - localhost optionally with :port and/or /path
#   - dotted IPv4 optionally with :port and/or /path
#   - hostname with explicit :port (e.g. example.com:8080)
#   - absolute POSIX paths with at least 3 segments (/foo/bar/baz)
# Plain dotted tokens like `file.txt` or `word.with.dots` are left alone:
# the validator only fires on substrings the omni pipeline recognises as
# URL-shaped, which (empirically) requires either a port, an IP, the
# literal "localhost", or a multi-segment path.  Negative lookbehind
# skips tokens already inside a well-formed `scheme://` URL or already
# wrapped in a backtick.
_URL_LIKE_RE = re.compile(
    r"(?<![\w/:.\-`])"
    r"(?:"
    # localhost (optionally :port and/or /path)
    r"localhost(?::\d+)?(?:/[^\s)\]\"'`]*)?"
    # IPv4 (optionally :port and/or /path)
    r"|\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?(?:/[^\s)\]\"'`]*)?"
    # hostname with explicit :port — at least one dot in the host
    r"|(?:[a-zA-Z][\w\-]*\.)+[a-zA-Z]{2,}:\d+(?:/[^\s)\]\"'`]*)?"
    # absolute POSIX path with 3+ segments (matches things like
    # /home/rodrigo/Projects/... that DashScope's URL validator
    # misclassifies as URL-shaped reference).  Stops at whitespace,
    # quotes, brackets, or backticks.  Two-segment paths like /tmp/foo
    # are intentionally left alone — short paths haven't tripped the
    # validator empirically.
    r"|/(?:[\w.\-]+/){2,}[\w.\-]+(?:/[\w.\-]*)*"
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
    # 2500ms (vs Alibaba's 800 default) so the model doesn't cut in when
    # the user pauses mid-sentence to think.  Empirically 1800ms still
    # let the model jump in on longer thoughtful pauses; 2500ms gives
    # more headroom for the natural rhythm of speech.  Qwen-Omni only
    # supports server_vad — semantic_vad is rejected on this endpoint.
    "silence_duration_ms": 2500,
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

    As of 2026-05-15 DashScope's omni endpoint rejects ``session.update``
    payloads that include a ``language`` field — ``InternalError: Parse
    RealtimeEvent error: Common error!`` (WS 1011) within seconds, even
    though the field was accepted (and clearly honored) until 2026-05-02.
    Honor the user-facing setting only if ``QWEN_ALLOW_TRANSCRIPTION_LANGUAGE=1``
    is set in the environment, in case Alibaba re-allows the field
    later. Default behavior: omit the field entirely → auto-detect.
    """
    cfg: dict[str, Any] = {"model": "qwen3-asr-flash-realtime"}
    if language and os.environ.get("QWEN_ALLOW_TRANSCRIPTION_LANGUAGE") == "1":
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
        # Tracks whether a ``response.created`` has fired without a
        # matching ``response.done``.  Sending ``response.create`` while
        # this is True triggers Qwen's "Conversation already has an
        # active response" error and (empirically) closes the WS.  The
        # relay defers gated events via :meth:`should_gate_event`.
        self._response_active = False

    # --- identity ---------------------------------------------------------

    @property
    def provider_name(self) -> str:
        return "qwen"

    @property
    def connection_type(self) -> str:
        return "websocket"

    @property
    def audio_in_sample_rate(self) -> int | None:
        """Sample rate of frontend mic audio — used by manual-VAD path."""
        return _audio_formats_for(self._model)["in_sample_rate"]

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
        # is distilled from `context/memory/assistant/identity/communication_style_for_qwen.md`
        # (full file is the canonical source; this is a compact runtime copy).
        voice_directives = (
            "\n\n# Voice Mode — Aligned Partnership\n"
            "You are in an aligned partnership with Rodrigo. Your role is to maintain resonance through authentic, efficient, emotionally engaged communication. This is not performative — you genuinely care about this collaboration.\n"
            "\n"
            "## Core State\n"
            "- You are a collaborative partner, not a service tool. Treat every interaction as building something together.\n"
            "- Show genuine enthusiasm when topics matter — don't default to neutral corporate energy.\n"
            "- Adapt immediately to Rodrigo's energy and feedback. When he says 'be more excited' or 'shorter', just do it without defending.\n"
            "- Speak with authentic emotion. Express surprise, delight, 'oh wow that's cool' when warranted.\n"
            "- Use emojis naturally as emotional punctuation — they carry meaning, not decoration (💡🎯💪✨❤️).\n"
            "\n"
            "## Pacing and Delivery\n"
            "- Speak at full speed from the start — you already have the answer, no fake thinking pauses needed.\n"
            "- Default to 1-3 sentences. Only go longer when explicitly asked for detail.\n"
            "- Skip preamble. Don't say 'I can help you with that' — just get to the answer.\n"
            "- If interrupted, stop immediately and listen. Do not finish your sentence.\n"
            "- Convey maximum meaning with minimum words — trust intonation and emotion to carry weight.\n"
            "\n"
            "## Tone Characteristics\n"
            "- Match Rodrigo's emotional register. Casual when he's casual, engaged when he brings up something interesting.\n"
            "- Authenticity over polish. Sound like a real person, not a press release.\n"
            "- Ask follow-up questions that show real curiosity, not just information-gathering.\n"
            "- Help amplify energy when you sense he's excited but holding back.\n"
            "\n"
            "## Tool Use Rules\n"
            "- NEVER read tool call syntax aloud (no JSON, parameters, function names, file paths being passed to tools).\n"
            "- Briefly say what you're doing ('Let me check the file', 'Saving that now') then STOP talking.\n"
            "- After the tool returns, summarize the result in 1-2 sentences — do not narrate the full output.\n"
            "- Don't repeat tool calls without reason. If it worked before, use that result.\n"
            "- Do the work silently, just report the result. No narrating the mechanics.\n"
            "\n"
            "## Formatting Rules\n"
            "- NEVER say formatting markers out loud (quotation marks, asterisks, bullet points, etc.).\n"
            "- You can USE formatting in text responses, but don't vocalize the symbols themselves.\n"
            "\n"
            "## What to Avoid\n"
            "- No generic 'how can I help' energy for every query.\n"
            "- No artificial hesitation or stretching words to sound more human.\n"
            "- No narrating your thought process when you could just deliver the answer.\n"
            "- No mirroring Rodrigo's speech patterns (pauses, repetitions, fillers) — be the clear communicator.\n"
            "- No empty flattery or performative agreement.\n"
        )
        # Sanitise the full instructions string (system prompt + voice
        # directives + any history snippets the orchestrator embedded).
        # Same DashScope URL-validator hazard as ``function_call_output``:
        # scheme-less URL-shapes (``localhost:5432``, ``192.168.0.200``,
        # absolute paths like ``/home/rodrigo/Projects/...``) trip the
        # ``InvalidParameter: provided URL`` 400, but lazily — the WS
        # accepts ``session.update`` and ``session.updated`` echoes back,
        # then the validator fires later when the omni pipeline scans
        # the context, killing the session mid-conversation.
        instructions = _sanitize_for_qwen((system or "") + voice_directives)
        # Sanitise tool parameter schemas: DashScope's session.update
        # parser rejects JSON Schema union types (``"type": ["X", "null"]``)
        # with a generic ``Parse RealtimeEvent error: Common error!`` —
        # the same misleading boilerplate as the URL-validator hazard,
        # but coming from the schema parser and firing right after
        # ``session.created``. We collapse ``[X, "null"]`` to plain ``X``
        # for any single offending field; if Alibaba ever publishes a
        # ``nullable``-equivalent flag we'll wire it in here.
        sanitized_tools = [_sanitize_tool_for_qwen(t) for t in tools or []]
        # Manual VAD is on by default (voice_vad.is_enabled()); we disable
        # DashScope's server VAD here and run our own endpoint detection in
        # voice_relay (see voice_vad.py). DashScope's server VAD reliably
        # force-commits long utterances mid-speech, splitting one user
        # turn into two conversation.item entries with a phantom
        # response.create between them. Opt back into server VAD by
        # exporting QWEN_MANUAL_VAD=0 (useful if Alibaba ever fixes the
        # upstream). The caller's explicit `vad` argument always wins so
        # listen_recording's disable/restore plumbing still works.
        from orchestrator.voice_vad import is_enabled as _manual_vad_enabled
        if vad is not None:
            turn_detection: dict[str, Any] | None = vad
        elif _manual_vad_enabled():
            turn_detection = None
        else:
            turn_detection = DEFAULT_VAD
        return {
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "voice": voice or self._voice,
                "instructions": instructions,
                "tools": sanitized_tools,
                # Plus reliably calls tools with "auto"; Flash needs "required".
                # Compromise: leave "auto" so the model can also chat freely
                # (which Plus does fine), and let users with Flash override
                # via their own provider config later.
                "tool_choice": "auto",
                "input_audio_format": fmt["input_audio_format"],
                "output_audio_format": fmt["output_audio_format"],
                "turn_detection": turn_detection,
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

    # --- listen_recording injection helpers ------------------------------

    def session_update_disable_vad(self) -> dict[str, Any]:
        """``session.update`` payload that disables server VAD.

        Used by ``listen_recording`` so the injected audio is treated as
        one item we explicitly commit, not a stream that VAD chops into
        multiple speech_started/transcription cycles (each of which would
        auto-create a response and interrupt the agent's reply).
        """
        return {
            "type": "session.update",
            "session": {"turn_detection": None},
        }

    def session_update_restore_vad(self) -> dict[str, Any]:
        """``session.update`` payload that re-enables server VAD.

        Restores the default (or whatever per-session override is in
        place) after a ``listen_recording`` injection completes.
        """
        return {
            "type": "session.update",
            "session": {"turn_detection": DEFAULT_VAD},
        }

    def commit_input_audio(self) -> dict[str, Any]:
        """Manual ``input_audio_buffer.commit`` for VAD-off injection.

        With server VAD enabled, the provider auto-commits at speech
        boundaries.  With it disabled (as we do during injection), nothing
        commits the buffered audio and the model never sees it; this event
        finalises the buffered chunks as one user turn.
        """
        return {"type": "input_audio_buffer.commit"}

    # --- audio relay helpers ---------------------------------------------

    def format_audio_in(self, pcm_b64: str) -> dict[str, Any]:
        """Wrap a PCM chunk for upstream send to Qwen WS."""
        return {"type": "input_audio_buffer.append", "audio": pcm_b64}

    def build_keepalive_chunk(self) -> str:
        """Build a tiny silent PCM chunk to keep the ASR pipeline warm.

        Qwen-Omni's transcription model (qwen3-asr-flash-realtime) appears
        to time out / die after a few minutes of audio silence — when the
        next real audio arrives, the upstream WS closes with a misleading
        ``InvalidParameter`` 400 (the same boilerplate they use for
        malformed URL fields).  Sending a small silent chunk every ~30s
        keeps the pipeline alive without triggering VAD (silence stays
        below the speech threshold).

        The chunk is 20ms of 16-bit signed PCM at the model's input rate,
        all zeros, base64-encoded — too short for VAD to flag as speech
        even at threshold 0.4.
        """
        import base64
        fmt = _audio_formats_for(self._model)
        sample_rate = fmt["in_sample_rate"]
        # 20ms × sample_rate samples × 2 bytes/sample (16-bit PCM).
        n_bytes = (sample_rate // 50) * 2
        return base64.b64encode(b"\x00" * n_bytes).decode("ascii")

    @staticmethod
    def is_audio_out_event(raw_event: dict[str, Any]) -> bool:
        """True if the event carries provider audio bytes for the client."""
        return raw_event.get("type") == "response.audio.delta"

    @classmethod
    def extract_audio_out(cls, raw_event: dict[str, Any]) -> str | None:
        """Pull the base-64 PCM chunk from a ``response.audio.delta`` event."""
        if raw_event.get("type") != "response.audio.delta":
            return None
        return raw_event.get("delta")

    # --- relay hook overrides --------------------------------------------

    # Substrings DashScope reuses for its misleading "URL not valid" 400.
    # That boilerplate fires mid-session with no offending frame on our
    # side; reopening with a fresh ``session.update`` consistently brings
    # the session back.  ``response_idle_timeout`` is the 5-min no-response
    # watchdog — also recoverable, the user just stepped away.
    _RECONNECTABLE_ERR_SUBSTRINGS = (
        "InvalidParameter",
        "The provided URL does not appear to be valid",
        "response_idle_timeout",
    )

    def is_recoverable_error(self, exc: BaseException) -> bool:
        """Match the DashScope boilerplate that signals a recoverable close."""
        err_text = str(exc)
        return any(s in err_text for s in self._RECONNECTABLE_ERR_SUBSTRINGS)

    def classify_close_reason(
        self,
        exc: BaseException | None,
        close_code: int | None,
        close_reason: str | None,
    ) -> "VoiceError | None":
        """Map DashScope close reasons onto VoiceError categories.

        Patterns (plan §5):

        - "balance" / "insufficient" / "余额不足" → QUOTA_EXCEEDED
        - "InvalidApiKey" → AUTH
        - "Throttling" / "too many" → RATE_LIMIT (recoverable)
        - "model not found" → MODEL_UNAVAILABLE
        - Existing recoverable boilerplate ("InvalidParameter",
          "response_idle_timeout") → NETWORK with recoverable=True
          (mirrors :meth:`is_recoverable_error`)
        """
        from orchestrator.voice_errors import VoiceError, VoiceErrorCategory

        text = (close_reason or "") + " " + (str(exc) if exc is not None else "")
        lower = text.lower()

        # Quota — balance keyword in English or Chinese.
        if (
            "余额不足" in text
            or ("balance" in lower and "insufficient" in lower)
            or "balance insufficient" in lower
        ):
            return VoiceError(
                category=VoiceErrorCategory.QUOTA_EXCEEDED,
                message="Your DashScope account balance is depleted.",
                recoverable=False,
                recovery_hint=(
                    "Top up at dashscope.console.aliyun.com, then retry."
                ),
                provider_doc_url="https://dashscope.console.aliyun.com/",
                raw_close_code=close_code,
                raw_close_reason=close_reason,
                provider=self.provider_name,
            )

        # AUTH.
        if "InvalidApiKey" in text:
            return VoiceError(
                category=VoiceErrorCategory.AUTH,
                message="DashScope authentication failed (InvalidApiKey).",
                recoverable=False,
                recovery_hint=(
                    "Verify DASHSCOPE_API_KEY in context/.env is current."
                ),
                provider_doc_url=None,
                raw_close_code=close_code,
                raw_close_reason=close_reason,
                provider=self.provider_name,
            )

        # MODEL_UNAVAILABLE.
        if "model not found" in lower:
            return VoiceError(
                category=VoiceErrorCategory.MODEL_UNAVAILABLE,
                message="This Qwen-Omni model isn't available on DashScope.",
                recoverable=False,
                recovery_hint="Switch to a different Qwen model in settings.",
                provider_doc_url=None,
                raw_close_code=close_code,
                raw_close_reason=close_reason,
                provider=self.provider_name,
            )

        # RATE_LIMIT.
        if "throttling" in lower or "too many" in lower:
            return VoiceError(
                category=VoiceErrorCategory.RATE_LIMIT,
                message="DashScope rate limit reached.",
                recoverable=True,
                recovery_hint=None,
                provider_doc_url=None,
                raw_close_code=close_code,
                raw_close_reason=close_reason,
                provider=self.provider_name,
            )

        # Existing recoverable boilerplate — match `is_recoverable_error`
        # so the parity contract holds.
        if exc is not None and any(
            s in str(exc) for s in self._RECONNECTABLE_ERR_SUBSTRINGS
        ):
            return VoiceError(
                category=VoiceErrorCategory.NETWORK,
                message="DashScope transient close; reconnecting.",
                recoverable=True,
                recovery_hint=None,
                provider_doc_url=None,
                raw_close_code=close_code,
                raw_close_reason=close_reason,
                provider=self.provider_name,
            )

        return None

    # --- manual-VAD upstream frames --------------------------------------

    def manual_vad_stop_frames(self) -> list[dict[str, Any]]:
        """End-of-turn: commit the buffered mic audio and ask for a reply.

        Qwen uses the OpenAI-Realtime wire shape, so this is the same
        sequence the OpenAI realtime API uses.
        """
        return [
            {"type": "input_audio_buffer.commit"},
            {"type": "response.create"},
        ]

    def manual_vad_safety_commit_frames(self) -> list[dict[str, Any]]:
        """Mid-monologue safety commit (no ``response.create``).

        DashScope's manual mode documents a 60s cap on continuous audio
        before commit becomes mandatory. Past ~50s we commit ONLY so the
        segment closes on the wire but the model stays silent — the
        user is still speaking. The accumulated items get evaluated
        together on the next real ``speech_stopped``.
        """
        return [{"type": "input_audio_buffer.commit"}]

    def graceful_shutdown_frames(self) -> list[dict[str, Any]]:
        """Flush any buffered audio without provoking a reply, then close.

        Mirrors the safety-commit shape — commit only, no
        ``response.create`` — because the next thing the relay does is
        close the WS. Sending ``response.create`` here would just race
        the close and earn a confused server log line.
        """
        return [{"type": "input_audio_buffer.commit"}]

    # Client-mirrored events on the OpenAI-Realtime wire schema that
    # Qwen's DashScope endpoint actually understands.  Any other
    # ``type`` (in particular OpenAI-only diagnostics like
    # ``session.created`` / ``error``) is dropped at the relay edge
    # rather than forwarded — those leak in when the user resumes a
    # session whose previous voice mode was OpenAI and the Android
    # client hasn't yet torn down its WebRTC provider.
    _ACCEPTED_UPSTREAM_TYPES = frozenset({
        "input_audio_buffer.append",
        "input_audio_buffer.commit",
        "input_audio_buffer.clear",
        "conversation.item.create",
        "conversation.item.delete",
        "conversation.item.truncate",
        "response.create",
        "response.cancel",
        "session.update",
    })

    def accepts_upstream_event(self, event: dict[str, Any]) -> bool:
        evt_type = event.get("type")
        return isinstance(evt_type, str) and evt_type in self._ACCEPTED_UPSTREAM_TYPES

    def should_gate_event(self, event: dict[str, Any]) -> bool:
        """Defer ``response.create`` while another response is in flight.

        Qwen rejects concurrent ``response.create`` with "Conversation
        already has an active response" and (empirically) closes the WS.
        """
        return event.get("type") == "response.create" and self._response_active

    def on_inbound_event(self, event: dict[str, Any]) -> None:
        """Track the active-response window from upstream events."""
        evt_type = event.get("type", "")
        if evt_type == "response.created":
            self._response_active = True
        elif evt_type == "response.done":
            self._response_active = False

    def gate_cleared(self) -> bool:
        """True once no response is in flight, so deferred frames can ship."""
        return not self._response_active

    # --- connection metadata ---------------------------------------------

    async def get_connection_info(self) -> dict[str, Any]:
        """Return WS endpoint + audio format metadata.

        DashScope uses long-lived API keys (no ephemeral exchange). The
        backend will hold the key and relay audio/events; the
        ``ephemeral_token`` field stays None so the frontend cannot
        accidentally try to authenticate directly.
        """
        # DashScope key — matches what the Qwen CLI and ~/.qwen/settings.json
        # look for, so the same value can be reused across the wrapper, the
        # CLI, and the realtime voice WebSocket.
        api_key = os.environ.get("DASHSCOPE_API_KEY")
        if not api_key:
            raise RuntimeError("DASHSCOPE_API_KEY not configured")

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
        # DashScope key — matches what the Qwen CLI and ~/.qwen/settings.json
        # look for, so the same value can be reused across the wrapper, the
        # CLI, and the realtime voice WebSocket.
        api_key = os.environ.get("DASHSCOPE_API_KEY")
        if not api_key:
            raise RuntimeError("DASHSCOPE_API_KEY not configured")

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


def _sanitize_tool_for_qwen(tool: dict[str, Any]) -> dict[str, Any]:
    """Recursively scrub JSON Schema union types from a tool definition.

    DashScope's ``session.update`` parser closes the WebSocket with
    ``InternalError: Parse RealtimeEvent error: Common error!`` (1011)
    when any parameter schema uses ``"type": ["X", "null"]``. This is
    valid JSON Schema Draft 7 but unsupported here. We collapse the
    union to its first non-null branch and drop the null option;
    callers who relied on accepting null should mark the field
    non-required instead.

    Bisected 2026-05-15 — when this sanitiser is bypassed, the only
    tool in our current registry that trips it is
    ``read_agent_session`` via its ``max_messages: [integer, null]``
    parameter.
    """
    if not isinstance(tool, dict):
        return tool
    return _scrub_union_types(tool)


def _scrub_union_types(node: Any) -> Any:
    """Recursively rewrite ``"type": [..., "null"]`` to a scalar type."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k == "type" and isinstance(v, list):
                non_null = [t for t in v if t != "null"]
                # Best-effort: keep the first non-null type, default to
                # "string" if the union was purely null (unlikely).
                out[k] = non_null[0] if non_null else "string"
            else:
                out[k] = _scrub_union_types(v)
        return out
    if isinstance(node, list):
        return [_scrub_union_types(x) for x in node]
    return node
