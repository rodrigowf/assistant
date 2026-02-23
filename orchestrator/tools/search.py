"""Search tools — semantic search over history and memory."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from orchestrator.tools import registry

logger = logging.getLogger(__name__)


def _do_search(
    query: str,
    collection_name: str,
    max_results: int,
    index_dir: str,
) -> list[dict[str, Any]]:
    """Run a semantic search against a ChromaDB collection."""
    import chromadb
    from sentence_transformers import SentenceTransformer

    try:
        client = chromadb.PersistentClient(path=index_dir)
        collection = client.get_collection(collection_name)
    except Exception as e:
        return [{"error": f"Collection '{collection_name}' not available: {e}"}]

    if collection.count() == 0:
        return []

    model = SentenceTransformer("all-MiniLM-L6-v2")
    query_embedding = model.encode([query])[0].tolist()

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=min(max_results, collection.count()),
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
