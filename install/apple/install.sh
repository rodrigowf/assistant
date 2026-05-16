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
# Fresh-install templates live in install/ (AGENTS.md, MEMORY.md, .env,
# assistant_config.json, .manager.json, sync.env).  This script copies them
# into place at the right moments — edit those files to change the defaults
# a fresh install lands on.
#
# Options:
#   --dev           Install development dependencies (linting, type checking)
#   --skip-prereqs  Skip prerequisite checks
#   --new-context   Create a fresh context (skip interactive prompt)
#   --import-context URL  Import existing context repository (skip interactive prompt)
#   -h, --help      Show this help message
set -euo pipefail

# This script lives at install/apple/install.sh.  Project root is two dirs up.
# Shared install templates (AGENTS.md, MEMORY.md, context.env, cli-runtime/, ...)
# live in install/ alongside the per-OS subdirs.
INSTALLER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_TEMPLATES="$(cd "$INSTALLER_DIR/.." && pwd)"
SCRIPT_DIR="$(cd "$INSTALL_TEMPLATES/.." && pwd)"
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

# Resolve a path through all symlinks, portable across macOS BSD readlink and
# GNU readlink.  Macs 11+ ship a readlink with -f; older macOS (10.x) don't.
# We try `realpath` first (Apple Silicon Macs have it via coreutils via brew,
# and modern macOS ships its own), fall back to readlink -f, then to a Python
# one-liner (always present since we already require Python 3.12+).
resolve_path() {
    local p="$1"
    if command -v realpath >/dev/null 2>&1; then
        realpath "$p" 2>/dev/null && return 0
    fi
    if readlink -f "$p" >/dev/null 2>&1; then
        readlink -f "$p"
        return 0
    fi
    python3 -c "import os, sys; print(os.path.realpath(sys.argv[1]))" "$p" 2>/dev/null
}

# ─────────────────────────────────────────────────────────────────────────────
# Parse arguments
# ─────────────────────────────────────────────────────────────────────────────
DEV_MODE=false
SKIP_PREREQS=false
SKIP_AUTH=false
NEW_CONTEXT=false
IMPORT_CONTEXT=""
# Two independent axes — leave empty so the interactive prompts ask:
#   Harness: which session-backing CLI(s) to set up (claude / qwen / both)
#   Orchestrator: which API SDK(s) to install (anthropic / openai / both)
# --with-X / --without-X pin them non-interactively.
WITH_CLAUDE=""
WITH_QWEN=""
WITH_GEMINI=""
WITH_ANTHROPIC=""
WITH_OPENAI=""
# Shortcut: --qwen-only sets harness=qwen-only and orchestrator=openai-only
# (Qwen models are served through the OpenAI-compatible endpoint, so the
# `openai` SDK is what you want — `anthropic` is not needed).
QWEN_ONLY=false

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
        --skip-auth)
            SKIP_AUTH=true
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
        --with-claude)
            WITH_CLAUDE=true
            shift
            ;;
        --with-qwen)
            WITH_QWEN=true
            shift
            ;;
        --with-gemini)
            WITH_GEMINI=true
            shift
            ;;
        --without-claude)
            WITH_CLAUDE=false
            shift
            ;;
        --without-qwen)
            WITH_QWEN=false
            shift
            ;;
        --without-gemini)
            WITH_GEMINI=false
            shift
            ;;
        --with-anthropic)
            WITH_ANTHROPIC=true
            shift
            ;;
        --with-openai)
            WITH_OPENAI=true
            shift
            ;;
        --without-anthropic)
            WITH_ANTHROPIC=false
            shift
            ;;
        --without-openai)
            WITH_OPENAI=false
            shift
            ;;
        --qwen-only)
            QWEN_ONLY=true
            shift
            ;;
        -h|--help)
            cat <<'HELP'
Usage: ./install.sh [OPTIONS]

Options:
  --dev                  Install development dependencies (ruff, mypy)
  --skip-prereqs         Skip prerequisite checks
  --skip-auth            Skip the agent-CLI install/login step (npm i + first run)
  --new-context          Create a fresh context (non-interactive)
  --import-context URL   Import existing context repository
  -h, --help             Show this help message

Session harness (which agent CLI runs your chats — multiple OK):
  --with-claude          Set up Claude Code (Anthropic)
  --with-qwen            Set up Qwen Code (Alibaba)
  --with-gemini          Set up Gemini CLI (Google)
  --without-claude       Skip Claude Code setup
  --without-qwen         Skip Qwen Code setup
  --without-gemini       Skip Gemini CLI setup

Orchestrator backends (which API SDKs to install):
  --with-anthropic       Install the `anthropic` SDK (for Claude models in the orchestrator)
  --with-openai          Install the `openai` SDK (for GPT, Qwen, Gemini — all use the OpenAI-compatible endpoint)
  --without-anthropic    Skip the `anthropic` SDK
  --without-openai       Skip the `openai` SDK

Shortcuts:
  --qwen-only            Equivalent to --with-qwen --without-claude
                         --with-openai --without-anthropic (a complete
                         Qwen-only install in one flag)

Examples:
  ./install.sh                           # Interactive (will prompt for both axes)
  ./install.sh --qwen-only               # Fully Qwen-backed setup, no Anthropic
  ./install.sh --with-claude --with-anthropic --with-openai
                                         # Default-power-user setup, no prompts
  ./install.sh --new-context             # Fresh install with new context
  ./install.sh --import-context git@github.com:user/context.git
HELP
            exit 0
            ;;
        *)
            error "Unknown option: $1. Use --help for usage."
            ;;
    esac
done

# Apply --qwen-only after parsing so it can override or be overridden by
# explicit per-axis flags depending on argv order.  We treat it as a
# shorthand that only fills in blanks; explicit --with-claude etc. wins.
if [ "$QWEN_ONLY" = true ]; then
    WITH_CLAUDE="${WITH_CLAUDE:-false}"
    WITH_QWEN="${WITH_QWEN:-true}"
    WITH_GEMINI="${WITH_GEMINI:-false}"
    WITH_ANTHROPIC="${WITH_ANTHROPIC:-false}"
    WITH_OPENAI="${WITH_OPENAI:-true}"
fi

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
# Step 0a: Session harness — which agent CLI(s) to set up
# ─────────────────────────────────────────────────────────────────────────────
# Each harness is independently optional.  The wrapper supports any
# combination — the UI's "Session provider" selector picks which one new
# chats use.  We ask up front so we only do the relevant per-harness
# setup work (SDK config symlinks, credential links, CLI install hints).
#
# Adding a new harness here: append a y/n prompt block matching the
# existing ones AND a corresponding --with-<name> / --without-<name> flag
# in the argv parser above.  The per-harness install blocks further down
# remain guarded by their WITH_<name> flag, so the new harness stays
# opt-in.
if [ -z "$WITH_CLAUDE" ] && [ -z "$WITH_QWEN" ] && [ -z "$WITH_GEMINI" ]; then
    echo -e "${BOLD}── Session harness ──${NC}"
    echo "Which agent CLI(s) should run your chats?  (You can pick more than one;"
    echo "the UI's Session Provider selector switches between them at runtime.)"
    echo ""
    ask "Set up Claude Code (Anthropic — recommended default)? [Y/n] "
    read -r ANS
    if [[ "${ANS:-Y}" =~ ^[Nn]$ ]]; then WITH_CLAUDE=false; else WITH_CLAUDE=true; fi
    ask "Set up Qwen Code (Alibaba — open weights, OAuth or DashScope key)? [y/N] "
    read -r ANS
    if [[ "${ANS:-N}" =~ ^[Yy]$ ]]; then WITH_QWEN=true;  else WITH_QWEN=false;  fi
    ask "Set up Gemini CLI (Google — OAuth or GEMINI_API_KEY)? [y/N] "
    read -r ANS
    if [[ "${ANS:-N}" =~ ^[Yy]$ ]]; then WITH_GEMINI=true; else WITH_GEMINI=false; fi
    echo ""
fi
WITH_CLAUDE="${WITH_CLAUDE:-false}"
WITH_QWEN="${WITH_QWEN:-false}"
WITH_GEMINI="${WITH_GEMINI:-false}"

if [ "$WITH_CLAUDE" = false ] && [ "$WITH_QWEN" = false ] && [ "$WITH_GEMINI" = false ]; then
    error "Refusing to install with no harnesses — pick at least one (--with-claude / --with-qwen / --with-gemini)."
fi

# ─────────────────────────────────────────────────────────────────────────────
# Step 0b: Orchestrator backends — which API SDK(s) to install
# ─────────────────────────────────────────────────────────────────────────────
# The orchestrator (text + voice modes) is independent of the session
# harness.  Anthropic SDK powers Claude models in the orchestrator;
# OpenAI SDK powers GPT, Qwen (via OpenAI-compatible endpoint), Gemini,
# and OpenAI Realtime voice.  A pure Qwen setup wants OpenAI only.
if [ -z "$WITH_ANTHROPIC" ] && [ -z "$WITH_OPENAI" ]; then
    echo -e "${BOLD}── Orchestrator backends ──${NC}"
    echo "Which API SDK(s) should the orchestrator use?"
    echo ""
    echo "  ${BOLD}1)${NC} OpenAI only (GPT models, Qwen, Gemini, voice mode — recommended default for Qwen-only setups)"
    echo "  ${BOLD}2)${NC} Anthropic only (Claude models in the orchestrator picker)"
    echo "  ${BOLD}3)${NC} Both"
    echo "  ${BOLD}4)${NC} Neither (orchestrator disabled — chats only)"
    echo ""
    ask "Choice [1/2/3/4] (default 3): "
    read -r ORCH_CHOICE
    case "${ORCH_CHOICE:-3}" in
        1) WITH_ANTHROPIC=false; WITH_OPENAI=true ;;
        2) WITH_ANTHROPIC=true;  WITH_OPENAI=false ;;
        3) WITH_ANTHROPIC=true;  WITH_OPENAI=true ;;
        4) WITH_ANTHROPIC=false; WITH_OPENAI=false ;;
        *) error "Invalid choice: ${ORCH_CHOICE}. Expected 1, 2, 3, or 4." ;;
    esac
    echo ""
fi
WITH_ANTHROPIC="${WITH_ANTHROPIC:-false}"
WITH_OPENAI="${WITH_OPENAI:-false}"

if [ "$WITH_CLAUDE" = true ]; then info "Will set up Claude Code harness"; fi
if [ "$WITH_QWEN"   = true ]; then info "Will set up Qwen Code harness"; fi
if [ "$WITH_GEMINI" = true ]; then info "Will set up Gemini CLI harness"; fi
if [ "$WITH_ANTHROPIC" = true ]; then info "Will install anthropic SDK (orchestrator)"; fi
if [ "$WITH_OPENAI"    = true ]; then info "Will install openai SDK (orchestrator + voice)"; fi
if [ "$WITH_ANTHROPIC" = false ] && [ "$WITH_OPENAI" = false ]; then
    warn "No orchestrator backend selected — the orchestrator tab will be disabled."
fi
echo ""

# Default provider written into assistant_config.json.  Claude wins if both
# are installed (historical default); otherwise Qwen.
if [ "$WITH_CLAUDE" = true ]; then
    DEFAULT_PROVIDER="claude"
else
    DEFAULT_PROVIDER="qwen"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Step 1: Check prerequisites
# ─────────────────────────────────────────────────────────────────────────────
if [ "$SKIP_PREREQS" = false ]; then
    step "Checking prerequisites..."
    echo ""

    if ! bash "$INSTALLER_DIR/install-prerequisites.sh"; then
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

            # Seed MEMORY.md and AGENTS.md from the install/ templates.
            cp "$INSTALL_TEMPLATES/MEMORY.md" context/memory/MEMORY.md
            info "Seeded context/memory/MEMORY.md from install/MEMORY.md"
            if [ ! -f context/AGENTS.md ]; then
                cp "$INSTALL_TEMPLATES/AGENTS.md" context/AGENTS.md
                info "Seeded context/AGENTS.md from install/AGENTS.md"
            fi

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

            # Seed context/.env from the install/context.env template, then
            # uncomment the keys for the axes the user opted into so they
            # show up as required (and not just commented-out hints).  Keys
            # for axes the user *didn't* pick stay commented — they're a
            # one-liner away if they want to flip an axis on later.
            cp "$INSTALL_TEMPLATES/context.env" context/.env
            info "Seeded context/.env from install/context.env"

            uncomment_env_key() {
                # Uncomment a single env var line in context/.env.  Idempotent:
                # if the key is already uncommented, leave it alone.
                # macOS ships BSD sed, which requires an explicit backup
                # suffix after -i ("''" = no backup).  GNU sed accepts that
                # too, so this form is portable.
                local key="$1"
                if grep -q "^# *${key}=" context/.env; then
                    sed -i '' "s|^# *${key}=|${key}=|" context/.env
                fi
            }

            if [ "$WITH_OPENAI" = true ]; then
                uncomment_env_key "OPENAI_API_KEY"
            fi
            if [ "$WITH_ANTHROPIC" = true ]; then
                uncomment_env_key "ANTHROPIC_API_KEY"
            fi
            if [ "$WITH_QWEN" = true ]; then
                uncomment_env_key "DASHSCOPE_API_KEY"
            fi

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
# Step 3: Set up Claude SDK config symlink (only if --with-claude)
# ─────────────────────────────────────────────────────────────────────────────
# MANGLED is shared with the Qwen setup step below — compute it unconditionally.
MANGLED=$(echo "$SCRIPT_DIR" | sed 's|/|-|g')

if [ "$WITH_CLAUDE" = true ]; then
    step "Setting up Claude SDK configuration..."

    # Create .claude_config structure
    mkdir -p .claude_config/projects

    # The Claude SDK stores session data in .claude_config/projects/<mangled-path>/
    # where <mangled-path> is the absolute project path with / replaced by -.
    # We symlink this to context/ so all session data lives in one place.
    SYMLINK_PATH=".claude_config/projects/$MANGLED"

    if [ -L "$SYMLINK_PATH" ]; then
        info "SDK symlink already exists"
    elif [ -d "$SYMLINK_PATH" ]; then
        # SDK created a real directory (e.g. from a previous run without the symlink).
        # Move any session files into context/ and replace with the symlink.
        warn "Found real directory at $SYMLINK_PATH — migrating to symlink"
        if ls "$SYMLINK_PATH"/*.jsonl &>/dev/null; then
            cp -n "$SYMLINK_PATH"/*.jsonl context/ 2>/dev/null || true
            info "Migrated session files to context/"
        fi
        rm -rf "$SYMLINK_PATH"
        ln -s "../../context" "$SYMLINK_PATH"
        info "Replaced directory with SDK symlink"
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
else
    info "Skipping Claude SDK setup (--without-claude)"
    echo ""
fi

# ─────────────────────────────────────────────────────────────────────────────
# Step 3b: Set up Qwen Code config symlink (only if --with-qwen)
# ─────────────────────────────────────────────────────────────────────────────
# Qwen Code stores per-project state under ~/.qwen/projects/<mangled-path>/.
# Symlink it to context/ (same approach as Claude) so Qwen sessions land at
# context/chats/<session>.jsonl alongside Claude sessions.
if [ "$WITH_QWEN" = true ]; then
    step "Setting up Qwen Code configuration..."

    QWEN_HOME="$HOME/.qwen"
    QWEN_PROJECT_DIR="$QWEN_HOME/projects/$MANGLED"
    EXPECTED_LINK_TARGET="$SCRIPT_DIR/context"

    mkdir -p "$QWEN_HOME/projects"

    if [ -L "$QWEN_PROJECT_DIR" ]; then
        # Verify the existing symlink points at OUR context, not a sibling
        # project's.  If it's pointing elsewhere we leave it alone and warn —
        # blowing away an unrelated project's symlink would be a bad time.
        CURRENT_TARGET="$(resolve_path "$QWEN_PROJECT_DIR" 2>/dev/null || true)"
        EXPECTED_RESOLVED="$(resolve_path "$EXPECTED_LINK_TARGET" 2>/dev/null || true)"
        if [ "$CURRENT_TARGET" = "$EXPECTED_RESOLVED" ]; then
            info "Qwen project symlink already points to context/"
        else
            warn "Qwen project symlink points to $CURRENT_TARGET (not this project) — leaving alone"
        fi
    elif [ -d "$QWEN_PROJECT_DIR" ]; then
        # Qwen already created a real directory (a previous direct run, etc.).
        # Migrate any chats into context/chats/ then replace with our symlink.
        # We back up the original directory under context/qwen-backup-<ts>/
        # before nuking it so a botched migration is recoverable.
        warn "Found real directory at $QWEN_PROJECT_DIR — migrating to symlink"
        mkdir -p context/chats
        BACKUP_DIR="context/qwen-backup-$(date +%Y%m%dT%H%M%S)"
        cp -r "$QWEN_PROJECT_DIR" "$BACKUP_DIR" 2>/dev/null || true
        if [ -d "$BACKUP_DIR" ]; then
            info "Backed up original Qwen project dir → $BACKUP_DIR"
        fi
        # Pull any JSONL chats into context/chats/ (Qwen's expected layout
        # once the symlink is in place).  We use cp -n to avoid overwriting
        # files that already exist in context/chats/.
        if [ -d "$QWEN_PROJECT_DIR/chats" ] && \
           compgen -G "$QWEN_PROJECT_DIR/chats/*.jsonl" > /dev/null; then
            cp -n "$QWEN_PROJECT_DIR/chats/"*.jsonl context/chats/ 2>/dev/null || true
            # Also lift any .runtime.json sibling files so Qwen can resume.
            cp -n "$QWEN_PROJECT_DIR/chats/"*.runtime.json context/chats/ 2>/dev/null || true
            info "Migrated Qwen chats into context/chats/"
        fi
        rm -rf "$QWEN_PROJECT_DIR"
        ln -s "$EXPECTED_LINK_TARGET" "$QWEN_PROJECT_DIR"
        info "Replaced directory with Qwen project symlink → context/"
    else
        ln -s "$EXPECTED_LINK_TARGET" "$QWEN_PROJECT_DIR"
        info "Created Qwen project symlink → context/"
    fi

    # Ensure context/chats/ exists so the SessionStore picks up Qwen sessions
    # from day one (even before the first Qwen turn runs).
    mkdir -p context/chats

    # Qwen reads project skills from ~/.qwen/skills (global) — mirror our pattern.
    if [ ! -L "$QWEN_HOME/skills" ]; then
        if [ -e "$QWEN_HOME/skills" ]; then
            warn "$QWEN_HOME/skills exists and is not a symlink — leaving alone"
        else
            ln -s "$SCRIPT_DIR/context/skills" "$QWEN_HOME/skills"
            info "Created Qwen skills discovery symlink"
        fi
    fi

    echo ""
else
    info "Skipping Qwen Code setup (--without-qwen)"
    echo ""
fi

# ─────────────────────────────────────────────────────────────────────────────
# Step 3c: Set up Gemini CLI config symlink (only if --with-gemini)
# ─────────────────────────────────────────────────────────────────────────────
# The Gemini CLI stores per-project session JSONLs under
# ~/.gemini/tmp/<label>/chats/ where <label> comes from ~/.gemini/projects.json
# (defaults to the cwd basename — for this repo, "assistant").  Symlink that
# label directory at context/, so Gemini writes chats/session-*.jsonl directly
# alongside Qwen's <uuid>.jsonl files.  The two harnesses coexist cleanly:
# Gemini's session-prefixed names don't collide with Qwen's <uuid>.jsonl
# names, and SessionStore picks up both formats via the same chats/ scan.
if [ "$WITH_GEMINI" = true ]; then
    step "Setting up Gemini CLI configuration..."

    GEMINI_HOME="$HOME/.gemini"
    # Compute Gemini's label for this cwd.  Read ~/.gemini/projects.json if
    # present (any prior `gemini` run in this dir registered one); fall back
    # to the cwd basename, which is exactly what the CLI does on first run.
    GEMINI_LABEL=""
    if [ -f "$GEMINI_HOME/projects.json" ] && command -v python3 &> /dev/null; then
        GEMINI_LABEL="$(python3 -c "
import json, sys
try:
    with open('$GEMINI_HOME/projects.json') as f:
        data = json.load(f)
    label = data.get('projects', {}).get('$SCRIPT_DIR')
    print(label or '')
except Exception:
    print('')
" 2>/dev/null || true)"
    fi
    if [ -z "$GEMINI_LABEL" ]; then
        GEMINI_LABEL="$(basename "$SCRIPT_DIR")"
    fi
    GEMINI_PROJECT_DIR="$GEMINI_HOME/tmp/$GEMINI_LABEL"
    EXPECTED_LINK_TARGET="$SCRIPT_DIR/context"

    mkdir -p "$GEMINI_HOME/tmp"

    if [ -L "$GEMINI_PROJECT_DIR" ]; then
        CURRENT_TARGET="$(resolve_path "$GEMINI_PROJECT_DIR" 2>/dev/null || true)"
        EXPECTED_RESOLVED="$(resolve_path "$EXPECTED_LINK_TARGET" 2>/dev/null || true)"
        if [ "$CURRENT_TARGET" = "$EXPECTED_RESOLVED" ]; then
            info "Gemini project symlink already points to context/"
        else
            warn "Gemini project symlink points to $CURRENT_TARGET (not this project) — leaving alone"
        fi
    elif [ -d "$GEMINI_PROJECT_DIR" ]; then
        # Gemini already populated a real directory in ~/.gemini/tmp/.
        # Same migration shape as Qwen above: back up the original, lift
        # chats into context/chats/, then replace with our symlink.
        warn "Found real directory at $GEMINI_PROJECT_DIR — migrating to symlink"
        mkdir -p context/chats
        BACKUP_DIR="context/gemini-backup-$(date +%Y%m%dT%H%M%S)"
        cp -r "$GEMINI_PROJECT_DIR" "$BACKUP_DIR" 2>/dev/null || true
        if [ -d "$BACKUP_DIR" ]; then
            info "Backed up original Gemini project dir → $BACKUP_DIR"
        fi
        if [ -d "$GEMINI_PROJECT_DIR/chats" ] && \
           compgen -G "$GEMINI_PROJECT_DIR/chats/session-*.jsonl" > /dev/null; then
            cp -n "$GEMINI_PROJECT_DIR/chats/session-"*.jsonl context/chats/ 2>/dev/null || true
            info "Migrated Gemini chats into context/chats/"
        fi
        rm -rf "$GEMINI_PROJECT_DIR"
        ln -s "$EXPECTED_LINK_TARGET" "$GEMINI_PROJECT_DIR"
        info "Replaced directory with Gemini project symlink → context/"
    else
        ln -s "$EXPECTED_LINK_TARGET" "$GEMINI_PROJECT_DIR"
        info "Created Gemini project symlink → context/"
    fi

    # Ensure context/chats/ exists so SessionStore picks up Gemini sessions
    # from day one (Qwen's setup creates this too — idempotent).
    mkdir -p context/chats

    echo ""
else
    info "Skipping Gemini CLI setup (--without-gemini)"
    echo ""
fi

# ─────────────────────────────────────────────────────────────────────────────
# Step 3d: Wire AGENTS.md as the shared project-instructions file
# ─────────────────────────────────────────────────────────────────────────────
# AGENTS.md lives inside context/ (the private data repo).  Claude Code reads
# CLAUDE.md, Qwen Code reads QWEN.md — both at the project root, both
# symlinks → context/AGENTS.md, so the agents see identical instructions
# from the location they each natively look for.
step "Wiring context/AGENTS.md as the shared project-instructions file..."

# Migration: legacy layouts may have AGENTS.md at the repo root (intermediate)
# or only CLAUDE.md at the root (original).  Normalize to context/AGENTS.md.
# If nothing migratable is present (clean install + imported context that
# didn't include AGENTS.md), fall back to the install/ template.
if [ ! -e "context/AGENTS.md" ]; then
    if [ -f "AGENTS.md" ] && [ ! -L "AGENTS.md" ]; then
        # Intermediate layout: real AGENTS.md at root.  Move into context/.
        mv AGENTS.md context/AGENTS.md
        info "Moved AGENTS.md → context/AGENTS.md"
    elif [ -f "CLAUDE.md" ] && [ ! -L "CLAUDE.md" ]; then
        # Original layout: real CLAUDE.md, no AGENTS.md.  Promote into context/.
        mv CLAUDE.md context/AGENTS.md
        info "Promoted CLAUDE.md → context/AGENTS.md"
    elif [ -f "$INSTALL_TEMPLATES/AGENTS.md" ]; then
        # No legacy file to migrate — seed from the install/ template.
        cp "$INSTALL_TEMPLATES/AGENTS.md" context/AGENTS.md
        info "Seeded context/AGENTS.md from install/AGENTS.md"
    fi
fi

# Clean up a stale root-level AGENTS.md (real file or wrong-target symlink)
# left behind by the intermediate layout, so the symlink step below can
# safely re-link without "file exists" errors.
if [ -e "AGENTS.md" ] || [ -L "AGENTS.md" ]; then
    if [ -L "AGENTS.md" ] || [ ! -s "AGENTS.md" ]; then
        rm -f AGENTS.md
    fi
fi

if [ -f "context/AGENTS.md" ]; then
    for shadow in CLAUDE.md QWEN.md; do
        target="$(readlink "$shadow" 2>/dev/null || true)"
        if [ "$target" = "context/AGENTS.md" ]; then
            continue  # already points where we want it
        fi
        if [ -L "$shadow" ]; then
            # Symlink pointing at the wrong target (likely the old AGENTS.md
            # at root).  Repoint it.
            rm -f "$shadow"
        elif [ -f "$shadow" ]; then
            warn "$shadow exists and is not a symlink — leaving alone (delete to enable shared instructions)"
            continue
        fi
        ln -s context/AGENTS.md "$shadow"
        info "Created $shadow → context/AGENTS.md symlink"
    done
else
    warn "No context/AGENTS.md found — skipping CLAUDE.md/QWEN.md symlinks"
fi

echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Step 3e: Seed per-CLI runtime dirs (.claude/, .qwen/, .gemini/)
# ─────────────────────────────────────────────────────────────────────────────
# The CLI runtime dirs at the project root are gitignored — each CLI normally
# auto-creates them on first run.  We seed them here from install/cli-runtime/
# so the carve-outs we depend on (Gemini's respectGitIgnore=false, default
# allowlists) are in place before the user's first run.
#
# Existing files are NEVER overwritten — the seed is idempotent and safe on
# re-runs (a hand-edited settings.json on a working install stays as-is).
step "Seeding local CLI runtime dirs..."

seed_cli_runtime() {
    local cli="$1"           # claude | qwen | gemini
    local dst=".$cli"
    local src="$INSTALL_TEMPLATES/cli-runtime/$cli"
    if [ ! -d "$src" ]; then
        warn "No template at $src — skipping $cli seed"
        return 0
    fi
    mkdir -p "$dst"
    # Copy regular files and dotfiles, but never clobber existing ones.
    shopt -s nullglob dotglob
    for f in "$src"/*; do
        local name
        name="$(basename "$f")"
        # Skip "." and ".." entries that dotglob picks up in some bash versions
        [ "$name" = "." ] || [ "$name" = ".." ] && continue
        if [ -e "$dst/$name" ] || [ -L "$dst/$name" ]; then
            : # already there — leave it alone
        else
            cp "$f" "$dst/$name"
            info "Seeded $dst/$name"
        fi
    done
    shopt -u nullglob dotglob
}

[ "$WITH_CLAUDE" = true ] && seed_cli_runtime claude
[ "$WITH_QWEN"   = true ] && seed_cli_runtime qwen
[ "$WITH_GEMINI" = true ] && seed_cli_runtime gemini

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
# Core deps always go in.  Provider-specific SDKs (claude-agent-sdk,
# anthropic, openai) install only if the matching axis was selected, so a
# pure Qwen + OpenAI-orchestrator deployment doesn't pull Anthropic packages
# (and vice versa).
step "Installing Python dependencies..."
if [ "$DEV_MODE" = true ]; then
    .venv/bin/pip install -r requirements-dev.txt --quiet
    info "Installed requirements-dev.txt (core + dev tools)"
else
    .venv/bin/pip install -r requirements.txt --quiet
    info "Installed requirements.txt (core)"
fi

if [ "$WITH_CLAUDE" = true ]; then
    .venv/bin/pip install -r requirements-claude.txt --quiet
    info "Installed requirements-claude.txt (claude-agent-sdk)"
fi
if [ "$WITH_ANTHROPIC" = true ]; then
    .venv/bin/pip install -r requirements-anthropic.txt --quiet
    info "Installed requirements-anthropic.txt (anthropic SDK)"
fi
if [ "$WITH_OPENAI" = true ]; then
    .venv/bin/pip install -r requirements-openai.txt --quiet
    info "Installed requirements-openai.txt (openai SDK)"
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
# Step 7b: Install + authenticate agent CLIs
# ─────────────────────────────────────────────────────────────────────────────
# Runs after Python + Node deps so npm is guaranteed present.  Each enabled
# harness is handled in its own block:
#
#   1. Install the CLI globally via npm (if missing).
#   2. Detect auth state.  Skip the login prompt if:
#        - the relevant API key is already set in context/.env, OR
#        - the CLI's local credential file already exists.
#   3. Otherwise pause and ask the user to run the login command in a separate
#      terminal, then press Enter to continue.  Re-checks auth state after.
#
# Interactive vs non-interactive:
#   The login prompt is skipped silently when stdin is not a TTY (CI, piped
#   installs) OR when --skip-auth is passed.  Either way, the script continues —
#   the user can finish login later before their first chat.
#
# Adding a new harness here: add a new `if [ "$WITH_<name>" = true ]` block
# below, supplying the npm package, the login command, the credential-file
# check, and the env-var fallback key.
if [ "$SKIP_AUTH" = false ]; then
    step "Installing and authenticating agent CLIs..."
    echo ""
fi

# Returns 0 if stdin is a TTY (interactive shell), 1 otherwise.
is_interactive() { [ -t 0 ]; }

# Install one CLI globally via npm if it's not already on PATH.  Returns 0 if
# the CLI is available after this call, non-zero if the install was skipped or
# failed.  Asks for confirmation in interactive mode; auto-installs otherwise.
install_harness_cli() {
    local cli="$1" pkg="$2"
    if command -v "$cli" &>/dev/null; then
        info "$cli CLI already installed ($(command -v "$cli"))"
        return 0
    fi
    if is_interactive; then
        ask "$cli CLI not found.  Install globally via npm? [Y/n] "
        read -r ANS
        if [[ "${ANS:-Y}" =~ ^[Nn]$ ]]; then
            warn "Skipped $cli install — run 'npm install -g $pkg' manually before first use."
            return 1
        fi
    else
        info "$cli CLI not found — installing (non-interactive mode)"
    fi
    step "Installing $cli via npm..."
    if npm install -g "$pkg"; then
        info "$cli installed"
        return 0
    else
        warn "$cli install failed — install manually: npm install -g $pkg"
        return 1
    fi
}

# Check auth state for one CLI; prompt the user to log in interactively if
# unauthenticated.  Skips silently in non-interactive mode or when --skip-auth.
#
#   $1: cli name (display only)
#   $2: login command shown to the user
#   $3: auth-check shell snippet (eval'd; returns 0 if authenticated)
#   $4: env-var name that, when set in context/.env, counts as "auth handled
#       via API key" and short-circuits the login prompt.
prompt_harness_login() {
    local cli="$1" login_cmd="$2" check_cmd="$3" env_key="$4"
    # API-key fallback wins — if the env var is set, the CLI uses it and no
    # OAuth login is needed.
    if [ -n "$env_key" ] && [ -f "context/.env" ] && \
       grep -q "^${env_key}=.\+" context/.env 2>/dev/null; then
        info "$cli: $env_key set in context/.env — no interactive login needed"
        return 0
    fi
    if eval "$check_cmd" &>/dev/null; then
        info "$cli already authenticated"
        return 0
    fi
    if [ "$SKIP_AUTH" = true ]; then
        warn "$cli not authenticated, --skip-auth set — log in manually before first use"
        return 0
    fi
    if ! is_interactive; then
        warn "$cli not authenticated (non-interactive install) — run '$login_cmd' manually before first use"
        return 0
    fi
    echo ""
    warn "$cli is not authenticated."
    echo "    Open a separate terminal in this directory and run:"
    echo "      ${BLUE}${login_cmd}${NC}"
    echo "    (Or set ${env_key} in context/.env to use an API key instead.)"
    ask "Press Enter once login completes (or just press Enter to finish setup later): "
    read -r _
    if eval "$check_cmd" &>/dev/null; then
        info "$cli authenticated"
    else
        warn "$cli still not authenticated — finish login before your first chat."
    fi
}

if [ "$SKIP_AUTH" = false ]; then
    if [ "$WITH_CLAUDE" = true ]; then
        install_harness_cli claude '@anthropic-ai/claude-code' || true
        if command -v claude &>/dev/null; then
            prompt_harness_login claude 'claude auth login' \
                'claude auth status 2>/dev/null | grep -q "\"loggedIn\": true"' \
                'ANTHROPIC_API_KEY'
        fi
    fi
    if [ "$WITH_QWEN" = true ]; then
        install_harness_cli qwen '@qwen-code/qwen-code' || true
        if command -v qwen &>/dev/null; then
            # Qwen has no `auth status` subcommand and stores OAuth state in
            # ~/.qwen/oauth_creds.json when used in OAuth mode.  API-key mode
            # (DashScope) is detected via context/.env.
            prompt_harness_login qwen 'qwen' \
                '[ -f "$HOME/.qwen/oauth_creds.json" ]' \
                'DASHSCOPE_API_KEY'
        fi
    fi
    if [ "$WITH_GEMINI" = true ]; then
        install_harness_cli gemini '@google/gemini-cli' || true
        if command -v gemini &>/dev/null; then
            prompt_harness_login gemini 'gemini' \
                '[ -f "$HOME/.gemini/oauth_creds.json" ]' \
                'GEMINI_API_KEY'
        fi
    fi
    echo ""
else
    info "Skipping agent CLI install/login step (--skip-auth)"
    echo ""
fi

# ─────────────────────────────────────────────────────────────────────────────
# Step 8: Create local directories
# ─────────────────────────────────────────────────────────────────────────────
step "Creating local directories..."
mkdir -p index logs
info "Created index/, logs/"

# ─────────────────────────────────────────────────────────────────────────────
# Step 9: Link Claude Code credentials into .claude_config/ (only if --with-claude)
# ─────────────────────────────────────────────────────────────────────────────
# run.sh sets CLAUDE_CONFIG_DIR=$PROJECT_DIR/.claude_config, so the SDK reads
# OAuth credentials from there — NOT from ~/.claude/. Symlink (rather than
# copy) so OAuth token refreshes by any Claude Code instance propagate
# automatically. A stale copy here causes 401s once the cached token expires.
if [ "$WITH_CLAUDE" = true ] && [ -f "$HOME/.claude/.credentials.json" ]; then
    if [ -L ".claude_config/.credentials.json" ]; then
        info "Claude Code credentials symlink already present"
    else
        if [ -f ".claude_config/.credentials.json" ]; then
            rm -f ".claude_config/.credentials.json"
        fi
        ln -s "$HOME/.claude/.credentials.json" ".claude_config/.credentials.json"
        info "Linked Claude Code credentials into .claude_config/"
    fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# Step 10: Create default assistant_config.json
# ─────────────────────────────────────────────────────────────────────────────
# The API seeds a default if missing, but writing it up front lets the user
# edit it (e.g. add SSH working-directory entries) before starting the backend.
if [ ! -f "assistant_config.json" ]; then
    step "Creating default assistant_config.json..."
    # default_model is provider-appropriate so the first run lands somewhere sensible.
    if [ "$DEFAULT_PROVIDER" = "qwen" ]; then
        DEFAULT_MODEL="qwen3.6-plus"
    else
        DEFAULT_MODEL="claude-sonnet-4-5-20250929"
    fi
    # Copy from the install/ template and substitute placeholders.  Using
    # `|` as the sed delimiter so $SCRIPT_DIR (which contains `/`) doesn't
    # break the substitution.
    sed \
        -e "s|@@SCRIPT_DIR@@|$SCRIPT_DIR|g" \
        -e "s|@@DEFAULT_PROVIDER@@|$DEFAULT_PROVIDER|g" \
        -e "s|@@DEFAULT_MODEL@@|$DEFAULT_MODEL|g" \
        "$INSTALL_TEMPLATES/assistant_config.json" > assistant_config.json
    info "Created assistant_config.json (provider=$DEFAULT_PROVIDER)"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Step 11: Create default manager config
# ─────────────────────────────────────────────────────────────────────────────
if [ ! -f ".manager.json" ]; then
    step "Creating default configuration..."
    cp "$INSTALL_TEMPLATES/manager.json" .manager.json
    info "Created .manager.json"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Step 12: Verify installation
# ─────────────────────────────────────────────────────────────────────────────
echo ""
step "Verifying installation..."

VERIFICATION_FAILED=false

# Check core Python packages — these are required regardless of which
# axes were selected.  Provider SDKs are checked separately below so a
# missing optional SDK doesn't fail verification.
if .venv/bin/python -c "import fastapi, uvicorn, chromadb, sentence_transformers" 2>/dev/null; then
    info "Core Python packages OK"
else
    error "Core Python package verification failed"
    VERIFICATION_FAILED=true
fi

# Optional-SDK presence checks — only complain about SDKs we just tried
# to install.  Each lazy-loads at first use, so a missing SDK is silent
# at backend startup; we surface it here so the user gets actionable
# feedback instead of a confusing 400 the first time they pick that
# provider in the UI.
check_optional_sdk() {
    local sdk="$1" axis="$2" reqfile="$3"
    if .venv/bin/python -c "import $sdk" 2>/dev/null; then
        info "$sdk SDK present"
    else
        warn "$sdk SDK not importable despite $axis being selected (try: pip install -r $reqfile)"
    fi
}
if [ "$WITH_CLAUDE" = true ]; then
    check_optional_sdk "claude_agent_sdk" "--with-claude" "requirements-claude.txt"
fi
if [ "$WITH_ANTHROPIC" = true ]; then
    check_optional_sdk "anthropic" "--with-anthropic" "requirements-anthropic.txt"
fi
if [ "$WITH_OPENAI" = true ]; then
    check_optional_sdk "openai" "--with-openai" "requirements-openai.txt"
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

# Check for .env file and the keys that the user's axes actually need.
# Each axis maps to a specific env var; only complain about keys the user
# opted into.  An empty value (KEY=) counts as "not set".
check_env_key() {
    local key="$1" feature="$2"
    if grep -q "^${key}=.\+" context/.env 2>/dev/null; then
        info "$key set in context/.env"
    else
        warn "$key not set in context/.env ($feature)"
    fi
}
if [ -f "context/.env" ]; then
    if [ "$WITH_OPENAI" = true ]; then
        check_env_key "OPENAI_API_KEY" "OpenAI orchestrator text + Realtime voice"
    fi
    if [ "$WITH_ANTHROPIC" = true ]; then
        check_env_key "ANTHROPIC_API_KEY" "Anthropic Claude models in orchestrator"
    fi
    if [ "$WITH_QWEN" = true ]; then
        check_env_key "DASHSCOPE_API_KEY" "Qwen harness + Qwen voice"
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

STEP=1

# Agent CLI install + login is handled inline at Step 7b.  Any harness that
# still needs attention after that step prints its own warning during the
# install run, so we don't repeat per-CLI instructions here.

# Check .env configuration — surface only the keys for axes the user selected.
ENV_KEYS_MISSING=()
if [ -f "context/.env" ]; then
    if [ "$WITH_OPENAI" = true ] && ! grep -q "^OPENAI_API_KEY=.\+" context/.env 2>/dev/null; then
        ENV_KEYS_MISSING+=("OPENAI_API_KEY")
    fi
    if [ "$WITH_ANTHROPIC" = true ] && ! grep -q "^ANTHROPIC_API_KEY=.\+" context/.env 2>/dev/null; then
        ENV_KEYS_MISSING+=("ANTHROPIC_API_KEY")
    fi
    if [ "$WITH_QWEN" = true ] && ! grep -q "^DASHSCOPE_API_KEY=.\+" context/.env 2>/dev/null; then
        ENV_KEYS_MISSING+=("DASHSCOPE_API_KEY")
    fi
else
    # No .env yet — list every key the user's axes need.
    [ "$WITH_OPENAI"    = true ] && ENV_KEYS_MISSING+=("OPENAI_API_KEY")
    [ "$WITH_ANTHROPIC" = true ] && ENV_KEYS_MISSING+=("ANTHROPIC_API_KEY")
    [ "$WITH_QWEN"      = true ] && ENV_KEYS_MISSING+=("DASHSCOPE_API_KEY")
fi
if [ "${#ENV_KEYS_MISSING[@]}" -gt 0 ]; then
    KEY_LIST="$(IFS=, ; echo "${ENV_KEYS_MISSING[*]}")"
    echo "  ${RED}${STEP}.${NC} Configure your API keys:"
    echo "     ${BLUE}Edit context/.env${NC}   ${CYAN}(${KEY_LIST})${NC}"
    echo ""
    STEP=$((STEP + 1))
fi

echo "  ${GREEN}${STEP}.${NC} Start the backend:"
echo "     ${BLUE}context/scripts/run.sh -m uvicorn api.app:create_app --factory --port 8765${NC}"
echo ""

echo "  ${GREEN}$((STEP + 1)).${NC} Start the frontend (new terminal):"
echo "     ${BLUE}cd frontend && npm run dev${NC}"
echo ""

echo "  ${GREEN}$((STEP + 2)).${NC} Open ${BLUE}https://localhost:5432${NC} in your browser"
echo ""

echo -e "${CYAN}Tip:${NC} Use ${BOLD}/help${NC} in the assistant to see available commands."
# Show the multi-harness tip whenever the user enabled more than one.
HARNESS_COUNT=0
[ "$WITH_CLAUDE" = true ] && HARNESS_COUNT=$((HARNESS_COUNT + 1))
[ "$WITH_QWEN"   = true ] && HARNESS_COUNT=$((HARNESS_COUNT + 1))
[ "$WITH_GEMINI" = true ] && HARNESS_COUNT=$((HARNESS_COUNT + 1))
if [ "$HARNESS_COUNT" -gt 1 ]; then
    echo -e "${CYAN}Tip:${NC} You can switch providers anytime in Configuration → Session provider."
fi
echo ""
