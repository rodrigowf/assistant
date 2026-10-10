#!/usr/bin/env bash
# context-sync.sh — Bidirectional real-time sync for the assistant context folder.
#
# Uses inotifywait to detect file changes and rsync over SSH to push them to
# the remote machine immediately. Handles the remote being offline gracefully,
# and replays deletes made while the machines were apart (see "Reconcile").
#
# Usage:
#   context-sync.sh [config]                    run the service (systemd: context-sync.service)
#   context-sync.sh --status [config]           manifest, held deletions, trash
#   context-sync.sh --approve-deletes [config]  apply the deletions held for approval
#   context-sync.sh --reject-deletes [config]   keep those files instead (restore them on both sides)
# Internal, called over ssh by the other machine (same script, same path):
#   context-sync.sh --list <dir>                "path<TAB>size<TAB>mtime" of every synced file
#   context-sync.sh --trash <dir>               move NUL-separated paths from stdin to <dir>/.sync-trash/

set -euo pipefail

TRASH_DIRNAME=".sync-trash"

# Every synced file under $1 as "path<TAB>size<TAB>mtime", sorted. Skips what
# rsync and inotifywait skip (git, trash, temp and conflict files).
list_tree() {
  (cd "$1" && find . \( -path ./.git -o -path "./$TRASH_DIRNAME" \) -prune -o -type f -printf '%P\t%s\t%Ts\n') |
    awk -F'\t' '$1 !~ /(^|\/)\.DS_Store$|\.tmp$|\.sync-conflict-|\.syncthing\.|(^|\/)\.st(folder|ignore|versions)(\/|$)/' |
    LC_ALL=C sort
}

# Move NUL-separated relative paths (stdin) under $1 into $1/.sync-trash/<date>/,
# keeping their relative layout. Nothing is ever removed outright: a deleted
# file can be restored from the trash for TRASH_DAYS.
trash_paths() {
  local base="$1" day p t d
  day=$(date +%F)
  while IFS= read -r -d '' p; do
    [[ -z "$p" || "$p" == /* || "/$p/" == */../* || "$p" == "$TRASH_DIRNAME"* ]] && continue
    [[ -e "$base/$p" || -L "$base/$p" ]] || continue
    t="$base/$TRASH_DIRNAME/$day/$p"
    [[ -e "$t" || -L "$t" ]] && t="$t.$(date +%s%N)"
    mkdir -p -- "$(dirname -- "$t")" && mv -f -- "$base/$p" "$t" || continue
    # Remove the parent folders this left empty (never $base itself).
    d="${p%/*}"
    while [[ "$d" != "$p" && -n "$d" ]] && rmdir -- "$base/$d" 2>/dev/null; do
      [[ "$d" == */* ]] && d="${d%/*}" || d=""
    done
  done
}

case "${1:-}" in
  --list) list_tree "${2:?--list needs a directory}"; exit 0 ;;
  --trash) trash_paths "${2:?--trash needs a directory}"; exit 0 ;;
esac

COMMAND=run
case "${1:-}" in
  --status|--approve-deletes|--reject-deletes) COMMAND="${1#--}"; shift ;;
esac

# ── Load config ──────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${1:-${SCRIPT_DIR}/config.env}"

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "ERROR: Config file not found: $CONFIG_FILE" >&2
  echo "Copy install/sync.env to infra/sync/config.env and fill in your values." >&2
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
# How long a deleted path stays "tombstoned": pushes skip it and a stale copy
# that comes back (older than the delete) is removed again. See "Deletions".
TOMBSTONE_SECONDS="${TOMBSTONE_SECONDS:-120}"
# Full two-way reconcile (see "Reconcile") at startup, after the remote comes
# back, and at least this often.
RECONCILE_SECONDS="${RECONCILE_SECONDS:-600}"
# How often the idle loop wakes up to check for approve/reject decisions and
# whether a periodic reconcile is due.
TICK_SECONDS="${TICK_SECONDS:-30}"
# Deletions a reconcile infers are held for approval when there are more than
# MAX_DELETES of them, or more than MAX_DELETE_PERCENT of the synced files.
MAX_DELETES="${MAX_DELETES:-50}"
MAX_DELETE_PERCENT="${MAX_DELETE_PERCENT:-25}"
# Trash folders (.sync-trash/<date>/) older than this are purged.
TRASH_DAYS="${TRASH_DAYS:-30}"
# Pause before exiting when inotifywait can't watch a directory, so a host that
# is still out of watches restarts every ~45 s instead of every 15 s.
WATCH_FAIL_DELAY="${WATCH_FAIL_DELAY:-30}"
# State (tombstones, manifest, held deletions): ours, and the remote's
# (relative to the remote $HOME). The remote runs this same script.
STATE_DIR="${STATE_DIR:-$HOME/.local/state/context-sync}"
REMOTE_STATE_DIR="${REMOTE_STATE_DIR:-.local/state/context-sync}"
REMOTE_SCRIPT="${REMOTE_SCRIPT:-assistant/infra/sync/context-sync.sh}"
LOG_TAG="${LOG_TAG:-context-sync}"
SCRIPT_PID=$$

TOMBSTONES="$STATE_DIR/tombstones"
# The manifest is the "last agreed" state: every file both machines had at the
# last sync, with its size and mtime. A file in it that is now missing on one
# side was deleted there (see "Reconcile").
MANIFEST="$STATE_DIR/manifest"
PENDING="$STATE_DIR/pending-deletes"
APPROVED="$STATE_DIR/approved-deletes"
REJECTED="$STATE_DIR/rejected-deletes"
# Paths the other machine is holding for approval (it writes this file): our
# pushes skip them too, so a hold can't be undone by a push from this side.
REMOTE_HELD="$STATE_DIR/remote-held"
mkdir -p "$STATE_DIR"
touch "$TOMBSTONES"

# ── Commands ─────────────────────────────────────────────────────────────────
case "$COMMAND" in
  status)
    echo "Manifest: $( [[ -f $MANIFEST ]] && wc -l < "$MANIFEST" || echo "none yet") file(s) agreed with ${REMOTE_HOST}."
    if [[ -s "$PENDING" ]]; then
      echo "Held for approval ($(wc -l < "$PENDING")): ('here' = this machine, 'there' = ${REMOTE_HOST})"
      sed -e 's/^DELLOCAL\t/  delete here:  /' -e 's/^DELREMOTE\t/  delete there: /' "$PENDING"
      echo "Apply: $0 --approve-deletes   Keep the files instead: $0 --reject-deletes"
    else
      echo "No deletions held for approval."
    fi
    [[ -s "$APPROVED" ]] && echo "Approved, waiting for the service: $(wc -l < "$APPROVED") path(s)."
    [[ -s "$REJECTED" ]] && echo "Kept, waiting for the service: $(wc -l < "$REJECTED") path(s)."
    echo "Trash: $(ls -1 "$LOCAL_DIR/$TRASH_DIRNAME" 2>/dev/null | tr '\n' ' ')"
    exit 0 ;;
  approve-deletes|reject-deletes)
    if [[ ! -s "$PENDING" ]]; then echo "No deletions held for approval."; exit 0; fi
    # Decisions are per path: the running service applies them at its next
    # tick (within TICK_SECONDS), even if more deletions were held meanwhile.
    if [[ "$COMMAND" == approve-deletes ]]; then target="$APPROVED"; other="$REJECTED"; else target="$REJECTED"; other="$APPROVED"; fi
    cut -f2 "$PENDING" >> "$target"
    LC_ALL=C sort -u -o "$target" "$target"
    [[ -f "$other" ]] && grep -Fxv -f "$target" "$other" > "$other.$$" || true
    [[ -f "$other.$$" ]] && mv -f "$other.$$" "$other"
    if [[ "$COMMAND" == approve-deletes ]]; then
      echo "Approved $(wc -l < "$PENDING") deletion(s); the service applies them within ${TICK_SECONDS}s (files go to $TRASH_DIRNAME/<date>/ on the machine they're removed from)."
    else
      echo "Kept $(wc -l < "$PENDING") file(s); the service restores them on both sides within ${TICK_SECONDS}s."
    fi
    exit 0 ;;
esac

# ── Helpers ──────────────────────────────────────────────────────────────────
log() { echo "[$(date '+%H:%M:%S')] $*" | systemd-cat -t "$LOG_TAG" -p info 2>/dev/null || echo "[$(date '+%H:%M:%S')] $*"; }
err() { echo "[$(date '+%H:%M:%S')] ERROR: $*" | systemd-cat -t "$LOG_TAG" -p err 2>/dev/null || echo "[$(date '+%H:%M:%S')] ERROR: $*" >&2; }

SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes -i $SSH_KEY"
REMOTE="${REMOTE_USER}@${REMOTE_HOST}"

# Files/patterns to sync (exclude git internals, the trash, temp files, conflict files)
RSYNC_EXCLUDES=(
  --exclude='.git/'
  --exclude="/$TRASH_DIRNAME/"
  --exclude='.stfolder'
  --exclude='.stignore'
  --exclude='.stversions/'
  --exclude='*.sync-conflict-*'
  --exclude='.syncthing.*.tmp'
  --exclude='*.tmp'
  --exclude='.DS_Store'
)

# "a, b, c (+N more)" for log lines.
summarize() {
  local -a items=("$@")
  local n=${#items[@]} shown
  (( n == 0 )) && return 0
  shown=$(printf '%s, ' "${items[@]:0:3}")
  shown=${shown%, }
  (( n > 3 )) && shown+=" (+$((n - 3)) more)"
  printf '%s' "$shown"
}

# ── Tombstones ───────────────────────────────────────────────────────────────
# One "<epoch>\t<relative path>" line per deleted path. Written by this side when
# it deletes something, and by the other side when it deletes something here.

# Drop expired tombstones. Called once per batch (not on every read), so it
# rarely overlaps with the other side appending to the file.
prune_tombstones() {
  local cutoff=$(( $(date +%s) - TOMBSTONE_SECONDS )) tmp="$TOMBSTONES.$$"
  awk -F'\t' -v c="$cutoff" '$1 >= c' "$TOMBSTONES" > "$tmp" 2>/dev/null && mv -f "$tmp" "$TOMBSTONES"
}

# Print "<epoch>\t<path>" for tombstones younger than TOMBSTONE_SECONDS.
active_tombstones() {
  local cutoff=$(( $(date +%s) - TOMBSTONE_SECONDS ))
  awk -F'\t' -v c="$cutoff" '$1 >= c' "$TOMBSTONES" 2>/dev/null || true
}

add_tombstones() {
  local now p
  now=$(date +%s)
  for p in "$@"; do printf '%s\t%s\n' "$now" "$p"; done >> "$TOMBSTONES"
}

# Epoch of the youngest tombstone covering a path (the path itself or a parent
# directory), or nothing.
tombstone_time() {
  local rel="$1"
  active_tombstones | awk -F'\t' -v p="$rel" \
    'p == $2 || index(p, $2 "/") == 1 { if ($1 > t) t = $1 } END { if (t) print t }'
}

# Forget the tombstones of the paths in file $1 (files we decided to keep),
# including those of their parent directories, which cover them too.
drop_tombstones() {
  local tmp="$TOMBSTONES.$$"
  awk -F'\t' '
    FILENAME == ARGV[1] {
      if ($0 == "") next
      p = $0; keep[p] = 1
      while ((i = match(p, /\/[^\/]*$/)) > 0) { p = substr(p, 1, i - 1); keep[p] = 1 }
      next
    }
    !($2 in keep)' "$1" "$TOMBSTONES" > "$tmp" && mv -f "$tmp" "$TOMBSTONES"
}

# Escape a relative path into an anchored rsync pattern.
rsync_pattern() { printf '/%s\n' "$(sed 's/[][*?\\]/\\&/g' <<< "$1")"; }

# rsync exclude patterns for held deletions (ours and the other machine's),
# except the paths listed in file $1 (ones you decided to keep, which must be
# copied back even while the other machine still holds them).
held_excludes() {
  local except="${1:-/dev/null}"
  { [[ -f "$PENDING" ]] && cut -f2 "$PENDING"; [[ -f "$REMOTE_HELD" ]] && cat "$REMOTE_HELD"; } 2>/dev/null |
    awk 'FILENAME == ARGV[1] { x[$0] = 1; next } $0 != "" && !($0 in x)' "$except" - |
    while IFS= read -r p; do rsync_pattern "$p"; done
  return 0
}

# True when n deletions (out of a manifest of m files) need approval.
over_brake() {
  local n="$1" m="$2"
  (( n > 0 )) && { (( n > MAX_DELETES )) || (( n * 100 > m * MAX_DELETE_PERCENT )); }
}

# rsync exclude patterns for the active tombstones. A path re-created here
# since its delete (mtime at or after the tombstone) is a new file and is sent.
tombstone_excludes() {
  local t p mtime
  while IFS=$'\t' read -r t p; do
    [[ -z "$p" ]] && continue
    mtime=$(stat -c %Y -- "${LOCAL_DIR%/}/$p" 2>/dev/null || echo 0)
    (( mtime >= t )) && continue
    rsync_pattern "$p"
  done < <(active_tombstones)
}

# ── Manifest ─────────────────────────────────────────────────────────────────
# Update the manifest after a live sync: $1 = file of paths now on both sides
# (re-stat'ed here), $2 = file of paths agreed deleted (their subtrees too).
manifest_apply() {
  local setf="$1" rmf="$2" stats tmp="$MANIFEST.$$"
  stats=$(mktemp)
  while IFS= read -r p; do
    [[ -f "${LOCAL_DIR%/}/$p" ]] || continue
    printf '%s\t%s\n' "$p" "$(stat -c '%s	%Y' -- "${LOCAL_DIR%/}/$p")"
  done < "$setf" > "$stats"
  touch "$MANIFEST"
  awk -F'\t' -v OFS='\t' '
    FILENAME == ARGV[1] { if ($0 != "") rm[$0] = 1; next }
    FILENAME == ARGV[2] { set[$1] = $0; next }
    {
      p = $1
      if (p in set) next
      for (r in rm) if (p == r || index(p, r "/") == 1) next
      print
    }
    END { for (p in set) print set[p] }' "$rmf" "$stats" "$MANIFEST" | LC_ALL=C sort > "$tmp"
  mv -f "$tmp" "$MANIFEST"
  rm -f "$stats"
}

# ── rsync / ssh ──────────────────────────────────────────────────────────────
PUSHED=()
PULLED=()

# rsync with our excludes plus the patterns in file $1; direction "push" or
# "pull". Sets TRANSFERRED to the files rsync sent.
TRANSFERRED=()
# Optional $3: a file of relative paths; only those are sent (live pushes).
rsync_dir() {
  local direction="$1" extra="$2" only="${3:-}" out rc src dst errexit=0
  local -a scope=()
  [[ -n "$only" ]] && scope=(--files-from="$only" -r)
  if [[ "$direction" == push ]]; then src="$LOCAL_DIR/"; dst="${REMOTE}:${REMOTE_DIR}/"
  else src="${REMOTE}:${REMOTE_DIR}/"; dst="$LOCAL_DIR/"; fi
  [[ $- == *e* ]] && errexit=1
  set +e
  out=$(rsync -az --update --prune-empty-dirs --out-format='%n' "${scope[@]}" \
    "${RSYNC_EXCLUDES[@]}" \
    --exclude-from="$extra" \
    -e "ssh $SSH_OPTS" \
    "$src" "$dst" 2>&1)
  rc=$?
  (( errexit )) && set -e
  (( rc == 24 )) && rc=0   # "some files vanished before they could be transferred": fine
  if (( rc != 0 )); then
    err "rsync $direction failed (exit $rc): $(tail -n 2 <<< "$out" | tr '\n' ' ')"
    return "$rc"
  fi
  mapfile -t TRANSFERRED < <(grep -v '/$' <<< "$out" | grep -v '^$' || true)
}

rsync_to_remote() {
  # Push only what changed here ($@: paths from this batch's events; a new
  # directory is sent with its contents). Never the whole tree: a file this
  # machine has but the other doesn't may have been deleted there while we
  # weren't looking, and only the reconcile (with the manifest) can tell.
  # No --delete either: deletions go out-of-band (remote_delete_paths).
  # Tombstoned and held paths are skipped. Sets PUSHED to the files sent.
  local excludes list rc=0 p
  PUSHED=()
  list=$(mktemp)
  for p in "$@"; do [[ -e "${LOCAL_DIR%/}/$p" ]] && printf '%s\n' "$p"; done | LC_ALL=C sort -u > "$list"
  if [[ ! -s "$list" ]]; then rm -f "$list"; return 0; fi
  excludes=$(mktemp)
  { tombstone_excludes; held_excludes; } > "$excludes"
  rsync_dir push "$excludes" "$list" || rc=$?
  rm -f "$excludes" "$list"
  (( rc == 0 )) || return "$rc"
  PUSHED=("${TRANSFERRED[@]}")
}

# Tombstone paths on the remote (so its pushes skip them and it doesn't echo
# the delete back), then move them to the remote's trash. Each entry is
# relative to $LOCAL_DIR / $REMOTE_DIR; directories work too.
remote_delete_paths() {
  local -a paths=("$@")
  [[ ${#paths[@]} -eq 0 ]] && return 0
  local now p
  now=$(date +%s)
  for p in "${paths[@]}"; do printf '%s\t%s\n' "$now" "$p"; done | \
    ssh $SSH_OPTS "$REMOTE" \
      "mkdir -p ~/'${REMOTE_STATE_DIR}' && cat >> ~/'${REMOTE_STATE_DIR}'/tombstones"
  # NUL-delimited list: tolerates spaces/newlines in paths; --trash refuses
  # absolute paths and "..", so nothing outside $REMOTE_DIR is ever touched.
  printf '%s\0' "${paths[@]}" | \
    ssh $SSH_OPTS "$REMOTE" "bash '${REMOTE_SCRIPT}' --trash '${REMOTE_DIR}'"
}

remote_reachable() {
  ssh $SSH_OPTS "$REMOTE" true 2>/dev/null
}

purge_trash() {
  local cutoff d
  cutoff=$(date -d "-${TRASH_DAYS} days" +%F)
  for d in "${LOCAL_DIR%/}/$TRASH_DIRNAME"/*/; do
    [[ -d "$d" ]] || continue
    [[ "$(basename "$d")" < "$cutoff" ]] && rm -rf -- "$d" && log "Purged trash folder $(basename "$d")."
  done
  return 0
}

# ── Reconcile ────────────────────────────────────────────────────────────────
# Compares three listings: the manifest (what both sides had at the last
# sync), this side and the other side.
#   - in the manifest, gone here, unchanged there   → deleted here while apart:
#     delete there (to its trash);
#   - in the manifest, gone there, unchanged here   → deleted there: delete here
#     (to our trash);
#   - gone on one side but changed on the other     → the edit wins: it is
#     copied back, and the conflict is logged;
#   - not in the manifest                           → new: copied, never deleted.
# Then both directions are copied (rsync --update, no --delete) and the
# manifest is rewritten from the result. If the inferred deletions exceed
# MAX_DELETES or MAX_DELETE_PERCENT of the manifest, none is applied: they are
# held in $PENDING for --approve-deletes / --reject-deletes, and those paths are
# left alone (not copied either way) until then.
NEED_RECONCILE=1
LAST_RECONCILE=0

reconcile() {
  local work m l r plan n_m n_del held=0
  work=$(mktemp -d)
  m="$work/manifest"; l="$work/local"; r="$work/remote"; plan="$work/plan"
  if [[ -f "$MANIFEST" ]]; then cp "$MANIFEST" "$m"; else : > "$m"; fi
  list_tree "$LOCAL_DIR" > "$l"
  if ! ssh $SSH_OPTS "$REMOTE" "bash '${REMOTE_SCRIPT}' --list '${REMOTE_DIR}'" > "$r" 2>"$work/err"; then
    err "Reconcile: couldn't list the remote: $(tail -n 1 "$work/err")"
    rm -rf "$work"; return 1
  fi

  awk -F'\t' -v OFS='\t' '
    FILENAME == ARGV[1] { m[$1] = $2 "\t" $3; next }
    FILENAME == ARGV[2] { l[$1] = $2 "\t" $3; next }
    FILENAME == ARGV[3] { r[$1] = $2 "\t" $3; next }
    END {
      for (p in m) {
        inl = (p in l); inr = (p in r)
        if (!inl && inr) {
          if (r[p] == m[p]) print "DELREMOTE", p
          else print "KEEP", p, "deleted here, changed there"
        } else if (inl && !inr) {
          if (l[p] == m[p]) print "DELLOCAL", p
          else print "KEEP", p, "deleted there, changed here"
        }
      }
    }' "$m" "$l" "$r" | LC_ALL=C sort > "$plan"

  grep -E '^DEL(LOCAL|REMOTE)	' "$plan" > "$work/dels" || true
  n_m=$(wc -l < "$m")
  # Per-path decisions from --approve-deletes / --reject-deletes come first.
  touch "$APPROVED" "$REJECTED"
  awk -F'\t' 'FILENAME == ARGV[1] { a[$0] = 1; next } ($2 in a)' "$APPROVED" "$work/dels" > "$work/approved"
  awk -F'\t' 'FILENAME == ARGV[1] { r[$0] = 1; next } ($2 in r)' "$REJECTED" "$work/dels" > "$work/rejected"
  awk -F'\t' 'FILENAME == ARGV[1] { d[$0] = 1; next } !($2 in d)' <(cut -f2 "$work/approved" "$work/rejected") "$work/dels" > "$work/undecided"
  # Decisions are consumed: whatever no longer matches a candidate is stale.
  : > "$APPROVED"; : > "$REJECTED"
  n_del=$(wc -l < "$work/undecided")
  : > "$work/held"
  if over_brake "$n_del" "$n_m"; then
    held=1
    cp "$work/undecided" "$work/held"
    if [[ -f "$PENDING" ]] && cmp -s "$PENDING" "$work/held"; then
      held=2   # same as last time: already reported
    fi
    cp "$work/held" "$PENDING"
    (( held == 1 )) && err "Reconcile: holding $n_del deletion(s) for approval (limit ${MAX_DELETES} or ${MAX_DELETE_PERCENT}%): $(cut -f2 "$work/held" | head -n 3 | tr '\n' ' ')… Review with: context-sync.sh --status"
  else
    rm -f "$PENDING"
  fi
  # Tell the other machine what we hold, so its pushes leave those paths alone.
  cut -f2 "$work/held" | ssh $SSH_OPTS "$REMOTE" \
    "mkdir -p ~/'${REMOTE_STATE_DIR}' && cat > ~/'${REMOTE_STATE_DIR}'/remote-held" ||
    err "Reconcile: couldn't send the held list to the remote."

  local -a del_local=() del_remote=() kept=() rejected=()
  # Applied: the approved ones, plus the undecided ones when not held.
  if (( held )); then cp "$work/approved" "$work/apply"; else cat "$work/approved" "$work/undecided" > "$work/apply"; fi
  mapfile -t del_local < <(awk -F'\t' '$1 == "DELLOCAL" { print $2 }' "$work/apply")
  mapfile -t del_remote < <(awk -F'\t' '$1 == "DELREMOTE" { print $2 }' "$work/apply")
  mapfile -t rejected < <(cut -f2 "$work/rejected")
  mapfile -t kept < <(awk -F'\t' '$1 == "KEEP" { print $2 " (" $3 ")" }' "$plan")
  # Kept files (rejected deletions, edit beat delete) are copied back: forget
  # their tombstones, and drop the rejected ones from the manifest so they
  # count as new on both sides.
  { cut -f2 "$work/rejected"; awk -F'\t' '$1 == "KEEP" { print $2 }' "$plan"; } > "$work/keep"
  drop_tombstones "$work/keep"
  if (( ${#rejected[@]} > 0 )); then
    awk -F'\t' 'FILENAME == ARGV[1] { r[$0] = 1; next } !($1 in r)' "$work/keep" "$m" > "$m.tmp" && mv -f "$m.tmp" "$m"
  fi

  # Deletions: tombstone (both sides), then move to the trash.
  if (( ${#del_remote[@]} > 0 )); then
    add_tombstones "${del_remote[@]}"
    remote_delete_paths "${del_remote[@]}" || err "Reconcile: deleting on the remote failed."
  fi
  if (( ${#del_local[@]} > 0 )); then
    add_tombstones "${del_local[@]}"
    printf '%s\0' "${del_local[@]}" | trash_paths "${LOCAL_DIR%/}"
  fi

  # Copies both ways; held paths are left alone.
  { tombstone_excludes; held_excludes "$work/keep"; } > "$work/excludes"
  if ! rsync_dir push "$work/excludes"; then rm -rf "$work"; return 1; fi
  PUSHED=("${TRANSFERRED[@]}")
  if ! rsync_dir pull "$work/excludes"; then rm -rf "$work"; return 1; fi
  PULLED=("${TRANSFERRED[@]}")

  # New manifest: what this side has now (= the other side), plus the held
  # paths deleted here (still agreed until approved or rejected).
  list_tree "$LOCAL_DIR" > "$work/new"
  awk -F'\t' 'FILENAME == ARGV[1] { if ($1 == "DELREMOTE") keep[$2] = 1; next } ($1 in keep)' \
    "$work/held" "$m" >> "$work/new"
  LC_ALL=C sort -u "$work/new" > "$MANIFEST"

  local msg="Reconcile: pushed ${#PUSHED[@]}, pulled ${#PULLED[@]}"
  (( ${#PUSHED[@]} > 0 )) && msg+=" [push: $(summarize "${PUSHED[@]}")]"
  (( ${#PULLED[@]} > 0 )) && msg+=" [pull: $(summarize "${PULLED[@]}")]"
  (( ${#del_remote[@]} > 0 )) && msg+="; deleted there (to trash): $(summarize "${del_remote[@]}")"
  (( ${#del_local[@]} > 0 )) && msg+="; deleted here (to trash): $(summarize "${del_local[@]}")"
  (( ${#rejected[@]} > 0 )) && msg+="; restored ${#rejected[@]} file(s) you chose to keep"
  (( ${#kept[@]} > 0 )) && msg+="; edit beat delete, kept: $(summarize "${kept[@]}")"
  (( held )) && msg+="; $n_del deletion(s) held for approval"
  # A reconcile with nothing to do (or only the same held deletions) is silent.
  if (( ${#PUSHED[@]} + ${#PULLED[@]} + ${#del_remote[@]} + ${#del_local[@]} + ${#rejected[@]} + ${#kept[@]} > 0 || held == 1 )); then
    log "$msg."
  fi
  purge_trash
  rm -rf "$work"
  NEED_RECONCILE=0
  LAST_RECONCILE=$SECONDS
}

# A reconcile is due: after an outage, when it's been RECONCILE_SECONDS, or
# when an approve/reject decision is waiting.
reconcile_due() {
  (( NEED_RECONCILE )) || (( SECONDS - LAST_RECONCILE >= RECONCILE_SECONDS )) || [[ -s "$APPROVED" || -s "$REJECTED" ]]
}

# ── Startup ───────────────────────────────────────────────────────────────────
log "Starting context-sync: $LOCAL_DIR → ${REMOTE}:${REMOTE_DIR}"
log "Debounce: ${DEBOUNCE_SECONDS}s, reconcile every ${RECONCILE_SECONDS}s, tombstones ${TOMBSTONE_SECONDS}s, hold deletes > ${MAX_DELETES} or ${MAX_DELETE_PERCENT}%"

# A full reconcile when the service starts catches up on the offline period,
# including deletes made while the service or the other machine was down.
while ! remote_reachable; do
  log "Remote not reachable, waiting ${RETRY_INTERVAL}s..."
  sleep "$RETRY_INTERVAL"
done
reconcile || err "Initial reconcile failed; retrying later."

# ── Watch loop ────────────────────────────────────────────────────────────────
# inotifywait monitors recursively and outputs one event per line. It honours only the LAST
# --exclude, so all exclusions are one alternation.
# We batch events with a debounce: wait DEBOUNCE_SECONDS after the last event
# before triggering rsync (avoids syncing mid-write during streaming responses).
# With no event for RECONCILE_SECONDS a full reconcile runs anyway.
#
# Deletions: instead of running rsync --delete (which races with files the
# other side just created and not yet pushed to us), we collect the exact set of
# paths that were deleted locally during the debounce window and move only
# those to the remote's trash after the content push. Each deleted path is also
# tombstoned on both sides for TOMBSTONE_SECONDS:
#   - pushes skip tombstoned paths, so neither side sends a deleted file back;
#   - a tombstoned path that reappears here with an mtime older than the delete
#     is a stale copy from a push that was already in flight; it is removed again;
#   - a local delete of a path the remote already tombstoned (the remote deleted
#     it here) is not echoed back.
# A delete made while the remote is unreachable stays in the manifest, and the
# reconcile after it comes back applies it.
#
# Watch failures: inotifywait never retries a directory it couldn't watch (for
# example when the user's inotify watches ran out), so that directory would stay
# blind until a restart. On such an error we exit with a failure and let systemd
# restart us with a fresh, complete set of watches.

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

# True when rel is the temp file of a finished rsync transfer: rsync writes
# "<dir>/.<name>.XXXXXX" and renames it to "<dir>/<name>", so the MOVED_FROM of
# the temp name is not a deletion worth replicating.
is_rsync_temp() {
  local rel="$1" dir base
  base="${rel##*/}"
  [[ "$rel" == */* ]] && dir="${rel%/*}/" || dir=""
  [[ "$base" =~ ^\.(.+)\.[A-Za-z0-9]{6}$ ]] || return 1
  [[ -e "${LOCAL_DIR%/}/${dir}${BASH_REMATCH[1]}" ]]
}

# Record one event in the batch arrays.
collect_event() {
  local rel
  parse_event_line "$1"
  rel=$(relative_to_local "$parse_path")
  [[ -z "$rel" ]] && return 0
  if is_delete_event "$parse_event"; then
    DELETED_PATHS+=("$rel")
  else
    CHANGED_PATHS+=("$rel")
  fi
}

# stderr of inotifywait: log it; a directory it couldn't watch means a restart.
watch_errors() {
  local e restarting=0
  while IFS= read -r e; do
    case "$e" in
      "Setting up watches"*|"Watches established"*) continue ;;
      # Harmless: inotifywait failing to drop the watch of a directory that was just deleted.
      *"emove watch"*|*"emoving watch"*) continue ;;
    esac
    err "inotifywait: $e"
    if (( ! restarting )) && [[ "$e" == *"Couldn't watch"* || "$e" == *"upper limit on inotify watches"* ]]; then
      restarting=1
      err "A directory is not being watched; restarting in ${WATCH_FAIL_DELAY}s (check fs.inotify.max_user_watches)."
      # SIGUSR1's default action ends the script with a failure status, so
      # systemd (Restart=on-failure) starts it again and kills the leftovers.
      ( sleep "$WATCH_FAIL_DELAY"; kill -USR1 "$SCRIPT_PID" ) &
    fi
  done
}

# One debounced batch of events, starting with $1.
handle_batch() {
  local rel abs t mtime msg
  prune_tombstones
  DELETED_PATHS=()
  CHANGED_PATHS=()
  collect_event "$1"

  # Drain any additional queued events within the debounce window.
  while IFS= read -r -t "$DEBOUNCE_SECONDS" extra; do
    collect_event "$extra"
  done

  # 1) Stale copies: a tombstoned path that reappeared with an mtime older than
  #    its delete came from a push that was already in flight. Move it to the
  #    trash again (the trash, so even this can be undone).
  local -a resurrected=()
  for rel in "${CHANGED_PATHS[@]}"; do
    abs="${LOCAL_DIR%/}/$rel"
    [[ -f "$abs" ]] || continue
    t=$(tombstone_time "$rel")
    [[ -z "$t" ]] && continue
    mtime=$(stat -c %Y "$abs" 2>/dev/null || echo 0)
    (( mtime < t )) && resurrected+=("$rel")
  done
  if (( ${#resurrected[@]} > 0 )); then
    printf '%s\0' "${resurrected[@]}" | trash_paths "${LOCAL_DIR%/}"
    log "Moved ${#resurrected[@]} stale cop(ies) of deleted path(s) to the trash: $(summarize "${resurrected[@]}")"
  fi

  # 2) Confirm deletions against the live filesystem. A MOVED_FROM during an
  #    atomic-rename (e.g. recorder rotating tempfiles) looks identical to a
  #    delete but the path reappears under the same name once the rename
  #    completes; replicating that as a remote rm races with the rsync push
  #    and can wipe the just-renamed file on the remote. Only keep paths that
  #    are genuinely gone locally after the debounce window closes and that
  #    aren't rsync temp files. Paths the remote deleted here itself
  #    (tombstoned before our delete) are agreed deletions, not echoed back.
  local -a confirmed=() remote_done=()
  for rel in "${DELETED_PATHS[@]}"; do
    [[ -e "${LOCAL_DIR%/}/$rel" ]] && continue
    is_rsync_temp "$rel" && continue
    if [[ -n "$(tombstone_time "$rel")" ]]; then remote_done+=("$rel"); continue; fi
    confirmed+=("$rel")
  done
  (( ${#confirmed[@]} > 0 )) && add_tombstones "${confirmed[@]}"
  # Brake for live deletes too: a mass delete is not replicated right away.
  # The paths stay in the manifest, and the reconcile below holds them for
  # approval (and tells the other machine to leave them alone meanwhile).
  local brake=0
  if over_brake "${#confirmed[@]}" "$(wc -l < "$MANIFEST" 2>/dev/null || echo 0)"; then
    brake=1
    log "${#confirmed[@]} deletion(s) at once: holding them for approval instead of replicating."
    confirmed=()
    NEED_RECONCILE=1
  fi

  local setf rmf
  setf=$(mktemp); rmf=$(mktemp)
  printf '%s\n' "${remote_done[@]}" "${resurrected[@]}" > "$rmf"

  if remote_reachable; then
    if reconcile_due; then
      # Back after an outage (or due): the full reconcile replays what happened meanwhile.
      if reconcile; then
        rm -f "$setf" "$rmf"
        return 0
      fi
      err "Reconcile failed; pushing this batch and retrying later."
    fi
    # 3) Push content first (no --delete). A file the remote already has but we
    #    just modified gets updated; new files get created.
    if ! rsync_to_remote "${CHANGED_PATHS[@]}"; then
      err "Sync failed after change."
      NEED_RECONCILE=1
      rm -f "$setf" "$rmf"
      return 0
    fi
    msg="Synced after change"
    (( ${#PUSHED[@]} > 0 )) && msg+=": pushed ${#PUSHED[@]} ($(summarize "${PUSHED[@]}"))"
    # 4) Then apply the per-path deletions we actually observed locally.
    if (( ${#confirmed[@]} > 0 )); then
      if remote_delete_paths "${confirmed[@]}" 2>/dev/null; then
        printf '%s\n' "${confirmed[@]}" >> "$rmf"
        log "$msg; moved to trash on remote: $(summarize "${confirmed[@]}")."
      else
        err "$msg, but remote deletion failed for: $(summarize "${confirmed[@]}") (retried at the next reconcile)."
        NEED_RECONCILE=1
      fi
    else
      log "$msg."
    fi
    # 5) The manifest follows: what changed here now exists on both sides.
    printf '%s\n' "${CHANGED_PATHS[@]}" > "$setf"
  else
    # Deletes stay in the manifest, so the reconcile after the outage applies them.
    log "Remote offline, skipping sync; reconciling when it's back."
    NEED_RECONCILE=1
  fi
  manifest_apply "$setf" "$rmf"
  rm -f "$setf" "$rmf"
}

set +e
inotifywait \
  --monitor \
  --recursive \
  --format '%T %e %w%f' \
  --timefmt '%s' \
  --event close_write,moved_to,moved_from,delete,create \
  --exclude '(/\.git/|/\.sync-trash(/|$)|\.sync-conflict-|\.syncthing\.|\.stfolder|\.tmp$)' \
  "$LOCAL_DIR" 2> >(watch_errors) | \
while true; do
  if IFS= read -r -t "$TICK_SECONDS" line; then
    handle_batch "$line"
  elif (( $? > 128 )); then
    # Idle tick: reconcile if one is due (periodic, after an outage, or a decision waiting).
    prune_tombstones
    if reconcile_due; then
      if remote_reachable; then reconcile || err "Periodic reconcile failed."; else NEED_RECONCILE=1; fi
    fi
  else
    break   # inotifywait exited
  fi
done
rc=$?
set -e

# inotifywait only exits on a fatal error (typically: out of inotify watches at
# startup). Pause, then fail so systemd restarts us.
err "inotifywait stopped (exit $rc); restarting in ${WATCH_FAIL_DELAY}s."
sleep "$WATCH_FAIL_DELAY"
exit 1
