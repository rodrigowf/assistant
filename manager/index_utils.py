"""Utilities for managing the vector index alongside session operations."""

from __future__ import annotations

import os
from pathlib import Path


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

    Args:
        session_id: The session ID to remove
        collection_name: The collection name (default: "history")

    Returns:
        True if chunks were deleted, False otherwise
    """
    collection = get_collection(collection_name)
    if collection is None:
        # ChromaDB not available, skip silently
        return False

    try:
        # Query for all chunks belonging to this session
        # The file_path in metadata looks like: .index-temp/SESSION_ID.md
        results = collection.get()

        if not results["ids"]:
            return False

        # Find chunks matching this session ID
        chunks_to_delete = []
        for chunk_id, metadata in zip(results["ids"], results["metadatas"]):
            file_path = metadata.get("file_path", "")
            # Check if this chunk belongs to the session
            # Path format: /path/to/.index-temp/SESSION_ID.md
            if f"/{session_id}.md" in file_path or f".index-temp/{session_id}.md" in file_path:
                chunks_to_delete.append(chunk_id)

        if chunks_to_delete:
            collection.delete(ids=chunks_to_delete)
            return True

        return False

    except Exception as e:
        # Silently fail - don't break session deletion if index cleanup fails
        import sys
        print(f"Warning: Failed to remove session {session_id} from index: {e}", file=sys.stderr)
        return False
