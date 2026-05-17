"""File tools — read and write files anywhere on the host.

Absolute paths (and `~`) resolve as-is; relative paths resolve against the
project directory.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from orchestrator.tools import registry

logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 100_000  # 100KB max read size


def _resolve_path(base_dir: str, path: str) -> Path:
    """Resolve a path. Absolute paths and `~` are honored verbatim; relative
    paths resolve against `base_dir` (the project directory)."""
    expanded = Path(path).expanduser()
    if expanded.is_absolute():
        return expanded.resolve()
    return (Path(base_dir) / expanded).resolve()


@registry.register(
    name="read_file",
    description="Read a file. Absolute paths read from anywhere on the host; relative paths resolve against the project directory.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path (e.g. '/etc/hosts', '~/notes.txt') or path relative to the project root (e.g. 'CLAUDE.md', 'context/memory/MEMORY.md').",
            },
        },
        "required": ["path"],
    },
)
async def read_file(context: dict[str, Any], path: str) -> str:
    project_dir = context.get("project_dir", "")
    if not project_dir:
        return json.dumps({"error": "Project directory not configured"})

    target = _resolve_path(project_dir, path)

    if not target.is_file():
        return json.dumps({"error": f"File not found: {path}"})

    try:
        content = target.read_text(encoding="utf-8")
        if len(content) > MAX_FILE_SIZE:
            content = content[:MAX_FILE_SIZE] + f"\n... (truncated at {MAX_FILE_SIZE} bytes)"
        return json.dumps({"path": str(target), "content": content})
    except Exception as e:
        return json.dumps({"error": f"Failed to read file: {e}"})


@registry.register(
    name="write_file",
    description="Write content to a file. Absolute paths write anywhere on the host; relative paths resolve against the project directory. Creates parent directories if needed.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path or path relative to the project root.",
            },
            "content": {
                "type": "string",
                "description": "The content to write to the file.",
            },
        },
        "required": ["path", "content"],
    },
)
async def write_file(context: dict[str, Any], path: str, content: str) -> str:
    project_dir = context.get("project_dir", "")
    if not project_dir:
        return json.dumps({"error": "Project directory not configured"})

    target = _resolve_path(project_dir, path)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return json.dumps({"path": str(target), "status": "written", "bytes": len(content)})
    except Exception as e:
        return json.dumps({"error": f"Failed to write file: {e}"})
