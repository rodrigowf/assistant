"""``context/.env`` manager: list, reveal, create, update and delete keys.

The file is ``source``d by ``run.sh`` (``set -a; source context/.env``) before the backend starts,
so it is a **bash** file, not a dotenv dialect. The parser understands what bash does with the
lines Archie's file actually has — ``NAME=value``, ``export NAME=value``, ``'single'`` and
``"double"`` quoted values (also spanning lines), backslash escapes, and comments — and the writer
only produces forms bash reads back verbatim (bare words, or single quotes with ``'\\''``).

Edits are surgical: only the line(s) of the key being changed are rewritten; comments, blank
lines, ordering, ``export`` prefixes and trailing ``# comments`` of every other line stay
byte-identical. Each write is atomic and keeps a timestamped backup (``files.atomic_write``).

The running backend's ``os.environ`` is updated for the changed key, so code that reads the
variable at call time (voice providers, catalogs, new agent sessions) sees the new value at once.
Values that are copied at startup need a restart; :func:`restart_scope` says which.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from utils.paths import PROJECT_ROOT, get_context_dir

from .files import atomic_write

# Any name bash accepts (existing files may hold lowercase ones); the UIs suggest CAPITALS for new keys.
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ASSIGN_RE = re.compile(r"^(?P<lead>\s*)(?P<export>export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<rest>.*)$", re.S)
# Characters that stay literal when bash reads an unquoted assignment value. No `~` (tilde
# expansion, also after `:` in assignments), no `$`, quotes, spaces, globs or `#`.
_BARE_SAFE = re.compile(r"^[A-Za-z0-9_@%+=:,./^-]+$")

# How many backups of .env to keep. They live in <repo>/.backups/env/ (gitignored, 0700) — not
# under context/, which context-sync copies to the other machine and which is a git repo.
ENV_BACKUPS = 10

# Variables the backend copies at startup (or that change how it starts): editing them needs a
# backend restart, and the running process's os.environ is left alone (changing PATH or HOME under
# a running server would only half-apply). Everything else is read at call time or by processes
# spawned later.
RESTART_BACKEND = frozenset({
    "CLAUDE_CONFIG_DIR", "HEADLESS", "PYTHONPATH", "LD_PRELOAD", "PATH", "HOME", "DISPLAY",
})

_lock = threading.Lock()


def env_path() -> Path:
    """``context/.env`` (tests monkeypatch this)."""
    return get_context_dir() / ".env"


def backups_dir() -> Path:
    return PROJECT_ROOT / ".backups" / "env"


class EnvError(ValueError):
    """A bad request against the env file (invalid name, missing key, duplicate)."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# ─────────────────────────────── parsing ───────────────────────────────


@dataclass
class _Assign:
    name: str
    value: str
    export: bool
    lead: str
    comment: str  # trailing " # …" text (with its leading whitespace), "" if none
    start: int  # index of the first physical line
    end: int  # index after the last physical line


@dataclass
class _Parsed:
    lines: list[str]  # physical lines, each with its own newline (if any)
    assigns: list[_Assign] = field(default_factory=list)


def _scan_value(text: str) -> tuple[str, int] | None:
    """Parse a bash word starting at text[0]; returns (value, consumed) or None when a quote is
    left open (the caller appends the next physical line and retries)."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            break
        if ch == "'":
            j = text.find("'", i + 1)
            if j < 0:
                return None
            out.append(text[i + 1:j])
            i = j + 1
        elif ch == '"':
            j = i + 1
            buf: list[str] = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n and text[j + 1] in '"\\$`\n':
                    if text[j + 1] != "\n":  # backslash-newline is a line continuation
                        buf.append(text[j + 1])
                    j += 2
                    continue
                buf.append(text[j])
                j += 1
            if j >= n:
                return None
            out.append("".join(buf))
            i = j + 1
        elif ch == "\\":
            if i + 1 >= n:
                return None
            if text[i + 1] != "\n":
                out.append(text[i + 1])
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out), i


def parse(text: str) -> _Parsed:
    lines = text.splitlines(keepends=True)
    parsed = _Parsed(lines=lines)
    i = 0
    while i < len(lines):
        m = _ASSIGN_RE.match(lines[i])
        if not m:
            i += 1
            continue
        rest = m.group("rest")
        end = i + 1
        scanned = _scan_value(rest)
        while scanned is None and end < len(lines):  # a quoted value spanning lines
            rest += lines[end]
            end += 1
            scanned = _scan_value(rest)
        if scanned is None:
            # A quote never closed before EOF (``FOO=it's``). Never let it swallow the rest of the
            # file: it is a one-line value, taken literally, and an edit rewrites only that line.
            end = i + 1
            rest = m.group("rest")
            value, consumed = rest.rstrip("\r\n"), len(rest)
        else:
            value, consumed = scanned
        tail = rest[consumed:].rstrip("\r\n")
        comment = tail if tail.strip().startswith("#") else ""
        parsed.assigns.append(_Assign(
            name=m.group("name"), value=value, export=bool(m.group("export")), lead=m.group("lead"),
            comment=comment, start=i, end=end,
        ))
        i = end
    return parsed


def quote(value: str) -> str:
    """Render *value* as a bash word that ``source`` reads back verbatim."""
    if value == "":
        return ""
    if _BARE_SAFE.match(value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def render(a: _Assign, value: str) -> str:
    return f"{a.lead}{'export ' if a.export else ''}{a.name}={quote(value)}{a.comment}\n"


# ─────────────────────────────── public API ───────────────────────────────


def mask(value: str) -> str:
    """Masked preview for lists: never more than the last 4 characters, and only of long values."""
    if not value:
        return ""
    if len(value) >= 16:
        return "••••" + value[-4:]
    return "••••"


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise EnvError("Key names use letters, digits and underscores and don't start with a digit (e.g. MY_API_KEY).")
    return name


def restart_scope(name: str) -> str:
    """When a change to *name* takes effect: ``"now"`` (read at call time by the backend and by
    agent sessions started from now on) or ``"backend_restart"``."""
    return "backend_restart" if name in RESTART_BACKEND else "now"


@dataclass(frozen=True)
class EnvKey:
    name: str
    set: bool
    preview: str
    length: int
    line: int  # 1-based line of the (last) assignment
    exported: bool
    duplicates: int  # extra assignments of the same name (bash keeps the last)
    in_process: bool  # the running backend's os.environ has this exact value

    def to_dict(self) -> dict:
        return {
            "name": self.name, "set": self.set, "preview": self.preview, "length": self.length,
            "line": self.line, "exported": self.exported, "duplicates": self.duplicates,
            "in_process": self.in_process,
        }


def _read() -> tuple[Path, str]:
    path = env_path()
    try:
        return path, path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return path, ""


def _last(parsed: _Parsed) -> dict[str, _Assign]:
    out: dict[str, _Assign] = {}
    for a in parsed.assigns:
        out[a.name] = a
    return out


def list_keys() -> list[EnvKey]:
    """Every key in file order (by its last assignment); values masked."""
    _, text = _read()
    parsed = parse(text)
    counts: dict[str, int] = {}
    for a in parsed.assigns:
        counts[a.name] = counts.get(a.name, 0) + 1
    out = []
    for name, a in sorted(_last(parsed).items(), key=lambda kv: kv[1].start):
        out.append(EnvKey(
            name=name, set=a.value != "", preview=mask(a.value), length=len(a.value), line=a.start + 1,
            exported=a.export, duplicates=counts[name] - 1, in_process=os.environ.get(name) == a.value,
        ))
    return out


def get_value(name: str) -> str | None:
    """The value bash would see for *name*, or None if the file doesn't assign it."""
    validate_name(name)
    _, text = _read()
    a = _last(parse(text)).get(name)
    return a.value if a else None


def has(name: str) -> bool:
    return get_value(name) is not None


def _write(path: Path, text: str) -> None:
    # Always 0600: the file holds every secret (it may have been created world-readable).
    atomic_write(path, text, mode=0o600, backup=path.exists(), backup_dir=backups_dir(), keep=ENV_BACKUPS)


def set_value(name: str, value: str, *, create: bool | None = None) -> EnvKey:
    """Create or update *name*. ``create=True`` fails if it exists, ``False`` if it doesn't.

    The last assignment is rewritten in place (bash keeps the last one); a new key is appended.
    ``os.environ`` is updated too.
    """
    validate_name(name)
    if not isinstance(value, str):
        raise EnvError("The value must be text.")
    if "\x00" in value:
        raise EnvError("The value can't contain NUL characters.")
    with _lock:
        path, text = _read()
        parsed = parse(text)
        a = _last(parsed).get(name)
        if create is True and a is not None:
            raise EnvError(f"{name} already exists.", status=409)
        if create is False and a is None:
            raise EnvError(f"{name} isn't in the .env file.", status=404)
        lines = list(parsed.lines)
        if a is not None:
            lines[a.start:a.end] = [render(a, value)]
        else:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(f"{name}={quote(value)}\n")
        _write(path, "".join(lines))
        if name not in RESTART_BACKEND:  # those only take effect on restart; don't half-apply them
            os.environ[name] = value
    return next(k for k in list_keys() if k.name == name)


def delete(name: str) -> int:
    """Remove every assignment of *name* (and unset it in ``os.environ``); returns how many."""
    validate_name(name)
    with _lock:
        path, text = _read()
        parsed = parse(text)
        hits = [a for a in parsed.assigns if a.name == name]
        if not hits:
            raise EnvError(f"{name} isn't in the .env file.", status=404)
        lines = list(parsed.lines)
        for a in reversed(hits):
            del lines[a.start:a.end]
        _write(path, "".join(lines))
        if name not in RESTART_BACKEND:
            os.environ.pop(name, None)
    return len(hits)


def effective(name: str) -> str:
    """The value the backend uses: ``os.environ`` (exported by run.sh), else the file."""
    v = os.environ.get(name)
    if v:
        return v
    try:
        return get_value(name) or ""
    except EnvError:
        return ""
