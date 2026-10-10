#!/usr/bin/env bash
# run-suite.sh — scenario tests for context-sync.sh, no second machine needed.
#
# Runs two instances of ../context-sync.sh against each other on two temp folders (A, B). A fake
# `ssh` runs the "remote" command locally with the other side's HOME, and fails while the file
# $T/offline exists, so outages are simulated by touching it. Logs go to the journal under the tag
# context-sync-test (never the real context-sync tag). Takes ~5 minutes; light on CPU and RAM.
#
# Usage: infra/sync/test/run-suite.sh [path/to/context-sync.sh]
# Exit status: number of failed checks.
set -u
S="$(cd "$(dirname "$0")/.." && pwd)/context-sync.sh"; S="${1:-$S}"
T=$(mktemp -d /tmp/context-sync-test.XXXXXX)
mkdir -p "$T/bin" "$T/A" "$T/B" "$T/homeA" "$T/homeB"
cat > "$T/bin/ssh" <<EOF
#!/usr/bin/env bash
[[ -e $T/offline ]] && { echo "ssh: connect: offline" >&2; exit 255; }
while [[ \$# -gt 0 ]]; do case "\$1" in -o|-i|-p|-l|-F) shift 2 ;; -*) shift ;; *) break ;; esac; done
shift
export HOME="\$FAKE_REMOTE_HOME"; cd "\$HOME"
exec bash -c "\$*"
EOF
chmod +x "$T/bin/ssh"
for s in A B; do o=$([ $s = A ] && echo B || echo A); cat > "$T/$s.env" <<EOF
LOCAL_DIR=$T/$s
REMOTE_HOST=fake$o
REMOTE_USER=me
REMOTE_DIR=$T/$o
SSH_KEY=/dev/null
DEBOUNCE_SECONDS=1
RETRY_INTERVAL=2
RECONCILE_SECONDS=8
TICK_SECONDS=2
MAX_DELETES=5
WATCH_FAIL_DELAY=3
STATE_DIR=$T/home$s/.local/state/context-sync
REMOTE_SCRIPT=$S
LOG_TAG=context-sync-test
EOF
done
start() {  # start one instance in its own process group
  local s=$1 o; o=$([ "$1" = A ] && echo B || echo A)
  PATH="$T/bin:$PATH" HOME="$T/home$s" FAKE_REMOTE_HOME="$T/home$o" setsid bash "$S" "$T/$s.env" > "$T/$s.out" 2>&1 &
  echo $! > "$T/$s.pid"
}
stop() { kill -- -"$(cat "$T/$1.pid")" 2>/dev/null; sleep 1; }
cleanup() { stop A; stop B; rm -rf "${T:?}"; }
trap cleanup EXIT
cli() { HOME="$T/homeA" bash "$S" "$1" "$T/A.env" >/dev/null; }
st() { [ -e "$1" ] && echo present || echo absent; }
cnt() { find "$1" -type f -not -path '*/.sync-trash/*' 2>/dev/null | wc -l; }
held() { [ -s "$T/home$1/.local/state/context-sync/pending-deletes" ] && echo yes || echo no; }
fails=0
check() { if [ "$2" = "$3" ]; then echo "PASS  $1"; else echo "FAIL  $1 (got $2, want $3)"; fails=$((fails + 1)); fi; }
cd "$T" || exit 1
D=$(date +%F); START=$(date +%T)

for i in 1 2 3 4; do echo "file $i" > A/f$i.md; done
mkdir -p A/bulk A/bulk2; for i in $(seq 1 8); do echo b$i > A/bulk/b$i.txt; echo c$i > A/bulk2/c$i.txt; done
start A; start B; sleep 6
check "S0 initial copy" "$(cnt B)" 20
echo live > A/live.md; sleep 4; r=$(st B/live.md); rm A/live.md; sleep 4
check "S1 live create + delete (to the other side's trash)" "$r/$(st B/live.md)/$(st B/.sync-trash/$D/live.md)" present/absent/present
touch offline; rm A/f1.md; echo new > A/g1.md; sleep 4; rm offline; sleep 14
check "S2 delete + create while the other side is offline" "$(st B/f1.md)/$(st B/.sync-trash/$D/f1.md)/$(st B/g1.md)" absent/present/present
stop A; rm A/f2.md; start A; sleep 7
check "S3 delete while this side's service is stopped" "$(st B/f2.md)/$(st B/.sync-trash/$D/f2.md)" absent/present
stop A; rm A/f4.md; echo busy > B/busy1.md; sleep 3; echo busy2 > B/busy2.md; sleep 3; start A; sleep 8
check "S3b same, while the other side keeps writing" "$(st A/f4.md)/$(st B/f4.md)/$(st B/.sync-trash/$D/f4.md)/$(st A/busy2.md)" absent/absent/present/present
touch offline; rm A/f3.md; sleep 1.5; echo "edited on B" >> B/f3.md; sleep 4; rm offline; sleep 14
check "S4 edit beats delete" "$(st A/f3.md)/$(tail -1 A/f3.md 2>/dev/null | tr ' ' _)" present/edited_on_B
touch offline; rm -r A/bulk; sleep 3; rm offline; sleep 14; h="$(cnt A/bulk)/$(cnt B/bulk)"; cli --approve-deletes; sleep 8
check "S5 brake holds, approve applies" "$h -> $(cnt A/bulk)/$(cnt B/bulk)/trash$(find B/.sync-trash/$D/bulk -type f 2>/dev/null | wc -l)" "0/8 -> 0/0/trash8"
touch offline; rm -r A/bulk2; sleep 3; rm offline; sleep 14; h="$(cnt A/bulk2)/$(cnt B/bulk2)"; cli --reject-deletes; sleep 10
check "S6 brake holds, reject restores" "$h -> $(cnt A/bulk2)/$(cnt B/bulk2)" "0/8 -> 8/8"
sleep 8; check "S7 nothing left held on either side" "$(held A)/$(held B)" no/no
sleep 10; check "S8 no log lines while idle" "$(journalctl -t context-sync-test --since '-10s' --no-pager -o cat | wc -l)" 0
check "S9 trees identical" "$(diff -rq --exclude=.sync-trash A B >/dev/null && echo same || echo differ)" same
nB=$(cnt B); stop A; find A -mindepth 1 -maxdepth 1 -not -name .sync-trash -exec rm -rf {} +; start A; sleep 8; echo other > B/during-hold.md; sleep 6
check "S10 context wiped while stopped -> held, other side intact" "$(cnt A)/$(cnt B)" "1/$((nB + 1))"
cli --reject-deletes; sleep 10; check "S10 reject restores everything" "$(cnt A)" "$(cnt B)"
mkdir A/bulk3; for i in $(seq 1 8); do echo d$i > A/bulk3/d$i.txt; done; sleep 5; rm -r A/bulk3; sleep 5; echo x > B/poke.md; sleep 6
check "S11 live mass delete held, not undone by the other side" "$(cnt A/bulk3)/$(cnt B/bulk3)" 0/8
cli --approve-deletes; sleep 8; check "S11 approve applies" "$(cnt A/bulk3)/$(cnt B/bulk3)" 0/0
sleep 10; check "S12 identical, nothing held" "$(diff -rq --exclude=.sync-trash A B >/dev/null && echo same || echo differ)/$(held A)" same/no
mkdir -p "$T/outside/movedin/sub"; for i in 1 2 3; do echo m$i > "$T/outside/movedin/sub/m$i.md"; done; mv "$T/outside/movedin" A/; sleep 5
check "S13 a folder moved in arrives whole" "$(cnt B/movedin)" 3
errs=$(journalctl -t context-sync-test --since "$START" --no-pager -o cat | grep ERROR | grep -cv holding)
check "no unexpected errors in the log" "$errs" 0
echo "$fails failure(s)"
exit "$fails"
