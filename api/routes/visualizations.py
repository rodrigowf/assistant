"""REST endpoints for HTML files under ``context/public/``.

These are the artifacts produced by ``/create-viz`` and friends. They are
already reachable over HTTP — the SPA catch-all in ``api/app.py`` resolves
``context/public/<path>`` before falling through to the frontend bundle — so
this module only provides *discovery*: listing the files with a display title
so the frontend can show them in the sidebar alongside conversations.

Titles resolve in three steps, first hit wins:

1. ``.titles.json`` under the ``viz:<relative-path>`` key (a manual rename,
   shared with the session-title mechanism via ``SessionStore.set_title``).
2. The ``<title>`` tag inside the HTML.
3. A prettified version of the filename.

There is deliberately no watcher or cache: the frontend refetches on load,
matching how it treats the session list.
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from api.deps import get_store
from api.models import VisualizationInfoResponse
from manager.store import SessionStore
from utils.paths import get_public_dir

router = APIRouter(prefix="/api/visualizations", tags=["visualizations"])

# Key namespace inside .titles.json. Keeps manual viz titles from colliding
# with session UUIDs in the shared file.
TITLE_PREFIX = "viz:"

# Only the head of each file is read when sniffing <title> — the largest
# visualization is ~65 KB and the tag is always in the first block.
_HEAD_BYTES = 8192

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def title_key(rel_path: str) -> str:
    """The ``.titles.json`` key for a visualization's relative path."""
    return f"{TITLE_PREFIX}{rel_path}"


def _extract_html_title(path: Path) -> str | None:
    """Return the <title> text of an HTML file, or None if absent/unreadable."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(_HEAD_BYTES)
    except OSError:
        return None
    match = _TITLE_RE.search(head)
    if not match:
        return None
    # Collapse whitespace — titles are often pretty-printed across lines.
    title = " ".join(match.group(1).split())
    return title or None


def _prettify(rel_path: str) -> str:
    """Fallback title derived from the filename.

    ``visualizations/berlin-destino.html`` → ``Berlin Destino``.  Files named
    ``index.html`` borrow their parent directory instead, so the handful of
    ``<dir>/index.html`` artifacts don't all render as "Index".
    """
    path = Path(rel_path)
    stem = path.stem
    if stem == "index" and path.parent != Path("."):
        stem = path.parent.name
    return re.sub(r"[-_]+", " ", stem).strip().title() or rel_path


def _birth_times(paths: list[Path]) -> dict[Path, float]:
    """Best-effort file birth times via ``stat -c %W``.

    Python's ``os.stat`` exposes no ``st_birthtime`` on Linux, but the ext4
    inode records one and GNU coreutils surfaces it.  One batched subprocess
    covers every file; anything that fails (non-GNU stat, unsupported
    filesystem, %W of 0) is simply omitted and the caller falls back to mtime.
    """
    if not paths:
        return {}
    try:
        proc = subprocess.run(
            ["stat", "-c", "%W %n", *[str(p) for p in paths]],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}

    result: dict[Path, float] = {}
    for line in proc.stdout.splitlines():
        birth, _, name = line.partition(" ")
        if not name:
            continue
        try:
            value = int(birth)
        except ValueError:
            continue
        # 0 = "unknown" per coreutils; '?' already fails the int() above.
        if value > 0:
            result[Path(name)] = float(value)
    return result


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def scan_visualizations(store: SessionStore) -> list[VisualizationInfoResponse]:
    """Walk ``context/public/`` for HTML files and resolve display metadata."""
    public_dir = get_public_dir()
    if not public_dir.is_dir():
        return []
    public_root = public_dir.resolve()

    files: list[Path] = []
    for path in public_dir.rglob("*.html"):
        if not path.is_file():
            continue
        # Skip anything reachable only by escaping the public dir via a
        # symlink — those would 404 on the static route anyway.
        try:
            if not path.resolve().is_relative_to(public_root):
                continue
        except OSError:
            continue
        files.append(path)

    titles = store.get_titles()
    births = _birth_times(files)

    entries: list[VisualizationInfoResponse] = []
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        rel_path = path.relative_to(public_dir).as_posix()
        title = (
            titles.get(title_key(rel_path))
            or _extract_html_title(path)
            or _prettify(rel_path)
        )
        entries.append(
            VisualizationInfoResponse(
                path=rel_path,
                url=f"/{rel_path}",
                title=title,
                created=_iso(births.get(path, stat.st_mtime)),
                modified=_iso(stat.st_mtime),
                size=stat.st_size,
            )
        )

    # Sort by mtime, newest first. Deliberately not by birth time: files
    # arriving via context-sync get a birth time of "when rsync copied it",
    # whereas rsync preserves mtime, so mtime is the faithful authoring date.
    entries.sort(key=lambda e: e.modified, reverse=True)
    return entries


@router.get("", response_model=list[VisualizationInfoResponse])
def list_visualizations(store: SessionStore = Depends(get_store)):
    return scan_visualizations(store)


@router.patch("/rename", status_code=204)
def rename_visualization(body: dict, store: SessionStore = Depends(get_store)):
    """Set a manual title for a visualization, keyed by its relative path.

    Path is taken from the body rather than the URL so that slashes in the
    relative path don't need escaping.
    """
    rel_path = (body.get("path") or "").strip()
    title = (body.get("title") or "").strip()
    if not rel_path:
        raise HTTPException(400, detail="path is required")
    if not title:
        raise HTTPException(400, detail="title is required")

    public_dir = get_public_dir()
    public_root = public_dir.resolve()
    candidate = (public_dir / rel_path).resolve()
    # Traversal guard: the target must be a real HTML file under public/.
    if not candidate.is_relative_to(public_root) or not candidate.is_file():
        raise HTTPException(404, detail=f"Visualization {rel_path!r} not found")

    store.set_title(title_key(rel_path), title)
