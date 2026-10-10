"""Small file helpers for credential files: atomic writes with a backup, JSON reads, JWT claims.

Credential files are written the same way everywhere: the previous file is copied to
``<name>.bak-<UTC timestamp>`` next to it (the newest ``KEEP_BACKUPS`` are kept), the new content
goes to a temp file in the same directory, is fsync'ed, gets mode 0600 and is renamed over the
target. A crash leaves either the old or the new file, never half of one.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

KEEP_BACKUPS = 5


def backup_name(path: Path, stamp: str | None = None) -> Path:
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    return path.with_name(f"{path.name}.bak-{stamp}")


def _prune_backups(path: Path, keep: int, backup_dir: Path | None = None) -> None:
    folder = backup_dir or path.parent
    try:
        olds = sorted(folder.glob(f"{path.name}.bak-*"))
    except OSError:
        return
    for old in olds[:-keep] if keep > 0 else olds:
        try:
            old.unlink()
        except OSError:
            pass


def backup_file(path: Path, *, keep: int = KEEP_BACKUPS, backup_dir: Path | None = None) -> Path | None:
    """Copy *path* to a timestamped backup (mode 0600; a separate *backup_dir* is made 0700).
    None when there is nothing to back up."""
    if not path.is_file():
        return None
    folder = backup_dir or path.parent
    if backup_dir is not None:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
    dest = folder / backup_name(path).name
    n = 1
    while dest.exists():  # two writes in the same second
        dest = folder / f"{backup_name(path).name}-{n}"
        n += 1
    shutil.copy2(path, dest)
    os.chmod(dest, 0o600)
    _prune_backups(path, keep, backup_dir)
    return dest


def atomic_write(
    path: Path,
    content: str,
    *,
    mode: int = 0o600,
    backup: bool = True,
    backup_dir: Path | None = None,
    keep: int = KEEP_BACKUPS,
) -> Path | None:
    """Write *content* to *path* atomically; returns the backup path (if one was made).

    *path* may be a symlink (``~/.qwen`` style setups): the link target is written, the link kept.
    """
    target = Path(os.path.realpath(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    saved = backup_file(target, keep=keep, backup_dir=backup_dir) if backup else None
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return saved


def move_aside(path: Path) -> Path | None:
    """Sign-out for CLIs without a logout command: rename the file to a backup (kept, 0600)."""
    if not path.is_file():
        return None
    dest = backup_name(path)
    os.replace(path, dest)
    os.chmod(dest, 0o600)
    _prune_backups(path, KEEP_BACKUPS)
    return dest


def read_json(path: Path) -> Any:
    """Parsed JSON of *path*, or None when it is missing or not JSON."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def jwt_claims(token: object) -> dict[str, Any]:
    """The (unverified) payload of a JWT; ``{}`` for anything that isn't one.

    Only used to *display* who is signed in (email, plan) — never to make a trust decision.
    """
    if not isinstance(token, str) or token.count(".") < 2:
        return {}
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, UnicodeError):
        return {}
    return data if isinstance(data, dict) else {}


def iso_from_epoch(seconds: float | int | None) -> str | None:
    """ISO-8601 UTC for an epoch in seconds (or milliseconds, auto-detected)."""
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    if seconds > 1e11:  # milliseconds
        seconds = seconds / 1000
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


# ─────────────────────────────── login snapshots ───────────────────────────────


def login_backups_dir(service: str) -> Path:
    """Where credential snapshots taken before a sign-in go: ``<repo>/.backups/login/<service>/``
    (gitignored, 0700; never under ``context/``, which is synced)."""
    from utils.paths import PROJECT_ROOT

    return PROJECT_ROOT / ".backups" / "login" / service


@dataclass(frozen=True)
class Snapshot:
    path: Path
    content: bytes | None  # None: the file did not exist
    mode: int


def snapshot(paths: Iterable[Path], backup_dir: Path | None = None) -> list[Snapshot]:
    """Remember *paths* (and copy existing ones to *backup_dir*, 0600) before a CLI login runs.

    Some CLIs log out first: ``codex login`` deletes ``auth.json`` and ``claude auth login`` blanks
    ``.credentials.json`` the moment they start, long before the new login succeeds.
    """
    out = []
    for p in paths:
        try:
            data = p.read_bytes()
            mode = p.stat().st_mode & 0o777
        except FileNotFoundError:
            out.append(Snapshot(p, None, 0o600))
            continue
        if backup_dir is not None:
            backup_file(p, backup_dir=backup_dir)
        out.append(Snapshot(p, data, mode))
    return out


def restore(snaps: Iterable[Snapshot]) -> list[Path]:
    """Put back every snapshotted file that is now missing or different; returns those restored.
    A file that did not exist before and appeared since is moved aside (kept as a backup)."""
    restored = []
    for s in snaps:
        try:
            now = s.path.read_bytes()
        except FileNotFoundError:
            now = None
        if now == s.content:
            continue
        if s.content is None:
            move_aside(s.path)
        else:
            atomic_write(s.path, s.content.decode("utf-8"), mode=s.mode or 0o600, backup=now is not None)
        restored.append(s.path)
    return restored
