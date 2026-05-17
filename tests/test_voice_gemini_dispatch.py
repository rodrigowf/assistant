"""Tests for ``OrchestratorSession.process_voice_event`` Gemini Live branch.

Gemini Live ships events with completely different shapes than
OpenAI / Qwen (no top-level ``type`` field; transcription /
turn-complete / interruption signals nested under ``serverContent``;
tool calls under ``toolCall.functionCalls``).  These tests pin the
persistence behaviour for each shape so the chat UI sees the user's
words, the assistant's reply, and any tool-call lifecycle entries.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.config import OrchestratorConfig
from orchestrator.session import OrchestratorSession


def _make_session(tmp_path: Path) -> OrchestratorSession:
    config = OrchestratorConfig(
        project_dir=str(tmp_path),
        memory_path=str(tmp_path / "mem.md"),
    )
    session = OrchestratorSession(config=config, context={}, voice=True)
    from orchestrator.persistence import HistoryWriter
    session._jsonl_path = tmp_path / "session.jsonl"
    session._writer = HistoryWriter(session._jsonl_path)
    # The dispatch branches on provider_name == "google" so the mock
    # MUST report that.
    provider = MagicMock()
    provider.provider_name = "google"
    provider.inject_event = AsyncMock()
    provider.format_tool_result = MagicMock(return_value=[{"toolResponse": {}}])
    session._voice_provider = provider
    return session


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_gemini_input_transcription_persists_voice_user_message(tmp_path):
    """serverContent.inputTranscription.text → [voice] user JSONL entry."""
    session = _make_session(tmp_path)
    await session.process_voice_event({
        "serverContent": {
            "inputTranscription": {"text": "what is two plus two"},
        },
    })
    entries = _read_jsonl(session._jsonl_path)
    voice_msgs = [e for e in entries if e.get("source") == "voice_transcription"]
    assert len(voice_msgs) == 1
    assert voice_msgs[0]["message"]["content"] == "[voice] what is two plus two"
    assert voice_msgs[0]["type"] == "user"


@pytest.mark.asyncio
async def test_gemini_input_transcription_dropped_while_injecting(tmp_path):
    """While listen_recording injects replay audio, phantom transcripts
    are dropped — same behaviour as the OpenAI branch."""
    session = _make_session(tmp_path)
    session.extend_injection_window(2.0)
    await session.process_voice_event({
        "serverContent": {"inputTranscription": {"text": "phantom"}},
    })
    entries = _read_jsonl(session._jsonl_path)
    assert all(e.get("source") != "voice_transcription" for e in entries)


@pytest.mark.asyncio
async def test_gemini_output_transcription_stages_and_persists_on_turn_complete(tmp_path):
    """outputTranscription deltas accumulate; turnComplete flushes them."""
    session = _make_session(tmp_path)
    await session.process_voice_event({
        "serverContent": {"outputTranscription": {"text": "The answer "}},
    })
    await session.process_voice_event({
        "serverContent": {"outputTranscription": {"text": "is four."}},
    })
    # Not persisted yet — still staged.
    assert _read_jsonl(session._jsonl_path) == []
    await session.process_voice_event({"serverContent": {"turnComplete": True}})
    entries = _read_jsonl(session._jsonl_path)
    assistant_msgs = [e for e in entries if e.get("source") == "voice_response"]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0]["message"]["content"] == "The answer is four."


@pytest.mark.asyncio
async def test_gemini_tool_call_executes_and_persists_lifecycle(tmp_path):
    """toolCall.functionCalls[] → registry.execute() + JSONL tool_use/result
    + format_tool_result commands shipped back to relay."""
    session = _make_session(tmp_path)

    # Stub the tool registry — execute() must be awaitable.
    from orchestrator.tools import registry
    original_execute = registry.execute
    try:
        registry.execute = AsyncMock(return_value="2026-05-15T20:00:00Z")

        commands = await session.process_voice_event({
            "toolCall": {
                "functionCalls": [
                    {
                        "id": "call-abc",
                        "name": "get_time",
                        "args": {"tz": "UTC"},
                    }
                ],
            },
        })
    finally:
        registry.execute = original_execute

    # Tool was invoked.
    # registry.execute is a fresh AsyncMock — re-grab via session's namespace.
    # The mock above already records the call internally; we verify side
    # effects via JSONL + commands.

    entries = _read_jsonl(session._jsonl_path)
    tool_uses = [e for e in entries if e.get("type") == "tool_use"]
    tool_results = [e for e in entries if e.get("type") == "tool_result"]
    assert len(tool_uses) == 1
    assert tool_uses[0]["tool_call_id"] == "call-abc"
    assert tool_uses[0]["tool_name"] == "get_time"
    assert tool_uses[0]["tool_input"] == {"tz": "UTC"}
    assert len(tool_results) == 1
    assert tool_results[0]["tool_call_id"] == "call-abc"
    assert tool_results[0]["output"] == "2026-05-15T20:00:00Z"

    # And the provider's format_tool_result was called → command list returned.
    session._voice_provider.format_tool_result.assert_called_once_with(
        "call-abc", "2026-05-15T20:00:00Z"
    )
    assert commands == [{"toolResponse": {}}]


@pytest.mark.asyncio
async def test_gemini_interrupted_persists_voice_interrupted_entry(tmp_path):
    """serverContent.interrupted → voice_interrupted JSONL entry."""
    session = _make_session(tmp_path)
    await session.process_voice_event({"serverContent": {"interrupted": True}})
    entries = _read_jsonl(session._jsonl_path)
    interrupts = [e for e in entries if e.get("type") == "voice_interrupted"]
    assert len(interrupts) == 1


@pytest.mark.asyncio
async def test_gemini_branch_skipped_when_provider_is_not_google(tmp_path):
    """A Qwen/OpenAI provider must NOT take the Gemini branch even if
    the event happens to look Gemini-shaped (defensive)."""
    session = _make_session(tmp_path)
    session._voice_provider.provider_name = "qwen"
    await session.process_voice_event({
        "serverContent": {"inputTranscription": {"text": "should not persist"}},
    })
    entries = _read_jsonl(session._jsonl_path)
    assert entries == []
