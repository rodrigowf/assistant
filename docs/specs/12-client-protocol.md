# 12 — Client Protocol and Data-Layer Specification (normative)

> **Status:** draft for review · 2026-10-03 · branch `frontend-refactory`
> **Applies to:** `frontend-next/` (web and its Safari 12 compat build) and `android-next/` (main app and lite app).
> **Inputs:** `inventory/01-backend-api.md` (the API contract), `inventory/02-web-frontend.md`, `inventory/03-android-app.md`. Where they disagree, the backend code wins; this document cites it as `path:line` (repo root).
> **Conformance:** `shared/protocol-fixtures/` holds JSON fixtures. Every client data layer MUST pass all of them (§11).

The key words MUST, MUST NOT, SHOULD, SHOULD NOT and MAY are used as in RFC 2119. A "client" is any of: web, compat, Android main, Android lite. Text marked *Rationale* is not normative.

This document specifies **what the data layer does**. It does not specify screens, layout or the audio engines. It references gotchas from chapter 01 as **G-n** and bugs from chapters 02/03 as **W-n** and **A-n** (the full table is in §10).

---

## Table of contents

1. [Architecture of the client data layer](#1-architecture-of-the-client-data-layer)
2. [Domain model](#2-domain-model)
3. [Transport and connection management](#3-transport-and-connection-management)
4. [The conversation reducer](#4-the-conversation-reducer)
5. [History (REST) and merging with live events](#5-history-rest-and-merging-with-live-events)
6. [User actions as protocol sequences](#6-user-actions-as-protocol-sequences)
7. [Voice signaling](#7-voice-signaling)
8. [Settings and configuration](#8-settings-and-configuration)
9. [Visualizations and Memory](#9-visualizations-and-memory)
10. [Bug-prevention rules](#10-bug-prevention-rules)
11. [Conformance fixtures](#11-conformance-fixtures)
- [Appendix A — Backend fixes required](#appendix-a--backend-fixes-required)
- [Appendix B — Open questions for the product owner](#appendix-b--open-questions-for-the-product-owner)

---

## 1. Architecture of the client data layer

Both platforms MUST implement the same five layers. Only layer 5 (UI) and the audio engines differ per platform.

```
 ┌───────────────────────────────────────────────────────────────────────────┐
 │ 5  UI (React / Compose) — reads immutable snapshots, dispatches actions   │
 ├───────────────────────────────────────────────────────────────────────────┤
 │ 4  Stores: SessionDirectory (list + pool), Config, Visualizations, Memory │
 ├───────────────────────────────────────────────────────────────────────────┤
 │ 3  Conversation reducer — PURE function (state, input) -> state  (§4, §5) │
 ├───────────────────────────────────────────────────────────────────────────┤
 │ 2  Connection managers: one per open conversation (§3)                    │
 │    seq dedupe, start/re-start, replay, pre-start buffer, reconnect        │
 ├───────────────────────────────────────────────────────────────────────────┤
 │ 1  Transport: WebSocket (binary in / text out), REST client                │
 └───────────────────────────────────────────────────────────────────────────┘
```

Rules:

- **L-1.** The conversation reducer MUST be a pure, synchronous, deterministic function with no I/O, no timers and no clock reads. Everything it needs (REST pages, data-channel events, user actions) arrives as an *input*. This is what makes the fixtures (§11) runnable on both platforms. The web implements it in TypeScript; Android implements it in pure Kotlin in the shared `:core` module.
- **L-2.** All inputs for one conversation MUST be applied in a single ordered stream (one queue, one consumer). The transport → reducer path MUST be lossless: no bounded buffer that drops on overflow. *Rationale:* Android's `MutableSharedFlow(extraBufferCapacity=64)` + `tryEmit` silently drops text deltas under load (A-3.3).
- **L-3.** High-rate audio frames (`voice_audio_out`, `voice_audio_in`) MUST NOT go through the conversation input queue. The connection manager routes them directly to the audio engine.
- **L-4.** The reducer state MUST be exposed to the UI as immutable snapshots, or as observable state with structural sharing. Mutations from more than one thread are forbidden (A-4.4.7).

---

## 2. Domain model

### 2.1 Identifiers (client rules)

The backend overloads `session_id` (G-13). Clients MUST use these names internally and never the bare word `session_id`:

| Client name | Backend source | Notes |
|---|---|---|
| `localId` | `start.local_id`; chat/orchestrator `session_started.session_id`; `pool/live[].local_id`; `agent_session_*.session_id`; `nested_session_event.session_id` | Minted by the client (UUID v4, with the non-crypto fallback for plain-HTTP origins, 02 §7 item 14). It is the pool key and the key of an open conversation view. |
| `sdkId` | chat `turn_complete.session_id`; `pool/live[].sdk_session_id`; `agent_session_opened.sdk_session_id`; `session_terminated.sdk_session_id`; REST `session_id` | Key for all history REST calls (G-12). For the orchestrator it is the **jsonl id** (`pool/live` row, or `resume_sdk_id` when resuming, or `localId` for a brand-new orchestrator, G-14). May be `null` for a new agent session until its first turn ends. |
| `streamId`, `seq` | chat frames (Claude only) | Resume cursor (§3.6). |
| `toolUseId` | `tool_use_id` | Joins a tool call to its result (§4.5). May be `""` (G-20, Appendix A BF-1). |
| `requestId` | `request_id` | Permission prompt id. |

- **ID-1.** On every `session_started`, the client MUST adopt `session_started.session_id` as the conversation's `localId`, even if it differs from the one it sent (G-16). The view key MUST be re-keyed without losing state.
- **ID-2.** The client learns `sdkId` from, in order of preference: the first non-null `turn_complete.session_id`; `agent_session_opened.sdk_session_id`; `GET /api/sessions/pool/live`. After the first `turn_complete` with no `session_id` (the Gemini harness never sends one), the client MUST do one `GET /api/sessions/pool/live` to learn it.
- **ID-3.** Every history REST call MUST use `sdkId`. A view with `sdkId == null` MUST disable rename, rewind, fork, session config and REST history, and SHOULD say why ("available after the first reply"). It MUST NOT silently ignore the action (W-6.2).
- **ID-4.** On an orchestrator conversation, a non-empty `session_started.jsonl_id` MUST be adopted as `sdkId` (the orchestrator's `sdkId` is its JSONL id, G-14; backend O-3). Agent conversations ignore the field. *Rationale:* every orchestrator `session_started` carries it (`api/routes/orchestrator.py:531,558,610,627,780`), so history REST works at once, with no `pool/live` lookup. Fixture `orchestrator_agent_approvals_and_jsonl_id`.

### 2.2 SessionRef

```ts
SessionRef = {
  localId: string,
  sdkId: string | null,
  kind: "agent" | "orchestrator",
  provider: "claude" | "qwen" | "gemini" | null,   // REST SessionInfo.provider; null for orchestrator
  title: string,                                    // derived, see §6.10
  live: boolean,                                    // present in GET /api/sessions/pool/live
  liveStatus: "idle"|"streaming"|"tool_use"|"thinking"|"interrupted"|"disconnected"|null,
}
seqCapable(ref) = ref.kind == "agent" && ref.provider == "claude"
```

Only Claude agent sessions carry `seq`/`stream_id` (01 §4.3, G-18). Qwen and Gemini agent sessions and the orchestrator have no replay.

### 2.3 Conversation state

One `Conversation` exists per open view (tab on web, session page on Android). Its full state:

```ts
Conversation = {
  ref: SessionRef,
  entries: Entry[],                       // the rendered timeline, oldest first
  tools: Map<toolUseId, ToolBlock>,       // every tool block with a non-empty id, in ANY entry
  perms: Map<requestId, PermissionBlock>,
  orphanResults: OrderedMap<toolUseId, Result>,  // results whose tool_use is not known yet (§4.5)
  unattributed: Result[],                 // results with tool_use_id "" that could not be placed
  queue: QueuedPrompt[],                  // prompts queued behind a running turn (the "tray")
  dispatchedFromTray: string[],          // texts popped from the tray at status{processing}; swallows a pre-O-6 re-echo (I-12); cleared on turn end

  status: SessionStatus,                  // §2.5
  inTurn: boolean,
  turnDepth: number,                      // orchestrator only (nested status:streaming)
  turnIsLocal: boolean,                   // orchestrator only
  turnHasContent: boolean,                // orchestrator only
  localTurnsPending: number,              // orchestrator only: local send/send_audio/compact not yet started
  promptSinceTurnEnd: boolean,
  compactPending: boolean,
  pendingInjects: string[],               // orchestrator inject_text texts awaiting their echo
  agentApprovals: { localId, request_id, tool_name, tool_input }[],  // orchestrator: agent permissions seen via nested_session_event (PM-5)
  voiceActive: boolean,                   // orchestrator: voice live on ANY device (§7)
  openVoiceUser: UserEntry | null,        // Gemini transcript being coalesced (§4.7)
  speechAnchor: number | null,            // §4.7

  stall: { elapsed_seconds, last_tool_name, last_tool_use_id } | null,
  termination: { reason, detail, sdk_session_id } | null,
  connectionBanner: { code, detail } | null,   // transport/start errors; never an entry
  gapPossible: boolean,                   // live activity may be missing; UI offers "Reload"

  checkpoint: { stream_id, seq } | null,  // §3.6, in memory only
  counters: { cost: number, turns: number, contextTokens: number | null, contextWindow: number | null },
  history: { loaded: boolean, startIndex: number, totalCount: number, hasMore: boolean },

  // reducer bookkeeping
  pendingSplit: TextBlock | ThinkingBlock | null,  // streaming block closed by an interleaved entry (I-5)
  converting: boolean,                    // a history page is being converted into a scratch list (H-6)
  expectStopAck: boolean,                 // our own stop/close: swallow the session_stopped reply

  // connection-manager bookkeeping (§3.6, §5.2)
  awaitingSessionStarted: boolean, startRequest: object | null, preStart: Frame[],
  reloading: boolean, reloadBuffer: Frame[],
}
```

Initial values: empty collections, `status = "connecting"` until the first `session_started` (or the `pool/live` status per ST-2), all booleans `false`, counters `0`/`null`, `history.loaded = false`.

### 2.4 Entries and blocks

The timeline is a list of **entries**. An entry is a user prompt, an assistant **run**, or a notice. Blocks live only inside assistant runs.

```ts
Entry = UserEntry | AssistantEntry | NoticeEntry

UserEntry = {
  kind: "user", text: string,
  origin: "local"     // typed or sent by this client
        | "echo"      // user_message from another client / REST inject
        | "voice"     // speech transcript (live or "[voice] …" history line)
        | "audio"     // voice message (local send_audio, user_message{source:"voice_message"}, "[audio:fmt] …" history line)
        | "inject"    // shared text / uploaded file link (inject_text, "[shared …]" history line)
        | "history",  // any other user line loaded from REST
  state: "sent" | "pending",      // "pending": local inject awaiting its echo
  streaming?: boolean,            // true only while a Gemini transcript is still growing
}

AssistantEntry = { kind: "assistant", blocks: Block[] }

NoticeEntry = {
  kind: "notice",
  notice: "error"        // a turn failed (send_failed, api_error, turn_complete.is_error, …)
        | "interrupted"  // the turn was interrupted
        | "compaction"   // context compacted (live compact_complete, or history summary line)
        | "background"   // the assistant started a turn this view did not prompt (§4.4.3), or a CLI <task-notification>
        | "command",     // CLI slash-command echo/output lines from history
  text: string,          // error: detail || code; compaction: summary or ""; others: "" or the raw line
  data?: object,         // e.g. {code}, {trigger, tokens_before, tokens_after}
}

Block = TextBlock | ThinkingBlock | ToolBlock | PermissionBlock

TextBlock     = { type: "text",     text, streaming: boolean, scope: "turn"|"voice", origin: "live"|"history",
                  implicitlyClosed?: boolean, continuationOf?: TextBlock }
ThinkingBlock = { type: "thinking", text, streaming: boolean, scope, origin, implicitlyClosed?, continuationOf? }
ToolBlock     = { type: "tool", tool_use_id: string, tool_name: string, tool_input: object,
                  status: "running" | "done" | "error" | "no_result",
                  output: string | null, inferred?: boolean, executing?: boolean,
                  progress?: { elapsed_seconds, message }, scope: "turn"|"voice", origin }
PermissionBlock = { type: "permission", request_id, tool_name, tool_input,
                  state: "pending" | "allowed" | "denied",
                  responder: "user"|"orchestrator"|"system"|null, message: string|null }
```

Mapping to the requested concept list:

| Concept | Representation |
|---|---|
| text, thinking | `TextBlock`, `ThinkingBlock` |
| tool_use + result + status | one `ToolBlock` (the result is merged into it, never a separate block) |
| permission | `PermissionBlock` inside the run that asked; resolution updates it in place |
| voice transcript | user side: `UserEntry{origin:"voice"}`; assistant side: `TextBlock{scope:"voice"}` in the current run |
| system notes | `NoticeEntry` (error, interrupted, command) |
| background notification | `NoticeEntry{notice:"background"}` |
| compaction marker | `NoticeEntry{notice:"compaction"}` |
| termination | conversation-level `termination` banner state, not an entry |
| transport errors | conversation-level `connectionBanner`, **never** an entry (A-8.2) |

### 2.5 Session status machine

`SessionStatus` is one of: `connecting`, `idle`, `processing`, `streaming`, `thinking`, `tool_use`, `retrying`, `compacting`, `stopped`, `terminated`. Connection state is tracked separately (§3.3) as `offline | connecting | open | subscribed | failed`. `busy(status) := status ∈ {processing, streaming, thinking, tool_use, retrying, compacting}`.

```
                    start sent / status:connecting
   (new view) ──────────────────────────────────────▶ connecting
   connecting ── session_started (no turn in flight) ─▶ idle
   idle ── local send / status:processing ───────────▶ processing          (agent)
   idle ── status:streaming ─────────────────────────▶ streaming           (orchestrator)
   processing|streaming|thinking|tool_use:
        text_delta/text_complete ▶ streaming · thinking_* ▶ thinking · tool_use ▶ tool_use
        status:retrying ▶ retrying
   any busy ── endTurn (turn_complete, status:interrupted, status:idle, turn-failure error,
               superseded by the next prompt) ─────────▶ idle
   idle ── local compact ──▶ compacting ── compact_complete/turn_complete/error ──▶ idle
   any ── session_terminated ──▶ terminated  (session_stopped that follows does NOT change it)
   any ── session_stopped (not our own stop/close) ──▶ stopped
   stopped|terminated ── a new start succeeds ──▶ idle
```

- **ST-1.** Unknown `status` values from the server MUST map to the current status unchanged, never to "Ready" (W-6.2, A-1.4). `retrying` MUST be shown as a distinct state.
- **ST-2.** On every (re)subscribe (each `session_started` of an agent socket) the client MUST fetch `pool/live` and take its row's `status` as authoritative: `streaming|tool_use|thinking` ⇒ `inTurn = true` and that status; any other status (`idle`, `interrupted` — Codex, Gemini and Qwen keep it after a stopped turn — or `disconnected`) while `inTurn` ⇒ `endTurn("unknown")`. A turn that ended while the socket was away, or ended without a terminal frame, otherwise stays "running" forever. The orchestrator row always says `idle` (G-15) and MUST NOT be used this way.
- **ST-3.** Tab/page indicators MUST distinguish: connecting, subscribed-idle, busy, stopped, terminated, connection failed (W-6.1).

---

## 3. Transport and connection management

### 3.1 Frames

- **T-1.** Server→client frames are **binary** WebSocket frames holding UTF-8 JSON (G-2). The web MUST set `binaryType = "arraybuffer"` and decode with `TextDecoder`. Android MUST handle `onMessage(ByteString)`. Clients SHOULD also accept text frames.
- **T-2.** Client→server frames MUST be **text** frames. A client MUST NEVER send a binary frame: the server drops the socket with no close frame.
- **T-3.** A frame that does not parse as a JSON object, or has no string `type`, MUST be logged and ignored. It MUST NOT reach the reducer.
- **T-4.** Clients MUST NOT send `ping` on the chat WS (it answers `error: unknown_type`, G-24). Keep-alive relies on protocol pings: OkHttp `pingInterval(30s)` on Android; the browser's own on web. Android MUST NOT add an app-level heartbeat (03 §3.3). Server `{"type":"ping"}` frames on the orchestrator WS MUST be ignored.
- **T-4a.** The server refuses requests from other web sites (`backend/api/guard.py`, [backend.md](../architecture/backend.md#auth-and-the-browser-origin-guard)): every `/api/*` `POST`/`PUT`/`PATCH`/`DELETE` and every WebSocket handshake with `Sec-Fetch-Site: cross-site` or an untrusted `Origin` (`null` included) gets 403 (sockets: closed with 1008 before `accept`), and every `/api/*`, `/memory`, `/uploads` and `/projects` request with an untrusted `Host` (DNS rebinding) gets 403. Trusted origins: the server's own host name (any scheme/port), the web dev/mock servers (5450/5451/8799) on a trusted host, `chrome-extension://`, `ARCHIE_TRUSTED_ORIGINS`. CORS echoes only trusted origins (no `*`). Clients MUST call the API from the origin that served them (web: `location.origin`) or send no `Origin` at all (native clients); a page served elsewhere needs its origin in `ARCHIE_TRUSTED_ORIGINS`. The web apps and `context/public/` pages are never refused.

### 3.2 Sockets per conversation

- **T-5.** Each open **agent** conversation MUST use its own chat WS (`/api/sessions/chat`). A client MUST NOT send `start` for a different `localId` on a socket that is already subscribed (G-6: chat frames carry no session id). To switch a socket to another session it MUST first send `stop` and wait for `session_stopped`.
- **T-6.** A client app instance MUST use **exactly one** orchestrator WS (`/api/orchestrator/chat`). Text `start`, `voice_start`, `voice_stop`, voice relay frames and the watcher events all go over that socket. *Rationale:* every orchestrator frame is broadcast to every subscribed socket; two sockets in one client double every event. This also removes the web's `stop`-then-`voice_start` dance (W-10).
- **T-7.** The orchestrator WS SHOULD be open whenever the app is in the foreground, even with no orchestrator view, because it is the only source of the pool watcher events (§3.7, G-39). It MAY stay unsubscribed (no `start`) until an orchestrator view opens.
- **T-8.** One extra chat WS MAY be opened, unsubscribed, only to send `permission_response{session_id: <agent localId>}` for a session that has no open view, and only as the fallback for a server without `POST /api/sessions/{localId}/permission` (§6.9). The web keeps this fallback; Android does not (it shows "Open that session to answer").

### 3.3 Connect, `start` and re-`start`

```
ConnState = offline | connecting | open | subscribed | failed

connect(conv):
  connState = connecting
  ws = open(url(conv.ref.kind))           // wss when the page/base URL is https
  on open:
     connState = open
     sendStart(conv)                       // EVERY time the socket opens (load-bearing, 02 §7.2)
  on frame: connectionManager.onFrame(conv, frame)
  on close/error:
     connState = offline
     conv.connectionBanner = {code: "disconnected"}   // banner only, never an entry (A-8.2)
     if conv.kind == "orchestrator" and conv.inTurn: conv.gapPossible = true
     scheduleReconnect(conv)

sendStart(conv, userAction = false):
  msg = { type: "start", local_id: conv.ref.localId }
  if conv.ref.sdkId: msg.resume_sdk_id = conv.ref.sdkId
  if not userAction: msg.reattach = true      // OPEN-2: subscribe only, never create
  if seqCapable(conv.ref) and conv.checkpoint != null and conv.history.loaded:
       msg.resume_from = { stream_id: conv.checkpoint.stream_id, seq: conv.checkpoint.seq }
  conv.startRequest = msg
  conv.awaitingSessionStarted = true
  conv.preStart = []
  send(msg)
```

- **T-9.** `start` MUST be (re-)sent on every socket open and on every visibility/foreground resume while the socket is open (§3.5). Re-sending `start` on a subscribed socket is safe: subscription is a set (`api/pool.py:488-491`). Every such automatic `start` of an existing view carries `reattach: true` (OPEN-2), so it can never re-create a conversation that was closed.
- **T-10.** `resume_from` MUST only be sent when the conversation's entries were built in this process from the same stream (in-memory checkpoint). A client that rebuilt the conversation from REST (cold open, process death, page reload) MUST NOT send a persisted checkpoint (fixes W-7). Checkpoints MUST NOT be written to persistent storage per event (A-2.2/A-8.14). Android MAY persist `{localId, checkpoint}` **together with a full snapshot of the entries** on `onStop`; restoring both is equivalent to "in memory".
- **T-11.** For the orchestrator, `voice_start` replaces `start` while this client owns voice (§7.3). A plain `start` from the owner's own socket is still safe: the server answers it with `voice_initiator: true` and voice metadata only (no `voice_session_update`, no `voice_connection_info`). Android's conversation layer sends one on every foreground.
- **T-12.** Responses to `start`:
  - `status{connecting}` → `status = connecting`.
  - `session_started` → `connState = subscribed`; clear `connectionBanner`; adopt `localId` (ID-1); set `counters.contextWindow` from `context_window` (chat) or `model_info.model_info.context_window` (orchestrator), fallback 200 000; then §3.6.
  - `error{start_timeout|start_failed}` → `connState = failed`, `connectionBanner = {code, detail}`, offer Retry. No automatic retry loop. Every start error also ends the wait for `session_started` (SEQ-8).
  - `error{orchestrator_active}` → §6.12 (conflict). `error{orchestrator_stopping}` → retry `start` once after 1 s, then fail.
  - `error{not_started}` received at any time → re-send `start` once (with `reattach`).
  - `error{session_closed}` (answer to a `reattach` start: the conversation is not open) → OPEN-3: the view closes.

### 3.4 Reconnect and backoff

- **T-13.** Reconnect with exponential backoff: delay = min(15 s, 1 s × 2^attempt) ± 20 % jitter, unlimited attempts while the view is open and the app is visible. Reset `attempt` on a successful `session_started`.
- **T-14.** Web: no reconnect attempts while `document.hidden`. On becoming visible, reconnect immediately (02 §7.2). Android: no attempts while the app is in the background, except the orchestrator socket while this device owns voice or the wake-word service needs it. Android MUST also reconnect immediately when `ConnectivityManager` reports a network becoming available.
- **T-15.** Changing the server URL (Android) MUST tear down all sockets, reset per-server state (orchestrator `localId`, pool cache, conversations), and **connect to the new server** (A-8.3, A-dead-code `teardownForServerUrlChange`).
- **T-16.** Handshake timeout: an upgrade that is not open 10 s after the attempt starts MUST be abandoned and handled like an abnormal close (code 1006, `willReconnect` while wanted), so T-13 backoff continues. Added 2026-10-04 after the Android app hung forever on an attempt made while the Jetson backend restarted: the WebSocket clients have no read timeout (T-4 pings guard open sockets only) and nginx proxies the API sockets with 24 h timeouts.

### 3.5 Visibility and foreground

```
onVisible():                                    // web visibilitychange→visible; Android onStart/onResume
  for conv in openConversations:
     if socket(conv).isOpen: sendStart(conv)    // resync with reattach (OPEN-2): replay what we missed
     else: reconnectNow(conv)
  sessionDirectory.syncPool()                   // §3.7: OPEN-4 reconcile
  sessionDirectory.refreshList()
```

### 3.6 Seq checkpoint, replay and `replay_overflow` (Claude agent sessions)

```
onFrame(conv, f):
  if f.type in ("voice_audio_out"): audio.push(f); return          // L-3
  if conv.awaitingSessionStarted and f.type != "session_started" and f.type != "error":
       conv.preStart.push(f); return                               // SEQ-5 (errors bypass the hold)
  if f.type == "session_started": onSessionStarted(conv, f); return
  dispatch(conv, f)

dispatch(conv, f):
  if isNumber(f.seq) and isNonEmptyString(f.stream_id) and f.type != "session_stalled":
       cp = conv.checkpoint
       if cp != null and cp.stream_id == f.stream_id and f.seq <= cp.seq: return   // duplicate (SEQ-1)
       conv.checkpoint = { stream_id: f.stream_id, seq: f.seq }
  reduce(conv, f)                                                  // §4

onSessionStarted(conv, f):
  conv.awaitingSessionStarted = false
  applySessionStartedFields(conv, f)                               // T-12
  reduce(conv, f)                                                  // sets voiceActive when f.voice (§4.3)
  rf = conv.startRequest.resume_from
  rs = f.resume_state                                              // may be absent
  if f.replay_overflow:
       conv.checkpoint = rs ? { stream_id: rs.stream_id, seq: rs.next_seq - 1 } : null
       startCanonicalReload(conv)                                  // §5.6; preStart frames are kept
       conv.reloadBuffer.prepend(conv.preStart)                    // and applied after the reload
  else if rf != null and rs != null and rs.stream_id == rf.stream_id:
       for g in conv.preStart: if not hasSeq(g): dispatch(conv, g)  // seq-stamped ones WILL be replayed
       // replay frames follow on the socket and go through dispatch()
  else:
       for g in conv.preStart: dispatch(conv, g)
       if rs != null:
          cp = conv.checkpoint
          conv.checkpoint = (cp != null and cp.stream_id == rs.stream_id)
                            ? { stream_id: rs.stream_id, seq: max(cp.seq, rs.next_seq - 1) }
                            : { stream_id: rs.stream_id, seq: rs.next_seq - 1 }
  conv.preStart = []
  if not conv.inTurn and conv.status in (connecting, stopped, terminated): conv.status = idle
  conv.termination = null      // only if this start created/attached a live session
```

```
onStartError(conv, code, detail):                                  // SEQ-8; called by onError (§4.3)
  if code == "orchestrator_stopping" and not conv.stoppingRetried:
       conv.stoppingRetried = true; retry start after 1 s; return    // T-12; keep waiting
  conv.connState = failed
  conv.connectionBanner = { code, detail }                         // never an entry (I-15)
  if conv.awaitingSessionStarted:
       conv.awaitingSessionStarted = false
       held = conv.preStart; conv.preStart = []
       for g in held: dispatch(conv, g)                            // in arrival order, never dropped (L-2)
// conv.stoppingRetried is reset by onSessionStarted
```

- **SEQ-1.** Within one `stream_id`, a frame with `seq <= checkpoint.seq` MUST be dropped. Gaps are normal (G-18) and MUST NOT trigger anything.
- **SEQ-2.** `session_stalled` MUST be exempt from seq dedupe: it carries the previous event's seq (`manager/claude/session.py:585-591` + `api/pool.py:1257-1264`, G-18).
- **SEQ-3.** Unsequenced frames (`user_message`, `status`, `session_*`, `error`, `compact_complete` from `compact`, the receive-loop-exit `turn_complete`) are always applied.
- **SEQ-4.** A frame with a *different* `stream_id` than the checkpoint MUST be applied and replaces the checkpoint (the CLI subprocess reconnected).
- **SEQ-5.** Frames received after `start` was sent and before `session_started` MUST be held (`preStart`). If a replay was requested and granted, held seq-stamped frames MUST be discarded because the replay re-delivers them in order (`api/routes/chat.py:296-301`: the socket is subscribed before `session_started` is sent). *Rationale:* applying them first and the replay later would reorder blocks. **Exception:** an `error` frame received while `preStart` is active (a failed `start`) bypasses the hold and is applied immediately; otherwise a start failure would be held forever and never shown. A start error also ends the wait and releases the held frames (SEQ-8).
- **SEQ-6.** `replay_overflow: true` ⇒ the client MUST perform a canonical reload from REST (§5.6). If the conversation has no `sdkId`, it MUST first try `pool/live`; if there is still none, it sets `gapPossible = true` and keeps its entries.
- **SEQ-7.** Qwen/Gemini agent sessions and the orchestrator have no replay. After a reconnect during which such a conversation was `inTurn`, the client MUST set `gapPossible = true`. For Qwen/Gemini agent sessions it SHOULD then run a canonical reload at the next `endTurn`, or immediately if `pool/live` reports `idle`. For the orchestrator it MUST NOT reload automatically (§5.7, A-4.4.1): it only offers "Reload".
- **SEQ-8.** A start error (`start_timeout`, `start_failed`, `orchestrator_active`, or `orchestrator_stopping` after its one retry) ends the wait for `session_started`: the client MUST clear `awaitingSessionStarted` and dispatch the held `preStart` frames in arrival order (`onStartError`). They MUST NOT be dropped (L-2). Any other `error` received while waiting is applied at once (SEQ-5 exception) but does not end the wait. *Rationale:* frames can already be flowing to a socket whose `start` then fails: every orchestrator socket is a pool watcher from connect (`api/routes/orchestrator.py:127-128`), and a chat socket keeps an earlier subscription across a second `start` (G-6). The start error is the last answer to that `start` (`api/routes/chat.py:362-373`; `api/routes/orchestrator.py:463,637,718`), so nothing else would release them. Fixture `start_failed_releases_held_frames`.

### 3.7 Pool sync, watcher events, multi-client and focus

```
SessionDirectory:
  list: SessionInfo[]            // GET /api/sessions
  pool: PoolSession[]            // GET /api/sessions/pool/live

syncPool():                      // OPEN-4: every orchestrator socket open, every visible/foreground, after any mutation
  pool = GET /api/sessions/pool/live
  orchestratorRef = pool.find(is_orchestrator) ?? null           // null: the Archie view closes (OPEN-3)
  for view in openConversationViews: if no row has view.localId: closeView(view)   // OPEN-3
  openNow = pool                                                  // OPEN-1: the list IS the pool

onWatcherEvent(e):               // only arrives on the orchestrator WS, even unsubscribed (T-7)
  agent_session_opened{session_id, sdk_session_id, is_orchestrator}:
       if is_orchestrator: orchestratorRef = {session_id, sdk_session_id}
       else: markLive(...) ; maybe open a background view (FOCUS-1)
       refreshList() ; refreshVisualizations()
  agent_turn_started{session_id, ...} / agent_turn_finished{session_id, status, preview, ...}:
       device notifications only, at the channel level (TURN-1, TURN-2); no view changes
  agent_session_closed{session_id, is_orchestrator}:            // OPEN-3
       closeView(openViewByLocalId(session_id))                 // active or not; no close request
       if is_orchestrator: orchestratorRef = null ; voice teardown if any (§7)
       openNow.remove(session_id) ; refreshList()
  visualization_changed / memory_changed:   // same fan-out, handled at the channel level (§9.3)
       onContentFrame(e)
```

- **FOCUS-1.** No server-originated event (pool sync, `agent_session_opened`, `user_message`, a background turn, a voice transcript) may change which view is active or navigate the UI. Only a direct user action may change focus. Clients MAY open a **background** view for a session started elsewhere (web parity), shown with an unread/live badge, but it MUST NOT become active (fixes W-6.1 focus stealing, A-1.1 auto-navigation).
- **FOCUS-2.** `agent_session_closed` MUST check `is_orchestrator` before acting on a view (G-39).
- **OPEN-1. The server owns the open set.** A conversation (Archie or agent) is open exactly when `GET /api/sessions/pool/live` has its row. "Open now" on every device **is** that list — the same conversations on every device, each with its live status, whether or not this device has a view of it (the order may be the user's, e.g. dragged tabs; membership is the pool's). A client MUST NOT list, keep or re-create a conversation that is not in the pool. The pool survives backend restarts: the server persists it and restores it at startup, a restored conversation listed as `idle` until something uses it (`backend/api/open_sessions.py`). So there is no "backend restarted" case for clients to guess at (it replaced RT-4 and the 2026-10-10 server-id heuristic).
- **OPEN-2. Reattach, never re-create.** Every automatic `start` (socket open, reconnect, visible/foreground resync, `not_started` recovery; Android's voice-owner `voice_start` on reconnect) carries `reattach: true`. The server then subscribes the socket if the conversation is in its pool (spawning a restored one) and otherwise answers `error{session_closed}` without creating anything. Only a direct user action that **creates** or resumes a conversation sends `start` without `reattach`: a new conversation, opening one from History, fork / duplicate / continue / rewind, Save and Restart, the §6.11 take-over. (A voice start pressed on a view that is already subscribed may keep `reattach`: while the conversation is open the result is the same.) The server is the one place that decides, so a device that missed a close cannot revive the conversation, whatever its timing.
- **OPEN-3. Closed = gone, everywhere, at once.** When a conversation leaves the pool — `agent_session_closed{session_id, is_orchestrator}` (check the flag, FOCUS-2), `error{session_closed}`, or a `pool/live` read without its row (OPEN-4) — every client closes its view immediately, whether it is active or not, without a `close` request (the server already closed it). If it was active, focus moves to its neighbouring view, as after an explicit close. Orchestrator: `orchestratorRef = null`, voice ends (§7), the Archie slot shows the empty "New conversation" state. A client MAY show a one-line notice ("Closed on another device"; for `session_terminated{reason}` the reason). *Rationale:* until 2026-10-10 a view the user had looked at stayed as a "Stopped" tab (FOCUS-3, now removed), listed under "Open now" with no session behind it, and its next automatic `start` re-opened the session on the server for every device.
- **OPEN-4. Reconcile on every read.** Clients read `pool/live` on every orchestrator socket open and every visible/foreground (§3.5), and after each `session_started` of an agent view (ST-2, one read for both): views whose row is missing close (OPEN-3), the others take the row's status (ST-2), rows with no view here are listed (OPEN-1). Watcher events keep it current in between: `agent_session_opened` adds the row on every device, `agent_session_closed` removes it.
- **MC-1.** Mutations are not broadcast (G-27). After rename, delete, duplicate, rewind, fork, close and config writes, the acting client refreshes its own stores. Other clients see changes on their next `refreshList()` (visible, watcher event, or any `turn_complete`).
- **TURN-1.** Turn watcher events (added 2026-10-09 for device notifications). `pool.send()` (`backend/api/pool.py`), the path of every agent turn (chat tabs and the orchestrator's runner; never the orchestrator's own turns), tells every pool watcher:
  - `agent_turn_started{session_id, sdk_session_id, provider}` when a turn begins;
  - `agent_turn_finished{session_id, sdk_session_id, provider, title, status, preview, error}` when it ends. `session_id` is the agent's `localId`. `status` is `ok` | `error` | `interrupted`: a turn stopped through `pool.interrupt` (chat Stop, `cancel_turn`, a superseding prompt, the runner's cancel) is `interrupted` even though the SDK still sends a `TurnComplete`; an `is_error` `TurnComplete`, an exception, an abandoned turn after its retry and the runner's timeout are `error` (the timeout arrives as `interrupted` then `error`). `title` is the session's custom title or first prompt (null when unknown: use MC-2). `preview` is one line of the turn's final assistant text (markdown markers dropped, ≤ 200 chars); `error` the failure detail when `status == "error"`. An unknown `status` reads as `ok`.

  Each `send()` announces exactly one finish; an abandoned attempt that is retried announces a second `agent_turn_started` and only the final outcome (a cancellation or failure in the pause between the attempts is announced there: `interrupted` / `error`). Both emissions run as their own tasks, so a stalled watcher socket never delays a turn; a finish waits for its start, so watchers see them in order. Clients handle both frames at the **channel level** (like `orchestrator_switch`), attached or not; they never reach a conversation reducer and never change focus (FOCUS-1).
- **TURN-2.** Device notification (Settings → This device → Notifications → "Agent session finished", §8.2, off by default). On `agent_turn_finished` a client posts one system notification when the switch is on, the OS/browser allows it, `status != interrupted`, and the user is not looking at that session (app visible and focused, the workspace on top, that view active; on the web also in another page of the same browser, published through a localStorage "viewing" entry refreshed every 20 s and trusted for 45 s). Title: `title`, else the derived title, else "Agent session". Body: `preview` (or "Finished"); errors read "Failed: <error>". One notification per session (tag `archie-turn:<localId>` on web, `turn:<localId>` on Android): a newer one replaces it. A tap is a user action: it brings the app forward and focuses that session (its open view, else the live pool session, else `start{local_id, resume_sdk_id}`). Web: `registration.showNotification` through the main build's service worker (the only path on Android Chrome), else `new Notification`; no Notification API (iOS 12 Safari) or an http origin → the setting explains instead. Android: the "Agent sessions" channel (high importance: heads-up; the feature is opt-in); while the switch is on and an agent turn is in flight (tracked from these frames, reconciled with `pool/live` on every reconnect and every 3 min while busy, a finish newer than a pool read wins, 2 h age-out) the voice host's foreground service is held, special-use type only, its notification reading "Waiting for N agent sessions", so the socket survives the background (spec 14 §2.5). Both log one line per decision (`[notify] …` / logcat tag `ArchieNotify`).
- **MC-2.** Titles are derived, not stored on the view (02 §1.6 load-bearing): `title = list.find(s => s.session_id == sdkId)?.title ?? list.find(s => s.local_id == localId)?.title ?? placeholder`. The backend's `"(active session)"` (a live session with no history file yet) counts as no title: the placeholder shows (2026-10-04). Placeholders: Archie → "New conversation" (CR-9), agent → "New agent session" on both platforms. `refreshList()` runs after every `endTurn` of any view (debounced to 1 per 2 s).

---

## 4. The conversation reducer

### 4.1 Invariants

These invariants are normative. Each one is checkable on the reducer's output, so each SHOULD be a property test on both platforms (random interleavings of the fixture inputs), in addition to the fixtures.

| ID | Invariant |
|---|---|
| **I-1 Tail attachment** | Every assistant-content input (`text_*`, `thinking_*`, a new `tool_use`, `permission_request`, voice assistant transcript) mutates only the **last** entry, and only if it is an `AssistantEntry`. Otherwise a new `AssistantEntry` is appended first. Content never goes into an earlier entry. The only inputs allowed to touch an earlier entry are the **by-id** updates: `tool_result`, `tool_executing`, `tool_progress`, `permission_resolved`, a repeated `tool_use` with a known id, `endTurn`/`endVoice` finalisation, and the voice anchor of I-9. |
| **I-2 Boundaries** | Appending any non-assistant entry (user, voice transcript, inject, notice) ends the current run: the next assistant content creates a new run **after** it. A run is never reopened. No reducer path relies on `turn_complete` to end a run (G-4). |
| **I-3 Arrival order** | Within a run, blocks are in arrival order. Text before a tool call is above it; text after a tool result is below it. Blocks are never grouped by type. |
| **I-4 Delta extension** | A `*_delta` extends the last block of the last entry iff that block has the same type, the same scope and `streaming == true`. Otherwise it opens a new block. Any other block appended in between closes the open block. |
| **I-5 Complete replaces** | `*_complete(t)` replaces the open block's text with `t` and closes it; it never appends to the deltas. If the open block is the continuation of a block split by an interleaved entry, the already-shown prefix is removed (`continuationOf`). |
| **I-6 RESULT-DELIVERY** | A tool result is never lost and always shown. See §4.5 (R-1…R-9). |
| **I-7 No stuck running** | After `endTurn` no block with `scope == "turn"` is `streaming`, no such tool is `running`, and no permission is `pending`. After `endVoice` the same holds for `scope == "voice"`. After `session_stopped`/`session_terminated` it holds for both. |
| **I-8 Seq idempotence** | Applying a seq-stamped frame twice (same `stream_id`, same `seq`) is the same as applying it once (SEQ-1). `session_stalled` is exempt. |
| **I-9 Voice anchor** | A completed voice user transcript is inserted at the position recorded at the matching `speech_started` (if any) rather than at the end. This is the only positional insertion. It never splits a run. |
| **I-10 No empty runs** | The reducer never leaves an `AssistantEntry` with zero blocks. Inputs that do not add a block (results, resolutions, status) never create entries. |
| **I-11 No duplicate tool cards** | At most one `ToolBlock` exists per non-empty `tool_use_id`. At most one `PermissionBlock` exists per `request_id`. |
| **I-12 Queue** | A prompt that the server queues behind a running turn is shown in the queue tray, not in the timeline, and appears in the timeline exactly once, at the point where it is dispatched (G-5). |
| **I-13 Determinism** | Same initial state + same input sequence ⇒ same state, on every platform. |
| **I-14 Focus** | The reducer has no notion of focus. No input may change the active view (FOCUS-1). |
| **I-15 Transport errors are not content** | Socket errors, reconnect failures and start failures never create entries. |

### 4.2 Inputs

The reducer consumes these inputs, already deduplicated by the connection manager (§3.6):

| Input | Source |
|---|---|
| server frames | chat WS and orchestrator WS, exact payloads of chapter 01 §4.2, §5.3, §7.4 |
| `history_page{mode: "replace"|"prepend"|"reconcile", response}` | REST `GET /api/sessions/{sdkId}/messages` (§5) |
| `datachannel_event{event}` | OpenAI WebRTC data channel, **inbound** events, voice owner only (§7) |
| `local_*` actions | user actions (§6): `local_send`, `local_send_audio`, `local_inject`, `local_interrupt`, `local_compact`, `local_close`/`local_stop` (sets `expectStopAck`) |
| `voice_local_end` | the voice controller tore voice down locally (timeout, fatal error) |
| `clear_agent_approvals{localId}` | orchestrator view: the agent view `localId` saw its turn end (PM-5, §6.9) |

### 4.3 Reducer pseudo-code

The pseudo-code is complete: the fixtures' `expected` values are derived from it by hand. `conv` is implicit.

```ts
// ───────────────────────── helpers ─────────────────────────
function last(): Entry | null { return entries.length ? entries[entries.length - 1] : null }

function tail(): AssistantEntry {                              // I-1
  const e = last()
  if (e && e.kind == "assistant") return e
  if (!converting) finalizeOpenVoiceUser()                     // H-6
  const n = { kind: "assistant", blocks: [] }
  entries.push(n)
  return n
}

function openBlock(type, scope): Block | null {
  const e = last()
  if (!e || e.kind != "assistant" || e.blocks.length == 0) return null
  const b = e.blocks[e.blocks.length - 1]
  return (b.type == type && b.scope == scope && b.streaming) ? b : null
}

function closeOpenBlock() {
  const e = last()
  if (e && e.kind == "assistant" && e.blocks.length && e.blocks.at(-1).streaming) {
    e.blocks.at(-1).streaming = false
    e.blocks.at(-1).implicitlyClosed = true
  }
}

function pushBlock(b) {                                        // b.origin defaults to "live"
  closeOpenBlock()
  const t = tail()
  if (!converting && pendingSplit && !(pendingSplit.type == b.type && pendingSplit.scope == b.scope)) pendingSplit = null
  t.blocks.push(b)
}

function appendEntry(x) {                                      // every non-assistant entry
  if (!converting && x !== openVoiceUser) finalizeOpenVoiceUser()   // H-6: history conversion leaves
  const e = last()                                             // the live state alone
  if (!converting && e && e.kind == "assistant" && e.blocks.length && e.blocks.at(-1).streaming) {
    const b = e.blocks.at(-1)
    b.streaming = false
    pendingSplit = b                                           // a continuation may follow (I-5)
  }
  entries.push(x)
  if (!converting && x.kind == "user") promptSinceTurnEnd = true
}

function appendNoticeOnce(notice) {
  const e = last()
  if (e && e.kind == "notice" && e.notice == notice) return
  appendEntry({ kind: "notice", notice, text: "" })
}

function currentScope() {
  return (ref.kind == "orchestrator" && voiceActive && !inTurn) ? "voice" : "turn"
}

function clearStall() { stall = null }

function maybeBackgroundNotice() {                             // §4.4.3, orchestrator only
  if (ref.kind != "orchestrator" || !inTurn) return
  const prev = entries[entries.length - 1]                     // a visible prompt (echo from another device,
  if (!turnIsLocal && !turnHasContent && !(prev && prev.kind == "user"))   // inject, voice transcript) is not "background"
    appendEntry({ kind: "notice", notice: "background", text: "" })
  turnHasContent = true
}

function ensureAgentTurn() {                                   // content seen without a turn start
  if (ref.kind == "agent" && !inTurn) { inTurn = true; promptSinceTurnEnd = false }
}

function setAgentStatus(s) { if (ref.kind == "agent" && inTurn) status = s }

function finalizeOpenVoiceUser() {
  if (openVoiceUser) { openVoiceUser.streaming = false; openVoiceUser = null }
}

function normalizeOutput(x): string {
  if (typeof x == "string") return x
  if (x === null || x === undefined) return ""
  return JSON.stringify(x)                                     // object / array outputs (Gemini voice)
}

// ───────────────────────── streamed text / thinking ─────────────────────────
function onDelta(type, text, scope = "turn") {                 // type: "text" | "thinking"
  clearStall()
  if (scope == "turn") { ensureAgentTurn(); setAgentStatus(type == "text" ? "streaming" : "thinking") }
  const b = openBlock(type, scope)
  if (b) { b.text += text; return }
  if (scope == "turn") maybeBackgroundNotice()
  const cont = (pendingSplit && pendingSplit.type == type && pendingSplit.scope == scope) ? pendingSplit : null
  pendingSplit = null
  pushBlock({ type, text, streaming: true, scope, origin: "live", continuationOf: cont })
}

function onComplete(type, text, scope = "turn") {
  clearStall()
  if (scope == "turn") { ensureAgentTurn(); setAgentStatus(type == "text" ? "streaming" : "thinking") }
  const b = openBlock(type, scope)
  if (b) {
    if (isHistoryDuplicate(type, text, b)) { removeBlock(b); return }        // §5.4
    let full = text
    if (b.continuationOf && full.startsWith(b.continuationOf.text))
      full = full.slice(b.continuationOf.text.length)
    b.text = full; b.streaming = false
    return
  }
  if (isDuplicateComplete(type, text)) return                                 // §5.4
  let full = text
  if (pendingSplit && pendingSplit.type == type && pendingSplit.scope == scope) {
    if (full.startsWith(pendingSplit.text)) full = full.slice(pendingSplit.text.length)
    pendingSplit = null
    if (full == "") return
  }
  if (scope == "turn") maybeBackgroundNotice()
  pushBlock({ type, text: full, streaming: false, scope, origin: "live" })
}

function removeBlock(b) {                                      // only used by §5.4 dedupe
  const t = last()                                             // b is always in the tail run
  t.blocks.splice(t.blocks.indexOf(b), 1)
  if (t.blocks.length == 0) entries.pop()                      // I-10
}

// ───────────────────────── tools ─────────────────────────
function onToolUse(id, name, input) {
  clearStall()
  const scope = currentScope()
  if (scope == "turn") { ensureAgentTurn(); setAgentStatus("tool_use") }
  if (id && tools.has(id)) {                                   // I-11: replay / history overlap
    const tb = tools.get(id)
    if (!tb.tool_name) tb.tool_name = name
    if (input && Object.keys(input).length) tb.tool_input = input
    return
  }
  if (scope == "turn") maybeBackgroundNotice()
  const tb = { type: "tool", tool_use_id: id ?? "", tool_name: name ?? "", tool_input: input ?? {},
               status: "running", output: null, scope, origin: "live" }
  pushBlock(tb)
  if (id) {
    tools.set(id, tb)
    if (orphanResults.has(id)) { applyResult(tb, orphanResults.get(id)); orphanResults.delete(id) }  // R-2
  }
}

function onToolResult(id, output, isError, origin = "live") {
  clearStall()
  deliverResult(id ?? "", { output: normalizeOutput(output), is_error: isError === true, origin })
}

function deliverResult(id, r) {                                // R-1 … R-4
  if (id) {
    const tb = tools.get(id)
    if (tb) { applyResult(tb, r); return }
    const prev = orphanResults.get(id)
    if (!prev || r.output != "" || prev.output == "") orphanResults.set(id, r)
    return
  }
  if (r.origin == "live") {
    const running = allToolBlocks().filter(b => b.status == "running")
    if (running.length == 1) { applyResult(running[0], { ...r, inferred: true }); return }
  }
  unattributed.push(r)
}

function applyResult(tb, r) {                                  // R-5
  const authoritative = r.origin == "reconcile"
  if (!authoritative && (tb.status == "done" || tb.status == "error")
      && tb.output && r.output == "") return                   // never replace output with nothing
  if (authoritative && (tb.status == "done" || tb.status == "error")
      && tb.output && !tb.inferred) return                     // live non-empty result already there
  tb.output = r.output
  tb.status = r.is_error ? "error" : "done"
  tb.inferred = r.inferred === true
  tb.executing = false
}

function onToolExecuting(id) { const tb = tools.get(id); if (tb && tb.status == "running") tb.executing = true }
function onToolProgress(id, elapsed, message) {
  const tb = tools.get(id); if (tb && tb.status == "running") tb.progress = { elapsed_seconds: elapsed, message }
}

// ───────────────────────── permissions ─────────────────────────
function onPermissionRequest(rid, name, input) {
  clearStall()
  if (perms.has(rid)) return                                   // I-11
  ensureAgentTurn()
  const pb = { type: "permission", request_id: rid, tool_name: name, tool_input: input ?? {},
               state: "pending", responder: null, message: null, scope: "turn", origin: "live" }
  pushBlock(pb)
  perms.set(rid, pb)
}

function onPermissionResolved(rid, decision, responder, message) {
  clearStall()
  const pb = perms.get(rid)
  if (!pb) return                                              // we never saw the request: nothing to show
  pb.state = decision == "allow" ? "allowed" : "denied"
  pb.responder = responder ?? null
  pb.message = message ?? null                                 // NO entry is appended (fixes W-5)
}

// ───────────────────────── turns ─────────────────────────
function beginTurn() {
  inTurn = true; promptSinceTurnEnd = false; turnHasContent = false; pendingSplit = null
  status = (ref.kind == "agent") ? "processing" : "streaming"
}

function endTurn(reason) {                                     // I-7, scope "turn"
  for (const b of allBlocks()) {
    if (b.scope != "turn") continue
    if (b.streaming) b.streaming = false
    if (b.type == "tool" && b.status == "running") b.status = "no_result"
    if (b.type == "permission" && b.state == "pending") {
      b.state = "denied"; b.responder = "system"; b.message = "stream ended"   // mirrors the backend drain (G-17)
    }
  }
  pendingSplit = null; stall = null
  inTurn = false; turnDepth = 0; turnHasContent = false; turnIsLocal = false
  if (status != "stopped" && status != "terminated") status = compactPending ? "compacting" : "idle"
}

function endVoice() {                                          // I-7, scope "voice"
  finalizeOpenVoiceUser(); speechAnchor = null
  for (const b of allBlocks()) {
    if (b.scope != "voice") continue
    if (b.streaming) b.streaming = false
    if (b.type == "tool" && b.status == "running") b.status = "no_result"
  }
  voiceActive = false
}

// ───────────────────────── frame dispatch ─────────────────────────
function reduce(f) {
  switch (f.type) {
  case "text_delta":        return onDelta("text", f.text)
  case "text_complete":     return onComplete("text", f.text)
  case "thinking_delta":    return onDelta("thinking", f.text)
  case "thinking_complete": return onComplete("thinking", f.text)
  case "tool_use":          return onToolUse(f.tool_use_id, f.tool_name, f.tool_input)
  case "tool_result":       return onToolResult(f.tool_use_id, f.output, f.is_error)
  case "tool_executing":    return onToolExecuting(f.tool_use_id)
  case "tool_progress":     return onToolProgress(f.tool_use_id, f.elapsed_seconds, f.message)
  case "permission_request":  return onPermissionRequest(f.request_id, f.tool_name, f.tool_input)
  case "permission_resolved": return onPermissionResolved(f.request_id, f.decision, f.responder, f.message)

  case "user_message": {
    if (f.queued) { queue.push({ text: f.text, owner: "remote" }); return }      // I-12
    if (f.source == "shared_inject") {
      const i = pendingInjects.indexOf(f.text)
      if (i >= 0) { pendingInjects.splice(i, 1); markInjectSent(f.text); return }
      appendEntry({ kind: "user", text: f.text, origin: "inject", state: "sent" }); return
    }
    if (f.source == "voice_message") {                          // another device's voice message (VM-1);
      appendEntry({ kind: "user", text: f.text, origin: "audio", state: "sent" }); return  // never sent to its sender
    }
    const d = dispatchedFromTray.indexOf(f.text)               // pre-O-6 backends still echo a dispatched
    if (d >= 0) { dispatchedFromTray.splice(d, 1); return }     // queued prompt once more: already shown
                                                               // (checked BEFORE endTurn, which clears dispatchedFromTray)
    if (ref.kind == "agent" && inTurn) endTurn("superseded")                    // observer of an interrupted turn
    const i = queue.findIndex(q => q.owner == "remote" && q.text == f.text)
    if (i >= 0) queue = queue.filter((q, k) => !(q.owner == "remote" && k <= i))
    appendEntry({ kind: "user", text: f.text, origin: "echo", state: "sent" })
    return
  }

  case "status": return onStatus(f.status)

  case "turn_complete":
    if (ref.kind == "agent") {
      counters.cost += f.cost ?? 0
      counters.turns += f.num_turns ?? 1
      const it = f.input_tokens ?? f.usage?.input_tokens
      if (typeof it == "number" && it > 0) counters.contextTokens = it
      if (f.session_id && !ref.sdkId) ref.sdkId = f.session_id
      if (f.is_error && typeof f.result == "string" && f.result != "")
        appendEntry({ kind: "notice", notice: "error", text: f.result, data: { code: "turn_error" } })
      endTurn("complete")
    } else {
      if (typeof f.input_tokens == "number" && f.input_tokens > 0) counters.contextTokens = f.input_tokens
      // the orchestrator turn ends at status:idle
    }
    return

  case "compact_complete":
    if (ref.kind == "agent")
      appendEntry({ kind: "notice", notice: "compaction", text: f.summary ?? "", data: { trigger: f.trigger } })
    else {
      appendEntry({ kind: "notice", notice: "compaction", text: "",
                    data: { trigger: f.trigger, tokens_before: f.tokens_before, tokens_after: f.tokens_after } })
      if (typeof f.tokens_after == "number") counters.contextTokens = f.tokens_after
    }
    compactPending = false
    if (!inTurn && status == "compacting") status = "idle"
    return

  case "session_stalled":
    if (inTurn || busy(status))
      stall = { elapsed_seconds: f.elapsed_seconds, last_tool_name: f.last_tool_name ?? null,
                last_tool_use_id: f.last_tool_use_id ?? null }
    return

  case "error": return onError(f.error, f.detail)

  case "session_terminated":
    termination = { reason: f.reason, detail: f.detail ?? null, sdk_session_id: f.sdk_session_id ?? null }
    queue = []
    status = "terminated"
    endTurn("terminated"); endVoice()
    checkpoint = null
    return

  case "session_stopped":
    if (expectStopAck) { expectStopAck = false; return }         // reply to our own stop / close
    if (status != "terminated") status = "stopped"
    endTurn("stopped"); endVoice()
    effects.push({type: "closed", reason: termination})        // OPEN-3: the session directory closes the view

  case "voice_event":       return onVoiceEvent(f.event)
  case "voice_owner_active": if (f.active) voiceActive = true; else endVoice(); return
  case "voice_ended":
  case "voice_stopped":     return endVoice()
  case "session_started":   if (f.voice === true) voiceActive = true; return   // fields handled in §3.6
  case "nested_session_event": {                               // orchestrator WS: route, do not render here
    routeToAgentView(f.session_id, f.event_data)
    if (ref.kind != "orchestrator" || !f.event_data?.request_id) return   // PM-5
    const rid = f.event_data.request_id
    const has = agentApprovals.some(a => a.localId == f.session_id && a.request_id == rid)
    if (f.event_type == "permission_request" && !has)
      agentApprovals.push({ localId: f.session_id, request_id: rid,
                            tool_name: f.event_data.tool_name ?? "", tool_input: f.event_data.tool_input ?? {} })
    else if (f.event_type == "permission_resolved" && has)
      agentApprovals = agentApprovals.filter(a => !(a.localId == f.session_id && a.request_id == rid))
    return
  }
  case "agent_session_closed":                                 // OPEN-3: this conversation itself left the pool (the view closes)
    if (f.session_id == ref.localId && (f.is_orchestrator === true) == (ref.kind == "orchestrator")) {
      if (status != "terminated") status = "stopped"
      endTurn("stopped"); endVoice()
    }
    return
  case "model_changed": case "model_info":
    counters.contextWindow = f.model_info?.model_info?.context_window ?? counters.contextWindow; return
  default: return                                              // voice_ending, voice_command, voice_audio_out,
                                                               // voice_connection_error, agent_session_opened,
                                                               // models_list, audio_upload, ping, unknown:
                                                               // handled outside the reducer or ignored
  }
}

function onStatus(s) {
  if (ref.kind == "agent") {
    switch (s) {
    case "connecting": status = "connecting"; return
    case "processing":
      if (inTurn) endTurn("superseded")
      if (!promptSinceTurnEnd && queue.length > 0) {            // a queued prompt was dispatched (server is FIFO)
        const q = queue.shift()                                 // local OR remote: since backend O-6 the observer
        dispatchedFromTray.push(q.text)                         // gets no second user_message for it
        appendEntry({ kind: "user", text: q.text, origin: q.owner == "local" ? "local" : "echo", state: "sent" })
      }
      beginTurn(); return
    case "retrying": if (inTurn) status = "retrying"; return
    case "interrupted":
      queue = []                                               // the server dropped every queued prompt
      if (inTurn || busy(status)) appendNoticeOnce("interrupted")
      endTurn("interrupted"); return
    default: return                                            // ST-1
    }
  } else {
    switch (s) {
    case "connecting": status = "connecting"; return
    case "streaming": {
      const local = localTurnsPending > 0
      if (local) localTurnsPending -= 1
      turnDepth += 1
      if (turnDepth == 1) { beginTurn(); turnIsLocal = local } else if (local) turnIsLocal = true
      return
    }
    case "idle":
      turnDepth = Math.max(0, turnDepth - 1)
      if (turnDepth == 0) endTurn("complete")
      return
    case "interrupted": if (inTurn) appendNoticeOnce("interrupted"); return
    default: return
    }
  }
}

const TURN_FAILURE_AGENT = ["send_failed", "upstream_wedged", "turn_timeout", "command_failed", "compact_failed"]
const TURN_FAILURE_ORCH  = ["api_error", "provider_error", "send_failed", "send_audio_failed",
                            "invalid_audio", "inject_text_failed", "compact_failed"]
const ORCH_NO_IDLE_AFTER = ["send_failed", "send_audio_failed", "compact_failed"]  // exception paths

function onError(code, detail) {
  if (code == "interrupted") { if (inTurn) appendNoticeOnce("interrupted"); return }   // orchestrator agent loop
  const isTurnFailure = (ref.kind == "agent" ? TURN_FAILURE_AGENT : TURN_FAILURE_ORCH).includes(code)
  if (!isTurnFailure) {                                        // §4.4.4: never an entry
    if (["start_timeout", "start_failed", "orchestrator_active", "orchestrator_stopping"].includes(code))
      return onStartError(code, detail)                        // SEQ-8 (§3.6)
    connectionOrVoiceBanner(code, detail); return
  }
  appendEntry({ kind: "notice", notice: "error", text: detail || code, data: { code } })
  compactPending = false
  if (ref.kind == "agent") { endTurn("error"); return }
  if (code == "invalid_audio" && localTurnsPending > 0) localTurnsPending -= 1   // no streaming will follow
  if (ORCH_NO_IDLE_AFTER.includes(code)) {
    turnDepth = Math.max(0, turnDepth - 1)
    if (turnDepth == 0) endTurn("error")
  }                                                            // api_error/provider_error: status:idle follows
}

// ───────────────────────── local actions ─────────────────────────
function local_send(text) {
  if (ref.kind == "agent") {
    if (busy(status) || inTurn) { queue.push({ text, owner: "local" }); return }   // I-12
    appendEntry({ kind: "user", text, origin: "local", state: "sent" })
    status = "processing"                                      // optimistic; status:processing follows
  } else {
    appendEntry({ kind: "user", text, origin: "local", state: "sent" })
    localTurnsPending += 1
  }
}
function local_send_audio(text = "") { appendEntry({ kind: "user", text, origin: "audio", state: "sent" }); localTurnsPending += 1 }
                               // text: the prompt sent with the clip (web composer note); Android sends none
function local_inject(text) {                                  // orchestrator only (§6.15)
  if (voiceActive) { appendEntry({ kind: "user", text, origin: "inject", state: "pending" }); pendingInjects.push(text) }
  else { appendEntry({ kind: "user", text, origin: "inject", state: "sent" }); localTurnsPending += 1 }
}
function local_interrupt()   { if (ref.kind == "agent") queue = queue.filter(q => q.owner != "local") }
function local_compact()     { compactPending = true
                               if (ref.kind == "agent") status = "compacting"; else localTurnsPending += 1 }
function local_stop()        { expectStopAck = true }          // before sending stop or POST close
function voice_local_end()   { endVoice() }
function clear_agent_approvals(localId) { agentApprovals = agentApprovals.filter(a => a.localId != localId) }  // PM-5
```

`allBlocks()` iterates the blocks of every `AssistantEntry` in order; `allToolBlocks()` filters tool blocks. `markInjectSent(text)` sets `state = "sent"` on the oldest pending inject entry with that text. `routeToAgentView` feeds `event_data` into the open agent view with that `localId` (as if it came on its chat WS; I-11 makes the double delivery harmless) and into the orchestrator-level approvals list (§6.9).

### 4.4 Turn lifecycle per kind

#### 4.4.1 Agent sessions (chat WS)

```
observer:  user_message{text} → status{processing} → content… → turn_complete
sender:    (local_send appended the entry) → status{processing} → content… → turn_complete
queued:    sender: local_send while busy ⇒ tray; … turn_complete → status{processing} ⇒ pop tray into timeline
           observer: user_message{text,queued:true} ⇒ tray; … turn_complete → status{processing} ⇒ pop tray head into timeline
           (backend O-6 no longer re-echoes it; a pre-O-6 re-echo is swallowed via dispatchedFromTray). `dispatchedFromTray` is cleared on turn end.
interrupt: sender: status{interrupted} (direct) ⇒ notice "interrupted", endTurn
           observer: nothing until the next user_message/status{processing} ⇒ endTurn("superseded")
```

- **TL-1.** `turn_complete` MUST end the turn (`endTurn`) but MUST NOT close the run (I-2). The next run starts only after a non-assistant entry.
- **TL-2.** A turn is also ended by: `status{interrupted}`, a turn-failure `error`, `session_stopped`, `session_terminated`, and a new `user_message`/`status{processing}` while `inTurn` (the observer of an interrupted turn, G-4).
- **TL-3.** Content arriving while `!inTurn` (observer attached mid-turn, replayed tail, slash-command output) implicitly starts a turn (`ensureAgentTurn`) without a prompt.

#### 4.4.2 Orchestrator text turns

```
status{streaming} → ( text_delta* text_complete | tool_use | tool_executing | tool_progress | tool_result )*
                  → turn_complete{input_tokens,output_tokens} → status{idle}
error path:       status{streaming} → … → error{interrupted|api_error|provider_error} → status{idle}
exception path:   status{streaming} → … → error{send_failed|send_audio_failed|compact_failed}   (no idle)
```

- **TL-4.** The orchestrator turn ends at `status{idle}`, or at an exception-path `error`. `status{streaming}` frames can **nest** (two devices sending, or a wake turn racing a typed send: `api/routes/orchestrator.py:1018-1031` runs per socket, the wake task runs concurrently, `:722-740`). The client counts depth (`turnDepth`).
- **TL-5.** A wake turn whose notification was already drained produces `status{streaming}` + `status{idle}` with no content. It MUST leave no trace in the timeline.

#### 4.4.3 Background and unprompted turns (orchestrator)

Before backend O-3 the orchestrator did not echo typed prompts to other devices (G-22); it now echoes typed prompts and voice messages (VM-1). Background-agent wake turns stream a reply with no prompt at all (01 §5.1). `background_notification` lines exist only in the JSONL and are never sent live (`orchestrator/session.py:1654-1670`).

- **BG-1.** When the first assistant content of an orchestrator text turn arrives, the turn was not started by this client (`turnIsLocal == false`), **and the entry immediately before the new run is not a user entry** (since backend O-3, prompts typed on other devices arrive as `user_message` echoes and voice messages as `user_message{source:"voice_message"}` echoes (VM-1); both are visible prompts, not background), the reducer appends `notice{background}` before the run (`maybeBackgroundNotice`). UI copy: "Background update" with the explanation "The orchestrator replied to a background task or to another device." This keeps the reply in its own run and visibly unprompted.
- **BG-2.** History lines `<task-notification>…` (Claude Code background tasks) become `notice{background, text: <line>}` (§5.1).

#### 4.4.4 Errors

| Class | Codes | Effect |
|---|---|---|
| Turn failure | agent: `send_failed`, `upstream_wedged`, `turn_timeout`, `command_failed`, `compact_failed`; orchestrator: `api_error`, `provider_error`, `send_failed`, `send_audio_failed`, `invalid_audio`, `inject_text_failed`, `compact_failed` | `notice{error}` entry + turn end |
| Interrupt | orchestrator `interrupted` | `notice{interrupted}` (deduped with `status{interrupted}`) |
| Start / connection | `start_timeout`, `start_failed`, `orchestrator_active`, `orchestrator_stopping`, client-side socket errors | `connectionBanner` (§3.3); never an entry |
| Voice | `voice_event_failed`, `voice_audio_failed`, `voice_restart_failed`, `voice_config_busy`, `not_voice_session`, `cannot_switch_voice` | voice controller banner (§7) |
| Protocol | `invalid_json`, `unknown_type`, `not_started`, `invalid_permission_response`, `unknown_model` | logged, transient toast; `not_started` re-sends `start` |

The `connectionBanner` MUST show human copy, never raw codes (W-6.2), MUST be dismissible, and clears on the next `session_started`.

### 4.5 Tool calls: RESULT-DELIVERY (I-6)

**Every path that emits tool output in the backend** (verified 2026-10-03):

| Path | Frame(s) | `tool_use_id` | `output` shape | Code |
|---|---|---|---|---|
| Claude agent, result in assistant content | `tool_result` | real id | `str`, list → `json.dumps` | `manager/claude/session.py:1086-1107` |
| Claude agent, `UserMessage.tool_use_result` dict | `tool_result` | **`result["tool_use_id"]` — absent in real CLI output** → `parent_tool_use_id` → usually **`""`** | `str(result["content"])` — absent for most tools → **`""`** | `manager/claude/session.py:1116-1127` |
| Claude agent, `tool_use_result` string | `tool_result` | `parent_tool_use_id` → usually **`""`** | the string (often `"Error: …"`), `is_error: false` | `:1128-1133` |
| Claude agent, `tool_use_result` list (MCP tools) | **none** (logged as unsupported) | — | — | `:1134-1138` |
| Qwen agent | `tool_result` | real id | list of text parts joined with `\n` | `manager/qwen/session.py:600-624` |
| Gemini agent | `tool_result` | `tool_id` (may be `""`) | `output` or `error.message` | `manager/gemini/session.py:556-569` |
| Orchestrator text turn | `tool_use` → `tool_executing` → `tool_progress`* → `tool_result` | provider call id | string | `api/serializers.py`, `orchestrator/agent.py` |
| Orchestrator voice (OpenAI, Qwen) | `tool_use` (only if `call_id` **and** `name` are known) then `tool_result` | `call_id` | string ≤ 8000 chars; `is_error` = JSON has `"error"` | `api/routes/orchestrator.py:1511-1586` |
| Orchestrator voice (Gemini) | `tool_use` per call, then `tool_result` per `functionResponses[]` | Gemini call id | `response.output` — **may be a non-string** | `api/routes/orchestrator.py:1589-1637` |
| History | `tool_result` block in a user (wrapper) message | real id | string | `manager/protocol.py:166-210` |

> **Verified defect (Appendix A, BF-1).** The real CLI puts *tool-specific metadata* in `tool_use_result` (Bash: `{stdout, stderr, interrupted, isImage, noOutputExpected}`; Edit: `{filePath, oldString, …}`), not `{tool_use_id, content}`. A stream-json probe on 2026-10-03 (`claude -p … --output-format stream-json`) shows the real id and output only in `message.content[0]` (`{"type":"tool_result","tool_use_id":"toolu_…","content":"probe123"}`). So today **almost every live Claude `tool_result` arrives with `tool_use_id: ""` and an empty or wrong `output`**, and MCP results arrive not at all. This is the main cause of "tool cards never show output live". The unit test `tests/test_session.py:581-612` mocks a shape the CLI never sends. The client rules below make the UI correct even before BF-1 lands, by inference plus REST reconciliation.

Rules:

- **R-1 By id, anywhere.** A result with a non-empty `tool_use_id` attaches to the `ToolBlock` with that id wherever it is in `entries`: regardless of which run is the tail, of user/voice/inject/notice entries appended in between, and of whether the turn already ended (`no_result` → `done/error`).
- **R-2 Orphans are held, never dropped.** If no block with that id exists yet (result before its `tool_use`: replay overlap, reconnect, the history/live boundary, a history page boundary, a voice call whose name was unknown so no `tool_use` was broadcast), the result goes into `orphanResults[id]`. When a `tool_use` with that id is created (live or from a history page), the orphan attaches immediately and leaves the map.
- **R-3 One card, any shape.** Results from every row of the table above reach the same card: `output` is normalised to a string (`normalizeOutput`: strings unchanged, `null` → `""`, objects/arrays → `JSON.stringify`); `is_error` is `true` only if it is the boolean `true`; `tool_executing` and `tool_progress` update the card by id and never create one.
- **R-4 Empty id.** A live result with `tool_use_id == ""` attaches to the single `running` tool block of the conversation if there is exactly one (`inferred: true`). Otherwise it is appended to `unattributed`. It is never guessed among several candidates.
- **R-5 Never downgrade.** A non-empty output is never replaced by an empty one. A card never returns to `running`. A `reconcile` result (REST) overrides an inferred or empty live result, but not a non-empty, id-matched live one.
- **R-6 No stuck "running".** At `endTurn` every `running` card of scope `turn` becomes `no_result`; at `endVoice` every `running` card of scope `voice` does; at `session_stopped`/`session_terminated` all of them do. `no_result` is not final: a later result upgrades it.
- **R-7 Reconcile after the turn.** For agent sessions with an `sdkId`, after `endTurn` the client MUST run `reconcile` (§5.5) if any card of that turn ended `no_result` or `inferred`, or `unattributed` is non-empty, or `orphanResults` has live entries. It SHOULD debounce (500 ms after `turn_complete`) and run at most one reconcile at a time.
- **R-8 Results never create entries or blocks.** Orphans and unattributed results are side collections, not timeline content.
- **R-9 Unmatched results are reachable.** The UI MUST make live orphans and all unattributed results reachable (for example a footer chip "2 tool results could not be matched" that opens a list). History orphans stay hidden while `history.hasMore` (their `tool_use` is on a page not loaded yet) and become reachable when `hasMore == false`.

**UI rules for tool cards** (both platforms):

- **TC-1.** The card's status icon and output region are bound to the `ToolBlock` by identity and update reactively when the result arrives, including when the card is already expanded (W-6.2 "expanded Bash shows nothing", A-4.3 (c)).
- **TC-2.** Status display: `running` → spinner; `done` → done; `error` → error styling with the output; `no_result` → neutral "No output received" (live) or "No output recorded" (history). A history tool without a result MUST NOT show "done" (W-17, A-4.2).
- **TC-3.** `inferred` results show a subtle "matched by position" hint. `executing`/`progress` show "running · 12 s".
- **TC-4.** Orchestrator tool names MUST be resolved before the Qwen snake_case mapping when the conversation is an orchestrator (W-15): the orchestrator's `read_file` keeps its own renderer.

### 4.6 Permissions

- **PM-1.** `permission_request` adds a `PermissionBlock` to the current run. For `ExitPlanMode`, the UI renders `tool_input.plan` as markdown **inside** the permission block; the reducer MUST NOT add a separate text block for the plan (no duplicate of the plan the model may also stream).
- **PM-2.** `permission_resolved` updates the block in place by `request_id` and MUST NOT append a timeline entry (W-5's common trigger). A resolve for an unknown `request_id` is ignored.
- **PM-3.** The approval bar shows the newest `pending` permission of the view. It closes when that block leaves `pending` (resolve by anyone, `endTurn`, stop, terminate), never on a different `request_id`.
- **PM-4.** Pending permissions are expired as `denied / system / "stream ended"` at `endTurn` (the backend auto-denies but does not broadcast it, G-17).
- **PM-5.** On an orchestrator conversation, `nested_session_event{event_type:"permission_request"}` MUST add `{localId: session_id, request_id, tool_name, tool_input}` to `agentApprovals` once per `(localId, request_id)`. `nested_session_event{event_type:"permission_resolved"}` MUST remove it, and so does `clear_agent_approvals{localId}` (the runtime sends it when that agent's view saw its turn end, §6.9). Neither adds a timeline entry. The `event_data` is also routed to the open agent view (`routeToAgentView`). *Rationale:* the pool mirrors exactly these two event types, keyed by the agent's `local_id`, to orchestrator subscribers (`api/pool.py:942-952`). It is the orchestrator device's only source for the "Agent approvals" list (§6.9). Fixture `orchestrator_agent_approvals_and_jsonl_id`.

### 4.7 Voice transcripts in the timeline

Mapping from provider events to reducer actions. The same mapping applies to `voice_event{event}` frames (received by every subscribed device except the OpenAI owner) and `datachannel_event{event}` inputs (OpenAI owner only, inbound events only).

| Provider event | Action |
|---|---|
| `input_audio_buffer.speech_started` (provider or backend-synthesised) | `speechAnchor = entries.length` if `openVoiceUser == null` |
| `conversation.item.input_audio_transcription.completed{transcript}` | `voiceUserFinal(transcript)` |
| `response.output_audio_transcript.delta`, `response.audio_transcript.delta`, `response.output_text.delta`, `response.text.delta` `{delta}` | `onDelta("text", delta, "voice")` after `finalizeOpenVoiceUser()` |
| `response.output_audio_transcript.done`, `response.audio_transcript.done` `{transcript}`; `response.output_text.done`, `response.text.done` `{text}` | `voiceAssistantDone(text)` |
| `response.done` | `voiceTurnEnd()` |
| Gemini (no `type`): `serverContent.inputTranscription.text` | `voiceUserFragment(text)` |
| Gemini: `serverContent.outputTranscription.text` | `finalizeOpenVoiceUser()`; if `text` is non-empty, `onDelta("text", text, "voice")` |
| Gemini: `serverContent.interrupted` | `voiceTurnEnd()` |
| Gemini: `serverContent.turnComplete` | `voiceTurnEnd()` |
| Gemini `serverContent.modelTurn.parts[].text` | ignored (the persister ignores it too; using both duplicates text) |
| `response.function_call_arguments.done`, `response.output_item.added`, Gemini `toolCall` | ignored by the reducer: tool cards come **only** from top-level `tool_use`/`tool_result` frames (fixes W-3) |
| `conversation.item.created` | ignored (shared injects arrive as `user_message{source:"shared_inject"}`) |
| `voice_status`, `voice_vad_state`, `voice_error`, `error`, `goAway`, `setupComplete`, `session.*` | voice controller only (§7) |

Gemini events are processed in this order within one event: `inputTranscription`, `outputTranscription`, `interrupted`, `turnComplete` (same as `orchestrator/voice_persister.py:146-195`).

```ts
function voiceUserFinal(text) {
  if (!text || !text.trim()) return
  insertUserAtAnchor({ kind: "user", text, origin: "voice", state: "sent" })
}
function voiceUserFragment(text) {                             // Gemini coalescing (02 §7.11)
  if (!text) return
  if (openVoiceUser) { openVoiceUser.text += text; return }    // raw concatenation, as the persister does
  const e = { kind: "user", text, origin: "voice", state: "sent", streaming: true }
  insertUserAtAnchor(e)
  openVoiceUser = e
}
function insertUserAtAnchor(e) {                               // I-9
  let pos = speechAnchor ?? entries.length
  speechAnchor = null
  if (pos >= entries.length) { appendEntry(e); return }
  if (e !== openVoiceUser) finalizeOpenVoiceUser()
  entries.splice(pos, 0, e)                                    // the tail run stays last and stays open
}
function voiceAssistantDone(text) {
  finalizeOpenVoiceUser()
  const b = openBlock("text", "voice")
  if (b) { if (text) b.text = text; b.streaming = false; return }   // empty text keeps content (Gemini)
  if (text) pushBlock({ type: "text", text, streaming: false, scope: "voice", origin: "live" })
}
function voiceTurnEnd() {
  finalizeOpenVoiceUser()
  const b = openBlock("text", "voice"); if (b) b.streaming = false
}
```

- **VT-1.** Voice assistant text streams into the current run as a `TextBlock{scope:"voice"}`; a voice user transcript is a user entry and therefore ends the run (I-2), unless it is placed by the anchor (I-9). This is the fix for the Android ordering bug (A-4.3): later tool calls go into the new tail run, never into an older message.
- **VT-2.** Passive viewers render transcripts exactly like the owner, for every provider. WS providers: the backend relay broadcasts every provider event. OpenAI (WebRTC): the owner mirrors its data-channel events as `voice_event`, and the backend re-broadcasts the transcript ones (the event types of the table above) to every other subscriber, never back to the owner, which renders from its data channel (`api/routes/orchestrator.py` `_mirror_to_passive_viewers`). Before 2026-10-10 the backend did not, and passive viewers saw only tool cards (G-32).
- **VT-3.** `voice_ended`/`voice_stopped`, `voice_owner_active{active:false}` and `voice_local_end` run `endVoice()` on **every** device, owner or passive, so voice tool cards always finish (A-4.3 compounding paths). This is conversation state; it is separate from the passive device's own voice-button state (§7.5).

---

## 5. History (REST) and merging with live events

### 5.1 REST page → reducer actions

History pages (`GET /api/sessions/{sdkId}/messages`) go through the **same** helpers as live events, so history and live produce the same structures (02 §7.5).

```ts
function applyHistoryPage(mode, resp) {          // mode: "replace" | "prepend" | "reconcile"
  if (mode == "reconcile") return reconcile(resp) // §5.5
  if (mode == "replace") resetContent()           // entries, tools, perms, orphanResults, unattributed,
                                                  // pendingSplit, openVoiceUser, speechAnchor
  const kept = entries
  entries = []                                    // convert into a scratch list; maps stay shared
  converting = true                               // H-6: no live-state side effects while converting
  for (const p of resp.messages) convertPreview(p)
  converting = false
  const page = entries
  entries = kept
  const lastOfPage = page.at(-1)
  for (const tb of toolBlocksOf(page)) {
    if (tb.status != "running") continue
    const mayStillRun = mode == "replace" && inTurn && lastOfPage?.kind == "assistant"
                        && lastOfPage.blocks.includes(tb)
    if (!mayStillRun) tb.status = "no_result"     // TC-2: never "done" without a result
  }
  if (mode == "replace") { entries = page; promptSinceTurnEnd = false }
  else {                                          // prepend; merge a run split by the page boundary
    if (page.length && entries.length && page.at(-1).kind == "assistant" && entries[0].kind == "assistant") {
      entries[0].blocks = page.at(-1).blocks.concat(entries[0].blocks)
      page.pop()
    }
    entries = page.concat(entries)
    if (speechAnchor != null) speechAnchor += page.length   // H-6: the anchor keeps pointing at the same place
  }
  history = { loaded: true, startIndex: resp.start_index, totalCount: resp.total_count, hasMore: resp.has_more }
}

function convertPreview(p) {
  const blocks = p.blocks ?? []
  if (p.role == "user") {
    for (const b of blocks) if (b.type == "tool_result")
      deliverResult(b.tool_use_id ?? "", { output: normalizeOutput(b.output), is_error: b.is_error === true, origin: "history" })
    if (blocks.length && blocks.every(b => b.type == "tool_result")) return     // protocol wrapper, not a turn
    const text = p.text ?? ""
    if (text == "") return                                                       // e.g. image-only line
    appendEntry(classifyUserLine(text))
    return
  }
  if (blocks.length == 0) { if (p.text) pushHistory("text", p.text); return }   // empty = Claude thinking placeholder
  for (const b of blocks) {
    if (b.type == "text" && b.text)          pushHistory("text", b.text)
    else if (b.type == "thinking" && b.text) pushHistory("thinking", b.text)    // only after backend fix (App. A)
    else if (b.type == "tool_use")           historyToolUse(b.tool_use_id, b.tool_name, b.tool_input)
    else if (b.type == "tool_result")
      deliverResult(b.tool_use_id ?? "", { output: normalizeOutput(b.output), is_error: b.is_error === true, origin: "history" })
  }
}

function pushHistory(type, text) { pushBlock({ type, text, streaming: false, scope: "turn", origin: "history" }) }

function historyToolUse(id, name, input) {
  if (id && tools.has(id)) return                                                // I-11
  const tb = { type: "tool", tool_use_id: id ?? "", tool_name: name ?? "", tool_input: input ?? {},
               status: "running", output: null, scope: "turn", origin: "history" }
  pushBlock(tb)
  if (id) { tools.set(id, tb)
            if (orphanResults.has(id)) { applyResult(tb, orphanResults.get(id)); orphanResults.delete(id) } }
}

function classifyUserLine(text): Entry {
  let m
  if (text.startsWith("[voice] "))                       return user(text.slice(8), "voice")
  if (m = /^\[voice, recording: [^\]]*\] ?/.exec(text))  return user(text.slice(m[0].length), "voice")
  if (m = /^\[audio:[A-Za-z0-9]+\] ?/.exec(text))        return user(audioPrompt(text.slice(m[0].length)), "audio")
  if (text.startsWith("[shared file] ") || text.startsWith("[shared text]")) return user(text, "inject")
  if (text.startsWith("[Request interrupted by user"))   return notice("interrupted", "")
  if (text.startsWith("This session is being continued from a previous conversation")) return notice("compaction", text)
  if (text.startsWith("<task-notification>"))           return notice("background", text)
  if (/^<(command-name|command-message|command-args|local-command-stdout|local-command-stderr|local-command-caveat)>/.test(text))
                                                         return notice("command", text)
  return user(text, "history")
}
// user(t, o) = { kind: "user", text: t, origin: o, state: "sent" };  notice(n, t) = { kind: "notice", notice: n, text: t }
// audioPrompt(t) = t == "(audio message)" ? "" : t   — the backend's placeholder for a voice message
//                  without a prompt, so a reloaded voice message matches the live one (VM-1)
```

The prefixes are the backend's own (`orchestrator/voice_persister.py` writes `[voice] `; `orchestrator/session.py` writes `[audio:<fmt>] `; the Android share flow writes `[shared file] ` / `[shared text]`, §6.15) or the Claude CLI's (surveyed in `context/*.jsonl` on 2026-10-03). The regexes avoid lookbehind and named groups (Safari 12, 02 §5.4).

### 5.2 Cold open (no in-memory state)

```
openConversation(ref):
  conv = new Conversation(ref)
  if ref.kind == "agent" and ref.live and ref.liveStatus in (streaming, tool_use, thinking):
       conv.inTurn = true; conv.status = ref.liveStatus                 // ST-2
  conv.reloading = true                       // onFrame() appends raw frames to conv.reloadBuffer
  connect(conv)                               // start{local_id, resume_sdk_id}; NO resume_from (T-10)
  if ref.sdkId:
       resp = GET /api/sessions/{sdkId}/messages?limit=50
       (404 → empty history: a new session whose JSONL does not exist yet)
       reduce(history_page{replace, resp})
  conv.history.loaded = true
  conv.reloading = false
  for f in conv.reloadBuffer: onFrame(conv, f)                          // §3.6 rules apply now
```

- **H-1.** Subscribe first, then fetch, then apply the frames that arrived meanwhile (01 §6.5.1). Frames are held raw and re-enter `onFrame` so `session_started`, `preStart` and seq dedupe behave exactly as in §3.6.
- **H-2.** The history-init MUST NOT re-run when `sdkId` becomes known after the first turn (02 §7.4).
- **H-3.** A view for a session that is not live and that the user only wants to read (for example a past orchestrator conversation while another orchestrator is active) opens **read-only**: REST only, no WebSocket, with a "Resume" action (fixes W-11).

### 5.3 Pagination

```
loadOlder(conv):
  if not conv.history.hasMore or conv.loadingOlder: return
  resp = GET /api/sessions/{sdkId}/messages?limit=50&before={conv.history.startIndex}
  if resp.start_index + resp.messages.length != conv.history.startIndex:   // page does not abut
       return canonicalReload(conv)                                       // file was truncated (A-4.4.6)
  reduce(history_page{prepend, resp})
```

- **H-4.** Trigger: web, `scrollTop <= 80 px`; Android, first visible item index ≤ 1 after the initial scroll, with the load-more guard (03 §8, `5c029d6`). Scroll restoration rules are in the UI specs (02 §7.7 freeze buffer and prepend anchoring remain load-bearing).
- **H-5.** Prepending merges a run that the page boundary split (§5.1). Tool results from newer pages wait in `orphanResults` and attach when the older page brings their `tool_use` (fixes W-17, A-4.4.5).
- **H-6.** Converting a history page MUST NOT change live-turn state (`converting` in §4.3/§5.1). On a `prepend`, `promptSinceTurnEnd`, `pendingSplit` and `openVoiceUser` keep their values, so an open Gemini transcript stays open and keeps coalescing. A pending `speechAnchor` MUST be shifted by the number of entries prepended (counted after the boundary merge). A `replace` resets these fields anyway (`resetContent`). *Rationale:* an older page (`GET …/messages?before=`, `manager/store.py:286-296`) is history, not a new prompt. Without this rule, scrolling up between turns blocks the dispatch of a queued prompt (I-12), and scrolling up while the user speaks misplaces their transcript (I-9) or splits it in two. Fixtures `history_prepend_queued_prompt_dispatch`, `history_prepend_voice_anchor_shift`.

### 5.4 History/live overlap dedupe

There is no join key between REST and live events (G-10). The client prevents duplicates with three idempotence rules, all inside the reducer:

1. `tool_use` with a known id updates the existing card (I-11).
2. Results are idempotent by id (R-1, R-5).
3. Text and thinking blocks:

```ts
function isDuplicateComplete(type, text) {            // a *_complete with no open block
  const e = last()
  if (!e || e.kind != "assistant") return false
  return e.blocks.some(b => b.type == type && b.text == text && (b.origin == "history" || b.implicitlyClosed))
}
function isHistoryDuplicate(type, text, open) {       // the open block's final text already came from REST
  const e = last()
  return e.blocks.some(b => b !== open && b.type == type && b.origin == "history" && b.text == text)
}
```

The dedupe looks only at the **tail run** (the in-flight turn); identical text in older runs is never touched. Together with T-10 (no `resume_from` after a rebuild), this fixes the duplicate-on-reload bug (W-7).

### 5.5 Reconciliation (tool results)

```ts
function reconcile(resp) {                            // history_page{reconcile}: GET …/messages?limit=50
  for (const p of resp.messages) for (const b of p.blocks ?? []) {
    if (b.type != "tool_result" || !b.tool_use_id) continue
    const tb = tools.get(b.tool_use_id)
    if (tb) applyResult(tb, { output: normalizeOutput(b.output), is_error: b.is_error === true, origin: "reconcile" })
  }
  unattributed = []                                   // superseded by the authoritative REST results
}
```

Reconcile never adds, removes or reorders entries. It only fills tool results. It runs per R-7, and after a Qwen/Gemini reconnect.

### 5.6 Canonical reload

`canonicalReload(conv)` = set `reloading = true`, `GET …/messages?limit=50`, `reduce(history_page{replace})`, then flush `reloadBuffer` through `onFrame`. It is used for: `replay_overflow` (SEQ-6), the user's Reload action, a non-abutting page (§5.3), Qwen/Gemini agent gaps (SEQ-7), and every newly opened view (rewind, fork, termination recovery). It discards local-only entries (notices, pending injects); this is acceptable because every case above is either user-initiated or follows a gap.

### 5.7 Known history lossiness and how to present it

| Loss | Cause | Client behaviour |
|---|---|---|
| Orchestrator tool calls, results and background notifications are missing; a text-mode turn's text is one joined message | G-8, `manager/claude/adapter.py:81`, `orchestrator/session.py:1884-1893` | Load REST only on cold open or explicit Reload. Never replace a live orchestrator view with REST automatically (A-4.4.1). A history-loaded orchestrator conversation shows a one-line footnote at the top of the loaded range: "Background updates from agents aren’t kept in history." (2026-10-04: backend O-1 now keeps tool calls and results; background notifications are still not in REST history; the UI never says "orchestrator", CR-9.) |
| Claude thinking blocks are absent (empty assistant lines are dropped) | G-9 | Show nothing; never fabricate. After the optional backend fix they arrive as `thinking` blocks (§5.1 handles it). |
| Qwen/Gemini thinking appears as ordinary text | G-9 | Accepted until the backend fix. |
| Voice and audio turns carry text prefixes | G-11 | `classifyUserLine` strips them and sets `origin`. |
| CLI meta lines (`<command-name>`, compaction summary, "[Request interrupted…]", `<task-notification>`) appear as user lines | 01 §6.2 | Rendered as notices, not user bubbles. |
| Live and history structure of a wake turn differ (live: separate run after `notice{background}`; history: merged into the previous run) | G-22 | Accepted. |
| Subagent (Task) events may appear flat in the live stream but not in history | 01 §4.4 AMBIGUOUS | Tolerate ids that never appear in history; R-2/R-9 keep their results reachable. |
| A `tool_use` whose result was never written (session died mid-tool) | — | `no_result` "No output recorded". |

---

## 6. User actions as protocol sequences

Notation: `WS→` client frame, `→REST` request, `⇐` expected server frame(s). Every action that needs `sdkId` follows ID-3 when it is missing.

### 6.1 Send (agent)

```
local_send(text)                                       // reducer: timeline entry, or tray if busy
WS→ {type:"send", text}
⇐ status{processing} (sender gets no user_message echo) … turn_complete
```
Sending while busy is allowed and queues server-side (web parity; Android must allow it, 03 §5). The tray shows queued prompts with a "queued" chip; they move into the timeline when dispatched (I-12). If a permission is pending, the same send denies it with the text as feedback (§6.9).

### 6.2 Send (orchestrator)

```
local_send(text); WS→ {type:"send", text}
⇐ status{streaming} … turn_complete … status{idle}
```
The server serialises turns; there is no tray for the orchestrator. Other devices receive `user_message{text}` (O-3) before the turn, so the reply is not a background run (BG-1).

### 6.3 Interrupt / Stop

```
agent:        local_interrupt(); WS→ {type:"interrupt"}  ⇐ status{interrupted} (direct)
orchestrator: WS→ {type:"interrupt"}                    ⇐ status{interrupted} (broadcast) … error{interrupted} … status{idle}
```
Interrupt drops every server-queued prompt; the sender's tray is cleared (its texts MAY be restored into the input). `interrupt` does not stop a realtime voice response (G-34): in voice mode the Stop control is `voice_stop` (§7).

### 6.4 Compact

```
local_compact()
agent:        WS→ {type:"compact"}   ⇐ (turn events) compact_complete{trigger,summary} and turn_complete, in either order
orchestrator: WS→ {type:"compact"}   ⇐ status{streaming} compact_complete{trigger,tokens_before,tokens_after} status{idle}
```
The compact button shows `round(contextTokens / contextWindow × 100)` %, `?` when unknown; caution ≥ 50 %, warning ≥ 80 % (02 F-10). Disabled while busy. Android MUST expose it (03 §5).

### 6.5 Rewind and fork

The backend counts `drop_last_n` in *visible JSONL lines* (G-26), which a rendered timeline cannot count reliably (W-6, A-4.4.3). The client therefore anchors the cut on **user prompts**, counted from the end, and resolves it against a fresh REST listing:

```ts
function computeDropLastN(conv, target): number | ABORT {
  // 1. prompts after the target in the view
  const k = entriesAfter(target).filter(e => e.kind == "user" && e.state == "sent").length
  // 2. REST listing from the tail: pages of limit=200 (before=…) until ≥ k+1 prompt lines or has_more=false
  const lines = fetchTailLines(conv.ref.sdkId, k + 1)          // [{index, preview}] in REST order
  const isPrompt  = l => l.preview.role == "user" && isVisible(l.preview) && l.preview.text
                         && classifyUserLine(l.preview.text).kind == "user"
  const isVisible = p => p.role == "assistant" || !((p.blocks ?? []).length && p.blocks.every(b => b.type == "tool_result"))
  const prompts = lines.filter(isPrompt)
  let cutFrom                                                  // REST index of the first dropped line
  if (target.kind == "user") {                                 // keep the prompt, drop its reply and everything after
    const j = prompts.length - 1 - k
    if (j < 0 || !matches(prompts[j], target)) return ABORT
    cutFrom = prompts[j].index + 1
  } else {                                                     // run or notice: keep it, cut at the next prompt
    if (k == 0) return 0
    const next = prompts[prompts.length - k]
    if (!next || !matches(next, firstUserEntryAfter(target))) return ABORT
    cutFrom = next.index
  }
  return lines.filter(l => l.index >= cutFrom && isVisible(l.preview)).length
}
// matches(line, entry): the classified origin class agrees; for typed prompts (local/echo/history/inject)
// the normalised texts (trim, collapse whitespace) must also be equal.
```

```
Rewind (target):
  require sdkId, not busy (else: "Stop the current reply first")
  n = computeDropLastN(target); ABORT → canonicalReload + toast "The conversation changed. Try again."
  n == 0 → action disabled for the last entry
  confirm (copy: "Messages after this one will be removed. This cannot be undone.")
  local_stop(); →REST POST /api/sessions/{localId}/close          (204; errors ignored)
  →REST POST /api/sessions/{sdkId}/truncate {drop_last_n: n}     (409 → retry after 500 ms, up to 3 times)
  replace the view IN PLACE with a new view: new localId, same sdkId, canonical cold open (§5.2)
Fork (target):
  n = computeDropLastN(target)  (n == 0 is allowed: a plain copy)
  →REST POST /api/sessions/{sdkId}/fork {drop_last_n: n}  ⇒ 201 {session_id: newSdk}
  open a new view (focused: user-initiated) with new localId, sdkId = newSdk, kind preserved, cold open
```
A busy overlay covers rewind, fork and duplicate (02 F-12). Failures show the backend `detail` (W-6.2).

### 6.6 Rename

`→REST PATCH /api/sessions/{sdkId}/rename {title}` ⇒ 204 (404 tolerated). Optimistically update `SessionDirectory.list`, then `refreshList()`. Allowed from the view header/tab and from the session list, for agent and orchestrator sessions (Appendix B Q4).

### 6.7 Close a view

```
agent view:   if busy → confirm "Closing stops the current reply."
              local_stop(); →REST POST /api/sessions/{localId}/close  (204) ; close the socket ; remove the view
orchestrator: see Appendix B Q1. Default (parity): confirm "Stop the orchestrator on all devices?"
              → same POST close; voice ends everywhere.
read-only view: just remove it.
```
`stop` on the chat WS only detaches (it does not stop the agent) and is used only for T-5 socket reuse.

### 6.8 Delete

```
confirm ("<title> will be moved to trash; recoverable from context/trash/")
for each open view v with v.sdkId == sdkId or v.localId == poolRow(sdkId)?.local_id:
     local_stop(v); →REST POST /api/sessions/{v.localId}/close ; remove v      // fixes W-8
if the session is live but not open here: →REST POST /api/sessions/{pool.local_id}/close
→REST DELETE /api/sessions/{sdkId}   (204; 404 tolerated)
remove from SessionDirectory.list; refreshList(); syncPool()
```
The pool close MUST come before `DELETE` so the CLI does not keep writing a file that moved to trash. Duplicate: `POST …/duplicate` ⇒ `{session_id}`, refresh, do not open (web parity).

### 6.9 Permissions: approve, deny, deny with feedback

```
approve: WS→ {type:"permission_response", request_id, decision:"allow"}
deny:    WS→ {type:"permission_response", request_id, decision:"deny"}            (optional message)
deny with feedback: just send the text: WS→ {type:"send", text}
         ⇒ server resolves every pending permission as deny with message=text (api/routes/chat.py:164-174),
           then queues the text behind the still-running turn (tray, I-12)
⇐ permission_resolved{request_id, decision, responder, message}   (first answer wins; user or orchestrator)
```
- The bar disables its buttons after one click (local UI flag) and closes only on PM-3.
- From the orchestrator view, `nested_session_event{session_id, event_type:"permission_request", event_data}` adds the request to an "Agent approvals" list in the orchestrator view (no timeline entry). Answering sends `permission_response{session_id:<agent localId>, request_id, decision}` on that agent's chat WS if a view is open, else uses the REST endpoint below (both platforms). `nested_session_event{event_type:"permission_resolved"}` removes it. The list also clears entries whose agent view saw the turn end. This is new on both platforms (02 F-25; 03 §5). The list is conversation state (`agentApprovals`, PM-5).

**Answering without a socket** (OI-2):

```
POST /api/sessions/{localId}/permission   {request_id, decision: "allow"|"deny", message?}
  200 {ok: true}   resolved by this call (responder "user"); permission_resolved is broadcast as for the WS frame
  404              the session is not in the pool
  404 "Not Found"  (FastAPI's route-missing detail) a server without this route → web: transient WS (T-8); Android: "Open that session to answer"
  409              no such pending request (already answered by another device or the orchestrator, expired at turn end, unknown)
  400              invalid body
```
- A 409 is not an error for the UI: someone answered first, and the `permission_resolved` already on its way removes the card. Any other failure re-enables the card's buttons and shows the reason (snackbar).
- Notification actions (AN-2) always use REST, never a socket (it may be half-dead in background).

**Attention notifications (Android, OI-6):**

- **AN-1.** A pending agent permission (from `agentApprovals`, PM-5, or a pending `PermissionBlock` of an open agent view) posts a notification unless the user is looking at that agent's view: the app is in the foreground (process started, screen on), the workspace is the top screen (no settings/history in front) and that agent is the selected view. Once notified, or seen in its view, the same `(localId, request_id)` is not posted again.
- **AN-2.** Channel "Approvals" (`approvals`, importance high → heads-up), `CATEGORY_REMINDER`, `VISIBILITY_PUBLIC`. Title = the agent session's title; text = "Plan ready for approval" + the start of the plan for `ExitPlanMode`, else "Wants to use <tool>" + its main input. Actions **Deny** and **Approve** (Approve requires an unlocked device) answer over REST from a broadcast receiver; a failure updates the notification with the error and never crashes. Tap opens that agent session (from the live pool if it has no view here).
- **AN-3.** The notification is removed when the request leaves `pending` (resolved by anyone, expired at turn end), when answered from the notification (200 or 409), or when the user opens that agent's view.
- **AN-4.** The device only learns about requests over a live socket: backgrounded with the orchestrator socket closed (T-14: no voice owner, wake word or "Stay connected"), or with no subscribed Archie conversation and no open agent view, nothing arrives and nothing is posted. No full-screen intent (heads-up only).

### 6.10 New agent session

```
localId = uuid(); view = open (focused), kind agent, sdkId null
WS→ start{local_id}   ⇐ status{connecting} session_started{session_id, context_window, resume_state}
first turn_complete.session_id ⇒ sdkId (ID-2); refreshList()
```
Android MUST offer this (03 §5: missing today). Provider, working directory and MCPs come from the global config.

### 6.11 New / attach orchestrator

```
syncPool()
if an orchestrator row exists:
     attach: start{local_id: row.local_id, resume_sdk_id: row.sdk_session_id}       (G-15)
else new: localId = uuid(); start{local_id}            (jsonl id == localId for a new orchestrator, G-14)
⇐ status{connecting}? session_started{session_id, voice, model_info, …}
```
`error{orchestrator_active}` → the conflict dialog with three actions: Open the running one (attach) / Stop it and start new (`POST close` on its `localId`, then `start` with a new id) / Cancel (03 §1.7). Resume a past orchestrator conversation: the same dialog when one is active; otherwise `start{local_id: uuid(), resume_sdk_id: <jsonl id>}`. Cold open loads REST history (lossy, §5.7).

### 6.11a Switch to a past orchestrator conversation (agent-initiated)

The orchestrator's `switch_conversation` tool (`backend/orchestrator/tools/agent_sessions.py`) moves the user from the live orchestrator conversation into a past one. The server does the stopping. The client only resumes.

```
server: [voice] end_voice("switch") ⇒ voice_ending/voice_ended{reason:"switch"} to all subscribers
        pool.stop_orchestrator()     ⇒ agent_session_closed{session_id: old localId, is_orchestrator:true} to all watchers (OPEN-3)
        ⇒ orchestrator_switch{sdk_session_id, title, voice, from_session_id} to ONE socket only:
           the voice owner (voice:true), else the socket that sent the latest send/send_audio/inject_text
acting client: drop the old (stopped) Archie view locally (no REST close needed, it is already gone);
        open Archie resuming sdk_session_id: start{local_id: uuid(), resume_sdk_id: sdk_session_id}   (§6.11, no conflict dialog)
        voice:true ⇒ start voice on it with the device's current voice settings:
                     voice_start{local_id: <the new id>, resume_sdk_id: sdk_session_id, …}   (§7.3)
other clients: nothing new; they follow the watcher frames as for any replace from another device.
```
- **SW-1.** `orchestrator_switch` is handled at the **socket/channel level**, not by the Archie view's reducer: it arrives after OPEN-3 already closed that view. Each client MUST act on it at most once (dedupe on `sdk_session_id` + `from_session_id`).
- **SW-2.** The switch is the user's request made by voice or text, so it counts as user intent: focus the resumed view (main apps) and auto-start voice without a gesture when `voice` is true. A client that cannot start voice without a gesture (web autoplay rules) opens the view and shows its normal voice button.
- **SW-3.** `voice_ended{reason:"switch"}` is a quiet end: no closing cue or "call ended" notice, because the call continues in the resumed conversation.
- **SW-4.** Clients without conversation history views (app-lite) apply the same sequence through their voice host: resume `sdk_session_id`, start voice when `voice` is true.

### 6.12 Stall banner

Shown while `stall != null` and the view is busy: "<tool> has been running for <t> with no response." or "No response from the agent for <t>." (provider-neutral copy, W-6.2). `t` = `Ns` under 90 s, else `XmYs`. Its Interrupt button = §6.3.

### 6.13 Errors, termination and recovery

- **Turn errors** are `notice{error}` entries (§4.4.4). No action needed.
- **Connection banner**: Retry = reconnect now (resets backoff). Start failures show the backend `detail`.
- **Retry after a failed start** (`start_failed` / SEQ-8): the user's Retry closes and reopens the socket (a fresh `socket_open` → `start`), never re-sends `start` on the failed socket; the reducer stays in `failed` until the new `session_started`. Both clients implement it this way.
- **Termination** (`session_terminated` then `session_stopped`): the pool closed the session, so its view closes on every device like any other close (OPEN-3, 2026-10-10; it used to stay with a "Continue in a new view" banner, W-9). If it was the active view, a one-line notice names the session and how it ended — `<title> crashed` / `ended unexpectedly` / `can't reach its host` / `was replaced` / `was closed` (by `reason`: `subprocess_crashed`, `subprocess_lost`, `unreachable`, `replaced`, `closed_by_user`; else `ended`), then `: <detail>` when there is one. Recovery is reopening it from History (a user `start` with its `resume_sdk_id`). `session_terminated` is only sent on the chat WS; an orchestrator that died shows as `agent_session_closed{is_orchestrator:true}` or a socket close.

### 6.14 Session config: Save and Restart

```
→REST GET /api/sessions/{sdkId}/config, GET /api/config, GET /api/mcp/servers, GET /api/config/harnesses
      (older servers: GET /api/config/providers + GET /api/config/harness/qwen/models)
→REST PUT /api/sessions/{sdkId}/config {only changed keys; null = inherit global}
      harness_options is replaced as a whole map: {key: value | null} — absent key = inherit the
      global harness_options[provider][key], null = CLI default; null / {} = inherit every key.
      Changing provider resets harness_model and harness_options to null (they are per harness).
      400 + detail on an unknown key or a bad value (validated against that provider's catalog).
Save and Restart (allowed when not busy; the backend applies config only on a new session):
  local_stop(); →REST POST /api/sessions/{localId}/close
  WS→ start{local_id: <same localId>, resume_sdk_id: sdkId}      (new pool entry with the new config)
  ⇐ status{connecting} session_started (new stream_id; the checkpoint is replaced per §3.6)
```
The entries stay as they are; nothing is lost because the JSONL is resumed. This fixes "idle treated as stopped" (02 F-32, W-6.2): restart always closes first.

**Harness model + options in the UI.** Both clients render the model picker and the options of
the effective harness from its catalog (§8.1), with the same rules: hide an option whose
`models` excludes the effective model, and a choice whose `models` excludes it; narrow the
`effort` choices to the model row's `efforts` (`[]` hides effort); hide `thinking` and
`thinking_*` when the model row has `supports_thinking: false`. The effective model is the
session's `harness_model`, else the global `harness_model[provider]`; when it is "" (CLI default)
or not in `models` (custom id), show everything. Every control offers "Default (<global value or
CLI default>)" = inherit, "CLI default" (+ the option's `default`, or the model's
`default_effort` for effort, when known) and the values; the model adds catalog rows and, when
`allow_custom_model`, a free id. Catalog `warnings` are shown as a notice.

### 6.15 Upload and share (orchestrator)

```
→REST POST /api/uploads  multipart field "file"   (stream from disk; never read the whole file into memory, A-3.4)
  413 with a non-JSON body = nginx 1 MiB limit (G-1): "File too large for the server (limit 1 MB)" until BF-3
  ⇒ {filename, path, url, size, content_type}
text = "[shared file] <filename> (<size: B | %.1f KB | %.1f MB>, <content_type>) — <url>"
       + ("\nNote: " + subject if any) + "\nLocal path: " + path
shared text: "[shared text]" + (" " + subject if any) + "\n" + text.trim()
local_inject(text); WS→ {type:"inject_text", text}
  text mode ⇒ a normal turn (no echo); voice mode ⇒ user_message{text, source:"shared_inject"} to all (matches the pending entry)
```
The formats are the Android ones (`AssistantViewModel.kt:262-332`); the web gains this action. If the orchestrator socket is not subscribed, the client keeps **one** pending inject and sends it after `session_started` (03 §3.3).

### 6.16 Voice message recording (talk mode)

```
record (max 60 s; mic with echoCancellation + noiseSuppression) → base64 + format ("webm"|"ogg"|"mp4"|"wav")
local_send_audio(text ?? ""); WS→ {type:"send_audio", audio, format, text?}   ON THE ORCHESTRATOR WS ONLY (A-8.6)
⇐ status{streaming} … turn_complete … status{idle}      (or error{invalid_audio|send_audio_failed})
other devices: user_message{text: text ?? "", source:"voice_message"} ⇒ audio entry, then the same turn
```
Shown only on orchestrator views and only when the selected orchestrator model or any audio-capable model exists (`GET /api/orchestrator/models.audio_capable_models`, Appendix B Q5). The server may switch the model to `gpt-audio` permanently without `model_changed` (01 §7.8); the client SHOULD re-send `get_model` after the turn. Not available on compat (no `MediaRecorder` on Safari 12). Android's wake-word talk capture uses the same frame.

- **VM-1.** A voice message is a visible prompt on every subscriber, exactly once. The sender shows its own bubble at `local_send_audio` (with the accompanying prompt, if any). After decoding the clip, the server broadcasts `user_message{text, source:"voice_message"}` (`text` = the accompanying prompt or `""`) to every **other** orchestrator subscriber (`api/routes/orchestrator.py` `_handle_send_audio`, `exclude` = the sending socket), before `status{streaming}`. The receivers append `UserEntry{origin:"audio"}`, so the reply follows a user entry and BG-1 adds no notice. No echo reaches the sending socket, so the sender needs no dedupe; a client MUST therefore send `send_audio` on the same socket that feeds its Archie view. An undecodable clip (`error{invalid_audio}`) is not echoed. The UI shows an audio entry as a "Voice message" bubble, with the prompt text when it is not empty. History stores the same turn as `[audio:<fmt>] <prompt>` (`[audio:<fmt>] (audio message)` without one), which §5.1 classifies to the same entry. Fixtures `orchestrator_voice_message_echo_no_background_notice`, `orchestrator_voice_message_sender_and_history`.
- `POST /api/orchestrator/audio` starts no turn (it only broadcasts an `audio_upload` frame that nothing consumes, G-36), so it has no echo. Clients MUST NOT use it. If it ever starts turns, it MUST echo like VM-1 and carry a client-generated id in the echo so the uploading device can recognise its own echo (a REST caller has no socket to exclude).

---

## 7. Voice signaling

This section covers the client side of voice **signaling** on the orchestrator WS. The audio engines (WebRTC media, PCM capture/playback, wake word) are specified elsewhere; their tuned constants are owned by chapter 04 and the parity suite (D1).

### 7.1 Roles

| Role | How a device knows | May do |
|---|---|---|
| **Owner** | it sent `voice_start` and received `session_started{voice:true, voice_initiator:true}` | open the transport, play audio, send `voice_audio_in`/`voice_event`/`voice_recording_*`, execute `voice_command` |
| **Passive viewer** | `session_started{voice:true, voice_initiator:false}` or `voice_owner_active{active:true}` while it is not the owner | render the conversation; show "Voice active on another device" |
| **None** | no voice live | start voice |

`owner_local_id` is the shared orchestrator id, the same on every device (G-29): it MUST NOT be used to decide ownership.

### 7.2 Provider selection

- **V-1.** For a new voice session the client SHOULD omit the `voice_*` fields so the server applies `assistant_config.json` `default_voice_*` (01 §7.1). When the user picks explicitly (an in-call picker or a per-device override), it sends all five fields `voice_provider`, `voice_model`, `voice_name`, `voice_transcription_language`, `voice_endpoint`.
- **V-2.** On re-arm (`voice_start` on a live orchestrator), a missing field means "keep the previous value". A changed config while a turn runs returns `error{voice_config_busy}`; otherwise the server rebuilds the orchestrator and drops every subscriber silently (G-30). Clients MUST therefore re-run `syncPool()` + attach after any `voice_start` that changes the config.
- **V-3.** Pickers use `GET /api/orchestrator/voice/models` plus `GET /api/config/voice/google/models?endpoint=…` (the live path omits `google`, G-33). The ephemeral token MUST come from `session_started.voice_connection_info`; `POST /api/orchestrator/voice/session` is only a fallback when it is absent, and then all five query params MUST be passed (its defaults differ, G-33).

### 7.3 Owner start sequence

```
startVoice():                                    // allowed from off | error
  voice.state = connecting; voice.pendingStart = true; voice.queue = []
  WS→ {type:"voice_start", local_id, resume_sdk_id?, voice_*?}          // on the single orchestrator WS (T-6)
  ⇐ voice_event{event:{type:"voice_status", status:"summarizing"}}?     → voice.state = summarizing
  ⇐ session_started{voice:true, voice_initiator:true, voice_provider, voice_model, voice_name,
                    voice_transcription_language, voice_recording_enabled,
                    voice_session_update?, voice_connection_info?, voice_connection_error?}
       wait at most 30 s (the Jetson summariser takes 10–20 s, 02 F-26) → else error "Voice did not start"
       voice_connection_error → error state showing it
       if voice_session_update: voice.queue.unshift(voice_session_update)     // must be the FIRST provider frame
  ⇐ voice_owner_active{active:true}                                     → voice.pendingStart = false

  connection_type == "webrtc" (OpenAI):
     RTCPeerConnection + mic track + data channel "oai-events"
     POST connection_info.endpoint, Authorization: Bearer <ephemeral_token>, Content-Type: application/sdp → answer
     dc.onopen: send every queued frame in order (session.update first), voice.state = active
     every data-channel event, inbound AND outbound → WS→ {type:"voice_event", event}
     every INBOUND event → reducer input datachannel_event{event}  (§4.7)
     ⇐ voice_command{command} → if dc open: dc.send(JSON) else voice.queue.push(command)
  connection_type == "websocket" (Qwen, Google; audio_relay "backend"):
     ⇐ voice_event{event:{type:"voice_status", status:"preparing"}} → connecting
     ⇐ voice_event{event:{type:"voice_status", status:"ready"}}     → active; flush voice.queue as voice_event frames
     mic capture at audio_in_format.sample_rate, PCM16 LE mono, 100 ms chunks → WS→ {type:"voice_audio_in", audio}
       (only after ready; earlier chunks are dropped by the server)
     ⇐ voice_audio_out{audio} → play at audio_out_format.sample_rate (owner only)
```

- **V-4. Command queue.** Provider-bound frames (`voice_session_update`, `voice_command.command`, client control events) MUST be queued until the transport is ready (data channel open, or `voice_status: ready`) and flushed in FIFO order. The queue is cleared on teardown. **No cap and no dropping**: a dropped frame can be the `session.update` (field bug, inv04 RS-04/B2); the queue only lives until the transport is ready or torn down.
- **V-5.** Only the initiator forwards `voice_session_update` (G-32). Only the owner executes `voice_command`; a passive device ignores it (the server falls back to broadcast when the owner socket is stale, 01 §7.4).
- **V-6.** No `status`/`turn_complete` framing exists in voice mode (G-4). The conversation reducer does not need it (§4.7).
- **V-7.** After `voice_ended` the owner MAY re-send `start` (text) on the same socket; it is idempotent.
- **V-8.** If the owner's socket drops, the server ends voice at once (G-31). On reconnect, the owner SHOULD re-arm with `voice_start` using the same config if voice was active at the drop (Android parity, 03 §3.3), and MUST NOT send a second plain `start` for the same reconnect (03 §3.3 "two start frames").

### 7.4 Barge-in (owner)

- **V-9.** WS providers (Qwen): on `input_audio_buffer.speech_started` the owner MUST flush local playback and, only if a response is in flight (`response.created` seen and no `response.done` yet), send `voice_event{event:{type:"response.cancel"}}`. A cancel without an active response makes DashScope close the socket (02 §7.11).
- **V-10.** Google: on `serverContent.interrupted` flush local playback; no cancel frame.
- **V-11.** OpenAI WebRTC: the provider handles interruption; the client sends nothing.

### 7.5 Passive viewers (fixes the wedge, W-1)

```
passive state = { remoteActive: boolean }          // the device's OWN voice.state stays "off"
session_started{voice:true, voice_initiator:false} → remoteActive = true
voice_owner_active{active:true}  and not voice.pendingStart and voice.state == off → remoteActive = true
voice_owner_active{active:false} | voice_ended | voice_stopped → remoteActive = false
```
- **V-12.** A passive device MUST NOT change its own `voice.state` from mirrored provider events, MUST NOT hide its text input, MUST NOT play `voice_audio_out` and MUST NOT send any `voice_*` frame. Its voice button shows "Active elsewhere" (disabled). The conversation reducer still processes `voice_event`, `tool_use`/`tool_result`, `voice_ended` (VT-2, VT-3).
- **V-13. Ownership loss.** If the owner receives `voice_owner_active{active:true}` while `voice.pendingStart == false`, another device took over: tear the transport down locally **without** sending `voice_stop`, set `voice.state = off`, `remoteActive = true`. The same applies to `session_started{voice:true, voice_initiator:false}` received while the client holds a live transport and has no `voice_start` in flight. A client MUST NOT become a non-owner while keeping its transport up: such a call drops every `voice_command` and ignores `voice_ended` (2026-10-08).

### 7.6 Ending

```
stopVoice(): WS→ {type:"voice_stop"}; voice.state = ending; start a 5 s timer
⇐ voice_ending{reason, session_id} → ending (owner) ; ⇐ voice_ended{reason, session_id} → close transport, off
⇐ voice_stopped{} (legacy alias) → same as voice_ended
timer fires before voice_ended → close transport, voice_local_end, off
```
`reason ∈ {user_stop, agent_end, client_disconnect, error, shutdown, switch}`. `switch` is quiet (§6.11a SW-3). `agent_end` (the agent called `end_voice_session`) SHOULD play the closing state like a user stop. Every end path, including timeouts and errors, MUST deliver `voice_local_end` or `voice_ended` to the reducer (A-4.3 compounding paths).

### 7.7 Voice errors and reconnect banners

| Event | Client behaviour |
|---|---|
| `voice_event{event:{type:"voice_status", status:"reconnect_warning", time_left}}` | banner "Pausing in ~Ns to reconnect…" |
| `voice_status: reconnecting` | banner "Reconnecting…" (keep the transport) |
| `voice_status: ready` | clear banners. Qwen sends no `ready` after a reconnect (G-35): clear the banner on the next transcript or audio-out too |
| `voice_event{event:{type:"voice_error", error:{category, message, recoverable, recovery_hint, …}}}` | show `message` (+ `recovery_hint`). `recoverable:false` ⇒ treat as fatal (below) |
| `voice_event{event:{type:"error", error:{code:"voice_relay_failed", message}}}` (legacy; `error` is an object here, G-28) | fatal |
| top-level `voice_connection_error{detail}` | error state with `detail` (Android ignores it today, 03 §3.6) |
| top-level `error{voice_event_failed|voice_audio_failed}` | voice banner; keep going |
| fatal | `voice_stop`, wait ≤ 5 s for `voice_ended`, else `voice_local_end`; state `error` showing the message (A-8.7). Retry = `voice_start` (G-21) |

The VAD indicator comes from `voice_event{event:{type:"voice_vad_state", state, duration_ms, silero_prob}}`: after 3 s of `listening` show "Listening Ns". It MUST be in the voice bar, which is visible during voice (W-4).

### 7.8 Recording

When `session_started.voice_recording_enabled` and the transport is WebRTC, the owner records both channels with the AudioWorklet registered as **`pcm-capture`** (W-2) and sends `voice_recording_chunk{channel:"user"|"assistant", audio}` every 5 s, then `voice_recording_end`. WS providers are recorded server-side.

### 7.9 Shared inject during voice

`inject_text` during voice is silent (no spoken reply). The server broadcasts `user_message{text, source:"shared_inject"}` to **all** subscribers including the sender (`api/routes/orchestrator.py:1058-1062`); the sender matches it to its pending entry (`pendingInjects`) instead of adding a second one (A-4.4.4).

---

## 8. Settings and configuration

### 8.1 Server-global (shared by every device)

| Store | Endpoint | Keys |
|---|---|---|
| Global config | `GET/PUT /api/config` (partial PUT, full object back) | `working_directory`, `working_directory_history`, `enabled_mcps`, `chrome_extension`, `provider`, `default_model`, `summarizer_model`, `harness_model` (`{provider: id}`, "" = CLI default), `harness_options` (`{provider: {key: value}}`; PUT merges per key, `null` deletes the key = CLI default), `default_voice_provider`, `default_voice_model`, `default_voice_name`, `default_voice_transcription_language`, `default_voice_endpoint`, `voice_recording_enabled`, `voice_vad_threshold`, `voice_vad_min_silence_ms`, `voice_mic_gain` |
| Per-session config | `GET/PUT /api/sessions/{sdkId}/config` | `working_directory`, `enabled_mcps`, `chrome_extension`, `provider`, `harness_model`, `harness_options` (`null` = inherit; `harness_options` is a whole-map overlay, see §6.14) |
| Harness catalogs | `GET /api/config/harnesses[?refresh=true]`, `GET /api/config/harness/{p}/catalog[?refresh=true]` | `{harnesses: [{id, label, description, catalog \| null}]}`; catalog = `{provider, models: [{id, label, source, description?, context_window?, supports_thinking?, supports_vision?, efforts?, default_effort?}], options: [{key, label, kind: select\|toggle\|number, choices?: [{value, label, description?, models?}], default?, help?, models?, min?, max?, step?}], default_model, allow_custom_model, warnings}`. Shared keys: `effort`, `thinking`. Cached ~5 min server-side; `refresh=true` rebuilds. Older servers: 404 → use `/api/config/providers` + `/api/config/harness/qwen/models` |
| Titles | `PATCH /api/sessions/{sdkId}/rename`, `PATCH /api/visualizations/rename` | — |
| Catalogs (read-only) | `/api/config/harnesses` (supersedes `/api/config/providers` + `/api/config/harness/qwen/models`, kept for older clients), `/api/orchestrator/models`, `/api/orchestrator/voice/models`, `/api/config/voice/google/models`, `/api/mcp/servers`, `/api/skills`, `/api/agents` | — |
| Claude CLI auth | `/api/auth/status`, `/api/auth/login` (legacy; 409 while a link sign-in runs), `/api/auth/credentials` | Both clients SHOULD check status after connecting. The AuthGate's "Sign in with Claude" uses the link sign-in (`POST /api/accounts/claude/login {method: "token"}`, below) and falls back to `/api/auth/login` only on servers without `/api/accounts`; credentials paste stays the alternative. |
| Accounts | `GET /api/accounts`, `GET /api/accounts/{id}`; `POST /api/accounts/{id}/login {method}` → flow, `GET …/login` (poll; 404 = none), `POST …/login/code {code}` (waits ≤15 s for the verdict), `DELETE …/login` (cancel); `POST …/credentials {method, content}` → `{message, service}`; `POST …/logout` → `{message, service}`; `POST …/verify` → service with `verified` | Service: `{id, label, group: harness\|api\|other, description, state: signed_in\|signed_out\|expired\|unavailable\|unknown, method, account, plan, expires_at, detail, warnings[], used_by[], docs, can_verify, verified: {ok, message, checked_at} \| null, flow \| null, methods: [{id, kind: link\|credentials\|env\|signout, label, description, recommended, active, available, unavailable_reason, needs_code, code_label, code_help, input: json\|secret, path, source_hint, placeholder, warning, fields: [{name, label, secret, help, placeholder, choices \| null, set, preview, value}]}]}`. Flow: `{id, service, method, status: starting\|waiting\|verifying\|succeeded\|failed\|cancelled\|expired, url, user_code, needs_code, code_label, code_help, message, started_at, expires_at, finished_at}`. One flow per service (another method → 409). See [authentication.md](../harnesses/authentication.md). |
| Env keys (`context/.env`) | `GET /api/env` → `{path, exists, keys: [{name, set, preview, length, line, exported, duplicates, in_process}]}`; `POST /api/env/{name}/reveal` → `{name, value}`; `POST /api/env {name, value}` (409 if it exists); `PUT /api/env/{name} {value}` (create or update); `DELETE /api/env/{name}` | Changes answer `{applies: now\|backend_restart, note, key?\|name?, removed?}`. Names `^[A-Z_][A-Z0-9_]*$` (400 otherwise). |

- **ACC-1.** Lists and statuses carry masked previews only; a client MUST fetch a full value only on an explicit user action (reveal, edit) via `POST /api/env/{name}/reveal`, keep it in view state only (never in persisted storage) and drop it when the page closes.
- **ACC-2.** While a service's `flow.status` is `starting|waiting|verifying` the Accounts page polls `GET …/login` (2 s) and, when it ends `succeeded`, refetches that service; a Claude change also re-checks `/api/auth/status` (the AuthGate).
- **ACC-4.** `/api/accounts/*` and `/api/env/*` answer 403 to cross-site browser requests on every method, reads included (`Sec-Fetch-Site: cross-site`, or an `Origin` other than the server / the web dev servers / Archie's browser extension / `ARCHIE_TRUSTED_ORIGINS`), on top of the API-wide guard (T-4a). Clients on the server's own origin and native clients (no `Origin`) are unaffected.
- **ACC-3.** The page refetches `GET /api/accounts` on open (CFG-3) and after every env change (keys are shared between services: `GEMINI_API_KEY`, `DASHSCOPE_API_KEY`).

- **CFG-1.** Each control saves immediately with a partial `PUT`; the response replaces the local copy. Sliders MUST commit on release, not on every tick (W-6.2). Controls of the section in flight are disabled.
- **CFG-2.** On 400 the client MUST show the backend `detail` string (W-6.2).
- **CFG-3.** Config is not broadcast (G-27): every settings screen refetches on open.
- **CFG-4.** `enabled_mcps: []` means "all enabled" (`api/routes/config.py:125`). The UI MUST show every server checked when the list is empty. Unchecking server X when the list is empty writes the explicit list of all other servers. Checking every server writes `[]`. (Fixes A-8.1; the web's all-unchecked display, W-6.2.)
- **CFG-5.** Voice defaults cascade server-side (01 §3.6); after a PUT the client re-renders from the returned object instead of patching locally.
- **CFG-6.** Google auto-correct (02 §7.12): if `default_voice_provider == "google"` and the saved model is not in a non-empty discovered list, PUT the discovered default once and show the dismissible notice.
- **CFG-7.** `working_directory_history` PUT is a full replacement; local paths must exist on the server. Android gets full add/edit/delete parity (03 §5).
- **CFG-8.** `default_model` is stored but the backend ignores it (G-37). Clients MUST label it accurately ("Default for new orchestrator sessions") and see Appendix B Q3.

### 8.2 Device-local (never sent to the backend)

| Setting | Web storage | Android storage |
|---|---|---|
| Backend URL, saved servers, auto-connect | n/a (page origin) | DataStore |
| Theme (system/light/dark) | localStorage | DataStore |
| Open views, active view, sidebar section, drafts | sessionStorage (per browser tab) | saved state + DataStore |
| Last orchestrator `localId` (hint only; validated by `syncPool`) | memory | DataStore |
| Mic gain, speaker volume, echo ducking, audio output route | — | DataStore (chapter 04) |
| Wake/talk words, sensitivities, button trigger | — | DataStore (chapter 04) |
| Remote console logging (default on for compat and lite) | localStorage flag | n/a |
| "Agent session finished" notifications (TURN-2; off by default; enabling asks the OS/browser permission from that tap) | localStorage `prefs:v1` `notifyAgentTurns` | DataStore `notify_agent_turns` |
| Conversation snapshots + checkpoint (T-10) | memory only | optional snapshot on `onStop` |

Storage writes MUST NOT happen per streamed event (A-8.14). Every storage access on the web is wrapped in try/catch (private mode, Safari 12).

---

## 9. Visualizations and Memory

These flows exist on the web today and are new on Android (charter goal 5). Lists and files are fetched over REST; since 2026-10-09 the backend also pushes change events (§9.3) so open views reload live, and links to either open in the app (§9.4).

### 9.1 Visualizations

```
list:    GET /api/visualizations → [{path, url, title, created, modified, size}]  (sorted by modified desc)
refresh: when the section opens; manual Refresh / pull-to-refresh; after any view's endTurn (debounced 2 s,
         compat included, 02 §5.2 regression); on agent_session_opened/closed; on visualization_changed
         (debounced 400 ms, §9.3)
open:    view key "viz:<path>"; load <origin> + encodePath(url) in an iframe (web) / WebView (Android)
rename:  optimistic title → PATCH /api/visualizations/rename {path, title} (204; 404 tolerated) → refresh
```
- **VZ-1.** `url` is not percent-encoded (G-38): encode each path segment with `encodeURIComponent`.
- **VZ-2.** Unknown paths return `200` + the SPA `index.html` (G-38), except unknown `*.html` / `*.htm` paths, which return `404` since 2026-10-09 (the app routes on the URL hash, so no app route ends in `.html`; a stale link must not load the whole app inside the viewer). For other paths, a client that needs to know whether a file still exists MUST `GET` it and check `Content-Type` and that the body is not the app shell. `HEAD` is not supported (405).
- **VZ-3.** Web iframe sandbox: `allow-scripts allow-same-origin allow-popups allow-forms allow-modals` (no top navigation, no downloads). Android WebView: JavaScript and DOM storage enabled, same origin as the backend, navigation outside the visualization opens the external browser, file access disabled, the self-signed certificate accepted only for the configured backend host.
- **VZ-4.** A visualization view keeps its frame/WebView alive while hidden (state survives view switches). Reload = remount (web) / `reload()` (Android). "Open in browser" opens `<origin><url>`.
- **VZ-5.** Item meta: relative `modified` time (parsed with its UTC offset, A-8.4), parent folder name or "public". Rename only; no delete.

### 9.2 Memory

```
tree:   GET /api/memory/tree → MemoryNode[] {name, path, is_dir, children|null}   (dirs first, alphabetical)
file:   GET /memory/<encodePath(path)> → raw markdown (text/markdown), 404 for dirs/missing
root:   GET /memory/ → MEMORY.md
refresh: when the section opens; manual Refresh; on memory_changed with a created/deleted file while the
         tree is loaded (§9.3). (No refresh on turn end.) An open document refetches on memory_changed for it.
```
- **MEM-1.** Split leading frontmatter with `/^---\r?\n([\s\S]*?)\r?\n---\r?\n?/` and show it verbatim in a collapsed "Frontmatter" section; render the rest as markdown (02 §7.13).
- **MEM-2.** Relative links (`[x](../projects/frontend-refactor/folder/y.md)`, `#anchor`) MUST resolve against the directory of the current file, normalising `.` and `..`. A result inside the memory root that ends in `.md` opens a memory view (`memory:<path>`) in the app; a result escaping the root, or an absolute `http(s)` link, opens externally (fixes 02 F-37).
- **MEM-3.** Paths are percent-encoded per segment when fetched (02 §6.2).
- **MEM-4.** Folder expand state is per view and local; top-level folders start expanded.
- **MEM-5.** Read-only: no write, rename or search endpoints exist.

### 9.3 Live changes

The backend's content watcher (`backend/api/content_watcher.py`, one `watchfiles` loop over `context/public/`,
`context/memory/` and `docs/`) pushes two frames to **every orchestrator socket**, the same fan-out as the pool
watcher events (§3.7: they arrive with no Archie view, subscribed or not):

```
visualization_changed {visualizations: [{path, kind}], files: [{path, kind}]}
    visualizations: list paths (GET /api/visualizations `path`) whose page or one of its assets changed
    files:          the raw changed paths under context/public/
memory_changed {changes: [{path, kind}]}
    changes:        markdown paths as in GET /api/memory/tree (docs/x.md is reported as archie/x.md)
kind: "created" | "modified" | "deleted"   (advisory: an atomic save or an rsync reads as a create)

onContentFrame(f):                      // channel level, never the conversation reducer
  visualization_changed: for c in f.visualizations: stamp.visuals[c.path] = {version+1, deleted: c.kind == deleted}
                         refreshVisuals() debounced 400 ms
  memory_changed:        for c in f.changes: stamp.memory[c.path] = {version+1, deleted: …}
                         if memory tree loaded and some kind != modified: refreshTree() debounced 400 ms
```

- **VZ-6.** An open visualization view reloads when its stamp moves past the version it loaded (a change from
  before it opened is already in what it loaded), once per burst (300 ms debounce) (web: the
  iframe stays mounted but waits while hidden; Android: the pooled WebView remembers the version it loaded), so
  a change that arrived while the view was hidden reloads it when it shows again. A deleted page is not reloaded; the meta line says
  "Deleted". The reload keeps the scroll position (web: same-origin frames are scrolled back after `load`;
  Android: `WebView.reload()`), and a small "Updated" cue shows for 2.5 s. An open memory document refetches
  in place the same way (the text stays visible, so does the scroll position); its cue shows only when the
  text changed. **Fallback:** when the orchestrator socket opens again after a drop (not on the first open),
  the client refetches the visualization list and bumps every entry whose `modified` moved (vanished ones as
  deleted), and bumps a resync epoch on which open memory documents refetch quietly. An asset-only change of a
  folder visualization during the outage is not caught by this fallback (the list's `modified` is the page's).
- **VZ-7.** Batches are coalesced on the server (`awatch` step 300 ms, max 1.5 s); temp and editor files
  (rsync's `.name.XXXXXX`, `*.swp`, `*.tmp.*`, dot-folders, `node_modules`) never appear. An asset maps to the
  pages in its folder or an ancestor folder (below the public root) whose source names it, else to the nearest
  `index.html` above it, else to nothing (the list still refreshes). Public and memory files are served with
  `Cache-Control: no-cache` + `ETag` (304 when unchanged), so a reload never shows a cached copy, and
  `/<dir>/` serves `<dir>/index.html` (`/<dir>` redirects there).

### 9.4 Internal links

In chat (agent sessions and Archie, including plans and tool output on the web) and in memory documents, a
link to a visualization or a memory file opens the in-app view (`viz:<path>` / `memory:<path>`, the same
navigation as the Visuals / Memory lists) instead of a browser tab. One resolver per platform
(`apps/web/src/features/links/internalLinks.ts`, `:core:markdown` `InternalLinks.kt`), both checked against
the shared corpus `apps/protocol-fixtures/links/internal-links.json`. First match wins:

- **LNK-1** Filesystem paths as agents print them: `context/public/<x>.html` → visual, `context/memory/<x>.md`
  → memory (relative, `./`, absolute `/home/u/assistant/context/…`, or `~/…`); `docs/<x>.md` and
  `…/assistant/docs/<x>.md` → memory `archie/<x>.md`. A trailing `:line` / `:line-line` is dropped.
- **LNK-2** Root-relative URLs: `/memory/<x>.md` (`/memory/` → `MEMORY.md`), the old
  `/markdown_reader.html?file=memory/<x>.md`, and `/<x>.html` or `/<dir>/` (→ `<dir>/index.html`) outside
  the app's own prefixes (`api assets compat legacy legacy_compat next next-compat memory uploads projects`),
  with no query string. Absolute filesystem roots (`/home/`, `/tmp/`, …) are not URLs.
- **LNK-3** `http(s)` URLs on the backend's host (any port) → as LNK-2.
- **LNK-4** Other private-network hosts (LAN, Tailscale CGNAT `100.64/10`, `*.ts.net`, localhost: the other
  Archie machine, whose content is synced) → LNK-2 only for `/memory/…`, the reader, `/visualizations/…`, or a
  path that is in the visualization list.
- **LNK-5** Auto-linking: inline code that is exactly an internal path or URL (no whitespace or glob
  characters) becomes a link; bare `context/public/….html` / `context/memory/….md` paths in plain text too.
  `docs/` paths only from inline code; never inside fenced code or an existing link. The link's href is the
  target's canonical URL (`/memory/archie/x.md`, `/x/index.html`), never the printed path, which a memory
  document's MEM-2 resolver would resolve relative to itself.
- **LNK-6** Web: a plain click opens in the app; middle / ctrl / cmd-click keep the browser default on the
  real URL (the anchor's `href`). Memory documents ask their relative-link resolver (MEM-2) first.
- **LNK-7** A visual target opens in the viewer only when it is a real page: in the visualization list,
  or in the list after one refresh (also under `visualizations/`). Otherwise the URL opens externally (web: a
  snackbar with "Open in browser", a user gesture; Android: the Custom Tab). A segment that decodes to a
  separator (`..%2F..`) makes a path not internal. Android opens only `http(s)`, `mailto`, `tel`, `sms` and
  `geo` links externally; anything else (`file:`, `content:`, `javascript:`, `intent:`) is inert.

**Link convention for agents.** Print root-relative markdown links, which work in the app and, against the
server, in any browser: `[Avatar pipeline](/avatar-pipeline/index.html)`, `[Energy](/visualizations/energy.html)`,
`[Voice notes](/memory/projects/voice.md)`, `[Client protocol](/memory/archie/specs/12-client-protocol.md)`.
A full URL on the server (`https://<server>/avatar-pipeline/`) also opens in the app, for a link meant to be
pasted elsewhere.

---

## 10. Bug-prevention rules

IDs: **W-n** = item n of 02 §6.3; **W-6.1 / W-6.2** = the lists in 02 §6.1 / §6.2; **A-x.y** = section or item of chapter 03 (A-8.n = item n of 03 §8 "Bugs"). Items that are pure UI/styling are listed so nothing is lost, with the spec that owns them.

### 10.1 Web (02 §6.3 and §4.2)

| Bug | Rule |
|---|---|
| W-1 Passive voice viewers wedge (input hidden forever) | V-12: a passive device's own voice state never changes from mirrored events; only `remoteActive` does (§7.5). |
| W-2 WebRTC recording never records (`pcm-capture-processor` vs `pcm-capture`) | §7.8: the worklet name is `pcm-capture`; a conformance check loads it at startup in dev builds. |
| W-3 Voice tool calls never complete | Tool cards come only from top-level `tool_use`/`tool_result` frames, never from provider events (§4.7); R-1; VT-3 sweeps at voice end. Fixtures `voice_mode_tool_result_gemini`, `android_voice_ordering_bug`. |
| W-4 VAD "listening Ns" never visible | §7.7: the indicator lives in the voice bar shown during voice. |
| W-5 Tool results lost after an interleaved user message; empty assistant bubbles | R-1 (by id anywhere), PM-2 (no entry for permission resolutions), I-12 (queued prompts in the tray), I-10 (no empty runs). Fixtures `web_bug5_permission_feedback_then_result`, `tool_result_after_interleaved_user_message`. |
| W-6 `dropLastN` mismatches the backend | §6.5: prompt-anchored cut resolved against a fresh REST listing, with verification and abort. |
| W-7 Reload mid-turn duplicates content | T-10 (no persisted checkpoint after a rebuild) + §5.4 overlap dedupe. Fixture `history_live_overlap_dedupe`. |
| W-8 Deleting an open session leaves its tab | §6.8: close every view with that `sdkId`/`localId` and the pool session, then `DELETE`. |
| W-9 Terminated tab closes immediately | Superseded 2026-10-10 by OPEN-3: a closed session's view closes on every device, with a notice carrying the termination reason (§6.13). |
| W-10 Voice start sends `stop` on the text WS | T-6: one orchestrator socket; `voice_start` on it; never `stop` to start voice. |
| W-11 Past orchestrator conversations cannot be viewed | H-3 read-only views; §6.11 resume through the conflict dialog. |
| W-12 Compat GFM shim strips inline formatting | UI/markdown spec (13-…): the compat table shim must keep inline markdown in non-table paragraphs. Not a protocol rule. |
| W-13 Compat MessageList shim not wired | UI spec: compat scroll helpers are wired by the build, verified by a compat smoke test. |
| W-14 Compat overlays use `inset` | Design-system spec: no `inset`; explicit offsets. |
| W-15 Orchestrator `read_file` renders as generic JSON | TC-4. |
| W-16 Thinking absent from reloaded history | §5.7; optional backend fix O-2 (Appendix A). |
| W-17 Tool on an older page shows "done" with no output | H-5 (orphans attach when the older page loads); TC-2 (`no_result`, never fake "done"). Fixture `tool_result_on_next_history_page`. |
| 02 §4.2 `turn_complete` leaves blocks streaming | I-7 / `endTurn`. |
| W-6.1 focus stealing by pool sync / `agent_session_opened` | FOCUS-1. |
| W-6.2 raw `websocket_error` banner, no dismiss | §4.4.4 connection banner with human copy, dismissible. |
| W-6.2 silent no-ops before the first turn | ID-3. |
| W-6.2 backend `detail` swallowed | CFG-2. |
| W-6.2 sliders PUT on every tick | CFG-1. |
| W-6.2 `enabled_mcps: []` shown as all unchecked | CFG-4. |
| W-6.2 memory links resolve against the app root; paths not encoded | MEM-2, MEM-3. |

### 10.2 Android (chapter 03)

| Bug | Rule |
|---|---|
| A-4.3 Tool calls bunch under the first one (voice mode; `streamingMessageId`) | I-1 tail attachment (no message id is ever tracked), I-2 boundaries, VT-1, VT-3 (every voice end path, owner and passive). Fixture `android_voice_ordering_bug`. |
| A-4.3 (a) error / compaction bubble mid-turn, later blocks land above it | I-1/I-2: later content goes into a new run below the notice; I-5 continuation keeps split text exact. Fixture `compaction`. |
| A-4.3 (b) a socket drop mid-turn splits the turn | A transport disconnect MUST NOT modify `entries`; replayed events continue the tail run (I-1). Fixture `replay_after_reconnect_overlap`. |
| A-4.3 (c) a result after the split is dropped | R-1. |
| A-4.4.1 REST refetch on orchestrator reconnect wipes the live turn | SEQ-7 and §5.7: no automatic REST replace for the orchestrator; canonical reloads buffer live frames (§5.6). |
| A-4.4.2 History re-keys the whole list on every fetch | Entry ids are client-generated once per entry and stable for its life; prepend and reconcile never change existing ids. |
| A-4.4.3 Rewind/fork index drift | §6.5. |
| A-4.4.4 Shared-text bubble duplicated in voice mode | §7.9 `pendingInjects`. |
| A-4.4.5 Result on another history page shows no output | H-5. |
| A-4.4.6 Load-more with a stale `start_index` | §5.3 abut check → canonical reload. |
| A-4.4.7 Unsynchronised reducer state across threads | L-4: one consumer, immutable snapshots. |
| A-3.3 Event bus drops events (`tryEmit`, capacity 64) | L-2 lossless ordered delivery; L-3 audio on its own path. |
| A-3.3 Fixed 3 s reconnect, no network callback | T-13, T-14. |
| A-2.2 / A-8.14 Checkpoint written to DataStore per token; keys never pruned | T-10: memory only (optional snapshot on `onStop`), pruned when the view closes. |
| A-8.1 MCP toggle inverts | CFG-4. |
| A-8.2 "Error: Failed to connect" bubble every 3 s | I-15: transport errors are a banner, never entries. |
| A-8.3 No reconnect after a server switch | T-15. |
| A-8.4 Relative times off by the UTC offset | All ISO timestamps MUST be parsed with their offset (`OffsetDateTime`/`Date.parse`), then shown in local time. |
| A-8.5 Termination "Continue" always reopens as an agent | §6.13: recovery keeps the view's kind. |
| A-8.6 `send_audio` routed to the agent socket | §6.16: `send_audio` only on the orchestrator WS. |
| A-8.7 `VoiceState.Error` message never shown | §7.7. |
| A-8.8 Recents `/dev/input` monitor ignores its toggle | Device spec (chapter 04 / lite app): every hardware trigger honours its setting. |
| A-8.9 About shows "1.0.0" | UI spec: read `BuildConfig.VERSION_NAME`. |
| A-8.10 Light theme broken | Design tokens (D3/D6): no hard-coded colours. |
| A-8.11 System tab "Couldn't load configuration" after process death | CFG-3: load when the screen becomes visible, not on a tab click. |
| A-8.12 BT/wired availability not live | Audio spec: observe `AudioDeviceCallback`. |
| A-8.13 Speaker slider drifts from the system volume | Device-local settings read the live system volume when shown. |
| A-8.15 Scanner accepts any host with port 80 open | DISC-1: a host is a backend only if `GET /api/auth/status` returns a JSON object with boolean `authenticated` and `headless`. |
| A-1.1 Auto-navigation on connect / orchestrator events | FOCUS-1. |
| A-1.3 IME Send ignored while streaming; no queueing | §6.1: sending while busy is allowed and goes to the tray. |
| A-3.6 `permission_*`, `session_stalled`, `voice_connection_error`, orchestrator `compact_complete` ignored | §4.3 handles every frame type; §6.9, §6.12, §7.7. |
| A-5 Non-owner devices show no transcripts | VT-2 (WS providers mirror from the relay; OpenAI from the owner's mirror, re-broadcast by the backend). |

---

## 11. Conformance fixtures

`shared/protocol-fixtures/` contains synthetic fixtures (recorded ones will be added from live transcripts, README charter quality gate). The schema, the input vocabulary and the normalisation are defined in `shared/protocol-fixtures/README.md`. Summary:

- A fixture runner builds a `Conversation` from `session`, applies `initial_history` as `history_page{replace}` (modelling §5.2: REST first, then the held frames), then feeds `events` in order through `onFrame` (§3.6) and the reducer (§4.3), executing each `client_actions` item before the event whose index is its `at`.
- It normalises the state and compares it with `expected`: `entries`, `orphan_results` and `unattributed_results` are compared exactly; `queue` exactly when present; `state` keys only when present.
- Both platforms MUST pass every fixture in CI (web: vitest; Android: JUnit in `:core`). A fixture that fails on one platform is a spec bug or a platform bug, never "platform difference".

| Fixture | Covers |
|---|---|
| `plain_text_turn` | I-1, I-5, counters, `sdkId` learning |
| `text_tool_interleaving` | I-3, I-4 |
| `parallel_tools_reverse_results` | R-1 |
| `thinking_and_text` | thinking blocks, status |
| `android_voice_ordering_bug` | A-4.3: `tool_use`, transcript, `tool_use`, text, no `turn_complete` |
| `interrupt_mid_tool` | TL-2, R-6, tray cleared |
| `orchestrator_error_no_turn_complete` | TL-4, §4.4.4 |
| `web_bug5_permission_feedback_then_result` | W-5, PM-2, I-12, deny-with-feedback |
| `permission_request_resolve` | PM-1…PM-3 |
| `stall_notice_repeated_seq` | SEQ-2 |
| `replay_after_reconnect_overlap` | SEQ-1, SEQ-5, A-4.3 (b) |
| `replay_overflow_rest_reload` | SEQ-6, §5.6 |
| `history_load_live_continuation` | §5.2, history tool still running |
| `history_live_overlap_dedupe` | W-7, §5.4 |
| `compaction` | auto + manual compaction notices, I-2 |
| `background_notification_wake_turn` | BG-1, TL-5 |
| `orchestrator_echoed_prompt_no_background_notice` | BG-1 (O-3 echo is a visible prompt) |
| `orchestrator_voice_message_echo_no_background_notice` | VM-1, BG-1 (another device's voice message is a visible prompt) |
| `orchestrator_voice_message_sender_and_history` | VM-1 (sender: one local bubble, no echo), §5.1 `[audio:<fmt>]` lines |
| `history_prepend_queued_prompt_dispatch` | H-6 (prepend keeps `promptSinceTurnEnd`), I-12 |
| `history_prepend_voice_anchor_shift` | H-6 (anchor shift, open transcript survives a prepend), I-9 |
| `orchestrator_closed_by_pool` | OPEN-3, FOCUS-2 |
| `start_failed_releases_held_frames` | SEQ-5 exception, SEQ-8, I-15 |
| `orchestrator_agent_approvals_and_jsonl_id` | ID-4, PM-5 |
| `voice_transcript_coalescing_gemini` | §4.7 fragments |
| `voice_late_user_transcript_anchor` | I-9 |
| `queued_prompt_echoed_twice` | G-5, I-12 (observer) |
| `queued_prompt_sender` | I-12 (sender) |
| `queued_prompt_observer_no_reecho` | I-12 (observer, backend O-6: no re-echo) |
| `termination_banner` | §6.13 (reducer state; the view then closes, OPEN-3), R-6 |
| `tool_result_after_interleaved_user_message` | R-1 across an inject entry |
| `tool_result_after_voice_transcript` | R-1 across a transcript |
| `tool_result_after_turn_ended` | R-6 `no_result` → `done` |
| `tool_result_before_tool_use` | R-2 |
| `tool_result_on_next_history_page` | H-5, W-17 |
| `tool_result_string_shape_empty_id` | R-3, R-4 (inferred) |
| `tool_result_empty_id_parallel_reconcile` | R-4, R-7, §5.5, BF-1 workaround |
| `voice_mode_tool_result_gemini` | R-3 (object output), VT-3 |
| `voice_passive_viewer_no_wedge` | W-1, V-12, VT-3 |
| `shared_inject_voice_echo` | §7.9, A-4.4.4 |

---

## Appendix A — Backend fixes required

Only fixes that a client cannot reasonably work around. Everything else in 01 §8.2 is optional; the clients above already cope with it.

### BF-1 — Live Claude tool results must carry the real `tool_use_id` and output (required, highest priority)

- **Problem.** `manager/claude/session.py:1109-1138` builds `ToolResult` from `UserMessage.tool_use_result`. In real CLI output that field is tool-specific metadata (Bash `{stdout, stderr, interrupted, isImage, noOutputExpected}`, Edit `{filePath, oldString, …}`, MCP: a list), so the broadcast `tool_result` has `tool_use_id: ""` (main-thread `parent_tool_use_id` is null) and an empty or wrong `output`; list-shaped results (MCP tools) are not emitted at all. The real id and output are in `UserMessage.content` as `ToolResultBlock`s, which the code ignores. Verified on 2026-10-03 with `claude -p … --output-format stream-json --verbose` (user line: `"tool_use_result": {"stdout":"probe123",…}`, `"message":{"content":[{"tool_use_id":"toolu_…","type":"tool_result","content":"probe123","is_error":false}]}`) and a survey of 40 recent JSONLs (no `toolUseResult` has `tool_use_id`). The unit tests `tests/test_session.py:581-650` mock a shape the CLI never sends.
- **Fix.** In the `UserMessage` branch, if `msg.content` is a list, emit one `ToolResult` per `ToolResultBlock` (`tool_use_id = block.tool_use_id`; `output` = string content, or text items joined with `\n` exactly like `manager/protocol.py:191-201`; `is_error = bool(block.is_error)`). Fall back to the current `tool_use_result` logic only when the content has no `ToolResultBlock`. Update the tests to the real shape.
- **Why the client cannot fix it.** For parallel calls the result cannot be attributed (R-4), and for most tools the output is empty until the turn ends and a REST reconcile runs (R-7). Live output during long turns is impossible without it.
- **Size / risk.** ≈ 25 lines + tests. Low risk: same event type and fields; both clients already match by id; the replay ring and JSONL are untouched.

### BF-2 — Broadcast `status: interrupted` to every subscriber (required)

- **Problem.** `api/routes/chat.py:205-210` replies `{"type":"status","status":"interrupted"}` to the interrupting socket only, and the cancelled turn sends no `turn_complete` (G-4). Other devices never learn that the turn ended: their busy state, running tool cards and queue tray stay until the next prompt.
- **Fix.** When `pool.cancel_turn()` returns `True`, broadcast the same frame to all subscribers of the session (keep the direct reply when it returns `False`).
- **Why the client cannot fix it.** The only alternative is polling `GET /api/sessions/pool/live` while busy.
- **Size / risk.** ≈ 5 lines. Low risk: clients already handle the frame; the sender receives it once.
- **Also the orchestrator's runner (2026-10-10).** Turns the runner ends (timeout, `interrupt_agent_session`, failure) never pass through `chat.py`, so the runner broadcasts the frames itself: `error{turn_timeout}` then `status{interrupted}` on timeout, `status{interrupted}` on cancel, `error{upstream_wedged|send_failed}` on failure (`backend/orchestrator/runner.py` `_tell_tabs`). These are unsequenced (not in the replay ring), so a socket that was away still relies on ST-2.

### BF-3 — nginx request-body limit (required, infrastructure)

- **Problem.** `~/nginx-server.conf` on the Jetson has no `client_max_body_size`, so nginx rejects bodies over 1 MiB with an HTML 413 before the app sees them (G-1). Uploads and shares of photos/documents fail.
- **Fix.** `client_max_body_size 200m;` in both server blocks (the app enforces 200 MB itself).
- **Size / risk.** 2 lines of config; no code risk.

### Optional (clients work around them today; do them if convenient)

| ID | Fix | Client workaround it would remove |
|---|---|---|
| O-1 | Orchestrator history: fold `tool_use`/`tool_result` JSONL lines into REST blocks; persist each text block at `TextComplete` (G-8) | §5.7 footnote; no auto reload |
| O-2 | `manager/protocol.py`: read `block["thinking"]`, emit `type:"thinking"` (G-9) | missing thinking after reload (W-16) |
| O-3 | Orchestrator: `jsonl_id` in `session_started`; broadcast `user_message` for typed `send` and `user_message{source:"voice_message"}` for `send_audio` (excluding the sender, VM-1); broadcast a `background_notification` frame before wake turns (G-14, G-22) | BG-1 inference; `pool/live` lookups |
| O-4 | Relay fatal failure → `end_voice("error")` (G-21) | §7.7 `voice_stop` + 5 s timeout |
| O-5 | Do not stamp `session_stalled` with the previous seq (G-18) | SEQ-2 exemption |
| O-6 | Skip the second `user_message` for prompts already announced as `queued` (G-5) | tray matching in `user_message` |

---

## Appendix B — Open questions for the product owner

1. **Closing the orchestrator view.** Today closing the tab stops the orchestrator for every device (`POST …/close`). Keep that (with a confirm), or make close = detach and add an explicit "Stop orchestrator"?
2. **Background updates.** BG-1 shows a "Background update" divider before every orchestrator reply this device did not prompt (wake turns and prompts typed on other devices). Acceptable, or should those replies merge silently into the previous reply as the web does today?
3. **Default text model.** `default_model` in Settings is ignored by the backend (G-37). Should the client send `set_model(default_model)` after starting a new orchestrator, or should the backend honour it (O-level fix), or should the setting be removed?
4. **Renaming orchestrator conversations.** The web forbids it; the backend allows it. Allow on both clients?
5. **Talk-mode button gating.** Show the voice-message button when *any* model is audio-capable (today), or only when the current orchestrator model is (the server switches models silently otherwise)?
6. **Sessions started elsewhere.** On web, auto-open them as background tabs (parity, without focus), or only show them in the session list with a live badge (Android-style)?
7. **Voice after a network blip.** Should the owner re-arm voice automatically after reconnecting (Android today, V-8), or wait for the user?
