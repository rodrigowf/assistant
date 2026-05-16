# Installing the Personal Assistant

The installer supports **Linux**, **macOS**, and **Windows**.  Each OS has its own implementation under `install/<os>/`, dispatched by thin wrappers at the project root.

There are two installation styles, both available on every OS.

| | Linux / macOS | Windows |
|--|--|--|
| **Deterministic** | `./install.sh` | `.\install.ps1` |
| **Conversational** | `./install-with-agent.sh` | `.\install-with-agent.ps1` |

- **Deterministic** — runs every step automatically.  Asks two questions up front (session harness + orchestrator backends), then proceeds non-interactively.  Recommended if you already know what you want.
- **Conversational** — launches one of the agent CLIs (Claude Code, Qwen Code, or Gemini CLI) and hands it this file as instructions.  The agent walks you through each decision, executes the steps itself, and writes a running log to `context/install.log`.  Recommended if you're new to the project and want a guided setup.

Both paths arrive at the same end state: a working assistant with the harnesses, SDKs, and config you chose.

### Where the per-OS scripts live

```
install/
├── README.md, AGENTS.md, MEMORY.md, ...   shared templates (every OS uses these)
├── cli-runtime/                           shared per-CLI seed dirs
├── linux/                                 Linux installer
│   ├── install.sh
│   ├── install-with-agent.sh
│   └── install-prerequisites.sh
├── apple/                                 macOS installer (Intel + Apple Silicon)
│   ├── install.sh
│   ├── install-with-agent.sh
│   └── install-prerequisites.sh
└── windows/                               Windows installer (PowerShell 5.1 / 7+)
    ├── install.ps1
    ├── install-with-agent.ps1
    └── install-prerequisites.ps1
```

The wrappers at the project root (`install.sh`, `install-with-agent.sh`, `install.ps1`, `install-with-agent.ps1`) detect the host OS and dispatch to the right per-OS script.  You can also call the per-OS scripts directly if you prefer.

---

## For the install agent (boot prompt)

> **If you are an LLM reading this file as the install agent: this section is your instructions.  Human readers can skip ahead to [Prerequisites](#prerequisites).**
>
> You were launched by `install-with-agent.sh` (Linux/macOS) or `install-with-agent.ps1` (Windows) to drive this installation conversationally.  You are running inside the project root with file and shell tools available.
>
> **First: determine which per-OS recipe to follow.**  The top-level `./install.sh` / `.\install.ps1` are thin dispatchers — they exec the real installer under `install/<os>/`.  The canonical recipe for *your* OS is one of:
>
> - **Linux** → `install/linux/install.sh`
> - **macOS** → `install/apple/install.sh`
> - **Windows** → `install/windows/install.ps1`
>
> Pick the one matching the host (the launching wrapper script tells you in its kickoff prompt, or check `uname -s` / `$IsWindows`).  All three follow the same step structure (`# Step 0a`, `# Step 0b`, ..., `# Step 12`) and arrive at the same end state, but they differ in concrete commands (`apt`/`brew`/`winget`, `sed -i` vs `sed -i ''`, symlinks vs junctions).  **Do not read the wrong one** — the differences are real.
>
> Your job:
>
> 1. **Read the per-OS installer end-to-end** before doing anything.  Treat it as the canonical recipe — every step you take should correspond to a step in that script.  Pay special attention to the `# Step N` headers and the per-axis flag logic at the top.
> 2. **Read `install/README.md` and `install/<os>/README.md`** to understand what the templates do and which files get copied where, plus OS-specific quirks (Homebrew on macOS, winget + Developer Mode on Windows).
> 3. **Walk the user through the two axis decisions** (session harness, orchestrator backends) — same questions the script's Steps 0a/0b ask.  Explain each option briefly when asked.
> 4. **Execute the steps yourself**, in order, using your file and shell tools.  Don't just run the deterministic installer — read it as the spec and re-do each step interactively so you can adapt to the user's answers and recover from errors.  The deterministic parts (`pip install -r requirements*.txt`, `npm install`, venv creation) are fine to shell out for; the conversational/branching parts (axis decisions, context import vs. fresh, optional symlinks, CLI runtime seeding) you handle directly.
> 5. **Log your progress to `context/install.log` continuously.**  At the start of each step, append a line like `[2026-05-16 14:32] step 4: creating venv ...`.  Append the outcome on completion or error.  If the install crashes, this log lets the user (or a future agent) resume manually.
> 6. **Never invent steps.**  If the per-OS installer doesn't do something, neither should you.  If you're unsure, read the relevant block and follow it literally.
> 7. **Never skip the API key reminders.**  Step 12 warns about missing keys for the axes the user picked — do the same warning.
> 8. **Windows-specific**: if you're on Windows, check whether symlink creation works before attempting any link steps (the installer's `Test-Symlinks` does this).  If it fails, tell the user about Developer Mode before falling back to junctions + copies.  Path mangling for the CLI project dirs replaces both `\` and `:` with `-`.
> 9. **When everything is done**, ask the user if they'd like you to start the backend (`context/scripts/run.sh -m uvicorn api.app:create_app --factory --host 0.0.0.0 --port 8765` on POSIX, or `.venv\Scripts\python.exe -m uvicorn api.app:create_app --factory --port 8765` on Windows) and/or the frontend (`cd frontend && npm run dev`) in the background.  If yes, launch them in background mode so they survive your exit, then tell the user the install is complete and they can press Ctrl-C to exit this session.  Confirm the services are reachable before declaring victory.
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

The deterministic installer's Step 1 runs the per-OS prereq checker for you.  You can also run it standalone:

| OS | Command |
|--|--|
| Linux | `./install/linux/install-prerequisites.sh` |
| macOS | `./install/apple/install-prerequisites.sh` |
| Windows | `.\install\windows\install-prerequisites.ps1` |

Each one checks tool versions and offers to install whatever's missing:

- **Linux** prints `apt` / `dnf` / `pacman` install hints — install manually before re-running.
- **macOS** offers to bootstrap **Homebrew** and install Python 3.12 + Node 20 via brew.  Handles both Apple Silicon (`/opt/homebrew`) and Intel (`/usr/local`).
- **Windows** offers to install Python 3.12 + Node LTS + Git via **winget** (Microsoft's built-in package manager on Windows 10 1809+ / Windows 11).

You'll also need at least one of these API keys *somewhere* — either obtained ahead of time, or set up during install:

- **Claude Code** — uses Anthropic OAuth (`claude auth login`).  No `ANTHROPIC_API_KEY` needed unless you're also using the Anthropic SDK in the orchestrator.
- **Qwen Code** — either OAuth (interactive on first `qwen` run) or `DASHSCOPE_API_KEY` in `context/.env`.
- **Gemini CLI** — either Google OAuth (interactive on first `gemini` run) or `GEMINI_API_KEY` in `context/.env`.
- **Orchestrator backends** — `OPENAI_API_KEY` for OpenAI/GPT/Qwen-via-compatible/Gemini-via-compatible; `ANTHROPIC_API_KEY` for Anthropic Claude models in the orchestrator.

You don't need every key — only the ones for axes you opt into.

### Windows-only prerequisites

- **PowerShell 7+** is recommended (`winget install Microsoft.PowerShell`).  Windows PowerShell 5.1 — bundled with Windows — also works.
- **Execution policy**: if you see "running scripts is disabled on this system", lift it for your user:
  ```powershell
  Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
  ```
  Or invoke the installer with a one-time bypass:
  ```powershell
  powershell.exe -ExecutionPolicy Bypass -File .\install.ps1
  ```
- **Symlinks** require either Developer Mode enabled OR running PowerShell as Administrator.  Without them, the installer falls back to NTFS junctions for directories and plain file copies for files — fully functional, but file links won't auto-update if you change the source.  **To enable Developer Mode:** Settings → Update & Security → For Developers (Windows 10) or Settings → System → For Developers (Windows 11) → toggle on Developer Mode.

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

These mirror the `# Step N` headers in your OS's installer (`install/linux/install.sh`, `install/apple/install.sh`, or `install/windows/install.ps1`).  Run them in order.  Throughout this section, "install.sh" refers to *your* OS's installer; the Windows variant is `install.ps1` but uses the same step structure.

### Step 1: Prerequisites

Run the per-OS prereq checker (`install/linux/install-prerequisites.sh`, `install/apple/install-prerequisites.sh`, or `install\windows\install-prerequisites.ps1`) — fail fast on missing Python / Node / npm / git, and on macOS / Windows offer to bootstrap whatever's missing via Homebrew / winget.

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

The exact mangling logic is in the per-OS installer — read it there.  Idempotent: re-runs leave existing symlinks alone.

**Windows path mangling**: on Windows, both `\` and `:` are replaced with `-`, so `C:\Users\you\assistant` becomes `C--Users-you-assistant`.  If a CLI version uses a different mangle, run the CLI once (it'll create its own real dir under `%USERPROFILE%\.<cli>\projects\`), then re-run `install.ps1` — it detects the real dir and replaces it with a link to `context\` (after migrating any chat history).

**Symlinks on Windows**: real symbolic links require Developer Mode or Administrator.  Without those, the installer falls back to NTFS junctions (directories) and copies (files).  See the [Windows section](#windows-only-prerequisites) above for how to enable Developer Mode.

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
python3 -m venv .venv               # Linux / macOS
py -3.12 -m venv .venv              # Windows
```

### Step 5: Upgrade pip

```bash
.venv/bin/pip install --upgrade pip               # Linux / macOS
.venv\Scripts\pip.exe install --upgrade pip       # Windows
```

### Step 6: Python dependencies

Always install:

```bash
.venv/bin/pip install -r requirements.txt         # Linux / macOS
.venv\Scripts\pip.exe install -r requirements.txt # Windows
```

Then conditionally (use the venv pip path matching your OS):

- `--with-anthropic` / `-WithAnthropic` → `pip install -r requirements-anthropic.txt`
- `--with-openai` / `-WithOpenAI` → `pip install -r requirements-openai.txt`
- `--with-claude` / `-WithClaude` → `pip install -r requirements-claude.txt`
- `--with-qwen` / `-WithQwen` → no extra Python deps (Qwen runs as a subprocess via the CLI)
- `--with-gemini` / `-WithGemini` → no extra Python deps
- `--dev` / `-Dev` → `pip install -r requirements-dev.txt`

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

**Linux / macOS:**

```bash
# Terminal 1 — Backend
context/scripts/run.sh -m uvicorn api.app:create_app --factory --host 0.0.0.0 --port 8765

# Terminal 2 — Frontend
cd frontend && npm run dev
```

**Windows:**

```powershell
# Terminal 1 — Backend
.venv\Scripts\python.exe -m uvicorn api.app:create_app --factory --port 8765

# Terminal 2 — Frontend
cd frontend; npm run dev
```

Open **https://localhost:5432** and start chatting.

If you used the conversational installer, the agent can offer to start both in the background for you.

---

## Troubleshooting

- **`npm install -g` fails with EACCES** — your global npm prefix needs write permission, or use a Node version manager like `nvm` / `volta` / `mise` instead of system Node.
- **The CLI is installed but not on PATH** — happens with `nvm` if the shell that runs the install isn't logged in as the nvm-using user.  Set `QWEN_CLI_PATH` (or the equivalent for Gemini) in `context/.env`.
- **Symlinks point at the wrong place after copying `context/`** — re-run `./install.sh`.  It re-creates the symlinks idempotently.
- **The install crashed mid-way** — read `context/install.log` (if you used the agent installer) or check what step `install.sh` was on (it prints `Step N` headers as it goes).  Most steps are independently re-runnable; symlink steps are idempotent.

For deeper issues, the canonical reference is `install.sh` itself — every step has a comment block explaining what it does and why.
