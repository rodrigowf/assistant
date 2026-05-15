"""Tests for manager/claude_adapter.py — Claude JSONL parsing and detection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from manager.claude_adapter import ClaudeAdapter
from manager.types import SessionInfo


@pytest.fixture
def adapter() -> ClaudeAdapter:
    return ClaudeAdapter()


# ---------------------------------------------------------------------------
# Realistic Claude JSONL fixture helpers
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, lines: list[dict]) -> None:
    """Write a list of dicts as a JSONL file."""
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")


def _claude_user(text: str, ts: str = "2026-05-15T01:00:00.000Z") -> dict:
    """Build a Claude user JSONL entry — string content (typical for user)."""
    return {
        "type": "user",
        "message": {"role": "user", "content": text},
        "timestamp": ts,
    }


def _claude_assistant(blocks: list[dict], ts: str = "2026-05-15T01:00:01.000Z") -> dict:
    """Build a Claude assistant JSONL entry — list-of-blocks content."""
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": blocks},
        "timestamp": ts,
    }


def _text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def _tool_use_block(tool_id: str, name: str, inp: dict) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": inp}


def _tool_result_block(tool_id: str, content: str | list, is_error: bool = False) -> dict:
    return {
        "type": "tool_result",
        "tool_use_id": tool_id,
        "content": content,
        "is_error": is_error,
    }


# ---------------------------------------------------------------------------
# provider_name
# ---------------------------------------------------------------------------

class TestProviderName:
    def test_provider_name(self, adapter: ClaudeAdapter):
        assert adapter.provider_name == "claude"


# ---------------------------------------------------------------------------
# detect_provider
# ---------------------------------------------------------------------------

class TestDetectProvider:
    def test_detects_classic_claude_file(self, tmp_path: Path, adapter: ClaudeAdapter):
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [
            _claude_user("hi"),
            _claude_assistant([_text_block("hi back")]),
        ])
        assert adapter.detect_provider(path) is True

    def test_detects_via_internal_event_types(self, tmp_path: Path, adapter: ClaudeAdapter):
        """Claude-only event types (ai-title, queue-operation, etc.) are
        sufficient evidence even without user/assistant messages."""
        path = tmp_path / "weird.jsonl"
        _write_jsonl(path, [
            {"type": "ai-title", "title": "Some session"},
        ])
        assert adapter.detect_provider(path) is True

    def test_rejects_qwen_format(self, tmp_path: Path, adapter: ClaudeAdapter):
        """Qwen uses message.parts and role:'model' — neither should fool us."""
        path = tmp_path / "qwen.jsonl"
        _write_jsonl(path, [
            {
                "type": "user",
                "message": {"role": "user", "parts": [{"text": "hi"}]},
                "timestamp": "2026-05-15T01:00:00Z",
            },
            {
                "type": "assistant",
                "message": {"role": "model", "parts": [{"text": "yo"}]},
                "timestamp": "2026-05-15T01:00:01Z",
            },
        ])
        assert adapter.detect_provider(path) is False

    def test_rejects_qwen_system_events_with_subtype(
        self, tmp_path: Path, adapter: ClaudeAdapter,
    ):
        path = tmp_path / "qwen-telemetry.jsonl"
        _write_jsonl(path, [
            {"type": "system", "subtype": "ui_telemetry", "systemPayload": {}},
        ])
        assert adapter.detect_provider(path) is False

    def test_tolerates_missing_file(self, tmp_path: Path, adapter: ClaudeAdapter):
        """Detection on a non-existent file degrades to False — no exception."""
        assert adapter.detect_provider(tmp_path / "does-not-exist.jsonl") is False

    def test_skips_malformed_lines(self, tmp_path: Path, adapter: ClaudeAdapter):
        """A garbage line in the middle of an otherwise-valid file shouldn't
        derail detection."""
        path = tmp_path / "mixed.jsonl"
        path.write_text(
            "not json at all\n"
            + json.dumps(_claude_user("hello")) + "\n"
        )
        assert adapter.detect_provider(path) is True


# ---------------------------------------------------------------------------
# read_messages
# ---------------------------------------------------------------------------

class TestReadMessages:
    def test_reads_user_and_assistant(self, tmp_path: Path, adapter: ClaudeAdapter):
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [
            _claude_user("first prompt"),
            _claude_assistant([_text_block("response")]),
        ])
        msgs = adapter.read_messages(path)
        assert len(msgs) == 2
        assert msgs[0]["type"] == "user"
        assert msgs[1]["type"] == "assistant"

    def test_filters_internal_event_types(self, tmp_path: Path, adapter: ClaudeAdapter):
        """ai-title, attachment, file-history-snapshot etc. shouldn't appear
        in the message list — they're metadata, not conversation."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [
            _claude_user("hello"),
            {"type": "ai-title", "title": "Greeting"},
            {"type": "attachment", "path": "/tmp/foo.png"},
            {"type": "file-history-snapshot"},
            _claude_assistant([_text_block("hi")]),
        ])
        msgs = adapter.read_messages(path)
        types = [m["type"] for m in msgs]
        assert types == ["user", "assistant"]

    def test_includes_system_messages(self, tmp_path: Path, adapter: ClaudeAdapter):
        """Claude system messages carry init metadata; the adapter keeps them
        so downstream code can extract session ids etc."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [
            {"type": "system", "subtype": "init", "session_id": "abc"},
            _claude_user("hi"),
        ])
        msgs = adapter.read_messages(path)
        assert any(m["type"] == "system" for m in msgs)

    def test_skips_unparseable_lines(self, tmp_path: Path, adapter: ClaudeAdapter):
        path = tmp_path / "session.jsonl"
        path.write_text(
            "{not valid json\n"
            + json.dumps(_claude_user("good line")) + "\n"
            + ""  # empty
            + "\n"
        )
        msgs = adapter.read_messages(path)
        assert len(msgs) == 1

    def test_missing_file_returns_empty(self, tmp_path: Path, adapter: ClaudeAdapter):
        msgs = adapter.read_messages(tmp_path / "nope.jsonl")
        assert msgs == []


# ---------------------------------------------------------------------------
# parse_session_info
# ---------------------------------------------------------------------------

class TestParseSessionInfo:
    def test_basic_metadata(self, tmp_path: Path, adapter: ClaudeAdapter):
        path = tmp_path / "abc.jsonl"
        _write_jsonl(path, [
            _claude_user("first prompt", ts="2026-05-15T01:00:00.000Z"),
            _claude_assistant(
                [_text_block("reply")], ts="2026-05-15T01:00:05.000Z",
            ),
            _claude_user("second prompt", ts="2026-05-15T01:01:00.000Z"),
        ])

        info = adapter.parse_session_info(path, "abc")
        assert info is not None
        assert isinstance(info, SessionInfo)
        assert info.session_id == "abc"
        assert info.message_count == 3
        assert info.title == "first prompt"
        assert info.started_at.isoformat().startswith("2026-05-15T01:00:00")
        assert info.last_activity.isoformat().startswith("2026-05-15T01:01:00")

    def test_title_override_from_titles_dict(
        self, tmp_path: Path, adapter: ClaudeAdapter,
    ):
        path = tmp_path / "renamed.jsonl"
        _write_jsonl(path, [_claude_user("original text")])

        info = adapter.parse_session_info(
            path, "renamed", titles={"renamed": "My Custom Title"},
        )
        assert info is not None
        assert info.title == "My Custom Title"

    def test_orchestrator_flag(self, tmp_path: Path, adapter: ClaudeAdapter):
        path = tmp_path / "orch.jsonl"
        _write_jsonl(path, [
            {
                "type": "orchestrator_meta",
                "orchestrator": True,
                "timestamp": "2026-05-15T01:00:00.000Z",
            },
            _claude_user("first"),
        ])
        info = adapter.parse_session_info(path, "orch")
        assert info is not None
        assert info.is_orchestrator is True

    def test_returns_none_for_file_without_timestamps(
        self, tmp_path: Path, adapter: ClaudeAdapter,
    ):
        """Without any timestamp we can't say when the session happened, so
        the adapter declines rather than fabricating one."""
        path = tmp_path / "empty.jsonl"
        path.write_text(json.dumps({"type": "ai-title", "title": "Headless"}) + "\n")
        assert adapter.parse_session_info(path, "empty") is None

    def test_returns_none_for_missing_file(
        self, tmp_path: Path, adapter: ClaudeAdapter,
    ):
        assert adapter.parse_session_info(tmp_path / "gone.jsonl", "x") is None

    def test_falls_back_to_empty_session_title(
        self, tmp_path: Path, adapter: ClaudeAdapter,
    ):
        """A file with a timestamped system event but no user message gets the
        '(empty session)' placeholder title."""
        path = tmp_path / "system-only.jsonl"
        _write_jsonl(path, [
            {
                "type": "system", "subtype": "init",
                "timestamp": "2026-05-15T01:00:00.000Z",
                "data": {"session_id": "x"},
            },
        ])
        info = adapter.parse_session_info(path, "x")
        assert info is not None
        assert info.title == "(empty session)"


# ---------------------------------------------------------------------------
# to_previews — verify Claude content blocks round-trip correctly
# ---------------------------------------------------------------------------

class TestToPreviews:
    def test_text_only_assistant(self, adapter: ClaudeAdapter):
        msgs = [
            _claude_user("hi"),
            _claude_assistant([_text_block("hello")]),
        ]
        previews = adapter.to_previews(msgs)
        assert [p.role for p in previews] == ["user", "assistant"]
        assert previews[0].text == "hi"
        assert previews[1].text == "hello"
        assert len(previews[1].blocks) == 1
        assert previews[1].blocks[0].type == "text"

    def test_tool_use_and_result_blocks(self, adapter: ClaudeAdapter):
        msgs = [
            _claude_user("run the tool"),
            _claude_assistant([
                _text_block("calling tool"),
                _tool_use_block("toolu_1", "Bash", {"command": "ls"}),
            ]),
            _claude_assistant([
                _tool_result_block("toolu_1", "file1\nfile2"),
            ]),
        ]
        previews = adapter.to_previews(msgs)
        # Two assistant turns + the user
        assert len(previews) == 3
        # First assistant message had text + tool_use blocks
        types_first = [b.type for b in previews[1].blocks]
        assert types_first == ["text", "tool_use"]
        tool_use = previews[1].blocks[1]
        assert tool_use.tool_name == "Bash"
        assert tool_use.tool_input == {"command": "ls"}
        # Second has the tool_result
        tool_result = previews[2].blocks[0]
        assert tool_result.type == "tool_result"
        assert tool_result.tool_use_id == "toolu_1"
        assert tool_result.output == "file1\nfile2"
        assert tool_result.is_error is False

    def test_tool_result_with_list_content(self, adapter: ClaudeAdapter):
        """Claude sometimes packs tool_result content as a list of text items."""
        msgs = [
            _claude_assistant([
                _tool_result_block(
                    "toolu_2",
                    [{"type": "text", "text": "alpha"}, {"type": "text", "text": "beta"}],
                ),
            ]),
        ]
        previews = adapter.to_previews(msgs)
        assert previews[0].blocks[0].output == "alpha\nbeta"

    def test_is_error_flag(self, adapter: ClaudeAdapter):
        msgs = [
            _claude_assistant([
                _tool_result_block("toolu_x", "boom", is_error=True),
            ]),
        ]
        previews = adapter.to_previews(msgs)
        assert previews[0].blocks[0].is_error is True
