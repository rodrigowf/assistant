"""Parity contract: Increment G — VoicePersister extraction preserves
the exact JSONL write sequence of ``OrchestratorSession.process_voice_event``.

This is the parity test the plan §G describes:

> Replay a recorded session through the new persister and diff JSONL
> against the file already on disk. No semantic diff allowed.

We pin this at the unit level using scripted event sequences that
exercise each persistence path:

1. **OpenAI / Qwen happy path**: input_audio_transcription.completed →
   audio_transcript.done (stages) → response.done with status=completed
   (flushes the staged assistant transcript).
2. **OpenAI audio_transcript fragment-then-barge-in**: audio_transcript
   .done arrives but response.done arrives with status="cancelled" —
   the staged transcript MUST be dropped, not persisted.
3. **Gemini Live happy path**: token-level inputTranscription deltas
   buffered into one user JSONL entry on first output delta; multi-
   delta assistant transcript flushed on turnComplete.
4. **Gemini barge-in**: serverContent.interrupted writes a
   ``voice_interrupted`` entry.
5. **Tool call (OpenAI/Qwen shape)**: function_call_arguments.done with
   accumulated args persists tool_use + tool_result.
6. **Tool call (Gemini shape)**: toolCall.functionCalls[*] each persist
   tool_use + tool_result.

For each, we capture the ``_writer.append`` calls — the writes are the
contract. Byte-for-byte parity within the variable-timestamp tolerance
described in ``_normalize_entry``.

Per plan §0.3: this test pins HEAD behavior. The Inc G refactor must
keep it green. If the test fails after Inc G, the refactor broke
persistence — which is a non-negotiable regression.
"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import MagicMock, AsyncMock

import pytest

from orchestrator.session import OrchestratorSession


def _make_voice_session(provider_name: str = "openai") -> OrchestratorSession:
    """Build a minimal OrchestratorSession in voice mode with a mocked
    provider + writer + recorder. The persistence code only reads
    ``provider.provider_name`` and ``provider.pending_calls`` /
    ``provider._pending_call_args`` (Inc E mixin), and ``writer.append``
    from the writer.
    """
    config = MagicMock()
    config.summarizer_model = None
    context = {"pool": MagicMock(), "store": MagicMock()}
    s = OrchestratorSession(
        config=config,
        context=context,
        voice=True,
        local_id="t-persister-parity",
    )

    provider = MagicMock()
    provider.provider_name = provider_name
    provider.inject_event = AsyncMock()
    # Tool-call accumulator surfaces — both legacy and Inc-E mixin
    # back-compat. process_voice_event reads from these on the
    # function_call_arguments.done path.
    provider.pending_calls = {}
    provider._pending_call_args = {}
    provider.format_tool_result = MagicMock(return_value=[
        {"type": "conversation.item.create"}, {"type": "response.create"},
    ])
    s._voice_provider = provider

    writer = MagicMock()
    writer.append = MagicMock()
    s._writer = writer

    # No audio recorder — segments are None, content = "[voice] X"
    # for users and the bare transcript for assistant.
    s._audio_recorder = None

    return s


def _normalize_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Strip variable fields (timestamp) so parity asserts on the
    payload shape, not the wall clock."""
    out = copy.deepcopy(entry)
    out.pop("timestamp", None)
    return out


def _writes(session: OrchestratorSession) -> list[dict[str, Any]]:
    """Return the normalized sequence of writer.append payloads."""
    return [_normalize_entry(c.args[0]) for c in session._writer.append.call_args_list]


# ---------- 1. OpenAI / Qwen — input transcript + assistant flush ---------


@pytest.mark.asyncio
async def test_openai_input_transcript_completed_persists_user_turn():
    s = _make_voice_session("openai")
    await s.process_voice_event({
        "type": "conversation.item.input_audio_transcription.completed",
        "transcript": "hello there",
    })
    assert _writes(s) == [
        {
            "type": "user",
            "message": {"role": "user", "content": "[voice] hello there"},
            "source": "voice_transcription",
        },
    ]


@pytest.mark.asyncio
async def test_openai_assistant_transcript_done_stages_but_does_not_persist_until_response_done():
    s = _make_voice_session("openai")
    await s.process_voice_event({
        "type": "response.audio_transcript.done",
        "transcript": "I'll help with that.",
    })
    # Stage only — no write yet.
    assert _writes(s) == []
    assert s._pending_assistant_transcript == "I'll help with that."


@pytest.mark.asyncio
async def test_openai_response_done_completed_flushes_staged_transcript():
    s = _make_voice_session("openai")
    await s.process_voice_event({
        "type": "response.audio_transcript.done",
        "transcript": "Sure.",
    })
    await s.process_voice_event({
        "type": "response.done",
        "response": {"status": "completed"},
    })
    assert _writes(s) == [
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": "Sure."},
            "source": "voice_response",
        },
    ]
    # Buffer cleared.
    assert s._pending_assistant_transcript is None


@pytest.mark.asyncio
async def test_openai_response_done_cancelled_drops_staged_transcript():
    """Cancelled turns must NOT persist their (fragment) transcript —
    that's the load-bearing behavior preventing pollution like
    'Yeah, I think' after barge-in.
    """
    s = _make_voice_session("openai")
    await s.process_voice_event({
        "type": "response.audio_transcript.done",
        "transcript": "I was about to say",
    })
    await s.process_voice_event({
        "type": "response.done",
        "response": {"status": "cancelled"},
    })
    assert _writes(s) == []
    assert s._pending_assistant_transcript is None


@pytest.mark.asyncio
async def test_openai_speech_started_persists_voice_interrupted():
    s = _make_voice_session("openai")
    await s.process_voice_event({"type": "input_audio_buffer.speech_started"})
    assert _writes(s) == [{"type": "voice_interrupted"}]


# ---------- 2. OpenAI ga-keyed variants route to same translator ----------


@pytest.mark.asyncio
async def test_openai_ga_audio_transcript_done_also_stages():
    """``response.output_audio_transcript.done`` (GA gpt-realtime) must
    stage the same way the legacy beta variant does.
    """
    s = _make_voice_session("openai")
    await s.process_voice_event({
        "type": "response.output_audio_transcript.done",
        "transcript": "Got it.",
    })
    assert s._pending_assistant_transcript == "Got it."


# ---------- 3. Gemini Live — token-level user delta accumulation ----------


@pytest.mark.asyncio
async def test_gemini_input_transcription_buffered_then_flushed_on_output_delta():
    s = _make_voice_session("google")

    # Token-level user transcript deltas (no top-level type).
    await s.process_voice_event({
        "serverContent": {"inputTranscription": {"text": "Hi "}},
    })
    await s.process_voice_event({
        "serverContent": {"inputTranscription": {"text": "there"}},
    })
    # Nothing persisted yet — buffered.
    assert _writes(s) == []
    assert s._pending_user_transcript == "Hi there"

    # First assistant output delta flushes user transcript + buffers assistant.
    await s.process_voice_event({
        "serverContent": {"outputTranscription": {"text": "Hello"}},
    })
    assert _writes(s) == [
        {
            "type": "user",
            "message": {"role": "user", "content": "[voice] Hi there"},
            "source": "voice_transcription",
        },
    ]
    assert s._pending_user_transcript is None
    assert s._pending_assistant_transcript == "Hello"

    # turnComplete persists assistant.
    await s.process_voice_event({
        "serverContent": {"turnComplete": True},
    })
    writes = _writes(s)
    assert writes[-1] == {
        "type": "assistant",
        "message": {"role": "assistant", "content": "Hello"},
        "source": "voice_response",
    }
    assert s._pending_assistant_transcript is None


@pytest.mark.asyncio
async def test_gemini_turn_complete_without_output_still_flushes_user_transcript():
    """Failsafe flush — audio-only turns (no text output) must still
    persist the buffered user transcript.
    """
    s = _make_voice_session("google")
    await s.process_voice_event({
        "serverContent": {"inputTranscription": {"text": "ping"}},
    })
    await s.process_voice_event({
        "serverContent": {"turnComplete": True},
    })
    writes = _writes(s)
    assert {
        "type": "user",
        "message": {"role": "user", "content": "[voice] ping"},
        "source": "voice_transcription",
    } in writes


@pytest.mark.asyncio
async def test_gemini_interrupted_persists_voice_interrupted():
    s = _make_voice_session("google")
    await s.process_voice_event({
        "serverContent": {"interrupted": True},
    })
    assert _writes(s) == [{"type": "voice_interrupted"}]


# ---------- 4. is_injecting gating ----------------------------------------


@pytest.mark.asyncio
async def test_openai_input_transcript_suppressed_while_injecting():
    """While listen_recording is replaying past audio, every
    fragment-transcription event is bogus — must be dropped.
    """
    s = _make_voice_session("openai")
    # Force injecting flag on.
    s._injection_until = 9e18
    s._injection_active = True
    await s.process_voice_event({
        "type": "conversation.item.input_audio_transcription.completed",
        "transcript": "ghost",
    })
    assert _writes(s) == []


@pytest.mark.asyncio
async def test_gemini_input_transcription_suppressed_while_injecting():
    s = _make_voice_session("google")
    s._injection_until = 9e18
    s._injection_active = True
    await s.process_voice_event({
        "serverContent": {"inputTranscription": {"text": "ghost"}},
    })
    # No buffer write either — the input-suppression is upstream of
    # the accumulator. The attribute may not be set at all (preferred),
    # or may be set to None / empty (acceptable).
    assert not getattr(s, "_pending_user_transcript", None)


# ---------- 5. Tool-call paths ---------------------------------------------


@pytest.mark.asyncio
async def test_openai_function_call_done_persists_tool_use_and_result(monkeypatch):
    s = _make_voice_session("openai")
    s._voice_provider.pending_calls = {"call-1": "search"}
    s._voice_provider._pending_call_args = {"call-1": '{"q": "x"}'}

    # Mock the registry.execute used in process_voice_event.
    from orchestrator.tools import registry as registry_module
    mock_execute = AsyncMock(return_value="result-string")
    monkeypatch.setattr(registry_module, "execute", mock_execute)

    cmds = await s.process_voice_event({
        "type": "response.function_call_arguments.done",
        "call_id": "call-1",
    })
    # tool_use + tool_result persisted.
    writes = _writes(s)
    assert writes == [
        {
            "type": "tool_use",
            "tool_call_id": "call-1",
            "tool_name": "search",
            "tool_input": {"q": "x"},
            "source": "voice",
        },
        {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "output": "result-string",
            "is_error": False,
            "source": "voice",
        },
    ]
    # Commands returned from provider.format_tool_result.
    assert len(cmds) == 2


@pytest.mark.asyncio
async def test_gemini_tool_call_persists_per_call(monkeypatch):
    s = _make_voice_session("google")

    from orchestrator.tools import registry as registry_module
    mock_execute = AsyncMock(return_value="ok")
    monkeypatch.setattr(registry_module, "execute", mock_execute)

    await s.process_voice_event({
        "toolCall": {
            "functionCalls": [
                {"id": "a", "name": "tool_a", "args": {"x": 1}},
                {"id": "b", "name": "tool_b", "args": {"y": 2}},
            ],
        },
    })
    writes = _writes(s)
    # Two pairs of (tool_use, tool_result).
    assert [w["type"] for w in writes] == [
        "tool_use", "tool_result", "tool_use", "tool_result",
    ]
    assert writes[0]["tool_call_id"] == "a"
    assert writes[0]["tool_input"] == {"x": 1}
    assert writes[2]["tool_call_id"] == "b"


# ---------- 6. Non-voice / no-provider short-circuit ----------------------


@pytest.mark.asyncio
async def test_process_voice_event_no_provider_returns_empty():
    """If end_voice fires mid-await, provider becomes None — must
    return [] silently, never raise.
    """
    s = _make_voice_session("openai")
    s._voice_provider = None
    cmds = await s.process_voice_event({"type": "anything"})
    assert cmds == []
    assert _writes(s) == []
