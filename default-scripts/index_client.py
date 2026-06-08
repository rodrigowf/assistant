"""Client for the warm search/index server.

Talks to the warm server over a Unix domain socket at
`<index>/.search-server.sock`. If no warm server is running (no
lockfile or stale socket), falls back to opening chroma directly —
safe because the warm server's flock guarantees that path can only be
taken when nobody else is writing.

Used by:
  - default-scripts/embed.py (the indexer CLI)
  - manager/index_utils.py (session-delete cleanup)
  - default-scripts/cleanup-history-index.py
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))

from utils.paths import get_index_dir

INDEX_DIR = get_index_dir() / "chroma"
SOCKET_PATH = INDEX_DIR / ".search-server.sock"
LOCKFILE = INDEX_DIR / ".search-server.lock"


def warm_server_running() -> bool:
    """Return True if a warm server holds the lockfile.

    We check by trying to acquire the same flock non-blockingly. If we
    can grab it, no server is holding it; release immediately."""
    if not LOCKFILE.exists():
        return False
    try:
        fd = os.open(str(LOCKFILE), os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                return True
            raise
        # We got the lock → nobody was holding it → no warm server.
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


class SocketClient:
    """Line-delimited JSON over a Unix domain socket. Single-threaded."""

    def __init__(self, sock_path: Path = SOCKET_PATH, timeout: float = 60.0):
        self.sock_path = sock_path
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._file = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(str(self.sock_path))
        self._sock = s
        # Buffered I/O (default 8KB block). Unbuffered mode lost data on
        # writes larger than ~200KB because socket sends weren't accumulated
        # correctly on slower hosts (Jetson). Must flush() after each write.
        self._file = s.makefile("rwb")

    def close(self) -> None:
        try:
            if self._file:
                self._file.close()
        finally:
            self._file = None
            if self._sock:
                try:
                    self._sock.close()
                finally:
                    self._sock = None

    def call(self, request: dict, *, request_timeout: Optional[float] = None) -> dict:
        if self._file is None:
            raise RuntimeError("not connected")
        if request_timeout is not None and self._sock is not None:
            self._sock.settimeout(request_timeout)
        self._file.write(json.dumps(request).encode() + b"\n")
        self._file.flush()
        line = self._file.readline()
        if not line:
            raise ConnectionError("warm server closed the connection")
        return json.loads(line.decode())


def try_connect(timeout: float = 5.0) -> Optional[SocketClient]:
    """Try to open a socket to the warm server. Returns None on failure."""
    if not SOCKET_PATH.exists():
        return None
    c = SocketClient(SOCKET_PATH, timeout=timeout)
    try:
        c.connect()
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError):
        return None
    return c


def wait_for_server(timeout: float = 5.0, poll_interval: float = 0.2) -> Optional[SocketClient]:
    """Poll the socket until a warm server answers, or timeout. Useful for
    just-after-spawn races."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        c = try_connect(timeout=2.0)
        if c is not None:
            try:
                r = c.call({"command": "ping"}, request_timeout=2.0)
                if r.get("status") == "ready":
                    return c
            except Exception:
                pass
            c.close()
        time.sleep(poll_interval)
    return None


# ── Convenience facades ───────────────────────────────────────────────────────

class IndexFacade:
    """Operations on the index — uses the warm server if reachable,
    otherwise opens chroma directly.

    The direct-chroma path is only taken when the warm server's lockfile
    is unheld (so nobody else can be racing us). Use as a context manager
    to ensure the socket is closed promptly."""

    def __init__(self):
        self._client: Optional[SocketClient] = try_connect()
        self._direct = None  # lazy chroma client
        self._mode = "socket" if self._client else "direct"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self._client:
            self._client.close()

    @property
    def mode(self) -> str:
        return self._mode

    def _direct_client(self):
        if self._direct is None:
            if warm_server_running():
                raise RuntimeError(
                    "warm server lockfile is held but socket not reachable; "
                    "refusing to open chroma directly to avoid concurrent-writer corruption"
                )
            import chromadb
            INDEX_DIR.mkdir(parents=True, exist_ok=True)
            self._direct = chromadb.PersistentClient(path=str(INDEX_DIR))
        return self._direct

    def _direct_collection(self, name: str):
        return self._direct_client().get_or_create_collection(
            name=name, metadata={"hnsw:space": "cosine", "hnsw:sync_threshold": 200}
        )

    # — Read operations —

    def list_collections(self) -> list[str]:
        if self._client:
            r = self._client.call({"command": "list_collections"})
            return r.get("collections") or []
        return [c.name for c in self._direct_client().list_collections()]

    def count(self, collection: str) -> int:
        if self._client:
            r = self._client.call({"command": "count", "collection": collection})
            return r.get("count") or 0
        try:
            return self._direct_client().get_collection(collection).count()
        except Exception:
            return 0

    def get_by_file(self, collection: str, file_path: str) -> list[str]:
        if self._client:
            r = self._client.call({"command": "get_by_file", "collection": collection, "file_path": file_path})
            return r.get("ids") or []
        col = self._direct_collection(collection)
        try:
            res = col.get(where={"file_path": file_path}, include=[])
            return res.get("ids") or []
        except Exception:
            res = col.get(include=["metadatas"])
            return [i for i, m in zip(res["ids"], res["metadatas"]) if m.get("file_path") == file_path]

    # — Write operations —

    def add_chunks(self, collection: str, chunks: list[dict]) -> int:
        """Add chunks. Each chunk = {id, embedding, document, metadata}."""
        if not chunks:
            return 0
        if self._client:
            r = self._client.call(
                {"command": "add_chunks", "collection": collection, "chunks": chunks},
                request_timeout=120.0,
            )
            if r.get("error"):
                raise RuntimeError(f"add_chunks failed: {r['error']}")
            return r.get("added") or 0
        col = self._direct_collection(collection)
        col.add(
            ids=[c["id"] for c in chunks],
            embeddings=[c["embedding"] for c in chunks],
            documents=[c["document"] for c in chunks],
            metadatas=[c["metadata"] for c in chunks],
        )
        return len(chunks)

    def delete_ids(self, collection: str, ids: list[str]) -> int:
        if not ids:
            return 0
        if self._client:
            r = self._client.call({"command": "delete_ids", "collection": collection, "ids": ids})
            if r.get("error"):
                raise RuntimeError(f"delete_ids failed: {r['error']}")
            return r.get("deleted") or 0
        col = self._direct_collection(collection)
        col.delete(ids=ids)
        return len(ids)

    def delete_where(self, collection: str, where: dict) -> int:
        if self._client:
            r = self._client.call({"command": "delete_where", "collection": collection, "where": where})
            if r.get("error"):
                raise RuntimeError(f"delete_where failed: {r['error']}")
            return r.get("deleted") or 0
        col = self._direct_collection(collection)
        try:
            res = col.get(where=where, include=[])
            ids = res.get("ids") or []
        except Exception:
            res = col.get(include=["metadatas"])
            ids = [i for i, m in zip(res["ids"], res["metadatas"])
                   if all(m.get(k) == v for k, v in where.items())]
        if ids:
            col.delete(ids=ids)
        return len(ids)

    def reset_collection(self, name: str) -> None:
        if self._client:
            r = self._client.call({"command": "reset_collection", "name": name})
            if r.get("error"):
                raise RuntimeError(f"reset_collection failed: {r['error']}")
            return
        client = self._direct_client()
        try:
            client.delete_collection(name)
        except Exception:
            pass
        self._direct_collection(name)

    def encode(self, text: str) -> list[float]:
        """Embed a single string using the same model the warm server uses."""
        if self._client:
            r = self._client.call({"command": "encode", "text": text}, request_timeout=60.0)
            if r.get("error"):
                raise RuntimeError(f"encode failed: {r['error']}")
            return r["embedding"]
        # Direct path: load the model in-process. Expensive on cold start.
        from sentence_transformers import SentenceTransformer
        if not hasattr(self, "_model"):
            self._model = SentenceTransformer("all-MiniLM-L6-v2")
        return self._model.encode([text])[0].tolist()

    def encode_many(self, texts: list[str]) -> list[list[float]]:
        """Batch encode — efficient with model warm in the server."""
        if not texts:
            return []
        if self._client:
            r = self._client.call(
                {"command": "encode_many", "texts": texts},
                request_timeout=300.0,
            )
            if r.get("error"):
                raise RuntimeError(f"encode_many failed: {r['error']}")
            return r["embeddings"]
        from sentence_transformers import SentenceTransformer
        if not hasattr(self, "_model"):
            self._model = SentenceTransformer("all-MiniLM-L6-v2")
        return [v.tolist() for v in self._model.encode(texts)]
