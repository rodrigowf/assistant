"""Search tools — semantic search over history and memory.

All ChromaDB access runs in a subprocess to:
1. Prevent segfaults in ChromaDB's native code from crashing the server
2. Avoid concurrent multi-process access to the same ChromaDB index
   (the background indexer also accesses it via subprocess)
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from orchestrator.tools import registry

logger = logging.getLogger(__name__)

# Path to the search script
_SEARCH_SCRIPT = Path(__file__).resolve().parent.parent.parent / "default-scripts" / "search.py"
_RUN_SH = Path(__file__).resolve().parent.parent.parent / "context" / "scripts" / "run.sh"


async def _do_search_subprocess(
    query: str,
    collection_name: str,
    max_results: int,
) -> list[dict[str, Any]]:
    """Run semantic search in a subprocess via search.py.

    This ensures ChromaDB is never opened in the server process,
    preventing index corruption from concurrent access and protecting
    against segfaults in ChromaDB's native code.
    """
    args = [
        str(_RUN_SH), str(_SEARCH_SCRIPT),
        query,
        "--collection", collection_name,
        "--n", str(max_results),
        "--json",
    ]

    logger.info("Searching '%s' for: %s", collection_name, query)

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=60,
        )
    except asyncio.TimeoutError:
        logger.error("Search subprocess timed out for query: %s", query)
        proc.kill()
        return [{"error": "Search timed out"}]

    if proc.returncode != 0:
        stderr_text = stderr.decode().strip()
        if proc.returncode < 0:
            logger.error(
                "Search subprocess crashed (signal %d) for '%s': %s",
                -proc.returncode, collection_name, stderr_text,
            )
            return [{"error": f"Search crashed (signal {-proc.returncode})"}]
        else:
            # Non-zero exit could mean empty collection or missing index
            logger.warning("Search returned exit %d: %s", proc.returncode, stderr_text)
            return []

    # Parse JSON output
    stdout_text = stdout.decode().strip()
    if not stdout_text or stdout_text == "No results found.":
        return []

    try:
        results = json.loads(stdout_text)
    except json.JSONDecodeError:
        logger.error("Failed to parse search output: %s", stdout_text[:200])
        return []

    logger.info("Search returned %d results.", len(results))
    return results


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
    results = await _do_search_subprocess(query, "history", max_results)
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
    results = await _do_search_subprocess(query, "memory", max_results)
    return json.dumps({"query": query, "results": results, "count": len(results)})
