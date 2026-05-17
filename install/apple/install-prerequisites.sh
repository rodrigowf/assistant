#!/usr/bin/env bash
# install/apple/install-prerequisites.sh
#
# Checks and installs system prerequisites on macOS.  Mirrors the
# Linux version (install/linux/install-prerequisites.sh) but offers to
# bootstrap dependencies via Homebrew when missing.
#
# Required:
#   - Python 3.12+
#   - Node.js 20+ (Qwen Code and Gemini CLI depend on it)
#   - npm (comes with Node)
#
# Optional but recommended:
#   - git
#
# Homebrew is the recommended install method on macOS.  If `brew` itself
# is missing, we offer to install it via the official one-liner and then
# proceed to install whatever else is missing.
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}✓${NC} $1"; }
warn()  { echo -e "${YELLOW}!${NC} $1"; }
error() { echo -e "${RED}✗${NC} $1"; }
ask()   { echo -e "${CYAN}?${NC} $1"; }

is_interactive() { [ -t 0 ]; }

# ─────────────────────────────────────────────────────────────────────────────
# Architecture detection — Homebrew's default prefix differs between
# Apple Silicon (/opt/homebrew) and Intel (/usr/local).  We ensure brew is on
# $PATH for whichever arch we land on.
# ─────────────────────────────────────────────────────────────────────────────
ARCH="$(uname -m)"
case "$ARCH" in
    arm64)
        BREW_PREFIX="/opt/homebrew"
        ;;
    x86_64)
        BREW_PREFIX="/usr/local"
        ;;
    *)
        BREW_PREFIX="/usr/local"  # best-effort fallback
        ;;
esac

if [ -x "$BREW_PREFIX/bin/brew" ] && ! command -v brew >/dev/null 2>&1; then
    # User has brew installed but their PATH doesn't include the prefix yet
    # (common on a fresh macOS where they haven't sourced their shell-init
    # yet).  Add it for the duration of this script.
    export PATH="$BREW_PREFIX/bin:$BREW_PREFIX/sbin:$PATH"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Bootstrap Homebrew if it's missing.  Homebrew is the canonical way to
# install Python/Node on macOS — without it we can't auto-install anything,
# so this is the gate we hit before checking individual tools.
# ─────────────────────────────────────────────────────────────────────────────
ensure_brew() {
    if command -v brew >/dev/null 2>&1; then
        info "Homebrew $(brew --version | head -1 | sed 's/^Homebrew //')"
        return 0
    fi
    warn "Homebrew is not installed."
    echo "    Homebrew is the recommended package manager on macOS — it's what we"
    echo "    use to install Python 3.12+ and Node.js 20+."
    echo ""
    if is_interactive; then
        ask "Install Homebrew now? [Y/n] "
        read -r ANS
        if [[ "${ANS:-Y}" =~ ^[Nn]$ ]]; then
            warn "Skipped Homebrew install — install missing prereqs manually and re-run."
            return 1
        fi
    fi
    echo ""
    echo "Running the official Homebrew installer..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    if [ -x "$BREW_PREFIX/bin/brew" ]; then
        export PATH="$BREW_PREFIX/bin:$BREW_PREFIX/sbin:$PATH"
        info "Homebrew installed at $BREW_PREFIX"
        echo ""
        warn "Add Homebrew to your shell's PATH permanently:"
        echo "    echo 'eval \"\$($BREW_PREFIX/bin/brew shellenv)\"' >> ~/.zprofile"
        echo "    eval \"\$($BREW_PREFIX/bin/brew shellenv)\""
        echo ""
    else
        error "Homebrew install did not produce $BREW_PREFIX/bin/brew — check the install output above."
        return 1
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Per-tool installers (called only if the tool is missing or outdated).
# Each one is idempotent — re-running on an already-installed tool just makes
# brew print a "already installed" line and returns 0.
# ─────────────────────────────────────────────────────────────────────────────
brew_install() {
    local pkg="$1"
    if is_interactive; then
        ask "Install $pkg via Homebrew? [Y/n] "
        read -r ANS
        if [[ "${ANS:-Y}" =~ ^[Nn]$ ]]; then
            warn "Skipped $pkg — install manually and re-run."
            return 1
        fi
    fi
    if brew install "$pkg"; then
        info "$pkg installed via Homebrew"
        return 0
    else
        error "brew install $pkg failed."
        return 1
    fi
}

MISSING=()

echo "Checking macOS prerequisites..."
echo

# Python
if command -v python3 >/dev/null 2>&1; then
    PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
    PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
    if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 12 ]; then
        info "Python $PY_VERSION"
    else
        error "Python $PY_VERSION (need 3.12+)"
        MISSING+=("python")
    fi
else
    error "Python not found"
    MISSING+=("python")
fi

# Node
if command -v node >/dev/null 2>&1; then
    NODE_VERSION=$(node -v | sed 's/v//')
    NODE_MAJOR=$(echo "$NODE_VERSION" | cut -d. -f1)
    if [ "$NODE_MAJOR" -ge 20 ]; then
        info "Node.js $NODE_VERSION"
    else
        error "Node.js $NODE_VERSION (need 20+)"
        MISSING+=("node")
    fi
else
    error "Node.js not found"
    MISSING+=("node")
fi

# npm
if command -v npm >/dev/null 2>&1; then
    info "npm $(npm -v)"
else
    error "npm not found"
    # npm is bundled with the node@20 brew formula, so "node" missing is
    # enough; only add it if node exists but npm doesn't (broken install).
    if command -v node >/dev/null 2>&1; then
        MISSING+=("node")
    fi
fi

# git (optional)
if command -v git >/dev/null 2>&1; then
    info "Git $(git --version | sed 's/git version //')"
else
    warn "Git not found (optional — comes pre-installed via Xcode Command Line Tools)"
    # Don't add to MISSING — git is technically optional, and on macOS the
    # easiest path is `xcode-select --install` rather than brew.
fi

echo

# ─────────────────────────────────────────────────────────────────────────────
# Install missing prereqs (interactive only).
# ─────────────────────────────────────────────────────────────────────────────
if [ "${#MISSING[@]}" -eq 0 ]; then
    info "All prerequisites satisfied!"
    echo
    echo "You can now run: ./install.sh"
    exit 0
fi

echo "Missing prerequisites: ${MISSING[*]}"
echo

if ! is_interactive; then
    cat <<MSG
Non-interactive shell detected — install the missing tools manually:

  Python 3.12+:
    brew install python@3.12

  Node.js 20+:
    brew install node@20
    brew link --overwrite --force node@20   # if you had an older node before

  Git (optional):
    xcode-select --install                  # or: brew install git
MSG
    exit 1
fi

# Interactive path — offer to install via brew.
ensure_brew || exit 1

INSTALL_FAILED=false
for item in "${MISSING[@]}"; do
    case $item in
        python)
            brew_install python@3.12 || INSTALL_FAILED=true
            ;;
        node)
            brew_install node@20 || INSTALL_FAILED=true
            # node@20 is keg-only; ensure its bin is on PATH for the
            # remainder of this install session.  Brew prints the right
            # `eval` line — we mirror it here so callers don't need to
            # restart their shell to get `npm` on PATH.
            if [ -x "$BREW_PREFIX/opt/node@20/bin/node" ]; then
                export PATH="$BREW_PREFIX/opt/node@20/bin:$PATH"
                info "Added node@20 to PATH for this session"
            fi
            ;;
    esac
done

if [ "$INSTALL_FAILED" = true ]; then
    error "One or more installs failed.  Fix the errors above and re-run."
    exit 1
fi

echo
info "All prerequisites installed!"
echo
echo "You may need to add Homebrew + node@20 to your shell init to make them"
echo "persist across sessions.  Brew's `shellenv` output above is the"
echo "canonical line for that.  Then run: ./install.sh"
