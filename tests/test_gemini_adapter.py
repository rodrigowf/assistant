"""Tests for manager/gemini/adapter.py — Gemini CLI JSONL parsing.

Gemini's native format differs from both Claude and Qwen:

1. A header line ``{"sessionId":..., "projectHash":..., "kind":"main"}``
   opens every session JSONL.
2. Assistant role is ``"gemini"`` (not ``"assistant"``).
3. Assistant content is a plain string (not a list of blocks).
4. ``thoughts`` is a top-level array, not inline thinking blocks.
5. ``{"$set": {...}}`` lines are bookkeeping markers and must be skipped.

The adapter's job is to normalize all of these into the same shape the
rest of the wrapper (SessionStore, MessagePreview, UI) understands.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from manager.gemini.adapter import (
    GeminiAdapter,
    _gemini_jsonl_candidates,
    _is_metadata_line,
    _normalize_message,
)
from manager.types import ContentBlock


@pytest.fixture
def adapter() -> GeminiAdapter:
    return GeminiAdapter()


# ---------------------------------------------------------------------------
# Fixture helpers — mirror Gemini's real-world JSONL shape
# ---------------------------------------------------------------------------


def _header_line(
    session_id: str = "11111111-1111-1111-1111-111111111111",
    started: str = "2026-05-15T20:55:00.745Z",
) -> dict:
    return {
        "sessionId": session_id,
        "projectHash": "abc123",
        "startTime": started,
        "lastUpdated": started,
        "kind": "main",
    }


def _user_line(text: str, ts: str = "2026-05-15T20:57:09.556Z") -> dict:
    return {
        "id": "u1",
        "timestamp": ts,
        "type": "user",
        "content": [{"text": text}],
    }


def _gemini_line(
    text: str,
    ts: str = "2026-05-15T20:57:11.910Z",
    thoughts: list[dict] | None = None,
) -> dict:
    return {
        "id": "g1",
        "timestamp": ts,
        "type": "gemini",
        "content": text,
        "thoughts": thoughts or [],
        "tokens": {"input": 10, "output": 5, "cached": 0, "thoughts": 0, "tool": 0, "total": 15},
        "model": "gemini-3-flash-preview",
    }


def _set_line(ts: str = "2026-05-15T20:57:11.910Z") -> dict:
    return {"$set": {"lastUpdated": ts}}


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


# ---------------------------------------------------------------------------
# detect_provider


def test_detect_provider_returns_true_for_header_line(
    adapter: GeminiAdapter, tmp_path: Path,
) -> None:
    p = tmp_path / "session.jsonl"
    _write_jsonl(p, [_header_line(), _user_line("hi")])
    assert adapter.detect_provider(p) is True


def test_detect_provider_returns_true_for_gemini_type(
    adapter: GeminiAdapter, tmp_path: Path,
) -> None:
    """Even without a header line, ``type: 'gemini'`` is a unique signature."""
    p = tmp_path / "session.jsonl"
    _write_jsonl(p, [_gemini_line("hi")])
    assert adapter.detect_provider(p) is True


def test_detect_provider_returns_false_for_claude_jsonl(
    adapter: GeminiAdapter, tmp_path: Path,
) -> None:
    """Claude's native format has neither the header line nor 'gemini' type."""
    p = tmp_path / "session.jsonl"
    _write_jsonl(p, [
        {
            "type": "user",
            "timestamp": "2026-05-15T20:57:09Z",
            "message": {"role": "user", "content": "hi"},
        },
    ])
    assert adapter.detect_provider(p) is False


def test_detect_provider_returns_false_for_qwen_jsonl(
    adapter: GeminiAdapter, tmp_path: Path,
) -> None:
    """Qwen's parts-shape doesn't trigger Gemini detection."""
    p = tmp_path / "session.jsonl"
    _write_jsonl(p, [
        {
            "type": "user",
            "timestamp": "2026-05-15T20:57:09Z",
            "message": {"role": "user", "parts": [{"text": "hi"}]},
        },
    ])
    assert adapter.detect_provider(p) is False


def test_detect_provider_tolerates_unreadable_file(
    adapter: GeminiAdapter, tmp_path: Path,
) -> None:
    p = tmp_path / "missing.jsonl"
    assert adapter.detect_provider(p) is False


# ---------------------------------------------------------------------------
# _is_metadata_line / _normalize_message — internal helpers


def test_is_metadata_line_recognizes_set_lines() -> None:
    assert _is_metadata_line({"$set": {"lastUpdated": "2026-05-15T20:00:00Z"}}) is True
    # A line with both $set AND a type field is a real message, not metadata.
    assert _is_metadata_line({"$set": {"x": 1}, "type": "user", "content": []}) is False
    assert _is_metadata_line({"type": "user", "content": []}) is False


def test_normalize_message_user_joins_content_parts() -> None:
    raw = {
        "type": "user",
        "timestamp": "t1",
        "content": [{"text": "hello"}, {"text": "world"}],
    }
    msg = _normalize_message(raw)
    assert msg is not None
    assert msg["type"] == "user"
    assert msg["message"]["content"] == "hello\nworld"


def test_normalize_message_user_accepts_plain_string_content() -> None:
    """Future-proofing: if the CLI ever emits user.content as a string,
    the adapter should still parse it."""
    raw = {"type": "user", "timestamp": "t1", "content": "hello"}
    msg = _normalize_message(raw)
    assert msg is not None
    assert msg["message"]["content"] == "hello"


def test_normalize_message_assistant_emits_text_block() -> None:
    raw = {
        "type": "gemini",
        "timestamp": "t1",
        "content": "the answer is 42",
        "thoughts": [],
    }
    msg = _normalize_message(raw)
    assert msg is not None
    assert msg["type"] == "assistant"
    assert msg["message"]["role"] == "assistant"
    assert msg["message"]["content"] == [
        {"type": "text", "text": "the answer is 42"},
    ]


def test_normalize_message_assistant_includes_thoughts_as_thinking_blocks() -> None:
    raw = {
        "type": "gemini",
        "timestamp": "t1",
        "content": "answer",
        "thoughts": [
            {"subject": "Step 1", "description": "Think hard", "timestamp": "t0"},
        ],
    }
    msg = _normalize_message(raw)
    assert msg is not None
    blocks = msg["message"]["content"]
    # Thinking blocks come first (the order users expect — think then say).
    assert blocks[0] == {"type": "thinking", "text": "Step 1\nThink hard"}
    assert blocks[1] == {"type": "text", "text": "answer"}


def test_normalize_message_returns_none_for_unrelated_types() -> None:
    """Header line, $set markers, and unknown event types all map to None."""
    assert _normalize_message(_header_line()) is None
    assert _normalize_message(_set_line()) is None
    assert _normalize_message({"type": "unknown"}) is None


# ---------------------------------------------------------------------------
# read_messages


def test_read_messages_skips_header_and_set_lines(
    adapter: GeminiAdapter, tmp_path: Path,
) -> None:
    p = tmp_path / "session.jsonl"
    _write_jsonl(p, [
        _header_line(),
        _user_line("hi"),
        _set_line(),
        _gemini_line("hello back"),
        _set_line(),
    ])
    msgs = adapter.read_messages(p)
    assert len(msgs) == 2
    assert msgs[0]["type"] == "user"
    assert msgs[0]["message"]["content"] == "hi"
    assert msgs[1]["type"] == "assistant"
    assert msgs[1]["message"]["content"][0]["text"] == "hello back"


def test_read_messages_handles_empty_file(
    adapter: GeminiAdapter, tmp_path: Path,
) -> None:
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    assert adapter.read_messages(p) == []


def test_read_messages_skips_malformed_lines(
    adapter: GeminiAdapter, tmp_path: Path,
) -> None:
    """A garbage line shouldn't blow up the whole file read."""
    p = tmp_path / "session.jsonl"
    content = "\n".join([
        json.dumps(_header_line()),
        "{this is not valid JSON",
        json.dumps(_user_line("hi")),
    ])
    p.write_text(content + "\n")
    msgs = adapter.read_messages(p)
    assert len(msgs) == 1
    assert msgs[0]["message"]["content"] == "hi"


# ---------------------------------------------------------------------------
# parse_session_info


def test_parse_session_info_extracts_title_and_counts(
    adapter: GeminiAdapter, tmp_path: Path,
) -> None:
    p = tmp_path / "session.jsonl"
    _write_jsonl(p, [
        _header_line(started="2026-05-15T20:55:00.000Z"),
        _user_line("First question?"),
        _set_line("2026-05-15T20:57:09.556Z"),
        _gemini_line("First answer.", ts="2026-05-15T20:57:11.910Z"),
        _set_line("2026-05-15T20:57:11.910Z"),
        _user_line("Second?", ts="2026-05-15T21:00:00.000Z"),
        _gemini_line("Second answer.", ts="2026-05-15T21:00:05.000Z"),
    ])
    info = adapter.parse_session_info(p, "session-test")
    assert info is not None
    assert info.title == "First question?"
    assert info.message_count == 4
    assert info.started_at is not None
    assert info.last_activity is not None


def test_parse_session_info_uses_provided_title_override(
    adapter: GeminiAdapter, tmp_path: Path,
) -> None:
    p = tmp_path / "session.jsonl"
    _write_jsonl(p, [_header_line(), _user_line("auto-title")])
    info = adapter.parse_session_info(
        p, "sid", titles={"sid": "Custom Title"},
    )
    assert info is not None
    assert info.title == "Custom Title"


def test_parse_session_info_returns_none_for_empty_file(
    adapter: GeminiAdapter, tmp_path: Path,
) -> None:
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    assert adapter.parse_session_info(p, "sid") is None


# ---------------------------------------------------------------------------
# Blocks integration — round-trip through extract_blocks


def test_assistant_blocks_round_trip_through_extract_blocks(
    adapter: GeminiAdapter, tmp_path: Path,
) -> None:
    """The normalized output should work with the existing
    ``extract_blocks`` helper, since SessionStore relies on it."""
    from manager.protocol import extract_blocks

    p = tmp_path / "session.jsonl"
    _write_jsonl(p, [
        _header_line(),
        _gemini_line(
            "hello",
            thoughts=[{"subject": "Greeting", "description": "say hi", "timestamp": "t0"}],
        ),
    ])
    msgs = adapter.read_messages(p)
    blocks = extract_blocks(msgs[0])
    # Both the thinking and the text content should land as ContentBlocks
    # (extract_blocks collapses thinking blocks into text blocks for display).
    assert any(b.type == "text" and "hello" in (b.text or "") for b in blocks)


# ---------------------------------------------------------------------------
# JSONL path resolver (registered on the HarnessSpec)


def test_jsonl_path_resolver_returns_empty_when_tmp_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ~/.gemini/tmp doesn't exist (fresh install), resolver returns []."""
    monkeypatch.setenv("GEMINI_HOME", str(tmp_path / "no-such-dir"))
    assert _gemini_jsonl_candidates("11111111-1111-1111-1111-111111111111") == []


def test_jsonl_path_resolver_finds_session_by_uuid_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The session filename embeds only the first 8 chars of the UUID, so
    the resolver must glob for that prefix across every project dir."""
    monkeypatch.setenv("GEMINI_HOME", str(tmp_path))
    chats = tmp_path / "tmp" / "assistant" / "chats"
    chats.mkdir(parents=True)
    sid = "11111111-1111-1111-1111-111111111111"
    target = chats / f"session-2026-05-15T20-55-{sid[:8]}.jsonl"
    target.write_text("{}\n")

    found = _gemini_jsonl_candidates(sid)
    assert target in found


def test_jsonl_path_resolver_ignores_unrelated_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_HOME", str(tmp_path))
    chats = tmp_path / "tmp" / "assistant" / "chats"
    chats.mkdir(parents=True)
    # File belongs to a different session — first 8 chars differ.
    (chats / "session-2026-05-15T20-55-deadbeef.jsonl").write_text("{}\n")
    found = _gemini_jsonl_candidates("11111111-1111-1111-1111-111111111111")
    assert found == []


def test_jsonl_path_resolver_returns_empty_for_short_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge: an empty session id shouldn't match every file in the dir."""
    monkeypatch.setenv("GEMINI_HOME", str(tmp_path))
    chats = tmp_path / "tmp" / "assistant" / "chats"
    chats.mkdir(parents=True)
    (chats / "session-anything.jsonl").write_text("{}\n")
    assert _gemini_jsonl_candidates("") == []
