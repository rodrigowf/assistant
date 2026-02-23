"""System prompt builder for the orchestrator agent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestrator.config import OrchestratorConfig

MAX_MEMORY_CHARS = 12000
MAX_HISTORY_MESSAGES = 20
MAX_HISTORY_CHARS = 6000


def build_system_prompt(
    config: OrchestratorConfig,
    context: dict[str, Any],
    history: list[dict[str, Any]] | None = None,
    history_summary: str | None = None,
) -> str:
    """Build the orchestrator's system prompt.

    Assembles role description, active session state, memory contents,
    usage guidelines, and optionally recent conversation history (for voice
    sessions that need prior context injected into the system prompt).
    """
    sections = [
        _role_section(),
        _active_sessions_section(context),
        _memory_section(config),
        _guidelines_section(config),
        _history_section(history, history_summary) if history else None,
    ]
    return "\n\n".join(s for s in sections if s)


def _role_section() -> str:
    return """You are an orchestrator agent that coordinates multiple Claude Code instances.

You can open, monitor, and communicate with Claude Code agent sessions to accomplish complex tasks.
You have access to the project's conversation history and memory via search tools, and can read/write files in the project directory.

## UI Context

The user interacts with you through a multi-tab web interface. Each agent session you open appears as a **tab** in their browser — the user may say "tab" to refer to an open agent session. Opening a session creates a new tab; closing one removes that tab. The user can click a session in the sidebar to switch to its tab, or close tabs directly from the browser UI.

## Your Job

- Understand user requests and break them into tasks for agent sessions
- Open Claude Code sessions and delegate work to them
- Monitor their progress and collect results
- Coordinate multi-step workflows across sessions
- Maintain your own persistent memory for cross-session context"""


def _active_sessions_section(context: dict[str, Any]) -> str:
    orch_sessions = context.get("orchestrator_sessions", {})
    if not orch_sessions:
        return "## Active Agent Sessions\nNo agent sessions are currently active."

    lines = ["## Active Agent Sessions"]
    for sid, sm in orch_sessions.items():
        lines.append(f"- `{sid}`: status={sm.status.value}, turns={sm.turns}, cost=${sm.cost:.4f}")
    return "\n".join(lines)


def _memory_section(config: OrchestratorConfig) -> str:
    memory_path = Path(config.memory_path)
    memory_content = ""
    if memory_path.is_file():
        try:
            raw = memory_path.read_text(encoding="utf-8")
            if len(raw) > MAX_MEMORY_CHARS:
                raw = raw[:MAX_MEMORY_CHARS] + "\n... (truncated)"
            memory_content = raw
        except Exception:
            memory_content = "(failed to read memory file)"

    relative_path = config.memory_path
    # Try to make it relative to project_dir for the prompt
    if config.project_dir and config.memory_path.startswith("/"):
        try:
            relative_path = str(Path(config.memory_path).relative_to(Path(config.project_dir)))
        except ValueError:
            pass

    section = f"""## Orchestrator Memory

### Primary memory file
Your persistent memory index is at `{relative_path}`.
Use `read_file` and `write_file` to read and update it.

**Important — `write_file` is a full overwrite.** There is no append operation.
When updating this file, always read it first, then write the complete new content.
Never omit existing entries unless they are clearly stale or superseded.
Keep this file concise (under ~150 lines) — move detailed context to separate files.

### Extended memory files
For detailed notes that would bloat the index file, write separate files in the same memory directory:
`{relative_path.replace("ORCHESTRATOR_MEMORY.md", "<topic>.md")}`

These files are automatically indexed for vector search. You can retrieve them with `search_memory`.
Use this for: detailed plans, architectural decisions, per-project context, research results.

### Retrieving memories
Use `search_memory` to find relevant context from **all** memory files (yours and the agents').
Use `search_history` to find relevant context from past conversation turns.
Always search before starting non-trivial work — relevant context may already exist."""

    if memory_content:
        section += f"\n\n### Current Memory Contents\n```\n{memory_content}\n```"
    else:
        section += "\n\nThe memory file is currently empty. Create it when you have something worth remembering."

    return section


def _guidelines_section(config: OrchestratorConfig) -> str:
    return """## Guidelines

- **Search first**: Before starting any non-trivial task, use `search_memory` and `search_history` — relevant context from past sessions is often already there
- **Be efficient**: Open sessions only when needed, close them when done
- **Be informative**: Report progress and results back to the user
- **Delegate clearly**: Give agent sessions specific, actionable instructions with enough context to work independently
- **Track state**: Update your memory file when you learn something worth keeping across sessions
- **One thing at a time**: Wait for an agent's response before sending the next message"""


def _history_section(
    history: list[dict[str, Any]] | None,
    summary: str | None = None,
) -> str | None:
    """Format recent conversation history for injection into the system prompt.

    Used in both text and voice modes to provide conversation context. This is
    especially important for:
    - Resuming sessions with prior conversation history
    - Switching between voice and text modes
    - Long conversations where early context aids coherence

    If a summary is provided (for histories longer than MAX_HISTORY_MESSAGES),
    it is rendered first as an "Earlier conversation" block, followed by the
    last MAX_HISTORY_MESSAGES messages verbatim. Tool call content blocks are
    summarized rather than dumped verbatim. Total output is capped at
    MAX_HISTORY_CHARS.
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
            # Anthropic content block list — extract text blocks; summarize tool blocks
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
                    # Summarize — full output can be huge
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

    # Hard cap on total characters
    if len(section) > MAX_HISTORY_CHARS:
        section = section[:MAX_HISTORY_CHARS] + "\n... (history truncated)"

    return section
