#!/usr/bin/env bash
# Type-check the compat app.
#
# A plain `tsc && vite build` is not usable here: the shared frontend/src tree
# carries pre-existing errors under this project's older TS lib (Uint8Array not
# being generic), which would fail the build forever and teach everyone to skip
# it. So we type-check everything but only *fail* on errors in compat's own
# src/ — which is where the App-vs-shared-component prop drift that broke the
# iPad actually shows up.
out=$(npx tsc --noEmit -p tsconfig.json 2>&1)
own=$(printf '%s\n' "$out" | grep '^src/' || true)
if [ -n "$own" ]; then
  echo "$own"
  echo "compat type-check failed (errors in frontend-compat/src)" >&2
  exit 1
fi
exit 0
