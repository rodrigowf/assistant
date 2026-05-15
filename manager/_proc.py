"""Provider-agnostic process helpers — alive checks, comm lookup, signal escalation.

These are shared between :mod:`manager.claude_session` (where Claude's bundled
SDK subprocess may need force-killing) and :mod:`manager.qwen_session` (where
the per-turn qwen subprocess can in theory be reaped the same way).  The pool's
orphan reaper also reaches for ``_process_alive`` and a per-provider
``looks_like(pid)`` check before sending signals.

Keeping these in their own tiny module means importing them does NOT pull in
``claude-agent-sdk`` (which ``manager.claude_session`` imports at module load).
Crucial for Qwen-only installs where the SDK may not even be present at
import time.
"""

from __future__ import annotations

import errno
import logging
import os
import signal
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def process_alive(pid: int) -> bool:
    """Return True if a process with *pid* exists and we can signal it.

    Uses ``os.kill(pid, 0)`` — the kernel resolves the pid and checks
    permissions but doesn't actually deliver any signal.  Distinguishes
    cleanly between "process is gone" (ESRCH) and "process exists but we
    can't touch it" (EPERM, treated as alive).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as e:
        return e.errno != errno.ESRCH


def process_comm(pid: int) -> str | None:
    """Read /proc/<pid>/comm and return its content (the kernel's view of
    the executable basename, capped at 15 chars).  Returns None if the
    process is gone or /proc isn't readable.

    Used as a sanity check before SIGKILL: PIDs are reused by the kernel
    after a process exits, so before nuking pid X we verify it still looks
    like the subprocess we spawned — not some innocent process that
    happened to be assigned the recycled pid.
    """
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip() or None
    except (OSError, FileNotFoundError):
        return None


def looks_like(pid: int, comm_prefix: str) -> bool:
    """Return True iff /proc/<pid>/comm starts with *comm_prefix*."""
    comm = process_comm(pid)
    return comm is not None and comm.startswith(comm_prefix)


def kill_subprocess(
    pid: int,
    *,
    comm_prefix: str,
    sigterm_grace_s: float = 0.5,
) -> bool:
    """Force-kill an orphaned subprocess identified by *pid*.

    Verifies the pid still belongs to a process whose ``/proc/<pid>/comm``
    starts with *comm_prefix* before signalling — the kernel can recycle
    pids immediately after a process exits, and we never want to SIGKILL
    an unrelated process that happened to inherit the number.

    First sends SIGTERM (giving the subprocess *sigterm_grace_s* seconds
    to wind down via its normal handlers — flushing JSONL, etc.); if the
    process is still alive after that, escalates to SIGKILL.  Returns
    True if a signal was sent (process was alive and matched the comm
    prefix), False otherwise.

    Safe to call concurrently from the per-session lifecycle finally and
    from the pool's orphan reaper — the second caller will simply observe
    the process is gone (or no longer matches the prefix) and no-op.
    """
    if not process_alive(pid):
        return False
    if not looks_like(pid, comm_prefix):
        # PID was reused by the kernel for an unrelated process — bail
        # out instead of nuking something innocent.
        logger.info(
            "Skipping kill of pid %d: comm=%r does not start with %r",
            pid, process_comm(pid), comm_prefix,
        )
        return False

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    except OSError:
        logger.exception("SIGTERM to pid %d failed", pid)

    # Brief grace period — the subprocess can take a moment to flush JSONL
    # before exiting.  Synchronous poll (no asyncio) so this helper is
    # safely callable from sync contexts (e.g. the orphan reaper running
    # in a thread executor).
    end = time.monotonic() + sigterm_grace_s
    while time.monotonic() < end:
        if not process_alive(pid):
            return True
        time.sleep(0.05)

    if process_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
            logger.warning(
                "Pid %d (%s*) ignored SIGTERM after %.1fs; sent SIGKILL",
                pid, comm_prefix, sigterm_grace_s,
            )
        except ProcessLookupError:
            return False
        except OSError:
            logger.exception("SIGKILL to pid %d failed", pid)
    return True
