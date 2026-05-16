# Installing the Personal Assistant

There are two ways to install this project.

- **`./install.sh`** — A deterministic shell installer.  Asks two questions (harness + orchestrator) up front, then runs every step automatically.  Recommended if you already know what you want, or if you want every action to be visible in plain bash.
- **`./install-with-agent.sh`** — A conversational installer.  Launches one of the agent CLIs (Claude Code, Qwen Code, or Gemini CLI) and hands it this file as instructions.  The agent then walks you through each decision, executes the steps itself, and writes a running log to `context/install.log`.  Recommended if you're new to the project and want a guided setup.

Both paths arrive at the same end state: a working assistant with the harnesses, SDKs, and config you chose.  Pick whichever you prefer.

---

## For the install agent (boot prompt)

> **If you are an LLM reading this file as the install agent: this section is your instructions.  Human readers can skip ahead to [Prerequisites](#prerequisites).**
>
> You were launched by `install-with-agent.sh` to drive this installation conversationally.  You are running inside the project root with file and Bash tools available.
>
> Your job:
>
> 1. **Read `install.sh` end-to-end** before doing anything.  Treat it as the canonical recipe — every step you take should correspond to a step in that script.  Pay special attention to the `# Step N` headers and the per-axis flag logic at the top.
> 2. **Read `install/README.md`** to understand what the `install/` templates do and which files get copied where.
> 3. **Walk the user through the two axis decisions** (session harness, orchestrator backends) — same questions the script's Steps 0a/0b ask.  Explain each option briefly when asked.
> 4. **Execute the steps yourself**, in order, using your file and Bash tools.  Don't just run `./install.sh` — read it as the spec and re-do each step interactively so you can adapt to the user's answers and recover from errors.  The deterministic parts (`pip install -r requirements*.txt`, `npm install`, venv creation) are fine to shell out for; the conversational/branching parts (axis decisions, context import vs. fresh, optional symlinks, `install/cli-runtime/` seeding) you handle directly.
> 5. **Log your progress to `context/install.log` continuously.**  At the start of each step, append a line like `[2026-05-16 14:32] step 4: creating venv ...`.  Append the outcome on completion or error.  If the install crashes, this log lets the user (or a future agent) resume manually.
> 6. **Never invent steps.**  If `install.sh` doesn't do something, neither should you.  If you're unsure, read the relevant block of `install.sh` and follow it literally.
> 7. **Never skip the API key reminders.**  Step 12 in `install.sh` warns about missing keys for the axes the user picked — do the same warning.
> 8. **When everything is done**, ask the user if they'd like you to start the backend (`context/scripts/run.sh -m uvicorn api.app:create_app --factory --host 0.0.0.0 --port 8765`) and/or the frontend (`cd frontend && npm run dev`) in the background.  If yes, launch them in background mode so they survive your exit, then tell the user the install is complete and they can press Ctrl-C to exit this session.  Confirm the services are reachable before declaring victory.
>
> A few additional rules:
>
> - You may have already been launched with one harness installed and authenticated (the one driving this session).  Don't re-install or re-authenticate that one — note its presence and move on.
> - When in doubt about a flag combination, defer to the user with a clear, scoped question.  Don't choose for them silently.
> - Treat `context/.env` carefully.  Read it before writing; preserve existing keys; only uncomment/add the keys for axes the user just picked.
> - If a step fails (e.g. `npm install` returns non-zero), don't silently continue.  Show the error, write it to `context/install.log`, and ask the user how to proceed.
>
> Continue below for the install procedure.  Human readers, the rest of this file is for you too — it's a faithful walkthrough of what `install.sh` does.

---

## Prerequisites

- **Python 3.12+**
- **Node.js 20+** (Qwen Code and Gemini CLI both depend on Node)
- **npm** (comes with Node)
- **git**

Run the bundled prereq checker:

```bash
./default-scripts/install-prerequisites.sh
```

It checks each tool's version and prints what's missing.  Install anything it flags before proceeding.

You'll also need at least one of these API keys *somewhere* — either obtained ahead of time, or set up during install:

- **Claude Code** — uses Anthropic OAuth (`claude auth login`).  No `ANTHROPIC_API_KEY` needed unless you're also using the Anthropic SDK in the orchestrator.
- **Qwen Code** — either OAuth (interactive on first `qwen` run) or `DASHSCOPE_API_KEY` in `context/.env`.
- **Gemini CLI** — either Google OAuth (interactive on first `gemini` run) or `GEMINI_API_KEY` in `context/.env`.
- **Orchestrator backends** — `OPENAI_API_KEY` for OpenAI/GPT/Qwen-via-compatible/Gemini-via-compatible; `ANTHROPIC_API_KEY` for Anthropic Claude models in the orchestrator.

You don't need every key — only the ones for axes you opt into.

---

## The two axis decisions

The installer asks two questions up front.  Both can be re-run later by passing the appropriate `--with-X` / `--without-X` flags to `install.sh`.

### Axis 1: Session harness

Which agent CLI runs your chats?  You can pick more than one.

- **Claude Code** (Anthropic) — recommended default.  Mature CLI, plan-mode permission gating, OAuth login, Sonnet/Opus models.
- **Qwen Code** (Alibaba) — open-weights models served via the OpenAI-compatible endpoint, OAuth or DashScope key.
- **Gemini CLI** (Google) — OAuth or `GEMINI_API_KEY`.

If you pick multiple, the UI's Session Provider selector lets you switch per chat.  Default for new chats is set in `assistant_config.json` (`provider` field) and is whichever you picked first.

### Axis 2: Orchestrator backends

The orchestrator agent (text + voice) is independent of the harness.  Which API SDK(s) should it use?

1. **OpenAI only** — GPT models, Qwen / Gemini / GLM via the OpenAI-compatible endpoint, and OpenAI Realtime voice.  Recommended for Qwen-only setups.
2. **Anthropic only** — Claude models in the orchestrator picker.
3. **Both** — full flexibility.
4. **Neither** — orchestrator disabled, chats only.

---

## Install steps

These mirror the `# Step N` headers in `install.sh`.  Run them in order.

### Step 1: Prerequisites

`./default-scripts/install-prerequisites.sh` — fail fast on missing Python / Node / npm / git.

### Step 2: Context setup

`context/` is a private, gitignored data directory (and optionally its own git repo).  Three modes:

- **Fresh** — `mkdir context/`, copy `install/AGENTS.md` → `context/AGENTS.md`, copy `install/MEMORY.md` → `context/memory/MEMORY.md`, copy `install/context.env` → `context/.env`.
- **Import** — `git clone <url> context/` (use this if you already have an `assistant-context` repo somewhere).
- **Existing** — if `context/` already has files, leave them alone and just ensure required files exist.

Then create the symlink trees:

- `context/skills/` — symlinks to every `default-skills/*/`
- `context/scripts/` — symlinks to every `default-scripts/*`
- `context/agents/` — symlinks to every `default-agents/*`

These let `context/` reach the public framework while keeping personal additions in the same directory.

### Step 3 (a, b, c): Per-harness SDK config dirs

For each enabled harness, create the project-local config dir the CLI expects:

- **Claude** — `.claude_config/` symlinked into `context/`.  Specifically: `.claude_config/projects/<mangled-cwd>` → `context/`, plus `.claude_config/skills` → `context/skills`, `.claude_config/agents` → `context/agents`.
- **Qwen** — `~/.qwen/projects/<mangled-cwd>` → `context/`.  Qwen mangles the cwd by replacing `/` with `-`, e.g. `-home-rodrigo-assistant`.
- **Gemini** — `~/.gemini/tmp/<label>/` symlinked similarly.  Gemini uses a hash-based label rather than a mangled path.

The exact mangling logic is in `install.sh` — read it there.  Idempotent: re-runs leave existing symlinks alone.

### Step 3d: AGENTS.md symlinks

`context/AGENTS.md` is the canonical project-instructions file.  Symlink the per-CLI shadows at the repo root:

- `CLAUDE.md` → `context/AGENTS.md`
- `QWEN.md` → `context/AGENTS.md`
- (Gemini reads `GEMINI.md` by default; add it if you want — currently `install.sh` does only Claude and Qwen.)

### Step 3e: Seed local CLI runtime dirs

For each enabled harness, seed the project-local runtime dir from `install/cli-runtime/<cli>/`:

- `install/cli-runtime/claude/settings.json` → `.claude/settings.json`
- `install/cli-runtime/qwen/settings.json` → `.qwen/settings.json`
- `install/cli-runtime/gemini/settings.json` → `.gemini/settings.json`

These hold default permission allowlists and the Gemini `respectGitIgnore=false` carve-out.  Never overwrite existing files — re-runs on a working setup must be no-ops.

### Step 4: Python venv

```bash
python3 -m venv .venv
```

### Step 5: Upgrade pip

```bash
.venv/bin/pip install --upgrade pip
```

### Step 6: Python dependencies

Always install:

```bash
.venv/bin/pip install -r requirements.txt
```

Then conditionally:

- `--with-anthropic` → `.venv/bin/pip install anthropic`
- `--with-openai` → `.venv/bin/pip install openai`
- `--with-claude` → `.venv/bin/pip install claude-agent-sdk`
- `--with-qwen` → no extra Python deps (Qwen runs as a subprocess via the CLI)
- `--with-gemini` → no extra Python deps
- `--dev` → `.venv/bin/pip install ruff mypy`

### Step 7: Frontend deps

```bash
cd frontend && npm install
cd frontend-compat && npm install
cd ..
```

### Step 7b: Agent CLI install + first-run login

For each enabled harness:

1. **Check if the CLI is on PATH.**  If yes, note the path and skip the install.
2. **If missing:** ask the user `Install <cli> globally via npm? [Y/n]`.  If yes: `npm install -g <pkg>`.  Packages:
   - `claude` → `@anthropic-ai/claude-code`
   - `qwen` → `@qwen-code/qwen-code`
   - `gemini` → `@google/gemini-cli`
3. **Check auth state.**  Skip the login prompt entirely if:
   - The relevant env key is already set in `context/.env` (`ANTHROPIC_API_KEY` for Claude, `DASHSCOPE_API_KEY` for Qwen, `GEMINI_API_KEY` for Gemini), **or**
   - The CLI's OAuth file already exists (`~/.claude/.credentials.json`, `~/.qwen/oauth_creds.json`, `~/.gemini/oauth_creds.json`).
4. **Otherwise**: tell the user to open a separate terminal and run the login command (`claude auth login`, `qwen`, or `gemini`), then come back and confirm.  Re-check auth state after.

Don't try to drive the OAuth browser flow from inside the install — the CLIs handle it themselves on first interactive run.

### Step 8: Local directories

```bash
mkdir -p index logs
```

### Step 9: Claude credentials link

If `--with-claude` and `~/.claude/.credentials.json` exists, symlink `.claude_config/.credentials.json` → `~/.claude/.credentials.json` so the SDK in this project picks up the same OAuth token Claude Code itself uses.

### Step 10: assistant_config.json

Copy `install/assistant_config.json` to the repo root, substituting:

- `@@SCRIPT_DIR@@` → the absolute path to the project root
- `@@DEFAULT_PROVIDER@@` → `claude` if `--with-claude`, else `qwen` (whichever was picked first)
- `@@DEFAULT_MODEL@@` → provider-appropriate (Claude Sonnet vs. Qwen 3 Plus)

### Step 11: .manager.json

Copy `install/manager.json` to `.manager.json` at the repo root.  No substitutions.

### Step 12: Verification

For each axis the user opted into, check that the required env key is set in `context/.env` and warn if not.  Don't fail the install — keys can be added later.  Specifically:

- `--with-anthropic` needs `ANTHROPIC_API_KEY`
- `--with-openai` needs `OPENAI_API_KEY`
- `--with-qwen` needs either `DASHSCOPE_API_KEY` or for OAuth login to have happened
- `--with-gemini` needs either `GEMINI_API_KEY` or for OAuth login to have happened

### After install

```bash
# Terminal 1 — Backend
context/scripts/run.sh -m uvicorn api.app:create_app --factory --host 0.0.0.0 --port 8765

# Terminal 2 — Frontend
cd frontend && npm run dev
```

Open **https://localhost:5432** and start chatting.

If you used `install-with-agent.sh`, the agent can offer to start both in the background for you.

---

## Troubleshooting

- **`npm install -g` fails with EACCES** — your global npm prefix needs write permission, or use a Node version manager like `nvm` / `volta` / `mise` instead of system Node.
- **The CLI is installed but not on PATH** — happens with `nvm` if the shell that runs the install isn't logged in as the nvm-using user.  Set `QWEN_CLI_PATH` (or the equivalent for Gemini) in `context/.env`.
- **Symlinks point at the wrong place after copying `context/`** — re-run `./install.sh`.  It re-creates the symlinks idempotently.
- **The install crashed mid-way** — read `context/install.log` (if you used the agent installer) or check what step `install.sh` was on (it prints `Step N` headers as it goes).  Most steps are independently re-runnable; symlink steps are idempotent.

For deeper issues, the canonical reference is `install.sh` itself — every step has a comment block explaining what it does and why.
