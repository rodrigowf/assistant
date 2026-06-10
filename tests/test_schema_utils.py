"""Increment E — parity tests for the shared schema_utils module.

The helpers previously lived inline in each provider:

* ``_sanitize_schema_for_gemini`` — was in ``gemini_voice_base.py``.
* ``_sanitize_tool_for_qwen`` + ``_scrub_union_types`` — were in
  ``qwen_voice.py``.
* ``_sanitize_for_qwen`` + the ``_URL_LIKE_RE`` pattern — were in
  ``qwen_voice.py``.

Plan §E moves them to a shared ``orchestrator/providers/schema_utils.py``
so the providers' files shrink to provider-specific logic. The move
must be byte-identical: the existing
``tests/test_gemini_voice.py::test_sanitize_*`` tests already pin the
Gemini sanitizer's output; this file complements that by pinning the
Qwen helpers (which had no dedicated tests at HEAD) and by re-checking
the Gemini sanitizer through the new import path.

Per plan §0.1, behavior MUST NOT change — only the file the helper
lives in changes.
"""

from __future__ import annotations

import pytest


# ---------- Gemini sanitizer (re-imported via the new path) -----------------


def test_gemini_sanitizer_reachable_through_schema_utils():
    """The new module re-exports the Gemini sanitizer. Existing
    test_gemini_voice.py still imports from gemini_voice_base — both
    paths must point at the same function.
    """
    from orchestrator.providers.schema_utils import (
        sanitize_schema_for_gemini,
    )
    from orchestrator.providers.gemini_voice_base import (
        _sanitize_schema_for_gemini,
    )
    # Same function object: gemini_voice_base must re-export from
    # schema_utils to keep the back-compat import path.
    assert sanitize_schema_for_gemini is _sanitize_schema_for_gemini


def test_gemini_sanitizer_collapses_optional_union():
    """Quick smoke test through the new module — full coverage stays
    in test_gemini_voice.py.
    """
    from orchestrator.providers.schema_utils import (
        sanitize_schema_for_gemini,
    )
    src = {"type": ["string", "null"], "description": "maybe"}
    out = sanitize_schema_for_gemini(src)
    assert out == {"type": "string", "description": "maybe", "nullable": True}


# ---------- Qwen tool sanitizer --------------------------------------------


def test_qwen_tool_sanitizer_collapses_union_types():
    """``_sanitize_tool_for_qwen`` must collapse ``type: [..., 'null']``
    in nested tool schemas. The 2026-05-15 bisect identified
    ``read_agent_session.max_messages: [integer, null]`` as the trigger;
    the same pattern in any tool would close the WS with 1011 from
    DashScope's ``session.update`` parser.
    """
    from orchestrator.providers.schema_utils import sanitize_tool_for_qwen
    src = {
        "name": "read_agent_session",
        "parameters": {
            "type": "object",
            "properties": {
                "max_messages": {"type": ["integer", "null"]},
                "session_id": {"type": "string"},
            },
        },
    }
    out = sanitize_tool_for_qwen(src)
    assert out["parameters"]["properties"]["max_messages"]["type"] == "integer"
    # Non-union types pass through untouched.
    assert out["parameters"]["properties"]["session_id"]["type"] == "string"


def test_qwen_tool_sanitizer_idempotent():
    """Running the sanitizer twice must yield the same result — no
    accidental double-mutation. Defends a future call site that pipes
    a tool through twice (e.g., reconnect rebuild path)."""
    from orchestrator.providers.schema_utils import sanitize_tool_for_qwen
    src = {
        "parameters": {
            "type": "object",
            "properties": {"x": {"type": ["string", "null"]}},
        },
    }
    once = sanitize_tool_for_qwen(src)
    twice = sanitize_tool_for_qwen(once)
    assert once == twice


def test_qwen_tool_sanitizer_does_not_mutate_input():
    """The sanitizer returns a new dict — callers (tools registry) keep
    a reference to the canonical tool schema for non-Qwen providers."""
    from orchestrator.providers.schema_utils import sanitize_tool_for_qwen
    src = {
        "parameters": {
            "type": "object",
            "properties": {"x": {"type": ["string", "null"]}},
        },
    }
    out = sanitize_tool_for_qwen(src)
    # Original is unchanged.
    assert src["parameters"]["properties"]["x"]["type"] == ["string", "null"]


# ---------- Qwen text URL sanitizer -----------------------------------------


def test_qwen_text_sanitizer_wraps_localhost_url():
    """``sanitize_text_for_qwen`` wraps bare URL-shapes in backticks
    so DashScope's URL validator skips them (it only accepts
    http/https/data/file schemes). Empirically:
    ``localhost:8765`` mid-prompt closes the WS with the misleading
    "URL does not appear to be valid" 400.
    """
    from orchestrator.providers.schema_utils import sanitize_text_for_qwen
    src = "Visit localhost:8765 for the dashboard."
    out = sanitize_text_for_qwen(src)
    assert "`localhost:8765`" in out


def test_qwen_text_sanitizer_leaves_proper_urls_alone():
    """Already-scheme'd URLs pass through unchanged."""
    from orchestrator.providers.schema_utils import sanitize_text_for_qwen
    src = "See https://example.com/docs for details."
    out = sanitize_text_for_qwen(src)
    assert out == src


def test_qwen_text_sanitizer_idempotent():
    """Tokens already wrapped in backticks are not double-wrapped."""
    from orchestrator.providers.schema_utils import sanitize_text_for_qwen
    src = "Run `localhost:8765` to start"
    out = sanitize_text_for_qwen(src)
    assert out.count("`localhost:8765`") == 1
    # No stacking.
    assert "``localhost" not in out
