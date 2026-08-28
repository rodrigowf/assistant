"""Tests for api/routes/memory.py — the context/memory/ markdown tree.

Covers:
- ``GET /api/memory/tree`` nests directories, reports paths relative to
  context/memory/, and includes only markdown.
- Directories sort before files; both case-insensitively alphabetical.
- Folders containing no markdown at any depth are pruned.
- Dotfiles/dotdirs and symlinks escaping the tree are skipped.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app


@pytest.fixture
def memory_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point utils.paths at a temp tree so the real memory wiki is untouched."""
    import utils.paths

    monkeypatch.setattr(utils.paths, "PROJECT_ROOT", tmp_path)
    d = tmp_path / "context" / "memory"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def client(memory_dir: Path) -> TestClient:
    return TestClient(create_app())


def _write(path: Path, body: str = "# doc") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _names(nodes: list[dict]) -> list[str]:
    return [n["name"] for n in nodes]


def _find(nodes: list[dict], name: str) -> dict:
    return next(n for n in nodes if n["name"] == name)


def test_empty_when_memory_dir_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import utils.paths

    monkeypatch.setattr(utils.paths, "PROJECT_ROOT", tmp_path)
    assert TestClient(create_app()).get("/api/memory/tree").json() == []


def test_lists_root_files(client: TestClient, memory_dir: Path) -> None:
    _write(memory_dir / "MEMORY.md")
    _write(memory_dir / "ARCHIVE.md")

    tree = client.get("/api/memory/tree").json()
    assert _names(tree) == ["ARCHIVE.md", "MEMORY.md"]
    assert all(n["is_dir"] is False for n in tree)
    assert all(n["children"] is None for n in tree)
    assert _find(tree, "MEMORY.md")["path"] == "MEMORY.md"


def test_nests_directories_with_relative_paths(
    client: TestClient, memory_dir: Path
) -> None:
    _write(memory_dir / "assistant" / "architecture" / "overview.md")
    _write(memory_dir / "assistant" / "INDEX.md")

    tree = client.get("/api/memory/tree").json()
    assistant = _find(tree, "assistant")
    assert assistant["is_dir"] is True
    assert assistant["path"] == "assistant"

    # Directories sort before files within a level.
    assert _names(assistant["children"]) == ["architecture", "INDEX.md"]
    assert _find(assistant["children"], "INDEX.md")["path"] == "assistant/INDEX.md"

    arch = _find(assistant["children"], "architecture")
    assert arch["path"] == "assistant/architecture"
    assert _find(arch["children"], "overview.md")["path"] == (
        "assistant/architecture/overview.md"
    )


def test_directories_sort_before_files(client: TestClient, memory_dir: Path) -> None:
    _write(memory_dir / "zzz_folder" / "a.md")
    _write(memory_dir / "aaa_file.md")

    assert _names(client.get("/api/memory/tree").json()) == ["zzz_folder", "aaa_file.md"]


def test_sort_is_case_insensitive(client: TestClient, memory_dir: Path) -> None:
    for name in ("banana.md", "Apple.md", "cherry.md"):
        _write(memory_dir / name)

    assert _names(client.get("/api/memory/tree").json()) == [
        "Apple.md",
        "banana.md",
        "cherry.md",
    ]


def test_ignores_non_markdown(client: TestClient, memory_dir: Path) -> None:
    _write(memory_dir / "keep.md")
    _write(memory_dir / "skip.txt", "nope")
    _write(memory_dir / "skip.json", "{}")
    _write(memory_dir / "image.png", "binary-ish")

    assert _names(client.get("/api/memory/tree").json()) == ["keep.md"]


def test_markdown_extension_is_case_insensitive(
    client: TestClient, memory_dir: Path
) -> None:
    _write(memory_dir / "Shouty.MD")
    assert _names(client.get("/api/memory/tree").json()) == ["Shouty.MD"]


def test_prunes_directories_with_no_markdown(
    client: TestClient, memory_dir: Path
) -> None:
    # A folder holding only non-markdown, and a deep chain of empty folders.
    _write(memory_dir / "assets" / "logo.png", "x")
    (memory_dir / "empty" / "deeper").mkdir(parents=True)
    _write(memory_dir / "real.md")

    assert _names(client.get("/api/memory/tree").json()) == ["real.md"]


def test_keeps_directory_whose_markdown_is_nested_deep(
    client: TestClient, memory_dir: Path
) -> None:
    """A folder with no direct .md but markdown further down must survive."""
    _write(memory_dir / "a" / "b" / "c" / "deep.md")

    tree = client.get("/api/memory/tree").json()
    a = _find(tree, "a")
    b = _find(a["children"], "b")
    c = _find(b["children"], "c")
    assert _names(c["children"]) == ["deep.md"]
    assert c["children"][0]["path"] == "a/b/c/deep.md"


def test_skips_dotfiles_and_dotdirs(client: TestClient, memory_dir: Path) -> None:
    _write(memory_dir / ".hidden.md")
    _write(memory_dir / ".obsidian" / "config.md")
    _write(memory_dir / "visible.md")

    assert _names(client.get("/api/memory/tree").json()) == ["visible.md"]


def test_skips_symlink_escaping_the_tree(
    client: TestClient, memory_dir: Path, tmp_path: Path
) -> None:
    outside = _write(tmp_path / "outside" / "secret.md", "# secret")
    (memory_dir / "leak.md").symlink_to(outside)
    _write(memory_dir / "legit.md")

    assert _names(client.get("/api/memory/tree").json()) == ["legit.md"]
