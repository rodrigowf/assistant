"""Unit tests for :class:`GeminiLiveVoiceProvider`.

Covers the translator + format_* methods + relay hook surface. Upstream
WebSocket calls are not exercised here (no real Gemini Live mock) —
those live in tests/test_voice_relay_hooks.py which drives a generic
WS double through the relay.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from orchestrator.providers.gemini_voice import (
    GEMINI_LIVE_VOICES,
    GeminiLiveVoiceProvider,
)
from orchestrator.types import (
    ErrorEvent,
    TextDelta,
    ToolUseStart,
    TurnComplete,
    VoiceInterrupted,
)


def _make_provider(**kwargs) -> GeminiLiveVoiceProvider:
    return GeminiLiveVoiceProvider(**kwargs)


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
    assert GeminiLiveVoiceProvider.extract_audio_out(raw) == "DEADBEEF"


def test_extract_audio_out_ignores_text_parts():
    raw = {"serverContent": {"modelTurn": {"parts": [{"text": "hello"}]}}}
    assert GeminiLiveVoiceProvider.extract_audio_out(raw) is None


def test_extract_audio_out_returns_none_for_non_server_content():
    assert GeminiLiveVoiceProvider.extract_audio_out({"setupComplete": {}}) is None
    assert GeminiLiveVoiceProvider.extract_audio_out({"toolCall": {}}) is None
    assert GeminiLiveVoiceProvider.extract_audio_out({}) is None


def test_extract_audio_out_skips_non_audio_inline_data():
    raw = {
        "serverContent": {
            "modelTurn": {
                "parts": [{"inlineData": {"mimeType": "image/png", "data": "AAAA"}}]
            },
        },
    }
    assert GeminiLiveVoiceProvider.extract_audio_out(raw) is None


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
async def test_get_connection_info_requires_api_key():
    p = _make_provider()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("GEMINI_API_KEY", None)
        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            await p.get_connection_info()


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
    assert cls is GeminiLiveVoiceProvider

    # VOICE_MODELS["google"] is populated and has at least one default entry.
    google_models = VOICE_MODELS["google"]
    assert any(m.get("default") for m in google_models)
    default = next(m for m in google_models if m["default"])
    assert default["voice"] == "Puck"
    assert default["voices"]
