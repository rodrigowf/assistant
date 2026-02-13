"""Tests for api/routes/auth.py — auth endpoints."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from api.deps import get_auth


def _make_app(authenticated=True, login_result=True):
    app = create_app()
    mock_auth = MagicMock()
    mock_auth.is_authenticated = AsyncMock(return_value=authenticated)
    mock_auth.login = AsyncMock(return_value=login_result)
    app.dependency_overrides[get_auth] = lambda: mock_auth
    return app


@pytest.fixture
async def client_authed():
    app = _make_app(authenticated=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def client_unauthed():
    app = _make_app(authenticated=False, login_result=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestAuthStatus:
    async def test_authenticated(self, client_authed):
        resp = await client_authed.get("/api/auth/status")
        assert resp.status_code == 200
        assert resp.json() == {"authenticated": True}

    async def test_not_authenticated(self, client_unauthed):
        resp = await client_unauthed.get("/api/auth/status")
        assert resp.status_code == 200
        assert resp.json() == {"authenticated": False}


class TestAuthLogin:
    async def test_login_success(self, client_authed):
        resp = await client_authed.post("/api/auth/login")
        assert resp.status_code == 200
        assert resp.json() == {"authenticated": True}

    async def test_login_failure(self, client_unauthed):
        resp = await client_unauthed.post("/api/auth/login")
        assert resp.status_code == 200
        assert resp.json() == {"authenticated": False}
