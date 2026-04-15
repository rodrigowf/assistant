"""Utilities for managing the vector index alongside session operations."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def get_index_dir() -> Path:
    """Get the ChromaDB index directory."""
    script_dir = Path(__file__).parent.resolve()
    project_dir = script_dir.parent
    return project_dir / "index" / "chroma"


def get_chroma_client():
    """Get a ChromaDB client instance."""
    try:
        import chromadb
        index_dir = get_index_dir()
        index_dir.mkdir(parents=True, exist_ok=True)
        return chromadb.PersistentClient(path=str(index_dir))
    except ImportError:
        return None


def get_collection(name: str = "history"):
    """Get or create a ChromaDB collection."""
    client = get_chroma_client()
    if client is None:
        return None
    return client.get_or_create_collection(
        name=name,
        metadata={"hnsw:space": "cosine"},
    )


def remove_session_from_index(session_id: str, collection_name: str = "history") -> bool:
    """Remove all chunks for a session from the vector index.

    Runs in a subprocess to protect the main server process from segfaults
    in ChromaDB's native code (e.g. corrupted HNSW index).

    Args:
        session_id: The session ID to remove
        collection_name: The collection name (default: "history")

    Returns:
        True if chunks were deleted, False otherwise
    """
    index_dir = str(get_index_dir())

    # Run in subprocess so a ChromaDB segfault can't crash the server
    script = f"""
import sys
import chromadb

try:
    client = chromadb.PersistentClient(path={index_dir!r})
    collection = client.get_collection({collection_name!r})
except Exception as e:
    print(f"Collection not available: {{e}}", file=sys.stderr)
    sys.exit(1)

try:
    results = collection.get(where={{"file_path": {{"$contains": "{session_id}.md"}}}})
except Exception:
    # Fallback: fetch all and filter (older ChromaDB versions)
    results = collection.get()

if not results["ids"]:
    sys.exit(0)

# Filter to matching chunks
to_delete = []
for chunk_id, metadata in zip(results["ids"], results["metadatas"]):
    fp = metadata.get("file_path", "")
    if "/{session_id}.md" in fp or ".index-temp/{session_id}.md" in fp:
        to_delete.append(chunk_id)

if to_delete:
    collection.delete(ids=to_delete)
    print(f"Deleted {{len(to_delete)}} chunks")
    sys.exit(0)
else:
    sys.exit(0)
"""

    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
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
