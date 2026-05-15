#!/usr/bin/env bash
# One-shot rewrite of "/home/rodrigo/Projects/assistant" → "/home/rodrigo/assistant"
# inside data files (JSONL sessions, runtime metadata, tool-result snapshots,
# old .bak files).  Run from the NEW project root after moving the folder.
#
# Safe to run multiple times: matches are exact and the new string contains no
# occurrence of the old one, so re-runs are no-ops.
set -euo pipefail

ROOT="${1:-/home/rodrigo/assistant}"
OLD="/home/rodrigo/Projects/assistant"
NEW="/home/rodrigo/assistant"

if [ ! -d "$ROOT/context" ]; then
    echo "Expected $ROOT/context/ to exist.  Pass the project root as the first arg." >&2
    exit 1
fi

cd "$ROOT"

declare -a PATTERNS=(
    'context/*.jsonl'
    'context/chats/*.jsonl'
    'context/chats/*.runtime.json'
    'context/*.jsonl.bak'
    'context/*.config.json'
    'context/*/tool-results/*.txt'
)

# Build a flat file list, skipping anything that doesn't actually contain the old path
mapfile -t TARGETS < <(
    for pat in "${PATTERNS[@]}"; do
        # shellcheck disable=SC2206
        files=( $pat )
        for f in "${files[@]}"; do
            [ -f "$f" ] || continue
            grep -q -F "$OLD" "$f" && printf '%s\n' "$f"
        done
    done
)

if [ "${#TARGETS[@]}" -eq 0 ]; then
    echo "No files contain '$OLD' — nothing to rewrite."
    exit 0
fi

echo "Rewriting $OLD → $NEW in ${#TARGETS[@]} file(s)..."
# In-place rewrite, no backup.  '|' as sed delimiter so we don't escape slashes.
sed -i "s|${OLD}|${NEW}|g" "${TARGETS[@]}"
echo "Done."
