"""Integration tests for embed.py — requires ChromaDB and sentence-transformer model."""

import pytest

import embed


pytestmark = pytest.mark.slow


class TestGetClient:
    def test_creates_index_dir(self, tmp_path, monkeypatch):
        index_dir = tmp_path / "new" / "chroma"
        monkeypatch.setattr(embed, "INDEX_DIR", index_dir)
        embed._clients.clear()

        client = embed.get_client()
        assert index_dir.exists()
        assert client is not None

        embed._clients.clear()

    def test_is_cached(self, tmp_index):
        c1 = embed.get_client()
        c2 = embed.get_client()
        assert c1 is c2


class TestGetCollection:
    def test_creates_with_cosine(self, tmp_index):
        coll = embed.get_collection("test_cosine")
        assert coll.metadata.get("hnsw:space") == "cosine"

    def test_default_name(self, tmp_index):
        coll = embed.get_collection()
        assert coll.name == "memory"


class TestIndexPath:
    def test_index_single_file(self, tmp_index, patched_model, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("\n".join(f"Line {i}" for i in range(1, 6)))

        embed.index_path(f, collection_name="test")
        coll = embed.get_collection("test")
        assert coll.count() > 0

        results = coll.get()
        paths = {m["file_path"] for m in results["metadatas"]}
        assert str(f) in paths

    def test_index_directory_filters_extensions(self, tmp_index, patched_model, sample_files):
        embed.index_path(sample_files, collection_name="test")
        coll = embed.get_collection("test")

        results = coll.get()
        paths = {m["file_path"] for m in results["metadatas"]}

        # Should have indexed .md, .py, .txt, .yaml files
        extensions = {p.rsplit(".", 1)[-1] for p in paths}
        assert "md" in extensions
        assert "py" in extensions
        assert "yaml" in extensions

        # Should NOT have indexed .png
        assert not any(p.endswith(".png") for p in paths)

    def test_index_directory_excludes_git(self, tmp_index, patched_model, sample_files):
        embed.index_path(sample_files, collection_name="test")
        coll = embed.get_collection("test")

        results = coll.get()
        assert not any(".git" in m["file_path"] for m in results["metadatas"])

    def test_index_empty_directory(self, tmp_index, patched_model, tmp_path, capsys):
        d = tmp_path / "empty"
        d.mkdir()
        (d / "image.png").write_bytes(b"\x89PNG")  # Only unsupported file

        embed.index_path(d, collection_name="test")
        captured = capsys.readouterr()
        assert "No indexable files" in captured.out

    def test_index_nonexistent_path(self, tmp_index, patched_model):
        with pytest.raises(SystemExit) as exc_info:
            embed.index_path("/nonexistent/path")
        assert exc_info.value.code == 1

    def test_reindex_replaces_old_chunks(self, tmp_index, patched_model, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("Original content\nLine 2\n")

        embed.index_path(f, collection_name="test")
        count1 = embed.get_collection("test").count()

        # Modify and re-index
        f.write_text("New content\nMore lines\nEven more\n")
        embed.index_path(f, collection_name="test")
        count2 = embed.get_collection("test").count()

        # Should have new count, not old + new
        assert count2 >= 1
        # Verify content is updated
        results = embed.get_collection("test").get()
        all_text = " ".join(results["documents"])
        assert "New content" in all_text
        assert "Original content" not in all_text

    def test_reindex_idempotent(self, tmp_index, patched_model, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("Stable content\nLine 2\n")

        embed.index_path(f, collection_name="test")
        count1 = embed.get_collection("test").count()

        embed.index_path(f, collection_name="test")
        count2 = embed.get_collection("test").count()

        assert count1 == count2

    def test_index_skips_binary_in_directory(self, tmp_index, patched_model, sample_files, capsys):
        embed.index_path(sample_files, collection_name="test")
        captured = capsys.readouterr()

        # binary.txt should be skipped with a warning on stderr
        assert "binary.txt" in captured.err or "Skipping" in captured.err


class TestDeletePath:
    def test_delete_file_chunks(self, tmp_index, patched_model, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("Content\nMore content\n")

        embed.index_path(f, collection_name="test")
        assert embed.get_collection("test").count() > 0

        embed.delete_path(f, collection_name="test")
        assert embed.get_collection("test").count() == 0

    def test_delete_directory_chunks(self, tmp_index, patched_model, sample_files):
        embed.index_path(sample_files, collection_name="test")
        assert embed.get_collection("test").count() > 0

        embed.delete_path(sample_files, collection_name="test")
        assert embed.get_collection("test").count() == 0

    def test_delete_nonexistent_path(self, tmp_index, capsys):
        embed.delete_path("/nonexistent/file.md", collection_name="test")
        captured = capsys.readouterr()
        assert "No chunks found" in captured.out or "Deleted" in captured.out


class TestResetCollection:
    def test_reset(self, tmp_index, patched_model, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("Content\n")
        embed.index_path(f, collection_name="test_reset")
        assert embed.get_collection("test_reset").count() > 0

        embed.reset_collection("test_reset")
        # After reset, re-creating should give count 0
        assert embed.get_collection("test_reset").count() == 0

    def test_reset_nonexistent(self, tmp_index, capsys):
        embed.reset_collection("nonexistent_collection")
        captured = capsys.readouterr()
        assert "doesn't exist" in captured.out or "already empty" in captured.out


class TestShowStats:
    def test_stats_empty(self, tmp_index, capsys):
        embed.show_stats("empty_stats_test")
        captured = capsys.readouterr()
        assert "empty" in captured.out

    def test_stats_with_data(self, tmp_index, patched_model, tmp_path, capsys):
        f = tmp_path / "test.md"
        f.write_text("\n".join(f"Line {i}" for i in range(1, 6)))
        embed.index_path(f, collection_name="stats_test")

        embed.show_stats("stats_test")
        captured = capsys.readouterr()
        assert "stats_test" in captured.out
        assert "Total chunks" in captured.out
        assert "test.md" in captured.out
