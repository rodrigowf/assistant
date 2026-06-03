"""Unit tests for the Gemini Live voice provider.

Exercises the AI Studio backend (the legacy default) for protocol-level
behaviour shared by both backends — translator + format_* methods +
relay hook surface. Vertex-specific tests live next to these as
:func:`test_vertex_*`. Upstream WebSocket calls are not exercised here
(no real Gemini Live mock) — those live in
``tests/test_voice_relay_hooks.py`` which drives a generic WS double
through the relay.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from orchestrator.providers.gemini_voice import (
    GEMINI_LIVE_VOICES,
    GeminiAIStudioBackend,
    GeminiLiveVoiceProvider,
    VertexAIBackend,
)
from orchestrator.providers.gemini_voice_base import _sanitize_schema_for_gemini
from orchestrator.types import (
    ErrorEvent,
    TextDelta,
    ToolUseStart,
    TurnComplete,
    VoiceInterrupted,
)


def _make_provider(**kwargs) -> GeminiAIStudioBackend:
    """Make an AI Studio backend (default test target)."""
    return GeminiAIStudioBackend(**kwargs)


# --- identity ---------------------------------------------------------------


def test_identity_defaults():
    p = _make_provider()
    assert p.provider_name == "google"
    assert p.connection_type == "websocket"
    assert p.model == "gemini-2.5-flash-native-audio-latest"
    assert p.voice == "Puck"


def test_identity_overrides():
    p = _make_provider(model="gemini-3.1-flash-live-preview", voice="Charon")
    assert p.model == "gemini-3.1-flash-live-preview"
    assert p.voice == "Charon"


# --- format_session_config --------------------------------------------------


def test_session_config_minimal():
    p = _make_provider()
    cfg = p.format_session_config(system="You are helpful.", tools=[])

    assert "setup" in cfg
    setup = cfg["setup"]
    assert setup["model"] == "models/gemini-2.5-flash-native-audio-latest"
    assert setup["generationConfig"]["responseModalities"] == ["AUDIO"]
    voice_cfg = setup["generationConfig"]["speechConfig"]["voiceConfig"]
    assert voice_cfg["prebuiltVoiceConfig"]["voiceName"] == "Puck"
    assert setup["systemInstruction"]["parts"][0]["text"] == "You are helpful."
    # No tools → no "tools" key in setup (Gemini rejects empty array as invalid).
    assert "tools" not in setup
    # Both ASR streams are enabled so the chat window sees transcripts.
    assert setup["inputAudioTranscription"] == {}
    assert setup["outputAudioTranscription"] == {}


def test_handshake_direction_is_client_first():
    """Gemini sends setup before setupComplete — opposite of OpenAI/Qwen."""
    p = _make_provider()
    assert p.handshake_direction == "client_first"


def test_translate_output_transcription_emits_text_delta():
    """outputTranscription.text → TextDelta so chat shows what Gemini said."""
    p = _make_provider()
    ev = {"serverContent": {"outputTranscription": {"text": "Hello there"}}}
    out = p.translate_event(ev)
    assert isinstance(out, TextDelta)
    assert out.text == "Hello there"


def test_translate_input_transcription_emits_text_delta():
    """inputTranscription.text → TextDelta so the user's words land in JSONL.

    Live API ships this at top level (older docs) — we accept it.
    """
    p = _make_provider()
    ev = {"inputTranscription": {"text": "what time is it"}}
    out = p.translate_event(ev)
    assert isinstance(out, TextDelta)
    assert out.text == "what time is it"


def test_translate_input_transcription_nested_under_server_content():
    """Newer Live API nests inputTranscription under serverContent."""
    p = _make_provider()
    ev = {"serverContent": {"inputTranscription": {"text": "hello there"}}}
    out = p.translate_event(ev)
    assert isinstance(out, TextDelta)
    assert out.text == "hello there"


def test_session_config_disables_server_vad_when_manual_vad_on():
    """Default mode (GEMINI_MANUAL_VAD unset) is manual VAD: server
    VAD must be disabled so the client controls turn boundaries."""
    p = _make_provider()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("GEMINI_MANUAL_VAD", None)
        cfg = p.format_session_config(system="x", tools=[])
    aad = cfg["setup"]["realtimeInputConfig"]["automaticActivityDetection"]
    assert aad == {"disabled": True}


def test_session_config_uses_server_vad_when_manual_off():
    """GEMINI_MANUAL_VAD=0 falls back to server VAD, tuned as
    conservatively as the Live API allows."""
    p = _make_provider()
    with patch.dict(os.environ, {"GEMINI_MANUAL_VAD": "0"}):
        cfg = p.format_session_config(system="x", tools=[])
    aad = cfg["setup"]["realtimeInputConfig"]["automaticActivityDetection"]
    assert aad["disabled"] is False
    assert aad["startOfSpeechSensitivity"] == "START_SENSITIVITY_LOW"
    assert aad["endOfSpeechSensitivity"] == "END_SENSITIVITY_LOW"
    assert aad["silenceDurationMs"] == 2500


def test_audio_in_sample_rate_declared_for_manual_vad():
    """Gemini must declare its mic sample rate so the relay's manual-VAD
    init gate (which requires non-None ``audio_in_sample_rate``) actually
    fires. Without this, the relay disables server VAD via setup but
    never sends ``activityStart`` — and Gemini sits silent on the audio."""
    p = _make_provider()
    assert p.audio_in_sample_rate == 16000


def test_manual_vad_frames_match_live_api_activity_shape():
    """Manual VAD: start/stop frames must use Live API's
    ``realtimeInput.activityStart`` / ``activityEnd`` envelopes."""
    p = _make_provider()
    assert p.manual_vad_start_frames() == [
        {"realtimeInput": {"activityStart": {}}},
    ]
    assert p.manual_vad_stop_frames() == [
        {"realtimeInput": {"activityEnd": {}}},
    ]
    assert p.supports_manual_vad is True


def test_manual_vad_safety_commit_chunks_long_utterance():
    """Long-utterance safety: close-then-reopen so a multi-minute
    monologue doesn't sit in one unbounded segment."""
    p = _make_provider()
    frames = p.manual_vad_safety_commit_frames()
    assert frames == [
        {"realtimeInput": {"activityEnd": {}}},
        {"realtimeInput": {"activityStart": {}}},
    ]


def test_session_config_skips_system_when_empty():
    p = _make_provider()
    cfg = p.format_session_config(system="", tools=[])
    assert "systemInstruction" not in cfg["setup"]


def test_session_config_translates_anthropic_style_tools():
    p = _make_provider()
    tools = [
        {
            "name": "search_history",
            "description": "Semantic search.",
            "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
        }
    ]
    cfg = p.format_session_config(system="x", tools=tools)
    fns = cfg["setup"]["tools"][0]["functionDeclarations"]
    assert len(fns) == 1
    assert fns[0]["name"] == "search_history"
    assert fns[0]["description"] == "Semantic search."
    assert fns[0]["parameters"] == {"type": "object", "properties": {"q": {"type": "string"}}}


def test_session_config_translates_openai_style_tools():
    p = _make_provider()
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Find a thing.",
                "parameters": {"type": "object"},
            },
        }
    ]
    cfg = p.format_session_config(system="x", tools=tools)
    fns = cfg["setup"]["tools"][0]["functionDeclarations"]
    assert fns[0]["name"] == "lookup"
    assert fns[0]["description"] == "Find a thing."
    assert fns[0]["parameters"] == {"type": "object"}


def test_session_config_voice_override():
    p = _make_provider()
    cfg = p.format_session_config(system="", tools=[], voice="Kore")
    voice_cfg = cfg["setup"]["generationConfig"]["speechConfig"]["voiceConfig"]
    assert voice_cfg["prebuiltVoiceConfig"]["voiceName"] == "Kore"


# --- format_audio_in --------------------------------------------------------


def test_format_audio_in_shapes_realtime_input():
    p = _make_provider()
    frame = p.format_audio_in("AAAA")
    assert frame == {
        "realtimeInput": {
            "audio": {"data": "AAAA", "mimeType": "audio/pcm;rate=16000"},
        },
    }


# --- extract_audio_out ------------------------------------------------------


def test_extract_audio_out_pulls_inline_data():
    raw = {
        "serverContent": {
            "modelTurn": {
                "parts": [
                    {"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": "DEADBEEF"}}
                ],
            },
        },
    }
    assert GeminiAIStudioBackend.extract_audio_out(raw) == "DEADBEEF"


def test_extract_audio_out_ignores_text_parts():
    raw = {"serverContent": {"modelTurn": {"parts": [{"text": "hello"}]}}}
    assert GeminiAIStudioBackend.extract_audio_out(raw) is None


def test_extract_audio_out_returns_none_for_non_server_content():
    assert GeminiAIStudioBackend.extract_audio_out({"setupComplete": {}}) is None
    assert GeminiAIStudioBackend.extract_audio_out({"toolCall": {}}) is None
    assert GeminiAIStudioBackend.extract_audio_out({}) is None


def test_extract_audio_out_skips_non_audio_inline_data():
    raw = {
        "serverContent": {
            "modelTurn": {
                "parts": [{"inlineData": {"mimeType": "image/png", "data": "AAAA"}}]
            },
        },
    }
    assert GeminiAIStudioBackend.extract_audio_out(raw) is None


# --- translate_event --------------------------------------------------------


def test_translate_setup_complete_returns_none():
    p = _make_provider()
    assert p.translate_event({"setupComplete": {}}) is None


def test_translate_text_part_emits_text_delta():
    p = _make_provider()
    ev = {"serverContent": {"modelTurn": {"parts": [{"text": "hello "}]}}}
    out = p.translate_event(ev)
    assert isinstance(out, TextDelta)
    assert out.text == "hello "


def test_translate_turn_complete_emits_turn_complete():
    p = _make_provider()
    ev = {
        "serverContent": {
            "turnComplete": True,
            "usageMetadata": {"promptTokenCount": 42, "candidatesTokenCount": 17},
        }
    }
    out = p.translate_event(ev)
    assert isinstance(out, TurnComplete)
    assert out.input_tokens == 42
    assert out.output_tokens == 17


def test_translate_interrupted_emits_voice_interrupted():
    p = _make_provider()
    p._current_transcript = "partial words"
    ev = {"serverContent": {"interrupted": True}}
    out = p.translate_event(ev)
    assert isinstance(out, VoiceInterrupted)
    assert out.partial_text == "partial words"


def test_translate_tool_call_emits_tool_use_start():
    p = _make_provider()
    ev = {
        "toolCall": {
            "functionCalls": [
                {"id": "call-1", "name": "search_history", "args": {"q": "hello"}}
            ]
        }
    }
    out = p.translate_event(ev)
    assert isinstance(out, ToolUseStart)
    assert out.tool_call_id == "call-1"
    assert out.tool_name == "search_history"
    assert out.tool_input == {"q": "hello"}


def test_translate_error_event():
    p = _make_provider()
    ev = {"error": {"code": "INTERNAL", "message": "boom"}}
    out = p.translate_event(ev)
    assert isinstance(out, ErrorEvent)
    assert out.detail == "boom"


# --- format_tool_result -----------------------------------------------------


def test_format_tool_result_uses_tracked_name():
    """The tool name must be threaded from the toolCall through to the response."""
    p = _make_provider()
    # Simulate the relay/agent loop seeing the toolCall first.
    p.translate_event({
        "toolCall": {
            "functionCalls": [{"id": "call-7", "name": "lookup", "args": {}}]
        }
    })
    cmds = p.format_tool_result("call-7", "the result")
    assert len(cmds) == 1
    fr = cmds[0]["toolResponse"]["functionResponses"][0]
    assert fr["id"] == "call-7"
    assert fr["name"] == "lookup"
    assert fr["response"] == {"output": "the result"}
    # And the lookup map gets consumed so subsequent results don't reuse it.
    assert "call-7" not in p.pending_calls


def test_format_tool_result_with_no_prior_call_uses_empty_name():
    """If we never saw the toolCall (shouldn't happen, but be permissive)."""
    p = _make_provider()
    cmds = p.format_tool_result("unknown-call", "x")
    assert cmds[0]["toolResponse"]["functionResponses"][0]["name"] == ""


# --- relay hooks ------------------------------------------------------------


def test_is_recoverable_error_default_false():
    """Gemini Live doesn't reuse Qwen's URL-validator boilerplate."""
    p = _make_provider()
    assert p.is_recoverable_error(ConnectionError("InvalidParameter")) is False
    assert p.is_recoverable_error(RuntimeError("anything")) is False


def test_is_recoverable_error_stale_handle_one_shot_recovery():
    """1008 "session expired" with a captured handle but no prior goAway:
    drop the (poisoned) handle and recover once. Replays of the same
    failure after recovery are fatal."""
    p = _make_provider()
    p._resumption_handle = "stale-handle-from-previous-session"
    exc = ConnectionError(
        "received 1008 (policy violation) BidiGenerateContent session expired; "
        "then sent 1008 (policy violation) BidiGenerateContent session expired"
    )
    # First time: recover.
    assert p.is_recoverable_error(exc) is True
    assert p._resumption_handle is None
    assert p._stale_handle_recovery_used is True
    # Second time without a setupComplete in between: fatal (no loop).
    assert p.is_recoverable_error(exc) is False


def test_is_recoverable_error_session_expired_without_handle_is_fatal():
    """If we never had a handle, a 1008 isn't a stale-handle situation —
    don't try to recover (could be a real quota / policy denial)."""
    p = _make_provider()
    assert p._resumption_handle is None
    exc = ConnectionError("1008 BidiGenerateContent session expired")
    assert p.is_recoverable_error(exc) is False


def test_should_gate_event_default_false():
    """Gemini Live doesn't reject concurrent client messages."""
    p = _make_provider()
    assert p.should_gate_event({"realtimeInput": {}}) is False
    assert p.should_gate_event({"toolResponse": {}}) is False


def test_build_keepalive_chunk_default_none():
    """No keepalive needed for Gemini Live."""
    p = _make_provider()
    assert p.build_keepalive_chunk() is None


# --- get_connection_info ----------------------------------------------------


@pytest.mark.asyncio
async def test_open_upstream_requires_api_key():
    """API key is checked at WS-open time, not at get_connection_info."""
    p = _make_provider()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("GEMINI_API_KEY", None)
        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            await p.open_upstream()


@pytest.mark.asyncio
async def test_get_connection_info_shape():
    p = _make_provider()
    with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=False):
        info = await p.get_connection_info()
    assert info["connection_type"] == "websocket"
    assert info["audio_in_format"] == {"sample_rate": 16000, "encoding": "pcm16"}
    assert info["audio_out_format"] == {"sample_rate": 24000, "encoding": "pcm16"}
    assert info["ephemeral_token"] is None
    assert info["model"] == "gemini-2.5-flash-native-audio-latest"
    assert info["voice"] == "Puck"
    assert info["audio_relay"] == "backend"


# --- voice catalogue --------------------------------------------------------


def test_voice_catalogue_includes_expected_prebuilt_voices():
    # Sanity: the static catalogue agrees with the documented Live voices.
    expected = {"Puck", "Charon", "Kore", "Fenrir", "Aoede", "Leda", "Orus", "Zephyr"}
    assert set(GEMINI_LIVE_VOICES) == expected


# --- registry integration ---------------------------------------------------


def test_registry_resolves_google_to_gemini_provider():
    from orchestrator.providers.voice_registry import (
        VOICE_MODELS,
        get_provider_class,
    )

    cls = get_provider_class("google")
    # ``get_provider_class`` returns the *default* concrete backend
    # (Vertex). The dispatcher in ``instantiate_provider`` picks the
    # right class based on the ``endpoint`` argument; this lookup is
    # only used by type-only callers.
    assert cls is VertexAIBackend

    # VOICE_MODELS["google"] is populated and has at least one default entry.
    google_models = VOICE_MODELS["google"]
    assert any(m.get("default") for m in google_models)
    default = next(m for m in google_models if m["default"])
    assert default["voice"] == "Puck"
    assert default["voices"]


def test_instantiate_provider_dispatches_on_endpoint():
    """Picking endpoint=aistudio yields the AI Studio backend."""
    from orchestrator.providers.voice_registry import instantiate_provider

    aistudio = instantiate_provider(
        "google",
        "gemini-2.5-flash-native-audio-latest",
        "Puck",
        "",
        endpoint="aistudio",
    )
    assert isinstance(aistudio, GeminiAIStudioBackend)
    assert aistudio.endpoint_id == "aistudio"

    vertex = instantiate_provider(
        "google",
        "gemini-live-2.5-flash-native-audio",
        "Puck",
        "",
        endpoint="vertex",
    )
    assert isinstance(vertex, VertexAIBackend)
    assert vertex.endpoint_id == "vertex"


# --- JSON Schema sanitization ----------------------------------------------
#
# Gemini's Live API uses OpenAPI 3.0 Schema, a strict subset of JSON
# Schema Draft 7. The orchestrator's tool registry produces Draft-7
# schemas (used by Anthropic/OpenAI), so we sanitize before sending.
# These tests pin the conversion rules — without them, Gemini rejects
# the setup with HTTP 1007 / "Invalid JSON payload" and the session
# never starts.


def test_sanitize_passes_through_simple_schema():
    src = {
        "type": "object",
        "properties": {
            "q": {"type": "string", "description": "Query string"},
        },
        "required": ["q"],
    }
    out = _sanitize_schema_for_gemini(src)
    assert out == src
    # And it returned a copy, not the same object.
    assert out is not src


def test_sanitize_collapses_type_array_with_null_to_nullable():
    """The exact bug that failed the first live Gemini smoke test."""
    src = {
        "type": "object",
        "properties": {
            "max_messages": {
                "type": ["integer", "null"],
                "description": "How many",
            },
        },
    }
    out = _sanitize_schema_for_gemini(src)
    mm = out["properties"]["max_messages"]
    assert mm["type"] == "integer"
    assert mm["nullable"] is True
    assert mm["description"] == "How many"


def test_sanitize_strips_disallowed_keywords():
    src = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "additionalProperties": False,
        "patternProperties": {".*": {"type": "string"}},
        "properties": {"x": {"type": "string"}},
    }
    out = _sanitize_schema_for_gemini(src)
    assert "$schema" not in out
    assert "additionalProperties" not in out
    assert "patternProperties" not in out
    assert out["properties"] == {"x": {"type": "string"}}


def test_sanitize_flattens_optional_anyof_pattern():
    """anyOf:[{...}, {"type": "null"}] → flatten + nullable."""
    src = {
        "anyOf": [
            {"type": "string", "enum": ["a", "b"]},
            {"type": "null"},
        ]
    }
    out = _sanitize_schema_for_gemini(src)
    assert out.get("type") == "string"
    assert out.get("enum") == ["a", "b"]
    assert out.get("nullable") is True
    assert "anyOf" not in out


def test_sanitize_recurses_into_items():
    src = {
        "type": "array",
        "items": {
            "type": ["string", "null"],
        },
    }
    out = _sanitize_schema_for_gemini(src)
    assert out["type"] == "array"
    assert out["items"]["type"] == "string"
    assert out["items"]["nullable"] is True


def test_sanitize_collapses_tuple_form_items():
    src = {
        "type": "array",
        "items": [{"type": "string"}, {"type": "integer"}],
    }
    out = _sanitize_schema_for_gemini(src)
    # Best-effort: keep the first.
    assert out["items"] == {"type": "string"}


def test_sanitize_nested_properties_recurse():
    src = {
        "type": "object",
        "properties": {
            "nested": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "leaf": {"type": ["string", "null"]},
                },
            },
        },
    }
    out = _sanitize_schema_for_gemini(src)
    nested = out["properties"]["nested"]
    assert "additionalProperties" not in nested
    leaf = nested["properties"]["leaf"]
    assert leaf["type"] == "string"
    assert leaf["nullable"] is True


def test_session_config_sanitizes_tool_schemas_end_to_end():
    """The user-visible smoke test — read_agent_session's max_messages
    is the exact schema that broke Gemini Live in the first attempt."""
    p = _make_provider()
    tools = [
        {
            "name": "read_agent_session",
            "description": "Read the persisted history of an agent session.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "max_messages": {
                        "type": ["integer", "null"],
                        "description": "How many",
                    },
                },
                "required": ["session_id"],
            },
        }
    ]
    cfg = p.format_session_config(system="x", tools=tools)
    fn = cfg["setup"]["tools"][0]["functionDeclarations"][0]
    mm = fn["parameters"]["properties"]["max_messages"]
    assert mm["type"] == "integer"
    assert mm["nullable"] is True
    # Sanity: the un-affected property is unchanged.
    assert fn["parameters"]["properties"]["session_id"] == {"type": "string"}
