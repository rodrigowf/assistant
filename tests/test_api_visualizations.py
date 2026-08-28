"""Tests for api/routes/visualizations.py — HTML artifact discovery.

Covers:
- ``GET /api/visualizations`` walks context/public/ recursively and reports
  relative path + serving URL for each HTML file.
- The three-step title resolution (manual override > <title> tag > filename).
- Sort order is newest-modified first.
- ``PATCH /api/visualizations/rename`` round-trips through ``.titles.json``
  under the ``viz:`` namespace, with traversal + validation guards.
- The shared ``.titles.json`` stays safe: ``viz:`` keys don't disturb session
  titles, which is the assumption that lets both features share one file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import get_store
from manager.store import SessionStore


@pytest.fixture
def public_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point utils.paths at a temp tree so the real context/public/ is untouched."""
    import utils.paths

    monkeypatch.setattr(utils.paths, "PROJECT_ROOT", tmp_path)
    d = tmp_path / "context" / "public"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def store(tmp_path: Path, public_dir: Path) -> SessionStore:
    return SessionStore(tmp_path)


@pytest.fixture
def client(store: SessionStore) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_store] = lambda: store
    return TestClient(app)


def _write(path: Path, body: str, *, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _titles(tmp_path: Path) -> dict[str, str]:
    return json.loads((tmp_path / "context" / ".titles.json").read_text())


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_lists_html_recursively_and_ignores_other_files(
    client: TestClient, public_dir: Path
) -> None:
    _write(public_dir / "top.html", "<title>Top</title>")
    _write(public_dir / "visualizations" / "nested.html", "<title>Nested</title>")
    _write(public_dir / "deep" / "deeper" / "buried.html", "<title>Buried</title>")
    # Non-HTML siblings must not appear.
    _write(public_dir / "notes.md", "# not html")
    _write(public_dir / "script.js", "console.log(1)")

    r = client.get("/api/visualizations")
    assert r.status_code == 200, r.text
    body = r.json()

    by_path = {e["path"]: e for e in body}
    assert set(by_path) == {
        "top.html",
        "visualizations/nested.html",
        "deep/deeper/buried.html",
    }
    # The URL is what the static catch-all in api/app.py already serves.
    assert by_path["visualizations/nested.html"]["url"] == "/visualizations/nested.html"
    assert by_path["deep/deeper/buried.html"]["url"] == "/deep/deeper/buried.html"


def test_empty_when_public_dir_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import utils.paths

    monkeypatch.setattr(utils.paths, "PROJECT_ROOT", tmp_path)
    app = create_app()
    app.dependency_overrides[get_store] = lambda: SessionStore(tmp_path)
    assert TestClient(app).get("/api/visualizations").json() == []


def test_reports_size_and_timestamps(client: TestClient, public_dir: Path) -> None:
    content = "<title>Sized</title>"
    _write(public_dir / "sized.html", content, mtime=1_700_000_000)

    entry = client.get("/api/visualizations").json()[0]
    assert entry["size"] == len(content)
    assert entry["modified"].startswith("2023-11-14")
    # created falls back to mtime when birth time is unavailable; either way
    # it must be a parseable ISO timestamp.
    assert entry["created"]


def test_sorted_by_modified_newest_first(client: TestClient, public_dir: Path) -> None:
    _write(public_dir / "old.html", "<title>Old</title>", mtime=1_600_000_000)
    _write(public_dir / "new.html", "<title>New</title>", mtime=1_800_000_000)
    _write(public_dir / "mid.html", "<title>Mid</title>", mtime=1_700_000_000)

    paths = [e["path"] for e in client.get("/api/visualizations").json()]
    assert paths == ["new.html", "mid.html", "old.html"]


# ---------------------------------------------------------------------------
# Title resolution
# ---------------------------------------------------------------------------


def test_title_from_html_tag(client: TestClient, public_dir: Path) -> None:
    _write(public_dir / "a.html", "<html><head><title>QVCM Interactive</title></head></html>")
    assert client.get("/api/visualizations").json()[0]["title"] == "QVCM Interactive"


def test_title_tag_whitespace_is_collapsed(client: TestClient, public_dir: Path) -> None:
    _write(public_dir / "a.html", "<title>\n   Multi\n   Line   Title\n</title>")
    assert client.get("/api/visualizations").json()[0]["title"] == "Multi Line Title"


def test_title_tag_handles_unicode(client: TestClient, public_dir: Path) -> None:
    _write(public_dir / "a.html", "<title>Tarô — Canvas</title>")
    assert client.get("/api/visualizations").json()[0]["title"] == "Tarô — Canvas"


def test_title_falls_back_to_prettified_filename(
    client: TestClient, public_dir: Path
) -> None:
    _write(public_dir / "berlin-destino.html", "<h1>no title tag</h1>")
    assert client.get("/api/visualizations").json()[0]["title"] == "Berlin Destino"


def test_index_html_falls_back_to_parent_directory_name(
    client: TestClient, public_dir: Path
) -> None:
    _write(public_dir / "tarot-canvas" / "index.html", "<h1>untitled</h1>")
    assert client.get("/api/visualizations").json()[0]["title"] == "Tarot Canvas"


def test_empty_title_tag_falls_through_to_filename(
    client: TestClient, public_dir: Path
) -> None:
    _write(public_dir / "my-viz.html", "<title>   </title>")
    assert client.get("/api/visualizations").json()[0]["title"] == "My Viz"


def test_manual_title_overrides_html_tag(
    client: TestClient, public_dir: Path, store: SessionStore
) -> None:
    _write(public_dir / "a.html", "<title>Original Tag Title</title>")
    store.set_title("viz:a.html", "Rodrigo's Custom Name")
    assert client.get("/api/visualizations").json()[0]["title"] == "Rodrigo's Custom Name"


# ---------------------------------------------------------------------------
# Rename
# ---------------------------------------------------------------------------


def test_rename_round_trips(
    client: TestClient, public_dir: Path, tmp_path: Path
) -> None:
    _write(public_dir / "visualizations" / "a.html", "<title>Before</title>")

    r = client.patch(
        "/api/visualizations/rename",
        json={"path": "visualizations/a.html", "title": "After"},
    )
    assert r.status_code == 204, r.text

    # Persisted under the namespaced key...
    assert _titles(tmp_path)["viz:visualizations/a.html"] == "After"
    # ...and reflected in the next listing.
    assert client.get("/api/visualizations").json()[0]["title"] == "After"


def test_rename_strips_whitespace(client: TestClient, public_dir: Path, tmp_path: Path) -> None:
    _write(public_dir / "a.html", "<title>Before</title>")
    r = client.patch(
        "/api/visualizations/rename", json={"path": "a.html", "title": "  Padded  "}
    )
    assert r.status_code == 204
    assert _titles(tmp_path)["viz:a.html"] == "Padded"


@pytest.mark.parametrize(
    "body",
    [
        {"title": "No path"},
        {"path": "", "title": "Empty path"},
        {"path": "a.html"},
        {"path": "a.html", "title": "   "},
    ],
)
def test_rename_rejects_missing_fields(
    client: TestClient, public_dir: Path, body: dict
) -> None:
    _write(public_dir / "a.html", "<title>x</title>")
    assert client.patch("/api/visualizations/rename", json=body).status_code == 400


def test_rename_rejects_unknown_file(client: TestClient, public_dir: Path) -> None:
    r = client.patch(
        "/api/visualizations/rename", json={"path": "nope.html", "title": "X"}
    )
    assert r.status_code == 404


def test_rename_rejects_path_traversal(
    client: TestClient, public_dir: Path, tmp_path: Path
) -> None:
    # A real file outside public/ — the guard must refuse it even though it exists.
    outside = tmp_path / "context" / "secret.html"
    _write(outside, "<title>Secret</title>")

    r = client.patch(
        "/api/visualizations/rename",
        json={"path": "../secret.html", "title": "Pwned"},
    )
    assert r.status_code == 404
    assert not (tmp_path / "context" / ".titles.json").exists()


# ---------------------------------------------------------------------------
# Shared .titles.json safety
# ---------------------------------------------------------------------------


def test_viz_titles_do_not_disturb_session_titles(
    client: TestClient, public_dir: Path, store: SessionStore, tmp_path: Path
) -> None:
    """The core assumption behind sharing one file: keys are independent."""
    session_uuid = "11111111-2222-3333-4444-555555555555"
    store.set_title(session_uuid, "My Conversation")
    _write(public_dir / "a.html", "<title>Tag</title>")

    client.patch("/api/visualizations/rename", json={"path": "a.html", "title": "Viz"})

    titles = _titles(tmp_path)
    assert titles[session_uuid] == "My Conversation"
    assert titles["viz:a.html"] == "Viz"


def test_rename_session_still_guards_on_missing_jsonl(store: SessionStore) -> None:
    """set_title is unguarded by design; rename_session must stay guarded."""
    assert store.rename_session("no-such-session", "Nope") is False


def test_set_title_is_unguarded(store: SessionStore, tmp_path: Path) -> None:
    store.set_title("viz:anything.html", "Works")
    assert store.get_titles()["viz:anything.html"] == "Works"
