#!/usr/bin/env bash
# Usage: ./install.sh [--dev]
# Description: Install the Personal Assistant and all dependencies.
#
# Options:
#   --dev    Install development dependencies (linting, type checking)
#   --skip-prereqs  Skip prerequisite checks
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info() { echo -e "${GREEN}✓${NC} $1"; }
step() { echo -e "${BLUE}→${NC} $1"; }
warn() { echo -e "${YELLOW}!${NC} $1"; }
error() { echo -e "${RED}✗${NC} $1"; exit 1; }

DEV_MODE=false
SKIP_PREREQS=false

for arg in "$@"; do
    case $arg in
        --dev) DEV_MODE=true ;;
        --skip-prereqs) SKIP_PREREQS=true ;;
        -h|--help)
            echo "Usage: ./install.sh [--dev] [--skip-prereqs]"
            echo ""
            echo "Options:"
            echo "  --dev           Install development dependencies"
            echo "  --skip-prereqs  Skip prerequisite checks"
            exit 0
            ;;
    esac
done

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "           Personal Assistant Installer"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

# Step 1: Check prerequisites
if [ "$SKIP_PREREQS" = false ]; then
    step "Checking prerequisites..."
    if ! bash scripts/install-prerequisites.sh; then
        echo
        error "Please install missing prerequisites and try again."
    fi
    echo
fi

# Step 2: Create Python virtual environment
step "Setting up Python virtual environment..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    info "Created .venv/"
else
    info ".venv/ already exists"
fi

# Step 3: Upgrade pip
step "Upgrading pip..."
.venv/bin/pip install --upgrade pip --quiet
info "pip upgraded"

# Step 4: Install Python dependencies
step "Installing Python dependencies..."
if [ "$DEV_MODE" = true ]; then
    .venv/bin/pip install -r requirements-dev.txt --quiet
    info "Installed requirements-dev.txt"
else
    .venv/bin/pip install -r requirements.txt --quiet
    info "Installed requirements.txt"
fi

# Step 5: Install frontend dependencies
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

# Step 6: Create local directories
step "Creating local directories..."
mkdir -p .claude_config index logs
info "Created .claude_config/, index/, logs/"

# Step 7: Create default config if it doesn't exist
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

# Step 8: Verify installation
echo
step "Verifying installation..."

# Check Python packages
if .venv/bin/python -c "import fastapi, uvicorn, chromadb, sentence_transformers, claude_agent_sdk" 2>/dev/null; then
    info "Python packages OK"
else
    error "Python package verification failed"
fi

# Check frontend build capability
if [ -f "frontend/package.json" ]; then
    info "Frontend package.json OK"
else
    error "Frontend package.json not found"
fi

echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${GREEN}Installation complete!${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo
echo "To start the assistant:"
echo
echo "  1. Start the backend:"
echo "     ${BLUE}scripts/run.sh -m uvicorn api.app:create_app --factory --port 8000${NC}"
echo
echo "  2. Start the frontend (new terminal):"
echo "     ${BLUE}cd frontend && npm run dev${NC}"
echo
echo "  3. Open ${BLUE}https://localhost:5173${NC} in your browser"
echo
if ! command -v claude &> /dev/null || ! claude auth status 2>/dev/null | grep -q '"loggedIn": true'; then
    warn "Don't forget to authenticate Claude Code:"
    echo "     ${BLUE}claude auth login${NC}"
    echo
fi
