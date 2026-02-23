#!/bin/bash
# Sets up context symlinks for a new installation.
#
# Creates the symlink structure needed for Claude SDK compatibility:
# - .claude_config/projects/<mangled-path> -> ../../context
#
# Usage: context/scripts/setup-context.sh

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONTEXT_DIR="$PROJECT_DIR/context"
CLAUDE_CONFIG="$PROJECT_DIR/.claude_config"

echo "Setting up context for: $PROJECT_DIR"

# Ensure context structure exists
mkdir -p "$CONTEXT_DIR/memory"

# Create mangled path name (replace / with -)
MANGLED=$(echo "$PROJECT_DIR" | sed 's|/|-|g')

# Ensure .claude_config/projects exists
mkdir -p "$CLAUDE_CONFIG/projects"

# Create main symlink if it doesn't exist
SYMLINK_PATH="$CLAUDE_CONFIG/projects/$MANGLED"
if [ -L "$SYMLINK_PATH" ]; then
    echo "Symlink already exists: $SYMLINK_PATH"
elif [ -e "$SYMLINK_PATH" ]; then
    echo "Warning: $SYMLINK_PATH exists but is not a symlink"
    echo "Please remove it manually and re-run this script"
    exit 1
else
    ln -s -- "../../context" "$SYMLINK_PATH"
    echo "Created symlink: $SYMLINK_PATH -> ../../context"
fi

echo ""
echo "Context setup complete!"
echo ""
echo "Structure:"
echo "  context/"
echo "  ├── *.jsonl       <- Session files (SDK writes here directly)"
echo "  ├── <uuid>/       <- SDK state dirs (subagents, tool-results)"
echo "  ├── .titles.json  <- Custom session titles"
echo "  └── memory/       <- Memory markdown files"
echo ""
echo "  .claude_config/projects/$MANGLED -> ../../context"
