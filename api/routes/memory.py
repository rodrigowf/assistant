"""REST discovery for the context/memory/ markdown wiki.

Only a *tree* endpoint lives here. File content is already reachable: the
``/memory/<path>`` route registered in ``api/app.py`` serves raw files from
context/memory/ and is matched before the SPA catch-all, so the frontend
fetches markdown straight from there rather than through a second endpoint.

Like the visualizations route, there is no watcher or cache — the frontend
refetches when the sidebar loads.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from api.models import MemoryNodeResponse
from utils.paths import get_memory_dir

router = APIRouter(prefix="/api/memory", tags=["memory"])


def _build_tree(directory: Path, root: Path) -> list[MemoryNodeResponse]:
    """Recursively collect markdown files under ``directory``.

    Directories that contain no markdown at any depth are dropped, so empty
    scaffolding folders don't show up as dead ends in the sidebar tree.
    Directories sort before files; both alphabetically, case-insensitively.
    """
    dirs: list[MemoryNodeResponse] = []
    files: list[MemoryNodeResponse] = []

    try:
        entries = list(directory.iterdir())
    except OSError:
        return []

    for entry in entries:
        # Skip dotfiles/dotdirs (.git, .obsidian, editor cruft).
        if entry.name.startswith("."):
            continue
        try:
            # Don't follow symlinks out of the memory tree.
            resolved = entry.resolve()
            if not resolved.is_relative_to(root):
                continue
        except OSError:
            continue

        if entry.is_dir():
            children = _build_tree(entry, root)
            if children:
                dirs.append(
                    MemoryNodeResponse(
                        name=entry.name,
                        path=entry.relative_to(root).as_posix(),
                        is_dir=True,
                        children=children,
                    )
                )
        elif entry.is_file() and entry.suffix.lower() == ".md":
            files.append(
                MemoryNodeResponse(
                    name=entry.name,
                    path=entry.relative_to(root).as_posix(),
                    is_dir=False,
                )
            )

    dirs.sort(key=lambda n: n.name.lower())
    files.sort(key=lambda n: n.name.lower())
    return dirs + files


@router.get("/tree", response_model=list[MemoryNodeResponse])
def memory_tree():
    memory_dir = get_memory_dir()
    if not memory_dir.is_dir():
        return []
    # Walk the resolved root so entry.relative_to(root) holds even when a
    # parent (context/, or memory/ itself) is a symlink.
    root = memory_dir.resolve()
    return _build_tree(root, root)
