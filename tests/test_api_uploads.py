"""Tests for the file-upload surface (``api/routes/uploads.py``).

Covers:
- ``POST /api/uploads`` writes the file under ``context/uploads/`` and returns
  the expected metadata (path, url, size, content_type, sanitized filename).
- Filename sanitization strips traversal / unsafe characters.
- The oversize guard rejects with 413 and leaves no partial file behind.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
import api.routes.uploads as uploads_module


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient with the context dir pointed at a temp dir so uploads never
    touch the real ``context/uploads/``."""
    import utils.paths
    monkeypatch.setattr(utils.paths, "PROJECT_ROOT", tmp_path)
    return TestClient(create_app())


def _uploads_dir(tmp_path: Path) -> Path:
    return tmp_path / "context" / "uploads"


def test_upload_writes_file_and_returns_metadata(client: TestClient, tmp_path: Path) -> None:
    content = b"hello upload world"
    r = client.post(
        "/api/uploads",
        files={"file": ("photo.jpg", io.BytesIO(content), "image/jpeg")},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["filename"] == "photo.jpg"
    assert body["size"] == len(content)
    assert body["content_type"] == "image/jpeg"
    assert body["url"].startswith("/uploads/")
    assert body["url"].endswith("-photo.jpg")

    # The stored path exists and holds the exact bytes.
    stored = Path(body["path"])
    assert stored.is_file()
    assert stored.read_bytes() == content
    # And it lives under the temp context/uploads dir.
    assert stored.parent == _uploads_dir(tmp_path)
    # url basename == stored basename.
    assert body["url"] == f"/uploads/{stored.name}"


def test_upload_sanitizes_traversal_filename(client: TestClient, tmp_path: Path) -> None:
    r = client.post(
        "/api/uploads",
        files={"file": ("../../etc/passwd", io.BytesIO(b"x"), "text/plain")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # No path separators survive; the basename is reduced to a safe token.
    assert "/" not in body["filename"]
    assert ".." not in body["url"].replace("/uploads/", "")
    stored = Path(body["path"])
    assert stored.parent == _uploads_dir(tmp_path)
    assert stored.is_file()


def test_upload_unique_names_no_clobber(client: TestClient, tmp_path: Path) -> None:
    r1 = client.post("/api/uploads", files={"file": ("a.txt", io.BytesIO(b"one"), "text/plain")})
    r2 = client.post("/api/uploads", files={"file": ("a.txt", io.BytesIO(b"two"), "text/plain")})
    assert r1.status_code == 200 and r2.status_code == 200
    p1, p2 = Path(r1.json()["path"]), Path(r2.json()["path"])
    assert p1 != p2
    assert p1.read_bytes() == b"one"
    assert p2.read_bytes() == b"two"


def test_upload_oversize_rejected(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Shrink the cap so the test payload trips it without allocating 200 MB.
    monkeypatch.setattr(uploads_module, "_MAX_UPLOAD_BYTES", 8)
    monkeypatch.setattr(uploads_module, "_CHUNK", 4)
    r = client.post(
        "/api/uploads",
        files={"file": ("big.bin", io.BytesIO(b"0123456789ABCDEF"), "application/octet-stream")},
    )
    assert r.status_code == 413, r.text
    # No partial file left behind.
    uploads = _uploads_dir(tmp_path)
    leftovers = list(uploads.iterdir()) if uploads.exists() else []
    assert leftovers == [], f"partial upload not cleaned up: {leftovers}"


def test_sanitize_filename_edge_cases() -> None:
    f = uploads_module._sanitize_filename
    assert f(None) == "upload"
    assert f("") == "upload"
    assert f("/") == "upload"
    assert f("../../x") == "x"
    # spaces/parens collapse to underscores; result stays a safe token
    out = f("my file (1).png")
    assert out.endswith(".png") and " " not in out and "(" not in out
    assert "/" not in f("a/b/c.txt")
    assert f("a/b/c.txt") == "c.txt"
