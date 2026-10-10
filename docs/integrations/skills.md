---
name: skills
category: archie/integrations
tags: [skills, slash-commands, skill-md, agents, scripts, run-script, orchestrator-scripts, scaffold-skill, self-modification, catalog]
created: 2026-04-15
modified: 2026-10-09
summary: How skills, agents and scripts work in Archie (format, locations, discovery, run_script allowlist) plus the catalog of every skill.
source: curated (consolidated from memory notes assistant/utilities/project_skills_not_registered.md, assistant/infrastructure/features_and_integrations_summary.md §15, projects/home-automation/tuya_local_control.md, ORCHESTRATOR_SCRIPTS.md, context/AGENTS.md; verified against code 2026-10-06)
references:
  - ../harnesses/claude-code.md
  - ../harnesses/qwen-code.md
  - ../harnesses/gemini-cli.md
  - ../architecture/orchestrator.md
  - ../architecture/backend.md
  - ../architecture/memory-and-search.md
  - ../overview/repo-layout.md
  - youtube.md
  - google-photos.md
  - visualizations-and-sharing.md
  - ../clients/browser-extension.md
  - ../clients/android.md
  - ../devices/fire-tv.md
  - ../devices/photo-servers.md
  - ../infrastructure/jetson-server.md
---

# Skills, agents and scripts

Archie extends itself with three kinds of plain files:

- **Skills** — `SKILL.md` instruction sets (YAML frontmatter + markdown) an agent loads on demand,
  usually invoked as `/<name>`. Declarative: they say *what* to do and *when*.
- **Scripts** — executables the skills call (Python through the project venv, or shell). They do
  the *how*, so any agent that can run a shell command can use a skill.
- **Agents** — subagent definitions (`<name>.md` with frontmatter) for isolated subtasks.

Each kind has a public, general-purpose home in `shared/` and a private, personal home in
`context/` (the gitignored data repo). The `context/` folders hold the personal items plus
symlinks to every shared item, so everything is reachable from one place.

## Where things live

| Kind | Public (this repo) | Private (`context/`) | Discovery |
|---|---|---|---|
| Skills | `shared/skills/<name>/SKILL.md` | `context/skills/<name>/` (personal) + `../../shared/skills/<name>` symlinks | `.claude_config/skills` → `../context/skills` (Claude), `~/.qwen/skills` → `context/skills` (Qwen) |
| Agents | `shared/agents/<name>.md` | `context/agents/` (personal + symlinks) | `.claude_config/agents` → `../context/agents` |
| Scripts | `shared/scripts/` | `context/scripts/` (personal + symlinks) | Referenced by path from skills |

`shared/scripts/setup-context.sh [--force]` creates the `context/` folder structure and one
symlink per item in `shared/skills/`, `shared/scripts/` and `shared/agents/` (relative,
`../../shared/...`), plus the Claude SDK project symlink. The installers do the same. Re-run it
after adding something to `shared/` — a new shared item is not visible under `context/` until
then (several newer shared scripts, e.g. `search-server.py`, are not linked on the laptop today).

The backend lists skills and agents for the clients: `GET /api/skills` reads every
`context/skills/*/SKILL.md` frontmatter (`name`, `description`), `GET /api/agents` every
`context/agents/*.md` (`backend/api/routes/skills.py`, `agents.py`). The web composer's slash
menu comes from `/api/skills`; picking an entry puts `/<name> ` at the start of the draft
(`apps/web/src/features/composer/parts.tsx`).

## SKILL.md format

```yaml
---
name: skill-name                 # lowercase, hyphens
description: What it does and when to use it   # drives auto-invocation; be specific
argument-hint: "[arg1] [arg2]"   # optional, shown in autocomplete
allowed-tools: Read, Write, Bash(context/scripts/*)   # optional, tools usable without prompts
user-invocable: true             # optional; false hides it from the / menu
disable-model-invocation: false  # optional; true = only the user can trigger it
context: fork                    # optional; run in an isolated subagent
agent: Explore                   # optional; subagent type when forked
---

# Instructions …
```

Substitutions inside the body: `$ARGUMENTS` (all arguments as one string), `$0`, `$1`, `$2`
(individual arguments), `${CLAUDE_SESSION_ID}` (current session id).

**Never write literal backtick command syntax in a SKILL.md** — the dynamic-context pattern (an
exclamation mark followed by a backticked command) is executed when the skill loads, and Claude
Code's permission system treats it as a real command attempt and blocks the skill. Describe the
command in words, or put it in a fenced code block the agent runs itself.

Script conventions (from `/scaffold-skill`): a usage/description comment at the top, arguments on
the command line, results on stdout, exit code 0 on success, `--json` for machine-readable output
where it makes sense. Run Python scripts through the venv:
`context/scripts/run.sh context/scripts/<script>.py [args]` (`run.sh` also exports
`PYTHONPATH=backend`, `CLAUDE_CONFIG_DIR` and the `context/.env` variables). General-purpose
scripts can be addressed directly as `shared/scripts/<script>.py`.

A skill that produces something the user must review should write it where every device can reach
it — see [visualizations and sharing](visualizations-and-sharing.md).

## Creating skills and agents

| Skill | Use it for |
|---|---|
| `/scaffold-skill <name>` | Quick project-local skill: writes `context/skills/<name>/SKILL.md` and helper scripts in `context/scripts/` (templates in `shared/skills/scaffold-skill/templates/`) |
| `/anthropic-skill-creator` | Anthropic's full framework (draft, test prompts, graded evals, description optimization) — for skills meant to ship beyond this project. This is the "skill-creator"; its directory and name are `anthropic-skill-creator` |
| `/scaffold-agent <name>` | New subagent at `context/agents/<name>.md` |

Put a new item in `shared/` (and link it with `setup-context.sh`) only if it has nothing personal
in it — no device addresses, accounts or private paths. Otherwise it goes straight into
`context/`.

When an agent finds a problem in a skill or script, it should propose fixes and offer to spin up a
session for that skill instead of silently working around it.

## Project skills may not register as `Skill()` / slash commands

In a Claude Code session inside the wrapper, project skills may be invisible to the Skill tool
(2026-08-28: `Skill(browser-control)` → "Unknown skill", `Skill(recall)` → "Did you mean recap?")
while built-in and plugin skills work. The `.claude_config/skills` symlink alone is not enough;
the likely reason (untested) is in [claude-code.md](../harnesses/claude-code.md#pitfalls).

The reliable workaround costs one read and loses nothing — the SKILL.md *is* the instruction set:

1. `Read context/skills/<name>/SKILL.md` and follow it. Script paths in it work the same by hand.
2. If the skill names a companion doc, read that too; the gotchas usually live there.
3. **When delegating**, hand the session the **path** to the SKILL.md, not just `/name`, and
   confirm the procedure actually loaded before reporting the task as started. The orchestrator's
   delegation rules (e.g. browser tasks) follow this.

## Orchestrator `run_script` allowlist

The orchestrator can run one allowlisted script per call with its `run_script` tool
(`backend/orchestrator/tools/run_script.py`) for quick single-shot actions; real tasks still go to
an agent session ([orchestrator](../architecture/orchestrator.md)).

- Allowlist: `context/memory/ORCHESTRATOR_SCRIPTS.md` (private). It is injected into the
  orchestrator prompt; the orchestrator curates it with `write_file`, and agent sessions add an
  entry whenever they create a reusable single-shot script the orchestrator should trigger.
- Entry format: one `### <name>` heading, a sentence of purpose and rules, then a fenced block with
  a `path:` line (the only line the parser reads — regex `^\s*path:\s*…`), `args:`, and usage
  examples:

  ````
  ### my_script

  What it does; any rule the orchestrator must follow (cost, writes, confirmation).

  ```
  path: context/scripts/my_script.py
  args: <command> [options]
  example: run_script(script="context/scripts/my_script.py", args=["status"])
  ```
  ````
- Execution: `context/scripts/run.sh <path> <args…>` with no sandbox, 300 s timeout, stdout and
  stderr each clipped to 20 000 characters, result returned as JSON
  (`script`, `args`, `exit_code`, `stdout`, `stderr`). Paths are normalized to repo-root-relative;
  anything not in the list is refused.
- Current entries: lamp control (`tuya_lamps.py`), the browser extension (`browser_cmd.py`), the
  voice prompt size diagnostic (`measure_voice_prompt.py`) and read-only X API calls
  (`x_api.py`, whose write commands need explicit approval per message).

## Skill catalog

Shared skills are in this repo; personal skills live only in `context/skills/` (they reference
private devices, accounts or paths), so their files are not public.

| Skill | Where | Purpose |
|---|---|---|
| `anthropic-skill-creator` | shared | Anthropic's skill-authoring framework with evals and description tuning |
| `browser-control` | shared | Drive Rodrigo's real, logged-in Chrome through the extension: `look`, navigate, click, fill, scroll, JS (`browser_cmd.py`) — [browser extension](../clients/browser-extension.md) |
| `debug-app` | shared | Start backend + web dev servers and debug with Chrome DevTools MCP |
| `recall` | shared | Search memory notes and conversation history (`search.py`) — [memory and search](../architecture/memory-and-search.md) |
| `scaffold-agent` | shared | Create a subagent definition in `context/agents/` |
| `scaffold-skill` | shared | Quick project-local skill scaffold |
| `wrapper-guide` | shared | Navigate and debug the wrapper application (manager, API, frontend) |
| `android-dev` | personal | Build, install and debug the Android apps (main + lite), logcat, ADB UI control — [android](../clients/android.md) |
| `connect-tv` | personal | Discover the Fire TV on the LAN and connect over ADB — [fire-tv](../devices/fire-tv.md) |
| `create-viz` | personal | Interactive HTML canvases (project interfaces, explainers, review pages) under `context/public/`, edited in place and reloaded live in the apps, shown on the TV — [visualizations](visualizations-and-sharing.md) |
| `generate-image` | personal | Text-to-image with Google's Nano Banana (Gemini Image) API |
| `generate-video` | personal | Text/image-to-video with Seedance 2.0 on BytePlus ModelArk |
| `google-photos` | personal | Google Photos Picker downloads + Google Drive search/download — [google-photos](google-photos.md) |
| `iphone-photos` | personal | List and download from the iPhone photo server — [photo servers](../devices/photo-servers.md) |
| `lamps` | personal | Local LAN control of Tuya smart bulbs via tinytuya (on/off, brightness, color temp, RGB, status, rescan) |
| `music-video-editor` | personal | Sync multitrack music takes to a master mix and build a Remotion composition |
| `remotion` | personal | Programmatic video with Remotion (rules from `remotion-dev/skills`) |
| `server-management` | personal | Jetson server runbook: status, deploy, sync, restart, logs — [jetson server](../infrastructure/jetson-server.md) |
| `tv-dev` | personal | Develop and debug the TvServerHub Fire TV app |
| `tv-remote` | personal | Fire TV remote: apps, media keys, navigation, volume, open URLs |
| `x` | personal | Post and read on X through the API (timelines, mentions, search, DMs), several authorized accounts |
| `youtube` | personal | YouTube Data API scripts and yt-dlp downloads — [youtube](youtube.md) |

Agents: `shared/agents/debug-app.md` (tools Bash/Read/Glob/Grep + Chrome DevTools MCP,
`permissionMode: acceptEdits`, preloads the `debug-app` skill), symlinked into `context/agents/`.

### Lamp control notes (`/lamps`)

`context/scripts/tuya_lamps.py` talks to each bulb directly on the LAN (port 6668, Tuya protocol
3.3/3.5) with tinytuya; no cloud in the control path. Device ids, local keys and IPs are in
`context/secrets/tuya/devices.json`; the cloud project credentials used only by `pull` are in
`context/secrets/tuya/cloud.json`. Category `dj` bulbs use DPS 20 power, 21 mode, 22 brightness
(10–1000), 23 colour temperature (0–1000), 24 colour (HSV hex). Gotchas:

- **IPs go stale** when the router re-leases them. `rescan` refreshes IPs and versions from a
  passive LAN scan without touching keys or the cloud; the write commands self-heal the same way.
- A stale address occupied by a *different* bulb gives `904 Unexpected Payload from Device`, which
  looks like a wrong key but is a wrong address — rescan, don't re-`pull` keys.
- tinytuya ≥ 1.17 "Bulb not configured, cannot determine value ranges" almost always means the
  bulb is unreachable, not a range problem.
- `pull` re-fetches local keys from the cloud project; only needed after pairing new bulbs.

## History

- The public/private split predates the 2026-10-05 reorganization, which renamed the public
  folders `default-skills/`, `default-scripts/`, `default-agents/` to `shared/skills/`,
  `shared/scripts/`, `shared/agents/` and re-pointed the `context/` symlinks
  ([repo layout](../overview/repo-layout.md)).
- 2026-08-28 — project skills found not to register as `Skill()` in wrapper sessions; read-by-path
  became the rule.
