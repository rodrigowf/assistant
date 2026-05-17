# install/apple/ — macOS installer

Per-OS implementation of the Personal Assistant installer for macOS
(Intel and Apple Silicon).

You normally don't run these scripts directly — the top-level
`./install.sh` and `./install-with-agent.sh` at the project root dispatch
here when they detect `OSTYPE=darwin*`.

## Files

| File | Purpose |
|------|---------|
| `install.sh` | Deterministic installer (macOS variant). Same flags and behavior as the Linux version, with portable `readlink` / `sed -i` handling. |
| `install-with-agent.sh` | Conversational installer. Launches one of the agent CLIs and hands it `INSTALL.md`. |
| `install-prerequisites.sh` | Bootstraps Homebrew (if missing), then installs Python 3.12+ and Node 20+ via brew. Handles both Apple Silicon (`/opt/homebrew`) and Intel (`/usr/local`) prefixes. |

## macOS-specific notes

- **Homebrew**: the prereq installer offers to install Homebrew via the
  official one-liner if it's not on `$PATH`.  After install, you'll want to
  add `eval "$(brew shellenv)"` to your `~/.zprofile` so brew persists
  across shell sessions.
- **node@20**: the prereq installer uses the `node@20` keg-only formula
  to pin a working Node version.  The script adds it to `$PATH` for the
  remainder of the install session, but the same `brew link --overwrite
  --force node@20` step (or `brew shellenv`) is what you need to make
  it persist.
- **BSD sed**: macOS ships BSD sed, which needs an explicit backup suffix
  after `-i` (`sed -i ''`).  The installer uses that form so it works on
  both BSD and GNU sed.
- **readlink -f**: pre-Big Sur macOS ships a BSD readlink without `-f`.
  The installer's `resolve_path` helper falls back to `realpath` then
  `python3 -c 'os.path.realpath(...)'` so symlink resolution works
  everywhere.
- **Symlinks**: macOS supports POSIX symlinks natively (same as Linux).
  No special setup required.
- **Xcode Command Line Tools**: provides `git`, `make`, `clang`, etc.
  Install with `xcode-select --install` if missing.

For the full step-by-step recipe, see `INSTALL.md` at the project root.
