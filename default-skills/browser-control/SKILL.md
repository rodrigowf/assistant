---
name: browser-control
description: Control Rodrigo's real, logged-in Chrome — look at a page, navigate, click, fill forms, scroll, and run JavaScript. Use when a task needs a real browser session (logged-in sites, web UIs, reading a page the agent cannot fetch, filling a form, checking something on screen).
---

# Browser Control

Drives Rodrigo's **actual Chrome profile**, with his real logins and cookies —
not a headless browser. Whatever you do here happens in the browser in front of
him.

All commands go through one script:

    context/scripts/run.sh context/scripts/browser_cmd.py <command> [options]

## The loop: look, then act

**Always start with `look`.** It returns a screenshot, the page as markdown,
and a list of elements with a `ref` and a CSS `selector` each. You cannot
target anything you have not looked at — refs and selectors only exist in its
output.

    context/scripts/run.sh context/scripts/browser_cmd.py look

Then target by `--ref` (preferred) or `--selector`:

    context/scripts/run.sh context/scripts/browser_cmd.py click --ref e12
    context/scripts/run.sh context/scripts/browser_cmd.py fill --selector 'input[name="email"]' --value "a@b.com"

**Re-run `look` after anything that changes the page** — navigating, clicking
something that re-renders, or scrolling. Refs from an older snapshot are
rejected with `unknown_ref` rather than silently acting on the wrong element.

Prefer `--ref` over `--selector` on app-like pages: many sites use
build-generated class names (Tailwind, CSS-in-JS) that change between deploys.
Elements inside a shadow root have no selector at all and print
`(shadow DOM — use ref)`.

## Commands

| Command | Purpose |
|---|---|
| `look` | Screenshot + markdown + element refs. **Do this first.** |
| `navigate <url>` | Open a URL in the active tab; waits for load |
| `tabs` | List open tabs and windows |
| `switch <tab_id>` | Make another tab active |
| `click` | Click, by `--ref` / `--selector` / `--x --y` |
| `fill` | Text, textarea, select, checkbox — `--value` or `--checked` |
| `scroll` | `--to top\|bottom`, `--pages N`, or `--ref`/`--selector` |
| `js <code>` | Run JavaScript in the page |
| `status` | Is the browser connected? |

Useful flags: `look --max-chars N` (markdown budget, default 40000 — a busy
page will truncate), `look --raw` (full JSON), `look --limit N` (element list
length), `look --no-screenshot`.

## Active tab only

Every command acts on the **active tab**. To work on another tab, `tabs` to
find its id, then `switch`, then `look`. There is no per-command tab argument.

## Running JavaScript

`js` takes an async function body — use `return` to get a value, and `await`
freely. It runs in the page's main world, so page globals and framework
internals are reachable, exactly like the DevTools console.

    context/scripts/run.sh context/scripts/browser_cmd.py js "return document.title;"
    context/scripts/run.sh context/scripts/browser_cmd.py js "return [...document.querySelectorAll('h2')].map(h => h.textContent);"

DOM elements come back as `{__element, tag, selector, text}` — the selector can
be fed straight back into `click` or `fill`.

Use it for what the dedicated commands don't cover. Prefer `click`/`fill` for
ordinary interaction: they handle scroll-into-view, visibility and enabled
checks, and the event sequences frameworks listen for.

## Position targeting is a fallback

`--x`/`--y` are proportions of the screenshot in `[0,1]`, and are only valid at
the scroll position where the snapshot was taken. Pass `--generation` from the
`look` output. If the page scrolled since, the command fails with
`stale_viewport` — re-run `look` instead of guessing.

## Where it runs

Chrome, the daemon that holds its WebSocket, and (normally) your session are
all on the same machine, so commands are a loopback call — nothing crosses the
network. The daemon starts automatically on first use; you never start it by
hand.

If your session happens to run on a *different* machine from Chrome, the script
detects that and re-runs itself over SSH on the browser host. Same command,
same output, just slower. Configured by `BROWSER_HOST_NAME` / `BROWSER_HOST_SSH`
in `context/.env`.

## When it doesn't work

- **`browser not connected`** — the daemon is up but Chrome isn't attached.
  Chrome may be closed, or the extension's popup may point at the wrong port
  (it should be `ws://127.0.0.1:8766/api/browser/ws`). Run `status` to see.
- **`daemon failed to start`** — check `logs/browser-daemon.log`.
- **`cannot script restricted page`** — `chrome://` pages and the Web Store
  can't be scripted by any extension. Navigate somewhere else first.
- **`unknown_ref`** — the snapshot moved on. Re-run `look`.
- **`userScripts_unavailable`** (js only) — the per-extension "Allow User
  Scripts" toggle is off; the error names the exact `chrome://extensions/?id=…`
  URL. Rodrigo has to flip it, you can't.

## Care

This is Rodrigo's real browser, logged into his real accounts. Read freely, but
treat anything that writes — submitting forms, sending messages, purchases,
destructive clicks — as needing his explicit intent for *that* action. If a
page turns out to be mid-way through something he was doing, say so rather than
clicking through it. Navigating the active tab replaces whatever he was looking
at, so note the original URL if you'll need to put it back.

Full design notes: `browser-extension/README.md`,
`context/memory/assistant/devices/browser_control_extension.md`.
