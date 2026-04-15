#!/usr/bin/env python3
"""
Clean up the history index, keeping only specified sessions.

Usage:
    context/scripts/cleanup-history-index.py <session_id1> <session_id2> ...

This script will:
1. Get all chunks from the 'history' collection
2. Delete all chunks EXCEPT those belonging to the specified session IDs
3. Show statistics before and after cleanup
"""
import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
INDEX_DIR = PROJECT_DIR / "index" / "chroma"


def get_client():
    import chromadb
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(INDEX_DIR))


def get_collection(name="history"):
    client = get_client()
    return client.get_or_create_collection(
        name=name,
        metadata={"hnsw:space": "cosine"},
    )


def cleanup_history(keep_sessions, auto_confirm=False):
    """Remove all sessions from history index except the ones to keep."""
    collection = get_collection("history")

    # Get all chunks
    print("Fetching all chunks from history collection...")
    results = collection.get()

    if not results["ids"]:
        print("History collection is empty!")
        return

    # Group chunks by session ID (extracted from file_path)
    session_chunks = {}
    for chunk_id, metadata in zip(results["ids"], results["metadatas"]):
        file_path = metadata.get("file_path", "")
        # Extract session ID from paths like:
        # /home/rodrigo/Projects/assistant/.index-temp/SESSION_ID.md
        # or /home/rodrigo/Projects/assistant/history/...

        session_id = None
        if ".index-temp" in file_path:
            # Extract from .index-temp/SESSION_ID.md
            filename = Path(file_path).stem  # Get filename without extension
            session_id = filename
        elif "/history/" in file_path:
            # Manual history files - keep these separate
            session_id = f"manual:{Path(file_path).name}"

        if session_id:
            if session_id not in session_chunks:
                session_chunks[session_id] = []
            session_chunks[session_id].append(chunk_id)

    # Show stats
    print(f"\nFound {len(session_chunks)} unique sessions in the index:")
    for session_id, chunks in sorted(session_chunks.items(), key=lambda x: len(x[1]), reverse=True):
        marker = " [KEEP]" if session_id in keep_sessions else ""
        print(f"  {session_id}: {len(chunks)} chunks{marker}")

    # Determine which chunks to delete
    chunks_to_delete = []
    sessions_to_delete = []

    for session_id, chunk_ids in session_chunks.items():
        if session_id not in keep_sessions:
            chunks_to_delete.extend(chunk_ids)
            sessions_to_delete.append(session_id)

    if not chunks_to_delete:
        print("\nNo chunks to delete - all sessions are being kept!")
        return

    # Confirm deletion
    print(f"\n=== DELETION PLAN ===")
    print(f"Sessions to DELETE: {len(sessions_to_delete)}")
    print(f"Sessions to KEEP: {len(keep_sessions)}")
    print(f"Total chunks to DELETE: {len(chunks_to_delete)}")
    print(f"Total chunks to KEEP: {len(results['ids']) - len(chunks_to_delete)}")

    if not auto_confirm:
        response = input("\nProceed with deletion? (yes/no): ")
        if response.lower() != "yes":
            print("Aborted.")
            return
    else:
        print("\n[Auto-confirmed - proceeding with deletion]")

    # Delete chunks
    print(f"\nDeleting {len(chunks_to_delete)} chunks...")
    collection.delete(ids=chunks_to_delete)

    print("✓ Cleanup complete!")

    # Show final stats
    final_results = collection.get()
    print(f"\n=== FINAL STATS ===")
    print(f"Remaining chunks: {len(final_results['ids'])}")
    print(f"Remaining sessions: {len(keep_sessions)}")


def main():
    parser = argparse.ArgumentParser(
        description="Clean up history index, keeping only specified sessions"
    )
    parser.add_argument(
        "sessions",
        nargs="+",
        help="Session IDs to keep (all others will be deleted)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without actually deleting"
    )
    parser.add_argument(
        "-y", "--yes",
        action="store_true",
        help="Auto-confirm deletion without prompting"
    )

    args = parser.parse_args()

    print(f"Sessions to KEEP: {', '.join(args.sessions)}")
    print(f"All other sessions will be DELETED from the history index.\n")

    if args.dry_run:
        print("[DRY RUN MODE - no changes will be made]\n")

    cleanup_history(set(args.sessions), auto_confirm=args.yes)


if __name__ == "__main__":
    main()
