# 01 — Backend API Contract (as-is)

> **Status:** normative inventory for the frontend rebuild (web `frontend/`, Safari-12 `frontend-compat/`, native `android/`).
> The backend (`api/`, `manager/`, `orchestrator/`) is **not** being changed by the rebuild. Everything below describes what the
> backend *actually does today* on branch `frontend-refactory`, verified by reading the code and, where marked **LIVE**, by
> read-only GET / WebSocket probes against the Jetson at `https://192.168.0.200` on 2026-10-03.
>
> Citations are `path:line` relative to the repo root (`/home/rodrigo/assistant`). When the code is ambiguous, the text says
> **AMBIGUOUS** explicitly. Items a fresh client is likely to trip on are tagged **⚠ GOTCHA** inline and collected again in §8.

---

## Table of contents

1. [Transport, hosting and auth](#1-transport-hosting-and-auth)
2. [Identifiers glossary (read this first)](#2-identifiers-glossary-read-this-first)
3. [REST endpoints](#3-rest-endpoints)
4. [WebSocket: `/api/sessions/chat` (agent sessions)](#4-websocket-apisessionschat-agent-sessions)
5. [WebSocket: `/api/orchestrator/chat` (orchestrator)](#5-websocket-apiorchestratorchat-orchestrator)
6. [History / JSONL loading and merging with live streams](#6-history--jsonl-loading-and-merging-with-live-streams)
7. [Voice](#7-voice)
8. [Contract gotchas and recommended backend fixes](#8-contract-gotchas-and-recommended-backend-fixes)

---

## 1. Transport, hosting and auth

### 1.1 Process / network topology

| Layer | Production (Jetson, 192.168.0.200) | Dev (laptop) |
|---|---|---|
| App server | `uvicorn api.app:create_app --factory --host 127.0.0.1 --port 8765` (systemd unit `agentic-backend.service`, not in repo) | `context/scripts/run.sh -m uvicorn api.app:create_app --factory --host 0.0.0.0 --port 8765` (CLAUDE.md) |
| TLS / public port | **nginx** (custom config `~/nginx-server.conf` on the Jetson, not in repo) terminates TLS on **:443** with a self-signed cert and also serves plain HTTP on **:80**; both `proxy_pass http://127.0.0.1:8765` | Vite dev server on `:5432` (`frontend/vite.config.ts:15-18`), HTTPS only if `context/certs/{key,cert}.pem` exist (`frontend/vite.config.ts:6-7,19-21`); proxies `/api` (incl. WS, `ws: true`) to `http://localhost:8765` (`frontend/vite.config.ts:22-27`) |
| WebSocket upgrade | nginx sets `Upgrade`/`Connection` via a `map`, `proxy_http_version 1.1`, `proxy_buffering off`, `proxy_read/send/connect_timeout 86400` | Vite proxy |

nginx facts (read from the Jetson's `~/nginx-server.conf`): `listen 443 ssl; server_name 192.168.0.200 server.local; ssl_protocols TLSv1.2 TLSv1.3;` and an identical `listen 80` block. `X-Real-IP` is forwarded. **No `client_max_body_size` is set**, so nginx's default of **1 MiB** applies to every request body (see §8, G-1).

- **Base URL convention.** Clients address the backend by origin only (`https://<host>` or `http://<host>`); every API path starts with `/api/…`. The web client derives the WS origin from `location` (`frontend/src/api/websocket.ts:14-15`: `wss:` when the page is `https:`). The Android client stores a configurable base URL and strips/rewrites `ws(s)://` ↔ `http(s)://` and trailing `/api/orchestrator[/chat]` / `/api/sessions/chat` segments (`android/app/src/main/java/com/assistant/peripheral/network/ApiClient.kt:41-52`).
- **HTTPS.** Self-signed certificate. Browsers need a one-time trust exception; Android must use a trust-all / pinned trust manager. Microphone (`getUserMedia`) and WebRTC require a secure context in browsers, so the web app must be loaded over `https://` (or `localhost`).
- **Frame encoding (all WebSockets).** **Every server→client frame is a *binary* frame** containing UTF-8 JSON (`ws.send_bytes(orjson.dumps(...))` — `api/pool.py:1362`, `api/pool.py:564`, `api/routes/chat.py:141` etc., `api/routes/orchestrator.py:98`). **LIVE**-verified: frames arrive as `bytes`. Client→server frames **must be text** frames: both handlers call `ws.receive_text()` (`api/routes/chat.py:137`, `api/routes/orchestrator.py:132`); a binary client frame raises inside the handler and the socket is dropped without a close frame (**LIVE**: `ConnectionClosedError no close frame`). ⚠ GOTCHA G-2.
- **Heartbeats.** The server sends no application-level pings. On the orchestrator WS, client frames `{"type":"ping"}` / `{"type":"pong"}` are accepted and silently ignored (`api/routes/orchestrator.py:147-148`). On the chat WS, `ping` is **not** recognised and returns `error: unknown_type` (`api/routes/chat.py:254-258`). Rely on WebSocket-protocol pings (OkHttp `pingInterval`, browser built-in) for keep-alive. nginx allows 24 h idle.

### 1.2 CORS

`CORSMiddleware(allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])` (`api/app.py:154-159`). No credentials mode is configured (`allow_credentials` defaults to False), which is fine because **the API uses no cookies**.

> **Superseded 2026-10-09:** CORS now echoes only trusted origins and a browser-origin guard refuses cross-site writes and WebSocket handshakes — see [backend.md](../../../architecture/backend.md#auth-and-the-browser-origin-guard).

### 1.3 Authentication — there is none for clients

- **There is no user/client authentication on any REST or WebSocket endpoint** except the browser-extension channel (§3.13). No tokens, cookies or headers are required. The trust model is "anyone on the LAN / tailnet". `GET /api/config/openai-key` even hands out the raw OpenAI key (`api/routes/config.py:304-322`).
- `/api/auth/*` (`api/routes/auth.py`) is about whether **the backend's bundled Claude Code CLI** has valid Anthropic OAuth credentials — not about the client. The web `AuthGate` (`frontend/src/components/AuthGate.tsx:15-19`) calls `GET /api/auth/status` on mount and, if `authenticated` is false, shows a screen to trigger `POST /api/auth/login` (non-headless only) or paste a `.credentials.json` into `POST /api/auth/credentials`. Endpoint details in §3.1.
- Headless detection: `HEADLESS=1|true|yes` env, or no `DISPLAY` on non-Windows (`api/app.py:46-49`). The Jetson is headless (**LIVE**: `{"authenticated":true,"auth_url":null,"headless":true}`).

### 1.4 Static file serving (non-`/api` paths)

Registration order matters — earlier routes win (`api/app.py:161-302`). All `/api/*` routers are registered first (`api/app.py:161-174`).

| URL | Serves | Code | Notes |
|---|---|---|---|
| `/compat`, `/compat/` | `frontend-compat/dist/index.html` with `Cache-Control: no-cache, no-store, must-revalidate` | `api/app.py:183-190` | Only if `frontend-compat/dist` exists at startup |
| `/compat/assets/*` | `frontend-compat/dist/assets` (StaticFiles) | `api/app.py:185` | Hashed, cacheable |
| `/compat/{path}` | file under `frontend-compat/dist` if it exists, else compat `index.html` (no-cache) | `api/app.py:192-197` | SPA fallback |
| `/projects/{path}` | file under repo `projects/` (traversal-guarded) else 404 | `api/app.py:212-225` | Only if `projects/` exists at startup |
| `/uploads/{path}` | file under `context/uploads/` (traversal-guarded, resolved per request) else 404 | `api/app.py:231-242` | Target of `POST /api/uploads` URLs |
| `/memory`, `/memory/` | `context/memory/MEMORY.md` (`Content-Type: text/markdown; charset=utf-8`, **LIVE**) | `api/app.py:251-257` | Only if `context/memory` exists at startup |
| `/memory/{path}` | raw file under `context/memory/` (traversal-guarded) else 404; **no directory listings** | `api/app.py:259-271` | Has `etag` + `last-modified` (Starlette `FileResponse`, **LIVE**) |
| `/assets/*` | `frontend/dist/assets` | `api/app.py:276` | Only if `frontend/dist` exists |
| `/` | `frontend/dist/index.html` (no-cache) | `api/app.py:279-281` | |
| `/{path}` (catch-all) | 1) file under `context/public/{path}` → 2) file under `frontend/dist/{path}` → 3) `frontend/dist/index.html` (no-cache) | `api/app.py:283-302` | **Visualizations are served by this catch-all.** |

- ⚠ GOTCHA: unknown paths return **200 + `index.html`**, never 404 (SPA fallback, `api/app.py:301-302`). A client probing whether a visualization URL exists must check `Content-Type`/body, not status.
- ⚠ GOTCHA: `context/public/` (visualizations, photo-server, downloads) is only served **inside the `if frontend_dist.exists()` block** (`api/app.py:275-302`). A backend without a built `frontend/dist` serves no visualizations at all.
- ⚠ GOTCHA: none of these routes answer `HEAD` (FastAPI `@app.get` only) → `HEAD` returns `405` JSON. Use `GET` (or `GET` with `Range`) for existence checks.
- **PWA files** (`/manifest.json`, `/sw.js`, `/icon-*.png`, `/icon.svg`, `/pcm-capture-worklet.js`) are **owned by the web frontend build** (`frontend/public/` → `frontend/dist/`), served through the catch-all. The backend has no manifest/service-worker logic of its own. Current `sw.js` only does network-first for navigations with an offline fallback to cached `/index.html` (`frontend/public/sw.js:24-32`).

---

## 2. Identifiers glossary (read this first)

The single biggest source of client bugs is that the field name `session_id` means different things in different payloads.

| Concept | What it is | Where it appears |
|---|---|---|
| **`local_id`** | Stable UUID minted **by the client** for a tab/conversation view; the **pool key**. Never changes across reconnects or backend restarts (as long as the client keeps it). | Chat/orchestrator `start` messages (`local_id`); chat `session_started.session_id`; orchestrator `session_started.session_id`; `GET /api/sessions/pool/live[].local_id`; `GET /api/sessions[].local_id`; watcher events `agent_session_opened/closed.session_id`; `nested_session_event.session_id`; `voice_owner_active.owner_local_id`; voice lifecycle `voice_ending/voice_ended.session_id`. |
| **`sdk_session_id`** | Provider-assigned conversation id (Claude Code SDK id / Qwen / Gemini id). For Claude it is also the **JSONL filename stem** (`context/<id>.jsonl`) and the key for history REST endpoints, titles and per-session config. May be unknown (`null`) for a brand-new session until the CLI reports it. | `start.resume_sdk_id` (input); `turn_complete.session_id` (chat WS); `session_terminated.sdk_session_id`; `GET /api/sessions[].session_id`; `GET /api/sessions/pool/live[].sdk_session_id`; `agent_session_opened.sdk_session_id`. |
| **orchestrator `jsonl_id`** | Filename stem of the orchestrator's JSONL: `resume_sdk_id` if resuming, else the orchestrator's `local_id` (`orchestrator/session.py:218-219,321-323`). Plays the role of `sdk_session_id` for the orchestrator. | `GET /api/sessions/pool/live[].sdk_session_id` for the orchestrator row; `agent_session_opened.sdk_session_id` (orchestrator). **Not** in orchestrator `session_started`. |
| **`stream_id` / `seq`** | Resume-protocol cursor for **Claude agent sessions only**: `stream_id = "<local_id>:<epoch_ms>"` minted each time the SDK subprocess (re)connects; `seq` is a per-stream monotonic integer assigned to every SDK-derived event (`manager/claude/session.py:365-373,745-774`). | Stamped on most chat-WS event payloads (§4.4); `session_started.resume_state`; `start.resume_from`. |
| **`tool_use_id`** | Provider tool-call id (`toolu_…` for Claude, OpenAI `call_…`, Gemini call id). Joins `tool_use` ↔ `tool_result` (and `tool_executing`/`tool_progress` on the orchestrator). | Live events and history blocks. |
| **`request_id`** | UUID4 for one permission prompt (`manager/base_session.py:341`). | `permission_request`, `permission_resolved`, `permission_response` (client). |
| **`turn_id`** | Orchestrator background-runner turn id (fire-and-forget delegation). | Only in orchestrator JSONL `background_notification` lines and orchestrator tool outputs; not a client protocol field. |

**Rules a client must follow**

1. Mint `local_id` (UUID v4) per tab and persist it together with the last known `sdk_session_id`.
2. Use `local_id` for all WebSocket `start` calls and for `POST /api/sessions/{local_id}/close`.
3. Use `sdk_session_id` (or orchestrator `jsonl_id`) for **every** history REST call (`/api/sessions/{id}`, `/messages`, `/preview`, `/rename`, `/config`, `/duplicate`, `/truncate`, `/fork`, `DELETE`). These endpoints do **not** resolve `local_id` (§8 G-12).
4. Learn the `sdk_session_id` of a new chat session from the first `turn_complete.session_id`, or from `GET /api/sessions/pool/live`, or from the watcher event `agent_session_opened.sdk_session_id` (orchestrator WS only). `session_started` on the chat WS does **not** carry it.

---

## 3. REST endpoints

Conventions for this section:

- All bodies are JSON unless stated. FastAPI validation failures return **422** `{"detail":[{loc,msg,type,...}]}`; `HTTPException`s return `{"detail": "<string>"}` with the stated status.
- "Untyped body" means the handler declares `body: dict` — any JSON object is accepted and fields are read with `.get()`; wrong types surface as 400 only where explicitly checked.
- Timestamps are ISO-8601 strings with offset (`+00:00`).

### 3.1 Auth (Claude CLI credentials) — `api/routes/auth.py`

| Method & path | Params / body | Response | Errors / side effects |
|---|---|---|---|
| `GET /api/auth/status` (`:14-26`) | — | `AuthStatusResponse` `{authenticated: bool, auth_url: str\|null, headless: bool}` (`api/models.py:93-98`). `auth_url` is set only when `authenticated=false` and is the constant `https://console.anthropic.com/settings/workspaces/default/oauth_tokens` (`manager/auth.py:51,147-153`). | Non-headless: runs `claude auth status` (≤10 s) first (`manager/auth.py:62-77,100-119`); headless: only checks that the credentials file has `claudeAiOauth.accessToken` (no expiry check, `manager/auth.py:79-98`). |
| `POST /api/auth/login` (`:29-41`) | — | same shape | Headless → always `authenticated:false` immediately. Otherwise spawns `claude setup-token` **on the server** and blocks until it exits (`manager/auth.py:121-145`). |
| `POST /api/auth/credentials` (`:44-59`) | `{"credentials_json": "<full .credentials.json text>"}` (`api/models.py:101-103`) | `{authenticated: bool, auth_url: null, headless: bool}` | `authenticated:false` if the JSON lacks `claudeAiOauth.accessToken`. Side effect: overwrites `$CLAUDE_CONFIG_DIR/.credentials.json` (mode 0600) (`manager/auth.py:155-188`). |

### 3.2 Sessions — `api/routes/sessions.py` (prefix `/api/sessions`)

Shared response models (`api/models.py`):

```jsonc
// SessionInfoResponse (api/models.py:12-22)
{ "session_id": "uuid",            // sdk_session_id / jsonl_id (see §2) — NOT local_id
  "started_at": "2026-08-28T07:57:31.607864+00:00",
  "last_activity": "2026-08-28T21:18:36.502531+00:00",
  "title": "Lamp presets",        // custom title from .titles.json, else first user text[:100], else "(empty session)"
  "message_count": 190,            // count of raw user+assistant JSONL lines (includes tool_result wrappers)
  "is_orchestrator": true,         // true iff JSONL has an orchestrator_meta line
  "provider": "claude",            // "claude" | "qwen" | "gemini" (registered harness id)
  "local_id": null }               // set only if this session is live in the pool right now

// ContentBlockResponse (api/models.py:52-59)
{ "type": "text" | "tool_use" | "tool_result",
  "text": "…" | null,              // text blocks
  "tool_use_id": "toolu_…" | null, // tool_use + tool_result
  "tool_name": "Bash" | null,      // tool_use
  "tool_input": {…} | null,        // tool_use
  "output": "…" | null,            // tool_result (always a string; list content is joined/JSON-stringified)
  "is_error": false }

// MessagePreviewResponse (api/models.py:62-66)
{ "role": "user" | "assistant", "text": "…", "blocks": [ContentBlockResponse…], "timestamp": "…" | null }
```

| Method & path | Params / body | Response | Errors / side effects |
|---|---|---|---|
| `GET /api/sessions` (`:37-114`) | — | `SessionInfoResponse[]`, newest `last_activity` first (`manager/store.py:207`). Live pool sessions with no local JSONL yet are **prepended** with `title:"(active session)"`, `started_at=last_activity=now`, `message_count=turns`, `is_orchestrator:false`; if the session has no SDK id yet, `session_id` is the **local_id** (`:79-112`). | Scans `context/*.jsonl`, `context/chats/*.jsonl` and harness discoverers (Gemini) (`manager/store.py:127-208`). Per-file mtime cache; first call after boot can be slow (pre-warmed at startup, `api/app.py:77-85`). **LIVE**: 202 sessions; providers `claude`/`qwen`/`gemini`. |
| `GET /api/sessions/pool/live` (`:117-165`) | — | `PoolSessionResponse[]` `{local_id, sdk_session_id: str\|null, status, cost: float, turns: int, title: str\|null, is_orchestrator}` (`api/models.py:82-90`). Orchestrator row first (if any): `sdk_session_id=jsonl_id`, **hard-coded** `status:"idle"`, `cost:0.0`, `turns:0`, `title` = stored title or `"Orchestrator"`. Agent rows: `status` ∈ `idle\|streaming\|tool_use\|thinking\|interrupted\|disconnected` (`manager/types.py:15-23`). | Read-only. **LIVE** example: `[{"local_id":"3540ff69-…","sdk_session_id":"528dbf6f-…","status":"idle","cost":0.0,"turns":0,"title":"twitter browser test","is_orchestrator":false}]`. |
| `GET /api/sessions/{id}/config` (`:168-172`) | `id` = sdk_session_id | `{"working_directory": str\|null, "enabled_mcps": str[]\|null, "chrome_extension": bool\|null, "provider": str\|null, "harness_model": str\|null}` — always all 5 keys; `null` = inherit global (`api/routes/session_config.py:217-255`). | Never 404s (missing file → defaults). File: `context/<id>.config.json`. |
| `PUT /api/sessions/{id}/config` (`:175-179`) | Untyped body; only the 5 keys above are kept (`session_config.py:258-270`) | Merged config (same shape) | No validation of values. Write errors are logged and swallowed (still 200). **Does not affect a live session** — takes effect on the next `start` (client implements "Save & Restart" = close + start again). |
| `GET /api/sessions/{id}` (`:182-204`) | `id` = sdk_session_id | `SessionDetailResponse` = SessionInfo fields + `messages: MessagePreview[]` (**all** messages, unpaginated) | 404 `{"detail":"Session 'x' not found"}`. ⚠ `title` here ignores custom titles (always first user text) and `is_orchestrator` is always `false` (`manager/store.py:238-247`). `local_id` always null. Prefer `/messages`. |
| `GET /api/sessions/{id}/messages` (`:207-240`) | Query: `limit` int 1–200 (default 50); `before` int (optional) = load messages with index `< before` | `PaginatedMessagesResponse` `{messages: MessagePreview[], total_count: int, has_more: bool, start_index: int}` (`api/models.py:73-79`). Without `before`: the last `limit` messages. `start_index` = absolute index of `messages[0]`. `has_more` = `start_index > 0` (`manager/store.py:286-296`). | 404 if no JSONL. Pagination cursor: pass `before=<start_index of the oldest page you hold>`. See §6. **LIVE** verified. |
| `GET /api/sessions/{id}/preview` (`:243-260`) | Query `max` int 1–50 (default 5) | `MessagePreview[]` — last N | 404 if session missing. |
| `PATCH /api/sessions/{id}/rename` (`:263-269`) | Untyped body `{"title": "…"}` | **204** no body | 400 `title is required` (empty/whitespace); 404 if JSONL missing. Writes `context/.titles.json`. **No broadcast** — other clients only see it on next `GET /api/sessions`. |
| `DELETE /api/sessions/{id}` (`:272-289`) | — | **204** | 404 if missing. Moves the JSONL to `context/trash/` (soft delete) and drops its title; vector-index cleanup runs in the background (`manager/store.py:329-359`). **Does not close a live pool session** and does not broadcast. |
| `POST /api/sessions/{id}/duplicate` (`:314-328`) | — | **201** `{"session_id": "<new uuid>"}` | 404. Copies JSONL + title (`"<title> (copy)"`) (`manager/store.py:361-384`). |
| `POST /api/sessions/{id}/truncate` (`:331-360`) | Untyped body `{"drop_last_n": int ≥ 0}` — counted **from the end** in *visible* messages (user lines that are only `tool_result` wrappers are not counted) | **200** `{"session_id": id}` | **409** `Session is currently open. Close the tab before rewinding.` if live in the pool (matches sdk id, local_id, orchestrator jsonl_id or orchestrator local_id, `:292-311`). 400 if `drop_last_n` missing/not int/negative; 404 if not found or out of range. `drop_last_n=0` is a successful no-op. Trailing non-visible lines after the cut are dropped too (`manager/store.py:386-480`). ⚠ "visible" counting ≠ REST message indices (REST counts tool_result wrappers too). |
| `POST /api/sessions/{id}/fork` (`:363-384`) | Untyped `{"drop_last_n": int ≥ 0}` | **201** `{"session_id": "<new uuid>"}` | 400/404 as above. Duplicate then truncate the copy; original untouched. |
| `POST /api/sessions/inject` (`:387-441`) | Untyped `{"text": str (non-empty), "local_id"?: str, "sdk_session_id"?: str}` (one of the ids required) | 200 `{"ok": true, "local_id": "…"}` | 400 missing text / ids; 404 if not live; 500 on failure. Side effect: identical to a chat `send` from an invisible client (`pool.send_or_queue`, no `source_ws`) → every subscriber receives `user_message` (§4.3). Used by out-of-band tools. |
| `POST /api/sessions/{local_id}/close` (`:444-475`) | path = **local_id** | **204** | Never 404s (unknown id is a no-op). If `local_id` is the orchestrator's: `pool.stop_orchestrator()` (watchers get `agent_session_closed`, orchestrator subscribers are dropped **without** any message). Else `pool.close(local_id)` → subscribers get `session_stopped` (no `session_terminated`, because no reason is passed) and watchers get `agent_session_closed` (`api/pool.py:331-429`). If the session was new (not resumed) and had 0 turns, its JSONL is deleted. |

### 3.3 Visualizations — `api/routes/visualizations.py` (prefix `/api/visualizations`)

Visualizations are **any `*.html` file anywhere under `context/public/`** (recursive `rglob`, `:336`). They are served by the SPA catch-all (§1.4) at `/<path>`.

| Method & path | Params / body | Response | Errors / side effects |
|---|---|---|---|
| `GET /api/visualizations` (`:381-383`) | — | `VisualizationInfoResponse[]` (`api/models.py:25-35`), sorted by `modified` desc (`:374-377`) | Never errors (missing dir → `[]`). No cache, no watcher, no push events — clients refetch. |
| `PATCH /api/visualizations/rename` (`:386-407`) | Untyped `{"path": "<relative path>", "title": "<new title>"}` (path in body so slashes need no escaping) | **204** | 400 `path is required` / `title is required`; 404 if not an existing file under `context/public/` (traversal-guarded). Stores title in `context/.titles.json` under key `viz:<path>`. No broadcast. |

Item shape (**LIVE**, 26 items):

```json
{ "path": "tarot-canvas/index.html",          // relative to context/public/ — the identity / join key
  "url": "/tarot-canvas/index.html",          // "/" + path, NOT percent-encoded
  "title": "Tarô Gamificado — Canvas de Progresso",
  "created": "2026-10-03T13:45:45.461139+00:00",  // ext4 birth time via `stat -c %W`, else mtime
  "modified": "2026-10-03T13:45:45.461139+00:00", // mtime (sort key)
  "size": 27851 }                             // bytes
```

- Title resolution order: `.titles.json["viz:<path>"]` → `<title>` tag within the first 8 KiB → prettified filename (`index.html` borrows its parent dir name) (`:217-221,253-284,358-362`).
- ⚠ `url` is not URL-encoded; a filename with spaces/`#`/`?` must be encoded by the client before use. ⚠ `created` falls back to mtime and, for files arriving via context-sync, can be the copy time; use `modified` for "recency".
- Rendering: the HTML is arbitrary, self-contained, often with inline JS. Clients load it in an iframe (web) / WebView (Android) pointed at `<origin><url>`. No sandboxing contract exists server-side.
- Deletion/creation of visualizations has no REST endpoint (files are produced by agents via `/create-viz`).

### 3.4 Memory — `api/routes/memory.py` + `api/app.py` static route

| Method & path | Params | Response | Notes |
|---|---|---|---|
| `GET /api/memory/tree` (`memory.py:198-206`) | — | `MemoryNodeResponse[]` (`api/models.py:38-49`) — recursive | Only `*.md` (case-insensitive) files; dot-entries skipped; symlinks resolving outside the tree skipped; directories with no markdown at any depth dropped; **directories before files**, each alphabetical case-insensitive (`memory.py:146-195`). `[]` if the dir is missing. No caching. |
| `GET /memory/{path}` (`api/app.py:259-271`) | `path` = node `path` from the tree | raw markdown, `text/markdown; charset=utf-8`, `etag`, `last-modified` (**LIVE**) | 404 JSON `{"detail":"Not Found"}` for missing files and for directories. |
| `GET /memory` or `/memory/` (`api/app.py:251-257`) | — | `MEMORY.md` | Root index. |

Node shape (**LIVE**, 13 roots):

```json
{ "name": "assistant", "path": "assistant", "is_dir": true,
  "children": [
    { "name": "android", "path": "assistant/android", "is_dir": true, "children": [
        { "name": "android_peripheral_project.md", "path": "assistant/android/android_peripheral_project.md",
          "is_dir": false, "children": null } ] } ] }
```

- `path` is POSIX, relative to `context/memory/`, and is both identity and URL suffix. Files have `children: null` (the key is present).
- **Memory is read-only over HTTP.** There is no write/rename/delete endpoint and no search endpoint (semantic search exists only as orchestrator tools / `/recall`). Memory files carry YAML frontmatter and relative markdown links (`[x.md](../folder/x.md)`) — a renderer must resolve relative links against the current file's `path` and route them back through `/memory/<resolved>`.

### 3.5 Uploads — `api/routes/uploads.py`

| Method & path | Body | Response | Errors |
|---|---|---|---|
| `POST /api/uploads` (`:57-122`) | `multipart/form-data` with one field **`file`** (`UploadFile = File(...)`) | `{"filename": "<sanitized basename>", "path": "<absolute server path>", "url": "/uploads/<YYYYmmddTHHMMSSffffff>-<name>", "size": int, "content_type": "<client type or application/octet-stream>"}` | 422 if `file` missing; **413** `File exceeds the 200 MB upload limit.` (streamed count, `:32,91-100`); 500 `Upload failed: …`. ⚠ Through nginx, bodies > **1 MiB** are rejected by nginx with an HTML 413 before reaching the app (confirmed in the Jetson nginx error log: `client intended to send too large body: 2862307 bytes … POST /api/uploads`). |

Filename sanitisation: basename only, `[^A-Za-z0-9._-]+ → _`, strip leading/trailing `._-`, fallback `upload` (`:41-54`). The file is then reachable at `GET /uploads/<stored name>` (§1.4). The established client flow (Android share sheet) is: upload → send the returned `path`/`url` to the orchestrator as text via `inject_text` (§5.2).

### 3.6 Global config — `api/routes/config.py` (prefix `/api/config`)

Config lives in `<repo>/assistant_config.json` (`:21-26`).

| Method & path | Params / body | Response | Errors / side effects |
|---|---|---|---|
| `GET /api/config` (`:270-273`) | — | Full config object (below), with missing keys filled from defaults and legacy working-dir entries migrated (`:29-67`) | Never errors (bad file → defaults). |
| `PUT /api/config` (`:587-812`) | `ConfigUpdate` (all fields optional, `:242-263`) — partial update | Full updated config | 400 for: unknown working dir id; local working dir path that doesn't exist; unknown `provider`; unknown `harness_model` key or non-string value; empty `default_model`; missing SDK for chosen model/voice; unresolvable voice provider/model/voice/lang; unknown `default_voice_endpoint`; `voice_vad_threshold ∉ [0.15,0.50]`; `voice_vad_min_silence_ms ∉ [800,5000]`; `voice_mic_gain ∉ [0.5,2.0]`. **No broadcast** to other clients. |
| `GET /api/config/openai-key` (`:304-322`) | — | `{"api_key": "sk-…"}` | 404 if not configured. Used by Android's direct Whisper wake-word confirmation. |
| `GET /api/config/providers` (`:325-339`) | — | `{"providers":[{"id":"claude","label":"Claude Code","description":"…"},{"id":"qwen",…},{"id":"gemini",…}]}` (**LIVE**) | Session harnesses (not voice providers). |
| `GET /api/config/harness/qwen/models` (`:342-353`) | — | `{"models":[…]}` from `~/.qwen/settings.json` (`manager/qwen/models.py`) | `[]` if none. |
| `GET /api/config/voice/google/models` (`:407-450`) | Query `endpoint` = `vertex`\|`aistudio` (default resolved via `resolve_endpoint_id`) | `{"models":[{id,label,voice,voices:[{id,label,description}],transcription_languages:[],default_transcription_language:"",default:bool,description}]}`; first entry `default:true` | Never errors (`[]` on failure). 60 s in-memory cache per backend. |

Config object keys (defaults from `:109-163`; **LIVE** values in comments):

```jsonc
{
  "working_directory": "192.168.0.28:/home/rodrigo/assistant", // active entry *id*
  "working_directory_history": [   // ≤ 20 entries
    {"id": "/home/rodrigo/assistant", "path": "/home/rodrigo/assistant", "label": "Jetson (local)",
     "ssh_host": null, "ssh_user": null, "ssh_key": null, "claude_config_dir": null},
    {"id": "192.168.0.28:/home/rodrigo/assistant", "path": "/home/rodrigo/assistant", "label": "Laptop (Desktop)",
     "ssh_host": "192.168.0.28", "ssh_user": "rodrigo", "ssh_key": null,
     "claude_config_dir": "/home/rodrigo/assistant/.claude_config"}],
  "enabled_mcps": ["chrome-devtools"],   // [] = all enabled (legacy)
  "chrome_extension": true,              // passes --chrome to Claude CLI (NOT the browser-extension/ feature)
  "provider": "claude",                  // default session harness
  "default_model": "gpt-audio-mini",     // ⚠ stored but not read by the orchestrator backend (see §8)
  "summarizer_model": "",
  "harness_model": {"claude": "", "qwen": ""},  // ⚠ keys only for harnesses present when the file was first written
  "default_voice_provider": "openai",
  "default_voice_model": "gpt-realtime-2",
  "default_voice_name": "cedar",
  "default_voice_transcription_language": "",   // "" = auto
  "default_voice_endpoint": "aistudio",         // google provider only: "vertex" | "aistudio"
  "voice_recording_enabled": false,
  "voice_vad_threshold": 0.28,
  "voice_vad_min_silence_ms": 1800,
  "voice_mic_gain": 1.0
}
```

PUT semantics worth knowing:
- `working_directory_history` is a **full replacement**; SSH entries get `id = "<host>:<path>"` and `claude_config_dir = "<path>/.claude_config"` auto-derived; local paths must exist on the server (`:592-615`).
- `harness_model` is a **shallow merge** by provider key (`:641-663`).
- Voice defaults **cascade**: changing provider snaps model/voice/lang to that provider's defaults unless also given; changing model snaps voice/lang; changing voice or lang alone preserves the other (`:691-756`).

### 3.7 Skills, agents, MCP discovery

| Method & path | Response | Code |
|---|---|---|
| `GET /api/skills` | `{"skills":[{"name","description","dir"}]}` — one per subdirectory of `context/skills/` (symlinks resolved) containing `SKILL.md`; sorted by entry name | `api/routes/skills.py:38-58` |
| `GET /api/agents` | `{"agents":[{"name","description","file"}]}` — one per `*.md` in `context/agents/` | `api/routes/agents.py:37-57` |
| `GET /api/mcp/servers` | `{"servers": {"<name>": {"type":"stdio","command":…,"args":[…],"env":{…}} , …}, "project_dir": "/home/rodrigo/assistant"}` | `api/routes/mcp.py:26-37` |
| `GET /api/mcp/servers/{name}` | `{"name": "<name>", "config": {…}}`; 404 `MCP server '<name>' not found` | `api/routes/mcp.py:40-49` |

These are read-only discovery lists used by the config / per-session gear panels. `name`/`description` come from frontmatter via `utils.paths.parse_md_frontmatter` (fallback: dir/file stem).

### 3.8 Orchestrator models & voice REST — `api/routes/voice.py`

| Method & path | Params / body | Response | Errors / side effects |
|---|---|---|---|
| `GET /api/orchestrator/models` (`:232-250`) | — | `{"models": ModelInfo[], "audio_capable_models": str[], "default_model": "claude-sonnet-4-5-20250929"}` (default is **hard-coded**) | Live discovery with static fallback. `ModelInfo` = `{provider:"anthropic"\|"openai", model_id, display_name, supports_audio, supports_vision, supports_tools, max_tokens, context_window:int\|null}` (`orchestrator/config.py:42-54`). **LIVE** verified. |
| `GET /api/orchestrator/models/audio` (`:253-264`) | — | `{"models": ModelInfo[]}` (audio-capable only) | |
| `GET /api/orchestrator/voice/models` (`:120-137`) | — | `{"providers": {"<provider_id>": VoiceModelEntry[]}, "default_provider": str, "default_model": str}` | See §7 for `VoiceModelEntry`. |
| `POST /api/orchestrator/voice/session` (`:40-117`) | **Query** params (not body): `provider`, `model`, `voice`, `transcription_language`, `endpoint` — all optional | `{"connection_info": {…}}` plus, for `openai` only, legacy `{"client_secret":{"value","expires_at"},"model","voice"}` | 400 bad target; 503 missing key/config; 502 upstream error. Mints a provider credential; see §7. |
| `POST /api/orchestrator/audio` (`:140-229`) | `multipart/form-data`: `audio` (file; ext/content-type ∈ wav,mp3,webm,ogg,m4a,flac), optional form field `text` | `{"status":"queued","audio_format":"webm","size_bytes":12345}` | 400 unsupported/empty/**no active orchestrator**; 413 > 25 MB. ⚠ It does **not** process the audio: it only broadcasts `{"type":"audio_upload","audio":<base64>,"format","text","size_bytes"}` to orchestrator subscribers (`:217-223`); no server code consumes that. Effectively dead/legacy — use the WS `send_audio` message instead. |

### 3.9 Debug / remote console — `api/routes/debug.py`

| Method & path | Body | Response |
|---|---|---|
| `POST /api/debug/log` (`:16-27`) | `{"level": "log"\|"warn"\|"error"\|…, "msg": str, "ts"?: str}` | **204** always (errors swallowed). Appends `[ts] [LEVEL] msg` to `<repo>/remote_console.log`. Used by compat/legacy devices for remote console. |
| `GET /api/debug/log` (`:30-34`) | — | `text/plain` whole log file, or `No logs yet.\n`. Unbounded size. |

### 3.10 Browser-control extension — **OUT OF SCOPE** (listed for completeness)

`api/routes/browser.py` — used by `browser-extension/` and `context/scripts/browser_cmd.py`, not by any frontend.

| Endpoint | Notes |
|---|---|
| `GET /api/browser/status` (`:248-254`) | status + `token_configured` |
| `POST /api/browser/command` (`:257-323`) | loopback-only (403 when proxied from LAN, `:274-283`) + `X-Browser-Token` header (401) |
| `WS /api/browser/ws` (`:326-…`) | extension hub, token-authenticated |

The rebuilt frontends must not call these.

---

## 4. WebSocket: `/api/sessions/chat` (agent sessions)

Handler: `api/routes/chat.py:127-269`. One WS can be attached to **one session at a time** (per-connection variables `sm`, `session_id`), but see G-6 (re-`start` without `stop` leaks the old subscription). The session itself is owned by the pool (`api/pool.py`) and **outlives** the WebSocket: disconnecting only unsubscribes (`api/routes/chat.py:262-269`); the in-flight turn continues (`api/pool.py:157-163,965-1009`).

### 4.1 Client → server messages

All are text frames with a JSON object; dispatch on `type` (`api/routes/chat.py:146-258`).

| `type` | Fields | Behaviour |
|---|---|---|
| `start` | `local_id`: str (strongly recommended; if absent a UUID is generated server-side and returned as `session_started.session_id`); `resume_sdk_id`: str\|absent (alias: `session_id`, `:291`); `fork`: bool (default false); `mcp_servers`: object\|absent (literal `{name: config}` map — overrides the per-session/global MCP selection); `resume_from`: `{"stream_id": str, "seq": int}`\|absent | **(a)** If `local_id` is already in the pool → subscribe this WS and send `session_started` (+ replay, §4.5) immediately (`:296-301`). `resume_sdk_id`, `fork`, `mcp_servers` are ignored in this branch. **(b)** Else build config (`api/session_factory.build_session_config`, reads `assistant_config.json` + `context/<resume_sdk_id>.config.json`), send `status: connecting`, create the session (30 s timeout), subscribe, send `session_started` (`:303-375`). If `resume_sdk_id` is already live in the pool under a *different* local_id **and healthy**, the pool returns that existing session's local_id (dedupe, `api/pool.py:235-240`) — so `session_started.session_id` may differ from the `local_id` you sent. ⚠ If the resume id's SDK state is gone ("No conversation found") the pool silently starts a **fresh** session instead (`api/pool.py:267-287`). |
| `send` | `text`: str | Requires a prior successful `start` (else `error: not_started`). If any permission is pending on this session and `text` is non-empty, **every pending permission is resolved as `deny` with `message=text`** (`:164-174`). Then `pool.send_or_queue`: starts a turn if idle, otherwise **queues** it behind the running turn (`api/pool.py:1011-1067`). Never interrupts. |
| `interrupt` | — | `pool.cancel_turn`: drops all queued prompts, sends SDK interrupt, cancels the turn task (`api/pool.py:1191-1234`). Replies `{"type":"status","status":"interrupted"}` **to this WS only** (`:205-210`). No-op without a session (no reply, **LIVE**). |
| `command` | `text`: str (slash command, e.g. `/help`) | Cancels any running turn, then runs the command and streams its events **only to this WS**, unwrapped (no `seq`/`stream_id`), not via the pool lock (`:191-203,445-461`). Errors → `error: command_failed`. |
| `compact` | — | Cancels any running turn, then runs `/compact`; its events are **broadcast** to all subscribers (no `seq`/`stream_id`), ending with `compact_complete` (always emitted; synthesized with `summary:""` if the CLI didn't send one) (`:212-219,425-442`; `manager/claude/session.py:477-491`). Errors → `error: compact_failed` (this WS only). |
| `permission_response` | `request_id`: str; `decision`: `"allow"\|"deny"`; `message`?: str (rejection reason); `session_id`?: str (local_id; defaults to the attached session) | Resolves a pending permission (`:221-240`). First answer wins (user vs orchestrator); losers are no-ops. Invalid → `error: invalid_permission_response` (**LIVE**). The result is announced via the broadcast `permission_resolved`. |
| `stop` | — | Unsubscribes this WS from the session **without** stopping the agent (`:242-252`). Replies `session_stopped` to this WS. To actually kill a session use `POST /api/sessions/{local_id}/close`. |
| anything else | — | `{"type":"error","error":"unknown_type","detail":"Unknown message type: 'x'"}` |
| invalid JSON | — | `{"type":"error","error":"invalid_json"}` |

### 4.2 Server → client messages (complete list)

All binary frames, UTF-8 JSON. "Broadcast" = sent to every WS subscribed to that `local_id`. "Direct" = only to the WS that triggered it.

| `type` | Payload | Emitted when | Delivery | Code |
|---|---|---|---|---|
| `session_started` | `{session_id: <local_id>, context_window: int\|null, resume_state?: {stream_id: str, next_seq: int}, replay_overflow?: true}` | After every successful `start` | Direct | `api/routes/chat.py:378-422` |
| `status` | `{status: "connecting"}` | New session being created | Direct | `chat.py:341-343` |
| `status` | `{status: "processing"}` | A prompt was accepted and the SDK turn begins | Broadcast | `api/pool.py:921-924` |
| `status` | `{status: "retrying", detail: "upstream silent for Ns, retrying"}` | Turn produced zero SDK messages for 240 s; interrupted and retried once | Broadcast | `api/pool.py:1121-1124`; `manager/claude/session.py:118` |
| `status` | `{status: "interrupted"}` | Reply to client `interrupt` | **Direct only** | `chat.py:208-210` |
| `user_message` | `{text: str}` or `{text: str, queued: true}` | A prompt was dispatched (`pool.send`, excluding the sender WS) / queued behind a running turn (excluding the sender) | Broadcast minus sender | `api/pool.py:905-909,1062-1066` |
| `text_delta` | `{text}` | Streaming answer token | Broadcast (+seq) | `api/serializers.py:134-135` |
| `text_complete` | `{text}` | A text content block finished; `text` is the **full** block text | Broadcast (+seq) | `serializers.py:136-137` |
| `thinking_delta` | `{text}` | Streaming thinking token | Broadcast (+seq) | `serializers.py:138-139` |
| `thinking_complete` | `{text}` | A thinking block finished (full text; may be `""` for redacted/omitted thinking) | Broadcast (+seq) | `serializers.py:140-141` |
| `tool_use` | `{tool_use_id, tool_name, tool_input: object}` | A complete tool call block (input JSON fully assembled — input deltas are **not** streamed) | Broadcast (+seq) | `serializers.py:142-148` |
| `tool_result` | `{tool_use_id, output: str, is_error: bool}` | Tool finished. `output` is a string; list content is `json.dumps`-ed. ⚠ `tool_use_id` can be `""` in edge cases | Broadcast (+seq) | `serializers.py:149-155` |
| `turn_complete` | `{cost: float\|null, usage: object, input_tokens: int, output_tokens: int, num_turns: int, session_id: <sdk_session_id>, is_error: bool, result: str\|null}` — `input_tokens` = input + cache_read + cache_creation | End of one prompt's turn (the CLI's `ResultMessage`) | Broadcast (+seq) | `serializers.py:156-174` |
| `compact_complete` | `{trigger: "manual"\|"auto", summary: str}` | Compaction finished (manual or auto during a turn) | Broadcast | `serializers.py:175-176` |
| `session_stalled` | `{elapsed_seconds: float, last_tool_name: str\|null, last_tool_use_id: str\|null}` | No SDK message for 120 s, then every 60 s while silent. **Advisory** — the stream continues | Broadcast (no seq) | `serializers.py:177-183`; `manager/claude/session.py:109-110,577-591` |
| `permission_request` | `{request_id, tool_name, tool_input}` | A gated tool (currently only `ExitPlanMode`, `manager/claude/session.py:83`) needs approval; the SDK blocks until answered | Broadcast (+seq) **and** mirrored to orchestrator WS as `nested_session_event` | `serializers.py:184-190`; `api/pool.py:928-938` |
| `permission_resolved` | `{request_id, decision: "allow"\|"deny", responder: "user"\|"orchestrator"\|"system", message: str\|null}` | A permission was answered | Broadcast (+seq) + orchestrator mirror | `serializers.py:191-198` |
| `session_terminated` | `{reason: "subprocess_crashed"\|"subprocess_lost"\|"closed_by_user"\|"replaced"\|"unreachable", detail: str\|null, sdk_session_id: str\|null}` | Session removed from the pool **with a reason** (dead-session reaper, dead session hit during a turn). Always immediately followed by `session_stopped` | Broadcast | `api/pool.py:375-381`; `manager/types.py:142-200` |
| `session_stopped` | `{}` | Session removed from the pool (any close), or reply to client `stop` | Broadcast / Direct | `api/pool.py:382`; `chat.py:252` |
| `error` | `{error: code, detail?: str}` — codes: `invalid_json`, `not_started`, `unknown_type`, `invalid_permission_response`, `start_timeout`, `start_failed`, `send_failed`, `command_failed`, `compact_failed`, `upstream_wedged` | See §4.1 / turn failures | Direct, except `send_failed` and `upstream_wedged` from the turn task, which are **broadcast** | `chat.py` passim; `api/pool.py:1150-1156,1184-1187` |
| `unknown` | `{}` | Serializer fallback for an unrecognised event class | — | `serializers.py:206` |

There is **no** `status: idle` / `status: streaming` on this socket. A client derives "busy" from `status: processing` (or any content event) and "idle" from `turn_complete` with an empty local queue, `status: interrupted`, `session_stopped` or an `error`. `GET /api/sessions/pool/live` gives the authoritative `status` on (re)connect.

### 4.3 Turn lifecycle and multi-client semantics

```
client A: send{text}
  └─ if idle → pool.start task ─┐        if busy → queue; broadcast user_message{text,queued:true} to B,C (not A)
                                ▼
  [per-session lock acquired]  broadcast user_message{text} to B,C (not A)   ← api/pool.py:905
                               broadcast status{processing}                  ← api/pool.py:921
                               … text_delta / text_complete / thinking_* / tool_use / tool_result / permission_* / session_stalled …
                               turn_complete
  [lock released] → next queued prompt (if any) repeats from "user_message"
```

- **Sender echo suppression.** The WS that sent `send` never receives a `user_message` for it (it is expected to render its own message optimistically). All other subscribers do.
- ⚠ **Queued prompts are announced twice to non-sender subscribers**: once at queue time `{text, queued:true}` (`api/pool.py:1062-1066`) and again `{text}` when dispatched (`api/pool.py:905-909`, `exclude` only covers the sender). Clients must dedupe (e.g. mark the queued bubble as sent when the non-queued echo with identical text arrives).
- **Ordering.** Within one session all broadcast frames are produced by a single coroutine under the per-session lock and written sequentially to each socket (`api/pool.py:890-939,1345-1366`), so **every subscriber receives the same events in the same order**, which is the SDK generation order. The exceptions are the direct-only frames listed above.
- **Interrupt visibility.** ⚠ Only the interrupting WS gets `status: interrupted`. Because the turn task is *cancelled*, **no `turn_complete` is broadcast** for an interrupted turn (the CLI's late `ResultMessage` goes only to the replay ring, §4.5). Other subscribers receive no terminal event. Clients must also treat "a new `user_message`/`status: processing` arrives while I think a turn is running" as the end of the previous turn.
- **Reconnect.** Re-sending `start` with the same `local_id` re-subscribes; the running turn keeps streaming to the new socket. Events emitted while disconnected are recoverable only via the resume protocol (§4.5) or REST (§6).
- **Session death.** If the CLI subprocess dies, the dead-session reaper (or the turn itself) closes it with `session_terminated{reason}` + `session_stopped` (`api/pool.py:835-870,1157-1177`). The JSONL is intact; recover by `start`ing a **new** `local_id` with `resume_sdk_id = session_terminated.sdk_session_id`.
- **Slash commands** (`command`) bypass all of the above: direct, unsequenced, no `user_message`.
- **Providers.** Claude (`manager/claude/session.py`) emits every event type above. Qwen (`manager/qwen/session.py:519-640`) emits text/thinking deltas+completes, tool_use/result, turn_complete, session_stalled — **no `seq`/`stream_id`** (no resume protocol). Gemini harness (`manager/gemini/session.py:399-590`) emits text deltas, `text_complete`, tool_use/result, `turn_complete` (`cost:null`, no `session_id`), session_stalled — no thinking, no seq.

### 4.4 Streamed-content structure and the client ordering/merging contract

**What the wire does NOT carry:** no block index, no content-block id, no message id, no "turn id", no `content_block_start/stop` events, no tool-input deltas (`input_json_delta` is dropped, `manager/claude/session.py:1051-1054`), no parent/subagent marker. The only identifiers are `tool_use_id` (joins a call to its result), `request_id` (permissions) and — Claude only — `seq`.

**How events are produced (Claude harness).** The SDK runs with `include_partial_messages=True` (`manager/claude/session.py:920`):

1. Partial stream events → `text_delta` / `thinking_delta` only (`:1037-1054`).
2. When a content block completes, the CLI emits an `AssistantMessage` for it, which becomes `text_complete` (full text), `thinking_complete` (full thinking) or `tool_use` (`:1070-1084`). Evidence that the CLI emits one assistant message *per content block*: the persisted JSONL has exactly one block per `assistant` line (multiple lines share one `message.id`; checked on `context/528dbf6f-….jsonl`).
3. Tool results arrive as `UserMessage.tool_use_result` → `tool_result` (`:1109-1138`). Placeholder results with empty id and empty content are skipped (`:1086-1102`).
4. `ResultMessage` → `turn_complete` (`:1140-1154`).

So the canonical event grammar of one turn is:

```
turn      := status{processing} step+ turn_complete
step      := text_block | thinking_block | tool_use | tool_result | permission_request | permission_resolved
             | session_stalled | compact_complete
text_block     := text_delta* text_complete        // deltas may be absent (e.g. after a replay cut)
thinking_block := thinking_delta* thinking_complete
```

with these guarantees / non-guarantees:

- **G-ORDER-1 (arrival order is the order).** For a given session, the order in which frames arrive on the socket **is** the order in which the model produced the blocks. Text that the model wrote *before* a tool call arrives before that `tool_use`; text written *after* the tool result arrives after the `tool_result`. A client must render blocks **in arrival order in a single ordered list per assistant turn** — never in separate "text" and "tools" lists, and never by grouping all tool calls at the top or bottom. The current Android reducer already appends in arrival order (`android/.../chat/ChatController.kt:619-711`), but it routes every event into the message pointed to by `streamingMessageId`, which is cleared only by `turn_complete` (and disconnect / voice end). Because the backend sends **no** `turn_complete` in voice mode, after interrupts, or after orchestrator errors (G-4), later tool blocks get appended into an older assistant message that sits *above* newer transcript/user messages — that is the observed out-of-order bug. **Rule: a new assistant turn begins at any `user_message`, locally sent prompt, `status: processing`/`streaming`, or voice user transcript — never rely on `turn_complete` alone to close the previous turn.**
- **G-ORDER-2 (deltas belong to the *open* block).** A `text_delta` extends the most recent block **iff** that block is an open text block; otherwise it opens a new text block at the end. The same for `thinking_delta` with thinking blocks. Any other event type (`tool_use`, `tool_result`, `thinking_*` for a text block, `text_*` for a thinking block, `permission_*`) **closes** the open block, so a later `text_delta` starts a new block positioned *after* that event.
- **G-ORDER-3 (`*_complete` replaces, it does not append).** `text_complete.text` / `thinking_complete.text` is the complete content of the block that was just streamed. Replace the open block's accumulated text with it (this also self-heals dropped deltas). If no matching open block exists (replay started mid-block, or a provider that sends no deltas), append a new block with that text. Never append `text_complete.text` to the delta text (that duplicates the text).
- **G-ORDER-4 (tool results attach by id, not by position).** `tool_result` must be merged into the existing `tool_use` block with the same `tool_use_id`; it does **not** create a new positional block. Results of parallel calls may arrive in any order relative to each other. If no matching `tool_use` exists (missed by a reconnect, or `tool_use_id == ""`), render it as a standalone result at the current end of the list.
- **G-ORDER-5 (`seq`).** On Claude sessions most events carry `seq` (int) and `stream_id` (str) (`api/pool.py:1243-1265`). Within one `stream_id`, `seq` is strictly increasing in arrival order, so it can be used to **dedupe** (drop any event with `seq <= last_seen_seq`) and to stitch replay batches. Seq numbers are **not contiguous** on the wire (events dispatched while no turn is listening consume seqs but go only to the replay ring), so a gap is not data loss. Unsequenced frames: `user_message`, `status`, `session_started`, `session_stopped`, `session_terminated`, `error`, `compact`/`command` output, and the synthetic `turn_complete{is_error:true,result:"receive_loop_exited"}` emitted when the receive loop dies mid-turn (`manager/claude/session.py:734-743`). ⚠ `session_stalled` is yielded by `send()` itself (not a ring event) but `_wrap_payload` stamps it with the **previous** event's `seq` (`manager/claude/session.py:585-591` leaves `_last_yielded_seq` unchanged; `api/pool.py:1257-1264`). A client that drops `seq <= last_seen` will wrongly drop every stall notice — **exempt `session_stalled` from seq dedupe**.
- **G-ORDER-6 (turn boundaries).** A turn starts at `status: processing` (or, for observers, the preceding `user_message`) and ends at `turn_complete`. ⚠ Interrupted turns end with no `turn_complete` for most subscribers (§4.3). Queued prompts produce back-to-back turns on the same socket.
- **Subagents (Task tool).** ⚠ **AMBIGUOUS.** `_process_message` does not inspect `parent_tool_use_id` on `AssistantMessage`s (`manager/claude/session.py:1070-1084`), so if the SDK forwards subagent messages, their `tool_use`/`text_complete` events appear flat in the parent stream with no marker. Subagent transcripts are not in the parent JSONL (they live under `context/<uuid>/subagents/`), so history and live views can disagree. Clients should tolerate `tool_use`/`tool_result` events whose ids never appear in history.

**Reference reducer (normative pseudocode)**

```text
state per session: turns[]   // each turn = {userText?, blocks[]}, blocks are ordered
on user_message      : push new turn {userText}
on status processing : if no open turn → push new turn {}
on text_delta(t)     : b = last(blocks); if b && b.kind=='text' && b.open → b.text += t
                       else push {kind:'text', text:t, open:true}
on text_complete(t)  : b = last(blocks); if b && b.kind=='text' && b.open → b.text = t; b.open=false
                       else push {kind:'text', text:t, open:false}
on thinking_delta/complete : same as text with kind 'thinking'
on tool_use(id,…)    : close open block; push {kind:'tool', id, name, input, result:null}
on tool_result(id,…) : close open block; tb = find tool block with id in current turn (else in any turn);
                       if tb → tb.result = {output,is_error} else push {kind:'orphan_result', id, output, is_error}
on permission_request: close open block; push {kind:'permission', request_id, …, state:'pending'}
on permission_resolved: update the permission block with request_id
on turn_complete     : close open block; mark turn complete (cost/usage)
on seq present       : if seq <= lastSeq[stream_id] → ignore; else lastSeq[stream_id] = seq
```

### 4.5 Resume / replay protocol (Claude sessions only)

Purpose: a client that drops and reconnects mid-turn can receive exactly the events it missed.

- **Server state.** Per session: a ring buffer of the last **500** `(seq, event)` pairs (`manager/claude/session.py:120-126,283`), the current `stream_id` (regenerated whenever the CLI subprocess (re)connects, `:365-373`) and `_next_seq`.
- **Handshake.** Client sends `start{local_id, resume_from:{stream_id, seq}}` where `seq` is the **last seq it processed**. Server replies `session_started{…, resume_state:{stream_id, next_seq}}` and then (`api/routes/chat.py:404-422`; `api/pool.py:1289-1339`; `manager/claude/session.py:784-837`):
  - `ok` → zero or more replayed event frames, each with `seq` + `stream_id`, all with `seq > resume_from.seq`, in order;
  - `overflow` (checkpoint older than the ring) or `mismatch` (different `stream_id`, e.g. backend restart) → `session_started.replay_overflow: true` and **no replay**; the client must refetch history over REST (§6);
  - provider without the protocol (Qwen/Gemini) → no `resume_state`, no replay, no overflow flag.
- **Fresh subscriber** (no `resume_from`): gets `resume_state` so it can start tracking; no replay. The events it missed before subscribing must come from REST.
- Replay includes events that were never broadcast live (e.g. the tail of an interrupted turn, including its `turn_complete`). Replay does **not** include `user_message`/`status` frames (not in the ring).
- Replayed frames and live frames can interleave only in this order: all replay frames are written before `_handle_start` returns, i.e. before this socket can receive any further broadcast — but the socket is subscribed *before* the replay is computed (`chat.py:298-300`), so a live event broadcast between `subscribe` and `send(session_started)` could arrive **before** `session_started` and again inside the replay. ⚠ Use `seq` dedupe (G-ORDER-5).

### 4.6 Permission flow (end to end)

1. The CLI asks to run a gated tool (`ExitPlanMode`). `can_use_tool` emits `permission_request{request_id, tool_name, tool_input}` and blocks (`manager/claude/session.py:875-901`; `manager/base_session.py:327-363`). If no turn is being listened to, the tool is **auto-allowed** (`manager/claude/session.py:891-896`).
2. The event is broadcast to session subscribers and mirrored to orchestrator subscribers as `nested_session_event{session_id:<local_id>, event_type:"permission_request", event_data:<same payload incl. seq>}` (`api/pool.py:928-938`).
3. Any of these resolves it (first wins): chat-WS `permission_response`; chat-WS `send` with non-empty text (auto-deny with the text as reason); orchestrator tool `respond_to_agent_permission`; end of the turn (deny, `message:"stream ended"`, `responder:"system"`) (`manager/base_session.py:365-374`).
4. `permission_resolved{request_id, decision, responder, message}` is broadcast + mirrored. ⚠ When the resolution is the end-of-turn drain, the `permission_resolved` event is produced after the listener detached, so it is **not broadcast** (ring only). Clients must clear any pending permission UI on `turn_complete`, `status: interrupted`, `session_stopped`, or a new `status: processing`.
5. The plan text of `ExitPlanMode` is also visible as normal assistant text in the stream (CLAUDE.md "Permission Gating").

---

## 5. WebSocket: `/api/orchestrator/chat` (orchestrator)

Handler: `api/routes/orchestrator.py:114-404`. **At most one orchestrator session exists in the backend at a time** (`api/pool.py:121-124`), shared by all devices. Every connected orchestrator WS is also a pool **watcher** from the moment it connects (before any `start`) (`api/routes/orchestrator.py:127-128`).

### 5.1 Lifecycle and identity

- The orchestrator is keyed by the `local_id` of whichever client created it. Other devices must attach with **the same `local_id`** (discover it via `GET /api/sessions/pool/live` → row with `is_orchestrator:true`, or via the watcher event `agent_session_opened{is_orchestrator:true}`). A `start` with a different `local_id` while one is active fails with `error: orchestrator_active` (`:614-620`).
- `start` decision tree (`_handle_start`, `:407-779`):
  1. If the same `local_id` is mid-teardown, wait up to `await_orchestrator_stop_s` (`orchestrator/voice_timeouts.py`), else `error: orchestrator_stopping` (`:447-460`).
  2. **Reconnect** (pool already has this `local_id`): subscribe and send `session_started` (variants for text↔voice re-arm, passive text attach to a live voice session, voice-config drift → rebuild, `:462-612`). No history is replayed.
  3. **Other orchestrator active** → `error: orchestrator_active`.
  4. **New / resumed**: create `OrchestratorSession(session_id=resume_id, local_id=…)`, send `status: connecting`, `session.start()` (loads history from `context/<resume_id>.jsonl` if it exists, else writes an `orchestrator_meta` first line), register in pool (watchers get `agent_session_opened{is_orchestrator:true, sdk_session_id: jsonl_id}`), subscribe, send `session_started` (`:622-779`; `orchestrator/session.py:508-604`; `api/pool.py:517-535`).
- Text mode only: a **wake callback** is installed so that when a background agent turn finishes while the orchestrator is idle, the server runs a synthetic empty-prompt turn (`:722-740`). Observers then see `status: streaming` … `status: idle` **with no preceding user message**.
- Teardown: `stop` (client) or `POST /api/sessions/{local_id}/close` → `pool.stop_orchestrator()` → watchers get `agent_session_closed{is_orchestrator:true}`; subscribers are dropped silently; only the WS that sent `stop` gets `session_stopped` (`:339-353`; `api/pool.py:570-609`).
- Disconnect of a socket only unsubscribes; if that socket owned voice, voice is ended but the orchestrator session stays alive (`:376-404`).

### 5.2 Client → server messages

| `type` | Fields | Behaviour | Code |
|---|---|---|---|
| `start` | `local_id`: str; `resume_sdk_id`\|`session_id`: str (JSONL id to resume) | Text-mode start/attach (§5.1) | `:150-151,407-779` |
| `voice_start` | `start` fields + `voice_provider`, `voice_model`, `voice_name`, `voice_transcription_language`, `voice_endpoint` (all optional; missing ones default from `assistant_config.json` `default_voice_*`) | Voice-mode start / re-arm; this WS becomes the **voice owner**; broadcasts `voice_owner_active{active:true}` to all subscribers. Concurrent `voice_start`s for one `local_id` are serialised. See §7. | `:153-193,634-652` |
| `send` | `text`: str | Runs a full agent turn (requires `start`) | `:195-202,1019-1031` |
| `inject_text` | `text`: str | Text mode: same as `send`. Voice mode: silent inject into the live voice conversation (no model response) + broadcast `user_message{text, source:"shared_inject"}` | `:204-217,1034-1076` |
| `send_audio` | `audio`: base64 str; `format`: str (default `"webm"`; wav/mp3/webm/ogg/…; converted to wav server-side); `text`?: str | One-shot multimodal audio turn through an audio-capable OpenAI model (`gpt-audio` family); auto-switches the model if needed | `:219-232,1079-1112`; `orchestrator/session.py:1723-1853` |
| `set_model` | `model`: str (model_id) | Switch the text model (refused in voice mode) → broadcast `model_changed` or `error: unknown_model`/`cannot_switch_voice` | `:234-241,1115-1139` |
| `get_model` | — | Direct `model_info` reply (requires session) | `:243-250,1142-1147` |
| `get_models` | — | Direct `models_list` reply (no session needed; **LIVE**) | `:252-254,1150-1160` |
| `compact` | — | Summarise history (`status: streaming` → `compact_complete{trigger:"manual",tokens_before,tokens_after}` → `status: idle`) | `:307-314,1163-1181` |
| `interrupt` | — | Interrupt the agent loop; broadcast `status: interrupted` (to **all** subscribers, unlike chat WS) | `:316-319` |
| `voice_stop` | — | End voice only; keep orchestrator; ack is the `voice_ended` broadcast | `:321-337` |
| `stop` | — | End voice (if any) and stop the orchestrator; direct `session_stopped` | `:339-353` |
| `voice_event`, `voice_audio_in`, `voice_recording_chunk`, `voice_recording_end` | see §7 | Voice relay / mirroring | `:256-305` |
| `ping` / `pong` | — | Ignored | `:147-148` |
| other | — | `error: unknown_type`; invalid JSON → `error: invalid_json` (**LIVE**) | `:355-359,135-139` |

### 5.3 Server → client messages (non-voice; voice in §7)

Delivery is **broadcast to all orchestrator subscribers** unless marked Direct. Frames from the orchestrator are **never** stamped with `seq`/`stream_id` (no resume protocol).

| `type` | Payload | When | Code |
|---|---|---|---|
| `session_started` | `{session_id: <local_id>, voice: bool, model_info: OrchestratorModelInfo}` + voice fields when voice (`voice_provider`, `voice_model`, `voice_name`, `voice_transcription_language`, `voice_initiator`, `voice_recording_enabled`, `voice_session_update?`, `voice_connection_info?`, `voice_connection_error?`) — Direct | after `start`/`voice_start` | `:518-528,542-552,591-611,755-772,782-891` |
| `status` | `{status: "connecting"}` Direct; `{status: "streaming"}` / `{status: "idle"}` / `{status: "interrupted"}` broadcast | turn start / end / interrupt | `:682,1024,1028,1099,1105,1168,1176,319` |
| `text_delta` | `{text}` | streaming orchestrator text | `api/serializers.py:223-224` |
| `text_complete` | `{text}` (full block text) | text block finished | `serializers.py:225-226` |
| `tool_use` | `{tool_use_id, tool_name, tool_input}` | tool block finished (text mode) / voice tool call starts | `serializers.py:227-233`; `orchestrator.py:1549-1554,1609-1614` |
| `tool_executing` | `{tool_use_id, tool_name}` | tool execution started (text mode) | `serializers.py:234-239` |
| `tool_progress` | `{tool_use_id, tool_name, elapsed_seconds: float, message: "Still executing <tool>..."}` | every 5 s while a tool runs | `serializers.py:240-247`; `orchestrator/agent.py:32,274-295` |
| `tool_result` | `{tool_use_id, output: str, is_error: bool}` | tool finished | `serializers.py:255-261`; `orchestrator.py:1565-1570,1627-1632` |
| `turn_complete` | `{input_tokens: int, output_tokens: int}` (summed over all model calls of the turn) | end of a successful turn (**not** emitted after an error/interrupt) | `serializers.py:262-267`; `orchestrator/agent.py:202-205` |
| `error` | `{error, detail}` — agent errors: `interrupted`, `api_error`, `provider_error`; route errors: `invalid_json`, `unknown_type`, `not_started` (Direct), `orchestrator_stopping`, `orchestrator_active`, `start_failed`, `voice_restart_failed`, `voice_config_busy` (Direct), `send_failed`, `send_audio_failed`, `invalid_audio`, `inject_text_failed`, `unknown_model`, `cannot_switch_voice`, `compact_failed`, `not_voice_session` (Direct), `voice_event_failed`, `voice_audio_failed` | | `serializers.py:268-269`; route passim |
| `model_changed` | `{model_info}` | after `set_model` | `:1130-1133` |
| `model_info` | `{model_info}` — Direct | reply to `get_model` | `:1144-1147` |
| `models_list` | `{models: ModelInfo[]}` — Direct | reply to `get_models` | `:1157-1160` |
| `compact_complete` | `{trigger: "manual", tokens_before: int, tokens_after: int}` (⚠ different fields from the chat-WS `compact_complete`) | after `compact` | `:1170-1175` |
| `session_stopped` | `{}` — Direct | reply to `stop` | `:353` |
| `user_message` | `{text, source: "shared_inject"}` | voice-mode `inject_text` only | `:1058-1062` |
| `nested_session_event` | `{session_id: <agent local_id>, event_type: "permission_request"\|"permission_resolved", event_data: <the chat-WS payload incl. seq/stream_id>}` | a delegated agent session needs/resolved a permission | `api/pool.py:933-938`; `serializers.py:248-254` |
| `agent_session_opened` | `{session_id: <local_id>, sdk_session_id: str\|null, is_orchestrator: bool}` | any agent session or the orchestrator joined the pool — **sent to every orchestrator WS (watcher), even before `start`** | `api/pool.py:322-327,530-535` |
| `agent_session_closed` | `{session_id: <local_id>, is_orchestrator: bool}` | left the pool | `api/pool.py:383-387,589-593` |
| `audio_upload` | `{audio, format, text, size_bytes}` | legacy side effect of `POST /api/orchestrator/audio` (§3.8) | `api/routes/voice.py:217-223` |
| `unknown` | `{}` | serializer fallback | `serializers.py:270` |

`OrchestratorModelInfo` (`orchestrator/config.py:278-286`): `{"model": str, "provider": "anthropic"|"openai", "max_tokens": int, "supports_audio": bool, "model_info": ModelInfo|null}`.

**Things the orchestrator socket does NOT send** (contrast with the chat WS): no `user_message` for typed `send`s (other devices do not see what was typed until the reply streams or history is reloaded), no `thinking_*`, no `permission_*` for itself, no `session_stalled`, no `seq`, no replay on reconnect.

### 5.4 Orchestrator stream structure and ordering contract

Text-mode turn (`orchestrator/agent.py:117-205`, providers `orchestrator/providers/anthropic.py:63-112`, `openai_text.py:605-702`):

```
status{streaming}
loop (≤ 20 model calls):
   ( text_delta* text_complete | tool_use )*      // in model block order; tool_use only after its block ends
   if no tool_use in this call → break
   for the tools of this call (run concurrently):
       tool_executing{id} … tool_progress{id}* … tool_result{id}   // interleaved across tools, completion order
turn_complete{input_tokens, output_tokens}
status{idle}
```

- The same rules G-ORDER-1…4 apply. Additional: `tool_executing`, `tool_progress` and `tool_result` for parallel calls interleave arbitrarily and arrive **after all `tool_use` blocks of that model call**; attach them by `tool_use_id`.
- `text_complete` is emitted at `content_block_stop` and only if the block had text (`anthropic.py:92-94`).
- Error/interrupt path: `error{interrupted|api_error|provider_error}` then `status{idle}`; **no `turn_complete`**.
- **Cross-task interleaving.** Unlike agent sessions, orchestrator broadcasts come from several concurrent tasks (the text turn, the voice relay drain, background voice tool-call tasks, the wake turn). Each task's own events are in order, but frames from different tasks can interleave. In voice mode, `tool_use`/`tool_result` for one `call_id` are ordered (use before result) (`api/routes/orchestrator.py:1547-1570`), while transcript `voice_event`s stream concurrently.

---

## 6. History / JSONL loading and merging with live streams

### 6.1 Where history lives

| Session kind | File | Writer |
|---|---|---|
| Claude agent session | `context/<sdk_session_id>.jsonl` | bundled Claude Code CLI (one line per content block; also `queue-operation`, `attachment`, `ai-title`, `last-prompt`, `file-history-snapshot`, `mode`, `system`… lines) |
| Qwen agent session | `context/chats/<id>.jsonl` | Qwen CLI (`parts`/`functionCall` shape) |
| Gemini agent session | discovered via the Gemini harness (`~/.gemini/tmp/<label>/chats/session-*.jsonl`) | Gemini CLI |
| Orchestrator | `context/<jsonl_id>.jsonl` | `orchestrator/persistence.py:198-225` (`HistoryWriter`) |

All are read through `SessionStore` + per-provider adapters into one normalized shape (`manager/protocol.py:298-368`), then served by `GET /api/sessions/{id}/messages` (§3.2).

### 6.2 What a REST message is

- **One JSONL `user`/`assistant` line = one `MessagePreview`** (`manager/protocol.py:351-368`). `system` and all internal line types are dropped.
- For **Claude agent sessions** that means an assistant turn is split into **many consecutive `assistant` messages, usually one block each** (text, tool_use, or an *empty* message for a thinking block), and every tool result is a separate **`user` message whose `blocks` is `[tool_result]` and whose `text` is `""`** (**LIVE** verified on `528dbf6f-…`).
- `text` = all `text` blocks joined with `\n` (thinking excluded); `blocks` keeps order within the line (`manager/protocol.py:421-483`).
- Block mapping (`manager/protocol.py:448-481`): `text` → `{type:"text", text}`; **`thinking` → `{type:"text", text: block["text"]}`** — but Claude stores thinking under the key `thinking`, so Claude thinking blocks yield **no block at all** (empty assistant message, **LIVE**), whereas Qwen/Gemini adapters normalise thinking to `{type:"thinking", text}` which then surfaces as an ordinary **`text` block** indistinguishable from the answer (`manager/qwen/adapter.py:45-63`, `manager/gemini/adapter.py:181`). ⚠ G-9.
- `tool_use` → `{type:"tool_use", tool_use_id, tool_name, tool_input}`; `tool_result` → `{type:"tool_result", tool_use_id, output: str, is_error}` (list content joined).
- **No ids:** history messages carry no message id, block id, `seq` or `source`. The only stable identity is the absolute index (`start_index + i`), which is stable while the file is append-only (it changes after `truncate`).
- User lines also include CLI-internal content verbatim (slash-command echoes like `<command-name>…`, compact summaries, "[Request interrupted by user]", system-reminder-like meta lines). The backend does not filter `isMeta`/`isCompactSummary` flags (`manager/claude/adapter.py:64-85`).

### 6.3 Orchestrator history specifics

The orchestrator JSONL (`orchestrator/session.py:572-591,1654-1693,1855-1893,1932-1939`; `orchestrator/voice_persister.py:188-356`) contains:

| JSONL `type` | Fields | Visible via REST? |
|---|---|---|
| `orchestrator_meta` | `orchestrator:true, session_id, model, provider, timestamp, voice?, voice_provider?, voice_model?, voice_name?, voice_transcription_language?, openai_model?` | no (sets `is_orchestrator`) |
| `user` | `message:{role, content: str}`, `timestamp`, optional `source`: `"voice_transcription"` \| `"shared_inject"` \| `"audio_input"` (+`audio_format`), optional `audio_segment` | **yes** (text only; `source` dropped). Voice turns are recognisable only by the content prefix `"[voice] "` or `"[voice, recording: <local_id> <start>-<end>ms] "`; audio turns by `"[audio:<fmt>] "`. |
| `assistant` | `message:{role, content: str}` — **text mode: ALL text blocks of the turn joined with `\n`, written once at the end of the turn** (`session.py:1884-1893`); voice: one line per spoken response, `source:"voice_response"` | **yes** |
| `tool_use` | `tool_call_id, tool_name, tool_input, timestamp, source?:"voice"` | ⚠ **no** — the Claude adapter keeps only `user`/`assistant`/`system` lines (`manager/claude/adapter.py:81`) |
| `tool_result` | `tool_call_id, output, is_error, timestamp, source?` | ⚠ **no** |
| `background_notification` | `notification_id, turn_id, session_id, session_title, origin_tool_use_id, status, cost, turns, duration_seconds, error, timestamp` | ⚠ **no** |
| `voice_interrupted` | `timestamp` | no |
| `model_switch` | `model, provider, timestamp` | no |
| `compact` | `trigger:"manual", summary, timestamp` | no |

**LIVE**: orchestrator `9ed0934e-…` has 16 `tool_use` + 16 `tool_result` + 6 `background_notification` lines, yet `/messages` returns only user/assistant text. **Consequences:** (a) reloading an orchestrator conversation loses every tool call and notification; (b) the relative order of text vs. tool calls inside a text-mode turn is lost even on disk (text is persisted after the tools); (c) a wake turn appears as an assistant message with no preceding user message.

### 6.4 Pagination

```
GET /api/sessions/{id}/messages?limit=50              → newest 50, start_index = total-50, has_more = start_index>0
GET /api/sessions/{id}/messages?limit=50&before=S     → messages [max(0,S-50), S), start_index = max(0,S-50)
```

`total_count` is computed on every call (the whole file is parsed each time — no server cache for messages). `before=0` is treated as "no before" by the route's `start_index` arithmetic but the store returns an empty page, so the result is `messages:[]`, `start_index:0`, `has_more:false` — harmless. If the file grew between pages, indices from the top are still valid (append-only).

### 6.5 Merging history with the live stream (normative guidance)

There is **no server-provided join key** between REST history and live events. A correct client:

1. **Subscribe first, then fetch.** Send `start` (with `resume_from` if you have one), buffer live frames, then `GET …/messages` for the latest page, then apply buffered frames. Messages already persisted to the JSONL *and* re-delivered live (or replayed) will duplicate — dedupe on `seq` for live frames and treat history as authoritative for anything completed before the subscribe.
2. **AMBIGUOUS:** the timing of the CLI's JSONL write relative to the WS broadcast of the same block is not controlled by the backend; a block can be in neither, one, or both views at subscribe time. The robust strategy is to **re-fetch the tail page after `turn_complete`** (and after `replay_overflow`, `session_terminated`, reconnects) and replace the in-memory tail with it.
3. For the **orchestrator**, history has no tool calls and coarser assistant messages (§6.3), and there is no replay. Keep the live-rendered transcript in memory for the current view; on reload accept the reduced fidelity (or see the backend fix in §8).
4. Render history with the same reducer as live events by converting each `MessagePreview` into events: consecutive `assistant` messages (until the next `user` message with a non-`tool_result` block) form one assistant turn; `user` messages consisting only of `tool_result` blocks are **not** user turns — attach each `tool_result` to the `tool_use` with the same `tool_use_id`. Drop empty assistant messages (thinking placeholders).

---

## 7. Voice

Voice is carried **only** on the orchestrator WebSocket (§5); there is no separate voice socket. "Broadcast" = `pool.broadcast_orchestrator` (all orchestrator subscribers, owner included); "Owner" = direct to `session.voice_owner_ws`; "Direct" = the requesting socket.

### 7.1 Providers and transports

Registered in `orchestrator/providers/voice_registry.py:77-81`.

| `voice_provider` id | Class | `connection_type` | Audio path |
|---|---|---|---|
| `openai` | `OpenAIVoiceProvider` (`orchestrator/providers/openai_voice.py:64,110-116`) | `webrtc` | Client ↔ OpenAI Realtime **directly** over WebRTC (mic track + data channel `oai-events`). The backend never sees audio; it sees only events the client mirrors to it. |
| `qwen` | `QwenVoiceProvider` (`qwen_voice.py:145,183-189`) | `websocket` | Client ⇄ orchestrator WS (base64 PCM in JSON) ⇄ backend relay (`orchestrator/voice_relay.py`) ⇄ DashScope realtime WS (`qwen_voice.py:60,929-948`). |
| `google` | `GeminiAIStudioBackend` / `VertexAIBackend` (`gemini_voice.py:68,105`), both `provider_name="google"` | `websocket` (`gemini_voice_base.py:184-186`) | Same relay path to Gemini Live. Backend chosen by `voice_endpoint` = `aistudio` \| `vertex` (`gemini_voice.py:42-45,170-182`; default env `GEMINI_VOICE_BACKEND`, else `vertex`). |

`session.needs_voice_relay` ⇔ `connection_type == "websocket"` (`orchestrator/session.py:873-880`).

Static registry (`voice_registry.py:304-376`; live discovery may add models): openai — `gpt-realtime` (default), `gpt-realtime-mini`, default voice `cedar`, no transcription languages; qwen — `qwen3.5-omni-plus-realtime` (default), `qwen3-omni-flash-realtime`, default voice `Aiden`, default language `en`; google — `gemini-3.1-flash-live-preview` (default), `gemini-2.5-flash-native-audio-latest`, `gemini-live-2.5-flash-native-audio` (Vertex), default voice `Puck`. Global default `openai` / `gpt-realtime` (`:375-376`). **LIVE** config currently uses `openai/gpt-realtime-2/cedar`.

`VoiceModelEntry` (`voice_registry.py:47-67`): `{id, label, voice, voices:[{id,label,description}], transcription_languages:[{id,label,description}], default_transcription_language, default: bool}`.

**Resolution** (`resolve_voice_target`, `voice_registry.py:422-495`): unknown provider → silently `openai`; unknown model id → accepted with a synthetic entry; unknown voice → model default; language `null`/unknown → model default, `""` → auto-detect. Qwen sends the language upstream only if env `QWEN_ALLOW_TRANSCRIPTION_LANGUAGE=1` (`qwen_voice.py:124-142`); Gemini ignores it (`gemini_voice_base.py:131-134`).

**How a client selects a provider.** Put `voice_provider`, `voice_model`, `voice_name`, `voice_transcription_language`, `voice_endpoint` on the `voice_start` message (`api/routes/orchestrator.py:428-434`). On the **new-session** path, any field that is missing is filled from `assistant_config.json` `default_voice_*` (`:634-652`). On the **re-arm** path (`restart_voice`), a missing field means "keep the session's previous value" (`orchestrator/session.py:1262-1276`). Model catalogues for pickers: `GET /api/orchestrator/voice/models` (live path returns only `openai` and `qwen`; `google` appears only in the static fallback, `orchestrator/providers/discovery.py:341-373`) + `GET /api/config/voice/google/models?endpoint=…` for Google.

### 7.2 `ConnectionInfo` and the REST token endpoint

`POST /api/orchestrator/voice/session?provider=&model=&voice=&transcription_language=&endpoint=` (§3.8) returns `{"connection_info": ConnectionInfo}` (+ legacy `client_secret{value,expires_at}`, `model`, `voice` for openai). ⚠ Unlike `voice_start`, a missing `provider`/`model` falls back to the **registry** defaults (openai/gpt-realtime), **not** `assistant_config.json` (only `endpoint` falls back to config, `api/routes/voice.py:66-83`). The docstring's `id` field is never returned. Prefer `session_started.voice_connection_info`, which is minted for the session's actual provider/model.

`ConnectionInfo` (`orchestrator/providers/voice_base.py:196-210`):

```jsonc
// openai (openai_voice.py:507-516)
{ "connection_type": "webrtc",
  "endpoint": "https://api.openai.com/v1/realtime/calls?model=<model>",
  "ephemeral_token": "ek_…", "expires_at": 1759500000,
  "audio_in_format":  {"sample_rate": 24000, "encoding": "pcm16"},
  "audio_out_format": {"sample_rate": 24000, "encoding": "pcm16"},
  "model": "gpt-realtime", "voice": "cedar" }
// qwen (qwen_voice.py:906-925) / google (gemini_voice_base.py:955-965)
{ "connection_type": "websocket", "endpoint": "<upstream URL, informational only>",
  "ephemeral_token": null, "expires_at": null,
  "audio_in_format": {…}, "audio_out_format": {…}, "model": "…", "voice": "…", "audio_relay": "backend" }
```

Errors: 400 bad target, 503 missing key (`OPENAI_API_KEY not configured`, `DASHSCOPE_API_KEY not configured`), 502 upstream error. The Gemini variant never checks keys. For WS providers this endpoint does **not** start a relay.

### 7.3 Audio formats

All audio on the orchestrator WS is **base64 strings inside JSON text frames** (client→server) / binary-JSON frames (server→client). PCM is 16-bit signed little-endian **mono**. Always read the rates from `voice_connection_info.audio_in_format` / `audio_out_format`; the `encoding` labels are inconsistent (`pcm`, `pcm16`, `pcm24` are all 16-bit PCM; `pcm24` means 24 kHz).

| Provider | Mic in (`voice_audio_in`) | Speaker out (`voice_audio_out`) | Code |
|---|---|---|---|
| openai | n/a (WebRTC media track; session.update declares `audio/pcm` 24 kHz) | n/a (WebRTC media track) | `openai_voice.py:458-467,512-513` |
| qwen `qwen3.5-omni-plus*` | 24 kHz, label `pcm` | 24 kHz, label `pcm` | `qwen_voice.py:100-106` |
| qwen other (flash/turbo) | 16 kHz, label `pcm16` | 24 kHz, label `pcm24` | `qwen_voice.py:107-113` |
| google | 16 kHz, `pcm16` (upstream mime hard-coded `audio/pcm;rate=16000`) | 24 kHz, `pcm16` | `gemini_voice_base.py:612-621,960-961` |

No chunk size is imposed by the backend; the web client sends 100 ms chunks (`frontend/src/voice/transports/websocket.ts:42-46`). The backend's Silero VAD resamples input to 16 kHz in 512-sample windows (`orchestrator/voice_vad.py:70-96`).

### 7.4 Voice messages on the orchestrator WS

**Client → server**

| `type` | Fields | Behaviour | Code |
|---|---|---|---|
| `voice_start` | `local_id`, `resume_sdk_id`\|`session_id`, `voice_provider?`, `voice_model?`, `voice_name?`, `voice_transcription_language?`, `voice_endpoint?` | Start / re-arm / re-attach voice; serialised per `local_id`; on success this socket becomes the **owner** and `voice_owner_active{active:true}` is broadcast | `api/routes/orchestrator.py:153-193,407-779` |
| `voice_audio_in` | `audio`: base64 PCM at `audio_in_format.sample_rate` | WS providers: forwarded upstream as `input_audio_buffer.append` (qwen) / `realtimeInput.audio` (google). **Silently dropped** when this socket has no voice session (`:277-278`) or before the upstream handshake completes (waits ≤15 s then drops, `voice_relay.py:800-805`). Relay exception → broadcast `error: voice_audio_failed`. | `:265-287` |
| `voice_event` | `event`: object (provider-native) | Requires voice (else Direct `error: not_voice_session`). OpenAI: the **mirror of every data-channel event** the client receives/sends. WS providers: client control frames forwarded upstream if `provider.accepts_upstream_event(event)` — qwen accepts `input_audio_buffer.append\|commit\|clear`, `conversation.item.create\|delete\|truncate`, `response.create`, `response.cancel`, `session.update` (`qwen_voice.py:770-784`); google accepts only type-less objects with `clientContent`/`realtimeInput`/`toolResponse`/`setup`/`sessionResumptionUpdate` (`gemini_voice_base.py:767-782`); others are dropped with a log line. Then `process_voice_event` persists transcripts, advances response gating, executes tools (`response.function_call_arguments.done` spawns a background tool task). Failure → broadcast `error: voice_event_failed`. | `:256-263,1184-1268` |
| `voice_recording_chunk` | `channel`: `"user"`(default)\|`"assistant"`, `audio`: base64 PCM16 | Only when `session_started.voice_recording_enabled` is true (WebRTC recordings); otherwise dropped | `:289-301` |
| `voice_recording_end` | — | No-op | `:303-305` |
| `voice_stop` | — | `end_voice("user_stop")`, keep the orchestrator; ack = `voice_ended` broadcast | `:321-337` |
| `inject_text` | `text` | Voice mode: silent inject (persist `user` line with `source:"shared_inject"`, broadcast `user_message{text,source:"shared_inject"}`, send `conversation.item.create` input_text without `response.create` — as an owner `voice_command` for openai, upstream for WS providers; google uses `clientContent{turnComplete:false}`) | `:1034-1076`; `voice_base.py:156-179`; `gemini_voice_base.py:480-494` |
| `send_audio` | `audio` b64, `format`, `text?` | "Talk mode" one-shot turn (§7.8) | `:219-232` |
| `interrupt` | — | Interrupts only the text agent loop; it does **not** cancel a realtime voice response | `:316-319`; `session.py:2010-2013` |

**Server → client**

| `type` | Payload | Delivery | When / code |
|---|---|---|---|
| `session_started` (voice fields) | `voice:true, voice_provider, voice_model, voice_name, voice_transcription_language, voice_initiator: bool, voice_recording_enabled: bool, voice_session_update?: object, voice_connection_info?: ConnectionInfo, voice_connection_error?: str` | Direct | `_attach_voice_payload`, `orchestrator.py:782-891`. `voice_initiator:true` for the `voice_start` sender, `false` for a text `start` joining live voice. ⚠ An OpenAI ephemeral token is minted on every attach, including for non-initiators (`:880-884`). |
| `voice_event` wrapping `{"type":"voice_status","status":"summarizing"}` | | Direct to initiator (broadcast if none), **before** `session_started` | history summary stale and no live relay (`:836-867`) |
| `voice_owner_active` | `{active: true, owner_local_id: <msg.local_id\|null>}` / `{active: false, owner_local_id: <session local_id>}` | Broadcast (owner receives it too) | after `voice_start` (`:189-193`) / after `voice_ended` (`session.py:1229-1234`) |
| `voice_command` | `{command: <OpenAI Realtime client event>}` — e.g. `[conversation.item.create{function_call_output, call_id, output≤8000 chars}, response.create]` after a tool, or a silent text inject | **Owner only** (falls back to broadcast if the owner socket is stale/missing) — **openai only** | `orchestrator.py:1271-1310`; `openai_voice.py:408-424`; `voice_base.py:40-78`. `response.create` may be **deferred** while OpenAI has a response in flight and drained later (next inbound `voice_event`, after tool dispatch, or a 2 s-poll/45 s watchdog; gate stale after 15 s) (`orchestrator.py:1313-1488`; `openai_voice.py:330`). |
| `voice_event` | `{event: <object>}` — mirrored upstream provider events (qwen OpenAI-style typed events; google **type-less camelCase** objects such as `setupComplete`, `serverContent{inputTranscription, outputTranscription, modelTurn, turnComplete, interrupted}`, `toolCall`, `goAway`) **plus backend-synthesised events** (below) | **Broadcast** (WS providers only) | `orchestrator.py:949-961`. Not mirrored: audio deltas (`voice_relay.py:1131-1147`), Gemini `sessionResumptionUpdate` (`orchestrator.py:941-953`), VAD/transcription events during a `listen_recording` injection (`:926-931`). |
| `voice_audio_out` | `{audio: <base64 PCM at audio_out_format>}` | **Broadcast to every subscriber** | WS providers only (`orchestrator.py:912-918`) |
| `voice_connection_error` | `{detail: str}` | Broadcast | background relay start raised (`orchestrator.py:1009-1014`) |
| `tool_use` / `tool_result` | same shapes as §5.3; `tool_use_id` = OpenAI/Qwen `call_id` or Gemini call id; `is_error` = output JSON has an `"error"` key | Broadcast | `orchestrator.py:1547-1570,1601-1632,1491-1508`. ⚠ No `tool_executing`/`tool_progress`/`turn_complete`/`status` frames are emitted in voice mode. |
| `user_message` | `{text, source:"shared_inject"}` | Broadcast | voice-mode `inject_text` |
| `voice_ending` → `voice_ended` → `voice_stopped` → `voice_owner_active{active:false}` | `voice_ending`/`voice_ended`: `{reason, session_id:<local_id>}`; `voice_stopped`: `{}` (legacy alias) | Broadcast, in that order | `session.py:1097,1170,1191-1239`. `reason` ∈ `user_stop`, `agent_end` (the agent called `end_voice_session`, `orchestrator/tools/voice_control.py:32-67`), `client_disconnect`, `error`, `shutdown` (`session.py:1052-1053`). |
| `error` | top-level route errors (`not_voice_session`, `voice_restart_failed`, `voice_config_busy`, `orchestrator_stopping`, `voice_audio_failed`, `voice_event_failed`, `cannot_switch_voice`, …) | see §5.3 | |

**Backend-synthesised events delivered inside `voice_event.event`** (all through `_on_event_for_frontend`, e.g. `orchestrator/voice_relay.py:955-960,1282-1291`) — ⚠ they are *not* top-level message types:

| `event.type` | Fields | Source |
|---|---|---|
| `voice_status` | `status`: `preparing` (relay starting), `ready` (handshake done — qwen in `start()`, google on `setupComplete`), `reconnect_warning` (+`time_left`, google `goAway`), `reconnecting`, `summarizing` | `voice_relay.py:437-452,1159-1165,1218-1222,1643-1650` |
| `input_audio_buffer.speech_started` / `.speech_stopped` | — | backend manual VAD (`voice_relay.py:871-884`) |
| `voice_vad_state` | `state`: `listening`\|`thinking` (docstring lists `idle`, never emitted), `duration_ms`: int, `silero_prob`: float\|null; on each transition + 1 s heartbeat while listening | `voice_relay.py:889-960` |
| `voice_error` | `error: {category: quota_exceeded\|rate_limit\|auth\|model_unavailable\|context_full\|network\|provider_internal\|unknown, message, recoverable: bool, recovery_hint, provider_doc_url, raw_close_code, raw_close_reason, provider}` | `orchestrator/voice_errors.py:41-54,102-122`; `voice_relay.py:1275-1281,1385-1391` |
| `error` (legacy) | `error: {code: "voice_relay_failed", message}` — ⚠ `error` is an **object** here, a string at top level | `voice_relay.py:1282-1291,1392-1398` |

### 7.5 End-to-end flows

**A. OpenAI (WebRTC).**
1. Client → `voice_start{local_id, resume_sdk_id?, voice_*?}`. Backend fills defaults, creates/re-arms the session (Direct `status:connecting` on a new session).
2. Backend builds `voice_session_update` (system prompt incl. history summary + tool definitions) and mints an ephemeral token; **no relay** for WebRTC. Direct `session_started{voice_initiator:true, voice_session_update, voice_connection_info}`, then broadcast `voice_owner_active{active:true}`.
3. Client creates an `RTCPeerConnection` with the mic track and data channel `oai-events`, POSTs the SDP offer to `voice_connection_info.endpoint` with `Authorization: Bearer <ephemeral_token>`, `Content-Type: application/sdp`, applies the SDP answer (reference: `frontend/src/api/voice.ts:46-65`, `frontend/src/voice/transports/webrtc.ts:71`).
4. When the data channel opens, the **initiator** sends `voice_session_update` over it. Without this OpenAI runs with no prompt, tools or input transcription (`openai_voice.py:433-445`). Non-initiators must not.
5. Client mirrors **every** data-channel event to the backend as `voice_event{event}`; the backend persists transcripts (`conversation.item.input_audio_transcription.completed`, `response.output_audio_transcript.done`/`response.audio_transcript.done`, `response.done`), tracks response gating, and executes tools on `response.function_call_arguments.done` (name may come from an earlier `response.output_item.added`, args from accumulated deltas, `orchestrator.py:1523-1545`).
6. Backend broadcasts `tool_use`/`tool_result` and sends the result frames as owner `voice_command`s; the client writes each `command` into the data channel verbatim.
7. End: `voice_stop`, agent `end_voice_session`, or owner WS disconnect → lifecycle broadcasts (§7.4); client closes the peer connection.
⚠ For OpenAI there is **no server-side transcript mirror**: non-owner devices see only `tool_use`/`tool_result` live (transcripts appear in REST history afterwards).

**B. Qwen (relay, server-first handshake).**
1. As A.1–2 but `voice_connection_info.audio_relay:"backend"`, and the relay is started **in the background** after `session_started` is sent (`orchestrator.py:1002-1016`).
2. Relay: broadcast `voice_status:preparing` → open DashScope → wait ≤10 s for `session.created` (mirrored) → send `session.update` → open gate → broadcast `voice_status:ready` (`voice_relay.py:414-627`).
3. Manual VAD is on by default (env `QWEN_MANUAL_VAD=0` disables, `voice_vad.py:352-379`): server VAD off upstream; the relay runs Silero on the mic stream; on speech start it emits `speech_started` + `voice_vad_state:listening`; on stop `speech_stopped` + `voice_vad_state:thinking` and sends upstream `input_audio_buffer.commit` + `response.create`; a commit-only safety frame every 50 s of continuous speech (`qwen_voice.py:731-751`; `voice_relay.py:909-924`).
4. Client streams `voice_audio_in` (after `ready`); plays every `voice_audio_out` (owner only — see §7.6).
5. Tool calls executed server-side; results go upstream directly (gated `response.create`, `qwen_voice.py:797-886`).
6. **Barge-in is the client's job:** on `speech_started` while a response is in flight, flush playback and send `voice_event{"type":"response.cancel"}`. A cancel with no active response makes DashScope close the socket (reference: `frontend/src/hooks/useVoiceOrchestrator.ts:389-409`).
7. Keep-alive: 20 ms silence frame after 30 s of mic silence (`voice_relay.py:1037-1073`). End: graceful `input_audio_buffer.commit` (≤0.5 s) then close.

**C. Google Gemini Live (relay, client-first handshake).**
1. As B but the relay sends `{"setup":…}` first; gate opens on `setupComplete` → `voice_status:ready` (`voice_relay.py:528-553,1159-1165`). Pre-ready audio/control frames wait ≤15 s then are dropped.
2. Manual VAD default on (env `GEMINI_MANUAL_VAD=0`): `automaticActivityDetection.disabled`; speech start/stop → upstream `realtimeInput.activityStart`/`activityEnd` (`gemini_voice_base.py:573-638`).
3. Mirrored events have **no `type`**: switch on keys (`serverContent.inputTranscription.text`, `.outputTranscription.text`, `.interrupted`, `.turnComplete`, `toolCall.functionCalls[{id,name,args}]`). Tool replies go upstream as `toolResponse.functionResponses[{id,name,response:{output}}]`.
4. `goAway` → `voice_status:reconnect_warning{time_left}` → transparent reconnect with the resumption handle → `reconnecting` → `ready` (`voice_relay.py:1211-1263`).

### 7.6 Ownership semantics

- **Owner** = the socket that most recently completed a `voice_start` (`orchestrator.py:180-184`; `session.py:397-428`). A device re-sending `voice_start` takes ownership.
- **Owner-only traffic:** `voice_command`, `voice_status:summarizing`, `session_started{voice_initiator:true}`.
- **Everything else is broadcast** — including `voice_audio_out` and all `voice_event`s. A non-owner must **not** open a transport, play `voice_audio_out`, send `voice_audio_in`/`voice_event`, or react to barge-in events; it should show a read-only "voice active on another device" state driven by `voice_owner_active` and `session_started{voice:true, voice_initiator:false}`. ⚠ `owner_local_id` is the shared orchestrator `local_id`, identical for all devices, so it cannot identify *which* device owns voice; a device knows it is the owner only from its own `voice_initiator:true`.
- **Owner disconnect** (any reason, including a brief network blip) → ownership cleared and `end_voice("client_disconnect")` immediately; the orchestrator session stays (`orchestrator.py:376-404`). There is no grace period; re-arm with `voice_start` (→ `restart_voice` on the same session/JSONL, `:487-533`).
- **Non-owner disconnect** → unsubscribe only.
- **Reconnect while voice live, same config** → re-subscribe, `session_started{voice_initiator:true}`, dead relay rebuilt (`:589-602,893-907`).
- **Reconnect with a different voice config** → `voice_config_busy` if a turn is running, else `pool.stop_orchestrator()` + fresh session. ⚠ This clears **all** subscribers (`api/pool.py:583`); passive peers silently lose their subscription and nobody receives `voice_ended` for the old connection.
- **Start during teardown** → waits ≤5 s, else Direct `error: orchestrator_stopping` (`orchestrator.py:447-460`; `voice_timeouts.py:47`).

### 7.7 Timeouts and reconnection (WS providers)

| Item | Value | Code |
|---|---|---|
| wait for prior teardown | 5 s | `orchestrator/voice_timeouts.py:47` |
| graceful shutdown frames | 0.5 s | `voice_timeouts.py:54` |
| server-first `session.created` wait | 10 s, then continue | `voice_relay.py:534` |
| handshake gate for outbound frames/audio | 15 s, then drop | `voice_relay.py:686,802` |
| reconnect handshake | 15 s | `voice_timeouts.py:62` |
| upstream WS open | 15 s | `qwen_voice.py:946`; `gemini_voice.py:100,146` |
| manual-VAD safety commit | 50 s (qwen only) | `voice_timeouts.py:68` |
| keep-alive | 30 s silence (qwen only) | `voice_timeouts.py:75` |
| VAD state heartbeat | 1 s | `voice_timeouts.py:81` |
| deferred `response.create` watchdog | poll 2 s, max 45 s | `orchestrator.py:1430-1435` |

Reconnect policies (`voice_relay.py:1295-1398,1480-1711`; `voice_reconnect.py:102-124`): `provider_goaway` (uncapped, keeps handle, surfaced), `recoverable_error` (provider-classified; capped at 2 per relay lifetime, counter never reset; surfaced as `reconnecting`), `stale_handle` (google 1008; one silent retry). During a reconnect, client `voice_event`s are buffered (cap 64) and flushed; mic audio is not buffered. On final failure the relay emits wrapped `voice_error` then wrapped legacy `error{code:"voice_relay_failed"}` but does **not** end voice (see §8 G-21).

### 7.8 Talk mode, transcription, wake word

- **Talk mode** (turn-based audio, no realtime provider): `send_audio{audio:<b64>, format:"wav"|"mp3"|"webm"|"ogg"|…, text?}`. Response = normal orchestrator text-turn frames (`status:streaming` … `turn_complete` … `status:idle`). Non-wav/mp3 is converted to wav with ffmpeg (`orchestrator/session.py:1817`). If the current model is not audio-capable, the session **permanently** switches to `gpt-audio` without broadcasting `model_changed` (`session.py:1802-1813`; `orchestrator/config.py:137`). Works during a voice session (provider swapped for that turn).
- **No transcription / wake-word endpoint exists** on the backend. Wake-word confirmation is done client-side by calling OpenAI Whisper directly with the key from `GET /api/config/openai-key`; after confirmation the client sends `voice_start` (re-arm).
- Agent tools that affect voice: `end_voice_session(farewell_message?)` → `end_voice("agent_end")`; `listen_recording` replays stored audio into a WS-provider session (rejected for openai) and suppresses VAD/transcription mirrors during the injection (`orchestrator/tools/audio_playback.py:28-258`).
- Recording: when `voice_recording_enabled` (config), WebRTC clients stream both channels via `voice_recording_chunk`; WS-provider audio is recorded server-side. Recorder sample rate is fixed at 24 kHz (`session.py:595-600`), so 16 kHz mic input (qwen flash, google) is mis-scaled.

---

## 8. Contract gotchas and recommended backend fixes

### 8.1 Gotchas (numbered; referenced from the sections above)

**Transport**

- **G-1 — nginx caps request bodies at 1 MiB.** `~/nginx-server.conf` on the Jetson sets no `client_max_body_size`, so uploads > 1 MiB through `https://192.168.0.200` get nginx's HTML 413 (confirmed in the nginx error log: `client intended to send too large body: 2862307 bytes … POST /api/uploads`). The app's own limit is 200 MB. Clients must handle a non-JSON 413 body.
- **G-2 — frame types are asymmetric.** Server → client is always **binary** frames of UTF-8 JSON; client → server must be **text** frames. A binary client frame kills the socket with no close frame (**LIVE**). Web must set `binaryType = "arraybuffer"` and decode with `TextDecoder`; OkHttp must handle `onMessage(ByteString)`.
- **G-3 — no client authentication at all,** and `GET /api/config/openai-key` returns the raw OpenAI key to any LAN caller. Do not expose the backend beyond the LAN/tailnet.

**Identity**

- **G-12 — history endpoints accept only the sdk/jsonl id, never `local_id`.** `GET /api/sessions` comments claim "the detail endpoints also accept local ids via the pool" (`api/routes/sessions.py:99-102`) but `/messages`, `/preview`, `/{id}` only consult the store. The "(active session)" placeholder rows whose `session_id` is a `local_id` therefore 404 on `/messages`.
- **G-13 — `session_id` is overloaded** (§2): chat and orchestrator `session_started.session_id` = `local_id`; chat `turn_complete.session_id` = sdk id; REST `session_id` = sdk/jsonl id; `nested_session_event.session_id` / `agent_session_*.session_id` = local_id. `session_terminated` has **no** `session_id`, only `sdk_session_id`.
- **G-14 — the orchestrator never tells a client its jsonl id over its own socket.** Orchestrator `session_started` has no `jsonl_id`/`sdk_session_id`, and orchestrator `turn_complete` has no `session_id`. For a *new* orchestrator `jsonl_id == local_id`; for a resumed one it is the `resume_sdk_id` the client sent. Otherwise read it from `pool/live` or `agent_session_opened`.
- **G-15 — the orchestrator is a global singleton.** A second device must attach with the *same* `local_id` (discover via `pool/live`), else `error: orchestrator_active`. `pool/live` reports the orchestrator with a hard-coded `status:"idle"`, `cost:0`, `turns:0`.
- **G-16 — `start` may hand you a different session than you asked for.** Chat-WS `start` with a `resume_sdk_id` that is already live under another `local_id` returns *that* local_id in `session_started.session_id` (`api/pool.py:235-240`); a resume whose SDK state is gone silently becomes a **fresh** session (`api/pool.py:267-287`). Always adopt `session_started.session_id`.

**Streaming**

- **G-4 — `turn_complete` is not a reliable turn terminator.** It is *absent* for: interrupted chat turns (task cancelled; only the interrupting WS gets `status: interrupted`), orchestrator turns ending in `error` (incl. `interrupted`), and **all voice-mode activity** (voice `tool_use`/`tool_result` arrive with no `status`/`turn_complete` framing). This is what breaks Android ordering (§4.4).
- **G-5 — queued prompts are echoed twice** to non-sender subscribers (`user_message{queued:true}` at queue time, then `user_message` again at dispatch, `api/pool.py:1062-1066` + `905-909`).
- **G-6 — one chat WS, many subscriptions.** Sending `start` for another `local_id` on a socket without `stop` first does **not** unsubscribe the previous session (`api/routes/chat.py:148-151` overwrites the local variables only). Events from both sessions then arrive on the socket, and **no chat event carries a session id**. Always `stop` before re-`start`, or use one socket per session.
- **G-7 — status vocabularies differ.** Chat WS: `connecting`, `processing`, `retrying`, `interrupted` (direct only) — never `idle`/`streaming`. Orchestrator WS: `connecting`, `streaming`, `idle`, `interrupted` (broadcast). `pool/live` uses the `SessionStatus` enum (`idle|streaming|tool_use|thinking|interrupted|disconnected`).
- **G-17 — a pending permission can disappear silently.** Permissions still pending when a turn ends are auto-denied *after* the listener detached, so `permission_resolved` is never broadcast (§4.6). Clear permission UI on turn end/interrupt/stop.
- **G-18 — `seq` subtleties.** Gaps are normal; `session_stalled` carries a duplicate (previous) seq; `user_message`, `status`, `compact`/`command` output and the synthetic receive-loop-exit `turn_complete` are unsequenced; orchestrator frames never have seq; Qwen/Gemini sessions never have seq.
- **G-19 — `compact_complete` has two shapes:** chat `{trigger, summary}` vs orchestrator `{trigger:"manual", tokens_before, tokens_after}`. Chat compaction also streams a `turn_complete`.
- **G-20 — no tool-input streaming, no block indices, no message ids** on the wire; `tool_result.tool_use_id` can be `""` (string tool results without `parent_tool_use_id`, `manager/claude/session.py:1128-1133`); subagent (Task) events may appear flat (AMBIGUOUS, §4.4).
- **G-22 — the orchestrator does not echo typed `send`s** to other subscribers (no `user_message`, unlike chat sessions), and wake turns stream an assistant reply with no user message at all.
- **G-23 — slash `command` output is direct-only and unsequenced,** and `sm.command()` runs outside the pool's per-session lock (`api/routes/chat.py:445-461`).
- **G-24 — the chat WS rejects `ping`** (`unknown_type`) while the orchestrator WS accepts it.

**History**

- **G-8 — orchestrator history is lossy.** `tool_use`, `tool_result`, `background_notification` lines are invisible to REST; text-mode assistant text is one joined line written after the turn's tools (§6.3). History and live views of the same orchestrator turn differ structurally. (Android currently re-fetches this on every orchestrator reconnect, replacing a correct live view with the lossy one.)
- **G-9 — thinking in history is inconsistent.** Claude thinking blocks become **empty assistant messages** (`blocks: []`, `text: ""`) because the adapter reads `block["text"]` but Claude stores `block["thinking"]`; Qwen/Gemini thinking becomes an ordinary **`text` block** indistinguishable from the answer (`manager/protocol.py:453-456`).
- **G-10 — no join key between history and live.** No message/block ids or seq in REST. Combined with `resume_from` replay, a client that loads REST history and then replays from a stored checkpoint gets duplicates (the current web client does this).
- **G-11 — `source` and other JSONL metadata are dropped.** Voice user turns are recognisable only by the `"[voice] "` / `"[voice, recording: …] "` text prefix; audio turns by `"[audio:<fmt>] "`; Claude CLI meta/command lines arrive as ordinary user messages.
- **G-25 — `GET /api/sessions/{id}`** ignores custom titles and always reports `is_orchestrator:false`; it also returns the whole conversation unpaginated. Use `/messages` + the list endpoint.
- **G-26 — three different message-counting schemes.** REST indices count every user/assistant JSONL line (incl. tool_result wrappers and thinking placeholders); `truncate/fork drop_last_n` counts only *visible* lines (excludes tool_result-only user lines); `message_count` counts raw lines. A UI that computes `drop_last_n` from its rendered list (which merges/drops messages and adds client-only ones) will rewind to the wrong place. Compute `drop_last_n` from REST data using the visible-message rule (`manager/protocol.py:383-411`).
- **G-27 — mutations are not broadcast.** Rename, delete, config/session-config PUT, visualization rename produce no WS event; `DELETE /api/sessions/{id}` does not close a live pool session. Other devices see changes only on refetch.

**Voice**

- **G-21 — zombie voice after a fatal relay failure.** The relay emits wrapped `voice_error` + legacy `error{code:"voice_relay_failed"}` but never calls `end_voice`, so no `voice_ended` arrives and the session stays "voice" while mic frames are silently dropped. Recovery: `voice_stop` then `voice_start`. If the relay failed to *start* (state ENDED), `voice_stop` is a no-op with no ack; recovery is a full `stop` + `voice_start` (`orchestrator/session.py:914-922,969-980,1073-1074`).
- **G-28 — backend-synthesised voice events are nested.** `voice_status`, `voice_vad_state`, `voice_error`, synthetic `speech_started/stopped` arrive as `voice_event.event.type`, not as top-level types (the current web client has dead top-level `case "voice_vad_state"`/`"voice_error"` handlers). `error` has two shapes (top-level string code vs nested `{code, message}` object).
- **G-29 — broadcast audio.** `voice_audio_out` and every mirrored `voice_event` go to all subscribers; only `voice_command` is owner-scoped. `owner_local_id` is the shared orchestrator id, useless for telling devices apart.
- **G-30 — voice config drift on reconnect** destroys the orchestrator and clears every subscriber without notifying them.
- **G-31 — any owner socket drop ends voice immediately** (no grace period).
- **G-32 — OpenAI-specific duties fall on the client:** forward `voice_session_update` on data-channel open (initiator only), mirror every data-channel event, execute every `voice_command`. Non-owners get no live transcripts for OpenAI sessions (fixed 2026-10-10: the backend re-broadcasts the owner's mirrored transcript events, spec 12 VT-2). Ephemeral tokens are minted even for non-initiators.
- **G-33 — provider/model defaults differ by entry point:** WS `voice_start` uses `assistant_config.json` defaults; REST `POST /voice/session` uses registry defaults; unknown provider ids silently become `openai`; `GET /api/orchestrator/voice/models` omits `google` on its live path.
- **G-34 — `interrupt` does not stop a realtime voice response;** barge-in is client-driven (`response.cancel` for qwen, only while a response is active).
- **G-35 — no `voice_status: ready` after a Qwen reconnect** (only Gemini re-emits it).

**Misc / dead or misleading surface**

- **G-36 — `POST /api/orchestrator/audio` is dead:** returns `queued` but only broadcasts an `audio_upload` frame nothing consumes. Use WS `send_audio`.
- **G-37 — `default_model` in `assistant_config.json` is not used by the orchestrator backend.** `OrchestratorConfig.load()` reads env `ORCHESTRATOR_MODEL` (default `gpt-audio`, `orchestrator/config.py:132,299`); `GET /api/orchestrator/models` reports a hard-coded `default_model:"claude-sonnet-4-5-20250929"`. A client wanting the configured model must send `set_model` after `start`. `voice_mic_gain` is saved but unused; `harness_model` may lack keys for harnesses added after the file was created (**LIVE**: no `gemini` key).
- **G-38 — static serving quirks:** unknown paths return 200 `index.html`; `HEAD` → 405; `context/public/` is served only when `frontend/dist` exists; visualization `url`s are not percent-encoded.
- **G-39 — watcher events go only to orchestrator sockets** (even ones that never sent `start`); chat sockets never get `agent_session_opened/closed`. Clients must check `is_orchestrator` on `agent_session_closed` before closing a tab (the current web client closes its own orchestrator tab here).
- **G-40 — closing the orchestrator via `POST /api/sessions/{local_id}/close`** drops all its subscribers without sending them anything; they learn only via the watcher event `agent_session_closed{is_orchestrator:true}`.

### 8.2 Recommended small backend fixes (NOT applied — for the backend owner to decide)

Ordered by impact on the rebuild. None changes an existing field; all are additive or bug fixes.

1. **nginx:** add `client_max_body_size 200m;` to both server blocks in `~/nginx-server.conf` (infra, fixes G-1).
2. **Terminal event for every turn:** in `pool.cancel_turn` broadcast `{"type":"status","status":"interrupted"}` to *all* subscribers (`api/pool.py:1191-1234`) instead of only the caller in `chat.py:208-210`; on the orchestrator, emit `turn_complete` (or a `turn_end{reason}`) on the error/interrupt paths in `orchestrator/agent.py:119,132,169,319` (fixes G-4).
3. **Orchestrator history fidelity:** teach the Claude adapter (or a dedicated orchestrator adapter) to fold top-level `tool_use`/`tool_result` lines into blocks, and persist each assistant text block when `TextComplete` fires instead of joining at turn end (`orchestrator/session.py:1863-1893`) (fixes G-8).
4. **Thinking in history:** in `manager/protocol.py:453-456` read `block.get("thinking") or block.get("text")` and emit `type:"thinking"` (add `"thinking"` to `ContentBlockResponse.type`) (fixes G-9).
5. **Stable ids:** expose the Claude JSONL line `uuid` / `message.id` as `MessagePreview.id`, and add `message_id` + `block_index` (from the SDK `StreamEvent` `index`) to live `text_*`/`thinking_*`/`tool_use` frames — gives clients a real merge key (fixes G-10, G-20).
6. **Clear `_last_yielded_seq` before yielding `SessionStalled`** (`manager/claude/session.py:585`) and route the receive-loop-exit synthetic `TurnComplete` through `_inject_event` (fixes G-18).
7. **Inject `PermissionResolved` before detaching the inbox** in `send()`'s `finally` (`manager/claude/session.py:622-630`) (fixes G-17).
8. **Queued echo:** in `pool.send` skip the `user_message` broadcast for prompts that were already announced with `queued:true` (pass a flag through `_PendingPrompt`) (fixes G-5).
9. **Chat WS re-start:** unsubscribe the previous `session_id` at the top of `_handle_start` (`api/routes/chat.py:148-151`); optionally stamp `local_id` on every broadcast payload (fixes G-6).
10. **Orchestrator ids & echoes:** add `jsonl_id` to orchestrator `session_started`; broadcast `user_message` (excluding the sender) for typed `send`s (fixes G-14, G-22).
11. **Relay fatal failure → `end_voice("error")`** so `voice_ended` fires (fixes G-21); include a client-supplied `device_id` in `voice_start` and echo it as `owner_device_id` in `voice_owner_active` (fixes G-29).
12. **Reply to binary client frames** with `error: binary_not_supported` instead of dropping the connection (fixes G-2 robustness).
13. **History endpoints accept `local_id`** by resolving through `pool.get(id).sdk_session_id` (fixes G-12).
14. **Small cleanups:** honour `assistant_config.default_model` in `OrchestratorConfig.load()` and report it from `/api/orchestrator/models`; make `POST /voice/session` fall back to config defaults; include `google` in live voice models; delete or fix `POST /api/orchestrator/audio`; make `GET /api/sessions/{id}` use stored titles/`is_orchestrator`; report real orchestrator status (`is_busy`) in `pool/live`; construct `AudioRecorder` with the provider's input sample rate; accept `ping` on the chat WS.
