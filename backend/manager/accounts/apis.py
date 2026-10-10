"""API-key services the backend itself calls (voice, orchestrator, search), plus read-only
status of other credentials Archie keeps (Google OAuth tokens of the personal scripts).

All keys live in ``context/.env`` and are read at call time, so a new key applies to the next
call without a restart.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

import httpx

from utils.paths import get_context_dir

from . import envfile
from .base import AccountError, AccountService, EnvField, Method
from .common import env_field, key_set, verify_http
from .files import read_json
from .gemini import verify_gemini_key
from .qwen import DASHSCOPE_ENV, verify_dashscope


class _KeyService(AccountService):
    """A service that is just one API key (plus optional extra settings)."""

    group = "api"
    key: str = ""
    key_label: str = ""
    placeholder: str = ""
    used: tuple[str, ...] = ()
    extra_fields: tuple[EnvField, ...] = ()
    method_description: str = ""

    async def status(self):
        st = self.new_status(used_by=list(self.used))
        st.state = "signed_in" if key_set(self.key) else "signed_out"
        st.method = f"{self.key_label} ({self.key})" if st.state == "signed_in" else None
        st.methods = [Method(
            id="keys", kind="env", label="API key", recommended=True, active=st.state == "signed_in",
            description=self.method_description,
            fields=[env_field(self.key, self.key_label, placeholder=self.placeholder), *[replace(f) for f in self.extra_fields]],
        )]
        return st


class OpenAIAccount(_KeyService):
    id = "openai"
    label = "OpenAI"
    description = "Realtime voice, talk mode, history re-ranking, session summaries, OpenAI orchestrator models."
    key, key_label, placeholder = "OPENAI_API_KEY", "OpenAI API key", "sk-…"
    used = ("OpenAI Realtime voice", "Voice messages (GPT Audio)", "History search re-rank + summaries", "Orchestrator (OpenAI models)")
    method_description = "From platform.openai.com/api-keys. Codex does NOT use this key (it has its own login)."

    async def verify(self) -> dict[str, Any]:
        key = envfile.effective(self.key)
        if not key:
            raise AccountError(f"Set {self.key} first.", 409)
        return await verify_http("https://api.openai.com/v1/models", {"Authorization": f"Bearer {key}"}, ok_message="The OpenAI key works.")


class AnthropicAccount(_KeyService):
    id = "anthropic"
    label = "Anthropic API"
    description = "Orchestrator with Claude models."
    key, key_label, placeholder = "ANTHROPIC_API_KEY", "Anthropic API key", "sk-ant-api03-…"
    used = ("Orchestrator (Anthropic models)",)
    method_description = (
        "From console.anthropic.com. Careful: Claude Code agent sessions also pick this key up and can bill it "
        "instead of your subscription."
    )

    async def verify(self) -> dict[str, Any]:
        key = envfile.effective(self.key)
        if not key:
            raise AccountError(f"Set {self.key} first.", 409)
        return await verify_http(
            "https://api.anthropic.com/v1/models?limit=1", {"x-api-key": key, "anthropic-version": "2023-06-01"},
            ok_message="The Anthropic API key works.",
        )


class GoogleAIAccount(_KeyService):
    id = "google_ai"
    label = "Google Gemini API"
    description = "Gemini Live voice (and the Gemini CLI harness)."
    key, key_label, placeholder = "GEMINI_API_KEY", "Gemini API key", "AIza…"
    used = ("Gemini Live voice", "Gemini CLI agent sessions", "Gemini model list")
    method_description = "From aistudio.google.com/apikey. The Vertex AI voice endpoint uses the project below with application-default credentials instead."
    extra_fields = (
        EnvField(name="GCP_PROJECT_ID", label="Vertex project (voice)", secret=False, help="Numeric Cloud project ID for the Vertex AI voice endpoint."),
        EnvField(name="GCP_LOCATION", label="Vertex location (voice)", secret=False, placeholder="us-central1"),
    )

    async def verify(self) -> dict[str, Any]:
        key = envfile.effective(self.key)
        if not key:
            raise AccountError(f"Set {self.key} first.", 409)
        return await verify_gemini_key(key)


class DashScopeAccount(_KeyService):
    id = "dashscope"
    label = "Alibaba DashScope"
    description = "Qwen voice (Qwen-Omni realtime), Qwen Code and Model Studio."
    key, key_label, placeholder = DASHSCOPE_ENV, "DashScope API key", "sk-…"
    used = ("Qwen voice", "Qwen Code agent sessions", "Model Studio sessions")
    method_description = "From the Alibaba Cloud Model Studio console (international)."

    async def verify(self) -> dict[str, Any]:
        return await verify_dashscope()


class BrowserExtensionAccount(_KeyService):
    group = "other"
    id = "browser"
    label = "Browser extension"
    description = "Shared token between the backend and the Chrome extension behind /browser-control."
    key, key_label, placeholder = "BROWSER_CONTROL_TOKEN", "Shared token", ""
    used = ("Browser control (Chrome extension)",)
    method_description = "Any long random string; the extension's options page must hold the same value."


class GoogleOAuthAccount(AccountService):
    """Read-only: the OAuth token files personal scripts keep in ``context/secrets/``."""

    id = "google_oauth"
    label = "Google OAuth tokens"
    group = "other"
    description = "Logins of the personal Google scripts (YouTube, Drive, Photos, Ads)."

    def _files(self) -> list[tuple[str, dict[str, Any]]]:
        folder = get_context_dir() / "secrets"
        out = []
        for p in sorted(folder.glob("*_tokens.json")) if folder.is_dir() else []:
            data = read_json(p)
            if isinstance(data, dict):
                out.append((p.name, data))
        return out

    async def status(self):
        st = self.new_status(used_by=["context/scripts Google tools"])
        files = self._files()
        with_refresh = [n for n, d in files if d.get("refresh_token")]
        st.state = "signed_in" if with_refresh else "signed_out"
        st.method = f"{len(with_refresh)} token file(s) in context/secrets" if files else None
        st.detail = ", ".join(n.replace("_tokens.json", "") for n, _ in files) or "No token files."
        st.methods = []
        return st

    async def verify(self) -> dict[str, Any]:
        """Refresh each token (Google refresh tokens don't rotate; the new access token is discarded)."""
        results = []
        async with httpx.AsyncClient(timeout=8.0) as client:
            for name, d in self._files():
                label = name.replace("_tokens.json", "")
                if not all(d.get(k) for k in ("refresh_token", "client_id", "client_secret")):
                    results.append(f"{label}: no refresh token")
                    continue
                try:
                    r = await client.post(d.get("token_uri") or "https://oauth2.googleapis.com/token", data={
                        "grant_type": "refresh_token", "refresh_token": d["refresh_token"],
                        "client_id": d["client_id"], "client_secret": d["client_secret"],
                    })
                    results.append(f"{label}: {'ok' if r.status_code == 200 else f'rejected ({r.status_code})'}")
                except httpx.HTTPError as e:
                    results.append(f"{label}: unreachable ({type(e).__name__})")
        ok = bool(results) and all(r.endswith(": ok") for r in results)
        return {"ok": ok, "message": "; ".join(results) or "No token files.", "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
