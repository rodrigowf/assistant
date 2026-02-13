#!/usr/bin/env bash
# Usage: scripts/run.sh <script.py> [args...]
# Description: Run a Python script using the project's virtual environment.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"

# Use .claude_config as Claude Code data directory
# This puts sessions, memory, credentials in a dedicated folder
export CLAUDE_CONFIG_DIR="$PROJECT_DIR/.claude_config"

if [ ! -f "$VENV_PYTHON" ]; then
    echo "Error: Virtual environment not found at $PROJECT_DIR/.venv/" >&2
    echo "Run: python3 -m venv $PROJECT_DIR/.venv && $PROJECT_DIR/.venv/bin/pip install chromadb sentence-transformers claude-agent-sdk" >&2
    exit 1
fi

exec "$VENV_PYTHON" "$@"
