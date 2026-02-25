#!/usr/bin/env python3
"""
Usage: context/scripts/search.py <query> [options]
Description: Search the vector index for relevant chunks.

Options:
    --collection NAME    Collection to search (default: memory)
    --n N                Number of results (default: 5)
    --threshold FLOAT    Max distance threshold (default: 1.5)
    --file PATTERN       Filter by file path (substring match)
    --json               Output as JSON for programmatic use

Examples:
    context/scripts/search.py "architecture decisions"
    context/scripts/search.py "how to create skills" --n 10
    context/scripts/search.py "embedding pipeline" --collection history
    context/scripts/search.py "session management" --file memory/ --json
"""
import argparse
import json
import sys
from pathlib import Path

# Add project root to path for utils import
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))

from utils.paths import get_index_dir

INDEX_DIR = get_index_dir() / "chroma"


def search(query, collection_name="memory", n_results=5, threshold=1.5, file_filter=None):
    """Search the vector index and return results with metadata."""
    import chromadb
    from sentence_transformers import SentenceTransformer

    if not INDEX_DIR.exists():
        print("Error: No index found. Run 'context/scripts/embed.py index <path>' first.", file=sys.stderr)
        sys.exit(1)

    client = chromadb.PersistentClient(path=str(INDEX_DIR))

    try:
        collection = client.get_collection(collection_name)
    except Exception:
        print(f"Error: Collection '{collection_name}' not found.", file=sys.stderr)
        sys.exit(1)

    if collection.count() == 0:
        print(f"Collection '{collection_name}' is empty.", file=sys.stderr)
        sys.exit(1)

    model = SentenceTransformer("all-MiniLM-L6-v2")
    query_embedding = model.encode([query])[0].tolist()

    # Build query kwargs
    query_kwargs = {
        "query_embeddings": [query_embedding],
        "n_results": min(n_results, collection.count()),
    }

    results = collection.query(**query_kwargs)

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

    return formatted


def print_results(results, as_json=False):
    """Display search results."""
    if not results:
        print("No results found.")
        return

    if as_json:
        print(json.dumps(results, indent=2))
        return

    for i, r in enumerate(results, 1):
        print(f"--- Result {i} (distance: {r['distance']}) ---")
        print(f"File: {r['file_path']}:{r['start_line']}-{r['end_line']}")
        print(r["text"])
        print()


def main():
    parser = argparse.ArgumentParser(description="Search the vector index")
    parser.add_argument("query", nargs="+", help="Search query")
    parser.add_argument("--collection", default="memory", help="Collection name (default: memory)")
    parser.add_argument("--n", type=int, default=5, help="Number of results (default: 5)")
    parser.add_argument("--threshold", type=float, default=1.5, help="Max distance (default: 1.5)")
    parser.add_argument("--file", default=None, help="Filter by file path substring")
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()
    query_text = " ".join(args.query)

    results = search(
        query_text,
        collection_name=args.collection,
        n_results=args.n,
        threshold=args.threshold,
        file_filter=args.file,
    )

    print_results(results, as_json=args.json)


if __name__ == "__main__":
    main()
