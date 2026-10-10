---
name: browser-extension
category: archie/clients
tags: [chrome, extension, manifest-v3, browser-control, userscripts, snapshot, daemon, automation]
created: 2026-08-27
modified: 2026-10-09
summary: apps/browser-extension — Chrome MV3 extension + local daemon that let agent sessions drive the user's real, logged-in Chrome.
source: curated (consolidated from memory notes assistant/devices/browser_control_extension.md, feedback/browser_control_x_snapshot_timeout.md; verified against code 2026-10-06)
references:
  - ../architecture/backend.md
  - ../architecture/orchestrator.md
  - ../integrations/skills.md
  - ../infrastructure/topology.md
  - ../operations/working-rules.md
  - ../devices/devices.md
---

# Browser control extension (`apps/browser-extension/`)

A Manifest V3 Chrome extension that lets Archie drive the user's **normal, logged-in Chrome
profile** — look at a page, navigate, open/switch tabs, click, fill forms, scroll, run arbitrary
JavaScript — instead of launching a separate automated browser. Pages see an ordinary browser
with real sessions and cookies; there is no CDP/headless surface. Because the existing browser
session *is* the authentication, this is the fallback for any site without a usable API.

Agents use it through the **`/browser-control` skill** (`shared/skills/browser-control/SKILL.md`,
reached as `context/skills/browser-control/SKILL.md`), which wraps one CLI script.

In-repo design docs: `apps/browser-extension/SPEC.md` (requirements + decisions),
`apps/browser-extension/PLAN.md` (the build plan), `apps/browser-extension/README.md`.

> **Naming collision.** `assistant_config.json` has a `chrome_extension` flag. It is
> **unrelated**: it passes `--chrome` to the bundled Claude Code CLI for Anthropic's own
> Claude-in-Chrome integration (`backend/api/session_factory.py`). This doc is about Archie's own
> extension in `apps/browser-extension/`.

## Architecture

```
Claude Code session ──Bash──> shared/scripts/browser_cmd.py
      ──HTTP POST /api/browser/command (loopback + token)──> browser_daemon.py :8766
      ──WebSocket /api/browser/ws──> extension service worker
      ──chrome.tabs / content script / chrome.userScripts──> page DOM
```

| Piece | File | Role |
|---|---|---|
| Transport hub | `backend/api/routes/browser.py` `BrowserHub` | Single-client slot: one browser, one socket; a new connection **replaces** the old one (close code 4409) and fails its in-flight commands immediately. One `result` frame per request `id`. Hello timeout 10 s, default command timeout **30 s** |
| Local daemon | `shared/scripts/browser_daemon.py` | Minimal FastAPI app with only the browser routes and `/api/uploads`, bound to **127.0.0.1:8766** (`BROWSER_DAEMON_PORT`). PID `logs/browser-daemon.pid`, log `logs/browser-daemon.log` |
| CLI | `shared/scripts/browser_cmd.py` | Starts the daemon on demand (detached, idempotent), waits for Chrome to attach, sends commands, renders `look` output; delegates over SSH when Chrome is on another machine |
| Service worker | `src/background/service-worker.js`, `connection.js`, `commands.js` | Holds the WebSocket (auth, reconnect, keepalive), command registry, screenshots, JS injection |
| DOM logic | `src/content/snapshot-core.js` | Snapshot, element refs/selectors, click, fill, scroll. Uses **no** `chrome.*` API, so it loads into a plain page for testing |
| Content shim | `src/content/content-script.js` | Thin `chrome.runtime` messaging shim over `snapshot-core.js` (both in the manifest's `content_scripts`, one isolated world) |
| Popup | `src/popup/` | Backend URL, token, handshake status |

The same router is also mounted in the main backend (`backend/api/app.py`), but the deployed
path is the daemon on the Chrome host (below).

Wire protocol (JSON **text** frames — a browser delivers binary frames as `Blob`s):

```jsonc
{ "type": "hello", "token": "…", "client": "chrome-extension", "version": "0.1.0" }  // extension → hub
{ "type": "ready" }                                                                    // hub → extension
{ "id": "ab12", "type": "command", "command": "navigate", "params": { "url": "…" } }   // hub → extension
{ "id": "ab12", "type": "result", "ok": true, "result": { … } }                        // extension → hub
```

## Security model

The extension has `<all_urls>` and unrestricted JS in a logged-in profile — anything that can
reach the socket can act as the user on every site he is signed into. So:

- **Shared token, fail closed.** `BROWSER_CONTROL_TOKEN` (in the private `context/.env`) must be
  presented by the extension (`hello`) and by every command (`X-Browser-Token` header). With no
  token configured nothing connects (503), a wrong one gets 401 / "Token rejected".
- **Loopback only.** The daemon binds 127.0.0.1; `/api/browser/command` also refuses any request
  that arrived through the reverse proxy (`X-Forwarded-For` from a non-loopback address → 403).
  A session on another machine goes through SSH, never an open port.
- **Other web sites are refused.** The main backend and the daemon both run the browser-origin
  guard (`backend/api/guard.py`, [backend.md](../architecture/backend.md#auth-and-the-browser-origin-guard)):
  a page open in Chrome cannot open the socket or POST `/api/uploads`; the extension's
  `chrome-extension://` origin and `browser_cmd.py` (no `Origin`) pass.
- Writes on real accounts (submitting forms, sending messages, purchases, destructive clicks)
  need the user's explicit intent for that action. Reading is free.

## Deployment: the daemon lives on the Chrome host

The only co-location requirement is between Chrome and the daemon that accepts its persistent
WebSocket. In the reference deployment Chrome runs on the laptop, and the Claude sessions that
drive it also run there (the Jetson spawns sessions on the laptop over SSH), so a command is a
pure loopback call.

Sessions on a machine without Chrome (the Jetson, or the orchestrator's `run_script`) are
detected by **hostname** — `context/.env` is synced to both machines, so a plain flag would be
true on both — and `browser_cmd.py` re-runs itself over SSH on the browser host:
`BROWSER_HOST_NAME` (the Chrome host's hostname), `BROWSER_HOST_SSH` (`user@host`), optional
`BROWSER_HOST_PATH` (default `~/assistant`).

It is deliberately **not** an orchestrator tool: Claude Code sessions are the intended consumer
(an early build added orchestrator tools; they were removed). The orchestrator can still fire the
script through `run_script` for one-shot checks.

## Installing the extension

1. `chrome://extensions` → Developer mode → **Load unpacked** → `apps/browser-extension/`.
   Chrome loads unpacked extensions by path: after the 2026-10-05 move it had to be re-loaded
   once from the new folder. The manifest pins a public `key`, so the extension ID no longer
   depends on the path.
2. In the popup set the backend WebSocket to **`ws://127.0.0.1:8766/api/browser/ws`** (the
   daemon — not 8765, which always means the main backend) and the shared token.
3. For `js` only: open `chrome://extensions/?id=<id>` and turn on **Allow User Scripts**. Since
   Chrome 138 this per-extension toggle replaced the global developer-mode switch and defaults
   to off; while off, `chrome.userScripts` reads as `undefined`, so `execute_js` feature-detects
   it and names the toggle in its error. These two steps cannot be automated.

## Using it (look first)

```bash
context/scripts/run.sh context/scripts/browser_cmd.py look                    # screenshot + markdown + element refs
context/scripts/run.sh context/scripts/browser_cmd.py click --ref e12
context/scripts/run.sh context/scripts/browser_cmd.py fill --selector 'input[name="email"]' --value "a@b.com"
context/scripts/run.sh context/scripts/browser_cmd.py js "return document.title;"
```

| CLI | Extension command | Notes |
|---|---|---|
| `look` | `snapshot` + `capture_screenshot` | `--no-screenshot`, `--max-chars N` (default 40000), `--limit N`, `--raw` |
| `navigate <url>` / `newtab <url>` | `navigate` / `open_tab` | `navigate` replaces the active tab's page; `newtab` opens and activates a new tab (`--background` to not focus) |
| `tabs` / `switch <id>` | `list_tabs` / `switch_tab` | |
| `click`, `fill`, `scroll` | same | target by `--ref`, `--selector`, or `--x --y` |
| `js <code>` | `execute_js` | async function body, `return` a value; runs in the page's MAIN world |
| `status`, `daemon …` | — | daemon and attachment state |

Rules that follow from the design:

- **Always `look` first**, then target by the `ref`/`selector` it returned. Re-run `look` after
  anything that changes the page.
- **Active tab only.** No command takes a tab id; use `tabs` + `switch`. This matches Chrome:
  `chrome.tabs.captureVisibleTab` can only capture the active tab of the focused window. There is
  no close command, so reuse a tab with `navigate` instead of opening one per loop iteration.
- **Refs are monotonic across snapshots.** A ref from an older snapshot fails with
  `unknown_ref` instead of silently hitting whatever now occupies its slot.
- **Selectors are generated and verified unique** at snapshot time, preferring `#id` → stable
  attributes (`data-testid`, `name`, `aria-label`) → tag+class → `:nth-child` path; build-hashed
  class names are avoided. Elements inside a shadow root get `selector: null` (no selector can
  pierce it) and are reachable by `ref` only. Prefer `--ref` on app-like pages.
- **Coordinates are proportions of the screenshot** (`[0,1]`, origin top-left), so
  `devicePixelRatio` and zoom cancel out. They are valid only at the scroll position of the
  snapshot: scrolling bumps a generation counter and a later position target fails with
  `stale_viewport`. Position targeting is a fallback; pass `--generation` from the `look`.
- **Use `fill`, never `el.value = x` in `js`, for form fields.** React installs an instance-level
  `value` property that records the last value it saw; a plain assignment updates that record
  too, React concludes nothing changed, and component state stays stale. `fill` writes through
  the **prototype** setter (`HTMLInputElement.prototype` / `HTMLTextAreaElement.prototype`) and
  fires the events frameworks listen for. `test-fixtures/react-form.html` reproduces the tracker.
- **`js` is for enumeration, `look` for orientation.** Extracting a list, table or paginated set
  with `js` returns just the named fields as JSON instead of a page render per screenful, and
  because `js` never consults the ref map it is immune to staleness — pagination can be driven
  entirely inside `js`. Refresh with `look` before the next `--ref` target.
- **The `Screenshot:` line is a URL, not a path.** The extension POSTs the capture to
  `/api/uploads`, which writes it under `context/uploads/` and returns `/uploads/<name>`; read
  the file at `context/uploads/<name>` (not `context/public/`).

### JS injection

MV3 removed code-string execution from `chrome.scripting.executeScript`; the sanctioned route is
`chrome.userScripts.execute()` with a `code` string (Chrome 120+). MAIN-world injection is not
blocked by page CSP (verified on a strict-CSP site in Chrome 151), so `js` has DevTools-console
parity, page globals included; the `USER_SCRIPT`-world fallback remains wired but should be dead
code. Rejected alternatives: Chromium (it implements MV3 itself, and the MV2 escape hatch is gone
upstream; a rarer browser with an empty profile would defeat the purpose), `chrome.debugger`
(visible "being debugged" banner), stripping CSP with `declarativeNetRequest` (would remove XSS
protection from logged-in sessions).

### Service-worker lifetime

Chrome kills idle MV3 service workers after ~30 s. WebSocket traffic resets the idle timer
(Chrome 116+, the manifest's minimum), a `chrome.alarms` heartbeat wakes it when the socket goes
quiet, and every wake path calls `connect()`. Reconnects back off exponentially with jitter
(1 s → 30 s), and the backoff is reset only on the **`ready`** frame — auth happens after the
socket opens, so resetting on open made a wrong token reconnect ~1.3 times per second forever.

## Pitfalls

| Symptom | Cause / fix |
|---|---|
| `look` returns HTTP 504 on X/Twitter (with or without screenshot) | The SPA's DOM is too heavy to snapshot within the 30 s command timeout. Skip `look` there and extract with `js` (e.g. `document.querySelectorAll('div[role="article"]')`, `document.title` to verify the page). `look` works on lighter pages |
| `browser not connected` | Daemon up, Chrome not attached: Chrome closed, or the popup points at the wrong port. Run `status` |
| `daemon failed to start` | See `logs/browser-daemon.log` |
| `cannot script restricted page` | `chrome://` pages and the Web Store can't be scripted by any extension |
| `userScripts_unavailable` | The Allow User Scripts toggle is off; only the user can flip it |
| Popup says "websocket error" (not "Token rejected") | A transport/TLS failure. A browser's click-through certificate exception does **not** apply to a `wss://` connection from an extension service worker (there is no UI to prompt), so a private-CA endpoint fails silently — one reason the daemon is plain `ws://` on loopback |

**Design lesson:** the hub first lived in the Jetson backend ("the backend of record"), so a
laptop session's command crossed to the Jetson and came back down an SSH tunnel to a browser on
the machine it started from — two hops, a TLS failure, a tunnel unit to keep alive.
**Put the transport where the device is**, not where the "main" backend is.
The tunnel's systemd unit (`tools/archie-browser-tunnel.service`) was a leftover of that era and
was deleted on 2026-10-07.

## Testing

- Python: `context/scripts/run.sh -m pytest backend/tests/test_api_browser.py -v`.
- In-browser fixtures: `apps/browser-extension/test-fixtures/*.html` (article, heavy page,
  injection, React form, shadow DOM and scroll), each exposing `window.__runTests()`; serve the
  folder over HTTP and open them in real Chrome. jsdom is useless here — it does no layout, so
  `getBoundingClientRect`/`elementFromPoint` assertions would be vacuous.
- Live end-to-end against the running daemon:
  `context/scripts/run.sh apps/browser-extension/tools/live_check.py recon|full` (`recon` is
  read-only; `full` navigates to local fixtures and restores the URL). `full` expects the
  fixtures on `http://127.0.0.1:8899/test-fixtures/`:
  `python3 -m http.server 8899 --bind 127.0.0.1 -d apps/browser-extension`.
- Both reconnect-storm and stale-ref bugs above were found only by driving real Chrome; the
  fixture suite exercised single-snapshot flows. Test multi-snapshot and failed-auth paths live.

## History

- 2026-08-27/28: built in eight phases; browser control moved from orchestrator tools to Claude
  sessions (`461308d`), and the hub moved from the Jetson backend to a local daemon on the Chrome
  host.
- 2026-10-05: moved from the repo root `browser-extension/` to `apps/browser-extension/`; the
  manifest got a pinned `key` so the extension ID is stable.
