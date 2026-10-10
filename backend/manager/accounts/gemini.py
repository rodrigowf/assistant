"""Gemini CLI: API key (default), Google sign-in, Vertex AI, ``oauth_creds.json`` paste.

Archie runs the CLI with auth type ``${ARCHIE_GEMINI_AUTH_TYPE:-gemini-api-key}``
(``manager.gemini.workspace_settings``), so what counts is that variable plus the credential the
type needs:

* ``gemini-api-key`` — ``GEMINI_API_KEY`` (AI Studio). The only option for personal Google
  accounts since Google stopped serving them through the CLI on 2026-06-18.
* ``oauth-personal`` — a Google login in ``~/.gemini/oauth_creds.json``; works for Gemini Code
  Assist Standard/Enterprise (with ``GOOGLE_CLOUD_PROJECT``). The CLI has no login command: its
  TUI, started with ``NO_BROWSER=true``, prints a URL and asks for the code Google shows
  (``codeassist.google.com/authcode``). The flow drives that TUI in a terminal and stops it once
  the credentials file is written.
* ``vertex-ai`` — ``GOOGLE_API_KEY`` (express mode) or ``GOOGLE_CLOUD_PROJECT`` +
  ``GOOGLE_CLOUD_LOCATION`` with application-default credentials.

Google refresh tokens don't rotate, so ``oauth_creds.json`` can be copied between machines.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from manager.gemini.workspace_settings import ENV_AUTH_TYPE

from . import envfile
from .base import AccountError, AccountService, Method
from .common import child_env, env_field, find_cli, key_set, verify_http
from .files import atomic_write, iso_from_epoch, move_aside, read_json
from .flows import FlowSpec, Scan, clean, find_url

API_KEY_ENV = "GEMINI_API_KEY"
_ERROR_RE = re.compile(r"(Failed to authenticate[^\n]*|IneligibleTierError[^\n]*|Authorization timed out[^\n]*)")
_TYPE_LABELS = {
    "gemini-api-key": "Gemini API key",
    "oauth-personal": "Google sign-in (Code Assist)",
    "vertex-ai": "Vertex AI",
}


def gemini_home() -> Path:
    explicit = os.environ.get("GEMINI_HOME")
    return Path(explicit).expanduser() if explicit else Path.home() / ".gemini"


def creds_path() -> Path:
    return gemini_home() / "oauth_creds.json"


def gemini_cli() -> str | None:
    return find_cli("gemini", "GEMINI_CLI_PATH")


def auth_type() -> str:
    return (os.environ.get(ENV_AUTH_TYPE) or "").strip() or "gemini-api-key"


def scan_google(raw: str) -> Scan:
    text = clean(raw)
    err = _ERROR_RE.search(text)
    return Scan(
        url=find_url(raw, "https://accounts.google.com/"),
        error=err.group(1).strip() if err else None,
    )


def check_oauth_creds(content: str) -> str:
    text = (content or "").strip()
    try:
        data = json.loads(text)
    except ValueError:
        raise AccountError("That isn't valid JSON. Copy the whole oauth_creds.json, including the braces.") from None
    if not isinstance(data, dict) or not (data.get("refresh_token") or data.get("access_token")):
        raise AccountError("This doesn't look like Gemini's oauth_creds.json: it has no refresh_token.")
    return text + "\n"


def _has_creds(p: Path) -> bool:
    data = read_json(p)
    return isinstance(data, dict) and bool(data.get("refresh_token") or data.get("access_token"))


def promote_login(staged: Path, home: Path) -> None:
    """Copy the login a staged Gemini CLI wrote (oauth_creds.json, google_accounts.json) into
    the real ~/.gemini (atomic, 0600, backups kept)."""
    if not _has_creds(staged / "oauth_creds.json"):
        raise AccountError("The Gemini CLI reported success but saved no login.", 502)
    for name in ("oauth_creds.json", "google_accounts.json"):
        if (staged / name).is_file():
            atomic_write(home / name, (staged / name).read_text(encoding="utf-8"))


class GeminiAccount(AccountService):
    id = "gemini"
    label = "Gemini CLI"
    group = "harness"
    description = "Agent sessions (Gemini CLI harness)."

    async def status(self):
        st = self.new_status(used_by=["Gemini CLI agent sessions", "Gemini model list"], docs="docs/harnesses/gemini-cli.md")
        kind = auth_type()
        creds = read_json(creds_path())
        has_creds = isinstance(creds, dict) and bool(creds.get("refresh_token") or creds.get("access_token"))
        st.method = _TYPE_LABELS.get(kind, kind)
        if kind == "gemini-api-key":
            st.state = "signed_in" if key_set(API_KEY_ENV) else "signed_out"
            st.detail = "GEMINI_API_KEY in context/.env (shared with Gemini Live voice)."
        elif kind == "oauth-personal":
            st.state = "signed_in" if has_creds else "signed_out"
            accounts = read_json(gemini_home() / "google_accounts.json")
            st.account = accounts.get("active") if isinstance(accounts, dict) else None
            if has_creds:
                st.detail = f"{creds_path()} — the access token refreshes by itself"
                exp = iso_from_epoch(creds.get("expiry_date"))
                if exp:
                    st.detail += f" (current one until {exp[11:16]} UTC)"
            st.warnings.append(
                "Google stopped serving personal accounts (free tier, Google AI Pro/Ultra) through the Gemini CLI on "
                "2026-06-18; a Google login only works for Gemini Code Assist Standard/Enterprise. Personal accounts: use an API key."
            )
        elif kind == "vertex-ai":
            ok = key_set("GOOGLE_API_KEY") or (key_set("GOOGLE_CLOUD_PROJECT") and key_set("GOOGLE_CLOUD_LOCATION"))
            st.state = "signed_in" if ok else "signed_out"
            st.detail = "GOOGLE_API_KEY (express mode), or GOOGLE_CLOUD_PROJECT + GOOGLE_CLOUD_LOCATION with application-default credentials."
        else:
            st.state = "unknown"
            st.detail = f"{ENV_AUTH_TYPE}={kind} — not one Archie knows how to check."
        if not gemini_cli():
            st.warnings.append("The gemini CLI wasn't found on the server (GEMINI_CLI_PATH, PATH, nvm).")
        st.methods = self._methods(kind, has_creds)
        return st

    def _methods(self, kind: str, has_creds: bool) -> list[Method]:
        return [
            Method(
                id="api_key", kind="env", label="API key", recommended=True, active=kind == "gemini-api-key",
                description="A Gemini API key from Google AI Studio (aistudio.google.com/apikey). Also used by Gemini Live voice.",
                fields=[env_field(API_KEY_ENV, "Gemini API key", placeholder="AIza…")],
            ),
            Method(
                id="mode", kind="env", label="Sign-in type",
                description="Which credential the Gemini CLI uses in Archie's sessions.",
                fields=[env_field(
                    ENV_AUTH_TYPE, "Sign-in type", secret=False,
                    choices=[
                        {"value": "", "label": "API key (default)"},
                        {"value": "oauth-personal", "label": "Google sign-in (Code Assist Standard/Enterprise)"},
                        {"value": "vertex-ai", "label": "Vertex AI"},
                    ],
                )],
            ),
            Method(
                id="google", kind="link", label="Sign in with Google",
                description=(
                    "Starts the Gemini CLI's own Google sign-in on the server. Open the link, sign in, paste the code "
                    "Google shows. Only Code Assist Standard/Enterprise accounts can use it; set Sign-in type to Google sign-in."
                ),
                needs_code=True, code_label="Authorization code", code_help="Google shows it after you sign in.",
                active=kind == "oauth-personal" and has_creds,
                warning="A Google login exists already: the new one replaces it only once the sign-in succeeds." if has_creds else "",
            ),
            Method(
                id="oauth_creds", kind="credentials", label="Paste oauth_creds.json",
                description="Copy the Google login from a machine where the Gemini CLI is signed in.",
                path=str(creds_path()), source_hint="~/.gemini/oauth_creds.json",
                placeholder='{"access_token": "…", "refresh_token": "…", "expiry_date": …}',
                active=kind == "oauth-personal" and has_creds,
            ),
            Method(
                id="vertex", kind="env", label="Vertex AI", active=kind == "vertex-ai",
                description="Set Sign-in type to Vertex AI, then either an API key (express mode) or a project + location (application-default credentials).",
                fields=[
                    env_field("GOOGLE_API_KEY", "Vertex API key (express mode)", placeholder="AIza…"),
                    env_field("GOOGLE_CLOUD_PROJECT", "Project ID", secret=False),
                    env_field("GOOGLE_CLOUD_LOCATION", "Location", secret=False, placeholder="us-central1"),
                    env_field("GOOGLE_APPLICATION_CREDENTIALS", "Service-account key file (path on the server)", secret=False),
                ],
            ),
            Method(
                id="signout", kind="signout", label="Sign out of Google",
                description="The Google login (oauth_creds.json) is removed — moved aside as a backup — and has to be signed in again. The API key is not touched.",
                available=has_creds, unavailable_reason="No Google login on the server.",
            ),
        ]

    def flow_spec(self, method: str) -> FlowSpec:
        if method != "google":
            return super().flow_spec(method)
        cli = gemini_cli()
        if not cli:
            raise AccountError("The gemini CLI isn't installed on the server.", 409)
        # A fresh private home: the TUI starts outside any project (no workspace settings, no trust
        # prompt), always asks for a new login (an existing one would just be used), and never
        # touches the real ~/.gemini; the new login is moved into place only on success.
        staging = Path(tempfile.mkdtemp(prefix="archie-gemini-login-"))
        (staging / "work").mkdir()
        staged = staging / ".gemini" / "oauth_creds.json"
        env = child_env(
            ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI"), cli=cli,
            HOME=str(staging), GEMINI_CLI_HOME=str(staging),
            NO_BROWSER="true", GEMINI_CLI_NO_RELAUNCH="true", GEMINI_CLI_AUTH_OVERRIDE="oauth-personal",
        )
        return FlowSpec(
            service=self.id, method=method, argv=[cli], env=env, cwd=str(staging / "work"), pty=True, scan=scan_google,
            on_close=lambda: shutil.rmtree(staging, ignore_errors=True),
            protect=(creds_path(), gemini_home() / "google_accounts.json"),
            promote=lambda: promote_login(staging / ".gemini", gemini_home()),
            needs_code=True, code_label="Authorization code", code_help="Google shows it after you sign in.",
            watch=lambda: _has_creds(staged), exit_ok_is_success=False, timeout_s=300,
            success_message=f"Signed in. Set Sign-in type to Google sign-in for Archie's sessions to use it ({ENV_AUTH_TYPE}).",
        )

    async def save_credentials(self, method: str, content: str) -> str:
        if method != "oauth_creds":
            return await super().save_credentials(method, content)
        backup = atomic_write(creds_path(), check_oauth_creds(content))
        return "Saved oauth_creds.json." + (" The previous file was kept as a backup." if backup else "")

    async def sign_out(self) -> str:
        moved = move_aside(creds_path())
        if not moved:
            raise AccountError("No Google login on the server.", 409)
        return f"Signed out of Google (the file was kept as {moved.name})."

    async def verify(self) -> dict[str, Any]:
        key = envfile.effective(API_KEY_ENV)
        if not key:
            raise AccountError("Set GEMINI_API_KEY first.", 409)
        return await verify_gemini_key(key)


async def verify_gemini_key(key: str) -> dict[str, Any]:
    return await verify_http(
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
        {"x-goog-api-key": key}, ok_message="The Gemini API key works.",
    )
