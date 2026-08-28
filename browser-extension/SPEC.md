# Archie Browser Control — Specification (living draft)

Status: **requirements complete; nothing implemented.** The core requirement
set below is final as of 2026-08-27. Implementation decisions still marked
**OPEN** need Rodrigo's call; each has a recommended default in
[PLAN.md](PLAN.md) so work isn't blocked on them.

The premise: the agent observes a page (screenshot + structured text), then acts
on it (click, fill, scroll, or arbitrary JS). Snapshot-then-act, in a real
logged-in Chrome profile.

---

## 0. Core requirements (final)

**Interaction**
- Navigate to specific URLs
- Click elements by CSS selector or page position
- Fill forms (text input) by CSS selector or position
- Scroll pages
- Inject arbitrary JavaScript safely, similar to running code via DevTools

**Page capture**
- Capture screenshots
- Extract all text, functionally parsed (clean markdown), with references to
  element IDs/selectors so the agent can interact with those elements

**Browser control**
- List all open tabs and windows
- Identify the active tab
- Switch the active tab

**Constraint:** agent control is **restricted to the active tab**. To act on
another tab, switch to it first.

### What the active-tab constraint buys us

It removes a whole class of complexity, and it happens to align with a Chrome
limitation we were going to hit regardless:

- `chrome.tabs.captureVisibleTab` can *only* capture the active tab of the
  focused window. The constraint makes that a non-issue rather than a caveat.
- No `tabId` parameter on any command — every command implicitly targets the
  active tab. This **supersedes the current `commands.js`**, which accepts an
  optional `tabId` and has a `navigate({newTab})` option; both come out.
- No concurrent multi-tab automation, so no cross-tab race conditions.
- Snapshot refs (§1.2) are only ever valid for one tab at a time, which makes
  the staleness story much simpler.

---

## 1. Page observation

### 1.1 Screenshot

- `chrome.tabs.captureVisibleTab` captures the visible viewport of the active
  tab. Rate-limited; needs retry/backoff.
- Full-page requires scroll-and-stitch, or CDP `Page.captureScreenshot` via
  `chrome.debugger` (which shows the "being debugged" banner — see §3.2).
- Must record viewport dimensions and `devicePixelRatio` alongside the image,
  so screenshot pixels can be reconciled with the coordinate space (§2.1).
- **OPEN:** viewport-only, or is full-page needed?

### 1.2 Text snapshot (markdown + element references)

Clean markdown, no HTML junk, where every interactive or textual section
carries a reference the agent can act on. Covers buttons, menus, lists, form
controls. This is the centerpiece of the design.

**Prior art:** the `chrome-devtools` MCP tools in this environment do exactly
this — `take_snapshot` returns a tree of `uid`s, `click`/`fill` take a `uid`.
Playwright ARIA snapshots are the same pattern. Review both output formats
before designing ours.

#### Reference scheme — RESOLVED

The final requirements specify **CSS selectors** (IDs/classes) as the reference
mechanism, and click/fill accept selectors. Resolution:

- The snapshot emits, for each addressable element, a **generated CSS selector
  that is verified unique at snapshot time** (`document.querySelectorAll(sel).length === 1`)
- Preference order when building it: `#id` → stable attribute
  (`name`, `data-testid`, `aria-label`) → tag+class → structural `:nth-child`
  path
- A short **`ref` alias** (`e17`) is emitted alongside and maps to the same
  element. Cheap to carry, and it's the reliable path when a page's classes are
  build-generated hashes
- Both are accepted by every interaction command

**Residual risk, stated once:** selectors built from hashed class names
(Tailwind, CSS-in-JS) can break across deploys, and any selector can go stale
if the DOM mutates after the snapshot. Mitigation: a **snapshot generation
counter**, so acting on a stale reference fails loudly rather than hitting the
wrong element.

#### Content rules

- Exclude `script`, `style`, `noscript`, comments
- Exclude non-visible: `display:none`, `visibility:hidden`, `aria-hidden`,
  zero-size
- Include accessible names — `aria-label` for icon-only buttons, `alt` for
  images, `placeholder` for inputs
- Include interactive state: `disabled`, `checked`, `expanded`, selected
  option, current input value
- Interactive set: `button`, `a`, `input`, `select`, `textarea`,
  `[role=button]`, `[tabindex]`, `contenteditable`

#### Images

Absolute URLs + alt text, so the agent can view them. **OPEN:** skip tracking
pixels / data URIs? Auth-gated images may not be fetchable outside the browser
session.

#### Bounding boxes

Per-reference boxes as **proportions of the screenshot dimensions** (§2.1), so
the agent can correlate what it sees in the screenshot with something it can
act on. Same coordinate space as position targeting. Cheap now, hard to
retrofit.

#### Still open

- **Size budget.** Full-page markdown of a heavy app can exceed any context
  limit. Needs truncation strategy and probably a viewport-only mode.
- **Frames.** Manifest is `all_frames: false`, so iframe content is invisible.
  Cross-origin frames need per-frame snapshots stitched. Shadow DOM needs
  explicit traversal.

---

## 2. Interaction

All commands target the **active tab** (§0).

### 2.1 Targeting

1. **CSS selector** — including the generated selectors from §1.2
2. **`ref` alias** — resolves through the snapshot map
3. **Position** — proportional coordinates, resolved via `elementFromPoint`

#### Coordinate space — RESOLVED

All geometry is expressed as **proportions of the captured screenshot
dimensions**: floats in `[0.0, 1.0]`, origin top-left. This holds for position
targeting *and* for snapshot bounding boxes (§1.2), so both speak one language.

The extension converts on receipt:

```
cssX = px * viewportCssWidth
cssY = py * viewportCssHeight
element = document.elementFromPoint(cssX, cssY)
```

Because the screenshot and `elementFromPoint` share the same viewport,
`devicePixelRatio` and browser zoom cancel out — which is what makes this
consistent across displays.

**Consequence worth naming:** proportions are *viewport*-relative, so they are
only valid for the scroll position at capture time. If the page scrolls between
screenshot and click, the same proportion points somewhere else. Mitigation:
capture `scrollX`/`scrollY` into the snapshot generation token and reject (or
re-scroll for) a position action whose scroll position has drifted — see
[PLAN.md](PLAN.md) §0.1.

This also effectively settles §1.1 toward **viewport-only screenshots**: a
stitched full-page image would break the 1:1 screenshot↔viewport
correspondence the scheme depends on.

### 2.2 Navigate

Implemented. Waits for load completion. Loses its `newTab` option per §0.

### 2.3 Click

Scroll into view, verify visible and enabled, then click. **OPEN:** synthesize
a full `pointerdown`/`mousedown`/`mouseup`/`click` sequence, or a plain
`.click()`? The former is more faithful to real input.

### 2.4 Fill

Set value, then dispatch `input` + `change`. A raw `.value` assignment alone
will **not** update a React/Vue controlled component — the most common way
naive form-filling silently fails. Also needs `select` dropdowns,
checkboxes/radios, and `contenteditable`. File inputs: out of scope.

### 2.5 Scroll

Window and element scroll: absolute position, by delta, by page, or
`scrollIntoView` on a reference. Infinite-scroll pagination ("scroll until no
new content") is a likely follow-up. Snapshot should be re-taken after
scrolling.

---

## 3. Arbitrary JavaScript injection

Maximum flexibility — anything the dedicated commands don't cover. Must mimic
typing into the Chrome DevTools console, to minimize detection risk.

### 3.1 The MV3 constraint — RESOLVED via `chrome.userScripts`

`chrome.scripting.executeScript` accepts a *function* or *files*, never a
string, and the agent's code arrives as a string over the WebSocket. That looked
like a hard architectural obstacle. It isn't: **`chrome.userScripts.execute()`
accepts arbitrary JavaScript as a `code` string** and is the MV3-sanctioned path
for exactly this. Chrome 120+; the machine runs Chrome 151.

Verified against the [API reference](https://developer.chrome.com/docs/extensions/reference/api/userScripts)
on 2026-08-27.

**Requirements:**

- `"userScripts"` permission in the manifest (**not currently present** — Phase 6
  adds it), plus `host_permissions` for target sites
- Since Chrome 138 the global developer-mode toggle was replaced by a
  **per-extension "Allow User Scripts" toggle** at
  `chrome://extensions/?id=<id>`, defaulting to **off** for newly installed
  extensions. Manual step for Rodrigo.
- When the toggle is off in Chrome 138+, the API is **`undefined` rather than
  throwing** — code must feature-detect and fail with a clear message instead of
  a confusing crash.

### 3.2 Execution world — the remaining sub-decision

`execute()` takes a `world`, and the two options trade off against each other:

| | Arbitrary strings | Page CSP | Page globals (`window.jQuery`, React internals, app state) |
|---|---|---|---|
| `USER_SCRIPT` (default) | yes | **exempt** | **no** — isolated context |
| `MAIN` | yes | subject to it | **yes** — true DevTools console parity |

The DevTools console runs in the main world with full access to the page's own
JS, so `MAIN` is what "mimic the DevTools console" actually means. `USER_SCRIPT`
buys CSP immunity at the cost of the page internals that make console access
powerful.

**DECIDED:** `MAIN` is the default, for maximum stealth and true console
parity. Fallbacks to `USER_SCRIPT` are to be explored for specific sites where
the main world turns out to be blocked, and `world` is exposed as a per-call
override.

**RESOLVED (2026-08-28, measured on Chrome 151).** The docs' "MAIN-world
scripts are subject to the page's existing CSP" refers to `eval()` *called from
within* injected code — **not** to the injection itself. `userScripts.execute()`
with a `code` string runs fine in the MAIN world on a strict-CSP site:
`github.com` returned `world=MAIN, result='GitHub'`.

Consequence: **full DevTools parity everywhere**, page globals included, with
no CSP gap. The `USER_SCRIPT` fallback stays wired but is expected to be dead
code in practice, and the `declarativeNetRequest` CSP-stripping escape hatch
(§3.2 "last-resort") is not needed — which is a good outcome, since it would
have meant stripping XSS protection from logged-in sessions.

**Last-resort fallback:** `declarativeNetRequest` + `modifyHeaders` can strip
`Content-Security-Policy` response headers. It works, but it removes the page's
XSS protection on sites Rodrigo is logged into — a real cost on a banking
session. Opt-in per-origin, never blanket.

### 3.2.1 Rejected: `chrome.debugger`, and switching to Chromium

`chrome.debugger` + `Runtime.evaluate` is literally the console's own mechanism
and is CSP-exempt, but it raises the visible "browser is being debugged" banner
and CDP attachment is detectable — contradicting the reason for wanting DevTools
semantics in the first place. Rejected.

**Chromium offers no advantage** (investigated 2026-08-27). MV3 is implemented
in Chromium's own source; Chrome inherits it from upstream. Chrome 151 —
the installed version — is the release that deleted the last MV2 feature switch
*from Chromium*, including the command-line `AllowLegacyMV2Extensions` override,
so the "drop to MV2 for `executeScript({code})`" escape hatch is gone upstream
too. The enterprise `ExtensionManifestV2Availability` policy ended at Chrome 139.
Switching would also mean a rarer, more fingerprintable browser (`Sec-CH-UA`
brands as Chromium, no Widevine, missing proprietary codecs) and a fresh profile
— defeating the premise of driving the real logged-in browser.

### 3.3 "Safe and reliable" — RESOLVED: reliability only

**No built-in restrictions on what code the agent may run.** Rodrigo takes
responsibility for guiding the agent. "Safe" means *reliable execution*, and
nothing else:

- execution timeout
- error capture with stack traces
- `await` / promise support
- result serialization (DOM nodes can't cross the boundary — return snapshot
  references instead)
- truncation of large results

No allowlist, no denylist, no confirmation gates, no per-origin policy.

Note that the shared-token auth on the WebSocket (§5.2) is **not** a restriction
on the agent — it prevents anything *else* on the network from reaching the
socket and driving the browser. It stays.

---

## 4. Browser control

- **List tabs and windows** — `chrome.tabs.query({})` + `chrome.windows.getAll()`.
  Return per tab: tabId, windowId, title, URL, active, index; per window: id,
  focused, state, type. Neither needs a permission beyond `tabs` (which is what
  grants access to `url`/`title`).
- **Identify active tab** — `chrome.tabs.query({active:true, lastFocusedWindow:true})`
- **Switch active tab** — `chrome.tabs.update(tabId, {active:true})` plus
  `chrome.windows.update(windowId, {focused:true})` when crossing windows.
  Should return the new active tab and wait for it to settle, since every
  subsequent command depends on the switch having taken effect.

---

## 5. Cross-cutting

### 5.1 Transport

Existing protocol (see [README.md](README.md)) is one `result` frame per
command id. Additions:

- Screenshots as base64 over the WebSocket get bulky; `api/routes/uploads.py`
  exists and may be the better path, with the response carrying a URL
- Snapshots need a size cap (§1.2)

### 5.2 Security

Arbitrary JS execution in a logged-in profile means **anything that can reach
the backend WebSocket can run arbitrary code in every session Rodrigo is signed
into** — bank, email, everything. Combined with `<all_urls>`, this is the
highest-value target in the system.

- `/api/browser/ws` does not exist yet; it should require a shared token from
  the first commit, not as a follow-up
- Backend must stay bound to localhost or the trusted LAN
- **OPEN:** origin allowlist/denylist? Confirmation gate on sensitive origins?

---

## 6. Open questions

| # | Question | Section |
|---|---|---|
| # | Question | Status | Section |
|---|---|---|---|
| 1 | JS injection mechanism | **Resolved** — `chrome.userScripts` | §3.1 |
| 2 | What "safe" means for injection | **Resolved** — reliability only, no restrictions | §3.3 |
| 3 | Coordinate space | **Resolved** — proportions of screenshot dimensions | §2.1 |
| 4 | Screenshot scope | **Resolved** — viewport-only (implied by #3) | §1.1 |
| 5 | Execution world | **Resolved** — `MAIN` default, fallback explored per-site | §3.2 |
| 6 | Reference scheme | **Resolved** — unique CSS selectors + `ref` alias | §1.2 |
| 7 | Iframe / shadow DOM support | Open — default: shadow DOM in v1, iframes deferred | §1.2 |
| 8 | Snapshot size budget | Open — default: 40k chars, configurable | §1.2 |
| 9 | Click event fidelity | Open — default: full pointer sequence | §2.3 |
| 10 | Backend WebSocket auth | Open — default: shared token in `context/.env` | §5.2 |
| 11 | Image URL filtering | Open — default: ≥32×32, skip large `data:` URIs | §1.2 |

Items 7–11 have recommended defaults in [PLAN.md](PLAN.md) §0 and are not
blocking; implementation proceeds on those defaults unless overridden.

Recommended defaults for all nine are in [PLAN.md](PLAN.md) §0.
