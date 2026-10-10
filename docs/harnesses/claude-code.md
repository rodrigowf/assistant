---
name: claude-code
category: archie/harnesses
tags: [claude-code, claude-agent-sdk, harness, claude-config-dir, jsonl, skills, auth, oauth, chrome, sdk-upgrade, harness-catalog, effort, thinking]
created: 2026-05-15
modified: 2026-10-09
summary: The Claude Code harness — ClaudeSessionManager over claude-agent-sdk, .claude_config layout, JSONL, SDK pin (0.2.164), config catalog + options, auth, Chrome flag.
source: curated (consolidated from memory notes assistant/providers/provider_generalization.md, assistant/providers/qwen_code_adaptation.md, assistant/utilities/project_skills_not_registered.md, auto-memory project_sdk_upgrade_path_2026_06_18.md, feedback_verify_auth_under_backend_config_dir.md, feedback_jetson_oauth_token_expiry.md, feedback_ssh_remote_cli_nvm_path.md; verified against code 2026-10-06)
references:
  - registry.md
  - authentication.md
  - qwen-code.md
  - gemini-cli.md
  - ../architecture/agent-sessions.md
  - ../architecture/backend.md
  - ../architecture/orchestrator.md
  - ../integrations/skills.md
  - ../infrastructure/ssh-remote-execution.md
  - ../infrastructure/installation.md
  - ../clients/browser-extension.md
  - ../operations/troubleshooting.md
  - model-studio.md
  - ../architecture/memory-and-search.md
---

# Claude Code harness

The default and most complete harness. Each chat tab with `provider: "claude"` is one
`ClaudeSessionManager` (`backend/manager/claude/session.py`) holding a `ClaudeSDKClient` from
`claude-agent-sdk`, which runs the CLI bundled inside the SDK as a long-lived subprocess. It is the
only harness with permission gating, `/compact`, slash commands, MCP servers, the Chrome flag and
seq-numbered replay for reconnecting clients.

Lifecycle, the persistent receive loop, permission gating, the stall watchdog and per-session
config are shared session machinery — see [agent sessions](../architecture/agent-sessions.md). This
doc covers what is specific to Claude Code.

## File map

| File | Contents |
|---|---|
| `backend/manager/claude/session.py` | `ClaudeSessionManager` (historical alias `SessionManager`), `SessionAbandoned(TurnAbandoned)`, `kill_claude_subprocess()`, `_extract_subprocess_pid()`, `_build_options()`, `_write_ssh_wrapper()`, `_DEFAULT_GATED_TOOLS`, `_PERMISSION_GATING_PROMPT` |
| `backend/manager/claude/adapter.py` | `ClaudeAdapter` (JSONL), `fold_orchestrator_lines()`, `HarnessSpec(name="claude", catalog_loader=…)` |
| `backend/manager/claude/catalog.py` | `load_claude_catalog()` (models + options for the settings UIs), `claude_model_caps()` (offline per-model effort/thinking support), `record_cli_models()` |
| `backend/manager/auth.py` | `AuthManager` — `claude auth status`, `claude setup-token`, credentials file under `CLAUDE_CONFIG_DIR` |
| `backend/api/routes/auth.py` | `GET /api/auth/status`, `POST /api/auth/login`, `POST /api/auth/credentials` |
| `backend/api/session_factory.py` | `build_session_config()` — working dir, SSH, provider, model, MCP servers, chrome flag |
| `backend/requirements-claude.txt` | `claude-agent-sdk==0.2.164` (exact pin; comment block = upgrade checklist) |
| `backend/tests/test_claude_adapter.py`, `test_session.py`, `test_claude_harness_options.py`, `test_ssh_session_churn.py`, `test_ssh_helper.py`, `test_orphan_reaper.py`, `test_pool_turn.py` | Tests |

## `CLAUDE_CONFIG_DIR=.claude_config/`

`shared/scripts/run.sh` (reached as `context/scripts/run.sh`, which the backend service uses)
exports `CLAUDE_CONFIG_DIR=<repo>/.claude_config` and sources `context/.env`. The bundled CLI
therefore keeps all its state in the gitignored `.claude_config/`, not in `~/.claude/`:

| Path in `.claude_config/` | What it is |
|---|---|
| `projects/-home-rodrigo-assistant` → `../../context` | Project dir for the repo root. Session JSONL lands at `context/<sdk-session-id>.jsonl`; subagent and tool-result state in `context/<uuid>/`; auto-memory would resolve to `context/memory/` (switched off in the repo — see below) |
| `skills` → `../context/skills` | Meant for skill discovery (see Pitfalls) |
| `agents` → `../context/agents` | Subagent definitions |
| `.credentials.json` | OAuth credentials of this machine's grant (not synced) |
| `.claude.json` | CLI user state, including MCP servers configured on this machine |
| `sessions/<pid>.json`, `history.jsonl` | CLI bookkeeping (pid, session id, cwd, version) |
| `settings.json`, `plugins/`, `todos/`, `debug/`, … | Ordinary CLI state |

The project key is the absolute working directory with `/` replaced by `-`. Both machines install
at `/home/rodrigo/assistant`, so the key and the symlink are identical on the laptop and the Jetson
and a session resumes on either ([ssh-remote-execution](../infrastructure/ssh-remote-execution.md)).
A session whose working directory is something else gets a different key, and its JSONL is not in
`context/` unless that project dir is linked too.

The installers create the `projects/`, `skills` and `agents` symlinks (`install/linux/install.sh`
step 3, for `--with-claude` and `--with-modelstudio`; the project key replaces every
non-alphanumeric character of the path with `-`, like the CLI); `shared/scripts/setup-context.sh`
also creates the SDK compatibility symlink. `install/doctor.sh` checks all three, the SDK version
against `requirements-claude.txt`, the bundled CLI and the auth source, and reports auto-memory for
information only (the backend switches it off in the repo; `autoMemoryEnabled` is not read)
([installation](../infrastructure/installation.md#the-doctor-installdoctorsh)). Project instructions
come from `CLAUDE.md` at the repo root, a symlink to `context/AGENTS.md` (shared with
`QWEN.md` and `GEMINI.md`).

**Memory.** Through that project symlink the CLI's auto-memory directory is `context/memory/`: it
loads the wiki's `MEMORY.md`, but its own instructions make the agent save flat notes in its own
frontmatter at the root of `context/memory/` and append pointers to `MEMORY.md` (seen live with
Model Studio on 2026-10-08; it overwrote `MEMORY.md` on 2026-10-03). `"autoMemoryEnabled": false`
in `.claude_config/settings.json` does not help — SDK sessions load only the `project` and `local`
setting sources. So for a session in the repo `_harness_env()` sets
`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` (forwarded over SSH too) and `_build_options()` appends the
shared memory block (`backend/manager/memory_context.py`: the live `MEMORY.md`, read at session
start, plus "write memory with file tools per `AGENTS.md`") after the gating prompt — the same block
Qwen and Codex get ([memory and search](../architecture/memory-and-search.md#every-harness-reads-and-writes-the-same-memory)).
Other working directories keep the CLI's default auto-memory (in `.claude_config/projects/<key>/memory/`).

## How a session is built (`_build_options()`)

| Option | Value | Why |
|---|---|---|
| `include_partial_messages` | `True` | Token-level `TextDelta` / `ThinkingDelta` |
| `setting_sources` | `["project", "local"]` | Reads `<project_dir>/.claude/settings.json` and `settings.local.json` (seeded from `install/cli-runtime/claude/settings.json`) |
| `can_use_tool` | `_can_use_tool` | Gates `_DEFAULT_GATED_TOOLS = {"ExitPlanMode"}`; everything else auto-allows |
| `system_prompt` | preset `claude_code` + `append=_PERMISSION_GATING_PROMPT` (+ the memory block in the repo) | Agent announces intent before a gated tool; `MEMORY.md` in context |
| `permission_mode` | from config (`"default"`, so the callback fires) | |
| `model`, `max_budget_usd`, `max_turns` | from `ManagerConfig` | Model comes from per-session / global `harness_model.claude` |
| `resume`, `fork_session` | SDK session id / fork flag | Resume and fork from history |
| `mcp_servers` | resolved per session | Overrides `.claude.json` when given |
| `effort`, `thinking`, `fallback_model` | from `ManagerConfig.harness_options` (see [Configuration](#configuration-catalog--options)) | Nothing is passed for an unset option |
| `extra_args` | `{"chrome": None}` when the chrome flag is on, plus `{"thinking-display": "summarized"}` when no thinking mode is set | `--chrome`; thinking text (see below) |
| `env` | `os.environ` minus `CLAUDECODE`, plus `CLAUDE_CODE_ENABLE_TODO_TOOLS=1` (unless `todo_tools` is off) and `CLAUDE_CODE_MCP_STARTUP_WAIT_MS=2000` (unless already set), and `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` in the repo | Spawn from inside Claude Code; checklist tools; MCP tools on turn 1. The SSH wrapper forwards the two knobs too |
| `stderr` | logged as `claude CLI stderr [<local_id>]` | Errors visible in the backend log |
| `cwd` / `cli_path` | `project_dir` locally; for SSH a temp wrapper script and `cwd=$HOME` | [ssh-remote-execution](../infrastructure/ssh-remote-execution.md) |

`_run_lifecycle()` creates the client, `connect()`s, grabs the subprocess PID through the SDK's
private `client._transport._process.pid` (best effort; the pool's orphan reaper is the fallback),
reads `get_server_info()` (the CLI's initialize response — its `models` list feeds the catalog's
alias rows through `record_cli_models()`; the session id comes from the resume id, the `init`
system message or the first `ResultMessage`), then starts the persistent
`_receive_loop()`. Shutdown cancels the loop, bounds `disconnect()` at 8 s, and SIGTERM/SIGKILLs a
surviving PID (`kill_claude_subprocess` checks `/proc/<pid>/comm` starts with `claude` first).
Constants: stall notice 120 s then every 60 s, abandoned turn 240 s, replay buffer 500 events.

## JSONL shape

One line per SDK event, `type` one of `user`, `assistant`, `system`, plus internal types
`queue-operation`, `ai-title`, `attachment`, `last-prompt`, `file-history-snapshot`. User content is
a string (or `tool_result` blocks); assistant content is a list of `text` / `thinking` / `tool_use`
blocks. That is already the normalized shape, so `ClaudeAdapter.read_messages()` only filters.

The orchestrator's own JSONL files (`context/<uuid>.jsonl` with an `orchestrator_meta` line, plus
top-level `tool_use` / `tool_result` lines carrying `tool_call_id`) are also read by this adapter:
`fold_orchestrator_lines()` folds each turn into one assistant message so truncate/fork counts match
what the clients render ([orchestrator](../architecture/orchestrator.md)).

## SDK version

Pinned **`claude-agent-sdk==0.2.164`** (bundled CLI 2.1.292), upgraded 2026-10-07 from 0.1.81 / CLI
2.1.139. The pin is exact on purpose: every SDK release swaps the bundled CLI, and CLI behaviour
changes without SDK changelog entries. The Python API only gained things (nothing Archie uses was
removed or renamed); the behaviour changes all come from the newer CLI, and each has a deliberate
default in `_build_options()` so sessions behave as before:

| CLI change | Effect without a fix | What `_build_options()` / `_process_message()` does |
|---|---|---|
| ≥ 2.1.287 streams thinking as `display: "updates"` unless the host asks for a display (SDK PR #1367) | `thinking_delta` events arrive with empty text — blank thinking cards | Always requests `--thinking-display summarized` (inside the `thinking` config when a mode is set, else as an extra arg — never twice); empty thinking deltas/blocks are dropped |
| ≥ 2.1.233 / 2.1.268 offer TodoWrite / Task* only on Claude 3.x, Opus ≤ 4.7, Sonnet ≤ 4.6, Haiku 4.5 | No checklist cards on 4.8 / 5.x models | `CLAUDE_CODE_ENABLE_TODO_TOOLS=1` (option `todo_tools`, default on). Verified live: TodoWrite is in the init tool list on Sonnet 5.5 with it, absent without |
| 2.1.274: a stream-json first turn no longer waits (≤ 2 s) for still-connecting MCP servers whose tools tool search defers | `chrome-devtools` etc. may be missing on turn 1 | `CLAUDE_CODE_MCP_STARTUP_WAIT_MS=2000`, the old bound; the wait ends as soon as servers connect |
| SDK 0.2.137: `ConversationResetMessage` on `/clear` | The session keeps the old SDK session id (wrong JSONL) mid-turn | Marks a reset; the next message carrying a new `session_id` (the `init` system message) is adopted. Verified live |
| Default model (no `--model`) is now Sonnet 5.5 on a subscription | Cost/behaviour change | Pick a model in the settings (catalog below) |

Also in 0.2.x: zombie-CLI prevention on cancel (0.2.111), `ResultError` for terminal error exits
(still lands as `SUBPROCESS_CRASHED`), `forward_subagent_text` / `verbatim_prompts` (keep both
`False`: subagent text would surface as top-level text, and Archie sends `/compact` as a prompt).
`permission_mode` stays explicitly `"default"` — 2.1.285 starts some SDK sessions in `auto` when
none is configured.

**Upgrade checklist** (also in `requirements-claude.txt`): install the exact version, check
`python -c "import claude_agent_sdk as s, claude_agent_sdk._cli_version as v; print(s.__version__, v.__cli_version__)"`,
run the Claude tests (the `_build_command` assertions in `test_claude_harness_options.py` catch
flag-mapping drift), then smoke a real session: a Bash tool call, a thinking turn with visible text,
the TodoWrite tool in the init list, ExitPlanMode gating, interrupt, `/compact`, resume, an SSH
session. On aarch64 hosts run the bundled binary once (`<venv>/lib/python3.*/site-packages/claude_agent_sdk/_bundled/claude --version`)
before restarting the backend; rollback is `pip install 'claude-agent-sdk==0.1.81'` + restart.

Earlier stages:

- **Stage A (done 2026-06-18, `3d24e80`)**: 0.1.39 → 0.1.81. 0.1.51 (upstream PR #746) replaced
  `anyio.TaskGroup` in `Query` with `asyncio.create_task`, fixing the cross-task `__aexit__` wedge
  (upstream issue #378) that pinned one core in anyio's `_deliver_cancellation` while the loop
  watchdog still saw a live loop. 0.1.40 made the SDK skip unknown message types. Both fixes let
  two monkey-patches (`_patch_sdk_message_parser`, `_patch_sdk_query_close`) be deleted.
- **Stage B (done 2026-10-07)**: 0.1.81 → 0.2.164, above.
- Details and the 2026-08-25 venv-drift incident: [agent sessions — SDK version](../architecture/agent-sessions.md).

## Configuration (catalog + options)

`HarnessSpec.catalog_loader` is `load_claude_catalog()` (`backend/manager/claude/catalog.py`), served
by `GET /api/config/harness/claude/catalog` ([registry](registry.md) has the shared contract).

**Models.** Live from `GET https://api.anthropic.com/v1/models` with the CLI's OAuth token
(`Authorization: Bearer $CLAUDE_CODE_OAUTH_TOKEN` + `anthropic-beta: oauth-2025-04-20`; falls back
to `ANTHROPIC_API_KEY`), 4 s timeout. Each row carries its context window, vision support, thinking
support and the effort levels it accepts (`capabilities.effort` / `capabilities.thinking.types`).
On failure or without a credential the catalog shows a built-in snapshot (taken 2026-10-07) plus a
`warnings` entry. The CLI aliases come first — `default`, `sonnet`, `opus`, `fable`, `haiku` — with
the caps of the model they resolve to; after a session has connected, the CLI's own picker list
(`server_info["models"]`) replaces the built-in alias map (rows marked `source: "cli"`).
`default_model` is what `default` resolves to (Sonnet 5.5 today).

**Options** (`harness_options.claude` globally, or per session):

| Key | Kind | Values | Becomes | Per-model rules |
|---|---|---|---|---|
| `effort` | select | `low` `medium` `high` `xhigh` `max` | `--effort` | Each model row lists its levels (4.6: no `xhigh`; Opus 4.5: up to `high`; Haiku/Sonnet 4.5: none). An unsupported level is lowered to the nearest supported one, or dropped |
| `thinking` | select | `adaptive` `enabled` `disabled` | `--thinking adaptive` / `--max-thinking-tokens N` / `--thinking disabled`, with `--thinking-display summarized` | Offered (`models`) only where there is a choice: hidden for adaptive-only models (Opus 5.5, Sonnet 5.5, Fable 5.x and the aliases resolving to them), and ignored there by the session too. `enabled` on a model without a budget mode (4.7+, 5.x) becomes `adaptive`; `adaptive` on a 4.5 model becomes `enabled` |
| `thinking_budget` | number 1024–128000 | tokens (default 16000) | the `N` above | Only with `thinking=enabled`; offered for 4.5/4.6 models |
| `fallback_model` | select | any model row | `--fallback-model` | Ignored when equal to the main model (the CLI refuses that) |
| `todo_tools` | toggle | default on | `CLAUDE_CODE_ENABLE_TODO_TOOLS=1` | Off = the CLI's own per-model choice (no TodoWrite on 4.8 / 5.x) |

The capability checks use `claude_model_caps()`, which never touches the network (last live list →
built-in snapshot → family rules); an unknown model gets the values unchanged. `permission_mode` is
deliberately not an option: gating needs `"default"`. No options → only the deliberate defaults from
[SDK version](#sdk-version) differ from a bare session.

## Auth

Settings → Accounts drives every method below from the UI, also on a headless server
([authentication.md](authentication.md)). Two credential paths, in precedence order:

1. **`CLAUDE_CODE_OAUTH_TOKEN` in `context/.env`** — a 1-year token printed by
   `claude setup-token`. `run.sh` exports it, local sessions inherit it through `env`, and
   `_write_ssh_wrapper()` forwards it to SSH-remote sessions. Because `context/` is synced, both
   machines get it. This is the current setup.
2. **`.claude_config/.credentials.json`** — this machine's own refreshing OAuth grant, read and
   written by `AuthManager` and the `/api/auth/*` routes.

Rules:

- **Never copy `.credentials.json` between machines.** Anthropic OAuth rotates refresh tokens; two
  machines sharing one grant revoke each other and one ends up "could not be refreshed". Each
  machine needs its own grant, or use the env token.
- **Verify auth under the backend's config dir.** In a Claude Code Bash tool `CLAUDE_CONFIG_DIR` is
  unset, so a bare `claude -p …` uses `~/.claude/.credentials.json` (kept fresh by the interactive
  CLI) and passes while every wrapper session fails. Test with
  `CLAUDE_CONFIG_DIR=/home/rodrigo/assistant/.claude_config claude -p 'reply OK' < /dev/null`.
- The backend caches credentials in memory: after fixing them, restart it
  (`sudo systemctl restart agentic-backend.service` on the Jetson).
- `claude setup-token` is a TUI that only prints its URL in a real PTY; drive it with Python
  `pty.fork()` and grep the log for the URL and the minted token.

Symptom → fix table: [troubleshooting](../operations/troubleshooting.md).

## Chrome flag

`assistant_config.json` / per-session config `chrome_extension: true` makes
`build_session_config()` set `extra_args={"chrome": None}`, i.e. the CLI runs with `--chrome` —
Anthropic's own Claude-in-Chrome integration. It has nothing to do with Archie's
`apps/browser-extension/` ([browser extension](../clients/browser-extension.md)), which agents drive
through the `/browser-control` skill. Only the Claude harness honors this flag.

## Pitfalls

- **Subclassed by the Model Studio harness** ([model-studio.md](model-studio.md)): keep
  `_harness_env()`, `_harness_option_kwargs()`, `_ssh_prefix`, `_record_cli_models()` and
  `_ssh_auth_env()` as the override points. `ClaudeAgentOptions.env` is merged *over*
  `os.environ` by the SDK, so removing a key from it never removes an inherited variable.
- **Project skills may not register** as `Skill(<name>)` or `/<name>` inside a wrapper session
  (seen 2026-08-28: "Unknown skill: browser-control" while built-in skills listed fine). The
  `.claude_config/skills` symlink is not enough. Untested but consistent with the code: with
  `CLAUDE_CONFIG_DIR` set, `.claude_config/skills` is the *user* skills dir, and
  `setting_sources=["project","local"]` does not include `"user"`; project-scoped skills would be
  read from `<project_dir>/.claude/skills/`, which does not exist. Test one change at a time if
  fixing it. Workaround: read `context/skills/<name>/SKILL.md` and follow it
  ([skills](../integrations/skills.md)).
- If sessions die with "Unknown message type" / `MessageParseError`, or behave differently after a
  deploy, check the **installed** SDK version before the pin; a drifted venv is the usual cause.
- Empty thinking cards after a CLI bump: the CLI changed its thinking display again — check that
  `--thinking-display` is still in the argv (`test_claude_harness_options.py`).
- `.venv/bin/pip` can have a stale shebang after the repo moves; use `.venv/bin/python -m pip`.
- Do not bump anyio to fix loop wedges; the SDK was the cause.
- `kill_claude_subprocess` and the reaper rely on the comm name `claude`; a future CLI rename would
  need `comm_prefix` updated.
- SSH-remote sessions that exit 127: two different causes (CLI not found, then `node` not found) —
  [ssh-remote-execution](../infrastructure/ssh-remote-execution.md).
- SSH-remote sessions run the *remote* machine's `claude`, not the bundled one: a new flag must
  exist there too (`--effort`, `--thinking`, `--thinking-display`, `--fallback-model` all exist in
  2.1.215). Env knobs reach it only because `_write_ssh_wrapper()` forwards them explicitly.
- Until 2026-10-07 the SSH wrapper broke on any argument containing an apostrophe (one backslash
  too few in its `sed` escaping); fixed in `manager/_ssh.py` with a round-trip regression test.

## History

- Before 2026-05-15 the wrapper was Claude-only (`manager/session.py` `SessionManager`). The Qwen
  work renamed it `ClaudeSessionManager`, moved shared state into `BaseSessionManager`, made the SDK
  import lazy, and moved it into `manager/claude/` ([registry](registry.md)).
- 2026-06-18 — SDK floor raised to 0.1.81, two monkey-patches deleted (`3d24e80`).
- 2026-08-24 — long-lived `CLAUDE_CODE_OAUTH_TOKEN` replaced copying `.credentials.json` between
  machines.
- 2026-08-25 — venv found at 0.1.39 despite the pin; the floor comment in `requirements-claude.txt`
  documents the crash (`a997c12`).
- 2026-10-07 — SDK 0.2.164 / CLI 2.1.292 (exact pin), behaviour-preserving defaults (thinking
  display, todo tools, MCP startup wait, `/clear` session id), config catalog + options, 1M context
  windows for 4.6+ / 5.x, SSH-wrapper apostrophe fix.
- 2026-10-08 — auto-memory off in the repo, the shared `MEMORY.md` block appended instead
  (memory parity across harnesses).
