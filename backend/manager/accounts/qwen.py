"""Qwen Code and Model Studio: API keys only.

Qwen Code's own login (Qwen OAuth, ``~/.qwen/oauth_creds.json``) gave free inference until Alibaba
discontinued it on 2026-04-15; Qwen Code 0.25 marks ``qwen auth`` "(removed)". What remains are
provider keys: ``~/.qwen/settings.json`` declares ``modelProviders`` entries that name the env var
holding their key (``envKey``) — Archie's install uses ``DASHSCOPE_API_KEY`` (Alibaba Model
Studio's OpenAI-compatible endpoint). The Model Studio harness (Claude Code against DashScope's
Anthropic-compatible endpoint) uses the same key.
"""

from __future__ import annotations

import os
from typing import Any

from manager.qwen.models import _settings_path as qwen_settings_path

from . import envfile
from .base import AccountError, AccountService, EnvField, Method
from .common import env_field, key_set, verify_http
from .files import read_json

DASHSCOPE_ENV = "DASHSCOPE_API_KEY"
_DASHSCOPE_MODELS = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/models"


def qwen_settings() -> dict[str, Any]:
    data = read_json(qwen_settings_path())
    return data if isinstance(data, dict) else {}


def provider_env_keys(settings: dict[str, Any]) -> list[str]:
    """The ``envKey`` names the user's model providers authenticate with, in order."""
    keys: list[str] = []
    providers = settings.get("modelProviders")
    if isinstance(providers, dict):
        for entries in providers.values():
            for e in entries if isinstance(entries, list) else []:
                k = e.get("envKey") if isinstance(e, dict) else None
                if isinstance(k, str) and envfile.NAME_RE.match(k) and k not in keys:
                    keys.append(k)
    return keys


async def verify_dashscope() -> dict[str, Any]:
    key = envfile.effective(DASHSCOPE_ENV)
    if not key:
        raise AccountError("Set DASHSCOPE_API_KEY first.", 409)
    return await verify_http(_DASHSCOPE_MODELS, {"Authorization": f"Bearer {key}"}, ok_message="The DashScope key works.")


class QwenAccount(AccountService):
    id = "qwen"
    label = "Qwen Code"
    group = "harness"
    description = "Agent sessions (Qwen Code harness)."

    async def status(self):
        st = self.new_status(used_by=["Qwen Code agent sessions", "Qwen model list"], docs="docs/harnesses/qwen-code.md")
        settings = qwen_settings()
        security = settings.get("security") if isinstance(settings.get("security"), dict) else {}
        auth = security.get("auth") if isinstance(security.get("auth"), dict) else {}
        selected = auth.get("selectedType")
        keys = provider_env_keys(settings) or [DASHSCOPE_ENV]
        settings_env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
        ready = [k for k in keys if key_set(k) or settings_env.get(k)]
        if selected == "qwen-oauth":
            st.state = "unavailable"
            st.method = "Qwen OAuth (discontinued)"
            st.warnings.append("Qwen OAuth was discontinued on 2026-04-15. Switch ~/.qwen/settings.json to an API-key provider.")
        else:
            st.state = "signed_in" if ready else "signed_out"
            st.method = f"API key ({', '.join(ready)})" if ready else None
        st.detail = f"Providers from {qwen_settings_path()}" + (f" · auth type {selected}" if selected else "")
        only_in_settings = [k for k in ready if not key_set(k)]
        if only_in_settings:
            st.warnings.append(f"{', '.join(only_in_settings)} is only in ~/.qwen/settings.json (env block), not in context/.env.")
        fields = [env_field(k, k, help="Referenced by a model provider in ~/.qwen/settings.json." if k != DASHSCOPE_ENV else "Alibaba Model Studio (DashScope). Shared with Model Studio and Qwen voice.") for k in keys]
        st.methods = [
            Method(id="keys", kind="env", label="API key", recommended=True, active=bool(ready), fields=fields,
                   description="Qwen Code authenticates with the key its model provider names (envKey)."),
            Method(id="qwen_oauth", kind="link", label="Qwen OAuth", available=False,
                   unavailable_reason="Discontinued by Alibaba on 2026-04-15 (Qwen Code marks `qwen auth` removed).",
                   description="The free Qwen login."),
        ]
        return st

    async def verify(self) -> dict[str, Any]:
        return await verify_dashscope()


class ModelStudioAccount(AccountService):
    id = "modelstudio"
    label = "Model Studio"
    group = "harness"
    description = "Claude Code · Model Studio sessions (GLM, DeepSeek, Kimi, Qwen via DashScope)."

    async def status(self):
        st = self.new_status(used_by=["Claude Code · Model Studio sessions", "Model Studio model list"], docs="docs/harnesses/model-studio.md")
        st.state = "signed_in" if key_set(DASHSCOPE_ENV) else "signed_out"
        st.method = "DashScope API key" if st.state == "signed_in" else None
        base = os.environ.get("MODELSTUDIO_ANTHROPIC_BASE_URL")
        st.detail = f"Endpoint: {base}" if base else "Endpoint: the international (Singapore) Anthropic-compatible endpoint"
        st.methods = [Method(
            id="keys", kind="env", label="API key", recommended=True, active=st.state == "signed_in",
            description="A DashScope (Alibaba Model Studio) API key. Shared with Qwen Code and Qwen voice.",
            fields=[
                env_field(DASHSCOPE_ENV, "DashScope API key", placeholder="sk-…"),
                EnvField(name="MODELSTUDIO_ANTHROPIC_BASE_URL", label="Endpoint override (optional)", secret=False,
                         help="e.g. the Beijing region or the Coding Plan endpoint."),
            ],
        )]
        return st

    async def verify(self) -> dict[str, Any]:
        return await verify_dashscope()
