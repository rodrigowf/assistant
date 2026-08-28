"""Tests for api/routes/browser.py — browser-extension WebSocket transport."""

from __future__ import annotations

import asyncio

import orjson
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from api.routes import browser
from api.routes.browser import (
    BrowserCommandError,
    BrowserCommandTimeout,
    BrowserHub,
    BrowserNotConnected,
)

TOKEN = "test-token-abc123"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeWebSocket:
    """Minimal WebSocket stand-in that records what the hub sends."""

    def __init__(self, fail_send: bool = False) -> None:
        self.sent: list[dict] = []
        self.closed_with: int | None = None
        self._fail_send = fail_send

    async def send_text(self, text: str) -> None:
        if self._fail_send:
            raise RuntimeError("socket is gone")
        self.sent.append(orjson.loads(text))

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code


def _make_app() -> FastAPI:
    """Mount only the browser router.

    Deliberately avoids ``create_app()``: its lifespan starts the session pool,
    file watchers and the search-server pre-warm, none of which this transport
    needs.
    """
    app = FastAPI()
    app.state.browser_hub = BrowserHub()
    app.include_router(browser.router)
    return app


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv(browser.TOKEN_ENV_VAR, TOKEN)
    return TOKEN


@pytest.fixture
def no_token(monkeypatch, tmp_path):
    """Unset the env var *and* point PROJECT_ROOT at an empty tree.

    Without the second half, a real context/.env on the dev machine would leak
    into the test and the 'not configured' path would never be exercised.
    """
    monkeypatch.delenv(browser.TOKEN_ENV_VAR, raising=False)
    monkeypatch.setattr(browser, "PROJECT_ROOT", tmp_path)


def _hello(token: str = TOKEN) -> str:
    return orjson.dumps({
        "type": "hello", "token": token,
        "client": "chrome-extension", "version": "0.1.0",
    }).decode()


# ---------------------------------------------------------------------------
# Token resolution
# ---------------------------------------------------------------------------

def test_token_from_environment(token):
    assert browser._resolve_token() == TOKEN


def test_token_falls_back_to_context_env(monkeypatch, tmp_path):
    monkeypatch.delenv(browser.TOKEN_ENV_VAR, raising=False)
    context = tmp_path / "context"
    context.mkdir()
    (context / ".env").write_text(
        f'# a comment\nOTHER=x\n{browser.TOKEN_ENV_VAR}="quoted-token"\n'
    )
    monkeypatch.setattr(browser, "PROJECT_ROOT", tmp_path)

    assert browser._resolve_token() == "quoted-token"


def test_token_absent_returns_none(no_token):
    assert browser._resolve_token() is None


def test_empty_token_treated_as_unconfigured(monkeypatch, tmp_path):
    """An empty value must not authenticate an empty presented token."""
    monkeypatch.setenv(browser.TOKEN_ENV_VAR, "   ")
    monkeypatch.setattr(browser, "PROJECT_ROOT", tmp_path)

    assert browser._resolve_token() is None


# ---------------------------------------------------------------------------
# Handshake / auth
# ---------------------------------------------------------------------------

def test_valid_token_is_accepted(token):
    with TestClient(_make_app()) as client:
        with client.websocket_connect("/api/browser/ws") as ws:
            ws.send_text(_hello())
            assert ws.receive_json() == {"type": "ready"}


def test_wrong_token_is_rejected(token):
    with TestClient(_make_app()) as client:
        with client.websocket_connect("/api/browser/ws") as ws:
            ws.send_text(_hello("wrong-token"))
            assert ws.receive_json() == {"type": "error", "error": "unauthorized"}


def test_missing_token_field_is_rejected(token):
    with TestClient(_make_app()) as client:
        with client.websocket_connect("/api/browser/ws") as ws:
            ws.send_text(orjson.dumps({"type": "hello"}).decode())
            assert ws.receive_json() == {"type": "error", "error": "unauthorized"}


def test_non_hello_first_frame_is_rejected(token):
    with TestClient(_make_app()) as client:
        with client.websocket_connect("/api/browser/ws") as ws:
            ws.send_text(orjson.dumps({"type": "result", "id": "x"}).decode())
            assert ws.receive_json() == {"type": "error", "error": "expected_hello"}


def test_malformed_hello_is_rejected(token):
    with TestClient(_make_app()) as client:
        with client.websocket_connect("/api/browser/ws") as ws:
            ws.send_text("not json at all")
            assert ws.receive_json() == {"type": "error", "error": "invalid_json"}


def test_connection_refused_when_no_token_configured(no_token):
    """Auth fails closed — an unconfigured backend accepts nobody."""
    with TestClient(_make_app()) as client:
        with client.websocket_connect("/api/browser/ws") as ws:
            assert ws.receive_json() == {
                "type": "error", "error": "auth_not_configured",
            }


def test_ping_gets_pong(token):
    with TestClient(_make_app()) as client:
        with client.websocket_connect("/api/browser/ws") as ws:
            ws.send_text(_hello())
            ws.receive_json()  # ready
            ws.send_text(orjson.dumps({"type": "ping"}).decode())
            assert ws.receive_json() == {"type": "pong"}


def test_junk_frame_does_not_drop_connection(token):
    with TestClient(_make_app()) as client:
        with client.websocket_connect("/api/browser/ws") as ws:
            ws.send_text(_hello())
            ws.receive_json()  # ready
            ws.send_text("<<< not json >>>")
            ws.send_text(orjson.dumps({"type": "ping"}).decode())
            assert ws.receive_json() == {"type": "pong"}


# ---------------------------------------------------------------------------
# Status endpoint
# ---------------------------------------------------------------------------

def test_status_reports_disconnected(token):
    with TestClient(_make_app()) as client:
        body = client.get("/api/browser/status").json()

    assert body["connected"] is False
    assert body["token_configured"] is True
    assert body["pending_commands"] == 0


def test_status_reports_connected_client(token):
    with TestClient(_make_app()) as client:
        with client.websocket_connect("/api/browser/ws") as ws:
            ws.send_text(_hello())
            ws.receive_json()  # ready
            body = client.get("/api/browser/status").json()

    assert body["connected"] is True
    assert body["client"]["client"] == "chrome-extension"
    assert body["client"]["version"] == "0.1.0"


def test_status_reports_token_not_configured(no_token):
    with TestClient(_make_app()) as client:
        assert client.get("/api/browser/status").json()["token_configured"] is False


# ---------------------------------------------------------------------------
# Hub: command dispatch and correlation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_command_without_connection_raises():
    hub = BrowserHub()
    with pytest.raises(BrowserNotConnected):
        await hub.send_command("ping")


@pytest.mark.asyncio
async def test_send_command_resolves_matching_result():
    hub = BrowserHub()
    ws = FakeWebSocket()
    await hub.attach(ws, {"client": "test"})

    task = asyncio.create_task(hub.send_command("navigate", {"url": "https://x"}))
    await asyncio.sleep(0)  # let the frame go out

    sent = ws.sent[0]
    assert sent["type"] == "command"
    assert sent["command"] == "navigate"
    assert sent["params"] == {"url": "https://x"}

    hub.handle_result({"id": sent["id"], "type": "result", "ok": True,
                       "result": {"tabId": 5}})
    assert await task == {"tabId": 5}
    assert hub.status()["pending_commands"] == 0


@pytest.mark.asyncio
async def test_results_correlate_by_id_not_arrival_order():
    """Two in-flight commands answered out of order must not cross wires."""
    hub = BrowserHub()
    ws = FakeWebSocket()
    await hub.attach(ws, {"client": "test"})

    first = asyncio.create_task(hub.send_command("a"))
    second = asyncio.create_task(hub.send_command("b"))
    await asyncio.sleep(0)

    id_a, id_b = ws.sent[0]["id"], ws.sent[1]["id"]
    assert id_a != id_b

    # Answer the second one first.
    hub.handle_result({"id": id_b, "type": "result", "ok": True, "result": "B"})
    hub.handle_result({"id": id_a, "type": "result", "ok": True, "result": "A"})

    assert await first == "A"
    assert await second == "B"


@pytest.mark.asyncio
async def test_failed_result_raises_command_error():
    hub = BrowserHub()
    ws = FakeWebSocket()
    await hub.attach(ws, {"client": "test"})

    task = asyncio.create_task(hub.send_command("click"))
    await asyncio.sleep(0)

    hub.handle_result({"id": ws.sent[0]["id"], "type": "result", "ok": False,
                       "error": "unknown command: click"})

    with pytest.raises(BrowserCommandError, match="unknown command"):
        await task


@pytest.mark.asyncio
async def test_send_command_times_out():
    hub = BrowserHub()
    await hub.attach(FakeWebSocket(), {"client": "test"})

    with pytest.raises(BrowserCommandTimeout, match="timed out"):
        await hub.send_command("ping", timeout=0.05)

    assert hub.status()["pending_commands"] == 0


@pytest.mark.asyncio
async def test_late_result_after_timeout_is_ignored():
    """A straggler must not blow up on an already-discarded future."""
    hub = BrowserHub()
    ws = FakeWebSocket()
    await hub.attach(ws, {"client": "test"})

    with pytest.raises(BrowserCommandTimeout):
        await hub.send_command("ping", timeout=0.05)

    hub.handle_result({"id": ws.sent[0]["id"], "type": "result", "ok": True,
                       "result": "late"})  # must not raise


@pytest.mark.asyncio
async def test_unknown_result_id_is_ignored():
    hub = BrowserHub()
    await hub.attach(FakeWebSocket(), {"client": "test"})
    hub.handle_result({"id": "never-issued", "type": "result", "ok": True})


@pytest.mark.asyncio
async def test_send_failure_surfaces_as_not_connected():
    hub = BrowserHub()
    await hub.attach(FakeWebSocket(fail_send=True), {"client": "test"})

    with pytest.raises(BrowserNotConnected, match="failed to send"):
        await hub.send_command("ping")

    assert hub.status()["pending_commands"] == 0


# ---------------------------------------------------------------------------
# Hub: lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disconnect_fails_pending_commands_immediately():
    """A dropped connection must fail in-flight commands rather than leaving
    the caller to wait out the full timeout."""
    hub = BrowserHub()
    ws = FakeWebSocket()
    await hub.attach(ws, {"client": "test"})

    task = asyncio.create_task(hub.send_command("navigate", timeout=30))
    await asyncio.sleep(0)

    await hub.detach(ws)

    with pytest.raises(BrowserNotConnected, match="disconnected"):
        await task


@pytest.mark.asyncio
async def test_reconnect_replaces_previous_connection():
    hub = BrowserHub()
    old, new = FakeWebSocket(), FakeWebSocket()

    await hub.attach(old, {"client": "old"})
    task = asyncio.create_task(hub.send_command("navigate", timeout=30))
    await asyncio.sleep(0)

    await hub.attach(new, {"client": "new"})

    with pytest.raises(BrowserNotConnected, match="reconnected"):
        await task
    assert old.closed_with == browser.WS_CLOSE_REPLACED
    assert hub.status()["client"] == {"client": "new"}


@pytest.mark.asyncio
async def test_detach_of_stale_socket_is_a_noop():
    """The displaced socket's receive loop also calls detach; it must not tear
    down the connection that replaced it."""
    hub = BrowserHub()
    old, new = FakeWebSocket(), FakeWebSocket()

    await hub.attach(old, {"client": "old"})
    await hub.attach(new, {"client": "new"})
    await hub.detach(old)

    assert hub.connected is True
    assert hub.status()["client"] == {"client": "new"}
