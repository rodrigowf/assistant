"""Search tools — semantic search over history and memory.

Uses a persistent search-server subprocess that loads the embedding model once
and accepts queries over stdin/stdout (JSON-line protocol). This avoids the
~60-70 second cold-start penalty on ARM devices (Jetson Nano) for every search.

Falls back to one-shot search.py if the warm server can't be started.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from orchestrator.tools import registry

logger = logging.getLogger(__name__)

# Paths
_PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
_SEARCH_SERVER = _PROJECT_DIR / "default-scripts" / "search-server.py"
_SEARCH_SCRIPT = _PROJECT_DIR / "default-scripts" / "search.py"
_RUN_SH = _PROJECT_DIR / "context" / "scripts" / "run.sh"

# Singleton warm server process
_server_proc: asyncio.subprocess.Process | None = None
_server_lock = asyncio.Lock()  # guards lifecycle (start/stop)
_query_lock = asyncio.Lock()   # serializes stdin/stdout pairing across concurrent queries
_server_ready = False


async def _ensure_server() -> asyncio.subprocess.Process | None:
    """Start the search server if not already running. Returns the process or None."""
    global _server_proc, _server_ready

    async with _server_lock:
        # Check if existing process is still alive
        if _server_proc is not None and _server_proc.returncode is None:
            return _server_proc

        # Need to (re)start
        _server_ready = False
        _server_proc = None

        logger.info("Starting search server subprocess...")
        try:
            proc = await asyncio.create_subprocess_exec(
                str(_RUN_SH), str(_SEARCH_SERVER),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as e:
            logger.error("Failed to start search server: %s", e)
            return None

        # Wait for the "ready" signal (model loaded)
        try:
            ready_line = await asyncio.wait_for(
                proc.stdout.readline(), timeout=180,  # Model load can be slow on Jetson
            )
            ready_data = json.loads(ready_line.decode().strip())
            if ready_data.get("status") == "ready":
                _server_proc = proc
                _server_ready = True
                logger.info("Search server ready (PID %d)", proc.pid)
                return proc
            else:
                logger.error("Unexpected ready response: %s", ready_data)
                proc.kill()
                return None
        except asyncio.TimeoutError:
            logger.error("Search server startup timed out (180s)")
            proc.kill()
            return None
        except Exception as e:
            logger.error("Search server startup error: %s", e)
            proc.kill()
            return None


async def _query_server(
    proc: asyncio.subprocess.Process,
    request: dict,
) -> dict | None:
    """Send a query to the warm server and read the response."""
    try:
        line = json.dumps(request) + "\n"
        proc.stdin.write(line.encode())
        await proc.stdin.drain()

        response_line = await asyncio.wait_for(
            proc.stdout.readline(), timeout=30,  # Warm queries should be fast
        )

        if not response_line:
            logger.warning("Search server returned empty response (process died?)")
            return None

        return json.loads(response_line.decode().strip())

    except asyncio.TimeoutError:
        logger.error("Search server query timed out (30s)")
        return None
    except Exception as e:
        logger.error("Search server query error: %s", e)
        return None


async def _do_search_cold(
    query: str,
    collection_name: str,
    max_results: int,
) -> list[dict[str, Any]]:
    """Fallback: run search.py as a one-shot subprocess (cold start)."""
    args = [
        str(_RUN_SH), str(_SEARCH_SCRIPT),
        query,
        "--collection", collection_name,
        "--n", str(max_results),
        "--json",
    ]

    logger.info("Cold search '%s' for: %s", collection_name, query)

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=120,
        )
    except asyncio.TimeoutError:
        logger.error("Cold search timed out for query: %s", query)
        proc.kill()
        return [{"error": "Search timed out"}]

    if proc.returncode != 0:
        stderr_text = stderr.decode().strip()
        # Tail of stderr carries the actual exception; the head is just the
        # traceback boilerplate ("Traceback (most recent call last)... in <module>").
        tail = stderr_text[-800:]
        print(
            f"[search cold-fallback FAILED] rc={proc.returncode} collection={collection_name} "
            f"query={query!r}\n--- stderr tail ---\n{tail}\n--- end ---",
            file=sys.stderr,
            flush=True,
        )
        if proc.returncode < 0:
            return [{"error": f"Search crashed (signal {-proc.returncode})"}]
        if "No index found" in stderr_text:
            return [{"error": "Index not found. Run index-memory.py to rebuild."}]
        elif "Collection" in stderr_text and "not found" in stderr_text:
            return [{"error": f"Collection '{collection_name}' not found."}]
        elif "empty" in stderr_text.lower():
            return [{"error": f"Collection '{collection_name}' is empty."}]
        else:
            return [{"error": f"Search failed: {tail}"}]

    stdout_text = stdout.decode().strip()
    if not stdout_text or stdout_text == "No results found.":
        return []

    try:
        return json.loads(stdout_text)
    except json.JSONDecodeError:
        return []


async def _do_search(
    query: str,
    collection_name: str,
    max_results: int,
) -> list[dict[str, Any]]:
    """Search using the warm server.

    Cold fallback only fires when the warm server can't be started AT ALL.
    Once the warm server is up, all queries go through it — chromadb's
    PersistentClient is not safe for concurrent multi-process access against
    the same path, so a cold subprocess opening the same index while the
    warm server holds it crashes with "Failed to apply logs to the hnsw
    segment writer".
    """
    global _server_proc, _server_ready

    logger.info("Searching '%s' for: %s", collection_name, query)

    request = {
        "query": query,
        "collection": collection_name,
        "n_results": max_results,
    }

    # Serialize concurrent queries — the warm server is single-threaded and
    # request/response pairing on its stdio is positional.
    async with _query_lock:
        # First attempt
        proc = await _ensure_server()
        if proc is not None:
            response = await _query_server(proc, request)
            if response is not None:
                error = response.get("error")
                if error:
                    msg = (
                        f"[search warm-server ERROR] collection={collection_name} "
                        f"query={query!r}: {error}"
                    )
                    logger.warning(msg)
                    print(msg, file=sys.stderr, flush=True)
                    return [{"error": error}]
                results = response.get("results", [])
                logger.info("Warm search returned %d results.", len(results))
                return results

            # Warm path failed — restart and retry once.
            rc = proc.returncode
            msg = (
                f"[search warm-server UNRESPONSIVE] pid={proc.pid} returncode={rc} "
                f"collection={collection_name} query={query!r} — restarting and retrying"
            )
            logger.warning(msg)
            print(msg, file=sys.stderr, flush=True)
            await _restart_server()

            proc = await _ensure_server()
            if proc is not None:
                response = await _query_server(proc, request)
                if response is not None:
                    error = response.get("error")
                    if error:
                        return [{"error": error}]
                    results = response.get("results", [])
                    logger.info("Warm search returned %d results (after restart).", len(results))
                    return results
                # Retry also failed — server keeps dying. Fall through.
                msg = (
                    f"[search warm-server FAILED AFTER RESTART] "
                    f"collection={collection_name} query={query!r}"
                )
                logger.error(msg)
                print(msg, file=sys.stderr, flush=True)

    # Cold fallback only runs when the warm server cannot be brought up.
    # This is mutually exclusive with a healthy warm server (so chromadb
    # multi-process access is not an issue here).
    results = await _do_search_cold(query, collection_name, max_results)
    logger.info("Cold search returned %d results.", len(results))
    return results


async def _restart_server() -> None:
    """Tear down the warm server so the next _ensure_server starts fresh."""
    global _server_proc, _server_ready
    async with _server_lock:
        proc = _server_proc
        _server_ready = False
        _server_proc = None
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (asyncio.TimeoutError, Exception):
                pass


async def shutdown_server() -> None:
    """Gracefully shut down the warm search server. Call during app teardown."""
    global _server_proc, _server_ready

    async with _server_lock:
        if _server_proc is not None and _server_proc.returncode is None:
            logger.info("Shutting down search server (PID %d)...", _server_proc.pid)
            try:
                _server_proc.stdin.write(json.dumps({"command": "shutdown"}).encode() + b"\n")
                await _server_proc.stdin.drain()
                await asyncio.wait_for(_server_proc.wait(), timeout=5)
            except Exception:
                _server_proc.kill()
            _server_proc = None
            _server_ready = False


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
    results = await _do_search(query, "history", max_results)
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
    results = await _do_search(query, "memory", max_results)
    return json.dumps({"query": query, "results": results, "count": len(results)})
