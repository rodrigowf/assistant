---
name: gemini-cli
category: archie/harnesses
tags: [gemini, gemini-cli, harness, jsonl, stream-json, skip-trust, glob-resolver, spawn-per-turn, api-key, workspace-settings, catalog, thinking]
created: 2026-05-15
modified: 2026-10-08
summary: The Gemini CLI harness (pinned 0.63.0) — spawn-per-turn gemini --prompt, API-key-only auth since the oauth-personal shutdown, env-templated workspace settings, catalog + options, JSONL log semantics, landmines.
source: curated (consolidated from memory notes assistant/providers/gemini_cli_adaptation.md, assistant/providers/provider_generalization.md; upgraded to CLI 0.63.0 and verified live 2026-10-07)
references:
  - authentication.md
  - registry.md
  - qwen-code.md
  - claude-code.md
  - ../architecture/agent-sessions.md
  - ../infrastructure/installation.md
  - ../infrastructure/ssh-remote-execution.md
  - ../voice/gemini-live.md
  - ../architecture/memory-and-search.md
---

# Gemini CLI harness

Google's `gemini` CLI (npm `@google/gemini-cli`, Node ≥ 20), **pinned to 0.63.0**
(`GEMINI_CLI_VERSION` in `install/harness-versions.env`, installed at step 7b), as a session harness. Like Qwen it is spawn-per-turn: each `send()` runs `gemini --prompt
<text>` with stream-json output and exits; turns are chained with `--resume`.

The Gemini CLI harness is unrelated to the Gemini Live **voice** provider
([gemini-live](../voice/gemini-live.md)), although both use the same `GEMINI_API_KEY`.

## Auth: API key only

Google stopped serving Gemini CLI to personal Google logins (`oauth-personal`, "Gemini Code Assist
for individuals", free tier and Google AI Pro/Ultra) on **2026-06-18** and points those users to its
successor, Antigravity CLI. On any CLI version such a login now fails before the first model call:

```
Error authenticating: IneligibleTierError: This client is no longer supported for Gemini Code Assist
for individuals. … reasonCode: 'UNSUPPORTED_CLIENT'
```

The CLI keeps shipping for **API-key (AI Studio), Vertex and enterprise** users. Archie therefore needs
`GEMINI_API_KEY` in `context/.env` (exported to the backend by `run.sh`; the spec's `env_keys`). The
catch: `~/.gemini/settings.json` usually still says `security.auth.selectedType: "oauth-personal"`,
and the configured type wins over the env key. Archie overrides it per run without touching that
file:

- the repo's workspace settings select `${ARCHIE_GEMINI_AUTH_TYPE:-gemini-api-key}`;
- for other working directories the manager sets `GEMINI_CLI_AUTH_OVERRIDE=gemini-api-key` (an
  internal CLI variable that replaces the user-level choice).

Set `ARCHIE_GEMINI_AUTH_TYPE` (e.g. `vertex-ai`, or `oauth-personal` for Code Assist
Standard/Enterprise accounts) to use something else. Without a key (and without that variable) a
local turn fails immediately with an explanation instead of an empty turn. SSH remotes bring their
own `GEMINI_API_KEY`.

Settings → Accounts sets the key and the auth type, pastes `oauth_creds.json`, and drives the TUI's
Google sign-in on a headless server ([authentication.md](authentication.md)).

A possible future harness: Google's successor, **Antigravity CLI** (`agy`).

## File map

| File | Contents |
|---|---|
| `backend/manager/gemini/session.py` | `GeminiSessionManager`, `GeminiAbandoned(TurnAbandoned)`, `_ThoughtTail` (thinking from the JSONL), process-tree reaping, `_gemini_executable()` (`GEMINI_CLI_PATH` or `gemini`) |
| `backend/manager/gemini/adapter.py` | `GeminiAdapter` (log replay: upserts, snapshots, rewinds), `gemini_session_is_resumable()`, `HarnessSpec(name="gemini")`, `_gemini_jsonl_candidates()` (glob), `_gemini_discover_sessions()`, `_read_gemini_session_id()` |
| `backend/manager/gemini/catalog.py` | `load_gemini_catalog()` (the spec's `catalog_loader`), model families, per-model clamping of thinking values |
| `backend/manager/gemini/workspace_settings.py` | Archie's keys for `<repo>/.gemini/settings.json`, `ensure_workspace_settings()`; stdlib-only, also run as a script by the installer |
| `install/cli-runtime/gemini/settings.json` | The seed — byte-identical to `workspace_settings.render_seed()` (a test keeps them equal) |
| `GEMINI.md` (repo root) | Symlink → `context/AGENTS.md`, committed in git |
| `backend/tests/test_gemini_{adapter,session,session_ssh,catalog,workspace_settings}.py` | Tests (subprocess mocked; two use real process groups) |

## CLI surface

| Aspect | Value |
|---|---|
| Headless prompt | `-p / --prompt <text>` — on argv, not stdin |
| Streaming | `--output-format stream-json` |
| Session id | `--session-id <uuid>` — starts a NEW session with our id; exits with "Session ID … already exists. Use --resume" if the id is on disk |
| Resume | `--resume <uuid>`; mutually exclusive with `--session-id` and `--session-file` |
| Approval | `--approval-mode yolo` (option `approval_mode`) |
| Trust gate | headless mode refuses untrusted dirs: `--skip-trust` plus `GEMINI_CLI_TRUST_WORKSPACE=true`; trust is also what makes the CLI load the workspace settings |
| Model | `--model <id or alias>` (precedence: flag > `GEMINI_MODEL` > `model.name` > `auto`) |
| State | `~/.gemini/` — `projects.json` (cwd → label), `tmp/<label>/`, `settings.json`, `trustedFolders.json` |

`_build_argv()` produces:

```
gemini --prompt <text> --skip-trust --output-format stream-json --approval-mode <yolo|option>
       (--resume <id> | --session-id <id>) [--model <id>]
```

The env is the backend's env minus `CLAUDECODE`, plus `GEMINI_CLI_TRUST_WORKSPACE=true`,
`GEMINI_CLI_NO_RELAUNCH=true`, `GEMINI_CLI_AUTH_OVERRIDE=gemini-api-key` (when a key is set) and the
per-turn option variables below (inherited copies are stripped). The subprocess starts in its own
process group (`start_new_session=True`); stdin is closed right after spawn.

`--resume` vs `--session-id` is decided by the JSONL on disk, by the CLI's own rule: the file must
hold *resumable* content (a real user message, or a gemini message with text / tool calls /
thoughts). Both CLI versions write the header — 0.63 also a `$set` snapshot — **before**
authenticating, so a turn that fails early leaves a stub that `--resume` rejects while
`--session-id` refuses its id. `_session_written()` deletes such a stub (only one whose header
carries this session's id) and pins the id again. SSH sessions can't be checked locally: there a
resume id or a completed turn means `--resume`. `_run_lifecycle()` pings SSH targets and pre-warms
(remote `which gemini` or local `gemini --version`), like Qwen. There is no permission gating, no
compaction and no cost reporting.

### Preflight (local turns)

Before spawning, `send()` checks:

1. auth — no `GEMINI_API_KEY` and no `ARCHIE_GEMINI_AUTH_TYPE` → an error `TurnComplete` with the
   explanation above, nothing spawned;
2. workspace settings — when the cwd is the repo root, `ensure_workspace_settings()` merges Archie's
   keys into `.gemini/settings.json` (writes only on change). If the file exists but is not a JSON
   object (comments, hand edits) it is left alone and the turn is refused: without
   `sessionRetention.enabled=false` the CLI would delete old sessions (landmine 10).

## Workspace settings (the per-run mechanism)

The CLI has no `--settings` flag, and since 0.60 a per-run `GEMINI_CLI_SYSTEM_SETTINGS_PATH` file is
skipped unless it is root-owned. What works: the CLI expands `$VAR` / `${VAR}` / `${VAR:-default}` in
every string of every settings file, so a **static** workspace file whose values are env templates
lets each run choose. Layering (later wins): defaults < system-defaults < user
(`~/.gemini/settings.json`) < workspace (`<cwd>/.gemini/settings.json`, trusted only) < system.

Archie-owned keys (`backend/manager/gemini/workspace_settings.py`):

| Key | Value | Why |
|---|---|---|
| `context.fileFiltering` | `respectGitIgnore: false`, `respectGeminiIgnore: true` | The agent must read the gitignored `context/`. Older seeds put `fileFiltering` at the top level, which the CLI never read; the merge moves it |
| `general.sessionRetention.enabled` | `false` | Retention runs on every start, headless too, over `context/chats/` (through the symlink) |
| `security.auth.selectedType` | `${ARCHIE_GEMINI_AUTH_TYPE:-gemini-api-key}` | Beats the user file's `oauth-personal` |
| `modelConfigs.customOverrides` (entries mentioning `ARCHIE_GEMINI_`) | three overrides, below | Thinking options |

The overrides:

```json
{ "match": { "model": "chat-base-3" },
  "modelConfig": { "generateContentConfig": { "thinkingConfig": { "thinkingLevel": "${ARCHIE_GEMINI_THINKING_LEVEL:-HIGH}" } } } },
{ "match": { "model": "${ARCHIE_GEMINI_LEVEL_MODEL:-archie-off}" },  … same thinkingLevel … },
{ "match": { "model": "${ARCHIE_GEMINI_BUDGET_MODEL:-archie-off}" },
  "modelConfig": { "generateContentConfig": { "thinkingConfig": { "thinkingBudget": "${ARCHIE_GEMINI_THINKING_BUDGET:-8192}" } } } }
```

Every Gemini 3 model in the CLI's alias table extends `chat-base-3`, whose own level is `HIGH` — so
with the variable unset nothing changes. A Gemini 3 id outside the table (e.g. `gemini-3.7-flash`)
falls back to `chat-base` with no level; the second override, gated by a model name that only exists
when the manager sets `ARCHIE_GEMINI_LEVEL_MODEL`, covers it (the requested name is part of the
override hierarchy). The budget override is gated the same way; its value reaches the API as a JSON
string, which the Gemini API accepts for int32 fields (verified: a non-numeric value is rejected with
`Invalid value at 'generation_config.thinking_config.thinking_budget' (TYPE_INT32)`). The merge keeps
every other key and every user override, and replaces older Archie overrides.

The file is gitignored and per machine. The installer seeds it (never overwriting) and then runs
`python3 backend/manager/gemini/workspace_settings.py <repo>` to merge the keys into an existing
one; the session manager does the same before every local turn in the repo root. Other working
directories get no workspace file from Archie: there auth still works (`GEMINI_CLI_AUTH_OVERRIDE`)
but the thinking options don't apply and the CLI's retention sweeps that project's own temp dir.

## Configuration (catalog + options)

`HarnessSpec.catalog_loader` is `load_gemini_catalog()` (`backend/manager/gemini/catalog.py`), served
by `GET /api/config/harness/gemini/catalog` ([registry](registry.md) has the shared contract).

**Models.** Built-in list mirrored from the CLI 0.63 bundle: the aliases `auto` (the CLI's router
picks Gemini 3.1 Pro or a Flash model per turn; `default_model`), `pro` (→ `gemini-3.1-pro-preview`
with an API key), `flash` (→ `gemini-3-flash-preview`), `flash-lite` (→ `gemini-3.1-flash-lite`),
then `gemini-3.1-pro-preview`, `gemini-3-flash-preview`, `gemini-3.8-flash`, `gemini-3.5-flash`,
`gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`, `gemini-2.5-pro`, `gemini-2.5-flash`,
`gemini-2.5-flash-lite`, `gemma-4-31b-it`, `gemma-4-26b-a4b-it`. `gemini-3-pro-preview` (still the
CLI's preview default) is left out: AI Studio no longer serves it. With `GEMINI_API_KEY` the loader
merges the free `GET generativelanguage.googleapis.com/v1beta/models` (key in the `x-goog-api-key`
header, 5 s timeout): only `generateContent` models named `gemini-*`/`gemma-*`, without TTS, image,
"nano-banana", transcribe, embedding, robotics, computer-use, omni, live/native-audio, customtools,
Lyria, Veo, AQA, Antigravity and deep-research variants. Matching built-in rows become
`source: "live"` (label and context window from the API); live-only rows (e.g. `gemini-3.6-flash`,
`gemini-3.7-flash`, `gemini-*-latest`) are appended; built-in ids missing from the list produce a
warning. No key, an HTTP error or a timeout → the built-in list plus a `warnings` entry.
`allow_custom_model` is true (any id is passed through to `--model`). Note: models.list still lists
`gemini-2.5-pro` and `gemini-2.5-flash-lite`, but the API answers 404 "no longer available to new
users" for newer keys.

**Options** (`harness_options.gemini` globally, or per session):

| Key | Kind | Values | Becomes | Per-model rules |
|---|---|---|---|---|
| `thinking_level` | select | `minimal` `low` `medium` `high` (CLI default `high`) | `ARCHIE_GEMINI_THINKING_LEVEL=<UPPER>` (+ `ARCHIE_GEMINI_LEVEL_MODEL=<model>` for concrete Gemini 3 ids) | Gemini 3 / Gemma 4 ids and the aliases (`models`). Clamped to what the model accepts, ties towards more thinking: 3 Pro `low`/`high`; 3.1 Pro, 3.7/3.8 Flash and `auto`/`pro` `low`–`high`; other 3.x Flash / Flash-Lite all four |
| `thinking_budget` | number −1…32768 (CLI default 8192) | tokens; `-1` dynamic, `0` off | `ARCHIE_GEMINI_BUDGET_MODEL=<model>`, `ARCHIE_GEMINI_THINKING_BUDGET=<n>` | Gemini 2.5 only. Clamped: 2.5 Pro 128–32768 (0 → 128); 2.5 Flash 0–24576; 2.5 Flash-Lite 0 or 512–24576 |
| `approval_mode` | select | `yolo` (default) `auto_edit` `plan` `default` | `--approval-mode` | Headless runs cannot ask, so an "ask" decision is a deny: `auto_edit` = edits run, shell denied; `plan` = read-only planning (runs autonomously since 0.63); `default` = read-only |

The family checks (`model_family()`) are name based, so a custom Gemini 3 / 2.5 id outside the
catalog still gets the matching option. No options → the argv is the pre-catalog one (`--approval-mode
yolo`) and no `ARCHIE_GEMINI_*` variable is set. `includeThoughts` is already `true` for every chat
model; there is no thinking on/off toggle (2.5 Flash: budget `0`; Gemini 3: `minimal` is the floor).
Generation does not always think: Gemini 3 Flash Preview answered simple prompts with zero thought
tokens even at `high`, while 3.1 Pro and 2.5 Flash produced thoughts.

## Storage layout

The CLI hard-codes:

```
~/.gemini/tmp/<project-label>/chats/session-<YYYY-MM-DDTHH-MM>[-<n>]-<first 8 chars of uuid>.jsonl
```

`<project-label>` is what `~/.gemini/projects.json` maps the cwd to (first run: the cwd basename,
`assistant`); `-<n>` appears only for same-minute collisions (0.63). Subagent sessions go to
`chats/<parentId>/<id>.jsonl` (ignored by our non-recursive globs). The installer (`--with-gemini`,
step 3c) replaces the label directory with a symlink:

```
~/.gemini/tmp/assistant  ->  /home/rodrigo/assistant/context
```

so Gemini's `chats/session-*.jsonl` lands in `context/chats/` next to Qwen's `<uuid>.jsonl`; the
name patterns never overlap. A pre-existing real directory is backed up to
`context/gemini-backup-<timestamp>/` and its chats lifted first. The session manager passes
`project_dir` as cwd so the label stays stable. The installer also pins the label
(`install/gemini-project.py`): it registers `<repo> → <label>` in `projects.json` and makes the
ownership marker `.project_root` in the label dir (= `context/.project_root`) name the repo — the CLI
claims a new label for an unregistered repo or when a marker in `tmp/<label>/` or
`history/<label>/` names another path, and chats and the memory index below would then leave
`context/`. `install/doctor.sh` checks the registration, both markers and the memory index.

Because the file name carries only an 8-character id prefix:

- `_gemini_discover_sessions()` (the spec's `session_discoverer`) globs `context/chats/session-*.jsonl`
  and reads each header line for the real `sessionId`; header-less files are skipped.
- `_gemini_jsonl_candidates()` (the `jsonl_path_resolver`) globs `session-*-<id[:8]>.jsonl` in
  `context/chats/`, then falls back to every `~/.gemini/tmp/*/chats/` for hosts without the symlink.
  It may return `[]`; the registry contract test allows that.
- `SessionStore` runs discoverers before its own scans and skips `session-*` names in the
  `chats/` scan, so a Gemini file never appears with its file stem as a fake id.
- The history index uses the header `sessionId` too (`history_index.session_id_for()`, since
  2026-10-08; before, search results carried the file stem, which `resume_conversation` and the
  titles did not know).

**Memory comes with the same symlink.** 0.63 keeps a "private project memory" in
`~/.gemini/tmp/<label>/memory/`, whose `MEMORY.md` index it loads into every session
(`<user_project_memory>`) — through the symlink that is `context/memory/MEMORY.md`, the wiki root.
0.63 has no `save_memory` tool: its prompt tells the model to edit memory files directly, so
writes follow `AGENTS.md` (`GEMINI.md`) into `context/memory/`. Nothing in the session manager is
needed for this ([memory and search](../architecture/memory-and-search.md#every-harness-reads-and-writes-the-same-memory)).
Two parts of its built-in guidance differ from the wiki rules: "brief facts directly into
`MEMORY.md`" and a "global personal memory" tier at `~/.gemini/GEMINI.md` (outside `context/`, not
synced, not indexed) for cross-project preferences. Verified live 2026-10-08 (gemini-2.5-flash): it
quoted `MEMORY.md` without tools and saved a test fact as a wiki note with frontmatter plus an
`INDEX.md` line. If `~/.gemini/GEMINI.md` ever appears, move its facts into the wiki (the
installers and `install/doctor.sh` warn when it exists).

## JSONL format (on disk)

The file is an append-only **log**; the adapter replays it the way the CLI's own
`loadConversationRecord` does.

| Line | Shape | Replay rule |
|---|---|---|
| Header (line 1, re-written on resume) | `{"sessionId", "projectHash", "startTime", "lastUpdated", "kind": "main"}` (+ `directories`, `summary`) | start time / session id |
| User | `{"id", "timestamp", "type": "user", "content": [{"text": "…"}], "displayContent"?}` | upsert by `id`; `displayContent` (what was typed before `@file` expansion) is shown first |
| Assistant | `{"id", "timestamp", "type": "gemini", "content": "<string>" or [parts], "thoughts": [{subject, description, timestamp}], "tokens", "model", "toolCalls"?}` | upsert by `id` — the CLI re-appends a message each time it changes (thoughts → toolCalls → results), last write wins, first position kept |
| Tool answer (0.63) | `{"type": "user", "content": [{"functionResponse": {id, name, response}}]}` | folded into `tool_result` blocks — only for calls not already answered from `toolCalls`; never an empty user turn |
| Snapshot (0.45+) | `{"$set": {"messages": [...], "lastUpdated"}}` | **replaces** the whole list; written on chat init, every resume (= every turn after the first), compression and rollback |
| Rewind | `{"$rewindTo": "<id>"}` | drops that message and everything after it (all of it if the id is unknown) |
| Bookkeeping | `{"$set": {"lastUpdated": "…"}}` after every change | last-activity time |
| `type: "info" / "error" / "warning"` | CLI notices | not turns |

In snapshots the gemini `content` is a part list — `{text}`, `{text, thought: true}`,
`{functionCall}`; `thoughts` / `toolCalls` stay authoritative and the thought / functionCall parts are
used only when those are missing, so nothing shows twice. The first snapshot message is a user turn
whose text starts with `<session_context>` (OS, directory tree, the whole GEMINI.md): hidden, like
`<hook_context>`, and never the title.

`GeminiAdapter` normalizes each surviving record: user text → a user message; each thought → a
`thinking` block `"<subject>\n<description>"`; text → a `text` block; a record with `toolCalls`
becomes **two** messages — the assistant turn with one `tool_use` per call, then a synthetic user
message with the matching `tool_result` blocks — so clients pair them by `tool_use_id`. Visibility
(`is_visible_message`) counts user text and non-empty assistant turns; `visible_line_indices` maps a
visible turn to the line just before the next record's first write, so truncate/fork keep a prefix
that replays to exactly the earlier turns. Detection: a header with `sessionId` + `projectHash` +
`kind`, or any `type: "gemini"` line.

## stream-json (live output)

| `type` | Fields | Normalized |
|---|---|---|
| `init` | `session_id`, `model` (the *requested* model) | adopts `session_id` if none pinned |
| `message` role `user` | echo of the prompt | skipped |
| `message` role `assistant`, `delta: true` | text chunk | `TextDelta`; accumulated into `TextComplete` |
| `tool_use` | `tool_name`, `tool_id`, `parameters` | `ToolUse` |
| `tool_result` | `tool_id`, `status`, `output` (display text, also on errors since 0.63), `error.message` | `ToolResult` (`is_error` when `status == "error"`) |
| `error` | `severity` `warning` (loop detected, blocked tool) or `error` (quota, invalid stream), `message` | logged; an error's message becomes the turn's error text if the turn fails |
| `result` | `status`, `error?{type, message}`, `stats {input_tokens, output_tokens, total_tokens, cached, models{…}}` | `TurnComplete` with usage (`cached` → `cache_read_input_tokens`); `status: "error"` → `is_error` + message |

A run that ends without a `result` never ends silently: exit ≠ 0 → `TurnComplete(is_error=True)`
whose `result` is the error event's message or the last useful stderr lines (an
`IneligibleTierError` gets the auth explanation in front); exit 0 → a plain `TurnComplete`. The web
UI shows `is_error` + `result` as an error notice. Non-JSON stdout lines are dropped.

**Thinking** is not in stream-json. The CLI writes a step's thoughts into the JSONL when it records
that step, so the manager tails the session file during the turn (`_ThoughtTail`, local sessions
only): at each `tool_use` / `tool_result` / `result` and at EOF it reads the new bytes and emits
`ThinkingDelta` + `ThinkingComplete` for thoughts not seen before (messages already on disk when the
turn started — including the ones a resume snapshot restates — count as seen). A step's thinking
therefore arrives after its text or tool call, not before it; reopened history shows it first.

## Interrupt and process cleanup

The CLI normally relaunches itself as a child Node process (to raise the heap limit), and the parent
installs no-op SIGINT/SIGTERM/SIGHUP handlers — so `interrupt()` was a no-op and killing the PID
orphaned the worker. `GEMINI_CLI_NO_RELAUNCH=true` keeps a single process (we lose only the automatic
`--max-old-space-size`; settable through `NODE_OPTIONS`). The CLI's shell tool starts commands
**detached** (their own process group) and does not stop them when the CLI exits on SIGINT, so
`interrupt()` / `_kill_proc()` snapshot the process tree from `/proc` before signalling the CLI's
group, then SIGTERM (and after 1 s SIGKILL) whatever survives the CLI — matching pid *and* start time.
Verified live: interrupting `sleep 45; echo finished` mid-tool leaves no `gemini`/`sleep` process.

## HarnessSpec values

`name="gemini"`, `label="Gemini CLI"`, `comm_prefix="node"` (shared with Qwen),
`ssh_control_path_prefix="gemini"`, `requirements_file=None`, `npm_package="@google/gemini-cli"`,
`cli_binary="gemini"`, `env_keys=("GEMINI_API_KEY",)`, `catalog_loader=_load_gemini_catalog`.

## SSH working directories

The argv is wrapped by `build_remote_argv()` like Qwen's. Only non-secret variables travel in the
remote command (`GEMINI_CLI_TRUST_WORKSPACE`, `GEMINI_CLI_NO_RELAUNCH` and the per-turn
`ARCHIE_GEMINI_*`); the remote host needs its own `GEMINI_API_KEY` — the non-interactive SSH shell
does not source `context/.env`, so put it in `~/.gemini/.env` (mode 600 — the Linux/macOS installer
offers to copy it, `install/doctor.sh` checks it; the CLI reads it; without it
turns fail with exit 41, the CLI's auth error) — and its repo's
`.gemini/settings.json` with Archie's keys (run the installer, or `python3
backend/manager/gemini/workspace_settings.py <repo>` there). Thinking tailing and stub cleanup are
local-only. See [ssh-remote-execution](../infrastructure/ssh-remote-execution.md).

## Landmines

1. **Trust check.** Without `--skip-trust` the CLI prints one stderr line and exits 1 in headless
   mode. The manager passes the flag and sets `GEMINI_CLI_TRUST_WORKSPACE=true`; untrusted, the
   workspace settings would not load at all.
2. **`type: "gemini"`, not `"assistant"`** — easy to miss when copying the Qwen adapter.
3. **The JSONL is a log.** Duplicate ids are updates, `$set.messages` replaces everything,
   `$rewindTo` truncates. Reading lines as messages showed every 0.42 tool step twice (fixed
   2026-10-07); 0.63's snapshots would have hidden or duplicated whole turns.
4. **File name ≠ session id** — only the 8-char prefix; glob and read the header.
5. **Content shapes vary.** User content is a list of `{text}` (or `{functionResponse}` for tool
   answers); assistant content is a string in live records and a part list in snapshots.
6. **`thoughts` is top-level**, not a content block, and thoughts never appear in stream-json.
7. **Tool calls are inline** (`toolCalls` on the assistant line). Without the split into
   tool_use + tool_result messages, reopened conversations lost their tool calls.
8. **`.gitignore` hides `context/`.** `context.fileFiltering.respectGitIgnore=false` lets the agent
   read memory, skills and history. Until 2026-10-07 the seed put `fileFiltering` at the top level,
   which the CLI never read — this never worked before.
9. **`--session-id` only creates; stubs block both flags.** Given an id that already exists, the CLI
   prints "Session ID … already exists. Use --resume" and exits; `--resume` only accepts files with
   resumable content. A turn failing before the model call (auth) still writes a header-only stub,
   so the 2026-10-07 "decide by the file on disk" fix still wedged the session; the manager now
   checks resumability and removes the stub.
10. **Session retention deletes history.** `general.sessionRetention` defaults to enabled / 30 days
    and `cleanupExpiredSessions` runs on every start, headless too, over `<project temp dir>/chats`
    — `context/chats/` here — deleting sessions older than 30 days, **every session file without
    resumable content**, their `context/<sessionId>/` artifact dirs and `context/tool-outputs/`.
    Archie's workspace settings switch it off, and the manager refuses a repo-root turn when it
    cannot guarantee that.
11. **`oauth-personal` is dead** (2026-06-18) and wins over `GEMINI_API_KEY` when selected in
    `~/.gemini/settings.json`. See [Auth](#auth-api-key-only).
12. **Self-relaunch swallows signals; the shell tool detaches.** See
    [Interrupt](#interrupt-and-process-cleanup).
13. **System settings path is root-only since 0.60** (`GEMINI_CLI_SYSTEM_SETTINGS_PATH` files must be
    root-owned with root-owned parents, else "Security Warning: Skipping system settings file") —
    hence the env-templated workspace file.

## Upgrading the CLI

1. Read the release notes from the pinned version on (`gh api repos/google-gemini/gemini-cli/releases`);
   watch for changes to stream-json, the JSONL record shapes (`ChatRecordingService`,
   `loadConversationRecord`), settings keys, `--session-id`/`--resume` and auth.
2. Install into a scratch prefix (`npm install --prefix <dir> @google/gemini-cli@<v>`) and check that
   the workspace file still resolves: load it with the bundle's `loadSettings(<repo>)` and
   `ModelConfigService.getResolvedConfig({model, isChatModel: true})` for a 2.5 and a 3.x model, with
   and without the `ARCHIE_GEMINI_*` variables; a bogus `ARCHIE_GEMINI_THINKING_LEVEL` must make the
   API answer 400 (no tokens spent).
3. Check the model tables in `catalog.py` against the bundle's `DEFAULT_MODEL_CONFIGS` /
   `VALID_GEMINI_MODELS`.
4. Run the gemini tests, one live turn with a shell tool, one resume, one interrupt (no leftover
   processes), then bump `GEMINI_CLI_VERSION` in `install/harness-versions.env`.

## History

- 2026-05-15 — added as the third harness on branch `provider-generalization` (`5c9cb0f`); the
  registry contract test for path resolvers was relaxed to allow `[]`.
- 2026-05-16 — `session_discoverer` added so sessions under `~/.gemini/tmp/` showed up (`989fd71`);
  then `~/.gemini/tmp/<label>` symlinked to `context/` and scans de-duplicated (`24ac981`);
  `GEMINI.md` symlink committed (`1a1b0d9`).
- 2026-06-18 — Google stopped serving Gemini CLI to personal-account logins; Archie's Gemini
  sessions failed silently (empty turns) from then on.
- 2026-10-07 — resume vs pin decided by the JSONL on disk (resumed sessions sent `--session-id`,
  which the CLI rejects for an existing id).
- 2026-10-07 — upgraded 0.42.0 → 0.63.0 (branch `harness-upgrade`): API-key auth per run, workspace
  settings with retention off and the real `context.fileFiltering`, env-templated thinking options,
  catalog (built-in ∪ models.list) with `thinking_level` / `thinking_budget` / `approval_mode`,
  log-replay adapter (upserts, snapshots, rewinds, functionResponse turns, hidden session context),
  errors surfaced instead of empty turns, stub cleanup, live thinking from the JSONL, single-process
  CLI + process-tree reaping for interrupts.
