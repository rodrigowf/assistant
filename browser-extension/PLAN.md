# Archie Browser Control — Execution Plan (final)

Companion to [SPEC.md](SPEC.md).

> **Status: all 8 phases implemented and verified against real Chrome
> (2026-08-28).** Approved 2026-08-27.
>
> - 55 Python tests + 99 in-browser fixture assertions
> - **30/31 live checks** against Rodrigo's real logged-in Chrome
>   (`tools/live_check.py full`); the one non-pass is a correct refusal to
>   script a `chrome://` tab
> - **CSP spike resolved:** MAIN-world injection works under strict CSP
> - Live testing found two bugs the fixtures could not — reconnect storm and
>   cross-snapshot ref reuse (see Deviations §8)
>
> **Deployed.** The daemon runs on the machine Chrome is on (the laptop);
> sessions elsewhere delegate over SSH. See "Stage 2" below. Deviations are
> listed at the bottom.

Ordering principle: transport first, so every later phase is testable
end-to-end. Then cheap-to-verify tab commands, then capture, then interaction
(which depends on the snapshot's selectors and the coordinate space), then JS
injection, then expose it all to the agent.

---

## 0. Settled decisions

| Area | Decision |
|---|---|
| Reference scheme | Generated CSS selectors, verified unique at snapshot time, plus a short `ref` alias |
| Coordinate space | Proportions of captured screenshot dimensions, `[0.0, 1.0]`, origin top-left |
| Screenshot scope | Viewport-only (follows from the coordinate decision) |
| JS injection | `chrome.userScripts.execute()` with a `code` string |
| Execution world | `MAIN` by default for stealth and console parity; per-site fallback explored if blocked |
| Injection safety | **Reliability only.** No allowlists, denylists, confirmation gates, or per-origin policy. Rodrigo owns what the agent runs. |
| Tab targeting | Active tab only; switch tabs to act elsewhere |
| Browser | Chrome (151 installed). Chromium investigated and rejected — SPEC §3.2.1 |

### 0.1 The coordinate contract

Cross-cutting, so it's specified once here and every phase conforms.

Screenshot responses carry:

```jsonc
{
  "width": 2560, "height": 1440,        // screenshot pixels
  "viewportCssWidth": 1280, "viewportCssHeight": 720,
  "devicePixelRatio": 2,
  "scrollX": 0, "scrollY": 1840,
  "generation": 7
}
```

All geometry — snapshot bounding boxes and position targets alike — is
proportional to screenshot dimensions. Conversion on receipt:

```
cssX = px * viewportCssWidth
cssY = py * viewportCssHeight
```

Screenshot and `elementFromPoint` share the same viewport, so `devicePixelRatio`
and zoom cancel out. That is what makes proportions consistent across displays.

**Scroll coupling — the non-obvious part.** Proportions are viewport-relative,
so they are only meaningful at the scroll position where the screenshot was
taken. If the page scrolls between capture and click, the same proportion lands
somewhere else. Therefore:

- `scrollX`/`scrollY` are part of the snapshot generation token
- A **position-targeted** action whose scroll position has drifted fails with an
  explicit `stale_viewport` error naming the drift, rather than clicking blind
- Selector- and `ref`-targeted actions are unaffected — they re-resolve through
  the DOM

This is the single most likely source of silent wrong-element clicks, which is
why it's an error rather than a best-effort guess.

### 0.2 Remaining defaults (non-blocking)

Proceeding on these unless overridden:

| # | Item | Default |
|---|---|---|
| 7 | Iframes / shadow DOM | Main frame + open shadow DOM; iframes deferred |
| 8 | Snapshot size budget | `maxChars` configurable, default 40k, truncation marker |
| 9 | Click fidelity | Full `pointerdown`→`mousedown`→`mouseup`→`click`, `.click()` fallback |
| 10 | WS auth | Shared token in `context/.env`, sent in the `hello` frame |
| 11 | Image filtering | Images ≥32×32, absolute URLs, skip `data:` URIs over 1KB |

Note on #10: this is not a restriction on the agent (§0, injection safety). It
prevents anything *else* on the network from reaching the socket and driving a
browser that has no code restrictions and full access to every logged-in
session.

---

## Phase 1 — Backend transport + auth

**Why first:** the extension currently sits in a reconnect loop because
`/api/browser/ws` doesn't exist. Nothing is verifiable end-to-end until it does.

- `api/routes/browser.py` — WebSocket endpoint `/api/browser/ws`
  - Shared-token check on the `hello` frame; reject and close otherwise
  - Connection registry (single extension client; last-wins on reconnect)
  - `send_command(command, params, timeout)` → correlates the `result` frame by
    request `id`, raises on timeout
  - Expose connection state so the agent distinguishes "browser offline" from
    "command failed"
- Register the router in `api/app.py`
- Token in `context/.env`; documented in the README
- `tests/test_browser_route.py` — auth accept/reject, id correlation, timeout,
  disconnect mid-command

**Verify:** `ping` and `list_tabs` round-trip from a Python REPL through the
extension to Chrome and back.

**Size:** small–medium. Gates everything.

---

## Phase 2 — Browser control + active-tab refactor

- Rework `src/background/commands.js` for the active-tab constraint:
  - Remove the `tabId` parameter from all commands
  - Remove `navigate({newTab})`
  - `resolveTab()` collapses to "the active tab"
- New commands: `list_tabs` (extended to include windows), `get_active_tab`,
  `switch_tab`
- `switch_tab` sets `chrome.tabs.update({active:true})` and, across windows,
  `chrome.windows.update({focused:true})`; returns the settled active tab

**Verify:** open several tabs across two windows; list, switch, confirm the
reported active tab matches what's on screen.

**Size:** small. Pure extension APIs, no DOM.

---

## Phase 3 — Screenshot capture

- `capture_screenshot` via `chrome.tabs.captureVisibleTab`
- Retry with backoff on the rate limit
- Return the full coordinate envelope from §0.1 — dimensions, viewport CSS
  size, dpr, scroll offsets, generation
- Upload to `api/routes/uploads.py`, return a URL rather than inline base64
- JPEG quality configurable; PNG option

**Verify:** capture on a HiDPI display and confirm
`width == viewportCssWidth * devicePixelRatio`. This is where a coordinate
mistake surfaces first and cheapest.

**Size:** small.

---

## Phase 4 — Page snapshot (the centerpiece)

Largest and highest-risk phase. Built in `src/content/content-script.js` as
discrete, independently testable pieces:

1. **Selector generator** — `#id` → stable attribute (`name`, `data-testid`,
   `aria-label`) → tag+class → `:nth-child` path. Every emitted selector
   verified unique via `querySelectorAll(sel).length === 1` before it goes out.
2. **Visibility predicate** — `display:none`, `visibility:hidden`,
   `aria-hidden`, zero-size, offscreen
3. **Tree walk + markdown serializer** — headings, lists, links, tables,
   paragraphs; drops `script`/`style`/`noscript`; open shadow roots traversed
4. **Interactive annotation** — buttons, links, inputs, selects, textareas,
   `[role=button]`, `[tabindex]`, `contenteditable`; with state (`disabled`,
   `checked`, `expanded`, current value, selected option) and accessible names
5. **Images** — absolute URLs, alt text, per the §0.2 filter
6. **Bounding boxes** — proportional, per §0.1
7. **Ref map + generation token** — `ref` alias → element, with scroll offsets
   folded into the generation per §0.1
8. **Truncation** — `maxChars` budget with an explicit marker

- Fixture pages under `browser-extension/test-fixtures/`: a static article, a
  React form with hashed class names, a page with shadow DOM, a heavy page for
  the truncation path

**Verify:** compare against `chrome-devtools` MCP `take_snapshot` on the same
pages. Every emitted selector must resolve to exactly one element.

**Review gate:** show Rodrigo the actual snapshot output before Phases 5–6 build
on it. The format is expensive to change later.

**Size:** large.

---

## Phase 5 — Interaction: click, fill, scroll

Depends on Phase 4 for selectors and on §0.1 for coordinates.

- **Resolution layer** — selector | `ref` | proportional position, shared by all
  three commands; generation check with `stale_viewport` on scroll drift
- **`click`** — scroll into view, assert visible and enabled, full pointer event
  sequence, `.click()` fallback
- **`fill`** — set value, dispatch `input` + `change` (the React/Vue
  correctness requirement); handle `select`, checkbox/radio, `contenteditable`
- **`scroll`** — window and element; absolute, delta, by page, `scrollIntoView`.
  Bumps the generation, since it invalidates position targets.

**Verify:** fill a real React form and confirm component state actually updates
— not merely that the DOM value changed. That's the specific failure mode.

**Size:** medium.

---

## Phase 6 — JavaScript injection

Mechanism settled: `chrome.userScripts.execute()` with a `code` string, `MAIN`
world by default.

- Add the `"userScripts"` permission to `manifest.json` — **not currently
  present**
- ~~**Spike first:** determine whether MAIN-world *injection* is CSP-blocked~~
  **RESOLVED 2026-08-28 on Chrome 151: it is not.** `userScripts.execute()`
  with a `code` string ran in the MAIN world on `github.com` (strict CSP) →
  `world=MAIN, result='GitHub'`. The docs' CSP warning applies to `eval()`
  called from *within* injected code, not to the injection. Full DevTools
  parity everywhere; the `USER_SCRIPT` fallback is expected to be dead code.
- Feature-detect `chrome.userScripts` — it is **`undefined`, not throwing**,
  when the per-extension toggle is off (Chrome 138+). Fail with a message
  naming the toggle and linking `chrome://extensions/?id=<id>`, or this becomes
  an unexplainable bug later.
- `MAIN` default; `world` exposed as a per-call override; `USER_SCRIPT`
  fallback wired but only engaged deliberately, per §0
- Execution timeout; error capture including stack
- `await` / promise support
- Result serialization: structured-cloneable values pass through; DOM nodes
  convert to snapshot references rather than being dropped
- Output truncation
- No code restrictions of any kind (§0)
- `declarativeNetRequest` CSP-stripping stays **out of scope** — it removes XSS
  protection on logged-in sessions, so it should be a deliberate later choice,
  not a default

**Verify:** run against a CSP-strict site and a permissive one; confirm a CSP
failure is a clean, identifiable error rather than a hang.

**Manual prerequisite:** Rodrigo flips "Allow User Scripts" at
`chrome://extensions/?id=<id>` — off by default for new extensions, and I
cannot set it.

**Size:** medium. The remaining unknown is the CSP spike, not the mechanism.

---

## Phase 7 — Agent surface  *(revised: skill, not orchestrator tools)*

Originally specified as orchestrator tools. **Reverted** — the orchestrator was
the wrong consumer. Claude Code sessions drive the browser; the orchestrator
delegates to a session, or fires the same script via `run_script`.

- `POST /api/browser/command` — one command per request, token-gated and
  loopback-only
- `default-scripts/browser_cmd.py` — the CLI, symlinked into `context/scripts/`
- `default-skills/browser-control/` — teaches the look-then-act loop, the
  ref-over-selector preference, active-tab-only, and the failure modes
- `context/memory/ORCHESTRATOR_SCRIPTS.md` entry for `run_script`

### Delegation logic (approved 2026-08-27)

The skill teaches this loop explicitly, in priority order:

1. **Start every task with `look`** — screenshot *and* text snapshot. Neither
   alone is sufficient: the screenshot gives visual context, the snapshot gives
   actionable references.
2. **Prefer references.** `ref` or `selector` from the snapshot is the default
   targeting mechanism whenever the element appears there.
3. **Proportional coordinates are a fallback**, subject to `stale_viewport`.

Stated as a priority order because the failure mode is an agent that guesses
selectors it never saw, or reuses coordinates after the page scrolled.

---

## Phase 8 — Documentation

- Update `browser-extension/README.md` (status table, new commands, setup,
  the "Allow User Scripts" step)
- Memory file under `context/memory/assistant/devices/`, per reuse-before-
  creating; update that topic's `INDEX.md`
- Note the `chrome_extension` vs `browser-extension` distinction in `AGENTS.md`
  so a future session doesn't conflate them

---

## Verification I cannot do myself

- **Loading the unpacked extension** — needs Rodrigo at `chrome://extensions`
- **The "Allow User Scripts" toggle** (Phase 6) — a manual per-extension setting
- **Anything requiring the logged-in profile** — by definition

I can write and run the backend tests and fixture-page snapshot tests
unattended. Every phase ends where a single manual check confirms it.

---

## Out of scope (v1)

File input uploads · cross-origin iframes · full-page stitched screenshots ·
multi-tab concurrency (excluded by the active-tab constraint) · per-origin
permission gating · `declarativeNetRequest` CSP stripping · cookie/storage
manipulation

---

## Sequencing

```
Phase 1 (transport+auth) ──┬── Phase 2 (tabs/windows)
                           ├── Phase 3 (screenshot) ──┐
                           └── Phase 4 (snapshot) ────┼── Phase 5 (interaction)
                                                      └── Phase 6 (JS injection)
                                                            │
                                              Phase 7 (orchestrator tools)
                                                            │
                                                   Phase 8 (docs)
```

Phases 2 and 3 are order-independent. Phase 4 has a review gate before 5–6.

---

## Deviations from this plan (2026-08-28)

Recorded so the plan stays an honest account of what was built.

1. **Extension-side auth moved into Phase 1.** The plan put it later, but Phase
   1's stated verification is a round-trip *through the extension*, which is
   impossible without the token in the `hello` frame.

2. **`snapshot-core.js` split out of `content-script.js`.** Unplanned. The DOM
   logic was unreachable for testing while wrapped in a `chrome`-dependent
   IIFE. The core now touches no extension API and the shim is ~40 lines; both
   are listed in `content_scripts` and share one isolated world, so runtime
   behaviour is unchanged.

3. **Fixture tests run in real Chrome, not jsdom.** jsdom performs no layout,
   so `getBoundingClientRect` and `elementFromPoint` return meaningless values
   — every visibility, bounding-box and position assertion would be vacuous.
   Driven via the `chrome-devtools` MCP; see `test-fixtures/README.md`.

4. **`browser_look` merges snapshot + screenshot into one tool.** The approved
   delegation logic says to always capture both first; a single tool makes the
   prescribed behaviour the path of least resistance rather than a convention
   the agent has to remember. A screenshot failure degrades to
   `screenshot_error` instead of discarding the snapshot.

5. **The 40k snapshot budget truncates ordinary pages.** A 400-paragraph
   fixture produces ~100k chars, so truncation is the common path on
   content-heavy pages, not an edge case. Left at 40k (it is per-call
   overridable via `max_chars`), but worth revisiting once there is real usage.

6. **`execute_js` auto-falls back to `USER_SCRIPT` on CSP failure**, reporting
   which world ran, rather than only engaging the fallback deliberately.
   `world` forces a choice and `fallback: false` disables it. The two readings
   in §0 and Phase 6 conflicted; this satisfies both and stays transparent.

7. **Injected DOM nodes serialise to a CSS selector, not a snapshot ref.** The
   ref map lives in the isolated world and is unreachable from the page's main
   world, so a selector is the actionable reference actually available.

8. **Two fixes forced by live testing (2026-08-28), neither reachable from the
   fixture suite:**
   - *Reconnect storm* — the backoff counter reset on socket `open`, but auth
     happens after open, so a bad token never backed off (211 reconnects at
     ~1.3/s). Now resets only on the `ready` frame.
   - *Cross-snapshot ref reuse* — `eN` restarted per snapshot, so a ref from an
     earlier snapshot silently resolved to whatever element then occupied that
     slot: the `stale_viewport` hazard, minus the loud failure. The counter is
     now monotonic and a stale ref fails with `unknown_ref`.

   The fixtures only exercised single-snapshot flows, which is why both hid.

## Stage 2 — deployment (COMPLETE, revised 2026-08-28)

**The daemon runs on the machine Chrome is on.** That is the only co-location
requirement: the extension holds one persistent WebSocket, so something
long-lived must accept it, and the Claude sessions that drive the browser run
on that same machine (the Jetson spawns them on the laptop over SSH).

    Claude session (laptop) → 127.0.0.1:8766 → browser_daemon.py → Chrome

`browser_cmd.py` starts the daemon on demand — detached and idempotent, so a
session cannot forget to and concurrent callers cannot double-start it.

### What was wrong before

The hub originally lived in the Jetson backend because that is "the backend of
record". A laptop session's request therefore crossed to the Jetson and came
straight back down an SSH tunnel to reach a browser on the same machine it
started from — two network hops for two co-located processes, plus a TLS
problem, a tunnel, and a systemd unit to keep it alive. All of that was
downstream of putting the hub in the wrong place.

The tunnel (`archie-browser-tunnel.service`) and its cert workaround are gone.
Kept for the record, because it is a real trap: the Jetson's cert is signed by
a private "Home CA" the laptop doesn't trust, and while browsing
`https://server.local/` works via a click-through exception, **that exception
does not apply to a `wss://` connection from an extension service worker** —
there is no UI to prompt with, so it fails as a bare transport error with
nothing reaching the server. Diagnostic: a token failure says "Token rejected",
a TLS failure says "websocket error".

### Sessions on a machine without Chrome

`browser_cmd.py` re-runs itself over SSH on the browser host, so the same
command works identically from either machine and the daemon stays
loopback-only with no open port and nothing to keep alive. Configured in
`context/.env` — tested by *hostname*, because that file is synced between
machines and a plain flag would be true on both:

    BROWSER_HOST_NAME=rodrigo-laptop
    BROWSER_HOST_SSH=rodrigo@192.168.0.28
    BROWSER_HOST_PATH=~/assistant

This also restores the orchestrator's `run_script` path, which executes on the
Jetson and now delegates transparently.

### Port

8766, deliberately not 8765. That port means "the main assistant backend" on
both machines; reusing it is what let browser traffic loop through the Jetson
unnoticed.

The Jetson backend still carries the browser routes (harmless — nothing
connects to them there). Restarting it needs sudo:

    ssh -t rodrigo@192.168.0.200 'sudo systemctl restart agentic-backend'

---
