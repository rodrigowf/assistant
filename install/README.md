# install/ — Fresh Install Templates

This directory holds the **template files** that `install.sh` copies into a
fresh checkout to bootstrap a new installation. Each file is a clean,
user-agnostic starting point — no secrets, no personal context, no machine-
specific paths baked in.

If you're setting up the assistant manually (without `install.sh`), copy these
files into place yourself and edit them. The install script is the
recommended path though — it handles symlinks, axis selection, and
substitutions for you.

## Files

| Template | Copied to | Purpose |
|----------|-----------|---------|
| `AGENTS.md` | `context/AGENTS.md` | Project instructions read by Claude Code (via the `CLAUDE.md` symlink at the project root) and Qwen Code (via the `QWEN.md` symlink). |
| `MEMORY.md` | `context/memory/MEMORY.md` | The shared memory index. Topic files referenced from here live alongside it. |
| `context.env` | `context/.env` | API keys and runtime configuration. Comments explain which axis each key belongs to; uncomment and fill in what you need. |
| `assistant_config.json` | `assistant_config.json` (repo root) | Default working directory, provider, and model picked up by the API on first run. Placeholders `@@SCRIPT_DIR@@`, `@@DEFAULT_PROVIDER@@`, `@@DEFAULT_MODEL@@` are substituted at install time. |
| `manager.json` | `.manager.json` (repo root) | Session-manager defaults (model, permission mode, budget caps). |
| `sync.env` | `sync/config.env` | Optional. Configures the `context-sync` systemd service for two-machine deployments. |
| `cli-runtime/<cli>/*` | `.<cli>/*` | Seeds the per-CLI runtime dirs (`.claude/`, `.qwen/`, `.gemini/`) at the project root with default `settings.json` and any other starter files. These dirs are gitignored on disk — the templates here are what gets dropped in on first install. Only the dirs for harnesses the user opts into are seeded. Existing files are never overwritten. |

## How install.sh uses these

`install.sh` reads the templates from this directory at the appropriate steps:

- **Context bootstrap (new install path)** — `MEMORY.md` and `AGENTS.md` are copied into `context/memory/` and `context/` respectively. `context.env` is copied into `context/.env`, and the keys for the axes the user opted into get uncommented so they show up as required.
- **AGENTS.md migration (existing-install path)** — If `context/AGENTS.md` already exists (e.g. legacy `AGENTS.md` at the repo root, or a real `CLAUDE.md` at root), the install script normalises it into `context/AGENTS.md`. The template here is only used for *fresh* installs, never to overwrite.
- **Config files** — `assistant_config.json` and `.manager.json` are copied into the repo root with placeholders substituted (`@@SCRIPT_DIR@@`, `@@DEFAULT_PROVIDER@@`, `@@DEFAULT_MODEL@@`).
- **CLI runtime seeds (Step 3e)** — For each enabled harness (`--with-claude` / `--with-qwen` / `--with-gemini`), files under `cli-runtime/<cli>/` are copied into `.<cli>/` at the project root. Drop additional starter files in here (default permission allowlists, file-filter carve-outs, ignore files) and they'll land on every fresh install.
- **Agent CLI install + login (Step 7b)** — After npm + Python deps are in, `install.sh` installs each enabled harness CLI via `npm install -g` (if missing), then pauses to walk the user through the first interactive login. `--skip-auth` bypasses this entire step; non-interactive shells (no TTY) skip the login prompt silently but still install the CLI.
- **Sync** — `sync.env` stays an opt-in step; copy it manually to `sync/config.env` if you want the systemd sync service.

Any file in this directory is safe to edit if you want to change the default a
fresh install lands on. Keep this directory user-agnostic — personal content
belongs in `context/` (which is gitignored), not here.
