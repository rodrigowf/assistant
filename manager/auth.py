"""Auth helper — detect auth state and trigger OAuth browser flow."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


def _get_auth_env() -> dict[str, str]:
    """Get environment for auth commands.

    Unset CLAUDECODE to allow running auth checks inside a Claude Code session.
    Keep CLAUDE_CONFIG_DIR so credentials are read from the project folder.
    """
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    return env


def _get_credentials_path() -> Path:
    """Get path to Claude credentials file."""
    return Path.home() / ".claude" / ".credentials.json"


class AuthManager:
    """Manages authentication for Claude Code sessions.

    Claude Code authenticates via OAuth (browser-based login tied to a Claude
    subscription).  The SDK handles the subprocess, but we expose helpers to
    check status and trigger login from the API layer.

    Supports two modes:
    1. CLI-based: Uses `claude auth status` and `claude setup-token`
    2. Headless: Directly reads/writes credentials file for environments
       without a working CLI (e.g., remote servers)
    """

    # Claude OAuth URLs for headless auth
    AUTH_URL = "https://console.anthropic.com/settings/workspaces/default/oauth_tokens"
    # Token refresh endpoint (OAuth2 standard)
    TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"

    def __init__(self, cli_path: str | None = None, headless: bool = False) -> None:
        self._cli = cli_path or shutil.which("claude") or "claude"
        self._headless = headless
        self._credentials_path = _get_credentials_path()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def is_authenticated(self) -> bool:
        """Return True if there is a valid auth session.

        In headless mode, checks the credentials file directly.
        Otherwise runs ``claude auth status`` which also refreshes expired tokens.
        """
        # If not headless, try CLI first (it handles token refresh)
        if not self._headless:
            if await self._cli_auth_status():
                return True

        # Fall back to credentials file check
        if self._check_credentials_file():
            return True

        return False

    def _check_credentials_file(self, allow_refresh: bool = True) -> bool:
        """Check if credentials file exists and has valid tokens.

        If token is expired and allow_refresh is True, attempts to refresh it.
        """
        try:
            if not self._credentials_path.exists():
                return False

            data = json.loads(self._credentials_path.read_text())
            oauth = data.get("claudeAiOauth", {})

            # Check if we have an access token
            if not oauth.get("accessToken"):
                return False

            # Check if token is expired (with 1 min buffer)
            expires_at = oauth.get("expiresAt", 0)
            if expires_at and expires_at < (time.time() * 1000) + 60000:
                # Token is expired - try to refresh if we have a refresh token
                if allow_refresh and oauth.get("refreshToken"):
                    logger.info("Access token expired, attempting refresh...")
                    if self._refresh_token_sync(data):
                        # Refresh succeeded, check again (without allow_refresh to avoid recursion)
                        return self._check_credentials_file(allow_refresh=False)
                return False

            return True
        except (json.JSONDecodeError, OSError):
            return False

    def _refresh_token_sync(self, credentials_data: dict) -> bool:
        """Synchronously refresh the access token using the refresh token.

        This updates the credentials file in place if successful.
        """
        oauth = credentials_data.get("claudeAiOauth", {})
        refresh_token = oauth.get("refreshToken")
        if not refresh_token:
            return False

        try:
            # Make the token refresh request
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    self.TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                    },
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )

                if response.status_code != 200:
                    logger.warning(f"Token refresh failed: {response.status_code} {response.text[:200]}")
                    return False

                token_data = response.json()

                # Update the credentials
                oauth["accessToken"] = token_data["access_token"]
                if "refresh_token" in token_data:
                    oauth["refreshToken"] = token_data["refresh_token"]

                # Calculate new expiry (token_data has expires_in in seconds)
                expires_in = token_data.get("expires_in", 3600)  # Default 1 hour
                oauth["expiresAt"] = int(time.time() * 1000) + (expires_in * 1000)

                # Update scopes if provided
                if "scope" in token_data:
                    oauth["scopes"] = token_data["scope"].split()

                # Write back to file
                self._credentials_path.write_text(json.dumps(credentials_data))
                logger.info("Token refresh successful, credentials updated")
                return True

        except Exception as e:
            logger.warning(f"Token refresh error: {e}")
            return False

    async def _cli_auth_status(self) -> bool:
        """Check auth status using CLI."""
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

        Note: In headless mode, use set_credentials() instead.
        """
        if self._headless:
            # Can't open browser in headless mode
            return False

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

    def get_auth_url(self) -> str:
        """Get the URL for manual OAuth token generation.

        Users can visit this URL to generate an OAuth token, then paste
        the full credentials JSON into set_credentials().
        """
        return self.AUTH_URL

    def set_credentials(self, credentials_json: str) -> bool:
        """Set credentials directly from JSON string.

        This is used for headless authentication where the user manually
        copies their credentials from another authenticated machine.

        Args:
            credentials_json: Full contents of .credentials.json file

        Returns:
            True if credentials were successfully saved
        """
        try:
            # Validate JSON
            data = json.loads(credentials_json)

            # Basic validation - must have OAuth data
            if "claudeAiOauth" not in data:
                return False

            oauth = data["claudeAiOauth"]
            if not oauth.get("accessToken"):
                return False

            # Ensure directory exists
            self._credentials_path.parent.mkdir(parents=True, exist_ok=True)

            # Write credentials with secure permissions
            self._credentials_path.write_text(credentials_json)
            self._credentials_path.chmod(0o600)

            return True
        except (json.JSONDecodeError, OSError):
            return False

    @property
    def cli_path(self) -> str:
        """Path to the Claude CLI binary."""
        return self._cli

    @property
    def is_headless(self) -> bool:
        """Whether running in headless mode."""
        return self._headless
