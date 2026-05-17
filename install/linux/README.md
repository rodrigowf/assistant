# install/linux/ — Linux installer

Per-OS implementation of the Personal Assistant installer for Linux
(Debian / Ubuntu / Fedora / Arch / Jetson Linux).

You normally don't run these scripts directly — the top-level
`./install.sh` and `./install-with-agent.sh` at the project root dispatch
here based on `$OSTYPE`.

## Files

| File | Purpose |
|------|---------|
| `install.sh` | Deterministic installer. Asks two axis questions (session harness + orchestrator backends), then runs every step automatically. |
| `install-with-agent.sh` | Conversational installer. Launches one of the agent CLIs and hands it `INSTALL.md` as instructions; the agent walks the user through the install. |
| `install-prerequisites.sh` | Verifies Python 3.12+, Node 20+, npm, and git are present; prints platform-specific install commands for what's missing. Called by `install.sh` Step 1. |

The shared templates (`AGENTS.md`, `MEMORY.md`, `context.env`,
`assistant_config.json`, `manager.json`, `sync.env`, `cli-runtime/`) live one
level up in `install/` and are reused across every OS.

## Linux-specific notes

- Symlinks: native, no special setup.
- Package managers supported by the prereq installer's install hints: `apt`
  (Debian / Ubuntu), `dnf` (Fedora), `pacman` (Arch).  Other distros fall
  through to "Download from upstream" messages.
- Node: the prereq installer suggests NodeSource (`deb.nodesource.com`) on
  Debian/Ubuntu.  Using `nvm` / `volta` / `mise` works too — just make sure
  `node` and `npm` are on `$PATH` when `./install.sh` runs.

For the full step-by-step recipe (every step `install.sh` performs), see
`INSTALL.md` at the project root.
