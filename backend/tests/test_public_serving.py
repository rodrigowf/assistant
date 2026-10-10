"""Public files and memory files change in place (live visualizations, spec 12 §9.3):
they revalidate on every load (``no-cache`` + ETag → 304), and a folder
visualization is served at ``/<dir>/``."""

from pathlib import Path

import pytest
from starlette.testclient import TestClient

import api.app as app_module


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    dist = tmp_path / "apps" / "web" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>app shell</html>")
    public = tmp_path / "context" / "public"
    (public / "dash" / "data").mkdir(parents=True)
    (public / "dash" / "index.html").write_text("<title>Dash</title>")
    (public / "dash" / "data" / "state.json").write_text("{}")
    (public / "empty").mkdir()
    (public / "my dir").mkdir()
    (public / "my dir" / "index.html").write_text("<title>Spaced</title>")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "index.html").write_text("SECRET")
    (public / "escape").mkdir()
    (public / "escape" / "index.html").symlink_to(outside / "index.html")
    (tmp_path / "secret.txt").write_text("top secret")
    memory = tmp_path / "context" / "memory"
    memory.mkdir()
    (memory / "MEMORY.md").write_text("# Memory")
    (memory / "note.md").write_text("# Note")
    monkeypatch.setattr(app_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(app_module, "_spa_dirs", lambda: [])
    monkeypatch.setattr("utils.paths.PROJECT_ROOT", tmp_path)
    return TestClient(app_module.create_app())


@pytest.mark.parametrize("path", ["/dash/index.html", "/dash/data/state.json", "/memory/note.md"])
def test_revalidates_then_304(client, path):
    first = client.get(path)
    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-cache"
    etag = first.headers["etag"]
    again = client.get(path, headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.headers["etag"] == etag
    assert again.content == b""


def test_changed_file_is_sent_again(client, tmp_path):
    etag = client.get("/dash/index.html").headers["etag"]
    target = tmp_path / "context" / "public" / "dash" / "index.html"
    target.write_text("<title>Dash v2</title>")
    import os
    st = target.stat()
    os.utime(target, (st.st_atime, st.st_mtime + 5))
    resp = client.get("/dash/index.html", headers={"If-None-Match": etag})
    assert resp.status_code == 200
    assert "v2" in resp.text


def test_folder_serves_its_index(client):
    resp = client.get("/dash/")
    assert resp.status_code == 200
    assert "<title>Dash</title>" in resp.text
    redirect = client.get("/dash", follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers["location"] == "/dash/"


def test_redirect_keeps_the_query_and_encodes_the_name(client):
    redirect = client.get("/my dir?x=1&y=two", follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers["location"] == "/my%20dir/?x=1&y=two"


def test_folder_index_symlinked_out_of_public_is_not_served(client):
    resp = client.get("/escape/")
    assert "SECRET" not in resp.text
    assert client.get("/escape", follow_redirects=False).status_code != 307


@pytest.mark.parametrize("path", ["/..%2F..%2Fsecret.txt", "/%2E%2E/%2E%2E/%2E%2E/secret.txt", "//etc/hostname"])
def test_dist_lookup_cannot_escape(client, path):
    resp = client.get(path)
    assert "top secret" not in resp.text
    assert "localhost" not in resp.text or "app shell" in resp.text


def test_unknown_html_is_404_not_the_app(client):
    resp = client.get("/visualizations/missing.html")
    assert resp.status_code == 404
    assert "app shell" not in resp.text
    # other unknown paths still get the app shell (client-side routes)
    assert "app shell" in client.get("/some/route").text


def test_memory_root_revalidates(client):
    first = client.get("/memory/")
    assert first.status_code == 200 and first.headers["cache-control"] == "no-cache"
    assert client.get("/memory/", headers={"If-None-Match": first.headers["etag"]}).status_code == 304


def test_folder_without_index_falls_back_to_the_app(client):
    resp = client.get("/empty/")
    assert "app shell" in resp.text
