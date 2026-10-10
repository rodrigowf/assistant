"""Claude Code: status from ``claude auth status``, three link logins, credentials paste, env keys.

Where the credential lives for the way Archie runs Claude Code (``CLAUDE_CONFIG_DIR`` =
``<repo>/.claude_config``):

* ``CLAUDE_CODE_OAUTH_TOKEN`` in ``context/.env`` — a 1-year subscription token printed by
  ``claude setup-token``. Synced to every machine with ``context/`` and forwarded to SSH remotes;
  wins over the file login. Archie's recommended setup.
* ``.claude_config/.credentials.json`` — this machine's own refreshing grant
  (``claude auth login``). Never copy it between machines (refresh tokens rotate).
* ``ANTHROPIC_API_KEY`` — API billing. Also used by the orchestrator's Anthropic models.

``claude auth login`` works over plain pipes (prints the URL, reads the code from stdin);
``claude setup-token`` is an Ink TUI that needs a terminal and prints the token, which the flow
stores in ``context/.env``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from utils.paths import PROJECT_ROOT

from . import envfile
from .base import AccountError, AccountService, Method
from .common import child_env, env_field, find_cli, key_set, run
from .files import atomic_write, iso_from_epoch, read_json
from .flows import FlowSpec, Scan, clean, find_url, redact

_AUTHORIZE = "/oauth/authorize"
_ERROR_RE = re.compile(r"(Login failed[^\n]*|OAuth error[^\n]*)")
# The token must be followed by something (whitespace in the cleaned text): output arrives in
# chunks and a token at the very end of the buffer may still be incomplete.
_TOKEN_RE = re.compile(r"sk-ant-oat\d{2}-[A-Za-z0-9_-]{20,}(?=\s)")
_LOGIN_DROP = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
API_KEY_ENV = "ANTHROPIC_API_KEY"

_PLANS = {"max": "Claude Max", "pro": "Claude Pro", "team": "Claude Team", "enterprise": "Claude Enterprise"}


def config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or PROJECT_ROOT / ".claude_config")


def credentials_path() -> Path:
    return config_dir() / ".credentials.json"


def claude_cli() -> str | None:
    """``claude`` on PATH, else the CLI bundled with claude-agent-sdk (what sessions run)."""
    bundled = ""
    try:
        spec = importlib.util.find_spec("claude_agent_sdk")
        if spec and spec.origin:
            bundled = str(Path(spec.origin).parent / "_bundled" / "claude")
    except (ImportError, ValueError):
        pass
    return find_cli("claude", "CLAUDE_CLI_PATH", extra=(bundled,))


def scan_login(raw: str) -> Scan:
    text = clean(raw)
    err = _ERROR_RE.search(text)
    return Scan(
        url=find_url(raw, "https://", _AUTHORIZE),
        error=err.group(1).strip() if err else None,
    )


def scan_setup_token(raw: str) -> Scan:
    found = scan_login(raw)
    token = _TOKEN_RE.search(clean(raw))
    return Scan(url=found.url, error=None if token else found.error, secret=token.group(0) if token else None)


def check_credentials_json(content: str) -> str:
    """The trimmed JSON if it is a Claude Code credentials file; raises AccountError otherwise."""
    text = (content or "").strip()
    try:
        data = json.loads(text)
    except ValueError:
        raise AccountError("That isn't valid JSON. Copy the whole file, including the braces.") from None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict) or not isinstance(oauth.get("accessToken"), str) or not oauth["accessToken"]:
        raise AccountError("Invalid credentials: the file has no claudeAiOauth.accessToken.")
    return text + "\n"


# Keys of a staged `.claude.json` that belong to the login (the rest is per-machine CLI state).
_LOGIN_STATE_KEYS = ("oauthAccount", "primaryApiKey")


def promote_login(staging: Path, target: Path) -> None:
    """Move the login a staged `claude auth login` wrote into the real config dir.

    `.credentials.json` is replaced (atomic, 0600, backup kept); from `.claude.json` only the
    login's own keys are merged in, so the running CLI's other state is left alone.
    """
    creds = read_json(staging / ".credentials.json")
    oauth = creds.get("claudeAiOauth") if isinstance(creds, dict) else None
    state = read_json(staging / ".claude.json")
    login_state = {k: state[k] for k in _LOGIN_STATE_KEYS if isinstance(state, dict) and k in state}
    has_creds = isinstance(oauth, dict) and bool(oauth.get("accessToken"))
    if not has_creds and not login_state.get("primaryApiKey"):
        raise AccountError("Claude Code reported success but saved no login.", 502)
    if has_creds:
        atomic_write(target / ".credentials.json", json.dumps(creds, indent=2) + "\n")
    if login_state:
        current = read_json(target / ".claude.json")
        merged = {**(current if isinstance(current, dict) else {}), **login_state}
        atomic_write(target / ".claude.json", json.dumps(merged, indent=2) + "\n")


def store_token(token: str) -> None:
    envfile.set_value(TOKEN_ENV, token)


class ClaudeAccount(AccountService):
    id = "claude"
    label = "Claude Code"
    group = "harness"
    description = "Agent sessions (Claude Code harness) and the CLI's model list."

    async def _cli_status(self) -> dict[str, Any] | None:
        cli = claude_cli()
        if not cli:
            return None
        env = child_env(cli=cli, CLAUDE_CONFIG_DIR=str(config_dir()))
        rc, out = await run([cli, "auth", "status", "--json"], env, timeout=15)
        if rc is None:
            return None
        try:
            start = out.index("{")
            data = json.loads(out[start:out.rindex("}") + 1])
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    async def status(self):
        st = self.new_status(used_by=["Claude Code agent sessions", "Claude model list"], docs="docs/harnesses/claude-code.md")
        creds = read_json(credentials_path())
        oauth = creds.get("claudeAiOauth") if isinstance(creds, dict) else None
        has_file = isinstance(oauth, dict) and bool(oauth.get("accessToken"))
        info = await self._cli_status()
        method = (info or {}).get("authMethod")
        if info is None:
            st.state = "signed_in" if (has_file or key_set(TOKEN_ENV) or key_set(API_KEY_ENV)) else "unknown"
            st.warnings.append("Couldn't run `claude auth status` on the server; showing what the files say.")
            method = "oauth_token" if key_set(TOKEN_ENV) else "claude.ai" if has_file else "api_key" if key_set(API_KEY_ENV) else None
        else:
            st.state = "signed_in" if info.get("loggedIn") else "signed_out"
        if method == "oauth_token":
            st.method = "Long-lived token (CLAUDE_CODE_OAUTH_TOKEN in context/.env)"
            st.detail = "Valid for a year from when it was made; shared by every machine that syncs context/."
            if has_file:
                st.detail += " This server's own login is also present but the token wins."
        elif method == "claude.ai":
            sub = (info or {}).get("subscriptionType") or (oauth or {}).get("subscriptionType")
            st.method = "This server's login (.claude_config/.credentials.json)"
            st.plan = _PLANS.get(str(sub).lower(), str(sub).title()) if sub else None
            st.account = (info or {}).get("email") or (info or {}).get("orgName")
            exp = (oauth or {}).get("refreshTokenExpiresAt")
            st.expires_at = iso_from_epoch(exp) if exp else None
            st.detail = "The access token refreshes by itself; the login itself lasts until the expiry shown."
        elif method == "api_key":
            st.method = "Anthropic API key (ANTHROPIC_API_KEY) — billed per token"
        elif method and method != "none":
            st.method = str(method)
        if (info or {}).get("apiKeySource") and method != "api_key":
            st.warnings.append("ANTHROPIC_API_KEY is set too. Claude Code can bill it instead of the subscription; remove it unless that's intended.")
        if not claude_cli():
            st.warnings.append("The claude CLI wasn't found on the server (PATH, claude-agent-sdk's bundled copy, nvm).")
        st.methods = self._methods(method, has_file)
        return st

    def _methods(self, active: str | None, has_file: bool) -> list[Method]:
        return [
            Method(
                id="token", kind="link", label="Sign in with a link · 1-year token", recommended=True,
                description=(
                    "Runs `claude setup-token` on the server. Open the link on any device, sign in with your Claude "
                    "subscription, paste the code back. The token is saved as CLAUDE_CODE_OAUTH_TOKEN in "
                    "context/.env, so every synced machine (and SSH sessions) uses it."
                ),
                needs_code=True, code_label="Code", code_help="The page shows a code after you sign in (it contains a #).",
                active=active == "oauth_token",
                warning=(
                    "A token is already set: the new one replaces CLAUDE_CODE_OAUTH_TOKEN only once the sign-in succeeds."
                    if key_set(TOKEN_ENV) else ""
                ),
            ),
            Method(
                id="login", kind="link", label="Sign in with a link · this server only",
                description=(
                    "Runs `claude auth login`: a refreshing login stored in .claude_config/.credentials.json on "
                    "this server only. Each machine needs its own."
                ),
                needs_code=True, code_label="Code", code_help="The page shows a code after you sign in (it contains a #).",
                active=active == "claude.ai",
                warning=(
                    "This server is signed in already: the new login replaces it only once it succeeds; cancelling or a failure keeps it."
                    if has_file else ""
                ),
            ),
            Method(
                id="console", kind="link", label="Anthropic Console account (API billing)",
                description="Runs `claude auth login --console`: signs in to the Anthropic Console and bills API usage instead of a subscription.",
                needs_code=True, code_label="Code", code_help="The page shows a code after you sign in.",
            ),
            Method(
                id="credentials", kind="credentials", label="Paste credentials JSON",
                description="Copy .credentials.json from a machine where Claude Code is signed in.",
                path=str(credentials_path()),
                source_hint="~/.claude/.credentials.json (or <repo>/.claude_config/.credentials.json for Archie)",
                placeholder='{"claudeAiOauth": {"accessToken": "…", "refreshToken": "…", …}}',
                warning=(
                    "Refresh tokens rotate: a login copied to two machines eventually breaks on one of them. "
                    "Prefer the 1-year token for more than one machine."
                ),
                active=active == "claude.ai",
            ),
            Method(
                id="keys", kind="env", label="Token or API key",
                description="Paste a token printed by `claude setup-token` elsewhere, or use an Anthropic API key.",
                fields=[
                    env_field(TOKEN_ENV, "Long-lived token", placeholder="sk-ant-oat01-…", help="From `claude setup-token`."),
                    env_field(API_KEY_ENV, "Anthropic API key", placeholder="sk-ant-api03-…",
                              help="Billed per token. Also used by the orchestrator's Anthropic models."),
                ],
                active=active in ("oauth_token", "api_key"),
            ),
            Method(
                id="signout", kind="signout", label="Sign out of this server's login",
                description=(
                    "Runs `claude auth logout`: this server's login (.credentials.json) is removed and has to be signed in again. "
                    "A token in context/.env stays; remove it under Token or API key."
                ),
                available=has_file, unavailable_reason="This server has no login of its own.",
            ),
        ]

    def flow_spec(self, method: str) -> FlowSpec:
        cli = claude_cli()
        if not cli:
            raise AccountError("The claude CLI isn't installed on the server.", 409)
        env = child_env(_LOGIN_DROP, cli=cli, CLAUDE_CONFIG_DIR=str(config_dir()))
        common = dict(service=self.id, method=method, env=env, needs_code=True, code_label="Code",
                      code_help="The page shows a code after you sign in.")
        if method == "token":
            # Success only from on_secret: an exit without a captured token saved nothing.
            return FlowSpec(argv=[cli, "setup-token"], pty=True, scan=scan_setup_token, on_secret=store_token,
                            exit_ok_is_success=False,
                            success_message="Signed in. The token is saved as CLAUDE_CODE_OAUTH_TOKEN in context/.env.",
                            **common)
        if method in ("login", "console"):
            # `claude auth login` logs out first — it blanks the tokens in .credentials.json the
            # moment it starts (seen on 2.1.295) — so it runs against a throwaway config dir and
            # the new login is moved into place only on success; the real file is also
            # snapshotted and restored on any other outcome.
            staging = Path(tempfile.mkdtemp(prefix="archie-claude-login-"))
            common["env"] = child_env(_LOGIN_DROP, cli=cli, CLAUDE_CONFIG_DIR=str(staging))
            flag = "--claudeai" if method == "login" else "--console"
            return FlowSpec(
                argv=[cli, "auth", "login", flag], scan=scan_login,
                success_message="Signed in on this server." if method == "login" else "Signed in with the Anthropic Console.",
                protect=(credentials_path(),), promote=lambda: promote_login(staging, config_dir()),
                on_close=lambda: shutil.rmtree(staging, ignore_errors=True), **common,
            )
        return super().flow_spec(method)

    async def save_credentials(self, method: str, content: str) -> str:
        if method != "credentials":
            return await super().save_credentials(method, content)
        text = check_credentials_json(content)
        backup = atomic_write(credentials_path(), text)
        return "Credentials saved." + (" The previous file was kept as a backup." if backup else "")

    async def sign_out(self) -> str:
        cli = claude_cli()
        if not cli:
            raise AccountError("The claude CLI isn't installed on the server.", 409)
        rc, out = await run([cli, "auth", "logout"], child_env(_LOGIN_DROP, cli=cli, CLAUDE_CONFIG_DIR=str(config_dir())), timeout=20)
        if rc != 0:
            raise AccountError(redact(f"`claude auth logout` failed: {out.strip()[-200:] or rc}"), 502)
        return "Signed out of this server's login."
