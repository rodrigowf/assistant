---
name: timeline
category: archie/history
tags: [timeline, changelog, history, milestones, incidents]
created: 2026-10-07
modified: 2026-10-10
summary: Dated milestones and incidents in Archie's development, February–October 2026, with commits where known.
source: curated (timeline entries collected from every memory note consolidated into docs/, 2026-10-07)
references:
  - ../overview/decisions.md
  - ../architecture/orchestrator.md
  - ../architecture/agent-sessions.md
  - ../architecture/memory-and-search.md
  - ../voice/architecture.md
  - ../voice/lifecycle.md
  - ../voice/wake-word.md
  - ../clients/android.md
  - ../clients/legacy-apps.md
  - ../clients/browser-extension.md
  - ../infrastructure/jetson-server.md
  - ../infrastructure/context-sync.md
  - ../infrastructure/deployment.md
  - ../infrastructure/ssh-remote-execution.md
  - ../operations/troubleshooting.md
  - wakeword-vosk-migration-2026-06/plan.md
---

# Timeline

What happened when. Incidents are marked **(incident)**; each rule they produced is in
[working-rules](../operations/working-rules.md) or [troubleshooting](../operations/troubleshooting.md).
Commit hashes are in the public repo unless they refer to the private context repo.

## 2026-02 — Foundations

- **02** — Orchestrator built in four phases: multi-tab UI, orchestrator foundation, agent-control
  tools, WebRTC voice. Dual `local_id` / `sdk_session_id` system. Public/private split (`context/`).
- **02-23** — TvServerHub Fire TV launcher, ADB control skills and the visualization system; YouTube Data API integration (`/youtube`).

## 2026-04 — Server hub and peripherals

- **04-14** — Old Android peripheral app and the old Safari 12 compat build in service; the iPad
  mini 2 uses `/compat/`.
- **04-15** — Companion app `com.assistant.device` deployed on the A300M.
- **04-17** — SSH remote execution (Jetson runs sessions on the laptop); SSH quoting bug fixed.
- **04-20** — **(incident)** Jetson crash: runaway systemd user-session spawn, SD card remounted
  read-only.
- **04-21** — Jetson rebuilt fresh with hardened units; first baseline image; SSH churn hardening
  (PR #38: `a122447`, `e660045`, `b837b73`).
- **04-22** — Unattended apt/anacron/motd timers disabled on the Jetson.
- **04-23** — fb0 three-layer display blanking; `nvargus-daemon` disabled; security audit clean.
- **04-25** — iPhone Pythonista photo server (v1.0 → v1.3 over the following days).
- **04-27** — **(incident)** A phone voice crash turned out to be a cleanup race after
  `insufficient_quota` → "capture logcat before theorizing".
- **04/05** — `permissions` branch: `can_use_tool` gating with the conversational checkpoint, stall
  watchdog, fire-and-forget `BackgroundAgentRunner` (`b5c5c80`, `f4ee1cf`), SIGKILL escalation,
  orphan reaper, warm search server. OpenAI Realtime WebRTC voice matured.

## 2026-05 — Providers and harnesses

- **05-01 → 05-16** — Qwen-Omni realtime voice; **05-15** Gemini Live voice; both behind
  `BaseVoiceProvider`.
- **05-10** — Chat turns became pool-owned; a closed socket no longer kills a turn (`9958ff0`).
- **05-14** — Google Photos Picker + Drive integration (`/google-photos`).
- **05-15** — Qwen Code as second harness (`f5eaa7d`; lazy provider SDKs `115a05b`; per-provider requirements and two-axis installer `3bab53d`); harness registry (`e2bf29b`), harnesses in subpackages (`3fe9c73`), Gemini CLI harness (`5c9cb0f`); Codex deferred. DashScope "Common
  error!" fixed with the tool-schema sanitiser (`4c38b23`). New Jetson baseline image — the
  thermal optimum and gold restore point.
- **05-16** — Gemini session discoverer (`989fd71`), `~/.gemini/tmp/<label>` → `context/` symlink (`24ac981`), `GEMINI.md` symlink (`1a1b0d9`).
- **05-17 / 05-19** — context-sync delete gating: first attempt failed; then per-path observed
  deletes, no more incremental `--delete`.
- **05-20** — Client-side Silero VAD for Qwen (extended to Gemini on 06-03).
- **05-23/24** — **(incident)** uvicorn anyio/watchfiles busy loop; fixed `be8514e` (Gemini stale
  model id `6a4712f`).

## 2026-06 — Refactors

- **06-04** — Voice-lifecycle refactor: `VoiceLifecycle`, `end_voice` / `restart_voice`, summary
  STALE-REUSE + pre-warm; wake after stop 60–90 s → 130 ms (`2f8c8ac`, `0f34923`). Speculative
  keepalive patches reverted (`f98bc24`) **(incident)** → "diagnose before patching". Orchestrator
  tools tightened for voice: peek merged into `read_agent_session`, `check_index_health` removed
  (`20ccfdd`). Old app voice lifecycle stable (`ef2aaae`); A300M debloat (125 packages);
  mic-amplitude-after-wake fix (`55037c2`).
- **06-05** — Duplicate-handshake guard; Gemini resumption handle cleared on teardown (`5eb7804`).
  Old compat app: remark-gfm inline reconstruction and gap-shim fixes.
- **06-06** — `voice_initiator` flag (`71f19ce`). Wake-word retune reverted (`2acf57c`, `51ba2e5`)
  → "don't touch the tuned constants".
- **06-07/08** — Memory reorganized into the folder ontology with frontmatter and cross-links.
- **06-09** — Voice A→H refactor (`5286baa` … `91b4e92`); wake-word refactor increments 1–9;
  the refactor working agreement written.
- **06-10** — Old Android `AssistantViewModel` split into controllers (`3d621fc`). Vosk wake-word
  migration V1/V2 (`482a311`, `ddfb53f`); V6 deferred. See
  [the migration plan](wakeword-vosk-migration-2026-06/plan.md).
- **06-12** — **(incident)** A 12 GB media project inside `context/` filled the Jetson disk; heavy
  projects moved out of `context/`.
- **06-14** — **(incident)** A claim about the system's name was "disproved" by a root grep that
  silently skipped the gitignored `context/` → "pass explicit `context/` paths".
- **06-17** — History indexer re-embedding everything every cycle, fixed with an mtime skip
  (`6e98e2b`). Android WS reconnect re-sends `start` for agent sessions (`b9646ee`).
- **06-18** — `claude-agent-sdk` floor raised to 0.1.81 (`3d24e80`) (anyio cancel-scope wedge fixed;
  monkey-patches removed).
- **06-30** — Ghost-voice fix: `end_voice` no longer bails early (`f5b339a`).

## 2026-07 — Wake word, multi-device voice

- **07-20/21** — **(incident)** `git reset --mixed` on the Jetson's main repo left stale files that
  the backend ran → normal git on the main repo; heredoc rule for remote `cd`.
- **07-21** — Two-layer wake word (Vosk → Whisper confirm) and same-mic talk capture
  (`dd5567f` … `9db8f37`); `gpt-4o-audio-preview` → `gpt-audio` (`254c818`); owner-scoped voice
  across devices (`b6d184f`, `afe77a4`); `session.update` self-heal on restart (`d4bc698`);
  voice prompt-budget fix (`3b0671d`). Old app markdown RenderThread crash fixed (`31c2fbf`).
- **07-22** — First voice → `run_script` lamp control (short spoken commands act on the
  apartment); verbatim budget 5k (`0888f13`); Vosk NoMatch backoff fix (`043340a`). A300M gets a
  static IP.
- **07-25** — **(incident)** Stale web dist on the Jetson ("voice fails on web, works on the
  phone"); experiments-don't-touch-the-app rule.
- **07-31** — OpenAI deferred `response.create` watchdog (`9ef6dc1`); **08-01** the same for Qwen
  and the WS relay.

## 2026-08 — Browser control, auth

- **08-24** — Long-lived `CLAUDE_CODE_OAUTH_TOKEN` in `context/.env` replaces copying credential
  files between machines.
- **08-25** — **(incident)** The venv had drifted to SDK 0.1.39; `rate_limit_event` parse errors
  killed sessions; the SDK floor is now documented as load-bearing
  ([handoff](HANDOFF-2026-08-25.md)).
- **08-27/28** — Browser-control Chrome extension; browser control moved from orchestrator tools
  to Claude sessions (`461308d`); the hub moved to a local daemon on port 8766. nvm exit-127 fix
  for SSH sessions (`_ssh.py`). Project skills found not to register as `Skill()`.

## 2026-10 — The rebuild

- **10-03** — Frontend refactor chartered: rebuild web and Android on one design system (D1–D8;
  D1 = rewrite the Android voice stack with tuned constants ported verbatim + parity tests).
  **(incident)** Six parallel agents hit the API limit → cap at four.
- **10-04** — **(incident)** A subagent's `git stash pop` overwrote the gitignored
  `assistant_config.json` → subagents never run git writes.
- **10-05** — Cutover: `apps/web` at `/` and `/compat/`; lite app 2.0.0 (100) installed in place on
  the A300M; main app on the POCO; old apps to `legacy/`. Same-day reorganizations: `apps/`,
  `shared/`, `legacy/`, then `backend/` and `infra/sync/` (`db7aa3e`). Browser extension moved
  with a pinned ID. **(incident)** VS Code exhausted the inotify budget and context-sync
  crash-looped → `.vscode/settings.json` excludes.
- **10-06** — History index moved to SQLite (`ab2f224`), then the evaluated search rebuild
  (`934fe7d`, deployed via `22cc377`): multilingual model, session summaries, LLM re-rank,
  navigation tools; ChromaDB retired ([results](../projects/history-search/RESULTS.md)).
  `resume_conversation` tool (`255309c`); `switch_conversation` in progress. **(incident)** Paid
  realtime evals drained the OpenAI credits that voice depends on → estimate cost first.
- **10-07** — Archie's documentation consolidated from ~45 scattered memory notes into `docs/`,
  linked into memory as `context/memory/archie`; the rebuild specs moved to `docs/specs/` and the
  workstreams to `docs/projects/`; memory reorganized (`people/`, `identity/`, `deployment/`).

- **10-09/10** — Agent-finished notifications (web + Android), floating voice controls, Settings →
  Accounts with every sign-in method and an `.env` key manager, live reload of visuals and memory
  docs, in-app links, the `create-viz` canvas skill; built in four parallel worktrees with review
  agents. **(incident)** The SPA catch-all served any file (`//etc/x`), `context/.env` included —
  fixed and deployed the same hour (`683c4f4`); then a cross-site request guard for the whole API.
  **(incident)** Probing `codex login` wiped the laptop's Archie Codex login (CLIs log out when a
  login *starts*) → logins run in a temp home. **(incident)** VS Code held ~51k inotify watches →
  five `context/` dirs unwatched; context-sync rebuilt (tombstones, manifest + reconcile, trash,
  mass-delete brake) and the laptop's watch limit raised. Android Notifications page found
  unreachable from Settings and fixed (`7fd6e99`). [Handoff](HANDOFF-2026-10-10.md).
- **10-10** — **(incident)** A delegated digest session was killed mid-edit by the runner's fixed
  600 s limit; the phone kept showing "Using tools…" and the orchestrator answered with its own
  summary. Fixed: delegated turns belong to the conversation (stopping the orchestrator never
  stops them), the limit counts time without progress (30 min), every runner-ended turn is
  signalled to the tabs, failed turns are reported (`b1dd5b0`, `df2025f`). **(incident)** Sessions
  closed on the web stayed "Open now" on the phone, and returning devices re-created closed
  conversations → **the server owns the open set** (spec 12 OPEN-1..4): `pool/live` is "Open now"
  on every device, closed anywhere = closed everywhere at once, the pool survives restarts
  (`state/open_sessions.json`), automatic starts only `reattach` (`a4768aa`).