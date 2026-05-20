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
  echo "Copy install/sync.env to sync/config.env and fill in your values." >&2
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
  # Push file contents only. Deletions are handled out-of-band by
  # remote_delete_paths so we never use rsync's --delete on incremental syncs
  # (which would race with files the other side just created and not yet
  # pushed to us).
  rsync -az --update \
    "${RSYNC_EXCLUDES[@]}" \
    -e "ssh $SSH_OPTS" \
    "$LOCAL_DIR/" \
    "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/"
}

rsync_full_with_delete() {
  # Used only for the initial sync at startup, when no concurrent writers
  # exist yet so --delete is safe.
  rsync -az --update --delete \
    "${RSYNC_EXCLUDES[@]}" \
    -e "ssh $SSH_OPTS" \
    "$LOCAL_DIR/" \
    "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/"
}

# Delete a specific set of paths on the remote. Each entry is relative to
# $LOCAL_DIR / $REMOTE_DIR. We rm -rf each one so this handles both files and
# directories (inotify reports ISDIR on directory deletes/renames).
remote_delete_paths() {
  local -a paths=("$@")
  [[ ${#paths[@]} -eq 0 ]] && return 0
  # Build a NUL-delimited list and feed it to a remote xargs that rm -rfs
  # each entry under $REMOTE_DIR. NUL-delimiting tolerates spaces/newlines
  # in paths; the cd guarantees we never rm outside $REMOTE_DIR even if a
  # path somehow got through as absolute.
  printf '%s\0' "${paths[@]}" | \
    ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" \
      "cd '${REMOTE_DIR}' && xargs -0 -r -I{} rm -rf -- './{}'"
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
if rsync_full_with_delete; then
  log "Initial sync complete."
else
  err "Initial sync failed, continuing anyway."
fi

# ── Watch loop ────────────────────────────────────────────────────────────────
# inotifywait monitors recursively and outputs one event per line.
# We batch events with a debounce: wait DEBOUNCE_SECONDS after the last event
# before triggering rsync (avoids syncing mid-write during streaming responses).
#
# Deletion strategy: instead of running rsync --delete (which races with files
# the other side just created and not yet pushed to us), we collect the exact
# set of paths that were deleted locally during the debounce window and rm
# only those on the remote after the content push.

# Parse one inotify line of form "<timestamp> <EVENT[,EVENT...]> <fullpath>".
# Sets parse_event / parse_path / parse_isdir as globals. The path may contain
# spaces, so we extract fields 1 and 2 with parameter expansion and treat the
# remainder as the path.
parse_event_line() {
  local rest="$1"
  rest="${rest#* }"           # drop timestamp
  parse_event="${rest%% *}"   # event field
  parse_path="${rest#* }"     # everything after the event field
  case ",${parse_event}," in
    *,ISDIR,*|*ISDIR,*|*,ISDIR*) parse_isdir=1 ;;
    *) parse_isdir=0 ;;
  esac
}

# Returns 0 if the event field contains DELETE or MOVED_FROM.
is_delete_event() {
  case "$1" in
    *DELETE*|*MOVED_FROM*) return 0 ;;
    *) return 1 ;;
  esac
}

# Convert an absolute path under $LOCAL_DIR into a path relative to $LOCAL_DIR.
# Echoes nothing if the path is not under $LOCAL_DIR (shouldn't happen).
relative_to_local() {
  local abs="$1"
  local base="${LOCAL_DIR%/}/"
  if [[ "$abs" == "$base"* ]]; then
    printf '%s' "${abs#"$base"}"
  fi
}

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
  PENDING=1
  DELETED_PATHS=()

  parse_event_line "$line"
  if is_delete_event "$parse_event"; then
    rel=$(relative_to_local "$parse_path")
    [[ -n "$rel" ]] && DELETED_PATHS+=("$rel")
  fi

  # Drain any additional queued events within the debounce window.
  while IFS= read -r -t "$DEBOUNCE_SECONDS" extra; do
    parse_event_line "$extra"
    if is_delete_event "$parse_event"; then
      rel=$(relative_to_local "$parse_path")
      [[ -n "$rel" ]] && DELETED_PATHS+=("$rel")
    fi
  done

  if [[ $PENDING -eq 1 ]]; then
    PENDING=0
    # Confirm deletions against the live filesystem. A MOVED_FROM during an
    # atomic-rename (e.g. recorder rotating tempfiles) looks identical to a
    # delete but the path reappears under the same name once the rename
    # completes; replicating that as a remote rm races with the rsync push
    # and can wipe the just-renamed file on the remote. Only keep paths that
    # are genuinely gone locally after the debounce window closes.
    CONFIRMED_DELETES=()
    for rel in "${DELETED_PATHS[@]}"; do
      if [[ ! -e "${LOCAL_DIR%/}/$rel" ]]; then
        CONFIRMED_DELETES+=("$rel")
      fi
    done
    if remote_reachable; then
      # 1) Push content first (no --delete). A file the remote already has
      #    but we just modified gets updated; new files get created.
      if ! rsync_to_remote 2>/dev/null; then
        err "Sync failed after change."
        continue
      fi
      # 2) Then apply the per-path deletions we actually observed locally.
      if [[ ${#CONFIRMED_DELETES[@]} -gt 0 ]]; then
        if remote_delete_paths "${CONFIRMED_DELETES[@]}" 2>/dev/null; then
          log "Synced after change (rm ${#CONFIRMED_DELETES[@]} path(s) on remote)."
        else
          err "Sync content ok but remote deletion failed for ${#CONFIRMED_DELETES[@]} path(s)."
        fi
      else
        log "Synced after change (no deletions)."
      fi
    else
      log "Remote offline, skipping sync (will retry on next event or restart)."
    fi
  fi
done
