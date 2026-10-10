---
name: codex-cli
category: archie/harnesses
tags: [codex, codex-cli, openai, harness, app-server, json-rpc, rollout, persistent-process]
created: 2026-10-07
modified: 2026-10-08
summary: The OpenAI Codex harness — one persistent `codex app-server` per session (JSON-RPC over stdio), CODEX_HOME choice, rollout storage and adapter, catalog/options, landmines.
source: curated (implemented and verified against Codex CLI 0.161.0 on 2026-10-07)
references:
  - authentication.md
  - registry.md
  - claude-code.md
  - gemini-cli.md
  - qwen-code.md
  - ../architecture/agent-sessions.md
  - ../infrastructure/installation.md
  - ../infrastructure/ssh-remote-execution.md
  - ../architecture/memory-and-search.md
---

# Codex CLI harness

OpenAI's `codex` CLI (npm `@openai/codex`, a Rust binary; built and verified on **0.161.0**) as the
fourth session harness, `provider="codex"`. Unlike Qwen and Gemini it is **not** spawn-per-turn:
each Archie session owns one long-lived `codex app-server` process and talks newline-delimited
JSON-RPC 2.0 to it over stdio, the same shape as the Claude harness (lifecycle task owns the
process, a persistent receive loop feeds the active `send()`). Authentication is the CLI's own
ChatGPT login (`codex login`); no API credits are used.

## File map

| File | Contents |
|---|---|
| `backend/manager/codex/session.py` | `CodexSessionManager`, `CodexAbandoned(TurnAbandoned)`, `kill_codex_subprocess()`, the notification → event mapping, the SSH bootstrap |
| `backend/manager/codex/rpc.py` | `CodexRpc` — minimal JSON-RPC peer (request/notify, response futures, server→client requests answered from tasks) |
| `backend/manager/codex/items.py` | `ThreadItem` → tool call / tool result / TodoWrite mapping, shared by the live stream and the history adapter |
| `backend/manager/codex/adapter.py` | `CodexAdapter` (rollout JSONL, three generations), `HarnessSpec(name="codex")`, `_codex_jsonl_candidates()`, `_codex_discover_sessions()` |
| `backend/manager/codex/catalog.py` | `load_codex_catalog()` — models (live `model/list` → `models_cache.json` → built-in) and options |
| `backend/manager/codex/home.py` | `codex_home()` (dedicated vs shared), `sessions_roots()`, `home_for_thread()`, `codex_env()`, `codex_executable()` |
| `AGENTS.md` (repo root) | Symlink → `context/AGENTS.md` — the file Codex reads natively; committed like `CLAUDE.md` / `QWEN.md` / `GEMINI.md` |
| `install/{linux,apple}/install.sh`, `install/windows/install.ps1` | `--with-codex` (`-WithCodex`): seeds `~/.codex-archie/config.toml` (from `install/cli-runtime/codex-home/`; an existing file only gets `[features] memories = false` added or replaced, via `install/codex-home-config.py`), links its `sessions/`, links `.agents/skills` → `context/skills`, installs the CLI pinned to `CODEX_CLI_VERSION` (0.161.0, `install/harness-versions.env`; warns on a different version), login hint; root `AGENTS.md` link. Without Node/npm (`--no-node`, e.g. the Jetson) it installs the static `codex-<arch>-unknown-linux-musl` binary from GitHub release `rust-v<version>` into `/usr/local/bin` (or `~/.local/bin` + `CODEX_CLI_PATH`). `install-with-agent.*` can install Codex as the driver CLI; `install/doctor.sh` checks binary, auth, config (incl. `memories = false`) and links |
| `backend/tests/test_codex_session.py`, `test_codex_adapter.py`, `fixtures/codex/` | Tests: a scripted fake app-server (`fake_app_server.py`) and a trimmed real rollout |

## Binary and process

`codex_executable()` resolves, in order: `CODEX_CLI_PATH`; the **native** binary inside the global
npm package (`…/@openai/codex/node_modules/@openai/codex-linux-<arch>/vendor/<triple>/bin/codex`);
`codex` on `PATH`; the VS Code extension's bundled copy. The npm `bin/codex.js` is a Node shim
that forwards signals; exec'ing the native binary means the PID the pool tracks is the agent
itself (`/proc/<pid>/comm` = `codex`, the spec's `comm_prefix`).

The process is spawned as

```
codex app-server --listen stdio:// -c project_doc_max_bytes=131072 --disable plugins --disable apps
```

with `cwd = project_dir`, `start_new_session=True` (teardown closes stdin, then SIGTERM/SIGKILL the
process group, so shell children die too), a 64 MB line limit on stdout, and an env that is the
backend's minus `CLAUDECODE`, `OPENAI_API_KEY`, `OPENAI_BASE_URL` plus `CODEX_HOME`.

## Protocol (app-server JSON-RPC)

Connect (`_connect()`):

1. `initialize {clientInfo: {name: "archie", …}}` → `initialized` notification. The client name
   becomes the rollout's `originator`.
2. `model/list {includeHidden: true}` — per-model reasoning efforts and the account's default model.
3. `thread/start` | `thread/resume {threadId, excludeTurns: true}` | `thread/fork {threadId}` with
   `{cwd, model, sandbox: "danger-full-access", approvalPolicy: "never", config?}`. The returned
   `thread.id` (UUIDv7) is the provider session id.

Each `send()` is one `turn/start {threadId, input: [{type: "text", text, text_elements: []}],
summary, effort?}`. `interrupt()` is `turn/interrupt {threadId, turnId}`; the process stays up. If a
previous turn is still running when `send()` starts (its caller stopped listening), it is
interrupted first so its `turn/completed` cannot end the new turn.

| Notification | Normalized |
|---|---|
| `item/agentMessage/delta` (and `item/plan/delta`) | `TextDelta` |
| `item/completed` `agentMessage` / `plan` | `TextComplete` |
| `item/reasoning/summaryTextDelta` (`summaryPartAdded` > 0 adds `"\n\n"`) | `ThinkingDelta` |
| `item/reasoning/textDelta` (only when no summary streamed) | `ThinkingDelta` |
| `item/completed` `reasoning` | `ThinkingComplete` (summary parts joined) |
| `item/started` tool-like item | `ToolUse` |
| `item/completed` tool-like item | `ToolUse` (if not started) + `ToolResult` |
| `turn/plan/updated` | `ToolUse("TodoWrite", {todos})` + `ToolResult` |
| `thread/tokenUsage/updated` | stashed → `TurnComplete.usage` (`last`; `cachedInputTokens` → `cache_read_input_tokens`, `input_tokens` excludes cached) |
| `error` (`willRetry: false`) | stashed; shown on a failed turn |
| `thread/compacted` / `contextCompaction` item | `CompactComplete("auto")` (once per turn) |
| `turn/completed` | open texts → `TextComplete`, open tools → error `ToolResult`, then `TurnComplete(cost=None, num_turns=1, session_id=thread, is_error=status=="failed", result=last answer / "interrupted" / error)`; a failed turn also emits `Codex error: …` as text |

Tool items (`items.py`), live camelCase and rollout PascalCase/snake_case alike:

| Item | Tool card |
|---|---|
| `commandExecution` | `Bash {command}` — `/bin/bash -lc '<cmd>'` unwrapped; result `aggregatedOutput`, error when `exitCode ≠ 0` or status failed/declined |
| `fileChange`, one file | add → `Write {file_path, content}`; update → `Edit {file_path, old_string, new_string}` (built from the unified diff); delete → `Delete {file_path}` |
| `fileChange`, several files | `apply_patch {changes: [{path, kind, diff}]}` |
| `mcpToolCall` | `mcp__<server>__<tool>` with the arguments |
| `webSearch` | `WebSearch {query}` |
| `dynamicToolCall`, `collabAgentToolCall` (`Task`), `imageView` (`Read`), `imageGeneration` | as named |

Server → client requests: command/file approvals are accepted (`accept` / legacy `approved`),
permission requests granted for the turn, `requestUserInput` answered empty, MCP elicitations
declined, anything else gets JSON-RPC `-32601` — each one logged. With approval policy `never`
they should not occur. **There is no `PermissionRequest` gating for Codex** (v1).

The stall watchdog matches the other harnesses (first `SessionStalled` after 120 s, then every
60 s; `CodexAbandoned` after 240 s without a single event); notifications that produce no event
(command output deltas, MCP progress) still count as activity. When the process exits without a
`stop()`, the receive loop records `SUBPROCESS_CRASHED` with the exit code and the last stderr
lines, unblocks the active `send()` with an error `TurnComplete`, and the pool's dead-session
reaper broadcasts `session_terminated`; later `send()`s raise `SessionDeadError`. The manager
keeps the same `(stream_id, seq)` replay ring as Claude for WebSocket resume.

## CODEX_HOME, login and storage

| Home | When | Rollouts |
|---|---|---|
| `~/.codex-archie` (dedicated) | it contains `auth.json` | `sessions/` → symlink to `context/codex/sessions` (installer), synced by context-sync |
| `~/.codex` (shared; `$CODEX_HOME` if exported) | fallback | stay in `~/.codex/sessions` |
| `ARCHIE_CODEX_HOME` | env override | wherever that home keeps them |

Create the dedicated login with `CODEX_HOME=~/.codex-archie codex login --device-auth` (each
machine needs its own) — or from Settings → Accounts, which runs the same device-code login (and
the browser login, API key, `auth.json` paste, sign out) for that home ([authentication.md](authentication.md)).

**Skills.** Codex 0.161 lists skills from `<cwd>/.agents/skills` up to the repo root,
`<repo>/.codex/skills`, `$CODEX_HOME/skills` (its own `.system/` skills live there — so do not link
that one into `context/`) and `~/.agents/skills` (checked with `codex debug prompt-input` in a
scratch repo; symlinked directories are followed). The installers link `<repo>/.agents/skills` →
`../context/skills`, which gives Codex the same skills as the other harnesses. The catalog shows a warning with that command while Archie runs on the
shared fallback, and another when no login exists at all.

Rollout path: `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<local time>-<thread id>.jsonl`; resume
appends to the same file. The nested date directories keep rollouts out of `SessionStore`'s flat
scans.

- `_codex_jsonl_candidates(id)` globs `*/*/*/rollout-*-<id>.jsonl` under every root in
  `sessions_roots()`: `context/codex/sessions`, the active home, the dedicated home, the shared home.
- `_codex_discover_sessions()` lists every rollout under `context/codex/sessions` and the dedicated
  home, but in the **shared** home only those whose `session_meta.payload.originator` is `archie`
  (VS Code and the TUI write there too). The originator is cached per path.
- The homes are per machine, so they are only searched when the store's project dir is this
  install's own (`PROJECT_ROOT`); any other project dir (tests, a second checkout) sees only its own
  `context/codex/sessions`.
- `thread/resume` only finds rollouts under the running `CODEX_HOME`, so `home_for_thread()` runs a
  resumed session with the home that holds its rollout (a session started on the shared fallback
  keeps resuming there after a dedicated login is created).
- The home is chosen per session start (`codex_home()` checks for `auth.json` every time), so a new
  login takes effect without a backend restart. Since 2026-10-08 both machines have the dedicated
  login: a new session's rollout lands in `context/codex/sessions/YYYY/MM/DD/` and syncs.
- History search: `history_index.session_sources()` adds the same rollouts through
  `_codex_discover_sessions()`, and the `HistoryIndexer` change hash includes them.

**Memory.** Codex has no counterpart to Claude's auto-memory that could point at the wiki: its
`memories` feature (off by default; `CODEX_HOME/memories` + `memories_1.sqlite`) keeps its own
store. Archie keeps it off — the installers pin `[features] memories = false` in
`~/.codex-archie/config.toml` (the backend passes no flag for it) — and, for sessions in the repo, passes the shared memory block
(`backend/manager/memory_context.py`: the live `context/memory/MEMORY.md` plus the rule to write
memory with file tools per `AGENTS.md`) as `developerInstructions` on `thread/start` only. Codex
records it once as a developer message at the head of the rollout (hidden by the adapter and the
history index); `thread/resume` and `thread/fork` reuse it from the history. Verified live
2026-10-08: quoted `MEMORY.md` without tools on a new thread and again after a resume, and saved a
test fact by extending an existing wiki note ([memory and search](../architecture/memory-and-search.md#every-harness-reads-and-writes-the-same-memory)).

## Rollout format (adapter)

Every line is `{timestamp, type, payload}` (current CLIs add `ordinal`); line 0 is `session_meta`
(`id`, `timestamp`, `cwd`, `originator`, `cli_version`, an ~18 KB `base_instructions`).
`CodexAdapter` reads three generations:

| Generation | Visible content from |
|---|---|
| **items** (≥ 0.15x) | `event_msg` `item_completed` items: `UserMessage`, `AgentMessage`, `Reasoning` (`summary_text`), `CommandExecution`, `FileChange` (`changes: {path: {type, content \| unified_diff}}`), … |
| **events** (0.4x–0.6x) | `event_msg` `user_message` / `agent_message` / `agent_reasoning`; tools from `response_item` `function_call` / `function_call_output` / `custom_tool_call` (`apply_patch`) |
| **responses** (2025-09) | bare Responses items after a `{id, timestamp, instructions}` header |

`response_item` `message` lines are the raw model input (the `developer` instructions, the
`<environment_context>` user message) and are never shown; injected user texts are filtered by
prefix. Consecutive assistant blocks merge into one message and tool results become a synthetic
user message (the Gemini split), so clients pair them by `tool_use_id`. `visible_line_indices()`
returns one index per visible REST message (its last contributing line), consistent with
`read_messages()` for `truncate_session`. Detection: line 0 is `session_meta` with an id (or the
2025-09 bare header) — no other harness writes that. Title = first real user message.

## Configuration and catalog

Nothing is written to `config.toml`. Fixed overrides are on argv (above); the model is always passed
explicitly — the chosen one, else the default from `model/list` — because a stale `model` in the
user's `config.toml` would fail every turn. A configured model starting with `claude` is ignored
(the global `.manager.json` model reaches every harness when no harness model is set).

`load_codex_catalog()` (≤ 5 s, never raises): live `model/list` from a short-lived app-server with
Archie's `CODEX_HOME` (rows carry `efforts` / `default_effort`; `context_window` comes from
`models_cache.json`), else `models_cache.json`, else a built-in 2026-10-07 list — each fallback adds
a warning.

| Option | Maps to | Values |
|---|---|---|
| `effort` | `turn/start.effort` | `low` `medium` `high` `xhigh` `max` (`ultra` on some models); restricted per model — an unsupported level is dropped with a log line |
| `reasoning_summary` | `turn/start.summary` | `auto` `concise` `detailed` `none`; **unset sends `concise`** (models default to `none`, which hides all thinking) |
| `verbosity` | thread `config.model_verbosity` | `low` `medium` `high` |
| `web_search` | thread `config.web_search` | `disabled` `cached` `live` |

Thread-level options apply at session start (restart the session to change them); effort and
summary apply per turn.

## SSH working directories

Supported through the generic helpers: `build_remote_argv()` runs `/bin/sh -c <bootstrap> <remote
codex> app-server …` on the host (`ControlPath=/tmp/codex-ssh-<host>-%r`). The bootstrap uses
`~/.codex-archie` on the remote when it holds a login and puts the CLI's directory on `PATH` (the npm
shim needs `node`). The remote writes rollouts into its own home; they appear locally only if that
host's dedicated `sessions/` link into a synced `context/`. The orphan reaper sees the local `ssh`
PID, not `codex`.

## HarnessSpec values

`name="codex"`, `label="Codex"`, `comm_prefix="codex"`, `ssh_control_path_prefix="codex"`,
`requirements_file=None`, `npm_package="@openai/codex"`, `cli_binary="codex"`, `env_keys=()`,
`session_discoverer` and `catalog_loader` set.

## Landmines

1. **Never copy `auth.json`** between homes or machines. ChatGPT refresh tokens rotate; two homes
   holding one token family break each other (`refresh_token_reused`) — VS Code or Archie stops
   working. A second home or machine needs its own `codex login`.
2. **`AGENTS.md` size.** Codex reads `AGENTS.md` from the git root down to the cwd, capped by
   `project_doc_max_bytes` (default 32 KiB). `context/AGENTS.md` is ~51 KB, so without the
   `-c project_doc_max_bytes=131072` override it is silently truncated. It also costs ~20 K input
   tokens per turn in the repo cwd — noticeable on a free plan's monthly quota.
3. **`OPENAI_API_KEY` in the backend env.** Codex would use it and bill API credits; `codex_env()`
   strips it. Set `CODEX_API_KEY` to opt into API-key auth deliberately.
4. **Stale `model` in `config.toml`** (`gpt-5.3-codex` on the laptop) is not in the catalog any
   more; always passing a model avoids it.
5. **Reasoning summaries default to `none`** — without `summary` on `turn/start` the UI shows no
   thinking.
6. **Large JSON-RPC lines.** asyncio's default 64 KB line limit breaks on big command output or
   `thread/resume`; the reader uses 64 MB and resume passes `excludeTurns: true`.
7. **Plugins/apps** enabled in the user's config inject ~10 KB of instructions per turn;
   `--disable plugins --disable apps` keeps them out of Archie sessions.
8. **Duplicate / fork from the history menu** copy the rollout to `<new uuid>.jsonl`, which Codex
   cannot resume (wrong name, old id inside). Use a resumed session instead; the harness's own
   `fork` flag maps to `thread/fork`. Truncation edits the rollout in place and Codex rebuilds the
   thread from what is left.
9. **Interrupted turns** leave no agent message in the rollout (`turn_aborted` only), so a reopened
   conversation shows the prompt without the partial answer.
10. A new `thread/start` reads `MEMORY.md` once; a long-lived thread keeps the index it started
    with (like a Claude session). Start a new session to see index edits.

## History

- 2026-05-15 — deferred during the harness generalization (recon notes in [registry.md](registry.md)).
- 2026-10-07 — added on branch `harness-upgrade`: app-server session manager, rollout adapter
  (three generations), catalog/options, installer `--with-codex`, root `AGENTS.md` symlink.
  Verified live on CLI 0.161.0 with a ChatGPT free-plan login: text, reasoning summary, `ls`,
  file write, resume in a new process, `turn/interrupt`, history through `/api/sessions/<id>`.
- 2026-10-08 — dedicated `~/.codex-archie` logins on both machines; memory block as
  `developerInstructions`; Codex rollouts in the history indexer's change hash.
