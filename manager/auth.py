"""Auth helper — detect auth state and trigger OAuth browser flow."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path


def _get_auth_env() -> dict[str, str]:
    """Get environment for auth commands.

    Unset CLAUDECODE to allow running auth checks inside a Claude Code session.
    Keep CLAUDE_CONFIG_DIR so credentials are read from the project folder.
    """
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    return env


class AuthManager:
    """Manages authentication for Claude Code sessions.

    Claude Code authenticates via OAuth (browser-based login tied to a Claude
    subscription).  The SDK handles the subprocess, but we expose helpers to
    check status and trigger login from the API layer.
    """

    def __init__(self, cli_path: str | None = None) -> None:
        self._cli = cli_path or shutil.which("claude") or "claude"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def is_authenticated(self) -> bool:
        """Return True if there is a valid auth session.

        Runs ``claude auth status`` which returns JSON with loggedIn status.
        This doesn't create a session unlike running a prompt.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                self._cli, "auth", "status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_get_auth_env(),
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode != 0:
                return False
            # Parse JSON response: {"loggedIn": true, ...}
            try:
                data = json.loads(stdout.decode())
                return data.get("loggedIn", False)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return False
        except (FileNotFoundError, asyncio.TimeoutError):
            return False

    async def login(self) -> bool:
        """Trigger the OAuth browser login flow.

        Runs ``claude setup-token`` which opens a browser for the user to
        authenticate via their Claude subscription.  Returns True if the
        process exits successfully.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                self._cli, "setup-token",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_get_auth_env(),
            )
            await proc.wait()
            return proc.returncode == 0
        except FileNotFoundError:
            return False

    @property
    def cli_path(self) -> str:
        """Path to the Claude CLI binary."""
        return self._cli
