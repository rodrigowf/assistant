"""Tests for manager/store.py — uses fixture JSONL files."""

import json
from datetime import datetime, timezone

import pytest

from manager.store import SessionStore, _mangle_path, _parse_timestamp, _extract_text


# ---------------------------------------------------------------------------
# Helper to write realistic JSONL
# ---------------------------------------------------------------------------

def _write_session_jsonl(path, session_id, messages):
    """Write a minimal JSONL file with user/assistant messages."""
    lines = []
    for msg in messages:
        lines.append(json.dumps(msg))
    path.write_text("\n".join(lines) + "\n")


def _user_msg(text, timestamp, uuid="u1", session_id="sess1"):
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        "timestamp": timestamp,
        "uuid": uuid,
        "sessionId": session_id,
    }


def _assistant_msg(text, timestamp, uuid="a1", session_id="sess1"):
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
        "timestamp": timestamp,
        "uuid": uuid,
        "sessionId": session_id,
    }


# ---------------------------------------------------------------------------
# Tests for utility functions
# ---------------------------------------------------------------------------


class TestManglePath:
    def test_basic(self):
        assert _mangle_path("/home/rodrigo/Projects/assistant") == "-home-rodrigo-Projects-assistant"

    def test_trailing_slash(self):
        assert _mangle_path("/home/user/project/") == "-home-user-project"

    def test_root(self):
        assert _mangle_path("/") == ""


class TestParseTimestamp:
    def test_with_z_suffix(self):
        dt = _parse_timestamp("2026-02-05T01:48:05.911Z")
        assert dt.year == 2026
        assert dt.month == 2
        assert dt.day == 5
        assert dt.tzinfo is not None

    def test_with_offset(self):
        dt = _parse_timestamp("2026-02-05T01:48:05.911+00:00")
        assert dt.year == 2026


class TestExtractText:
    def test_text_blocks(self):
        msg = {"message": {"content": [{"type": "text", "text": "Hello"}]}}
        assert _extract_text(msg) == "Hello"

    def test_string_content(self):
        msg = {"message": {"content": "Plain string"}}
        assert _extract_text(msg) == "Plain string"

    def test_multiple_blocks(self):
        msg = {"message": {"content": [
            {"type": "text", "text": "Part 1"},
            {"type": "text", "text": "Part 2"},
        ]}}
        assert _extract_text(msg) == "Part 1\nPart 2"

    def test_empty_message(self):
        assert _extract_text({}) == ""


# ---------------------------------------------------------------------------
# Tests for SessionStore
# ---------------------------------------------------------------------------


class TestSessionStoreConfigDir:
    """Test CLAUDE_CONFIG_DIR environment variable support."""

    def test_default_uses_home_claude(self, tmp_path, monkeypatch):
        """Without CLAUDE_CONFIG_DIR, uses ~/.claude."""
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        project_dir = tmp_path / "myproject"
        project_dir.mkdir()

        store = SessionStore(project_dir)
        mangled = _mangle_path(str(project_dir))

        from pathlib import Path
        expected = Path.home() / ".claude" / "projects" / mangled
        assert store.sessions_dir == expected

    def test_respects_claude_config_dir(self, tmp_path, monkeypatch):
        """CLAUDE_CONFIG_DIR overrides the default location."""
        custom_config = tmp_path / "custom-claude"
        custom_config.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom_config))

        project_dir = tmp_path / "myproject"
        project_dir.mkdir()

        store = SessionStore(project_dir)
        mangled = _mangle_path(str(project_dir))

        expected = custom_config / "projects" / mangled
        assert store.sessions_dir == expected


class TestSessionStoreListSessions:
    @pytest.fixture
    def store_dir(self, tmp_path):
        """Set up a directory mimicking ~/.claude/projects/<mangled>/."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        mangled = _mangle_path(str(project_dir))
        sessions_dir = tmp_path / "claude_home" / "projects" / mangled
        sessions_dir.mkdir(parents=True)
        return project_dir, sessions_dir

    def test_list_empty(self, store_dir, monkeypatch):
        project_dir, sessions_dir = store_dir
        store = SessionStore(project_dir)
        # Patch the sessions dir to point to our fixture
        store._sessions_dir = sessions_dir
        assert store.list_sessions() == []

    def test_list_one_session(self, store_dir):
        project_dir, sessions_dir = store_dir
        _write_session_jsonl(
            sessions_dir / "abc123.jsonl",
            "abc123",
            [
                _user_msg("Hello world", "2026-02-05T10:00:00Z"),
                _assistant_msg("Hi!", "2026-02-05T10:00:01Z"),
            ],
        )

        store = SessionStore(project_dir)
        store._sessions_dir = sessions_dir
        sessions = store.list_sessions()

        assert len(sessions) == 1
        assert sessions[0].session_id == "abc123"
        assert sessions[0].title == "Hello world"
        assert sessions[0].message_count == 2

    def test_list_sorted_by_recency(self, store_dir):
        project_dir, sessions_dir = store_dir

        _write_session_jsonl(
            sessions_dir / "old.jsonl", "old",
            [_user_msg("Old session", "2026-01-01T10:00:00Z")],
        )
        _write_session_jsonl(
            sessions_dir / "new.jsonl", "new",
            [_user_msg("New session", "2026-02-05T10:00:00Z")],
        )

        store = SessionStore(project_dir)
        store._sessions_dir = sessions_dir
        sessions = store.list_sessions()

        assert sessions[0].session_id == "new"
        assert sessions[1].session_id == "old"

    def test_nonexistent_dir_returns_empty(self, tmp_path):
        store = SessionStore(tmp_path / "nope")
        store._sessions_dir = tmp_path / "nonexistent"
        assert store.list_sessions() == []


class TestSessionStoreGetSession:
    @pytest.fixture
    def populated_store(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        _write_session_jsonl(
            sessions_dir / "sess1.jsonl", "sess1",
            [
                _user_msg("First question", "2026-02-05T10:00:00Z"),
                _assistant_msg("Answer here", "2026-02-05T10:00:01Z"),
                _user_msg("Follow up", "2026-02-05T10:00:02Z"),
                _assistant_msg("More info", "2026-02-05T10:00:03Z"),
            ],
        )

        store = SessionStore(project_dir)
        store._sessions_dir = sessions_dir
        return store

    def test_get_existing(self, populated_store):
        detail = populated_store.get_session("sess1")
        assert detail is not None
        assert detail.session_id == "sess1"
        assert detail.message_count == 4
        assert detail.title == "First question"

    def test_messages_present(self, populated_store):
        detail = populated_store.get_session("sess1")
        assert len(detail.messages) == 4
        assert detail.messages[0].role == "user"
        assert detail.messages[1].role == "assistant"

    def test_get_nonexistent(self, populated_store):
        assert populated_store.get_session("nope") is None


class TestSessionStoreGetPreview:
    @pytest.fixture
    def store_with_long_session(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        messages = []
        for i in range(10):
            messages.append(_user_msg(f"Question {i}", f"2026-02-05T10:00:{i:02d}Z"))
            messages.append(_assistant_msg(f"Answer {i}", f"2026-02-05T10:00:{i:02d}Z"))

        _write_session_jsonl(sessions_dir / "long.jsonl", "long", messages)

        store = SessionStore(project_dir)
        store._sessions_dir = sessions_dir
        return store

    def test_preview_limits_messages(self, store_with_long_session):
        preview = store_with_long_session.get_preview("long", max_messages=3)
        assert len(preview) == 3

    def test_preview_returns_last_messages(self, store_with_long_session):
        preview = store_with_long_session.get_preview("long", max_messages=2)
        # Should be the last 2 messages
        assert "9" in preview[-1].text


class TestSessionStoreDeleteSession:
    def test_delete_existing(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        (sessions_dir / "del.jsonl").write_text("{}")

        store = SessionStore(tmp_path)
        store._sessions_dir = sessions_dir

        assert store.delete_session("del") is True
        assert not (sessions_dir / "del.jsonl").exists()

    def test_delete_nonexistent(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        store = SessionStore(tmp_path)
        store._sessions_dir = sessions_dir
        assert store.delete_session("nope") is False
