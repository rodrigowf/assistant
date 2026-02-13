"""Integration tests for search.py — requires indexed data."""

import json

import pytest

import embed
import search


pytestmark = pytest.mark.slow


@pytest.fixture
def indexed_data(tmp_index, patched_model, tmp_path):
    """Index two files with distinct content for relevance testing."""
    d = tmp_path / "searchdata"
    d.mkdir()

    # File about embeddings
    (d / "embeddings.md").write_text(
        "Embedding pipeline design\n"
        "Vector search using ChromaDB and sentence transformers\n"
        "Chunking files into overlapping segments for indexing\n"
        "Cosine similarity measures semantic relatedness\n"
        "The model encodes text into 384-dimensional vectors\n"
    )

    # File about cooking
    (d / "cooking.md").write_text(
        "Recipe for chocolate cake\n"
        "Mix flour sugar and eggs in a bowl\n"
        "Preheat the oven to 350 degrees\n"
        "Bake for thirty minutes until golden brown\n"
        "Serve with whipped cream and strawberries\n"
    )

    embed.index_path(d, collection_name="search_test")
    return d


class TestSearchResults:
    def test_returns_results(self, indexed_data):
        results = search.search("embedding pipeline", collection_name="search_test")
        assert len(results) > 0

    def test_result_fields(self, indexed_data):
        results = search.search("embedding", collection_name="search_test")
        r = results[0]
        assert "text" in r
        assert "file_path" in r
        assert "start_line" in r
        assert "end_line" in r
        assert "distance" in r

    def test_distance_is_float(self, indexed_data):
        results = search.search("embedding", collection_name="search_test")
        for r in results:
            assert isinstance(r["distance"], float)

    def test_n_results_limit(self, indexed_data):
        results = search.search("content", collection_name="search_test", n_results=2)
        assert len(results) <= 2

    def test_relevance_ordering(self, indexed_data):
        """Embedding-related query should rank embedding file higher than cooking."""
        results = search.search(
            "vector search embeddings", collection_name="search_test", n_results=5
        )
        # Find which file appears first
        first_file = results[0]["file_path"]
        assert "embeddings.md" in first_file


class TestThresholdFiltering:
    def test_strict_threshold_filters_all(self, indexed_data):
        results = search.search(
            "embedding", collection_name="search_test", threshold=0.0
        )
        assert len(results) == 0

    def test_lenient_threshold_allows(self, indexed_data):
        results = search.search(
            "embedding", collection_name="search_test", threshold=2.0
        )
        assert len(results) > 0


class TestFileFilter:
    def test_filter_narrows_results(self, indexed_data):
        results = search.search(
            "content", collection_name="search_test", file_filter="cooking.md"
        )
        for r in results:
            assert "cooking.md" in r["file_path"]


class TestSearchErrors:
    def test_no_index_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(search, "INDEX_DIR", tmp_path / "nonexistent")

        with pytest.raises(SystemExit) as exc_info:
            search.search("query", collection_name="test")
        assert exc_info.value.code == 1

    def test_missing_collection(self, tmp_index, patched_model):
        with pytest.raises(SystemExit) as exc_info:
            search.search("query", collection_name="nonexistent_collection")
        assert exc_info.value.code == 1

    def test_empty_collection(self, tmp_index, patched_model):
        # Create but don't populate
        embed.get_collection("empty_test")

        with pytest.raises(SystemExit) as exc_info:
            search.search("query", collection_name="empty_test")
        assert exc_info.value.code == 1


class TestPrintResults:
    def test_text_format(self, capsys):
        results = [
            {
                "text": "Test content",
                "file_path": "/tmp/test.md",
                "start_line": 1,
                "end_line": 5,
                "file_name": "test.md",
                "distance": 0.1234,
            }
        ]
        search.print_results(results)
        captured = capsys.readouterr()
        assert "Result 1" in captured.out
        assert "0.1234" in captured.out
        assert "Test content" in captured.out

    def test_json_format(self, capsys):
        results = [
            {
                "text": "Test content",
                "file_path": "/tmp/test.md",
                "start_line": 1,
                "end_line": 5,
                "file_name": "test.md",
                "distance": 0.1234,
            }
        ]
        search.print_results(results, as_json=True)
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert len(parsed) == 1
        assert parsed[0]["text"] == "Test content"

    def test_empty_results(self, capsys):
        search.print_results([])
        captured = capsys.readouterr()
        assert "No results found" in captured.out
