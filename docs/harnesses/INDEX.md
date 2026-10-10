# Harnesses

The agent CLIs that run Archie's chat sessions. Each harness plugs into the backend through one
`HarnessSpec` in `backend/manager/registry.py` plus a subpackage under `backend/manager/<name>/`
(JSONL adapter + session manager); everything else — the pool, the chat WebSocket, the history list,
the settings pickers — goes through the registry. Start with `registry.md`; the per-harness docs
cover storage layout, CLI flags, JSONL quirks and landmines.

- [registry.md](registry.md) — HarnessRegistry / HarnessSpec, ProviderAdapter, normalized events, SessionStore across harnesses, dispatch sites, carve-outs, how to add a harness (incl. Codex recon notes)
- [claude-code.md](claude-code.md) — Claude Code over claude-agent-sdk: `.claude_config/` layout, session options, JSONL, SDK pin and upgrade stages, auth, Chrome flag
- [qwen-code.md](qwen-code.md) — Qwen Code: spawn-per-turn stream-json CLI, `~/.qwen` symlinks into `context/`, JSONL normalization, model catalog, lazy SDKs, landmines
- [gemini-cli.md](gemini-cli.md) — Gemini CLI (pinned 0.63.0): `--prompt` per turn, API-key-only auth since the 2026-06-18 oauth-personal shutdown, env-templated workspace settings (retention off), catalog/options (thinking level/budget, approval), JSONL log replay, interrupt/process reaping, landmines
- [codex-cli.md](codex-cli.md) — OpenAI Codex: one persistent `codex app-server` per session (JSON-RPC over stdio), dedicated vs shared `CODEX_HOME`, rollout storage + adapter (three generations), catalog/options, landmines (auth rotation, AGENTS.md size)
- [authentication.md](authentication.md) — Settings → Accounts: every sign-in method per harness and API service (link logins driven on the headless server, credentials paste, API keys, sign out), where each credential lives, status detection, the `context/.env` key manager and its trust model
- [model-studio.md](model-studio.md) — Claude Code · Model Studio: the claude harness pointed at Alibaba Model Studio's Anthropic-compatible endpoint (GLM, DeepSeek, Kimi, Qwen); env contract (no Anthropic credential leak), provider pinning on the first turn, catalog/options, why not dsh/ZCode
