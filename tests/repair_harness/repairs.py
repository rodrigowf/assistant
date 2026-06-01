"""Repair tier implementations — each runs in a SUBPROCESS so a chroma
SIGSEGV is recoverable.

Tier 0: per-chunk repair. Identify failing chunk IDs from sqlite, delete +
        re-add them via the collection client.
Tier 1: per-file repair. delete(where={"file_path": X}) + re-embed file X.
Tier 2: SQL-replay full rebuild. Read embeddings/docs/metadata from
        sqlite, delete the collection, recreate, batch-add.
Tier 3: full re-embed from source. Run embed.py from scratch against the
        original src dir.

All four return a string description so the matrix harness can label rows.
"""
from __future__ import annotations

import json
import shutil
import signal
import subprocess
import sys
import textwrap
from pathlib import Path


def _run_subprocess(script: str, timeout: float = 600.0) -> tuple[int, str, str]:
    """Run an isolated python -c script and return (exit, stdout, stderr)."""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    rc = proc.returncode
    if rc == 139:
        rc = -11
    return rc, proc.stdout, proc.stderr


# ── Tier 0 ──────────────────────────────────────────────────────────────────

T0_SCRIPT = textwrap.dedent('''
    import json, sys, random, faulthandler
    faulthandler.enable()
    sys.path.insert(0, {project_path!r})

    import chromadb
    client = chromadb.PersistentClient(path={chroma_path!r})
    col = client.get_collection({collection_name!r})

    # We do not yet know which IDs are bad; sample N random IDs and try to
    # `get` each one. Any that raise are quarantined.
    all_results = col.get(include=[])
    all_ids = all_results["ids"]
    rng = random.Random(0)
    sample = rng.sample(all_ids, min(50, len(all_ids)))

    bad = []
    for cid in sample:
        try:
            col.get(ids=[cid], include=["embeddings", "documents", "metadatas"])
        except Exception as e:
            bad.append((cid, type(e).__name__, str(e)[:120]))

    if not bad:
        print(json.dumps({{"tier": 0, "scanned": len(sample), "bad": 0, "noop": True}}))
        sys.exit(0)

    # For each bad ID, fetch its embedding/doc/metadata from a full collection
    # get (which DOES work for the rest), delete it, re-add it.
    full = col.get(include=["embeddings", "documents", "metadatas"])
    by_id = {{i: (e, d, m) for i, e, d, m in zip(full["ids"], full["embeddings"], full["documents"], full["metadatas"])}}

    repaired = 0
    for cid, _etype, _emsg in bad:
        if cid not in by_id:
            continue
        emb, doc, meta = by_id[cid]
        col.delete(ids=[cid])
        col.add(ids=[cid], embeddings=[emb], documents=[doc], metadatas=[meta])
        repaired += 1

    print(json.dumps({{"tier": 0, "scanned": len(sample), "bad": len(bad), "repaired": repaired}}))
''')


def tier0_per_chunk(chroma_dir: Path, collection_name: str, src_dir: Path) -> str:
    project_path = str(Path(__file__).resolve().parent.parent.parent)
    script = T0_SCRIPT.format(
        project_path=project_path,
        chroma_path=str(chroma_dir),
        collection_name=collection_name,
    )
    rc, stdout, stderr = _run_subprocess(script, timeout=60.0)
    if rc < 0:
        return f"tier0 crashed signal={signal.Signals(-rc).name}"
    if rc != 0:
        return f"tier0 exit={rc} stderr={stderr[-200:]}"
    try:
        info = json.loads(stdout.strip().splitlines()[-1])
        return f"tier0 {info}"
    except (json.JSONDecodeError, IndexError):
        return f"tier0 stdout={stdout[-200:]}"


# ── Tier 1 ──────────────────────────────────────────────────────────────────

T1_SCRIPT = textwrap.dedent('''
    import json, sys, faulthandler
    faulthandler.enable()
    sys.path.insert(0, {project_path!r})
    sys.path.insert(0, {scripts_path!r})

    import chromadb
    client = chromadb.PersistentClient(path={chroma_path!r})
    col = client.get_collection({collection_name!r})

    # Identify a file with chunks that fail to fetch; pick one such file.
    full = col.get(include=["metadatas"])
    file_paths = sorted({{m.get("file_path", "") for m in full["metadatas"] if m.get("file_path")}})

    affected = None
    for fp in file_paths:
        try:
            res = col.get(where={{"file_path": fp}}, include=["embeddings", "documents", "metadatas"])
            # Try fetching by IDs too; that's where SIGSEGV usually fires.
            if res["ids"]:
                col.get(ids=res["ids"][:5], include=["embeddings"])
        except Exception:
            affected = fp
            break
    if affected is None:
        # No file-level failures; pick the first file as a no-op probe.
        affected = file_paths[0] if file_paths else None
    if affected is None:
        print(json.dumps({{"tier": 1, "noop": True, "reason": "no files"}}))
        sys.exit(0)

    # Re-embed just that file.
    import embed, pathlib
    embed.INDEX_DIR = pathlib.Path({chroma_path!r})
    embed._clients.clear()
    embed._model = None
    embed.index_path(affected, collection_name={collection_name!r})

    new_count = col.count()
    print(json.dumps({{"tier": 1, "file": affected, "new_count": new_count}}))
''')


def tier1_per_file(chroma_dir: Path, collection_name: str, src_dir: Path) -> str:
    project_path = str(Path(__file__).resolve().parent.parent.parent)
    scripts_path = str(Path(project_path) / "default-scripts")
    script = T1_SCRIPT.format(
        project_path=project_path,
        scripts_path=scripts_path,
        chroma_path=str(chroma_dir),
        collection_name=collection_name,
    )
    rc, stdout, stderr = _run_subprocess(script, timeout=120.0)
    if rc < 0:
        return f"tier1 crashed signal={signal.Signals(-rc).name}"
    if rc != 0:
        return f"tier1 exit={rc} stderr={stderr[-200:]}"
    try:
        info = json.loads(stdout.strip().splitlines()[-1])
        return f"tier1 {info}"
    except (json.JSONDecodeError, IndexError):
        return f"tier1 stdout={stdout[-200:]}"


# ── Tier 2 ──────────────────────────────────────────────────────────────────

T2_SCRIPT = textwrap.dedent('''
    import json, sys, sqlite3, faulthandler
    faulthandler.enable()
    sys.path.insert(0, {project_path!r})

    chroma_path = {chroma_path!r}
    collection_name = {collection_name!r}

    # 1. Read embeddings + docs + metadata from sqlite directly, without
    #    opening chroma (so a corrupt hnsw segment can't crash us).
    db = sqlite3.connect("file:" + chroma_path + "/chroma.sqlite3?mode=ro", uri=True)

    # Find the collection id.
    row = db.execute("SELECT id FROM collections WHERE name = ?", (collection_name,)).fetchone()
    if not row:
        print(json.dumps({{"tier": 2, "error": "collection not in sqlite"}}))
        sys.exit(2)
    col_id = row[0]

    # Read straight from the embeddings_queue WAL. In chroma 1.x this
    # table durably stores ALL embeddings (vector bytes + metadata json,
    # which includes the doc text under "chroma:document") regardless of
    # which segment owns them, keyed by seq_id.
    rows = list(db.execute("""
        SELECT seq_id, id, vector, encoding, metadata
        FROM embeddings_queue
        WHERE operation = 0
        ORDER BY seq_id
    """))

    print(json.dumps({{"tier": 2, "stage": "read_sqlite", "rows": len(rows)}}), flush=True)

    if not rows:
        print(json.dumps({{"tier": 2, "error": "no embeddings in WAL — compaction may have evicted them"}}))
        sys.exit(2)

    import struct
    decoded = []
    for seq_id, cid, vec_bytes, encoding, meta_blob in rows:
        if encoding.upper() != "FLOAT32":
            print(json.dumps({{"tier": 2, "error": "unexpected encoding", "encoding": encoding}}))
            sys.exit(2)
        n = len(vec_bytes) // 4
        vec = list(struct.unpack(f"<{{n}}f", vec_bytes))
        meta = json.loads(meta_blob) if meta_blob else {{}}
        decoded.append((cid, vec, meta))

    print(json.dumps({{"tier": 2, "stage": "decoded", "n": len(decoded)}}), flush=True)
    db.close()

    # 2. Now: drop the collection (metadata-only, safe) and re-add from
    #    in-memory state.
    import chromadb
    client = chromadb.PersistentClient(path=chroma_path)
    try:
        client.delete_collection(collection_name)
    except Exception:
        pass
    col = client.create_collection(collection_name, metadata={{"hnsw:space": "cosine"}})

    # Add documents in batches. We need the document text too — pull from
    # the metadata's chroma:document key (chroma stores docs inside the
    # metadata blob in 1.x).
    BATCH = 500
    ids, embs, docs, metas = [], [], [], []
    for cid, vec, meta in decoded:
        doc = meta.pop("chroma:document", "")
        ids.append(cid)
        embs.append(vec)
        docs.append(doc)
        metas.append(meta)
        if len(ids) >= BATCH:
            col.add(ids=ids, embeddings=embs, documents=docs, metadatas=metas)
            ids, embs, docs, metas = [], [], [], []
    if ids:
        col.add(ids=ids, embeddings=embs, documents=docs, metadatas=metas)

    print(json.dumps({{"tier": 2, "stage": "done", "final_count": col.count()}}))
''')


def tier2_sql_replay(chroma_dir: Path, collection_name: str, src_dir: Path) -> str:
    project_path = str(Path(__file__).resolve().parent.parent.parent)
    script = T2_SCRIPT.format(
        project_path=project_path,
        chroma_path=str(chroma_dir),
        collection_name=collection_name,
    )
    rc, stdout, stderr = _run_subprocess(script, timeout=300.0)
    if rc < 0:
        return f"tier2 crashed signal={signal.Signals(-rc).name}"
    if rc != 0:
        return f"tier2 exit={rc} stderr={stderr[-200:]}"
    last_line = stdout.strip().splitlines()[-1] if stdout.strip() else "{}"
    try:
        info = json.loads(last_line)
        return f"tier2 {info}"
    except json.JSONDecodeError:
        return f"tier2 stdout={stdout[-200:]}"


# ── Tier 3 ──────────────────────────────────────────────────────────────────

T3_SCRIPT = textwrap.dedent('''
    import json, sys, shutil, faulthandler
    faulthandler.enable()
    sys.path.insert(0, {project_path!r})
    sys.path.insert(0, {scripts_path!r})

    chroma_path = {chroma_path!r}
    collection_name = {collection_name!r}
    src_dir = {src_dir!r}

    # Nuke the chroma dir entirely and re-embed.
    shutil.rmtree(chroma_path, ignore_errors=True)
    import os
    os.makedirs(chroma_path, exist_ok=True)

    import embed
    embed.INDEX_DIR = __import__("pathlib").Path(chroma_path)
    embed._clients.clear()
    embed._model = None
    embed.index_path(src_dir, collection_name=collection_name)

    import chromadb
    client = chromadb.PersistentClient(path=chroma_path)
    col = client.get_collection(collection_name)
    print(json.dumps({{"tier": 3, "final_count": col.count()}}))
''')


def tier3_full_reembed(chroma_dir: Path, collection_name: str, src_dir: Path) -> str:
    project_path = str(Path(__file__).resolve().parent.parent.parent)
    scripts_path = str(Path(project_path) / "default-scripts")
    script = T3_SCRIPT.format(
        project_path=project_path,
        scripts_path=scripts_path,
        chroma_path=str(chroma_dir),
        collection_name=collection_name,
        src_dir=str(src_dir),
    )
    rc, stdout, stderr = _run_subprocess(script, timeout=600.0)
    if rc < 0:
        return f"tier3 crashed signal={signal.Signals(-rc).name}"
    if rc != 0:
        return f"tier3 exit={rc} stderr={stderr[-200:]}"
    last_line = stdout.strip().splitlines()[-1] if stdout.strip() else "{}"
    try:
        info = json.loads(last_line)
        return f"tier3 {info}"
    except json.JSONDecodeError:
        return f"tier3 stdout={stdout[-200:]}"


ALL_REPAIRS = [
    tier0_per_chunk,
    tier1_per_file,
    tier2_sql_replay,
    tier3_full_reembed,
]
