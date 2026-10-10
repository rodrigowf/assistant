"""The root SPA catch-all (apps/web/dist at /) must never serve files outside the dist
or context/public — absolute paths ("//etc/x") and dot segments used to escape it."""

from pathlib import Path

import pytest
from starlette.testclient import TestClient

import api.app as app_module


@pytest.fixture
def root_client(tmp_path: Path, monkeypatch):
    dist = tmp_path / "apps" / "web" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>root-build</html>")
    (dist / "icon.svg").write_text("<svg/>")
    (tmp_path / "context" / "public").mkdir(parents=True)
    (tmp_path / "context" / ".env").write_text("SECRET_KEY=top-secret")
    monkeypatch.setattr(app_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(app_module, "_spa_dirs", lambda: [])
    return TestClient(app_module.create_app()), tmp_path


def test_dist_files_still_served(root_client):
    client, _ = root_client
    resp = client.get("/icon.svg")
    assert resp.status_code == 200
    assert resp.text == "<svg/>"


@pytest.mark.parametrize(
    "path",
    [
        "/..%2F..%2F..%2Fcontext%2F.env",
        "/assets%2F..%2F..%2F..%2F..%2Fcontext%2F.env",
    ],
)
def test_dot_segments_rejected(root_client, path):
    client, _ = root_client
    resp = client.get(path)
    # The app shell (catch-all) or a 404 (/assets mount) — never the file.
    assert "top-secret" not in resp.text


def test_absolute_path_rejected(root_client):
    client, root = root_client
    resp = client.get("/%2F" + str(root / "context" / ".env").lstrip("/").replace("/", "%2F"))
    assert "top-secret" not in resp.text
    assert "root-build" in resp.text
