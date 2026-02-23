#!/usr/bin/env bash
# Usage: default-scripts/setup-context.sh [--force]
# Description: Set up the context folder structure and SDK symlinks.
#
# This script:
#   1. Creates the context folder structure if it doesn't exist
#   2. Creates symlinks to default-skills, default-scripts, default-agents
#   3. Sets up the Claude SDK compatibility symlink
#   4. Creates a template .env file if none exists
#
# Options:
#   --force    Recreate symlinks even if they exist
#   -h, --help Show this help message
set -euo pipefail

# Resolve to project root (works whether called directly or via symlink)
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(dirname "$SCRIPT_PATH")"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info() { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}!${NC} $1"; }
step() { echo -e "${BLUE}→${NC} $1"; }

# Parse arguments
FORCE=false
for arg in "$@"; do
    case $arg in
        --force)
            FORCE=true
            ;;
        -h|--help)
            echo "Usage: default-scripts/setup-context.sh [--force]"
            echo ""
            echo "Set up the context folder structure and SDK symlinks."
            echo ""
            echo "Options:"
            echo "  --force    Recreate symlinks even if they exist"
            echo "  -h, --help Show this help message"
            exit 0
            ;;
    esac
done

echo "Setting up context for: $PROJECT_DIR"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Step 1: Create context folder structure
# ─────────────────────────────────────────────────────────────────────────────
step "Creating context folder structure..."

mkdir -p context/{memory,skills,scripts,agents,secrets,certs}
info "Created context subdirectories"

# ─────────────────────────────────────────────────────────────────────────────
# Step 2: Create symlinks to default-skills
# ─────────────────────────────────────────────────────────────────────────────
step "Creating skill symlinks..."

# Use relative paths for portability
cd context/skills
for skill_dir in ../../default-skills/*/; do
    skill_name=$(basename "$skill_dir")

    if [ "$FORCE" = true ] && [ -L "$skill_name" ]; then
        rm "$skill_name"
    fi

    if [ ! -e "$skill_name" ]; then
        ln -s "../../default-skills/$skill_name" "$skill_name"
        info "Linked $skill_name"
    fi
done
cd ../..

# ─────────────────────────────────────────────────────────────────────────────
# Step 3: Create symlinks to default-scripts
# ─────────────────────────────────────────────────────────────────────────────
step "Creating script symlinks..."

# Use relative paths for portability
cd context/scripts
for script_file in ../../default-scripts/*; do
    script_name=$(basename "$script_file")

    if [ "$FORCE" = true ] && [ -L "$script_name" ]; then
        rm "$script_name"
    fi

    if [ ! -e "$script_name" ]; then
        ln -s "../../default-scripts/$script_name" "$script_name"
        info "Linked $script_name"
    fi
done
cd ../..

# ─────────────────────────────────────────────────────────────────────────────
# Step 4: Create symlinks to default-agents
# ─────────────────────────────────────────────────────────────────────────────
step "Creating agent symlinks..."

# Use relative paths for portability
cd context/agents
for agent_file in ../../default-agents/*; do
    agent_name=$(basename "$agent_file")

    if [ "$FORCE" = true ] && [ -L "$agent_name" ]; then
        rm "$agent_name"
    fi

    if [ ! -e "$agent_name" ]; then
        ln -s "../../default-agents/$agent_name" "$agent_name"
        info "Linked $agent_name"
    fi
done
cd ../..

# ─────────────────────────────────────────────────────────────────────────────
# Step 5: Set up Claude SDK compatibility symlink
# ─────────────────────────────────────────────────────────────────────────────
step "Setting up Claude SDK symlink..."

# Create .claude_config structure
mkdir -p .claude_config/projects

# Create mangled path name (replace / with -)
MANGLED=$(echo "$PROJECT_DIR" | sed 's|/|-|g')
SYMLINK_PATH=".claude_config/projects/$MANGLED"

if [ "$FORCE" = true ] && [ -L "$SYMLINK_PATH" ]; then
    rm "$SYMLINK_PATH"
fi

if [ -L "$SYMLINK_PATH" ]; then
    info "SDK symlink already exists"
elif [ -e "$SYMLINK_PATH" ]; then
    warn "$SYMLINK_PATH exists but is not a symlink - skipping"
else
    ln -s "../../context" "$SYMLINK_PATH"
    info "Created SDK symlink: $SYMLINK_PATH -> ../../context"
fi

# Create skills symlink for SDK discovery
if [ "$FORCE" = true ] && [ -L ".claude_config/skills" ]; then
    rm ".claude_config/skills"
fi

if [ ! -L ".claude_config/skills" ]; then
    ln -sf "../context/skills" ".claude_config/skills"
    info "Created skills discovery symlink"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Step 6: Create template files if missing
# ─────────────────────────────────────────────────────────────────────────────
step "Checking template files..."

# Create MEMORY.md if missing
if [ ! -f "context/memory/MEMORY.md" ]; then
    cat > context/memory/MEMORY.md << 'EOF'
# Assistant Memory Index

Reference index to detailed memory files. Keep this under 200 lines.

## Getting Started

Welcome to your personal assistant! This memory file helps the AI remember
important context across sessions.

### How Memory Works

- This file (`MEMORY.md`) is an index - keep it under 200 lines
- Store detailed content in separate `.md` files in this folder
- Add one-line references here: `- filename.md - Brief description`

## Quick Reference

### Running the Assistant

1. Start the backend:
   ```bash
   context/scripts/run.sh -m uvicorn api.app:create_app --factory --port 8000
   ```

2. Start the frontend (new terminal):
   ```bash
   cd frontend && npm run dev
   ```

3. Open https://localhost:5173
EOF
    info "Created context/memory/MEMORY.md"
fi

# Create .env from .env.example if missing
if [ ! -f "context/.env" ] && [ -f "context/.env.example" ]; then
    cp context/.env.example context/.env
    info "Created context/.env from template"
    warn "Remember to edit context/.env with your API keys!"
elif [ ! -f "context/.env" ]; then
    cat > context/.env << 'EOF'
# Personal Assistant Environment Configuration
# Edit this file with your API keys

# OpenAI API key (required for voice mode)
OPENAI_API_KEY=

# Realtime voice model
REALTIME_MODEL=gpt-realtime
EOF
    info "Created context/.env template"
    warn "Remember to edit context/.env with your API keys!"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Done
# ─────────────────────────────────────────────────────────────────────────────
echo ""
info "Context setup complete!"
echo ""
echo "Structure:"
echo "  context/"
echo "  ├── *.jsonl           <- Session files"
echo "  ├── memory/           <- Memory markdown files"
echo "  │   └── MEMORY.md     <- Memory index"
echo "  ├── skills/           <- Skill folders (symlinks + custom)"
echo "  ├── scripts/          <- Script files (symlinks + custom)"
echo "  ├── agents/           <- Agent definitions (symlinks + custom)"
echo "  ├── secrets/          <- OAuth credentials"
echo "  ├── certs/            <- SSL certificates"
echo "  └── .env              <- Environment variables"
echo ""
echo "  .claude_config/"
echo "  ├── skills -> ../context/skills"
echo "  └── projects/$MANGLED -> ../../context"
echo ""
