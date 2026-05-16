# Personal Assistant

**A transparent, hackable AI assistant that evolves with you.**

Most AI assistants are black boxes—frozen binaries that do what they do and nothing more. This one is different: it's a small, readable codebase that you can understand, modify, and extend. It can chat, execute commands, remember context, coordinate multiple AI agents, and even edit its own source code while running.

The entire system runs locally. Your conversations, memory, and credentials never leave your machine.

**Two backends, your choice.** Chat sessions can be backed by either [Claude Code](https://docs.claude.com/claude-code) or [Qwen Code](https://github.com/QwenLM/Qwen-Code) (or both — pick at session creation). The orchestrator can use the Anthropic SDK, the OpenAI SDK, or both — Qwen / Gemini / GLM models route through the OpenAI-compatible endpoint. No provider is mandatory; install only the SDKs you need.

---

## What Makes This Special

### 🔍 **Radical Transparency**
The entire codebase is small enough to read in an afternoon:
- **Backend**: Python — orchestrator + API + a thin session-manager layer around the agent CLIs
- **Frontend**: TypeScript / React — multi-tab UI with WebRTC voice

No enterprise frameworks, no hidden abstraction layers. Every line that touches your files or executes commands is right there to inspect.

### 🔀 **Pick Your Agent Backend**
The session harness (the thing that runs your chats) is a thin layer above either the Claude Code SDK or the Qwen Code CLI. They share a common base and the UI lets you flip between them per session:
- **Claude Code** — Anthropic's CLI with OAuth, Claude Sonnet/Opus models, plan-mode permission gating
- **Qwen Code** — Alibaba's open-weights CLI, DashScope or OAuth auth, Qwen 3 models served via the OpenAI-compatible endpoint
- **Both** — installed side by side, picked at session creation time

The orchestrator is independent: it can talk to Anthropic (Claude models) or OpenAI (GPT, plus Qwen / Gemini / GLM through the compatible endpoint), or both. Install only the SDKs you need.

### 🎭 **Multi-Agent Orchestration**
An orchestrator agent coordinates multiple chat sessions simultaneously. It's like having a conductor who thinks strategically while specialized agents execute deeply:
- Break complex tasks into parallel workstreams
- Delegate work to specialized agents (code review, testing, documentation)
- Search across all past conversations for relevant context
- Each agent appears as its own tab in the browser UI
- Fire-and-forget delegation — the orchestrator stays responsive while delegated turns run in the background and report completion via a notification queue

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
- Full Claude Code or Qwen Code capabilities in each tab (picked per session)
- Stream responses with thinking blocks and tool execution visible
- Real-time cost and token tracking
- Permission gating for sensitive tools (e.g. `ExitPlanMode`) — backed by an in-chat modal and recoverable from a conversational "no, do it this way instead" reply

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
├── orchestrator/         # Orchestrator agent (text + voice)
│   ├── agent.py          # Main loop with tool execution
│   ├── session.py        # JSONL persistence, dual-mode support
│   ├── providers/        # Anthropic / OpenAI (text) + OpenAI / Qwen (voice)
│   │                     #   — lazy-loaded, no SDK is mandatory
│   └── tools/            # Tools: agent control, search, files, permissions
├── api/                  # FastAPI backend
│   ├── app.py            # Server with WebSocket routes
│   ├── pool.py           # Unified session pool (Claude + Qwen)
│   ├── indexer.py        # Background memory/history indexing
│   └── routes/           # REST + WebSocket endpoints
├── manager/              # Session managers (per-provider)
│   ├── base_session.py   # BaseSessionManager, dual ID, lifecycle, permission gating
│   ├── claude_session.py # ClaudeSessionManager — Claude Code SDK wrapper
│   ├── qwen_session.py   # QwenSessionManager — Qwen Code CLI driver
│   ├── _proc.py          # Provider-agnostic process helpers
│   └── store.py          # JSONL session reader (handles both providers)
├── frontend/             # React multi-tab UI (main, Node/Vite)
│   ├── context/          # TabsContext (global state)
│   ├── hooks/            # useChatInstance, useVoiceOrchestrator
│   └── components/       # ChatPanel, VoiceButton, TabBar, PermissionModal
├── frontend-compat/      # React 18 compat build for Safari 12 / iOS 12,
│                         #   served at /compat/
├── android/              # Native Android peripheral (Kotlin + Jetpack Compose)
├── install/              # Templates copied into place on fresh installs
│                         #   (AGENTS.md, MEMORY.md, .env, configs, sync.env)
├── default-skills/       # General-purpose skills (shareable)
├── default-scripts/      # General-purpose scripts (shareable)
├── default-agents/       # General-purpose agents (shareable)
├── context/              # PRIVATE — standalone git repo, gitignored here
│   ├── AGENTS.md         # Project instructions (symlinked from root as CLAUDE.md / QWEN.md)
│   ├── *.jsonl           # Session files (SDKs write directly)
│   ├── <uuid>/           # SDK state dirs (subagents, tool-results)
│   ├── memory/           # Memory markdown files
│   ├── skills/           # Symlinks to default-skills + personalized
│   ├── scripts/          # Symlinks to default-scripts + personalized
│   ├── agents/           # Symlinks to default-agents + personalized
│   ├── secrets/          # OAuth credentials and tokens
│   └── .env              # Environment variables
├── utils/                # Shared Python utilities (paths.py)
├── .claude_config/       # Claude SDK config (only when --with-claude)
└── ~/.qwen/projects/<m>/ # Qwen project dir (symlinked to context/, only when --with-qwen)
```

### Key Design Decisions

**Two independent provider axes.** The session harness (which CLI runs your chats) and the orchestrator backend (which API SDK the orchestrator calls) are picked separately at install time and configured independently at runtime. A pure "Qwen everywhere" install is one flag away; a mixed install (Claude harness + OpenAI orchestrator) is also fine.

**Lazy SDK loading.** `manager/__init__.py` and `orchestrator/providers/__init__.py` use PEP 562 `__getattr__` so importing `claude_agent_sdk`, `anthropic`, or `openai` only happens when you actually use that provider. If an SDK isn't installed, the backend still boots — the affected provider just stays disabled and the Config UI returns a 400 with an install hint when you try to pick it.

**Dual Session IDs.** Each session has two IDs:
- `local_id`: Stable UUID from frontend (primary key everywhere)
- `provider_session_id`: The underlying CLI/SDK's ID (for JSONL files and resume)

This eliminated the "triple-ID-change problem" where tabs would go: `new-N` → placeholder UUID → SDK UUID.

**Headless Instances.** Tab state is separated from presentation. All tabs stay mounted (with `display: none`) to preserve WebSocket/WebRTC connections when inactive. Tab switching is instant.

**Event-Queue Voice.** Frontend mirrors OpenAI Realtime events to backend via WebSocket. Backend only handles tool execution—audio never touches the server. This keeps latency sub-100ms.

**Single Orchestrator.** Only one orchestrator can be active at a time (enforced via modal). This prevents conflicting commands to agent sessions and maintains a clear mental model.

**Shared project instructions.** `context/AGENTS.md` is the canonical project-instructions file. `CLAUDE.md` and `QWEN.md` at the repo root are symlinks pointing at it, so both CLIs read the same content from the location they each natively expect.

---

## Installation

The assistant is **ready to deploy** on any machine with the prerequisites installed. The installer asks two questions — which agent CLI(s) to set up and which orchestrator SDK(s) to install — then does the rest automatically.

### Prerequisites

- **Python 3.12+**
- **Node.js 20+** (Qwen Code and Gemini CLI both depend on Node)
- **At least one agent CLI** — the installer can install and walk you through login for any combination:
  - Claude Code (Anthropic) — `@anthropic-ai/claude-code`, OAuth via `claude auth login`
  - Qwen Code (Alibaba) — `@qwen-code/qwen-code`, OAuth or DashScope key
  - Gemini CLI (Google) — `@google/gemini-cli`, OAuth or `GEMINI_API_KEY`
- **API keys** for the axes you opted into (see [Environment Variables](#environment-variables)):
  - `ANTHROPIC_API_KEY` — only if you picked the Anthropic orchestrator backend (Claude Code itself uses OAuth)
  - `OPENAI_API_KEY` — for the OpenAI orchestrator backend and realtime voice
  - `DASHSCOPE_API_KEY` — for the Qwen harness, Qwen voice, and Qwen models served via the OpenAI-compatible endpoint
  - `GEMINI_API_KEY` — for the Gemini CLI harness (when not using OAuth) and the `/generate-image` skill

Check prerequisites (pick the script for your OS):

```bash
./install/linux/install-prerequisites.sh    # Linux
./install/apple/install-prerequisites.sh    # macOS
.\install\windows\install-prerequisites.ps1 # Windows (PowerShell)
```

On macOS and Windows the prereq script also offers to bootstrap missing tools via Homebrew / winget respectively.

For the full step-by-step procedure (used by both `install.sh` and `install-with-agent.sh`, on every OS), see [INSTALL.md](INSTALL.md).

### Two ways to install

Pick whichever feels right — both arrive at the same end state on every supported OS (Linux, macOS, Windows).  Full details for either are in [INSTALL.md](INSTALL.md).

```bash
git clone https://github.com/rodrigowf/assistant.git
cd assistant
```

The installer entry points at the project root auto-detect the host OS and dispatch to the right per-OS implementation under `install/<os>/`.  Pick the appropriate command for your platform:

|  | Linux / macOS | Windows (PowerShell) |
|---|---|---|
| **Option A — Conversational installer (recommended for first-time users)** | `./install-with-agent.sh` | `.\install-with-agent.ps1` |
| **Option B — Deterministic installer** | `./install.sh` | `.\install.ps1` |

**Option A** launches one of the agent CLIs (Claude Code / Qwen Code / Gemini CLI) and lets it walk you through the install conversationally — asking each question, running each step, and writing progress to `context/install.log`.  Reads the appropriate per-OS `install.sh` / `install.ps1` as its recipe.  If no agent CLI is installed yet, the script offers to `npm install -g` one and walks you through first-run login before handing off.

**Option B** asks two questions up front (harness + orchestrator) then runs every step automatically.  No agent involved.  Recommended if you already know what you want or prefer to see every action as plain shell.

Either way, the installer asks two questions:

1. **Session harness** — which agent CLI(s) should run your chats: Claude Code, Qwen Code, Gemini CLI, or any combination.
2. **Orchestrator backends** — which API SDK(s) the orchestrator should use: OpenAI, Anthropic, both, or neither (orchestrator disabled).

Then it:

1. ✓ Checks system prerequisites (Python, Node.js, the agent CLI(s) you picked)
2. ✓ Sets up your context (fresh install or import existing)
3. ✓ Copies fresh-install templates from `install/` into `context/` (AGENTS.md, MEMORY.md, `.env`, configs)
4. ✓ Creates symlinks to default skills, scripts, and agents
5. ✓ Configures the agent CLI(s) you picked (Claude SDK config dir and/or Qwen / Gemini project dirs)
6. ✓ Wires `CLAUDE.md` and `QWEN.md` symlinks → `context/AGENTS.md` so both CLIs read the same instructions
7. ✓ Seeds the per-CLI runtime dirs (`.claude/`, `.qwen/`, `.gemini/`) from `install/cli-runtime/` templates
8. ✓ Installs core Python dependencies and only the provider SDKs for the axes you picked
9. ✓ Installs frontend dependencies (React, Vite)
10. ✓ Installs and walks you through first-run login for each agent CLI you picked (skippable via `--skip-auth`)
11. ✓ Verifies the installation (warns about missing API keys for the keys your axes actually need)

**Installation Options:**

```bash
./install.sh                                    # Interactive — asks both questions
./install.sh --new-context                      # Fresh install, no context prompt
./install.sh --import-context URL               # Import existing context repo
./install.sh --dev                              # Include dev dependencies (ruff, mypy)
./install.sh --skip-prereqs                     # Skip prerequisite checks
./install.sh --skip-auth                        # Skip the agent CLI install/login step
```

**Skip the two questions** by pinning the axes from the command line:

```bash
# Session harness
--with-claude         --without-claude
--with-qwen           --without-qwen

# Orchestrator backends
--with-anthropic      --without-anthropic
--with-openai         --without-openai

# Shortcut: everything Qwen-backed
--qwen-only           # = --with-qwen --without-claude --with-openai --without-anthropic
```

Examples:

```bash
./install.sh --qwen-only                                       # Qwen harness + OpenAI orchestrator (Qwen routes via OpenAI-compatible endpoint)
./install.sh --with-claude --with-anthropic --with-openai      # Default power-user setup, no prompts
./install.sh --with-claude --with-qwen --with-openai --without-anthropic
                                                               # Both CLIs, OpenAI-only orchestrator
```

### Fresh-Install Templates (`install/`)

The `install/` directory holds the **template files** the installer copies into a fresh checkout. Edit them to change the defaults a clean install lands on:

| Template | Copied to | Purpose |
|----------|-----------|---------|
| `AGENTS.md` | `context/AGENTS.md` | Project instructions (read via `CLAUDE.md` / `QWEN.md` symlinks). |
| `MEMORY.md` | `context/memory/MEMORY.md` | The shared memory index. |
| `context.env` | `context/.env` | API keys + runtime config. The installer uncomments the keys for the axes you picked. |
| `assistant_config.json` | `assistant_config.json` (repo root) | Default working dir, provider, model. Placeholders substituted at install time. |
| `manager.json` | `.manager.json` (repo root) | Session-manager defaults. |
| `sync.env` | `sync/config.env` (manual copy) | Optional — for the `context-sync` two-machine service. |

See [install/README.md](install/README.md) for details.

### Run

```bash
# Terminal 1 — Backend
context/scripts/run.sh -m uvicorn api.app:create_app --factory --port 8765

# Terminal 2 — Frontend
cd frontend && npm run dev
```

Open **https://localhost:5432** and start chatting.

**Tip**: Use `/debug-app` to launch both backend and frontend with browser automation.

### Switching Providers Later

The provider selection at install time is not a one-way door:

- **Flip the harness for an individual chat** — Configuration → Session provider (only installed providers appear in the selector).
- **Add a provider you didn't install** — run `./install.sh --with-<provider>` again; it only does the per-axis steps for what's newly enabled. Or `pip install -r requirements-<axis>.txt` directly.
- **Default for new chats** — edit the `provider` field in `assistant_config.json`, or use the Configuration UI.

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

For continuous two-machine sync (e.g. laptop ↔ home server), see `sync/README.md` — the `context-sync` systemd user service mirrors `context/` in real time via `inotifywait` + `rsync` over SSH.

### Peripheral Frontends

Beyond the main web UI, the project ships two additional frontend surfaces — both speak the same REST + WebSocket API:

- **`frontend-compat/`** — React 18 compat build served at `/compat/`. Targets Safari 12 / iOS 12 (iPad mini 2 etc.).
- **`android/`** — Full native Android peripheral (Kotlin + Jetpack Compose). Chat, sessions, and WebRTC voice. Targets Android 5.0+. Build with `cd android && ./gradlew assembleDebug`; the `/android-dev` skill handles build + deploy + debug.

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
| `/android-dev` | Build, deploy, and debug the Android peripheral app |
| `/tv-remote` `/connect-tv` `/create-viz` | Fire TV control (media, navigation, visualizations) |
| `/youtube` | Full YouTube Data API access (search, playlists, videos) |
| `/generate-image` | Generate images via Google's Nano Banana (Gemini Image) |
| `/iphone-photos` | Connect to the iOS photo server and browse media |
| `/music-video-editor` | Multitrack-to-composite music video workflow (sync + Remotion) |

Run `/help` inside the assistant for the full list (it changes as you add skills) or browse `default-skills/` directly.

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
Session-manager defaults (model, permissions, budget). Seeded from `install/manager.json` at install time:
```json
{
  "model": "claude-sonnet-4-20250514",
  "permission_mode": "default",
  "max_budget_usd": 10.0,
  "max_turns": null
}
```

### Default Provider (`assistant_config.json`)
The `provider` field decides which CLI new chats use (`claude` or `qwen`). The Configuration UI flips it at runtime; this file is just the seed. `default_model` is provider-appropriate (Claude Sonnet vs. Qwen 3 Plus).

### Environment Variables

API keys are kept in `context/.env`. The installer uncomments the keys for the axes you opted into; the others stay commented so they're a one-liner away if you flip an axis on later.

```bash
# Orchestrator + voice (OpenAI / GPT, Qwen via OpenAI-compatible endpoint, Gemini)
OPENAI_API_KEY=sk-proj-...

# Orchestrator Claude models. Not needed if you only use Claude Code as the
# harness — Claude Code authenticates with its own OAuth.
ANTHROPIC_API_KEY=sk-ant-...

# Qwen harness + Qwen voice + Qwen orchestrator models via the OpenAI-compatible endpoint.
DASHSCOPE_API_KEY=sk-...

# Google Gemini — used by:
#   - the /generate-image (Nano Banana) skill
#   - the Gemini CLI harness (if installed via --with-gemini and using API key auth instead of OAuth)
#   - the Gemini Live realtime voice provider (when enabled)
# This is the canonical name Google's own SDKs and the gemini CLI expect.
GEMINI_API_KEY=...

# Session harness default (env var only applies when assistant_config.json hasn't been
# written yet — the Configuration UI is the primary way to flip this).
# Options: claude, qwen
# ASSISTANT_PROVIDER=claude

# Path to the qwen CLI — only if `qwen` isn't on $PATH for the user the backend runs as
# (e.g. NVM installs it under a versioned node directory).
# QWEN_CLI_PATH=/home/you/.nvm/versions/node/vXX.YY.ZZ/bin/qwen

# Orchestrator defaults (UI can change per session).
# ORCHESTRATOR_MODEL=gpt-4o
# REALTIME_MODEL=gpt-realtime
```

`CLAUDE_CONFIG_DIR` is set to `.claude_config/` automatically by `context/scripts/run.sh` — no need to set it yourself.

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
3. Pool → `ClaudeSessionManager` or `QwenSessionManager` (based on the session's `provider`) → agent CLI subprocess
4. Agent streams response → Pool broadcasts to all subscribers
5. Events rendered in ChatPanel with real-time updates (text deltas, tool calls, permission requests)

### Orchestrator Flow (Text)
1. User sends message → Frontend with `local_id`
2. Frontend → Orchestrator WebSocket
3. `OrchestratorSession` → `AnthropicProvider` or `OpenAITextProvider` (based on the selected model)
4. Tool calls executed concurrently (e.g. open agent session, search history)
5. Background-agent turns delegated via `BackgroundAgentRunner` — orchestrator stays responsive; completions reported via `Notification` queue prepended to the next prompt
6. Results streamed back to frontend; agent tabs auto-spawn when sessions opened

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
| Permission gating (with conversational + modal paths) | ✅ Complete |
| Fire-and-forget delegated agent turns | ✅ Complete |
| SSH remote-session execution | ✅ Complete |
| **Claude Code + Qwen Code harnesses (interchangeable)** | ✅ Complete |
| **No provider mandatory — install only the SDKs you need** | ✅ Complete |
| **Two-axis installer (harness × orchestrator backend)** | ✅ Complete |
| **`install/` directory of fresh-install templates** | ✅ Complete |
| **Frontend-compat for Safari 12 / iOS 12** | ✅ Complete |
| **Native Android peripheral app** | ✅ Complete |
| Public/private separation | ✅ Complete |
| Cross-machine migration + real-time sync | ✅ Complete |

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
