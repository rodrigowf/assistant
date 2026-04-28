#!/usr/bin/env python3
"""
Persistent search server — loads the embedding model once, accepts queries over stdin.

Protocol (JSON-line, one JSON object per line):

  Input:  {"query": "...", "collection": "memory", "n_results": 5, "threshold": 1.5}
  Output: {"results": [...], "error": null}

  Input:  {"command": "ping"}
  Output: {"status": "ready"}

  Input:  {"command": "shutdown"}
  → process exits cleanly

The model is loaded once on startup. Subsequent queries reuse it, avoiding the
~60-70 second cold-start penalty on ARM devices (Jetson Nano).
"""
import json
import sys
from pathlib import Path

# Add project root to path for utils import
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))

from utils.paths import get_index_dir

INDEX_DIR = get_index_dir() / "chroma"


def send_response(data: dict) -> None:
    """Write a JSON response line to stdout and flush."""
    sys.stdout.write(json.dumps(data) + "\n")
    sys.stdout.flush()


def send_error(message: str) -> None:
    """Write an error response."""
    send_response({"results": [], "error": message})


def main() -> None:
    # --- Eager model + client loading (the expensive part) ---
    import chromadb
    from sentence_transformers import SentenceTransformer

    if not INDEX_DIR.exists():
        send_error("No index found. Run index-memory.py first.")
        sys.exit(1)

    client = chromadb.PersistentClient(path=str(INDEX_DIR))
    model = SentenceTransformer("all-MiniLM-L6-v2")

    # Signal readiness
    send_response({"status": "ready"})

    # --- Query loop ---
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            send_error(f"Invalid JSON: {line[:200]}")
            continue

        # Handle commands
        if "command" in request:
            cmd = request["command"]
            if cmd == "ping":
                send_response({"status": "ready"})
            elif cmd == "shutdown":
                break
            else:
                send_error(f"Unknown command: {cmd}")
            continue

        # Handle search queries
        query = request.get("query", "")
        collection_name = request.get("collection", "memory")
        n_results = request.get("n_results", 5)
        threshold = request.get("threshold", 1.5)
        file_filter = request.get("file_filter")

        if not query:
            send_error("Missing 'query' field")
            continue

        try:
            collection = client.get_collection(collection_name)
        except Exception:
            send_error(f"Collection '{collection_name}' not found.")
            continue

        count = collection.count()
        if count == 0:
            send_error(f"Collection '{collection_name}' is empty.")
            continue

        # Encode and search
        query_embedding = model.encode([query])[0].tolist()
        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=min(n_results, count),
        )

        # Format results with post-query filtering
        formatted = []
        for i, doc in enumerate(results["documents"][0]):
            meta = results["metadatas"][0][i]
            distance = results["distances"][0][i]

            if distance > threshold:
                continue
            if file_filter and file_filter not in meta.get("file_path", ""):
                continue

            formatted.append({
                "text": doc,
                "file_path": meta["file_path"],
                "start_line": int(meta["start_line"]),
                "end_line": int(meta["end_line"]),
                "file_name": meta.get("file_name", ""),
                "distance": round(distance, 4),
            })

        send_response({"results": formatted, "error": None})


if __name__ == "__main__":
    main()
