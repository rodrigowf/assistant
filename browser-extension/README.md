# Archie Browser Control (Chrome Extension)

A Manifest V3 extension that lets Archie drive **your regular, logged-in Chrome
profile** — see the page, navigate, click, fill, scroll, and run arbitrary
JavaScript — instead of launching a separate automated browser. Because it runs
as an ordinary extension inside your normal profile, pages see a real browser
with real sessions and cookies; there is no CDP/headless automation surface for
sites to trip over.

> **Naming note:** this is *not* the `chrome_extension` flag in
> `assistant_config.json`. That flag passes `--chrome` to the bundled Claude Code
> CLI (Anthropic's own Claude-in-Chrome integration) and is unrelated.

Design docs: [SPEC.md](SPEC.md) (requirements + decisions) ·
[PLAN.md](PLAN.md) (execution plan).

## Status

All eight phases implemented. Verified by 55 Python tests and 99 in-browser
assertions. Two steps need you and cannot be automated: loading the unpacked
extension, and the "Allow User Scripts" toggle.

Agents reach it through the **`/browser-control` skill**, which wraps
`context/scripts/browser_cmd.py` → `POST /api/browser/command`. It is
deliberately *not* an orchestrator tool: Claude Code sessions are the intended
consumer, and the orchestrator can fire the same script via `run_script`.

| Command | CLI | State |
|---|---|---|
| `snapshot` + `capture_screenshot` | `look` | Working |
| `navigate` | `navigate <url>` | Working |
| `list_tabs` | `tabs` | Working |
| `get_active_tab` / `switch_tab` | `switch <id>` | Working |
| `click` | `click` | Working |
| `fill` | `fill` | Working |
| `scroll` | `scroll` | Working |
| `execute_js` | `js <code>` | Working (MAIN world, verified under strict CSP) |

## Layout

```
browser-extension/
├── manifest.json                    # MV3 manifest
├── icons/                           # placeholder icons (generated)
├── test-fixtures/                   # in-browser tests — see its README
└── src/
    ├── background/
    │   ├── service-worker.js        # entry point; wakes and wires everything
    │   ├── connection.js            # WebSocket client, auth, reconnect, keepalive
    │   └── commands.js              # command registry, screenshot, JS injection
    ├── content/
    │   ├── snapshot-core.js         # all DOM logic; zero chrome.* dependencies
    │   └── content-script.js        # thin chrome.runtime messaging shim
    └── popup/                       # status UI, backend URL, token
```

No build step — plain JS, loaded unpacked.

## Installing

1. Open `chrome://extensions`
2. Enable **Developer mode** (top right)
3. **Load unpacked** → select this `browser-extension/` directory
4. Click the extension icon and set:
   - **Backend WebSocket** — default `ws://127.0.0.1:8765/api/browser/ws`; use
     `ws://192.168.0.200:8765/...` when the backend runs on the Jetson
   - **Shared token** — the `BROWSER_CONTROL_TOKEN` value from `context/.env`
5. For `execute_js` only: open `chrome://extensions/?id=<id>` and enable
   **Allow User Scripts**

The popup reports the handshake result. "Token rejected" means the value
doesn't match `context/.env`; "Backend has no BROWSER_CONTROL_TOKEN configured"
means the backend side is unset.

### Step 5 is not optional for JS injection

Since Chrome 138 the global developer-mode toggle was replaced by a
per-extension **Allow User Scripts** toggle, and newly installed extensions
default it to **off**. While off, `chrome.userScripts` reads as `undefined`
rather than throwing — so `execute_js` feature-detects it and returns an error
naming the toggle, instead of failing in a way that looks like a bug.

## Architecture

```
Archie backend  ──WebSocket──>  service worker  ──chrome.tabs──>  tab
                                      │
                                      ├──sendMessage──> content script ──> DOM
                                      └──chrome.userScripts──> page main world
```

The service worker holds the connection and owns everything needing extension
APIs. DOM work is delegated to the content script, which runs in an isolated
world — it shares the DOM with the page but not its JS context. `execute_js`
goes to the page's **main** world, where page globals live.

**Wire protocol** (JSON text frames, both directions):

```jsonc
// extension → backend, on connect
{ "type": "hello", "token": "…", "client": "chrome-extension", "version": "0.1.0" }
{ "type": "ready" }   // ← backend, handshake accepted

// backend → extension
{ "id": "abc123", "type": "command", "command": "navigate", "params": { "url": "…" } }

// extension → backend
{ "id": "abc123", "type": "result", "ok": true,  "result": { … } }
{ "id": "abc123", "type": "result", "ok": false, "error": "no_match: #nope" }
```

One `result` frame per request `id`, so the backend awaits a specific call
rather than the next reply.

### Active tab only

No command takes a `tabId`; `switch_tab` is how you reach another tab. Beyond
simplifying concurrency, `chrome.tabs.captureVisibleTab` can only ever capture
the active tab of the focused window — so the constraint matches Chrome.

### Coordinates

All geometry is **proportions of the screenshot**, `[0,1]`, origin top-left —
both snapshot bounding boxes and position targets. The screenshot and
`elementFromPoint` share a viewport, so `devicePixelRatio` and zoom cancel out.

Proportions are viewport-relative, so they're only valid at the scroll position
where the screenshot was taken. Scrolling bumps a generation counter, and a
later position target fails with `stale_viewport` rather than clicking blind.
`ref`/`selector` targeting is unaffected — it re-resolves through the DOM.

### MV3 service worker lifetime

Chrome kills idle service workers after ~30s. WebSocket activity resets the
idle timer (Chrome 116+), a `chrome.alarms` heartbeat wakes it if the socket
goes quiet, and every wake path calls `connect()` again. Disconnects are
normal; reconnects use exponential backoff with jitter (1s → 30s).

## Permissions

| Permission | Why |
|---|---|
| `tabs` | read tab URLs/titles, navigate, switch |
| `scripting` | inject the content script into tabs already open at load time |
| `storage` | persist the backend URL and token |
| `alarms` | service worker keepalive heartbeat |
| `userScripts` | run arbitrary JS strings (MV3's only sanctioned route) |
| `<all_urls>` | automation targets aren't known ahead of time |

`<all_urls>` is broad by design, and this extension has full access to every
logged-in session in the profile, with no restrictions on what JS the agent may
run. The backend WebSocket therefore requires a shared token and **fails
closed**: with no `BROWSER_CONTROL_TOKEN` configured, nothing connects. Keep
the backend bound to localhost or the trusted LAN.

## Testing

- `context/scripts/run.sh -m pytest tests/test_api_browser.py -v`
- In-browser fixtures: see [test-fixtures/README.md](test-fixtures/README.md)
