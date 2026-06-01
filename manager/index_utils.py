"""Utilities for managing the vector index alongside session operations.

Single-writer discipline: never open chromadb.PersistentClient directly
from this module. Instead, route every read/write through
default-scripts/index_client.IndexFacade, which talks to the warm
search-server when one is running, and falls back to a direct chroma
open only when the lockfile is unheld (so no other writer can race us).
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).parent.parent.resolve()
SCRIPTS_DIR = PROJECT_DIR / "default-scripts"


def get_index_dir() -> Path:
    """Path to the chroma index directory."""
    return PROJECT_DIR / "index" / "chroma"


def remove_session_from_index(
    session_id: str,
    collection_name: str = "history",
    timeout: float = 30.0,
) -> bool:
    """Remove all chunks for a session from the vector index.

    Runs in a subprocess so a chroma SIGSEGV is a recoverable exit
    code, not a crashed backend. Inside the subprocess we use the
    IndexFacade, which prefers the warm server's socket (single-writer
    safe). If the warm server has shut down (e.g. backend teardown),
    the facade refuses to open chroma directly while the lockfile is
    held — we treat that as a soft skip; the next HistoryIndexer tick
    will pick up the missing JSONL via its mtime hash and re-index from
    scratch (which removes the orphan).
    """
    script = f"""
import sys
sys.path.insert(0, {str(PROJECT_DIR)!r})
sys.path.insert(0, {str(SCRIPTS_DIR)!r})
import index_client

session_id = {session_id!r}
collection_name = {collection_name!r}

facade = index_client.IndexFacade()
with facade:
    try:
        # Match by file_path containing the session id. Sessions live
        # in .index-temp/<uuid>.md (the converted JSONL form).
        # Use delete_where for an exact-path match if known; otherwise
        # fall back to enumerating IDs by file_path metadata.
        candidates = [
            f"/home/rodrigo/assistant/.index-temp/{{session_id}}.md",
        ]
        total = 0
        for path in candidates:
            ids = facade.get_by_file(collection_name, path)
            if ids:
                total += facade.delete_ids(collection_name, ids)
        print(f"Deleted {{total}} chunks")
    except RuntimeError as e:
        # Lockfile-held-but-socket-unreachable case: skip cleanly.
        # The next indexer tick will re-derive the truth.
        print(f"SKIP: {{e}}", file=sys.stderr)
        sys.exit(0)
"""

    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if result.returncode < 0:
                logger.error(
                    "Index cleanup for session %s crashed (signal %d): %s",
                    session_id, -result.returncode, stderr,
                )
            else:
                logger.warning(
                    "Index cleanup for session %s failed (exit %d): %s",
                    session_id, result.returncode, stderr,
                )
            return False

        stdout = result.stdout.strip()
        if stdout:
            logger.info("Index cleanup for session %s: %s", session_id, stdout)
        return True

    except subprocess.TimeoutExpired:
        logger.warning("Index cleanup for session %s timed out", session_id)
        return False
    except Exception as e:
        logger.warning("Index cleanup for session %s error: %s", session_id, e)
        return False
