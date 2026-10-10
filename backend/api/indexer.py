"""Background indexers for memory and history.

- MemoryWatcher: Re-indexes memory when the content watcher (api/content_watcher.py)
  reports markdown changes under the memory tree
- HistoryIndexer: Periodically indexes conversation history (every 5 min if any session
  JSONL of any harness changed — context/*.jsonl, context/chats/*.jsonl, Codex rollouts).
  The run is incremental (index/history.sqlite3 keeps per-session state), so
  a tick usually embeds only the messages added since the last one.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

from utils.paths import get_chats_dir, get_memory_dir, get_sessions_dir

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
        # Per-session failures are printed as "FAILED <file>: <error>"; keep the tail of both
        # streams so the actual cause reaches journald (stderr alone was just "[embed] mode=…").
        out = stdout.decode(errors="replace").strip()[-1500:]
        err = stderr.decode(errors="replace").strip()[-1500:]
        logger.error("Indexer %s failed (exit %s)\n--- stdout tail ---\n%s\n--- stderr tail ---\n%s",
                     " ".join(args), proc.returncode, out, err)
        return False

    return True


class MemoryWatcher:
    """Re-indexes memory when markdown under the memory tree changes.

    The file watching itself is :class:`api.content_watcher.ContentWatcher`'s
    (one ``watchfiles`` loop for the memory tree and ``context/public/``, which
    also pushes ``memory_changed`` to the clients); it calls :meth:`notify` for
    every batch with markdown changes. Runs are single-flight: changes that
    arrive while the index script runs trigger exactly one more run, so a burst
    never queues a run per batch and the watcher is never blocked by indexing.
    """

    def __init__(self, project_dir: Path):
        self._project_dir = project_dir.resolve()
        self._running = True
        # Eager Events so notify()/stop() before run() are race-free.
        self._stop_event = asyncio.Event()
        self._dirty = asyncio.Event()

    def _get_memory_dir(self) -> Path:
        """Get the memory directory (uses context/memory/ directly)."""
        return get_memory_dir()

    def notify(self) -> None:
        """Markdown changed: index once the current run (if any) finishes."""
        self._dirty.set()

    async def run(self) -> None:
        """Index after each :meth:`notify` until :meth:`stop`."""
        logger.info(f"Memory indexer started: {self._get_memory_dir()}")
        stop = asyncio.ensure_future(self._stop_event.wait())
        dirty: asyncio.Future | None = None
        try:
            while self._running:
                dirty = asyncio.ensure_future(self._dirty.wait())
                await asyncio.wait({dirty, stop}, return_when=asyncio.FIRST_COMPLETED)
                if not self._running or not self._dirty.is_set():
                    break
                self._dirty.clear()
                try:
                    if await _run_index_script(self._project_dir, "--memory-only"):
                        logger.info("Memory indexed successfully")
                except Exception as e:
                    logger.error(f"Memory indexer error: {e}")
        finally:
            stop.cancel()
            if dirty is not None:
                dirty.cancel()

    def stop(self) -> None:
        """Signal the indexer to stop."""
        self._running = False
        self._stop_event.set()
        logger.info("Memory indexer stopping")


class HistoryIndexer:
    """Background task that periodically indexes session history.

    Only re-indexes when session files have changed since the last run.
    """

    def __init__(self, project_dir: Path, interval_seconds: int = 300):
        self._project_dir = project_dir.resolve()
        self._interval = interval_seconds
        self._running = True
        self._last_hash: str | None = None

    def _get_sessions_dir(self) -> Path:
        """Get the sessions directory (uses context/ directly)."""
        return get_sessions_dir()

    def _compute_sessions_hash(self) -> str:
        """Compute a hash of all session file mtimes and sizes (every harness)."""
        sessions_dir = self._get_sessions_dir()
        if not sessions_dir.exists():
            return ""

        # Hash based on file names, sizes, and modification times
        paths = list(sessions_dir.glob("*.jsonl"))
        chats_dir = get_chats_dir()
        if chats_dir.is_dir():
            paths.extend(chats_dir.glob("*.jsonl"))
        # Codex rollouts (context/codex/sessions/YYYY/MM/DD/, or a home's
        # sessions/) — the same discovery the history index itself uses, so a
        # Codex-only change also triggers a run.
        from utils.history_index import _codex_sources

        paths.extend(_codex_sources())
        entries = []
        for jsonl_path in sorted(paths):
            try:
                stat = jsonl_path.stat()
                entries.append(f"{jsonl_path.parent.name}/{jsonl_path.name}:{stat.st_size}:{stat.st_mtime}")
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
