"""FastAPI dependency injection — shared state accessors."""

from __future__ import annotations

from fastapi import Request

from manager.accounts import AccountsManager
from manager.auth import AuthManager
from manager.config import ManagerConfig
from manager.store import SessionStore

from .connections import ConnectionManager
from .pool import SessionPool


def get_config(request: Request) -> ManagerConfig:
    return request.app.state.config


def get_store(request: Request) -> SessionStore:
    return request.app.state.store


def get_auth(request: Request) -> AuthManager:
    return request.app.state.auth


def get_accounts(request: Request) -> AccountsManager:
    # Created in the lifespan; tests that build the app without it get one on first use.
    accounts = getattr(request.app.state, "accounts", None)
    if accounts is None:
        accounts = request.app.state.accounts = AccountsManager()
    return accounts


def get_connections(request: Request) -> ConnectionManager:
    return request.app.state.connections


def get_pool(request: Request) -> SessionPool:
    return request.app.state.pool
