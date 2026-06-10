#!/usr/bin/env bash
# One-shot migration for the wake-word rename (Detour 3).
# wakeword_subsystem_refactor_plan_2026_06_09.md §0.5
#
# Swap-rename:
#   OLD "wake_word"  (turn-based)  -> NEW "turn_talk_word"     (talkWord)
#   OLD "voice_word" (realtime)    -> NEW "realtime_wake_word" (wakeWord)
#
# No inline legacy migration in the app (user preference). This script
# clears app data; re-enter the phrases via the new UI labels after install.
#
# Usage:  ./migrate_wake_word_rename.sh <adb-serial>

set -euo pipefail

DEVICE="${1:-}"
PKG="com.assistant.peripheral"

if [[ -z "$DEVICE" ]]; then
    echo "Usage: $0 <adb-serial>"
    adb devices -l | tail -n +2
    exit 1
fi
if ! adb -s "$DEVICE" shell echo ok > /dev/null 2>&1; then
    echo "ERROR: $DEVICE not reachable."
    exit 1
fi
if ! adb -s "$DEVICE" shell pm list packages | tr -d '\r' | grep -q "^package:$PKG$"; then
    echo "ERROR: $PKG not installed on $DEVICE."
    exit 1
fi

cat <<EOF
[$DEVICE] === Wake-word rename migration ===

  +---------------------------------------------------------------+
  | After installing the new APK, re-enter your phrases in the    |
  | Settings UI under the swapped labels:                         |
  |                                                               |
  |   "Single voice message / turn based" <- what was wake_word
  |   "Realtime voice conversation"       <- what was voice_word
  |                                                               |
  | The umbrella toggle and the sensitivity slider keep their     |
  | meaning.                                                      |
  +---------------------------------------------------------------+

EOF

echo "[$DEVICE] Force-stopping $PKG..."
adb -s "$DEVICE" shell am force-stop "$PKG"
sleep 1

read -r -p "[$DEVICE] Proceed with pm clear $PKG? (wipes prefs + DataStore) [y/N] " confirm
if [[ "${confirm,,}" != "y" ]]; then
    echo "Aborted. App data unchanged."
    exit 0
fi
adb -s "$DEVICE" shell "pm clear $PKG"

cat <<EOF

[$DEVICE] Migration complete.

Next:
  cd $(cd "$(dirname "$0")" && pwd)
  adb -s $DEVICE install -r app/build/outputs/apk/debug/app-debug.apk
  adb -s $DEVICE shell am start -n $PKG/.MainActivity
  # In Settings, re-enter the two phrases under the new labels.
EOF
