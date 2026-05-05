"""Tests for the injection-window suppression that prevents the
``listen_recording`` feedback loop.

Background: when ``listen_recording`` injects past audio into the live
voice WS via ``input_audio_buffer.append``, the realtime provider's VAD
fires ``input_audio_buffer.speech_started`` and its ASR fires
``conversation.item.input_audio_transcription.completed`` for each
fragment of the replay.  Persisting those events as user turns (and
mirroring them to the frontend, which would respond with
``response.cancel``) creates a runaway loop.

These tests verify that OrchestratorSession honours an injection window:
while it's active, transcription / interrupt events are dropped from
JSONL and the recorder is shielded from the replayed bytes.  The
route-layer mirror suppression is exercised via ``is_injecting`` from
``api/routes/orchestrator.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.config import OrchestratorConfig
from orchestrator.session import OrchestratorSession


def _make_session(tmp_path: Path) -> OrchestratorSession:
    """Build a minimally-wired voice session for direct method testing."""
    config = OrchestratorConfig(
        project_dir=str(tmp_path),
        memory_path=str(tmp_path / "mem.md"),
    )
    session = OrchestratorSession(
        config=config,
        context={},
        voice=True,
    )
    # Wire a writer pointed at a temp JSONL so process_voice_event has
    # something to append to.  We don't call session.start() because that
    # would spin up the provider and agent.
    from orchestrator.persistence import HistoryWriter
    session._jsonl_path = tmp_path / "session.jsonl"
    session._writer = HistoryWriter(session._jsonl_path)
    # Stand in for the voice provider so process_voice_event's voice gate
    # passes.  None of the methods on it are called for the events we
    # exercise.
    session._voice_provider = MagicMock()
    return session


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_is_injecting_default_false(tmp_path):
    """A fresh session is not injecting."""
    session = _make_session(tmp_path)
    assert session.is_injecting is False


@pytest.mark.asyncio
async def test_extend_injection_window_marks_active(tmp_path):
    """extend_injection_window flips the flag immediately."""
    session = _make_session(tmp_path)
    session.extend_injection_window(5.0)
    assert session.is_injecting is True


@pytest.mark.asyncio
async def test_window_expires_and_clears_flag(tmp_path):
    """When the deadline passes the watchdog clears the flag."""
    session = _make_session(tmp_path)
    session.extend_injection_window(0.15)  # 150ms
    assert session.is_injecting is True
    # Wait past the deadline + watchdog scheduling slack.
    await asyncio.sleep(0.4)
    assert session.is_injecting is False


@pytest.mark.asyncio
async def test_window_extension_pushes_deadline_out(tmp_path):
    """Re-extending while active keeps the window open instead of closing."""
    session = _make_session(tmp_path)
    session.extend_injection_window(0.15)
    # Just before expiry, push it out further.
    await asyncio.sleep(0.10)
    assert session.is_injecting is True
    session.extend_injection_window(0.30)
    # Wait past the *original* deadline — should still be active.
    await asyncio.sleep(0.15)
    assert session.is_injecting is True
    # Now wait out the new deadline.
    await asyncio.sleep(0.30)
    assert session.is_injecting is False


@pytest.mark.asyncio
async def test_phantom_transcription_is_dropped_while_injecting(tmp_path):
    """While injecting, transcription.completed events do NOT hit JSONL."""
    session = _make_session(tmp_path)
    session.extend_injection_window(2.0)

    await session.process_voice_event(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "fragment from replay",
        },
        inject=False,
    )

    entries = _read_jsonl(session._jsonl_path)
    assert all(e.get("source") != "voice_transcription" for e in entries), (
        "Transcription events fired by injected audio must not be persisted"
    )


@pytest.mark.asyncio
async def test_phantom_speech_started_is_not_persisted_while_injecting(tmp_path):
    """While injecting, speech_started must NOT write voice_interrupted."""
    session = _make_session(tmp_path)
    session.extend_injection_window(2.0)

    await session.process_voice_event(
        {"type": "input_audio_buffer.speech_started"},
        inject=False,
    )

    entries = _read_jsonl(session._jsonl_path)
    assert all(e.get("type") != "voice_interrupted" for e in entries), (
        "speech_started events fired by injected audio must not be persisted"
    )


@pytest.mark.asyncio
async def test_real_transcription_is_persisted_when_not_injecting(tmp_path):
    """Sanity: outside the window, normal user transcripts still land in JSONL."""
    session = _make_session(tmp_path)
    assert session.is_injecting is False

    await session.process_voice_event(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "real user words",
        },
        inject=False,
    )

    entries = _read_jsonl(session._jsonl_path)
    voice_entries = [e for e in entries if e.get("source") == "voice_transcription"]
    assert len(voice_entries) == 1
    content = voice_entries[0]["message"]["content"]
    assert "real user words" in content


@pytest.mark.asyncio
async def test_real_speech_started_persisted_when_not_injecting(tmp_path):
    """Sanity: outside the window, speech_started writes voice_interrupted."""
    session = _make_session(tmp_path)
    assert session.is_injecting is False

    await session.process_voice_event(
        {"type": "input_audio_buffer.speech_started"},
        inject=False,
    )

    entries = _read_jsonl(session._jsonl_path)
    interrupts = [e for e in entries if e.get("type") == "voice_interrupted"]
    assert len(interrupts) == 1


@pytest.mark.asyncio
async def test_send_voice_audio_in_skips_recorder_during_injection(tmp_path):
    """While injecting, send_voice_audio_in must not feed the recorder.

    Otherwise the replayed bytes get re-recorded into the live session's
    audio.pcm, polluting it with material that is already saved elsewhere.
    """
    session = _make_session(tmp_path)
    fake_relay = MagicMock()
    fake_relay.send_audio = AsyncMock()
    session._voice_relay = fake_relay
    fake_recorder = MagicMock()
    fake_recorder.is_recording = True
    session._audio_recorder = fake_recorder

    session.extend_injection_window(2.0)
    await session.send_voice_audio_in("AAAA")

    fake_relay.send_audio.assert_awaited_once_with("AAAA")
    fake_recorder.write_user_audio.assert_not_called()


@pytest.mark.asyncio
async def test_send_voice_audio_in_uses_recorder_outside_injection(tmp_path):
    """Sanity: the recorder still receives mic audio when not injecting."""
    session = _make_session(tmp_path)
    fake_relay = MagicMock()
    fake_relay.send_audio = AsyncMock()
    session._voice_relay = fake_relay
    fake_recorder = MagicMock()
    fake_recorder.is_recording = True
    session._audio_recorder = fake_recorder

    await session.send_voice_audio_in("AAAA")

    fake_relay.send_audio.assert_awaited_once_with("AAAA")
    fake_recorder.write_user_audio.assert_called_once_with("AAAA")
