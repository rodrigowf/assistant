"""Archie's memory index for harnesses that do not load it themselves.

Claude Code (and Model Studio, which is the same CLI) loads
``context/memory/MEMORY.md`` into every session's system prompt through its
auto-memory: ``CLAUDE_CONFIG_DIR=.claude_config`` and
``.claude_config/projects/-home-rodrigo-assistant → ../../context`` make its
auto-memory directory ``context/memory/``.  Gemini CLI (0.63) does the same
on its own: its private project memory index is
``~/.gemini/tmp/<label>/memory/MEMORY.md``, which the install symlink
``~/.gemini/tmp/<label> → context`` makes ``context/memory/MEMORY.md``.

Qwen Code and Codex have no equivalent that can point at the wiki (Qwen's
managed auto-memory rewrites ``MEMORY.md`` in its own format and costs a
model call per turn; Codex's ``memories`` feature keeps its own store under
``CODEX_HOME``), so their session managers add :func:`memory_instructions`
per run: Qwen through ``--append-system-prompt`` (rebuilt on every spawn,
i.e. every turn), Codex through the ``developerInstructions`` of
``thread/start`` (recorded once at the head of the thread).

The file is read live, so an edit shows up in the next session — like
Claude's auto-memory.  Only sessions whose working directory is the Archie
repo (or inside it) get it: that is where ``AGENTS.md`` (the wiki rules)
applies and where Claude's auto-memory directory is ``context/memory/``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from utils.paths import PROJECT_ROOT, get_memory_dir

logger = logging.getLogger(__name__)

MEMORY_INDEX_FILENAME = "MEMORY.md"
# Claude Code loads the first 200 lines of its MEMORY.md; keep the same cap
# (plus a byte cap so a runaway file cannot blow up argv / the prompt).
MAX_LINES = 200
MAX_BYTES = 25_000

# Opening line of the block — tests and the history extractors key on it.
HEADER = "# Memory"


def is_archie_project(project_dir: str | os.PathLike[str] | None) -> bool:
    """True when *project_dir* is the Archie repo root or a directory inside it.

    SSH sessions pass the remote path; both machines install Archie at the
    same path, so a plain path comparison is right there too.
    """
    if not project_dir:
        return False
    root = Path(os.path.normpath(PROJECT_ROOT))
    candidate = Path(os.path.normpath(os.path.expanduser(str(project_dir))))
    try:
        if candidate.exists():
            candidate = candidate.resolve()
            root = root.resolve()
    except OSError:
        pass
    return candidate == root or root in candidate.parents


def read_memory_index(memory_dir: Path | None = None) -> str | None:
    """The live ``MEMORY.md`` (capped like Claude's), or ``None`` if absent/empty."""
    path = Path(memory_dir or get_memory_dir()) / MEMORY_INDEX_FILENAME
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    truncated = False
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES]
        truncated = True
    out = "\n".join(lines).strip()
    if len(out.encode("utf-8")) > MAX_BYTES:
        out = out.encode("utf-8")[:MAX_BYTES].decode("utf-8", "ignore").rstrip()
        truncated = True
    if not out:
        return None
    if truncated:
        out += f"\n\n[MEMORY.md truncated here — read {path} for the rest]"
    return out


def memory_instructions(
    project_dir: str | os.PathLike[str] | None,
    *,
    memory_dir: Path | None = None,
) -> str | None:
    """The memory block for a session in *project_dir*, or ``None`` when it
    does not apply (not the Archie repo, or no ``MEMORY.md``)."""
    if not is_archie_project(project_dir):
        return None
    index = read_memory_index(memory_dir)
    if index is None:
        return None
    # Name the path the agent sees (the remote one for SSH sessions: the
    # same install path on both machines).
    shown_dir = Path(os.path.normpath(PROJECT_ROOT)) / "context" / "memory"
    return (
        f"{HEADER}\n\n"
        f"You have a persistent, file-based memory at `{shown_dir}/` — Archie's memory wiki, "
        "the same memory every Archie harness (Claude Code, Qwen Code, Gemini CLI, Codex) reads "
        "and writes. When you are asked to remember something, or learn something worth keeping, "
        "save it there with your file tools following the \"Memory System\" rules in the project "
        "instructions (AGENTS.md): reuse the closest existing note before creating one, put a new "
        "note in the right folder with the full frontmatter, cross-link it, and add a one-line "
        "entry to that folder's INDEX.md. Do not save memories anywhere else (no CLI-global "
        "memory file, no built-in memory tool), and never write fact text into MEMORY.md itself.\n\n"
        f"Its root index, `{shown_dir}/{MEMORY_INDEX_FILENAME}`, is loaded below as of this "
        "session's start — do not re-read it before using it:\n\n"
        f"<memory_index>\n{index}\n</memory_index>"
    )


__all__ = [
    "HEADER",
    "MAX_BYTES",
    "MAX_LINES",
    "MEMORY_INDEX_FILENAME",
    "is_archie_project",
    "memory_instructions",
    "read_memory_index",
]
