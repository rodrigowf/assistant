# Fixture tests

Browser-side tests for `src/content/snapshot-core.js` and the `execute_js`
wrapper. Each page exposes `window.__runTests()`, returning
`{passed, failed, failures}`.

## Why real Chrome and not jsdom

jsdom performs **no layout**. `getBoundingClientRect()` returns zeros and
`elementFromPoint()` doesn't work, so every visibility check, bounding box and
position-targeting assertion would either pass vacuously or fail for reasons
unrelated to the code. These tests are only meaningful in a real engine.

## Running

Serve the repo (the fixtures load `../src/content/snapshot-core.js` by relative
path, so the server root must be `browser-extension/`):

    cd browser-extension && python3 -m http.server 8899 --bind 127.0.0.1

Then open each page and call the harness from the console — or drive it with
the `chrome-devtools` MCP tools, which is how it was verified:

    http://127.0.0.1:8899/test-fixtures/article.html
    http://127.0.0.1:8899/test-fixtures/react-form.html
    http://127.0.0.1:8899/test-fixtures/shadow-and-scroll.html
    http://127.0.0.1:8899/test-fixtures/heavy.html
    http://127.0.0.1:8899/test-fixtures/injection.html

    > await window.__runTests()
    { passed: 28, failed: 0, failures: [] }

## Coverage — 99 assertions

| Fixture | Asserts |
|---|---|
| `article.html` (28) | Markdown structure; exclusion of `display:none` / `visibility:hidden` / `aria-hidden` / zero-size / `<script>`; role + accessible name; stable-attribute selectors; **every emitted selector resolves to exactly one element**; boxes within [0,1]; tracking-pixel filtering; generation increments |
| `react-form.html` (16) | **React value-tracker defeat** (see below); select by value *and* visible label; checkbox; click by selector and by ref; selectors avoid hashed classes; rejection of unknown option, non-fillable element, missing selector, unknown ref, absent target |
| `shadow-and-scroll.html` (18) | Open shadow-root traversal; shadow elements have `selector: null` and are clickable by ref; position targeting; **`stale_viewport` after scroll, with and without a generation**; selector targeting immune to scroll drift; scroll variants; viewport metrics |
| `heavy.html` (10) | ~100k-char page; truncation at the default 40k budget and at an explicit one; truncation marker; element list survives markdown truncation |
| `injection.html` (27) | `execute_js` wrapper: primitives, objects, `await`, `undefined`→null; DOM elements → descriptors carrying a usable selector; NodeList → array; functions and circular refs degrade; errors captured with stack; truncation; real DOM mutation |

## The React fixture is the important one

`react-form.html` reproduces ReactDOM's `inputValueTracking` faithfully: an
instance `value` property that records the last observed value. A naive
`el.value = x` goes through it, so the tracker updates too and React concludes
nothing changed — the input event is swallowed and component state stays stale.
Writing through the *prototype* setter bypasses the instance property, leaving
the tracker stale, so the change is detected.

This was verified to genuinely discriminate: a naive assignment updates
`el.value` but leaves component state at its prior value, while
`__archieCore.fill()` updates both. A fixture that passed either way would
prove nothing.
