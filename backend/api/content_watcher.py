"""Change notifications for visualizations and memory files (spec 12 §9.3).

One ``watchfiles`` loop watches ``context/public/`` (visualizations and their
assets), ``context/memory/`` and the directories the memory tree links into
(``context/memory/archie`` → ``docs/``; inotify does not follow the symlink).
Each coalesced batch becomes at most two frames, pushed to every pool watcher
— i.e. every orchestrator WebSocket (spec 12 §3.7):

    {"type": "visualization_changed",
     "visualizations": [{"path": "avatar-pipeline/index.html", "kind": "modified"}],
     "files": [{"path": "avatar-pipeline/data/state.json", "kind": "modified"}]}

    {"type": "memory_changed",
     "changes": [{"path": "archie/specs/12-client-protocol.md", "kind": "modified"}]}

Paths are relative to ``context/public/`` (the ``path`` of ``GET
/api/visualizations``) and to ``context/memory/`` (the ``path`` of the memory
tree; files under ``docs/`` are reported under their ``archie/`` alias).
``kind`` is ``created`` | ``modified`` | ``deleted`` and is advisory: an
atomic save or an rsync from context-sync writes a temp file and renames it
over the target, which inotify reports as a create.

``visualizations`` maps the changed files to the visualizations a client may
have open, so a multi-file visualization reloads when any of its files
changes (:func:`affected_visualizations`).

Bursts are coalesced by ``awatch`` itself (``step``: a quiet gap closes the
batch; ``debounce``: the batch is never held longer), which keeps an rsync of
a whole folder down to one or two frames on the Jetson. Temp and editor files
(rsync's ``.name.XXXXXX``, ``*.swp``, ``*.tmp.*`` …) never reach a batch.

Markdown changes under the memory tree also wake the memory indexer
(``on_memory_markdown``), so the backend runs a single watcher over the tree.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path, PurePosixPath

from utils.paths import get_memory_dir, get_memory_link_targets, get_public_dir

logger = logging.getLogger(__name__)

Broadcast = Callable[[dict], Awaitable[None]]

# A quiet gap of STEP_MS closes a batch; no batch is held longer than DEBOUNCE_MS.
DEBOUNCE_MS = 1500
STEP_MS = 300

# Used only when inotify is out of watches (see run()).
POLL_DELAY_MS = 2000
# A missing root (context/public/, context/memory/) is looked for again this often.
ROOTS_RECHECK_S = 30
# Retry after a watcher error: 5 s, doubling, at most 5 min.
ERROR_BACKOFF_S = 5
ERROR_BACKOFF_MAX_S = 300

HTML_SUFFIXES = (".html", ".htm")

# Never reported: dotfiles (rsync's ".name.XXXXXX", ".#lock", ".DS_Store"),
# editor backups/swap files, partial downloads, atomic-write temp files
# ("name.tmp", "name.tmp.<pid>.<ts>") and vim's write probe "4913".
_IGNORED_NAME = re.compile(
    r"^\.|~$|\.(?:sw[a-z]|tmp|temp|part|crdownload|bak)$|\.tmp\.|^4913$", re.IGNORECASE
)
_IGNORED_DIRS = frozenset({"node_modules", "__pycache__", ".git", ".venv"})

# Only the head of a sibling page is searched for a reference to a changed asset.
_REFERENCE_SCAN_BYTES = 512 * 1024

_KIND = {1: "created", 2: "modified", 3: "deleted"}  # watchfiles.Change values


def is_ignored(rel_path: str | PurePosixPath) -> bool:
    """True for temp/editor files and anything inside an ignored or hidden
    directory. *rel_path* is relative to a watched root (the folders above the
    root may be hidden themselves, e.g. a checkout under ``~/.local``)."""
    parts = PurePosixPath(rel_path).parts
    if not parts:
        return True
    if _IGNORED_NAME.search(parts[-1]):
        return True
    return any(p in _IGNORED_DIRS or p.startswith(".") for p in parts[:-1])


def _relative(path: Path, roots: Iterable[Path]) -> PurePosixPath | None:
    """*path* relative to the first root containing it."""
    for root in roots:
        if path.is_relative_to(root):
            return PurePosixPath(path.relative_to(root).as_posix())
    return None


def _merge_kind(old: str | None, new: str) -> str:
    """Fold two kinds for the same path within one batch (temp+rename, create+modify …)."""
    if old is None or old == new:
        return new
    if new == "deleted" or old == "deleted":
        # "deleted then created" is a replace; "created then deleted" a vanished temp.
        return new if new == "deleted" else "modified"
    if "created" in (old, new):
        return "created"
    return new


def _refers_to(page: Path, name: str) -> bool:
    """*page* names the file *name* as a whole path segment (``a.js`` is not
    found in ``data.js`` or ``a.json``)."""
    pattern = re.compile(r"(?<![\w.-])" + re.escape(name) + r"(?![\w-]|\.\w)")
    try:
        with open(page, "r", encoding="utf-8", errors="replace") as f:
            return pattern.search(f.read(_REFERENCE_SCAN_BYTES)) is not None
    except OSError:
        return False


def affected_visualizations(public_dir: Path, rel_path: str) -> list[str]:
    """The visualizations (``.html`` paths relative to *public_dir*) a change to
    *rel_path* concerns.

    - An ``.html`` file is its own visualization.
    - An asset (js/css/json/data …) concerns every page in its own folder or an
      ancestor folder (below the public root) whose source mentions the asset's
      file name; at the public root, only pages in the same folder are searched.
    - Failing that, the nearest ``index.html`` above it (a folder visualization:
      ``index.html`` + ``app.js`` + ``data/…``, where only ``app.js`` names the data).
    - Otherwise none: the client still refreshes its list.
    """
    rel = PurePosixPath(rel_path)
    if rel.suffix.lower() in HTML_SUFFIXES:
        return [rel.as_posix()]

    folders: list[PurePosixPath] = []
    folder = rel.parent
    while True:
        folders.append(folder)
        if folder == PurePosixPath("."):
            break
        folder = folder.parent

    found: list[str] = []
    for i, folder in enumerate(folders):
        directory = public_dir / folder
        # The public root holds unrelated pages: search it only for a root-level asset.
        if folder == PurePosixPath(".") and i > 0:
            break
        try:
            pages = sorted(p for p in directory.iterdir() if p.suffix.lower() in HTML_SUFFIXES and p.is_file())
        except OSError:
            continue
        for page in pages:
            if _refers_to(page, rel.name):
                found.append((folder / page.name).as_posix())
    if found:
        return found

    for folder in folders:
        if folder == PurePosixPath("."):
            break
        if (public_dir / folder / "index.html").is_file():
            return [(folder / "index.html").as_posix()]
    return []


class ContentWatcher:
    """Watches public/ and the memory tree; pushes ``visualization_changed`` /
    ``memory_changed`` through *broadcast* and wakes the memory indexer."""

    def __init__(
        self,
        broadcast: Broadcast,
        on_memory_markdown: Callable[[], None] | None = None,
        debounce_ms: int = DEBOUNCE_MS,
        step_ms: int = STEP_MS,
    ):
        self._broadcast = broadcast
        self._on_memory_markdown = on_memory_markdown
        self._debounce_ms = debounce_ms
        self._step_ms = step_ms
        self._running = True
        # Eager Event so stop() before run() reaches awatch is race-free.
        self._stop_event = asyncio.Event()

    @staticmethod
    def roots() -> tuple[Path | None, list[tuple[Path, str]]]:
        """``(public_root, [(memory_root, memory_prefix), …])``, resolved.

        The memory roots are context/memory/ (prefix "") and every link target
        under the alias a symlink in the memory tree gives it (``docs/`` →
        "archie/"). Link targets with no symlink pointing at them are skipped.
        """
        public = get_public_dir()
        public_root = public.resolve() if public.is_dir() else None
        memory_dir = get_memory_dir()
        memory_roots: list[tuple[Path, str]] = []
        if memory_dir.is_dir():
            memory_roots.append((memory_dir.resolve(), ""))
            targets = {t.resolve() for t in get_memory_link_targets() if t.is_dir()}
            try:
                links = [e for e in memory_dir.iterdir() if e.is_symlink()]
            except OSError:
                links = []
            for link in sorted(links):
                try:
                    target = link.resolve()
                except OSError:
                    continue
                if target in targets:
                    memory_roots.append((target, f"{link.name}/"))
        return public_root, memory_roots

    @staticmethod
    def classify(
        changes: Iterable[tuple[int, str]],
        public_root: Path | None,
        memory_roots: list[tuple[Path, str]],
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Split a watchfiles batch into ``({public_rel: kind}, {memory_rel: kind})``.

        Memory entries are markdown only; public entries are any file. Ignored
        names (temp/editor files) are dropped; a path seen twice in one batch
        gets the folded kind (:func:`_merge_kind`).
        """
        public: dict[str, str] = {}
        memory: dict[str, str] = {}
        for change, raw in changes:
            kind = _KIND.get(int(change))
            if kind is None:
                continue
            path = Path(raw)
            if public_root is not None and path.is_relative_to(public_root):
                rel = PurePosixPath(path.relative_to(public_root).as_posix())
                if is_ignored(rel) or (kind != "deleted" and path.is_dir()):
                    continue
                # A deleted folder cannot be stat'ed any more; its files are
                # reported one by one, so a suffix-less deletion is skipped.
                if kind == "deleted" and not rel.suffix:
                    continue
                public[rel.as_posix()] = _merge_kind(public.get(rel.as_posix()), kind)
                continue
            if not path.name.lower().endswith(".md"):
                continue
            # Longest root first: docs/ is never inside context/memory/, but be exact anyway.
            for root, prefix in sorted(memory_roots, key=lambda r: len(str(r[0])), reverse=True):
                if path.is_relative_to(root):
                    rel = PurePosixPath(path.relative_to(root).as_posix())
                    if not is_ignored(rel):
                        key = prefix + rel.as_posix()
                        memory[key] = _merge_kind(memory.get(key), kind)
                    break
        return public, memory

    @staticmethod
    def visualization_frame(public_root: Path, files: dict[str, str]) -> dict | None:
        """The ``visualization_changed`` frame for one batch (blocking: reads pages)."""
        if not files:
            return None
        affected: dict[str, str] = {}
        for rel, kind in files.items():
            for viz in affected_visualizations(public_root, rel):
                # A page's own create/delete wins; an asset change is a "modified" of its page.
                if viz == rel:
                    affected[viz] = kind
                else:
                    affected.setdefault(viz, "modified")
        return {
            "type": "visualization_changed",
            "visualizations": [{"path": p, "kind": k} for p, k in sorted(affected.items())],
            "files": [{"path": p, "kind": k} for p, k in sorted(files.items())],
        }

    async def _sleep(self, seconds: float) -> None:
        """Sleep, or return early on :meth:`stop`."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def run(self) -> None:
        try:
            from watchfiles import awatch
        except ImportError:
            logger.error("watchfiles not installed, content watcher disabled")
            return

        # awatch wraps a blocking Rust call in anyio.to_thread; that thread
        # only honors stop_event, not asyncio cancellation. If we don't set
        # the event in the cancel path, anyio's CancelScope retries
        # cancellation forever and burns 100% CPU. Set it in finally so it
        # fires for both CancelledError and any other exit.
        polling = False
        failures = 0
        try:
            while self._running and not self._stop_event.is_set():
                # Roots are recomputed on every (re)start: context/public/,
                # context/memory/ or the archie link may appear after boot.
                public_root, memory_roots = self.roots()
                watched = [p for p in [public_root, *(r for r, _ in memory_roots)] if p is not None]
                if not watched:
                    await self._sleep(ROOTS_RECHECK_S)
                    continue
                # Some root still missing: wake up now and then to pick it up.
                complete = public_root is not None and bool(memory_roots)
                logger.info("Content watcher started: %s", ", ".join(str(p) for p in watched))

                def keep(_change, raw: str, watched=watched) -> bool:
                    rel = _relative(Path(raw), watched)
                    return rel is None or not is_ignored(rel)

                try:
                    async for changes in awatch(
                        *watched,
                        watch_filter=keep,
                        debounce=self._debounce_ms,
                        step=self._step_ms,
                        stop_event=self._stop_event,
                        force_polling=polling or None,
                        poll_delay_ms=POLL_DELAY_MS,
                        rust_timeout=None if complete else int(ROOTS_RECHECK_S * 1000),
                        yield_on_timeout=not complete,
                    ):
                        if not self._running:
                            break
                        failures = 0
                        if not changes:
                            if self.roots() != (public_root, memory_roots):
                                break  # a root appeared: restart with it
                            continue
                        try:
                            await self._handle(changes, public_root, memory_roots)
                        except Exception:
                            logger.exception("Content watcher: failed to handle a batch")
                except Exception as e:
                    if not self._running:
                        return
                    # The per-user inotify watch budget is shared with every other
                    # process (an IDE can take all of it): poll instead of going dark.
                    if not polling and "limit" in str(e).lower():
                        logger.warning("Content watcher: %s; falling back to polling every %d ms", e, POLL_DELAY_MS)
                        polling = True
                        continue
                    # Anything else (a watched dir removed, a transient OS error):
                    # retry with backoff rather than leave the clients and the
                    # memory index without changes until the next restart.
                    failures += 1
                    delay = min(ERROR_BACKOFF_MAX_S, ERROR_BACKOFF_S * 2 ** (failures - 1))
                    logger.error("Content watcher error: %s; retrying in %ds", e, delay)
                    await self._sleep(delay)
        finally:
            self._stop_event.set()

    async def _handle(self, changes, public_root: Path | None, memory_roots: list[tuple[Path, str]]) -> None:
        public, memory = self.classify(changes, public_root, memory_roots)
        if memory:
            logger.info("Memory files changed: %d file(s)", len(memory))
            if self._on_memory_markdown is not None:
                self._on_memory_markdown()
            await self._broadcast({
                "type": "memory_changed",
                "changes": [{"path": p, "kind": k} for p, k in sorted(memory.items())],
            })
        if public and public_root is not None:
            frame = await asyncio.to_thread(self.visualization_frame, public_root, public)
            if frame is not None:
                await self._broadcast(frame)

    def stop(self) -> None:
        """Signal the watcher to stop."""
        self._running = False
        self._stop_event.set()
        logger.info("Content watcher stopping")
