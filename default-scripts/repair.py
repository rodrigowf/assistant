"""Repair toolkit for the chroma index.

Three pieces:
    validate_collection(index_dir, name)  -> {"healthy": bool, ...}
    wal_replay(index_dir, name)           -> {"recovered": int, ...}
    full_reembed(index_dir, name, src_dir, chunker, encoder) -> {...}
    auto_repair(index_dir, name, ...)     -> {"tier_used": str, ...}

All operations that could trigger chroma's rust binding to SIGSEGV run
in a child subprocess. The parent observes the exit code and surfaces
SIGSEGV as a recoverable error rather than dying.

Tier coverage (from empirical research, see tests/repair_harness/):
  - WAL replay recovers ONLY embeddings still resident in the
    embeddings_queue sqlite table. After chroma compacts (~every few
    hundred adds), those embeddings are dropped from sqlite and live
    only in segment files. So WAL replay is partial: best for fresh
    corruption right after a write burst.
  - Full re-embed re-derives every chunk from source. Always works,
    always slow (~2h for our history collection on Jetson).

The validator's probe runs the cheapest read that exercises hnsw:
get_collection(name).count() + a 1-result query with a dummy vector.
On chroma 1.5.x with a corrupted segment, count() raises InternalError
(possibly via SIGSEGV depending on the corruption mode). The
subprocess wrapper catches both.
"""
from __future__ import annotations

import json
import signal
import sqlite3
import struct
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Callable


# ── Validator ────────────────────────────────────────────────────────────────

_VALIDATE_SCRIPT = textwrap.dedent('''
    import json, sys, random, faulthandler
    faulthandler.enable()
    sys.path.insert(0, {project!r})

    import chromadb
    client = chromadb.PersistentClient(path={chroma!r})
    out = {{"healthy": False, "stage": "init"}}
    try:
        col = client.get_collection({name!r})
    except Exception as e:
        out["stage"] = "get_collection"
        out["error"] = f"{{type(e).__name__}}: {{e}}"
        print(json.dumps(out))
        sys.exit(0)
    try:
        n = col.count()
        out["count"] = n
    except Exception as e:
        out["stage"] = "count"
        out["error"] = f"{{type(e).__name__}}: {{e}}"
        print(json.dumps(out))
        sys.exit(0)
    if n == 0:
        out["healthy"] = True
        out["stage"] = "empty"
        print(json.dumps(out))
        sys.exit(0)
    try:
        rng = random.Random(0)
        emb = [rng.random() for _ in range(384)]
        col.query(query_embeddings=[emb], n_results=min(3, n))
        out["healthy"] = True
        out["stage"] = "ok"
    except Exception as e:
        out["stage"] = "query"
        out["error"] = f"{{type(e).__name__}}: {{e}}"
    print(json.dumps(out))
''')


def validate_collection(index_dir: Path, name: str, timeout: float = 60.0) -> dict[str, Any]:
    """Probe collection health in a subprocess. SIGSEGV becomes a clean
    "unhealthy" result rather than killing the parent."""
    project = str(Path(index_dir).resolve().parent.parent)
    script = _VALIDATE_SCRIPT.format(
        project=project,
        chroma=str(index_dir),
        name=name,
    )
    t0 = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"healthy": False, "stage": "timeout", "duration_s": time.time() - t0}

    rc = proc.returncode
    if rc == 139:
        rc = -11
    if rc < 0:
        return {
            "healthy": False,
            "stage": "signal",
            "signal": signal.Signals(-rc).name,
            "stderr": proc.stderr[-400:],
            "duration_s": time.time() - t0,
        }

    line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "{}"
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        data = {"healthy": False, "stage": "no_output", "stdout": proc.stdout[-400:]}
    data["duration_s"] = time.time() - t0
    return data


# ── WAL replay ───────────────────────────────────────────────────────────────

_WAL_REPLAY_SCRIPT = textwrap.dedent('''
    import json, sys, sqlite3, struct, faulthandler
    faulthandler.enable()
    sys.path.insert(0, {project!r})

    chroma = {chroma!r}
    name = {name!r}

    # Read the WAL in read-only mode FIRST, before we touch chroma.
    # If we drop the collection before reading, chroma drops the WAL
    # too — losing exactly the data we wanted to recover.
    db = sqlite3.connect("file:" + chroma + "/chroma.sqlite3?mode=ro", uri=True)
    rows = list(db.execute("""
        SELECT id, vector, encoding, metadata FROM embeddings_queue
        WHERE operation = 0 ORDER BY seq_id
    """))
    db.close()
    if not rows:
        print(json.dumps({{"recovered": 0, "reason": "WAL empty (compaction has run)"}}))
        sys.exit(0)

    decoded = []
    for cid, vec_bytes, encoding, meta_blob in rows:
        if encoding.upper() != "FLOAT32":
            continue
        n = len(vec_bytes) // 4
        vec = list(struct.unpack(f"<{{n}}f", vec_bytes))
        meta = json.loads(meta_blob) if meta_blob else {{}}
        doc = meta.pop("chroma:document", "")
        decoded.append((cid, vec, doc, meta))

    # Dedup by id (the WAL can have multiple add-ops per id from
    # re-indexing); keep the latest occurrence.
    by_id = {{}}
    for cid, vec, doc, meta in decoded:
        by_id[cid] = (vec, doc, meta)
    ids = list(by_id.keys())
    embs = [by_id[i][0] for i in ids]
    docs = [by_id[i][1] for i in ids]
    metas = [by_id[i][2] for i in ids]

    # Now we drop and recreate the collection. The WAL goes with the
    # drop, but we already have its contents in memory.
    import chromadb
    client = chromadb.PersistentClient(path=chroma)
    try:
        client.delete_collection(name)
    except Exception:
        pass
    col = client.create_collection(name, metadata={{"hnsw:space": "cosine", "hnsw:sync_threshold": 200}})

    BATCH = 500
    for i in range(0, len(ids), BATCH):
        col.add(
            ids=ids[i:i+BATCH], embeddings=embs[i:i+BATCH],
            documents=docs[i:i+BATCH], metadatas=metas[i:i+BATCH],
        )
    print(json.dumps({{"recovered": len(ids), "final_count": col.count()}}))
''')


def wal_replay(index_dir: Path, name: str, timeout: float = 300.0) -> dict[str, Any]:
    """Replay WAL-resident embeddings into a fresh collection. Partial:
    only recovers embeddings the WAL still holds (i.e., not yet
    compacted into segment files)."""
    project = str(Path(index_dir).resolve().parent.parent)
    script = _WAL_REPLAY_SCRIPT.format(project=project, chroma=str(index_dir), name=name)
    t0 = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"recovered": 0, "error": "timeout", "duration_s": time.time() - t0}

    rc = proc.returncode
    if rc == 139:
        rc = -11
    if rc < 0:
        return {
            "recovered": 0,
            "error": f"signal {signal.Signals(-rc).name}",
            "stderr": proc.stderr[-400:],
            "duration_s": time.time() - t0,
        }
    if rc != 0:
        return {"recovered": 0, "error": f"exit {rc}", "stderr": proc.stderr[-400:], "duration_s": time.time() - t0}

    last = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "{}"
    try:
        data = json.loads(last)
    except json.JSONDecodeError:
        data = {"recovered": 0, "error": f"no output: {proc.stdout[-200:]}"}
    data["duration_s"] = time.time() - t0
    return data


# ── Full re-embed ────────────────────────────────────────────────────────────
# Driven by index-memory.py, which already knows how to convert JSONL
# session files → chunks. We just delete the collection and let the
# next HistoryIndexer / MemoryWatcher tick rebuild from source.

def full_reembed_trigger(index_dir: Path, name: str) -> dict[str, Any]:
    """Drop the collection. Re-embedding happens out-of-band via the
    HistoryIndexer/MemoryWatcher background tasks in api/indexer.py
    (they detect the missing chunks via their hash check and rebuild).

    This is a stub: the actual re-embed work runs after this returns,
    asynchronously."""
    import chromadb
    t0 = time.time()
    try:
        client = chromadb.PersistentClient(path=str(index_dir))
        try:
            client.delete_collection(name)
        except Exception:
            pass
        client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine", "hnsw:sync_threshold": 200},
        )
        return {
            "triggered": True,
            "note": "collection reset; rebuild runs in the background indexer task",
            "duration_s": time.time() - t0,
        }
    except Exception as e:
        return {
            "triggered": False,
            "error": f"{type(e).__name__}: {e}",
            "duration_s": time.time() - t0,
        }


# ── Auto-repair dispatcher ───────────────────────────────────────────────────

def auto_repair(
    index_dir: Path,
    name: str,
    *,
    preferred_tier: str = "auto",
) -> dict[str, Any]:
    """Try cheap tiers first, escalate to expensive ones.

    preferred_tier:
      "auto"         — try wal_replay, then full_reembed_trigger.
      "wal_replay"   — only try WAL replay; report what's missing.
      "full_reembed" — skip WAL replay; drop and trigger rebuild.
    """
    before = validate_collection(index_dir, name)
    pre_count = before.get("count", 0)

    if preferred_tier in ("auto", "wal_replay"):
        wal = wal_replay(index_dir, name)
        if wal.get("recovered", 0) > 0 and "error" not in wal:
            after = validate_collection(index_dir, name)
            if after["healthy"]:
                return {
                    "tier_used": "wal_replay",
                    "before": pre_count,
                    "after": after.get("count", 0),
                    "wal": wal,
                    "error": None,
                }
        if preferred_tier == "wal_replay":
            return {
                "tier_used": "wal_replay",
                "before": pre_count,
                "after": 0,
                "wal": wal,
                "error": wal.get("error") or "WAL replay insufficient",
            }

    # Escalate.
    full = full_reembed_trigger(index_dir, name)
    after = validate_collection(index_dir, name)
    return {
        "tier_used": "full_reembed",
        "before": pre_count,
        "after": after.get("count", 0),
        "full": full,
        "error": None if full.get("triggered") else full.get("error"),
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

def _main():
    import argparse
    ap = argparse.ArgumentParser(description="Chroma index repair toolkit")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_val = sub.add_parser("validate", help="Probe collection health")
    p_val.add_argument("collection")
    p_val.add_argument("--index-dir", default=None)

    p_wal = sub.add_parser("wal-replay", help="Recover WAL-resident embeddings")
    p_wal.add_argument("collection")
    p_wal.add_argument("--index-dir", default=None)

    p_auto = sub.add_parser("auto", help="Auto-pick cheapest repair tier")
    p_auto.add_argument("collection")
    p_auto.add_argument("--tier", default="auto", choices=["auto", "wal_replay", "full_reembed"])
    p_auto.add_argument("--index-dir", default=None)

    args = ap.parse_args()
    if args.index_dir:
        index_dir = Path(args.index_dir)
    else:
        from utils.paths import get_index_dir
        index_dir = get_index_dir() / "chroma"

    if args.cmd == "validate":
        print(json.dumps(validate_collection(index_dir, args.collection), indent=2))
    elif args.cmd == "wal-replay":
        print(json.dumps(wal_replay(index_dir, args.collection), indent=2))
    elif args.cmd == "auto":
        print(json.dumps(auto_repair(index_dir, args.collection, preferred_tier=args.tier), indent=2))


if __name__ == "__main__":
    SCRIPT_DIR = Path(__file__).resolve().parent
    PROJECT_DIR = SCRIPT_DIR.parent
    sys.path.insert(0, str(PROJECT_DIR))
    _main()
