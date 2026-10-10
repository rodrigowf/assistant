"""Link sign-in flows: run a CLI's own login on the (possibly headless) server and relay it.

The CLIs print a URL (and sometimes a device code) and then either finish by themselves (device
flows: the user approves on any device) or wait for a code the provider's page shows, pasted back.
A :class:`LoginFlow` spawns the login, scans its output with the method's :class:`FlowSpec`
(``scan``), and exposes a small state machine to the API:

    starting → waiting (URL known) → verifying (code sent) → succeeded | failed
                                   ↘ cancelled | expired (timeout)

Some logins only behave in a terminal (``claude setup-token`` and the Gemini TUI are Ink /
readline apps that print their URL only to a TTY), so a flow can run in a pseudo-terminal; it is
made very wide so URLs and tokens are never wrapped. Others work over plain pipes.

Raw output never leaves the backend (it can hold tokens): clients get the URL, the device code,
and one-line, redacted messages. One flow per service at a time (:class:`FlowManager`); finished
flows are kept for a few minutes so every polling client sees the result; the process group is
killed on cancel, timeout, success-by-watch and backend shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import pty
import re
import signal
import struct
import termios
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .base import AccountError
from .files import Snapshot, login_backups_dir, restore, snapshot

logger = logging.getLogger(__name__)

FlowStatus = Literal["starting", "waiting", "verifying", "succeeded", "failed", "cancelled", "expired"]
_DONE: frozenset[str] = frozenset({"succeeded", "failed", "cancelled", "expired"})

# Finished flows stay visible this long (seconds).
KEEP_FINISHED_S = 600
# How long POST /login waits for the URL before answering "starting".
READY_WAIT_S = 12.0
_MAX_OUTPUT = 256 * 1024

# ─────────────────────────────── terminal text ───────────────────────────────

_OSC8_RE = re.compile(r"\x1b\]8;[^;\x07\x1b]*;([^\x07\x1b]*)(?:\x07|\x1b\\)")
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CSI_COL_RE = re.compile(r"\x1b\[\d*G")  # Ink positions words with "cursor to column"
_CSI_RE = re.compile(r"\x1b\[[0-9;?<>=!]*[ -/]*[@-~]")
_ESC_RE = re.compile(r"\x1b[@-Z\\-_78]")
# A plain-text URL only counts once something follows it: output arrives in chunks, and a URL at
# the very end of the buffer may still be growing.
_URL_RE = re.compile(r"https?://[^\s\x07\x1b\"'<>]+(?=[\s\"'<>])")
_LONG_TOKEN_RE = re.compile(r"(sk-[A-Za-z0-9_-]{6})[A-Za-z0-9_-]+|\b[A-Za-z0-9_-]{40,}\b")


def link_targets(raw: str) -> list[str]:
    """URLs of OSC 8 hyperlinks (the exact target, even when the visible text is wrapped)."""
    return [u for u in _OSC8_RE.findall(raw) if u]


def clean(raw: str) -> str:
    """Terminal output as plain text: escape sequences out, column jumps as spaces, CRs as newlines."""
    text = _OSC_RE.sub("", raw)
    text = _CSI_COL_RE.sub(" ", text)
    text = _CSI_RE.sub("", text)
    text = _ESC_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(ch for ch in text if ch == "\n" or ch == "\t" or ch >= " ")


def find_url(raw: str, prefix: str = "https://", contains: str = "") -> str | None:
    """First URL starting with *prefix* (and containing *contains*) — hyperlink targets first,
    then the plain text."""
    for u in link_targets(raw) + [u.rstrip(".,)") for u in _URL_RE.findall(clean(raw))]:
        if u.startswith(prefix) and contains in u:
            return u
    return None


def redact(text: str) -> str:
    """Hide anything token-shaped in a message meant for clients."""
    return _LONG_TOKEN_RE.sub(lambda m: (m.group(1) + "…") if m.group(1) else "…", text)


def last_line(raw: str, skip: tuple[str, ...] = ()) -> str:
    """The last non-empty, non-noise line of the output (for failure messages)."""
    for line in reversed(clean(raw).splitlines()):
        s = line.strip()
        if s and not any(x in s for x in skip) and len(s) > 2:
            return redact(s)[:300]
    return ""


# ─────────────────────────────── specs ───────────────────────────────


@dataclass
class Scan:
    """What a method's scanner found in the output so far."""

    url: str | None = None
    user_code: str | None = None
    error: str | None = None  # the login failed (message for the user)
    success: bool = False  # the CLI said it is done
    secret: str | None = None  # a credential the CLI printed (``claude setup-token``)


@dataclass
class FlowSpec:
    service: str
    method: str
    argv: list[str]
    env: dict[str, str]
    scan: Callable[[str], Scan]
    cwd: str | None = None
    pty: bool = False
    needs_code: bool = False
    code_label: str = "Code"
    code_help: str = ""
    timeout_s: float = 900.0
    # The CLI printed a credential: store it (raises on failure). Then the flow succeeds.
    on_secret: Callable[[str], None] | None = None
    # Polled every second once a code was sent: True = a *new* credential landed (CLIs that never
    # exit, e.g. a TUI). The callable compares against its own baseline.
    watch: Callable[[], bool] | None = None
    # Custom handling of the pasted code (e.g. relay a redirect URL); default writes it to stdin.
    submit: Callable[[str], Awaitable[None]] | None = None
    # Exit status 0 means signed in (most CLIs).
    exit_ok_is_success: bool = True
    success_message: str = "Signed in."
    # Called once when the flow's process is gone (temp dirs and the like).
    on_close: Callable[[], None] | None = None
    # Credential files the login could touch. Snapshotted before the CLI starts and restored when
    # the flow ends any other way than success (cancel, failure, timeout, shutdown): some CLIs log
    # the current login out as soon as their login starts.
    protect: tuple[Path, ...] = ()
    # Runs before a success is reported: moves the new login from a staging home into place.
    # Raising turns the success into a failure (and the protected files are restored).
    promote: Callable[[], None] | None = None
    # Output lines never worth showing as the failure reason.
    noise: tuple[str, ...] = ()


# ─────────────────────────────── a running flow ───────────────────────────────


@dataclass
class LoginFlow:
    spec: FlowSpec
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: FlowStatus = "starting"
    url: str | None = None
    user_code: str | None = None
    message: str = "Starting the sign-in…"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    code_sent: bool = False

    def __post_init__(self) -> None:
        self._raw = ""
        self._proc: asyncio.subprocess.Process | None = None
        self._master: int | None = None
        self._reader: asyncio.Task | None = None
        self._supervisor: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._done = asyncio.Event()
        self._snaps: list[Snapshot] = []

    # ── lifecycle ──

    @property
    def done(self) -> bool:
        return self.status in _DONE

    @property
    def expires_at(self) -> float:
        return self.started_at + self.spec.timeout_s

    async def start(self) -> None:
        """Spawn the login. Never raises: a failure to start leaves the flow `failed`."""
        spec = self.spec
        env = dict(spec.env)
        master: int | None = None
        try:
            self._snaps = snapshot(spec.protect, login_backups_dir(spec.service))
            if spec.pty:
                master, slave = pty.openpty()
                try:
                    # Very wide, so Ink / readline never wrap the URL or a printed token.
                    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 60, 1000, 0, 0))
                    env.setdefault("TERM", "xterm-256color")
                    env["COLUMNS"], env["LINES"] = "1000", "60"
                    self._proc = await asyncio.create_subprocess_exec(
                        *spec.argv, stdin=slave, stdout=slave, stderr=slave,
                        env=env, cwd=spec.cwd, start_new_session=True,
                    )
                finally:
                    os.close(slave)
                self._master, master = master, None
                os.set_blocking(self._master, False)
                asyncio.get_running_loop().add_reader(self._master, self._on_pty_readable)
            else:
                self._proc = await asyncio.create_subprocess_exec(
                    *spec.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT, env=env, cwd=spec.cwd, start_new_session=True,
                )
                self._reader = asyncio.create_task(self._read_pipe())
        except Exception as e:  # noqa: BLE001 — OSError (missing CLI, no PTYs left, bad cwd) or worse
            if master is not None:
                with contextlib.suppress(OSError):
                    os.close(master)
            self._finish("failed", f"Couldn't run {os.path.basename(spec.argv[0])}: {getattr(e, 'strerror', None) or e}")
            await self._kill()
            return
        logger.info("accounts: %s/%s login started (pid %s)", spec.service, spec.method, self._proc.pid)
        self._supervisor = asyncio.create_task(self._supervise())

    async def wait_ready(self, timeout: float = READY_WAIT_S) -> None:
        """Until the URL is known, the flow ended, or *timeout*."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._ready.wait(), timeout)

    async def wait_done(self, timeout: float | None = None) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._done.wait(), timeout)

    async def submit_code(self, code: str) -> None:
        code = (code or "").strip()
        if self.done:
            raise AccountError("This sign-in has already finished. Start a new one.", 409)
        if self.url is None:
            raise AccountError("The sign-in link isn't ready yet.", 409)
        if not code:
            raise AccountError(f"Paste the {self.spec.code_label.lower()} first.")
        if "\n" in code or len(code) > 4096:
            raise AccountError("That doesn't look like a sign-in code.")
        if self.spec.submit is not None:
            await self.spec.submit(code)
        else:
            self._write(code + ("\r" if self.spec.pty else "\n"))
        self.code_sent = True
        self.status = "verifying"
        self.message = "Checking the code…"

    async def cancel(self, status: FlowStatus = "cancelled", message: str = "Sign-in cancelled.") -> None:
        if not self.done:
            self._finish(status, message)
        await self._kill()
        await self.wait_closed()

    async def wait_closed(self, timeout: float = 8.0) -> None:
        """Until the supervisor has reaped the process and closed the pipes."""
        if self._supervisor is not None and not self._supervisor.done():
            await asyncio.wait([self._supervisor], timeout=timeout)

    # ── internals ──

    def _write(self, text: str) -> None:
        data = text.encode()
        if self._master is not None:
            os.write(self._master, data)
        elif self._proc is not None and self._proc.stdin is not None:
            self._proc.stdin.write(data)
        else:
            raise AccountError("The sign-in process isn't accepting input.", 409)

    def _on_pty_readable(self) -> None:
        assert self._master is not None
        try:
            chunk = os.read(self._master, 65536)
        except BlockingIOError:
            return
        except OSError:  # EIO: the child closed the terminal
            chunk = b""
        if not chunk:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_reader(self._master)
            return
        self._feed(chunk)

    async def _read_pipe(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            chunk = await self._proc.stdout.read(65536)
            if not chunk:
                return
            self._feed(chunk)

    def _feed(self, chunk: bytes) -> None:
        self._raw = (self._raw + chunk.decode("utf-8", "replace"))[-_MAX_OUTPUT:]
        if self.done:
            return
        try:
            found = self.spec.scan(self._raw)
        except Exception:  # a scanner bug must not wedge the flow
            logger.exception("accounts: scanner failed for %s/%s", self.spec.service, self.spec.method)
            return
        if found.url and not self.url:
            self.url = found.url
        if found.user_code and not self.user_code:
            self.user_code = found.user_code
        if self.url and self.status == "starting":
            self.status = "waiting"
            self.message = (
                f"Open the link, sign in, then paste the {self.spec.code_label.lower()} here."
                if self.spec.needs_code else "Open the link and approve the sign-in; this page updates by itself."
            )
            self._ready.set()
        if found.secret and self.spec.on_secret is not None:
            try:
                self.spec.on_secret(found.secret)
            except Exception as e:  # noqa: BLE001 — reported to the user
                logger.exception("accounts: storing the credential of %s failed", self.spec.service)
                self._finish("failed", redact(f"Signed in, but saving the credential failed: {e}"))
            else:
                self._finish("succeeded", self.spec.success_message)
            asyncio.get_running_loop().create_task(self._kill())
        elif found.success:
            self._finish("succeeded", self.spec.success_message)
        elif found.error:
            self._finish("failed", redact(found.error)[:300])
            asyncio.get_running_loop().create_task(self._kill())

    async def _supervise(self) -> None:
        assert self._proc is not None
        proc = self._proc
        deadline = self.expires_at
        while True:
            try:
                rc = await asyncio.wait_for(proc.wait(), 1.0)
                break
            except asyncio.TimeoutError:
                pass
            if self.done:
                if self.status == "succeeded":
                    # The CLI said it's done: give it time to finish writing and exit on its own.
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(proc.wait(), 5.0)
                await self._kill()
                return
            if time.time() > deadline:
                self._finish("expired", "The sign-in timed out. Start a new one.")
                await self._kill()
                return
            if self.spec.watch is not None and self.code_sent:
                try:
                    landed = bool(self.spec.watch())
                except Exception:
                    landed = False
                if landed:
                    self._finish("succeeded", self.spec.success_message)
                    await self._kill()
                    return
        await asyncio.sleep(0.2)  # let the reader drain the last output
        if self._master is not None:
            self._on_pty_drain()
        await self._release()
        if not self.done:
            if rc == 0 and self.spec.exit_ok_is_success:
                self._finish("succeeded", self.spec.success_message)
            else:
                why = last_line(self._raw, self.spec.noise) or f"exit status {rc}"
                self._finish("failed", f"The sign-in didn't finish: {why}")

    def _on_pty_drain(self) -> None:
        while self._master is not None:
            try:
                chunk = os.read(self._master, 65536)
            except OSError:
                return
            if not chunk:
                return
            self._feed(chunk)

    def _finish(self, status: FlowStatus, message: str) -> None:
        if self.done:
            return
        if status == "succeeded" and self.spec.promote is not None:
            try:
                self.spec.promote()
            except Exception as e:  # noqa: BLE001 — reported to the user
                logger.exception("accounts: promoting the %s login failed", self.spec.service)
                status, message = "failed", redact(f"The sign-in finished, but saving the login failed: {e}")
        if status != "succeeded" and self._snaps:
            try:
                if restore(self._snaps):
                    message += " Your previous login was restored."
            except Exception:  # noqa: BLE001
                logger.exception("accounts: restoring the %s credentials failed", self.spec.service)
                message += " Restoring the previous login failed; see the backups in .backups/login/."
        self.status = status
        self.message = message
        self.finished_at = time.time()
        self._ready.set()
        self._done.set()
        logger.info("accounts: %s/%s login %s", self.spec.service, self.spec.method, status)

    async def _kill(self) -> None:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            for sig, wait in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 2.0)):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, sig)
                try:
                    await asyncio.wait_for(proc.wait(), wait)
                    break
                except asyncio.TimeoutError:
                    continue
        await self._release()

    async def _release(self) -> None:
        """Close our ends (stdin pipe, PTY master) so asyncio can close the transport."""
        proc = self._proc
        if self.done and self.status != "succeeded" and self._snaps and (proc is None or proc.returncode is not None):
            # Once more now that the CLI is gone: it can't write after this.
            with contextlib.suppress(Exception):
                restore(self._snaps)
        if self.spec.on_close is not None and (proc is None or proc.returncode is not None):
            hook, self.spec.on_close = self.spec.on_close, None
            with contextlib.suppress(Exception):
                hook()
        if proc is not None and proc.stdin is not None and not proc.stdin.is_closing():
            proc.stdin.close()
        self._close_master()
        if proc is not None and proc.returncode is not None and self._reader is not None and not self._reader.done():
            await asyncio.wait([self._reader], timeout=2)

    def _close_master(self) -> None:
        if self._master is not None:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_reader(self._master)
            with contextlib.suppress(OSError):
                os.close(self._master)
            self._master = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "service": self.spec.service,
            "method": self.spec.method,
            "status": self.status,
            "url": self.url,
            "user_code": self.user_code,
            "needs_code": self.spec.needs_code and not self.done,
            "code_label": self.spec.code_label,
            "code_help": self.spec.code_help,
            "message": self.message,
            "started_at": _iso(self.started_at),
            "expires_at": _iso(self.expires_at),
            "finished_at": _iso(self.finished_at) if self.finished_at else None,
        }


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


# ─────────────────────────────── manager ───────────────────────────────


class FlowManager:
    """At most one flow per service; finished ones linger for :data:`KEEP_FINISHED_S`."""

    def __init__(self) -> None:
        self._flows: dict[str, LoginFlow] = {}
        self._lock = asyncio.Lock()

    def get(self, service: str) -> LoginFlow | None:
        flow = self._flows.get(service)
        if flow and flow.done and flow.finished_at and time.time() - flow.finished_at > KEEP_FINISHED_S:
            del self._flows[service]
            return None
        return flow

    async def start(self, spec: FlowSpec) -> LoginFlow:
        async with self._lock:
            current = self.get(spec.service)
            if current and not current.done:
                if current.spec.method == spec.method:
                    return current
                raise AccountError(
                    "Another sign-in for this service is in progress. Cancel it first.", 409,
                )
            flow = LoginFlow(spec)
            self._flows[spec.service] = flow
            try:
                await flow.start()
            except BaseException:
                self._flows.pop(spec.service, None)
                raise
        await flow.wait_ready()
        return flow

    async def cancel(self, service: str) -> LoginFlow | None:
        flow = self.get(service)
        if flow:
            await flow.cancel()
        return flow

    async def shutdown(self) -> None:
        for flow in list(self._flows.values()):
            with contextlib.suppress(Exception):
                await flow.cancel(message="The server stopped.")
        self._flows.clear()
