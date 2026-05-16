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


class TestSessionStoreListSessionsCache:
    """The (mtime_ns, size) cache keeps list_sessions() O(N) in file count
    after the first call, instead of O(total bytes)."""

    @pytest.fixture
    def store_dir(self, tmp_path):
        context_dir = tmp_path / "context"
        context_dir.mkdir(parents=True)
        return tmp_path, context_dir

    def _make_store(self, project_dir, n_sessions=3):
        for i in range(n_sessions):
            _write_session_jsonl(
                project_dir / "context" / f"sess{i}.jsonl",
                f"sess{i}",
                [_user_msg(f"Question {i}", f"2026-02-05T10:00:{i:02d}Z")],
            )
        with patch("utils.paths.PROJECT_ROOT", project_dir):
            return SessionStore(project_dir)

    def test_cache_hit_skips_reparse_for_unchanged_files(self, store_dir):
        project_dir, context_dir = store_dir
        store = self._make_store(project_dir, n_sessions=3)

        store.list_sessions()  # cold — populates cache

        with patch.object(SessionStore, "_parse_session_info", wraps=store._parse_session_info) as spy:
            store.list_sessions()
            assert spy.call_count == 0, (
                f"unchanged files should not be re-parsed; was called {spy.call_count} times"
            )

    def test_cache_invalidated_when_file_changes(self, store_dir):
        project_dir, context_dir = store_dir
        store = self._make_store(project_dir, n_sessions=3)
        store.list_sessions()  # populate cache

        # Modify one session by appending a new message AND bumping mtime
        target = context_dir / "sess1.jsonl"
        with open(target, "a") as f:
            f.write(json.dumps(_user_msg("New question", "2026-02-06T10:00:00Z")) + "\n")
        # Force mtime to be measurably newer (the test runs in well under
        # filesystem mtime resolution so we set it explicitly)
        import os
        st = target.stat()
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

        with patch.object(SessionStore, "_parse_session_info", wraps=store._parse_session_info) as spy:
            sessions = store.list_sessions()
            assert spy.call_count == 1, "only the modified file should be re-parsed"
            # Verify the modified session reflects the new state
            modified = [s for s in sessions if s.session_id == "sess1"][0]
            assert modified.message_count == 2

    def test_cache_drops_deleted_files(self, store_dir):
        project_dir, context_dir = store_dir
        store = self._make_store(project_dir, n_sessions=3)
        store.list_sessions()
        assert "sess1" in store._info_cache

        (context_dir / "sess1.jsonl").unlink()
        sessions = store.list_sessions()

        assert "sess1" not in store._info_cache
        assert all(s.session_id != "sess1" for s in sessions)

    def test_cache_picks_up_new_files(self, store_dir):
        project_dir, context_dir = store_dir
        store = self._make_store(project_dir, n_sessions=2)
        store.list_sessions()
        assert len(store._info_cache) == 2

        _write_session_jsonl(
            context_dir / "brand_new.jsonl", "brand_new",
            [_user_msg("Hello", "2026-03-01T10:00:00Z")],
        )

        sessions = store.list_sessions()
        assert "brand_new" in store._info_cache
        assert any(s.session_id == "brand_new" for s in sessions)

    def test_titles_loaded_once_per_call(self, store_dir):
        """_load_titles() should be called once per list_sessions() call,
        not once per file. Hoisting it out of the parse loop avoids 92×
        redundant reads of .titles.json on the Jetson."""
        project_dir, context_dir = store_dir
        store = self._make_store(project_dir, n_sessions=5)

        with patch.object(SessionStore, "_load_titles", wraps=store._load_titles) as spy:
            store.list_sessions()
            assert spy.call_count == 1, (
                f"titles should be loaded once per call, was {spy.call_count}"
            )

    def test_title_override_applied_on_cache_hit(self, store_dir):
        """If the user renames a session, the new title should appear even
        if the JSONL itself didn't change (titles live in a separate file)."""
        project_dir, context_dir = store_dir
        store = self._make_store(project_dir, n_sessions=2)
        store.list_sessions()  # populate cache

        store.rename_session("sess0", "Custom name")
        sessions = store.list_sessions()
        renamed = [s for s in sessions if s.session_id == "sess0"][0]
        assert renamed.title == "Custom name"


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
            assert (context_dir / "trash" / "del.jsonl").is_file()

    def test_delete_collision_keeps_both(self, tmp_path):
        context_dir = tmp_path / "context"
        context_dir.mkdir(parents=True)
        trash_dir = context_dir / "trash"
        trash_dir.mkdir()
        (trash_dir / "dup.jsonl").write_text("old")
        (context_dir / "dup.jsonl").write_text("new")

        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            store = SessionStore(tmp_path)
            assert store.delete_session("dup") is True

        assert (trash_dir / "dup.jsonl").read_text() == "old"
        moved = [p for p in trash_dir.iterdir() if p.name != "dup.jsonl"]
        assert len(moved) == 1
        assert moved[0].read_text() == "new"

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


# ---------------------------------------------------------------------------
# Tests for duplicate_session / truncate_session / fork_session
# ---------------------------------------------------------------------------


def _tool_result_user_line(tool_use_id, output, timestamp, uuid="tr1"):
    """Claude-style user message that contains only a tool_result block."""
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": output}],
        },
        "timestamp": timestamp,
        "uuid": uuid,
    }


def _assistant_tool_use_line(name, tool_id, timestamp, uuid="au1"):
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": {}}],
        },
        "timestamp": timestamp,
        "uuid": uuid,
    }


class TestDuplicateSession:
    def test_copies_file_and_title(self, tmp_path):
        from unittest.mock import patch
        context_dir = tmp_path / "context"
        context_dir.mkdir(parents=True)
        _write_session_jsonl(
            context_dir / "src.jsonl", "src",
            [
                _user_msg("Hello", "2026-02-05T10:00:00Z", uuid="u1"),
                _assistant_msg("Hi!", "2026-02-05T10:00:01Z", uuid="a1"),
            ],
        )
        (context_dir / ".titles.json").write_text(json.dumps({"src": "My chat"}))

        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            store = SessionStore(tmp_path)
            new_id = store.duplicate_session("src")

        assert new_id is not None
        assert new_id != "src"
        # The copy file exists with same content
        src_text = (context_dir / "src.jsonl").read_text()
        new_text = (context_dir / f"{new_id}.jsonl").read_text()
        assert src_text == new_text
        # Title was copied with " (copy)" suffix
        titles = json.loads((context_dir / ".titles.json").read_text())
        assert titles["src"] == "My chat"
        assert titles[new_id] == "My chat (copy)"

    def test_no_title_entry_when_source_unnamed(self, tmp_path):
        from unittest.mock import patch
        context_dir = tmp_path / "context"
        context_dir.mkdir(parents=True)
        _write_session_jsonl(
            context_dir / "src.jsonl", "src",
            [_user_msg("Hello", "2026-02-05T10:00:00Z")],
        )
        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            store = SessionStore(tmp_path)
            new_id = store.duplicate_session("src")

        assert new_id is not None
        # No titles entry should be created if source had none
        titles_path = context_dir / ".titles.json"
        if titles_path.exists():
            titles = json.loads(titles_path.read_text())
            assert new_id not in titles

    def test_returns_none_for_missing(self, tmp_path):
        from unittest.mock import patch
        context_dir = tmp_path / "context"
        context_dir.mkdir(parents=True)
        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            store = SessionStore(tmp_path)
            assert store.duplicate_session("does-not-exist") is None


class TestTruncateSession:
    def test_drops_last_n_visible_messages(self, tmp_path):
        from unittest.mock import patch
        context_dir = tmp_path / "context"
        context_dir.mkdir(parents=True)
        _write_session_jsonl(
            context_dir / "s.jsonl", "s",
            [
                _user_msg("first", "2026-02-05T10:00:00Z", uuid="u1"),
                _assistant_msg("reply 1", "2026-02-05T10:00:01Z", uuid="a1"),
                _user_msg("second", "2026-02-05T10:00:02Z", uuid="u2"),
                _assistant_msg("reply 2", "2026-02-05T10:00:03Z", uuid="a2"),
            ],
        )
        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            store = SessionStore(tmp_path)
            # Drop the last two visible messages — leaves u1 + a1.
            assert store.truncate_session("s", 2) is True

        lines = [json.loads(line) for line in (context_dir / "s.jsonl").read_text().strip().split("\n")]
        assert [l["uuid"] for l in lines] == ["u1", "a1"]

    def test_drops_trailing_tool_result_lines(self, tmp_path):
        """Claude-style: a 'user' line containing only tool_result is internal,
        not a visible message — and trailing internal lines after the last
        kept visible message should be dropped too (they'd be dangling)."""
        from unittest.mock import patch
        context_dir = tmp_path / "context"
        context_dir.mkdir(parents=True)

        _write_session_jsonl(
            context_dir / "s.jsonl", "s",
            [
                _user_msg("ask", "2026-02-05T10:00:00Z", uuid="u1"),
                _assistant_tool_use_line("read", "tool-1", "2026-02-05T10:00:01Z", uuid="a1"),
                _tool_result_user_line("tool-1", "result", "2026-02-05T10:00:02Z", uuid="tr1"),
                _assistant_msg("done", "2026-02-05T10:00:03Z", uuid="a2"),
                _user_msg("next", "2026-02-05T10:00:04Z", uuid="u3"),
            ],
        )
        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            store = SessionStore(tmp_path)
            # Visible turns: u1, a1, a2, u3 (4 total — tr1 is not visible).
            # drop_last_n=1 → drop u3 → keep u1, a1, tr1, a2.
            assert store.truncate_session("s", 1) is True

        lines = [json.loads(line) for line in (context_dir / "s.jsonl").read_text().strip().split("\n")]
        assert [l["uuid"] for l in lines] == ["u1", "a1", "tr1", "a2"]

    def test_qwen_parts_format_counted_as_visible(self, tmp_path):
        """Qwen JSONL uses ``message.parts`` (not ``content``) and ``role: model``.
        Those user/assistant turns must still count as visible."""
        from unittest.mock import patch
        context_dir = tmp_path / "context"
        context_dir.mkdir(parents=True)

        qwen_lines = [
            {"type": "user", "message": {"role": "user", "parts": [{"text": "hi"}]},
             "timestamp": "2026-02-05T10:00:00Z", "uuid": "u1"},
            {"type": "system", "subtype": "ui_telemetry", "timestamp": "2026-02-05T10:00:00.5Z"},
            {"type": "assistant", "message": {"role": "model", "parts": [{"text": "yo"}]},
             "timestamp": "2026-02-05T10:00:01Z", "uuid": "a1"},
            {"type": "user", "message": {"role": "user", "parts": [{"text": "more?"}]},
             "timestamp": "2026-02-05T10:00:02Z", "uuid": "u2"},
        ]
        (context_dir / "chats").mkdir(parents=True)
        (context_dir / "chats" / "qs.jsonl").write_text(
            "\n".join(json.dumps(l) for l in qwen_lines) + "\n"
        )

        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            store = SessionStore(tmp_path)
            # 3 visible turns (u1, a1, u2). drop_last_n=1 → keep u1 + telemetry + a1.
            assert store.truncate_session("qs", 1) is True

        out = [json.loads(l) for l in (context_dir / "chats" / "qs.jsonl").read_text().strip().split("\n")]
        assert [l.get("uuid", l["type"]) for l in out] == ["u1", "system", "a1"]

    def test_keeps_orchestrator_internal_lines(self, tmp_path):
        """Orchestrator JSONL has tool_use / tool_result as their own line types;
        they should be preserved when they precede the cutoff visible message."""
        from unittest.mock import patch
        context_dir = tmp_path / "context"
        context_dir.mkdir(parents=True)

        lines_in = [
            {"type": "orchestrator_meta", "orchestrator": True, "session_id": "s"},
            {"type": "user", "message": {"role": "user", "content": "ask"}, "timestamp": "2026-02-05T10:00:00Z"},
            {"type": "tool_use", "tool_call_id": "t1", "tool_name": "x", "tool_input": {}},
            {"type": "tool_result", "tool_call_id": "t1", "output": "r"},
            {"type": "assistant", "message": {"role": "assistant", "content": "done"}, "timestamp": "2026-02-05T10:00:01Z"},
            {"type": "user", "message": {"role": "user", "content": "next"}, "timestamp": "2026-02-05T10:00:02Z"},
        ]
        (context_dir / "s.jsonl").write_text("\n".join(json.dumps(l) for l in lines_in) + "\n")

        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            store = SessionStore(tmp_path)
            # Visible: user (0), assistant (1), user (2). Drop last 1 → drop "next".
            assert store.truncate_session("s", 1) is True

        out_lines = [json.loads(l) for l in (context_dir / "s.jsonl").read_text().strip().split("\n")]
        types = [l["type"] for l in out_lines]
        assert types == ["orchestrator_meta", "user", "tool_use", "tool_result", "assistant"]

    def test_drop_last_n_out_of_range_returns_false(self, tmp_path):
        from unittest.mock import patch
        context_dir = tmp_path / "context"
        context_dir.mkdir(parents=True)
        _write_session_jsonl(
            context_dir / "s.jsonl", "s",
            [_user_msg("only", "2026-02-05T10:00:00Z", uuid="u1")],
        )
        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            store = SessionStore(tmp_path)
            assert store.truncate_session("s", 5) is False
            # File untouched
            assert (context_dir / "s.jsonl").read_text().strip() != ""

    def test_drop_last_n_zero_is_noop(self, tmp_path):
        from unittest.mock import patch
        context_dir = tmp_path / "context"
        context_dir.mkdir(parents=True)
        _write_session_jsonl(
            context_dir / "s.jsonl", "s",
            [
                _user_msg("one", "2026-02-05T10:00:00Z", uuid="u1"),
                _assistant_msg("two", "2026-02-05T10:00:01Z", uuid="a1"),
            ],
        )
        original = (context_dir / "s.jsonl").read_text()
        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            store = SessionStore(tmp_path)
            assert store.truncate_session("s", 0) is True
        assert (context_dir / "s.jsonl").read_text() == original


class TestForkSession:
    def test_duplicates_then_drops_last_n(self, tmp_path):
        from unittest.mock import patch
        context_dir = tmp_path / "context"
        context_dir.mkdir(parents=True)
        _write_session_jsonl(
            context_dir / "src.jsonl", "src",
            [
                _user_msg("first", "2026-02-05T10:00:00Z", uuid="u1"),
                _assistant_msg("reply 1", "2026-02-05T10:00:01Z", uuid="a1"),
                _user_msg("second", "2026-02-05T10:00:02Z", uuid="u2"),
                _assistant_msg("reply 2", "2026-02-05T10:00:03Z", uuid="a2"),
            ],
        )
        (context_dir / ".titles.json").write_text(json.dumps({"src": "Chat"}))

        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            store = SessionStore(tmp_path)
            new_id = store.fork_session("src", 2)

        assert new_id is not None
        # Original untouched
        src_lines = [json.loads(line) for line in (context_dir / "src.jsonl").read_text().strip().split("\n")]
        assert len(src_lines) == 4
        # Fork has only the first two messages (dropped last 2 visible)
        new_lines = [json.loads(line) for line in (context_dir / f"{new_id}.jsonl").read_text().strip().split("\n")]
        assert [l["uuid"] for l in new_lines] == ["u1", "a1"]
        # Title was copied
        titles = json.loads((context_dir / ".titles.json").read_text())
        assert titles[new_id] == "Chat (copy)"

    def test_rolls_back_on_bad_drop_count(self, tmp_path):
        from unittest.mock import patch
        context_dir = tmp_path / "context"
        context_dir.mkdir(parents=True)
        _write_session_jsonl(
            context_dir / "src.jsonl", "src",
            [_user_msg("only", "2026-02-05T10:00:00Z", uuid="u1")],
        )
        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            store = SessionStore(tmp_path)
            with patch("manager.store.remove_session_from_index", return_value=True):
                # We patch index removal because fork_session→delete_session calls it
                new_id = store.fork_session("src", 10)

        assert new_id is None
        # No orphan files left behind
        jsonls = sorted(p.name for p in context_dir.glob("*.jsonl"))
        assert jsonls == ["src.jsonl"]
