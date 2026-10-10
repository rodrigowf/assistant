---
name: registry
category: archie/harnesses
tags: [harness, harness-registry, harnessspec, provider-adapter, session-store, multi-harness, claude-code, qwen, gemini, codex]
created: 2026-05-15
modified: 2026-10-08
summary: The pluggable agent-CLI harness layer — HarnessRegistry, HarnessSpec, adapters, SessionStore, dispatch sites, and how to add one.
source: curated (consolidated from memory notes assistant/providers/provider_generalization.md, assistant/providers/qwen_code_adaptation.md, assistant/providers/gemini_cli_adaptation.md; verified against code 2026-10-06)
references:
  - authentication.md
  - claude-code.md
  - qwen-code.md
  - gemini-cli.md
  - codex-cli.md
  - model-studio.md
  - ../architecture/agent-sessions.md
  - ../architecture/backend.md
  - ../architecture/orchestrator.md
  - ../infrastructure/installation.md
  - ../infrastructure/ssh-remote-execution.md
  - ../voice/architecture.md
  - ../architecture/memory-and-search.md
---

# Harness registry

A **harness** is the agent CLI that runs a chat session: Claude Code, Qwen Code, Gemini CLI or
Codex. Each one plugs into the backend through one module that registers a `HarnessSpec` in
`backend/manager/registry.py` (`HarnessRegistry`). Nothing else in the backend branches on harness
names, so adding another harness is a new subpackage plus one line in `_ADAPTER_MODULES`.

The harness axis is separate from two other "provider" axes:

| Axis | What it picks | Where |
|---|---|---|
| Session harness | Which CLI runs a chat tab | `backend/manager/registry.py` (this doc) |
| Orchestrator text provider | Which API the orchestrator's own loop calls (Anthropic / OpenAI-compatible) | `backend/orchestrator/providers/` — [orchestrator](../architecture/orchestrator.md) |
| Voice provider | OpenAI Realtime / Qwen-Omni / Gemini Live | `backend/orchestrator/providers/voice_registry.py` — [voice architecture](../voice/architecture.md) |

Do not merge them: a harness ↔ voice provider mapping is many-to-many.

## File map

| File | Role |
|---|---|
| `backend/manager/registry.py` | `HarnessSpec` (frozen dataclass), `HarnessRegistry`, `get_registry()`, `register_harness()`, `registered_provider_names()`, `ensure_all_registered()`, `_ADAPTER_MODULES` |
| `backend/manager/protocol.py` | `ProviderAdapter` ABC (JSONL parsing), `ProviderRegistry`, `register_provider()`, `detect_provider()`, shared helpers `extract_text`, `extract_blocks`, `tool_result_text`, `is_visible_message_default` |
| `backend/manager/base_session.py` | `BaseSessionManager` ABC, `TurnAbandoned`, `SessionDeadError` |
| `backend/manager/types.py` | Normalized `Event` dataclasses and `SessionInfo` / `SessionDetail` / `MessagePreview` / `ContentBlock` |
| `backend/manager/store.py` | `SessionStore` — lists and reads sessions of every harness from disk |
| `backend/manager/_proc.py` | SDK-free process helpers: `process_alive`, `process_comm`, `looks_like`, `kill_subprocess` |
| `backend/manager/_ssh.py` | Shared SSH helpers (control paths, remote CLI resolution) — [ssh-remote-execution](../infrastructure/ssh-remote-execution.md) |
| `backend/manager/__init__.py` | PEP 562 lazy exports (`ClaudeSessionManager`, `SessionManager` alias, `QwenSessionManager`, …) so `import manager` never pulls `claude-agent-sdk` |
| `backend/manager/{claude,qwen,gemini,codex}/` | One subpackage per harness: `adapter.py` (spec + adapter registration), `session.py` (session manager), `catalog.py` (`catalog_loader`), optional `models.py` (Qwen), `home.py` / `rpc.py` / `items.py` (Codex) |
| `backend/manager/harness_catalog.py` | `HarnessCatalog` / `HarnessModel` / `HarnessOption`, `get_catalog()` (5-minute cache), `validate_options()`, `merge_options()` |
| `backend/tests/test_harness_registry.py` | Registry contract tests (spec completeness, unique SSH prefixes, reaper dispatch, registration order) |

## `HarnessSpec`

Each `manager/<x>/adapter.py` calls `register_harness(HarnessSpec(...))` and
`register_provider(<x>Adapter())` at import time. `ensure_all_registered()` imports every module
in `_ADAPTER_MODULES` (currently `manager.claude.adapter`, `manager.qwen.adapter`,
`manager.gemini.adapter`, `manager.codex.adapter`, `manager.modelstudio.adapter`); it is idempotent and every dispatch site calls it first.
`manager.protocol.ensure_all_registered()` delegates to the same function.

| Field | Purpose | claude | qwen | gemini | codex | modelstudio |
|---|---|---|---|---|---|---|
| `name` | Canonical id in `assistant_config.json` and session config | `claude` | `qwen` | `gemini` | `codex` | `modelstudio` |
| `label` | UI picker label | Claude Code | Qwen Code | Gemini CLI | Codex | Claude Code · Model Studio |
| `description` | One-liner under the picker | | | | | |
| `session_class_loader` | Lazy → `BaseSessionManager` subclass | `ClaudeSessionManager` | `QwenSessionManager` | `GeminiSessionManager` | `CodexSessionManager` | `ModelStudioSessionManager` |
| `adapter_loader` | → the module's `ProviderAdapter` instance | | | | | `ModelStudioAdapter` (Claude's adapter; not passed to `register_provider()`, never claims a file by format) |
| `comm_prefix` | `/proc/<pid>/comm` check before the orphan reaper kills a PID | `claude` | `node` | `node` | `codex` (native binary) | `claude` (same bundled binary) |
| `kill_helper_loader` | Lazy → `kill_<x>_subprocess(pid) -> bool` | | | | | |
| `ssh_control_path_prefix` | First part of `/tmp/<prefix>-ssh-<host>-%r`; must be unique per harness | `claude` | `qwen` | `gemini` | `codex` | `modelstudio` |
| `jsonl_path_resolver` | `session_id → [candidate paths]`; may return `[]` | `context/<id>.jsonl` | `context/chats/<id>.jsonl` | glob `session-*-<id[:8]>.jsonl` | glob `*/*/*/rollout-*-<id>.jsonl` under each sessions root | Claude's (`context/<id>.jsonl`) |
| `session_discoverer` | Optional; yields `(session_id, path)` for files whose name is not the id | — | — | `_gemini_discover_sessions` | `_codex_discover_sessions` | — |
| `requirements_file` | Extra pip deps | `requirements-claude.txt` | — | — | — | `requirements-claude.txt` |
| `npm_package` | Global npm package (install hint only) | `@anthropic-ai/claude-code` | `@qwen-code/qwen-code@<QWEN_CLI_VERSION>` (0.25.0) | `@google/gemini-cli` | `@openai/codex` | `@anthropic-ai/claude-code` |
| `cli_binary` | Executable name on `PATH` | `claude` | `qwen` | `gemini` | `codex` | `claude` |
| `env_keys` | Env vars the harness needs in `context/.env` | — | `DASHSCOPE_API_KEY` | `GEMINI_API_KEY` | — (ChatGPT login via `codex login`; `CODEX_API_KEY` optional) | `DASHSCOPE_API_KEY` |
| `catalog_loader` | Lazy → `HarnessCatalog` (models + options) for the settings UIs; see [Harness catalog](#harness-catalog-models--options) | `load_claude_catalog` | `load_qwen_catalog` | `load_gemini_catalog` | `load_codex_catalog` | `load_modelstudio_catalog` |

The spec is frozen. `register()` is last-wins so tests can swap a spec without clearing the
registry. `names()` keeps registration order, which matters: with no provider pinned the pool uses
the first registered harness (Claude), and the reaper's first-match-wins dispatch uses that order.

## Normalized events and messages

Every session manager yields the same `Event` types from `send()` (`backend/manager/types.py`):
`TextDelta`, `TextComplete`, `ThinkingDelta`, `ThinkingComplete`, `ToolUse`, `ToolResult`,
`TurnComplete`, `CompactComplete`, `PermissionRequest`, `PermissionResolved`, `SessionStalled`,
`SessionTerminated` (with a `TerminationReason`). The pool, the chat WebSocket and the orchestrator
only see these ([agent sessions](../architecture/agent-sessions.md)).

On disk, each adapter converts its native JSONL into one shape — Claude Code's own content-block
format:

```
{"type": "user"|"assistant", "timestamp": "...", "message": {"role": ..., "content": <str | [block]>}}
block = {"type":"text","text"} | {"type":"thinking","text"} |
        {"type":"tool_use","id","name","input"} | {"type":"tool_result","tool_use_id","content","is_error"}
```

`ProviderAdapter` has four abstract members (`provider_name`, `detect_provider`, `read_messages`,
`parse_session_info`) and defaults for `to_previews`, `is_visible_message` and
`visible_line_indices` (used by truncate/fork to count visible turns from the bottom; Gemini and
the orchestrator-file folding in the Claude adapter override them).

## `SessionStore` — one history list across harnesses

`SessionStore` (`backend/manager/store.py`) is what `GET /api/sessions` and the history pane read.

| Harness | Where its JSONL lands | How it gets there |
|---|---|---|
| Claude Code (and the orchestrator) | `context/<sdk-session-id>.jsonl` | `.claude_config/projects/<mangled-path>` → `../../context` |
| Qwen Code | `context/chats/<session-id>.jsonl` | `~/.qwen/projects/<mangled-path>` → `<repo>/context` |
| Gemini CLI | `context/chats/session-<iso-minute>-<id[:8]>.jsonl` | `~/.gemini/tmp/<label>` → `<repo>/context` |
| Codex | `context/codex/sessions/YYYY/MM/DD/rollout-<ts>-<thread-id>.jsonl` (or the shared `~/.codex/sessions/`) | `~/.codex-archie/sessions` → `<repo>/context/codex/sessions` |

`list_sessions()` order of work:

1. Every spec with a `session_discoverer` runs first; it reads Gemini's header line to get the real
   session id and claims those paths.
2. `context/*.jsonl` is scanned (skipping `.sync-conflict-` files and claimed paths).
3. `context/chats/*.jsonl` is scanned, skipping claimed paths and anything named `session-*`
   (so a header-less Gemini file never leaks in with a fake id).
4. Each file's adapter is found by `detect_provider()` (tries adapters in registration order),
   cached per session id in `_provider_cache`; per-file `SessionInfo` is cached by
   `(mtime_ns, size)`. Results are sorted by `last_activity`, newest first.

`_locate_jsonl(id)` checks the Claude root, then `chats/`, then the external-path cache, then every
spec's `jsonl_path_resolver`. Delete is a soft-delete into the store's own `context/trash/`
(the JSONL plus its `<id>.config.json` and `<stem>.summary.json` sidecars, so no orphan config is
left behind; a name collision adds the same `.<timestamp>` suffix to all three). Titles for every
harness live in one `context/.titles.json`.

Memory is shared the same way: every harness gets `context/memory/MEMORY.md` in its context
when working in the repo and writes notes into `context/memory/` per `AGENTS.md` — how each CLI
gets there is in [memory and search](../architecture/memory-and-search.md#every-harness-reads-and-writes-the-same-memory).

## Dispatch sites (all registry lookups)

| Site | What it does |
|---|---|
| `backend/api/pool.py` `_session_manager_for()` | `get_registry().require(provider).session_class_loader()`; empty provider → first registered name; unknown → `ValueError` |
| `backend/api/pool.py` `_kill_tracked_pid()` | Orphan reaper: first spec whose `comm_prefix` matches `/proc/<pid>/comm` supplies the kill helper; unknown comm → left alone |
| `backend/manager/config.py` `_valid_provider_names()` | Validates `provider` / `ASSISTANT_PROVIDER` in `ManagerConfig.load()` (computed per call — do not cache; tests register fixture specs after import) |
| `backend/api/routes/config.py` `_valid_provider_names()` | Validates `PUT /api/config` `provider`; seeds `harness_model` with one empty entry per harness |
| `backend/api/routes/chat.py` `_resolve_session_provider()` | On resume without a pinned provider, sniffs every spec's `jsonl_path_resolver` candidates with `detect_provider()`; a detected provider is persisted to the session config |
| `backend/manager/store.py` | Discoverers and resolvers as above; `_effective_provider()` overlays the per-session pinned provider when it names a harness that reads the detected format (`modelstudio` over `claude`) |
| `backend/api/pool.py` `_pin_provider()` | On every `TurnComplete` in `send()` (chat tabs and the orchestrator runner), writes the session's `provider_name` into `context/<sdk-id>.config.json` unless one is pinned (`pin_session_provider()`); UUID ids and registered harnesses only |

`backend/api/session_factory.py` `build_session_config()` resolves the provider in this order:
per-session config `provider` → provider detected from the resumed JSONL → `assistant_config.json`
`provider`. The harness model comes from the per-session `harness_model` or
`assistant_config.json` `harness_model[<provider>]` ([per-session config](../architecture/agent-sessions.md)).
Harness options come from `assistant_config.json` `harness_options[<provider>]` overlaid key by key
with the per-session `harness_options` (`_resolve_harness_options()`, `merge_options()`); the result
is `ManagerConfig.harness_options`, already validated and without unset keys.

## Harness catalog (models + options)

`HarnessSpec.catalog_loader` returns a `HarnessCatalog` (`backend/manager/harness_catalog.py`): the
models the harness can run (`HarnessModel`: id, label, context window, thinking/vision support,
per-model effort levels, source `builtin`/`settings`/`live`/`cli`) and the options it accepts
(`HarnessOption`: key, label, kind `select`/`toggle`/`number`, choices, informational default,
optional `models` restriction; a `Choice` can carry its own `models` restriction too). Shared keys:
`effort` (reasoning effort) and `thinking`; anything else is harness-specific. `get_catalog()` caches
each catalog for 5 minutes; a loader may hit a models API or the CLI but must not raise for an
unreachable upstream (it returns its builtin list plus a `warnings` entry), and a loader that does
raise yields an empty catalog with a warning.

Presentation hints on `HarnessOption` tell the settings UIs which control to draw; when absent they
infer one from `kind`: `control` (`switch` / `segmented` / `levels` / `slider` / `dropdown`), `ordered`
(the choices are levels — effort, thinking level, verbosity), `unit` and `scale` (`log` for token
budgets), `presets` (named special numbers such as Gemini's −1 = dynamic, 0 = off) with `custom_min` for
the slider's lower end, and `requires` (`{other_key: [values]}` — the option only applies while the other
option's effective value is one of these; `null` stands for unset).

Values: a missing key means "pass nothing, let the CLI decide". `PUT /api/config` merges
`harness_options[<provider>]` key by key (`null` deletes a key); `PUT /api/sessions/{id}/config`
replaces the session's map, where an absent key inherits the global value and a `null` value forces
the CLI default. Both validate against the catalog (`validate_options()`, 400 on unknown keys or
values). The session manager maps the keys it knows onto CLI flags, env or SDK options, ignores the
rest, and must respect the `models` restrictions.

The orchestrator reads catalogs with `list_harness_catalog` and sets defaults through
`update_assistant_config` (`harness_model`, `harness_options`).

## Provider config endpoints and the UI

- `GET /api/config/providers` → `{"providers": [{"id", "label", "description"}, …]}` straight from
  the registry.
- `GET /api/config/harnesses[?refresh=true]` → `{"harnesses": [{"id", "label", "description",
  "catalog"}, …]}` — every harness with its catalog (or `null`); `GET /api/config/harness/{id}/catalog`
  for one.
- `GET /api/config/harness/qwen/models` → Qwen's model catalog from `~/.qwen/settings.json`
  (`backend/manager/qwen/models.py`); kept for older clients.

Web (`apps/web/`): `services/http/endpoints/config.ts` fetches both; the **Default harness** picker
is in `features/settings/pages/ModelPages.tsx` (`AgentSessionsForm`) and the per-session override
("Default (<label>)" plus each harness) in `features/settings/session/SessionSettingsSheet.tsx`.
Android (`apps/android/`): `core/network/.../ArchieApi.kt` `providers()` / `qwenModels()`, used by
`feature/settings` (`ServerPages.kt`, `SessionSettingsSheet.kt`). A harness the UI does not know
still shows up with its registry label.

## Intentional carve-outs

These still name harnesses literally. None of them blocks a new harness.

| Site | Why |
|---|---|
| `install/{linux,apple}/install.sh`, `install/windows/install.ps1` — `WITH_CLAUDE` / `WITH_QWEN` / `WITH_GEMINI` / `WITH_CODEX`, `--with-<x>` / `--without-<x>`, `--qwen-only`, per-harness symlink steps, pinned CLI versions; the `install-with-agent.*` harness → npm package maps | Runs before the venv exists, so it cannot import the registry. A new harness adds its own block ([installation](../infrastructure/installation.md)). |
| `install/cli-runtime/<cli>/` | Per-CLI starter files seeded into `.claude/`, `.qwen/`, `.gemini/` at the repo root |
| `ManagerConfig.provider = "claude"`, `types.py` defaults, `backend/api/routes/sessions.py` `s.get("provider", "claude")` | Fallbacks for records older than per-session provider tracking |
| `backend/manager/auth.py` + `/api/auth/*` | Claude-only OAuth helper behind the first-run AuthGate. Sign-in for every harness lives in `backend/manager/accounts/` (one `AccountService` per service, not part of `HarnessSpec`; see [authentication.md](authentication.md)) |
| `backend/api/session_factory.py` chrome flag / MCP servers | Only `ClaudeSessionManager` consumes `extra_args` and `mcp_servers`; the other harnesses ignore them |
| `apps/web/src/features/history/HistoryPane.tsx` `PROVIDER` label map | Short tag text; unknown ids fall back to the raw id |
| `backend/orchestrator/config.py` `mid.startswith("qwen")` etc. | Orchestrator text-model routing — a different axis |
| `backend/manager/context_windows.py` | Context-window lookup per (provider, model); Qwen reads its settings file, the others a static table |

## How to add a harness

1. **Subpackage** `backend/manager/<x>/` with `__init__.py`, `adapter.py`, `session.py`
   (+ `catalog.py` with a `catalog_loader` for the settings UIs — models and options).
2. **Adapter** (`adapter.py`): subclass `ProviderAdapter`; make `detect_provider` reject the other
   harnesses' files (they are tried in registration order); normalize to the content-block shape;
   call `register_provider(...)` and `register_harness(HarnessSpec(...))` at module scope. Fill every
   spec field — the registry tests assume `comm_prefix`, `ssh_control_path_prefix`,
   `kill_helper_loader` and `jsonl_path_resolver` are real. If the file name is not the session id,
   add a `session_discoverer` and return globbed candidates from the resolver.
3. **Session manager** (`session.py`): subclass `BaseSessionManager`; implement `provider_name`,
   `_run_lifecycle`, `send`, `interrupt`, `subprocess_pid`; raise a `TurnAbandoned` subclass for
   wedged turns; call the pool's PID callbacks (`set_pid_callbacks`) around per-turn subprocesses.
   Spawn-per-turn CLIs copy `manager/qwen/session.py` or `manager/gemini/session.py`; a long-lived
   agent loop copies `manager/claude/session.py` (lifecycle task owns the client and captures the
   PID at connect).
4. **Register** the module in `_ADAPTER_MODULES`.
5. **Storage**: point the CLI's per-project dir at `context/` (or `context/chats/`) with a symlink in
   the installers, choosing a file-name pattern that cannot collide with existing ones.
6. **Installers**: `WITH_<X>` block, `--with-<x>` / `--without-<x>`, post-install hint,
   `install/cli-runtime/<x>/` seed if the CLI needs settings.
7. **Tests**: mirror `backend/tests/test_<existing>_adapter.py` and `test_<existing>_session.py`
   (mock `asyncio.create_subprocess_exec`), plus `_session_ssh.py` if it supports SSH.
8. **Docs**: a peer of [gemini-cli.md](gemini-cli.md) in this folder.

No edits are needed in `pool.py`, `chat.py`, `config.py` or the clients: the new harness appears in
the pickers automatically. Acceptance: picker shows it; a real-CLI turn produces
`TextDelta → TextComplete → TurnComplete`; the adapter rebuilds the conversation from the JSONL it
wrote; removing the module from `_ADAPTER_MODULES` disables it everywhere.

### Claude Code · Model Studio (added 2026-10-07)

The fifth harness (`provider="modelstudio"`) is the claude harness again — a
`ClaudeSessionManager` subclass — with the CLI pointed at Alibaba Model Studio's
Anthropic-compatible endpoint. Same JSONL as Claude, so its adapter is not registered for
detection and the harness is told apart by the provider pinned on the first turn. Details:
[model-studio.md](model-studio.md).

### OpenAI Codex (added 2026-10-07)

Codex is the fourth harness (`provider="codex"`), and the first one driven over a JSON-RPC
protocol: one persistent `codex app-server` per session, `comm_prefix="codex"` (a native binary,
not Node). Details, storage and landmines: [codex-cli.md](codex-cli.md).

## Pitfalls

- `detect_provider` order matters: a loose heuristic in an early adapter claims other harnesses'
  files. Claude's adapter matches `message.content` without `parts`; Qwen's matches `message.parts`,
  `role: "model"` or `system` lines with `subtype`; Gemini's matches its header / `type: "gemini"`.
- Qwen and Gemini share `comm_prefix="node"`. The reaper only kills PIDs the pool tracked, so the
  shared prefix cannot hit a stranger, but whichever Node spec registered first supplies the kill
  helper (both call `_proc.kill_subprocess(pid, comm_prefix="node")`, so the result is the same).
- There is no `.provider` marker file (an old `store.py` docstring mentioned one; corrected
  2026-10-07). Provider detection is purely by JSONL format plus the per-session config `provider`,
  which the pool pins on every session's first turn (2026-10-07) — needed because two harnesses
  (`claude`, `modelstudio`) write the same format.
- `ensure_all_registered()` must run before any lookup; call it rather than reading `_registry`.

## History

- 2026-05-15 — Qwen Code added as second harness (`f5eaa7d`), lazy SDK loading (`115a05b`),
  per-provider requirements (`3bab53d`); `HarnessRegistry` introduced (`e2bf29b`); each harness moved
  into its own subpackage (`3fe9c73`); Gemini CLI added (`5c9cb0f`). Codex deferred.
- 2026-10-07 — Harness catalog (`catalog_loader`, `harness_options`) added; OpenAI Codex added as
  the fourth harness ([codex-cli.md](codex-cli.md)).
- 2026-05-16 — Gemini sessions discovered via `session_discoverer` (`989fd71`), then stored under
  `context/chats/` through a symlink (`24ac981`).
