"""Programmatic corruption of a chroma 1.x index, for repair-tier testing.

Each `corrupt_*` function takes a path to a chroma dir (the parent of
`chroma.sqlite3` and the segment UUID dirs) and produces ONE known kind
of damage. Functions return a description string that names the mode so
the test matrix can label rows.

What we corrupt and why:

- truncate_data_level0       — partial write of the main hnsw graph. Mimics
                                SIGKILL mid-flush. Per chroma#6975, this
                                breaks the on-disk graph but sqlite vectors
                                are intact.
- zero_data_level0           — total zero-out of the hnsw graph (worst of
                                that class).
- truncate_length_bin        — partial write of the per-element count file.
                                Per chroma#7069 this surfaces as "Error
                                loading hnsw index" -> SIGSEGV.
- garbage_length_bin         — write IEEE-754-looking float bytes into the
                                u32 count slots. Reproduces #7069's exact
                                failure mode.
- truncate_header            — drop bytes from header.bin. Per #7069 thread
                                this can crash the rust loader at open.
- delete_data_level0         — file goes missing. Power loss + fsync gap.
- corrupt_one_sqlite_vector  — flip bytes in ONE embedding row. Tests
                                whether per-chunk repair (Tier 0) catches
                                a SQL-resident bad value.
- delete_segment_dir         — whole segment dir gone but sqlite intact.
                                The clean case for SQL replay (Tier 2).
- corrupt_sqlite_header      — overwrite SQLite magic. Reproduces "sqlite
                                also broken" so Tier 3 (full re-embed) is
                                the only option.

The functions operate on the chroma dir IN PLACE. The caller copies the
baseline first.
"""
from __future__ import annotations

import shutil
import sqlite3
import struct
from pathlib import Path


def _find_segment_dir(chroma_dir: Path) -> Path:
    """Return the (one) segment dir under chroma_dir."""
    dirs = [p for p in chroma_dir.iterdir() if p.is_dir()]
    if len(dirs) != 1:
        raise RuntimeError(f"expected exactly 1 segment dir, found {len(dirs)}: {dirs}")
    return dirs[0]


def truncate_data_level0(chroma_dir: Path, keep_fraction: float = 0.5) -> str:
    seg = _find_segment_dir(chroma_dir)
    p = seg / "data_level0.bin"
    size = p.stat().st_size
    new_size = int(size * keep_fraction)
    with open(p, "r+b") as f:
        f.truncate(new_size)
    return f"truncate_data_level0(keep={keep_fraction}; {size}->{new_size}B)"


def zero_data_level0(chroma_dir: Path) -> str:
    seg = _find_segment_dir(chroma_dir)
    p = seg / "data_level0.bin"
    size = p.stat().st_size
    with open(p, "r+b") as f:
        f.write(b"\x00" * size)
    return f"zero_data_level0({size}B)"


def truncate_length_bin(chroma_dir: Path, keep_fraction: float = 0.5) -> str:
    seg = _find_segment_dir(chroma_dir)
    p = seg / "length.bin"
    size = p.stat().st_size
    new_size = int(size * keep_fraction)
    new_size -= new_size % 4  # keep aligned to u32
    with open(p, "r+b") as f:
        f.truncate(new_size)
    return f"truncate_length_bin(keep={keep_fraction}; {size}->{new_size}B)"


def garbage_length_bin(chroma_dir: Path) -> str:
    """Overwrite length.bin entries with bytes that look like floats — per
    chroma#7069 this reproduces "1+ billion elements" reports.
    """
    seg = _find_segment_dir(chroma_dir)
    p = seg / "length.bin"
    size = p.stat().st_size
    # write IEEE-754 doubles -> read back as u32 will be huge numbers
    payload = struct.pack("<d", 3.14159) * (size // 8 + 1)
    with open(p, "r+b") as f:
        f.write(payload[:size])
    return f"garbage_length_bin({size}B)"


def truncate_header(chroma_dir: Path) -> str:
    seg = _find_segment_dir(chroma_dir)
    p = seg / "header.bin"
    size = p.stat().st_size
    with open(p, "r+b") as f:
        f.truncate(size // 2)
    return f"truncate_header({size}->{size // 2}B)"


def delete_data_level0(chroma_dir: Path) -> str:
    seg = _find_segment_dir(chroma_dir)
    p = seg / "data_level0.bin"
    p.unlink()
    return "delete_data_level0"


def delete_segment_dir(chroma_dir: Path) -> str:
    seg = _find_segment_dir(chroma_dir)
    shutil.rmtree(seg)
    return f"delete_segment_dir({seg.name})"


def corrupt_sqlite_header(chroma_dir: Path) -> str:
    """Overwrite SQLite magic. The DB becomes unreadable — Tier 3 only."""
    p = chroma_dir / "chroma.sqlite3"
    with open(p, "r+b") as f:
        f.seek(0)
        f.write(b"X" * 16)
    return "corrupt_sqlite_header"


def corrupt_one_sqlite_vector(chroma_dir: Path) -> str:
    """Flip bytes in ONE embedding's stored vector. Tier 0 should catch this.

    Embeddings live in `embeddings_queue` (the WAL) and in segment-specific
    tables. For chroma 1.x the durable copy is in `embedding_fulltext_search`
    (docs) and segment vector files (vectors). We deliberately target the
    sqlite-resident row representation if present; failing that, this is a
    no-op and we report so.
    """
    p = chroma_dir / "chroma.sqlite3"
    db = sqlite3.connect(str(p))
    # Inspect what tables hold per-chunk data on this chroma version.
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    # In chroma 1.x the embeddings table has an id->seq_id mapping; actual
    # vectors live in segment files. We can still corrupt the metadata row
    # to test whether reads catch it.
    if "embedding_metadata" in tables:
        first = db.execute("SELECT id, key, string_value FROM embedding_metadata WHERE key='file_path' LIMIT 1").fetchone()
        if first:
            row_id, key, val = first
            db.execute("UPDATE embedding_metadata SET string_value = ? WHERE id = ? AND key = ?",
                       (val + "_CORRUPTED_BY_HARNESS", row_id, key))
            db.commit()
            db.close()
            return f"corrupt_one_sqlite_vector(embedding_metadata id={row_id})"
    db.close()
    return "corrupt_one_sqlite_vector(no-op; embedding_metadata not present)"


ALL_CORRUPTIONS = [
    truncate_data_level0,
    zero_data_level0,
    truncate_length_bin,
    garbage_length_bin,
    truncate_header,
    delete_data_level0,
    delete_segment_dir,
    corrupt_sqlite_header,
    corrupt_one_sqlite_vector,
]
