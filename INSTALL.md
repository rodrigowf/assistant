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
> 4. **Execute the steps yourself**, in order, using your file and shell tools.  Don't just run the deterministic installer — read it as the spec and re-do each step interactively so you can adapt to the user's answers and recover from errors.  The deterministic parts (`pip install -r backend/requirements*.txt`, `npm install`, venv creation) are fine to shell out for; the conversational/branching parts (axis decisions, context import vs. fresh, optional symlinks, CLI runtime seeding) you handle directly.
> 5. **Log your progress to `context/install.log` continuously.**  At the start of each step, append a line like `[2026-05-16 14:32] step 4: creating venv ...`.  Append the outcome on completion or error.  If the install crashes, this log lets the user (or a future agent) resume manually.
> 6. **Never invent steps.**  If the per-OS installer doesn't do something, neither should you.  If you're unsure, read the relevant block and follow it literally.
> 7. **Never skip the API key reminders.**  Step 12 warns about missing keys for the axes the user picked — do the same warning.
> 7b. **Help the user actually obtain keys when they're missing.**  This is where you add value over the deterministic installer.  For each missing key you'd warn about: tell the user *what* it's for, *where* to get it, and offer to walk them through the signup / console flow.  Voice especially — Vertex AI vs. AI Studio is a real choice with real trade-offs (see "Voice provider selection (Gemini Live)" further down this file).  Don't just say "set `GEMINI_API_KEY`"; ask what they want voice for, propose the right backend, and stay with them through the console clicks.  Re-run `install/probe-gemini-voice.py` after the user pastes a key — its JSON output tells you immediately whether the new key works, and if not, the `reason` field has the actionable next step (billing, ADC, allowlist enablement) you should surface verbatim.  The deterministic installer runs the probe once at Step 12b; you should run it again whenever the user changes a relevant env var so the verdict tracks reality.
> 8. **Windows-specific**: if you're on Windows, check whether symlink creation works before attempting any link steps (the installer's `Test-Symlinks` does this).  If it fails, tell the user about Developer Mode before falling back to junctions + copies.  Path mangling for the CLI project dirs replaces both `\` and `:` with `-`.
> 9. **When everything is done**, ask the user if they'd like you to start the backend (`context/scripts/run.sh -m uvicorn api.app:create_app --factory --host 0.0.0.0 --port 8765` on POSIX, or `.venv\Scripts\python.exe -m uvicorn api.app:create_app --factory --app-dir backend --port 8765` on Windows) and/or the frontend (`cd apps/web && npm run dev`) in the background.  If yes, launch them in background mode so they survive your exit, then tell the user the install is complete and they can press Ctrl-C to exit this session.  Confirm the services are reachable before declaring victory.
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

- **Python 3.11+** (3.12 recommended; the always-on server runs 3.11, and the per-OS prerequisite checkers accept 3.11 or newer)
- **Node.js 22.12+** for the web frontend's toolchain (`apps/web/package.json` `engines`); Qwen Code and Gemini CLI also depend on Node.  A backend-only host without a usable Node (e.g. glibc < 2.28) can skip it — see [Backend-only hosts](#backend-only-hosts-no-nodejs)
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
- **macOS** offers to bootstrap **Homebrew** and install Python 3.12 + Node 22 (`node@22`) via brew.  Handles both Apple Silicon (`/opt/homebrew`) and Intel (`/usr/local`).
- **Windows** offers to install Python 3.12 + Node LTS + Git via **winget** (Microsoft's built-in package manager on Windows 10 1809+ / Windows 11).

You'll also need at least one of these API keys *somewhere* — either obtained ahead of time, or set up during install:

- **Claude Code** — uses Anthropic OAuth (`claude auth login`).  No `ANTHROPIC_API_KEY` needed unless you're also using the Anthropic SDK in the orchestrator.
- **Qwen Code** — either OAuth (interactive on first `qwen` run) or `DASHSCOPE_API_KEY` in `context/.env`.
- **Gemini CLI** — `GEMINI_API_KEY` in `context/.env` (create one at https://aistudio.google.com/apikey).  Google stopped serving Gemini CLI to personal Google-account logins (OAuth) on 2026-06-18, so an OAuth login no longer works.
- **Codex CLI** — a ChatGPT login (`CODEX_HOME=~/.codex-archie codex login --device-auth`; an existing `~/.codex` login also works).  No API key; Archie deliberately keeps `OPENAI_API_KEY` away from Codex.
- **Claude Code · Model Studio** — `DASHSCOPE_API_KEY` in `context/.env` (Alibaba Model Studio).  No extra CLI: it runs the Claude Code CLI bundled with `claude-agent-sdk`.
- **Orchestrator backends** — `OPENAI_API_KEY` for OpenAI/GPT/Qwen-via-compatible/Gemini-via-compatible; `ANTHROPIC_API_KEY` for Anthropic Claude models in the orchestrator.
- **Gemini Live voice** (optional) — see [Voice provider selection](#voice-provider-selection-gemini-live) below.  Two interchangeable backends, neither required, but at least one needs config if you want Google voice.

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
- **Gemini CLI** (Google) — `GEMINI_API_KEY` only (personal-account OAuth is no longer served).
- **Codex CLI** (OpenAI) — GPT models with a ChatGPT login.
- **Claude Code · Model Studio** (`--with-modelstudio`) — Claude Code's agent loop on GLM / DeepSeek / Kimi / Qwen through Alibaba Model Studio's Anthropic-compatible endpoint.  Nothing to install beyond `backend/requirements-claude.txt` (pulled in automatically) and the `.claude_config/` links; needs `DASHSCOPE_API_KEY`.

If you pick multiple, the UI's Session Provider selector lets you switch per chat.  Default for new chats is set in `assistant_config.json` (`provider` field): the first installed harness in the order Claude, Qwen, Gemini, Codex, Model Studio.

CLI version pins for every installer live in **`install/harness-versions.env`** (`QWEN_CLI_VERSION`, `GEMINI_CLI_VERSION`, `CODEX_CLI_VERSION`, `NODE_MIN_MAJOR`); the claude-agent-sdk pin (which bundles the Claude Code CLI) is in `backend/requirements-claude.txt`.

### Backend-only hosts (no Node.js)

The Linux / macOS installer detects a host where `node` is missing or does not start (or take `--no-node`) and does a backend-only install: no `npm install` for the web app (build `apps/web` on another machine and copy `apps/web/dist` + `apps/web/dist-compat` in), Qwen Code and Gemini CLI skipped (reach them through an SSH working directory on a machine that runs them — `assistant_config.json` `ssh_host`), Claude / Model Studio unaffected (bundled CLI), and Codex installed as the static binary from its GitHub release: `https://github.com/openai/codex/releases/download/rust-v<CODEX_CLI_VERSION>/codex-<arch>-unknown-linux-musl.tar.gz` (`apple-darwin` on macOS), extracted to `/usr/local/bin/codex` (writable or via sudo) or `~/.local/bin/codex` (then set `CODEX_CLI_PATH` in `context/.env`, since a systemd service's `PATH` usually lacks `~/.local/bin`).  Windows has no such mode.

### Axis 2: Orchestrator backends

The orchestrator agent (text + voice) is independent of the harness.  Which API SDK(s) should it use?

1. **OpenAI only** — GPT models, Qwen / Gemini / GLM via the OpenAI-compatible endpoint, and OpenAI Realtime voice.  Recommended for Qwen-only setups.
2. **Anthropic only** — Claude models in the orchestrator picker.
3. **Both** — full flexibility.
4. **Neither** — orchestrator disabled, chats only.

---

## Voice provider selection (Gemini Live)

The Google voice provider has **two interchangeable backends** for the same Live API.  You can switch between them at any time from the Config page (Voice mode → Endpoint dropdown), but the installer asks Google which one your account currently has access to so it can pick a sensible default.

| Backend | URL | Auth | Stability | When to use |
|--|--|--|--|--|
| **Vertex AI** (default) | `wss://{location}-aiplatform.googleapis.com` | OAuth Bearer token from Application Default Credentials (GCP IAM) | Stable — gated by your project's Vertex AI API enablement + billing | Recommended.  Once set up, doesn't get revoked. |
| **AI Studio** (legacy) | `wss://generativelanguage.googleapis.com` | `?key=$GEMINI_API_KEY` query param | Unstable — Google periodically revokes preview-model access (WS close 1008 *"Your project has been denied access"*) | Use when AI Studio happens to have a preview model Vertex doesn't yet mirror (e.g. `gemini-3.1-flash-live-preview` as of mid-2026). |

You don't have to choose between them.  Set up whichever has working access, and the assistant uses that one.  Both is fine too — the Config page dropdown lets you flip per session.

### Setting up Vertex AI (recommended)

1. **Cloud project** — [create one](https://console.cloud.google.com/projectcreate) or pick an existing one.
2. **Enable Vertex AI API** — [console link](https://console.cloud.google.com/apis/library/aiplatform.googleapis.com) (substitute your project in the picker at the top).  Click "Enable".
3. **Link billing** — Vertex AI Live requires a billing account, even with no spend.  [Billing link](https://console.cloud.google.com/billing/linkedaccount).
4. **Application Default Credentials**:
   ```bash
   gcloud auth application-default login
   gcloud auth application-default set-quota-project <PROJECT_ID>
   ```
   This writes `~/.config/gcloud/application_default_credentials.json`.  No service-account key file required.
5. **Fill in `context/.env`**:
   ```
   GCP_PROJECT_ID=<numeric project id, e.g. 493034518147>
   GCP_LOCATION=us-central1
   ```

### Setting up AI Studio (legacy)

1. **Get an API key** at [aistudio.google.com/apikey](https://aistudio.google.com/apikey).
2. **Fill in `context/.env`**: `GEMINI_API_KEY=AIzaSy...`.

That's it — no project setup needed (AI Studio creates one for you).  Just be aware Google may revoke preview-model access without notice; if voice suddenly stops working with WS 1008, switch the Config page Endpoint dropdown to Vertex.

### How the installer picks a default

`install.sh` Step 12b runs `install/probe-gemini-voice.py` after env-key checks.  The script opens both upstream WebSockets in parallel and looks for `setupComplete`.  If both work, it picks Vertex.  If one works, it picks that one.  If neither does, it warns and leaves `default_voice_endpoint=vertex` (so it'll be tried first when you finish configuration later).

You can re-run the probe anytime: `.venv/bin/python install/probe-gemini-voice.py` prints a JSON report telling you exactly what's broken and how to fix it.

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

- `context/skills/` — symlinks to every `shared/skills/*/`
- `context/scripts/` — symlinks to every `shared/scripts/*` (except `__pycache__`)
- `context/agents/` — symlinks to every `shared/agents/*`

These let `context/` reach the public framework while keeping personal additions in the same directory.  Existing entries are never replaced.  After adding something to `shared/`, re-run `shared/scripts/setup-context.sh` to link it.

### Step 3 (a, b, c, c2, c3): Per-harness SDK config dirs

For each enabled harness, create the project-local config dir the CLI expects:

- **Claude** — `.claude_config/` symlinked into `context/`.  Specifically: `.claude_config/projects/<mangled-cwd>` → `context/`, plus `.claude_config/skills` → `context/skills` and `.claude_config/agents` → `context/agents` (the bundled CLI finds skills and agents under `$CLAUDE_CONFIG_DIR`, which `run.sh` points at `.claude_config/`).
- **Claude / Model Studio** — the same links serve both harnesses (Model Studio runs the same bundled CLI with the same `CLAUDE_CONFIG_DIR`).  A wrong existing link is reported and left alone; an empty real `skills`/`agents` directory is replaced.
- **Qwen** — `~/.qwen/projects/<mangled-cwd>` → `context/`, and `~/.qwen/skills` → `context/skills`.  Claude Code and Qwen both mangle the cwd by replacing every character that is not a letter or digit with `-`, e.g. `-home-rodrigo-assistant` (`/home/me/my.repo` → `-home-me-my-repo`).
- **Gemini** — `~/.gemini/tmp/<label>/` → `context/`, so the CLI writes `context/chats/session-*.jsonl` and its project memory index `~/.gemini/tmp/<label>/memory/MEMORY.md` *is* `context/memory/MEMORY.md` (Gemini loads it into every session — that is how Gemini gets Archie's memory).  `<label>` is the one `~/.gemini/projects.json` assigns to the repo path, else the one the CLI would claim on its first run (the folder name, or `<name>-1`… when taken): `python3 install/gemini-project.py <repo> label` prints it.  Then pin it: `python3 install/gemini-project.py <repo> apply --label <label>` registers `<repo> → <label>` in `projects.json` (on Windows the key is the lower-cased path) and makes the ownership marker `context/.project_root` name the repo — a marker naming another path (an imported context from a machine with a different path) makes Gemini claim a new label, and chats and memory silently leave `context/`.  Warn if `~/.gemini/GEMINI.md` exists: Gemini's prompt offers it as a global memory tier, outside `context/` (not synced, not indexed).  If other machines will run Gemini sessions on this host over SSH, `GEMINI_API_KEY` must also be in `~/.gemini/.env` (mode 600) — the non-interactive SSH shell never sources `context/.env`; the Linux/macOS installer offers to copy it.
- **Codex** (Step 3c2) — Archie's own Codex home `~/.codex-archie` (`%USERPROFILE%\.codex-archie` on Windows): seed `config.toml` if missing (`project_doc_max_bytes = 131072` so the ~51 KB `context/AGENTS.md` isn't truncated; `[features] plugins = false`, `apps = false`, `memories = false`).  For an existing `config.toml` run `python3 install/codex-home-config.py ~/.codex-archie/config.toml apply`: it adds or replaces only `[features] memories = false` (Codex's own memory store would live under `CODEX_HOME`, outside the wiki; the backend passes `MEMORY.md` per session instead).  Link `~/.codex-archie/sessions` → `context/codex/sessions` (if it is a real directory, copy its rollouts in and move it aside).  Never copy `auth.json` between homes or machines — ChatGPT refresh tokens rotate.

- **Repo skills** (Step 3c3, when Codex, Gemini or Qwen is enabled) — `.agents/skills` → `../context/skills`.  Codex 0.161 lists skills from `<repo>/.agents/skills` (verified with `codex debug prompt-input`), Gemini CLI reads it as its workspace-skills alias, and Qwen Code as a project skill dir.

The exact mangling logic is in the per-OS installer — read it there.  Idempotent: re-runs leave existing symlinks alone.

**Windows path mangling**: on Windows the same rule turns `C:\Users\you\assistant` into `C--Users-you-assistant`; Qwen lower-cases the path first (`c--users-you-assistant`).  If a CLI version uses a different mangle, run the CLI once (it'll create its own real dir under `%USERPROFILE%\.<cli>\projects\`), then re-run `install.ps1` — it detects the real dir and replaces it with a link to `context\` (after migrating any chat history).

**Symlinks on Windows**: real symbolic links require Developer Mode or Administrator.  Without those, the installer falls back to NTFS junctions (directories) and copies (files).  See the [Windows section](#windows-only-prerequisites) above for how to enable Developer Mode.

**Memory and history, per harness** — after Steps 3–3e every harness writes its conversations into `context/` (picked up by the history indexer every 5 minutes: `context/*.jsonl`, `context/chats/*.jsonl`, `context/codex/sessions/**`) and reads/writes the same memory wiki: Claude Code / Model Studio, Qwen and Codex get `context/memory/MEMORY.md` from the backend (Claude's auto-memory is switched off by the backend with `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, so `autoMemoryEnabled` in `.claude_config/settings.json` no longer matters), Gemini loads it natively through the `tmp/<label>` link, and all of them write memory with their file tools following `AGENTS.md`.  The table in `install/README.md` lists what each harness depends on.  Codex rollouts only land in `context/` once `~/.codex-archie` has its own login.

### Step 3d: AGENTS.md symlinks

`context/AGENTS.md` is the canonical project-instructions file.  Symlink the per-CLI shadows at the repo root:

- `CLAUDE.md` → `context/AGENTS.md`
- `QWEN.md` → `context/AGENTS.md`
- `GEMINI.md` → `context/AGENTS.md`
- `AGENTS.md` → `context/AGENTS.md` (Codex reads `AGENTS.md`)

The repo commits all four links; the installers re-create any that are missing (and on Windows replace git's text stubs).

### Step 3e: Seed local CLI runtime dirs

For each enabled harness, seed the project-local runtime dir from `install/cli-runtime/<cli>/`:

- `install/cli-runtime/claude/settings.json` → `.claude/settings.json`
- `install/cli-runtime/qwen/settings.json` → `.qwen/settings.json`
- `install/cli-runtime/gemini/settings.json` → `.gemini/settings.json`

These hold default permission allowlists, Qwen's `memory.enableManagedAutoMemory` / `enableManagedAutoDream` / `enableAutoSkill` = `false` (Qwen's project dir is `context/`, so its auto-memory would write into the memory wiki) and the Gemini `respectGitIgnore=false` carve-out.  Never overwrite existing files — re-runs on a working setup must be no-ops — except that an existing `.qwen/settings.json` gets those three memory keys set to `false` if they aren't.  For Gemini, then run `python3 backend/manager/gemini/workspace_settings.py <repo>` (stdlib-only; on Windows the installer runs it with the venv's Python after Step 4): it merges Archie's keys (session retention off, `context.fileFiltering`, API-key auth, thinking overrides) into an existing `.gemini/settings.json`.

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
.venv/bin/pip install -r backend/requirements.txt         # Linux / macOS
.venv\Scripts\pip.exe install -r backend\requirements.txt # Windows
```

Then conditionally (use the venv pip path matching your OS):

- `--with-anthropic` / `-WithAnthropic` → `pip install -r backend/requirements-anthropic.txt`
- `--with-openai` / `-WithOpenAI` → `pip install -r backend/requirements-openai.txt`
- `--with-claude` / `-WithClaude` or `--with-modelstudio` / `-WithModelStudio` → `pip install -r backend/requirements-claude.txt` (claude-agent-sdk, which bundles the Claude Code CLI both harnesses run)
- `--with-qwen` / `-WithQwen` → no extra Python deps (Qwen runs as a subprocess via the CLI)
- `--with-gemini` / `-WithGemini` → no extra Python deps
- `--with-codex` / `-WithCodex` → no extra Python deps
- `--dev` / `-Dev` → `pip install -r backend/requirements-dev.txt`

### Step 7: Frontend deps

```bash
(cd apps/web && npm install)
(cd apps/design-tokens && npm install)   # the web build's token gate needs it
```

Skipped on a [backend-only host](#backend-only-hosts-no-nodejs).

`apps/web/` is one project with two builds: `npm run build` writes `apps/web/dist` (served at `/`) and `apps/web/dist-compat` (Safari 12 / iOS 12 build, served at `/compat/`). The previous web apps are kept under `legacy/frontend/` and `legacy/frontend-compat/` (served at `/legacy/` and `/legacy_compat/` once built); they are optional — install their deps (`npm install` in each) only if you want those builds.

### Step 7b: Agent CLI install + first-run login

For each enabled harness:

1. **Check if the CLI is on PATH.**  If yes, note the path and skip the install.
2. **If missing:** ask the user `Install <cli> globally via npm? [Y/n]`.  If yes: `npm install -g <pkg>`.  Packages (Qwen, Gemini and Codex are pinned to the versions in `install/harness-versions.env`):
   - `claude` → `@anthropic-ai/claude-code` (optional — only for `claude auth login`; the backend runs the CLI bundled with claude-agent-sdk)
   - `qwen` → `@qwen-code/qwen-code@<QWEN_CLI_VERSION>` (0.25.0; needs Node.js 22+)
   - `gemini` → `@google/gemini-cli@<GEMINI_CLI_VERSION>` (0.63.0)
   - `codex` → `@openai/codex@<CODEX_CLI_VERSION>` (0.161.0); without npm, the static binary from the GitHub release (see [Backend-only hosts](#backend-only-hosts-no-nodejs))
   - `modelstudio` → nothing to install; check `DASHSCOPE_API_KEY`

   If the CLI is already installed at a different version, warn and show the `npm install -g <pkg>@<pin>` command; don't reinstall without asking.
3. **Check auth state.**  Skip the login prompt entirely if:
   - The relevant env key is already set in `context/.env` (`CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` for Claude, `DASHSCOPE_API_KEY` for Qwen), **or**
   - The CLI's credential file already exists (`~/.claude/.credentials.json`, `~/.qwen/oauth_creds.json`, `~/.codex-archie/auth.json` or `~/.codex/auth.json`).

   Gemini has no login step: it needs `GEMINI_API_KEY` in `context/.env` — warn if it's missing (an old `~/.gemini/oauth_creds.json` doesn't count).
4. **Otherwise**: tell the user to open a separate terminal and run the login command (`claude auth login`, `qwen`, or `CODEX_HOME=~/.codex-archie codex login --device-auth`), then come back and confirm.  Re-check auth state after.

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
- `@@DEFAULT_PROVIDER@@` → the first installed harness in the order `claude`, `qwen`, `gemini`, `codex`, `modelstudio`
- `@@DEFAULT_MODEL@@` → the orchestrator's default model: `qwen3.6-plus` when the default provider is `qwen` or `modelstudio`, otherwise Claude Sonnet

### Step 11: .manager.json

Copy `install/manager.json` to `.manager.json` at the repo root.  No substitutions.

### Step 12: Verification

For each axis the user opted into, check that the required env key is set in `context/.env` and warn if not.  Don't fail the install — keys can be added later.  Specifically:

- `--with-anthropic` needs `ANTHROPIC_API_KEY`
- `--with-openai` needs `OPENAI_API_KEY`
- `--with-qwen` needs either `DASHSCOPE_API_KEY` or for OAuth login to have happened
- `--with-gemini` needs `GEMINI_API_KEY` (OAuth no longer works)
- `--with-codex` needs a Codex login (`~/.codex-archie/auth.json` or `~/.codex/auth.json`) — no env key
- `--with-modelstudio` needs `DASHSCOPE_API_KEY`

Then run **`install/doctor.sh --harness <the chosen ones>`** (the Linux / macOS installers do this as their last step).  It prints one OK/WARN/FAIL row per check, per harness: CLI present and at the pinned version, env keys (names only), auth files (existence only), every symlink from Steps 3–3d, the Qwen / Gemini seed settings, the memory/history wiring (`context/memory/MEMORY.md`, Gemini's `projects.json` label, ownership markers and memory index, `~/.gemini/GEMINI.md`, Codex's `memories` key; Claude's auto-memory as information), and the `context/{skills,scripts,agents}` links.  `install/doctor.sh --dry-run` lists what `--fix` would repair; `--fix` repairs only symlinks, seed files and seed keys (never packages or auth).  Re-run it any time something looks off.

### Step 12b: Voice backend probe

Runs `install/probe-gemini-voice.py` to ask both Gemini Live backends (AI Studio + Vertex) whether they answer.  The probe is independent of which axes the user picked — voice is always available in the orchestrator.  Outcomes:

- **Both work** — set `assistant_config.json:default_voice_endpoint = "vertex"` (more stable).
- **Only one works** — set that one as default.
- **Neither works** — print actionable hints from the probe's `reason` fields (e.g. *"GCP_PROJECT_ID not set — create a Cloud project at ..."* or *"Run `gcloud auth application-default login`"*) and leave the default as `vertex` so it'll be tried first once configured.

The probe respects existing user choices: it only writes `default_voice_endpoint` if it's missing or still on the install template's default (`"vertex"`).

### After install

**Linux / macOS:**

```bash
# Terminal 1 — Backend
context/scripts/run.sh -m uvicorn api.app:create_app --factory --host 0.0.0.0 --port 8765

# Terminal 2 — Frontend
cd apps/web && npm run dev
```

**Windows:**

```powershell
# Terminal 1 — Backend
.venv\Scripts\python.exe -m uvicorn api.app:create_app --factory --port 8765

# Terminal 2 — Frontend
cd apps/web; npm run dev
```

Open **https://localhost:5450** (the Vite dev server; plain `http://` when no certificate is present in `context/certs/`) and start chatting. The Safari 12 build has its own dev server: `npm run dev:compat` (port 5451). Alternatively, `cd apps/web && npm run build` once and use the backend directly at `http://localhost:8765/` (it serves `apps/web/dist` at `/` and `apps/web/dist-compat` at `/compat/`).

If you used the conversational installer, the agent can offer to start both in the background for you.

---

## Troubleshooting

- **`npm install -g` fails with EACCES** — your global npm prefix needs write permission, or use a Node version manager like `nvm` / `volta` / `mise` instead of system Node.
- **The CLI is installed but not on PATH** — happens with `nvm` if the shell that runs the install isn't logged in as the nvm-using user.  Set `QWEN_CLI_PATH` (or the equivalent for Gemini) in `context/.env`.
- **Symlinks point at the wrong place after copying `context/`** — run `install/doctor.sh --dry-run` to see which, then `install/doctor.sh --fix` (or re-run `./install.sh`).  Both re-create missing links idempotently and never clobber a link that points somewhere else — remove that one by hand first.
- **The install crashed mid-way** — read `context/install.log` (if you used the agent installer) or check what step `install.sh` was on (it prints `Step N` headers as it goes).  Most steps are independently re-runnable; symlink steps are idempotent.

For deeper issues, the canonical reference is `install.sh` itself — every step has a comment block explaining what it does and why.
