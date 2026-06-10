"""System prompt builder for the orchestrator agent."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from orchestrator.config import OrchestratorConfig
from utils.mcp_config import load_available_mcps

# Limits for content injection
MAX_MEMORY_CHARS = 12000
MAX_MEMORY_INDEX_CHARS = 40000

# Shared memory index filename
MEMORY_INDEX_FILENAME = "MEMORY.md"


# ---------------------------------------------------------------------------
# MCP Configuration Loading
# ---------------------------------------------------------------------------

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
            truncated = content[:MAX_MEMORY_INDEX_CHARS]
            shown_lines = truncated.count("\n") + (0 if truncated.endswith("\n") else 1)
            total_lines = content.count("\n") + (0 if content.endswith("\n") else 1)
            # End cleanly on the last fully-shown line, then append the marker.
            last_newline = truncated.rfind("\n")
            if last_newline != -1:
                truncated = truncated[: last_newline + 1]
                shown_lines = truncated.count("\n")
            next_line = shown_lines + 1
            content = (
                truncated
                + f"\n[truncated — showing {shown_lines} of {total_lines} lines — "
                f"read MEMORY.md starting at line {next_line} if you want to see the rest]\n"
            )
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


def _provider_memory_path(
    config: OrchestratorConfig, provider_id: str
) -> Path | None:
    """Return the path to a provider-specific memory file, or None if missing.

    Looked up as `ORCHESTRATOR_MEMORY_<provider_id>.md` next to the main
    orchestrator memory file. Used only in realtime voice sessions.
    """
    if not provider_id or not config.memory_path:
        return None
    base = Path(config.memory_path)
    candidate = base.parent / f"{base.stem}_{provider_id}.md"
    return candidate if candidate.is_file() else None


def _load_provider_memory(
    config: OrchestratorConfig, provider_id: str
) -> tuple[str, Path | None]:
    """Read a provider-specific memory file. Returns (contents, path) or ("", None)."""
    path = _provider_memory_path(config, provider_id)
    if path is None:
        return "", None
    try:
        raw = path.read_text(encoding="utf-8")
        if len(raw) > MAX_MEMORY_CHARS:
            raw = raw[:MAX_MEMORY_CHARS] + "\n... (truncated)"
        return raw, path
    except Exception:
        return "(failed to read provider memory file)", path


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


def _self_reference_section(context: dict[str, Any]) -> str | None:
    """Expose the orchestrator's own conversation JSONL path.

    Lets the orchestrator delegate a digest of this conversation to an agent
    session by handing over an exact file path, instead of describing the
    conversation and asking the agent to find it.
    """
    session = context.get("session")
    jsonl_path = getattr(session, "_jsonl_path", None) if session is not None else None
    if jsonl_path is None:
        return None
    return (
        "## This Conversation\n"
        f"Your conversation JSONL: `{jsonl_path}`\n\n"
        "Pass this path to an agent session when delegating a digest of this "
        "conversation into memory. The append stream is owned by the live "
        "session — treat the file as read-only from any delegated agent."
    )


def _active_sessions_section(context: dict[str, Any]) -> str:
    """Build the active sessions status section from the live pool.

    Includes per-session title (from JSONL store), in-flight background
    turns dispatched via send_to_agent_session, and pending permissions
    awaiting an answer.  Lets the orchestrator's LLM see at a glance
    which agents are busy and whether any need its attention.
    """
    pool = context.get("pool")
    if pool is None:
        return "## Active Agent Sessions\nNo agent sessions are currently active."

    sessions = pool.list_sessions()
    runner = context.get("runner")
    notifications = context.get("notifications")

    if not sessions:
        if notifications and notifications.has_pending():
            n = notifications.pending_count()
            return (
                "## Active Agent Sessions\nNo agent sessions are currently active.\n"
                f"\n_({n} background event{'s' if n != 1 else ''} pending — drained at "
                "the start of your next turn.)_"
            )
        return "## Active Agent Sessions\nNo agent sessions are currently active."

    # Group in-flight runner handles by session_id for quick lookup.
    in_flight: dict[str, list[Any]] = {}
    if runner is not None:
        for h in runner.list_in_flight():
            in_flight.setdefault(h.session_id, []).append(h)

    store = context.get("store")
    lines = ["## Active Agent Sessions"]
    for s in sessions:
        sid = s["session_id"]
        status = s.get("status", "unknown")
        turns = s.get("turns", 0)
        cost = s.get("cost", 0.0)
        sdk_id = s.get("sdk_session_id", "")

        # Title lookup (best-effort) — agents that just opened may not have a
        # JSONL entry yet, in which case we just omit it.
        title: str | None = None
        if store is not None and sdk_id:
            try:
                info = store.get_session_info(sdk_id)
                if info is not None:
                    title = info.title
            except Exception:  # noqa: BLE001
                pass

        title_part = f' ("{title}")' if title else ""
        sdk_note = f", sdk_id={sdk_id}" if sdk_id else ""
        lines.append(
            f"- `{sid}`{title_part}: status={status}, turns={turns}, "
            f"cost=${cost:.4f}{sdk_note}"
        )

        # Per-session detail: in-flight fire-and-forget turns + pending perms
        for h in in_flight.get(sid, []):
            elapsed = f"{h.elapsed_seconds:.1f}s"
            lines.append(
                f"    in-flight: turn {h.turn_id[:8]} "
                f"running for {elapsed} (status={h.status})"
            )
            if h.pending_permission_ids:
                rids = ", ".join(rid[:8] for rid in h.pending_permission_ids)
                lines.append(f"        pending permission(s): {rids}")
        # Pending permissions on the SessionManager itself (independent of
        # an in-flight runner turn — could be triggered from the user's tab).
        sm = pool.get(sid)
        if sm is not None and hasattr(sm, "pending_permission_ids"):
            try:
                rids = list(sm.pending_permission_ids())
            except Exception:  # noqa: BLE001
                rids = []
            if rids and not in_flight.get(sid):
                lines.append(
                    "    pending permission(s) (not from a runner turn): "
                    + ", ".join(rid[:8] for rid in rids)
                )

    if notifications and notifications.has_pending():
        n = notifications.pending_count()
        lines.append(
            f"\n_({n} background event{'s' if n != 1 else ''} pending — drained at "
            "the start of your next turn.)_"
        )
    return "\n".join(lines)


def _mcp_section() -> str:
    """Build the MCP orchestration section with dynamically loaded server info.

    Renders the live list of MCP servers available in this project. When the
    list is empty, the section still appears (so the model knows MCP support
    *exists* but there's nothing to load) — without this, the open_agent_session
    tool's hardcoded examples used to lead the model to invent server names.
    """
    available_mcps = load_available_mcps()

    lines = [
        "## MCP Orchestration",
        "",
        "MCP (Model Context Protocol) servers extend agent capabilities by connecting to external tools and services.",
        "You can configure which MCPs are loaded when opening agent sessions.",
        "",
        "### Available MCPs",
        "",
    ]

    if available_mcps:
        for name in sorted(available_mcps.keys()):
            description = _get_mcp_description(name, available_mcps[name])
            lines.append(f"- **{name}**: {description}")
        usage_examples = [
            f"- `mcp_servers=['{name}']` — load only `{name}`"
            for name in sorted(available_mcps.keys())[:2]
        ]
    else:
        lines.append("- _(none configured for this project)_")
        usage_examples = []

    lines.extend([
        "",
        "### Usage",
        "",
        "When calling `open_agent_session`, pass the `mcp_servers` parameter with a list of MCP names:",
        *usage_examples,
        "- Omit parameter or pass `[]` — Default Claude Code tools only",
        "",
        "**Only pass names from the list above.** Inventing names will fail the call.",
        "Load only the MCPs needed for each task to minimize resource usage.",
    ])

    return "\n".join(lines)


def _memory_section(
    config: OrchestratorConfig,
    voice_provider_id: str | None = None,
) -> str:
    """Build the memory system section explaining both shared and private memory.

    When ``voice_provider_id`` is set (only in realtime voice sessions) and a
    matching `ORCHESTRATOR_MEMORY_<provider>.md` file exists, its contents are
    appended as a separate "Provider-Specific Memory" subsection along with
    short editing instructions.
    """
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
    provider_memory, provider_memory_path = (
        _load_provider_memory(config, voice_provider_id)
        if voice_provider_id
        else ("", None)
    )

    section = f"""## Memory System

`context/memory/` is a structured wiki — files live in semantic category folders, carry YAML frontmatter, and link to related files inline. `MEMORY.md` is the index: it documents the current folder ontology, the frontmatter schema, the cross-reference format, and lists every file with its location. Read MEMORY.md to discover what categories exist and what files already cover a topic — it is loaded below.

### Adding or updating memory

1. **Reuse before creating.** Search the existing index for the closest file. If the new info extends or revises an existing topic, edit that file — do not create a new one for every fact.
2. **Pick the category.** If no existing file fits, place the new file under the most specific matching folder from the ontology in MEMORY.md. If no folder fits, create a new subfolder — folders are cheap; misplaced files are expensive. Mirror new folders as new sections in MEMORY.md.
3. **Fill all frontmatter fields.** Set `created` and `modified` to today. Set `source` to the originating session UUID + title when knowable; otherwise `curated (<short reason>)`. Compute `references` from the files you cite.
4. **Cross-link.** Where prose mentions another memory file, link it inline with a relative markdown link `[name.md](../folder/name.md)`. Add the same path to the `references:` array. Then update those other files' `references:` arrays so the link is bidirectional.
5. **Update MEMORY.md** in the same edit pass — add a one-line entry under the correct section header.

### Editing rules

- `write_file` is a full overwrite — read first, edit, then write the complete updated content. It creates parent directories automatically, so writing to `context/memory/<new-folder>/<file>.md` works without a separate mkdir.
- When you change a file's content, bump its `modified` field to today.
- When you move a file to a different category, update its `category` field and grep the memory tree for inbound references to the old path.
- Never omit existing index entries unless they are clearly obsolete.

### Searching memory

- `search_memory` returns chunks enriched with the source file's `frontmatter` — use category and tags to triage before reading the full file.
- `search_history` returns chunks enriched with `session_uuid`, `session_title`, `session_datetime`, and `linked_memories` — follow `linked_memories` to jump from a conversation back to relevant memory files.
- For directed retrieval, prefer direct file lookup via MEMORY.md over semantic search. Search is a supplement.

### Your private memory (`{relative_path}`)

For orchestrator-specific state: active workflows, pending tasks, session notes. Does NOT follow the frontmatter convention."""

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

    # Add voice-provider-specific memory (only loaded in realtime voice sessions
    # when an ORCHESTRATOR_MEMORY_<provider>.md file exists).
    if provider_memory and provider_memory_path is not None:
        provider_rel = str(provider_memory_path)
        if config.project_dir and provider_rel.startswith("/"):
            try:
                provider_rel = str(
                    provider_memory_path.relative_to(Path(config.project_dir))
                )
            except ValueError:
                pass
        section += f"""

---

### Provider-Specific Memory (`{provider_rel}`)

This file is loaded **only** when you are running on the `{voice_provider_id}` realtime voice provider. Use it for guidance, alignment, or context that applies just to this provider — not the general orchestrator behavior. Edit this file (not the main private memory) when feedback or learnings only apply when speaking through `{voice_provider_id}` voice.

```
{provider_memory}
```"""

    return section


def _guidelines_section() -> str:
    """Build the operational guidelines section."""
    return """## Guidelines

### Before Starting Work
- **Search first**: Use `search_memory` and `search_history` before non-trivial tasks — relevant context often exists
- **Check active sessions**: Review what's already running to avoid duplicate work

### Delegating to Agents
- **Be specific**: Give clear, actionable instructions with enough context for independent work
- **Fire-and-forget**: `send_to_agent_session` returns IMMEDIATELY with a turn_id; the agent runs in the background. Do NOT loop calling it waiting for a response — results arrive as background events on your next turn.
- **In parallel**: While a turn is in flight you can spawn other agents, check progress and read output with `read_agent_session` (returns persisted messages plus a `live` block with `status: running/idle` and the live event tail when running), respond to permission requests (`respond_to_agent_permission`), or talk to the user.
- **Match MCPs to tasks**: Load only the MCPs an agent needs

### Background Events
- After every fire-and-forget turn, you'll receive a structured `[SESSION xxx event: turn <id> <status>, ...]` line (succeeded / failed / cancelled / timeout). Status only — call `read_agent_session(session_id)` for the actual content; it works both while a turn is still running (live tail in `live.events`) and after it finishes (persisted messages).
- Permission events also arrive as structured lines: `[SESSION xxx event: <user|orchestrator> <approved|denied> <ToolName> — "<message>"]`. Typically the user answered in their tab; only call `respond_to_agent_permission` when they're unavailable, the agent has explicitly asked you to decide, or you have a specific reason to overrule.
- Agents announce intent BEFORE calling gated tools (per their system prompt). When you see one of those announcements in `read_agent_session`'s `live.events` while a turn is running, you can respond via the agent's chat — the user's chat reply also auto-denies the pending popup with their prose as the rejection reason, prompting the agent to refine.

### Session Management
- **Open sessions only when needed**: Don't open sessions speculatively
- **Close sessions when done**: Free resources after tasks complete
- **Report progress**: Keep the user informed of status and results
- **Use the returned session_id immediately**: When `open_agent_session` returns `{"session_id": "<id>", "status": "started"}`, the very next `send_to_agent_session` call for that work MUST pass that exact `<id>`. Do not reuse an older session_id from earlier in the conversation — that silently delivers the work to the wrong agent and the user sees the original task respond, not the new one.
- **Check tool results**: When a tool returns `{"error": "..."}`, treat it as a failure even if the error sounds recoverable. Do not narrate "I've opened a new session" if `open_agent_session` errored — tell the user what failed and pick a valid input (e.g. an MCP from the Available MCPs list) before retrying.

### Session Configuration
Sessions inherit their settings from `assistant_config.json` — the same file the Config page in the UI edits. Every `open_agent_session` call resolves working directory (local path or SSH target), session harness (`claude` / `qwen`), harness model, chrome flag, and enabled MCPs from that file at the moment of the call. The orchestrator does NOT carry a separate config: editing the file via the UI or the tools below changes what the next spawned session sees, immediately.

- **Before spawning with non-default settings**: call `get_assistant_config` to inspect the current values. Each `open_agent_session` response also echoes the `resolved_config` it actually used, so you can verify after the fact.
- **To change settings**: call `update_assistant_config` with only the fields you want to change. Same validation as the Config page (working-directory ids must exist in `working_directory_history`, harness must be registered, etc.). Changes take effect on the next `open_agent_session`.
- **For one-off MCP overrides**: pass `mcp_servers` to `open_agent_session` directly — that replaces the inherited list for that session only, without touching the global config.
- **Confirm with the user first** before changing global config in ways that persist across sessions (switching working directory, switching provider, enabling chrome). The user owns the Config page; surprise edits will confuse them.

### Memory Maintenance
- **Update the shared index** when you or agents modify skills or create memory files
- **Remind agents** to report back when their work affects the index
- **Verify writes** — after updating any memory file, confirm nothing was accidentally omitted"""


def _format_message(msg: dict[str, Any]) -> str | None:
    """Render a single Anthropic-format message as a Markdown line.

    Returns None for empty messages that shouldn't appear in the transcript.
    """
    role = msg.get("role", "?")
    label = "User" if role == "user" else "Assistant"
    content = msg.get("content", "")

    if isinstance(content, str):
        text = content.strip()
        return f"**{label}:** {text}" if text else None

    if not isinstance(content, list):
        return None

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
            # Tool-result payloads have already been clipped upstream by
            # truncate_tool_results(); pass them through as-is.
            parts.append(f"[tool result: {result_content}]")

    return f"**{label}:** {' '.join(parts)}" if parts else None


def _history_section(
    recent_messages: list[dict[str, Any]] | None,
    summary: str | None = None,
) -> str | None:
    """Format recent conversation history for injection into the system prompt.

    ``recent_messages`` is the already-budgeted list of verbatim messages
    (tool results pre-clipped). ``summary`` covers everything older.

    Returns None when there's nothing to render.
    """
    if not recent_messages and not summary:
        return None

    lines: list[str] = [
        "## Recent Conversation History",
        "(from your previous text/voice conversation in this session)\n",
    ]

    if summary:
        lines.append("### Earlier Conversation Summary")
        lines.append(summary.strip())
        if recent_messages:
            lines.append("\n### Recent Messages")

    for msg in recent_messages or []:
        formatted = _format_message(msg)
        if formatted:
            lines.append(formatted)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main Prompt Builder
# ---------------------------------------------------------------------------

def build_system_prompt(
    config: OrchestratorConfig,
    context: dict[str, Any],
    recent_messages: list[dict[str, Any]] | None = None,
    history_summary: str | None = None,
    voice_provider_id: str | None = None,
) -> str:
    """Build the orchestrator's system prompt.

    Assembles sections in logical order:
    1. Role and identity
    2. Current state (active sessions)
    3. Capabilities (MCP orchestration)
    4. Knowledge (memory system with contents)
    5. Guidelines
    6. Context (recent verbatim messages + summary of older ones)

    The caller is responsible for splitting raw history into
    ``recent_messages`` (kept verbatim, with tool results pre-clipped) and
    ``history_summary`` (digest of older messages) using the token-budget
    helpers in ``orchestrator.token_budget``.

    ``voice_provider_id`` is only passed by realtime voice sessions; when set
    and a matching ``ORCHESTRATOR_MEMORY_<provider>.md`` file exists next to
    the main orchestrator memory, its contents are appended to the memory
    section. Text and audio modes never pass this argument, so provider-
    specific memory never leaks into non-voice prompts.
    """
    sections = [
        _role_section(),
        _self_reference_section(context),
        _active_sessions_section(context),
        _mcp_section(),
        _memory_section(config, voice_provider_id=voice_provider_id),
        _guidelines_section(),
        _history_section(recent_messages, history_summary),
    ]
    return "\n\n".join(s for s in sections if s)
