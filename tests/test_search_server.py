"""Integration tests for the warm search-server: protocol, lockfile,
socket transport, and the IndexFacade client.

These tests spawn the real server as a subprocess against a temp index
dir. They are integration tests — slow (~10s each because of model
load), but they verify the whole pipeline end-to-end.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
SERVER_SCRIPT = PROJECT_DIR / "default-scripts" / "search-server.py"
SCRIPTS_DIR = PROJECT_DIR / "default-scripts"

pytestmark = [pytest.mark.slow, pytest.mark.timeout(240)]


def _make_shim(index_dir: Path) -> str:
    """A small wrapper that monkey-patches utils.paths.get_index_dir to
    the parent of our chroma dir before importing the server. Returns
    the script as a string for subprocess `python -c`. The server
    appends '/chroma' to whatever get_index_dir returns, so we point at
    the parent."""
    parent = index_dir.parent  # `index_dir` ends in /chroma
    return f"""
import sys, pathlib
sys.path.insert(0, {str(PROJECT_DIR)!r})
sys.path.insert(0, {str(SCRIPTS_DIR)!r})
import utils.paths
utils.paths.get_index_dir = lambda: pathlib.Path({str(parent)!r})
import importlib.util
spec = importlib.util.spec_from_file_location("search_server", {str(SERVER_SCRIPT)!r})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.main()
"""


@pytest.fixture
def index_dir(tmp_path: Path) -> Path:
    d = tmp_path / "index" / "chroma"
    d.mkdir(parents=True)
    return d


def _wait_ready(proc: subprocess.Popen, timeout: float = 180.0) -> dict:
    """Read the 'ready' line from the server's stdout. Model load can
    take ~100s on slow machines."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            time.sleep(0.1)
            continue
        try:
            data = json.loads(line.decode().strip())
        except json.JSONDecodeError:
            continue
        if data.get("status") == "ready":
            return data
    raise TimeoutError(f"server did not become ready within {timeout}s")


def _spawn_server(index_dir: Path, *, socket_mode: bool = False) -> subprocess.Popen:
    args = [sys.executable, "-c", _make_shim(index_dir)]
    if socket_mode:
        args.append("--socket")
    proc = subprocess.Popen(
        args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return proc


def _send_stdio(proc: subprocess.Popen, request: dict, timeout: float = 30.0) -> dict:
    """Round-trip one stdio request."""
    proc.stdin.write(json.dumps(request).encode() + b"\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    return json.loads(line.decode().strip())


def _shutdown(proc: subprocess.Popen) -> None:
    try:
        proc.stdin.write(b'{"command": "shutdown"}\n')
        proc.stdin.flush()
    except (BrokenPipeError, OSError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


# ── Stdio tests ──────────────────────────────────────────────────────────────

def test_stdio_ping(index_dir):
    proc = _spawn_server(index_dir)
    try:
        ready = _wait_ready(proc)
        assert ready["status"] == "ready"
        r = _send_stdio(proc, {"command": "ping"})
        assert r["status"] == "ready"
    finally:
        _shutdown(proc)


def test_stdio_add_and_query(index_dir):
    proc = _spawn_server(index_dir)
    try:
        _wait_ready(proc)
        # Encode a few texts so we have real embeddings
        texts = ["the cat sat on the mat", "dogs bark at strangers", "fish swim in water"]
        r = _send_stdio(proc, {"command": "encode_many", "texts": texts}, timeout=120)
        assert r["error"] is None
        embeddings = r["embeddings"]
        assert len(embeddings) == 3
        assert len(embeddings[0]) == 384

        chunks = [
            {"id": f"chunk_{i}", "embedding": e, "document": t,
             "metadata": {"file_path": f"/x/file_{i}.md", "start_line": 1, "end_line": 1, "file_name": f"file_{i}.md"}}
            for i, (t, e) in enumerate(zip(texts, embeddings))
        ]
        r = _send_stdio(proc, {"command": "add_chunks", "collection": "test", "chunks": chunks})
        assert r["error"] is None
        assert r["added"] == 3

        r = _send_stdio(proc, {"command": "count", "collection": "test"})
        assert r["count"] == 3

        # Query — should return the cat chunk for a cat-themed query
        r = _send_stdio(proc, {"query": "feline animal", "collection": "test", "n_results": 1})
        assert r["error"] is None
        assert len(r["results"]) == 1
        assert "cat" in r["results"][0]["text"]
    finally:
        _shutdown(proc)


def test_stdio_delete_ids(index_dir):
    proc = _spawn_server(index_dir)
    try:
        _wait_ready(proc)
        r = _send_stdio(proc, {"command": "encode_many", "texts": ["x", "y", "z"]}, timeout=120)
        embeddings = r["embeddings"]
        chunks = [
            {"id": f"id_{i}", "embedding": e, "document": "doc",
             "metadata": {"file_path": "/a.md", "start_line": 1, "end_line": 1, "file_name": "a.md"}}
            for i, e in enumerate(embeddings)
        ]
        _send_stdio(proc, {"command": "add_chunks", "collection": "tcol", "chunks": chunks})
        r = _send_stdio(proc, {"command": "delete_ids", "collection": "tcol", "ids": ["id_0", "id_2"]})
        assert r["deleted"] == 2
        r = _send_stdio(proc, {"command": "count", "collection": "tcol"})
        assert r["count"] == 1
    finally:
        _shutdown(proc)


def test_stdio_delete_where(index_dir):
    proc = _spawn_server(index_dir)
    try:
        _wait_ready(proc)
        r = _send_stdio(proc, {"command": "encode_many", "texts": ["x", "y", "z"]}, timeout=120)
        embeddings = r["embeddings"]
        chunks = [
            {"id": f"id_{i}", "embedding": e, "document": "doc",
             "metadata": {"file_path": f"/{['a','a','b'][i]}.md", "start_line": 1, "end_line": 1, "file_name": "x"}}
            for i, e in enumerate(embeddings)
        ]
        _send_stdio(proc, {"command": "add_chunks", "collection": "tcol", "chunks": chunks})
        r = _send_stdio(proc, {"command": "delete_where", "collection": "tcol", "where": {"file_path": "/a.md"}})
        assert r["deleted"] == 2
        r = _send_stdio(proc, {"command": "count", "collection": "tcol"})
        assert r["count"] == 1
    finally:
        _shutdown(proc)


def test_stdio_reset_collection(index_dir):
    proc = _spawn_server(index_dir)
    try:
        _wait_ready(proc)
        r = _send_stdio(proc, {"command": "encode_many", "texts": ["x"]}, timeout=120)
        chunks = [{"id": "a", "embedding": r["embeddings"][0], "document": "d",
                   "metadata": {"file_path": "/x.md", "start_line": 1, "end_line": 1, "file_name": "x"}}]
        _send_stdio(proc, {"command": "add_chunks", "collection": "tcol", "chunks": chunks})
        r = _send_stdio(proc, {"command": "count", "collection": "tcol"})
        assert r["count"] == 1
        r = _send_stdio(proc, {"command": "reset_collection", "name": "tcol"})
        assert r["reset"] is True
        r = _send_stdio(proc, {"command": "count", "collection": "tcol"})
        assert r["count"] == 0
    finally:
        _shutdown(proc)


# ── Socket tests ─────────────────────────────────────────────────────────────

def test_socket_concurrent_clients(index_dir):
    """Two simultaneous socket clients writing in parallel should both
    succeed without corrupting the index. This is the scenario the
    pre-refactor code couldn't handle safely."""
    proc = _spawn_server(index_dir, socket_mode=True)
    try:
        ready = _wait_ready(proc)
        assert "socket" in ready
        sock_path = Path(ready["socket"])
        assert sock_path.exists()

        # Encode some texts via the first client.
        c1 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c1.connect(str(sock_path))
        f1 = c1.makefile("rwb", buffering=0)
        f1.write(json.dumps({"command": "encode_many",
                             "texts": [f"text_{i}" for i in range(10)]}).encode() + b"\n")
        r = json.loads(f1.readline().decode())
        embs = r["embeddings"]

        # Two clients each adding half the chunks concurrently.
        import threading
        results = {}

        def add_chunks(client_id, ids_range):
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(str(sock_path))
            f = c.makefile("rwb", buffering=0)
            chunks = [
                {"id": f"c{client_id}_chunk_{i}", "embedding": embs[i],
                 "document": "d", "metadata": {"file_path": f"/c{client_id}.md", "start_line": 1, "end_line": 1, "file_name": "x"}}
                for i in ids_range
            ]
            f.write(json.dumps({"command": "add_chunks", "collection": "concurrent", "chunks": chunks}).encode() + b"\n")
            reply = json.loads(f.readline().decode())
            results[client_id] = reply
            c.close()

        t1 = threading.Thread(target=add_chunks, args=(1, range(5)))
        t2 = threading.Thread(target=add_chunks, args=(2, range(5, 10)))
        t1.start(); t2.start()
        t1.join(); t2.join()

        assert results[1]["error"] is None
        assert results[2]["error"] is None
        assert results[1]["added"] == 5
        assert results[2]["added"] == 5

        # Count via stdio
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(str(sock_path))
        f = c.makefile("rwb", buffering=0)
        f.write(json.dumps({"command": "count", "collection": "concurrent"}).encode() + b"\n")
        r = json.loads(f.readline().decode())
        assert r["count"] == 10
        c.close()
    finally:
        _shutdown(proc)


# ── Lockfile tests ───────────────────────────────────────────────────────────

def test_lockfile_prevents_second_server(index_dir):
    """A second server pointing at the same index should refuse to start."""
    p1 = _spawn_server(index_dir)
    try:
        _wait_ready(p1)
        # Second spawn should exit ~immediately with exit 2.
        p2 = _spawn_server(index_dir)
        try:
            p2.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p2.kill()
            pytest.fail("second server did not exit; lockfile not enforced")
        assert p2.returncode == 2
        stderr = p2.stderr.read().decode()
        assert "lockfile" in stderr or "held by another process" in stderr
    finally:
        _shutdown(p1)


def test_lockfile_released_on_shutdown(index_dir):
    """After the first server shuts down, a second should start fine."""
    p1 = _spawn_server(index_dir)
    _wait_ready(p1)
    _shutdown(p1)

    p2 = _spawn_server(index_dir)
    try:
        _wait_ready(p2)
        r = _send_stdio(p2, {"command": "ping"})
        assert r["status"] == "ready"
    finally:
        _shutdown(p2)
