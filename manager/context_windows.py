"""Context window lookup by (provider, model).

Lets the API report a provider-correct context window to the frontend so
the "context used %" badge on the compact button stays meaningful for
Qwen/Gemini sessions (which support 1M+ tokens) and for orchestrator
sessions (which run on Anthropic / OpenAI text models directly).

Lookup order for a given (provider, model):

1. **Qwen**: parse ``~/.qwen/settings.json`` and read ``contextWindowSize``
   for the exact model id.  This is the only provider that ships a
   user-configurable catalog, so we trust whatever the user wrote.
2. **Claude / Gemini / Anthropic / OpenAI**: regex-match the model id
   against a small static table.  Aliases (``"sonnet"``, ``"opus"``)
   resolve to the same window as the dated id.

Returns a positive int (the window size in tokens) or ``None`` when we
have no opinion.  Callers should fall back to a conservative default
(200K) when ``None`` comes back.
"""

from __future__ import annotations

import re


# Static fallback table — (regex, window_tokens), checked in order.
# Keep ordered specific → general so ``claude-haiku`` doesn't shadow
# ``claude-sonnet`` etc.  Numbers are the public documented limits as of
# the date noted next to each entry.
_STATIC_WINDOWS: list[tuple[re.Pattern[str], int]] = [
    # --- Anthropic Claude (claude.com/docs/about-claude/models, 2026-05) ---
    # Claude 4.x family — 200K standard, 1M with the long-context beta header
    # (orchestrator does NOT currently set that header, so 200K is right).
    (re.compile(r"claude-(opus|sonnet|haiku)-4", re.I), 200_000),
    (re.compile(r"claude-(opus|sonnet|haiku)-3", re.I), 200_000),
    (re.compile(r"^(opus|sonnet|haiku)$", re.I),       200_000),

    # --- OpenAI (platform.openai.com/docs/models, 2026-05) ---
    # GPT-5 family (incl. gpt-5.5) — 400K context.
    (re.compile(r"^gpt-5",          re.I), 400_000),
    # GPT-4o / gpt-4.1 — 128K standard, gpt-4.1 has a 1M variant we don't
    # use by default.  Stick with the conservative documented number.
    (re.compile(r"^gpt-4\.1",       re.I), 1_000_000),
    (re.compile(r"^gpt-4o",         re.I), 128_000),
    (re.compile(r"^gpt-4",          re.I), 128_000),
    (re.compile(r"^o[134](-|$)",    re.I), 200_000),
    (re.compile(r"^gpt-realtime",   re.I), 32_000),

    # --- Google Gemini (ai.google.dev/gemini-api/docs/models, 2026-05) ---
    # Gemini 2.5 / 3.x all share a 1M input window.
    (re.compile(r"gemini-(2\.5|3\.|2\.0)",   re.I), 1_000_000),
    (re.compile(r"gemini-1\.5",              re.I), 1_000_000),
    (re.compile(r"^gemini",                  re.I), 1_000_000),
]


def _qwen_window(model: str) -> int | None:
    """Look up ``model`` in the user's Qwen settings.json catalog."""
    try:
        from manager.qwen.models import list_qwen_models
    except Exception:
        return None
    try:
        for entry in list_qwen_models():
            if entry.id == model and entry.context_window:
                return entry.context_window
    except Exception:
        return None
    return None


def context_window_for(provider: str | None, model: str | None) -> int | None:
    """Return the context window (tokens) for a (provider, model) pair.

    Returns ``None`` when nothing matches — callers should fall back to a
    conservative default (200K) so the UI still shows *something*.
    """
    if provider == "qwen" and model:
        # Qwen catalog is user-editable; honor whatever they configured.
        explicit = _qwen_window(model)
        if explicit is not None:
            return explicit
        # Fall through to the static table for an unknown qwen model.
    if not isinstance(model, str) or not model:
        # No model id → fall back to provider-level defaults.
        if provider == "qwen":
            return 1_000_000   # Qwen3.x baseline
        if provider == "gemini":
            return 1_000_000
        if provider in ("claude", None):
            return 200_000
        return None

    for pattern, window in _STATIC_WINDOWS:
        if pattern.search(model):
            return window
    return None
