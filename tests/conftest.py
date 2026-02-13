import sys
from pathlib import Path

import pytest

# Make scripts/ importable
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture
def tmp_index(tmp_path, monkeypatch):
    """Redirect embed.py and search.py to a temporary ChromaDB directory."""
    index_dir = tmp_path / "index" / "chroma"
    index_dir.mkdir(parents=True)

    import embed as embed_mod
    import search as search_mod

    monkeypatch.setattr(embed_mod, "INDEX_DIR", index_dir)
    monkeypatch.setattr(search_mod, "INDEX_DIR", index_dir)

    embed_mod._clients.clear()

    yield index_dir

    embed_mod._clients.clear()


@pytest.fixture(scope="session")
def sentence_model():
    """Load the sentence-transformer model once for all integration tests."""
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer("all-MiniLM-L6-v2")


@pytest.fixture
def patched_model(sentence_model, monkeypatch):
    """Patch embed.py and search.py to reuse the session-scoped model."""
    import embed as embed_mod

    monkeypatch.setattr(embed_mod, "_model", sentence_model)
    monkeypatch.setattr(
        "sentence_transformers.SentenceTransformer", lambda name: sentence_model
    )


@pytest.fixture
def sample_files(tmp_path):
    """Create a directory tree with known content for indexing tests."""
    d = tmp_path / "docs"
    d.mkdir()

    # 15-line markdown file (predictable chunk counts)
    md_content = "\n".join(
        f"Line {i}: This is test content for line number {i}." for i in range(1, 16)
    )
    (d / "notes.md").write_text(md_content, encoding="utf-8")

    # Python file (should be indexed)
    (d / "script.py").write_text("# A test script\nprint('hello')\n", encoding="utf-8")

    # Unsupported extension (should be SKIPPED)
    (d / "image.png").write_bytes(b"\x89PNG\r\n")

    # Empty file (should produce zero chunks)
    (d / "empty.md").write_text("", encoding="utf-8")

    # Unicode content
    (d / "unicode.txt").write_text(
        "Caf\u00e9 \u2014 m\u00f6tley cr\u00fce\n\u65e5\u672c\u8a9e\u30c6\u30b9\u30c8\n",
        encoding="utf-8",
    )

    # Binary file disguised as .txt (UnicodeDecodeError test)
    (d / "binary.txt").write_bytes(b"\x80\x81\x82\x83\xff\xfe")

    # Nested directory with a .yaml file
    sub = d / "subdir"
    sub.mkdir()
    (sub / "config.yaml").write_text("key: value\nnested:\n  item: 1\n", encoding="utf-8")

    # .git directory (should be excluded)
    gitdir = d / ".git"
    gitdir.mkdir()
    (gitdir / "config").write_text("gitconfig", encoding="utf-8")

    return d
