"""Background indexers for memory and history.

- MemoryWatcher: Watches memory folder, indexes on file changes
- HistoryIndexer: Periodically indexes session history (every 2 min if changed)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

from utils.paths import get_memory_dir, get_sessions_dir, get_project_dir

logger = logging.getLogger(__name__)


async def _run_index_script(project_dir: Path, *args: str) -> bool:
    """Run the index-memory.py script with given arguments. Returns True on success."""
    run_sh = project_dir / "context" / "scripts" / "run.sh"
    index_py = project_dir / "context" / "scripts" / "index-memory.py"

    if not run_sh.exists() or not index_py.exists():
        logger.warning("Indexer scripts not found at context/scripts/")
        return False

    proc = await asyncio.create_subprocess_exec(
        str(run_sh), str(index_py), *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(project_dir),
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        logger.error(f"Indexer failed: {stderr.decode()}")
        return False

    return True


class MemoryWatcher:
    """Watches the memory folder and indexes on file changes.

    Uses watchfiles for efficient filesystem monitoring.
    """

    def __init__(self, project_dir: Path, debounce_ms: int = 1000):
        self._project_dir = project_dir.resolve()
        self._debounce_ms = debounce_ms
        self._running = True

    def _get_memory_dir(self) -> Path:
        """Get the memory directory (uses context/memory/ directly)."""
        return get_memory_dir()

    async def run(self) -> None:
        """Watch memory directory and index on changes."""
        memory_dir = self._get_memory_dir()

        if not memory_dir.exists():
            logger.info(f"Memory directory not found: {memory_dir}")
            # Wait for it to be created
            while self._running and not memory_dir.exists():
                await asyncio.sleep(5)
            if not self._running:
                return

        logger.info(f"Memory watcher started: {memory_dir}")

        try:
            from watchfiles import awatch, Change

            async for changes in awatch(
                memory_dir,
                debounce=self._debounce_ms,
                stop_event=self._stop_event,
            ):
                if not self._running:
                    break

                # Filter to only .md file changes
                md_changes = [
                    (change, path) for change, path in changes
                    if path.endswith(".md")
                ]

                if md_changes:
                    logger.info(f"Memory files changed: {len(md_changes)} file(s)")
                    if await _run_index_script(self._project_dir, "--memory-only"):
                        logger.info("Memory indexed successfully")

        except ImportError:
            logger.error("watchfiles not installed, memory watcher disabled")
        except Exception as e:
            if self._running:
                logger.error(f"Memory watcher error: {e}")

    @property
    def _stop_event(self) -> asyncio.Event:
        """Create a stop event that's set when _running is False."""
        if not hasattr(self, "_event"):
            self._event = asyncio.Event()
        return self._event

    def stop(self) -> None:
        """Signal the watcher to stop."""
        self._running = False
        if hasattr(self, "_event"):
            self._event.set()
        logger.info("Memory watcher stopping")


class HistoryIndexer:
    """Background task that periodically indexes session history.

    Only re-indexes when session files have changed since the last run.
    """

    def __init__(self, project_dir: Path, interval_seconds: int = 120):
        self._project_dir = project_dir.resolve()
        self._interval = interval_seconds
        self._running = True
        self._last_hash: str | None = None

    def _get_sessions_dir(self) -> Path:
        """Get the sessions directory (uses context/ directly)."""
        return get_sessions_dir()

    def _compute_sessions_hash(self) -> str:
        """Compute a hash of all session file mtimes and sizes."""
        sessions_dir = self._get_sessions_dir()
        if not sessions_dir.exists():
            return ""

        # Hash based on file names, sizes, and modification times
        entries = []
        for jsonl_path in sorted(sessions_dir.glob("*.jsonl")):
            try:
                stat = jsonl_path.stat()
                entries.append(f"{jsonl_path.name}:{stat.st_size}:{stat.st_mtime}")
            except OSError:
                continue

        return hashlib.md5("\n".join(entries).encode()).hexdigest()

    async def run(self) -> None:
        """Run the periodic indexing loop."""
        logger.info(f"History indexer started (interval: {self._interval}s)")

        while self._running:
            try:
                # Check if sessions have changed
                current_hash = self._compute_sessions_hash()

                if current_hash and current_hash != self._last_hash:
                    logger.info("Session files changed, re-indexing...")
                    if await _run_index_script(self._project_dir, "--history-only"):
                        self._last_hash = current_hash
                        logger.info("History indexed successfully")
                else:
                    logger.debug("No session changes, skipping index")

            except Exception as e:
                logger.error(f"Indexer error: {e}")

            # Wait for next interval
            await asyncio.sleep(self._interval)

    def stop(self) -> None:
        """Signal the indexer to stop."""
        self._running = False
        logger.info("History indexer stopping")
