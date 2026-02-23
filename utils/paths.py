"""
Centralized path resolution for context files.

All internal code uses these functions. No legacy path logic elsewhere.
The symlink at .claude_config/projects/<mangled> exists only for Claude SDK.

Structure:
    context/
    ├── *.jsonl          # Session files (SDK writes here directly)
    ├── <uuid>/          # SDK state directories (subagents, tool-results)
    ├── .titles.json     # Custom session titles
    └── memory/          # Memory files (Markdown)
"""
from pathlib import Path

# Resolve project root (works from any file in the project)
PROJECT_ROOT = Path(__file__).parent.parent.resolve()


def get_project_dir() -> Path:
    """Get the project root directory."""
    return PROJECT_ROOT


def get_context_dir() -> Path:
    """Get the context directory (contains sessions, memory, SDK state)."""
    return PROJECT_ROOT / "context"


def get_memory_dir() -> Path:
    """Get the memory directory."""
    return get_context_dir() / "memory"


def get_sessions_dir() -> Path:
    """Get the sessions directory (JSONL files live at context/ root)."""
    return get_context_dir()


def get_index_dir() -> Path:
    """Get the vector index directory."""
    return PROJECT_ROOT / "index"


def get_session_path(session_id: str) -> Path:
    """Get the path for a specific session JSONL file."""
    return get_context_dir() / f"{session_id}.jsonl"


def get_titles_path() -> Path:
    """Get the path for the session titles file."""
    return get_context_dir() / ".titles.json"


def ensure_context_dirs() -> None:
    """Ensure all context directories exist."""
    get_context_dir().mkdir(parents=True, exist_ok=True)
    get_memory_dir().mkdir(parents=True, exist_ok=True)
    get_index_dir().mkdir(parents=True, exist_ok=True)
