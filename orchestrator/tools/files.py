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
    description=(
        "Read a file. Absolute paths read from anywhere on the host; relative "
        "paths resolve against the project directory. Optionally pass "
        "start_line/end_line (1-indexed, inclusive) to read just a slice — "
        "useful for large files. When the response gets truncated by the "
        "byte limit you'll see a marker like '[truncated at line X of Y total "
        "— call read_file again with start_line=X+1 to continue]' so you can "
        "page through the rest."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path (e.g. '/etc/hosts', '~/notes.txt') or path relative to the project root (e.g. 'CLAUDE.md', 'context/memory/MEMORY.md').",
            },
            "start_line": {
                "type": "integer",
                "minimum": 1,
                "description": "1-indexed first line to return (inclusive). Default: 1 (start of file).",
            },
            "end_line": {
                "type": "integer",
                "minimum": 1,
                "description": "1-indexed last line to return (inclusive). Default: end of file. Clamped to total line count.",
            },
        },
        "required": ["path"],
    },
)
async def read_file(
    context: dict[str, Any],
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    project_dir = context.get("project_dir", "")
    if not project_dir:
        return json.dumps({"error": "Project directory not configured"})

    target = _resolve_path(project_dir, path)

    if not target.is_file():
        return json.dumps({"error": f"File not found: {path}"})

    try:
        raw = target.read_text(encoding="utf-8")
    except Exception as e:
        return json.dumps({"error": f"Failed to read file: {e}"})

    lines = raw.splitlines(keepends=True)
    total_lines = len(lines)

    # Normalise the range. start_line defaults to 1; end_line defaults to EOF.
    # Both are clamped to [1, total_lines]; an inverted range yields empty.
    s = 1 if start_line is None else max(1, start_line)
    e = total_lines if end_line is None else min(total_lines, max(1, end_line))

    if total_lines == 0:
        # Empty file — preserve original behaviour (return empty content).
        return json.dumps({
            "path": str(target),
            "content": "",
            "start_line": 1,
            "end_line": 0,
            "total_lines": 0,
        })

    if s > total_lines:
        return json.dumps({
            "error": (
                f"start_line {s} is past end of file "
                f"(total {total_lines} lines)"
            ),
            "path": str(target),
            "total_lines": total_lines,
        })

    sliced = "".join(lines[s - 1:e])
    returned_end = e

    # Byte-size truncation — count whole lines so the marker can name a
    # line number the model can resume from.
    if len(sliced) > MAX_FILE_SIZE:
        kept: list[str] = []
        running = 0
        last_idx = s - 1
        for idx in range(s - 1, e):
            chunk = lines[idx]
            if running + len(chunk) > MAX_FILE_SIZE and kept:
                break
            kept.append(chunk)
            running += len(chunk)
            last_idx = idx
        sliced = "".join(kept)
        returned_end = last_idx + 1
        next_line = returned_end + 1
        marker = (
            f"\n... [truncated at line {returned_end} of {total_lines} total "
            f"— call read_file again with start_line={next_line} to continue]"
        )
        sliced += marker

    return json.dumps({
        "path": str(target),
        "content": sliced,
        "start_line": s,
        "end_line": returned_end,
        "total_lines": total_lines,
    })


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
