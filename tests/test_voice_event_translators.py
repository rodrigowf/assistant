"""Increment E — dispatcher coverage tests for the
``_EVENT_TRANSLATORS`` tables on each voice provider.

Goal: every key in each provider's dispatch table is exercised by a
representative event. This is the safety net that catches silent
translation drops if a future patch accidentally removes an entry
(per plan §E risk: "Mistakes here cause silent translation drops").

For each provider:

* OpenAI / Qwen: top-level ``type`` dispatch — a fake event with the
  matching ``type`` field is fed through ``translate_event``; the
  result must NOT be None for "active" events (those whose translator
  always returns something), and the canonical event type matches the
  expected class.
* Gemini: ordered probe dispatch — a representative event for each
  probe class is fed through; the result class matches.

We deliberately don't pin every output field — :doc:`tests/test_gemini_voice`
and :doc:`tests/test_voice_gemini_dispatch` already cover that. This
file is purely about the dispatch surface.
"""

from __future__ import annotations

import pytest

from orchestrator.providers.openai_voice import OpenAIVoiceProvider
from orchestrator.providers.qwen_voice import QwenVoiceProvider
from orchestrator.providers.gemini_voice import GeminiAIStudioBackend
from orchestrator.types import (
    ErrorEvent,
    TextComplete,
    TextDelta,
    ToolUseStart,
    TurnComplete,
    VoiceInterrupted,
)


# ---------- OpenAI dispatch table ------------------------------------------


def _openai():
    return OpenAIVoiceProvider(model="gpt-realtime", voice="cedar")


def test_openai_table_text_delta_both_keys():
    """GA + legacy beta both map to the same TextDelta translator."""
    p = _openai()
    assert isinstance(
        p.translate_event({
            "type": "response.output_audio_transcript.delta",
            "delta": "hello",
        }),
        TextDelta,
    )
    assert isinstance(
        p.translate_event({
            "type": "response.audio_transcript.delta",
            "delta": "hello",
        }),
        TextDelta,
    )


def test_openai_table_text_complete_both_keys():
    p = _openai()
    for key in (
        "response.output_audio_transcript.done",
        "response.audio_transcript.done",
    ):
        result = p.translate_event({"type": key, "transcript": "done"})
        assert isinstance(result, TextComplete)


def test_openai_table_function_call_done():
    p = _openai()
    # Register a call first so peek_name/peek_args find it.
    p.register_call("call-1", "search_history")
    p.accumulate_args("call-1", '{"query": "x"}')
    result = p.translate_event({
        "type": "response.function_call_arguments.done",
        "call_id": "call-1",
    })
    assert isinstance(result, ToolUseStart)
    assert result.tool_call_id == "call-1"
    assert result.tool_name == "search_history"
    assert result.tool_input == {"query": "x"}


def test_openai_table_response_done():
    p = _openai()
    result = p.translate_event({
        "type": "response.done",
        "response": {"usage": {"input_tokens": 10, "output_tokens": 5}},
    })
    assert isinstance(result, TurnComplete)
    assert result.input_tokens == 10
    assert result.output_tokens == 5


def test_openai_table_speech_started():
    p = _openai()
    result = p.translate_event({"type": "input_audio_buffer.speech_started"})
    assert isinstance(result, VoiceInterrupted)


def test_openai_table_error():
    p = _openai()
    result = p.translate_event({
        "type": "error",
        "error": {"code": "bad_request", "message": "oops"},
    })
    assert isinstance(result, ErrorEvent)


def test_openai_table_unknown_type_returns_none():
    p = _openai()
    assert p.translate_event({"type": "something.weird"}) is None
    # Empty/missing type also yields None.
    assert p.translate_event({}) is None


def test_openai_table_keys_are_all_dispatched():
    """Every key in the dispatch table must resolve to a callable method
    that returns the same type when fed a representative event. This
    catches typos in the method-name strings.
    """
    p = _openai()
    for event_type, method_name in OpenAIVoiceProvider._EVENT_TRANSLATORS.items():
        assert hasattr(p, method_name), (
            f"dispatch table entry {event_type!r} → {method_name!r} "
            f"doesn't exist on the provider"
        )


# ---------- Qwen dispatch table --------------------------------------------


def _qwen():
    return QwenVoiceProvider(
        model="qwen3-omni-flash-realtime",
        voice="Cherry",
    )


def test_qwen_table_audio_transcript_delta():
    p = _qwen()
    result = p.translate_event({
        "type": "response.audio_transcript.delta",
        "delta": "ni hao",
    })
    assert isinstance(result, TextDelta)


def test_qwen_table_text_delta_routes_to_same_translator():
    """Both ``response.audio_transcript.delta`` AND
    ``response.text.delta`` map to the delta translator.
    """
    p = _qwen()
    result = p.translate_event({
        "type": "response.text.delta",
        "delta": "raw text",
    })
    assert isinstance(result, TextDelta)


def test_qwen_table_audio_transcript_done_vs_text_done():
    """Done payloads differ: ``transcript`` for audio, ``text`` for raw."""
    p = _qwen()
    assert isinstance(
        p.translate_event({
            "type": "response.audio_transcript.done",
            "transcript": "T",
        }),
        TextComplete,
    )
    assert isinstance(
        p.translate_event({"type": "response.text.done", "text": "T"}),
        TextComplete,
    )


def test_qwen_table_function_call_done():
    p = _qwen()
    p.register_call("call-1", "tool_x")
    p.accumulate_args("call-1", '{"a": 1}')
    result = p.translate_event({
        "type": "response.function_call_arguments.done",
        "call_id": "call-1",
    })
    assert isinstance(result, ToolUseStart)


def test_qwen_table_response_done():
    p = _qwen()
    result = p.translate_event({
        "type": "response.done",
        "response": {"usage": {"input_tokens": 2, "output_tokens": 3}},
    })
    assert isinstance(result, TurnComplete)


def test_qwen_table_speech_started():
    p = _qwen()
    assert isinstance(
        p.translate_event({"type": "input_audio_buffer.speech_started"}),
        VoiceInterrupted,
    )


def test_qwen_table_error():
    p = _qwen()
    result = p.translate_event({
        "type": "error",
        "error": {"code": "InvalidParameter", "message": "bad"},
    })
    assert isinstance(result, ErrorEvent)


def test_qwen_table_keys_are_all_dispatched():
    p = _qwen()
    for event_type, method_name in QwenVoiceProvider._EVENT_TRANSLATORS.items():
        assert hasattr(p, method_name)


# ---------- Gemini ordered-probe dispatch ----------------------------------


def _gemini():
    return GeminiAIStudioBackend()


def test_gemini_dispatch_setup_complete_returns_none():
    p = _gemini()
    assert p.translate_event({"setupComplete": {}}) is None


def test_gemini_dispatch_server_content_interrupted():
    p = _gemini()
    result = p.translate_event({"serverContent": {"interrupted": True}})
    assert isinstance(result, VoiceInterrupted)


def test_gemini_dispatch_input_transcription_nested():
    p = _gemini()
    result = p.translate_event({
        "serverContent": {"inputTranscription": {"text": "hello"}},
    })
    assert isinstance(result, TextDelta)
    assert result.text == "hello"


def test_gemini_dispatch_input_transcription_empty_text_falls_through_to_output():
    """Legacy if/elif fell through to outputTranscription when
    inputTranscription had empty text. The dispatch table must
    preserve that.
    """
    p = _gemini()
    result = p.translate_event({
        "serverContent": {
            "inputTranscription": {"text": ""},
            "outputTranscription": {"text": "reply"},
        },
    })
    assert isinstance(result, TextDelta)
    assert result.text == "reply"


def test_gemini_dispatch_output_transcription_nested():
    p = _gemini()
    result = p.translate_event({
        "serverContent": {"outputTranscription": {"text": "reply"}},
    })
    assert isinstance(result, TextDelta)


def test_gemini_dispatch_model_turn_text_parts():
    p = _gemini()
    result = p.translate_event({
        "serverContent": {
            "modelTurn": {"parts": [{"text": "first"}, {"text": "second"}]},
        },
    })
    assert isinstance(result, TextDelta)
    # First non-empty part wins.
    assert result.text == "first"


def test_gemini_dispatch_turn_complete_with_usage():
    p = _gemini()
    result = p.translate_event({
        "serverContent": {
            "turnComplete": True,
            "usageMetadata": {
                "promptTokenCount": 100, "candidatesTokenCount": 50,
            },
        },
    })
    assert isinstance(result, TurnComplete)
    assert result.input_tokens == 100
    assert result.output_tokens == 50


def test_gemini_dispatch_top_level_input_transcription():
    p = _gemini()
    result = p.translate_event({"inputTranscription": {"text": "old"}})
    assert isinstance(result, TextDelta)


def test_gemini_dispatch_tool_call_registers_and_returns_first():
    p = _gemini()
    result = p.translate_event({
        "toolCall": {
            "functionCalls": [
                {"id": "c1", "name": "search", "args": {"q": "x"}},
                {"id": "c2", "name": "fetch", "args": {"u": "y"}},
            ],
        },
    })
    assert isinstance(result, ToolUseStart)
    assert result.tool_call_id == "c1"
    # Both names recorded for later format_tool_result.
    assert p.peek_name("c1") == "search"
    assert p.peek_name("c2") == "fetch"


def test_gemini_dispatch_tool_call_no_valid_calls_returns_none():
    """``toolCall is not None`` short-circuits even when there are no
    valid calls — matches legacy ``return first`` (where first is None).
    """
    p = _gemini()
    result = p.translate_event({"toolCall": {"functionCalls": []}})
    assert result is None
    # And no fall-through to error path even if "error" key also present.
    result = p.translate_event({
        "toolCall": {"functionCalls": []},
        "error": {"code": "ignored", "message": "ignored"},
    })
    assert result is None


def test_gemini_dispatch_top_level_error():
    p = _gemini()
    result = p.translate_event({"error": {"code": "bad", "message": "oops"}})
    assert isinstance(result, ErrorEvent)


def test_gemini_dispatch_empty_event_returns_none():
    p = _gemini()
    assert p.translate_event({}) is None
