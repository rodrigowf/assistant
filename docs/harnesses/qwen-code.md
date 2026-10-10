---
name: qwen-code
category: archie/harnesses
tags: [qwen, qwen-code, harness, jsonl, stream-json, dashscope, spawn-per-turn, models, lazy-loading, catalog, harness-options, auto-memory]
created: 2026-05-15
modified: 2026-10-08
summary: The Qwen Code harness (qwen-code 0.25.0) — spawn-per-turn qwen CLI, per-run system settings file, ~/.qwen symlinks into context/, JSONL normalization, model catalog + options, the auto-memory landmine.
source: curated (consolidated from memory notes assistant/providers/qwen_code_adaptation.md, assistant/providers/provider_generalization.md; verified against code 2026-10-06)
references:
  - authentication.md
  - registry.md
  - claude-code.md
  - gemini-cli.md
  - ../architecture/agent-sessions.md
  - ../infrastructure/installation.md
  - ../infrastructure/ssh-remote-execution.md
  - ../voice/qwen-omni.md
  - ../architecture/memory-and-search.md
---

# Qwen Code harness

Alibaba's `qwen` CLI (npm `@qwen-code/qwen-code`, a Node program; **pinned to 0.25.0** in
`install/harness-versions.env`, needs Node 22+) as a session harness. Unlike
Claude Code there is no persistent process: every turn spawns a fresh `qwen` that reads one
stream-json prompt on stdin, runs its agent loop, streams stream-json on stdout and exits. Turns are
chained with `--resume <session-id>`.

Qwen Code as a chat harness is unrelated to the Qwen-Omni **voice** provider
([qwen-omni](../voice/qwen-omni.md)); they only share the DashScope account.

## File map

| File | Contents |
|---|---|
| `backend/manager/qwen/session.py` | `QwenSessionManager`, `QwenAbandoned(TurnAbandoned)`, `_qwen_executable()` |
| `backend/manager/qwen/adapter.py` | `QwenAdapter` (JSONL → normalized), `HarnessSpec(name="qwen")`, `_qwen_jsonl_candidates()` |
| `backend/manager/qwen/models.py` | `list_qwen_models()` / `QwenModelInfo` — model list from `~/.qwen/settings.json` (back-compat route) |
| `backend/manager/qwen/catalog.py` | `load_qwen_catalog()` (the `HarnessSpec.catalog_loader`), DashScope live list, `qwen_model_traits()` family table, `option_applies()` |
| `backend/manager/qwen/run_settings.py` | `build_run_settings()` — the per-run `QWEN_CODE_SYSTEM_SETTINGS_PATH` file; write/remove helpers |
| `backend/api/routes/config.py` | `GET /api/config/harness/qwen/models` (old dropdown); the catalog is served by the generic `/api/config/harness/qwen/catalog` |
| `backend/manager/context_windows.py` | Reads `contextWindowSize` per model from the settings file; family-table fallback for live-only ids |
| `backend/tests/test_qwen_session.py`, `test_qwen_adapter.py`, `test_store_qwen.py`, `test_qwen_models.py`, `test_qwen_catalog.py`, `test_qwen_run_settings.py`, `test_qwen_session_ssh.py`, `test_qwen_session_e2e.py` | Tests (e2e runs the real CLI only with `QWEN_E2E=1`) |

## Storage and config symlinks

Qwen keeps per-project state in `~/.qwen/projects/<mangled-path>/` and writes chats to its `chats/`
subfolder. The installer (`--with-qwen`, `install/linux/install.sh` step 3b) replaces that
directory with a symlink:

```
~/.qwen/projects/-home-rodrigo-assistant  ->  /home/rodrigo/assistant/context
~/.qwen/skills                            ->  /home/rodrigo/assistant/context/skills
```

so sessions land at `context/chats/<session-id>.jsonl`, next to Claude's flat `context/*.jsonl`,
and the same skills are visible (Qwen also reads the repo's `.agents/skills` → `context/skills`,
created by the installer for Codex / Gemini / Qwen). The key replaces every non-alphanumeric
character of the path with `-` (`sanitizeCwd`; on Windows the path is lower-cased first). An empty
real `~/.qwen/skills` directory is replaced by the link. (0.15 also left a `<session-id>.runtime.json` sibling; since 0.25
that file is a short-lived liveness marker that is gone after the run. Old ones are harmless.) If
a real directory already exists the installer copies it to `context/qwen-backup-<timestamp>/`,
lifts `chats/*.jsonl` into `context/chats/`, then links. An existing symlink that points at
another project is left alone with a warning.

The symlink also means **everything else Qwen keeps per project lands in `context/`** — including
the `memory/` folder of its managed auto-memory, which is `context/memory/`, Archie's memory wiki.
That is why every Archie run switches auto-memory off (see [Per-run settings](#per-run-settings)
and the first landmine).

**Memory like Claude's.** With auto-memory off nothing loads `MEMORY.md`, so in the Archie repo
`_build_argv()` adds `--append-system-prompt <memory block>`: the live `context/memory/MEMORY.md`
plus the rule that memory is written with file tools following `AGENTS.md`
(`backend/manager/memory_context.py`, shared by all harnesses —
[memory and search](../architecture/memory-and-search.md#every-harness-reads-and-writes-the-same-memory)).
Qwen rebuilds its system prompt on every spawn, so the block is current every turn and never lands
in the chat JSONL; over SSH it travels as one shell-quoted argument. 0.25 registers no
`save_memory` tool (`manage_memory` / `search_memory` are declared only in structured-recall mode),
so the model saves memory with `write_file` / `edit`. Verified live 2026-10-08 (deepseek-v4-flash):
it quoted `MEMORY.md` without tools and saved a fact by extending an existing wiki note.

Other Qwen state stays in `~/.qwen/` per machine: `settings.json` (model providers, env, auth
type, default model), `output-language.md`, `tmp/`, `todos/`, `debug/`, and since 0.25
`usage_record.jsonl`, `extensions/`, `extension-store/`. Project instructions come from `QWEN.md` at the
repo root, a symlink to `context/AGENTS.md`. `install/cli-runtime/qwen/settings.json` seeds the
repo-local `.qwen/settings.json` (a small command allowlist).

Why everything under `context/`: separate `claude/` and `qwen/` subdirs would have broken every
existing Claude session, and a `context/qwen-data/` tree would split the synced context. With the
symlink, context-sync carries Qwen sessions between machines like everything else.

## Turn protocol

Argv built by `_build_argv()`:

```
qwen --input-format stream-json --output-format stream-json --include-partial-messages \
     --approval-mode yolo --channel SDK [--resume <id> [--fork-session]] [--model <id>] [--max-session-turns N]
```

- The binary is `QWEN_CLI_PATH` or `qwen` on `PATH`.
- The prompt is written to stdin as one line:
  `{"type":"user","message":{"role":"user","content":[{"type":"text","text":…}]}}`, then stdin closes.
- `--approval-mode yolo`: Qwen auto-approves every tool. There is **no permission gating** for
  Qwen sessions (no `PermissionRequest`, no ExitPlanMode popup).
- `--channel SDK` tags wrapper-driven runs in Qwen's logs.
- The env is the backend's env minus `CLAUDECODE`, plus `QWEN_CODE_SUPPRESS_YOLO_WARNING=1` (0.25
  prints a yolo-without-sandbox warning to stderr on every turn otherwise) and
  `QWEN_CODE_SYSTEM_SETTINGS_PATH=<per-run file>` ([Per-run settings](#per-run-settings)). API keys
  (`DASHSCOPE_API_KEY`) come from `context/.env` via `run.sh`, or from the settings `env` block. Qwen
  OAuth was discontinued on 2026-04-15 (0.25 marks `qwen auth` "removed"); Settings → Accounts
  manages the keys ([authentication.md](authentication.md)).
- Fork: the first turn of a forked session runs `--resume <parent> --fork-session` (0.16+); the
  new session id from `system/init` / `result` becomes the session's own id and later turns
  resume it without the flag. If the prewarm saw a CLI older than 0.16 the flag is left out and
  the fork continues the parent session as before.

Qwen's stdout is Anthropic-shaped, and `_translate_event()` maps it:

| Qwen event | Normalized |
|---|---|
| `system` `subtype: init` | captures `session_id` (first turn) |
| `system` `subtype: compact` | `CompactComplete` |
| `stream_event` `content_block_delta` `text_delta` / `thinking_delta` | `TextDelta` / `ThinkingDelta` |
| `stream_event` `content_block_stop` | `TextComplete` / `ThinkingComplete` |
| `assistant` (complete message) | `ToolUse` for each `tool_use` block |
| `user` with `tool_result` blocks | `ToolResult` |
| `result` | `TurnComplete` (`cost=None` — Qwen reports no cost; `session_id`, usage). On `is_error` with no `result` text, `error.message` (API errors, loop detection, `--max-wall-time`/tool-call budgets) — else the `subtype` — becomes `TurnComplete.result` |
| `stream_event` `goal_state`, `control_response`, unknown `system` subtypes | ignored |

Lifecycle: `_run_lifecycle()` pings SSH targets first, adopts the resume id, runs `_prewarm()`
(remote `which qwen`, or a local `qwen --version` to fault Node and the CLI bundle into the page
cache and record the CLI version, 10 s cap — so the cold start is paid at tab open, not on the
first prompt), then idles.
`interrupt()` sends SIGINT; stop sends SIGTERM, then SIGKILL after 2 s. Each turn's PID is reported
through the pool's PID callbacks so the orphan reaper sees it. Watchdog constants match Claude:
stall notice 120 s then every 60 s, `QwenAbandoned` after 240 s of total silence.

SSH: `_maybe_wrap_with_ssh()` resolves the remote `qwen` path and builds an
`ssh … "cd '<dir>' && QWEN_CODE_SUPPRESS_YOLO_WARNING='1' QWEN_CODE_SYSTEM_SETTINGS_PATH='…' exec '<qwen>' …"`
argv with `build_remote_argv()`. One SSH connection per turn, kept cheap by
`ControlMaster`/`ControlPersist=60s` under the `qwen` control-path prefix. The local env is
deliberately not forwarded (the remote has its own `.env`); only those two non-secret switches go
along ([ssh-remote-execution](../infrastructure/ssh-remote-execution.md)).

## Per-run settings

Every turn gets its own Qwen settings file, passed as `QWEN_CODE_SYSTEM_SETTINGS_PATH`
(`backend/manager/qwen/run_settings.py`). Qwen's layers, lowest to highest: defaults <
system-defaults < user `~/.qwen/settings.json` < project `.qwen/settings.json` < **system** < env <
CLI flags. The user's `~/.qwen/settings.json` is never written.

Always in the file:

| Key | Value | Why |
|---|---|---|
| `memory.enableManagedAutoMemory`, `memory.enableManagedAutoDream`, `memory.enableAutoSkill` | `false` | Auto-memory writes into `context/memory/` (first landmine) and costs a model call per turn (extract) plus a recall call |
| `general.outputLanguage` | `"English"` | 0.25 migrates a generated "always English" `~/.qwen/output-language.md` to `auto` when this is unset |
| `general.enableAutoUpdate` | `false` | The CLI is pinned; 0.18+ would update itself on startup |
| `agents.crossSessionMessaging` | `false` | 0.25 opens a per-session local socket otherwise |
| `$version` | `4` | Current schema in 0.15.11 … 0.25.0; avoids a migration rewrite |

Only when needed — a model knob is set, or the chosen model is not in the user's list — the file
also carries a **copy of the user's `modelProviders`** (`mergeStrategy: "replace"`, so it has to be
the whole list) with the selected entry's `generationConfig` patched. Provider entries are
*sealed*: a top-level `model.generationConfig` is ignored for provider models (stderr warning
only), so knobs must sit on the entry. A model id that is not in the user's list (a DashScope live
row) gets a synthetic entry `{id, name, baseUrl: <the user's DashScope base, else intl>, envKey:
<the user's DashScope envKey, else DASHSCOPE_API_KEY>, generationConfig}` — only when the user
already has a DashScope entry or `DASHSCOPE_API_KEY` is set; otherwise `--model` passes through as
before. With no options and no model, the file holds only the fixed keys and argv is unchanged.

Secrets never go in: the user's `env` and `security` blocks are not copied, and keys that look like
credentials (`apiKey`, `Authorization`, `*token`, `password`, …) are stripped from the copied
entries; entries authenticate through `envKey`, an env var *name*. The file is written 0600 into
`$XDG_RUNTIME_DIR/archie-qwen/` (else `<tmp>/archie-qwen-<uid>/`, 0700) — never under `context/`,
which is synced — and deleted when the turn ends. If writing fails the turn runs without it.

SSH sessions: the remote's provider list is unknown locally, so the remote gets only the fixed keys,
piped over the multiplexed connection to `/tmp/archie-qwen-<local_id>.json` (`umask 077`) before
each turn and removed at session stop. Per-run model knobs are **not** applied over SSH; `--model`
still is.

## Catalog and options

`load_qwen_catalog()` (`HarnessSpec.catalog_loader`, cached 5 min by `get_catalog`) merges:

- `source: "settings"` — `~/.qwen/settings.json` `modelProviders`: name, `contextWindowSize`, vision
  (`modalities.image`), thinking badge (`extra_body.enable_thinking`, widened by the family table).
- `source: "live"` — DashScope `GET <base>/models` with `DASHSCOPE_API_KEY` (5 s timeout), filtered
  to chat/coding ids (no realtime/omni/tts/asr/image/embedding/mt/vl/…, no vendor-prefixed ids, no
  dated snapshots), minus ids already in settings, sorted. The endpoint returns bare ids, so
  context window / thinking / efforts come from `qwen_model_traits()` (approximate). On failure or
  without a key: settings rows plus a `warnings` entry.

`default_model` is settings `model.name`; custom ids are allowed.

| Option | Kind | Sent as | Applies to |
|---|---|---|---|
| `thinking` | toggle | `generationConfig.extra_body.enable_thinking` | Hybrid models: Qwen3.5–3.7 plus/flash/max, `qwen3-max`, `qwen-plus/flash/turbo`, open-weight `qwen3*-<n>b`, GLM-5/5.1/5.2, Kimi K2.5/2.6, DeepSeek V3.2/V4. Not thinking-only models (`qwen3.8-max`, `glm-5.3`, `kimi-k2.7-code`, `qwq`, `*-thinking`) or coder models |
| `thinking_budget` | number 1–32768 | `extra_body.thinking_budget` (dropped when `thinking` is off) | Qwen3.x (not 3.8), GLM, Kimi, `qwq` |
| `effort` | select `low`/`medium`/`xhigh` | `extra_body.reasoning_effort` | `qwen3.8-max*`, `qwen3.8-flash*` only — DashScope ignores effort elsewhere |
| `temperature` | number 0–2 | `generationConfig.samplingParams.temperature` | all |

`option_applies()` decides both the option's `models` list in the catalog and what the session
manager actually sends, so a value set globally for one model is silently dropped for a model it
does not fit. Verified on the wire (0.25.0 and 0.15.11 against a capture proxy) and live on
DashScope: thinking off → no thinking deltas (qwen3.6-plus, deepseek-v4-flash); budget honoured;
`qwen3.8-flash` (live-only) with `reasoning_effort: "low"` accepted.

## JSONL format and the adapter

Native lines (written by the CLI):

```
{"type":"user","message":{"role":"user","parts":[{"text":"…"}]}, "uuid","parentUuid","sessionId","timestamp",…}
{"type":"assistant","message":{"role":"model","parts":[{"text":"…","thought":true},{"text":"…"},{"functionCall":{"id","name","args"}}]},"usageMetadata":{…},"model":"…"}
{"type":"tool_result","message":{"role":"user","parts":[{"functionResponse":{"id","name","response":{"output"|"error":…}}}]},"toolCallResult":{"callId","status":"success"|"error"|"cancelled","resultDisplay"}}
{"type":"system","subtype":"ui_telemetry"|"attribution_snapshot","systemPayload":{…}}
```

`QwenAdapter` normalizes: `role: "model"` → `"assistant"`; `parts` → `content` blocks;
`{text, thought: true}` → `thinking`; `functionCall` → `tool_use`; a `type: "tool_result"` line
becomes a `user` message of `tool_result` blocks (`functionResponse.id` → `tool_use_id`,
`response.output` — or `response.error` — → `content`, `is_error` when there is an error or
`toolCallResult.status` is `error`/`cancelled`), the same shape Claude writes and the Gemini adapter
builds, so clients pair each result with its call; keeps `usageMetadata` and `model`; skips
`system` lines. Tool-result lines are not visible turns and do not count as messages. Detection: `message.parts` without `content`, `role: "model"`, or a
`system` line with `subtype`. Sessions are never marked orchestrator.

## Models (old dropdown)

`GET /api/config/harness/qwen/models` (still used by the Android and legacy UIs) returns whatever
`~/.qwen/settings.json` `modelProviders` lists (`id`, `name`, `baseUrl`, `generationConfig.contextWindowSize`, modalities, `enable_thinking`)
— the same catalog `qwen --model` validates against, so DeepSeek, GLM or a local Ollama endpoint
added there appear automatically. `QWEN_HOME` overrides the location (tests). The web and Android
settings show this list as the Qwen harness-model dropdown; the choice is stored in
`assistant_config.json` `harness_model.qwen` or per session. Qwen-only installs default the model
to `qwen3.6-plus`.

## Lazy SDK loading

Qwen needs no Python SDK (`requirements_file=None`). Because `manager/__init__.py` resolves
`ClaudeSessionManager` lazily (PEP 562) and the orchestrator providers do the same, a Qwen-only
install can skip `requirements-claude.txt`, `requirements-anthropic.txt` and even
`requirements-openai.txt`, and the backend still boots; an SDK is imported only when something
asks for that provider. Saving an orchestrator model whose SDK is missing returns 400 with the
`pip install -r requirements-<x>.txt` hint. `install.sh --qwen-only` = `--with-qwen
--without-claude` plus OpenAI-only orchestrator.

## Landmines

- **Qwen's managed auto-memory writes into Archie's memory wiki.** It is on by default (0.15 and
  0.25): after a turn a background subagent with `write_file`/`edit` extracts "memories" into
  `~/.qwen/projects/<mangled>/memory/`, which the project symlink makes `context/memory/`; a recall
  step also reads that folder before each turn, and auto-dream consolidates it. Each costs an extra
  model call. On 2026-08-28 it created Qwen-format notes under `context/memory/project/` and
  `context/memory/feedback/` and prepended two index lines to `MEMORY.md`, plus its cursor files
  `context/meta.json` / `context/extract-cursor.json` (gitignored). The notes were removed in the
  2026-10-07 memory restructure. Fixed by the per-run `memory.*=false` keys; verified live: one
  request per turn and no file under `context/memory/` changes. Interactive `qwen` runs started by
  hand in the repo are *not* covered — set the same keys in `.qwen/settings.json` or run with
  `--bare` if that matters.
- **Never set `QWEN_RUNTIME_DIR` / `QWEN_HOME` for production runs** — they move `projects/` (and so
  the chats) away from the `context/` symlink. `QWEN_HOME` is honoured by `models.py`/`catalog.py`
  only, for tests.
- **Provider entries are sealed**: per-model generation knobs must be on the `modelProviders`
  entry, not in `model.generationConfig` (silently ignored). `samplingParams` on a non-DashScope /
  non-GPT host suppresses Qwen's own `reasoning` injection — the `thinking` option goes through
  `extra_body`, so it is unaffected.
- **Tool results live on their own `type: "tool_result"` lines** (not `user`), with
  `functionResponse` parts. Code that keeps only `user` / `assistant` lines drops them — the
  adapter did until 2026-10-07, so reopened Qwen chats showed tool calls without outputs (fixed
  2026-10-07; live turns were always fine, those come from stdout).
- `role: "model"` and `parts` are the two shapes that break code copied from the Claude path.
- No cost: `TurnComplete.cost` is `None`, so cost badges stay empty.
- Qwen and Gemini both have `comm_prefix="node"`; harmless (see [registry](registry.md)).
- The `.runtime.json` siblings are not sessions; the store's `*.jsonl` glob never matches them.
- After changing the remote CLI location, restart the backend — the resolved remote path is cached
  in-process.
- 0.25 writes a one-line stderr advisory on every turn when the always-loaded context (QWEN.md →
  `context/AGENTS.md`) is over ~10K tokens; it is logged as a WARNING and harmless.
- 0.25 fetches `https://models.dev/api.json` at startup for model limits; `QWEN_CODE_MODELS_DEV=off`
  turns it off if it ever shows up in latency or offline use.
- Parallel tool calls (0.19+) can interleave `tool_use`/`tool_result`; the UI pairs them by id.
- New 0.25 tools (`ask_user_question`, `enter_plan_mode`, `tool_call`/`tool_search` wrappers for
  deferred tools, `manage_memory`, …) may show up as tool cards. `ask_user_question` answers "not in
  non-interactive mode", `enter_plan_mode` stays in yolo, and `manage_memory` is denied while
  auto-memory is off.

## Upgrading the CLI

The version is pinned (`QWEN_CLI_VERSION` in `backend/manager/qwen/adapter.py` and in
`install/harness-versions.env`, which every installer reads; `install/doctor.sh` warns when the two
differ); qwen-code ships a stable release every couple of days, so track a
version deliberately. For a bump: install into a scratch prefix and point `QWEN_CLI_PATH` at it,
re-check the stream-json lines, the chat JSONL shapes, `--resume` of an older session, the
`memory.*` keys (one model request per turn), and the request bodies of the options above against a
local capture proxy (`proxy` setting + a `http://dashscope…` baseUrl in a scratch system-settings
file, `QWEN_RUNTIME_DIR` pointed at scratch); then run the unit tests and a few tiny live turns.
Upgrade every machine with `npm install -g @qwen-code/qwen-code@<version>` and restart the backend.
**0.25 needs Node 22+**: check `node -v` (in the nvm environment the backend runs with) on the
server before upgrading it; an older Node fails at CLI start.


## Why spawn-per-turn instead of `qwen serve`

Qwen has an HTTP+SSE server mode (ACP protocol, port 4170). It was rejected because the wrapper
needs the same control it has over Claude — tool events, mid-run interrupt, streaming intermediate
steps — and the subprocess plus stdin/stdout path gives that uniformly.

## History

- 2026-05-15 — branch `qwen-code`: `f5eaa7d` (harness, adapters, store, badge), `115a05b` (lazy
  SDK loading, `manager/session.py` shim removed, `_proc.py`, shared `TurnAbandoned`, per-turn PID
  tracking), `3bab53d` (per-provider requirements, two-axis install prompt), `0250dcb` (tests,
  e2e gated by `QWEN_E2E`). Then the harness registry and the `manager/qwen/` subpackage
  ([registry](registry.md)).
- The old web app showed a `C` / `Q` letter badge per session; the current apps show the
  harness name as a tag (orchestrator sessions show "Archie" instead).
- 2026-10-07 — `QwenAdapter` converts `type: "tool_result"` lines, so reopened Qwen chats show
  tool outputs.
- 2026-10-07 — branch `harness-upgrade`: qwen-code 0.15.11 → 0.25.0; per-run system settings file
  (auto-memory off, English output, no auto-update), catalog with live DashScope models and the
  `thinking` / `thinking_budget` / `effort` / `temperature` options, `--fork-session`, turn errors
  from `error.message`, yolo warning suppressed.
- 2026-10-08 — `MEMORY.md` block via `--append-system-prompt` in the repo (memory parity with
  Claude Code).
