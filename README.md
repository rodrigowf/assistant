# Personal Assistant

**A transparent, hackable AI assistant that evolves with you.**

An open-source personal AI assistant built on Claude Code. It can automate workflows, remember context across sessions, and modify its own capabilities—all running locally on your machine with code you can read and understand.

## What Makes This Different

**Fully transparent.** The entire system is ~1000 lines of Python and a simple React frontend. No magic, no hidden complexity. You can read every line of code that touches your files, executes commands, or stores your data.

**Developer-native.** This isn't a chatbot bolted onto a messaging app. It's a proper development environment with a web interface, designed for people who think in code and want to extend their tools.

**Self-improving.** The assistant can create new skills, modify existing ones, and even edit its own wrapper code. Teach it something once, and it can turn that into a reusable automation.

**Local-first.** Your conversations, memory, and credentials stay on your machine. No cloud sync, no third-party platforms, no account required beyond your Anthropic API access.

## What It Can Do

- **Chat** through a clean web interface with real-time streaming
- **Execute** code, manage files, run shell commands—full Claude Code capabilities
- **Remember** context across sessions with searchable conversation history
- **Automate** workflows through custom skills (slash commands)
- **Evolve** by creating new skills and modifying its own behavior

## Quick Start

### Prerequisites

- Python 3.12+
- Node.js 20+
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI

Check if you have everything:

```bash
scripts/install-prerequisites.sh
```

### Installation

```bash
git clone https://github.com/yourusername/assistant.git
cd assistant
./install.sh
```

That's it. The script sets up the Python environment, installs dependencies, and configures the frontend.

### Run

```bash
# Terminal 1 — Backend
scripts/run.sh -m uvicorn api.app:create_app --factory --port 8000

# Terminal 2 — Frontend
cd frontend && npm run dev
```

Open **http://localhost:5173** and start chatting.

### Manual Installation

If you prefer to install manually:

```bash
# Python environment
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Frontend
cd frontend && npm install
```

## Architecture

```
assistant/
├── api/             # FastAPI backend (~300 lines)
├── manager/         # Claude SDK wrapper (~400 lines)
├── frontend/        # React chat interface
├── skills/          # Custom slash commands
├── agents/          # Specialized agent definitions
├── scripts/         # Shared automation scripts
└── .claude_config/  # Local data (sessions, memory)
```

The backend wraps the Claude Agent SDK, managing sessions and streaming responses over WebSocket. Background workers keep your memory and conversation history indexed for semantic search.

## Skills: Extensible Automation

Skills are markdown files that define slash commands:

```yaml
# skills/standup/SKILL.md
---
name: standup
description: Run my morning routine
---

1. Check calendar for today's meetings
2. Summarize unread Slack messages
3. List PRs waiting for my review
```

Type `/standup` and it runs. The assistant can also create skills for you—just ask it to "turn this into a skill" after showing it a workflow.

**Built-in skills:**

| Command | Purpose |
|---------|---------|
| `/recall <query>` | Search memory and past conversations |
| `/scaffold-skill` | Create a new skill |
| `/scaffold-agent` | Define a specialized agent |
| `/debug-app` | Debug this application |

## Memory

The system maintains searchable memory:

- **Auto-memory**: Patterns and preferences Claude learns over time
- **History**: All past conversations, indexed for semantic search

Both are indexed automatically in the background. Use `/recall` to search explicitly, or the assistant searches when relevant.

## Configuration

The installer creates `.manager.json` with sensible defaults. You can customize it:

```json
{
  "model": "claude-sonnet-4-20250514",
  "permission_mode": "default",
  "max_budget_usd": 10.0,
  "max_turns": 50
}
```

Or use environment variables (see `.env.example`).

## Philosophy

This project prioritizes **transparency over polish**. The codebase is intentionally simple—you should be able to understand how it works in an afternoon.

The assistant can modify itself: fix bugs, add features, improve skills. You're building a tool that grows with you, not adapting to someone else's vision of what an AI assistant should be.

## Development

```bash
# Install with dev dependencies
./install.sh --dev

# Run tests
scripts/run.sh -m pytest tests/ -v

# Lint and type check
.venv/bin/ruff check .
.venv/bin/mypy api manager
```

## License

MIT
