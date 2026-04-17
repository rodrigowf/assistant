# Personal Assistant

**A transparent, hackable AI assistant that evolves with you.**

Most AI assistants are black boxes—frozen binaries that do what they do and nothing more. This one is different: it's ~6,400 lines of readable code that you can understand, modify, and extend. It can chat, execute commands, remember context, coordinate multiple AI agents, and even edit its own source code while running.

The entire system runs locally. Your conversations, memory, and credentials never leave your machine.

---

## What Makes This Special

### 🔍 **Radical Transparency**
The entire codebase is small enough to read in an afternoon:
- **Backend**: ~2,400 lines of Python (orchestrator + API + Claude SDK wrapper)
- **Frontend**: ~4,000 lines of TypeScript/React (multi-tab UI with voice)

No enterprise frameworks, no hidden abstraction layers. Every line that touches your files or executes commands is right there to inspect.

### 🎭 **Multi-Agent Orchestration**
An orchestrator agent coordinates multiple Claude Code instances simultaneously. It's like having a conductor who thinks strategically while specialized agents execute deeply:
- Break complex tasks into parallel workstreams
- Delegate work to specialized agents (code review, testing, documentation)
- Search across all past conversations for relevant context
- Each agent appears as its own tab in the browser UI

This mirrors how humans actually work on complex projects—you don't context-switch constantly, you coordinate parallel efforts.

### 🎤 **Voice-First, Actually**
Talk to the orchestrator naturally via WebRTC with sub-100ms latency:
- Audio streams directly browser ↔ OpenAI (no backend relay)
- Server-side voice activity detection (no push-to-talk)
- Interrupt by speaking (barge-in support)
- Tool calls work during voice (e.g., "open two agent sessions")
- Voice and text share the same conversation history

Voice isn't bolted on—it's a first-class interface that feels native.

### 🧠 **Persistent Memory**
The system maintains searchable memory across all sessions:
- **Agent memory**: Patterns, preferences, and decisions learned over time
- **Orchestrator memory**: Cross-session project context
- **Conversation history**: Every past interaction, semantically indexed

Memory and history are indexed automatically in the background using ChromaDB + sentence-transformers. Search via `/recall <query>` or the orchestrator searches proactively when relevant.

### 🛠️ **Self-Modifying**
The assistant can edit its own capabilities:
- Fix bugs in the UI (while running in that UI)
- Add new tools to the orchestrator
- Create custom skills (slash commands) from workflows
- Modify the wrapper application's source code
- Changes hot-reload automatically

Teach it something once, and it can codify that knowledge into a reusable automation.

---

## What It Can Do

### Regular Agent Sessions
- Execute code, manage files, run shell commands
- Full Claude Code capabilities in each tab
- Stream responses with thinking blocks and tool execution visible
- Real-time cost and token tracking

### Orchestrator Coordination
- **Open multiple agents**: `"Open two agents—one writes tests, the other writes implementation"`
- **Delegate tasks**: `"Have an agent refactor auth while we discuss the API design"`
- **Search context**: `"What was I working on yesterday?"`
- **Read agent history**: Monitor what each agent is doing in real-time
- **Dynamic tab management**: Agent tabs auto-spawn when opened, auto-close when terminated

### Voice Conversations
- Talk to the orchestrator naturally (no clicking)
- Audio level visualization (mic + speaker)
- Live status indicators (listening, thinking, speaking, tool use)
- Mic mute toggle
- Seamless text ↔ voice mode switching

### Memory & Search
- Semantic search over all past conversations
- Search memory files for project context
- Results ranked by relevance (distance threshold: 1.5)
- Automatic background indexing (memory: instant, history: 2-min intervals)

### Custom Skills
Define workflows as YAML + markdown:
```yaml
# context/skills/standup/SKILL.md
---
name: standup
description: Run my morning routine
---

1. Check calendar for today's meetings
2. Summarize unread Slack messages
3. List PRs waiting for review
```

Type `/standup` and it runs. Ask the assistant to "turn this into a skill" after showing it a workflow.

---

## Architecture Overview

```
assistant/
├── orchestrator/     # Orchestrator agent (~800 lines)
│   ├── agent.py      # Main loop with tool execution
│   ├── session.py    # JSONL persistence, dual-mode support
│   ├── providers/    # Anthropic (text) + OpenAI (voice)
│   └── tools/        # 8 tools: agent control, search, files
├── api/              # FastAPI backend (~1000 lines)
│   ├── app.py        # Server with WebSocket routes
│   ├── pool.py       # Unified session pool
│   ├── indexer.py    # Background memory/history indexing
│   └── routes/       # REST + WebSocket endpoints
├── manager/          # Claude SDK wrapper (~600 lines)
│   ├── session.py    # Dual ID system, event streaming
│   └── store.py      # JSONL session reader
├── frontend/         # React multi-tab UI (~4000 lines)
│   ├── context/      # TabsContext (global state)
│   ├── hooks/        # useChatInstance, useVoiceOrchestrator
│   └── components/   # ChatPanel, VoiceButton, TabBar
├── default-skills/   # General-purpose skills (shareable)
├── default-scripts/  # General-purpose scripts (shareable)
├── default-agents/   # General-purpose agents (shareable)
├── context/          # PRIVATE - Standalone git repo, gitignored here (assistant-context repo)
│   ├── *.jsonl       # Session files (SDK writes directly)
│   ├── <uuid>/       # SDK state dirs (subagents, tool-results)
│   ├── memory/       # Memory markdown files
│   ├── skills/       # Symlinks to default-skills + personalized
│   ├── scripts/      # Symlinks to default-scripts + personalized
│   ├── agents/       # Symlinks to default-agents + personalized
│   ├── secrets/      # OAuth credentials and tokens
│   └── .env          # Environment variables
├── utils/            # Shared Python utilities (paths.py)
└── .claude_config/   # SDK config (symlink to context/)
```

### Key Design Decisions

**Dual Session IDs**: Each session has two IDs:
- `local_id`: Stable UUID from frontend (primary key everywhere)
- `sdk_session_id`: Claude SDK's ID (for JSONL files and resume)

This eliminated the "triple-ID-change problem" where tabs would go: `new-N` → placeholder UUID → SDK UUID.

**Headless Instances**: Tab state is separated from presentation. All tabs stay mounted (with `display: none`) to preserve WebSocket/WebRTC connections when inactive. Tab switching is instant.

**Event-Queue Voice**: Frontend mirrors OpenAI Realtime events to backend via WebSocket. Backend only handles tool execution—audio never touches the server. This keeps latency sub-100ms.

**Single Orchestrator**: Only one orchestrator can be active at a time (enforced via modal). This prevents conflicting commands to agent sessions and maintains a clear mental model.

---

## Installation

The assistant is **ready to deploy** on any machine with the prerequisites installed. The installation script handles everything automatically.

### Prerequisites

- **Python 3.12+**
- **Node.js 20+**
- **Claude Code CLI** — `npm install -g @anthropic-ai/claude-code && claude auth login`
- **API Keys** — `ANTHROPIC_API_KEY` (required), `OPENAI_API_KEY` (for voice mode)

Check prerequisites:
```bash
./default-scripts/install-prerequisites.sh
```

### One-Command Installation

```bash
git clone https://github.com/rodrigowf/assistant.git
cd assistant
./install.sh
```

The interactive installer will:
1. ✓ Check system prerequisites (Python, Node.js, Claude CLI)
2. ✓ Set up your context (fresh install or import existing)
3. ✓ Create symlinks to default skills, scripts, and agents
4. ✓ Configure Claude SDK compatibility
5. ✓ Install Python dependencies (FastAPI, ChromaDB, etc.)
6. ✓ Install frontend dependencies (React, Vite)
7. ✓ Verify the installation

**Installation Options:**
```bash
./install.sh                                    # Interactive (recommended)
./install.sh --new-context                      # Fresh install, no prompts
./install.sh --import-context URL               # Import existing context repo
./install.sh --dev                              # Include dev dependencies
./install.sh --skip-prereqs                     # Skip prerequisite checks
```

### Run

```bash
# Terminal 1 — Backend
context/scripts/run.sh -m uvicorn api.app:create_app --factory --port 8765

# Terminal 2 — Frontend
cd frontend && npm run dev
```

Open **https://localhost:5432** and start chatting.

**Tip**: Use `/debug-app` to launch both backend and frontend with browser automation.

### Migrate to Another Machine

Your personal data lives in `context/` (conversations, memory, credentials). To migrate:

1. **If context is a git repo**: Push to private remote, then on new machine:
   ```bash
   ./install.sh --import-context git@github.com:you/assistant-context.git
   ```

2. **Manual migration**: Copy `context/` folder to new machine, then run `./install.sh`

3. **Copy remaining secrets** (not in git):
   - `context/secrets/` — OAuth tokens
   - `context/.env` — API keys
   - `context/certs/` — SSL certificates

---

## Using the Multi-Tab Interface

The web UI is a multi-tab browser application inspired by modern code editors.

### Regular Agent Sessions
Click "New Session" in the sidebar to open a Claude Code agent. Each session appears as a tab. You can:
- Open multiple sessions simultaneously
- Switch between tabs without losing state
- Rename sessions (click the pencil icon in sidebar)
- Delete old sessions (click the × icon)
- Resume past sessions from history

Sessions persist across browser refresh—if you close and reopen, active sessions reconnect automatically.

### Orchestrator Tab
Click the **✦** button in the sidebar to open the orchestrator. This is a special agent that can coordinate all other sessions.

**What the orchestrator can do:**
- `list_agent_sessions` — See all active sessions
- `open_agent_session` — Create or resume agent sessions (tabs auto-spawn)
- `close_agent_session` — Terminate sessions (tabs auto-close)
- `send_to_agent_session` — Delegate work and wait for responses
- `read_agent_session` — Read conversation history
- `list_history` — List all past sessions with metadata
- `search_history` — Semantic search over conversations
- `search_memory` — Semantic search over memory files
- `read_file` / `write_file` — File operations

**Example workflows:**
- "Open two agents—one writes tests, the other writes the implementation. Have them work in parallel."
- "Search my history for how we handled authentication last time, then apply the same pattern here."
- "Open an agent to refactor the user module while we discuss the new API design here."

### Voice Mode (Orchestrator Only)
Click the microphone button in the orchestrator tab to start voice conversation.

**Features:**
- **No push-to-talk**: Server-side VAD detects when you're speaking
- **Interrupt anytime**: Start speaking and the assistant stops (barge-in)
- **Tool calls work**: Say "open two agent sessions" and they spawn
- **Shared history**: Voice transcripts appear inline with text messages
- **Audio visualization**: See mic input and speaker output levels in real-time

**States shown in UI:**
- **Connecting**: Establishing WebRTC
- **Active**: Listening for your voice
- **Speaking**: Assistant is responding
- **Thinking**: Processing your request
- **Tool use**: Executing a tool (e.g., opening sessions)
- **Error**: Connection failed (check `OPENAI_API_KEY`)

---

## Skills: Extensible Automation

Skills are YAML + markdown files in the `context/skills/` directory. They define slash commands that the assistant executes.

### Built-in Skills

| Command | Purpose |
|---------|---------|
| `/recall <query>` | Search memory and past conversations semantically |
| `/scaffold-skill` | Create a new skill from a workflow description |
| `/scaffold-agent` | Define a specialized agent with custom system prompt |
| `/debug-app` | Launch backend + frontend with browser automation |
| `/keybindings-help` | Customize keyboard shortcuts |
| `/wrapper-guide` | Understand the wrapper application internals |

### Creating Custom Skills

Ask the assistant to create a skill:
```
"Turn this into a skill that runs my morning standup:
1. Check calendar for today's meetings
2. Summarize unread Slack messages
3. List PRs waiting for my review"
```

Or manually create `context/skills/standup/SKILL.md`:
```yaml
---
name: standup
description: Run my morning routine
---

1. Use the Bash tool to run `gcal today` and show today's meetings
2. Use the Bash tool to run `slack-cli unread` and summarize
3. Use the Bash tool to run `gh pr list --author @me` and format as table
```

Type `/standup` to run it.

---

## Memory System

The assistant maintains two types of memory:

### Agent Memory
- **Location**: `context/memory/MEMORY.md`
- **Purpose**: Index file with references to detailed docs
- **Pattern**: Keep MEMORY.md under 200 lines with one-line references
- **Details**: Store detailed plans, decisions, and context in separate `.md` files
- **Indexing**: Instant (file watcher with 1s debounce)

Example structure:
```
memory/
├── MEMORY.md                   # Index (one-line references)
├── project-overview.md         # Detailed project docs
├── multi-tab-plan.md           # Frontend implementation notes
└── complete-system-analysis.md # Analysis and improvement ideas
```

### Orchestrator Memory
- **Location**: `context/memory/ORCHESTRATOR_MEMORY.md`
- **Purpose**: Cross-session context for the orchestrator
- **Usage**: Orchestrator reads this before each session start
- **Auto-indexed**: Yes (same as agent memory)

### Conversation History
- **Format**: JSONL files (one per session)
- **Indexing**: Every 2 minutes (if files changed)
- **Search**: Via `/recall` or orchestrator `search_history` tool
- **Cleanup**: Deleted sessions removed from index automatically

---

## Configuration

### Session Settings (`.manager.json`)
```json
{
  "model": "claude-sonnet-4-20250514",
  "permission_mode": "default",
  "max_budget_usd": 10.0,
  "max_turns": 50
}
```

### Environment Variables
```bash
# Required
ANTHROPIC_API_KEY=sk-...        # Claude SDK
OPENAI_API_KEY=sk-...           # Voice mode

# Optional
CLAUDE_CONFIG_DIR=.claude_config       # Local data directory
ORCHESTRATOR_MODEL=claude-sonnet-4-6   # Orchestrator model
ORCHESTRATOR_MAX_TOKENS=8192           # Max tokens per turn
```

---

## Philosophy

### Transparency Over Polish
The codebase is intentionally small and readable. You should understand how it works in an afternoon. No hidden magic, no enterprise frameworks, no vendor lock-in.

### Developer-Native
This isn't a chatbot bolted onto Slack or Discord. It's a proper development environment designed for people who think in code and want to extend their tools.

### Self-Improving
The assistant can modify itself. Fix bugs, add features, create skills—all while running. You're building a tool that grows with you, not adapting to someone else's vision.

### Local-First
Your conversations, memory, and credentials stay on your machine. No cloud sync, no third-party platforms, no account required beyond API keys.

---

## Development

### Setup with Dev Dependencies
```bash
./install.sh --dev
```

### Run Tests
```bash
context/scripts/run.sh -m pytest tests/ -v
```

### Lint and Type Check
```bash
.venv/bin/ruff check .
.venv/bin/mypy api manager orchestrator
```

### Frontend Development
```bash
cd frontend
npm run dev       # Start dev server
npm run build     # Production build
npm run lint      # ESLint
```

---

## API Endpoints

### Sessions
- `GET /api/sessions` — List all sessions (JSONL + live pool status)
- `GET /api/sessions/pool/live` — Active sessions (for reconnect after refresh)
- `GET /api/sessions/{session_id}` — Session detail with messages
- `PATCH /api/sessions/{session_id}/rename` — Rename session
- `DELETE /api/sessions/{session_id}` — Delete session (removes JSONL + index)

### WebSockets
- `WS /api/sessions/chat` — Agent session WebSocket
- `WS /api/orchestrator/chat` — Orchestrator WebSocket

### Voice
- `POST /api/orchestrator/voice/session` — Get ephemeral OpenAI token

---

## How It Works

### Regular Agent Flow
1. User sends message → Frontend (React) with stable `local_id`
2. Frontend → WebSocket (`/api/sessions/chat`) → SessionPool
3. Pool → SessionManager → Claude SDK
4. Claude streams response → Pool broadcasts to all subscribers
5. Events rendered in ChatPanel with real-time updates

### Orchestrator Flow (Text)
1. User sends message → Frontend with `local_id`
2. Frontend → Orchestrator WebSocket
3. OrchestratorSession → AnthropicProvider
4. Tool calls executed (e.g., open agent session)
5. Results streamed back to frontend
6. Agent tabs auto-spawn when sessions opened

### Orchestrator Flow (Voice)
1. Frontend establishes WebRTC to OpenAI (via ephemeral token)
2. User speaks → OpenAI Realtime API
3. Events mirrored to backend via orchestrator WebSocket
4. Backend processes tool calls, sends results back as `voice_command`
5. Frontend forwards results to OpenAI via data channel
6. Assistant responds with voice (audio direct from OpenAI)

### Session Persistence
1. On browser refresh, frontend calls `GET /api/sessions/pool/live`
2. Backend returns all active sessions with `local_id` + `sdk_session_id`
3. Frontend reopens tabs using original `local_id` values
4. Sessions seamlessly reconnect via WebSocket

---

## Production Readiness

### ✅ Ready to Deploy

The assistant is **production-ready** and can be deployed on any machine:

| Feature | Status |
|---------|--------|
| Multi-tab frontend with voice | ✅ Complete |
| Orchestrator agent (text + voice) | ✅ Complete |
| Session persistence & resumption | ✅ Complete |
| Semantic search (memory + history) | ✅ Complete |
| Background indexing | ✅ Complete |
| Cost and usage tracking | ✅ Complete |
| Dynamic agent tab management | ✅ Complete |
| WebRTC voice mode | ✅ Complete |
| **One-command installation** | ✅ Complete |
| **Public/private separation** | ✅ Complete |
| **Cross-machine migration** | ✅ Complete |

### 🎯 Deployment Options

- **Local workstation** — Run on your development machine
- **Home server** — Deploy on a Mac Mini, NUC, or similar
- **Cloud VM** — AWS EC2, GCP, Azure, DigitalOcean
- **Headless server** — Backend-only for API access

### 🔐 Data Portability

Your data is fully portable via the `context/` folder:
- Initialize as a private git repo for version control
- Import on any machine with `./install.sh --import-context URL`
- Symlinks to framework skills/agents are created automatically
- Personal skills, memory, and conversations travel with you

---

## Known Limitations

See `memory/complete-system-analysis.md` for detailed analysis, but high-level:

1. **Error Recovery**: No auto-reconnect on WebSocket failure, no retry logic
2. **Observability**: Limited structured logging, no performance dashboard
3. **Context Management**: No auto-summarization (except voice mode), manual memory updates
4. **Agent Coordination**: Handoffs feel abrupt, no "report back" button
5. **Voice Polish**: No wake word, no live transcription, interruption has 300ms lag

These are all solvable and documented in memory with implementation suggestions.

---

## Future Ideas

See `memory/complete-system-analysis.md` for full brainstorm, but top picks:

1. **Proactive Memory System** — Auto-extract decisions, ask "should I remember this?"
2. **Agent Templates** — Pre-configured agents (code-review, debugger, docs-writer)
3. **Collaborative Sessions** — Multiple humans + agents in one conversation
4. **Workflow Automation DSL** — Define multi-step workflows declaratively
5. **Explain Mode** — Toggle that makes orchestrator explain every decision

---

## License

MIT

---

## Contributing

This is a personal project, but ideas and suggestions are welcome. Open an issue to discuss before submitting a PR.

The goal is to keep the codebase small, readable, and hackable—not to become a framework with every feature imaginable.
