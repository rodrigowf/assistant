#!/usr/bin/env python3
"""
Usage: context/scripts/embed.py <command> [options]
Description: Core embedding pipeline — chunk files and store in ChromaDB.

Commands:
    index <path> [--collection NAME] [--chunk-size N] [--overlap N]
        Index a file or directory into the vector store.

    delete <path> [--collection NAME]
        Remove all chunks from a specific file path.

    reset [--collection NAME]
        Clear all data from a collection.

    stats [--collection NAME]
        Show collection statistics.

Examples:
    context/scripts/embed.py index context/memory/
    context/scripts/embed.py index history/2026-02-05-session.md --collection history
    context/scripts/embed.py stats
    context/scripts/embed.py reset --collection history

Implementation notes
--------------------
When a warm search-server is running (its lockfile is held and its
Unix domain socket is reachable), all chroma access goes through the
server. Otherwise we open chroma directly. The IndexFacade helper
enforces this — including a refusal to open chroma directly if a warm
server's lockfile is present (which would risk concurrent-writer
corruption).
"""
import argparse
import hashlib
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(SCRIPT_DIR))  # for index_client

INDEX_DIR = PROJECT_DIR / "index" / "chroma"

# Lazy state for the direct-chroma path (kept so tests that monkey-patch
# `embed.INDEX_DIR` + clear `_clients` keep working).
_model = None
_clients = {}


def get_client():
    """Direct chroma client. Only used when no warm server is reachable.
    Tests use this directly via fixture."""
    import chromadb
    if "default" not in _clients:
        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        _clients["default"] = chromadb.PersistentClient(path=str(INDEX_DIR))
    return _clients["default"]


def get_collection(name="memory"):
    client = get_client()
    return client.get_or_create_collection(
        name=name,
        metadata={"hnsw:space": "cosine", "hnsw:sync_threshold": 200},
    )


def get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


def chunk_file(filepath, chunk_size=10, overlap=3):
    """Split a file into overlapping line-based chunks with metadata."""
    if overlap >= chunk_size:
        raise ValueError(f"overlap ({overlap}) must be less than chunk_size ({chunk_size})")

    filepath = Path(filepath)
    try:
        text = filepath.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError, FileNotFoundError) as e:
        print(f"  Skipping {filepath}: {e}", file=sys.stderr)
        return []

    lines = text.splitlines()
    if not lines:
        return []

    chunks = []
    i = 0
    while i < len(lines):
        end = min(i + chunk_size, len(lines))
        chunk_lines = lines[i:end]
        chunk_text = "\n".join(chunk_lines).strip()

        if chunk_text:
            chunk_id = hashlib.sha256(
                f"{filepath}:{i+1}-{end}".encode()
            ).hexdigest()[:16]
            chunks.append({
                "id": chunk_id,
                "text": chunk_text,
                "metadata": {
                    "file_path": str(filepath),
                    "start_line": i + 1,
                    "end_line": end,
                    "file_name": filepath.name,
                },
            })

        i += chunk_size - overlap
        if i >= len(lines):
            break

    return chunks


def _open_facade():
    """Return an IndexFacade pointed at INDEX_DIR.

    The facade uses the warm server if reachable, else direct chroma.
    If our module-level INDEX_DIR has been monkey-patched (tests), we
    override the facade's path to match so test isolation works."""
    # Import lazily so the module loads cheaply for --help.
    import index_client
    index_client.INDEX_DIR = INDEX_DIR
    index_client.SOCKET_PATH = INDEX_DIR / ".search-server.sock"
    index_client.LOCKFILE = INDEX_DIR / ".search-server.lock"
    return index_client.IndexFacade()


def index_path(path, collection_name="memory", chunk_size=10, overlap=3):
    """Index a file or directory."""
    path = Path(path)

    if path.is_file():
        files = [path]
    elif path.is_dir():
        files = sorted(
            f for f in path.rglob("*")
            if f.is_file()
            and f.suffix in (".md", ".txt", ".py", ".sh", ".yaml", ".yml", ".json")
            and ".git" not in f.parts
        )
    else:
        print(f"Error: {path} not found", file=sys.stderr)
        sys.exit(1)

    if not files:
        print(f"No indexable files found in {path}")
        return

    facade = _open_facade()
    print(f"[embed] mode={facade.mode}", file=sys.stderr)

    total_chunks = 0
    with facade:
        for filepath in files:
            # Remove old chunks for this file before re-indexing
            old_ids = facade.get_by_file(collection_name, str(filepath))
            if old_ids:
                facade.delete_ids(collection_name, old_ids)

            chunks = chunk_file(filepath, chunk_size, overlap)
            if not chunks:
                continue

            texts = [c["text"] for c in chunks]
            embeddings = facade.encode_many(texts)

            payload = [
                {
                    "id": c["id"],
                    "embedding": e,
                    "document": c["text"],
                    "metadata": c["metadata"],
                }
                for c, e in zip(chunks, embeddings)
            ]
            facade.add_chunks(collection_name, payload)

            total_chunks += len(chunks)
            print(f"  Indexed {filepath} ({len(chunks)} chunks)")

    print(f"\nTotal: {len(files)} files, {total_chunks} chunks in '{collection_name}'")


def _delete_file_chunks(collection, file_path):
    """Tests still call this with a direct chroma collection. Kept for
    test compatibility — production code goes through the facade."""
    try:
        results = collection.get(where={"file_path": file_path})
        if results["ids"]:
            collection.delete(ids=results["ids"])
    except Exception:
        pass


def delete_path(path, collection_name="memory"):
    path = Path(path)
    facade = _open_facade()
    with facade:
        if path.is_dir():
            # No direct "delete prefix" command; fetch all metadata and filter.
            # Round-trip is fine for the rare cleanup case.
            if facade._client:
                # When using the warm server we can't enumerate by prefix
                # cheaply; fall back to per-file deletes for each known file.
                # Caller can also pass an exact file path.
                print(
                    f"[embed] delete on directory via warm server is not supported; "
                    f"pass an exact file path or use 'reset' for a full clear.",
                    file=sys.stderr,
                )
                sys.exit(2)
            client = get_client()
            collection = client.get_or_create_collection(
                name=collection_name,
                metadata={"hnsw:space": "cosine", "hnsw:sync_threshold": 200},
            )
            results = collection.get()
            ids_to_delete = [
                id_ for id_, meta in zip(results["ids"], results["metadatas"])
                if meta.get("file_path", "").startswith(str(path))
            ]
            if ids_to_delete:
                collection.delete(ids=ids_to_delete)
                print(f"Deleted {len(ids_to_delete)} chunks from {path}")
            else:
                print(f"No chunks found for {path}")
        else:
            ids = facade.get_by_file(collection_name, str(path))
            facade.delete_ids(collection_name, ids)
            print(f"Deleted chunks for {path} ({len(ids)} chunks)")


def reset_collection(collection_name="memory"):
    facade = _open_facade()
    with facade:
        try:
            facade.reset_collection(collection_name)
            print(f"Collection '{collection_name}' reset")
        except Exception as e:
            print(f"Collection '{collection_name}' reset failed: {e}")


def show_stats(collection_name="memory"):
    facade = _open_facade()
    with facade:
        count = facade.count(collection_name)
        if count == 0:
            print(f"Collection '{collection_name}': empty")
            return
        print(f"Collection '{collection_name}':")
        print(f"  Total chunks: {count}")
        # File-level breakdown only available via direct chroma (we'd
        # need a new server command to enumerate; not critical for now).
        if not facade._client:
            client = get_client()
            collection = client.get_collection(collection_name)
            results = collection.get()
            files = set()
            for meta in results["metadatas"]:
                files.add(meta.get("file_path", "unknown"))
            print(f"  Files indexed: {len(files)}")
            for f in sorted(files):
                file_chunks = sum(1 for m in results["metadatas"] if m.get("file_path") == f)
                print(f"    {f} ({file_chunks} chunks)")


def main():
    parser = argparse.ArgumentParser(description="Embedding pipeline for memory and history")
    sub = parser.add_subparsers(dest="command")

    p_index = sub.add_parser("index", help="Index a file or directory")
    p_index.add_argument("path", help="File or directory to index")
    p_index.add_argument("--collection", default="memory")
    p_index.add_argument("--chunk-size", type=int, default=10)
    p_index.add_argument("--overlap", type=int, default=3)

    p_delete = sub.add_parser("delete", help="Delete chunks for a file/directory")
    p_delete.add_argument("path")
    p_delete.add_argument("--collection", default="memory")

    p_reset = sub.add_parser("reset", help="Clear a collection")
    p_reset.add_argument("--collection", default="memory")

    p_stats = sub.add_parser("stats", help="Show collection statistics")
    p_stats.add_argument("--collection", default="memory")

    args = parser.parse_args()

    if args.command == "index":
        index_path(args.path, args.collection, args.chunk_size, args.overlap)
    elif args.command == "delete":
        delete_path(args.path, args.collection)
    elif args.command == "reset":
        reset_collection(args.collection)
    elif args.command == "stats":
        show_stats(args.collection)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
