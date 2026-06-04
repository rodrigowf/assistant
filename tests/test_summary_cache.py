"""Tests for orchestrator.summary_cache."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from orchestrator import summary_cache


@pytest.fixture
def jsonl(tmp_path: Path) -> Path:
    p = tmp_path / "abc-1234.jsonl"
    p.write_text('{"role":"user","content":"hi"}\n')
    return p


def test_read_returns_none_when_no_cache(jsonl: Path) -> None:
    assert summary_cache.read(jsonl) is None


def test_write_then_read_roundtrip(jsonl: Path) -> None:
    summary_cache.write(
        jsonl,
        summary_text="A long summary of what was said.",
        input_message_count=42,
        summary_target_words=(100, 200),
        summarizer_model="claude-sonnet-4-6",
    )
    cached = summary_cache.read(jsonl)
    assert cached is not None
    assert cached.summary_text == "A long summary of what was said."
    assert cached.input_message_count == 42
    assert cached.summary_target_words == (100, 200)
    assert cached.summarizer_model == "claude-sonnet-4-6"
    assert cached.generated_at  # ISO timestamp


def test_cache_invalidated_when_jsonl_grows(jsonl: Path) -> None:
    summary_cache.write(
        jsonl, summary_text="s", input_message_count=1,
        summary_target_words=None, summarizer_model=None,
    )
    assert summary_cache.read(jsonl) is not None

    # Append a new turn — size changes → cache stale.
    # Sleep ensures mtime ns also bumps even on filesystems that round.
    time.sleep(0.01)
    with open(jsonl, "a") as f:
        f.write('{"role":"assistant","content":"reply"}\n')

    assert summary_cache.read(jsonl) is None
    assert summary_cache.is_fresh(jsonl) is False


def test_is_fresh_short_circuits_without_reading_summary(jsonl: Path) -> None:
    # No cache → not fresh.
    assert summary_cache.is_fresh(jsonl) is False
    summary_cache.write(
        jsonl, summary_text="s" * 10_000, input_message_count=1,
        summary_target_words=None, summarizer_model=None,
    )
    assert summary_cache.is_fresh(jsonl) is True


def test_schema_version_bump_invalidates_cache(jsonl: Path, monkeypatch) -> None:
    summary_cache.write(
        jsonl, summary_text="s", input_message_count=1,
        summary_target_words=None, summarizer_model=None,
    )
    # Manually rewrite the cache with an older schema_version.
    cp = summary_cache.cache_path_for(jsonl)
    raw = json.loads(cp.read_text())
    raw["schema_version"] = summary_cache.SCHEMA_VERSION - 1
    cp.write_text(json.dumps(raw))
    assert summary_cache.read(jsonl) is None


def test_corrupt_cache_returns_none_without_raising(jsonl: Path) -> None:
    cp = summary_cache.cache_path_for(jsonl)
    cp.write_text("not json {{{")
    assert summary_cache.read(jsonl) is None
    assert summary_cache.is_fresh(jsonl) is False


def test_write_skipped_when_jsonl_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nope.jsonl"
    # Should not raise.
    summary_cache.write(
        missing, summary_text="s", input_message_count=1,
        summary_target_words=None, summarizer_model=None,
    )
    assert not summary_cache.cache_path_for(missing).exists()


def test_cache_path_is_sibling_with_summary_suffix(tmp_path: Path) -> None:
    j = tmp_path / "uuid-here.jsonl"
    assert summary_cache.cache_path_for(j) == tmp_path / "uuid-here.summary.json"
