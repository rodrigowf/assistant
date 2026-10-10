# Archie documentation

The documentation of **Archie**, the self-hosted personal AI assistant in this repo, created by
Rodrigo, who is also its main user. It describes
the system **as it is now**: what each part is, where it lives, how it works, the rules and pitfalls
learned the hard way, and a short history. New here? Read
[overview/archie.md](overview/archie.md), then
[architecture/system-overview.md](architecture/system-overview.md).

These docs are also part of Archie's memory wiki: `context/memory/archie` is a symlink to this
folder, so agents find them through `MEMORY.md`, `search_memory`, `browse_memory` and the memory
browser, and they are served at `/memory/archie/<path>`.

## Map

| Folder | What's in it | Start with |
|---|---|---|
| [overview/](overview/INDEX.md) | What Archie is, repo layout, glossary, cross-cutting decisions | [archie.md](overview/archie.md) |
| [architecture/](architecture/INDEX.md) | Backend: components and data flows, FastAPI app, agent sessions, orchestrator, memory and search | [system-overview.md](architecture/system-overview.md) |
| [harnesses/](harnesses/INDEX.md) | The pluggable agent CLIs: registry, Claude Code, Qwen Code, Gemini CLI, Codex; sign-in of every service (Settings → Accounts) | [registry.md](harnesses/registry.md), [authentication.md](harnesses/authentication.md) |
| [voice/](voice/INDEX.md) | Realtime voice: providers, transports, lifecycle, prompt budget, OpenAI / Qwen / Gemini specifics, Android wake word | [architecture.md](voice/architecture.md) |
| [clients/](clients/INDEX.md) | Web app (+ Safari 12 build), Android apps, companion app, Chrome extension, legacy apps | [web.md](clients/web.md), [android.md](clients/android.md) |
| [devices/](devices/INDEX.md) | The reference deployment's devices; Fire TV; photo servers | [devices.md](devices/devices.md) |
| [integrations/](integrations/INDEX.md) | Skills system and catalog; YouTube; Google Photos; visualizations and document sharing | [skills.md](integrations/skills.md) |
| [infrastructure/](infrastructure/INDEX.md) | Laptop + Jetson topology, the Jetson server, deployment, context-sync, installation, SSH remote sessions | [topology.md](infrastructure/topology.md) |
| [operations/](operations/INDEX.md) | Working rules for agents, debugging playbook, symptom → fix table, refactor methodology | [working-rules.md](operations/working-rules.md) |
| [history/](history/INDEX.md) | Timeline and historical records (handoffs, plans, logs) | [timeline.md](history/timeline.md) |
| [specs/](specs/INDEX.md) | Normative client specs: information architecture, client protocol, web and Android architecture | [12-client-protocol.md](specs/12-client-protocol.md) |
| [projects/](projects/INDEX.md) | Workstreams on Archie: the 2026-10 web/Android rebuild (charter, inventory, plan, handoff), the 2026-10-06 search rebuild (plan, results) | [projects/INDEX.md](projects/INDEX.md) |
| `assets/` | Logo | |

Common questions:

- *How does a message get from my phone to Claude and back?* → [system-overview](architecture/system-overview.md)
- *What tools does the orchestrator have?* → [orchestrator](architecture/orchestrator.md)
- *Voice is broken / stuck "preparing".* → [voice/lifecycle](voice/lifecycle.md), [troubleshooting](operations/troubleshooting.md)
- *How do I deploy to the Jetson?* → [deployment](infrastructure/deployment.md)
- *Where is X in the repo / what was the old path?* → [repo-layout](overview/repo-layout.md)
- *What does this word mean?* → [glossary](overview/glossary.md)

## Writing and maintaining these docs

**These docs are public** (the repo `rodrigowf/archie` is public). Never put in them:
passwords, tokens, API keys or their values (naming the env var or file that holds them is fine);
Tailscale or public IP addresses; anything about Rodrigo's personal life or other people; employer
or client installations; social-media handles; conversation content or session ids. That material
belongs in the private memory under `context/memory/` (`people/`, `deployment/`, `projects/`, …).
The reference deployment's LAN addresses, device models, serials and package names are fine.

Conventions (the same as the memory wiki's, `context/memory/MEMORY.md`):

- **Frontmatter on every doc** except `INDEX.md` files:

  ```yaml
  ---
  name: <filename without .md>
  category: archie/<folder>
  tags: [<keyword>, ...]
  created: <YYYY-MM-DD>
  modified: <YYYY-MM-DD>
  summary: <one line, ≤ 20 words>
  source: curated (<where the content came from>; verified against code <date>)
  references:
    - <relative path to every doc this one links to>
  ---
  ```

- **One `INDEX.md` per folder** — a short description, then one line per doc. Add the line in the
  same change that adds the doc; update this root map only for a new folder.
- **Links** between docs are relative (`../voice/lifecycle.md`) and also listed in `references:`.
  Code paths go in backticks, repo-root-relative (`backend/api/pool.py`). **Never link from `docs/`
  into `context/`** — private paths may appear as plain text in backticks only.
- **Describe the current state.** When the code changes, change the doc in the same commit; move
  superseded detail into a short "History" section or into [history/](history/INDEX.md). The code
  wins any disagreement — fix the doc.
- **Keep the hard-won knowledge.** Incident root causes, tuned constants and "don't do X because Y"
  rules are the most valuable content; state them as current rules.
- Bump `modified:` when you change a doc.

Docs are versioned in this repo, not by context-sync: an edit made on one machine (directly or
through `context/memory/archie/`) reaches the other machine only after commit, push and pull.
