---
name: decisions
category: archie/overview
tags: [decisions, adr, architecture, rationale, ideas]
created: 2026-02-23
modified: 2026-10-10
summary: Cross-cutting architecture decisions with dates and reasons, plus ideas considered but not planned.
source: curated (consolidated from memory notes assistant/architecture/project-overview.md "Architecture Decisions Log", assistant/architecture/orchestrator-vision.md, docs/projects/frontend-refactor/README.md decisions D1–D8, and the writers' reports while building docs/, 2026-10-07)
references:
  - archie.md
  - repo-layout.md
  - ../history/timeline.md
  - ../architecture/system-overview.md
  - ../architecture/agent-sessions.md
  - ../architecture/orchestrator.md
  - ../architecture/memory-and-search.md
  - ../harnesses/registry.md
  - ../voice/architecture.md
  - ../voice/lifecycle.md
  - ../voice/prompt-budget.md
  - ../voice/wake-word.md
  - ../clients/android.md
  - ../clients/browser-extension.md
  - ../infrastructure/topology.md
  - ../infrastructure/context-sync.md
  - ../infrastructure/deployment.md
  - ../operations/refactor-methodology.md
---

# Decisions

The decisions that shape more than one part of the system. Subsystem-level choices live in each
subsystem's doc; the frontend rebuild's own log (D1–D8) is in
[projects/frontend-refactor/README.md](../projects/frontend-refactor/README.md). Dates are when the decision took
effect; see the [timeline](../history/timeline.md) for the surrounding events.

## Data and repos

| Date | Decision | Why | Detail |
|---|---|---|---|
| 2026-02 | **Private data in a separate repo** (`context/`), gitignored by the public framework; general tooling in `shared/`, personal tooling directly in `context/` with symlinks to `shared/` | Share the framework without exposing anything personal; migrate by cloning two repos | [repo-layout](repo-layout.md) |
| 2026-02 | **Everything on disk is plain files** — JSONL, markdown, JSON, later SQLite | Readable, greppable, portable; matches "transparency over polish" | [system-overview](../architecture/system-overview.md) |
| 2026-04 | **Same install path (`~/assistant`) on every machine** | Claude/Qwen key session storage by the mangled cwd; same path = sessions resume on either machine | [topology](../infrastructure/topology.md) |
| 2026-04 | **`context/` synced live by rsync, code by git** | Conversations and memory change constantly and must be on both machines within seconds; code wants review and history | [context-sync](../infrastructure/context-sync.md) |
| 2026-05-19 | **context-sync never uses `rsync --delete`**; only observed deletes propagate | `--delete` raced with new files and deleted them | [context-sync](../infrastructure/context-sync.md) |
| 2026-06-07 | **Memory is a wiki**: folder ontology, frontmatter, cross-links, two-level index | A flat `MEMORY.md` stopped scaling; folded in the useful parts of the LLM-Wiki idea | [memory-and-search](../architecture/memory-and-search.md) |
| 2026-10-06 | **Search on SQLite (FTS5 + embeddings in numpy), multilingual model, LLM re-rank**; ChromaDB retired | Portable index files, Portuguese recall, measured gains on a frozen eval set | [memory-and-search](../architecture/memory-and-search.md), [`../history-search/RESULTS.md`](../projects/history-search/RESULTS.md) |
| 2026-10-07 | **Archie's documentation lives in `docs/` (public, versioned with the code)**, linked into memory as `context/memory/archie` | Docs were scattered, overlapping memory notes that went stale; versioning them next to the code keeps them honest, and the link keeps agents finding them through the wiki | [repo-layout](repo-layout.md) |

## Sessions and the orchestrator

| Date | Decision | Why | Detail |
|---|---|---|---|
| 2026-02 | **Stable client-minted `local_id` is the key everywhere**; the harness id is only for history/resume | The old flow changed a tab's id up to three times | [agent-sessions](../architecture/agent-sessions.md) |
| 2026-02 | **A custom orchestrator loop on the model APIs**, not another Claude Code instance or an agent framework | No shell on the coordinator; switchable models; the same tools in text and voice | [orchestrator](../architecture/orchestrator.md) |
| 2026-02 | **One active orchestrator** | One conductor; no conflicting commands to agent sessions. Can be relaxed later | [orchestrator](../architecture/orchestrator.md) |
| 2026-02 | **Agent tabs auto-appear** when the orchestrator opens a session | You can watch and step into delegated work | [orchestrator](../architecture/orchestrator.md) |
| 2026-04/05 | **Fire-and-forget delegation**: `send_to_agent_session` returns at once; completions arrive as notifications | The orchestrator (and voice) stays responsive while agents work for minutes | [orchestrator](../architecture/orchestrator.md) |
| 2026-04/05 | **Permission popups are the backstop, conversation is the checkpoint**: agents announce intent before a gated tool; typing a reply denies with your text | Guide agents in prose instead of yes/no dialogs | [agent-sessions](../architecture/agent-sessions.md) |
| 2026-05-10 | **Turns are owned by the pool, not by a socket** | Many devices watch one session; a closed tab never kills work | [agent-sessions](../architecture/agent-sessions.md) |
| 2026-05-15 | **Pluggable harnesses through a registry** (Claude Code, Qwen Code, Gemini CLI) | Choice of provider; adding a harness is one spec plus a manager | [harnesses](../harnesses/registry.md) |
| 2026-08-28 | **Browser control is done by Claude sessions with a skill**, not by orchestrator tools | Browsing needs judgment and many steps; the orchestrator keeps only a one-shot `run_script` path | [browser-extension](../clients/browser-extension.md) |
| 2026-08-24 | **Claude CLI auth via a long-lived `CLAUDE_CODE_OAUTH_TOKEN` in `context/.env`**, not copied credential files | Copied OAuth grants broke the other machine's refresh | [deployment](../infrastructure/deployment.md) |
| 2026-10-10 | **The server owns the open set**: "Open now" on every device is the pool; a conversation closed anywhere closes its view everywhere at once (even on screen); the pool survives restarts (persisted, restored); automatic starts only `reattach` | Devices kept "Stopped" tabs with nothing behind them and re-created closed sessions; Rodrigo's rule: the backend is the ground truth and all UIs follow it immediately | [spec 12 OPEN-1..4](../specs/12-client-protocol.md), [agent-sessions](../architecture/agent-sessions.md#sessionpool-backendapipoolpy) |
| 2026-10-10 | **Delegated agent turns belong to the conversation, not the orchestrator session**: ending the orchestrator never stops them; the runner stops a turn only after 30 min without progress | A fixed 600 s limit killed a working session mid-edit, and closing/switching the orchestrator cancelled every agent it had started | [orchestrator](../architecture/orchestrator.md#fire-and-forget-agent-turns-backendorchestratorrunnerpy) |

## Voice

| Date | Decision | Why | Detail |
|---|---|---|---|
| 2026-02 | **WebRTC voice goes client ↔ provider directly; the client mirrors events to the backend** | Lowest audio latency, while tools and persistence stay server-side | [voice/architecture](../voice/architecture.md) |
| 2026-05 | **Multiple realtime providers behind one contract**; non-WebRTC providers go through a backend WS relay | OpenAI, Qwen-Omni and Gemini Live differ wildly on the wire | [voice/architecture](../voice/architecture.md) |
| 2026-06-04 | **One canonical teardown (`end_voice`) and re-arm (`restart_voice`)** | Ad-hoc teardown paths left ghost state | [voice/lifecycle](../voice/lifecycle.md) |
| 2026-07-21 | **Voice is owned by one device**; others see it read-only | Broadcasting voice commands to every peripheral wedged the peers | [voice/lifecycle](../voice/lifecycle.md) |
| 2026-07-21 | **Prompt sizing: never hard-cap the assembled prompt, never force a lossless summary** | Truncation silently drops context; a lossless "summary" is a transcript and defeats the budget | [prompt-budget](../voice/prompt-budget.md) |
| 2026-07-21 | **Two-layer wake word**: permissive on-device Vosk, then a Whisper transcription confirm (fail-closed) | Fast detection without false triggers; SpeechRecognizer clipped the start of every phrase | [wake-word](../voice/wake-word.md) |

## Clients and process

| Date | Decision | Why | Detail |
|---|---|---|---|
| 2026-06-09 | **Structural refactors follow the working agreement**: source fidelity, parity tests before the change, on-device verification, per-increment approval | Tuned voice/audio code breaks in ways unit tests don't see | [refactor-methodology](../operations/refactor-methodology.md) |
| 2026-10-03 | **Rebuild web and Android from scratch on one design system** (frontend refactor D1–D8): rewrite the Android voice stack with every tuned constant ported verbatim; separate Views-only lite app for the A300M; one token source; built side by side, then cut over | Old apps looked dated and diverged; the A300M needs a minimal app | [`../frontend-refactor/README.md`](../projects/frontend-refactor/README.md), [android](../clients/android.md) |
| 2026-10-05 | **Old apps kept, frozen, under `legacy/`** and still served | Safe rollback; some devices still run the old app | [legacy-apps](../clients/legacy-apps.md) |
| 2026-10-05 | **Web is built on the laptop and rsynced; both dists always ship together** | The Jetson has no usable Node; a stale second dist caused "voice fails on web only" | [deployment](../infrastructure/deployment.md) |

## Ideas considered, not planned

Collected from the old design notes; none is scheduled.

- **Orchestrator:** several orchestrators delegating to each other; agent-to-agent messages without
  the orchestrator; agent presets (spawn with a given system prompt) and warm agent pools;
  streaming progress of long tools.
- **Frontend:** tab groups/workspaces, split view, drag-to-reorder tabs, background-tab
  notifications.
- **Memory:** a daily-log file; automated ingestion and categorization of conversations into notes;
  a reverse-reference maintenance tool; a fixed tag taxonomy (see
  [memory-and-search](../architecture/memory-and-search.md) "Open ideas").
- **Harnesses:** a Codex CLI harness (recon notes in [registry](../harnesses/registry.md)).
- **Security gaps known and accepted for a home LAN:** SSH password auth still enabled on the
  Jetson; the laptop's key has no `from=` restriction; one OAuth token shared by both machines.
