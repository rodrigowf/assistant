#!/usr/bin/env python3
"""Run :mod:`probe-gemini-voice` and emit shell-evalable KEY=VALUE lines.

Used by the installers (Linux / macOS / Windows) so they can ``eval``
the result without parsing JSON inline. Keeps the shell side trivial
and the Python side single-source-of-truth.

Output (one line each, single-quoted values for safe ``eval`` in
bash/zsh; PowerShell parses these directly with ``$_ -split '='``)::

    VERTEX_STATUS='ok'
    VERTEX_REASON='...'
    AISTUDIO_STATUS='skip'
    AISTUDIO_REASON='GEMINI_API_KEY not set...'
    RECOMMENDED='vertex'

Exit code mirrors :mod:`probe-gemini-voice`'s — always 0.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


def _shell_quote(value: str) -> str:
    """Single-quote ``value`` for safe POSIX shell eval."""
    return "'" + value.replace("'", "'\\''") + "'"


def main() -> int:
    # Load the sibling probe module without depending on package layout.
    here = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location(
        "probe_gemini_voice", here / "probe-gemini-voice.py",
    )
    if spec is None or spec.loader is None:
        sys.stderr.write("probe-gemini-voice.py not found next to this script\n")
        return 0
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    mod._load_context_env()
    import asyncio
    result = asyncio.run(mod.probe())

    v = result.get("vertex", {})
    a = result.get("aistudio", {})
    print(f"VERTEX_STATUS={_shell_quote(v.get('status', ''))}")
    print(f"VERTEX_REASON={_shell_quote(v.get('reason', ''))}")
    print(f"VERTEX_MODEL={_shell_quote(v.get('model') or '')}")
    print(f"AISTUDIO_STATUS={_shell_quote(a.get('status', ''))}")
    print(f"AISTUDIO_REASON={_shell_quote(a.get('reason', ''))}")
    print(f"AISTUDIO_MODEL={_shell_quote(a.get('model') or '')}")
    print(f"RECOMMENDED={_shell_quote(result.get('recommended_default') or '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
