---
name: model-studio
category: archie/harnesses
tags: [harness, model-studio, dashscope, claude-code, glm, deepseek, kimi, qwen, anthropic-compatible, provider-pinning, harness-catalog, thinking]
created: 2026-10-07
modified: 2026-10-08
summary: The Model Studio harness — Claude Code's agent loop driving GLM, DeepSeek, Kimi and Qwen through Alibaba Model Studio's Anthropic-compatible endpoint; env contract, provider pinning, catalog/options, landmines.
source: curated (implementation + live verification 2026-10-07, CLI 2.1.292, claude-agent-sdk 0.2.164)
references:
  - authentication.md
  - registry.md
  - claude-code.md
  - qwen-code.md
  - ../architecture/agent-sessions.md
  - ../infrastructure/ssh-remote-execution.md
  - ../infrastructure/installation.md
---

# Model Studio harness (`modelstudio`)

**Claude Code · Model Studio** runs the *same* bundled Claude Code CLI as the
[claude harness](claude-code.md) — permission gating, `/compact`, slash commands, MCP servers,
skills, SSH, replay — but points it at Alibaba Model Studio's Anthropic-compatible endpoint
(`https://dashscope-intl.aliyuncs.com/apps/anthropic`) with `DASHSCOPE_API_KEY`. The result is
Claude Code's agent loop on GLM, DeepSeek, Kimi and Qwen models.

## Why this and not the vendors' own harnesses

| Option | Verdict |
|---|---|
| **Claude Code → Model Studio** (this harness) | Small: an env overlay on the existing Claude stack. Verified: `tool_use` with GLM, streamed `thinking_delta` with DeepSeek V4, the whole Claude feature set for free. |
| DeepSeek Harness (`dsh`) | Developer preview ("there will be compatibility-breaking changes"), no token streaming in its headless JSON mode, large install, and its session-log plugin **uploads the full session log to the provider by default**. Revisit only to compare DeepSeek's own agent loop. |
| ZCode (Z.ai) | No official Linux CLI package (build from source), primary auth is a GLM Coding Plan login, a DashScope provider route is unverified. |
| Qwen Code with GLM/DeepSeek ids | Already works (OpenAI-compatible endpoint, see [qwen-code.md](qwen-code.md)); this harness adds Claude Code's loop and tools instead of Qwen Code's. |

## File map

| File | Contents |
|---|---|
| `backend/manager/modelstudio/session.py` | `ModelStudioSessionManager(ClaudeSessionManager)`: env overlay, model pinning, thinking option, cost suppression, SSH auth env; `kill_modelstudio_subprocess()`; `resolve_model()` |
| `backend/manager/modelstudio/catalog.py` | `load_modelstudio_catalog()` (DashScope model list, family filter, builtin fallback), options, `model_context_window()`, `model_thinks()`, `is_claude_model()` |
| `backend/manager/modelstudio/adapter.py` | `ModelStudioAdapter` (Claude's adapter, `provider_name="modelstudio"`, **not** registered for detection) and the `HarnessSpec` |
| `backend/manager/claude/session.py` | Hooks the subclass overrides: `_ssh_prefix`, `_record_cli_models()`, `_ssh_auth_env()` (claude behaviour unchanged) |
| `backend/api/pool.py` `_pin_provider()` + `backend/api/routes/session_config.py` `pin_session_provider()` | Provider pinning on the first turn (all harnesses) |
| `backend/manager/store.py` `_effective_provider()` | Listing overlay: pinned `modelstudio` over detected `claude` |
| `backend/tests/test_modelstudio.py` | Registration, catalog, env (local + SSH), options, cost, pinning (chat + orchestrator paths), store overlay |

## Env contract

`_harness_env()` (applied locally and forwarded through the SSH wrapper):

| Variable | Value |
|---|---|
| `ANTHROPIC_BASE_URL` | `https://dashscope-intl.aliyuncs.com/apps/anthropic` (override: `MODELSTUDIO_ANTHROPIC_BASE_URL`, e.g. another region or the Coding Plan endpoint) |
| `ANTHROPIC_AUTH_TOKEN` | `DASHSCOPE_API_KEY` (missing key → the session fails to start with a clear error) |
| `ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU,FABLE}_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL` | the chosen model — background calls and subagents never ask DashScope for a `claude-*` id |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | `1` (no telemetry, error reporting, update checks) |
| `CLAUDE_CODE_MAX_CONTEXT_TOKENS` | the model's window when known (the CLI assumes 200K for unknown models, which skews auto-compact) |
| `CLAUDE_CODE_EXTRA_BODY` | the `thinking` option (below); blank otherwise |
| `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, `ANTHROPIC_CUSTOM_HEADERS`, `CLAUDE_CODE_USE_{BEDROCK,VERTEX,FOUNDRY}` | **blanked** (`""`) so the Anthropic credentials never reach a third party |

The model passed to the CLI is never empty or a Claude id: `None`, `default`, `sonnet`,
`claude-…` and similar become the harness default (`glm-5.1`).

## Provider pinning (why resume works)

The JSONL is byte-for-byte Claude's (`context/<id>.jsonl`), so `detect_provider()` says `claude` —
which is correct for parsing, but a resume would then run under real Claude. The harness a session
belongs to is therefore recorded in the per-session config:

- `SessionPool.send()` calls `_pin_provider(sm)` on every `TurnComplete`; the first one writes
  `provider` into `context/<sdk-id>.config.json` unless a provider is already pinned. It covers the
  chat tabs (`start_turn` / `send_or_queue`) and the orchestrator (`BackgroundAgentRunner` →
  `pool.send()`), for **every** harness. Non-UUID ids and unregistered providers are skipped.
- `build_session_config()` already treats a pinned provider as authoritative over detection and
  the global default.
- `SessionStore` overlays the pinned provider on listings (`list_sessions`, `get_session_info`,
  `get_session`, `get_messages_paginated`) when the pinned harness's adapter reads the detected
  format (`isinstance(spec.adapter_loader(), type(detected))`). Only sessions whose format is
  shared pay for it: one `stat` per session, a JSON read only when the config file changed.

`ModelStudioAdapter` is never passed to `register_provider()`, so format detection is unaffected.

## Catalog and options

Models: DashScope's OpenAI-compatible `GET …/compatible-mode/v1/models` (same key; the Anthropic
endpoint has no models route), filtered with the Qwen catalog's `filter_live_ids()` and then to the
families the Anthropic endpoint serves — `glm-*`, `deepseek-*`, `kimi-*`, `qwen3*`, `qwq` (one tiny
request each, 2026-10-07). Curated ids first (GLM-5.1/5.3/5.2, DeepSeek V4 Pro/Flash, Kimi K2.7
Code, Qwen3.7/3.6 Plus, Qwen3 Coder Plus), then the rest by vendor. Unreachable list → the curated
list plus a `warnings` entry. Context windows and thinking support come from the Qwen catalog's
DashScope family table (`qwen_model_traits`). Default model: `glm-5.1`.

| Option | Kind | Effect |
|---|---|---|
| `thinking` | toggle | on → `CLAUDE_CODE_EXTRA_BODY={"thinking":{"type":"enabled","budget_tokens":N}}`; off → `{"type":"disabled"}` plus `--thinking disabled`; unset → the CLI's own `{"type":"adaptive"}` and the model decides. Hidden for models without thinking (coder models); always-thinking models (GLM-5.3, Kimi K2.7) ignore off. |
| `thinking_budget` | number 1024–31000 | `budget_tokens` when thinking is on (default 16000; the CLI asks for `max_tokens` 32000). |
| `todo_tools` | toggle | Same as the claude harness (`CLAUDE_CODE_ENABLE_TODO_TOOLS`). |

No `effort` (the CLI always sends `output_config.effort`; Model Studio does not document it for
these models) and no `fallback_model` (the CLI's fallbacks are Anthropic models).

## Cost

The CLI prices every token at Anthropic rates (`modelUsage.costBasis: "unknown"`), so the harness
reports `TurnComplete.cost = None` and keeps the session cost at 0; the UIs hide a zero cost.
DashScope bills by its own price list.

## Installation

Nothing to install beyond the claude harness's runtime: `--with-modelstudio` (`-WithModelStudio`)
installs `backend/requirements-claude.txt` (claude-agent-sdk with its bundled CLI), creates the
`.claude_config/` links ([claude-code](claude-code.md)) even without `--with-claude`, and checks
`DASHSCOPE_API_KEY`. `install/doctor.sh` reports it as its own row group
([installation](../infrastructure/installation.md)).

## SSH working directories

Same wrapper as Claude (`_write_ssh_wrapper`), its own ControlMaster prefix (`modelstudio`), the env
above forwarded inline, and `CLAUDE_CODE_OAUTH_TOKEN=''` / `ANTHROPIC_API_KEY=''` instead of the
OAuth token the claude harness forwards.

## HarnessSpec values

`name="modelstudio"`, label "Claude Code · Model Studio", `comm_prefix="claude"` (same binary),
`ssh_control_path_prefix="modelstudio"`, `jsonl_path_resolver` = Claude's, `env_keys=("DASHSCOPE_API_KEY",)`,
`requirements_file="requirements-claude.txt"`, `cli_binary="claude"`. Registered after
`manager.claude.adapter` in `_ADAPTER_MODULES`.

## Landmines

- **The SDK merges `os.environ` under `ClaudeAgentOptions.env`** (`{**os.environ, **options.env}`):
  deleting a key from `options.env` does not remove an inherited variable. Blank it instead (the
  CLI treats empty as unset). Found live: the OAuth token was present (though not sent) in the
  child env until this was fixed.
- With `ANTHROPIC_AUTH_TOKEN` set the CLI authenticates with it even when `.credentials.json`,
  `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` exist (mock-verified) — the blanking is defence in depth.
- The CLI treats unknown model ids as adaptive-thinking models: `--max-thinking-tokens` has no
  effect, hence `CLAUDE_CODE_EXTRA_BODY` for the thinking option.
- `server_info["models"]` of a Model Studio session lists DashScope models under Claude aliases;
  `_record_cli_models()` is a no-op here so it never pollutes the claude catalog.
- Each request carries Claude Code's full system prompt (~15K tokens) and Claude Code's request
  metadata (device id, session id) to the third-party endpoint.
- A session started before provider pinning existed and run under this harness would resume as
  `claude`; pin it by hand (`PUT /api/sessions/{id}/config {"provider":"modelstudio"}`).
- A fresh session's harness comes from the global default (`assistant_config.json` `provider`, which
  defaults to `claude`); `ASSISTANT_PROVIDER` alone does not switch it.
- Memory works exactly as in the claude harness (inherited): auto-memory off in the repo and the
  shared `MEMORY.md` block appended ([claude-code](claude-code.md)). Live 2026-10-08 before the
  change, GLM-5.1 followed auto-memory's own format (flat note at the root of `context/memory/`,
  pointer appended to `MEMORY.md`); after it, it quoted `MEMORY.md` and saved the fact into the
  existing wiki note.

## History

- 2026-10-07 — Added (fifth harness). Live check: GLM-5.1 Bash tool turn, close + resume under
  the default global config (resumed as `modelstudio`, context kept), DeepSeek V4 Pro turn with
  thinking on (69 thinking deltas), `cost: null`, provider pinning written on the first turn.
