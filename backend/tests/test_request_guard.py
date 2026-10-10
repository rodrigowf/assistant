"""API-wide browser-origin guard and CORS policy (api/guard.py, wired in api/app.py).

Probe routes are added to the real app (no lifespan) so the middleware stack is the production one.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import WebSocket
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import api.app as app_module

JETSON = "192.168.0.200"
EVIL = "https://evil.example.com"


def _front(app) -> None:
    """Move the route just added ahead of the SPA catch-all."""
    app.router.routes.insert(0, app.router.routes.pop())


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("ARCHIE_TRUSTED_ORIGINS", raising=False)
    monkeypatch.delenv("ARCHIE_TRUSTED_HOSTS", raising=False)
    build = tmp_path / "compat"
    (build / "assets").mkdir(parents=True)
    (build / "index.html").write_text("<html>compat</html>")
    monkeypatch.setattr(app_module, "_spa_dirs", lambda: [("compat", build)])
    app = app_module.create_app()

    async def probe():
        return {"ok": True}

    app.add_api_route("/api/test-probe", probe, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    _front(app)

    async def ws_probe(ws: WebSocket):
        await ws.accept()
        await ws.send_text("hello")
        await ws.close()

    app.add_api_websocket_route("/api/test-ws", ws_probe)
    _front(app)
    app.add_api_websocket_route("/other-ws", ws_probe)
    _front(app)
    return app


@pytest.fixture
def client(app):
    return TestClient(app, base_url=f"https://{JETSON}")


def _ws_ok(client: TestClient, path: str = "/api/test-ws", **headers: str) -> bool:
    headers.setdefault("host", JETSON)  # the WS test client ignores base_url's host
    try:
        with client.websocket_connect(path, headers=headers) as ws:
            return ws.receive_text() == "hello"
    except WebSocketDisconnect as e:
        assert e.code == 1008
        return False


class TestHttpWrites:
    @pytest.mark.parametrize("origin,host", [
        (f"https://{JETSON}", JETSON),                      # nginx: Host $host, no port
        (f"http://{JETSON}:8765", f"{JETSON}:8765"),         # direct to uvicorn
        (f"https://{JETSON}", f"{JETSON}:8765"),             # same host name, other port/scheme
        ("http://localhost:8765", "localhost:8765"),
        ("http://127.0.0.1:8765", "127.0.0.1:8765"),
        ("https://archie.tail1234.ts.net", "archie.tail1234.ts.net"),
        ("http://192.168.0.28:5450", "localhost:8765"),      # vite dev server proxying (changeOrigin)
        ("http://localhost:5451", "127.0.0.1:8765"),
        ("http://localhost:8799", "localhost:8765"),         # mock server
        ("chrome-extension://abcdefghijklmnop", "127.0.0.1:8765"),
    ])
    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_trusted_origins_pass(self, client, method, origin, host):
        r = client.request(method, "/api/test-probe", headers={"origin": origin, "host": host, "sec-fetch-site": "same-origin"})
        assert r.status_code == 200, r.text

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_no_origin_passes(self, client, method):
        # Android OkHttp, curl, context/scripts/*.py, the orchestrator's tools.
        assert client.request(method, "/api/test-probe").status_code == 200

    @pytest.mark.parametrize("origin", [
        EVIL,
        "http://192.168.0.99",            # another LAN machine's page
        "http://evil.example.com:5450",   # dev port on an untrusted host
        "null",                           # sandboxed iframe / file://
        "file://",
    ])
    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_untrusted_origins_rejected(self, client, method, origin):
        r = client.request(method, "/api/test-probe", headers={"origin": origin})
        assert r.status_code == 403
        assert "ARCHIE_TRUSTED_ORIGINS" in r.json()["detail"]

    @pytest.mark.parametrize("headers", [
        {"sec-fetch-site": "cross-site"},
        {"sec-fetch-site": "cross-site", "origin": f"https://{JETSON}"},  # trusted-looking Origin
        {"sec-fetch-site": "cross-site", "origin": "http://localhost:5450"},
    ])
    def test_fetch_metadata_cross_site_rejected(self, client, headers):
        assert client.post("/api/test-probe", headers=headers).status_code == 403

    def test_real_routes_are_covered(self, client):
        r = client.post("/api/sessions/inject", json={}, headers={"origin": EVIL})
        assert r.status_code == 403
        r = client.post("/api/debug/log", json={"level": "log", "args": ["x"]}, headers={"origin": EVIL})
        assert r.status_code == 403
        r = client.post("/api/uploads", headers={"origin": EVIL})
        assert r.status_code == 403

    def test_same_origin_safari12_without_fetch_metadata(self, client):
        # Safari 12 (iPad, /compat/) sends Origin on POST but no Sec-Fetch-*.
        r = client.post("/api/test-probe", headers={"origin": f"https://{JETSON}"})
        assert r.status_code == 200

    def test_env_override(self, client, monkeypatch):
        origin = "http://192.168.0.28:5173"
        headers = {"origin": origin, "sec-fetch-site": "cross-site"}  # another machine: always cross-site
        assert client.post("/api/test-probe", headers=headers).status_code == 403
        monkeypatch.setenv("ARCHIE_TRUSTED_ORIGINS", f"https://other.example, {origin}/")
        assert client.post("/api/test-probe", headers=headers).status_code == 200
        monkeypatch.setenv("ARCHIE_TRUSTED_ORIGINS", "null")
        assert client.post("/api/test-probe", headers={"origin": "null"}).status_code == 403

    def test_extension_exempt_from_cross_site(self, client):
        headers = {"origin": "chrome-extension://abcdefghijklmnop", "sec-fetch-site": "cross-site"}
        assert client.post("/api/test-probe", headers=headers).status_code == 200


class TestHost:
    @pytest.mark.parametrize("host", ["evil.example.com", "192.168.0.200.nip.io", "rebind.attacker.net:8765"])
    @pytest.mark.parametrize("method", ["GET", "POST"])
    def test_rebinding_host_rejected_on_api(self, client, method, host):
        # A rebound page is same-origin with itself, so Origin matches Host; the Host gives it away.
        r = client.request(method, "/api/test-probe", headers={"host": host, "origin": f"http://{host}"})
        assert r.status_code == 403
        assert "ARCHIE_TRUSTED_HOSTS" in r.json()["detail"]

    @pytest.mark.parametrize("host", [JETSON, "127.0.0.1:8765", "[::1]:8765", "localhost:5450", "server.local", "jetson", "archie.tail1234.ts.net"])
    def test_private_hosts_pass(self, client, host):
        assert client.get("/api/test-probe", headers={"host": host}).status_code == 200

    def test_env_trusted_host(self, client, monkeypatch):
        monkeypatch.setenv("ARCHIE_TRUSTED_HOSTS", "archie.example.org")
        r = client.post("/api/test-probe", headers={"host": "archie.example.org", "origin": "https://archie.example.org"})
        assert r.status_code == 200


class TestReadsAndStatic:
    def test_cross_site_get_passes_without_acao(self, client):
        r = client.get("/api/test-probe", headers={"origin": EVIL, "sec-fetch-site": "cross-site"})
        assert r.status_code == 200
        assert "access-control-allow-origin" not in r.headers

    @pytest.mark.parametrize("origin", [f"https://{JETSON}", "http://localhost:5450", "chrome-extension://abc"])
    def test_trusted_get_gets_acao(self, client, origin):
        r = client.get("/api/test-probe", headers={"origin": origin})
        assert r.headers["access-control-allow-origin"] == origin
        assert "origin" in r.headers["vary"].lower()

    def test_preflight(self, client):
        pre = {"access-control-request-method": "POST", "access-control-request-headers": "content-type"}
        r = client.options("/api/test-probe", headers={**pre, "origin": EVIL})
        assert r.status_code == 400 and "access-control-allow-origin" not in r.headers
        r = client.options("/api/test-probe", headers={**pre, "origin": "http://localhost:5450"})
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == "http://localhost:5450"

    def test_static_content_never_blocked(self, client):
        # The Fire TV, phones and other browsers opening pages; even with a foreign Host.
        for headers in ({"origin": EVIL, "sec-fetch-site": "cross-site"}, {"host": "evil.example.com"}):
            r = client.get("/compat/", headers=headers)
            assert r.status_code == 200 and "compat" in r.text


class TestWebSocket:
    def test_same_origin_and_no_origin_pass(self, client):
        assert _ws_ok(client)  # no Origin: Android OkHttp, scripts
        assert _ws_ok(client, origin=f"https://{JETSON}")
        assert _ws_ok(client, origin=f"https://{JETSON}", host=f"{JETSON}:8765")
        assert _ws_ok(client, origin="http://localhost:5450", host="localhost:8765")

    def test_extension_origin_passes(self, client):
        assert _ws_ok(client, origin="chrome-extension://abcdefghijklmnop", host="127.0.0.1:8766")

    @pytest.mark.parametrize("headers", [
        {"origin": EVIL},
        {"origin": "null"},
        {"sec-fetch-site": "cross-site", "origin": f"https://{JETSON}"},
        {"host": "evil.example.com", "origin": "http://evil.example.com"},
    ])
    def test_cross_site_rejected(self, client, headers):
        assert not _ws_ok(client, **headers)

    def test_every_ws_path_is_guarded(self, client):
        assert not _ws_ok(client, "/other-ws", origin=EVIL)
        assert _ws_ok(client, "/other-ws", origin=f"https://{JETSON}")
        for path in ("/api/orchestrator/chat", "/api/sessions/chat", "/api/browser/ws"):
            with pytest.raises(WebSocketDisconnect) as exc:
                with client.websocket_connect(path, headers={"origin": EVIL, "host": JETSON}):
                    pass
            assert exc.value.code == 1008


def test_rejection_logged_once(client, caplog):
    with caplog.at_level(logging.WARNING, logger="api.guard"):
        client.post("/api/test-probe?token=secret", headers={"origin": EVIL})
    records = [r for r in caplog.records if r.name == "api.guard"]
    assert len(records) == 1
    msg = records[0].getMessage()
    assert "POST /api/test-probe" in msg and EVIL in msg and "secret" not in msg


def test_browser_daemon_is_guarded():
    """shared/scripts/browser_daemon.py (the extension's loopback daemon) runs the same guard."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "shared" / "scripts" / "browser_daemon.py"
    spec = importlib.util.spec_from_file_location("browser_daemon_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    client = TestClient(module.build_app(), base_url="http://127.0.0.1:8766")
    assert client.post("/api/uploads", headers={"origin": EVIL}).status_code == 403
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/api/browser/ws", headers={"origin": EVIL, "host": "127.0.0.1:8766"}):
            pass
    assert exc.value.code == 1008
    # The extension's own origin gets past the guard (the upload then fails on its empty body).
    r = client.post("/api/uploads", headers={"origin": "chrome-extension://abcdefghijklmnop"})
    assert r.status_code != 403


class TestPrivateStatic:
    """/memory, /uploads, /projects get the Host check (DNS rebinding); the apps and public pages don't."""

    @pytest.fixture
    def static_client(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ARCHIE_TRUSTED_HOSTS", raising=False)
        dist = tmp_path / "apps" / "web" / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<html>root-build</html>")
        (tmp_path / "context" / "public").mkdir(parents=True)
        (tmp_path / "context" / "public" / "viz.html").write_text("<html>viz</html>")
        (tmp_path / "context" / "memory").mkdir(parents=True)
        (tmp_path / "context" / "memory" / "MEMORY.md").write_text("# memory")
        (tmp_path / "context" / "memory" / "note.md").write_text("# note")
        (tmp_path / "context" / "uploads").mkdir(parents=True)
        (tmp_path / "context" / "uploads" / "f.txt").write_text("upload")
        (tmp_path / "projects").mkdir()
        (tmp_path / "projects" / "p.txt").write_text("project")
        monkeypatch.setattr(app_module, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(app_module, "_spa_dirs", lambda: [])
        return TestClient(app_module.create_app(), base_url=f"http://{JETSON}")

    PRIVATE = ["/memory", "/memory/", "/memory/note.md", "/uploads/f.txt", "/projects/p.txt"]

    @pytest.mark.parametrize("path", PRIVATE)
    def test_rebinding_host_rejected(self, static_client, path):
        r = static_client.get(path, headers={"host": "rebind.attacker.net"})
        assert r.status_code == 403
        assert "ARCHIE_TRUSTED_HOSTS" in r.json()["detail"]

    @pytest.mark.parametrize("host", [JETSON, f"{JETSON}:8765", "localhost:8765", "archie.tail1234.ts.net", "jetson"])
    @pytest.mark.parametrize("path", PRIVATE)
    def test_trusted_hosts_served(self, static_client, path, host):
        assert static_client.get(path, headers={"host": host}).status_code == 200

    def test_cross_site_reads_not_refused(self, static_client):
        # Only the Host matters for these reads (a link from another page still opens a note).
        r = static_client.get("/memory/note.md", headers={"origin": EVIL, "sec-fetch-site": "cross-site"})
        assert r.status_code == 200

    @pytest.mark.parametrize("path", ["/", "/viz.html", "/assets/missing.js", "/memoryless", "/uploadsfoo"])
    def test_apps_and_public_pages_any_host(self, static_client, path):
        r = static_client.get(path, headers={"host": "some-device-name.example"})
        assert r.status_code != 403

    def test_env_trusted_host(self, static_client, monkeypatch):
        monkeypatch.setenv("ARCHIE_TRUSTED_HOSTS", "archie.example.org")
        assert static_client.get("/memory/note.md", headers={"host": "archie.example.org"}).status_code == 200
