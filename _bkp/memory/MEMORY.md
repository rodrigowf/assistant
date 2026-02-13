# Active Memory

This is the always-read memory file. Keep it concise — move detailed information to topical files in this folder.

## Project Overview

Personal assistant framework: agent-agnostic, skill-centered, self-bootstrapping.
See [architecture.md](architecture.md) for full design.

## Key Decisions

- Skills and agents live at project root, symlinked into `.claude/`
- All executable logic goes in `scripts/` (centralized, not per-skill)
- Skills are declarative (SKILL.md), scripts are executable (bash/python)
- Memory stored in `memory/`, history in `history/`, vector index in `index/`
- Embedding pipeline: ChromaDB + sentence-transformers (all-MiniLM-L6-v2)
- Python dependencies managed via `.venv/`

## Active Skills

- `/scaffold-skill` — Create new skills from conversations
- `/scaffold-agent` — Create new agent definitions
- `/recall` — Search memory and history via embeddings
- `/remember` — Write to memory
- `/save-session` — Export conversation to history
- `/debug-app` — Start and debug the assistant application (backend + frontend)

## Lessons Learned

- Backtick command syntax in SKILL.md triggers permission errors — describe in words instead
- Symlinks from `.claude/` to root folders work for skill/agent discovery
- Keep meta-skill prompts concise and actionable

## Full-Stack Architecture (complete)

Three-layer system, all implemented:

### Layer 1: `manager/` (Python library)
- Wraps Claude Code via `claude-agent-sdk`
- `SessionManager` — start/resume/fork, stream events, interrupt, compact, slash commands
- `SessionStore` — list/read past sessions from Claude Code's JSONL storage
- `HistoryBridge` — export to `history/` + reindex embeddings
- `AuthManager` — OAuth browser flow detection and login
- Auto-export: on `stop()` and via `PreCompact` hook
- Event types: TextDelta, TextComplete, ThinkingDelta, ThinkingComplete, ToolUse, ToolResult, TurnComplete, CompactComplete

### Layer 2: `api/` (FastAPI server)
- Thin adapter over manager — no business logic duplication
- REST: `GET/DELETE /api/sessions/{id}`, `POST .../export`, `GET/POST /api/auth/...`
- WebSocket: `/api/sessions/chat` — JSON frames, orjson serialization
- Protocol: `start` → `session_started` → `send` → event stream → `turn_complete`
- `ConnectionManager` tracks active WS sessions
- Dependencies via `app.state` (lifespan pattern)
- Run: `scripts/run.sh -m uvicorn api.app:create_app --factory --port 8000`

### Layer 3: `frontend/` (Vite + React + TS)
- Dark theme, minimalist UI with CSS custom properties
- `useChat` hook: reducer-based state machine maps WS events to `ChatMessage[]`
- `useWebSocket` hook: connect, auto-reconnect, event dispatch
- `useSessions` hook: REST session list
- Components: Sidebar, ChatPanel, MessageList, Message, Markdown (react-markdown + syntax highlighting), ThinkingBlock, ToolUseBlock, ChatInput, StatusBar, AuthGate
- Vite proxy: `/api` → `localhost:8000` (dev), FastAPI serves `dist/` (prod)
- Run: `cd frontend && npm run dev`

### Tests
- 184 total: 143 manager + 41 API (serializers, connections, auth, sessions, chat WS)
- All mocked — no real Claude Code calls in tests
- Key pattern: `TestClient(app)` with `with` block for WS lifespan

See [manager-plan.md](manager-plan.md) for the full design document.
