"""Pure unit tests for chunk_file() — no model, no ChromaDB needed."""

import pytest

from embed import chunk_file


class TestBasicChunking:
    def test_basic_chunk_count(self, tmp_path):
        """15-line file with chunk_size=10, overlap=3 → 3 chunks (trailing overlap)."""
        f = tmp_path / "test.md"
        f.write_text("\n".join(f"Line {i}" for i in range(1, 16)))

        chunks = chunk_file(f, chunk_size=10, overlap=3)
        # i=0 → lines 1-10, i=7 → lines 8-15, i=14 → line 15
        assert len(chunks) == 3

    def test_first_chunk_lines(self, tmp_path):
        """First chunk covers lines 1-10."""
        f = tmp_path / "test.md"
        f.write_text("\n".join(f"Line {i}" for i in range(1, 16)))

        chunks = chunk_file(f, chunk_size=10, overlap=3)
        assert chunks[0]["metadata"]["start_line"] == 1
        assert chunks[0]["metadata"]["end_line"] == 10

    def test_second_chunk_lines(self, tmp_path):
        """Second chunk starts at line 8 (overlap of 3) and ends at line 15."""
        f = tmp_path / "test.md"
        f.write_text("\n".join(f"Line {i}" for i in range(1, 16)))

        chunks = chunk_file(f, chunk_size=10, overlap=3)
        assert chunks[1]["metadata"]["start_line"] == 8
        assert chunks[1]["metadata"]["end_line"] == 15

    def test_overlap_creates_shared_lines(self, tmp_path):
        """Lines 8-10 appear in both chunks."""
        f = tmp_path / "test.md"
        lines = [f"Line {i}" for i in range(1, 16)]
        f.write_text("\n".join(lines))

        chunks = chunk_file(f, chunk_size=10, overlap=3)
        shared = set(range(8, 11))  # lines 8, 9, 10
        chunk1_range = set(range(chunks[0]["metadata"]["start_line"], chunks[0]["metadata"]["end_line"] + 1))
        chunk2_range = set(range(chunks[1]["metadata"]["start_line"], chunks[1]["metadata"]["end_line"] + 1))
        assert shared.issubset(chunk1_range & chunk2_range)


class TestChunkIDs:
    def test_ids_are_stable(self, tmp_path):
        """Calling chunk_file twice produces the same IDs."""
        f = tmp_path / "test.md"
        f.write_text("Hello\nWorld\n")

        ids1 = [c["id"] for c in chunk_file(f)]
        ids2 = [c["id"] for c in chunk_file(f)]
        assert ids1 == ids2

    def test_ids_differ_across_files(self, tmp_path):
        """Same content, different paths → different IDs."""
        f1 = tmp_path / "a.md"
        f2 = tmp_path / "b.md"
        content = "Same content\n"
        f1.write_text(content)
        f2.write_text(content)

        ids1 = {c["id"] for c in chunk_file(f1)}
        ids2 = {c["id"] for c in chunk_file(f2)}
        assert ids1.isdisjoint(ids2)


class TestChunkMetadata:
    def test_metadata_fields_present(self, tmp_path):
        """Each chunk has file_path, start_line, end_line, file_name."""
        f = tmp_path / "test.md"
        f.write_text("Line 1\nLine 2\n")

        chunks = chunk_file(f)
        for chunk in chunks:
            meta = chunk["metadata"]
            assert "file_path" in meta
            assert "start_line" in meta
            assert "end_line" in meta
            assert "file_name" in meta

    def test_start_lines_are_1_indexed(self, tmp_path):
        """First chunk starts at line 1, not 0."""
        f = tmp_path / "test.md"
        f.write_text("Line 1\nLine 2\n")

        chunks = chunk_file(f)
        assert chunks[0]["metadata"]["start_line"] == 1

    def test_file_name_in_metadata(self, tmp_path):
        """file_name matches the actual filename."""
        f = tmp_path / "my-notes.md"
        f.write_text("Content\n")

        chunks = chunk_file(f)
        assert chunks[0]["metadata"]["file_name"] == "my-notes.md"


class TestEdgeCases:
    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.md"
        f.write_text("")
        assert chunk_file(f) == []

    def test_blank_lines_only(self, tmp_path):
        f = tmp_path / "blank.md"
        f.write_text("\n\n\n\n")
        assert chunk_file(f) == []

    def test_single_line_file(self, tmp_path):
        f = tmp_path / "one.md"
        f.write_text("Single line")

        chunks = chunk_file(f)
        assert len(chunks) == 1
        assert "Single line" in chunks[0]["text"]

    def test_file_shorter_than_chunk_size(self, tmp_path):
        f = tmp_path / "short.md"
        f.write_text("\n".join(f"Line {i}" for i in range(1, 6)))

        chunks = chunk_file(f, chunk_size=10)
        assert len(chunks) == 1

    def test_chunk_size_equals_file_length(self, tmp_path):
        """10-line file, chunk_size=10, overlap=3 → 2 chunks (trailing overlap)."""
        f = tmp_path / "exact.md"
        f.write_text("\n".join(f"Line {i}" for i in range(1, 11)))

        chunks = chunk_file(f, chunk_size=10, overlap=3)
        # i=0 → lines 1-10, i=7 → lines 8-10
        assert len(chunks) == 2

    def test_no_overlap(self, tmp_path):
        """overlap=0 → non-overlapping chunks."""
        f = tmp_path / "test.md"
        f.write_text("\n".join(f"Line {i}" for i in range(1, 11)))

        chunks = chunk_file(f, chunk_size=5, overlap=0)
        assert len(chunks) == 2
        assert chunks[0]["metadata"]["end_line"] == 5
        assert chunks[1]["metadata"]["start_line"] == 6

    def test_chunk_text_is_stripped(self, tmp_path):
        """Chunk text has no leading/trailing whitespace."""
        f = tmp_path / "test.md"
        f.write_text("\n\nContent here\n\n")

        chunks = chunk_file(f)
        for chunk in chunks:
            assert chunk["text"] == chunk["text"].strip()


class TestUnicodeAndErrors:
    def test_unicode_content(self, tmp_path):
        f = tmp_path / "unicode.md"
        f.write_text("Caf\u00e9 \u2014 m\u00f6tley cr\u00fce\n\u65e5\u672c\u8a9e\u30c6\u30b9\u30c8\n")

        chunks = chunk_file(f)
        assert len(chunks) >= 1
        assert "Caf\u00e9" in chunks[0]["text"]

    def test_binary_file_returns_empty(self, tmp_path):
        f = tmp_path / "binary.txt"
        f.write_bytes(b"\x80\x81\x82\x83\xff\xfe")

        chunks = chunk_file(f)
        assert chunks == []

    def test_permission_error_returns_empty(self, tmp_path, monkeypatch):
        f = tmp_path / "noperm.md"
        f.write_text("Content")

        # Mock read_text to raise PermissionError
        monkeypatch.setattr(
            type(f), "read_text", lambda self, **kw: (_ for _ in ()).throw(PermissionError("denied"))
        )

        chunks = chunk_file(f)
        assert chunks == []


class TestOverlapGuard:
    def test_overlap_equals_chunk_size_raises(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("Line 1\nLine 2\nLine 3\n")

        with pytest.raises(ValueError, match="overlap.*must be less than chunk_size"):
            chunk_file(f, chunk_size=10, overlap=10)

    def test_overlap_greater_than_chunk_size_raises(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("Line 1\nLine 2\nLine 3\n")

        with pytest.raises(ValueError, match="overlap.*must be less than chunk_size"):
            chunk_file(f, chunk_size=5, overlap=7)

    def test_large_valid_overlap(self, tmp_path):
        """overlap=9, chunk_size=10 on 20-line file → many chunks, but terminates."""
        f = tmp_path / "test.md"
        f.write_text("\n".join(f"Line {i}" for i in range(1, 21)))

        chunks = chunk_file(f, chunk_size=10, overlap=9)
        assert len(chunks) >= 10  # Should produce many overlapping chunks
        # All lines should be covered
        all_covered = set()
        for c in chunks:
            all_covered.update(range(c["metadata"]["start_line"], c["metadata"]["end_line"] + 1))
        assert all_covered == set(range(1, 21))
