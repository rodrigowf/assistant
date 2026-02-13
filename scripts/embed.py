#!/usr/bin/env python3
"""
Usage: scripts/embed.py <command> [options]
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
    scripts/embed.py index memory/
    scripts/embed.py index history/2026-02-05-session.md --collection history
    scripts/embed.py stats
    scripts/embed.py reset --collection history
"""
import argparse
import hashlib
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_DIR = SCRIPT_DIR.parent
INDEX_DIR = PROJECT_DIR / "index" / "chroma"

# Lazy imports to keep startup fast when just checking --help
_model = None
_clients = {}


def get_client():
    import chromadb
    if "default" not in _clients:
        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        _clients["default"] = chromadb.PersistentClient(path=str(INDEX_DIR))
    return _clients["default"]


def get_collection(name="memory"):
    client = get_client()
    return client.get_or_create_collection(
        name=name,
        metadata={"hnsw:space": "cosine"},
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
    except (UnicodeDecodeError, PermissionError) as e:
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

        if chunk_text:  # Skip empty chunks
            # Stable ID based on file path + line range
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


def index_path(path, collection_name="memory", chunk_size=10, overlap=3):
    """Index a file or directory."""
    path = Path(path)
    collection = get_collection(collection_name)
    model = get_model()

    # Gather files
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

    total_chunks = 0
    for filepath in files:
        # Remove old chunks for this file before re-indexing
        _delete_file_chunks(collection, str(filepath))

        chunks = chunk_file(filepath, chunk_size, overlap)
        if not chunks:
            continue

        # Batch embed and store
        texts = [c["text"] for c in chunks]
        embeddings = model.encode(texts, show_progress_bar=False)

        collection.add(
            ids=[c["id"] for c in chunks],
            embeddings=embeddings.tolist(),
            documents=texts,
            metadatas=[c["metadata"] for c in chunks],
        )

        total_chunks += len(chunks)
        print(f"  Indexed {filepath} ({len(chunks)} chunks)")

    print(f"\nTotal: {len(files)} files, {total_chunks} chunks in '{collection_name}'")


def _delete_file_chunks(collection, file_path):
    """Remove all chunks belonging to a file path."""
    try:
        results = collection.get(where={"file_path": file_path})
        if results["ids"]:
            collection.delete(ids=results["ids"])
    except Exception:
        pass  # Collection might be empty


def delete_path(path, collection_name="memory"):
    """Delete all chunks from a file path."""
    collection = get_collection(collection_name)
    path = Path(path)

    if path.is_dir():
        # Delete all files under this directory
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
        _delete_file_chunks(collection, str(path))
        print(f"Deleted chunks for {path}")


def reset_collection(collection_name="memory"):
    """Clear all data from a collection."""
    client = get_client()
    try:
        client.delete_collection(collection_name)
        print(f"Collection '{collection_name}' reset")
    except Exception:
        print(f"Collection '{collection_name}' doesn't exist or already empty")


def show_stats(collection_name="memory"):
    """Show collection statistics."""
    collection = get_collection(collection_name)
    count = collection.count()

    if count == 0:
        print(f"Collection '{collection_name}': empty")
        return

    results = collection.get()
    files = set()
    for meta in results["metadatas"]:
        files.add(meta.get("file_path", "unknown"))

    print(f"Collection '{collection_name}':")
    print(f"  Total chunks: {count}")
    print(f"  Files indexed: {len(files)}")
    for f in sorted(files):
        file_chunks = sum(1 for m in results["metadatas"] if m.get("file_path") == f)
        print(f"    {f} ({file_chunks} chunks)")


def main():
    parser = argparse.ArgumentParser(description="Embedding pipeline for memory and history")
    sub = parser.add_subparsers(dest="command")

    # index
    p_index = sub.add_parser("index", help="Index a file or directory")
    p_index.add_argument("path", help="File or directory to index")
    p_index.add_argument("--collection", default="memory", help="Collection name (default: memory)")
    p_index.add_argument("--chunk-size", type=int, default=10, help="Lines per chunk (default: 10)")
    p_index.add_argument("--overlap", type=int, default=3, help="Overlap lines between chunks (default: 3)")

    # delete
    p_delete = sub.add_parser("delete", help="Delete chunks for a file/directory")
    p_delete.add_argument("path", help="File or directory path")
    p_delete.add_argument("--collection", default="memory", help="Collection name")

    # reset
    p_reset = sub.add_parser("reset", help="Clear a collection")
    p_reset.add_argument("--collection", default="memory", help="Collection name")

    # stats
    p_stats = sub.add_parser("stats", help="Show collection statistics")
    p_stats.add_argument("--collection", default="memory", help="Collection name")

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
