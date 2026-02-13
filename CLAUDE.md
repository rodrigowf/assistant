# Personal Assistant — Project Instructions

This is a self-contained Claude Code environment. Everything runs within this folder: the wrapper application (api/manager/frontend), skills, agents, scripts, and memories. You can modify any of these components — including the wrapper you're running inside.

## Architecture

```
assistant/
├── .claude_config/  # Claude Code config, sessions & memory (gitignored)
├── index/           # Vector search index (gitignored)
├── skills/          # Skill definitions — you can create and modify these
├── agents/          # Agent definitions — you can create and modify these
├── scripts/         # Shared executables — you can create and modify these
├── manager/         # Python wrapper for Claude Code SDK
├── api/             # FastAPI server (REST + WebSocket)
├── frontend/        # React UI for chat interface
├── tests/           # Test suite
└── .venv/           # Python virtual environment
```

`CLAUDE_CONFIG_DIR` is set to `.claude_config/` by `scripts/run.sh`, keeping all Claude Code data local.

## The Wrapper Application

The wrapper (api + manager + frontend) provides a web interface for interacting with Claude Code. When running inside the wrapper, you can edit its own code — the manager, API routes, frontend components — and those changes affect the very application you're running in.

Start the backend:
```
scripts/run.sh -m uvicorn api.app:create_app --factory --port 8000
```

Start the frontend:
```
cd frontend && npm run dev
```

Or use `/debug-app` which handles both and provides browser automation.

## Memory & History

Your auto-memory lives at `.claude_config/projects/-home-rodrigo-Projects-assistant/memory/`. Use it normally — write patterns, preferences, and insights to MEMORY.md as you work.

Both are indexed automatically by the API server for semantic search via `/recall <query>`:
- **Memory**: Indexed immediately when files change (file watcher)
- **History**: Indexed every 2 minutes (if files changed)

## Self-Modification

You can extend and modify your own capabilities:

- **Skills** (`skills/`): Create with `/scaffold-skill`, modify existing ones directly
- **Agents** (`agents/`): Create with `/scaffold-agent` for specialized subagents
- **Scripts** (`scripts/`): Shared tools any skill can reference
- **Wrapper** (`api/`, `manager/`, `frontend/`): The application code itself

Run Python scripts through the venv: `scripts/run.sh scripts/<script>.py [args]`

## Writing Skills

- Format: YAML frontmatter + markdown instructions
- Never use literal backtick command syntax in SKILL.md (triggers permission prompts)
- Variable substitution: `$ARGUMENTS`, `$0`/`$1`/`$2`, `${CLAUDE_SESSION_ID}`

## Testing

Run the full suite: `scripts/run.sh -m pytest tests/ -v`

Write tests alongside code. Mock external dependencies with `unittest.mock`.

## Browser Automation

Chrome DevTools MCP provides full browser control. Use `/debug-app` for integrated frontend testing.

## Compact Instructions

When compacting, preserve:
- Current task context and progress
- Key decisions made during this session
- Key learnings from this session
- Any unresolved questions or blockers
