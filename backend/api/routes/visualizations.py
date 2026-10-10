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

There is no cache: clients refetch on load and when
``api/content_watcher.py`` pushes ``visualization_changed`` (spec 12 §9.3).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import shutil
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException

from api.deps import get_store
from api.models import VisualizationInfoResponse
from manager.store import SessionStore
from utils.paths import get_public_dir

logger = logging.getLogger(__name__)

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


# ---------------------------------------------------------------------------
# Show on TV (BX-2, IA §9.4)
#
# Wraps the mechanism the /create-viz and /tv-remote skills use: open the
# visualization's network URL in the TvServerHub WebView on the Fire TV via
# ``adb shell am start -n com.example.tvserverhub/.WebPageViewActivity -e url``.
# Never raises on TV problems — the result is always ``{ok, message}``.
# ---------------------------------------------------------------------------

_TV_ACTIVITY = "com.example.tvserverhub/.WebPageViewActivity"
_ADB_TIMEOUT_S = 10.0


def _cast_base_url() -> str:
    """Base URL the TV loads visualizations from.

    ``VIZ_CAST_BASE_URL`` overrides (needed on dev machines, where the
    public files are not behind nginx).  Otherwise this follows the skill's
    server logic: nginx on 443 of this machine's LAN IP serves
    ``context/public/`` at the URL root.
    """
    override = os.environ.get("VIZ_CAST_BASE_URL", "").strip()
    if override:
        return override.rstrip("/")
    return f"https://{_lan_ip()}"


def _lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.0.1", 80))  # no packet is sent for UDP connect
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _resolve_visualization(rel_path: str) -> str | None:
    """Return the normalized relative path if *rel_path* names an entry of
    the visualization list (an ``.html`` file inside ``context/public/``,
    not escaping it via ``..`` or a symlink), else None."""
    rel_path = (rel_path or "").strip().lstrip("/")
    if not rel_path or not rel_path.endswith(".html"):
        return None
    public_dir = get_public_dir()
    if not public_dir.is_dir():
        return None
    public_root = public_dir.resolve()
    candidate = public_dir / rel_path
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if not resolved.is_relative_to(public_root) or not resolved.is_file():
        return None
    # Normalize ("a/./b.html" → "a/b.html") and reject ".." segments: list
    # entries never contain them, even when they resolve back inside.
    try:
        normalized = candidate.absolute().relative_to(public_dir.absolute())
    except ValueError:
        return None
    if ".." in normalized.parts:
        return None
    return normalized.as_posix()


async def _run_adb(*args: str, timeout: float = _ADB_TIMEOUT_S) -> tuple[int, str, str]:
    """Run ``adb <args>`` without blocking the loop.  Raises
    FileNotFoundError (no adb) or asyncio.TimeoutError."""
    proc = await asyncio.create_subprocess_exec(
        "adb", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise
    return (
        proc.returncode or 0,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


async def _find_fire_tv() -> tuple[str | None, str]:
    """Return ``(serial, reason)`` of the connected Fire TV.

    ``FIRE_TV_ADB_SERIAL`` pins the device; otherwise the first connected
    device whose manufacturer is Amazon is used, so a phone attached over
    adb is never targeted by mistake.
    """
    if shutil.which("adb") is None:
        return None, "adb is not installed on the server"
    try:
        _, out, _ = await _run_adb("devices")
    except (OSError, asyncio.TimeoutError):
        return None, "adb did not respond"
    serials = [
        parts[0] for parts in (line.split() for line in out.splitlines()[1:])
        if len(parts) >= 2 and parts[1] == "device"
    ]
    pinned = os.environ.get("FIRE_TV_ADB_SERIAL", "").strip()
    if pinned:
        if pinned in serials:
            return pinned, ""
        return None, f"Fire TV {pinned} is not connected"
    for serial in serials:
        try:
            _, maker, _ = await _run_adb(
                "-s", serial, "shell", "getprop", "ro.product.manufacturer",
                timeout=5.0,
            )
        except (OSError, asyncio.TimeoutError):
            continue
        if maker.strip().lower() == "amazon":
            return serial, ""
    return None, "No Fire TV connected over adb"


@router.get("/cast")
async def cast_capability():
    """Capability probe for "Show on TV": ``{available, reason}``."""
    serial, reason = await _find_fire_tv()
    return {"available": serial is not None, "reason": reason}


@router.post("/cast")
async def cast_visualization(body: dict):
    """Display a visualization on the Fire TV.  Body: ``{path}`` — the
    ``path`` field of an entry from ``GET /api/visualizations``."""
    rel_path = _resolve_visualization(str(body.get("path") or ""))
    if rel_path is None:
        raise HTTPException(404, detail="Visualization not found")

    serial, reason = await _find_fire_tv()
    if serial is None:
        return {"ok": False, "message": reason}

    url = f"{_cast_base_url()}/{quote(rel_path, safe='/')}"
    try:
        code, out, err = await _run_adb(
            "-s", serial, "shell", "am", "start",
            "-n", _TV_ACTIVITY, "-e", "url", shlex.quote(url),
        )
    except asyncio.TimeoutError:
        return {"ok": False, "message": "The TV did not respond in time"}
    except OSError as exc:
        return {"ok": False, "message": f"adb failed: {exc}"}
    # ``am start`` exits 0 even when the activity is missing; it reports
    # failures as "Error: ..." on stdout/stderr instead.
    output = (out + err).strip()
    if code != 0 or any(
        line.lstrip().startswith("Error") for line in output.splitlines()
    ):
        logger.warning("TV cast of %s failed: %s", rel_path, output)
        return {"ok": False, "message": output.splitlines()[-1] if output else "adb failed"}
    return {"ok": True, "message": f"Showing on TV: {url}"}
