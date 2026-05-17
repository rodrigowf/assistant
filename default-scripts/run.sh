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

# On aarch64 (Jetson Nano), preload libgomp to avoid "cannot allocate memory
# in static TLS block" errors when importing sklearn via sentence_transformers.
# Loading libgomp early ensures it gets space in the fixed-size TLS block.
if [ "$(uname -m)" = "aarch64" ] && [ -f /usr/lib/aarch64-linux-gnu/libgomp.so.1 ]; then
    export LD_PRELOAD="${LD_PRELOAD:+$LD_PRELOAD:}/usr/lib/aarch64-linux-gnu/libgomp.so.1"
fi

exec "$VENV_PYTHON" "$@"
