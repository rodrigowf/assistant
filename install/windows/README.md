# install/windows/ — Windows installer

PowerShell implementation of the Personal Assistant installer for Windows.

You normally don't run these scripts directly — the top-level `install.ps1`
and `install-with-agent.ps1` at the project root dispatch here. You can
also run them directly from PowerShell:

```powershell
.\install\windows\install.ps1
.\install\windows\install-with-agent.ps1
```

## Files

| File | Purpose |
|------|---------|
| `install.ps1` | Deterministic installer. Same two-axis decision model (session harness + orchestrator backends) as the POSIX scripts, ported to PowerShell. |
| `install-with-agent.ps1` | Conversational installer. Detects which CLI is installed, picks (or asks for) a driver, then launches it with `INSTALL.md` as its boot prompt. |
| `install-prerequisites.ps1` | Verifies Python 3.12+, Node 20+, npm, and git are present. When something is missing, offers to install it via **winget** (Microsoft's built-in package manager). |

The shared templates (`AGENTS.md`, `MEMORY.md`, `context.env`, etc.) live
one level up in `install/` and are reused across every OS.

## Windows-specific notes

### PowerShell version

- **PowerShell 7+** (`pwsh`) is recommended. Install with
  `winget install Microsoft.PowerShell`.
- **Windows PowerShell 5.1** also works (it ships with Windows 10/11).
- The scripts declare `#Requires -Version 5.1` so both are accepted.

### Execution policy

If you see "running scripts is disabled on this system" when launching the
installer, either lift the policy session-only:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\install.ps1
```

or set it for your user once:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

`RemoteSigned` is the recommended setting: lets local scripts run, blocks
downloaded ones unless signed.

### Symlinks

Windows symbolic links require **either Developer Mode enabled** OR the
shell running **as Administrator**. The installer probes for symlink
creation at the start and falls back gracefully if neither is available:

| Target | Symlinks available | Fallback |
|--------|--------------------|----------|
| Directory | Symbolic link | **NTFS junction** (no privileges; same drive) |
| File | Symbolic link | **Plain copy** (one-time; re-run installer if source changes) |

The fallback path is fully functional — the SDK config dirs work just as
well via junctions, and there are only three "shadow" markdown files
(`CLAUDE.md`, `QWEN.md`, `GEMINI.md`) that become copies if symlinks
aren't available. Re-running `install.ps1` re-syncs those copies.

**To enable Developer Mode (recommended):**

1. Open Settings → Update & Security → For Developers (Windows 10) or
   Settings → System → For Developers (Windows 11).
2. Turn on **Developer Mode**.
3. Re-run the installer. Any existing junctions/copies will be left in
   place; new links will be real symlinks.

### Prerequisites bootstrap

`install-prerequisites.ps1` uses **winget** to install Python 3.12 and
Node.js LTS when missing. Winget ships with Windows 10 1809+ / Windows
11 via the App Installer package. If `winget` is unavailable on your
machine, the script points you at the Microsoft Store link.

### Path mangling

The Claude / Qwen / Gemini CLIs each compute a "mangled" version of the
project path to use as their per-project subdirectory name. On POSIX:
`/` → `-`. On Windows: backslashes and `:` are both replaced with `-`,
so `C:\Users\you\assistant` becomes `C--Users-you-assistant`.

If a CLI version uses a different mangle than what the installer guesses,
the easiest fix is: run the CLI once (it'll create its own real dir),
then re-run `install.ps1` — it detects the real dir and replaces it with
a link to `context\` (migrating any chat history first).

### Backend startup on Windows

The Linux/macOS scripts print `context/scripts/run.sh -m uvicorn ...` as
the post-install command. On Windows there's no equivalent `run.sh`; the
installer prints the equivalent venv-direct command instead:

```powershell
.venv\Scripts\python.exe -m uvicorn api.app:create_app --factory --port 8765
```

This wraps the same env loading the POSIX `run.sh` does, just inline.

For the full step-by-step recipe, see `INSTALL.md` at the project root.
