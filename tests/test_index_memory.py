"""Tests for index-memory.py — indexes memory and session history from context/."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# index-memory.py has a hyphen, so import via importlib
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "index_memory",
    Path(__file__).parent.parent / "scripts" / "index-memory.py",
)
index_memory = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(index_memory)


class TestRunEmbed:
    def test_constructs_correct_command(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            index_memory.run_embed("index", "memory/")

            args = mock_run.call_args[0][0]
            assert args[0] == sys.executable
            assert "embed.py" in args[1]
            assert args[2] == "index"
            assert args[3] == "memory/"

    def test_returns_true_on_success(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert index_memory.run_embed("index", "memory/") is True

    def test_returns_false_on_failure(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            assert index_memory.run_embed("index", "memory/") is False


class TestExtractSessionText:
    def test_extracts_user_and_assistant_messages(self, tmp_path):
        jsonl = tmp_path / "test.jsonl"
        jsonl.write_text(
            '{"type": "user", "message": {"content": [{"type": "text", "text": "Hello"}]}}\n'
            '{"type": "assistant", "message": {"content": [{"type": "text", "text": "Hi there"}]}}\n'
            '{"type": "system", "message": {"content": "ignored"}}\n'
        )

        result = index_memory.extract_session_text(jsonl)

        assert "## User" in result
        assert "Hello" in result
        assert "## Assistant" in result
        assert "Hi there" in result
        assert "ignored" not in result

    def test_handles_string_content(self, tmp_path):
        jsonl = tmp_path / "test.jsonl"
        jsonl.write_text(
            '{"type": "user", "message": {"content": "Simple string"}}\n'
        )

        result = index_memory.extract_session_text(jsonl)

        assert "Simple string" in result

    def test_handles_missing_file(self, tmp_path):
        jsonl = tmp_path / "nonexistent.jsonl"
        result = index_memory.extract_session_text(jsonl)
        assert result == ""


class TestIndexMemory:
    @pytest.fixture
    def setup_context_dirs(self, tmp_path):
        """Set up temporary context/ directory structure."""
        memory_dir = tmp_path / "context" / "memory"
        memory_dir.mkdir(parents=True)

        # Add memory files
        (memory_dir / "MEMORY.md").write_text("# Memory\nTest content")
        (memory_dir / "patterns.md").write_text("# Patterns\nMore content")

        return tmp_path, memory_dir

    def test_indexes_memory_files(self, setup_context_dirs):
        tmp_path, memory_dir = setup_context_dirs
        calls = []

        def fake_run_embed(command, *args):
            calls.append((command, args))
            return True

        with patch.object(index_memory, "run_embed", side_effect=fake_run_embed):
            with patch("utils.paths.PROJECT_ROOT", tmp_path):
                index_memory.index_memory(reset=False)

        commands = [c[0] for c in calls]
        assert "index" in commands

        # Should index memory collection
        index_call = next(c for c in calls if c[0] == "index")
        assert "memory" in index_call[1]

    def test_skips_empty_memory_dir(self, tmp_path, capsys):
        memory_dir = tmp_path / "context" / "memory"
        memory_dir.mkdir(parents=True)
        # Empty memory dir

        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            index_memory.index_memory(reset=False)

        captured = capsys.readouterr()
        assert "No memory files" in captured.out

    def test_skips_missing_memory_dir(self, tmp_path, capsys):
        # No context/memory dir

        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            index_memory.index_memory(reset=False)

        captured = capsys.readouterr()
        assert "Memory directory not found" in captured.out


class TestIndexHistory:
    @pytest.fixture
    def setup_sessions(self, tmp_path):
        """Set up temporary session files in context/."""
        context_dir = tmp_path / "context"
        context_dir.mkdir(parents=True)

        # Add session files at context/ root (where they live now)
        (context_dir / "session1.jsonl").write_text(
            '{"type": "user", "message": {"content": [{"type": "text", "text": "Hello"}]}}\n'
        )
        (context_dir / "session2.jsonl").write_text(
            '{"type": "assistant", "message": {"content": [{"type": "text", "text": "Hi"}]}}\n'
        )

        return tmp_path, context_dir

    def test_indexes_session_files(self, setup_sessions):
        tmp_path, sessions_dir = setup_sessions
        calls = []

        def fake_run_embed(command, *args):
            calls.append((command, args))
            return True

        with patch.object(index_memory, "run_embed", side_effect=fake_run_embed):
            with patch("utils.paths.PROJECT_ROOT", tmp_path):
                # Also need to patch PROJECT_DIR in index_memory for temp file handling
                with patch.object(index_memory, "PROJECT_DIR", tmp_path):
                    index_memory.index_history(reset=False)

        commands = [c[0] for c in calls]
        assert "index" in commands

        # Should index history collection
        index_call = next(c for c in calls if c[0] == "index")
        assert "history" in index_call[1]

    def test_skips_missing_context_dir(self, tmp_path, capsys):
        # No context/ dir at all

        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            index_memory.index_history(reset=False)

        captured = capsys.readouterr()
        assert "not found" in captured.out


class TestMain:
    def test_runs_with_memory_only_flag(self, tmp_path):
        memory_dir = tmp_path / "context" / "memory"
        memory_dir.mkdir(parents=True)
        (memory_dir / "test.md").write_text("content")

        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            with patch.object(index_memory, "PROJECT_DIR", tmp_path):
                with patch.object(index_memory, "run_embed", return_value=True) as mock_embed:
                    with patch("sys.argv", ["index-memory.py", "--memory-only"]):
                        index_memory.main()

        # Should have called index for memory
        calls = [c[0][0] for c in mock_embed.call_args_list]
        # Index should be called if memory files exist
        # Stats is always called

    def test_runs_with_history_only_flag(self, tmp_path):
        context_dir = tmp_path / "context"
        context_dir.mkdir(parents=True)
        (context_dir / "session.jsonl").write_text(
            '{"type": "user", "message": {"content": "test"}}\n'
        )

        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            with patch.object(index_memory, "PROJECT_DIR", tmp_path):
                with patch.object(index_memory, "run_embed", return_value=True):
                    with patch("sys.argv", ["index-memory.py", "--history-only"]):
                        index_memory.main()

        # Should complete without error
