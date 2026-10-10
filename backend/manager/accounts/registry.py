"""``AccountsManager``: the services in display order, their status, and the flows.

Statuses are computed on demand (no cache): reading a few files plus ``claude auth status``
(~1 s). Every service runs concurrently and a failing one degrades to ``state: "unknown"`` with
the error as a warning instead of failing the whole page.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from . import envfile
from .apis import (
    AnthropicAccount,
    BrowserExtensionAccount,
    DashScopeAccount,
    GoogleAIAccount,
    GoogleOAuthAccount,
    OpenAIAccount,
)
from .base import AccountError, AccountService, ServiceStatus
from .claude import ClaudeAccount
from .codex import CodexAccount
from .flows import FlowManager
from .gemini import GeminiAccount
from .qwen import ModelStudioAccount, QwenAccount

logger = logging.getLogger(__name__)

STATUS_TIMEOUT_S = 20.0


def default_services() -> list[AccountService]:
    return [
        ClaudeAccount(), CodexAccount(), GeminiAccount(), QwenAccount(), ModelStudioAccount(),
        OpenAIAccount(), GoogleAIAccount(), DashScopeAccount(), AnthropicAccount(),
        BrowserExtensionAccount(), GoogleOAuthAccount(),
    ]


class AccountsManager:
    def __init__(self, services: list[AccountService] | None = None) -> None:
        self._services = {s.id: s for s in (services or default_services())}
        self.flows = FlowManager()
        self._verified: dict[str, dict[str, Any]] = {}

    def service(self, service_id: str) -> AccountService:
        svc = self._services.get(service_id)
        if svc is None:
            raise AccountError(f"Unknown service {service_id!r}.", 404)
        return svc

    @property
    def ids(self) -> list[str]:
        return list(self._services)

    async def status(self, service_id: str) -> dict[str, Any]:
        svc = self.service(service_id)
        try:
            st = await asyncio.wait_for(svc.status(), STATUS_TIMEOUT_S)
        except AccountError:
            raise
        except Exception as e:  # noqa: BLE001 — one broken service must not break the page
            logger.exception("accounts: status of %s failed", service_id)
            st = svc.new_status(state="unknown", warnings=[f"Couldn't check: {type(e).__name__}: {e}"])
        st.can_verify = type(svc).verify is not AccountService.verify
        return self._finish(st)

    async def status_all(self) -> list[dict[str, Any]]:
        return list(await asyncio.gather(*(self.status(i) for i in self._services)))

    def _finish(self, st: ServiceStatus) -> dict[str, Any]:
        for m in st.methods:
            for f in m.fields:
                value = envfile.effective(f.name)
                f.set = bool(value.strip())
                f.preview = envfile.mask(value) if f.secret else ""
                f.value = None if f.secret else value
        flow = self.flows.get(st.id)
        st.flow = flow.to_dict() if flow else None
        st.verified = self._verified.get(st.id)
        return st.to_dict()

    async def verify(self, service_id: str) -> dict[str, Any]:
        self._verified[service_id] = await self.service(service_id).verify()
        return await self.status(service_id)

    def forget_verification(self, service_id: str | None = None) -> None:
        if service_id is None:
            self._verified.clear()
        else:
            self._verified.pop(service_id, None)
