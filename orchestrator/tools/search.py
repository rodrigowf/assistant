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
import re
import sys
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from orchestrator.tools import registry

logger = logging.getLogger(__name__)

# Frontmatter parser — simple YAML subset (no nested structures except a list
# under `references:`). Keeps the dependency surface zero.
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _parse_frontmatter(text: str) -> dict[str, Any] | None:
    """Extract YAML frontmatter from the head of a markdown file.

    Returns a dict of {key: value}, with `references` as a list and `tags` as a
    list when bracketed-inline (e.g. `tags: [a, b]`). Returns None when there
    is no frontmatter or it can't be parsed.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    block = m.group(1)
    out: dict[str, Any] = {}
    current_key: str | None = None
    for raw in block.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        # List continuation: "  - value"
        if line.lstrip().startswith("- ") and current_key is not None:
            existing = out.get(current_key)
            if not isinstance(existing, list):
                out[current_key] = []
            out[current_key].append(line.lstrip()[2:].strip())
            continue
        # Key: value
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if value == "":
                # Block-style list begins on next line
                current_key = key
                out[key] = []
            elif value.startswith("[") and value.endswith("]"):
                # Inline list: [a, b, c]
                items = [v.strip() for v in value[1:-1].split(",")]
                out[key] = [v for v in items if v]
                current_key = None
            else:
                out[key] = value
                current_key = None
    return out


@lru_cache(maxsize=128)
def _read_frontmatter_cached(file_path: str, mtime_ns: int) -> dict[str, Any] | None:
    """Cache-keyed read of frontmatter. mtime_ns invalidates on edits."""
    try:
        text = Path(file_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return _parse_frontmatter(text)


def _frontmatter_for(file_path: str) -> dict[str, Any] | None:
    """Read frontmatter for a file, using mtime-keyed cache."""
    try:
        mtime_ns = Path(file_path).stat().st_mtime_ns
    except OSError:
        return None
    return _read_frontmatter_cached(file_path, mtime_ns)


def _enrich_memory_results(results: list[dict[str, Any]]) -> None:
    """In-place: attach `frontmatter` to each memory search hit.

    Each result chunk gets a `frontmatter` field with the parsed YAML
    metadata block from the head of its source file (name, category,
    tags, created, modified, summary, source, references). Missing
    frontmatter yields `null` — common only for MEMORY.md and
    ORCHESTRATOR_MEMORY*.md which intentionally skip it.
    """
    for r in results:
        fp = r.get("file_path")
        if not fp:
            r["frontmatter"] = None
            continue
        r["frontmatter"] = _frontmatter_for(fp)


# ── History enrichment ──────────────────────────────────────────────────────
# Sessions live under context/<uuid>.jsonl and context/chats/<uuid>.jsonl.
# .titles.json maps UUID → human title (some entries missing).
# Linked memories: scan a sample of the JSONL for memory-path mentions.

_CONTEXT_DIR = _PROJECT_DIR / "context"
_TITLES_PATH = _CONTEXT_DIR / ".titles.json"

# Pattern: memory/<category>/.../*.md OR memory/<file>.md (for root files)
_MEMORY_PATH_RE = re.compile(r"memory/[A-Za-z0-9_/-]+\.md")


@lru_cache(maxsize=1)
def _load_titles_cached(mtime_ns: int) -> dict[str, str]:
    try:
        text = _TITLES_PATH.read_text(encoding="utf-8")
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _load_titles() -> dict[str, str]:
    try:
        mtime_ns = _TITLES_PATH.stat().st_mtime_ns
    except OSError:
        return {}
    return _load_titles_cached(mtime_ns)


def _session_uuid_from_path(file_path: str) -> str | None:
    """Extract the session UUID from a JSONL file_path or its temp .md form."""
    stem = Path(file_path).stem
    # UUID pattern: 8-4-4-4-12 hex
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", stem):
        return stem
    return None


def _session_jsonl_path(session_uuid: str) -> Path | None:
    """Find the actual JSONL file for a session UUID."""
    candidates = [
        _CONTEXT_DIR / f"{session_uuid}.jsonl",
        _CONTEXT_DIR / "chats" / f"{session_uuid}.jsonl",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _first_message_datetime(jsonl_path: Path) -> str | None:
    """Read the first user/assistant message and extract its timestamp.

    JSONL line shapes vary by harness — try `timestamp`, `created_at`,
    `created`. Returns ISO-8601 string or None.
    """
    try:
        with jsonl_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for key in ("timestamp", "created_at", "created"):
                    val = obj.get(key)
                    if isinstance(val, str) and val:
                        return val
                    if isinstance(val, (int, float)):
                        return datetime.utcfromtimestamp(val).isoformat() + "Z"
                # Only need one line that has content
                if obj.get("type") in ("user", "assistant"):
                    break
    except OSError:
        return None
    return None


@lru_cache(maxsize=64)
def _linked_memories_cached(session_uuid: str, mtime_ns: int) -> list[str]:
    """Scan a JSONL for unique memory-file paths it mentions."""
    jsonl_path = _session_jsonl_path(session_uuid)
    if jsonl_path is None:
        return []
    try:
        text = jsonl_path.read_text(encoding="utf-8")
    except OSError:
        return []
    found = set(_MEMORY_PATH_RE.findall(text))
    # Trim to canonical relative paths (sometimes preceded by `context/`).
    return sorted(found)


def _linked_memories(session_uuid: str) -> list[str]:
    jsonl_path = _session_jsonl_path(session_uuid)
    if jsonl_path is None:
        return []
    try:
        mtime_ns = jsonl_path.stat().st_mtime_ns
    except OSError:
        return []
    return _linked_memories_cached(session_uuid, mtime_ns)


def _enrich_history_results(results: list[dict[str, Any]]) -> None:
    """In-place: attach `session_uuid`, `session_title`, `session_datetime`,
    `session_modified`, and `linked_memories` to each history search hit.

    The indexer writes a temp `.md` file per session named `<uuid>.md`, so
    each result's `file_path` ends with `<uuid>.md`. We extract the UUID
    and resolve the rest from .titles.json and the underlying JSONL.
    """
    titles = _load_titles()
    # Cache per-session lookups so duplicate hits don't re-stat.
    enrichment_cache: dict[str, dict[str, Any]] = {}
    for r in results:
        fp = r.get("file_path") or ""
        uuid = _session_uuid_from_path(fp)
        if not uuid:
            continue
        if uuid in enrichment_cache:
            data = enrichment_cache[uuid]
        else:
            jsonl_path = _session_jsonl_path(uuid)
            session_datetime = _first_message_datetime(jsonl_path) if jsonl_path else None
            try:
                modified = (
                    datetime.utcfromtimestamp(jsonl_path.stat().st_mtime).isoformat() + "Z"
                    if jsonl_path else None
                )
            except OSError:
                modified = None
            data = {
                "session_uuid": uuid,
                "session_title": titles.get(uuid),
                "session_datetime": session_datetime,
                "session_modified": modified,
                "linked_memories": _linked_memories(uuid),
            }
            enrichment_cache[uuid] = data
        r.update(data)

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
            # `--socket` opens a Unix domain socket transport in addition
            # to the stdio one used by this client. The socket lets
            # external writers (embed.py, manager/index_utils.py) reach
            # the same warm server, keeping chroma single-writer.
            proc = await asyncio.create_subprocess_exec(
                str(_RUN_SH), str(_SEARCH_SERVER), "--socket",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as e:
            logger.error("Failed to start search server: %s", e)
            return None

        # Forward the server's stderr to our logger so boot-probe /
        # boot-repair / write-error messages surface in journalctl.
        # We start this BEFORE waiting on `ready` so any messages
        # emitted during the model-load window aren't lost.
        asyncio.create_task(_forward_stderr(proc))

        # Wait for the "ready" signal (model loaded)
        try:
            ready_line = await asyncio.wait_for(
                proc.stdout.readline(), timeout=180,  # Model load can be slow on Jetson
            )
            ready_data = json.loads(ready_line.decode().strip())
            if ready_data.get("status") == "ready":
                _server_proc = proc
                _server_ready = True
                socket_path = ready_data.get("socket")
                if socket_path:
                    logger.info("Search server ready (PID %d, socket=%s)", proc.pid, socket_path)
                else:
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


async def _forward_stderr(proc: asyncio.subprocess.Process) -> None:
    """Pipe the search server's stderr into our logger one line at a time.

    Lines that look like our own `[search-server]` markers (boot-probe,
    boot-repair, etc.) are logged at WARNING — they're once-per-boot
    events worth surfacing under the default log config without
    needing to flip the orchestrator logger to INFO. Lines from chatty
    libraries we trust (sentence-transformers' "Loading weights:"
    progress bar, huggingface_hub's auth warning) are dropped.
    Anything else also goes to WARNING — that's the bucket for genuine
    surprises like chroma tracebacks."""
    if proc.stderr is None:
        return
    # Patterns we silence outright — high-volume noise that's not
    # actionable. Anything not matched falls through to WARNING.
    NOISE_PREFIXES = (
        "Loading weights",
        "BertModel LOAD REPORT",
        "Warning: You are sending unauthenticated requests to the HF Hub",
        "Notes:",
        "- UNEXPECTED",
        "Key",
        "embeddings.position_ids",
        "------------------------",
    )
    try:
        while True:
            raw = await proc.stderr.readline()
            if not raw:
                return
            line = raw.decode(errors="replace").rstrip()
            if not line:
                continue
            if line.startswith("[search-server]"):
                logger.warning("search-server: %s", line[len("[search-server] "):])
                continue
            if any(line.startswith(p) for p in NOISE_PREFIXES):
                continue
            logger.warning("search-server stderr: %s", line)
    except Exception as e:
        logger.warning("search-server stderr reader stopped: %s", e)


async def _query_server(
    proc: asyncio.subprocess.Process,
    request: dict,
    *,
    timeout: float = 30.0,
) -> dict | None:
    """Send a query to the warm server and read the response."""
    try:
        line = json.dumps(request) + "\n"
        proc.stdin.write(line.encode())
        await proc.stdin.drain()

        response_line = await asyncio.wait_for(
            proc.stdout.readline(), timeout=timeout,  # Warm queries should be fast
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
    _enrich_history_results(results)
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
    _enrich_memory_results(results)
    return json.dumps({"query": query, "results": results, "count": len(results)})


