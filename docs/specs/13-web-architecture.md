# 13 — Web Architecture (frontend-next: main + compat builds)

Status: **DRAFT for Rodrigo's review** · 2026-10-03 · Branch `frontend-refactory`

This spec describes how the new web app is built, laid out and tested. **One codebase** in
`frontend-next/` produces **two builds**:

| Build | Served at | Targets | Device of record |
|---|---|---|---|
| **main** | `/` | Chrome/Edge ≥ 109, Firefox ≥ 115, Safari/iOS ≥ 15.4, Samsung Internet ≥ 21 | Laptop Chrome, Android phones (Chrome), PWA install |
| **compat** | `/compat/` | Safari 12 / iOS 12 (legacy chunks only) | iPad mini 2: A7, 1 GB RAM, iOS 12.5.x |

Inputs: `README.md` (charter, D1–D8, R1–R7), `spec/11-information-architecture.md` (IA, **approved**,
§9 decisions), `inventory/02-web-frontend.md` (features F-01…F-40, §5 compat, §6 bugs, §7
must-preserve), `inventory/01-backend-api.md` (contract, gotchas G-1…G-40).
Consumed (owned by other agents, not edited here): `spec/12-client-protocol.md` and
`shared/protocol-fixtures/` (normative data layer), `design/tokens/dist/{tokens.css,tokens.ts}`
(M3 tokens, **ready**).

Conventions: `fn/…` = `frontend-next/…`; `inv02 F-11` = inventory 02 feature F-11; `G-n` =
inventory 01 gotcha. Versions are those current on npm on 2026-10-03, pinned exactly (no `^`)
in `fn/package.json`.

---

## 0. Summary of key choices

| Topic | Choice | Why (short) |
|---|---|---|
| React | **React 18.3.1 on both builds** | One runtime and one test run for both builds; no "works on main, breaks on iPad" class of bugs (§1.3) |
| Build | Vite 8.3.2 + `@vitejs/plugin-react` 6.1.1; compat adds `@vitejs/plugin-legacy` 8.2.3 (legacy-only chunks) | One toolchain, two config files; fallback pair Vite 7.3.6 / legacy 7.2.1 if the W-01 spike fails |
| CSS | Plain CSS + CSS Modules; tokens from `design/tokens/dist/tokens.css`; PostCSS (`postcss-preset-env` 11.6.0) per-build browserslist; **no flex `gap` anywhere** | One stylesheet source, correct on Safari 12 by construction |
| State | Zustand 5.0.15 (vanilla stores + React selectors) on top of a framework-free protocol module | Works on React 18 (`useSyncExternalStore`), per-session stores isolate streaming re-renders |
| Markdown | `react-markdown` 10.1.0 + `remark-gfm` 4.0.1 **on both builds**, with `mdast-util-gfm-autolink-literal` aliased to a lookbehind-free fork | Replaces the compat table-only shim (which strips inline formatting, inv02 §6.3 #12) with full GFM everywhere |
| Highlight | `highlight.js` 11.12.0 core via `lowlight` 3.3.0, **curated language set per build** | Same highlighter on both builds; unsafe grammars excluded from compat by registry + gate |
| Long lists | **Bounded DOM window** (no virtualizer) re-using the prepend/scroll-restore path | Works under iOS 12 momentum scroll, keeps the freeze-buffer semantics (§5.2) |
| Safety net | **Compat bundle scanner gate** (AST + regexpp), stylelint bans, eslint `es-x`/`compat`, RegExp runtime guard in tests | Safari 12 rules are enforced by tools, not by memory |
| Icons / fonts | Material Symbols Rounded SVG paths generated from a manifest; Roboto Flex + JetBrains Mono woff2 bundled with our own `@font-face` | Tree-shaken, shared names with Android, Safari-12-safe font declarations |

---

## 1. Location, builds and deployment

### 1.1 Package layout

`frontend-next/` is **one npm package** (`"name": "frontend-next"`, `"private": true`,
`"type": "module"`). It sits beside `frontend/` and `frontend-compat/` until cutover (D5), so the
current apps keep working.

```
frontend-next/
├── package.json / package-lock.json     # single dependency tree for both builds
├── index.html                           # shared HTML template (build-specific head via plugin)
├── vite.shared.ts                       # aliases, define, css/postcss, dev proxy, fs.allow
├── vite.config.main.ts                  # base '/', outDir dist/, PWA files
├── vite.config.compat.ts                # base '/compat/', outDir dist-compat/, plugin-legacy
├── vitest.config.ts                     # test projects: protocol (node), dom (jsdom), compat (jsdom + compat aliases)
├── tsconfig.json                        # solution file → tsconfig.app.json, tsconfig.protocol.json, tsconfig.node.json
├── .browserslistrc                      # [main] and [compat] environments
├── eslint.config.js / stylelint.config.mjs
├── public-main/                         # manifest.json, sw.js, pcm-capture-worklet.js, icon-192/512.png, icon.svg
├── public-compat/                       # icon.svg, icon-192.png (no manifest, no SW, no worklet)
├── scripts/                             # build gates and generators (§2.3, §6)
├── mock-server/                         # fixture-replaying mock backend (§6.5)
├── qa/                                  # QA checklists; qa/screenshots/ is gitignored (fn/.gitignore)
└── src/                                 # §3
```

`fn/.gitignore`: `node_modules/`, `dist/`, `dist-compat/`, `dist-preview/`, `qa/screenshots/`,
`*.tsbuildinfo`. The repo-root `.gitignore` is not edited.

### 1.2 Toolchain (exact versions)

| Role | Package @ version |
|---|---|
| Runtime | `react` 18.3.1, `react-dom` 18.3.1, `zustand` 5.0.15 |
| Markdown | `react-markdown` 10.1.0, `remark-gfm` 4.0.1 (+ alias of `mdast-util-gfm-autolink-literal` 2.0.1), `hast-util-to-jsx-runtime` 2.3.6 |
| Highlight | `lowlight` 3.3.0, `highlight.js` 11.12.0 (deduped through `overrides` so lowlight uses the same copy) |
| Diff | `diff` 9.0.0 (lazy chunk) |
| Positioning / a11y | `@floating-ui/react-dom` 2.1.9, `tabbable` 6.5.0 |
| Compat polyfills | `@juggle/resize-observer` 3.4.0, `focus-visible` 5.2.1; `core-js` 3.50.0 via plugin-legacy (usage-based) |
| Fonts / icons | `@fontsource-variable/roboto-flex` 5.3.0, `@fontsource-variable/jetbrains-mono` 5.3.0 (only their woff2 files are used), `@material-symbols/svg-400` 0.47.6 (dev only, read by the icon generator) |
| Build | `vite` 8.3.2, `@vitejs/plugin-react` 6.1.1, `@vitejs/plugin-legacy` 8.2.3, `terser` 5.51.2, `postcss` 8.5.28, `postcss-preset-env` 11.6.0 (bundles `autoprefixer` 10.6.1 and `postcss-logical`) |
| Types | `typescript` **5.9.3** (not 7.x: `typescript-eslint` 8.71.0 requires `<6.1`), `@types/react` 18.3.31, `@types/react-dom` 18.3.7 |
| Lint | `eslint` **9.39.5** (not 10: `eslint-plugin-jsx-a11y` 6.10.2 supports ≤ 9), `typescript-eslint` 8.71.0, `eslint-plugin-react-hooks` 7.1.1, `eslint-plugin-jsx-a11y` 6.10.2, `eslint-plugin-es-x` 9.7.0, `eslint-plugin-compat` 7.0.2, `globals` 17.13.0; `stylelint` 17.16.0, `stylelint-config-standard` 40.0.0, `stylelint-no-unsupported-browser-features` 8.1.2 |
| Test | `vitest` 5.0.3, `@vitest/coverage-v8` 5.0.3, `jsdom` 30.1.1, `@testing-library/react` 16.3.3, `@testing-library/dom` 10.4.2, `@testing-library/user-event` 14.6.7, `axe-core` 4.13.0 |
| Gate scripts | `acorn` 8.18.0, `acorn-walk` 8.3.5, `@eslint-community/regexpp` 4.12.2, `source-map-js` 1.2.2 |
| Mock server | `ws` 8.22.0 |

Node: builds run **on the laptop only** (Node 22.21.1; Vitest 5 needs ≥ 22.12). The Jetson's
glibc cannot run Node ≥ 20 (`context/skills/server-management/SKILL.md`), so dists are built
locally and copied, as today.

W-01 owns `package.json` and installs **every** dependency above in its first commit. Later work
packages do not edit `package.json`; a needed extra dependency is a request to the coordinator,
who applies it in one place (this prevents lockfile conflicts between parallel agents).

### 1.3 React version strategy: React 18.3.1 for both builds

**Decision (needs Rodrigo's confirmation, §10 D-W1):** both builds use React 18.3.1.

The alternative was React 19 on main with compat aliased to React 18 (`"react18": "npm:react@18.3.1"`
plus a Vite alias). It is rejected for these reasons:

1. **One runtime, one test run.** Component tests run once against the React that ships to both
   builds. With two Reacts, every test would need to run twice, and the two `@types/react`
   versions would conflict in one `tsconfig` (type aliases for an npm-aliased package are fragile).
2. **No API-subset discipline.** React 19 APIs (`use`, Actions, `useActionState`, ref-as-prop,
   `<Context>` as provider, `<Activity>`) would compile on main and fail at runtime on compat.
   Keeping a "shared subset" by lint rule alone is a constant source of iPad-only bugs, which is
   the failure mode this rebuild exists to remove.
3. **Little to gain.** This is a client-only SPA. React 19's main wins (server components, form
   Actions, document metadata) do not apply. The one useful feature, `<Activity mode="hidden">`
   for background tabs, is replaced by our "frozen while hidden" subscriptions (§4.4).
4. **Proven on the device.** Today's compat build already runs React 18.3 on the iPad mini 2.

Upgrade path: code stays **React-19-ready**, enforced by lint: `createRoot` only, no string refs,
no legacy context, no `defaultProps` on function components, no `findDOMNode`. When the iPad
mini 2 is retired, both builds move to React 19 in a single commit. React 18.3.1 is
feature-frozen; the 2025 React security advisories affected the `react-server-dom-*` packages,
which this app does not use.

### 1.4 The two Vite configs

`vite.shared.ts` exports `sharedConfig(target: 'main' | 'compat', base: string)`:

- **Aliases:** `@/` → `src/`; `@tokens/` → `../design/tokens/dist/`;
  `mdast-util-gfm-autolink-literal` → `src/features/markdown/gfm/autolinkLiteralSafe.ts` (both
  builds, §2.4); `@/features/markdown/highlight/languages` → `languages.main.ts` or
  `languages.compat.ts`.
- **Define:** `__TARGET__` (`"main"`/`"compat"`), `__APP_VERSION__` (from `package.json` +
  `git rev-parse --short HEAD`), `__BUILD_TIME__`. Code branches on `__TARGET__` are
  dead-code-eliminated, so compat-only polyfills never reach main.
- **CSS:** `css.postcss` = `postcss-preset-env({ browsers: <target env from .browserslistrc>,
  stage: 2, features: { 'nesting-rules': true, 'custom-media-queries': true,
  'logical-properties-and-values': true, 'cascade-layers': false, 'focus-visible-pseudo-class':
  target==='compat' } })`. CSS Modules use `generateScopedName: '[name]__[local]__[hash:base64:5]'`.
- **Dev server:** `/api` (with `ws: true`), `/memory`, `/uploads` and `/projects` proxy to
  `process.env.ARCHIE_BACKEND ?? 'http://localhost:8765'` (`secure: false`, so the Jetson
  `https://192.168.0.200` works read-only). Visualization iframes use `VITE_VIZ_ORIGIN` in dev
  (the backend origin), because viz URLs are arbitrary root paths served by the backend
  catch-all (G-38). HTTPS uses `context/certs/{key,cert}.pem` when present (needed for mic and
  WebRTC on LAN devices), as in `frontend/vite.config.ts:6-21`.
- **Ports:** main dev **5450**, compat dev **5451**, mock server **8799**. These never collide with
  the current apps (5432/5433) or the backend (8765).
- `server.fs.allow`: `['..', '../../design/tokens/dist', '../../shared/protocol-fixtures']`.

`vite.config.main.ts`: `base: process.env.FN_BASE ?? '/'`, `outDir: process.env.FN_OUT ?? 'dist'`,
`publicDir: 'public-main'`, `build.target: ['es2020','chrome109','safari15.4','firefox115']`,
`build.manifest: true` (for the budget gate), `build.sourcemap: 'hidden'`.

`vite.config.compat.ts`: `base: process.env.FN_BASE ?? '/compat/'`,
`outDir: process.env.FN_OUT ?? 'dist-compat'`, `publicDir: 'public-compat'`, and
`legacy({ targets: ['safari >= 12', 'ios_saf >= 12'], renderModernChunks: false,
modernPolyfills: false, polyfills: true })`, `build.manifest: true`,
`build.sourcemap: 'hidden'` (the scanner maps findings back to source modules), and terser.

`index.html` is shared. A small local plugin (`fn/scripts/vite-plugin-html-target.ts`, owned
by W-01) injects per-target head content:

| Item | main | compat |
|---|---|---|
| Remote-console inline script (inv02 F-38) | yes, **off by default** (enabled by the device pref) | yes, **on by default**, `[compat] ` prefix |
| `manifest.json`, apple-touch-icon, SW registration | yes | no (as today, inv02 §5.1) |
| `<link rel=preload>` for the latin Roboto Flex woff2 | yes | yes |
| `theme-color` meta | `#0d0e10` (tokens `background`, dark) | same |
| Viewport | `width=device-width, initial-scale=1, viewport-fit=cover` | same |

The remote-console script is hand-written ES5 (it is not transpiled). It is improved: rate
limit 60 messages per 10 s, 4 KB per message, a dropped-message counter, and always-on capture
of `error`/`unhandledrejection`. The bundle scanner parses it (§2.3).

### 1.5 npm scripts

| Script | Does |
|---|---|
| `dev` / `dev:compat` | Vite dev server for main (5450) / compat (5451) |
| `mock` | Start `mock-server/server.mjs` on 8799 (use with `ARCHIE_BACKEND=http://localhost:8799`) |
| `build` | `typecheck` → `gate:tokens` → `build:main` → `build:compat` → `gate:compat` → `gate:budgets` |
| `build:main` / `build:compat` | Single-target builds |
| `build:preview` | Both builds with `FN_BASE=/next/` → `dist-preview/main` and `FN_BASE=/next-compat/` → `dist-preview/compat` |
| `typecheck` | `tsc -b` (app + protocol + node projects) |
| `lint` / `lint:css` | ESLint / Stylelint |
| `test` / `test:watch` / `test:coverage` | Vitest (all projects) |
| `gate:compat` | `node scripts/scan-compat-bundle.mjs dist-compat` (§2.3) |
| `gate:budgets` | `node scripts/check-budgets.mjs` against `scripts/budgets.json` (§5.4) |
| `gate:tokens` | Run `npm --prefix ../design/tokens run check` (fails if the token dist is stale), then `node scripts/check-token-vars.mjs` (every `var(--md-…)`/`var(--app-…)` used in `src/**/*.css` exists in `tokens.css`) |
| `icons` | `node scripts/gen-icons.mjs` (manifest → `src/ui/icons/generated.ts`) |
| `verify` | `lint && lint:css && typecheck && test && build` (the done-gate for every work package) |

### 1.6 Trying the new build on real devices before cutover

Two options. Option B is recommended for day-to-day use because it does not need the laptop.

**Option A (no backend change):** run `vite preview` of each build on the laptop over HTTPS
with the API proxied to the Jetson, as `frontend/vite.audit.config.ts` does today. The devices
open `https://192.168.0.28:5450/` and `:5451/`. Each device must trust the laptop's
self-signed certificate once. This only works while the laptop is on.

**Option B (additive backend route; needs Rodrigo's approval, §10 D-W2):** the backend serves
`frontend-next/dist-preview/main` at **`/next/`** and `dist-preview/compat` at **`/next-compat/`**.
This is same-origin with the API, so mic, WebRTC, iframes and storage behave exactly as in
production. The change is ~25 lines in `api/app.py` plus one test, and is owned by W-15:

- Register the routes **before** the `if frontend_dist.exists()` block (the SPA catch-all at
  `api/app.py:283-302` would otherwise swallow `/next/*`), and only if the directory exists
  (same pattern as compat, `api/app.py:182-197`).
- For each `(prefix, dir)`: mount `/{prefix}/assets` with `StaticFiles`; `GET /{prefix}` and
  `/{prefix}/` return `index.html` with the existing `_no_cache` headers; `GET /{prefix}/{path}`
  returns the file if `candidate.resolve().is_relative_to(root)` and it is a file, else
  `index.html` (no-cache). The traversal guard is new; the current compat route lacks one.
- Test `tests/test_next_preview_routes.py`: index served with no-cache, asset served, SPA
  fallback, traversal rejected, routes absent when the dir is missing.
- Deploy: `npm run build:preview` on the laptop, then rsync `frontend-next/dist-preview/` to the
  Jetson (same procedure as the current dists). `context/public/` has no `next*` entry, so
  nothing is shadowed.
- Preview builds do **not** register the service worker (avoids a second SW scope while
  dogfooding). Hash routing (§4.5) means the documents are always at the base path, so no
  deep-link fallback issues.
- Removed at cutover (W-15).

### 1.7 Cutover (end of P5)

1. Acceptance matrix green; Rodrigo signs off on device tests (desktop, phone, iPad mini 2).
2. `git rm -r frontend frontend-compat`, then `git mv frontend-next frontend`.
3. The main build already writes `frontend/dist` (served at `/` with no backend change). For the
   compat output, choose one (§10 D-W3):
   - **Recommended:** compat writes `frontend/dist-compat`; a **one-line backend change** in
     `api/app.py:182` (`compat_dist = …/"frontend"/"dist-compat"`) plus removal of the `/next`
     routes.
   - **No backend change:** compat writes `../frontend-compat/dist` (`FN_OUT`), which keeps an
     otherwise-empty `frontend-compat/` directory as a deploy target.
4. The new `sw.js` uses cache name `assistant-v2` and deletes `assistant-v1` on activate, so
   installed PWAs pick up the new shell on their next navigation (the SW is network-first
   for navigations, inv02 §1.10).
5. Update the deploy steps in `context/skills/server-management/SKILL.md` (rsync paths) and the
   CLAUDE.md "Peripheral Frontends" section. This is done by the coordinator, outside the
   frontend boundary.
6. Manifest changes: `orientation` becomes `any` (the adaptive layout supports landscape),
   `name`/`short_name` become "Archie".

---

## 2. Safari 12 strategy (built in from the start)

The principle: **one source, correct by construction, verified by machines**. Nothing in the
source relies on a later compat patch. Every rule below is enforced by at least one automated
gate (§6.6).

### 2.1 Spacing without flex `gap`: layout primitives

Flex `gap` needs Safari 14.1. The app **never uses `gap`, `row-gap` or `column-gap`**
(stylelint `property-disallowed-list`). The one exception is `grid-gap`, which Safari 10.1+
supports for grid only. All spacing goes through these primitives (`fn/src/ui/primitives/layout/`,
CSS in `fn/src/styles/primitives.css`, exported both as React components and as classes):

| Primitive | CSS rule (space = `var(--app-space-N)`) | Use |
|---|---|---|
| `Stack` (`.stack`) | `.stack > * + * { margin-top: var(--stack-space) }` | Vertical lists of blocks |
| `Inline` (`.inline`) | `display:flex; align-items:center` + `.inline > * + * { margin-left: var(--inline-space) }` | Icon + label, button rows |
| `Cluster` (`.cluster`) | wrapping flex; container `margin: calc(var(--cluster-space) / -2)`, children `margin: calc(var(--cluster-space) / 2)` | Chips, wrapping toolbars |
| `Center` | `max-width` + auto margins | 840 dp message column |
| `Box` | padding only | Surfaces |
| `Split` | flex, `margin-left: auto` on the last child | Header with trailing actions |

Rules that keep the "owl" safe:

- **Text-node trap (inv02 §5.3, commit 8b8fb9c):** `* + *` never matches text nodes. The
  `Inline` and `Cluster` React components wrap every string or number child in a `<span>`
  (`React.Children.map`), so `<Icon/> label` always becomes two elements. Components with
  internal icon/label pairs (Button, Chip, ListItem, MenuItem) use the same primitive or an
  explicit `margin-right` on the icon slot, never `gap`.
- Conditional children rendering `null`/`false` are not DOM nodes, so the owl still spaces
  correctly.
- Spacing is set by custom properties (`--stack-space`), so a component can change spacing for
  one instance without new CSS.
- The same stylesheet ships to both builds. This deletes today's hand-synced 335-line
  `gap-compat.css` and its drift risk (71 `gap:` declarations).

### 2.2 PostCSS: `inset`, logical properties, prefixes

`postcss-preset-env` 11.6.0 runs on both builds with the target's browserslist:

- `logical-properties-and-values` lowers `inset`, `inset-inline`, `margin-inline`,
  `padding-block` and similar to physical properties. The app is LTR-only, so the output is
  exact. Today's broken `inset: 0` overlays (inv02 §5.4 #3, §6.3 #14) cannot recur.
- `autoprefixer` adds `-webkit-backdrop-filter`, `-webkit-mask-image`, `position: -webkit-sticky`,
  `-webkit-user-select`, `-webkit-appearance`.
- `custom-media-queries` provides the size classes once: `@custom-media --compact (max-width:
  599.98px)`, `--medium (min-width: 600px) and (max-width: 839.98px)`, `--expanded (min-width:
  840px)` in `fn/src/styles/media.css`.
- `nesting-rules` lets authors use CSS nesting (it is compiled away).
- `color-functional-notation` lowers space-separated `rgb()`; `focus-visible-pseudo-class`
  (compat only) rewrites `:focus-visible` to `.focus-visible`, paired with the `focus-visible`
  5.2.1 polyfill loaded by the compat entry.

Belt and braces: Stylelint bans what PostCSS cannot lower (§6.6), and the scanner re-checks the
built compat CSS (§2.3).

Tokens: `design/tokens/dist/tokens.css` declares itself Safari-12-safe (plain custom
properties, no `color-mix`/`oklch`/`@layer`/`:is`/`:where`/nesting). It provides precomputed
state-layer colors (`--md-sys-color-on-surface-hover`, `-pressed`, …) and `-rgb` triplets, so the
kit never needs `color-mix()`. State layers are a `::before` pseudo-element with
`background: currentColor; opacity: var(--state-opacity)` or the precomputed role, both of which
work on Safari 12.

### 2.3 No lookbehind or named groups: the compat bundle scanner gate

Safari 12 throws a `SyntaxError` at **parse time** for a regex literal with lookbehind or a named
group, which takes down the whole chunk (a white screen). A `new RegExp("(?<=…)")` throws only
when the code runs. Both cases are gated.

`fn/scripts/scan-compat-bundle.mjs <distDir>` (owned by W-01; it has its own unit tests in
`fn/scripts/__tests__/`):

1. **Inputs:** every `*.js` under `dist-compat/` (legacy chunks, polyfills, `sw`/worker files if
   any), every inline `<script>` in `dist-compat/index.html`, and every `*.css`.
2. **Syntax check:** parse each JS file with `acorn` (`ecmaVersion: 2018`, `sourceType:
   'script'`). A parse failure fails the gate. This catches ES2019+ syntax that escaped Babel
   (optional chaining, class fields, BigInt literals, numeric separators), for example in a file
   copied from `public-compat/`.
3. **Regex literals:** walk the AST (`acorn-walk`). For every `Literal` with `regex`, validate
   the pattern with `@eslint-community/regexpp`'s `RegExpValidator` and fail on:
   lookbehind (`onLookaroundAssertionEnter` with kind `lookbehind`, covering `(?<=` and `(?<!`),
   named groups (`onCapturingGroupEnter` with a name, covering `(?<name>`), named
   back-references (`\k<name>`), and the flags `d` and `v`. Validation is semantic, so a pattern
   that *matches the text* `(?<` is not a false positive. For example highlight.js's
   `/\(\?<(?![=!])[^>]+>/` passes.
4. **`RegExp` constructor calls:** for every `new RegExp(…)`/`RegExp(…)` (callee `RegExp`,
   `window.RegExp`, `globalThis.RegExp`, `self.RegExp`) whose first argument is a string literal
   or an expression-free template, validate it as in step 3. Dynamic arguments are reported as
   **warnings** with the source location.
5. **String heuristics:** every string literal or template quasi containing `(?<=`, `(?<!`, or
   `(?<` followed by an identifier and `>` **fails** unless allowlisted. This catches regex
   sources assembled from strings (highlight.js grammars built with `concat()`, marked's
   `.replace("x", "(?<!`)")`).
6. **Allowlist:** `fn/scripts/compat-scan-allowlist.json` lists `{ module, snippet, reason,
   addedBy }`. `module` is the original source path (resolved through the hidden sourcemap with
   `source-map-js`), so entries survive hash changes. An entry that matches nothing fails the
   gate, so stale entries do not accumulate.
7. **CSS check:** parse each CSS file with `postcss` and fail on: `gap`/`row-gap`/`column-gap`;
   `inset*`; `aspect-ratio`; any `*-inline*`/`*-block*` logical property; unprefixed-only
   `backdrop-filter` or `mask-image`; selectors containing `:is(`, `:where(`, `:has(`, or a raw
   `:focus-visible`; values with `dvh|svh|lvh`, `color-mix(`, `clamp(`; and the at-rules `@layer`
   and `@container`.
8. **Output:** a human summary (finding, original module:line, snippet), plus
   `dist-compat/.scan-report.json`. Exit 1 on any failure.

Its fixtures must include: a real lookbehind (`mdast-util-gfm-autolink-literal`'s
`/(?<=^|\s|\p{P}|\p{S})…/u`, which **fails**); highlight.js's escaped `\(\?<` pattern (which
**passes**); marked's `new RegExp("(?<=1)(?<!1)")` (which **fails** as a string); a comment
containing `(?<=` (which **passes**); a named group (**fails**); `inset: 0` in CSS (**fails**).

Source-level complements (fast feedback before a build): ESLint `es-x/no-regexp-lookbehind-assertions`,
`es-x/no-regexp-named-capture-groups`, `es-x/no-regexp-d-flag`, `es-x/no-regexp-v-flag` on
`src/**`. Runtime complement: a **RegExp guard** in the `compat` Vitest project
(`fn/src/test/regexpGuard.ts`) wraps the global `RegExp` constructor and throws on the same
features. Grammars that build their regexes at registration time (highlight.js) are then caught
in tests, even though no browser test runs Safari 12.

Measured on 2026-10-03 (an `npm i` of the candidate libraries in a scratch directory, grepped for
`(?<`), the real offenders are: `mdast-util-gfm-autolink-literal` (lookbehind literal),
`marked` (named groups), and highlight.js grammars `gcode` (real lookbehind) and others whose
hits are inside comments. `diff` 9's hit is in a comment, and `refractor` has comment-only
hits. The scanner, not grep, is the authority.

### 2.4 Markdown and syntax highlighting per build

| Library | main | compat | Notes |
|---|---|---|---|
| `react-markdown` 10.1.0 + `micromark` core | ✅ | ✅ | Clean (state machines, no lookbehind) |
| `remark-gfm` 4.0.1 | ✅ | ✅ | **Only with the alias** below |
| `mdast-util-gfm-autolink-literal` 2.0.1 (stock) | ❌ | ❌ | Lookbehind + `\p{P}` literal. Replaced on **both** builds by `src/features/markdown/gfm/autolinkLiteralSafe.ts`, an MIT-attributed fork (~200 lines) whose find-and-replace uses a leading boundary group `(^|[\s!-/:-@[-\`{-~])` instead of lookbehind, then adjusts the match index. Same output on both builds, so screenshots compare 1:1 |
| `highlight.js` 11.12.0 core via `lowlight` 3.3.0 | ✅ | ✅ | Core and the curated languages are lookbehind-free (verified by the scanner and the RegExp guard) |
| Language set: bash, shell, javascript, typescript, json, python, kotlin, java, css, scss, xml/html, yaml, markdown, diff, sql, go, rust, c, cpp, csharp, dockerfile, ini/toml, makefile, nginx, php, ruby, lua, powershell, plaintext | ✅ | ✅ | `languages.compat.ts` = this list; `languages.main.ts` = the same list |
| Extra languages (swift, scala, haskell, fsharp, r, gcode, …) | ✅ lazy chunk | ❌ | `languages.main.ts` registers a loader for a second lazy chunk on first use |
| Prism / `react-syntax-highlighter` / `refractor` | ❌ | ❌ | Dropped: named groups in grammars, heavy, and a second highlighter is pointless |
| Shiki 4.x | ❌ | ❌ | Oniguruma/TextMate grammars (lookbehind everywhere, WASM or a large JS regex engine). Too heavy for the A7 and unnecessary on main |
| `marked`, `markdown-it` | ❌ | ❌ | Not used (`marked` has named groups) |

Rendering rules (both builds; fixes inv02 F-03 and §6.2):

- No raw HTML (no `rehype-raw`). `react-markdown`'s default `urlTransform` blocks `javascript:`
  links.
- All external links get `target="_blank" rel="noopener noreferrer"` and the `md-link` class,
  which carries the iPad tap fix (`cursor:pointer; touch-action:manipulation`, commit e6f2f53).
- **Fenced code without a language renders as a code block** with a Copy button (fix).
- Copy uses `navigator.clipboard.writeText` when available. Otherwise it falls back to a hidden
  textarea + `document.execCommand('copy')`, because Safari 12 (< 13.1) and plain-HTTP origins
  have no Clipboard API.
- Tables are wrapped in a horizontal scroll container.
- Memory documents resolve relative `.md` links against the current file's path and open them
  in the app (inv02 F-37, §6.2 fix). The file fetch URL-encodes each path segment.
- Highlighting runs only on closed fences, after streaming. On compat/low-end it is deferred
  (`setTimeout` chunks) and skipped for blocks > 8 KB. "Syntax highlighting" is a device pref
  under Appearance, default on.

### 2.5 Audio feature matrix

Features are chosen by **runtime capability detection** (`src/platform/capabilities.ts`), not by
build flag, so a modern Safari loading `/compat/` still gets the better path.

| Feature | Requires | main | compat (iOS 12) | Fallback / behaviour |
|---|---|---|---|---|
| Realtime voice, OpenAI (WebRTC) | `RTCPeerConnection`, data channel, `getUserMedia` (secure context) | ✅ | ✅ best effort | `webkitAudioContext` for meters; `<audio playsinline autoplay>`; start only from a user tap (iOS autoplay rule); `AudioContext.resume()` inside the tap handler |
| Realtime voice, WS relay (Qwen, Gemini) mic capture | AudioWorklet (Safari 14.1+) | ✅ AudioWorklet (`/pcm-capture-worklet.js`) | ⚠ `ScriptProcessorNode` fallback (4096-frame buffer, linear resample to `audio_in_format.sample_rate`, 100 ms chunks) | Behind one `PcmCaptureSource` interface. Scope on compat is decision §10 D-W4 |
| Voice playback (`voice_audio_out`) | `AudioBufferSourceNode` | ✅ | ✅ (`webkitAudioContext`) | Same gapless `PCMPlayer` (inv02 F-26) |
| Level meters | `AnalyserNode` | ✅ ~15 fps | ✅ 10 fps on low-end | — |
| Audio message (`send_audio`) | `MediaRecorder` (Safari 14.1+) | ✅ webm/opus → ogg → mp4 | ⚠ **WAV fallback**: PCM via the same ScriptProcessor capture at 16 kHz, encoded to WAV in JS (`src/voice/audio/wav.ts`), `format: "wav"` (accepted by the backend, inv01 §5.2) | 60 s cap; ~2.6 MB base64 per minute over WS (WS frames are not subject to nginx's 1 MiB body cap) |
| WebRTC session recording (`voice_recording_chunk`) | AudioWorklet | ✅ (fixes the `"pcm-capture-processor"` name bug, inv02 F-30) | ❌ hidden | — |
| Echo cancellation / noise suppression constraints | `getUserMedia` constraints | ✅ | partial (ignored if unsupported) | — |

The UI never shows a control that cannot work. Unsupported controls are hidden, and the voice
dock explains why ("Voice needs HTTPS" when `!isSecureContext`, "Not supported on this browser"
otherwise). For screenshots, `?caps=compat` simulates the compat capability set in Chrome (§6.4).

### 2.6 Other Safari 12 rules

| Concern | Rule (where enforced) |
|---|---|
| **Pointer Events** (Safari 13+) | No `onPointer*` in app code (ESLint `no-restricted-syntax`). Gestures (tab drag-reorder, long-press, swipe) use mouse + touch events in `src/ui/a11y/gestures.ts`. Outside-dismiss listens to `mousedown` + `touchstart`. |
| `ResizeObserver` (Safari 13.1+) | Compat entry loads `@juggle/resize-observer` before render (`src/platform/polyfills.compat.ts`). |
| `IntersectionObserver` (iOS 12.2+) | Not used for anything load-bearing. Scroll position comes from scroll events. |
| `matchMedia(...).addEventListener` (Safari 14+) | `src/platform/media.ts` uses `addListener`/`removeListener` when needed. |
| `EventTarget` constructor, `structuredClone`, `WeakRef`, `Intl.RelativeTimeFormat`, `Intl.DateTimeFormat` `dateStyle`, `requestIdleCallback`, `Element.scrollTo(options)`, `scrollIntoView(options)` | Banned by `eslint-plugin-compat` (compat browserslist) + `no-restricted-properties`. Replacements: `src/platform/emitter.ts`, `src/platform/time.ts` (relative-time formatter), direct `scrollTop` assignment. ES built-ins (`Object.fromEntries`, `String.prototype.matchAll`, `Array.prototype.at`, `queueMicrotask`, `Promise.allSettled`) are polyfilled by core-js through plugin-legacy. |
| `crypto.randomUUID` (missing on Safari 12 **and** on any plain-HTTP origin) | `src/platform/uuid.ts`: `crypto.randomUUID` → `crypto.getRandomValues` v4 → `Math.random` (inv02 F-20, **[LOAD-BEARING]**). Direct `crypto.randomUUID` is banned by lint. |
| **16 px inputs** (iOS focus zoom) | Only `TextField`, `Select`, `Composer` and `SearchField` render `<input>`/`<textarea>`/`<select>` (ESLint `no-restricted-syntax` outside `src/ui/controls/` and `src/features/composer/`). They use `--md-sys-typescale-body-large-size` (16 px) and never a smaller size, at any breakpoint. The device "Text size" pref cannot go below 100 % on iOS. This fixes the ≤ 640 px override bug (inv02 §6.2). |
| **Momentum scrolling** | Every scroll container is the `ScrollArea` primitive (`src/ui/primitives/ScrollArea/`): `-webkit-overflow-scrolling: touch`, `overflow-anchor: none`, and an imperative API: `scrollToBottom()` (iOS-safe: set `webkitOverflowScrolling='auto'`, assign `scrollTop`, restore `touch` on the next frame, from `fc/src/shims/MessageList.tsx`), `preserveAnchor(fn)` for prepends (record the first-old-item offset, hide for one frame, restore), and `isNearBottom(px)`. Programmatic scrolls are deferred while a touch is active and until scroll events go quiet for 100 ms (momentum end). This is the shim that was never wired (inv02 §6.3 #13), now the only code path. |
| Page-level scroll and viewport height | `html, body { height: 100%; overflow: hidden }`; the shell is `position: fixed` on iOS so the page never rubber-bands. No `vh`/`dvh` for layout: `--app-height` is set from `window.innerHeight` on resize/orientation change. Safe-area insets via `env(safe-area-inset-*)` (iOS 11.2+). Scroll lock for modals is a reference-counted `data-scroll-locked` marker on `<html>` and never the fixed-body technique: on Safari 12, `body { position: fixed }` set in the same commit as an overlay mount left the overlay's fixed layer with no layout (0x0, invisible, untappable; 2026-10-09). The page cannot scroll anyway (see above). |
| iOS keyboard | iOS 12 has no `visualViewport`. On composer focus the browser scrolls the field into view; on blur the shell calls `window.scrollTo(0, 0)` to undo any page offset. Device-tested in W-11. |
| `<dialog>`, `inert`, `:focus-visible` native | Not used. Dialogs are portals; background inertness is `aria-hidden` on siblings plus a focus trap; `:focus-visible` is polyfilled on compat. |
| `auxclick` (middle-click close) | Detected via `mouseup` with `button === 1` (and `mousedown` `preventDefault` to stop autoscroll). |
| Theme | `prefers-color-scheme` needs iOS 13. On iOS 12, "System" falls back to dark, the default in `tokens.css`. |
| Fonts | Fontsource's CSS uses the family name `"Roboto Flex Variable"` and `format('woff2-variations')`, which mismatches the tokens' `"Roboto Flex"` and is a format hint older Safari may skip. So **we write our own `@font-face`** (`src/styles/fonts.css`) with the **token family names**, `format('woff2')`, `font-weight: 100 1000`, `font-display: swap`, and `unicode-range` per subset, pointing at the package's woff2 files through Vite asset imports. |

### 2.7 Low-end mode

`src/platform/lowEnd.ts` sets `html.low-end` when `navigator.hardwareConcurrency <= 2` or
`navigator.deviceMemory <= 1` (inv02 §1.2, **[LOAD-BEARING]**; the iPad mini 2 reports 2 cores).
It also sets it on the compat build by default; the device pref "Reduce motion" (Appearance)
can force it on anywhere. Effects:

| Area | Normal | Low-end |
|---|---|---|
| CSS motion | M3 durations from tokens | `animation: none; transition: none` (`src/styles/motion.css`; also under `prefers-reduced-motion: reduce`) |
| Blur / shadows | `backdrop-filter` on scrims, M3 elevation shadows | No blur; shadows only on menus |
| Stream flush to React | once per animation frame | every 100 ms |
| Mounted messages per panel | 200 | 80 |
| Highlighting | after the fence closes | deferred chunks, ≤ 8 KB blocks |
| Meters | ~15 fps | 10 fps |
| Hidden panels | frozen (§4.4) | frozen |

---

## 3. Module structure

### 3.1 Directory map and ownership

Every directory has exactly one owning work package (§7). Cross-package use goes through each
directory's `index.ts` **entry point** (§3.9). An owner creates its `index.ts` in its first
commit, stubs allowed, so dependents can import early.

```
src/
├── main.tsx                     W-01  entry: compat polyfills → platform init → createRoot(<App/>)
├── env.d.ts                     W-01  __TARGET__, __APP_VERSION__, __BUILD_TIME__
├── platform/                    W-01  capabilities, lowEnd, uuid, clipboard, media, storage (safe local/sessionStorage), emitter, time, remoteLog (pref + beacon), polyfills.compat.ts, debug (?debug=voice|all, window.__archie)
├── test/                        W-01  vitest setup: jsdom shims, regexpGuard.ts, render helpers, axe helper
├── protocol/                    W-05  framework-free client core (§3.2)
├── services/                    W-06  REST, WebSockets, session runtimes, pool sync, uploads (§3.4)
├── stores/                      W-06  Zustand stores (§3.3)
├── voice/                       W-12  voice engine (non-UI): core, transports, audio (§3.4)
├── styles/                      W-02  reset, base, fonts, typography, primitives, media, motion, theme.ts
├── ui/
│   ├── primitives/              W-02  layout primitives, ScrollArea, Icon, StateLayer, FocusRing, VisuallyHidden, Spinner
│   ├── icons/                   W-02  icons.manifest.json, generated.ts (by `npm run icons`)
│   ├── controls/                W-03  actions, inputs, selection, display components
│   ├── overlays/                W-04  Portal, Scrim, Menu, Dialog, sheets, Snackbar, Tooltip, Popover
│   ├── navigation/              W-04  NavigationRail, NavigationDrawer, Tabs, TopAppBar, Tree
│   └── a11y/                    W-04  focus trap, dismiss, roving focus, scroll lock, live region, gestures
├── features/
│   ├── markdown/                W-08  Markdown, StreamingMarkdown, CodeBlock, gfm/, highlight/
│   ├── conversation/            W-09  ConversationPanel, MessageList, messages, inline cards
│   ├── tools/                   W-10  tool registry, ToolCard, renderers, StepGroup, DiffView
│   ├── composer/                W-11  Composer, primary action, context ring, attach, audio message
│   ├── session-actions/         W-11  session ⋮ menu actions, confirm dialogs, busy overlay, rewind/fork
│   ├── voice/                   W-12  VoiceDock, VoiceButton, LevelOrb, ActiveElsewhere
│   ├── settings/                W-13  Settings home + pages, SessionSettingsSheet
│   ├── auth/                    W-13  AuthGate screen
│   ├── history/                 W-14  HistoryPane (search, Open now, date groups), SessionRow
│   ├── memory/                  W-14  MemoryPane (tree + search), MemoryDocument
│   └── visuals/                 W-14  VisualsPane, VisualViewer, Show on TV
├── app/                         W-07  App, AppShell, layouts, navigation/routing, tabs UI, workspace, keyboard
└── dev/gallery/                 W-02  gallery shell (route #/dev/gallery); each UI package adds its own `sections/*.gallery.tsx` (auto-discovered by import.meta.glob)
```

### 3.2 `protocol/`: framework-free client core (W-05)

The TypeScript implementation of `spec/12-client-protocol.md`. **This spec does not define
protocol semantics**; spec 12 is normative. This section fixes only the packaging.

- **Framework-free, enforced:** compiled by `tsconfig.protocol.json` with `lib: ["ES2022"]` (no
  DOM types) and `types: []`. ESLint `no-restricted-imports` forbids `react`, `zustand` and
  `@/services|stores|ui|features|app`. Storage, clocks and ID generation are injected as small
  interfaces, so the module is pure and deterministic under test.
- **Expected contents** (final shape follows spec 12):
  - `wire/`: typed server events and client messages for the chat WS and the orchestrator WS,
    plus a tolerant decoder (unknown JSON → typed event or `unknown`; never throws).
  - `reducer/`: the conversation reducer (ordering, tool-result-by-id across all messages, never
    lost (R7); status machine; permission state; stall; compaction).
  - `history/`: REST `MessagePreview` → state, pagination merge, `drop_last_n` computed from
    server indices (G-26).
  - `resume/`: `(stream_id, seq)` checkpoint logic with injected per-tab storage (inv02 F-24).
  - `voice/`: the pure voice signaling state machine, if spec 12 places it there; otherwise it
    lives in `src/voice/core/` (§3.4).
  - `selectors/`: pure derived views, for example tool-step grouping (§3.6) and `isBusy`.
- **Interface assumptions** on spec 12 (raise with its author if they do not hold): the reducer is
  immutable with **structural sharing** (unchanged messages and blocks keep their object
  identity, which memoized rendering depends on); every message and block has a **stable client
  id** for React keys; reducer state is plain serializable data.
- **Conformance suite:** `src/protocol/__tests__/conformance.test.ts` loads every fixture under
  `FIXTURES_DIR` (`vitest.config.ts`, default `<repo>/shared/protocol-fixtures`; one constant to
  change if spec 12 places them elsewhere), feeds its event transcript through decoder and
  reducer, and compares with the fixture's expected state in the format spec 12 defines. No
  fixture may be skipped. The suite runs in the `protocol` (node) project.
- **Mock backend:** W-05 also owns `fn/mock-server/` (§6.5). It replays the same fixtures over
  WS and REST, so UI work and screenshots never depend on the Jetson.

### 3.3 Stores (W-06): Zustand 5 on React 18

Zustand 5.0.15 uses React's built-in `useSyncExternalStore`. Stores are created with the
vanilla `createStore` and read through typed hooks, so services can update them without React.

| Store (`src/stores/`) | Holds | Persistence |
|---|---|---|
| `sessionStore.ts` | **Factory**: one store per open session (`local_id`): protocol state, draft text, scroll anchor, pending-voice flags | Draft in `sessionStorage` (`draft:<local_id>`); resume checkpoint **in memory only** (spec 12 T-10: a reload rebuilds from REST and never sends a persisted `resume_from`, which fixes the duplicate-on-reload bug W-7; in-page reconnects still replay, preserving inv02 F-24) |
| `sessionRegistry.ts` | Map `local_id → { store, runtime }`; creation and disposal | — |
| `tabs.ts` | Ordered tab list (`kind: archie | agent | memory | visual`, ids, `resumeSdkId`, unseen badge), active id | Order and doc tabs in `localStorage` (`tabs:v1`). Chat tabs are re-derived from `pool/live` on load (inv02 F-23) |
| `catalog.ts` | Session history list, memory tree, visuals list, with loading/error | — |
| `serverConfig.ts` | `/api/config`, providers, models, voice catalogs, MCP list; save state | — |
| `prefs.ts` | Device prefs: theme, text size, reduce motion, syntax highlighting, list pane collapsed, last rail destination, remote logging, tool step grouping | `localStorage` (`prefs:v1`), via the try/catch wrapper in `platform/storage.ts` |
| `capabilities.ts` | Client capabilities (`platform/capabilities`) + backend capabilities (`voiceAudioModels`, `castAvailable`) | — |
| `snackbar.ts` | Queue of snackbars (message, action, duration) | — |
| `connection.ts` | Backend reachability for the app-level connection indicator | — |

Rendering rules:

- Components select **narrow slices** (`useSession(localId, s => s.messages[i])`) with shallow
  equality (`useShallow`), so a delta in session A never re-renders session B. Because of
  structural sharing, only the streaming message re-renders.
- **Delta coalescing:** session runtimes apply every event to the reducer immediately, so logic
  always sees current state. React subscribers are notified at most **once per animation frame**
  (main) or **every 100 ms** (low-end). This is the main streaming-cost control (§5.1).
- **Frozen while hidden:** a session store's React-facing snapshot is not published while its
  panel is hidden (§4.4). Catch-up happens in one render on show.

### 3.4 Services (W-06) and the voice engine (W-12)

`src/services/`:

| Module | Responsibility (load-bearing sources) |
|---|---|
| `http/client.ts` | `fetch` wrapper with timeouts (`AbortController`). Error type `ApiError{status, detail, raw}` keeps the backend `detail` verbatim (fixes inv02 §6.2: "400 Bad Request" instead of "Directory does not exist…"). Recognizes nginx's **HTML 413** as `PayloadTooLarge` (G-1). Treats a `text/html` 200 on an API probe as "not found" (SPA fallback, G-38). |
| `http/endpoints/*.ts` | One typed function per REST endpoint in inv01 §3, grouped by domain (sessions, config, voice, memory, visuals, auth, uploads, debug). Memory paths are URL-encoded per segment. `GET /api/config/openai-key` is never called. |
| `ws/socket.ts` | `binaryType = 'arraybuffer'`; decode binary frames with `TextDecoder`; **always send text frames** (G-2); a close or error becomes typed connection events, never raw strings in the UI. |
| `ws/reconnect.ts` | Reconnect every 2000 ms, up to 10 attempts, **paused while `document.hidden`**. On visibility → visible: if OPEN, re-send `start`; else reset attempts and connect now (**[LOAD-BEARING]** inv02 F-01, §7 #2). |
| `sessions/SessionRuntime.ts` | One per open agent session (one socket per session, G-6). History load (`limit=50`), the `start` handshake on every open (with `resume_from`), `replay_overflow` fallback, pagination, send/queue, interrupt, compact, permission response, restart. The history-init step never re-runs when `resumeSdkId` arrives (**[LOAD-BEARING]** F-01 #4). Adopts `session_started.session_id` (G-16). |
| `sessions/ArchieRuntime.ts` | The orchestrator: singleton attach by shared `local_id` (G-15), `orchestrator_active` handling, model info, `send_audio`, `inject_text` for uploads, watcher events. |
| `sessions/poolSync.ts` | `GET /api/sessions/pool/live` on mount and on visibility. Handles `agent_session_opened/closed` pushes and checks `is_orchestrator` (G-39). **No focus stealing:** background opens add an unseen-badged tab plus a snackbar "Archie opened <title> · Open" (fixes inv02 §6.1). The pool is the open set (spec 12 OPEN-1..4): automatic starts carry `reattach`, and a conversation that leaves the pool closes its view, active or not (implemented in `sessions/manager.ts`). |
| `sessions/poolWatcher.ts` | Keeps one **passive** orchestrator WS (never sends `start`) so pool pushes arrive even with no Archie tab open (G-39 makes every orchestrator socket a watcher). Ships only if spec 12 endorses it and a laptop-backend check shows no side effects; otherwise sync stays visibility-driven. |
| `uploads.ts` | `POST /api/uploads` multipart with progress (XHR, for Safari 12 progress events). The result is inserted into the draft as a reference line. A 413 shows "File too large for the server (nginx limit 1 MB)" until the nginx fix lands (§10 D-W6). |
| `capabilitiesProbe.ts` | Backend capability probes: audio-capable models for the **selected** Archie model (fixes inv02 §6.2), and `castAvailable` (§3.7). |

`src/voice/` (W-12, no React; the UI is `features/voice`):

| Module | Responsibility |
|---|---|
| `core/VoiceController.ts` | The voice signaling state machine (`off · connecting · active · speaking · thinking · tool_use · ending · error` + `remoteActive`, VAD state). Command queue until transport/data channel is ready; forward `voice_session_update` only when initiator **and** WebRTC; 30 s wait for connection info; `response.cancel` only while a response is in flight; flush local audio on barge-in; Gemini transcript coalescing; empty-text `turnComplete` finalisation; 5 s ending timeout; passive viewers ignore lifecycle events and act only on `voice_owner_active`, **and their own status stays `off`** (fixes inv02 F-29). Voice tool calls get their results (fix F-26). Pure: transport and clock are injected, so it is unit-tested without audio. Every rule here comes from inv02 §7 #11 and is tested one by one. |
| `transports/webrtc.ts` | OpenAI: peer connection, `oai-events` data channel, SDP exchange with the ephemeral token, mirror every data-channel event as `voice_event`, execute `voice_command`s (G-32). |
| `transports/wsRelay.ts` | Qwen/Gemini: `voice_audio_in` base64 PCM16 chunks of 100 ms; play `voice_audio_out`. |
| `audio/context.ts` | `AudioContext ‖ webkitAudioContext`, resume-on-gesture. |
| `audio/capture/worklet.ts`, `audio/capture/scriptProcessor.ts` | Two `PcmCaptureSource` implementations (§2.5). |
| `audio/pcmPlayer.ts`, `audio/meters.ts`, `audio/wav.ts` | Gapless PCM16 playback with flush; RMS meters; WAV encoder. |
| `recording/sessionRecorder.ts` | WebRTC session recording (main only). |
| `recording/audioMessage.ts` | Push-to-record: MediaRecorder or the WAV fallback; 60 s cap. |

Protocol-level voice behaviour (dedicated voice socket vs. the text socket; dropping the vestigial
pre-start `stop`, inv02 §6.3 #10) follows spec 12. If spec 12 is silent, keep today's dedicated
voice WS, drop the `stop`, and verify on the laptop backend before shipping.

### 3.5 UI component kit (W-02, W-03, W-04)

Custom M3 components on CSS variables from `tokens.css`; no third-party component library
(none supports Safari 12 without `gap`). Each component lives in its own folder
(`Button/Button.tsx`, `Button.module.css`, `Button.test.tsx`, `Button.gallery.tsx`).

**Accessibility contract (all components):** correct role and `aria-*`; visible focus ring
(`:focus-visible`, polyfilled on compat); full keyboard support; icon-only buttons **require**
`aria-label` (a TypeScript-required prop, not optional); minimum 48 × 48 dp touch target (a
visual 40 dp may sit inside a 48 dp hit area); disabled = `aria-disabled` + 38 % content
opacity from tokens; `axe-core` check in every component test.

**Overlay contract (W-04):** rendered through `Portal` into `#overlay-root`; **focus trap**
(`tabbable` 6.5.0) with initial focus and **return focus to the trigger** on close; **Escape**
closes the topmost overlay only (an overlay stack in `ui/a11y/overlayStack.ts`); outside
press dismiss where M3 allows it; background `aria-hidden`; scroll lock (iOS-safe);
`role="dialog"` + `aria-modal="true"` + `aria-labelledby`; Android back gesture / browser Back
closes it (history entry, §4.5). These fix inv02 §6.2 (no Escape, no focus trap, no
`role=dialog`).

| Package | Components |
|---|---|
| **W-02 primitives** | `Stack`, `Inline`, `Cluster`, `Center`, `Box`, `Split`, `ScrollArea`, `Icon`, `StateLayer`, `FocusRing`, `VisuallyHidden`, `Spinner` |
| **W-03 actions** | `Button` (filled, tonal, outlined, text, elevated; leading icon; loading), `IconButton` (standard, filled, tonal, outlined; toggle with `aria-pressed`), `Fab` (small, regular, extended; a menu trigger for "New") |
| **W-03 inputs** | `TextField` (filled and outlined; label, supporting/error text, prefix/suffix icon, multiline auto-grow; 16 px), `SearchField`, `Select` (native `<select>` for touch and compat; an M3 menu-based exposed dropdown on pointer devices; same props), `Checkbox`, `Radio` + `RadioGroup` (arrow keys), `Switch` (`role="switch"`), `Slider` (native `input[type=range]` styled with `-webkit-` pseudos; **commits on release** via `change`, shows a value label while dragging; fixes PUT-per-tick), `SegmentedButton` (single/multi, `role="radiogroup"`/`group`) |
| **W-03 display** | `Chip` (assist, filter, input with remove, suggestion), `Badge` (dot, count), `LinearProgress`, `CircularProgress` (determinate; used for the context ring; indeterminate), `Card` (filled, elevated, outlined), `Divider`, `List`, `ListItem` (one-, two-, three-line; leading icon/avatar; trailing meta/action; selectable; renders a real `button`/`a`, fixing div-with-onClick rows), `Disclosure` (a header button with `aria-expanded` + `aria-controls`; the body is outside the button, fixing invalid button>div), `EmptyState`, `StatusDot` (idle / working / warning / disconnected, with a text alternative) |
| **W-04 overlays** | `Portal`, `Scrim`, `Popover` (anchored by `@floating-ui/react-dom` 2.1.9, flip/shift, repositions on scroll/resize, fixing detached menus), `Menu` + `MenuItem` (roving focus, type-ahead, `role="menu"`, separators, destructive items), `Dialog` (basic, confirm with destructive variant, full-screen on Compact), `BottomSheet` (modal, drag handle, swipe-down via touch events, snap to content height), `SideSheet` (modal, and standard for Expanded), `Snackbar` + `SnackbarHost` (queue from `stores/snackbar`, action, `aria-live="polite"`), `Tooltip` (plain; hover and focus; long-press on touch; never the only label), `BusyOverlay` primitive (`role=status`, `aria-busy`) |
| **W-04 navigation** | `NavigationRail` (80 dp; FAB slot; destinations with active indicator; `role="navigation"`; arrow keys), `NavigationDrawer` (modal, plus a standard variant), `Tabs` (primary/secondary; `role="tablist"`; arrow keys + Home/End; a scrollable variant; the base of the session tab strip built in W-07), `TopAppBar` (small; with a title button, subtitle line and trailing actions; same surface as content, no divider, R1), `Tree` (`role="tree"`; arrow-key expand/collapse; type-ahead; for Memory) |
| **W-04 a11y** | `useFocusTrap`, `useDismiss`, `useRovingFocus`, `useScrollLock`, `useReturnFocus`, `overlayStack`, `announce()` (shared `aria-live` region), `gestures.ts` (long-press, horizontal swipe, drag; mouse + touch) |

### 3.6 Features

| Feature | Owner | Scope (inventory reference → change) |
|---|---|---|
| `features/markdown` | W-08 | `Markdown` (static, memoized by source), `StreamingMarkdown` (§5.1), `CodeBlock` (header, copy, highlight), `gfm/autolinkLiteralSafe.ts`, `highlight/` (`highlighter.ts`, `languages.main.ts`, `languages.compat.ts`), `links.ts` (external link props; memory-relative resolution hook) |
| `features/conversation` | W-09 | `ConversationPanel` (composes MessageList + inline cards + composer/voice slot). `MessageList` (bounded window §5.2; freeze buffer; prepend restore with the 80 px / 150 px thresholds; scroll-to-bottom FAB that never overlaps text, W6). `UserMessage` (tonal bubble, fold > 25 lines, F-07). `AssistantMessage` (prose; blocks in arrival order). `ThinkingBlock` (Disclosure; expanded while streaming). `CompactDivider`. `MessageActions` (⋮ → Rewind / Fork; hidden until the session has an SDK id; `drop_last_n` from `protocol/history`). Inline cards above the composer: `PermissionCard` (ExitPlanMode wording; Approve/Reject; "type to give feedback"), `StallCard` (provider-neutral wording, fixing "No response from Claude"), `ErrorCard` (human message + detail disclosure + dismiss), (no termination card since 2026-10-10: a terminated session's tab closes, spec 12 OPEN-3). Empty states (Archie greeting + suggestion chips + voice button; agent "Start a conversation"). |
| `features/tools` | W-10 | `registry.ts` (normalized name → category, icon, `summary(input)`, `Body`, default expansion). `normalize.ts` (Qwen snake_case map; **context-aware**, so orchestrator `read_file`/`write_file` use the orchestrator renderers, fixing §6.3 #15). Categories → `--md-ext-color-tool-<category>` from tokens (`toolCategories` in `tokens.ts`). `ToolCard` (one line: icon + summary + status; expandable). Renderers: Read, Write, Edit (+ `DiffView`, lazy `diff` 9), Bash (command highlight; "Running…" before the result, fixing the empty expanded state), Glob, Grep, WebFetch, WebSearch, Task (collapsible now), TodoWrite (collapsible; shows progress "3/7"), AskUserQuestion, Skill, plan mode, NotebookEdit, agent-session tools, `send_to_agent_session`, search_history/memory, chrome-devtools (~25 phrasings from F-05), generic MCP, default JSON. Output: `<pre>` with ANSI stripped, the first 200 lines / 20 KB shown, then "Show all". **`StepGroup`**: consecutive tool calls (≥ 2) form an "N steps" stack, **expanded while live, collapsed to a one-line summary once text follows**; every card stays individually expandable (IA §9.3, approved). A device pref turns grouping off. |
| `features/composer` | W-11 | `Composer` (one 28 dp container: leading ＋ menu → Attach file (upload), Voice message, Slash commands (inserts `/skill` from `GET /api/skills`); multiline 16 px field; context ring (tap → Compact, colours at 50 % / 80 %); 🎙 audio-message button (Archie, when the selected model is audio-capable); **morphing primary button**: Voice (Archie, empty) → Send (text) → Stop (working and empty). Enter sends, Shift+Enter is a newline; Enter while working sends a queued message. Draft per session survives tab switches and reloads). |
| `features/session-actions` | W-11 | The ⋮ session menu: Rename, Session settings (opens the W-13 sheet), Compact context, Fork, Close, Delete. Confirm dialogs: close-while-running, delete (closes the tab by `local_id`, fixing §6.3 #8), rewind (correct copy, fixing §6.2), fork. `BusyOverlay` host. Rewind order close → truncate → reopen (**[LOAD-BEARING]** F-09). New-Archie flow ("Archie already active" dialog, F-25). Viewing a past Archie conversation while one is open shows it read-only (fixes §6.3 #11). |
| `features/voice` | W-12 | `VoiceDock` replaces the composer while voice is active: level orb, state label (Listening · Speaking · Thinking · Using tools), **VAD "Listening Ns" counter** (fixes §6.3 #4), mic mute, speaker mute, end. `ActiveElsewhere` read-only dock with transcripts still mirroring and the text input usable (IA §6). Typed `voice_error` envelope rendered with recovery hint (fixes §6.4). `VoiceOverlay`: while this device has a call, the same controls float over every non-Archie view (z-index 19, under the modal layers), fade to a pill after 4 s idle, drag-to-snap with a per-device anchor pref; see `docs/voice/architecture.md` "Floating voice controls". |
| `features/settings` | W-13 | IA §7 hierarchy. Web pages: **This device** → Appearance (theme System/Dark/Light, text size, reduce motion, syntax highlighting, tool step grouping), Notifications (agent turn finished, spec 12 §8.2), Remote logging. **Archie (server)** → Conversation model, Voice, Voice tuning, Agent sessions, Working directories (full CRUD incl. SSH, defensive coercion **[LOAD-BEARING]** F-33), MCP servers ("all enabled" semantics shown correctly, fixing §6.2), Accounts (every service's sign-in through every method + the `context/.env` key manager; `features/settings/accounts/`, spec 12 §8.1). **About** (app version, build target, backend host). Expanded: two-pane list/detail; Compact: pushed pages. Every save → snackbar "Saved" or the verbatim server error + Retry. Google voice model auto-correct (**[LOAD-BEARING]** F-31). `SessionSettingsSheet` (side sheet on Expanded, bottom sheet on Compact; `null` = inherit; "Save and restart"). Android-only pages (Connection, Audio, Wake word) are not rendered on web. |
| `features/auth` | W-13 | `AuthGate` (status → "Sign in with Claude" or manual credentials paste; headless path), the Claude panel is also reachable from Settings → Accounts. |
| `features/history` | W-14 | `HistoryPane` (list pane for "Chats" and drawer body): search (client-side title filter), **Open now** section (pool sessions with status dot), date groups Today / Yesterday / Previous 7 days / Earlier, provider chips ("Claude", "Qwen", "Gemini"), Archie mark, rename / duplicate / delete actions (menu on touch, hover icons on pointer). |
| `features/memory` | W-14 | `MemoryPane` (Tree + search over names/paths, refresh), `MemoryDocument` (frontmatter in a collapsed disclosure, **[LOAD-BEARING]** F-37; relative links open in-app; Reload; Open raw). |
| `features/visuals` | W-14 | `VisualsPane` (cards: title, project folder, age; rename), `VisualViewer` (iframe with the **exact sandbox tokens** `allow-scripts allow-same-origin allow-popups allow-forms allow-modals`; Reload remounts via `key`; Open in new tab; stays mounted while hidden, **[LOAD-BEARING]** F-36), **Show on TV** (§3.7). |

### 3.7 "Show on TV" (pending backend approval)

IA §9.4 (approved) adds "Show on TV" for visuals. It depends on a **proposed additive endpoint**,
`POST /api/visualizations/cast {path}`, which wraps the existing TV display script. The backend
change is listed for approval, not assumed.

- Availability probe (W-06 `capabilitiesProbe.ts`): `GET /api/visualizations/cast` (as shipped in backend BX-2) →
  `{available: true, tv_connected: bool}`, **proposed together with the endpoint**. Any other
  answer (404, a `text/html` SPA-fallback 200, network error) means "unavailable", and the action
  is **hidden**, not disabled. Probed on app start and on visibility.
- Action (W-14): in `VisualViewer`'s toolbar and the visual card's menu. Snackbar "Showing on TV"
  / server error verbatim. If `tv_connected` is false: the snackbar says "TV not connected".
- If the backend owner prefers no status route, the fallback probe is `OPTIONS` on the POST
  route; this is decided with the backend proposal.

### 3.8 Icons and fonts

**Icons (W-02):** Material Symbols **Rounded**, weight 400, from `@material-symbols/svg-400`
0.47.6 (dev dependency, 23,565 files; never bundled wholesale). `src/ui/icons/icons.manifest.json`
lists every icon name used (plus `-fill` variants for selected navigation states).
`npm run icons` reads `node_modules/@material-symbols/svg-400/rounded/<name>.svg`, extracts
the path data, and writes `src/ui/icons/generated.ts` (`export const iconPaths = {…} as const`
and `type IconName`). `<Icon name="terminal" />` renders a 24 × 24 inline SVG with
`currentColor` and `aria-hidden` by default. An unknown name is a **type error**, and only listed
icons are in the bundle. Icon names are the Material Symbols names, **the same names the Android
app uses** (R2). W-02 seeds the manifest with the icons this spec names (tool icons from F-05,
navigation, composer, voice, settings). Other packages add names through the coordinator, or
W-02 accepts a manifest-only edit request; `generated.ts` is regenerated, never hand-edited.

**Fonts (W-02):** our own `@font-face` rules in `src/styles/fonts.css`, using the family names in
`tokens.css` (`"Roboto Flex"`, `"JetBrains Mono"`):

| Face | File (from the Fontsource package) | Size | Loading |
|---|---|---|---|
| Roboto Flex, wght 100–1000, latin | `roboto-flex-latin-wght-normal.woff2` | 34 KB | `<link rel=preload>` |
| Roboto Flex, latin-ext | `roboto-flex-latin-ext-wght-normal.woff2` | small | by `unicode-range` |
| JetBrains Mono, wght, latin | `jetbrains-mono-latin-wght-normal.woff2` | 40 KB | on first code use |

`font-display: swap` throughout. The `opsz` axis file (84 KB) is **not** used in v1; optical
sizing is a later, budget-gated option. Portuguese text is covered by the latin subset.

### 3.9 Entry-point contracts (for parallel work)

Dependents import only these names. Props are TypeScript interfaces in each `index.ts`. Stubs
are allowed until the owner delivers.

| Entry | Exports (props in brief) |
|---|---|
| `@/protocol` | wire types, `decode`, `reduceConversation`, `initialConversation`, history converters, `computeDropLastN`, checkpoint helpers, selectors (`groupToolSteps`, `isBusy`) |
| `@/services` | `getSessionRuntime(localId)`, `openSession(opts)`, `closeSession(localId)`, `api.*` endpoint functions, `uploadFile(file, onProgress)` |
| `@/stores` | `useTabs`, `useSession(localId, selector)`, `useCatalog`, `useServerConfig`, `usePrefs`, `useCapabilities`, `useSnackbar`, `useConnection` |
| `@/voice` | `getVoiceController(localId)` (`start/stop/mute`, subscribe), capability helpers |
| `@/ui/*` | components per §3.5 |
| `@/features/markdown` | `Markdown{source, linkResolver?}`, `StreamingMarkdown{source, streaming}`, `CodeBlock{code, lang}`; under an `InternalLinksProvider`, viz / memory links open in the app and printed paths auto-link (spec 12 §9.4) |
| `@/features/links` | `resolveInternalLink(href, ctx)`, `linkableCode`, `findBarePaths`, `internalTargetUrl`, `InternalLinksProvider{value: {context, open, hrefOf}}` (spec 12 §9.4). No markdown renderer: the app root provides it without growing the initial bundle (§5.4) |
| `@/features/conversation` | `ConversationPanel{localId, hidden}` |
| `@/features/tools` | `ToolCard{block, sessionKind}`, `StepGroup{blocks, live}` |
| `@/features/composer` | `Composer{localId}` |
| `@/features/session-actions` | `SessionMenu{localId}`, `useSessionActions(localId)`, `NewMenu{}` |
| `@/features/voice` | `VoiceDock{localId}`, `useVoiceUi(localId)`, `VoiceOverlay{localId, regionRef, getAvoid?, layoutKey?, compact, onOpenConversation}` (floating controls during a call, mounted by `app/shell/VoiceOverlayHost`), `useLiveVoiceId(archieIds)` |
| `@/features/settings` | `SettingsScreen{page?}`, `SessionSettingsSheet{localId, open, onClose}` |
| `@/features/auth` | `AuthGate{children}` |
| `@/features/history` | `HistoryPane{variant: 'pane' | 'drawer' | 'switcher'}` |
| `@/features/memory` | `MemoryPane{}`, `MemoryDocument{path, hidden}` |
| `@/features/visuals` | `VisualsPane{}`, `VisualViewer{path, url, hidden}` |

---

## 4. Adaptive layout

### 4.1 Window size classes

`src/app/useWindowClass.ts` reads `matchMedia` with the breakpoints from IA §0: **compact**
< 600, **medium** 600–839, **expanded** ≥ 840 CSS px. It uses `addListener` on Safari 12 (§2.6).
CSS uses the same breakpoints through `@custom-media`. JS drives structure (rail vs. drawer);
CSS drives dimensions. The iPad mini 2 is **medium** in portrait (768) and **expanded** in
landscape (1024).

### 4.2 Shell structure (W-07)

```
<AuthGate>
  <AppShell data-wc={wc}>                                  fixed, full --app-height
    {wc !== 'compact' && <NavigationRail/>}                ＋ New (FAB→Menu) · Chats · Memory · Visuals · Settings
    {wc === 'expanded' && listPaneOpen && <ListPane/>}     320 dp, standard; HistoryPane / MemoryPane / VisualsPane
    <main class="workspace">                               ← ALWAYS the same element, same tree position
      <WorkspaceTopBar/>                                   expanded/medium: tab strip in the top app bar; compact: ☰ · title⌄ · status · voice · ⋮
      <PanelHost/>                                         every open tab's panel, mounted; active one visible
    </main>
    {wc === 'medium' && <ListPaneOverlay/>}                modal side sheet next to the rail
    {wc === 'compact' && <NavigationDrawer/>}              modal; header (Archie mark, connection), search, Open now, history, Memory/Visuals/Settings
    <ScreenLayer/>                                         compact full screens (Memory, Visuals, Settings, documents) stacked ABOVE the workspace
    <SessionSwitcherSheet/> <SnackbarHost/> <div id="overlay-root"/>
  </AppShell>
</AuthGate>
```

| Size | Navigation | List pane | Tabs | Memory / Visuals docs |
|---|---|---|---|---|
| Expanded | Rail | Standard, collapsible (toggle in top bar; remembered in prefs) | Tab strip in the top app bar | **Open as tabs** (IA §9.2, kept) |
| Medium | Rail | Modal overlay from the rail or a top-bar button | Tab strip; "All tabs" overflow menu kicks in earlier | As tabs |
| Compact | Drawer (☰) | Drawer body | **Session switcher bottom sheet** from the title (IA §9.1, approved; no bottom bar) | Detail screens in `ScreenLayer` (back arrow) |

Tab strip (fixes R3, W1): 40 dp tabs inside a 48 dp hit area, a leading kind icon (Archie mark /
provider / memory / visual), a status indicator (spinner working, dot idle, warning
disconnected; connection state is styled, fixing §6.1), title **derived from the session list**
(**[LOAD-BEARING]** §7 #9), close × always visible on the active tab and on hover, middle-click
close, double-click / context menu / long-press → Rename, drag to reorder (Archie pinned first),
**⌄ All tabs** menu with search when the strip overflows, an "unseen" badge for background
opens. Right side: status text ("Thinking…", "Using Bash…"; turns and cost in a tooltip) and the
⋮ session menu.

Compact top bar: the title button opens the switcher sheet; horizontal swipe on the title
switches open sessions (touch events, optional on web); the subtitle shows live status.

### 4.3 Keyboard (expanded)

The IA's Ctrl+Tab, Ctrl+Shift+Tab, Ctrl+W and Ctrl+1…9 are **reserved by browsers**. A page in
a normal Chrome tab never receives them, and Linux Chrome also uses Alt+1…8. Proposed bindings
(`src/app/keyboard/bindings.ts`, one table; §10 D-W5):

| Action | Browser tab | Installed PWA window |
|---|---|---|
| Next / previous tab | Ctrl+Alt+→ / Ctrl+Alt+← | also Ctrl+Tab / Ctrl+Shift+Tab when delivered |
| Close tab | Ctrl+Alt+W | also Ctrl+W when delivered |
| Go to tab N | Ctrl+Alt+1…9 | also Ctrl+1…9 when delivered |
| Focus composer | `/` (when not typing) | same |
| New Archie / agent | Ctrl+Alt+N / Ctrl+Alt+Shift+N | same |
| Escape | closes the topmost overlay | same |

### 4.4 All tabs stay mounted (must-preserve inv02 §7 #1)

- **Connections live outside React.** `SessionRuntime`, `ArchieRuntime` and `VoiceController`
  are services in the session registry. They survive any React re-render, layout change or
  panel visibility change. A runtime is disposed only when its tab is closed.
- **Panels stay mounted.** `PanelHost` renders one panel per open tab, keyed by tab id, and hides
  inactive ones with the `hidden` attribute (`display: none`). Scroll position, the composer
  draft, expanded/collapsed card state and **visual iframes** (**[LOAD-BEARING]** F-36) survive
  tab switches.
- **Stable tree position.** `<main class="workspace">` and `PanelHost` have the same parent and
  index in every window class. Conditional siblings render `false` placeholders, which keep the
  index stable. Rotating the iPad or resizing the desktop window **never remounts panels**. A
  component test asserts DOM-node identity of the panel and the iframe across a window-class
  change and across tab switches.
- **Compact screens overlay; they do not replace.** On Compact, Memory/Visuals/Settings open in
  `ScreenLayer` above the workspace. The workspace stays mounted beneath with `aria-hidden`.
- **Frozen while hidden.** A hidden panel receives `hidden=true`. Its store subscriptions return
  the last published snapshot, so background streaming costs reducer time only, not React render
  or layout. On show, one catch-up render happens; then the "tab activation" scroll rule applies
  (bottom if the user was near it, F-11). This replaces React 19's `<Activity>`.
- **Memory budget on compat:** each hidden panel keeps at most its DOM window (§5.2). A hidden
  panel's window is trimmed to 40 messages after 60 s hidden (the trim happens above the viewport
  and the anchor is restored on show). This bounds memory on the 1 GB iPad without unmounting.

### 4.5 Navigation state and Back

No router library. `src/app/navigation/` keeps a small store mirrored to the **URL hash**, which
works under any base path (`/`, `/compat/`, `/next/`) and needs no server fallback:
`#/` (workspace), `#/history`, `#/memory[/<path>]`, `#/visuals[/<path>]`,
`#/settings[/<page>]`, `#/dev/gallery`. The active tab is not in the URL. Overlays (drawer,
sheets, dialogs, Compact screens) push a history entry and close on `popstate`, so the Android
back gesture and the browser Back button close the topmost layer first. A history-stack helper
prevents double pops.

---

## 5. Performance

### 5.1 Streaming render cost

1. **Coalesced notifications:** reducer per event; React notified once per frame (or per
   100 ms on low-end) (§3.3).
2. **Structural sharing + narrow selectors:** only the last message re-renders during a stream.
   Each block component is `React.memo` on its block object identity.
3. **Incremental markdown (`StreamingMarkdown`):** while `streaming`, the source is split into
   top-level chunks by `splitBlocks()` (blank-line boundaries outside open code fences ``` and
   ~~~, and never inside a table or list run). Every chunk except the last is a memoized
   `<Markdown>` keyed by `index + hash(source)`, so a delta re-parses only the tail chunk. When
   the block completes (`text_complete` is authoritative), one full parse replaces the chunks,
   so the final output is exact CommonMark (for example reference-style links that the split
   view cannot resolve).
4. **Highlighting** only for closed fences, after streaming (§2.4). Open fences render as plain
   monospace.
5. **Auto-scroll keyed on content height, not block count** (fixes inv02 §6.2): `MessageList`
   observes the tail's height (ResizeObserver, polyfilled on compat) and pins to bottom only when
   near the bottom and no touch/momentum is active.
6. **Tool output** is truncated (§3.6); collapsed cards do not render their bodies.

### 5.2 Long conversations: a bounded DOM window, no virtualizer

`@tanstack/react-virtual` is **not used**. Variable-height items that grow while streaming,
prepend pagination, the freeze buffer and iOS 12 momentum scroll (programmatic `scrollTop`
changes during momentum jump or are ignored) together make virtualizer anchoring fragile on the
one device that needs it most.

Instead, `useMessageWindow` (W-09) renders a **slice** `[start, end)` of the session's messages:

- `end` is the tail, except while the **freeze buffer** holds (user scrolled up; new messages
  are buffered, not rendered, **[LOAD-BEARING]** F-11).
- When the slice exceeds the cap (200 main, 80 low-end) **and** the user is at the bottom,
  `start` advances (trimming from the top) and the list re-pins to the bottom in the same frame,
  with no visible jump.
- Scrolling up past the top of the slice first **re-expands from memory**, then pages from REST
  (`before=start_index`). Both use the **same** `ScrollArea.preserveAnchor()` path (hide-for-frame
  + offset restore under momentum-safe scrolling). One code path, already load-bearing.
- On main, `content-visibility: auto; contain-intrinsic-size: auto 120px` on message rows reduces
  paint further. Safari 12 ignores it harmlessly.

If the performance budgets (§5.4) fail on main with very long sessions, a virtualizer may be
added to main later behind the same `MessageList` props. Compat keeps the window.

### 5.3 Other costs

- Code splitting (`React.lazy`): Settings, the Memory document view, the Visual viewer, `DiffView`
  + `diff`, highlight languages, the voice engine, the dev gallery (excluded from production).
  On compat, plugin-legacy turns these into SystemJS chunks, which still load lazily.
- `Markdown` and tool renderers do no work during render beyond parsing; derived data
  (summaries, grouping) is memoized selectors.
- Remote logging is rate-limited (§1.4).

### 5.4 Budgets (enforced by `gate:budgets`)

`scripts/check-budgets.mjs` reads each build's Vite manifest, gzips every emitted file
(`zlib`, level 9) and compares with `scripts/budgets.json`. Over budget fails the build. A budget
change needs a one-line justification in the file.

| Budget (gzip) | main | compat |
|---|---|---|
| Initial JS (entry + static imports) | ≤ 190 KB | ≤ 270 KB (legacy transpile + SystemJS) |
| Polyfills chunk | — | ≤ 60 KB |
| Initial CSS | ≤ 35 KB | ≤ 40 KB |
| Fonts on first paint (woff2, uncompressed) | ≤ 80 KB | ≤ 80 KB |
| Largest lazy chunk | ≤ 60 KB | ≤ 75 KB |
| Total JS | ≤ 480 KB | ≤ 560 KB |

Runtime targets (measured in W-15 with the chrome-devtools MCP performance trace; compat
proxied in Chrome with 6× CPU throttling, plus a real iPad timing beacon):

| Scenario | Target |
|---|---|
| iPad mini 2 cold load of `/compat/` to first rendered conversation (LAN) | ≤ 4 s (logged by a `perf` remote-console line at first render) |
| Laptop cold load of `/` | ≤ 1.5 s to first conversation render |
| Streaming 30 deltas/s into a 2,000-word reply | main: ≤ 25 % main-thread busy at 4× throttle; compat proxy: ≤ 50 % at 6× throttle |
| Open a session with 50 history messages | ≤ 300 ms scripting (laptop, unthrottled) |
| 5 open sessions × 200 messages | ≤ 150 MB JS heap (Chrome heap snapshot) |
| Tab switch | ≤ 100 ms to visible (no remount) |

---

## 6. Testing

### 6.1 Unit tests (Vitest 5.0.3)

`vitest.config.ts` defines three projects:

| Project | Environment | Covers |
|---|---|---|
| `protocol` | node | `src/protocol/**`: **fixture conformance suite** (every fixture, no skips), reducer invariants (property-style tests for "tool result never lost" R7 and arrival order R4), history conversion, `drop_last_n`, checkpoint logic |
| `dom` | jsdom 30.1.1 | services (fake WebSocket + fake timers for reconnect 2 s × 10, visibility pause and `start` re-send), stores, `voice/core` (**voice signaling state**: every rule of inv02 §7 #11 as a named test; passive-viewer status stays `off`; command queue; 5 s ending timeout; 30 s connection-info timeout), `useMessageWindow` + freeze buffer, `splitBlocks`, components (§6.2) |
| `compat` | jsdom, **compat aliases** (`languages.compat.ts`) + `regexpGuard.ts` | renders the markdown corpus (`src/features/markdown/__fixtures__/*.md`: tables, inline formatting in paragraphs, autolinks, task lists, every compat language) under the RegExp guard; asserts inline formatting survives (fixes §6.3 #12) |

Coverage gate (v8): `src/protocol` ≥ 95 % lines, `src/voice/core` ≥ 90 %, `src/services` ≥ 80 %.

### 6.2 Component tests

`@testing-library/react` 16.3.3 + `user-event` 14.6.7 in the `dom` project:

- Every kit component: roles, keyboard (Tab, arrows, Home/End, Escape, Enter/Space), focus trap
  and return focus for overlays, `aria-*` states, and an `axe-core` 4.13.0 run with no violations.
- Feature components against stores seeded from protocol fixtures: tool cards per tool type
  (snapshot of the summary line + expanded body), StepGroup live → collapsed, permission card
  flows, server close / termination closes the tab with a notice (spec 12 OPEN-3), composer morphing button states, settings save → snackbar
  (verbatim server error), memory frontmatter + relative links, viz sandbox tokens.
- Must-preserve tests: panel and iframe DOM identity across tab switch and window-class change
  (§4.4); history init not re-run on `resumeSdkId`; derived tab titles.

### 6.3 Gates

| Gate | Command | Fails on |
|---|---|---|
| Type-check | `npm run typecheck` | Any TS error in any project (strict, `noUncheckedIndexedAccess`). No tolerated-error filters (unlike today's compat script) |
| ESLint | `npm run lint` | typescript-eslint strict; react-hooks; jsx-a11y; `es-x` regex rules; `eslint-plugin-compat` (compat browserslist; polyfilled APIs declared); restricted syntax/properties of §2.6; `no-restricted-imports` boundaries (protocol purity; features import other features only via `index.ts`) |
| Stylelint | `npm run lint:css` | `stylelint-config-standard`; `property-disallowed-list` (`gap`, `row-gap`, `column-gap`, `aspect-ratio`); `selector-pseudo-class-disallowed-list` (`is`, `where`, `has`); `function-disallowed-list` (`color-mix`, `clamp`); `unit-disallowed-list` (`dvh`, `svh`, `lvh`); `at-rule-disallowed-list` (`layer`, `container`); `stylelint-no-unsupported-browser-features` (compat browserslist) as a warning pass |
| Tokens | `npm run gate:tokens` | Stale token dist; unknown `var(--md-…)`/`var(--app-…)` |
| Compat scanner | `npm run gate:compat` | §2.3 |
| Budgets | `npm run gate:budgets` | §5.4 |

### 6.4 Visual QA with the chrome-devtools MCP

- **One browser at a time** (charter working rules): one chrome-devtools page, against one dev or
  preview server; `close_page` when done; never two agents driving browsers concurrently (the
  coordinator serializes QA slots). No Playwright or Vitest browser mode in this project.
- **Data source:** the mock server (§6.5) by default, so screenshots contain no private
  conversations and are reproducible. The live Jetson only read-only, and **never** open Archie
  or voice sessions there without telling Rodrigo.
- **Viewports:**

| Name | Size | Emulation | Build |
|---|---|---|---|
| desktop | 1440 × 900, DPR 1 | — | main |
| tablet | 768 × 1024, DPR 2, touch, iOS 12 Safari UA, `?caps=compat` | iPad mini 2 portrait | **compat** |
| phone | 412 × 915, DPR 2.625, touch, Android Chrome UA | — | main |

  The tablet set is also taken in landscape (1024 × 768) for screens with layout changes.
- `?caps=compat` forces the compat capability set in Chrome (no AudioWorklet, no MediaRecorder, no
  Clipboard API), so hidden or fallback UI shows in screenshots. Chrome cannot reproduce Safari
  12's parser or CSS gaps; those are covered by gates (§6.3) and the device checklist.
- Screenshots go to `fn/qa/screenshots/<W-id>/<viewport>-<screen>-<state>.png` (gitignored),
  listed in the work package's QA note `fn/qa/<W-id>.md` with what was checked.
- **Device checklist** (`fn/qa/device-checklist.md`, run on the real iPad mini 2 and a phone via
  `/next-compat/` and `/next/` or Option A): load, rotate, scroll a long conversation with
  momentum, prepend history, streaming, open/close every overlay, code copy, audio message (WAV),
  voice start/stop (when in scope), visuals iframe, memory links, remote console shows no
  errors (`GET /api/debug/log`).

### 6.5 Mock backend (W-05)

`fn/mock-server/server.mjs` (Node + `ws` 8.22.0, port 8799): serves REST fixtures (sessions,
pool/live, messages pages, config, models, memory tree and files, visuals, auth status) from
`fn/mock-server/data/` (synthetic content), and the chat and orchestrator WebSockets. On `start`
it replays a chosen `shared/protocol-fixtures` transcript, selected by `?scenario=` or by
`local_id` prefix, with real timing or `--fast`. It emits **binary** frames and accepts text
frames only, like the backend (G-2). Used by dev, QA screenshots and the `dom` tests' fake
socket fixtures.

### 6.6 Definition of done (every work package)

1. `npm run verify` green (lint, CSS lint, type-check, all tests, both builds, compat scanner,
   budgets).
2. Tests listed in the package's DoD exist and pass.
3. Screenshots at the three viewports for every UI state the package adds, with the QA note.
4. `git diff --stat` shows changes only inside the package's boundary.
5. Every ported load-bearing behaviour carries a source citation comment
   (`// LOAD-BEARING inv02 F-11 (frontend/src/components/MessageList.tsx:36-90)`).
6. No new dependency without coordinator approval.

---

## 7. Work breakdown

Sizes: **S** ≤ 1 agent-day, **M** 1–2, **L** 2–4, **XL** 4–6 (rough, including tests and QA).

| ID | Title | Size | Depends on | Wave |
|---|---|---|---|---|
| W-01 | Scaffold, builds and gates | M | — | 0 |
| W-05 | Protocol client, conformance suite, mock server | L | W-01 skeleton; spec 12 + fixtures | 0–1 |
| W-15 | Backend preview route → hardening → cutover | S + M | approval (D-W2); later all | 0 / 5 |
| W-02 | Styles foundation: tokens, fonts, icons, primitives, ScrollArea, gallery | M | W-01 | 1 |
| W-08 | Markdown and code rendering | M | W-01 (W-02 for final styling) | 1 |
| W-03 | UI kit I: actions, inputs, display | L | W-02 | 2 |
| W-04 | UI kit II: overlays, navigation, a11y | L | W-02 | 2 |
| W-06 | Services and stores | L | W-05 | 2 |
| W-07 | App shell, adaptive layout, tabs, navigation | L | W-04, W-06 (W-03) | 3 |
| W-10 | Tool cards | L | W-03, W-04, W-08, W-05 | 3 |
| W-13 | Settings, session settings, auth | L | W-03, W-04, W-06 | 3 |
| W-14 | History, Memory, Visuals | M | W-03, W-04, W-06, W-08 | 3 |
| W-09 | Conversation view and inline cards | L | W-06, W-08, W-10 (stub ok), W-03/04 | 4 |
| W-11 | Composer and session actions | M | W-06, W-03/04, W-13 entry | 4 |
| W-12 | Voice engine and voice UI | XL | W-05, W-06, W-03/04 | 3–4 |

Parallelism: wave 0 runs 3 agents; wave 2 runs 3; wave 3 runs up to 5 (W-07, W-10, W-13, W-14,
W-12 engine); wave 4 runs 2–3. Boundaries are disjoint directories, and entry-point stubs (§3.9)
let a dependent start before its dependency finishes.

### W-01 — Scaffold, builds and gates

- **Goal:** a working two-build package with every gate in place before feature code lands.
- **Inputs:** §1, §2.3, §2.6–2.7, §5.4, §6.3; inv02 §1.2, F-38; inv01 §1.4.
- **Boundary:** `fn/package.json`, `package-lock.json`, `index.html`, `vite.*.ts`,
  `vitest.config.ts`, `tsconfig*.json`, `.browserslistrc`, `eslint.config.js`,
  `stylelint.config.mjs`, `.gitignore`, `public-main/`, `public-compat/`, `scripts/` (except
  `gen-icons.mjs`), `src/main.tsx`, `src/env.d.ts`, `src/platform/`, `src/test/`, a placeholder
  `src/app/App.tsx` (handed to W-07 at its start).
- **Steps:** install all §1.2 dependencies; both Vite configs; HTML target plugin (remote console
  ES5 script with rate limit); platform modules; scanner + allowlist + scanner tests; budget and
  token-var scripts; RegExp guard; lint configs.
- **Spike (go/no-go, first day):** a hello-world compat build (React 18 render + a regex + a
  `ResizeObserver` use) loads on the **real iPad** (Option A preview). If Vite 8 + legacy 8 fails,
  switch to Vite 7.3.6 + plugin-legacy 7.2.1 + plugin-react 5.x and record it in §1.2.
- **DoD:** `npm run verify` green on the hello page; scanner unit tests cover the §2.3 fixture
  list; deliberately inserting `/(?<=a)b/` or `gap: 8px` makes the build fail; the iPad shows the
  page and its remote-console beacon arrives; screenshots of the hello page at three viewports.

### W-02 — Styles foundation: tokens, fonts, icons, primitives, ScrollArea, gallery

- **Goal:** the visual base every component builds on.
- **Inputs:** §2.1, §2.2, §2.6 (fonts, scrolling, viewport), §2.7, §3.8; `design/tokens/dist/*`.
- **Boundary:** `src/styles/`, `src/ui/primitives/`, `src/ui/icons/`, `scripts/gen-icons.mjs`,
  `src/dev/gallery/` (shell + `sections/primitives.gallery.tsx`).
- **DoD:** tokens imported (dark default; `data-theme` light/system switch in `theme.ts`, which
  also updates `theme-color`); `@font-face` with token family names; icon generator with a seeded
  manifest; primitives with the text-node wrap; `ScrollArea` imperative API with unit tests
  (momentum-safe scroll sequence, anchor preservation); gallery route; screenshots of gallery
  primitives, typography and color roles in dark and light at three viewports.

### W-03 — UI kit I: actions, inputs, display

- **Goal:** all non-overlay M3 components from §3.5.
- **Inputs:** §3.5, §2.6 (16 px inputs, no pointer events), tokens.
- **Boundary:** `src/ui/controls/` (one folder per component, including gallery sections).
- **DoD:** each component has tests (roles, keyboard, axe) and a gallery section covering every
  variant and state (enabled, hover, focus, pressed, disabled, error); Slider commits on release
  (test); Select native on touch and compat; screenshots of the gallery sections at three
  viewports in dark and light.

### W-04 — UI kit II: overlays, navigation, a11y

- **Goal:** overlays, navigation components and the accessibility infrastructure.
- **Inputs:** §3.5 (overlay contract), §2.6 (scroll lock, gestures), §4.5 (Back closes overlays).
- **Boundary:** `src/ui/overlays/`, `src/ui/navigation/`, `src/ui/a11y/`.
- **DoD:** tests for focus trap, return focus, Escape on the topmost only, outside dismiss,
  roving focus, Tree keyboard model, Popover repositioning, BottomSheet swipe (touch events),
  overlay-stack history integration; axe clean; gallery sections; screenshots (menus, dialogs,
  sheets, rail, drawer, tabs, snackbar) at three viewports.

### W-05 — Protocol client, conformance suite, mock server

- **Goal:** the framework-free TS implementation of spec 12, proven against all fixtures.
- **Inputs:** `spec/12-client-protocol.md`, `shared/protocol-fixtures/`, inv01 §4–§7, inv02 §4.
- **Boundary:** `src/protocol/`, `tsconfig.protocol.json` (created by W-01, content owned by
  W-05), `mock-server/`.
- **DoD:** conformance suite runs **every** fixture and passes; protocol coverage ≥ 95 %;
  `tsc -p tsconfig.protocol.json` passes with no DOM lib; the mock server replays at least: a plain
  chat turn, interleaved text and tools, a permission flow (ExitPlanMode), a stall, a termination,
  a reconnect with replay and with `replay_overflow`, an Archie turn with parallel tools, and a
  voice transcript mirror.

### W-06 — Services and stores

- **Goal:** everything between the wire and the UI: REST, sockets, runtimes, pool sync, stores.
- **Inputs:** §3.3, §3.4 (services part), §3.7 (probe); inv02 F-01, F-19…F-24; inv01 §2, §3,
  G-1…G-40.
- **Boundary:** `src/services/`, `src/stores/`.
- **DoD:** tests with a fake WebSocket and fake timers: reconnect 2 s × 10, paused while hidden,
  `start` re-sent on open and on visibility, in-memory checkpoint (spec 12 T-10), replay
  overflow → REST reload, adoption of `session_started.session_id`, pool sync without focus
  stealing, `agent_session_closed` with `is_orchestrator`, `ApiError.detail` verbatim, HTML 413
  mapped, frame coalescing (one notify per frame), frozen-while-hidden snapshots, cast probe
  (available / HTML fallback / 404). A headless demo against the mock server logs a full turn.

### W-07 — App shell, adaptive layout, tabs, navigation

- **Goal:** the IA's frame on all three size classes, with all tabs kept mounted.
- **Inputs:** §4 (all), IA §2–§5, inv02 §1.4–§1.7, F-23, F-25 (single Archie tab).
- **Boundary:** `src/app/` (takes over the W-01 placeholder `App.tsx`).
- **DoD:** rail / list pane / drawer / switcher / screen layer per size class; tab strip with all
  §4.2 behaviours; hash navigation and Back closing layers; keyboard table; tests for DOM identity
  across tab switches and window-class changes, rename/close/reorder, unseen badge; screenshots of
  each size class (empty, 3 tabs, overflow menu, drawer open, switcher open, list pane collapsed;
  iPad portrait and landscape).

### W-08 — Markdown and code rendering

- **Goal:** one markdown pipeline for both builds, fast while streaming.
- **Inputs:** §2.4, §5.1 (items 3–4); inv02 F-03, §5.3 remark-gfm shim, §6.2.
- **Boundary:** `src/features/markdown/`.
- **DoD:** autolink-safe fork with tests matching stock remark-gfm's output on the corpus (run
  stock on main only as an oracle in tests); compat project renders the corpus under the RegExp
  guard; per-build language registries; CodeBlock copy with clipboard fallback; `splitBlocks` tests
  (fences, tables, lists); final full parse on completion; memory link resolver hook;
  screenshots of the corpus at three viewports.

### W-09 — Conversation view and inline cards

- **Goal:** the message column: rendering, scrolling, actions, cards, empty states.
- **Inputs:** §3.6 (conversation), §5.1–§5.2; inv02 F-02, F-04, F-07…F-15, F-40; IA §6.
- **Boundary:** `src/features/conversation/`.
- **DoD:** bounded window with freeze buffer, prepend restore, thresholds 80/150 px (tests);
  rewind/fork with server-derived `drop_last_n`; permission card (request_id matching, typing =
  deny-with-feedback), stall, error, termination (tab kept); empty states; screenshots for: long
  conversation, streaming, scrolled up with buffered messages, each card, empty Archie and agent,
  at three viewports; mock-server streaming trace within §5.4 budgets.

### W-10 — Tool cards

- **Goal:** every tool type rendered compactly and correctly, never missing output (R7).
- **Inputs:** §3.6 (tools); inv02 F-05, F-06, §6.2–§6.3; IA §6, §9.3; `tokens.ts`
  `toolCategories`.
- **Boundary:** `src/features/tools/`.
- **DoD:** registry tests for name normalization (incl. orchestrator `read_file`), category,
  icon and summary for every tool in F-05; renderer tests per tool; StepGroup expanded while live
  and collapsed once text follows; Bash "Running…" state; ANSI strip + truncation; lazy DiffView;
  gallery section with every tool in running, done and error states; screenshots at three
  viewports.

### W-11 — Composer and session actions

- **Goal:** input, attachments, audio messages, and every session-level action.
- **Inputs:** §3.6 (composer, session-actions), §2.5 (audio messages), §2.6 (16 px, iOS keyboard);
  inv02 F-08…F-10, F-16, F-17, F-20…F-22, F-25; IA §6.
- **Boundary:** `src/features/composer/`, `src/features/session-actions/`.
- **DoD:** morphing primary button state tests; queued send while working; draft persistence;
  upload flow with progress and 413 handling; audio message via MediaRecorder and WAV fallback
  (`?caps=compat`); context ring thresholds; rewind order close → truncate → reopen (test with a
  mocked API); delete closes the tab by `local_id`; screenshots of composer states and dialogs at
  three viewports; iOS keyboard check on the iPad.

### W-12 — Voice engine and voice UI

- **Goal:** realtime voice for Archie (WebRTC and WS relay), multi-device ownership, the dock.
- **Inputs:** §2.5, §3.4 (voice), §3.6 (voice UI); inv02 F-26…F-30, §6.3 #1–#4, #10; inv01 §7,
  G-21, G-28…G-35; spec 12 voice sections.
- **Boundary:** `src/voice/`, `src/features/voice/`, `public-main/pcm-capture-worklet.js`
  (hand-off from W-01).
- **DoD:** `VoiceController` unit tests, one per load-bearing rule plus the four fixed bugs;
  transports tested with fakes (data channel, AudioContext); ScriptProcessor capture and WAV
  encoder tests; dock screenshots for each state including Active elsewhere and errors with
  recovery hints; a **laptop-backend** voice run (OpenAI WebRTC; Qwen or Gemini relay) with a
  transcript and tool call. Jetson voice runs only with Rodrigo's go-ahead; iPad voice per D-W4.

### W-13 — Settings, session settings, auth

- **Goal:** the IA §7 settings hierarchy on web, and the auth gate.
- **Inputs:** §3.6 (settings, auth); IA §7; inv02 F-31…F-34, §1.11; inv01 §3.1, §3.6–§3.8.
- **Boundary:** `src/features/settings/`, `src/features/auth/`.
- **DoD:** every config field of F-31 with correct control and range; sliders commit on release;
  snackbar with the verbatim server error + Retry; Google model auto-correct (test); working
  directory CRUD with SSH and coercion (tests); MCP "all enabled" semantics; session settings
  inherit/reset; two-pane on Expanded, pushed pages on Compact; screenshots of every page at three
  viewports.

### W-14 — History, Memory, Visuals

- **Goal:** the three list destinations and the two document views, plus Show on TV.
- **Inputs:** §3.6 (history, memory, visuals), §3.7; inv02 F-19, F-36, F-37; IA §3, §5, §9.2–§9.4.
- **Boundary:** `src/features/history/`, `src/features/memory/`, `src/features/visuals/`.
- **DoD:** search + date grouping + Open now (tests with fixed clock); row actions;
  memory tree keyboard + search, frontmatter disclosure, relative links opening in-app, encoded
  fetch; visual viewer sandbox tokens (test), remount reload, stays mounted while hidden; Show on
  TV hidden when the probe fails, working against a mock endpoint; screenshots of the three panes
  and two viewers at three viewports.

### W-15 — Backend preview route → hardening → cutover

- **Goal:** (a) early: the `/next/` + `/next-compat/` preview route; (b) late: the performance
  pass, device QA, cutover.
- **Inputs:** §1.6, §1.7, §5.4, §6.4 device checklist, §8.
- **Boundary:** (a) `api/app.py` (preview routes only) and `tests/test_next_preview_routes.py`,
  **only after Rodrigo approves D-W2**; (b) `fn/qa/`, `fn/scripts/budgets.json` adjustments with
  justification, then the cutover moves of §1.7 (with the D-W3 backend line).
- **DoD (a):** the route test passes in the backend suite; the iPad loads `/next-compat/` from the
  Jetson. **DoD (b):** every §8 must-preserve item and R3/R5/R7 checked off with evidence;
  §5.4 runtime targets measured and recorded; the device checklist passes on the iPad mini 2 and a
  phone; Rodrigo sign-off; cutover done and both dists deployed together.

---

## 8. Must-preserve traceability (inv02 §7)

| # | Behaviour | Implemented in | WP |
|---|---|---|---|
| 1 | All open sessions live in the background | runtimes outside React; mounted panels; frozen-while-hidden | W-06, W-07 |
| 2 | `start` on every open and visibility; reconnect 2 s × 10, paused while hidden | `services/ws/reconnect.ts`, `SessionRuntime` | W-06 |
| 3 | Per-tab `(stream_id, seq)` checkpoint, `resume_from`, overflow fallback | `protocol/resume` + `SessionRuntime` | W-05, W-06 |
| 4 | History init not re-run on `resumeSdkId` | `SessionRuntime` | W-06 |
| 5 | Ordering algorithm (with the reference's defects fixed) | `protocol/reducer` per spec 12 | W-05 |
| 6 | `drop_last_n` contract; rewind order | `protocol/history`, `session-actions` | W-05, W-11 |
| 7 | Freeze buffer; prepend restore; 150/80 px | `useMessageWindow`, `ScrollArea` | W-09, W-02 |
| 8 | Permission `request_id` match; typing = deny-with-feedback; plan inline | reducer + `PermissionCard` | W-05, W-09 |
| 9 | Tab titles derived from the session list | tab strip selectors | W-07 |
| 10 | Pool sync on mount/visibility; opened/closed pushes | `poolSync.ts` | W-06 |
| 11 | Voice rules (queue, initiator update, 30 s, cancel gating, flush, Gemini coalescing, empty `turnComplete`, 5 s ending, passive viewers) | `voice/core/VoiceController.ts` | W-12 |
| 12 | Google voice model auto-correct | settings Voice page | W-13 |
| 13 | Memory frontmatter block; viz sandbox + remount reload | `MemoryDocument`, `VisualViewer` | W-14 |
| 14 | UUID fallback; low-end + reduced motion; remote console | `platform/` | W-01 |
| 15 | Compat: margin spacing incl. text-node trap; no lookbehind/named groups; GFM tables keeping inline markdown; safe momentum scrolling; no `inset` | §2.1–§2.4, `ScrollArea`, gates | W-01, W-02, W-08 |

Charter issues: **R3** (tab bar) W-07; **R5** (settings) W-13; **R7** (tool output never lost)
W-05 conformance + W-10 rendering; **R1/R2** (look, consistency) W-02/W-03/W-04 with shared
tokens and icon names. R4 and R6 are Android-side.

---

## 9. Risks

| # | Risk | Likelihood / impact | Mitigation |
|---|---|---|---|
| K1 | Vite 8 (Rolldown) + plugin-legacy 8 is less proven for Safari 12 than the current Vite 5 + legacy 6 | M / H | W-01 day-1 device spike; fallback Vite 7.3.6 + legacy 7.2.1 |
| K2 | Chrome cannot emulate Safari 12; a parser/CSS issue could slip past screenshots | M / H | Scanner + Stylelint + ESLint compat + RegExp guard; device checklist each milestone via `/next-compat/`; remote console |
| K3 | A transitive dependency adds a lookbehind in a patch release | M / H | Exact pins + lockfile; scanner on every build; allowlist entries expire when unmatched |
| K4 | iOS 12 momentum scroll + programmatic scroll still glitches | M / M | Single `ScrollArea` path from the proven shim; touch/momentum deferral; device test in W-02 and W-09 |
| K5 | Memory pressure on the 1 GB iPad with several sessions and iframes | M / M | DOM window caps, hidden-panel trim, frozen-while-hidden, lazy chunks; heap measurements |
| K6 | Spec 12 not final when W-05/W-06 start, or its reducer lacks structural sharing / stable ids | M / H | Interface assumptions stated (§3.2); W-05 starts from fixtures; coordinator resolves gaps |
| K7 | Autolink-literal fork drifts from upstream behaviour | L / L | Oracle test against stock remark-gfm on main; small file |
| K8 | Browser-reserved shortcuts make the IA bindings impossible in a tab | H / L | Alternate bindings (§4.3, D-W5) |
| K9 | WS-relay voice on Safari 12 via ScriptProcessor is CPU-heavy on the A7 | M / M | Scoped by D-W4; feature-detected; dock explains unavailability |
| K10 | Uploads > 1 MiB fail through nginx | H / M | Clear error; nginx fix proposed (D-W6) |
| K11 | Parallel agents collide on shared files | M / M | Disjoint boundaries; entry-point stubs; W-01 owns `package.json`; icon manifest edits via W-02; coordinator merges |
| K12 | Jetson cannot build (old glibc) | known | Build on laptop and rsync both dists together (as today) |
| K13 | Passive orchestrator watcher socket has unknown side effects | L / M | Behind spec 12 endorsement + a laptop-backend check; otherwise visibility-only sync |
| K14 | "Show on TV" endpoint not approved | M / L | Action hidden by probe; no client change needed when it ships |

---

## 10. Decisions needed from Rodrigo

| ID | Decision | Recommendation |
|---|---|---|
| D-W1 | React version | **React 18.3.1 on both builds**; move both to 19 when the iPad mini 2 is retired |
| D-W2 | Additive backend route serving `frontend-next/dist-preview` at `/next/` and `/next-compat/` (`api/app.py`, ~25 lines + test; removed at cutover) | Approve. Option A (laptop-served preview) works meanwhile |
| D-W3 | Compat output path at cutover | `frontend/dist-compat` + a one-line `api/app.py` change (vs. keeping an empty `frontend-compat/` as deploy target) |
| D-W4 | Voice scope on the iPad (compat) | Support OpenAI WebRTC voice and WAV audio messages; WS-relay voice (Qwen/Gemini) via ScriptProcessor as best effort, hidden if the device test fails |
| D-W5 | Keyboard bindings, since Ctrl+Tab / Ctrl+W / Ctrl+1…9 cannot be captured in a browser tab | Ctrl+Alt+→/←/W/1…9 everywhere, plus the IA bindings when running as an installed PWA |
| D-W6 | nginx `client_max_body_size 200m` on the Jetson (G-1, infra) so uploads > 1 MiB work | Approve together with the backend-changes proposal |
| D-W7 | "Show on TV" endpoint `POST /api/visualizations/cast {path}` **plus** `GET /api/visualizations/cast` → `{available, reason}` for the availability probe (shipped) | Approve both in the backend proposal; the UI hides the action until then |
