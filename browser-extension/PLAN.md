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
> **Not yet deployed to the Jetson**, which is the backend of record. See
> "Stage 2" below. Deviations are listed at the bottom.

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

## Phase 7 — Orchestrator tools

- `orchestrator/tools/browser.py` — one tool per command, registered in the
  `ToolRegistry` so both the Anthropic text path and the OpenAI voice path get
  them
- Graceful degradation when the extension is disconnected

### Delegation logic (approved 2026-08-27)

The tool descriptions and the delegation skill must teach this loop explicitly,
in this priority order:

1. **Start every task by capturing both** a screenshot *and* a text snapshot of
   the page. Neither alone is sufficient — the screenshot gives visual context,
   the snapshot gives actionable references.
2. **Prefer references for navigation and interaction.** CSS selectors or `ref`
   aliases from the snapshot map are the default targeting mechanism whenever
   the element appears in the snapshot.
3. **Proportional viewport coordinates are a fallback**, for position-targeted
   actions that references can't express — subject to the `stale_viewport`
   detection in §0.1.

Stated as a priority order because the failure mode is an agent that guesses
selectors it never saw, or reuses coordinates after the page has scrolled.

**Size:** small–medium.

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

## Stage 2 — deployment (COMPLETE, 2026-08-28)

Deployed as `8f7303a`. The Jetson (`server`, 192.168.0.200) runs the backend as
the **system** unit `agentic-backend.service` (uvicorn on 127.0.0.1:8765,
`Restart=on-failure`, `User=rodrigo`), fronted by a 443 listener.

Verified in production: route live through 443, `token_configured: true`, WSS
handshake accepted with a valid token and **refused** with an invalid one,
screenshot upload through 443, and 55 tests passing on aarch64.

### The connection route: SSH tunnel, not `wss://`

The obvious plan — point the extension at `wss://server.local/api/browser/ws` —
**does not work**, and the reason is worth remembering:

- The Jetson's cert is issued by a private "Home CA" that the laptop does not
  trust (`verify error:num=20: unable to get local issuer certificate`).
- Rodrigo can browse `https://server.local/` because he clicked through the
  interstitial once. **That exception applies only to page navigations.** A
  `wss://` connection from an extension service worker has no UI to prompt
  with, so it fails outright — surfacing as a bare transport error, with
  nothing at all reaching the server.

The working route is an SSH tunnel from the laptop:

    ssh -f -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
        -L 8765:127.0.0.1:8765 rodrigo@192.168.0.200

with the extension left on its default `ws://127.0.0.1:8765/api/browser/ws`.

This is also **strictly safer** than the `wss://` route, and retracts the
LAN-exposure warning previously recorded here: the Jetson's backend stays bound
to loopback, so nothing on the network can reach a socket that runs arbitrary
JS in a logged-in browser — only an authenticated SSH session from the laptop.

The tunnel is managed by a **systemd user service on the laptop**,
`~/.config/systemd/user/archie-browser-tunnel.service` (enabled, `Restart=always`,
`RestartSec=5`). Mirrors the existing `context-sync.service` pattern — the
project's established way of doing cross-machine SSH plumbing.

Why a user unit rather than logic inside the browser tools: **the tools run on
the Jetson, but the tunnel must terminate on the laptop** where Chrome is. A
tool that established it would be reaching across machines to mutate network
topology as a side effect of a tool call — hidden state, and failures
surfacing as confusing tool errors. A user unit also needs no sudo (unlike
`agentic-backend`, a system unit) and no `autossh` (not installed):
systemd's `Restart=always` plus SSH keepalives cover it. Its lifetime ties to
the desktop session, which is exactly when Chrome exists.

The forward is bound explicitly to `127.0.0.1:8765` on the laptop side, so the
port is not exposed on the laptop's LAN interfaces either — loopback-only on
both ends.

Verified: killing the tunnel drops the extension, and it reconnects on its own
backoff within ~5s of the tunnel returning (`systemctl --user restart
archie-browser-tunnel`).

Alternative, not taken: installing the Home CA into the system trust store *and*
Chrome's NSS db (`libnss3-tools`) would enable direct `wss://`, at the cost of
more moving parts and a worse security posture.

    systemctl --user status archie-browser-tunnel     # check
    systemctl --user disable --now archie-browser-tunnel   # undo

Note: restarting the service needs `sudo` on the Jetson (no passwordless sudo,
polkit refuses over SSH):

    ssh -t rodrigo@192.168.0.200 'sudo systemctl restart agentic-backend'

---

## Awaiting approval

Ready to start at Phase 1 on your go-ahead. Two things I'd surface at the
moment of approval rather than bury:

1. **Phase 4's output format is the one expensive decision left.** I'll stop and
   show it to you before Phases 5–6 build on it.
2. **Phase 6's CSP spike could change the stealth story.** If MAIN-world
   injection turns out to be CSP-blocked on strict sites, the choice becomes
   `USER_SCRIPT` (no page globals) or CSP-stripping (weakens the page). Worth
   knowing before it's load-bearing.
