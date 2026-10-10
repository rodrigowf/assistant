---
name: fire-tv
category: archie/devices
tags: [fire-tv, tvserverhub, adb, launcher, screensaver, webview, visualizations, show-on-tv]
created: 2026-02-23
modified: 2026-10-09
summary: Fire TV integration — TvServerHub launcher, its screensaver service, ADB connection, the TV skills and "Show on TV".
source: curated (consolidated from memory notes assistant/devices/television_integration_project.md, assistant/infrastructure/features_and_integrations_summary.md §6; verified against code 2026-10-06)
references:
  - devices.md
  - ../integrations/visualizations-and-sharing.md
  - ../integrations/skills.md
  - ../integrations/youtube.md
  - ../clients/web.md
  - ../clients/android.md
---

# Fire TV

The living-room TV is an **Amazon Fire TV Stick (AFTKM, Fire OS / Android 11)** controlled
entirely over ADB. It runs a custom launcher, **TvServerHub**, that shows web apps in a
full-screen WebView, so anything Archie can render as a web page — visualizations, dashboards,
the web app itself — can be put on the TV.

| Fact | Value |
|---|---|
| ADB address | `192.168.0.16:5555` (DHCP — can change; rediscovered by MAC address) |
| Launcher package | `com.example.tvserverhub` (`.MainActivity`, `.WebPageViewActivity`, `.ScreensaverMonitorService`, `.BootReceiver`) |
| Launcher source | A separate Android Studio project outside this repo (`~/AndroidStudioProjects/TvServerHub` on the laptop, with its own `CLAUDE.md` and `DEBUGGING.md`) |
| Discovery script | `~/AndroidStudioProjects/TvServerHub/scripts/discover-firetv.sh` |

## TvServerHub

- **Launcher** (`MainActivity.kt`, `AppLauncher.kt`): a Compose app grid with tiles for
  services on the Jetson (an "Agentic" tile, Jellyfin on `:8096`, Copyparty on `:3923`) and an
  animated gradient background. On start, `initializeEnvironment()` starts the screensaver
  service, sets `screensaver_enabled=0`, `screen_off_timeout=2147483647` and
  `stay_on_while_plugged_in=7`, and launches YouTube in the background for casting.
- **`WebPageViewActivity`** opens any URL: JavaScript on, hardware-accelerated, camera/microphone
  permissions auto-granted, remote WebView debugging on, Back navigates history, `singleTask`
  (reused for each new URL). Certificate errors are accepted for the local server.

```bash
adb -s 192.168.0.16:5555 shell am start -n com.example.tvserverhub/.WebPageViewActivity -e url "https://example.com"
```

### ScreensaverMonitorService

TvServerHub replaces Android's screensaver with its own:

1. Native screensaver off, screen timeout at maximum.
2. `ScreensaverMonitorService` runs as a **foreground service**, checking every
   **10 s** (`CHECK_INTERVAL = 10000L`).
3. It tracks app switches (`UsageStatsManager`, fallback `getRunningTasks`), media playback
   (`AudioManager.isMusicActive`) and remote input (DPAD / gamepad / keyboard sources).
4. After **80 s** idle (`IDLE_TIMEOUT = 80`) it brings `MainActivity` back to the front.

It needs three app-ops, granted after every fresh install:

```bash
adb -s 192.168.0.16:5555 shell "appops set com.example.tvserverhub android:get_usage_stats allow"
adb -s 192.168.0.16:5555 shell "appops set com.example.tvserverhub SYSTEM_ALERT_WINDOW allow"
adb -s 192.168.0.16:5555 shell "appops get com.example.tvserverhub"   # expect GET_USAGE_STATS, SYSTEM_ALERT_WINDOW, START_FOREGROUND: allow
```

**Pitfall: the service can be killed by Android under memory pressure.** It is `START_STICKY`
but does not reliably come back. Symptom: the TV no longer returns to the launcher after
inactivity, and there are no screensaver logs (the launcher process may still be running).

```bash
adb -s 192.168.0.16:5555 shell "dumpsys activity services com.example.tvserverhub" | grep -A 10 ScreensaverMonitorService
# no output → not running. Restarting the activity re-runs initializeEnvironment():
adb -s 192.168.0.16:5555 shell "am force-stop com.example.tvserverhub && sleep 1 && am start -n com.example.tvserverhub/.MainActivity"
```

## Connecting

`/connect-tv` runs the discovery script: it looks up the Fire TV's MAC address in the ARP cache,
ping-sweeps the subnet if needed, then `adb connect <ip>:5555`. If it fails, the TV may be off,
on another network, or waiting for the user to accept the ADB pairing dialog on screen.
Commands in the skills assume `192.168.0.16:5555`; if the IP changed, discover it first.

## Skills

| Skill | Use for |
|---|---|
| `/connect-tv` | (Re)establish ADB after a power cycle or IP change |
| `/tv-remote` | Remote-control actions: launch/close apps (YouTube, Netflix, Prime Video, TvServerHub), open URLs in `WebPageViewActivity`, media keys (85 play/pause, 126/127, 87/88, 89/90), D-pad (19–23), back/home (4/3), volume (24/25/164), screenshots (`exec-out screencap -p`), current app (`dumpsys activity activities \| grep mResumedActivity`) |
| `/tv-dev` | Build, deploy and debug TvServerHub itself: WebView DevTools via `adb forward tcp:9222 localabstract:webview_devtools_remote_<pid>`, logcat tags `TvServerHub`, `WebViewScreen`, `ScreensaverMonitor`, screensaver troubleshooting, changing the timeout or launcher tiles |
| `/create-viz` | Build interactive HTML canvases under `context/public/`; show one on the TV with "Show on TV" or `POST /api/visualizations/cast` |

Skills live in `context/skills/<name>/SKILL.md`. When delegating TV work to a session, load
`/tv-remote` (or `/tv-dev` for app debugging). YouTube results can be played on the TV through
the same remote commands ([youtube.md](../integrations/youtube.md)).

## Showing visualizations on the TV

Files in `context/public/` are served by the backend at the URL root (see
[visualizations-and-sharing.md](../integrations/visualizations-and-sharing.md)), so a
visualization is a URL the TV's WebView can open:

- On the Jetson: `https://192.168.0.200/visualizations/<name>.html` (nginx on 443).
- On a dev machine: `http://<lan-ip>:8765/visualizations/<name>.html` (the backend directly).
  The old Vite dev port 5432 no longer serves visualizations, and the new dev servers
  (5450/5451) do not proxy `/visualizations`.

`context/scripts/create_visualization.py` and `get_visualization_url.py` print the right URL
for the machine they run on. `create_visualization.py --display-tv` then opens it on the TV the
same way "Show on TV" does (below): same device selection, same `WebPageViewActivity` intent.

**"Show on TV"** in the web and Android apps' Visuals screen uses the backend endpoints in
`backend/api/routes/visualizations.py`:

- `GET /api/visualizations/cast` → `{available, reason}` — the capability probe. It looks for a
  connected adb device whose `ro.product.manufacturer` is Amazon (so a phone on adb is never
  targeted), or the device pinned by `FIRE_TV_ADB_SERIAL`.
- `POST /api/visualizations/cast {path}` → `{ok, message}` — validates that `path` is an `.html`
  file under `context/public/` (no `..`, no symlink escape), then runs
  `adb -s <serial> shell am start -n com.example.tvserverhub/.WebPageViewActivity -e url <url>`
  with a 10 s timeout. The base URL is `https://<this machine's LAN IP>`, or `VIZ_CAST_BASE_URL`
  when set (needed on dev machines without nginx). It never raises on TV problems; failures come
  back as `{ok: false, message}`.

So "Show on TV" only works on a machine where adb is installed and connected to the TV.
Tests: `backend/tests/test_api_visualizations_cast.py`.

## Pitfalls

- The TV's IP is DHCP-assigned; when commands fail with "device offline/not found", run
  `/connect-tv` before anything else.
- After reinstalling TvServerHub, re-grant the app-ops above or the screensaver silently stops
  working.
- Pages for the TV should be self-contained (inline CSS/JS), sized for 1920×1080 and navigable
  with the D-pad (arrow keys + Enter); a mouse-only page is unusable from the remote.
- The launcher's "Agentic" tile points at `https://192.168.0.200/agentic/voice/`, a path from
  the pre-Archie setup. It still works: the backend's SPA catch-all answers any unknown path with
  the web app's `index.html` (absolute `/assets/…` URLs, hash routing), so the tile opens the
  Archie app — the same bytes as `https://192.168.0.200/`. Don't point it at `/` without also
  changing `WebViewScreen.kt`: its Menu-key "Agentic Options" dialog (clear cache, storage and
  cookies, then reload) is enabled by `url.contains("agentic")`.

## History

- 2026-02: TvServerHub and the ADB-based control skills built; visualization system added
  (templates `basic`, `chart`, `dashboard`, `tv-remote`).
- Later: the screensaver idle timeout was doubled from 40 s to 80 s and the check interval from 5 s to 10 s.
- 2026-10: "Show on TV" (BX-2) added to the rebuilt web and Android apps.
