"""Tests for manager/store.py — uses fixture JSONL files."""

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from manager.store import SessionStore, _parse_timestamp, _extract_text


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


class TestSessionStoreUsesContextPath:
    """Test that SessionStore uses context/ directly for sessions."""

    def test_uses_context_sessions_dir(self, tmp_path, monkeypatch):
        """SessionStore should use context/ directly via utils.paths."""
        # Create context structure
        context_dir = tmp_path / "context"
        context_dir.mkdir(parents=True)

        # Mock utils.paths to use our tmp_path
        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            store = SessionStore(tmp_path)

            # The store should be using context/
            assert store.sessions_dir == context_dir


class TestSessionStoreListSessions:
    @pytest.fixture
    def store_dir(self, tmp_path):
        """Set up a directory mimicking context/ (sessions live at root)."""
        context_dir = tmp_path / "context"
        context_dir.mkdir(parents=True)
        return tmp_path, context_dir

    def test_list_empty(self, store_dir):
        project_dir, context_dir = store_dir
        with patch("utils.paths.PROJECT_ROOT", project_dir):
            store = SessionStore(project_dir)
            assert store.list_sessions() == []

    def test_list_one_session(self, store_dir):
        project_dir, context_dir = store_dir
        _write_session_jsonl(
            context_dir / "abc123.jsonl",
            "abc123",
            [
                _user_msg("Hello world", "2026-02-05T10:00:00Z"),
                _assistant_msg("Hi!", "2026-02-05T10:00:01Z"),
            ],
        )

        with patch("utils.paths.PROJECT_ROOT", project_dir):
            store = SessionStore(project_dir)
            sessions = store.list_sessions()

        assert len(sessions) == 1
        assert sessions[0].session_id == "abc123"
        assert sessions[0].title == "Hello world"
        assert sessions[0].message_count == 2

    def test_list_sorted_by_recency(self, store_dir):
        project_dir, context_dir = store_dir

        _write_session_jsonl(
            context_dir / "old.jsonl", "old",
            [_user_msg("Old session", "2026-01-01T10:00:00Z")],
        )
        _write_session_jsonl(
            context_dir / "new.jsonl", "new",
            [_user_msg("New session", "2026-02-05T10:00:00Z")],
        )

        with patch("utils.paths.PROJECT_ROOT", project_dir):
            store = SessionStore(project_dir)
            sessions = store.list_sessions()

        assert sessions[0].session_id == "new"
        assert sessions[1].session_id == "old"

    def test_nonexistent_dir_returns_empty(self, tmp_path):
        with patch("utils.paths.PROJECT_ROOT", tmp_path / "nope"):
            store = SessionStore(tmp_path / "nope")
            store._sessions_dir = tmp_path / "nonexistent"
            assert store.list_sessions() == []


class TestSessionStoreGetSession:
    @pytest.fixture
    def populated_store(self, tmp_path):
        context_dir = tmp_path / "context"
        context_dir.mkdir(parents=True)

        _write_session_jsonl(
            context_dir / "sess1.jsonl", "sess1",
            [
                _user_msg("First question", "2026-02-05T10:00:00Z"),
                _assistant_msg("Answer here", "2026-02-05T10:00:01Z"),
                _user_msg("Follow up", "2026-02-05T10:00:02Z"),
                _assistant_msg("More info", "2026-02-05T10:00:03Z"),
            ],
        )

        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            store = SessionStore(tmp_path)
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
        context_dir = tmp_path / "context"
        context_dir.mkdir(parents=True)

        messages = []
        for i in range(10):
            messages.append(_user_msg(f"Question {i}", f"2026-02-05T10:00:{i:02d}Z"))
            messages.append(_assistant_msg(f"Answer {i}", f"2026-02-05T10:00:{i:02d}Z"))

        _write_session_jsonl(context_dir / "long.jsonl", "long", messages)

        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            store = SessionStore(tmp_path)
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
        context_dir = tmp_path / "context"
        context_dir.mkdir(parents=True)
        (context_dir / "del.jsonl").write_text("{}")

        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            store = SessionStore(tmp_path)

        assert store.delete_session("del") is True
        assert not (context_dir / "del.jsonl").exists()

    def test_delete_nonexistent(self, tmp_path):
        context_dir = tmp_path / "context"
        context_dir.mkdir(parents=True)

        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            store = SessionStore(tmp_path)
        assert store.delete_session("nope") is False

    def test_delete_removes_from_index(self, tmp_path, monkeypatch):
        """Test that deleting a session also removes it from the vector index."""
        from unittest.mock import MagicMock, patch as mock_patch

        context_dir = tmp_path / "context"
        context_dir.mkdir(parents=True)
        (context_dir / "indexed-session.jsonl").write_text("{}")

        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            store = SessionStore(tmp_path)

        # Mock the remove_session_from_index function
        with mock_patch("manager.store.remove_session_from_index") as mock_remove:
            mock_remove.return_value = True

            # Delete the session
            result = store.delete_session("indexed-session")

            # Verify session was deleted
            assert result is True
            assert not (context_dir / "indexed-session.jsonl").exists()

            # Verify index removal was called
            mock_remove.assert_called_once_with(
                "indexed-session", collection_name="history"
            )
