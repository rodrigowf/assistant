"""Tests for manager/qwen_adapter.py — Qwen JSONL parsing and normalization.

Qwen's native format differs from Claude in three notable ways:

1. Messages carry ``message.parts`` (not ``message.content``).
2. Assistant messages use ``role: "model"``.
3. Tool calls are ``{"functionCall": {id, name, args}}`` parts.

The adapter's job is to normalize these into the same shape the rest of
the wrapper (SessionStore, MessagePreview, UI) already understands.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from manager.qwen.adapter import QwenAdapter
from manager.types import ContentBlock, SessionInfo


@pytest.fixture
def adapter() -> QwenAdapter:
    return QwenAdapter()


# ---------------------------------------------------------------------------
# Fixture helpers — mirror Qwen's real-world JSONL shape
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")


def _qwen_user(text: str, ts: str = "2026-05-15T01:00:00.000Z", sid: str = "s1") -> dict:
    return {
        "uuid": "u1",
        "parentUuid": None,
        "sessionId": sid,
        "timestamp": ts,
        "type": "user",
        "message": {"role": "user", "parts": [{"text": text}]},
    }


def _qwen_assistant(
    parts: list[dict],
    ts: str = "2026-05-15T01:00:01.000Z",
    sid: str = "s1",
    usage: dict | None = None,
) -> dict:
    """Build a Qwen assistant entry. Qwen uses role:'model' natively."""
    entry: dict = {
        "uuid": "a1",
        "parentUuid": "u1",
        "sessionId": sid,
        "timestamp": ts,
        "type": "assistant",
        "model": "qwen3.6-plus",
        "message": {"role": "model", "parts": parts},
    }
    if usage is not None:
        entry["usageMetadata"] = usage
    return entry


def _qwen_system_telemetry(ts: str = "2026-05-15T01:00:00.500Z") -> dict:
    """Qwen emits these noisy system events; the adapter must skip them."""
    return {
        "uuid": "sys-1",
        "parentUuid": None,
        "sessionId": "s1",
        "timestamp": ts,
        "type": "system",
        "subtype": "ui_telemetry",
        "systemPayload": {"uiEvent": {"event.name": "qwen-code.api_response"}},
    }


def _qwen_function_call(call_id: str, name: str, args: dict) -> dict:
    """Build a Qwen function-call part inside an assistant message."""
    return {"functionCall": {"id": call_id, "name": name, "args": args}}


# ---------------------------------------------------------------------------
# provider_name
# ---------------------------------------------------------------------------

class TestProviderName:
    def test_provider_name(self, adapter: QwenAdapter):
        assert adapter.provider_name == "qwen"


# ---------------------------------------------------------------------------
# detect_provider
# ---------------------------------------------------------------------------

class TestDetectProvider:
    def test_detects_parts_array(self, tmp_path: Path, adapter: QwenAdapter):
        path = tmp_path / "qwen.jsonl"
        _write_jsonl(path, [_qwen_user("hi")])
        assert adapter.detect_provider(path) is True

    def test_detects_role_model(self, tmp_path: Path, adapter: QwenAdapter):
        """``role: 'model'`` is a Qwen-specific signal even without ``parts``."""
        path = tmp_path / "qwen-model-role.jsonl"
        _write_jsonl(path, [
            {
                "type": "assistant",
                "message": {"role": "model", "parts": []},
                "timestamp": "2026-05-15T01:00:00Z",
            },
        ])
        assert adapter.detect_provider(path) is True

    def test_detects_system_telemetry(self, tmp_path: Path, adapter: QwenAdapter):
        """Telemetry system events with subtype are unique to Qwen."""
        path = tmp_path / "telemetry-only.jsonl"
        _write_jsonl(path, [_qwen_system_telemetry()])
        assert adapter.detect_provider(path) is True

    def test_rejects_claude_format(self, tmp_path: Path, adapter: QwenAdapter):
        path = tmp_path / "claude.jsonl"
        _write_jsonl(path, [
            {
                "type": "user",
                "message": {"role": "user", "content": "hi"},
                "timestamp": "2026-05-15T01:00:00Z",
            },
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "yo"}]},
                "timestamp": "2026-05-15T01:00:01Z",
            },
        ])
        assert adapter.detect_provider(path) is False

    def test_tolerates_missing_file(self, tmp_path: Path, adapter: QwenAdapter):
        assert adapter.detect_provider(tmp_path / "nope.jsonl") is False

    def test_skips_malformed_lines(self, tmp_path: Path, adapter: QwenAdapter):
        path = tmp_path / "mixed.jsonl"
        path.write_text(
            "not valid\n"
            + json.dumps(_qwen_user("hi")) + "\n"
        )
        assert adapter.detect_provider(path) is True


# ---------------------------------------------------------------------------
# read_messages — normalization to Claude-shaped events
# ---------------------------------------------------------------------------

class TestReadMessages:
    def test_normalizes_model_role_to_assistant(
        self, tmp_path: Path, adapter: QwenAdapter,
    ):
        """Qwen's ``role:'model'`` should be rewritten to ``role:'assistant'``
        in the normalized output."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [
            _qwen_user("hi"),
            _qwen_assistant([{"text": "hello"}]),
        ])

        msgs = adapter.read_messages(path)
        assert len(msgs) == 2
        assert msgs[1]["message"]["role"] == "assistant"

    def test_parts_become_content_blocks(self, tmp_path: Path, adapter: QwenAdapter):
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [
            _qwen_assistant([
                {"text": "thinking out loud", "thought": True},
                {"text": "the actual reply"},
                _qwen_function_call("call_abc", "Bash", {"command": "ls"}),
            ]),
        ])

        msgs = adapter.read_messages(path)
        content = msgs[0]["message"]["content"]
        assert content == [
            {"type": "thinking", "text": "thinking out loud"},
            {"type": "text", "text": "the actual reply"},
            {"type": "tool_use", "id": "call_abc", "name": "Bash", "input": {"command": "ls"}},
        ]

    def test_skips_telemetry_system_events(self, tmp_path: Path, adapter: QwenAdapter):
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [
            _qwen_user("hi"),
            _qwen_system_telemetry(),
            _qwen_assistant([{"text": "yo"}]),
        ])

        msgs = adapter.read_messages(path)
        assert [m["type"] for m in msgs] == ["user", "assistant"]

    def test_preserves_usage_metadata(self, tmp_path: Path, adapter: QwenAdapter):
        """``usageMetadata`` and ``model`` should survive into the normalized
        message — downstream consumers (telemetry, cost reporting) rely on it."""
        usage = {"promptTokenCount": 100, "candidatesTokenCount": 50}
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_qwen_assistant([{"text": "hi"}], usage=usage)])

        msgs = adapter.read_messages(path)
        assert msgs[0]["usageMetadata"] == usage
        assert msgs[0]["model"] == "qwen3.6-plus"

    def test_skips_unparseable_lines(self, tmp_path: Path, adapter: QwenAdapter):
        path = tmp_path / "session.jsonl"
        path.write_text(
            "garbage\n"
            + json.dumps(_qwen_user("hi")) + "\n"
        )
        msgs = adapter.read_messages(path)
        assert len(msgs) == 1

    def test_missing_file_returns_empty(self, tmp_path: Path, adapter: QwenAdapter):
        assert adapter.read_messages(tmp_path / "gone.jsonl") == []

    def test_empty_parts_yields_empty_content(self, tmp_path: Path, adapter: QwenAdapter):
        """Defensive: empty parts list should produce empty content, not crash."""
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_qwen_assistant([])])
        msgs = adapter.read_messages(path)
        assert msgs[0]["message"]["content"] == []


# ---------------------------------------------------------------------------
# parse_session_info — must ignore system events when counting messages
# ---------------------------------------------------------------------------

class TestParseSessionInfo:
    def test_counts_only_user_and_assistant(self, tmp_path: Path, adapter: QwenAdapter):
        """Qwen sprays system telemetry between every turn — those should NOT
        inflate the message count."""
        path = tmp_path / "abc.jsonl"
        _write_jsonl(path, [
            _qwen_user("first", ts="2026-05-15T01:00:00.000Z"),
            _qwen_system_telemetry(ts="2026-05-15T01:00:00.500Z"),
            _qwen_assistant(
                [{"text": "first reply"}],
                ts="2026-05-15T01:00:05.000Z",
            ),
            _qwen_system_telemetry(ts="2026-05-15T01:00:05.500Z"),
            _qwen_user("second", ts="2026-05-15T01:01:00.000Z"),
        ])

        info = adapter.parse_session_info(path, "abc")
        assert info is not None
        assert isinstance(info, SessionInfo)
        assert info.message_count == 3  # 2 user + 1 assistant, NOT the telemetry

    def test_title_from_first_user_message_skips_thoughts(
        self, tmp_path: Path, adapter: QwenAdapter,
    ):
        """The title should come from the first user prompt — thinking blocks
        (``thought: true``) should not contaminate it."""
        path = tmp_path / "abc.jsonl"
        _write_jsonl(path, [
            {
                "uuid": "u1", "parentUuid": None, "sessionId": "abc",
                "timestamp": "2026-05-15T01:00:00.000Z",
                "type": "user",
                "message": {
                    "role": "user",
                    "parts": [
                        {"text": "internal reflection", "thought": True},
                        {"text": "real prompt"},
                    ],
                },
            },
        ])
        info = adapter.parse_session_info(path, "abc")
        assert info is not None
        # _extract_text_from_parts skips thought:true blocks, joins the rest
        assert "real prompt" in info.title
        assert "internal reflection" not in info.title

    def test_title_override_from_titles_dict(
        self, tmp_path: Path, adapter: QwenAdapter,
    ):
        path = tmp_path / "named.jsonl"
        _write_jsonl(path, [_qwen_user("original")])
        info = adapter.parse_session_info(
            path, "named", titles={"named": "Custom Title"},
        )
        assert info is not None
        assert info.title == "Custom Title"

    def test_is_orchestrator_always_false(self, tmp_path: Path, adapter: QwenAdapter):
        """Qwen has no orchestrator concept — the field should be False."""
        path = tmp_path / "abc.jsonl"
        _write_jsonl(path, [_qwen_user("hi")])
        info = adapter.parse_session_info(path, "abc")
        assert info is not None
        assert info.is_orchestrator is False

    def test_returns_none_when_no_timestamps(
        self, tmp_path: Path, adapter: QwenAdapter,
    ):
        path = tmp_path / "abc.jsonl"
        # Only a malformed line; no valid timestamped events.
        path.write_text("garbage\n")
        assert adapter.parse_session_info(path, "abc") is None

    def test_returns_none_for_missing_file(
        self, tmp_path: Path, adapter: QwenAdapter,
    ):
        assert adapter.parse_session_info(tmp_path / "gone.jsonl", "x") is None


# ---------------------------------------------------------------------------
# to_previews — verify normalized blocks render correctly
# ---------------------------------------------------------------------------

class TestToPreviews:
    def test_function_call_becomes_tool_use_preview(
        self, tmp_path: Path, adapter: QwenAdapter,
    ):
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [
            _qwen_user("run ls"),
            _qwen_assistant([
                {"text": "running it"},
                _qwen_function_call("call_1", "Bash", {"command": "ls"}),
            ]),
        ])
        msgs = adapter.read_messages(path)
        previews = adapter.to_previews(msgs)

        assistant = previews[1]
        block_types = [b.type for b in assistant.blocks]
        assert block_types == ["text", "tool_use"]
        tool_block = assistant.blocks[1]
        assert isinstance(tool_block, ContentBlock)
        assert tool_block.tool_name == "Bash"
        assert tool_block.tool_input == {"command": "ls"}
        assert tool_block.tool_use_id == "call_1"

    def test_thinking_block_rendered_as_text(self, adapter: QwenAdapter):
        """The shared ``extract_blocks`` collapses thinking → text so the UI
        can show it as a regular block (Qwen's adapter already maps the
        ``thought: True`` part to a ``thinking`` typed block in content)."""
        # Build a normalized message directly
        msg = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "text": "let me think"},
                    {"type": "text", "text": "the answer"},
                ],
            },
            "timestamp": "2026-05-15T01:00:00.000Z",
        }
        previews = adapter.to_previews([msg])
        # Both blocks rendered; the user-facing text only includes "the answer"
        texts = [b.text for b in previews[0].blocks if b.type == "text"]
        assert texts == ["let me think", "the answer"]
        assert previews[0].text == "the answer"  # extract_text skips thinking
