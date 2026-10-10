#!/usr/bin/env bash
# install/doctor.sh — check (and optionally repair) an Archie install's harness wiring.
#
# Usage: install/doctor.sh [--repo DIR] [--harness LIST] [--fix | --dry-run] [--no-color]
#
# Checks, per harness (claude, modelstudio, qwen, gemini, codex):
#   - the CLI / SDK is present and matches the pinned version
#     (install/harness-versions.env, backend/requirements-claude.txt)
#   - the env keys it needs are set (names only — values are never printed)
#   - its auth exists (file existence only — nothing is read or printed)
#   - every symlink that routes its sessions/skills into context/
#   - the repo seed settings Archie depends on (.qwen/, .gemini/)
#   - memory/history wiring: context/memory/MEMORY.md, Gemini's project label
#     (projects.json + .project_root markers) and memory index, Codex's
#     [features] memories = false, ~/.gemini/GEMINI.md (warned about)
# plus the root instruction symlinks (CLAUDE.md, QWEN.md, GEMINI.md, AGENTS.md)
# and the context/{skills,scripts,agents} links to shared/.
#
# --fix repairs ONLY symlinks, seed files and seed keys (Qwen's memory keys,
# Codex's memories key, Gemini's projects.json entry and context/.project_root
# marker), with the installers' rules:
#   correct link → left alone; missing → created; real directory → its
#   sessions are copied into context/ (never overwriting) and the directory is
#   moved aside to <name>.bak-<timestamp>, then linked; empty real directory →
#   replaced; wrong or foreign link / regular file → warned about, never
#   clobbered.  It never installs packages and never touches auth.
# --dry-run prints what --fix would change and changes nothing.
#
# Harness selection: by default every harness found on this host is checked
# and the others are reported as SKIP.  --harness claude,codex (or "all")
# requires the listed ones, so anything missing becomes a FAIL.
#
# Exit status: 0 = no FAIL rows, 1 = at least one FAIL, 2 = usage error.
#
# Portable: bash 3.2+ (macOS), no GNU-only flags.  Safe to copy elsewhere and
# run with --repo (built-in pin defaults are used if the repo has no
# install/harness-versions.env; the helpers install/gemini-project.py and
# install/codex-home-config.py are looked up next to this script, then in
# the repo's install/).

set -u

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO=""
HARNESS_ARG=""
MODE="check"          # check | fix | plan
USE_COLOR=auto

usage() {
    sed -n '2,39p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --repo)      REPO="${2:-}"; shift 2 ;;
        --repo=*)    REPO="${1#--repo=}"; shift ;;
        --harness)   HARNESS_ARG="${2:-}"; shift 2 ;;
        --harness=*) HARNESS_ARG="${1#--harness=}"; shift ;;
        --fix)       MODE="fix"; shift ;;
        --dry-run)   MODE="plan"; shift ;;
        --no-color)  USE_COLOR=no; shift ;;
        -h|--help)   usage; exit 0 ;;
        *) echo "doctor.sh: unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
done

[ -n "$REPO" ] || REPO="$(cd "$SELF_DIR/.." && pwd)"
if [ ! -d "$REPO" ]; then
    echo "doctor.sh: repo dir not found: $REPO" >&2; exit 2
fi
REPO="$(cd "$REPO" && pwd)"
CTX="$REPO/context"

# ─────────────────────────────────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────────────────────────────────
if [ "$USE_COLOR" = auto ]; then
    if [ -t 1 ]; then USE_COLOR=yes; else USE_COLOR=no; fi
fi
if [ "$USE_COLOR" = yes ]; then
    C_OK=$'\033[0;32m'; C_WARN=$'\033[1;33m'; C_FAIL=$'\033[0;31m'
    C_DIM=$'\033[0;90m'; C_BOLD=$'\033[1m'; C_NC=$'\033[0m'
else
    C_OK=""; C_WARN=""; C_FAIL=""; C_DIM=""; C_BOLD=""; C_NC=""
fi

N_OK=0; N_WARN=0; N_FAIL=0; N_FIXED=0; N_SKIP=0
PLANNED=""     # newline-separated "would" actions (--dry-run)

row() {   # row STATUS SECTION CHECK DETAIL
    local st="$1" sec="$2" chk="$3" det="${4:-}" col=""
    case "$st" in
        OK)    col="$C_OK";   N_OK=$((N_OK + 1)) ;;
        FIXED) col="$C_OK";   N_FIXED=$((N_FIXED + 1)) ;;
        WARN)  col="$C_WARN"; N_WARN=$((N_WARN + 1)) ;;
        FAIL)  col="$C_FAIL"; N_FAIL=$((N_FAIL + 1)) ;;
        SKIP)  col="$C_DIM";  N_SKIP=$((N_SKIP + 1)) ;;
        INFO)  col="$C_DIM" ;;
    esac
    printf '%s%-5s%s  %-11s  %-30s  %s\n' "$col" "$st" "$C_NC" "$sec" "$chk" "$det"
}

section() { printf '\n%s%s%s\n' "$C_BOLD" "$1" "$C_NC"; }

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
have() { command -v "$1" >/dev/null 2>&1; }

# Resolve a path through all symlinks (macOS + Linux).  Prints nothing and
# fails if the path does not exist.
resolve() {
    [ -e "$1" ] || return 1
    if have realpath; then realpath "$1" 2>/dev/null && return 0; fi
    if readlink -f / >/dev/null 2>&1; then readlink -f "$1" 2>/dev/null && return 0; fi
    python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1" 2>/dev/null
}

# Run with a timeout when the platform has one (GNU coreutils / Homebrew).
run_to() {
    if have timeout; then timeout 30 "$@"
    elif have gtimeout; then gtimeout 30 "$@"
    else "$@"
    fi
}

# Show repo paths relative to the repo and $HOME as ~ in output.
pretty() {
    case "$1" in
        "$REPO"/*) printf '%s' "${1#"$REPO"/}" ;;
        "$HOME"/*) printf '~/%s' "${1#"$HOME"/}" ;;
        *) printf '%s' "$1" ;;
    esac
}

TS="$(date +%Y%m%dT%H%M%S)"

# helper NAME — path of an install/ helper script (next to this script first,
# so a copy run with --repo can bring its own), or nothing.
helper() {
    local d
    for d in "$SELF_DIR" "$REPO/install"; do
        [ -f "$d/$1" ] && { printf '%s' "$d/$1"; return 0; }
    done
    return 1
}

# env_has KEY — true when KEY is non-empty in the environment or context/.env.
# Values are never printed.
env_has() {
    local key="$1" val=""
    eval "val=\${$key:-}"
    [ -n "$val" ] && return 0
    [ -f "$CTX/.env" ] || return 1
    grep -Eq "^[[:space:]]*(export[[:space:]]+)?${key}=[\"']?[^\"'[:space:]#]" "$CTX/.env" 2>/dev/null
}
env_where() {   # where a key comes from (for the detail column)
    local key="$1" val=""
    eval "val=\${$key:-}"
    if [ -f "$CTX/.env" ] && grep -Eq "^[[:space:]]*(export[[:space:]]+)?${key}=[\"']?[^\"'[:space:]#]" "$CTX/.env" 2>/dev/null; then
        printf 'context/.env'
    elif [ -n "$val" ]; then
        printf 'environment'
    fi
}
# env_path KEY — value of a NON-SECRET path variable (QWEN_CLI_PATH, ...).
env_path() {
    local key="$1" val=""
    eval "val=\${$key:-}"
    if [ -z "$val" ] && [ -f "$CTX/.env" ]; then
        val="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$CTX/.env" 2>/dev/null | tail -n 1 | sed -e 's/^[^=]*=//' -e "s/^[\"']//" -e "s/[\"']\$//")"
    fi
    case "$val" in "~/"*) val="$HOME/${val#"~/"}" ;; esac
    printf '%s' "$val"
}

# need_fix SEV SECTION CHECK DETAIL ACTION — report a problem --fix can repair.
# check/dry-run: prints the row (dry-run also records ACTION) and returns 1.
# --fix: prints nothing and returns 0; the caller repairs and prints FIXED/FAIL.
need_fix() {
    case "$MODE" in
        fix)  return 0 ;;
        plan) row "$1" "$2" "$3" "$4 [would fix]"
              PLANNED="${PLANNED}  - $5
"; return 1 ;;
        *)    row "$1" "$2" "$3" "$4 [fixable: --fix]"; return 1 ;;
    esac
}

# Copy files from SRC into DST recursively, never overwriting.
# FILTER is a shell glob matched against the file's base name.
copy_noclobber() {
    local src="$1" dst="$2" filter="${3:-*}" f rel
    [ -d "$src" ] || return 0
    find "$src" -type f | while IFS= read -r f; do
        # shellcheck disable=SC2254
        case "$(basename "$f")" in $filter) ;; *) continue ;; esac
        rel="${f#"$src"/}"
        if [ ! -e "$dst/$rel" ]; then
            mkdir -p "$(dirname "$dst/$rel")" && cp -p "$f" "$dst/$rel"
        fi
    done
}

# check_link SECTION LABEL LINK TARGET LINKTEXT MIGRATE SEVERITY
#   LINK     — the path that should be a symlink
#   TARGET   — absolute path it must resolve to
#   LINKTEXT — what to write into a new link (relative or absolute)
#   MIGRATE  — what to do with a real directory at LINK:
#                none          leave alone (warn)
#                empty         replace only if empty
#                claude|qwen|gemini|codex   lift sessions into context/, move aside, link
#   SEVERITY — WARN or FAIL for a missing/broken link
check_link() {
    local sec="$1" label="$2" link="$3" target="$4" text="$5" migrate="$6" sev="$7"
    local shown cur
    shown="$(pretty "$link") → $(pretty "$target")"

    if [ -L "$link" ]; then
        if [ -e "$link" ] && [ -e "$target" ] && [ "$(resolve "$link")" = "$(resolve "$target")" ]; then
            row OK "$sec" "$label" "$shown"
            return
        fi
        cur="$(readlink "$link")"
        if [ ! -e "$link" ] && { [ "$cur" = "$text" ] || [ "$cur" = "$target" ]; }; then
            # Right link, target directory not created yet.
            if need_fix "$sev" "$sec" "$label" "$(pretty "$link") → $cur (target missing)" "mkdir -p $(pretty "$target")"; then
                if mkdir -p "$target"; then row FIXED "$sec" "$label" "created $(pretty "$target")"
                else row FAIL "$sec" "$label" "could not create $(pretty "$target")"; fi
            fi
            return
        fi
        row WARN "$sec" "$label" "$(pretty "$link") points to $cur, expected $(pretty "$target") — left alone (remove it by hand to relink)"
        return
    fi

    if [ -d "$link" ]; then
        case "$migrate" in
            none)
                row WARN "$sec" "$label" "$(pretty "$link") is a real directory — left alone"
                return ;;
            empty)
                if [ -n "$(ls -A "$link" 2>/dev/null)" ]; then
                    row WARN "$sec" "$label" "$(pretty "$link") is a non-empty real directory — merge it into $(pretty "$target") by hand, then relink"
                    return
                fi
                if need_fix "$sev" "$sec" "$label" "$(pretty "$link") is an empty real directory" "replace empty dir $(pretty "$link") with link → $text"; then
                    if rmdir "$link" && ln -s "$text" "$link"; then row FIXED "$sec" "$label" "$shown"
                    else row FAIL "$sec" "$label" "could not replace $(pretty "$link")"; fi
                fi
                return ;;
        esac
        if need_fix "$sev" "$sec" "$label" "$(pretty "$link") is a real directory (sessions not in context/)" \
            "migrate real dir $(pretty "$link") into $(pretty "$target"), move it to $(pretty "$link").bak-$TS, link → $text"; then
            mkdir -p "$target"
            case "$migrate" in
                claude) copy_noclobber "$link" "$target" '*' ;;
                qwen)   copy_noclobber "$link/chats" "$target/chats" '*.jsonl' ;;
                gemini) copy_noclobber "$link/chats" "$target/chats" 'session-*.jsonl' ;;
                codex)  copy_noclobber "$link" "$target" '*' ;;
            esac
            if mv "$link" "$link.bak-$TS" && ln -s "$text" "$link"; then
                row FIXED "$sec" "$label" "$shown (old dir kept at $(pretty "$link").bak-$TS)"
            else
                row FAIL "$sec" "$label" "could not migrate $(pretty "$link")"
            fi
        fi
        return
    fi

    if [ -e "$link" ]; then
        row WARN "$sec" "$label" "$(pretty "$link") is a regular file — left alone"
        return
    fi

    if need_fix "$sev" "$sec" "$label" "missing: $shown" "ln -s $text $(pretty "$link")"; then
        mkdir -p "$(dirname "$link")" "$target" 2>/dev/null
        if ln -s "$text" "$link"; then row FIXED "$sec" "$label" "$shown"
        else row FAIL "$sec" "$label" "could not create $(pretty "$link")"; fi
    fi
}

# version check: check_version SECTION LABEL HAVE PIN
check_version() {
    local sec="$1" label="$2" have_v="$3" pin="$4" binpath="$5" fix_hint="$6"
    if [ -z "$have_v" ]; then
        row WARN "$sec" "$label" "$(pretty "$binpath") (version unknown; pin $pin)"
    elif [ "$have_v" = "$pin" ]; then
        row OK "$sec" "$label" "$have_v = pin ($(pretty "$binpath"))"
    else
        row WARN "$sec" "$label" "$have_v ≠ pin $pin ($(pretty "$binpath")) — $fix_hint"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Pins
# ─────────────────────────────────────────────────────────────────────────────
QWEN_CLI_VERSION=0.25.0
GEMINI_CLI_VERSION=0.63.0
CODEX_CLI_VERSION=0.161.0
NODE_MIN_MAJOR=22
PIN_SOURCE="built-in defaults (no install/harness-versions.env in repo)"
if [ -f "$REPO/install/harness-versions.env" ]; then
    # shellcheck disable=SC1091
    . "$REPO/install/harness-versions.env"
    PIN_SOURCE="install/harness-versions.env"
fi
CLAUDE_SDK_PIN="$(grep -E '^claude-agent-sdk==' "$REPO/backend/requirements-claude.txt" 2>/dev/null | head -n 1 | sed 's/^claude-agent-sdk==//')"

# ─────────────────────────────────────────────────────────────────────────────
# Harness selection
# ─────────────────────────────────────────────────────────────────────────────
ALL_HARNESSES="claude modelstudio qwen gemini codex"
REQUESTED=""
if [ -n "$HARNESS_ARG" ]; then
    if [ "$HARNESS_ARG" = all ]; then REQUESTED="$ALL_HARNESSES"
    else REQUESTED="$(printf '%s' "$HARNESS_ARG" | tr ',' ' ')"; fi
    for h in $REQUESTED; do
        case " $ALL_HARNESSES " in *" $h "*) ;; *) echo "doctor.sh: unknown harness: $h" >&2; exit 2 ;; esac
    done
fi
requested() { case " $REQUESTED " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

# Locate the venv's claude-agent-sdk (dist-info gives the version without
# importing anything).
SDK_DISTINFO=""
for d in "$REPO"/.venv/lib/python3*/site-packages/claude_agent_sdk-*.dist-info; do
    [ -d "$d" ] && { SDK_DISTINFO="$d"; break; }
done
SDK_VERSION=""; SDK_BUNDLED=""
if [ -n "$SDK_DISTINFO" ]; then
    SDK_VERSION="$(basename "$SDK_DISTINFO" | sed -e 's/^claude_agent_sdk-//' -e 's/\.dist-info$//')"
    SDK_BUNDLED="$(dirname "$SDK_DISTINFO")/claude_agent_sdk/_bundled/claude"
fi

# Node (the CLIs for qwen/gemini and the web build need it).
NODE_VERSION=""
if have node; then NODE_VERSION="$(node -v 2>/dev/null | sed 's/^v//')"; fi
NODE_MAJOR="${NODE_VERSION%%.*}"

bin_for() {   # bin_for NAME OVERRIDE_VAR
    local p
    p="$(env_path "$2")"
    if [ -n "$p" ] && [ -x "$p" ]; then printf '%s' "$p"; return 0; fi
    command -v "$1" 2>/dev/null
}
QWEN_BIN="$(bin_for qwen QWEN_CLI_PATH)"
GEMINI_BIN="$(bin_for gemini GEMINI_CLI_PATH)"
CODEX_BIN="$(bin_for codex CODEX_CLI_PATH)"

enabled() {   # decide whether a harness is checked on this host
    if [ -n "$REQUESTED" ]; then requested "$1"; return; fi
    case "$1" in
        claude)      [ -n "$SDK_DISTINFO" ] || [ -d "$REPO/.claude_config" ] ;;
        modelstudio) [ -n "$SDK_DISTINFO" ] && env_has DASHSCOPE_API_KEY ;;
        qwen)        [ -n "$QWEN_BIN" ] ;;
        gemini)      [ -n "$GEMINI_BIN" ] ;;
        codex)       [ -n "$CODEX_BIN" ] ;;
    esac
}
skip_reason() {
    case "$1" in
        claude) printf 'claude-agent-sdk not in .venv and no .claude_config/' ;;
        modelstudio)
            if [ -z "$SDK_DISTINFO" ]; then printf 'needs claude-agent-sdk in .venv'
            else printf 'DASHSCOPE_API_KEY not set (not configured here)'; fi ;;
        qwen|gemini)
            if [ -z "$NODE_VERSION" ]; then printf '%s CLI not installed; no Node on this host — use it through an SSH working dir on a machine that has it' "$1"
            else printf '%s CLI not on PATH (set %s_CLI_PATH in context/.env if it lives elsewhere)' "$1" "$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]')"; fi ;;
        codex) printf 'codex CLI not on PATH (CODEX_CLI_PATH unset)' ;;
    esac
}

# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────
printf '%sArchie doctor%s — %s on %s (%s)\n' "$C_BOLD" "$C_NC" "$REPO" "$(hostname 2>/dev/null || echo '?')" "$(uname -sm 2>/dev/null)"
printf 'mode: %s · pins: %s\n' "$( case $MODE in check) echo check-only ;; fix) echo fix ;; plan) echo dry-run ;; esac )" "$PIN_SOURCE"
printf '%-5s  %-11s  %-30s  %s\n' "STAT" "AREA" "CHECK" "DETAIL"

# ── Common ───────────────────────────────────────────────────────────────────
section "common"
if [ -d "$CTX" ]; then row OK common "context/" "$(pretty "$CTX")"
else row FAIL common "context/" "missing — run the installer (--new-context / --import-context)"; fi
if [ -f "$CTX/AGENTS.md" ]; then row OK common "context/AGENTS.md" "present"
else row FAIL common "context/AGENTS.md" "missing — the root instruction links have nothing to point at"; fi
if [ -f "$CTX/.env" ]; then row OK common "context/.env" "present"
else row WARN common "context/.env" "missing — env keys only come from the environment"; fi
# The orchestrator's private memory: its identity (seeded from install/ on a new context).
if [ -d "$CTX" ]; then
    if [ -f "$CTX/memory/ORCHESTRATOR_MEMORY.md" ]; then row OK common "ORCHESTRATOR_MEMORY.md" "present"
    elif [ ! -f "$REPO/install/ORCHESTRATOR_MEMORY.md" ]; then
        row WARN common "ORCHESTRATOR_MEMORY.md" "missing — the orchestrator starts with no identity (no install/ORCHESTRATOR_MEMORY.md template to seed from)"
    elif need_fix WARN common "ORCHESTRATOR_MEMORY.md" "missing — the orchestrator starts with no identity" \
        "seed context/memory/ORCHESTRATOR_MEMORY.md from install/ORCHESTRATOR_MEMORY.md"; then
        mkdir -p "$CTX/memory" && cp "$REPO/install/ORCHESTRATOR_MEMORY.md" "$CTX/memory/ORCHESTRATOR_MEMORY.md" \
            && row FIXED common "ORCHESTRATOR_MEMORY.md" "seeded from install/" \
            || row FAIL common "ORCHESTRATOR_MEMORY.md" "could not seed"
    fi
    # Archie's docs are part of the memory wiki (indexed, searchable, linked from MEMORY.md).
    check_link common "context/memory/archie" "$CTX/memory/archie" "$REPO/docs" "../../docs" none WARN
    # The memory index every harness loads (Claude/Model Studio/Qwen/Codex via
    # the backend's memory block, Gemini natively through its tmp/<label> link).
    if [ -f "$CTX/memory/MEMORY.md" ]; then row OK common "context/memory/MEMORY.md" "present (every harness's memory index)"
    elif [ ! -f "$REPO/install/MEMORY.md" ]; then
        row FAIL common "context/memory/MEMORY.md" "missing — no session gets a memory index (no install/MEMORY.md template to seed from)"
    elif need_fix FAIL common "context/memory/MEMORY.md" "missing — no session gets a memory index" \
        "seed context/memory/MEMORY.md from install/MEMORY.md"; then
        mkdir -p "$CTX/memory" && cp "$REPO/install/MEMORY.md" "$CTX/memory/MEMORY.md" \
            && row FIXED common "context/memory/MEMORY.md" "seeded from install/" \
            || row FAIL common "context/memory/MEMORY.md" "could not seed"
    fi
fi
if [ -f "$REPO/backend/manager/memory_context.py" ]; then
    row OK common "memory block (backend)" "repo sessions get MEMORY.md + the wiki write rules (Claude, Model Studio, Qwen, Codex)"
else
    row WARN common "memory block (backend)" "backend/manager/memory_context.py missing — this backend predates memory parity across harnesses"
fi

if [ -n "$NODE_VERSION" ]; then
    if [ "${NODE_MAJOR:-0}" -ge "$NODE_MIN_MAJOR" ] 2>/dev/null; then row OK common "node" "$NODE_VERSION (≥ $NODE_MIN_MAJOR)"
    else row WARN common "node" "$NODE_VERSION (< $NODE_MIN_MAJOR: web build and qwen need $NODE_MIN_MAJOR+)"; fi
else
    row INFO common "node" "not installed — fine for a backend-only host (build the web app elsewhere; codex via its static binary)"
fi

for md in CLAUDE.md QWEN.md GEMINI.md AGENTS.md; do
    check_link common "$md" "$REPO/$md" "$CTX/AGENTS.md" "context/AGENTS.md" none FAIL
done

# context/{skills,scripts,agents} → shared/ links (what setup-context.sh makes)
for kind in skills scripts agents; do
    missing=""; nmissing=0; dangling=""; ndangling=0
    if [ -d "$REPO/shared/$kind" ] && [ -d "$CTX/$kind" ]; then
        for src in "$REPO/shared/$kind"/*; do
            [ -e "$src" ] || continue
            name="$(basename "$src")"
            [ "$name" = __pycache__ ] && continue
            [ "$kind" = skills ] && [ ! -d "$src" ] && continue
            if [ ! -e "$CTX/$kind/$name" ] && [ ! -L "$CTX/$kind/$name" ]; then
                missing="$missing $name"; nmissing=$((nmissing + 1))
            fi
        done
        for l in "$CTX/$kind"/*; do
            if [ -L "$l" ] && [ ! -e "$l" ]; then dangling="$dangling $(basename "$l")"; ndangling=$((ndangling + 1)); fi
        done
        if [ "$nmissing" -eq 0 ]; then row OK common "context/$kind -> shared" "every shared/$kind entry linked"
        else
            if need_fix WARN common "context/$kind -> shared" "$nmissing not linked:$missing" \
                "link$missing into context/$kind (setup-context.sh rule)"; then
                for name in $missing; do ln -s "../../shared/$kind/$name" "$CTX/$kind/$name"; done
                row FIXED common "context/$kind -> shared" "linked:$missing"
            fi
        fi
        [ "$ndangling" -gt 0 ] && row WARN common "context/$kind dangling" "$ndangling broken link(s):$dangling — left alone"
    elif [ ! -d "$CTX/$kind" ]; then
        row WARN common "context/$kind" "missing directory"
    fi
done

# ── Claude Code (and Model Studio, which runs the same CLI) ──────────────────
CLAUDE_ON=false; MS_ON=false
enabled claude && CLAUDE_ON=true
enabled modelstudio && MS_ON=true
MANGLED="$(printf '%s' "$REPO" | sed 's/[^A-Za-z0-9]/-/g')"

section "claude"
if [ "$CLAUDE_ON" = true ] || [ "$MS_ON" = true ]; then
    if [ -z "$CLAUDE_SDK_PIN" ]; then
        row WARN claude "claude-agent-sdk pin" "no claude-agent-sdk== line in backend/requirements-claude.txt"
    fi
    if [ -n "$SDK_VERSION" ]; then
        check_version claude "claude-agent-sdk" "$SDK_VERSION" "${CLAUDE_SDK_PIN:-?}" "$REPO/.venv" "pip install -r backend/requirements-claude.txt"
        if [ -x "$SDK_BUNDLED" ]; then row OK claude "bundled CLI" "$(pretty "$SDK_BUNDLED")"
        else row FAIL claude "bundled CLI" "missing in claude_agent_sdk/_bundled — reinstall the SDK"; fi
    else
        row FAIL claude "claude-agent-sdk" "not installed in .venv (pip install -r backend/requirements-claude.txt)"
    fi
    if have claude; then row INFO claude "claude on PATH" "$(pretty "$(command -v claude)") (optional; used for 'claude auth login')"; fi

    if [ "$CLAUDE_ON" = true ]; then
        CREDS="$REPO/.claude_config/.credentials.json"
        if env_has CLAUDE_CODE_OAUTH_TOKEN; then
            row OK claude "auth" "CLAUDE_CODE_OAUTH_TOKEN set ($(env_where CLAUDE_CODE_OAUTH_TOKEN))"
        elif [ -L "$CREDS" ] && [ -e "$CREDS" ]; then
            row OK claude "auth" ".claude_config/.credentials.json → $(pretty "$(readlink "$CREDS")")"
        elif [ -f "$CREDS" ]; then
            row OK claude "auth" ".claude_config/.credentials.json (a copy — can go stale; a link to ~/.claude or CLAUDE_CODE_OAUTH_TOKEN avoids that)"
        elif env_has ANTHROPIC_API_KEY; then
            row OK claude "auth" "ANTHROPIC_API_KEY set ($(env_where ANTHROPIC_API_KEY))"
        else
            row FAIL claude "auth" "no .claude_config/.credentials.json, CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY"
        fi
    fi

    check_link claude "projects/<cwd> -> context" "$REPO/.claude_config/projects/$MANGLED" "$CTX" "../../context" claude FAIL
    check_link claude ".claude_config/skills" "$REPO/.claude_config/skills" "$CTX/skills" "../context/skills" empty WARN
    check_link claude ".claude_config/agents" "$REPO/.claude_config/agents" "$CTX/agents" "../context/agents" empty WARN
    # Auto-memory: the backend switches it off for sessions in the repo
    # (CLAUDE_CODE_DISABLE_AUTO_MEMORY=1) and appends MEMORY.md itself;
    # .claude_config/settings.json is a user setting SDK sessions never read.
    AUTOMEM="not set"
    if [ -f "$REPO/.claude_config/settings.json" ]; then
        AUTOMEM="$(sed -n 's/.*"autoMemoryEnabled"[[:space:]]*:[[:space:]]*\([a-z]*\).*/\1/p' "$REPO/.claude_config/settings.json" | head -n 1)"
        [ -n "$AUTOMEM" ] || AUTOMEM="not set"
    fi
    if grep -q 'CLAUDE_CODE_DISABLE_AUTO_MEMORY' "$REPO/backend/manager/claude/session.py" 2>/dev/null; then
        row INFO claude "auto-memory" "off in repo sessions (backend: CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 + MEMORY.md block); .claude_config autoMemoryEnabled: $AUTOMEM (SDK sessions don't read it)"
    else
        row WARN claude "auto-memory" "this backend leaves Claude's auto-memory on — it writes its own flat notes into context/memory/ (.claude_config autoMemoryEnabled: $AUTOMEM is not read by SDK sessions)"
    fi
    if [ -f "$REPO/.claude/settings.json" ]; then row INFO claude ".claude/settings.json" "present"
    else row INFO claude ".claude/settings.json" "not seeded (install/cli-runtime/claude/settings.json; optional allowlist, not auto-fixed)"; fi
else
    row SKIP claude "harness" "$(skip_reason claude)"
fi

section "modelstudio"
if [ "$MS_ON" = true ]; then
    if [ -n "$SDK_VERSION" ]; then row OK modelstudio "runtime" "claude-agent-sdk $SDK_VERSION (same bundled CLI and .claude_config links as claude)"
    else row FAIL modelstudio "runtime" "needs claude-agent-sdk (pip install -r backend/requirements-claude.txt)"; fi
    if env_has DASHSCOPE_API_KEY; then row OK modelstudio "DASHSCOPE_API_KEY" "set ($(env_where DASHSCOPE_API_KEY))"
    else row FAIL modelstudio "DASHSCOPE_API_KEY" "not set — sessions fail to start"; fi
else
    row SKIP modelstudio "harness" "$(skip_reason modelstudio)"
fi

# ── Qwen Code ────────────────────────────────────────────────────────────────
section "qwen"
if enabled qwen; then
    if [ -n "$QWEN_BIN" ]; then
        check_version qwen "qwen CLI" "$(run_to "$QWEN_BIN" --version 2>/dev/null | head -n 1 | tr -d '[:space:]')" "$QWEN_CLI_VERSION" "$QWEN_BIN" "npm install -g @qwen-code/qwen-code@$QWEN_CLI_VERSION"
    else
        row FAIL qwen "qwen CLI" "not found (npm install -g @qwen-code/qwen-code@$QWEN_CLI_VERSION; needs Node $NODE_MIN_MAJOR+)"
    fi
    BACKEND_QWEN_PIN="$(grep -E '^QWEN_CLI_VERSION *=' "$REPO/backend/manager/qwen/adapter.py" 2>/dev/null | head -n 1 | sed -e 's/.*= *//' -e 's/["'\'' ]//g')"
    if [ -n "$BACKEND_QWEN_PIN" ] && [ "$BACKEND_QWEN_PIN" != "$QWEN_CLI_VERSION" ]; then
        row WARN qwen "pin consistency" "backend QWEN_CLI_VERSION=$BACKEND_QWEN_PIN ≠ $PIN_SOURCE $QWEN_CLI_VERSION"
    fi
    if [ -n "$NODE_VERSION" ] && [ "${NODE_MAJOR:-0}" -lt "$NODE_MIN_MAJOR" ] 2>/dev/null; then
        row FAIL qwen "node" "$NODE_VERSION — qwen-code $QWEN_CLI_VERSION needs Node $NODE_MIN_MAJOR+"
    fi
    if env_has DASHSCOPE_API_KEY; then row OK qwen "auth" "DASHSCOPE_API_KEY set ($(env_where DASHSCOPE_API_KEY))"
    elif [ -f "$HOME/.qwen/oauth_creds.json" ]; then row OK qwen "auth" "~/.qwen/oauth_creds.json present"
    else row FAIL qwen "auth" "no DASHSCOPE_API_KEY and no ~/.qwen/oauth_creds.json"; fi

    check_link qwen "projects/<cwd> -> context" "$HOME/.qwen/projects/$MANGLED" "$CTX" "$CTX" qwen FAIL
    check_link qwen "~/.qwen/skills" "$HOME/.qwen/skills" "$CTX/skills" "$CTX/skills" empty WARN

    QS="$REPO/.qwen/settings.json"
    if [ ! -f "$QS" ]; then
        if need_fix FAIL qwen ".qwen/settings.json" "missing (seed: install/cli-runtime/qwen/settings.json)" \
            "seed .qwen/settings.json from install/cli-runtime/qwen/settings.json"; then
            if [ -f "$REPO/install/cli-runtime/qwen/settings.json" ]; then
                mkdir -p "$REPO/.qwen" && cp "$REPO/install/cli-runtime/qwen/settings.json" "$QS" && row FIXED qwen ".qwen/settings.json" "seeded"
            else row FAIL qwen ".qwen/settings.json" "no seed file in this repo"; fi
        fi
    elif have python3; then
        QBAD="$(python3 - "$QS" <<'PY' 2>/dev/null
import json, sys
try:
    m = (json.load(open(sys.argv[1])).get("memory") or {})
except Exception:
    print("unparseable"); sys.exit(0)
bad = [k for k in ("enableManagedAutoMemory", "enableManagedAutoDream", "enableAutoSkill") if m.get(k) is not False]
print(" ".join(bad))
PY
)"
        if [ -z "$QBAD" ]; then
            row OK qwen ".qwen/settings.json" "memory.enableManagedAutoMemory/AutoDream/AutoSkill = false"
        elif [ "$QBAD" = unparseable ]; then
            row FAIL qwen ".qwen/settings.json" "not valid JSON — fix it by hand"
        else
            if need_fix FAIL qwen ".qwen/settings.json" "memory keys not false: $QBAD" \
                "set memory.{$QBAD}=false in .qwen/settings.json (backup .bak-$TS)"; then
                cp -p "$QS" "$QS.bak-$TS" && python3 - "$QS" <<'PY' && row FIXED qwen ".qwen/settings.json" "memory keys set to false (backup .qwen/settings.json.bak-$TS)"
import json, sys
p = sys.argv[1]
d = json.load(open(p))
m = d.setdefault("memory", {})
for k in ("enableManagedAutoMemory", "enableManagedAutoDream", "enableAutoSkill"):
    m[k] = False
open(p, "w").write(json.dumps(d, indent=2) + "\n")
PY
            fi
        fi
    else
        row WARN qwen ".qwen/settings.json" "present; python3 missing, cannot verify the memory keys"
    fi
else
    row SKIP qwen "harness" "$(skip_reason qwen)"
fi

# ── Gemini CLI ───────────────────────────────────────────────────────────────
section "gemini"
if enabled gemini; then
    if [ -n "$GEMINI_BIN" ]; then
        check_version gemini "gemini CLI" "$(run_to "$GEMINI_BIN" --version 2>/dev/null | tail -n 1 | tr -d '[:space:]')" "$GEMINI_CLI_VERSION" "$GEMINI_BIN" "npm install -g @google/gemini-cli@$GEMINI_CLI_VERSION"
    else
        row FAIL gemini "gemini CLI" "not found (npm install -g @google/gemini-cli@$GEMINI_CLI_VERSION)"
    fi
    if env_has GEMINI_API_KEY; then row OK gemini "GEMINI_API_KEY" "set ($(env_where GEMINI_API_KEY))"
    else row FAIL gemini "GEMINI_API_KEY" "not set — the only auth Google still serves Gemini CLI"; fi
    GENV="$HOME/.gemini/.env"
    if [ -f "$GENV" ] && grep -Eq '^[[:space:]]*(export[[:space:]]+)?GEMINI_API_KEY=.' "$GENV" 2>/dev/null; then
        perms="$(ls -ln "$GENV" | cut -c1-10)"
        if [ "$perms" = "-rw-------" ]; then row OK gemini "~/.gemini/.env" "has GEMINI_API_KEY, mode 600"
        else row WARN gemini "~/.gemini/.env" "has GEMINI_API_KEY but mode is $perms (chmod 600 it)"; fi
    else
        row WARN gemini "~/.gemini/.env" "no GEMINI_API_KEY — needed only if this host is an SSH remote for Gemini (non-interactive SSH shells don't read context/.env)"
    fi

    # The label pins where Gemini keeps this repo's chats and its memory index
    # (~/.gemini/tmp/<label>/{chats,memory/MEMORY.md}); the link makes that
    # context/.  projects.json maps the repo to the label, and a .project_root
    # marker naming another path makes Gemini claim a new label.
    GPROJ="$(helper gemini-project.py || true)"
    GLABEL=""; GSTAT=""
    if [ -n "$GPROJ" ] && have python3; then
        GLABEL="$(python3 "$GPROJ" "$REPO" label 2>/dev/null)"
        [ -n "$GLABEL" ] && GSTAT="$(python3 "$GPROJ" "$REPO" check --label "$GLABEL" 2>/dev/null)"
    fi
    gval() { printf '%s\n' "$GSTAT" | sed -n "s/^$1=//p" | head -n 1; }
    if [ -z "$GLABEL" ]; then
        if [ -f "$HOME/.gemini/projects.json" ] && have python3; then
            GLABEL="$(python3 -c 'import json,sys; print((json.load(open(sys.argv[1])).get("projects") or {}).get(sys.argv[2]) or "")' "$HOME/.gemini/projects.json" "$REPO" 2>/dev/null)"
        fi
        if [ -n "$GLABEL" ]; then row OK gemini "projects.json label" "$REPO → $GLABEL"
        else
            GLABEL="$(basename "$REPO")"
            row WARN gemini "projects.json label" "not checked (needs python3 and install/gemini-project.py); assuming \"$GLABEL\""
        fi
    fi
    check_link gemini "tmp/<label> -> context" "$HOME/.gemini/tmp/$GLABEL" "$CTX" "$CTX" gemini FAIL

    gemini_apply() {   # gemini_apply SECTION CHECK — run the registration helper (--fix)
        local out
        if out="$(python3 "$GPROJ" "$REPO" apply --label "$GLABEL" 2>&1)"; then
            row FIXED gemini "$2" "$(printf '%s\n' "$out" | sed -n 's/^note: //p' | head -n 1)"
        else
            row FAIL gemini "$2" "$(printf '%s\n' "$out" | sed -n 's/^problem: //p' | head -n 1)"
        fi
    }
    if [ -n "$GSTAT" ]; then
        case "$(gval registry)" in
            unparseable) row FAIL gemini "projects.json" "~/.gemini/projects.json is not valid JSON — Gemini resets it (every project gets a new label); fix it by hand" ;;
            *)
                case "$(gval registered)" in
                    yes) row OK gemini "projects.json label" "$REPO → $GLABEL" ;;
                    *)
                        if need_fix WARN gemini "projects.json label" "repo not registered — Gemini picks a label on its next run (\"$GLABEL\" if still free)" \
                            "register $REPO → $GLABEL in ~/.gemini/projects.json"; then
                            gemini_apply gemini "projects.json label"
                        fi ;;
                esac ;;
        esac
        MT="$(gval marker_tmp)"
        case "$MT" in
            foreign:*)
                if need_fix FAIL gemini "ownership marker" "context/.project_root names ${MT#foreign:} — Gemini would claim a new label (chats + MEMORY.md leave context/)" \
                    "rewrite context/.project_root to $REPO"; then
                    gemini_apply gemini "ownership marker"
                fi ;;
        esac
        MH="$(gval marker_history)"
        case "$MH" in
            foreign:*) row FAIL gemini "history marker" "~/.gemini/history/$GLABEL/.project_root names ${MH#foreign:} — Gemini would claim a new label; move that dir aside by hand" ;;
        esac
    fi
    # Gemini loads ~/.gemini/tmp/<label>/memory/MEMORY.md into every session;
    # through the link it must be the wiki's root index.
    GMEM="$HOME/.gemini/tmp/$GLABEL/memory/MEMORY.md"
    if [ -e "$GMEM" ] && [ -e "$CTX/memory/MEMORY.md" ] && [ "$(resolve "$GMEM")" = "$(resolve "$CTX/memory/MEMORY.md")" ]; then
        row OK gemini "memory index" "$(pretty "$GMEM") → context/memory/MEMORY.md"
    elif [ ! -f "$CTX/memory/MEMORY.md" ]; then
        row FAIL gemini "memory index" "context/memory/MEMORY.md missing (see common)"
    else
        row FAIL gemini "memory index" "$(pretty "$GMEM") does not resolve to context/memory/MEMORY.md — fix the tmp/<label> link"
    fi
    if [ -f "$HOME/.gemini/GEMINI.md" ]; then
        row WARN gemini "~/.gemini/GEMINI.md" "exists — Gemini's global memory tier (outside context/, not synced, not indexed); move its facts into context/memory/ and delete it"
    else
        row OK gemini "~/.gemini/GEMINI.md" "absent (no memory outside context/)"
    fi

    GS="$REPO/.gemini/settings.json"
    GPY="python3"; [ -x "$REPO/.venv/bin/python" ] && GPY="$REPO/.venv/bin/python"
    if [ ! -f "$REPO/backend/manager/gemini/workspace_settings.py" ]; then
        row WARN gemini ".gemini/settings.json" "backend/manager/gemini/workspace_settings.py missing — cannot verify"
    else
        GSTATE="$("$GPY" - "$REPO" <<'PY' 2>/dev/null
import json, os, sys
repo = sys.argv[1]
sys.path.insert(0, os.path.join(repo, "backend", "manager", "gemini"))
import workspace_settings as w
p = w.settings_path(repo)
if not p.exists():
    print("missing"); sys.exit(0)
try:
    cur = json.loads(p.read_text())
except Exception:
    print("unparseable"); sys.exit(0)
print("ok" if w.merge_archie_settings(cur) == cur else "stale")
PY
)"
        case "$GSTATE" in
            ok) row OK gemini ".gemini/settings.json" "Archie's keys present (retention off, fileFiltering, auth, thinking)" ;;
            missing|stale)
                if need_fix FAIL gemini ".gemini/settings.json" "$GSTATE — the backend refuses Gemini turns" \
                    "python3 backend/manager/gemini/workspace_settings.py $REPO (merge Archie's keys)"; then
                    "$GPY" "$REPO/backend/manager/gemini/workspace_settings.py" "$REPO" >/dev/null 2>&1 \
                        && row FIXED gemini ".gemini/settings.json" "Archie's keys merged" \
                        || row FAIL gemini ".gemini/settings.json" "merge failed — fix the file by hand"
                fi ;;
            unparseable) row FAIL gemini ".gemini/settings.json" "not valid JSON — fix it by hand" ;;
            *) row WARN gemini ".gemini/settings.json" "could not verify (needs Python 3.7+)" ;;
        esac
    fi
else
    row SKIP gemini "harness" "$(skip_reason gemini)"
fi

# ── Codex ────────────────────────────────────────────────────────────────────
section "codex"
if enabled codex; then
    if [ -n "$CODEX_BIN" ]; then
        check_version codex "codex CLI" "$(run_to "$CODEX_BIN" --version 2>/dev/null | head -n 1 | awk '{print $NF}')" "$CODEX_CLI_VERSION" "$CODEX_BIN" "npm install -g @openai/codex@$CODEX_CLI_VERSION (or the static binary from GitHub release rust-v$CODEX_CLI_VERSION)"
    else
        row FAIL codex "codex CLI" "not found (npm install -g @openai/codex@$CODEX_CLI_VERSION, or the static binary)"
    fi
    CODEX_ARCHIE="$HOME/.codex-archie"
    OVERRIDE_HOME="$(env_path ARCHIE_CODEX_HOME)"
    SHARED_HOME="${CODEX_HOME:-$HOME/.codex}"
    if [ -n "$OVERRIDE_HOME" ]; then
        if [ -f "$OVERRIDE_HOME/auth.json" ]; then row OK codex "auth" "ARCHIE_CODEX_HOME=$(pretty "$OVERRIDE_HOME") has auth.json"
        else row FAIL codex "auth" "ARCHIE_CODEX_HOME=$(pretty "$OVERRIDE_HOME") has no auth.json"; fi
    elif [ -f "$CODEX_ARCHIE/auth.json" ]; then
        row OK codex "auth" "~/.codex-archie/auth.json (dedicated login)"
    elif [ -f "$SHARED_HOME/auth.json" ]; then
        row WARN codex "auth" "only the shared $(pretty "$SHARED_HOME")/auth.json — rollouts stay outside context/; dedicated login: CODEX_HOME=~/.codex-archie codex login --device-auth"
    else
        row FAIL codex "auth" "no login — CODEX_HOME=~/.codex-archie codex login --device-auth"
    fi
    if [ -f "$CODEX_ARCHIE/config.toml" ]; then
        if grep -q '^[[:space:]]*project_doc_max_bytes' "$CODEX_ARCHIE/config.toml"; then row OK codex "~/.codex-archie/config.toml" "present (project_doc_max_bytes set)"
        else row WARN codex "~/.codex-archie/config.toml" "no project_doc_max_bytes (the backend passes it per spawn too) — left alone"; fi
    else
        if need_fix WARN codex "~/.codex-archie/config.toml" "missing" \
            "seed ~/.codex-archie/config.toml (install/cli-runtime/codex-home/config.toml)"; then
            mkdir -p "$CODEX_ARCHIE" && chmod 700 "$CODEX_ARCHIE" 2>/dev/null
            if [ -f "$REPO/install/cli-runtime/codex-home/config.toml" ]; then
                cp "$REPO/install/cli-runtime/codex-home/config.toml" "$CODEX_ARCHIE/config.toml"
            else
                printf '# Archie'\''s Codex home (CODEX_HOME=~/.codex-archie).\nproject_doc_max_bytes = 131072\n\n[features]\nplugins = false\napps = false\nmemories = false\n' > "$CODEX_ARCHIE/config.toml"
            fi
            row FIXED codex "~/.codex-archie/config.toml" "seeded"
        fi
    fi
    # Codex's own memory store (CODEX_HOME/memories) stays off: Archie passes
    # MEMORY.md per session and memory is written into the wiki.
    if [ -f "$CODEX_ARCHIE/config.toml" ]; then
        CCFG="$(helper codex-home-config.py || true)"
        CMEM=""
        if [ -n "$CCFG" ] && have python3; then
            CMEM="$(python3 "$CCFG" "$CODEX_ARCHIE/config.toml" check 2>/dev/null)"
        fi
        case "$CMEM" in
            ok) row OK codex "memories feature" "[features] memories = false (memory lives in the wiki)" ;;
            missing|on)
                if [ "$CMEM" = on ]; then CSEV=FAIL; CDET="enabled — Codex keeps its own memory store outside context/"
                else CSEV=WARN; CDET="not pinned in [features] (off by default in $CODEX_CLI_VERSION)"; fi
                if need_fix "$CSEV" codex "memories feature" "$CDET" \
                    "set [features] memories = false in ~/.codex-archie/config.toml"; then
                    if python3 "$CCFG" "$CODEX_ARCHIE/config.toml" apply >/dev/null 2>&1; then
                        row FIXED codex "memories feature" "[features] memories = false"
                    else
                        row FAIL codex "memories feature" "could not edit ~/.codex-archie/config.toml — add it by hand"
                    fi
                fi ;;
            unsupported) row WARN codex "memories feature" "config.toml uses an inline features table — set memories = false there by hand" ;;
            *)
                if grep -Eq '^[[:space:]]*memories[[:space:]]*=[[:space:]]*false' "$CODEX_ARCHIE/config.toml"; then
                    row OK codex "memories feature" "memories = false (grep only — install/codex-home-config.py or python3 unavailable)"
                else
                    row WARN codex "memories feature" "not verified (needs python3 and install/codex-home-config.py); want [features] memories = false"
                fi ;;
        esac
    fi
    check_link codex "sessions -> context" "$CODEX_ARCHIE/sessions" "$CTX/codex/sessions" "$CTX/codex/sessions" codex FAIL
else
    row SKIP codex "harness" "$(skip_reason codex)"
fi

# Repo-level skills dir read by Codex (verified on 0.161), Gemini CLI and Qwen
# Code: <repo>/.agents/skills → context/skills.
if enabled codex || enabled gemini || enabled qwen; then
    check_link codex ".agents/skills" "$REPO/.agents/skills" "$CTX/skills" "../context/skills" empty WARN
fi

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────
printf '\n%sSummary:%s %d OK, %d WARN, %d FAIL, %d SKIP' "$C_BOLD" "$C_NC" "$N_OK" "$N_WARN" "$N_FAIL" "$N_SKIP"
[ "$MODE" = fix ] && printf ', %d FIXED' "$N_FIXED"
printf '\n'
if [ "$MODE" = plan ]; then
    if [ -n "$PLANNED" ]; then printf '\n%s--fix would:%s\n%s' "$C_BOLD" "$C_NC" "$PLANNED"
    else printf '\n--fix would change nothing.\n'; fi
fi
[ "$N_FAIL" -eq 0 ]
