---
name: visualizations-and-sharing
category: archie/integrations
tags: [visualizations, create-viz, context-public, markdown-reader, memory-urls, uploads, sharing, ipad, fire-tv, hardlink, review-artifacts, media-generation, remotion]
created: 2026-04-15
modified: 2026-10-09
summary: Making things viewable on any device — /create-viz (interactive canvases, live reload, in-app links), what each URL prefix serves, the markdown reader, review-artifact placement, media generation skills.
source: curated (consolidated from memory notes assistant/infrastructure/features_and_integrations_summary.md §8, §10, §14, assistant/utilities/markdown_workflow.md, assistant/utilities/memory_url_and_hardlink_edit_gotcha.md, assistant/utilities/review_artifacts_location.md, assistant/utilities/experiments_dont_touch_app.md, projects/content-creation/content_creation_project.md (tooling parts); verified against code 2026-10-06)
references:
  - skills.md
  - youtube.md
  - google-photos.md
  - ../architecture/backend.md
  - ../architecture/memory-and-search.md
  - ../devices/fire-tv.md
  - ../devices/devices.md
  - ../clients/web.md
  - ../clients/android.md
  - ../clients/browser-extension.md
  - ../infrastructure/jetson-server.md
  - ../infrastructure/context-sync.md
  - ../operations/working-rules.md
---

# Visualizations and sharing

Rodrigo looks at Archie's output on many screens: the laptop, the phone, the iPad (Safari 12), the
Fire TV. Anything he must see has to be a URL that works from all of them, which means a file the
**Jetson's** backend can serve. This doc covers how files become URLs, the `/create-viz` skill, the
markdown reader, where review artifacts must go, and the media-generation skills.

## What each URL prefix serves

All on the backend (`backend/api/app.py`). On the Jetson the backend listens on
`127.0.0.1:8765` and nginx (self-signed TLS on 443, 80 redirects) proxies everything, so LAN URLs
are `https://192.168.0.200/<path>` — never `:8765`, which only works on the Jetson itself. On a dev
machine without nginx use `http://<lan-ip>:8765/<path>`.

| URL | Files | Notes |
|---|---|---|
| `/<path>` | `context/public/<path>` | Checked first by the SPA catch-all, then `apps/web/dist`, then `index.html`. The catch-all only exists when `apps/web/dist` has been built |
| `/memory/<path>` | `context/memory/<path>` | Dedicated route; follows symlinks only into `docs/` (`context/memory/archie` → `docs/`, `utils/paths.py` `get_memory_link_targets()`), so docs are at `/memory/archie/<path>`. `/memory/` returns `MEMORY.md`. No directory listings |
| `/uploads/<path>` | `context/uploads/<path>` | Files posted with `POST /api/uploads` (shared from peripherals, browser-extension screenshots) |
| `/projects/<path>` | repo-root `projects/<path>` | Only if that dir existed at startup; machine-local (not synced) |

**A URL path is not a filesystem path.** `/memory/x` is *not* `context/public/memory/x`, and
`/uploads/x` is *not* `context/public/uploads/x`. When something hands you a `/`-prefixed path,
check which mount owns it before looking for the file. Full route table: [backend](../architecture/backend.md).

## `/create-viz`: interactive canvases

The personal skill `context/skills/create-viz/` (`SKILL.md` + `references/patterns.md`,
`references/devices.md`, rewritten 2026-10-09) treats visualizations as the agents' canvas: project
interfaces that grow with the work (one folder per project at the public root, e.g.
`context/public/<project>/index.html` with its assets beside it), interactive explainers, review and
editor pages that post choices back to the session (`POST /api/sessions/inject`), and live
dashboards that read the backend. Agents write the HTML directly with Write/Edit and edit it in
place; the apps reload open pages on every change (below). `<title>` names the page in the
Visuals list.

`context/scripts/create_visualization.py` (templates `basic`, `chart`, `dashboard`, `tv-remote`
into `context/public/visualizations/<name>.html`; `--show-url`, `--display-tv`) and
`get_visualization_url.py` still work but are legacy: they only cover `visualizations/`. The Vite
dev servers (5450/5451, and the old 5432) do **not** serve public files.

Because `context/` is synced, a visualization written on the laptop is live on the Jetson's URL a
few seconds later ([context-sync](../infrastructure/context-sync.md)).

### Live reload

`backend/api/content_watcher.py` watches `context/public/` and the memory tree (one `watchfiles`
loop, which also wakes the memory indexer) and pushes `visualization_changed` / `memory_changed`
to every orchestrator socket (spec 12 §9.3). The web and Android apps reload an open
visualization when its page **or any of its assets** changes (an asset maps to the pages in its
folder tree that name it, else the folder's `index.html`), keep the scroll position, and show a
short "Updated" cue (a hidden tab waits until it is shown); open memory documents refetch in place. Bursts (an rsync from context-sync)
are coalesced; temp files are ignored. If the machine is out of inotify watches the watcher
falls back to polling every 2 s; other errors retry with backoff, and a root that appears after
boot is picked up. Public and memory files are served with `Cache-Control: no-cache` + `ETag`,
`/<dir>/` serves `<dir>/index.html`, and an unknown `*.html` path is a 404 (not the app shell).

### Links open in the app

Chat (agent sessions and Archie) and memory documents open links to a visualization or a memory
file in the app's own viewer (spec 12 §9.4): root-relative URLs (`/avatar-pipeline/index.html`,
`/<dir>/`, `/memory/<path>.md`, the old `/markdown_reader.html?file=memory/…`), the same on the
server's host, filesystem paths as agents print them (`context/public/…html`,
`context/memory/…md`, `docs/…md` → `/memory/archie/…`, bare or in backticks). The **convention**
for agents is a root-relative markdown link: `[Avatar pipeline](/avatar-pipeline/index.html)`,
`[Voice notes](/memory/projects/voice.md)`.

### Listing and "Show on TV"

`backend/api/routes/visualizations.py` makes every `*.html` under `context/public/` discoverable
for the Visuals screens of the web and Android apps:

- `GET /api/visualizations` → `{path, url, title, created, modified, size}` newest-modified first.
  Title: `.titles.json` key `viz:<path>` (manual rename), else the `<title>` tag, else the
  prettified file name. Symlinks escaping `context/public/` are skipped.
- `PATCH /api/visualizations/rename {path, title}` stores a manual title.
- `GET /api/visualizations/cast` → `{available, reason}`; `POST /api/visualizations/cast {path}`
  opens the file's URL in the Fire TV's TvServerHub WebView over adb. Details (device selection,
  `FIRE_TV_ADB_SERIAL`, `VIZ_CAST_BASE_URL`) in [fire-tv](../devices/fire-tv.md).

`create_visualization.py --display-tv` uses the same mechanism as "Show on TV" (fixed
2026-10-07; it used to send a generic `VIEW` intent to the default adb device): it picks the device
pinned by `FIRE_TV_ADB_SERIAL`, else the first connected device whose manufacturer is Amazon, and
runs `adb -s <serial> shell am start -n com.example.tvserverhub/.WebPageViewActivity -e url <url>`
with the URL it printed. Run `/connect-tv` first if the TV is not on adb.

## The markdown reader

`context/public/markdown_reader.html` (served at `/markdown_reader.html`, with a local
`marked.min.js`) renders a markdown file in the browser, YAML frontmatter shown as a header — the
standard way to put a document in front of Rodrigo, including as a teleprompter on the iPad while
recording.

- `?file=<path>` is fetched **relative to the reader's own URL**, i.e. from the URL root. Paths
  containing `..` or `://` are rejected.
- A file in `context/public/`: `?file=my_document.md` or `?file=sub/dir/file.md`.
- A memory note: `?file=memory/<path-under-context-memory>` — the fetch goes to `/memory/<path>`,
  which the memory route serves straight from `context/memory/`. No copy is needed.
- A doc in this tree: `?file=memory/archie/<path-under-docs>` (through the `archie` symlink).

**Link convention**: in chat, link a memory note as `[Revised script](/memory/projects/<project>/<file>.md)`;
the apps render it in their memory viewer (§ Links open in the app). For a URL that must work
outside the app (pasted into another app, opened on a device without Archie), use the reader on
the Jetson IP as a readable markdown link:

```
[Revised script](https://192.168.0.200/markdown_reader.html?file=memory/projects/<project>/<file>.md)
```

The self-signed certificate shows a warning the first time on a new device; it is harmless on the
LAN.

## Review artifacts must be reachable from every device

Anything Rodrigo has to **review, choose between or sign off on** — test renders, transcripts,
draft scripts or subtitles, frame samples, waveforms — goes under
`context/memory/projects/<project>/`, one subfolder per kind (`transcripts/`, `test_renders/`,
`frames/`, `source/`, …). context-sync mirrors it to the Jetson within seconds, so the orchestrator
and every client can open it, and `/memory/projects/<project>/<file>` is its URL.

- The laptop's working dirs (`~/assistant/projects/<name>/`) are **laptop-only**: the Jetson cannot
  see them, so the Jetson-served apps and the orchestrator cannot either. Fine for the producer's
  intermediate files, wrong for anything a human must look at.
- **Size**: context-sync is real-time rsync, and a 12 GB working dir inside `context/` once crashed
  the Jetson. Multi-GB asset projects live in `~/assistant/projects/<name>/`; copy a source file into
  `context/memory/projects/` only if it is under ~200 MB, otherwise render a downscaled preview
  (50–100 MB) and copy that. Test renders of tens of MB are fine.
- A skill that produces "please review" output should write it there by default.
- Things the user only needs to *trust* (caches, raw analysis) can stay in the working dir.

### The `context/public/memory/` mirror and the hardlink gotcha

Older artifacts were hardlinked into both `context/memory/<path>` and `context/public/memory/<path>`
(the mirror still exists for a few projects). The mirror is never needed for `/memory/` URLs — that
route reads `context/memory/` — and it has a trap: **the Edit tool writes to a temp file and
renames it, which gives the edited path a new inode and silently breaks the hardlink.** The other
path keeps the old bytes and the browser shows stale content.

- Detect: `ls -la` both paths — different sizes mean the link is broken. Check this before
  suspecting nginx or browser caches.
- Fix: `cp` the edited file over the other path, or better, keep a single copy at the path that is
  served and edit only that one.
- Note that rsync turns hardlinks into two real copies on the Jetson anyway.

## Experiments stay out of the app

Standalone experiments (demo pages, pipelines, prototypes) live in their own folder under
`context/public/<name>/` with their HTML, JS, data and scripts, browsable at
`https://192.168.0.200/<name>/`. They must not modify `apps/`, `backend/`, `legacy/`, `shared/`,
`infra/` or the top-level scripts without an explicit, specific instruction — "yes to all" on a
list that includes "ship it into Archie" is not that instruction. Don't add helpers to
`context/scripts/` for an experiment unless a reusable orchestrator script is asked for
([working rules](../operations/working-rules.md)).

## Media generation

| Skill | Script | Backend | Output |
|---|---|---|---|
| `/generate-image` | `context/scripts/generate_image.py "<prompt>" [--model] [--aspect] [--style] [--count 1-4] [--reference PATH\|URL]… [--output] [--json]` | Google Gemini Image ("Nano Banana"): `nano-banana-2` (default, `gemini-3.1-flash-image-preview`), `nano-banana-pro` (`gemini-3-pro-image-preview`), `gemini-flash` (`gemini-2.5-flash-image`); key `GEMINI_API_KEY` | `context/images/` |
| `/generate-video` | `context/scripts/generate_video.py "<prompt>" [--model fast\|standard\|pro] [--duration] [--ratio] [--audio] …` | Seedance 2.0 on BytePlus ModelArk (720p / 1080p / 2K); key `BYTEPLUS_ARK_API_KEY` | `context/videos/` |
| `/remotion` | — | Programmatic video with React/Remotion; `context/skills/remotion/rules/` holds the rule files from `remotion-dev/skills` (animations, audio, captions, 3D, charts, …). Needs Node 18+ and FFmpeg | the Remotion project's `out/` |
| `/music-video-editor` | `context/scripts/music_video/audio_sync.py`, `plot_sync.py` (README alongside) | Syncs separately recorded takes to a master mix by cross-correlation and writes `sync.json`, which a Remotion composition reads; templates, layouts and transitions ship with the skill | the project's Remotion `out/` |

`context/images/` and `context/videos/` are not served over HTTP; copy a result into
`context/public/` (or a review folder under `context/memory/projects/`) to share it.

Rules baked into `/music-video-editor` (learned on its reference project):

- Non-destructive only: never modify source clips; trim and sync live in the composition
  (`startFrom`, `<Sequence from>`).
- `audio_sync.cross_correlate(a, b, sr)` returns δ such that *b is delayed by δ relative to a*. When
  the master is delayed relative to the anchor video, **skip δ of the master**
  (`master_skip_s` → `<Audio startFrom>`) — trimming the video instead puts the picture about a bar
  ahead of the sound.
- Use **hard links, not symlinks**, for assets in the Remotion `public/` folder — the bundler does
  not follow symlinks.
- iPhone HEVC sources work with `OffthreadVideo`; the Studio preview may struggle, rendering is
  fine.
- For cyclical music, a strong correlation peak can be a bar off; require several methods
  (`cross_correlate_envelope`, `cross_correlate_band`, onset detection) to agree within ~10 ms.
- Frame-based animation only (`useCurrentFrame()` + `interpolate()`); CSS transitions do not render
  deterministically. Round seconds to integer frames.

## History

- Before the 2026-10-05 cutover, `context/public/` was also reachable through the old Vite dev
  server on port 5432. The `/create-viz` skill pages and `test_visualization_on_tv.sh` printed that
  port until 2026-10-07.
- 2026-06 — markdown reader created for the first demo recording; the clickable-link convention
  followed. 2026-06-14/17 — review-artifact placement rule and the hardlink/URL gotchas, from a video
  editing project. 2026-08-28 — the `/uploads/` URL vs path surprise.
