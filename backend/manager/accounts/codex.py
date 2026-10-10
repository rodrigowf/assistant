"""OpenAI Codex: device-code and browser logins, API key, ``auth.json`` paste, sign out.

Archie runs Codex with ``CODEX_HOME`` = the dedicated ``~/.codex-archie`` when it holds a login,
else the shared ``~/.codex`` (``manager.codex.home``). Logins started here always target the
dedicated home (or ``ARCHIE_CODEX_HOME``), so Archie gets its own login and its rollouts sync.

``codex login --device-auth`` is the headless method: a URL and a one-time code, approved on any
device; the CLI exits 0 when done. ``codex login`` (browser) redirects to a local server on
``127.0.0.1:1455`` *of the machine that runs the CLI* — on a headless server the user's browser
can't reach it, so the user pastes the address it lands on and the backend replays that request to
the CLI's local server itself.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from manager.codex import home as codex_home_mod

from .base import AccountError, AccountService, Method
from .common import child_env, run
from .files import atomic_write, jwt_claims, read_json
from .flows import FlowSpec, Scan, clean, find_url, redact

_DEVICE_CODE_RE = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4,6})\b")
_PORT_RE = re.compile(r"login server on http://(?:localhost|127\.0\.0\.1):(\d+)")
_ERROR_RE = re.compile(r"(?im)^\s*((?:error|login failed)[^\n]*)$")
_NOISE = ("WARNING: proceeding", "Welcome to Codex", "Continue only if")
_AUTH_CLAIMS = "https://api.openai.com/auth"


def login_home() -> Path:
    """Where logins started from Archie go: ``ARCHIE_CODEX_HOME``, else ``~/.codex-archie``."""
    override = os.environ.get("ARCHIE_CODEX_HOME")
    return Path(override).expanduser() if override else codex_home_mod.dedicated_home()


def codex_cli() -> str | None:
    exe = codex_home_mod.codex_executable()
    if os.path.isabs(exe):
        return exe if os.path.isfile(exe) else None
    return shutil.which(exe)


def _env(home: Path, cli: str) -> dict[str, str]:
    # Same stripping as codex_env(): a stray OPENAI_API_KEY must not become the login.
    return child_env(("OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_HOME"), cli=cli, CODEX_HOME=str(home))


def _staging_home() -> Path:
    """A private, empty CODEX_HOME for one login (0700; removed when the flow ends)."""
    return Path(tempfile.mkdtemp(prefix="archie-codex-login-"))


def _promote(staging: Path, home: Path) -> None:
    """Move the login a staged `codex login` wrote into Archie's home (atomic, 0600, backup kept)."""
    text = (staging / "auth.json").read_text(encoding="utf-8") if (staging / "auth.json").is_file() else ""
    if not text.strip():
        raise AccountError("Codex reported success but wrote no auth.json.", 502)
    check_auth_json(text)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write(home / "auth.json", text.strip() + "\n")


def scan_device(raw: str) -> Scan:
    text = clean(raw)
    code = None
    after = text.split("one-time code", 1)
    if len(after) == 2:
        m = _DEVICE_CODE_RE.search(after[1])
        code = m.group(1) if m else None
    err = _ERROR_RE.search(text)
    return Scan(
        url=find_url(raw, "https://auth.openai.com/"),
        user_code=code,
        error=err.group(1).strip() if err else None,
    )


def scan_browser(raw: str) -> Scan:
    text = clean(raw)
    err = _ERROR_RE.search(text)
    return Scan(
        url=find_url(raw, "https://auth.openai.com/oauth/authorize"),
        error=err.group(1).strip() if err else None,
    )


def callback_query(pasted: str) -> str:
    """The ``code``/``state`` query of a pasted redirect address (or of the bare query)."""
    text = pasted.strip()
    query = urlsplit(text).query if "://" in text else text.lstrip("?")
    params = parse_qs(query, keep_blank_values=True)
    if "error" in params:
        raise AccountError(f"The sign-in page returned an error: {params['error'][0]}")
    if not params.get("code") or not params.get("state"):
        raise AccountError("Paste the whole address your browser landed on — it contains code=… and state=….")
    return urlencode({k: v[0] for k, v in params.items()})


def check_auth_json(content: str) -> str:
    text = (content or "").strip()
    try:
        data = json.loads(text)
    except ValueError:
        raise AccountError("That isn't valid JSON. Copy the whole auth.json, including the braces.") from None
    if not isinstance(data, dict):
        raise AccountError("auth.json is a JSON object.")
    tokens = data.get("tokens")
    has_tokens = isinstance(tokens, dict) and bool(tokens.get("refresh_token") or tokens.get("access_token"))
    if not has_tokens and not data.get("OPENAI_API_KEY"):
        raise AccountError("This doesn't look like Codex's auth.json: it has neither tokens nor OPENAI_API_KEY.")
    return text + "\n"


class CodexAccount(AccountService):
    id = "codex"
    label = "Codex"
    group = "harness"
    description = "Agent sessions (OpenAI Codex harness), with your ChatGPT plan."

    async def status(self):
        st = self.new_status(used_by=["Codex agent sessions"], docs="docs/harnesses/codex-cli.md")
        home = codex_home_mod.codex_home()
        target = login_home()
        data = read_json(home / "auth.json")
        mode = data.get("auth_mode") if isinstance(data, dict) else None
        tokens = data.get("tokens") if isinstance(data, dict) else None
        shared = codex_home_mod.is_shared_fallback()
        st.detail = f"Login in {_tilde(home)}" + (" (shared with the Codex CLI / VS Code)" if shared else "")
        if not isinstance(data, dict):
            st.state = "signed_out"
        elif isinstance(tokens, dict) and tokens.get("refresh_token"):
            st.state = "signed_in"
            claims = jwt_claims(tokens.get("id_token"))
            auth = claims.get(_AUTH_CLAIMS) if isinstance(claims.get(_AUTH_CLAIMS), dict) else {}
            st.method = "ChatGPT sign-in"
            st.account = claims.get("email")
            plan = auth.get("chatgpt_plan_type")
            st.plan = f"ChatGPT {str(plan).title()}" if plan else None
            # No expiry: the tokens refresh by themselves (the id_token's `exp` and the claim
            # `chatgpt_subscription_active_until` are not the login's lifetime).
            st.detail += " · refreshes automatically"
            if data.get("last_refresh"):
                st.detail += f" · last refresh {str(data['last_refresh'])[:10]}"
        elif data.get("OPENAI_API_KEY") or mode == "apikey":
            st.state = "signed_in"
            st.method = "OpenAI API key (billed per token)"
        else:
            st.state = "signed_out"
        if shared and st.state == "signed_in":
            st.warnings.append(
                f"Archie is borrowing the shared {_tilde(home)} login. Sign in here to give it its own login in "
                f"{_tilde(target)} (a shared login can break when both clients refresh it)."
            )
        if (target / "auth.json").is_file() and not (target / "sessions").is_symlink():
            st.warnings.append(f"{_tilde(target)}/sessions isn't linked into context/: Codex rollouts won't sync (run the installer with --with-codex).")
        if not codex_cli():
            st.warnings.append("The codex CLI wasn't found on the server (CODEX_CLI_PATH, PATH, nvm).")
        st.methods = self._methods(st.state == "signed_in", st.method or "", target, own_login=(target / "auth.json").is_file())
        return st

    def _methods(self, signed_in: bool, method: str, target: Path, own_login: bool) -> list[Method]:
        replaces = (
            f"You're signed in already: a new sign-in replaces the login in {_tilde(target)} only once it succeeds; "
            "cancelling or a failure keeps the current one."
            if own_login else ""
        )
        return [
            Method(
                id="device", kind="link", label="Sign in with a device code", recommended=True,
                description=f"Runs `codex login --device-auth` for {_tilde(target)}. Open the link on any device, enter the code, approve; this page updates by itself.",
                active=signed_in and method.startswith("ChatGPT"), warning=replaces,
            ),
            Method(
                id="browser", kind="link", label="Sign in in the browser", warning=replaces,
                description="Runs `codex login`. After you sign in, the browser lands on a 127.0.0.1:1455 address that won't load on your device — paste that address here and the server finishes the login.",
                needs_code=True, code_label="Address the browser landed on",
                code_help="Copy the whole address from the browser's address bar (http://127.0.0.1:1455/auth/callback?code=…).",
            ),
            Method(
                id="apikey", kind="credentials", input="secret", label="OpenAI API key",
                description="Runs `codex login --with-api-key`: Codex bills the API key instead of a ChatGPT plan.",
                path=str(target / "auth.json"), placeholder="sk-…",
                active=signed_in and method.startswith("OpenAI API key"),
            ),
            Method(
                id="auth_json", kind="credentials", label="Paste auth.json",
                description="Moves an existing Codex login here.",
                path=str(target / "auth.json"), source_hint="~/.codex/auth.json on the machine that has the login",
                placeholder='{"auth_mode": "chatgpt", "tokens": {…}, "last_refresh": "…"}',
                warning="ChatGPT refresh tokens rotate: once pasted here, don't keep using the same login elsewhere, or one of the two stops working (refresh_token_reused).",
            ),
            Method(
                id="signout", kind="signout", label="Sign out",
                description=(
                    f"Runs `codex logout` for Archie's own login in {_tilde(target)}: the login is removed and Codex sessions "
                    "stop working until you sign in again (Archie falls back to ~/.codex if that has a login)."
                ),
                available=own_login,
                unavailable_reason=(
                    "Archie is borrowing the shared ~/.codex login, which the Codex CLI and VS Code also use; it is not signed out from here."
                    if signed_in else "Not signed in."
                ),
            ),
        ]

    def flow_spec(self, method: str) -> FlowSpec:
        """Logins run against a throwaway CODEX_HOME and are moved into place only on success.

        `codex login` logs out first — it deletes the home's auth.json the moment it starts (seen
        on 0.161.0) — so running it on the real home would destroy a working login the user then
        cancels. The real auth.json is also snapshotted and restored on any non-success.
        """
        cli = codex_cli()
        if not cli:
            raise AccountError("The codex CLI isn't installed on the server.", 409)
        home = login_home()
        staging = _staging_home()
        common = dict(
            service=self.id, method=method, env=_env(staging, cli), noise=_NOISE,
            success_message=f"Signed in ({_tilde(home)}).",
            protect=(home / "auth.json",),
            promote=lambda: _promote(staging, home),
            on_close=lambda: shutil.rmtree(staging, ignore_errors=True),
        )
        if method == "device":
            return FlowSpec(argv=[cli, "login", "--device-auth"], scan=scan_device, **common)
        if method == "browser":
            holder: dict[str, Any] = {}

            async def submit(code: str) -> None:
                query = callback_query(code)
                port = holder.get("port") or "1455"
                try:
                    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                        r = await client.get(f"http://127.0.0.1:{port}/auth/callback?{query}")
                except httpx.HTTPError as e:
                    raise AccountError(f"Couldn't reach Codex's login server on the server: {type(e).__name__}", 502) from None
                if r.status_code >= 400:
                    raise AccountError(f"Codex's login server refused the address ({r.status_code}). Start again and paste the newest address.", 502)

            def scan(raw: str) -> Scan:
                m = _PORT_RE.search(clean(raw))
                if m:
                    holder["port"] = m.group(1)
                return scan_browser(raw)

            return FlowSpec(argv=[cli, "login"], scan=scan, submit=submit, needs_code=True,
                            code_label="Address the browser landed on",
                            code_help="Copy the whole address from the browser's address bar.", **common)
        shutil.rmtree(staging, ignore_errors=True)
        return super().flow_spec(method)

    async def save_credentials(self, method: str, content: str) -> str:
        home = login_home()
        if method == "auth_json":
            text = check_auth_json(content)
            home.mkdir(mode=0o700, parents=True, exist_ok=True)
            backup = atomic_write(home / "auth.json", text)
            return f"Saved {_tilde(home)}/auth.json." + (" The previous file was kept as a backup." if backup else "")
        if method == "apikey":
            key = (content or "").strip()
            if not key or any(c.isspace() for c in key):
                raise AccountError("Paste the API key (one line, no spaces).")
            cli = codex_cli()
            if not cli:
                raise AccountError("The codex CLI isn't installed on the server.", 409)
            staging = _staging_home()  # `codex login` logs the home out first: never run it on the real one
            try:
                rc, out = await run([cli, "login", "--with-api-key"], _env(staging, cli), stdin=key + "\n", timeout=30)
                if rc == 0:
                    _promote(staging, home)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
            if rc != 0:
                raise AccountError(redact(f"`codex login --with-api-key` failed: {out.strip().splitlines()[-1] if out.strip() else rc}"), 502)
            return "Codex now uses the API key."
        return await super().save_credentials(method, content)

    async def sign_out(self) -> str:
        """Logs out Archie's own home only — never the shared ~/.codex (VS Code / the TUI use it)."""
        cli = codex_cli()
        if not cli:
            raise AccountError("The codex CLI isn't installed on the server.", 409)
        home = login_home()
        if not (home / "auth.json").is_file():
            raise AccountError(f"Archie has no login of its own in {_tilde(home)}; the shared ~/.codex login is left alone.", 409)
        rc, out = await run([cli, "logout"], _env(home, cli), timeout=20)
        if rc != 0:
            raise AccountError(redact(f"`codex logout` failed: {out.strip()[-200:] or rc}"), 502)
        return f"Signed out of {_tilde(home)}."


def _tilde(p: Path) -> str:
    home = str(Path.home())
    s = str(p)
    return "~" + s[len(home):] if s == home or s.startswith(home + os.sep) else s
