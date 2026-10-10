"""Helpers shared by the account services: locating CLIs, running short commands, child env,
and testing API keys against free provider endpoints."""

from __future__ import annotations

import asyncio
import glob
import os
import shutil
import signal
import time
from collections.abc import Iterable
from typing import Any

import httpx

from . import envfile
from .base import EnvField

# Never let a login open a browser on the server: the link is shown to the user instead.
_NO_GUI = ("DISPLAY", "WAYLAND_DISPLAY")


def find_cli(name: str, override_env: str | None = None, extra: Iterable[str] = ()) -> str | None:
    """Path of a CLI: ``$<override_env>``, ``PATH``, extra candidates, then nvm's global bins
    (the backend may run under systemd without nvm on its ``PATH``)."""
    if override_env and os.environ.get(override_env):
        return os.environ[override_env]
    found = shutil.which(name)
    if found:
        return found
    for cand in extra:
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    nvm = sorted(
        glob.glob(os.path.expanduser(f"~/.nvm/versions/node/*/bin/{name}")),
        key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0,
        reverse=True,
    )
    return nvm[0] if nvm else None


def child_env(drop: Iterable[str] = (), *, cli: str | None = None, **extra: str) -> dict[str, str]:
    """The backend's environment minus *drop*, ``CLAUDECODE`` and the display variables.

    When the CLI lives under nvm, its ``node`` must be on ``PATH`` too (npm shims are
    ``#!/usr/bin/env node`` scripts): pass the CLI path as *cli* and its directory is prepended.
    """
    dropped = {"CLAUDECODE", *_NO_GUI, *drop}
    env = {k: v for k, v in os.environ.items() if k not in dropped}
    env["BROWSER"] = "true"  # `xdg-open`-style launchers that honour $BROWSER do nothing
    if cli:
        bindir = os.path.dirname(os.path.realpath(cli))
        shim_dir = os.path.dirname(cli)
        path = env.get("PATH", "")
        for d in (shim_dir, bindir):
            if d and d not in path.split(os.pathsep):
                path = d + os.pathsep + path
        env["PATH"] = path
    env.update(extra)
    return env


async def run(argv: list[str], env: dict[str, str], *, timeout: float = 15.0, stdin: str | None = None, cwd: str | None = None) -> tuple[int | None, str]:
    """Run a short command; (exit status or None on timeout/missing, combined output)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env=env, cwd=cwd, start_new_session=True,
        )
    except (FileNotFoundError, PermissionError) as e:
        return None, str(e)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(stdin.encode() if stdin is not None else None), timeout)
    except asyncio.TimeoutError:
        await _reap(proc)
        return None, "timed out"
    except asyncio.CancelledError:  # the request went away: don't leave the CLI behind
        await asyncio.shield(_reap(proc))
        raise
    return proc.returncode, out.decode("utf-8", "replace")


async def _reap(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    await proc.wait()


def env_field(name: str, label: str, **kw: Any) -> EnvField:
    return EnvField(name=name, label=label, **kw)


def key_set(name: str) -> bool:
    return bool(envfile.effective(name).strip())


async def verify_http(url: str, headers: dict[str, str], *, ok_message: str) -> dict[str, Any]:
    """GET a free "list models" endpoint with the credential; never echoes the key."""
    checked = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        return {"ok": False, "message": f"Couldn't reach the provider: {type(e).__name__}", "checked_at": checked}
    if r.status_code == 200:
        return {"ok": True, "message": ok_message, "checked_at": checked}
    if r.status_code in (401, 403):
        return {"ok": False, "message": f"The provider rejected the key ({r.status_code}).", "checked_at": checked}
    return {"ok": False, "message": f"Unexpected answer from the provider ({r.status_code}).", "checked_at": checked}
