"""Shared schema + text sanitizers for voice providers.

Increment E (plan §E) moved these helpers out of the provider files so
each provider's source shrinks to provider-specific logic. Two
sanitizers live here:

* ``sanitize_schema_for_gemini`` — converts JSON Schema Draft 7 to
  the OpenAPI 3.0 subset Gemini Live's ``functionDeclarations`` accepts.
* ``sanitize_tool_for_qwen`` — recursively scrubs ``"type": [..., "null"]``
  union types from a tool definition. DashScope's ``session.update``
  parser closes the WS with WS 1011 when it sees one.
* ``sanitize_text_for_qwen`` — wraps URL-shaped substrings in
  backticks so DashScope's URL validator skips them. The validator
  only accepts ``http://``, ``https://``, ``data:``, ``file://``
  schemes; scheme-less URL-shapes mid-prompt close the WS with a
  misleading "URL does not appear to be valid" 400.

All functions return new objects — they don't mutate inputs. Each
function preserves the behavior it had at HEAD before Inc E (parity
covered by ``tests/test_schema_utils.py`` and the pre-existing
``tests/test_gemini_voice.py::test_sanitize_*`` tests).
"""

from __future__ import annotations

import re
from typing import Any


# --- Gemini Live schema sanitizer ------------------------------------------

# JSON Schema keywords Gemini Live ignores or rejects in
# function-declaration parameters. Stripping rather than rejecting:
# we want to send the best schema we can, not refuse to call the tool.
_GEMINI_SCHEMA_STRIP_KEYS = frozenset({
    "$schema",
    "$id",
    "$ref",
    "$defs",
    "definitions",
    "additionalProperties",
    "patternProperties",
    "unevaluatedProperties",
    "unevaluatedItems",
    "if",
    "then",
    "else",
    "not",
    "dependencies",
    "dependentSchemas",
    "dependentRequired",
})


def sanitize_schema_for_gemini(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert a JSON Schema to the subset Gemini's Live API accepts.

    Gemini's ``functionDeclarations[].parameters`` follows OpenAPI 3.0
    Schema, which is a strict subset of JSON Schema Draft 7.
    Mismatches the orchestrator's tool schemas tend to hit:

    - ``"type": ["X", "null"]`` (union types) → split into
      ``"type": "X", "nullable": true``.
    - ``anyOf`` / ``oneOf`` / ``allOf`` containing exactly one schema
      and one ``{"type": "null"}`` (the OpenAPI pattern for optionals)
      → flatten to the non-null branch + ``nullable: true``.
    - ``additionalProperties``, ``$schema``, ``$ref``, etc. → strip.

    Everything else (``type``, ``description``, ``properties``,
    ``required``, ``items``, ``enum``, ``format``, ``minimum``,
    ``maximum``, ``nullable``) passes through. Recurses into
    ``properties``, ``items``, ``anyOf``/``oneOf``/``allOf``.

    Returns a new dict — does not mutate the input.
    """
    if not isinstance(schema, dict):
        return schema

    out: dict[str, Any] = {}
    nullable = False

    # Handle anyOf/oneOf/allOf with a null branch (optional pattern).
    for combinator in ("anyOf", "oneOf", "allOf"):
        if combinator in schema:
            branches = schema[combinator]
            if isinstance(branches, list):
                non_null = [b for b in branches if not (isinstance(b, dict) and b.get("type") == "null")]
                has_null = len(non_null) < len(branches)
                if has_null:
                    nullable = True
                if len(non_null) == 1:
                    # Pattern: anyOf:[{...}, {type: null}] → merge the
                    # single non-null branch directly into ``out`` and
                    # drop the combinator (Gemini still rejects raw
                    # anyOf even of length 1 in practice).
                    out.update(sanitize_schema_for_gemini(non_null[0]))
                elif len(non_null) > 1:
                    # Multi-branch union — keep as anyOf with each
                    # branch sanitized. Gemini accepts anyOf in some
                    # cases; if it still rejects, the caller will see
                    # the error and refine.
                    out[combinator] = [sanitize_schema_for_gemini(b) for b in non_null]
                # Mark this combinator handled.
                # (Falls through — we don't break since multiple combinators
                # are rare; we sanitize each.)

    for k, v in schema.items():
        if k in _GEMINI_SCHEMA_STRIP_KEYS:
            continue
        if k in ("anyOf", "oneOf", "allOf"):
            # Already handled above.
            continue
        if k == "type":
            if isinstance(v, list):
                # ["X", "null"] → "X" + nullable=True; ["X", "Y"] →
                # keep first non-null (best-effort — Gemini wants a
                # scalar type).
                non_null = [t for t in v if t != "null"]
                nullable = nullable or ("null" in v)
                out["type"] = non_null[0] if non_null else "string"
            else:
                out["type"] = v
        elif k == "properties" and isinstance(v, dict):
            out["properties"] = {
                pname: sanitize_schema_for_gemini(pschema)
                for pname, pschema in v.items()
            }
        elif k == "items" and isinstance(v, dict):
            out["items"] = sanitize_schema_for_gemini(v)
        elif k == "items" and isinstance(v, list):
            # Tuple-form items — Gemini doesn't support; collapse to
            # the first entry as a best-effort.
            if v:
                out["items"] = sanitize_schema_for_gemini(v[0])
        else:
            out[k] = v

    if nullable:
        out["nullable"] = True
    return out


# --- Qwen tool schema sanitizer (union-type collapse) ----------------------


def sanitize_tool_for_qwen(tool: dict[str, Any]) -> dict[str, Any]:
    """Recursively scrub JSON Schema union types from a tool definition.

    DashScope's ``session.update`` parser closes the WebSocket with
    ``InternalError: Parse RealtimeEvent error: Common error!`` (1011)
    when any parameter schema uses ``"type": ["X", "null"]``. This is
    valid JSON Schema Draft 7 but unsupported here. We collapse the
    union to its first non-null branch and drop the null option;
    callers who relied on accepting null should mark the field
    non-required instead.

    Bisected 2026-05-15 — when this sanitiser is bypassed, the only
    tool in our current registry that trips it is
    ``read_agent_session`` via its ``max_messages: [integer, null]``
    parameter.
    """
    if not isinstance(tool, dict):
        return tool
    return _scrub_union_types(tool)


def _scrub_union_types(node: Any) -> Any:
    """Recursively rewrite ``"type": [..., "null"]`` to a scalar type."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k == "type" and isinstance(v, list):
                non_null = [t for t in v if t != "null"]
                # Best-effort: keep the first non-null type, default to
                # "string" if the union was purely null (unlikely).
                out[k] = non_null[0] if non_null else "string"
            else:
                out[k] = _scrub_union_types(v)
        return out
    if isinstance(node, list):
        return [_scrub_union_types(x) for x in node]
    return node


# --- Qwen text URL sanitizer -----------------------------------------------

# DashScope's omni URL validator only accepts URLs with one of the
# ``http://`` / ``https://`` / ``data:`` / ``file://`` schemes;
# scheme-less URL-shapes mid-prompt close the WS with a misleading
# "URL does not appear to be valid" 400. We pre-process the text to
# wrap bare URL-shapes in backticks so the validator treats them as
# code spans and skips them. Targets:
# - ``localhost`` / ``localhost:port`` / ``localhost:port/path``
# - IPv4 / IPv4:port / IPv4/path
# - ``host.tld:port`` (any explicit-port URL)
# - absolute POSIX paths with 3+ segments (``/home/rodrigo/...``)
_URL_LIKE_RE = re.compile(
    r"(?<![\w/:.\-`])"
    r"(?:"
    # localhost (optionally :port and/or /path)
    r"localhost(?::\d+)?(?:/[^\s)\]\"'`]*)?"
    # IPv4 (optionally :port and/or /path)
    r"|\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?(?:/[^\s)\]\"'`]*)?"
    # hostname with explicit :port — at least one dot in the host
    r"|(?:[a-zA-Z][\w\-]*\.)+[a-zA-Z]{2,}:\d+(?:/[^\s)\]\"'`]*)?"
    # absolute POSIX path with 3+ segments (matches things like
    # /home/rodrigo/Projects/... that DashScope's URL validator
    # misclassifies as URL-shaped reference).  Stops at whitespace,
    # quotes, brackets, or backticks.  Two-segment paths like /tmp/foo
    # are intentionally left alone — short paths haven't tripped the
    # validator empirically.
    r"|/(?:[\w.\-]+/){2,}[\w.\-]+(?:/[\w.\-]*)*"
    r")"
)


def sanitize_text_for_qwen(text: str) -> str:
    """Neutralise URL-shaped substrings that DashScope's omni URL
    validator rejects.

    The validator only accepts URLs with one of ``http://``, ``https://``,
    ``data:``, ``file://`` schemes; scheme-less URL-shapes (bare hosts,
    ``localhost:port``, IPs, dotted names) are rejected with the same
    misleading "URL does not appear to be valid" 400 used for malformed
    multimodal inputs.  Wrapping the matches in backticks (markdown code
    span) makes the validator skip them while keeping them legible to
    the model.
    """
    def _wrap(m: re.Match[str]) -> str:
        token = m.group(0)
        # Already inside backticks?  Leave alone (the prior char check is
        # cheap and avoids stacking quotes when the model echoes back).
        return f"`{token}`"
    return _URL_LIKE_RE.sub(_wrap, text)
