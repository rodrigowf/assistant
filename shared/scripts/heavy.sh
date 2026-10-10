#!/bin/bash
# heavy.sh — run one heavy job (build, test suite, typecheck, Gradle) at a time on the laptop,
# memory-capped and niced, so parallel agents can't freeze or crash it (2026-10-07 crash, 2026-10-09).
#   - one global lock (/tmp/archie-locks/heavy.lock): jobs queue instead of piling up;
#   - waits until >= 3 GB is free (Gradle: >= 5 GB), up to 30 min, else exits 75 ("try later");
#   - each job runs in its own cgroup (systemd-run --scope, MemoryMax, no swap), so only it can die;
#   - Gradle runs the capped form (1 worker, 2 GB heap, in-process Kotlin) and ./gradlew --stop after.
# Usage: shared/scripts/heavy.sh <cmd...>                 (cap 2500M; MEM=3500M shared/scripts/heavy.sh ...)
#        shared/scripts/heavy.sh --gradle <android dir> <tasks...>
# See docs/operations/working-rules.md "Parallelism and resources".
set -u
LOCK=/tmp/archie-locks/heavy.lock
mkdir -p /tmp/archie-locks   # /tmp is wiped on reboot
MEM="${MEM:-2500M}"
wait_mem() {
  for i in $(seq 1 60); do
    avail=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
    [ "$avail" -ge 3000 ] && return 0
    echo "[heavy] only ${avail}MB available, waiting..." >&2; sleep 10
  done
  echo "[heavy] memory never freed up; aborting" >&2; return 1
}
if [ "${1:-}" = "--gradle" ]; then
  dir="$2"; shift 2
  exec flock "$LOCK" bash -c '
    cd "$0" || exit 1
    for i in $(seq 1 180); do avail=$(awk "/MemAvailable/{print int(\$2/1024)}" /proc/meminfo); [ "$avail" -ge 5000 ] && break; echo "[heavy] gradle needs 5000MB free, ${avail}MB available, waiting" >&2; sleep 10; done
    [ "$avail" -ge 5000 ] || { echo "[heavy] not enough RAM for Gradle right now; try again later" >&2; exit 75; }
    echo "[heavy] gradle start, ${avail}MB available" >&2
    systemd-run --user --scope -q -p MemoryMax=3500M -p MemorySwapMax=0 nice -n 15 \
      ./gradlew "$@" --max-workers=1 -Dorg.gradle.jvmargs=-Xmx2g -Pkotlin.compiler.execution.strategy=in-process
    rc=$?; ./gradlew --stop >/dev/null 2>&1; exit $rc' "$dir" "$@"
fi
exec flock "$LOCK" bash -c '
  for i in $(seq 1 180); do a=$(awk "/MemAvailable/{print int(\$2/1024)}" /proc/meminfo); [ "$a" -ge 3000 ] && break; echo "[heavy] ${a}MB available, waiting" >&2; sleep 10; done
  [ "$a" -ge 3000 ] || { echo "[heavy] not enough RAM right now; try again later" >&2; exit 75; }
  exec systemd-run --user --scope -q -p MemoryMax='"$MEM"' -p MemorySwapMax=0 nice -n 15 "$@"' _ "$@"
