"""File tools — read and write files within the project directory."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from orchestrator.tools import registry

logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 100_000  # 100KB max read size


def _resolve_safe_path(base_dir: str, relative_path: str) -> Path | None:
    """Resolve a path safely within the project directory.

    Returns None if the path escapes the project directory.
    """
    base = Path(base_dir).resolve()
    target = (base / relative_path).resolve()
    if not str(target).startswith(str(base)):
        return None
    return target


@registry.register(
    name="read_file",
    description="Read a file from the project directory. Path is relative to project root.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the file (e.g., 'CLAUDE.md' or '.claude_config/projects/.../memory/ORCHESTRATOR_MEMORY.md').",
            },
        },
        "required": ["path"],
    },
)
async def read_file(context: dict[str, Any], path: str) -> str:
    project_dir = context.get("project_dir", "")
    if not project_dir:
        return json.dumps({"error": "Project directory not configured"})

    target = _resolve_safe_path(project_dir, path)
    if target is None:
        return json.dumps({"error": "Path escapes project directory"})

    if not target.is_file():
        return json.dumps({"error": f"File not found: {path}"})

    try:
        content = target.read_text(encoding="utf-8")
        if len(content) > MAX_FILE_SIZE:
            content = content[:MAX_FILE_SIZE] + f"\n... (truncated at {MAX_FILE_SIZE} bytes)"
        return json.dumps({"path": path, "content": content})
    except Exception as e:
        return json.dumps({"error": f"Failed to read file: {e}"})


@registry.register(
    name="write_file",
    description="Write content to a file in the project directory. Creates parent directories if needed.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the file.",
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

    target = _resolve_safe_path(project_dir, path)
    if target is None:
        return json.dumps({"error": "Path escapes project directory"})

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return json.dumps({"path": path, "status": "written", "bytes": len(content)})
    except Exception as e:
        return json.dumps({"error": f"Failed to write file: {e}"})
