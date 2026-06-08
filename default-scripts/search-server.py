#!/usr/bin/env python3
"""
Persistent search/index server — SINGLE owner of the chroma PersistentClient.

This process is the ONLY one that opens the chroma index. Other code
(embed.py, manager/index_utils.py, cleanup-history-index.py, the
orchestrator's search tools) sends requests to this server via a Unix
domain socket. Chroma's PersistentClient is not safe across concurrent
processes; routing every access through one long-lived process removes
that risk class entirely.

The server supports TWO transports for backward compatibility:

1. stdin/stdout (legacy) — when the server is spawned as a child
   subprocess by the orchestrator; the parent already owns stdio. This
   path is what `orchestrator/tools/search.py` uses today.
2. Unix domain socket at <INDEX_DIR>/.search-server.sock — usable by any
   client process that knows the index path. This is how out-of-tree
   writers like embed.py reach the server.

Both transports speak the same JSON-line protocol:

  Read commands
  -------------
  {"query": "...", "collection": "memory", "n_results": 5,
   "threshold": 1.5, "file_filter": "...optional..."}
    -> {"results": [...], "error": null}

  {"command": "ping"}                            -> {"status": "ready"}
  {"command": "count", "collection": "history"}  -> {"count": int, "error": null}
  {"command": "list_collections"}                -> {"collections": [str], "error": null}
  {"command": "get_by_file", ...}                -> {"ids": [str], "error": null}
  {"command": "encode", "text": "..."}           -> {"embedding": [float], "error": null}

  Write commands
  --------------
  {"command": "add_chunks", "collection": "history", "chunks": [...]}
    -> {"added": int, "error": null}
  {"command": "delete_ids", "collection": "...", "ids": [...]}
    -> {"deleted": int, "error": null}
  {"command": "delete_where", "collection": "...", "where": {...}}
    -> {"deleted": int, "error": null}
  {"command": "reset_collection", "name": "..."}
    -> {"reset": true, "error": null}

  Maintenance
  -----------
  {"command": "validate", "collection": "..."}
    -> {"healthy": bool, "details": {...}, "error": null}
  {"command": "repair", "collection": "...", "tier": "auto"|"wal_replay"|"full_reembed"}
    -> {"tier_used": str, "before": int, "after": int, "error": str|None}
  {"command": "shutdown"}
    -> process exits cleanly

Crash-safety
------------
- A POSIX flock on <INDEX_DIR>/.search-server.lock prevents two servers
  from running against the same index. If the lock is held, this process
  exits non-zero with a clear error.
- Collections created here use hnsw:sync_threshold=200 (vs chroma's
  default 1000) to shrink the corruption window if the process is killed
  mid-flush.
- validate/repair commands run their work in subprocesses so a chroma
  SIGSEGV is a recoverable exit code, not a server crash.
"""
import errno
import fcntl
import json
import os
import socket
import sys
import threading
import traceback
from pathlib import Path

# Add project root to path for utils import (and sibling default-scripts)
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(SCRIPT_DIR))

from utils.paths import get_index_dir

INDEX_DIR = get_index_dir() / "chroma"
LOCKFILE = INDEX_DIR / ".search-server.lock"
SOCKET_PATH = INDEX_DIR / ".search-server.sock"

HNSW_METADATA = {"hnsw:space": "cosine", "hnsw:sync_threshold": 200}


# ── stdio helpers ────────────────────────────────────────────────────────────

def stdio_send(data: dict) -> None:
    sys.stdout.write(json.dumps(data) + "\n")
    sys.stdout.flush()


def stdio_send_error(message: str) -> None:
    stdio_send({"results": [], "error": message})


# ── locking ──────────────────────────────────────────────────────────────────

def acquire_lock(lockfile: Path):
    lockfile.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lockfile), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            print(
                f"[search-server] lockfile {lockfile} held by another process; refusing to start",
                file=sys.stderr, flush=True,
            )
            sys.exit(2)
        raise
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


# ── request dispatch ─────────────────────────────────────────────────────────

class IndexServer:
    """Owns the chroma client and dispatches requests. Methods return dicts
    (the wire format); they never write to stdio/socket directly."""

    def __init__(self):
        import chromadb
        from sentence_transformers import SentenceTransformer

        self.client = chromadb.PersistentClient(path=str(INDEX_DIR))
        self.model = SentenceTransformer("all-MiniLM-L6-v2")
        # All writes serialize through this lock so concurrent socket
        # clients can't interleave inside a single chroma write.
        self._write_lock = threading.Lock()

    def get_or_create_collection(self, name: str):
        return self.client.get_or_create_collection(name=name, metadata=HNSW_METADATA)

    def handle(self, request: dict) -> dict:
        """Dispatch one request → reply dict."""
        try:
            if "command" in request:
                return self._handle_command(request)
            return self._handle_query(request)
        except Exception as e:
            tb = traceback.format_exc()
            print(
                f"[search-server FAILED] req={request!r}\n{tb}",
                file=sys.stderr, flush=True,
            )
            return {"error": f"{type(e).__name__}: {e}"}

    def _handle_command(self, request: dict) -> dict:
        cmd = request["command"]

        if cmd == "ping":
            return {"status": "ready"}
        if cmd == "shutdown":
            # Caller writes the reply then we exit.
            return {"shutting_down": True}
        if cmd == "list_collections":
            return {"collections": [c.name for c in self.client.list_collections()], "error": None}
        if cmd == "count":
            name = request["collection"]
            try:
                return {"count": self.client.get_collection(name).count(), "error": None}
            except Exception as e:
                return {"count": None, "error": f"{type(e).__name__}: {e}"}
        if cmd == "encode":
            emb = self.model.encode([request["text"]])[0].tolist()
            return {"embedding": emb, "error": None}
        if cmd == "encode_many":
            texts = request["texts"]
            embs = [v.tolist() for v in self.model.encode(texts)] if texts else []
            return {"embeddings": embs, "error": None}
        if cmd == "get_by_file":
            return self._get_by_file(request["collection"], request["file_path"])
        if cmd == "add_chunks":
            return self._add_chunks(request["collection"], request["chunks"])
        if cmd == "delete_ids":
            return self._delete_ids(request["collection"], request["ids"])
        if cmd == "delete_where":
            return self._delete_where(request["collection"], request["where"])
        if cmd == "reset_collection":
            return self._reset_collection(request["name"])
        if cmd == "validate":
            import repair
            details = repair.validate_collection(INDEX_DIR, request["collection"])
            return {"healthy": details.get("healthy", False), "details": details, "error": None}
        if cmd == "repair":
            return self._repair(request["collection"], request.get("tier", "auto"))
        return {"error": f"Unknown command: {cmd}"}

    def _get_by_file(self, name: str, file_path: str) -> dict:
        col = self.get_or_create_collection(name)
        try:
            res = col.get(where={"file_path": file_path}, include=[])
            return {"ids": res.get("ids", []), "error": None}
        except Exception:
            res = col.get(include=["metadatas"])
            ids = [i for i, m in zip(res["ids"], res["metadatas"]) if m.get("file_path") == file_path]
            return {"ids": ids, "error": None}

    def _add_chunks(self, name: str, chunks: list) -> dict:
        with self._write_lock:
            col = self.get_or_create_collection(name)
            if chunks:
                col.add(
                    ids=[c["id"] for c in chunks],
                    embeddings=[c["embedding"] for c in chunks],
                    documents=[c["document"] for c in chunks],
                    metadatas=[c["metadata"] for c in chunks],
                )
            return {"added": len(chunks), "error": None}

    def _delete_ids(self, name: str, ids: list) -> dict:
        with self._write_lock:
            col = self.get_or_create_collection(name)
            if ids:
                col.delete(ids=ids)
            return {"deleted": len(ids), "error": None}

    def _delete_where(self, name: str, where: dict) -> dict:
        with self._write_lock:
            col = self.get_or_create_collection(name)
            try:
                res = col.get(where=where, include=[])
                ids = res.get("ids", [])
            except Exception:
                res = col.get(include=["metadatas"])
                ids = [i for i, m in zip(res["ids"], res["metadatas"])
                       if all(m.get(k) == v for k, v in where.items())]
            if ids:
                col.delete(ids=ids)
            return {"deleted": len(ids), "error": None}

    def _reset_collection(self, name: str) -> dict:
        with self._write_lock:
            try:
                self.client.delete_collection(name)
            except Exception:
                pass
            self.get_or_create_collection(name)
            return {"reset": True, "error": None}

    def _repair(self, name: str, tier: str) -> dict:
        # Repair tears down + rebuilds chroma state. We acquire the write
        # lock for the full duration, drop our chroma client, run the
        # repair in a subprocess, then reopen.
        import repair
        with self._write_lock:
            try:
                self.client.clear_system_cache()  # type: ignore[attr-defined]
            except Exception:
                pass
            self.client = None  # type: ignore[assignment]
            try:
                result = repair.auto_repair(INDEX_DIR, name, preferred_tier=tier)
            finally:
                import chromadb
                self.client = chromadb.PersistentClient(path=str(INDEX_DIR))
            return result

    def _handle_query(self, request: dict) -> dict:
        query = request.get("query", "")
        collection_name = request.get("collection", "memory")
        n_results = request.get("n_results", 5)
        threshold = request.get("threshold", 1.5)
        file_filter = request.get("file_filter")

        if not query:
            return {"results": [], "error": "Missing 'query' field"}

        try:
            collection = self.client.get_collection(collection_name)
        except Exception:
            return {"results": [], "error": f"Collection '{collection_name}' not found."}

        count = collection.count()
        if count == 0:
            return {"results": [], "error": f"Collection '{collection_name}' is empty."}

        query_embedding = self.model.encode([query])[0].tolist()
        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=min(n_results, count),
        )

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
        return {"results": formatted, "error": None}


# ── transports ───────────────────────────────────────────────────────────────

def run_stdio_loop(server: IndexServer, shutdown_flag: threading.Event) -> None:
    """Read JSON-line requests on stdin, write replies on stdout."""
    for line in sys.stdin:
        if shutdown_flag.is_set():
            return
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            stdio_send_error(f"Invalid JSON: {line[:200]}")
            continue
        reply = server.handle(request)
        stdio_send(reply)
        if request.get("command") == "shutdown":
            shutdown_flag.set()
            return


def _create_listening_socket(sock_path: Path) -> socket.socket:
    """Bind + listen on the Unix domain socket. Returns the listening
    socket. Caller should accept() in a loop."""
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    if sock_path.exists():
        sock_path.unlink()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(sock_path))
    os.chmod(str(sock_path), 0o600)
    sock.listen(8)
    sock.settimeout(0.5)
    return sock


def run_socket_loop(server: IndexServer, sock: socket.socket, sock_path: Path, shutdown_flag: threading.Event) -> None:
    """Accept connections on a pre-bound socket."""

    def serve_conn(conn: socket.socket) -> None:
        # Buffered I/O (default 8KB block). Unbuffered mode (`buffering=0`)
        # made `readline()` fall back to single-byte recv() calls, which
        # was both slow AND lost data on requests larger than ~200KB (the
        # default Linux socket buffer) because partial reads weren't
        # accumulated correctly. Buffered mode uses chunked recv() and
        # accumulates until `\n`. Must explicitly flush() after each write.
        try:
            f = conn.makefile("rwb")
            while not shutdown_flag.is_set():
                line = f.readline()
                if not line:
                    return
                try:
                    request = json.loads(line.decode().strip())
                except json.JSONDecodeError:
                    try:
                        f.write(json.dumps({"error": "Invalid JSON"}).encode() + b"\n")
                        f.flush()
                    except BrokenPipeError:
                        return
                    continue
                reply = server.handle(request)
                try:
                    f.write(json.dumps(reply).encode() + b"\n")
                    f.flush()
                except BrokenPipeError:
                    return
                if request.get("command") == "shutdown":
                    shutdown_flag.set()
                    return
        finally:
            try:
                conn.close()
            except Exception:
                pass

    try:
        while not shutdown_flag.is_set():
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            t = threading.Thread(target=serve_conn, args=(conn,), daemon=True)
            t.start()
    finally:
        sock.close()
        try:
            sock_path.unlink()
        except FileNotFoundError:
            pass


# ── entry point ──────────────────────────────────────────────────────────────

def _boot_validate_and_repair(skip: bool = False) -> None:
    """At server boot, probe each existing collection in a subprocess
    (so a chroma SIGSEGV stays out of this process). On detection, run
    the auto-repair tier dispatcher. The lockfile is already held by us
    at this point — the repair subprocess opens its own
    chromadb.PersistentClient briefly. That is single-writer-safe
    because we are the lock holder and the repair runs synchronously
    before we accept any external requests.
    """
    if skip:
        print("[search-server] boot validation skipped (--skip-validate)", file=sys.stderr, flush=True)
        return
    # We can list collections via sqlite without opening chroma at all.
    import sqlite3
    db_path = INDEX_DIR / "chroma.sqlite3"
    if not db_path.exists():
        print("[search-server] no chroma.sqlite3 yet; skipping boot validation", file=sys.stderr, flush=True)
        return
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        names = [r[0] for r in db.execute("SELECT name FROM collections")]
        db.close()
    except Exception as e:
        print(f"[search-server] sqlite read failed during boot: {e}", file=sys.stderr, flush=True)
        return

    if not names:
        print("[search-server] no collections to validate", file=sys.stderr, flush=True)
        return

    sys.path.insert(0, str(SCRIPT_DIR))
    import repair  # noqa: E402

    for name in names:
        result = repair.validate_collection(INDEX_DIR, name)
        if result.get("healthy"):
            print(
                f"[search-server] boot-probe '{name}': OK count={result.get('count')} "
                f"({result.get('duration_s', 0):.1f}s)",
                file=sys.stderr, flush=True,
            )
            continue
        print(
            f"[search-server] boot-probe '{name}': UNHEALTHY stage={result.get('stage')} "
            f"error={result.get('error')} signal={result.get('signal')}",
            file=sys.stderr, flush=True,
        )
        rep = repair.auto_repair(INDEX_DIR, name)
        print(
            f"[search-server] boot-repair '{name}': tier={rep.get('tier_used')} "
            f"before={rep.get('before')} after={rep.get('after')} error={rep.get('error')}",
            file=sys.stderr, flush=True,
        )


def main() -> None:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    _ = acquire_lock(LOCKFILE)  # kept open for process lifetime

    # --socket flag opens a Unix domain socket transport in addition to
    # the stdio one. Default is stdio-only to preserve the existing
    # orchestrator integration. When --socket is set, both transports
    # run concurrently on separate threads.
    use_socket = "--socket" in sys.argv

    # Boot-time validation: probe every existing collection in a child
    # subprocess so a corrupt segment can't crash us before we even
    # accept requests. If we find corruption, auto-repair (cheap tier
    # first) so subsequent queries don't fail.
    _boot_validate_and_repair(skip="--skip-validate" in sys.argv)

    server = IndexServer()
    shutdown_flag = threading.Event()

    # Bind the socket BEFORE announcing readiness — otherwise clients
    # can race and find no socket on disk.
    sock = None
    if use_socket:
        sock = _create_listening_socket(SOCKET_PATH)

    # Announce readiness (and the socket path if applicable) on stdout.
    # The orchestrator waits for this to know the model is loaded.
    ready_payload: dict = {"status": "ready"}
    if use_socket:
        ready_payload["socket"] = str(SOCKET_PATH)
    stdio_send(ready_payload)

    if use_socket and sock is not None:
        sock_thread = threading.Thread(
            target=run_socket_loop, args=(server, sock, SOCKET_PATH, shutdown_flag),
            daemon=True,
        )
        sock_thread.start()

    # The stdio loop runs on the main thread (it owns sys.stdin). When a
    # shutdown command arrives via either transport, the flag fires and
    # both loops drain.
    run_stdio_loop(server, shutdown_flag)
    shutdown_flag.set()


if __name__ == "__main__":
    main()
