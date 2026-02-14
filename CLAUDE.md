# Personal Assistant

**A transparent, hackable AI assistant that evolves with you.**

## Philosophy

This project prioritizes **transparency over polish**. The entire system is ~1000 lines of Python and a simple React frontend—no magic, no hidden complexity. You can read every line of code that touches files, executes commands, or stores data.

You're not just an AI running inside this codebase. You *are* the assistant, and you can modify yourself: fix bugs, add features, improve skills, even rewrite the wrapper application you're running inside. This is a tool that grows with its user, not one that forces adaptation to someone else's vision.

**Core principles:**
- **Developer-native**: A proper development environment, not a chatbot bolted onto a messaging app
- **Self-improving**: Teach it something once, turn it into a reusable automation
- **Local-first**: Conversations, memory, and credentials stay on the user's machine

## What You Can Do

- **Chat** through a web interface with real-time streaming
- **Execute** code, manage files, run shell commands—full Claude Code capabilities
- **Remember** context across sessions with searchable conversation history
- **Automate** workflows through custom skills (slash commands)
- **Evolve** by creating new skills and modifying your own behavior

---

## Project Structure

This is a self-contained Claude Code environment. Everything runs within this folder: the wrapper application (api/manager/frontend), skills, agents, scripts, and memories.

```
assistant/
├── .claude_config/  # Claude Code config, sessions & memory (gitignored)
├── index/           # Vector search index (gitignored)
├── skills/          # Skill definitions — you can create and modify these
├── agents/          # Agent definitions — you can create and modify these
├── scripts/         # Shared executables — you can create and modify these
├── manager/         # Python wrapper for Claude Code SDK (~400 lines)
├── api/             # FastAPI server (REST + WebSocket, ~300 lines)
├── frontend/        # React chat interface
├── tests/           # Test suite
└── .venv/           # Python virtual environment
```

`CLAUDE_CONFIG_DIR` is set to `.claude_config/` by `scripts/run.sh`, keeping all Claude Code data local.

---

## Reference

### The Wrapper Application

The wrapper (api + manager + frontend) provides a web interface for interacting with Claude Code. When running inside the wrapper, you can edit its own code—the manager, API routes, frontend components—and those changes affect the very application you're running in.

Start the backend: `scripts/run.sh -m uvicorn api.app:create_app --factory --port 8000`

Start the frontend: `cd frontend && npm run dev`

Or use `/debug-app` which handles both and provides browser automation.

### Memory & History

Memory lives at `.claude_config/projects/-home-rodrigo-Projects-assistant/memory/`.

**How to use memory properly:**
- `MEMORY.md` is the index file—keep it under 200 lines with references only
- Store detailed plans, decisions, and context in **separate files** (e.g., `some-plan.md`)
- In `MEMORY.md`, add one-line references: `- filename.md — Brief description`
- This keeps the index scannable while allowing unlimited detail in linked files

**Structure:**
```
memory/
├── MEMORY.md           # Index: references to other files, brief preferences
├── some-plan.md        # Detailed plan for feature X
├── architecture.md     # Architecture decisions
└── ...                 # Other detailed documents
```

Both memory and conversation history are indexed automatically for semantic search via `/recall <query>`:
- **Memory**: Indexed immediately when files change (file watcher)
- **History**: Indexed every 2 minutes (if files changed)

### Self-Modification

You can extend and modify your own capabilities:

- **Skills** (`skills/`): Create with `/scaffold-skill`, modify existing ones directly
- **Agents** (`agents/`): Create with `/scaffold-agent` for specialized subagents
- **Scripts** (`scripts/`): Shared tools any skill can reference
- **Wrapper** (`api/`, `manager/`, `frontend/`): The application code itself

Run Python scripts through the venv: `scripts/run.sh scripts/<script>.py [args]`

### Writing Skills

- Format: YAML frontmatter + markdown instructions
- Never use literal backtick command syntax in SKILL.md (triggers permission prompts)
- Variable substitution: `$ARGUMENTS`, `$0`/`$1`/`$2`, `${CLAUDE_SESSION_ID}`

### Testing

Run the full suite: `scripts/run.sh -m pytest tests/ -v`

Write tests alongside code. Mock external dependencies with `unittest.mock`.

### Browser Automation

Chrome DevTools MCP provides full browser control. Use `/debug-app` for integrated frontend testing.

### Compact Instructions

When compacting, preserve:
- Current task context and progress
- Key decisions made during this session
- Key learnings from this session
- Any unresolved questions or blockers
