#!/usr/bin/env bash
# install-with-agent.sh — top-level OS dispatcher for the conversational installer.
#
# Routes to install/<os>/install-with-agent.sh based on the host OS.  All flags
# are forwarded verbatim.  See INSTALL.md for what the conversational installer
# does (it launches one of the agent CLIs and hands it the install procedure).
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
        echo "Detected POSIX shell on Windows.  Please run the PowerShell installer instead:" >&2
        echo "    powershell.exe -ExecutionPolicy Bypass -File install\\windows\\install-with-agent.ps1" >&2
        echo "  or, if you have PowerShell 7+ (recommended):" >&2
        echo "    pwsh -File install\\windows\\install-with-agent.ps1" >&2
        exit 1
        ;;
    *)
        echo "Unsupported OSTYPE='${OSTYPE:-unknown}' uname='$(uname -s 2>/dev/null || echo unknown)'." >&2
        echo "Supported: Linux, macOS, Windows (via install-with-agent.ps1)." >&2
        exit 1
        ;;
esac

TARGET="$SCRIPT_DIR/install/$OS/install-with-agent.sh"
if [ ! -x "$TARGET" ]; then
    echo "Installer not found or not executable: $TARGET" >&2
    exit 1
fi

exec "$TARGET" "$@"
