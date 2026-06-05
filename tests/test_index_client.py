"""Tests for default-scripts/index_client.IndexFacade.

Verifies:
  - The facade can talk to a running warm server via socket
  - Falls back to direct chroma when no warm server is running
  - Refuses to use direct chroma if the warm server's lockfile is held
    (corruption avoidance)
  - Reads and writes work consistently across both modes
"""
from __future__ import annotations

import importlib
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_DIR / "default-scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

pytestmark = [pytest.mark.slow, pytest.mark.timeout(240)]


def _fresh_index_client_module(index_dir: Path):
    """Import index_client fresh with INDEX_DIR pointed at our temp dir.

    The module's globals INDEX_DIR / SOCKET_PATH / LOCKFILE need to
    target our temp index, not the project default."""
    if "index_client" in sys.modules:
        del sys.modules["index_client"]
    mod = importlib.import_module("index_client")
    mod.INDEX_DIR = index_dir
    mod.SOCKET_PATH = index_dir / ".search-server.sock"
    mod.LOCKFILE = index_dir / ".search-server.lock"
    return mod


@pytest.fixture
def index_dir(tmp_path: Path) -> Path:
    d = tmp_path / "index" / "chroma"
    d.mkdir(parents=True)
    return d


def _spawn_socket_server(index_dir: Path) -> subprocess.Popen:
    """Spawn the search server with --socket against the temp index."""
    parent = index_dir.parent  # server appends /chroma
    shim = f"""
import sys, pathlib
sys.path.insert(0, {str(PROJECT_DIR)!r})
sys.path.insert(0, {str(SCRIPTS_DIR)!r})
import utils.paths
utils.paths.get_index_dir = lambda: pathlib.Path({str(parent)!r})
import importlib.util
spec = importlib.util.spec_from_file_location("ss", {str(SCRIPTS_DIR / "search-server.py")!r})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.main()
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", shim, "--socket"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    # Wait for ready line.
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            time.sleep(0.05)
            continue
        try:
            data = json.loads(line.decode().strip())
            if data.get("status") == "ready":
                return proc
        except json.JSONDecodeError:
            continue
    proc.kill()
    raise TimeoutError("server did not become ready")


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


# ── Direct mode (no warm server) ─────────────────────────────────────────────

def test_direct_mode_basic(index_dir):
    """No warm server running → facade uses direct chroma."""
    ic = _fresh_index_client_module(index_dir)
    assert ic.warm_server_running() is False
    facade = ic.IndexFacade()
    assert facade.mode == "direct"
    with facade:
        emb = facade.encode("hello world")
        assert len(emb) == 384

        chunk = {"id": "abc", "embedding": emb, "document": "hello world",
                 "metadata": {"file_path": "/x.md", "start_line": 1, "end_line": 1, "file_name": "x.md"}}
        assert facade.add_chunks("col", [chunk]) == 1
        assert facade.count("col") == 1
        assert facade.get_by_file("col", "/x.md") == ["abc"]
        assert facade.delete_ids("col", ["abc"]) == 1
        assert facade.count("col") == 0


# ── Socket mode ──────────────────────────────────────────────────────────────

def test_socket_mode_basic(index_dir):
    proc = _spawn_socket_server(index_dir)
    try:
        ic = _fresh_index_client_module(index_dir)
        assert ic.warm_server_running() is True
        facade = ic.IndexFacade()
        assert facade.mode == "socket"
        with facade:
            embs = facade.encode_many(["alpha", "beta", "gamma"])
            assert len(embs) == 3
            chunks = [
                {"id": f"id_{i}", "embedding": e, "document": t,
                 "metadata": {"file_path": f"/f{i}.md", "start_line": 1, "end_line": 1, "file_name": f"f{i}.md"}}
                for i, (t, e) in enumerate(zip(["alpha", "beta", "gamma"], embs))
            ]
            assert facade.add_chunks("col", chunks) == 3
            assert facade.count("col") == 3
            assert sorted(facade.list_collections()) == ["col"]
            ids = facade.get_by_file("col", "/f1.md")
            assert ids == ["id_1"]
            facade.delete_where("col", {"file_path": "/f0.md"})
            assert facade.count("col") == 2
            facade.reset_collection("col")
            assert facade.count("col") == 0
    finally:
        _shutdown(proc)


def test_socket_mode_dedup_per_file(index_dir):
    """Re-adding chunks for the same file should be idempotent — same
    file_path + line range = same chunk_id → chroma de-dupes on add."""
    proc = _spawn_socket_server(index_dir)
    try:
        ic = _fresh_index_client_module(index_dir)
        facade = ic.IndexFacade()
        with facade:
            emb = facade.encode("static text")
            chunk = {"id": "stable_id", "embedding": emb, "document": "x",
                     "metadata": {"file_path": "/a.md", "start_line": 1, "end_line": 1, "file_name": "a"}}
            facade.add_chunks("col", [chunk])
            facade.add_chunks("col", [chunk])
            assert facade.count("col") == 1
    finally:
        _shutdown(proc)


# ── Safety: refuse direct-chroma access when lock is held ────────────────────

def test_refuses_direct_access_when_lock_held(index_dir):
    """If the warm server lockfile is held but socket isn't reachable
    (e.g. crash mid-startup), facade.direct path must refuse to avoid
    concurrent-writer corruption."""
    proc = _spawn_socket_server(index_dir)
    try:
        ic = _fresh_index_client_module(index_dir)
        # Pretend we couldn't connect to the socket (delete it; lockfile
        # still held).
        sock_path = index_dir / ".search-server.sock"
        sock_path.unlink()

        # Now construct the facade — it tries socket (fails), falls
        # back to direct. The first direct operation should refuse.
        facade = ic.IndexFacade()
        assert facade.mode == "direct"
        with facade:
            # Use add_chunks rather than count — count() swallows
            # exceptions intentionally (treats missing-collection as 0).
            with pytest.raises(RuntimeError, match="lockfile is held"):
                facade.add_chunks("col", [{"id": "x", "embedding": [0.1]*384, "document": "d",
                                            "metadata": {"file_path": "/x", "start_line": 1, "end_line": 1, "file_name": "x"}}])
    finally:
        _shutdown(proc)
