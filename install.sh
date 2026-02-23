#!/usr/bin/env bash
# Usage: ./install.sh [OPTIONS]
# Description: Complete installation script for the Personal Assistant.
#
# This script handles:
#   1. Checking and installing system prerequisites
#   2. Setting up the context submodule (new or import existing)
#   3. Installing Python and Node.js dependencies
#   4. Configuring the environment
#
# Options:
#   --dev           Install development dependencies (linting, type checking)
#   --skip-prereqs  Skip prerequisite checks
#   --new-context   Create a fresh context (skip interactive prompt)
#   --import-context URL  Import existing context repository (skip interactive prompt)
#   -h, --help      Show this help message
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ─────────────────────────────────────────────────────────────────────────────
# Colors and output helpers
# ─────────────────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info() { echo -e "${GREEN}✓${NC} $1"; }
step() { echo -e "${BLUE}→${NC} $1"; }
warn() { echo -e "${YELLOW}!${NC} $1"; }
error() { echo -e "${RED}✗${NC} $1"; exit 1; }
ask() { echo -e "${CYAN}?${NC} $1"; }

# ─────────────────────────────────────────────────────────────────────────────
# Parse arguments
# ─────────────────────────────────────────────────────────────────────────────
DEV_MODE=false
SKIP_PREREQS=false
NEW_CONTEXT=false
IMPORT_CONTEXT=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --dev)
            DEV_MODE=true
            shift
            ;;
        --skip-prereqs)
            SKIP_PREREQS=true
            shift
            ;;
        --new-context)
            NEW_CONTEXT=true
            shift
            ;;
        --import-context)
            IMPORT_CONTEXT="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: ./install.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --dev                Install development dependencies"
            echo "  --skip-prereqs       Skip prerequisite checks"
            echo "  --new-context        Create a fresh context (non-interactive)"
            echo "  --import-context URL Import existing context repository"
            echo "  -h, --help           Show this help message"
            echo ""
            echo "Examples:"
            echo "  ./install.sh                           # Interactive installation"
            echo "  ./install.sh --new-context             # Fresh install with new context"
            echo "  ./install.sh --import-context git@github.com:user/context.git"
            exit 0
            ;;
        *)
            error "Unknown option: $1. Use --help for usage."
            ;;
    esac
done

# ─────────────────────────────────────────────────────────────────────────────
# Header
# ─────────────────────────────────────────────────────────────────────────────
clear
echo -e "${BOLD}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "           Personal Assistant Installer"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${NC}"
echo "A transparent, hackable AI assistant that evolves with you."
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Step 1: Check prerequisites
# ─────────────────────────────────────────────────────────────────────────────
if [ "$SKIP_PREREQS" = false ]; then
    step "Checking prerequisites..."
    echo ""

    if ! bash default-scripts/install-prerequisites.sh; then
        echo ""
        error "Please install missing prerequisites and try again."
    fi
    echo ""
fi

# ─────────────────────────────────────────────────────────────────────────────
# Step 2: Context setup
# ─────────────────────────────────────────────────────────────────────────────
step "Setting up context..."
echo ""

CONTEXT_SETUP_NEEDED=false

# Check if context already exists and is properly configured
if [ -d "context" ] && [ -f "context/memory/MEMORY.md" ]; then
    info "Context folder already exists and is configured"
    echo ""
    ask "Do you want to keep the existing context? [Y/n]"
    read -r KEEP_CONTEXT
    if [[ "$KEEP_CONTEXT" =~ ^[Nn]$ ]]; then
        warn "Backing up existing context to context.bak/"
        rm -rf context.bak
        mv context context.bak
        CONTEXT_SETUP_NEEDED=true
    fi
elif [ -d "context" ]; then
    # Context folder exists but might be empty/incomplete
    if [ -z "$(ls -A context 2>/dev/null)" ]; then
        # Empty directory - remove it
        rmdir context 2>/dev/null || true
        CONTEXT_SETUP_NEEDED=true
    else
        warn "Context folder exists but may be incomplete"
        CONTEXT_SETUP_NEEDED=true
    fi
else
    CONTEXT_SETUP_NEEDED=true
fi

if [ "$CONTEXT_SETUP_NEEDED" = true ]; then
    # Determine context setup mode
    if [ -n "$IMPORT_CONTEXT" ]; then
        CONTEXT_MODE="import"
        CONTEXT_URL="$IMPORT_CONTEXT"
    elif [ "$NEW_CONTEXT" = true ]; then
        CONTEXT_MODE="new"
    else
        # Interactive mode
        echo ""
        echo "The context folder stores your personal data:"
        echo "  - Conversation history"
        echo "  - Memory files"
        echo "  - Custom skills and scripts"
        echo "  - API credentials"
        echo ""
        echo "Choose how to set up your context:"
        echo ""
        echo "  ${BOLD}1)${NC} ${GREEN}New installation${NC} - Start fresh with an empty context"
        echo "  ${BOLD}2)${NC} ${BLUE}Import existing${NC} - Clone your existing context repository"
        echo ""
        ask "Enter choice [1/2]: "
        read -r CONTEXT_CHOICE

        case "$CONTEXT_CHOICE" in
            1)
                CONTEXT_MODE="new"
                ;;
            2)
                CONTEXT_MODE="import"
                ask "Enter your context repository URL (e.g., git@github.com:user/assistant-context.git): "
                read -r CONTEXT_URL
                ;;
            *)
                error "Invalid choice. Please run the installer again."
                ;;
        esac
    fi

    echo ""

    # Execute context setup
    case "$CONTEXT_MODE" in
        new)
            step "Creating fresh context..."

            # Initialize context folder structure
            mkdir -p context/{memory,skills,scripts,agents,secrets,certs}

            # Create initial MEMORY.md
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

### Useful Commands

| Command | Description |
|---------|-------------|
| `/recall <query>` | Search memory and history |
| `/scaffold-skill` | Create a new skill |
| `/scaffold-agent` | Create a new agent |
EOF

            # Create symlinks to default-skills (using relative paths for portability)
            step "Creating skill symlinks..."
            cd context/skills
            for skill in ../../default-skills/*/; do
                skill_name=$(basename "$skill")
                if [ ! -e "$skill_name" ]; then
                    ln -s "../../default-skills/$skill_name" "$skill_name"
                fi
            done
            cd ../..

            # Create symlinks to default-scripts (using relative paths for portability)
            step "Creating script symlinks..."
            cd context/scripts
            for script in ../../default-scripts/*; do
                script_name=$(basename "$script")
                if [ ! -e "$script_name" ]; then
                    ln -s "../../default-scripts/$script_name" "$script_name"
                fi
            done
            cd ../..

            # Create symlinks to default-agents (using relative paths for portability)
            step "Creating agent symlinks..."
            cd context/agents
            for agent in ../../default-agents/*; do
                agent_name=$(basename "$agent")
                if [ ! -e "$agent_name" ]; then
                    ln -s "../../default-agents/$agent_name" "$agent_name"
                fi
            done
            cd ../..

            # Create .env template
            cat > context/.env << 'EOF'
# Personal Assistant Environment Configuration
# Copy this file and fill in your API keys

# OpenAI API key (required for voice mode)
# Get yours at: https://platform.openai.com/api-keys
OPENAI_API_KEY=

# Realtime voice model (optional, defaults to gpt-realtime)
REALTIME_MODEL=gpt-realtime

# Add your custom environment variables below
EOF

            info "Created fresh context with default structure"
            echo ""
            warn "Remember to:"
            echo "    1. Edit context/.env with your API keys"
            echo "    2. (Optional) Initialize as a git repo for backup:"
            echo "       cd context && git init && git add . && git commit -m 'Initial context'"
            ;;

        import)
            step "Importing context from: $CONTEXT_URL"

            # Clone as a regular directory (not submodule for simpler management)
            if git clone "$CONTEXT_URL" context; then
                info "Successfully cloned context repository"

                # Verify expected structure exists
                if [ ! -d "context/memory" ]; then
                    warn "Creating missing memory/ folder"
                    mkdir -p context/memory
                fi

                if [ ! -d "context/skills" ]; then
                    warn "Creating missing skills/ folder"
                    mkdir -p context/skills
                fi

                if [ ! -d "context/scripts" ]; then
                    warn "Creating missing scripts/ folder"
                    mkdir -p context/scripts
                fi

                if [ ! -d "context/agents" ]; then
                    warn "Creating missing agents/ folder"
                    mkdir -p context/agents
                fi

                # Ensure symlinks to defaults exist (using relative paths)
                step "Ensuring default symlinks..."
                cd context/skills
                for skill in ../../default-skills/*/; do
                    skill_name=$(basename "$skill")
                    if [ ! -e "$skill_name" ]; then
                        ln -s "../../default-skills/$skill_name" "$skill_name"
                    fi
                done
                cd ../..

                cd context/scripts
                for script in ../../default-scripts/*; do
                    script_name=$(basename "$script")
                    if [ ! -e "$script_name" ]; then
                        ln -s "../../default-scripts/$script_name" "$script_name"
                    fi
                done
                cd ../..

                cd context/agents
                for agent in ../../default-agents/*; do
                    agent_name=$(basename "$agent")
                    if [ ! -e "$agent_name" ]; then
                        ln -s "../../default-agents/$agent_name" "$agent_name"
                    fi
                done
                cd ../..

            else
                error "Failed to clone context repository. Check the URL and your access."
            fi
            ;;
    esac
fi

echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Step 3: Set up Claude SDK config symlink
# ─────────────────────────────────────────────────────────────────────────────
step "Setting up Claude SDK configuration..."

# Create .claude_config structure
mkdir -p .claude_config/projects

# Create mangled path symlink for SDK compatibility
MANGLED=$(echo "$SCRIPT_DIR" | sed 's|/|-|g')
SYMLINK_PATH=".claude_config/projects/$MANGLED"

if [ -L "$SYMLINK_PATH" ]; then
    info "SDK symlink already exists"
elif [ -e "$SYMLINK_PATH" ]; then
    warn "$SYMLINK_PATH exists but is not a symlink - skipping"
else
    ln -s "../../context" "$SYMLINK_PATH"
    info "Created SDK symlink"
fi

# Also create skills symlink for SDK discovery
if [ ! -L ".claude_config/skills" ]; then
    ln -sf "../context/skills" ".claude_config/skills"
    info "Created skills discovery symlink"
fi

echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Step 4: Create Python virtual environment
# ─────────────────────────────────────────────────────────────────────────────
step "Setting up Python virtual environment..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    info "Created .venv/"
else
    info ".venv/ already exists"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Step 5: Upgrade pip
# ─────────────────────────────────────────────────────────────────────────────
step "Upgrading pip..."
.venv/bin/pip install --upgrade pip --quiet
info "pip upgraded"

# ─────────────────────────────────────────────────────────────────────────────
# Step 6: Install Python dependencies
# ─────────────────────────────────────────────────────────────────────────────
step "Installing Python dependencies..."
if [ "$DEV_MODE" = true ]; then
    .venv/bin/pip install -r requirements-dev.txt --quiet
    info "Installed requirements-dev.txt"
else
    .venv/bin/pip install -r requirements.txt --quiet
    info "Installed requirements.txt"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Step 7: Install frontend dependencies
# ─────────────────────────────────────────────────────────────────────────────
step "Installing frontend dependencies..."
cd frontend
if [ ! -d "node_modules" ]; then
    npm install --silent
    info "Installed node_modules/"
else
    npm install --silent
    info "Updated node_modules/"
fi
cd ..

# ─────────────────────────────────────────────────────────────────────────────
# Step 8: Create local directories
# ─────────────────────────────────────────────────────────────────────────────
step "Creating local directories..."
mkdir -p index logs
info "Created index/, logs/"

# ─────────────────────────────────────────────────────────────────────────────
# Step 9: Create default manager config
# ─────────────────────────────────────────────────────────────────────────────
if [ ! -f ".manager.json" ]; then
    step "Creating default configuration..."
    cat > .manager.json << 'EOF'
{
  "model": "claude-sonnet-4-20250514",
  "permission_mode": "default",
  "max_budget_usd": null,
  "max_turns": null
}
EOF
    info "Created .manager.json"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Step 10: Verify installation
# ─────────────────────────────────────────────────────────────────────────────
echo ""
step "Verifying installation..."

VERIFICATION_FAILED=false

# Check Python packages
if .venv/bin/python -c "import fastapi, uvicorn, chromadb, sentence_transformers, claude_agent_sdk" 2>/dev/null; then
    info "Python packages OK"
else
    error "Python package verification failed"
    VERIFICATION_FAILED=true
fi

# Check frontend build capability
if [ -f "frontend/package.json" ]; then
    info "Frontend package.json OK"
else
    warn "Frontend package.json not found"
fi

# Check context structure
if [ -d "context/memory" ] && [ -d "context/skills" ]; then
    info "Context structure OK"
else
    warn "Context structure incomplete"
fi

# Check for .env file
if [ -f "context/.env" ]; then
    # Check if OPENAI_API_KEY is set (not just present)
    if grep -q "^OPENAI_API_KEY=.\+" context/.env 2>/dev/null; then
        info "Environment variables configured"
    else
        warn "OPENAI_API_KEY not set in context/.env (required for voice mode)"
    fi
else
    warn "No context/.env file found"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Completion
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
if [ "$VERIFICATION_FAILED" = false ]; then
    echo -e "${GREEN}${BOLD}Installation complete!${NC}"
else
    echo -e "${YELLOW}${BOLD}Installation completed with warnings${NC}"
fi
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# Show next steps
echo -e "${BOLD}Next steps:${NC}"
echo ""

# Check Claude authentication
if ! command -v claude &> /dev/null; then
    echo "  ${RED}1.${NC} Install Claude Code CLI:"
    echo "     ${BLUE}npm install -g @anthropic-ai/claude-code${NC}"
    echo ""
    echo "  ${RED}2.${NC} Authenticate Claude Code:"
    echo "     ${BLUE}claude auth login${NC}"
    echo ""
    STEP=3
elif ! claude auth status 2>/dev/null | grep -q '"loggedIn": true'; then
    echo "  ${RED}1.${NC} Authenticate Claude Code:"
    echo "     ${BLUE}claude auth login${NC}"
    echo ""
    STEP=2
else
    STEP=1
fi

# Check .env configuration
if [ ! -f "context/.env" ] || ! grep -q "^OPENAI_API_KEY=.\+" context/.env 2>/dev/null; then
    echo "  ${RED}${STEP}.${NC} Configure your API keys:"
    echo "     ${BLUE}Edit context/.env${NC}"
    echo ""
    STEP=$((STEP + 1))
fi

echo "  ${GREEN}${STEP}.${NC} Start the backend:"
echo "     ${BLUE}context/scripts/run.sh -m uvicorn api.app:create_app --factory --port 8000${NC}"
echo ""

echo "  ${GREEN}$((STEP + 1)).${NC} Start the frontend (new terminal):"
echo "     ${BLUE}cd frontend && npm run dev${NC}"
echo ""

echo "  ${GREEN}$((STEP + 2)).${NC} Open ${BLUE}https://localhost:5173${NC} in your browser"
echo ""

echo -e "${CYAN}Tip:${NC} Use ${BOLD}/help${NC} in the assistant to see available commands."
echo ""
