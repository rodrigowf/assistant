"""Subprocess probe — answers "is this chroma dir healthy?" without crashing the parent.

Returns a result dict:
  {
    "ok": bool,
    "exit_code": int,         # -SIGNO if killed by signal
    "stderr": str,
    "details": {...},          # if ok=True: counts and one sample query
  }

The probe runs in a child subprocess. SIGSEGV from chroma's rust binding
becomes exit code -11 (SIGSEGV) or 139 in shell terms; we surface that
distinctly.
"""
from __future__ import annotations

import json
import signal
import subprocess
import sys
import textwrap
from pathlib import Path


PROBE_SCRIPT = textwrap.dedent('''
    import json, os, random, sys, faulthandler
    faulthandler.enable()
    sys.path.insert(0, {project_path!r})

    chroma_path = {chroma_path!r}
    collection_name = {collection_name!r}

    import chromadb
    client = chromadb.PersistentClient(path=chroma_path)
    try:
        col = client.get_collection(collection_name)
    except Exception as e:
        print(json.dumps({{"stage": "get_collection", "error": str(e), "type": type(e).__name__}}))
        sys.exit(2)
    try:
        count = col.count()
    except Exception as e:
        print(json.dumps({{"stage": "count", "error": str(e), "type": type(e).__name__}}))
        sys.exit(3)

    # Query with a dummy vector to exercise hnsw read path.
    rng = random.Random(0)
    emb = [rng.random() for _ in range(384)]
    try:
        r = col.query(query_embeddings=[emb], n_results=min(3, count))
        n_returned = len(r["documents"][0])
    except Exception as e:
        print(json.dumps({{"stage": "query", "error": str(e), "type": type(e).__name__, "count": count}}))
        sys.exit(4)

    print(json.dumps({{"ok": True, "count": count, "query_returned": n_returned}}))
''')


def probe_collection(chroma_dir: Path, collection_name: str = "history", timeout: float = 30.0, python: str | None = None) -> dict:
    project_path = str(Path(__file__).resolve().parent.parent.parent)
    script = PROBE_SCRIPT.format(
        project_path=project_path,
        chroma_path=str(chroma_dir),
        collection_name=collection_name,
    )
    try:
        result = subprocess.run(
            [python or sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        return {"ok": False, "exit_code": -1, "stderr": "timeout", "details": {}, "signal": None}

    out = {
        "ok": False,
        "exit_code": result.returncode,
        "stderr": result.stderr[-500:] if result.stderr else "",
        "details": {},
        "signal": None,
    }
    if result.returncode < 0:
        out["signal"] = signal.Signals(-result.returncode).name
    elif result.returncode == 139:
        out["signal"] = "SIGSEGV"
        out["exit_code"] = -11
    try:
        last_line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "{}"
        out["details"] = json.loads(last_line)
        out["ok"] = bool(out["details"].get("ok"))
    except (json.JSONDecodeError, IndexError):
        pass

    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("chroma_dir")
    ap.add_argument("--collection", default="history")
    ap.add_argument("--python", default=None, help="Python interpreter to use")
    args = ap.parse_args()
    r = probe_collection(Path(args.chroma_dir), args.collection, python=args.python)
    print(json.dumps(r, indent=2))
