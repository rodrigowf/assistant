"""Accounts: sign-in status and sign-in methods for every service Archie uses, and the
``context/.env`` key manager. API: ``api/routes/accounts.py`` (``/api/accounts``, ``/api/env``).
Research notes per service: ``docs/harnesses/authentication.md``.

Names resolve lazily (PEP 562) so that importing a light submodule (``files``, ``envfile``) does
not import every harness package through the registry.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .base import AccountError, AccountService, EnvField, Method, ServiceStatus
    from .registry import AccountsManager, default_services

_LAZY = {
    "AccountError": "base", "AccountService": "base", "EnvField": "base", "Method": "base", "ServiceStatus": "base",
    "AccountsManager": "registry", "default_services": "registry",
}

__all__ = sorted(_LAZY)


def __getattr__(name: str) -> Any:
    mod = _LAZY.get(name)
    if mod is None:
        raise AttributeError(name)
    return getattr(importlib.import_module(f"{__name__}.{mod}"), name)
