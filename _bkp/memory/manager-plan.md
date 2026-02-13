# Manager + API + Frontend Plan

## Architecture

```
frontend/ (Vite + React + TS)
    │ WebSocket
API server (FastAPI + WebSocket)
    │ Python imports
manager/ (claude-agent-sdk wrapper)
```

## Layer 1: manager/ (COMPLETE)

Package structure:
- `manager/session.py` — SessionManager (start/resume/fork, stream events, compact, slash commands)
- `manager/store.py` — SessionStore (list/read Claude Code JSONL sessions)
- `manager/history.py` — HistoryBridge (export to history/, search embeddings, reindex)
- `manager/config.py` — ManagerConfig (load from .manager.json / env vars / defaults)
- `manager/auth.py` — AuthManager (OAuth browser flow detection, login)
- `manager/types.py` — Event hierarchy, SessionInfo, SessionDetail, MessagePreview, SearchResult, SessionStatus

Key features:
- PreCompact hook auto-exports before compaction
- Auto-export on stop() when turns > 0
- compact() and command() for slash commands
- Event stream: TextDelta, TextComplete, ThinkingDelta, ThinkingComplete, ToolUse, ToolResult, TurnComplete, CompactComplete

## Layer 2: API Server (NEXT)

- **Framework**: FastAPI + WebSockets
- `POST /sessions` — Create new session (returns session_id)
- `WS /sessions/{id}/chat` — WebSocket for streaming chat
- `GET /sessions` — List sessions (from SessionStore)
- `POST /sessions/{id}/export` — Export to history
- `POST /sessions/{id}/interrupt` — Interrupt response
- `GET /sessions/{id}/preview` — Session preview
- `POST /auth/login` — Trigger OAuth flow

## Layer 3: Frontend (AFTER API)

- **Stack**: Vite + React + TypeScript
- Chat interface with real-time streaming via WebSocket
- Session sidebar (list, resume, fork)
- Tool use display (collapsible)
- Markdown + code syntax highlighting
- Multi-session tabs

## User Preferences

- Auth via Claude Code plan OAuth (browser link)
- Two folders: `manager/` and `frontend/`
- Library first, design rest later with solid foundation
- WebSocket streaming for real-time chat
