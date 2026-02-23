"""Tests for api/indexer.py — background indexing for memory and history."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from api.indexer import (
    _run_index_script,
    MemoryWatcher,
    HistoryIndexer,
)


class TestRunIndexScript:
    @pytest.mark.asyncio
    async def test_runs_script(self, tmp_path):
        # Create mock scripts
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        run_sh = scripts_dir / "run.sh"
        run_sh.write_text("#!/bin/bash\nexit 0")
        run_sh.chmod(0o755)
        index_py = scripts_dir / "index-memory.py"
        index_py.write_text("")

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_exec.return_value = mock_proc

            result = await _run_index_script(tmp_path, "--memory-only")

            assert result is True
            mock_exec.assert_called_once()
            args = mock_exec.call_args[0]
            assert "--memory-only" in args

    @pytest.mark.asyncio
    async def test_returns_false_on_missing_scripts(self, tmp_path):
        result = await _run_index_script(tmp_path, "--memory-only")
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_failure(self, tmp_path):
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "run.sh").write_text("")
        (scripts_dir / "index-memory.py").write_text("")

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 1
            mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))
            mock_exec.return_value = mock_proc

            result = await _run_index_script(tmp_path, "--history-only")
            assert result is False


class TestMemoryWatcher:
    def test_get_memory_dir_uses_context(self, tmp_path):
        """Memory watcher should use context/memory/ directly."""
        # Create context structure
        memory_dir = tmp_path / "context" / "memory"
        memory_dir.mkdir(parents=True)

        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            watcher = MemoryWatcher(tmp_path)
            result = watcher._get_memory_dir()
            assert result == memory_dir

    def test_stop(self):
        watcher = MemoryWatcher(Path("/tmp/test"))
        # Access the event first to create it
        _ = watcher._stop_event
        watcher.stop()
        assert watcher._running is False
        assert watcher._stop_event.is_set()


class TestHistoryIndexer:
    def test_get_sessions_dir_uses_context(self, tmp_path):
        """History indexer should use context/ directly."""
        # Create context structure
        context_dir = tmp_path / "context"
        context_dir.mkdir(parents=True)

        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            indexer = HistoryIndexer(tmp_path)
            result = indexer._get_sessions_dir()
            assert result == context_dir

    def test_compute_hash_empty_dir(self, tmp_path):
        """Hash should be empty for non-existent directory."""
        # Don't create the context directory

        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            indexer = HistoryIndexer(tmp_path)
            assert indexer._compute_sessions_hash() == ""

    def test_compute_hash_with_files(self, tmp_path):
        """Hash should change when files change."""
        # Set up directory structure — sessions live at context/ root
        context_dir = tmp_path / "context"
        context_dir.mkdir(parents=True)
        (context_dir / "session1.jsonl").write_text("content1")
        (context_dir / "session2.jsonl").write_text("content2")

        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            indexer = HistoryIndexer(tmp_path)

            hash1 = indexer._compute_sessions_hash()
            assert hash1 != ""

            # Modify a file
            (context_dir / "session1.jsonl").write_text("modified")
            hash2 = indexer._compute_sessions_hash()

            assert hash2 != hash1

    def test_stop(self):
        indexer = HistoryIndexer(Path("/tmp/test"))
        indexer.stop()
        assert indexer._running is False

    @pytest.mark.asyncio
    async def test_run_indexes_on_change(self, tmp_path):
        """Indexer should call the index script when files change."""
        # Set up directory structure — sessions at context/ root
        context_dir = tmp_path / "context"
        context_dir.mkdir(parents=True)
        (context_dir / "session.jsonl").write_text("content")

        # Create mock scripts
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "run.sh").write_text("")
        (scripts_dir / "index-memory.py").write_text("")

        with patch("utils.paths.PROJECT_ROOT", tmp_path):
            # Use a very short interval for the test
            indexer = HistoryIndexer(tmp_path, interval_seconds=0.05)

            with patch("api.indexer._run_index_script", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = True

                # Run for long enough to complete at least one iteration
                task = asyncio.create_task(indexer.run())
                await asyncio.sleep(0.2)
                indexer.stop()
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

                # Should have called the indexer
                assert mock_run.called
