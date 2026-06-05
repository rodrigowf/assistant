# AGENTS.md

This file is the canonical project-instructions document for coding agents (Claude Code, Qwen Code). It lives in `context/AGENTS.md` (the private gitignored data repo); `CLAUDE.md` and `QWEN.md` at the project root are symlinks pointing here, so both CLIs read the same content from the location they each natively expect.

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
- **Talk** using realtime voice mode — speak through the orchestrator via WebRTC
- **Execute** code, manage files, run shell commands—full agent capabilities
- **Remember** context across sessions with searchable conversation history
- **Automate** workflows through custom skills (slash commands)
- **Evolve** by creating new skills and modifying your own behavior

---

## Project Structure

This project separates **public framework** from **private data** for easy sharing and environment migration:

```
assistant/                    # PUBLIC - shareable framework
├── default-skills/           # General-purpose skills (actual files)
├── default-scripts/          # General-purpose scripts (actual files)
├── default-agents/           # General-purpose agents (actual files)
├── install/                  # Templates for a fresh installation
├── .claude_config/           # Claude Code SDK config (when --with-claude)
│   └── skills → ../context/skills  # SDK skill discovery
├── manager/                  # Session managers (Claude / Qwen)
├── orchestrator/             # Orchestrator agent — controls chat sessions
│   └── providers/            # Model providers (Anthropic, OpenAI, voice)
├── api/                      # FastAPI server (REST + WebSocket)
├── frontend/                 # React multi-tab chat interface (Node/Vite)
├── frontend-compat/          # React 18 compat build for legacy browsers, served at /compat/
├── android/                  # Native Android app peripheral (Kotlin + Jetpack Compose)
├── utils/                    # Shared Python utilities (paths.py)
├── tests/                    # Test suite
├── index/                    # Vector search index (gitignored)
└── .venv/                    # Python virtual environment

context/                      # PRIVATE - Standalone git repo, gitignored here
├── AGENTS.md                 # This file (symlinked from project root as CLAUDE.md / QWEN.md)
├── *.jsonl                   # Conversation JSONL files
├── <uuid>/                   # SDK state directories (subagents, tool-results)
├── memory/                   # Memory markdown files
├── public/                   # Static files served at URL root (downloads, visualizations, etc.)
├── skills/                   # Symlinks to default-skills + personalized skills
├── scripts/                  # Symlinks to default-scripts + personalized scripts
├── agents/                   # Symlinks to default-agents + personalized agents
├── secrets/                  # OAuth credentials and tokens
├── certs/                    # SSL certificates
└── .env                      # Environment variables
```

**Public/Private separation:**
- `default-skills/`, `default-scripts/`, and `default-agents/` contain general-purpose tools (shareable)
- `context/` is a standalone git repo, gitignored by the parent
- `context/skills/` has symlinks to `default-skills/*` plus personalized skill folders
- `context/scripts/` has symlinks to `default-scripts/*` plus personalized scripts
- `context/agents/` has symlinks to `default-agents/*` plus personalized agents
- Swap the `context/` directory (clone a different context repo in its place) to migrate to a new environment

`CLAUDE_CONFIG_DIR` is set to `.claude_config/` by `context/scripts/run.sh`. All code references `context/` directly via `utils/paths.py`.

---

## Reference

### The Wrapper Application

The wrapper (api + manager + orchestrator + frontend) provides a multi-tab web interface for interacting with the agent CLI you chose at install time (Claude Code, Qwen Code, or both). The orchestrator agent can control multiple chat instances simultaneously and supports both text and realtime voice modes. When running inside the wrapper, you can edit its own code—the manager, API routes, frontend components—and those changes affect the very application you're running in.

**Session IDs:** Each session has a stable `local_id` (UUID, generated by frontend, never changes) and a `provider_session_id` (from the underlying CLI/SDK, used for resume/JSONL). The `local_id` is the primary key for the session pool, tabs, and orchestrator.

Start the backend: `context/scripts/run.sh -m uvicorn api.app:create_app --factory --host 0.0.0.0 --port 8765`

Start the frontend: `cd frontend && npm run dev`

Or use `/debug-app` which handles both and provides browser automation.

**Provider selection:** The "Session provider" selector in the Configuration panel picks which CLI new chats use (only installed providers appear). The default for new chats is read from `assistant_config.json` (`provider` field).

**SSH Remote Execution:** The backend can spawn agent sessions on a remote machine over SSH. When a working directory in `assistant_config.json` has `ssh_host`/`ssh_user` fields, the session manager generates a shell wrapper script that SSHes into the remote machine, sets the appropriate config dir, and runs the agent CLI there. SSH multiplexing (`ControlMaster`) reuses connections. All SDK flags are single-quoted and expanded locally to avoid the SSH quoting bug (SSH space-joins arguments).

### Peripheral Frontends

The assistant supports multiple frontend surfaces beyond the main web interface:

**frontend-compat** — React 18 compat build served at `/compat/`. Targets legacy browsers (Safari 12, iOS 12). Built separately.

**Android app** (`android/`) — Full native Android peripheral app (Kotlin + Jetpack Compose). Provides chat, session management, and WebRTC realtime voice. Connects to the same backend WebSocket/REST API as the web frontend. Targets API 21+ (Android 5.0).

- Build: `cd android && ./gradlew assembleDebug`
- APK output: `app/build/outputs/apk/debug/app-debug.apk`
- Use `/android-dev` for building, deploying, and debugging

### Memory System

The `context/` folder is a standalone git repo (gitignored by the parent) containing all private data, including the memory system:

```
context/
├── *.jsonl            # Conversation history (JSONL files)
├── .titles.json       # Custom session titles
├── <uuid>/            # SDK state dirs (subagents, tool-results)
├── memory/            # Memory files (Markdown)
│   ├── MEMORY.md      # Authoritative index (keep under 200 lines)
│   └── *.md           # Detailed topic files
├── public/            # Static files served at URL root
├── skills/            # Symlinks to default-skills/* + personalized folders
├── scripts/           # Symlinks to default-scripts/* + personalized files
├── secrets/           # OAuth credentials and tokens
├── certs/             # SSL certificates
└── .env               # Environment variables
```

**The Shared Memory Index (`context/memory/MEMORY.md`)**

This file is the **single source of truth** for all skills, memory files, and project references. Both the orchestrator (which loads it dynamically into its prompt) and chat agents rely on this index.

**Structure:**
- Keep `MEMORY.md` under 200 lines with one-line references only
- Store detailed content in separate `<topic>.md` files
- Reference format: `- filename.md — Brief description`

**Your maintenance responsibilities:**

When you make changes that affect the memory index, update `MEMORY.md` directly:
1. **Skills**: Update the Skills Reference table when skills are added, removed, or modified
2. **Memory files**: Add a reference line when creating new `<topic>.md` files
3. **Projects**: Update entries when project status changes (started, completed, abandoned)

You have full editing capabilities via your tools — use them to keep the index accurate. The orchestrator loads this file dynamically, so your updates are immediately visible to the entire system.

**Semantic search:**

Both memory and conversation history are indexed for search via `/recall <query>`:
- Memory files: Indexed immediately when changed (file watcher)
- History: Indexed every 2 minutes (if changed)

**Memory navigation:**

Navigate the memory hierarchy recursively:
- `MEMORY.md` → references detailed topic files
- Project memory file → may contain nested references to sub-topics

When the user asks to remember something or retrieve memory, prefer **direct file lookup** over automated search. Read `MEMORY.md` to identify relevant files, then read those files directly. The `search_memory` / `/recall` tool is useful for broad searches but may not capture the full structured context. Use direct file reading as the primary approach and search as a supplement.

### Voice Mode (Realtime)

The orchestrator supports a realtime voice mode powered by the OpenAI Realtime API via WebRTC. Audio flows directly between the browser and OpenAI for low latency; the backend only handles signaling, tool execution, and persistence.

**Architecture:**
- Frontend establishes a WebRTC connection to OpenAI using an ephemeral token from the backend (`POST /api/orchestrator/voice/session`)
- Audio streams directly between browser ↔ OpenAI (sub-100ms latency)
- The frontend mirrors all OpenAI data channel events to the backend via the orchestrator WebSocket (`voice_event` messages)
- The backend processes tool calls and sends commands back (`voice_command` messages) for the frontend to forward to OpenAI
- Server-side VAD (voice activity detection) — no push-to-talk needed

**Key files:**
- `api/routes/voice.py` — Ephemeral token endpoint (exchanges `OPENAI_API_KEY` for a short-lived token)
- `orchestrator/providers/openai_voice.py` — `OpenAIVoiceProvider` that translates OpenAI Realtime events into `OrchestratorEvent`s
- `orchestrator/session.py` — Voice session lifecycle, tool execution, JSONL persistence
- `frontend/src/hooks/useVoiceSession.ts` — WebRTC connection management (SDP exchange, mic, data channel)
- `frontend/src/hooks/useVoiceOrchestrator.ts` — Bridges the WebRTC session and orchestrator WebSocket
- `frontend/src/components/VoiceButton.tsx` — Mic toggle UI with states: off, connecting, active, speaking, thinking, tool_use, error
- `frontend/src/api/voice.ts` — API client for ephemeral token and SDP exchange

**Environment:** Requires `OPENAI_API_KEY` set in the environment. Default model: `gpt-realtime`.

**Tool sharing:** Both text and voice modes use the same `ToolRegistry`. `get_definitions()` returns Anthropic format; `get_openai_definitions()` returns OpenAI function-calling format.

**JSONL persistence:** Voice turns are saved with `"source": "voice_transcription"` / `"voice_response"` fields. User transcripts are prefixed with `[voice]`. Interruptions are logged as `voice_interrupted` entries.

### Self-Modification

You can extend and modify your own capabilities:

- **Skills** (`context/skills/`): Create with `/scaffold-skill`, modify existing ones directly
- **Agents** (`context/agents/`): Create with `/scaffold-agent` for specialized subagents
- **Scripts** (`context/scripts/`): Shared tools any skill can reference
- **Wrapper** (`api/`, `manager/`, `frontend/`): The application code itself
- **Android app** (`android/`): Native mobile peripheral — use `/android-dev` to build, deploy, and debug

Run Python scripts through the venv: `context/scripts/run.sh context/scripts/<script>.py [args]`

**General vs Personalized:**
- General-purpose items live in `default-skills/`, `default-scripts/`, and `default-agents/`
- Personalized ones live directly in `context/skills/`, `context/scripts/`, and `context/agents/`
- The context folders have symlinks to the defaults, so all are accessible from one place

### Skill and Script Maintenance

You have an active role in maintaining and improving skills and scripts. When you identify any problems, gaps, or issues with a skill or script, you should:

1. Think about how to address and fix that issue.
2. Present options to the user for addressing and fixing the problem.
3. Offer to spin up a nested agent to work on that specific skill or script improvement.

This ensures that the assistant is always evolving and that skills/scripts remain up-to-date and effective.

### Writing Skills

- Format: YAML frontmatter + markdown instructions
- Never use literal backtick command syntax in SKILL.md (triggers permission prompts)
- Variable substitution: `$ARGUMENTS`, `$0`/`$1`/`$2`, `${CLAUDE_SESSION_ID}`

### Two Distinct Agent Systems

This project contains **two separate agent systems** that should not be confused:

1. **`manager/` → `BaseSessionManager` (with `ClaudeSessionManager` / `QwenSessionManager` subclasses)** — Wraps the underlying agent CLI. Each instance spawns a subprocess (Claude Code or Qwen Code, depending on the session's `provider`) and streams events (`TextDelta`, `ToolUse`, `TurnComplete`, etc.). Managed by `api/pool.py` (`SessionPool`). Used for the main chat tabs.

2. **`orchestrator/session.py` → `OrchestratorSession`** — A hand-written agent loop that calls Anthropic or OpenAI APIs directly. Has its own tool registry (`orchestrator/tools/`), system prompt, and JSONL persistence. Used for the orchestrator tab (higher-level coordination, voice mode). Does NOT use either agent CLI.

**Event flow for regular chat sessions:**
```
Frontend WebSocket → api/routes/chat.py → SessionPool.send()
  → ClaudeSessionManager/QwenSessionManager.send() → agent CLI subprocess
  → Events broadcast to all WebSocket subscribers
```

**Event flow for orchestrator:**
```
Frontend WebSocket → api/routes/orchestrator.py → OrchestratorSession.send()
  → OrchestratorAgent.run() → ModelProvider → Anthropic/OpenAI API
  → ToolRegistry.execute() (non-blocking, concurrent)
  → Events broadcast via SessionPool.broadcast_orchestrator()
```

### Fire-and-Forget Agent Turns

The orchestrator delegates work to chat sessions through `orchestrator/runner.py` (`BackgroundAgentRunner`) instead of awaiting `pool.send()` inline. The runner owns one `asyncio.Task` per in-flight agent turn, buffers events in a per-turn ring for `read_agent_session`'s live tail, and pushes one terminal `Notification` per turn (succeeded / failed / cancelled / timeout) onto a `NotificationQueue`. The orchestrator drains the queue at the top of each turn and prepends the notifications as `[SESSION xxx event: ...]` lines to the prompt the LLM sees, so the model can react to background completions even though it is not blocking on them.

- `send_to_agent_session` returns immediately with `{turn_id, session_id, status: "running", started_at}`. Concurrent fan-out to multiple sessions is parallel; two calls to the same session serialize naturally inside `pool.send()`'s per-session lock.
- `read_agent_session` returns persisted messages from JSONL plus a `live` block with `status` (`running`/`idle`) and, while a turn is in flight, the recent tail of live events (text deltas, tool_use, tool_result, permission events) from the runner's ring buffer. One tool, whether you want canonical history or progress on an in-flight turn.
- `interrupt_agent_session` cancels a running turn. `respond_to_agent_permission` answers a pending permission on a delegated session.
- `OrchestratorSession._busy_lock` (exposed via the `is_busy` property) wraps every `send()` / `send_audio()` body. The wake callback installed by `api/routes/orchestrator.py` consults `is_busy` and schedules a synthetic empty-prompt turn only when the orchestrator is idle — so a notification arriving mid-turn just queues for the next drain. If you add a new wake source, gate it the same way. Voice mode skips the synthetic wake (notifications still drain on the next text turn).
- Each drained notification is persisted as a `background_notification` JSONL entry tied to the originating `tool_use_id`, so a fire-and-forget run is replayable.

### Permission Gating

The bundled Claude Code CLI fires a permission gate for tools like `ExitPlanMode`. `ClaudeSessionManager` wires the SDK `can_use_tool` callback: tools in `_DEFAULT_GATED_TOOLS` (currently `{"ExitPlanMode"}`) emit a `PermissionRequest` event into the active `send()` stream and await an `asyncio.Future`; anything else auto-allows. `permission_mode` is `"default"` so the SDK actually invokes the callback.

**Conversational checkpoint design** — the popup is a backstop, not the primary mechanism. The session manager appends a permission-gating prompt to the bundled system prompt instructing the agent to announce its intent in chat *before* calling a gated tool. The user (or orchestrator) guides with prose; the popup remains as the formal yes/no path.

- Frontend (`useChatInstance` + `PermissionModal`): per-tab `pendingPermission` state. The user can Approve, Reject, or just type a chat message — typing resolves all pending permissions on that session as `deny` with the prose as the rejection reason (`api/routes/chat.py`), then sends the message normally. The agent receives both signals and refines.
- Orchestrator: permission events are mirrored to `broadcast_orchestrator` as `nested_session_event`, and the orchestrator can answer via the `respond_to_agent_permission` tool. First write wins between user and orchestrator; the loser's modal closes via the broadcast.
- The plan text from `ExitPlanMode` is pushed into the conversation as a normal assistant text block. `permission_request` / `permission_resolved` are broadcast over the WebSocket (driving modal lifecycle) but are not written as dedicated JSONL entries — the persisted record is the orchestrator's `background_notification` line plus the agent's own JSONL events.

### Stall Watchdog

When the bundled `claude` subprocess goes silent mid-tool (most often `WebFetch` waiting on an unresponsive endpoint), the SDK never emits a `ResultMessage`. The session manager's `send()` drains the SDK receiver via a worker task and pulls from a queue with `asyncio.wait_for`; on timeout it yields a `SessionStalled` event naming the in-flight tool, then keeps waiting. First notice at 120s of silence, repeats every 60s. The frontend shows a yellow banner above the input with an Interrupt button while a stall is reported and the session is still streaming. `SessionStalled` is advisory — it does NOT abort the stream.

The send loop also recovers from a related SDK bug: some `claude-cli` versions deliver bundled-tool output as a plain string instead of the documented dict. The old code dropped the `ToolResult` entirely and left the UI's tool block stuck on "running"; the loop now treats the string as the tool output and recovers `tool_use_id` from `parent_tool_use_id` on the `UserMessage`.

### Warm Search Server

Loading PyTorch + sentence-transformers + the embedding model takes ~100s on low-power hardware, so `default-scripts/search-server.py` runs as a persistent subprocess that loads the model once and serves queries over stdin/stdout (JSON-line protocol). `orchestrator/tools/search.py` manages the singleton with auto-recovery and a cold fallback. The server is pre-warmed during API startup (`api/app.py`) so the first query is fast too. `default-scripts/run.sh` sets `LD_PRELOAD=libgomp.so.1` on aarch64 to fix the "cannot allocate memory in static TLS block" ImportError. Searches went from ~68–103s to ~1–3s.

### Testing

Run the full suite: `context/scripts/run.sh -m pytest tests/ -v`

Run a single test: `context/scripts/run.sh -m pytest tests/test_foo.py::TestClass::test_method -v`

Mock external dependencies with `unittest.mock`.

### Browser Automation

Chrome DevTools MCP provides full browser control. Use `/debug-app` for integrated frontend testing.

### Skills & Integrations

The default skill set provides a broad range of integrations. Run `/help` inside the assistant to list them, or browse `default-skills/` directly.

Categories include:
- **Code/dev tooling** — `/debug-app`, `/scaffold-skill`, `/scaffold-agent`, `/android-dev`, `/tv-dev`
- **Media** — `/generate-image`, `/create-viz`, `/music-video-editor`
- **Device control** — `/tv-remote`, `/connect-tv`, `/iphone-photos`
- **APIs** — `/youtube` (YouTube Data API), Google integrations

Personalized skills live next to the defaults in `context/skills/` and are discovered automatically.

### Orchestration Behavior

When delegating tasks to agent sessions, ensure the related skills and context are loaded. Match the skill to the task type (TV → `/tv-remote`, video → `/youtube` + `/create-viz`, image → `/generate-image`, etc.).

When handling repeating-context requests: reuse an open session if relevant, search history for a past similar session to resume, or create a new one only if needed.

### User Context

This is a fresh installation. Personalize this section as you learn about the user: who they are, what they work on, how they prefer to collaborate. Keep detailed personal context in `context/memory/user_context.md` (or similar) and link to it from `MEMORY.md`.

### Tracked Projects

This section lists active projects with dedicated memory files. A fresh installation starts empty — add entries as projects are scoped.

See `context/memory/personal_projects_index.md` (create this when you have multiple tracked projects) for full details.

### Compact Instructions

When compacting, preserve:
- Current task context and progress
- Key decisions made during this session
- Key learnings from this session
- Any unresolved questions or blockers
