---
name: memory-and-search
category: archie/architecture
tags: [memory, wiki, frontmatter, search, history-search, sqlite, fts5, embeddings, rerank, session-summaries, indexing, recall]
created: 2026-02-23
modified: 2026-10-09
summary: The memory wiki as a system, and the search stack over memory and conversation history (indexes, warm server, tools).
source: curated (consolidated from memory notes assistant/architecture/project-overview.md, assistant/architecture/permissions_branch_architecture.md, assistant/plans/memory_improvements.md, assistant/infrastructure/features_and_integrations_summary.md, context/memory/MEMORY.md, auto-memory project_indexer_full_reembed_fix_2026_06_17.md, project_history_search_rebuild_2026_10_06.md; verified against code 2026-10-06)
references:
  - system-overview.md
  - backend.md
  - orchestrator.md
  - ../projects/history-search/PLAN.md
  - ../projects/history-search/RESULTS.md
  - ../integrations/visualizations-and-sharing.md
  - ../integrations/skills.md
  - ../infrastructure/context-sync.md
  - ../infrastructure/deployment.md
  - ../operations/troubleshooting.md
  - ../harnesses/registry.md
---

# Memory and search

Archie remembers through two stores:

- **Memory** — a curated markdown wiki in `context/memory/` (private repo), written deliberately by
  agents and the orchestrator.
- **History** — every past conversation as JSONL (`context/*.jsonl` for Claude Code and the
  orchestrator, `context/chats/` for Qwen and Gemini, Codex rollouts under `context/codex/sessions/`
  — found through the Codex harness's session discoverer, keyed by thread id), never edited.

Both are searchable by keyword and meaning in English and Portuguese through two SQLite indexes in
`index/` and one shared search service. These docs are part of the memory wiki too:
`context/memory/archie` is a symlink to `docs/` (being added on 2026-10-06), and the backend follows
it when indexing, browsing and serving memory.

## The memory wiki

### Layout

```
context/memory/
├── MEMORY.md                 # root index: rules, folder ontology, frontmatter schema, key files per topic
├── <topic>/INDEX.md          # sub-index: full file list of that folder
├── <topic>/<sub>/<note>.md   # notes, each with YAML frontmatter
├── ARCHIVE.md                # retired index sections (not loaded into prompts)
├── ORCHESTRATOR_MEMORY.md    # orchestrator's private memory (no frontmatter)
├── ORCHESTRATOR_MEMORY_<provider>.md   # voice-provider-specific memory (qwen, gemini)
├── ORCHESTRATOR_SCRIPTS.md   # run_script allowlist
└── archie -> ../../docs      # this documentation tree
```

Top-level categories are semantic folders (the project itself, personal projects, people,
external references); `MEMORY.md` is the authoritative list of the ontology. Folders are cheap,
misplaced files are expensive: a note goes in the most specific matching folder, and a new folder
gets a section in its parent's `INDEX.md`.

### Two-level index

`MEMORY.md` stays lean: rules, ontology, and only each topic's *key* (start-here) files plus a
pointer to that topic's `INDEX.md`. The `INDEX.md` sub-indexes hold the full file lists. A file not
named in `MEMORY.md` may still exist — always open the topic's `INDEX.md`. When a whole area ships
or is parked, its index section moves to `ARCHIVE.md`.

### Frontmatter

Every note (not the indexes, `ARCHIVE.md`, or `ORCHESTRATOR_*.md`) starts with:

```yaml
---
name: <filename without .md>
category: <folder path>
tags: [<keyword>, ...]
created: <YYYY-MM-DD>       # best-guess true creation date
modified: <YYYY-MM-DD>      # bumped on every content change
summary: <one line, ≤ 20 words>
source: <session UUID + title> | curated (<reason>) | <URL>
references:
  - <relative path to another note>
---
```

Cross-references are inline relative links plus the `references:` list, kept **bidirectional**.
The `source:` field matters to search: it is how a conversation result can say "already saved to
memory in X" and a memory hit can say "written from conversation Y".

### Add/update flow

Taught to agents by `AGENTS.md` and to the orchestrator by its system prompt
([orchestrator.md](orchestrator.md)): (1) reuse before creating — extend the closest existing note;
(2) pick the most specific folder; (3) fill every frontmatter field; (4) cross-link both ways;
(5) add a line to the topic's `INDEX.md` in the same pass (touch `MEMORY.md` only for key files or
new categories). Write tools overwrite whole files, so read first. Digesting a long conversation
into memory is delegated to an agent session given the conversation's absolute JSONL path.

### What reaches prompts

Only the root `MEMORY.md` and `ORCHESTRATOR_MEMORY*.md` (plus `ORCHESTRATOR_SCRIPTS.md`) are
injected into the orchestrator's system prompt, with size caps (40 000 chars for `MEMORY.md`,
12 000 for the others — see [orchestrator.md](orchestrator.md)).
Agent sessions of every harness get `AGENTS.md` (as `CLAUDE.md` / `QWEN.md` / `GEMINI.md` /
`AGENTS.md`) through their CLI, and — when their working directory is the Archie repo — the live
`MEMORY.md` once per session (below). Everything else is read on demand.

### Every harness reads and writes the same memory

Sessions in the repo get `context/memory/MEMORY.md`, read when the session (Qwen: each spawn)
starts, with one "Memory" block saying that the wiki is the persistent memory and that writes
follow `AGENTS.md`'s rules. The block comes from `backend/manager/memory_context.py` (capped at 200
lines / 25 KB like Claude Code's auto-memory) unless the CLI loads the file itself:

| Harness | How `MEMORY.md` gets in | Built-in memory writer | Where writes land |
|---|---|---|---|
| Claude Code, Model Studio | `system_prompt.append` (after the gating prompt); the CLI's auto-memory is switched off with `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` | auto-memory off | file tools → `context/memory/` per the wiki rules |
| Qwen Code 0.25 | `--append-system-prompt` on every spawn | managed auto-memory off per run; 0.25 has no `save_memory` tool (`manage_memory`/`search_memory` are only declared in structured-recall mode) | file tools |
| Gemini CLI 0.63 | natively: its private project memory index is `~/.gemini/tmp/<label>/memory/MEMORY.md`, i.e. `context/memory/MEMORY.md` through the install symlink | none (0.63 has no `save_memory`; it edits memory files) | file tools; its prompt also offers `~/.gemini/GEMINI.md` for cross-project preferences |
| Codex 0.161 | `developerInstructions` on `thread/start` (one developer message at the head of the rollout; resume/fork reuse it) | `memories` feature off (default) | file tools |

Why Claude's auto-memory is off in the repo: its own instructions write flat notes in its own
frontmatter at the root of `context/memory/` and append pointers to the wiki's `MEMORY.md` (seen
live 2026-10-08; it overwrote `MEMORY.md` on 2026-10-03). `"autoMemoryEnabled": false` in
`.claude_config/settings.json` never reached SDK sessions, which load only the `project` and
`local` setting sources. Qwen's managed auto-memory has the same problem plus a model call per
turn ([qwen-code](../harnesses/qwen-code.md)). Sessions outside the repo keep each CLI's defaults.

### Serving

`/memory/<path>` serves raw files and `/api/memory/tree` the markdown tree, used by the clients'
memory browser. Readable links to a note for a device such as the iPad go through the markdown
reader page in `context/public/` — see [visualizations-and-sharing](../integrations/visualizations-and-sharing.md).

## Search stack

Rebuilt and evaluated on 2026-10-06 (design and measurements: [PLAN.md](../projects/history-search/PLAN.md),
[RESULTS.md](../projects/history-search/RESULTS.md)).

| Piece | Path | Role |
|---|---|---|
| History index | `backend/utils/history_index.py` → `index/history.sqlite3` | Extracts turns from every harness's JSONL, chunks, embeds, FTS5; hybrid search grouped by session |
| Memory index | `backend/utils/memory_index.py` → `index/memory.sqlite3` | Frontmatter-aware, heading-aware chunks of every note; hybrid search grouped by note; `browse`, `grep` |
| Session summaries | `backend/utils/session_summaries.py` → `index/session_summaries.sqlite3` | gpt-4.1-mini summary per session (title, summary, topics, names, decisions, EN+PT keywords), indexed as an extra chunk |
| Re-rank | `backend/utils/rerank.py` | gpt-4.1-mini re-orders the top 8 candidate sessions (6 s timeout) |
| History navigation | `backend/utils/history_nav.py` | `list_conversations`, `grep_conversation`, `read_conversation` |
| Time words | `backend/utils/timewords.py` | "last week", "ontem", "em agosto" → date window, resolved on the server |
| Search service | `backend/utils/search_service.py` `SearchService` | `history_search` / `memory_search` requests; the single code path for the warm server, the cold fallback and the eval harness |
| Warm server | `shared/scripts/search-server.py` | Long-lived process that keeps the embedding model loaded; JSON lines over stdin/stdout and a Unix socket `index/.search-server.sock` |
| Orchestrator client | `backend/orchestrator/tools/search.py` | Manages the warm server singleton (`_ensure_server`, auto-restart), cold fallback to `shared/scripts/search.py`, the seven search/navigation tools |
| Indexers | `backend/api/indexer.py` `MemoryWatcher`, `HistoryIndexer`; `backend/api/content_watcher.py` `ContentWatcher` | Background tasks in the backend that run `index-memory.py` (the content watcher does the file watching) |
| CLI | `shared/scripts/search.py`, `shared/scripts/index-memory.py` | One-shot search (`--collection memory|history`, `--also`, `--json`); (re)index (`--memory-only`, `--history-only`, `--reset`, `--no-summaries`, `--local-model`) |
| `/recall` skill | `shared/skills/recall/SKILL.md` | Lets Claude Code sessions search both stores via `search.py` |
| Eval harness | `shared/scripts/history_eval/` (data private in `context/evals/history_search/`) | Retrieval and agent-level evaluation |

### How a search works

- **Storage**: one SQLite file per index (WAL), tables `sessions`/`files`, `chunks` (text, role,
  turn, timestamp, float32 unit-normalized embedding as a BLOB), an FTS5 table (porter stemming,
  diacritics folded) and `meta` (schema, `model`, a `generation` counter readers use to reload).
  Vectors are searched brute-force in numpy (~20k × 384 floats ≈ 30 MB — milliseconds even on the
  Jetson); no vector database.
- **Model**: `paraphrase-multilingual-MiniLM-L12-v2` (384-d, needs `max_seq_length = 256`). Each
  index records its model in `meta.model` and queries are encoded with that model, so an index can
  be rebuilt with another model without code changes.
- **Ranking**: BM25 and cosine rankings fused with reciprocal-rank fusion (`RRF_K = 60`), grouped by
  session (score = best chunk) or by note (score = best section; `MEMORY.md`/`INDEX.md`/`ARCHIVE.md`
  demoted ×0.5). Per-model strength thresholds label results `strong`/`weak`; weak-only results come
  back as `weak_matches`. Then the gpt-4.1-mini re-rank confirms the best candidates (it keeps every
  candidate: confirmed first and strong, the rest weak).
- **History filters are preferences**: `when` (time window) and `kind` (`orchestrator`, `agent`,
  `claude`, `qwen`, `gemini`) rank matching sessions first but still show others, flagged — hard
  filters made the voice agent miss misremembered dates.
- **Noise control**: system reminders, long blobs, tool noise and one-word turns are not indexed,
  nor is the orchestrator's narration of its own search results (`SEARCH_TOOL_NAMES`) — otherwise
  every search would match the earlier searches. Claude sessions that mostly read history (digests,
  search debugging) are demoted ×0.35, not dropped.
- **Joins**: each conversation result lists the memory notes whose `source:` names it; a
  `search_history` call also returns the best memory matches for the same query; memory hits list
  the conversations they came from.

### Tools on top

| Tool (orchestrator) | Use |
|---|---|
| `search_history` | Find conversations by what was said; several phrasings in `queries`, `when`, `kind`; results carry title, kind, dates, working dir, `relevance`, up to 3 excerpts with `turn` numbers, and how to `open` it |
| `list_conversations` | Browse by time / kind / title words, newest first, paged |
| `grep_conversation` / `read_conversation` | Exact words inside one conversation; read around a turn, a range, or one long turn paged |
| `search_memory` | Notes with title, description, `relevance`, matching sections with line ranges (`lines` → `read_file`), `from_conversations` |
| `browse_memory` / `grep_memory` | List a folder (with its `INDEX.md`); exact words across notes → `file:line` |

Claude Code sessions use `/recall` or `context/scripts/run.sh context/scripts/search.py ...`.

### Indexing cadence

| Store | Trigger | What happens |
|---|---|---|
| Memory | `ContentWatcher`: `watchfiles.awatch` on `context/memory/` **and** the linked `docs/` (inotify does not follow the symlink) plus `context/public/`, batches closed after 300 ms of quiet (max 1.5 s), polling fallback when out of inotify watches; markdown changes wake `MemoryWatcher`, which runs the script single-flight (changes during a run → one more run) | `index-memory.py --memory-only` |
| History | `HistoryIndexer`: every 300 s, only if the hash of all JSONL names/sizes/mtimes changed (`context/*.jsonl`, `context/chats/*.jsonl` and the Codex rollouts) | `index-memory.py --history-only` (summaries for new/grown sessions first) |

A history session's id is the one `SessionStore` uses: the file name, except Codex (thread id from
`rollout-…-<id>.jsonl`) and Gemini (the header's `sessionId`; the file name only has `id[:8]`), so
`resume_conversation`, titles and `source:` links work for every harness.

Indexing is incremental: unchanged files are skipped, changed ones re-derived with only new chunks
embedded, deleted ones removed, and one failing file never blocks the rest (exit status 1, failures
logged). The indexers borrow the warm server's loaded model through its Unix socket. A session is
re-summarized when it grew by 25% and ≥ 4 turns and its summary is older than 6 h.

## Pitfalls

- **Paid API calls**: summaries and re-rank call OpenAI. On a quota/rate-limit error the re-ranker
  pauses for 30 min and the summary run stops (circuit breakers added after realtime-model agent
  evals exhausted the account's credits and took voice mode down). Estimate the cost of any eval
  before running it.
- **Never swap index files under a running server** — query and index models would mismatch. To
  rebuild for the Jetson: build `history.sqlite3` and `memory.sqlite3` on the laptop with
  `index-memory.py --local-model`, checkpoint the WAL, copy them plus `session_summaries.sqlite3` as
  `.new`, stop the service, swap, start ([deployment](../infrastructure/deployment.md)).
- **Search server pinned at high CPU with no queries** means a re-embed loop. Each indexer pass
  should report unchanged files; if nothing is ever unchanged, change detection is broken. The
  2026-06-17 incident (every JSONL re-embedded every 2 min, 40+ min per pass, search server at
  200%+ CPU on the 4-core Jetson) was this ([troubleshooting](../operations/troubleshooting.md)).
- **Warm server on aarch64** needs `LD_PRELOAD=libgomp.so.1` (set by `run.sh`), or torch fails with
  "cannot allocate memory in static TLS block". Model load is ~100 s on the Jetson, hence the
  pre-warm at backend startup; warm searches take ~1–3 s (they were 68–103 s cold).
- **Ripgrep from the repo root skips `context/`** (gitignored by the parent repo). Pass an explicit
  `context/...` path when grepping memory or conversations.
- **A memory note is a claim about a past state.** Verify names, flags and paths against the code
  before acting on them.
- **Prefer direct lookup** (`MEMORY.md` → topic `INDEX.md` → file) when you know roughly where
  something lives; search is the supplement.

## Open ideas

From the memory-improvements plan, still open:

- **Automated digestion**: no background process turns finished conversations into memory updates;
  every memory write is deliberate. A daily log (one dated file per day summarizing the day's
  sessions with links to notes touched) is the cheapest first step. Keep a high manual-confirmation
  bar — Rodrigo prefers to stay in control.
- **Graph traversal**: `references:` form a real graph, but nothing follows it automatically; a
  search for X does not pull X's neighbors.
- **Reference lint**: a script that reports one-way `references:` links, stale `modified` dates and
  orphan notes.
- **Tag taxonomy**: tags are free-form and drift (`voice` / `voice-mode` / `realtime-voice`); a
  canonical list or a duplicate-tag report would help.
- **Fold the older LLM wiki** (`context/wiki/`, Karpathy-style entity/concept/event pages from
  `/wiki-digest`) into the structured memory instead of keeping two systems.
- From the search plan: recalibrate strength thresholds with more negative questions (they
  generalized worse on the held-out test), and per-message (not per-session) incremental history
  indexing.

## History

- 2026-02: ChromaDB + `all-MiniLM-L6-v2`, memory re-indexed on change, history every 2 min.
- 2026-04/05: warm search server (`search-server.py`) — searches 68–103 s → 1–3 s on the Jetson.
- 2026-06-07/08: memory reorganized into the folder ontology with frontmatter and cross-references;
  search results enriched with frontmatter and linked memories.
- 2026-06-17 (`6e98e2b`): mtime-skip fix for the history re-embed loop; history interval raised.
- 2026-10-06 (`ab2f224`, then `934fe7d`): history and memory moved to SQLite hybrid indexes,
  multilingual model, session summaries, re-rank, navigation tools; ChromaDB retired. Held-out test:
  history hit@1 0.57 → 0.70, hit@5 0.80 → 0.92, Portuguese hit@5 0.26 → 0.89; memory hit@5
  0.65 → 0.85.
- 2026-10-06: `docs/` linked into the wiki as `context/memory/archie`.
- 2026-10-08: memory parity across harnesses — `MEMORY.md` block for Claude/Model Studio (auto-memory
  off), Qwen and Codex; Gemini history ids = header `sessionId`; Codex rollouts in the history
  indexer's change hash.

Related: [system overview](system-overview.md), [backend](backend.md) (`/memory/` routes), [skills](../integrations/skills.md) (`/recall`), [context sync](../infrastructure/context-sync.md) (how `context/memory/` reaches the Jetson).
