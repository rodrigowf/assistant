#!/usr/bin/env bash
# install.sh — top-level OS dispatcher for the Personal Assistant installer.
#
# This is a thin wrapper that detects the host OS and execs the right
# per-OS installer under install/<os>/install.sh.  All real install logic
# lives in those per-OS scripts:
#
#   install/linux/install.sh    — Linux (Debian / Fedora / Arch / Jetson)
#   install/apple/install.sh    — macOS (Intel + Apple Silicon)
#   install/windows/install.ps1 — Windows (PowerShell 7+)
#
# Windows users can either run install.ps1 directly from PowerShell, or
# this script will redirect them.  All command-line flags are forwarded
# verbatim to the per-OS installer; see ./install.sh --help (after OS
# dispatch) for the full flag list.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case "${OSTYPE:-$(uname -s)}" in
    darwin*|Darwin*)
        OS="apple"
        ;;
    linux*|Linux*)
        OS="linux"
        ;;
    msys*|cygwin*|MINGW*|MSYS*|CYGWIN*)
        # Running under a POSIX shell layer on Windows (Git Bash / MSYS2 /
        # Cygwin).  PowerShell is the supported entry point on Windows —
        # bash inside those environments doesn't have the privileges
        # PowerShell does for symlink creation, winget, etc.
        echo "Detected POSIX shell on Windows.  Please run the PowerShell installer instead:" >&2
        echo "    powershell.exe -ExecutionPolicy Bypass -File install\\windows\\install.ps1" >&2
        echo "  or, if you have PowerShell 7+ (recommended):" >&2
        echo "    pwsh -File install\\windows\\install.ps1" >&2
        exit 1
        ;;
    *)
        echo "Unsupported OSTYPE='${OSTYPE:-unknown}' uname='$(uname -s 2>/dev/null || echo unknown)'." >&2
        echo "Supported: Linux, macOS, Windows (via install.ps1)." >&2
        exit 1
        ;;
esac

TARGET="$SCRIPT_DIR/install/$OS/install.sh"
if [ ! -x "$TARGET" ]; then
    echo "Installer not found or not executable: $TARGET" >&2
    exit 1
fi

exec "$TARGET" "$@"
