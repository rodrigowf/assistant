#!/usr/bin/env bash
# Usage: context/scripts/run.sh <script.py> [args...]
# Description: Run a Python script using the project's virtual environment.
set -euo pipefail

# Resolve symlinks so this works when called via context/scripts/run.sh
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"

# Use .claude_config as Claude Code data directory
# This puts sessions, memory, credentials in a dedicated folder
export CLAUDE_CONFIG_DIR="$PROJECT_DIR/.claude_config"

# Load .env file from context submodule if it exists
if [ -f "$PROJECT_DIR/context/.env" ]; then
    set -a
    source "$PROJECT_DIR/context/.env"
    set +a
fi

if [ ! -f "$VENV_PYTHON" ]; then
    echo "Error: Virtual environment not found at $PROJECT_DIR/.venv/" >&2
    echo "Run: python3 -m venv $PROJECT_DIR/.venv && $PROJECT_DIR/.venv/bin/pip install chromadb sentence-transformers claude-agent-sdk" >&2
    exit 1
fi

exec "$VENV_PYTHON" "$@"
