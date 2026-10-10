---
name: agent-sessions
category: archie/architecture
tags: [agent-sessions, session-pool, session-manager, claude-agent-sdk, local-id, lifecycle, permissions, stall-watchdog, session-config, resume]
created: 2026-02-23
modified: 2026-10-09
summary: How agent (chat) sessions run — SessionPool, session managers, dual IDs, lifecycle hardening, permissions, stall watchdog, per-session config.
source: curated (consolidated from memory notes assistant/architecture/project-overview.md, assistant/architecture/permissions_branch_architecture.md, assistant/architecture/orchestrator-vision.md, auto-memory project_session_config.md, project_sdk_upgrade_path_2026_06_18.md, feedback_diagnose_via_direct_ws_probe.md; verified against code 2026-10-06)
references:
  - system-overview.md
  - backend.md
  - orchestrator.md
  - ../harnesses/registry.md
  - ../harnesses/claude-code.md
  - ../infrastructure/ssh-remote-execution.md
  - ../operations/debugging.md
  - ../operations/troubleshooting.md
---

# Agent sessions

An **agent session** is one live conversation with a coding-agent CLI (Claude Code by default; Qwen
Code and Gemini CLI are pluggable harnesses). Each chat tab on any client is a view onto one agent
session. Sessions live in the backend's `SessionPool`, are driven by a `BaseSessionManager`
subclass, and persist as the harness's own JSONL file under `context/`.

This is the first of Archie's two agent systems; the orchestrator (a hand-written agent loop on
the model APIs) is the second — see [orchestrator.md](orchestrator.md).

## File map

| Path | Role |
|---|---|
| `backend/api/pool.py` | `SessionPool` — sessions keyed by `local_id`, subscribers, locks, session-owned turns, prompt queue, replay, reapers, orchestrator slot |
| `backend/api/routes/chat.py` | `WS /api/sessions/chat` handler, `_handle_start`, chat-as-deny, `_resolve_session_provider` |
| `backend/api/session_factory.py` | `build_session_config()` — working dir / SSH / harness / model / MCPs / chrome flag |
| `backend/api/routes/session_config.py` | Per-session config load/save |
| `backend/manager/base_session.py` | `BaseSessionManager` (lifecycle task, permission futures, IDs, status), `TurnAbandoned`, `SessionDeadError` |
| `backend/manager/claude/session.py` | `ClaudeSessionManager` (alias `SessionManager`) — wraps `claude_agent_sdk.ClaudeSDKClient` |
| `backend/manager/{qwen,gemini,codex}/session.py` | The other harnesses ([registry](../harnesses/registry.md)) |
| `backend/manager/types.py` | Typed events: `TextDelta`, `TextComplete`, `ThinkingDelta/Complete`, `ToolUse`, `ToolResult`, `TurnComplete`, `CompactComplete`, `PermissionRequest`, `PermissionResolved`, `SessionStalled`, `SessionTerminated`; `SessionStatus`, `TerminationReason` |
| `backend/manager/protocol.py` | `ProviderAdapter` — reads a harness's native JSONL into normalized messages; `detect_provider()` |
| `backend/manager/registry.py` | `HarnessSpec` / `HarnessRegistry` |
| `backend/manager/store.py` | `SessionStore` — list/read/rename/delete/duplicate/truncate/fork sessions on disk |
| `backend/manager/_proc.py` | PID liveness, `/proc/<pid>/comm` checks, SIGTERM→SIGKILL |
| `backend/manager/_ssh.py` | SSH wrapper script, reachability probe ([ssh-remote-execution](../infrastructure/ssh-remote-execution.md)) |
| `backend/manager/loop_watchdog.py` | Event-loop liveness watchdog thread |

## Dual IDs: `local_id` and `sdk_session_id`

| ID | Minted by | Used for |
|---|---|---|
| `local_id` | The client (UUID v4) when it opens a tab, or the pool (`uuid4`) for orchestrator-opened sessions | Pool key, WebSocket `start`, `POST /api/sessions/{local_id}/close`, orchestrator tools, watcher events. Never changes |
| `sdk_session_id` | The harness CLI (Claude: from `get_server_info()` at connect, or the first `ResultMessage`) | JSONL filename stem (`context/<id>.jsonl`), every history REST call, titles, per-session config, resume |

Resuming from history = a new `local_id` plus `resume_sdk_id=<old sdk_session_id>`. The REST
history endpoints do **not** accept a `local_id`. `pool.find_by_sdk_id()` maps back. Before this
split a tab's ID changed three times (`new-N` → placeholder → SDK id); keying everything on the
stable `local_id` removed the re-keying.

## SessionPool (`backend/api/pool.py`)

State per agent session, all keyed by `local_id`: `_sessions` (the manager), `_subscribers` (set of
WebSockets), `_locks` (one `asyncio.Lock` serializing `send()`), `_turn_tasks` (the in-flight turn),
`_pending_prompts` + `_pending_locks` (queued messages). Plus `_watchers` (sockets that get
`agent_session_opened` / `agent_session_closed` and `agent_turn_started` / `agent_turn_finished`), the single orchestrator slot (`_orchestrator`,
`_orchestrator_id`, `_orchestrator_subs`), and PID tracking for the orphan reaper.

- **`create()`** — dedupes on `resume_sdk_id` (returns the existing `local_id` if that session is
  live and healthy), serializes creates per SSH host (`_host_create_locks`), builds the manager via
  the harness registry, `await sm.start()`. If resume fails with "No conversation found" it backs
  off 0.5–1 s and starts fresh once. Announces `agent_session_opened` to watchers — this is how
  agent tabs auto-appear on every client when the orchestrator opens a session.
- **`send()`** — under the per-session lock: broadcast `user_message` (except to the sender) and
  `status: processing`, iterate `sm.send()`, broadcast each serialized event with `seq`/`stream_id`
  attached, and mirror `permission_request` / `permission_resolved` to the orchestrator as
  `nested_session_event`. It is also the single emission point of the turn watcher events
  (spec 12 TURN-1) behind the "agent session finished" device notifications: `agent_turn_started`
  first, then exactly one `agent_turn_finished{status: ok|error|interrupted, title, preview, error}`
  (fire-and-forget task; the title comes from `SessionStore.session_title` in a thread).
  `interrupt()` marks the turn so the SDK's closing `TurnComplete` reads as `interrupted`; an
  abandoned turn is announced by whoever gives up (`_drive_turn`, the runner), and the runner's
  timeout adds an `error`.
- **Session-owned turns** — the chat WS never drives a turn itself. `send_or_queue()` spawns
  `_drive_turn()` as a pool-owned task if the session is idle, or queues the prompt (broadcast as
  `user_message` with `queued: true`) behind the running turn; `_drive_turn` drains the queue after
  each turn. A page reload only unsubscribes: the turn keeps running and the next subscriber picks
  up the stream. `start_turn()` is the destructive variant (cancel + replace) used for slash
  commands. `cancel_turn()` sends the SDK interrupt first, then cancels the task, and drops queued
  prompts.
- **`TurnAbandoned` retry** — if a turn produced zero SDK messages for 240 s (`_TURN_ABANDON_S`),
  `_drive_turn` broadcasts `status: retrying`, interrupts, waits 1 s and retries once; a second
  failure broadcasts `error: upstream_wedged`. Seen when the TCP path to the API silently wedged.
- **Resume protocol** — `stream_id = "<local_id>:<epoch_ms>"` is minted on each SDK (re)connect;
  every event gets a monotonic `seq` and goes into a 500-entry replay ring
  (`_REPLAY_BUFFER_SIZE`). A reconnecting client sends `resume_from: {stream_id, seq}` in `start`;
  `replay_for_subscriber()` replays the gap or answers `replay_overflow` (client refetches over
  REST). Claude only; Qwen/Gemini sessions are non-resumable.
- **`close()`** — cancel the turn task, broadcast `session_terminated` (typed reason, when given)
  then `session_stopped`, notify watchers, hand the PID to the reaper's grace map, and
  `asyncio.wait_for(sm.stop(), 10 s)`.
- **Reapers** — the orphan reaper (every 30 s, 30 s grace) SIGKILLs tracked PIDs whose session left
  the pool, after verifying `/proc/<pid>/comm` still matches the harness's prefix (PIDs get
  recycled). The dead-session reaper (every 5 s) closes sessions whose receive loop has exited
  (`_receive_loop_done`), broadcasting `subprocess_crashed` / `subprocess_lost`.
- **The open set** (spec 12 OPEN-1) — the pool is what every device shows as open, and it
  survives restarts. `_open_agents` (local_id → sdk id, in open order) holds every open agent
  session, live (in `_sessions`) or restored and not spawned yet; `_restored_orchestrator` the
  Archie conversation restored and not started yet. `backend/api/open_sessions.py` writes the set to
  `state/open_sessions.json` (machine-local, gitignored; `ARCHIE_STATE_DIR` overrides) on every
  create, close, orchestrator start/stop and turn end (a no-op when unchanged), and `lifespan()`
  restores it before serving. A restored session is listed in `pool/live` as `idle` and spawns on
  first use under its old `local_id`: a client's `reattach` start (chat route) or the orchestrator's
  runner (`ensure_live()`). `close_all()` freezes the set first, so a shutdown never empties it.
  `stop_orchestrator(replacing=True)` (voice drift, ENDING drop) rebuilds the conversation in place:
  no close event, and it stays open as if restored until `set_orchestrator()`.
- **Orchestrator slot** — at most one orchestrator. `stop_orchestrator()` parks the dying session in
  `_stopping_orchestrator` so a concurrent `voice_start` for the same `local_id` can
  `await_orchestrator_stop()` instead of reconnecting into the husk. Broadcasts use a snapshot of
  the subscriber set (concurrent (un)subscribe used to raise "Set changed size during iteration").

## The chat WebSocket (`backend/api/routes/chat.py`)

Client → server: `start` (`local_id`, `resume_sdk_id`, `fork`, `mcp_servers`, `resume_from`),
`send`, `command` (slash command, single-socket output), `interrupt`, `compact`,
`permission_response`, `stop` (unsubscribe only — the agent keeps working). A `start` with
`reattach: true` (every automatic start, spec 12 OPEN-2) only re-attaches: for a session that is
not open it answers `error{session_closed}` and creates nothing; for a restored one it resumes
its sdk id under the same `local_id`. Otherwise `start` either
re-subscribes to a pool session with that `local_id` or builds a config with
`build_session_config()` and calls `pool.create()` with a 30 s timeout (`start_timeout` error
usually means the CLI is not authenticated). The reply is `session_started` (`session_id` =
`local_id`, `context_window`, `resume_state`, optional `replay_overflow`). The full frame contract
is in `docs/specs/12-client-protocol.md`.

## Session managers

`BaseSessionManager` (`backend/manager/base_session.py`) holds what every harness shares: the
lifecycle task, `_stop_requested`, status/cost/turns, `local_id` / `sdk_session_id`, the
`_event_inbox` of the active `send()`, and the permission futures. Subclasses implement
`_run_lifecycle()`, `send()`, `interrupt()`, `provider_name`, and optionally `compact()`,
`command()`, `subprocess_pid`.

`ClaudeSessionManager` (`backend/manager/claude/session.py`):

- `_build_options()` → `ClaudeAgentOptions`: `include_partial_messages`, `setting_sources=["project","local"]`,
  `can_use_tool`, `system_prompt={"type":"preset","preset":"claude_code","append":_PERMISSION_GATING_PROMPT}`,
  `permission_mode`, `model`, `resume`, `fork_session`, `mcp_servers`, `extra_args` (`--chrome`),
  `env` without `CLAUDECODE` (so a session can be launched from inside Claude Code), stderr logged.
  For SSH targets `cli_path` points at a generated wrapper script ([ssh-remote-execution](../infrastructure/ssh-remote-execution.md)).
- **Persistent receive loop** — `_receive_loop()` owns `client.receive_messages()` for the whole
  session lifetime. Each event gets a seq, goes into the replay ring, and into the active `send()`
  inbox if there is one. `send()` is a thin subscriber: register inbox, `client.query()`, read the
  inbox until `TurnComplete`. Because the loop never stops reading, an interrupted or abandoned
  `send()` no longer leaves stale messages in the SDK buffer, and `status` reflects what the
  subprocess is actually doing. When the loop ends it records a `TerminationReason`, sets
  `_receive_loop_done`, and later `send()` calls raise `SessionDeadError`.
- **String tool results** — some CLI versions deliver bundled-tool output on the `UserMessage` as a
  plain string (or an MCP content list) instead of a `ToolResultBlock`/dict. `_process_message`
  treats it as the output and takes the `tool_use_id` from `parent_tool_use_id`. Dropping it left
  the tool card spinning forever.

Qwen and Gemini managers follow the same base class; their specifics (per-turn subprocesses, JSONL
locations) are in [harnesses/registry.md](../harnesses/registry.md) and the
per-harness docs. `SessionStore` and the provider adapters make all three appear in one history list.

## Lifecycle hardening

| Mechanism | Where | Why |
|---|---|---|
| **Single lifecycle task** | `BaseSessionManager.start()` / `_lifecycle()`; Claude's `_run_lifecycle()` | `connect()` and `disconnect()` run in the same task, as the SDK's cancel scopes require. `stop()` sets `_stop_requested` and awaits the task |
| **Shielded stop** | `BaseSessionManager.stop()` awaits `asyncio.shield(task)` | `pool.close()` bounds `sm.stop()` at 10 s; without the shield the timeout's cancellation propagated into the lifecycle task and left a rescheduling `_read_messages` task at ~98% CPU |
| **Bounded disconnect + SIGKILL** | `_run_lifecycle()` finally: `client.disconnect()` capped at 8 s, then `kill_claude_subprocess(pid)` (SIGTERM, 0.5 s grace, SIGKILL) if still alive | The SDK's `transport.close()` awaits `process.wait()` with no timeout; a `claude` that ignores SIGTERM pinned a CPU and leaked (~2.5% CPU each on the Jetson) |
| **PID capture** | `_extract_subprocess_pid()` reads `client._transport._process.pid` | Private attribute; if a future SDK moves it, only the per-session kill is lost and the orphan reaper still covers it |
| **Orphan reaper** | `SessionPool._reaper_loop` | Last-line defense for any path that skipped the per-session kill |
| **Dead-session reaper** | `SessionPool._reap_dead_sessions_once` | UI learns within ~5 s that a crashed/SSH-dropped session is gone, instead of a frozen "streaming" status |
| **Turns survive disconnects** | `SessionPool.send_or_queue` / `_drive_turn` | A closed tab or reload no longer interrupts work; `stop` and socket close just unsubscribe |
| **SSH pre-probe** | `ClaudeSessionManager._pre_start_check()` — one ICMP ping, 2 s | Fails fast when the remote is asleep instead of a 30 s SSH timeout that spun the Jetson's fan |

## Permission gating

The bundled CLI calls the SDK `can_use_tool` callback before tools when `permission_mode` is
`"default"` (the `ManagerConfig` default — required for the callback to fire).

- `_DEFAULT_GATED_TOOLS = frozenset({"ExitPlanMode"})`. Every other tool auto-allows.
- For a gated tool, `_emit_permission_request()` injects a `PermissionRequest` (with a fresh
  `request_id`) into the stream and awaits a future; the answer is injected as `PermissionResolved`
  (`decision`, `responder`, `message`). No active `send()` stream → auto-allow (a popup nobody can
  see would deadlock). When `send()` ends, still-pending requests resolve as `deny` ("stream ended").
- **Conversational checkpoint**: `_PERMISSION_GATING_PROMPT` is appended to the Claude Code system
  prompt, telling the agent to announce what it intends in chat *before* calling a gated tool. The
  card is the backstop; prose is the primary channel.
- Ways to answer (first answer wins, via `BaseSessionManager.resolve_permission()`):
  1. `permission_response` frame on the chat WS (Approve / Reject);
  2. `POST /api/sessions/{local_id}/permission` (REST twin, for devices without that socket open);
  3. the orchestrator's `respond_to_agent_permission` tool (it sees requests as `nested_session_event`);
  4. **chat-as-deny** — a `send` while a permission is pending resolves every pending request on
     that session as `deny` with the typed text as the reason, then sends the text normally.
- `permission_request` / `permission_resolved` are broadcast, not written as their own JSONL lines;
  the plan text from `ExitPlanMode` appears as normal assistant text.

## Stall watchdog

While waiting on the inbox, `ClaudeSessionManager.send()` uses `asyncio.wait_for` with a deadline:
after **120 s** of silence (`_STALL_FIRST_NOTICE_S`) it yields `SessionStalled(elapsed_seconds,
last_tool_name, last_tool_use_id)` and repeats every **60 s** (`_STALL_REPEAT_INTERVAL_S`). It is
advisory — the stream is not aborted (a slow tool may be legitimate). The typical cause is
`WebFetch` waiting on an endpoint that never answers. The web client marks the named tool card as
waiting (hourglass) while the session still streams. Stall notices are synthetic: they carry no
`seq` and are not in the replay ring.

Distinct from a stall: **zero messages for 240 s** raises `SessionAbandoned` (a `TurnAbandoned`),
handled by the retry-once logic in the pool and in the orchestrator's runner.

## Loop watchdog

`start_loop_watchdog()` (`backend/manager/loop_watchdog.py`) runs a daemon thread that schedules a
no-op on the event loop every 5 s. It calls `os._exit(1)` (systemd restarts the service; sessions
resume from JSONL) if the callback does not run within 30 s (**liveness**), or if it runs ≥ 5 s late
for 6 probes in a row (**degraded latency** — the "alive but one core pinned" mode). It has to be a
thread: a coroutine on a starved loop cannot detect the starvation.

## Per-session config

Stored at `context/<sdk_session_id>.config.json` (keyed by the SDK id, next to the JSONL). Allowed
keys (`_ALLOWED_KEYS` in `backend/api/routes/session_config.py`):

| Key | Meaning (`null` = inherit from `assistant_config.json`) |
|---|---|
| `working_directory` | An entry id from the global `working_directory_history` (local path or SSH target) |
| `enabled_mcps` | MCP server names for this session (replaces, not extends, the global list) |
| `chrome_extension` | Pass `--chrome` to the CLI (Anthropic's Claude-in-Chrome, unrelated to `apps/browser-extension/`) |
| `provider` | Harness id, **pinned**: once a JSONL exists for a harness, resuming it with another would corrupt it |
| `harness_model` | Harness model id; `""` = the CLI's own default |

REST: `GET/PUT /api/sessions/{sdk_session_id}/config`. Resolution happens in
`build_session_config()`: per-session value → global value. For a resumed session without a pinned
provider, `_resolve_session_provider()` sniffs the JSONL with `detect_provider()` and persists the
result so later resumes are deterministic. Clients expose this as a per-session settings panel with
a "Save and Restart" action that re-sends `start` once the session is idle.

## Claude Agent SDK version

`backend/requirements-claude.txt` pins `claude-agent-sdk==0.2.164` exactly (bundled CLI 2.1.292,
since 2026-10-07). What changed with 0.2.x and the defaults that keep sessions behaving as before:
[Claude Code harness — SDK version](../harnesses/claude-code.md#sdk-version). The earlier floors
still explain why the version matters:

- 0.1.51 (upstream PR #746) replaced `anyio.TaskGroup` in `Query` with `asyncio.create_task`,
  fixing the cross-task `__aexit__` wedge (upstream issue #378) that pinned the event loop in
  anyio's `_deliver_cancellation` retry loop. Two monkey-patches that used to live in
  `manager/claude/session.py` were deleted with the upgrade.
- 0.1.40+ skips unknown message types. On 2026-08-25 a venv had drifted to 0.1.39; the CLI's new
  `rate_limit_event` raised `MessageParseError`, killed the receive loop, and sessions were reaped as
  `subprocess_crashed`. If sessions die with "Unknown message type", check the **installed** version:
  `.venv/bin/python -c "import claude_agent_sdk; print(claude_agent_sdk.__version__)"`.
- 0.2.164 (2026-10-07): the newer CLI blanks thinking text unless a display is requested, hides
  TodoWrite on 4.8 / 5.x models and stops waiting for deferred MCP servers on turn 1 —
  `_build_options()` sets `--thinking-display summarized`, `CLAUDE_CODE_ENABLE_TODO_TOOLS=1` and
  `CLAUDE_CODE_MCP_STARTUP_WAIT_MS=2000` to keep the old behaviour.
- Do not bump anyio as a "fix" for loop wedges — anyio was not the cause; the SDK was.

## Pitfalls

- **Use `local_id` for live operations, `sdk_session_id` for history.** Mixing them is the most
  common client bug; `session_id` means different things in different payloads (see the identifier
  table in `docs/projects/frontend-refactor/inventory/01-backend-api.md` §2).
- **`stop` and socket close never cancel a turn.** Only `interrupt` (or `cancel_turn`) does.
- **Backend session state is not fully exposed over REST.** For state bugs (stuck status, ghost
  voice), open a throwaway WebSocket client, send `start` with the suspect `local_id`, and read the
  real `session_started` payload before theorizing. `POST /api/sessions/{local_id}/close` is the
  cheap remedy (drops the pool entry, keeps the JSONL). See [debugging](../operations/debugging.md).
- **Closing a never-used new session deletes its JSONL** (`close_pool_session`: zero turns and not
  resumed); resumed sessions are never deleted there.
- **`DELETE /api/sessions/{id}` is a soft delete** to `context/trash/` and removes the session from
  the history index.

## History

- 2026-02: dual-ID system and the unified pool (agent sessions + one orchestrator).
- 2026-04/05 (`permissions` branch): `can_use_tool` gating with the conversational checkpoint,
  stall watchdog, lifecycle task ownership, SIGKILL escalation, orphan reaper, shielded stop.
- 2026-05-10 (`9958ff0`): turns became session-owned; this replaced the earlier rule of
  interrupting a turn when its last subscriber disconnected.
- Later: the persistent receive loop replaced the per-turn drain task; dead-session reaper and typed
  `session_terminated`; seq/stream resume protocol; `send_or_queue` prompt queue.
- 2026-06-18: SDK floor raised to 0.1.81.

Related: [system overview](system-overview.md), [backend](backend.md), [Claude Code harness](../harnesses/claude-code.md), [troubleshooting](../operations/troubleshooting.md).
