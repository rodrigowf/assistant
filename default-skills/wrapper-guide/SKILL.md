---
name: wrapper-guide
description: Understand, navigate, and debug the wrapper application (claude manager, API, frontend). Use when investigating issues, understanding data flow, or making changes to the wrapper.
argument-hint: "[topic]"
allowed-tools: Read, Glob, Grep
---

# Wrapper Application Guide

This skill provides comprehensive knowledge for understanding, navigating, and debugging the wrapper application that powers this assistant.

Topic requested: **$ARGUMENTS**

---

## Architecture Overview

The wrapper consists of three interconnected components:

```
Frontend (React) ──WebSocket/REST──> API (FastAPI) ──SDK──> Manager (Python)
     │                                    │                       │
     │                                    │                       ▼
     │                                    │              Claude Code SDK
     │                                    │                       │
     └────────────────────────────────────┴───────────────────────┘
                          User interaction
```

**Data flows from user input through:**
1. Frontend captures input, sends via WebSocket
2. API routes message to SessionManager
3. Manager wraps Claude SDK, yields typed events
4. API serializes events back to WebSocket
5. Frontend renders streaming updates

---

## 1. Manager Package (`manager/`)

The manager wraps the `claude-agent-sdk` to provide a clean async interface.

### Key Files

| File | Purpose |
|------|---------|
| `session.py` | `SessionManager` - core session lifecycle and message handling |
| `types.py` | Event dataclasses (`TextDelta`, `ToolUse`, etc.) and data types |
| `store.py` | `SessionStore` - reads Claude Code's JSONL session files from disk |
| `config.py` | `ManagerConfig` - loads from JSON file, env vars, or defaults |
| `auth.py` | `AuthManager` - OAuth status checks and login flow |

### SessionManager (`session.py`)

The heart of the manager. Wraps a single Claude Code conversation.

**Dual ID System:**
Each session has two IDs:
- `local_id` — Stable UUID generated at creation time. Used as the primary key throughout the system (pool, tabs, orchestrator). Never changes.
- `sdk_session_id` — The Claude Code SDK's session ID. Only available after the first message is sent. Used for resuming sessions and looking up JSONL files on disk.

**Lifecycle:**
```python
sm = SessionManager(session_id="sdk-id-to-resume", local_id="stable-uuid", config=config)
await sm.start()           # Connect to SDK, returns local_id (stable)
async for event in sm.send("Hello"):  # Stream events
    ...                    # sdk_session_id becomes available after first response
await sm.stop()            # Disconnect
```

**Key methods:**
- `start()` → Connect to SDK, return `local_id` (stable, never changes)
- `send(prompt)` → Yields `Event` objects as response streams
- `command(slash_cmd)` → Send `/compact`, `/help`, etc.
- `interrupt()` → Stop current response
- `stop()` → Disconnect

**Properties:**
- `local_id` — Stable local UUID (primary identifier)
- `sdk_session_id` — Claude SDK session ID (available after first message)
- `session_id` — Alias for `local_id`
- `status`, `cost`, `turns`, `is_active`

**Internal flow in `_process_message()`:**
- `StreamEvent` with `content_block_delta` → `TextDelta` / `ThinkingDelta`
- `SystemMessage` with `subtype="compact"` → `CompactComplete`
- `AssistantMessage` → Iterates blocks: `TextBlock`→`TextComplete`, `ToolUseBlock`→`ToolUse`, `ToolResultBlock`→`ToolResult`
- `UserMessage` with `tool_use_result` → `ToolResult`
- `ResultMessage` → `TurnComplete` (updates cost/turns)

### Event Types (`types.py`)

All events inherit from `Event` base class:

| Event | When | Key Fields |
|-------|------|------------|
| `TextDelta` | Streaming text token | `text` |
| `TextComplete` | Full text block done | `text` |
| `ThinkingDelta` | Streaming thinking | `text` |
| `ThinkingComplete` | Thinking block done | `text` |
| `ToolUse` | Tool invoked | `tool_use_id`, `tool_name`, `tool_input` |
| `ToolResult` | Tool finished | `tool_use_id`, `output`, `is_error` |
| `TurnComplete` | Turn finished | `cost`, `usage`, `num_turns`, `session_id` |
| `CompactComplete` | Compaction done | `trigger` ("manual"/"auto") |

### SessionStore (`store.py`)

Reads Claude Code's session files from disk (JSONL format).

**Session location:**
```
$CLAUDE_CONFIG_DIR/projects/<mangled-path>/<session-id>.jsonl
```
Where `<mangled-path>` = `/home/rodrigo/Projects/assistant` → `-home-rodrigo-Projects-assistant`

**Key methods:**
- `list_sessions()` → All sessions sorted by recency
- `get_session(id)` → Full `SessionDetail` with messages
- `get_preview(id, max)` → Last N messages
- `delete_session(id)` → Remove JSONL file

**JSONL line types:** `user`, `assistant`, `system`, `progress`, `file-history-snapshot`, `queue-operation`

### ManagerConfig (`config.py`)

Configuration loading order: JSON file → env vars → defaults

| Field | Env Var | Default |
|-------|---------|---------|
| `project_dir` | `MANAGER_PROJECT_DIR` | Parent of manager/ |
| `model` | `MANAGER_MODEL` | None (SDK default) |
| `permission_mode` | `MANAGER_PERMISSION_MODE` | "default" |
| `max_budget_usd` | `MANAGER_MAX_BUDGET_USD` | None |
| `max_turns` | `MANAGER_MAX_TURNS` | None |

### AuthManager (`auth.py`)

OAuth authentication helper.

- `is_authenticated()` → Runs `claude auth status`, parses JSON
- `login()` → Runs `claude setup-token` (opens browser)

**Important:** Unsets `CLAUDECODE` env var to run auth commands inside a session.

---

## 2. API Package (`api/`)

FastAPI server providing REST + WebSocket interfaces.

### Key Files

| File | Purpose |
|------|---------|
| `app.py` | Application factory with lifespan (startup/shutdown) |
| `routes/chat.py` | WebSocket endpoint for real-time streaming |
| `routes/sessions.py` | REST endpoints for session CRUD |
| `routes/auth.py` | Auth status and login endpoints |
| `pool.py` | `SessionPool` - manages all active sessions, keyed by local_id |
| `connections.py` | `ConnectionManager` - tracks active WebSocket sessions |
| `serializers.py` | Converts manager Events to JSON dicts |
| `models.py` | Pydantic response models |
| `deps.py` | FastAPI dependency injection |
| `indexer.py` | Background tasks for memory/history indexing |

### Application Startup (`app.py`)

The `lifespan` context manager initializes:
1. `ManagerConfig.load()` → `app.state.config`
2. `SessionStore(project_dir)` → `app.state.store`
3. `AuthManager()` → `app.state.auth`
4. `ConnectionManager()` → `app.state.connections`
5. `MemoryWatcher` → Watches memory folder, indexes on change
6. `HistoryIndexer` → Periodic (120s) history indexing

**CORS:** Allows `http://localhost:5173` and `https://localhost:5173` (frontend dev server)

### SessionPool (`pool.py`)

Manages all active sessions, keyed by stable `local_id`.

**Key methods:**
- `create(config, local_id=None, resume_sdk_id=None, fork=False)` → Creates session, returns `local_id`
- `send(local_id, prompt)` → Yields events from session
- `has(local_id)` → Check if session exists
- `list_sessions()` → Returns list with both `session_id` (local) and `sdk_session_id`

Sessions are announced to watchers immediately on creation (no waiting for SDK ID).

### WebSocket Protocol (`routes/chat.py`)

Endpoint: `WS /api/sessions/chat`

**Client → Server messages:**
```json
{"type": "start", "local_id": "..."}                              // New session (frontend-generated UUID)
{"type": "start", "local_id": "...", "resume_sdk_id": "..."}     // Resume session
{"type": "start", "local_id": "...", "fork": true}               // Fork session
{"type": "send", "text": "..."}                                   // Send message
{"type": "command", "text": "/compact"}                            // Slash command
{"type": "interrupt"}                                              // Stop response
{"type": "stop"}                                                   // End session
```

**Server → Client messages:**
```json
{"type": "session_started", "session_id": "..."}
{"type": "status", "status": "connecting|interrupted|disconnected"}
{"type": "text_delta", "text": "..."}
{"type": "text_complete", "text": "..."}
{"type": "thinking_delta", "text": "..."}
{"type": "thinking_complete", "text": "..."}
{"type": "tool_use", "tool_use_id": "...", "tool_name": "...", "tool_input": {...}}
{"type": "tool_result", "tool_use_id": "...", "output": "...", "is_error": false}
{"type": "turn_complete", "cost": 0.01, "num_turns": 1, "session_id": "..."}
{"type": "compact_complete", "trigger": "manual"}
{"type": "error", "error": "...", "detail": "..."}
{"type": "session_stopped"}
```

**Error types:** `invalid_json`, `not_started`, `start_timeout`, `start_failed`, `send_failed`, `command_failed`, `unknown_type`

**Timeout:** 30s for `session.start()`

### REST Endpoints (`routes/sessions.py`, `routes/auth.py`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/sessions` | List all sessions |
| GET | `/api/sessions/{id}` | Get session detail with messages |
| GET | `/api/sessions/{id}/preview?max=5` | Get last N messages |
| DELETE | `/api/sessions/{id}` | Delete session |
| GET | `/api/auth/status` | Check if authenticated |
| POST | `/api/auth/login` | Trigger OAuth login |

### ConnectionManager (`connections.py`)

Tracks active (local_id → WebSocket) pairs for WebSocket routing.

- `connect(local_id, ws)` → Register connection
- `disconnect(local_id)` → Cleanup
- `is_active(local_id)` → Check if connected
- `active_count` → Number of active connections

### Event Serialization (`serializers.py`)

`serialize_event(event)` converts manager Event → dict for JSON:
- `TextDelta` → `{"type": "text_delta", "text": "..."}`
- `ToolUse` → `{"type": "tool_use", "tool_use_id": "...", ...}`
- etc.

### Background Indexers (`indexer.py`)

**MemoryWatcher:**
- Uses `watchfiles` to monitor memory directory
- Triggers `context/scripts/index-memory.py --memory-only` on .md changes
- Debounced 1s

**HistoryIndexer:**
- Runs every 120s
- Hashes session file names/sizes/mtimes
- Only re-indexes if changed
- Triggers `context/scripts/index-memory.py --history-only`

---

## 3. Frontend (`frontend/src/`)

React application for the chat interface.

### Key Files

| File | Purpose |
|------|---------|
| `context/TabsContext.tsx` | Tab state management (open/close/switch tabs) |
| `hooks/useChatInstance.ts` | Per-tab chat state machine (reducer + WebSocket) |
| `hooks/useWebSocket.ts` | WebSocket connection management |
| `hooks/useSessions.ts` | Session list from REST API |
| `api/websocket.ts` | `ChatSocket` class - low-level WS |
| `api/rest.ts` | REST API client functions |
| `types.ts` | TypeScript type definitions |
| `App.tsx` | Root component, tab creation |
| `components/ChatPanelContainer.tsx` | Renders active tab's ChatPanel |
| `components/ChatPanel.tsx` | Main chat layout |
| `components/Sidebar.tsx` | Session history list, opens tabs |
| `components/Message.tsx` | Individual message rendering |
| `components/ToolUseBlock.tsx` | Tool use display |

### Tab Management (`context/TabsContext.tsx`)

Multi-tab system using `useReducer`. Each tab is identified by a stable `local_id` (UUID generated by the frontend via `crypto.randomUUID()`).

**Tab state:**
```typescript
interface TabState {
  sessionId: string;     // Stable local UUID (never changes)
  title: string;
  status: SessionStatus;
  connectionState: ConnectionState;
  isOrchestrator?: boolean;
  resumeSdkId?: string;  // SDK session ID for resuming from history
}
```

**Actions:** `OPEN_TAB`, `CLOSE_TAB`, `SWITCH_TAB`, `UPDATE_TAB`

**Key helpers:**
- `openTab(sessionId, title?, isOrchestrator?, resumeSdkId?)` → Open new tab with stable UUID
- `findTabByResumeId(sdkId)` → Find tab that's resuming a specific SDK session

### Per-Tab Chat State (`hooks/useChatInstance.ts`)

Each tab gets its own `useChatInstance` hook. Uses `useReducer` for predictable state updates.

**Options:**
```typescript
{ localId, resumeSdkId, isOrchestrator }
```

**State shape:**
```typescript
interface ChatState {
  messages: ChatMessage[];
  status: SessionStatus;  // idle, streaming, thinking, tool_use, etc.
  sessionId: string | null;
  cost: number;
  turns: number;
  error: string | null;
}
```

**Action types:**
- `RESET` → Clear state
- `LOAD_HISTORY` → Populate from REST API (converts `MessagePreview[]` → `ChatMessage[]`)
- `SESSION_STARTED` → Set sessionId, status=idle
- `USER_MESSAGE` → Add user message immediately
- `TEXT_DELTA` / `TEXT_COMPLETE` → Build/finalize text blocks
- `THINKING_DELTA` / `THINKING_COMPLETE` → Build/finalize thinking blocks
- `TOOL_USE` → Add tool_use block (pending)
- `TOOL_RESULT` → Attach result to matching tool_use block by `toolUseId`
- `TURN_COMPLETE` → Update cost/turns, status=idle
- `STATUS` / `ERROR` → Update status or error

**WebSocket start message:** Sends `{ type: "start", local_id, resume_sdk_id }` — the backend uses `local_id` as the pool key and `resume_sdk_id` to resume the Claude SDK session.

**Message building logic:**
- `ensureAssistantMessage()` → Creates assistant message if needed
- `updateLastAssistantBlock()` → Updates blocks in last assistant message
- Streaming text appends to existing block with `streaming: true`

**Hook returns:**
```typescript
{
  messages, status, connectionState, sessionId, cost, turns, error,
  send, command, interrupt, startSession, stopSession
}
```

### WebSocket Handling (`hooks/useWebSocket.ts`, `api/websocket.ts`)

**ChatSocket class:**
- Constructs WS URL from current location (`ws://` or `wss://`)
- Handles binary (arraybuffer) and text frames
- Parses JSON, silently ignores malformed
- Emits `status: disconnected` on close, `error: websocket_error` on error

**useWebSocket hook:**
- Manages connection lifecycle based on `active` boolean
- Tracks `connectionState`: connecting → connected → disconnected/error
- Returns `send()`, `close()`, `connectionState`

### Session Flow

**Starting new session:**
1. User clicks "+" → `App` generates `localId = crypto.randomUUID()`
2. `openTab(localId, "New session")` → tab appears with stable ID
3. `ChatPanelContainer` mounts `useChatInstance({ localId })`
4. On WS open, send `{"type": "start", "local_id": localId}`
5. Backend creates session in pool keyed by `localId`
6. Receive `{"type": "session_started", "session_id": localId}` → status=idle
7. Tab ID stays the same throughout — no ID replacement needed

**Resuming session from sidebar:**
1. User clicks past session in `Sidebar`
2. `Sidebar` generates `localId = crypto.randomUUID()`, opens tab with `resumeSdkId` set to the history session's SDK ID
3. REST fetch `GET /api/sessions/{sdkId}` → get messages, dispatch `LOAD_HISTORY`
4. On WS open, send `{"type": "start", "local_id": localId, "resume_sdk_id": sdkId}`
5. Backend creates session with `resume_sdk_id` for Claude SDK continuation
6. Tab uses stable `localId`, `resumeSdkId` links it to the sidebar entry

**Sending message:**
1. User types, presses Enter
2. `send(text)` → dispatch `USER_MESSAGE` (optimistic)
3. WS send `{"type": "send", "text": "..."}`
4. Stream events arrive → dispatch each action
5. `TURN_COMPLETE` → status=idle, refresh session list

### Type System (`types.ts`)

**Server types (from API):**
- `SessionInfo`, `SessionDetail`, `MessagePreview`, `ContentBlock`

**WebSocket events:**
- `ServerEvent` union type with all possible event shapes

**Frontend types:**
- `ChatMessage` → `{id, role, blocks[]}`
- `MessageBlock` → text, thinking, or tool_use block
- `SessionStatus` → connecting, idle, streaming, thinking, tool_use, interrupted, disconnected
- `ConnectionState` → connecting, connected, disconnected, error

---

## Common Debugging Scenarios

### WebSocket Connection Issues

**Symptoms:** UI shows disconnected, messages not flowing

**Debug steps:**
1. Check browser console for WS errors
2. Verify backend is running on port 8000
3. Check backend logs for connection events
4. Look for CORS issues (should allow localhost:5173)

**Key breakpoints:**
- `api/routes/chat.py:20` → `chat_ws()` function
- `frontend/src/api/websocket.ts:23` → `onopen` handler

### Session Start Timeout

**Symptoms:** Error "Session start timed out" after 30s

**Debug steps:**
1. Check if Claude Code is authenticated: `GET /api/auth/status`
2. Check backend logs for SDK connection issues
3. Verify `CLAUDE_CONFIG_DIR` is set correctly
4. Try `claude auth status` in terminal

**Key locations:**
- `api/routes/chat.py:103` → 30s timeout
- `manager/session.py:90` → `start()` method

### Message Not Appearing

**Symptoms:** Sent message but no response

**Debug steps:**
1. Check WS connection state in UI status bar
2. Look for errors in browser console
3. Check if `send_failed` error received
4. Verify backend logs show message received

**Key locations:**
- `frontend/src/hooks/useChat.ts:403` → `send()` function
- `api/routes/chat.py:126` → `_handle_send()`

### Tool Results Not Attaching

**Symptoms:** Tool shows "pending" forever

**Debug steps:**
1. Check if `tool_result` event received (browser Network tab, WS frames)
2. Verify `tool_use_id` matches between `tool_use` and `tool_result`
3. Look at reducer logic for `TOOL_RESULT` action

**Key location:**
- `frontend/src/hooks/useChat.ts:235` → `TOOL_RESULT` case

### History Not Loading

**Symptoms:** Resume session but no messages

**Debug steps:**
1. Check REST response: `GET /api/sessions/{id}`
2. Verify session file exists in `$CLAUDE_CONFIG_DIR/projects/<mangled>/`
3. Check `LOAD_HISTORY` action dispatched (React DevTools)

**Key locations:**
- `frontend/src/hooks/useChat.ts:84` → `LOAD_HISTORY` case
- `manager/store.py:149` → `get_session()`

---

## Quick Reference: File Locations

**To understand message flow:**
1. `frontend/src/hooks/useChat.ts` - State machine
2. `api/routes/chat.py` - WebSocket handler
3. `manager/session.py` - SDK wrapper

**To understand data types:**
1. `manager/types.py` - Python event types
2. `api/serializers.py` - Event → JSON
3. `frontend/src/types.ts` - TypeScript types

**To understand session storage:**
1. `manager/store.py` - Read JSONL files
2. `api/routes/sessions.py` - REST endpoints

**To understand authentication:**
1. `manager/auth.py` - OAuth helpers
2. `api/routes/auth.py` - Auth endpoints
3. `frontend/src/components/AuthGate.tsx` - Auth UI

---

## Making Changes

When modifying the wrapper:

1. **Adding new event types:**
   - Add to `manager/types.py`
   - Handle in `manager/session.py:_process_message()`
   - Add to `api/serializers.py`
   - Add to `frontend/src/types.ts` ServerEvent union
   - Handle in `frontend/src/hooks/useChat.ts` reducer

2. **Adding new REST endpoints:**
   - Add route in `api/routes/`
   - Add Pydantic models in `api/models.py`
   - Add client function in `frontend/src/api/rest.ts`

3. **Adding new WebSocket messages:**
   - Handle in `api/routes/chat.py` message loop
   - Add to `frontend/src/types.ts` ServerEvent
   - Handle in `useChat` reducer

4. **Modifying session storage:**
   - Update `manager/store.py`
   - Update `api/models.py` if response shape changes
   - Update `frontend/src/types.ts` if needed
