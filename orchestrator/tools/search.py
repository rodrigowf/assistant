"""Search tools — semantic search over history and memory."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from orchestrator.tools import registry

logger = logging.getLogger(__name__)

# Cache model at module level to avoid reloading on every search call
_model = None


def _get_model():
    """Lazy-load and cache the SentenceTransformer model."""
    global _model
    if _model is None:
        logger.info("Loading SentenceTransformer model (first search call)...")
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-MiniLM-L6-v2")
        logger.info("SentenceTransformer model loaded.")
    return _model


def _do_search(
    query: str,
    collection_name: str,
    max_results: int,
    index_dir: str,
) -> list[dict[str, Any]]:
    """Run a semantic search against a ChromaDB collection."""
    import chromadb

    try:
        client = chromadb.PersistentClient(path=index_dir)
        collection = client.get_collection(collection_name)
    except Exception as e:
        logger.error("Failed to open collection '%s' at %s: %s", collection_name, index_dir, e)
        raise RuntimeError(f"Collection '{collection_name}' not available: {e}") from e

    count = collection.count()
    if count == 0:
        logger.info("Collection '%s' is empty, returning no results.", collection_name)
        return []

    logger.info("Searching '%s' collection (%d chunks) for: %s", collection_name, count, query)
    model = _get_model()
    query_embedding = model.encode([query])[0].tolist()

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=min(max_results, count),
    )

    formatted = []
    for i, doc in enumerate(results["documents"][0]):
        meta = results["metadatas"][0][i]
        distance = results["distances"][0][i]
        if distance > 1.5:
            continue
        formatted.append({
            "text": doc,
            "file_path": meta.get("file_path", ""),
            "distance": round(distance, 4),
        })

    logger.info("Search returned %d results (filtered from %d).", len(formatted), len(results["documents"][0]))
    return formatted


@registry.register(
    name="search_history",
    description="Search conversation history using semantic search.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query.",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results (default: 5).",
            },
        },
        "required": ["query"],
    },
)
async def search_history(
    context: dict[str, Any], query: str, max_results: int = 5
) -> str:
    index_dir = context.get("index_dir", "")
    if not index_dir:
        return json.dumps({"error": "Index directory not configured"})

    loop = asyncio.get_running_loop()
    results = await loop.run_in_executor(
        None, _do_search, query, "history", max_results, index_dir
    )
    return json.dumps({"query": query, "results": results, "count": len(results)})


@registry.register(
    name="search_memory",
    description="Search memory files (MEMORY.md and related docs) using semantic search.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query.",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results (default: 5).",
            },
        },
        "required": ["query"],
    },
)
async def search_memory(
    context: dict[str, Any], query: str, max_results: int = 5
) -> str:
    index_dir = context.get("index_dir", "")
    if not index_dir:
        return json.dumps({"error": "Index directory not configured"})

    loop = asyncio.get_running_loop()
    results = await loop.run_in_executor(
        None, _do_search, query, "memory", max_results, index_dir
    )
    return json.dumps({"query": query, "results": results, "count": len(results)})
