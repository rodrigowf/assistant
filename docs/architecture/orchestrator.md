---
name: orchestrator
category: archie/architecture
tags: [orchestrator, agent-loop, tools, providers, system-prompt, background-runner, notifications, run-script, persistence, jsonl]
created: 2026-02-23
modified: 2026-10-08
summary: The orchestrator agent — session vs agent loop, model providers, system prompt, the 24 tools, background runner, persistence.
source: curated (consolidated from memory notes assistant/architecture/orchestrator-vision.md, assistant/architecture/permissions_branch_architecture.md, assistant/architecture/project-overview.md, assistant/infrastructure/features_and_integrations_summary.md; verified against code 2026-10-06)
references:
  - system-overview.md
  - agent-sessions.md
  - backend.md
  - memory-and-search.md
  - ../voice/architecture.md
  - ../voice/lifecycle.md
  - ../voice/prompt-budget.md
  - ../voice/openai-realtime.md
  - ../voice/qwen-omni.md
  - ../voice/gemini-live.md
  - ../integrations/skills.md
  - ../overview/archie.md
  - ../infrastructure/installation.md
---

# Orchestrator

The orchestrator is Archie's coordinating agent: the conversation Rodrigo talks to (text or voice)
that opens, drives and reads coding-agent sessions, searches past conversations and memory, edits
files, and runs a few allowlisted scripts. It is a **hand-written agent loop** calling model APIs
directly (Anthropic, OpenAI) — it does not use the Claude Code SDK and has no shell of its own;
real work is delegated to agent sessions ([agent-sessions.md](agent-sessions.md)).

Lives in `backend/orchestrator/`; its socket is `WS /api/orchestrator/chat`
(`backend/api/routes/orchestrator.py`).

> Work in progress on 2026-10-06: `prompt.py`, `session.py`, `tools/search.py`,
> `tools/agent_sessions.py` and `backend/api/routes/orchestrator.py` had uncommitted edits (the
> `switch_conversation` tool and search changes). This doc describes that working tree.

## File map

| Path | Role |
|---|---|
| `backend/orchestrator/session.py` | `OrchestratorSession` — one conversation: JSONL, model/provider, voice lifecycle, notification drain, summaries |
| `backend/orchestrator/agent.py` | `OrchestratorAgent` — the model ↔ tools loop (`MAX_TOOL_LOOPS = 20`) |
| `backend/orchestrator/prompt.py` | `build_system_prompt()` and its sections |
| `backend/orchestrator/config.py` | `OrchestratorConfig`, `Provider` enum, static model table, default/audio/text-fallback model resolution |
| `backend/orchestrator/runner.py` | `BackgroundAgentRunner`, `NotificationQueue`, `Notification`, `AgentTurnHandle` |
| `backend/orchestrator/persistence.py` | `HistoryLoader` (JSONL → API history) and the JSONL writer |
| `backend/orchestrator/tools/` | `ToolRegistry` (`__init__.py`) and the tool modules |
| `backend/orchestrator/providers/` | `base.py` (`ModelProvider`), `anthropic.py`, `openai_text.py`, voice providers, `voice_registry.py`, `discovery.py` (live model lists) |
| `backend/orchestrator/summary_cache.py`, `token_budget.py` | History summary cache and token budgets for the voice prompt |
| `backend/orchestrator/voice_*.py`, `audio_*.py` | Voice relay, VAD, persister, timeouts, recorder ([voice architecture](../voice/architecture.md)) |
| `backend/api/routes/orchestrator.py` | WS handler: `start`, `voice_start`, `send`, `inject_text`, `send_audio`, `set_model`/`get_model`/`get_models`, `voice_event`, `voice_audio_in`, `voice_recording_chunk`/`_end`, `compact`, `interrupt`, `voice_stop`, `stop`; wake callback |

## OrchestratorSession vs OrchestratorAgent

- **`OrchestratorAgent`** (`agent.py`) is the loop for one user turn: append the prompt to history,
  build the system prompt, call `provider.create_message(messages, tools, system)`, stream
  `TextDelta`/`TextComplete`/`ToolUseStart`, then execute every requested tool concurrently
  (`_execute_tools_streaming`, with `ToolProgressEvent` heartbeats every 5 s so the socket never
  goes quiet), append the results, and loop. At most **20** model↔tool rounds per turn
  (`MAX_TOOL_LOOPS`).
- **`OrchestratorSession`** (`session.py`) owns everything around it: the JSONL file
  (`context/<jsonl_id>.jsonl`; `jsonl_id` is the resumed id, else the `local_id`), loading history
  on resume, model switching (`set_model`, logged as `model_switch`), compaction, the voice
  lifecycle (`VoiceLifecycle` state machine, relay, persister), the `BackgroundAgentRunner`, and the
  `_busy_lock`. Every `send()` / `send_audio()` body runs under `_busy_lock`; `is_busy` exposes it.

At most **one orchestrator** is active at a time (`SessionPool._orchestrator`). A `start` for a
different `local_id` while one is active gets `error: orchestrator_active`; clients attach to the
active one instead. One conductor avoids conflicting commands to the same agent sessions.

## Model providers

`ModelProvider.create_message(messages, tools, system)` → async iterator of orchestrator events
(`backend/orchestrator/types.py`). Provider SDKs are optional and lazily imported
(`providers/__init__.py`): without `anthropic` or `openai` installed, those models just disappear
from the picker.

| Provider | Module | Used for |
|---|---|---|
| `AnthropicProvider` | `providers/anthropic.py` | Claude models, streaming text |
| `OpenAITextProvider` | `providers/openai_text.py` | OpenAI chat models, including audio-input models (`gpt-audio` family) for voice messages |
| Voice providers | `providers/openai_voice.py`, `qwen_voice.py`, `gemini_voice.py` (+ `voice_base.py`, `gemini_voice_base.py`, `voice_registry.py`) | Realtime voice — see [voice/architecture.md](../voice/architecture.md), [openai-realtime](../voice/openai-realtime.md), [qwen-omni](../voice/qwen-omni.md), [gemini-live](../voice/gemini-live.md) |

Model selection: a new session starts on `resolve_default_model()` — the `default_model` from
`assistant_config.json`, then the `ORCHESTRATOR_MODEL` env var, then `DEFAULT_MODEL_ID`
(`gpt-audio`). Turns that carry audio use `default_audio_model` (fallback `gpt-audio`). An
audio-only model refuses text-only turns, so a typed turn on one is routed to
`TEXT_FALLBACK_MODEL_ID` (`gpt-4o`) for that turn only. Retired ids (`gpt-4o-audio-preview`, ...)
are rejected. History summaries use `summarizer_model` (default `DEFAULT_SUMMARIZER_MODEL`).

Both text and voice use the same `ToolRegistry`: `get_definitions()` returns Anthropic format,
`get_openai_definitions()` OpenAI function-calling format, so every tool works in every mode.

## System prompt (`build_system_prompt()` in `prompt.py`)

Sections, in order: role → date/time → **This Conversation** (the orchestrator's own JSONL path, so
it can hand it to an agent for digestion) → **active sessions** (titles, in-flight turns, pending
permissions) → **available MCPs** → **memory** → **scripts allowlist** → guidelines (delegation,
background events, searching) → recent history (voice only; text mode passes history as messages).

Files injected from `context/memory/`:

| File | Limit | When |
|---|---|---|
| `MEMORY.md` (root index of the wiki) | `MAX_MEMORY_INDEX_CHARS = 40000`, truncated with a "read from line N" hint | Always |
| `ORCHESTRATOR_MEMORY.md` (private orchestrator memory, no frontmatter; a new install seeds it from `install/ORCHESTRATOR_MEMORY.md` — identity + first-run onboarding, see [installation](../infrastructure/installation.md#a-new-archies-first-conversations)) | `MAX_MEMORY_CHARS = 12000` | Always |
| `ORCHESTRATOR_MEMORY_<provider>.md` (e.g. `_qwen`, `_gemini`) | `MAX_MEMORY_CHARS` | Only in a realtime voice session on that provider |
| `ORCHESTRATOR_SCRIPTS.md` (`run_script` allowlist) | `MAX_SCRIPTS_CHARS = 12000` | Always, verbatim |

The memory section also teaches the add/update flow for the wiki (see
[memory-and-search.md](memory-and-search.md)). The voice prompt is built by
`OrchestratorSession.get_session_update()` with a token-budgeted history (verbatim recent window +
summary of older turns, from `token_budget.py` and `summary_cache.py`); details in
[voice/prompt-budget.md](../voice/prompt-budget.md).

## Tools

Registered by decorator in `backend/orchestrator/tools/*.py`; `OrchestratorSession.start()` imports
each module so they self-register. Handlers receive a `context` dict (pool, store, session, ...);
`ToolRegistry.execute()` drops unknown arguments and turns exceptions into `{"error": ...}`.
Tools can declare a `schema_builder` to inject live state (e.g. the MCP list) into their schema.

| Module | Tool | What it does |
|---|---|---|
| `agent_sessions.py` | `list_agent_sessions` | Live agent sessions with status, `session_id` (local) and `sdk_session_id` |
| | `open_agent_session` | Start a session, or resume one with `resume_sdk_id`; optional `mcp_servers` (validated). Config comes from `build_session_config()` like the UI's "+"; returns the resolved config |
| | `resume_conversation` | Reopen a past agent conversation (Claude/Qwen/Gemini) from a search result's `session_id` |
| | `switch_conversation` | Leave this conversation and continue in one of the orchestrator's own past conversations: ends voice if active, stops this orchestrator, sends the client an `orchestrator_switch` frame (uncommitted on 2026-10-06; protocol §6.11a) |
| | `close_agent_session` | Close a live session |
| | `read_agent_session` | Last N persisted messages (default 5) plus a `live` block: `running` (turn id, live event tail) or `idle` |
| | `send_to_agent_session` | Fire-and-forget: returns `{turn_id, session_id, status: "running", started_at}` at once (below) |
| | `interrupt_agent_session` | Cancel a running turn (SDK interrupt); the runner reports it as `cancelled` |
| | `respond_to_agent_permission` | Answer a pending `ExitPlanMode` request (`allow`/`deny`, optional message) |
| | `list_history` | Past sessions with type (`agent` / `orchestrator`) |
| `assistant_config.py` | `get_assistant_config` | Coding-agent settings the next session inherits (working dir + history, harness/model, MCPs, chrome flag) |
| | `update_assistant_config` | Change only those coding-agent fields; voice/orchestrator runtime settings are deliberately not exposed |
| `files.py` | `read_file` | Any path (relative = repo root), optional `start_line`/`end_line`, 100 KB cap with a "continue at line X" marker |
| | `write_file` | Full overwrite; creates parent directories |
| `run_script.py` | `run_script` | Run one allowlisted script (below) |
| `search.py` | `search_history` | Find past conversations (all harnesses) — [memory-and-search.md](memory-and-search.md) |
| | `list_conversations` | Browse conversations by time / kind / title words |
| | `grep_conversation` | Exact words inside one conversation → turn numbers |
| | `read_conversation` | Read turns of a past conversation (around a hit, a range, or one long turn paged) |
| | `search_memory` | Find memory notes by keyword and meaning |
| | `browse_memory` | List one folder of the memory wiki with its `INDEX.md` |
| | `grep_memory` | Exact words across memory notes |
| `voice_control.py` | `end_voice_session` | Hang up the realtime voice call |
| `audio_playback.py` | `listen_recording` | Replay a time range of a recorded voice session into the live call |

24 tools in total. `peek_agent_session` and `check_index_health` no longer exist (merged into
`read_agent_session` / removed on 2026-06-04, `20ccfdd`).

## Fire-and-forget agent turns (`backend/orchestrator/runner.py`)

`send_to_agent_session` calls `BackgroundAgentRunner.spawn()` instead of awaiting the agent:

1. The runner creates one `asyncio.Task` per turn (`_drive`) that iterates `pool.send()` — so the
   turn is broadcast to every client watching that agent tab like any other turn. Two turns on the
   same session serialize on the pool's per-session lock; different sessions run in parallel.
2. Events are buffered in a per-turn ring (200 entries) that `read_agent_session` shows as the live
   tail; pending permission ids are tracked for the prompt's active-sessions section.
3. Exactly one terminal `Notification` per turn — `succeeded`, `failed`, `cancelled` or `timeout`
   — is pushed onto the conversation's `NotificationQueue`. The timeout counts time **without
   progress** (`idle_timeout`, default 1800 s with no text, tool call, tool result or permission
   event; `SessionStalled` notices don't count, a pending permission is never idle), so a working
   agent is never stopped however long the task; `max_turn_seconds` is an optional hard cap (off by
   default). Until 2026-10-10 it was a fixed 600 s wall clock, which killed a session mid-edit.
   `TurnAbandoned` (no SDK messages for 240 s) is retried once.
   A cancelled `pool.send()` emits no `turn_complete`, so every turn the runner ends tells the
   agent's open tabs itself (`broadcast_session`): `error{turn_timeout}` then
   `status{interrupted}` on timeout, `status{interrupted}` on cancel, `status{retrying}` before the
   abandoned-turn retry, `error{upstream_wedged}` / `error{send_failed}` on failure — the same
   frames `pool._drive_turn` sends for chat-driven turns. Without them a tab kept showing the turn
   as running ("Using tools…") forever.
4. At the top of the next `OrchestratorSession.send()` (under `_busy_lock`) the queue is drained:
   each notification is persisted as a `background_notification` JSONL line (tied to
   `origin_tool_use_id`), and rendered as status lines prepended to the prompt:
   `[SESSION 1a2b3c4d ("title"), event: turn 5e6f7a8b succeeded, duration=42.0s, cost=$0.1234]`.
   Lines carry status only; the model calls `read_agent_session` for content. When a turn did not
   succeed the block adds an instruction to tell the user it stopped and why before anything else
   (2026-10-10: on a timeout the model had answered with its own summary instead).
5. **Wake callback**: `backend/api/routes/orchestrator.py` installs a callback on the queue. When a
   notification arrives and `session.is_busy` is false, it schedules a synthetic empty-prompt turn
   so the model can react without the user typing; a busy orchestrator just drains it next turn.
   An empty prompt with nothing pending returns immediately. **Voice sessions get no synthetic
   wake** — notifications wait for the next turn.

**Agent turns belong to the conversation, not to the session object.** The runner and its queue
live in `pool.agent_runtimes` (`AgentRuntimes`, keyed by the conversation's `jsonl_id`), and every
`OrchestratorSession` of that conversation acquires the same pair. `OrchestratorSession.stop()` —
closing the orchestrator tab, `switch_conversation`, a voice provider/model rebuild, a voice start
dropping a dying session — therefore **never stops an agent turn** (it called `cancel_all()` until
2026-10-10); it only releases its own wake callback (`release_wake_callback(owner)`, so an old
session stopping late can't unhook its successor's). Finished turns' notifications wait in the
queue; when the conversation gets a session again its route installs the wake callback and, if
anything is pending, runs the synthetic turn at once. Only an explicit `interrupt_agent_session`
(`runner.cancel`) or a backend shutdown stops a delegated turn. A runner keeps its last 100
finished turns (never a session's latest) for `read_agent_session`.

The runner imports only the pool, store and manager event types (not the session or agent), so it
is unit-tested with mock pools (`backend/tests/test_runner.py`,
`backend/tests/test_orchestrator_stop_keeps_agents.py`). Any new wake source must gate on
`is_busy` the same way.

## `run_script` and its allowlist

`run_script(script, args)` runs one script through `context/scripts/run.sh` (the project venv, no
sandbox), with a 300 s timeout and output clipped to 20 000 chars, returning exit code, stdout and
stderr. Only paths listed in `context/memory/ORCHESTRATOR_SCRIPTS.md` may run: the file is plain
markdown, one `### name` heading per script plus a fenced block whose `path:` line is what the
parser (`_PATH_LINE_RE`) reads; `args:` and examples are for the model. The orchestrator curates the
file with `write_file`, and Claude sessions add an entry whenever they create a reusable single-shot
script. Rule: `run_script` is for one self-contained call (toggle a lamp, one browser `look`);
anything open-ended, multi-step or needing judgment goes to an agent session.

## Conversation resume and switching

- Agent conversations found by `search_history` / `list_conversations` reopen *alongside* the
  orchestrator with `resume_conversation` (same as `open_agent_session(resume_sdk_id=...)`); a new
  agent tab appears on every client through the pool's `agent_session_opened` watcher event.
- The orchestrator's own past conversations cannot be opened as agent sessions; `switch_conversation`
  closes the current orchestrator and tells the requesting app (voice owner, else last input socket)
  which one to reopen; in a voice call the call reconnects inside it. The model is told to say a
  short "switching now" first because nothing after the call is heard. Spec:
  `docs/specs/12-client-protocol.md` §6.11a.
- Finding or summarizing a conversation is not resuming it — the prompt tells the model to reopen
  only when asked.

## JSONL persistence

Orchestrator conversations are JSONL files in `context/` like agent sessions, so they appear in the
history list, are searchable, and can be resumed. `HistoryLoader` (`persistence.py`) rebuilds API
history from them (and recovers `}{` concatenations left by an old crash-before-flush bug).

| Entry `type` | Written when |
|---|---|
| `orchestrator_meta` | Session start: `orchestrator: true`, `session_id`, `model`, `provider` (+ voice info) — marks the file as an orchestrator conversation |
| `user` | Typed prompt; voice transcript (`source: "voice_transcription"`, text prefixed `[voice]`); audio message (`source: "audio_input"`); silent inject (`source: "shared_inject"`) |
| `assistant` | Model reply; voice replies carry `source: "voice_response"` |
| `tool_use` / `tool_result` | Tool calls (voice ones carry `source: "voice"`) |
| `background_notification` | Each drained runner notification |
| `voice_interrupted` | The user barged in on a voice reply (`voice_stopped` / `voice_owner_active` are WebSocket broadcasts, not JSONL lines — [voice/lifecycle.md](../voice/lifecycle.md)) |
| `model_switch` | `set_model` |
| `compact` | Manual compaction (summary text) |

Voice-side writes go through `backend/orchestrator/voice_persister.py`.

## Pitfalls

- **Never loop on `send_to_agent_session` waiting for a reply.** It returns immediately; the result
  arrives as a background event next turn. The tool description says so because models tried.
- **Use the `session_id` returned by `open_agent_session` in the next `send_to_agent_session`**, not
  an older one; `sdk_session_id` is only for resuming.
- **Open a new session for a distinct task** instead of repurposing a busy one — repurposing loses
  that session's context.
- **Delegate digestion** of a long conversation into memory to an agent session (pass the absolute
  JSONL path from the "This Conversation" section) rather than reading it inline.
- **`update_assistant_config` cannot touch voice settings on purpose**: changing the voice
  model/provider mid-call would disrupt the orchestrator's own session.
- **Large prompt files**: `MEMORY.md` over 40 000 chars or the private memory over 12 000 chars is
  truncated in the prompt — keep the root index lean and put detail in sub-indexes.

## History

- 2026-02: built in four phases (multi-tab UI, orchestrator foundation, agent-control tools, voice
  over WebRTC). The original plan of a lightweight model wrapper became the modular provider system.
- 2026-04/05: fire-and-forget turns, `BackgroundAgentRunner`, wake callback, permission tools
  (`permissions` branch).
- 2026-06-04 (`20ccfdd`): tool surface tightened for realtime voice (peek merged into read,
  `check_index_health` removed, config tool scoped to coding-agent fields).
- 2026-10-06: search/navigation tools rebuilt (`934fe7d`), `resume_conversation` (`255309c`),
  `switch_conversation` (in progress).

Related: [system overview](system-overview.md), [backend](backend.md), [what Archie is](../overview/archie.md), [skills](../integrations/skills.md) (which skill to load when delegating).
