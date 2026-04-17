#!/usr/bin/env bash
# context-sync.sh — Bidirectional real-time sync for the assistant context folder.
#
# Uses inotifywait to detect file changes and rsync over SSH to push them to
# the remote machine immediately. Handles the remote being offline gracefully.
#
# Usage: context-sync.sh [--config /path/to/config]
# Normally started by the systemd service (context-sync.service).

set -euo pipefail

# ── Load config ──────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${1:-${SCRIPT_DIR}/config.env}"

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "ERROR: Config file not found: $CONFIG_FILE" >&2
  echo "Copy sync/config.env.example to sync/config.env and fill in your values." >&2
  exit 1
fi

# shellcheck source=/dev/null
source "$CONFIG_FILE"

# ── Required config variables ────────────────────────────────────────────────
: "${LOCAL_DIR:?config: LOCAL_DIR must be set}"
: "${REMOTE_HOST:?config: REMOTE_HOST must be set}"
: "${REMOTE_USER:?config: REMOTE_USER must be set}"
: "${REMOTE_DIR:?config: REMOTE_DIR must be set}"
: "${SSH_KEY:?config: SSH_KEY must be set}"

# ── Defaults ─────────────────────────────────────────────────────────────────
DEBOUNCE_SECONDS="${DEBOUNCE_SECONDS:-2}"
RETRY_INTERVAL="${RETRY_INTERVAL:-30}"
LOG_TAG="context-sync"

# ── Helpers ──────────────────────────────────────────────────────────────────
log() { echo "[$(date '+%H:%M:%S')] $*" | systemd-cat -t "$LOG_TAG" -p info 2>/dev/null || echo "[$(date '+%H:%M:%S')] $*"; }
err() { echo "[$(date '+%H:%M:%S')] ERROR: $*" | systemd-cat -t "$LOG_TAG" -p err 2>/dev/null || echo "[$(date '+%H:%M:%S')] ERROR: $*" >&2; }

SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes -i $SSH_KEY"

# Files/patterns to sync (exclude git internals, temp files, conflict files)
RSYNC_EXCLUDES=(
  --exclude='.git/'
  --exclude='.stfolder'
  --exclude='.stignore'
  --exclude='.stversions/'
  --exclude='*.sync-conflict-*'
  --exclude='.syncthing.*.tmp'
  --exclude='*.tmp'
  --exclude='.DS_Store'
)

rsync_to_remote() {
  rsync -az --update --delete \
    "${RSYNC_EXCLUDES[@]}" \
    -e "ssh $SSH_OPTS" \
    "$LOCAL_DIR/" \
    "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/"
}

remote_reachable() {
  ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" true 2>/dev/null
}

# ── Initial sync on startup ───────────────────────────────────────────────────
log "Starting context-sync: $LOCAL_DIR → ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}"
log "Debounce: ${DEBOUNCE_SECONDS}s, Retry interval: ${RETRY_INTERVAL}s"

# Do an initial full sync when the service starts (catches offline period)
while ! remote_reachable; do
  log "Remote not reachable, waiting ${RETRY_INTERVAL}s..."
  sleep "$RETRY_INTERVAL"
done
log "Initial sync..."
if rsync_to_remote; then
  log "Initial sync complete."
else
  err "Initial sync failed, continuing anyway."
fi

# ── Watch loop ────────────────────────────────────────────────────────────────
# inotifywait monitors recursively and outputs one event per line.
# We batch events with a debounce: wait DEBOUNCE_SECONDS after the last event
# before triggering rsync (avoids syncing mid-write during streaming responses).

PENDING=0
LAST_EVENT=0

inotifywait \
  --monitor \
  --recursive \
  --format '%T %e %w%f' \
  --timefmt '%s' \
  --event close_write,moved_to,moved_from,delete,create \
  --exclude '/\.git/' \
  --exclude '\.sync-conflict-' \
  --exclude '\.syncthing\.' \
  --exclude '\.stfolder' \
  --exclude '\.tmp$' \
  "$LOCAL_DIR" 2>/dev/null | \
while IFS= read -r line; do
  NOW=$(date +%s)
  LAST_EVENT=$NOW
  PENDING=1

  # Read any additional queued events (drain the buffer)
  while IFS= read -r -t "$DEBOUNCE_SECONDS" _; do
    LAST_EVENT=$(date +%s)
  done

  if [[ $PENDING -eq 1 ]]; then
    PENDING=0
    if remote_reachable; then
      if rsync_to_remote 2>/dev/null; then
        log "Synced after change."
      else
        err "Sync failed after change."
      fi
    else
      log "Remote offline, skipping sync (will retry on next event or restart)."
    fi
  fi
done
