"""Google Gemini Live realtime voice — shared protocol base.

This module hosts the protocol-level Live API logic that both Google
backends (AI Studio and Vertex AI) share. The two backends speak the
*same* JSON protocol on the wire — camelCase message names (``setup``,
``realtimeInput``, ``serverContent``, ``toolCall``, ``toolResponse``),
audio shipped as ``inlineData`` parts inside ``modelTurn``, etc. — but
differ in three places:

1. **URL** — ``generativelanguage.googleapis.com`` (AI Studio) vs.
   ``{location}-aiplatform.googleapis.com`` (Vertex).
2. **Auth** — ``?key=<API_KEY>`` query param (AI Studio) vs.
   ``Authorization: Bearer <ADC token>`` header (Vertex).
3. **Model field in the setup payload** — ``models/<id>`` (AI Studio)
   vs. ``projects/<proj>/locations/<loc>/publishers/google/models/<id>``
   (Vertex).

:class:`GeminiVoiceProviderBase` owns everything else: event translation,
tool-call bookkeeping, schema sanitisation, voice catalogue, audio frame
helpers. Concrete backends live in
:mod:`orchestrator.providers.gemini_voice` and only fill in
:meth:`_open_upstream_ws` and :meth:`_qualify_model`.

Why this split exists (history): The AI Studio endpoint started
returning WS close ``1008`` — *"Your project has been denied access.
Please contact support."* — for preview Live models on previously
working keys. Google maintainers' documented workaround is to switch to
Vertex AI, which uses GCP IAM instead of AI Studio's allowlist. We keep
both backends available because Vertex doesn't yet mirror every preview
model AI Studio carries (e.g. ``gemini-2.5-flash-native-audio-latest``
is AI Studio's canonical id; Vertex still serves it as
``gemini-live-2.5-flash-native-audio``).

References:
- https://ai.google.dev/api/live (AI Studio Live API)
- https://docs.cloud.google.com/vertex-ai/generative-ai/docs/live-api/get-started-websocket (Vertex Live API)
- https://github.com/google/adk-python/issues/3964 (Google maintainer
  recommending the Vertex switch when AI Studio returns 1008)

Out of scope:
- Video input (Live supports it; our frontend doesn't capture).
- Voice cloning (separate endpoint).
- Function-calling beyond the realtime audio flow (text-only Gemini is
  a separate provider class).
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import websockets

from orchestrator import voice_vad
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

# Default voice (overridable via constructor). The default *model*
# differs per backend so each concrete subclass carries its own
# ``DEFAULT_MODEL`` rather than sharing one here.
GEMINI_VOICE_NAME = "Puck"

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


class GeminiVoiceProviderBase(BaseVoiceProvider, abc.ABC):
    """Shared Gemini Live protocol logic.

    Subclasses provide the URL, auth, and model-qualification details:

    - :meth:`_open_upstream_ws` — opens and returns the upstream WS,
      handling backend-specific URL + auth.
    - :meth:`_qualify_model` — converts a bare model id (e.g.
      ``"gemini-live-2.5-flash-native-audio"``) into the form the
      ``setup.model`` field expects (``"models/..."`` for AI Studio,
      ``"projects/.../publishers/google/models/..."`` for Vertex).
    - :meth:`_get_endpoint_url` — observability-only URL surfaced to the
      frontend via ``get_connection_info`` (no auth in the string).
    """

    # Subclasses must override.
    DEFAULT_MODEL: str = ""

    def __init__(
        self,
        model: str = "",
        voice: str = GEMINI_VOICE_NAME,
        transcription_language: str = "",
    ) -> None:
        if not model:
            model = self.DEFAULT_MODEL
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
        # Session resumption — Gemini Live closes the upstream WS after
        # ~10–15 minutes per session. Opting in via ``sessionResumption``
        # in the setup payload makes the server emit periodic
        # ``sessionResumptionUpdate`` frames carrying a ``newHandle``; on
        # disconnect we can reopen with that handle and pick up the
        # in-memory context. We hold the most recent resumable handle
        # here so :meth:`format_session_config` includes it whenever the
        # relay rebuilds setup (first session: handle is None →
        # opt-in-only). See https://ai.google.dev/api/live#session-resumption.
        self._resumption_handle: str | None = None
        # Sticky flag: set when the upstream emits ``goAway`` (typically
        # ~30–60s before the server force-closes with WS 1008 / "session
        # duration"). The relay's drain task catches that close and asks
        # the provider via :meth:`is_recoverable_error` whether to try a
        # transparent reconnect — we only return True when this flag is
        # set, because a 1008 from any other cause (e.g. AI Studio's
        # "project denied access") is genuinely fatal and reconnecting
        # would just loop. Cleared on ``setupComplete`` after reconnect.
        self._goaway_received: bool = False
        # Single-shot guard for the "stale handle" recovery path: when
        # we observe 1008 "BidiGenerateContent session expired" with no
        # prior goAway, the saved ``_resumption_handle`` is poisoned
        # (server invalidated it — common when the session was
        # force-closed previously, or has aged out across an
        # orchestrator restart). We drop the handle and reconnect once
        # with a fresh setup. If that fresh attempt ALSO gets 1008, we
        # treat it as genuinely fatal instead of looping. Reset on the
        # next ``setupComplete``.
        self._stale_handle_recovery_used: bool = False

    # --- identity ---------------------------------------------------------

    @property
    def provider_name(self) -> str:
        # Both backends advertise as "google" — the choice of backend is
        # an orthogonal config knob (see ``GEMINI_VOICE_BACKEND`` /
        # ``default_voice_endpoint``). Keeping a single provider id means
        # downstream code (assistant_config, JSONL ``voice_provider``
        # field, the frontend voice dropdown) stays unchanged.
        return "google"

    @property
    @abc.abstractmethod
    def endpoint_id(self) -> str:
        """Backend identifier (``"aistudio"`` or ``"vertex"``)."""

    @property
    def connection_type(self) -> str:
        return "websocket"

    @property
    def audio_in_sample_rate(self) -> int | None:
        """16 kHz mono PCM16 — declared so the relay can initialise
        Silero for manual VAD (see ``manual_vad_*`` hooks). Matches the
        hardcoded ``audio/pcm;rate=16000`` in :meth:`format_audio_in`
        and what the Android client ships.
        """
        return 16000

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

            # Input ASR transcription — what the user said. Live API
            # ships this either as ``serverContent.inputTranscription``
            # (current docs) or at the top level (older docs); we
            # accept both.
            input_t = server_content.get("inputTranscription")
            if isinstance(input_t, dict):
                txt = input_t.get("text", "")
                if txt:
                    return TextDelta(text=txt)

            # Output ASR transcription — the model's spoken reply as text.
            # Surfaces in the chat as a streaming assistant message
            # alongside the audio.
            output_t = server_content.get("outputTranscription")
            if output_t is not None:
                txt = output_t.get("text", "")
                if txt:
                    return TextDelta(text=txt)

            # Streaming text via parts[].text (rare with native-audio
            # models but supported by the half-cascade Live preview).
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

        # Top-level inputTranscription — older Live API shape.  Newer
        # builds nest it under serverContent (handled above); we accept
        # either since the docs disagree across versions.
        input_t = raw_event.get("inputTranscription")
        if isinstance(input_t, dict):
            txt = input_t.get("text", "")
            if txt:
                return TextDelta(text=txt)

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
                    "parameters": _sanitize_schema_for_gemini(fn.get("parameters", {})),
                })
            elif "name" in t:
                function_declarations.append({
                    "name": t.get("name"),
                    "description": t.get("description", ""),
                    "parameters": _sanitize_schema_for_gemini(
                        t.get("input_schema") or t.get("parameters") or {}
                    ),
                })

        setup: dict[str, Any] = {
            "model": self._qualify_model(self._model),
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
            # Surface both ASR streams so the orchestrator can persist
            # user speech as ``[voice]`` JSONL entries and the model's
            # spoken reply as TextDelta/TextComplete events alongside
            # the audio.  Without these the Live API ships audio only
            # and our chat window has nothing to show.
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
            # Activity (VAD) detection.  Two modes:
            #
            # Manual (default, GEMINI_MANUAL_VAD=1 — opt out with =0):
            #   ``disabled: True`` hands turn boundaries to us. The
            #   relay runs Silero locally over the mic stream and
            #   demarcates each user turn with
            #   ``realtimeInput.activityStart`` / ``activityEnd``
            #   (see ``manual_vad_start_frames`` / ``stop_frames``).
            #   Why: Gemini's server VAD ends turns mid-sentence on
            #   natural breathing pauses even with
            #   ``endOfSpeechSensitivity: LOW`` and
            #   ``silenceDurationMs: 2500``. The same client-side VAD
            #   architecture that fixed Qwen's force-commit issue
            #   fixes this here.
            #
            # Server (GEMINI_MANUAL_VAD=0):
            #   Fall back to the upstream's VAD, tuned as conservatively
            #   as the API allows. Tradeoff is ~1s extra latency at
            #   real end-of-turn from the 2.5s silence window; you
            #   still get cut off mid-sentence on longer pauses.
            "realtimeInputConfig": {
                "automaticActivityDetection": (
                    {"disabled": True}
                    if voice_vad.is_enabled_for("google")
                    else {
                        "disabled": False,
                        "startOfSpeechSensitivity": "START_SENSITIVITY_LOW",
                        "endOfSpeechSensitivity": "END_SENSITIVITY_LOW",
                        "prefixPaddingMs": 300,
                        "silenceDurationMs": 2500,
                    }
                ),
            },
            # Opt into session resumption. First setup sends an empty
            # object (no handle) to ask the server to start emitting
            # ``sessionResumptionUpdate`` frames; on a relay-initiated
            # reconnect we send the most recent ``newHandle`` and Gemini
            # restores the upstream session's in-memory state.
            "sessionResumption": (
                {"handle": self._resumption_handle}
                if self._resumption_handle else {}
            ),
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

    # --- manual-VAD upstream frames --------------------------------------
    #
    # When ``automaticActivityDetection.disabled = True`` (see
    # format_session_config), Gemini Live expects the client to send
    # ``realtimeInput.activityStart`` to open a user turn and
    # ``realtimeInput.activityEnd`` to close it. The model only
    # generates a response after receiving ``activityEnd``. There's no
    # separate ``response.create`` like the OpenAI-realtime wire shape.

    def manual_vad_start_frames(self) -> list[dict[str, Any]]:
        """Mark start of a user turn — sent on local-VAD speech_started."""
        return [{"realtimeInput": {"activityStart": {}}}]

    def manual_vad_stop_frames(self) -> list[dict[str, Any]]:
        """Mark end of a user turn — triggers the model's reply."""
        return [{"realtimeInput": {"activityEnd": {}}}]

    def manual_vad_safety_commit_frames(self) -> list[dict[str, Any]]:
        """No safety commit for Gemini — return ``[]`` to disable.

        History: we initially copied Qwen's safety-commit pattern as
        ``activityEnd`` + ``activityStart`` on the assumption that
        Gemini would batch the response generation until a real pause.
        Live test 2026-06-04 disproved that hypothesis: when the user
        spoke continuously for 50s, our safety commit fired and
        Gemini started replying within 290ms of the ``activityEnd``,
        cutting the user off mid-sentence. The architectural reality:
        Qwen's wire shape SEPARATES ``input_audio_buffer.commit``
        (close segment) from ``response.create`` (ask for reply), so
        a "commit-only" safety chunking works. Gemini Live's
        ``activityEnd`` collapses those into one — there is no
        documented or empirical way to chunk a long utterance without
        provoking a reply.

        Gemini Live also has NO documented per-utterance cap in
        manual mode (vs. DashScope's documented 60s) and no observed
        cap in 200+s monologues. So the safety path was guarding
        against a problem that does not exist; removing it cleanly
        fixes the only real issue (false interruption).
        """
        return []

    def graceful_shutdown_frames(self) -> list[dict[str, Any]]:
        """Close any in-flight user turn before we tear the WS down.

        Gemini's ``activityEnd`` ALSO triggers a model reply (see
        :meth:`manual_vad_safety_commit_frames` for the long story).
        That's normally a problem, but here it's harmless: the relay
        closes the WS immediately after sending these frames, so the
        reply — if any — never reaches us. What matters is that the
        server-side activity gets a clean close instead of dangling
        until the session age-out.

        If no manual-VAD activity is open the frame is benign;
        Gemini ignores ``activityEnd`` outside an active turn.
        """
        return [{"realtimeInput": {"activityEnd": {}}}]

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

    @property
    def handshake_direction(self) -> str:
        """Gemini Live: client sends ``setup`` first, server acks with ``setupComplete``."""
        return "client_first"

    # No keepalive needed empirically — Gemini Live doesn't have Qwen's
    # ASR-timeout pathology. If a similar problem surfaces, override
    # build_keepalive_chunk() to return a short silent PCM chunk.

    def on_inbound_event(self, event: dict[str, Any]) -> None:
        """Track session-resumption handles and the goAway signal.

        Gemini Live exposes two pieces of state we need to react to:

        - ``sessionResumptionUpdate.newHandle`` — opaque string that, on
          a future reconnect, restores this session's in-memory context.
          We keep the most recent one (the server replaces older
          handles); :meth:`format_session_config` reads it on rebuild.
        - ``goAway.timeLeft`` — warning emitted ~30–60s before the
          server force-closes the WS with code 1008 because the
          per-session duration limit is up. We don't close eagerly: we
          let Gemini drop the connection and the relay's drain task
          catches the close; :meth:`is_recoverable_error` then returns
          True so the existing reconnect machinery reopens with the
          saved handle.
        - ``setupComplete`` — fires after every successful (re)open.
          Clears the goAway flag so a *new* genuine 1008 (e.g. quota)
          won't loop reconnects.
        """
        if "setupComplete" in event:
            self._goaway_received = False
            self._stale_handle_recovery_used = False
        update = event.get("sessionResumptionUpdate")
        if isinstance(update, dict):
            handle = update.get("newHandle")
            if isinstance(handle, str) and handle:
                self._resumption_handle = handle
                logger.info(
                    "gemini session_resumption handle captured (resumable=%s)",
                    update.get("resumable"),
                )
        go_away = event.get("goAway")
        if isinstance(go_away, dict):
            self._goaway_received = True
            logger.info(
                "gemini goAway received timeLeft=%s (will reconnect on close)",
                go_away.get("timeLeft"),
            )

    def should_close_after_event(self, event: dict[str, Any]) -> bool:
        """Close the upstream WS immediately after a ``goAway``.

        Per Google's Live API spec, the client is expected to close the
        connection on receiving ``goAway`` — failing to do so triggers
        the punitive ``1008`` close with the misleading "policy
        violation" reason. We close cleanly (1000), which routes the
        drain loop into :meth:`is_recoverable_error` (which returns True
        because we just set ``_goaway_received``) and lets the relay
        reconnect with the saved session-resumption handle.
        """
        return isinstance(event.get("goAway"), dict)

    # Client-mirrored events on Gemini Live's wire schema that we
    # actually forward upstream.  Gemini doesn't use OpenAI's ``type=...``
    # envelopes; its top-level keys are camelCase ``clientContent``,
    # ``realtimeInput``, ``toolResponse``, ``setup``.  Anything else
    # (most importantly: stray OpenAI-shape events that leak in when
    # the user resumes a session whose previous voice mode was OpenAI)
    # is dropped at the relay edge to avoid WS 1007 "Invalid JSON
    # payload: Unknown name 'type'" close.
    _ACCEPTED_UPSTREAM_KEYS = frozenset({
        "clientContent",
        "realtimeInput",
        "toolResponse",
        "setup",
        # ``sessionResumptionUpdate`` is sent by us during reconnect
        # but always passes through ``send_event`` from the relay's own
        # rebuild path, so it's also accepted here for completeness.
        "sessionResumptionUpdate",
    })

    def accepts_upstream_event(self, event: dict[str, Any]) -> bool:
        if "type" in event:
            # OpenAI-Realtime envelope.  Definitely not for us.
            return False
        return any(k in event for k in self._ACCEPTED_UPSTREAM_KEYS)

    def is_recoverable_error(self, exc: BaseException) -> bool:
        """Reconnect when the upstream closed *after* a goAway, OR when
        the close was a "BidiGenerateContent session expired" 1008
        triggered by a stale resumption handle (one-shot recovery).

        Other 1008 closes are treated as fatal: AI Studio's "project
        denied access" close shares the same code but isn't recoverable
        — retrying would just loop.
        """
        if self._goaway_received and self._resumption_handle:
            # Standard goAway recovery: rebuild with the live handle.
            return True

        # Stale-handle recovery: we DID have a handle on this attempt
        # but it was rejected with "session expired" without a prior
        # goAway. The handle is poisoned (server invalidated it across
        # a previous force-close, or it aged out). Drop it and let the
        # relay rebuild with an empty resumption block. One-shot to
        # prevent infinite loops if the fresh setup also dies.
        if (
            self._resumption_handle
            and not self._stale_handle_recovery_used
            and self._is_session_expired_close(exc)
        ):
            logger.warning(
                "gemini stale resumption handle rejected; dropping handle and "
                "reconnecting with fresh setup"
            )
            self._resumption_handle = None
            self._stale_handle_recovery_used = True
            return True

        return False

    @staticmethod
    def _is_session_expired_close(exc: BaseException) -> bool:
        """Detect Gemini's 1008 "BidiGenerateContent session expired"
        close. The reason string travels in ``str(exc)`` because
        ``websockets`` formats ``ConnectionClosedError`` as
        ``received <code> (<short>) <reason>; then sent ...``.
        """
        msg = str(exc)
        return "1008" in msg and "session expired" in msg.lower()

    def classify_close_reason(
        self,
        exc: BaseException | None,
        close_code: int | None,
        close_reason: str | None,
    ) -> VoiceError | None:
        """Map Gemini Live close reasons onto :class:`VoiceErrorCategory`.

        Pattern table (must stay aligned with plan §5):

        - ``1011`` + "exceeded its monthly spending cap" → QUOTA_EXCEEDED
        - ``1008`` + "session expired" → recoverable (existing
          stale-handle path; mirrors :meth:`_is_session_expired_close`)
        - ``1008`` + "denied access" → AUTH (non-recoverable)
        - reason mentions "rate" → RATE_LIMIT (recoverable)
        - "not available in your region" / "Model not found" /
          "is not supported" → MODEL_UNAVAILABLE (non-recoverable)

        Read-only contract: must NOT mutate ``_resumption_handle``,
        ``_goaway_received``, or ``_stale_handle_recovery_used`` — only
        :meth:`is_recoverable_error` does that.
        """
        text = (close_reason or "") + " " + (str(exc) if exc is not None else "")
        lower = text.lower()

        # Quota / billing — Bug 1 in the 2026-06-08 log inventory.
        if "exceeded its monthly spending cap" in text or "Your project has" in text and "cap" in lower:
            return VoiceError(
                category=VoiceErrorCategory.QUOTA_EXCEEDED,
                message=(
                    "Your Google AI Studio project has exceeded its monthly "
                    "spending cap."
                ),
                recoverable=False,
                recovery_hint=(
                    "Visit AI Studio at ai.studio/spend to raise the cap or "
                    "wait for the cycle to reset."
                ),
                provider_doc_url="https://ai.studio/spend",
                raw_close_code=close_code,
                raw_close_reason=close_reason,
                provider=self.provider_name,
            )

        # Recoverable: stale-handle "session expired" 1008. Match the
        # legacy gate (which mutates state); the classifier only reads.
        if self._is_session_expired_close(exc) if exc is not None else False:
            return VoiceError(
                category=VoiceErrorCategory.NETWORK,
                message="Gemini session resumption handle expired; reconnecting.",
                recoverable=True,
                recovery_hint=None,
                provider_doc_url=None,
                raw_close_code=close_code,
                raw_close_reason=close_reason,
                provider=self.provider_name,
            )

        # AUTH — AI Studio denied access (separate 1008 reason, fatal).
        if "denied access" in lower or "denied" in lower and "access" in lower:
            return VoiceError(
                category=VoiceErrorCategory.AUTH,
                message=(
                    "Your Google AI Studio project has been denied access to "
                    "the Live API."
                ),
                recoverable=False,
                recovery_hint=(
                    "Verify your GEMINI_API_KEY is valid, the project is "
                    "active, and the model is available on AI Studio. "
                    "If AI Studio keeps refusing, switch the endpoint to "
                    "Vertex AI."
                ),
                provider_doc_url="https://aistudio.google.com/apikey",
                raw_close_code=close_code,
                raw_close_reason=close_reason,
                provider=self.provider_name,
            )

        # MODEL_UNAVAILABLE — region or discontinued model.
        if (
            "not available in your region" in lower
            or "model not found" in lower
            or "is not supported" in lower
        ):
            return VoiceError(
                category=VoiceErrorCategory.MODEL_UNAVAILABLE,
                message="This Gemini model isn't available for your project.",
                recoverable=False,
                recovery_hint=(
                    "Switch to a different Gemini Live model in voice "
                    "settings, or change the endpoint to Vertex AI."
                ),
                provider_doc_url=(
                    "https://ai.google.dev/gemini-api/docs/models/gemini"
                ),
                raw_close_code=close_code,
                raw_close_reason=close_reason,
                provider=self.provider_name,
            )

        # RATE_LIMIT — opaque but transient.
        if "rate" in lower and ("limit" in lower or "throttl" in lower):
            return VoiceError(
                category=VoiceErrorCategory.RATE_LIMIT,
                message="Google Live API rate limit reached.",
                recoverable=True,
                recovery_hint=None,
                provider_doc_url=None,
                raw_close_code=close_code,
                raw_close_reason=close_reason,
                provider=self.provider_name,
            )

        # No semantic match — let the relay synthesise a generic NETWORK
        # envelope.
        return None

    # --- connection metadata ---------------------------------------------

    async def get_connection_info(self) -> dict[str, Any]:
        """Return metadata the frontend needs.

        The URL surfaced here is observability-only — auth bytes (API key
        / bearer token) never appear in the response. The audio formats
        are the same for both backends (16kHz in, 24kHz out).
        """
        return {
            "connection_type": "websocket",
            "endpoint": self._get_endpoint_url(),
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
        """Open the upstream Live WebSocket.

        Called once per session by ``orchestrator/voice_relay.py``. The
        relay keeps the connection alive for the session's lifetime.
        Backend-specific URL + auth handled by :meth:`_open_upstream_ws`.
        """
        return await self._open_upstream_ws()

    # --- backend hooks (subclasses must implement) -----------------------

    @abc.abstractmethod
    def _qualify_model(self, model_id: str) -> str:
        """Return the value of the ``setup.model`` field for ``model_id``.

        AI Studio: ``"models/<id>"``.
        Vertex AI: ``"projects/.../publishers/google/models/<id>"``.
        """

    @abc.abstractmethod
    def _get_endpoint_url(self) -> str:
        """Return the observability URL surfaced to the frontend.

        Auth must NOT appear in this string — it's logged + sent to the
        client. The real auth happens inside :meth:`_open_upstream_ws`.
        """

    @abc.abstractmethod
    async def _open_upstream_ws(self) -> websockets.ClientConnection:
        """Open the upstream WebSocket with backend-specific URL + auth."""


# JSON Schema keywords Gemini's OpenAPI 3.0 Schema doesn't accept on
# function-declaration parameters. Stripping rather than rejecting:
# we want to send the best schema we can, not refuse to call the tool.
_GEMINI_SCHEMA_STRIP_KEYS = frozenset({
    "$schema",
    "$id",
    "$ref",
    "$defs",
    "definitions",
    "additionalProperties",
    "patternProperties",
    "unevaluatedProperties",
    "unevaluatedItems",
    "if",
    "then",
    "else",
    "not",
    "dependencies",
    "dependentSchemas",
    "dependentRequired",
})


def _sanitize_schema_for_gemini(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert a JSON Schema to the subset Gemini's Live API accepts.

    Gemini's ``functionDeclarations[].parameters`` follows OpenAPI 3.0
    Schema, which is a strict subset of JSON Schema Draft 7.
    Mismatches the orchestrator's tool schemas tend to hit:

    - ``"type": ["X", "null"]`` (union types) → split into
      ``"type": "X", "nullable": true``.
    - ``anyOf`` / ``oneOf`` / ``allOf`` containing exactly one schema
      and one ``{"type": "null"}`` (the OpenAPI pattern for optionals)
      → flatten to the non-null branch + ``nullable: true``.
    - ``additionalProperties``, ``$schema``, ``$ref``, etc. → strip.

    Everything else (``type``, ``description``, ``properties``,
    ``required``, ``items``, ``enum``, ``format``, ``minimum``,
    ``maximum``, ``nullable``) passes through. Recurses into
    ``properties``, ``items``, ``anyOf``/``oneOf``/``allOf``.

    Returns a new dict — does not mutate the input.
    """
    if not isinstance(schema, dict):
        return schema

    out: dict[str, Any] = {}
    nullable = False

    # Handle anyOf/oneOf/allOf with a null branch (optional pattern).
    for combinator in ("anyOf", "oneOf", "allOf"):
        if combinator in schema:
            branches = schema[combinator]
            if isinstance(branches, list):
                non_null = [b for b in branches if not (isinstance(b, dict) and b.get("type") == "null")]
                has_null = len(non_null) < len(branches)
                if has_null:
                    nullable = True
                if len(non_null) == 1:
                    # Pattern: anyOf:[{...}, {type: null}] → merge the
                    # single non-null branch directly into ``out`` and
                    # drop the combinator (Gemini still rejects raw
                    # anyOf even of length 1 in practice).
                    out.update(_sanitize_schema_for_gemini(non_null[0]))
                elif len(non_null) > 1:
                    # Multi-branch union — keep as anyOf with each
                    # branch sanitized. Gemini accepts anyOf in some
                    # cases; if it still rejects, the caller will see
                    # the error and refine.
                    out[combinator] = [_sanitize_schema_for_gemini(b) for b in non_null]
                # Mark this combinator handled.
                # (Falls through — we don't break since multiple combinators
                # are rare; we sanitize each.)

    for k, v in schema.items():
        if k in _GEMINI_SCHEMA_STRIP_KEYS:
            continue
        if k in ("anyOf", "oneOf", "allOf"):
            # Already handled above.
            continue
        if k == "type":
            if isinstance(v, list):
                # ["X", "null"] → "X" + nullable=True; ["X", "Y"] →
                # keep first non-null (best-effort — Gemini wants a
                # scalar type).
                non_null = [t for t in v if t != "null"]
                nullable = nullable or ("null" in v)
                out["type"] = non_null[0] if non_null else "string"
            else:
                out["type"] = v
        elif k == "properties" and isinstance(v, dict):
            out["properties"] = {
                pname: _sanitize_schema_for_gemini(pschema)
                for pname, pschema in v.items()
            }
        elif k == "items" and isinstance(v, dict):
            out["items"] = _sanitize_schema_for_gemini(v)
        elif k == "items" and isinstance(v, list):
            # Tuple-form items — Gemini doesn't support; collapse to
            # the first entry as a best-effort.
            if v:
                out["items"] = _sanitize_schema_for_gemini(v[0])
        else:
            out[k] = v

    if nullable:
        out["nullable"] = True
    return out
