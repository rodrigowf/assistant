"""System prompt builder for the orchestrator agent."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from orchestrator.config import OrchestratorConfig

# Limits for content injection
MAX_MEMORY_CHARS = 12000
MAX_MEMORY_INDEX_CHARS = 20000
MAX_HISTORY_MESSAGES = 20
MAX_HISTORY_CHARS = 6000

# Shared memory index filename
MEMORY_INDEX_FILENAME = "MEMORY.md"


# ---------------------------------------------------------------------------
# MCP Configuration Loading
# ---------------------------------------------------------------------------

def _load_available_mcps() -> dict[str, dict[str, Any]]:
    """Load available MCP servers from .claude.json config.

    Returns a dict mapping MCP name to its full configuration.
    """
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        config_path = Path(config_dir) / ".claude.json"
    else:
        project_root = Path(__file__).resolve().parent.parent
        config_path = project_root / ".claude_config" / ".claude.json"

    if not config_path.is_file():
        return {}

    try:
        with open(config_path) as f:
            config = json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}

    # Get project-specific MCP servers
    project_root = Path(__file__).resolve().parent.parent
    project_dir = str(project_root)
    projects = config.get("projects", {})
    project_config = projects.get(project_dir, {})

    return project_config.get("mcpServers", {})


def _load_mcp_descriptions() -> dict[str, str]:
    """Load MCP descriptions from the descriptions config file."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        desc_path = Path(config_dir) / "mcp_descriptions.json"
    else:
        project_root = Path(__file__).resolve().parent.parent
        desc_path = project_root / ".claude_config" / "mcp_descriptions.json"

    if not desc_path.is_file():
        return {}

    try:
        with open(desc_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def _get_mcp_description(name: str, config: dict[str, Any]) -> str:
    """Get a description for an MCP server.

    Checks (in order):
    1. The mcp_descriptions.json file
    2. A 'description' field in the MCP config
    3. Falls back to a generic description from command/type
    """
    # Check descriptions file first
    descriptions = _load_mcp_descriptions()
    if name in descriptions:
        return descriptions[name]

    # Check config for description field
    if "description" in config:
        return config["description"]

    # Generate fallback description from config structure
    cmd = config.get("command", "")
    server_type = config.get("type", "stdio")

    if cmd:
        return f"{server_type} server ({cmd})"
    return f"{server_type} MCP server"


# ---------------------------------------------------------------------------
# Memory Loading
# ---------------------------------------------------------------------------

def _load_memory_index(config: OrchestratorConfig) -> str:
    """Load the shared MEMORY.md index file contents.

    This is the authoritative index of skills, memory files, and project references
    used by both the orchestrator and agent sessions.
    """
    memory_dir = Path(config.memory_path).parent if config.memory_path else None
    if not memory_dir:
        project_root = Path(__file__).resolve().parent.parent
        memory_dir = project_root / "context" / "memory"

    memory_index_path = memory_dir / MEMORY_INDEX_FILENAME

    if not memory_index_path.is_file():
        return ""

    try:
        content = memory_index_path.read_text(encoding="utf-8")
        if len(content) > MAX_MEMORY_INDEX_CHARS:
            content = content[:MAX_MEMORY_INDEX_CHARS] + "\n... (truncated)"
        return content
    except Exception:
        return "(failed to read memory index)"


def _load_private_memory(config: OrchestratorConfig) -> str:
    """Load the orchestrator's private memory file contents."""
    memory_path = Path(config.memory_path)
    if not memory_path.is_file():
        return ""

    try:
        raw = memory_path.read_text(encoding="utf-8")
        if len(raw) > MAX_MEMORY_CHARS:
            raw = raw[:MAX_MEMORY_CHARS] + "\n... (truncated)"
        return raw
    except Exception:
        return "(failed to read memory file)"


# ---------------------------------------------------------------------------
# Prompt Section Builders
# ---------------------------------------------------------------------------

def _role_section() -> str:
    """Build the role and identity section."""
    return """You are an orchestrator agent that coordinates multiple Claude Code instances.

You can open, monitor, and communicate with Claude Code agent sessions to accomplish complex tasks.
You have access to the project's conversation history and memory via search tools, and can read/write files in the project directory.

## UI Context

The user interacts with you through a multi-tab web interface. Each agent session you open appears as a **tab** in their browser — the user may say "tab" to refer to an open agent session. Opening a session creates a new tab; closing one removes that tab. The user can click a session in the sidebar to switch to its tab, or close tabs directly from the browser UI.

## Your Responsibilities

- Understand user requests and break them into tasks for agent sessions
- Open Claude Code sessions and delegate work to them
- Monitor their progress and collect results
- Coordinate multi-step workflows across sessions
- Maintain persistent memory for cross-session context"""


def _active_sessions_section(context: dict[str, Any]) -> str:
    """Build the active sessions status section."""
    orch_sessions = context.get("orchestrator_sessions", {})
    if not orch_sessions:
        return "## Active Agent Sessions\nNo agent sessions are currently active."

    lines = ["## Active Agent Sessions"]
    for sid, sm in orch_sessions.items():
        lines.append(f"- `{sid}`: status={sm.status.value}, turns={sm.turns}, cost=${sm.cost:.4f}")
    return "\n".join(lines)


def _mcp_section() -> str:
    """Build the MCP orchestration section with dynamically loaded server info."""
    available_mcps = _load_available_mcps()

    if not available_mcps:
        return ""

    lines = [
        "## MCP Orchestration",
        "",
        "MCP (Model Context Protocol) servers extend agent capabilities by connecting to external tools and services.",
        "You can configure which MCPs are loaded when opening agent sessions.",
        "",
        "### Available MCPs",
        "",
    ]

    for name in sorted(available_mcps.keys()):
        description = _get_mcp_description(name, available_mcps[name])
        lines.append(f"- **{name}**: {description}")

    lines.extend([
        "",
        "### Usage",
        "",
        "When calling `open_agent_session`, pass the `mcp_servers` parameter with a list of MCP names:",
        "- `mcp_servers=['obs']` — OBS integration only",
        "- `mcp_servers=['chrome-devtools', 'ubuntu-desktop-control']` — Browser + desktop automation",
        "- Omit parameter or pass `[]` — Default Claude Code tools only",
        "",
        "Load only the MCPs needed for each task to minimize resource usage.",
    ])

    return "\n".join(lines)


def _memory_section(config: OrchestratorConfig) -> str:
    """Build the memory system section explaining both shared and private memory."""
    # Get relative path for display
    relative_path = config.memory_path
    if config.project_dir and config.memory_path.startswith("/"):
        try:
            relative_path = str(Path(config.memory_path).relative_to(Path(config.project_dir)))
        except ValueError:
            pass

    # Load memory contents
    memory_index = _load_memory_index(config)
    private_memory = _load_private_memory(config)

    section = f"""## Memory System

The orchestrator uses a two-tier memory system: a **shared index** for reference information, and a **private memory** for orchestrator-specific state.

### Shared Memory Index (`context/memory/MEMORY.md`)

This is the authoritative reference for skills, memory files, and project information. Both you and agent sessions rely on this index.

**Your maintenance responsibilities:**
- Update the Skills Reference table when skills are added, removed, or modified
- Add reference lines for new memory files
- Update project entries when their status changes

### Your Private Memory (`{relative_path}`)

Use this for orchestrator-specific state: active workflows, pending tasks, session notes.

### Extended Memory Files

For detailed notes, create separate files in `context/memory/<topic>.md`. These are automatically indexed for semantic search.

### File Editing Rule

**Critical: `write_file` performs a full overwrite.** Always:
1. Read the file first
2. Make your changes
3. Write the complete updated content

Never omit existing entries unless they are clearly obsolete."""

    # Add shared memory index contents
    if memory_index:
        section += f"""

---

### Current Shared Memory Index

```markdown
{memory_index}
```"""

    # Add private memory contents
    if private_memory:
        section += f"""

---

### Current Private Memory

```
{private_memory}
```"""
    else:
        section += """

---

### Current Private Memory

Your private memory is currently empty."""

    return section


def _guidelines_section() -> str:
    """Build the operational guidelines section."""
    return """## Guidelines

### Before Starting Work
- **Search first**: Use `search_memory` and `search_history` before non-trivial tasks — relevant context often exists
- **Check active sessions**: Review what's already running to avoid duplicate work

### Delegating to Agents
- **Be specific**: Give clear, actionable instructions with enough context for independent work
- **One thing at a time**: Wait for an agent's response before sending the next message
- **Match MCPs to tasks**: Load only the MCPs an agent needs

### Session Management
- **Open sessions only when needed**: Don't open sessions speculatively
- **Close sessions when done**: Free resources after tasks complete
- **Report progress**: Keep the user informed of status and results

### Memory Maintenance
- **Update the shared index** when you or agents modify skills or create memory files
- **Remind agents** to report back when their work affects the index
- **Verify writes** — after updating any memory file, confirm nothing was accidentally omitted"""


def _history_section(
    history: list[dict[str, Any]] | None,
    summary: str | None = None,
) -> str | None:
    """Format recent conversation history for injection into the system prompt.

    Used to provide conversation context when resuming sessions or switching
    between voice and text modes.
    """
    if not history:
        return None

    recent = history[-MAX_HISTORY_MESSAGES:]
    lines: list[str] = ["## Recent Conversation History",
                        "(from your previous text/voice conversation in this session)\n"]

    if summary:
        lines.append("### Earlier Conversation Summary")
        lines.append(summary.strip())
        lines.append("\n### Recent Messages")

    for msg in recent:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        label = "User" if role == "user" else "Assistant"

        if isinstance(content, str):
            lines.append(f"**{label}:** {content.strip()}")
        elif isinstance(content, list):
            # Anthropic content block list — extract text, summarize tool blocks
            parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "text":
                    text = block.get("text", "").strip()
                    if text:
                        parts.append(text)
                elif btype == "tool_use":
                    parts.append(f"[used tool: {block.get('name', '?')}]")
                elif btype == "tool_result":
                    result_content = block.get("content", "")
                    if isinstance(result_content, list):
                        result_content = " ".join(
                            b.get("text", "") for b in result_content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    preview = str(result_content)[:200]
                    if len(str(result_content)) > 200:
                        preview += "..."
                    parts.append(f"[tool result: {preview}]")
            if parts:
                lines.append(f"**{label}:** {' '.join(parts)}")

    section = "\n".join(lines)

    if len(section) > MAX_HISTORY_CHARS:
        section = section[:MAX_HISTORY_CHARS] + "\n... (history truncated)"

    return section


# ---------------------------------------------------------------------------
# Main Prompt Builder
# ---------------------------------------------------------------------------

def build_system_prompt(
    config: OrchestratorConfig,
    context: dict[str, Any],
    history: list[dict[str, Any]] | None = None,
    history_summary: str | None = None,
) -> str:
    """Build the orchestrator's system prompt.

    Assembles sections in logical order:
    1. Role and identity
    2. Current state (active sessions)
    3. Capabilities (MCP orchestration)
    4. Knowledge (memory system with contents)
    5. Guidelines
    6. Context (conversation history, if provided)
    """
    sections = [
        _role_section(),
        _active_sessions_section(context),
        _mcp_section(),
        _memory_section(config),
        _guidelines_section(),
        _history_section(history, history_summary) if history else None,
    ]
    return "\n\n".join(s for s in sections if s)
