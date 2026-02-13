"""CLI argument parsing tests — mock downstream functions, test argparse routing."""

from unittest.mock import patch

import pytest

import embed
import search


class TestEmbedCLI:
    def test_index_default_args(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["embed.py", "index", "memory/"])

        with patch.object(embed, "index_path") as mock:
            embed.main()
            mock.assert_called_once_with("memory/", "memory", 10, 3)

    def test_index_custom_args(self, monkeypatch):
        monkeypatch.setattr(
            "sys.argv",
            ["embed.py", "index", "data/", "--collection", "test", "--chunk-size", "20", "--overlap", "5"],
        )

        with patch.object(embed, "index_path") as mock:
            embed.main()
            mock.assert_called_once_with("data/", "test", 20, 5)

    def test_delete_command(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["embed.py", "delete", "memory/notes.md"])

        with patch.object(embed, "delete_path") as mock:
            embed.main()
            mock.assert_called_once_with("memory/notes.md", "memory")

    def test_reset_command(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["embed.py", "reset", "--collection", "history"])

        with patch.object(embed, "reset_collection") as mock:
            embed.main()
            mock.assert_called_once_with("history")

    def test_stats_command(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["embed.py", "stats"])

        with patch.object(embed, "show_stats") as mock:
            embed.main()
            mock.assert_called_once_with("memory")

    def test_no_command_shows_help(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["embed.py"])

        embed.main()
        # Should not crash; argparse prints help or nothing


class TestSearchCLI:
    def test_basic_query(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["search.py", "test", "query"])

        with patch.object(search, "search", return_value=[]) as mock_search:
            with patch.object(search, "print_results") as mock_print:
                search.main()
                mock_search.assert_called_once_with(
                    "test query",
                    collection_name="memory",
                    n_results=5,
                    threshold=1.5,
                    file_filter=None,
                )

    def test_with_options(self, monkeypatch):
        monkeypatch.setattr(
            "sys.argv",
            ["search.py", "query", "--n", "10", "--threshold", "0.5", "--file", "memory/", "--json"],
        )

        with patch.object(search, "search", return_value=[]) as mock_search:
            with patch.object(search, "print_results") as mock_print:
                search.main()
                mock_search.assert_called_once_with(
                    "query",
                    collection_name="memory",
                    n_results=10,
                    threshold=0.5,
                    file_filter="memory/",
                )
                mock_print.assert_called_once_with([], as_json=True)

    def test_multi_word_query_joined(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["search.py", "how", "to", "embed"])

        with patch.object(search, "search", return_value=[]) as mock_search:
            with patch.object(search, "print_results"):
                search.main()
                mock_search.assert_called_once()
                assert mock_search.call_args[0][0] == "how to embed"
