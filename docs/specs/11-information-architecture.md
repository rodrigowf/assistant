# 11 — Information Architecture & Navigation (web + Android)

Status: **Structure approved by Rodrigo (2026-10-03, Q1–Q4 below); visual mockups pending review**

One product, one structure. Web and Android implement the **same screens, names, and
hierarchy**, adapted by window size class (M3 adaptive layout). Nothing here changes the API.

Window size classes (both platforms): **Compact** < 600 dp (phones, web mobile, A300M-class
excluded — the lite app has its own IA, §8), **Medium** 600–839 dp (tablets portrait, iPad mini 2),
**Expanded** ≥ 840 dp (desktop, tablets landscape).

## 1. Vocabulary (used verbatim in UI copy on both platforms)

| Concept | UI name | Notes |
|---|---|---|
| Orchestrator session | **Archie** conversation | Icon: Archie mark. Today's "orch" badge goes away. |
| Claude/Qwen/Gemini Code session | **Agent** session | Provider shown as a small labeled chip ("Claude", "Qwen", "Gemini"), not one-letter circles |
| Session live in the backend pool | **Open** | Shown as a live dot; open sessions are the "tabs" |
| Past session (JSONL) | Conversation history | |
| Memory files | **Memory** | |
| Visualizations | **Visuals** | |
| Server config (`/api/config`) | Settings → **Archie (server)** | Clearly separated from device settings |
| Device-local prefs | Settings → **This device** | |

## 2. Global structure

```
App
├── Conversation workspace (default)      ← open sessions ("tabs"), one active
│   ├── Archie conversation (text + voice)
│   ├── Agent session
│   ├── Memory document  (opens as a tab on Expanded; as a screen on Compact)
│   └── Visual           (same)
├── History        (all past conversations, search, date groups)
├── Memory         (tree browser)
├── Visuals        (gallery)
└── Settings       (This device · Archie (server) · About)
```

## 3. Expanded layout (desktop web, Android tablets landscape)

```
┌──────┬────────────────────┬──────────────────────────────────────────────────────────┐
│ RAIL │ LIST PANE (320dp,  │ WORKSPACE                                                │
│      │ collapsible)       │ ┌─ Top app bar (same surface as content, no divider) ──┐ │
│ [＋] │ ┌Search──────────┐ │ │ [◉ Archie] [● Agent: refactor ×] [Memory: x.md ×] [⌄]│ │
│      │ Open now           │ │                                     status · ⋮      │ │
│ Chats│  ● Archie          │ └──────────────────────────────────────────────────────┘ │
│Memory│  ● refactor (Claude)│                                                         │
│Visual│ Today              │   messages (max width 840dp, centered)                   │
│      │  …                 │                                                          │
│      │ Yesterday          │                                                          │
│  ⚙   │  …                 │ ┌ Composer ────────────────────────────────────────────┐ │
│      │                    │ │ ＋  Message Archie…            ◔ 42%   🎙   (●voice) │ │
└──────┴────────────────────┴─┴──────────────────────────────────────────────────────┴─┘
```

- **Navigation rail** (80 dp): New (extended FAB-style "＋" → menu: *New Archie conversation*,
  *New agent session*), destinations **Chats / Memory / Visuals**, **Settings** pinned at bottom.
  Rail destination drives the list pane content.
- **List pane**: Chats → search, *Open now* section (pool sessions, status dot), then history
  grouped Today / Yesterday / Previous 7 days / Earlier. Memory → tree with search. Visuals →
  list with thumbnails-less cards (title, project, age). Collapsible (toggle in top bar; state
  remembered per device).
- **Tab strip integrated in the top app bar** (fixes R3): 40 dp tall tabs (M3 secondary-tab
  style), min touch target 48 dp, leading type icon (Archie / agent provider / memory / visual),
  live status indicator (spinner while working, dot when idle, warning when disconnected),
  title, close button **always visible on the active tab and on hover**, middle-click closes,
  double-click / context menu → Rename, drag to reorder (Archie stays first),
  overflow chevron **⌄ "All tabs" menu** with search when tabs overflow,
  keyboard: Ctrl+Tab / Ctrl+Shift+Tab, Ctrl+W, Ctrl+1…9.
- Right of tabs: compact status text ("Thinking…", "Using Bash…", turns · cost on hover) and
  ⋮ session menu (Rename, Session settings, Compact context, Fork, Close, Delete).
- **No colored bars**: rail, list pane, top app bar and composer sit on surface /
  surface-container tones only; separation by tone and spacing, not by borders or tinted strips (fixes R1).

## 4. Medium layout (tablet portrait, iPad mini 2 via compat)

Rail stays; list pane becomes an overlay (modal) opened from the rail or a top-bar button.
Tabs as in Expanded, with the overflow menu kicking in earlier.

## 5. Compact layout (Android phone app, web on phones)

```
┌──────────────────────────────────────┐
│ ☰   Archie ⌄              🔊  ⋮      │  ← top app bar, same surface as chat; title opens
│      Thinking…                        │     the session switcher; subtitle = live status
├──────────────────────────────────────┤
│  messages                             │
│                                       │
│  [permission card / stall card here]  │
│ ┌───────────────────────────────────┐ │
│ │ ＋  Message Archie…      🎙  (◉)   │ │  ← single floating composer, no strip behind it
│ └───────────────────────────────────┘ │
└──────────────────────────────────────┘
```

- **No bottom navigation bar.** Modern assistant pattern: the conversation is the home; everything
  else is one swipe/tap away. (Removes the two stacked tinted strips — R1/A11.)
- **Top app bar**: ☰ opens the **navigation drawer**; the title (session name + ⌄) opens the
  **session switcher** bottom sheet; trailing actions: voice/speaker state, ⋮ session menu.
- **Session switcher sheet** = the phone equivalent of tabs: *Open now* list with status, each
  closable (swipe or ×), plus "New Archie conversation" / "New agent session".
  Horizontal swipe on the top app bar title switches between open sessions (Android; optional on web).
- **Navigation drawer** (modal, M3): header with the Archie mark + connection status;
  search; *Open now*; history grouped by date; footer entries **Memory**, **Visuals**, **Settings**.
- Memory and Visuals are full screens with their own top app bar (back arrow); documents open
  as detail screens (not tabs).

## 6. Conversation screen (all sizes)

- **Message column**: max width 840 dp centered on large screens; user messages as tonal
  bubbles (surface-container-high, right-aligned, max 80%); assistant as full-width prose; tool
  calls as compact cards (one line: category icon + summary + status; expand for input/output).
  Consecutive tool calls group into a collapsible "N steps" stack while live (expanded by default).
- **Composer** (shared component): one rounded container (28 dp radius, surface-container-high):
  leading **＋** (attach file / upload, voice message, slash command list), multiline field,
  trailing context-usage ring (tap → Compact), **🎙 voice message** (push to record), and a
  **primary action button that morphs**: Voice (realtime, Archie only, when empty) → Send (when
  text) → Stop (while working and empty). While the agent is working, Enter sends a *queued*
  message (parity with web). Gear (session settings) moves to the ⋮ menu.
- **Inline cards above the composer** (not banners stuck to edges): permission request
  (Approve / Reject / type to give feedback), stall ("Bash silent for 2 min" · Interrupt),
  error with dismiss + detail. A session closed or terminated on the server closes its view on
  every device (spec 12 OPEN-3); the active one shows a one-line notice with the reason.
- **Voice mode** (Archie): the composer morphs into a **voice dock**: animated level orb, state
  label (Listening · Speaking · Thinking · Using tools), mic mute, speaker mute, end. Transcripts
  stream into the conversation. On another device's voice session: dock shows "Voice active on
  <device>" read-only, transcripts still mirror, text input remains usable (fixes web bug 1).
  While this device has a call and another view is open (agent session, memory, visual,
  settings), the same controls **float** above it (default where the dock sits, above any
  composer; draggable to a corner), fading to a small orb + mic pill after a few still seconds
  and returning on any touch, move, scroll or key. Its state text returns to the Archie view.
- **Empty states**: new Archie conversation shows a greeting + suggestion chips + big voice
  button; a blank screen is never shown (fixes A6).

## 7. Settings (fixes R5) — identical hierarchy on both platforms

Settings home is a grouped list; each row opens a detail page. Expanded: two-pane list/detail.

```
Settings
├── THIS DEVICE
│   ├── Connection        servers (saved + discovered), scan, add, auto-connect, status   [Android]
│   ├── Audio             mic level, speaker level, echo ducking, output route (labeled)    [Android]
│   ├── Wake word & triggers  enable, talk phrases, wake phrases, sensitivity, auto-stop,
│   │                         assist gesture / recents trigger, notification actions        [Android]
│   └── Appearance        theme (System / Dark / Light), text size, reduce motion           [both]
├── ARCHIE (SERVER)       ← header shows server name + connection; disabled when offline
│   ├── Conversation model     text provider + model, history summarizer provider + model
│   ├── Voice                  provider, (Google backend), model, voice, language, recording
│   ├── Voice tuning           VAD threshold, min silence, server mic gain
│   ├── Agent sessions         default provider, harness model, Chrome flag
│   ├── Working directories    full list CRUD incl. SSH host/user (now on Android too)
│   ├── MCP servers            per-server switches with "all enabled" semantics shown correctly
│   └── Accounts               sign-in of every harness / API (link, device code, credentials paste, API key, sign out) + context/.env keys
└── ABOUT                  app version (real), backend version/host, remote logging, licenses
```

Rules: sliders commit on release; every save shows a snackbar ("Saved" / server error message
verbatim + Retry); helper text is one short line (details behind an ⓘ); controls are labeled
(no unlabeled icon rows); device-vs-server scope is always visible.

**Session settings** (per session, from ⋮): side sheet (Expanded) / bottom sheet (Compact):
working directory, MCPs, skills & agents, "Save and restart".

## 8. Lite app (Samsung A300M) — separate IA, specified in `15-lite-app.md`

Single voice-first screen designed to be read from across the room: large state orb + one-word
state, last exchange as large captions, connection indicator, and a small settings page
(server, mic level, wake phrases, output route). No history browsing, no tabs.

## 9. Decisions (Rodrigo, 2026-10-03)

1. Phone layout: **drawer + session switcher, no bottom navigation bar** — approved.
2. Memory documents and Visuals open as tabs on Expanded (today's behavior) — kept.
3. Tool calls: **consecutive calls group into a collapsible "N steps" stack** — expanded while
   live, collapses to a one-line summary once text follows; every card still expandable.
4. Visuals on Android: **full-screen in-app WebView** (reload, open in browser, share link)
   **plus a "Show on TV" action** (also offered on web). Needs an additive backend endpoint
   (e.g. `POST /api/visualizations/cast {path}` wrapping the existing TV display script) —
   listed in the backend-changes proposal for approval.
5. Main Android app: launcher name **Archie**, applicationId **`com.assistant.archie`**.
