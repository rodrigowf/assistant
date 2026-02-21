"""Tests for orchestrator persistence (JSONL history loading)."""

import json
import tempfile
from pathlib import Path

import pytest

from orchestrator.persistence import HistoryLoader, HistoryWriter


def test_history_loader_empty_file():
    """Test loading from an empty JSONL file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        jsonl_path = Path(f.name)

    try:
        loader = HistoryLoader(jsonl_path)
        history = loader.load()
        assert history == []
    finally:
        jsonl_path.unlink()


def test_history_loader_simple_conversation():
    """Test loading a simple text conversation."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps({
            "type": "orchestrator_meta",
            "session_id": "test-123",
        }) + "\n")
        f.write(json.dumps({
            "type": "user",
            "message": {"role": "user", "content": "Hello"},
        }) + "\n")
        f.write(json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": "Hi there!"},
        }) + "\n")
        jsonl_path = Path(f.name)

    try:
        loader = HistoryLoader(jsonl_path)
        history = loader.load()

        assert len(history) == 2
        assert history[0] == {"role": "user", "content": "Hello"}
        assert history[1] == {
            "role": "assistant",
            "content": [{"type": "text", "text": "Hi there!"}],
        }
    finally:
        jsonl_path.unlink()


def test_history_loader_with_tool_calls():
    """Test loading conversation with tool calls and results.

    Note: When tool_use entries appear after an assistant message in the JSONL,
    they are stored as separate assistant messages. This matches how the
    orchestrator actually writes the JSONL (text response first, then tool calls).
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps({
            "type": "user",
            "message": {"role": "user", "content": "Search for something"},
        }) + "\n")
        f.write(json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": "Let me search for that."},
        }) + "\n")
        f.write(json.dumps({
            "type": "tool_use",
            "tool_call_id": "call_123",
            "tool_name": "search_memory",
            "tool_input": {"query": "something"},
        }) + "\n")
        f.write(json.dumps({
            "type": "tool_result",
            "tool_call_id": "call_123",
            "output": "Found 3 results",
        }) + "\n")
        f.write(json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": "I found 3 results."},
        }) + "\n")
        jsonl_path = Path(f.name)

    try:
        loader = HistoryLoader(jsonl_path)
        history = loader.load()

        assert len(history) == 5

        # User message
        assert history[0] == {"role": "user", "content": "Search for something"}

        # Assistant text response (without tool call)
        assert history[1]["role"] == "assistant"
        assert history[1]["content"] == [{"type": "text", "text": "Let me search for that."}]

        # Assistant with tool call
        assert history[2]["role"] == "assistant"
        content = history[2]["content"]
        assert len(content) == 1
        assert content[0] == {
            "type": "tool_use",
            "id": "call_123",
            "name": "search_memory",
            "input": {"query": "something"},
        }

        # Tool result as user message
        assert history[3] == {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "call_123",
                "content": "Found 3 results",
            }],
        }

        # Final assistant response
        assert history[4] == {
            "role": "assistant",
            "content": [{"type": "text", "text": "I found 3 results."}],
        }
    finally:
        jsonl_path.unlink()


def test_history_loader_voice_mode():
    """Test loading voice mode transcriptions."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps({
            "type": "orchestrator_meta",
            "voice": True,
        }) + "\n")
        f.write(json.dumps({
            "type": "user",
            "message": {"role": "user", "content": "[voice] Hello"},
            "source": "voice_transcription",
        }) + "\n")
        f.write(json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": "Hi!"},
            "source": "voice_response",
        }) + "\n")
        f.write(json.dumps({
            "type": "voice_interrupted",
        }) + "\n")
        jsonl_path = Path(f.name)

    try:
        loader = HistoryLoader(jsonl_path)
        history = loader.load()

        assert len(history) == 2
        assert history[0] == {"role": "user", "content": "[voice] Hello"}
        assert history[1] == {
            "role": "assistant",
            "content": [{"type": "text", "text": "Hi!"}],
        }
    finally:
        jsonl_path.unlink()


def test_history_writer():
    """Test writing events to JSONL."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        jsonl_path = Path(f.name)

    try:
        writer = HistoryWriter(jsonl_path)
        writer.append({"type": "user", "message": {"role": "user", "content": "Test"}})
        writer.append({"type": "assistant", "message": {"role": "assistant", "content": "Response"}})

        # Read back and verify
        with open(jsonl_path) as f:
            lines = [json.loads(line) for line in f if line.strip()]

        assert len(lines) == 2
        assert lines[0]["type"] == "user"
        assert lines[1]["type"] == "assistant"
    finally:
        jsonl_path.unlink()


def test_history_loader_invalid_json():
    """Test that invalid JSON lines are skipped gracefully."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "Valid"}}) + "\n")
        f.write("{ invalid json\n")
        f.write(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "Also valid"}}) + "\n")
        jsonl_path = Path(f.name)

    try:
        loader = HistoryLoader(jsonl_path)
        history = loader.load()

        # Should successfully load the 2 valid entries
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[1]["role"] == "assistant"
    finally:
        jsonl_path.unlink()


def test_history_loader_multiple_tool_calls():
    """Test loading conversation with multiple sequential tool calls."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps({
            "type": "user",
            "message": {"role": "user", "content": "Do two searches"},
        }) + "\n")
        f.write(json.dumps({
            "type": "tool_use",
            "tool_call_id": "call_1",
            "tool_name": "search_memory",
            "tool_input": {"query": "first"},
        }) + "\n")
        f.write(json.dumps({
            "type": "tool_use",
            "tool_call_id": "call_2",
            "tool_name": "search_memory",
            "tool_input": {"query": "second"},
        }) + "\n")
        f.write(json.dumps({
            "type": "tool_result",
            "tool_call_id": "call_1",
            "output": "Result 1",
        }) + "\n")
        f.write(json.dumps({
            "type": "tool_result",
            "tool_call_id": "call_2",
            "output": "Result 2",
        }) + "\n")
        jsonl_path = Path(f.name)

    try:
        loader = HistoryLoader(jsonl_path)
        history = loader.load()

        assert len(history) == 3

        # User message
        assert history[0]["role"] == "user"

        # Assistant with two tool calls
        assert history[1]["role"] == "assistant"
        content = history[1]["content"]
        assert len(content) == 2
        assert all(b["type"] == "tool_use" for b in content)

        # Tool results
        assert history[2]["role"] == "user"
        results = history[2]["content"]
        assert len(results) == 2
        assert all(r["type"] == "tool_result" for r in results)
    finally:
        jsonl_path.unlink()
