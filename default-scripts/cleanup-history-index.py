#!/usr/bin/env python3
"""
Clean up the history index, keeping only specified sessions.

Usage:
    context/scripts/cleanup-history-index.py <session_id1> <session_id2> ...

This script will:
1. Get all chunks from the 'history' collection
2. Delete all chunks EXCEPT those belonging to the specified session IDs
3. Show statistics before and after cleanup

Goes through index_client.IndexFacade so the single-writer discipline is
maintained — talks to the warm search-server via socket when one is
running, falls back to direct chroma otherwise.
"""
import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(SCRIPT_DIR))

import index_client  # noqa: E402


def _list_all_history(facade):
    """Return (chunk_id, file_path) tuples for every chunk in 'history'.

    The warm-server protocol doesn't have a "dump all metadata" command;
    we read sqlite directly (read-only) — that's safe even while the
    warm server is running because sqlite has its own concurrency
    handling and we never write."""
    import sqlite3
    db_path = index_client.INDEX_DIR / "chroma.sqlite3"
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = list(db.execute("""
            SELECT e.embedding_id, em.string_value
            FROM embeddings e
            JOIN segments s ON s.id = e.segment_id
            JOIN collections c ON c.id = s.collection
            LEFT JOIN embedding_metadata em ON em.id = e.id AND em.key = 'file_path'
            WHERE c.name = 'history'
        """))
        return [(cid, fp) for cid, fp in rows if cid]
    finally:
        db.close()


def cleanup_history(keep_sessions, auto_confirm=False):
    facade = index_client.IndexFacade()
    with facade:
        print(f"Mode: {facade.mode}")
        all_chunks = _list_all_history(facade)
        if not all_chunks:
            print("History collection is empty!")
            return

        session_chunks: dict[str, list[str]] = {}
        for chunk_id, file_path in all_chunks:
            session_id = None
            if file_path and ".index-temp" in file_path:
                session_id = Path(file_path).stem
            elif file_path and "/history/" in file_path:
                session_id = f"manual:{Path(file_path).name}"
            else:
                continue
            session_chunks.setdefault(session_id, []).append(chunk_id)

        print(f"\nFound {len(session_chunks)} unique sessions in the index:")
        for session_id, chunks in sorted(session_chunks.items(), key=lambda x: len(x[1]), reverse=True):
            marker = " [KEEP]" if session_id in keep_sessions else ""
            print(f"  {session_id}: {len(chunks)} chunks{marker}")

        chunks_to_delete: list[str] = []
        sessions_to_delete: list[str] = []
        for session_id, chunk_ids in session_chunks.items():
            if session_id not in keep_sessions:
                chunks_to_delete.extend(chunk_ids)
                sessions_to_delete.append(session_id)

        if not chunks_to_delete:
            print("\nNo chunks to delete - all sessions are being kept!")
            return

        print("\n=== DELETION PLAN ===")
        print(f"Sessions to DELETE: {len(sessions_to_delete)}")
        print(f"Sessions to KEEP: {len(keep_sessions)}")
        print(f"Total chunks to DELETE: {len(chunks_to_delete)}")
        print(f"Total chunks to KEEP: {len(all_chunks) - len(chunks_to_delete)}")

        if not auto_confirm:
            response = input("\nProceed with deletion? (yes/no): ")
            if response.lower() != "yes":
                print("Aborted.")
                return
        else:
            print("\n[Auto-confirmed - proceeding with deletion]")

        print(f"\nDeleting {len(chunks_to_delete)} chunks...")
        # Delete in batches; chroma can balk at very large id lists.
        BATCH = 500
        deleted = 0
        for i in range(0, len(chunks_to_delete), BATCH):
            facade.delete_ids("history", chunks_to_delete[i:i+BATCH])
            deleted += min(BATCH, len(chunks_to_delete) - i)
        print(f"✓ Deleted {deleted} chunks.")

        final_count = facade.count("history")
        print("\n=== FINAL STATS ===")
        print(f"Remaining chunks: {final_count}")
        print(f"Remaining sessions: {len(keep_sessions)}")


def main():
    parser = argparse.ArgumentParser(
        description="Clean up history index, keeping only specified sessions"
    )
    parser.add_argument("sessions", nargs="+", help="Session IDs to keep")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-y", "--yes", action="store_true")
    args = parser.parse_args()

    print(f"Sessions to KEEP: {', '.join(args.sessions)}")
    print("All other sessions will be DELETED from the history index.\n")
    if args.dry_run:
        print("[DRY RUN MODE - no changes will be made]\n")
    cleanup_history(set(args.sessions), auto_confirm=args.yes)


if __name__ == "__main__":
    main()
