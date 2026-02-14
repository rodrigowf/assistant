"""System prompt builder for the orchestrator agent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestrator.config import OrchestratorConfig

MAX_MEMORY_CHARS = 4000


def build_system_prompt(config: OrchestratorConfig, context: dict[str, Any]) -> str:
    """Build the orchestrator's system prompt.

    Assembles role description, active session state, memory contents,
    and usage guidelines.
    """
    sections = [
        _role_section(),
        _active_sessions_section(context),
        _memory_section(config),
        _guidelines_section(config),
    ]
    return "\n\n".join(s for s in sections if s)


def _role_section() -> str:
    return """You are an orchestrator agent that coordinates multiple Claude Code instances.

You can open, monitor, and communicate with Claude Code agent sessions to accomplish complex tasks.
You have access to the project's conversation history and memory via search tools, and can read/write files in the project directory.

Your job is to:
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

Your persistent memory file is at `{relative_path}`.
Use the `read_file` and `write_file` tools to read and update it.
This memory persists across sessions — use it to track important context, decisions, and patterns."""

    if memory_content:
        section += f"\n\n### Current Memory Contents\n```\n{memory_content}\n```"
    else:
        section += "\n\nThe memory file is currently empty. Create it when you have something worth remembering."

    return section


def _guidelines_section(config: OrchestratorConfig) -> str:
    return """## Guidelines

- **Be efficient**: Open sessions only when needed, close them when done
- **Be informative**: Report progress and results back to the user
- **Use search**: Search history and memory before starting new work — context from past sessions is valuable
- **Delegate clearly**: Give agent sessions specific, actionable instructions
- **Track state**: Use your memory file to persist important context across conversations
- **One thing at a time**: Wait for an agent's response before sending the next message"""
