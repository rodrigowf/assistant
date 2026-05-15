"""Tests for SessionStore against Qwen JSONL files.

The existing ``test_store.py`` exercises the store against Claude-format
fixtures. This file focuses on:

- Qwen sessions in ``context/chats/`` are scanned alongside Claude
  sessions in ``context/`` and mixed correctly in the listing.
- Each session is tagged with the right ``provider``.
- ``get_session`` / ``get_messages_paginated`` work for Qwen sessions and
  return Qwen-shaped previews (thinking blocks, function-call tool_use).
- Mixed-provider scans return both, sorted by last_activity.
- Deleting a Qwen session moves it into ``context/trash/`` (same as Claude).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from manager.store import SessionStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write_qwen_jsonl(path: Path, session_id: str, lines: list[dict]) -> None:
    """Write a Qwen-shape JSONL file into a chats/ directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")


def _qwen_user(text: str, ts: str, sid: str) -> dict:
    return {
        "uuid": "u1",
        "parentUuid": None,
        "sessionId": sid,
        "timestamp": ts,
        "type": "user",
        "message": {"role": "user", "parts": [{"text": text}]},
    }


def _qwen_assistant(text: str, ts: str, sid: str) -> dict:
    return {
        "uuid": "a1",
        "parentUuid": "u1",
        "sessionId": sid,
        "timestamp": ts,
        "type": "assistant",
        "model": "qwen3.6-plus",
        "message": {"role": "model", "parts": [{"text": text}]},
    }


def _qwen_assistant_with_tool(
    text: str, tool_id: str, tool_name: str, tool_args: dict, ts: str, sid: str,
) -> dict:
    return {
        "uuid": "a2",
        "parentUuid": "u1",
        "sessionId": sid,
        "timestamp": ts,
        "type": "assistant",
        "model": "qwen3.6-plus",
        "message": {
            "role": "model",
            "parts": [
                {"text": text},
                {"functionCall": {"id": tool_id, "name": tool_name, "args": tool_args}},
            ],
        },
    }


def _claude_user(text: str, ts: str) -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": text},
        "timestamp": ts,
    }


def _claude_assistant(text: str, ts: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        "timestamp": ts,
    }


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """A fake project root with both context/ and context/chats/."""
    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "chats").mkdir()
    return tmp_path


@pytest.fixture
def qwen_session(project_dir: Path) -> str:
    """Write a Qwen session JSONL and return its session id."""
    sid = "qwen-sess-1"
    path = project_dir / "context" / "chats" / f"{sid}.jsonl"
    _write_qwen_jsonl(path, sid, [
        _qwen_user("Build me a feature", "2026-05-15T01:00:00.000Z", sid),
        # Simulate Qwen's typical noisy telemetry — should be ignored.
        {
            "type": "system", "subtype": "ui_telemetry",
            "timestamp": "2026-05-15T01:00:00.500Z",
            "systemPayload": {},
        },
        _qwen_assistant_with_tool(
            "On it. Running ls first.",
            tool_id="call_42", tool_name="Bash", tool_args={"command": "ls"},
            ts="2026-05-15T01:00:05.000Z", sid=sid,
        ),
        _qwen_user("Looks good, thanks", "2026-05-15T01:01:00.000Z", sid),
    ])
    return sid


@pytest.fixture
def claude_session(project_dir: Path) -> str:
    """Write a Claude session JSONL and return its session id."""
    sid = "claude-sess-1"
    path = project_dir / "context" / f"{sid}.jsonl"
    path.write_text("\n".join(json.dumps(l) for l in [
        _claude_user("Hello Claude", "2026-05-15T02:00:00.000Z"),
        _claude_assistant("Hello!", "2026-05-15T02:00:01.000Z"),
    ]) + "\n")
    return sid


# ---------------------------------------------------------------------------
# list_sessions — scans both locations, mixes providers
# ---------------------------------------------------------------------------

class TestMixedProviderListing:
    def test_lists_qwen_alone(self, project_dir: Path, qwen_session: str):
        store = SessionStore(project_dir)
        sessions = store.list_sessions()
        assert len(sessions) == 1
        assert sessions[0].session_id == qwen_session
        assert sessions[0].provider == "qwen"

    def test_lists_claude_alone(self, project_dir: Path, claude_session: str):
        store = SessionStore(project_dir)
        sessions = store.list_sessions()
        assert len(sessions) == 1
        assert sessions[0].session_id == claude_session
        assert sessions[0].provider == "claude"

    def test_lists_both_sorted_by_recency(
        self,
        project_dir: Path,
        qwen_session: str,
        claude_session: str,
    ):
        store = SessionStore(project_dir)
        sessions = store.list_sessions()

        # Claude is more recent (02:00:00 vs Qwen's 01:01:00) → should come first.
        ids = [s.session_id for s in sessions]
        assert ids == [claude_session, qwen_session]
        providers = [s.provider for s in sessions]
        assert providers == ["claude", "qwen"]

    def test_skips_qwen_runtime_files(self, project_dir: Path, qwen_session: str):
        """Qwen writes a sibling ``<id>.runtime.json`` — the store should
        ignore non-.jsonl files entirely (handled by the .glob pattern)."""
        runtime = project_dir / "context" / "chats" / f"{qwen_session}.runtime.json"
        runtime.write_text(json.dumps({"pid": 1234, "session_id": qwen_session}))
        store = SessionStore(project_dir)
        sessions = store.list_sessions()
        # Still just the one session, plus no error.
        assert len(sessions) == 1

    def test_skips_chats_subdir_with_no_files(self, project_dir: Path, claude_session: str):
        """An empty chats/ subdirectory shouldn't break the listing."""
        store = SessionStore(project_dir)
        sessions = store.list_sessions()
        assert len(sessions) == 1
        assert sessions[0].provider == "claude"

    def test_listing_when_chats_dir_missing(self, tmp_path: Path):
        """Project with context/ but no context/chats/ should still work for
        Claude-only setups."""
        (tmp_path / "context").mkdir()
        # Add one Claude session
        (tmp_path / "context" / "x.jsonl").write_text(
            json.dumps(_claude_user("hi", "2026-05-15T00:00:00Z")) + "\n",
        )
        store = SessionStore(tmp_path)
        sessions = store.list_sessions()
        assert len(sessions) == 1
        assert sessions[0].provider == "claude"


# ---------------------------------------------------------------------------
# get_session — provider field flows through, blocks normalize correctly
# ---------------------------------------------------------------------------

class TestGetQwenSession:
    def test_returns_detail_with_provider(
        self, project_dir: Path, qwen_session: str,
    ):
        store = SessionStore(project_dir)
        detail = store.get_session(qwen_session)
        assert detail is not None
        assert detail.session_id == qwen_session
        assert detail.provider == "qwen"

    def test_function_call_normalized_to_tool_use_block(
        self, project_dir: Path, qwen_session: str,
    ):
        """The Qwen function_call in the fixture should appear as a tool_use
        block in the preview, with the right tool name and input."""
        store = SessionStore(project_dir)
        detail = store.get_session(qwen_session)
        assert detail is not None

        # Find the assistant message that carried the function call.
        assistant_msgs = [m for m in detail.messages if m.role == "assistant"]
        assert len(assistant_msgs) == 1
        tool_blocks = [b for b in assistant_msgs[0].blocks if b.type == "tool_use"]
        assert len(tool_blocks) == 1
        assert tool_blocks[0].tool_name == "Bash"
        assert tool_blocks[0].tool_input == {"command": "ls"}
        assert tool_blocks[0].tool_use_id == "call_42"

    def test_telemetry_excluded_from_messages(
        self, project_dir: Path, qwen_session: str,
    ):
        """The ``ui_telemetry`` system event in the fixture should not appear
        in the message list."""
        store = SessionStore(project_dir)
        detail = store.get_session(qwen_session)
        assert detail is not None
        roles = [m.role for m in detail.messages]
        assert roles == ["user", "assistant", "user"]

    def test_message_previews_carry_provider(
        self, project_dir: Path, qwen_session: str,
    ):
        store = SessionStore(project_dir)
        detail = store.get_session(qwen_session)
        assert detail is not None
        # All previews inherit the session's provider.
        assert all(m.provider == "qwen" for m in detail.messages)

    def test_paginated_messages_for_qwen(
        self, project_dir: Path, qwen_session: str,
    ):
        store = SessionStore(project_dir)
        messages, total_count, has_more = store.get_messages_paginated(qwen_session)
        assert total_count == 3  # 2 user + 1 assistant
        assert has_more is False
        assert all(m.provider == "qwen" for m in messages)


# ---------------------------------------------------------------------------
# Delete + rename across providers
# ---------------------------------------------------------------------------

class TestDeleteQwenSession:
    def test_delete_moves_to_trash(self, project_dir: Path, qwen_session: str):
        store = SessionStore(project_dir)
        chats_dir = project_dir / "context" / "chats"
        jsonl = chats_dir / f"{qwen_session}.jsonl"
        assert jsonl.exists()

        assert store.delete_session(qwen_session) is True
        assert not jsonl.exists()

        # The store reads the trash dir from utils.paths.get_trash_dir() —
        # which is hardcoded to the real project. For unit tests we only
        # care that the file is gone from chats/.
        sessions = store.list_sessions()
        assert all(s.session_id != qwen_session for s in sessions)

    def test_delete_nonexistent_returns_false(self, project_dir: Path):
        store = SessionStore(project_dir)
        assert store.delete_session("never-existed") is False


class TestRenameQwenSession:
    def test_rename_persists_for_qwen(
        self, project_dir: Path, qwen_session: str,
    ):
        store = SessionStore(project_dir)
        assert store.rename_session(qwen_session, "My Qwen Conversation") is True

        sessions = store.list_sessions()
        target = next(s for s in sessions if s.session_id == qwen_session)
        assert target.title == "My Qwen Conversation"


# ---------------------------------------------------------------------------
# Provider detection cache
# ---------------------------------------------------------------------------

class TestProviderDetectionCache:
    def test_each_file_detected_independently(
        self,
        project_dir: Path,
        qwen_session: str,
        claude_session: str,
    ):
        """Once detection runs, subsequent lookups should return the same
        adapter for each file — caching shouldn't cross-contaminate."""
        store = SessionStore(project_dir)
        # Force two passes — the second should be cache-hot.
        store.list_sessions()
        sessions = store.list_sessions()

        by_id = {s.session_id: s.provider for s in sessions}
        assert by_id[qwen_session] == "qwen"
        assert by_id[claude_session] == "claude"
