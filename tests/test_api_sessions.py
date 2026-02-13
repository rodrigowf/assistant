"""Tests for api/routes/sessions.py — REST session endpoints."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from api.deps import get_store
from manager.types import MessagePreview, SessionDetail, SessionInfo


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 2, 5, 12, 0, 0, tzinfo=timezone.utc)


def _sample_sessions():
    return [
        SessionInfo(
            session_id="s1",
            started_at=_NOW,
            last_activity=_NOW,
            title="First session",
            message_count=5,
        ),
        SessionInfo(
            session_id="s2",
            started_at=_NOW,
            last_activity=_NOW,
            title="Second session",
            message_count=3,
        ),
    ]


def _sample_detail():
    return SessionDetail(
        session_id="s1",
        started_at=_NOW,
        last_activity=_NOW,
        title="First session",
        message_count=5,
        messages=[
            MessagePreview(role="user", text="Hello", timestamp=_NOW),
            MessagePreview(role="assistant", text="Hi there", timestamp=_NOW),
        ],
    )


def _make_app(sessions=None, detail=None, delete_ok=True):
    app = create_app()

    mock_store = MagicMock()
    mock_store.list_sessions.return_value = sessions or []
    mock_store.get_session.return_value = detail
    mock_store.get_preview.return_value = detail.messages[:5] if detail else []
    mock_store.delete_session.return_value = delete_ok

    app.dependency_overrides[get_store] = lambda: mock_store
    return app


@pytest.fixture
async def client():
    app = _make_app(
        sessions=_sample_sessions(),
        detail=_sample_detail(),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def client_empty():
    app = _make_app(sessions=[], detail=None, delete_ok=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestListSessions:
    async def test_returns_sessions(self, client):
        resp = await client.get("/api/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["session_id"] == "s1"
        assert data[0]["title"] == "First session"
        assert data[0]["message_count"] == 5

    async def test_empty_list(self, client_empty):
        resp = await client_empty.get("/api/sessions")
        assert resp.status_code == 200
        assert resp.json() == []


class TestGetSession:
    async def test_found(self, client):
        resp = await client.get("/api/sessions/s1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "s1"
        assert len(data["messages"]) == 2
        assert data["messages"][0]["role"] == "user"

    async def test_not_found(self, client_empty):
        resp = await client_empty.get("/api/sessions/nope")
        assert resp.status_code == 404


class TestGetPreview:
    async def test_returns_previews(self, client):
        resp = await client.get("/api/sessions/s1/preview")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2

    async def test_not_found(self, client_empty):
        resp = await client_empty.get("/api/sessions/nope/preview")
        assert resp.status_code == 404


class TestDeleteSession:
    async def test_delete_success(self, client):
        resp = await client.delete("/api/sessions/s1")
        assert resp.status_code == 204

    async def test_delete_not_found(self, client_empty):
        resp = await client_empty.delete("/api/sessions/nope")
        assert resp.status_code == 404
