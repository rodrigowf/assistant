---
name: android
category: archie/clients
tags: [android, kotlin, compose, navigation3, views, app-main, app-lite, a300m, poco, gradle, adb, signing, vosk]
created: 2026-04-14
modified: 2026-10-10
summary: apps/android — Gradle multi-module project with the main app (com.assistant.archie) and the A300M lite app (com.assistant.peripheral).
source: curated (consolidated from memory notes assistant/android/android_peripheral_project.md, assistant/infrastructure/repo_layout_cutover_2026_10.md, assistant/devices/peripheral_devices.md, auto-memory feedback_android_ws_keepalive_silent_drop.md, feedback_use_adb_input_for_device_tests.md, feedback_hands_on_checks_over_suites.md, feedback_run_test_before_speculating.md, project_frontend_refactor_2026_10_03.md; verified against code 2026-10-06)
references:
  - ../harnesses/authentication.md
  - ../integrations/visualizations-and-sharing.md
  - ../specs/14-android-architecture.md
  - ../specs/12-client-protocol.md
  - ../projects/frontend-refactor/README.md
  - ../voice/architecture.md
  - ../voice/wake-word.md
  - ../voice/lifecycle.md
  - android-device.md
  - legacy-apps.md
  - web.md
  - ../devices/devices.md
  - ../operations/debugging.md
  - ../operations/working-rules.md
---

# Android clients (`apps/android/`)

One Gradle multi-module project (`rootProject.name = "archie-android"`) builds two apps that
talk to the same backend WebSocket/REST API as the [web client](web.md):

| | `:app-main` | `:app-lite` |
|---|---|---|
| Package | `com.assistant.archie` | `com.assistant.peripheral` |
| Device | POCO X7 Pro (Android 15 / HyperOS 2) | Samsung Galaxy A300M (Android 5.0.2, API 21) |
| SDK | minSdk 26, targetSdk 36, compileSdk 37 | minSdk 21, targetSdk 36, compileSdk 37 |
| ABIs | `arm64-v8a` | `armeabi-v7a` (32-bit userspace) |
| UI | Jetpack Compose + Material 3 + Navigation 3, adaptive layout | Plain framework Views (`android.app.Activity`, `Theme.Material`) — no Compose, AppCompat, Material Components, WebView or RecyclerView |
| Purpose | Full client: chat, sessions, orchestrator, voice, memory, visualizations, settings, approvals | Voice-first "face": wake word, talk word, last exchange, device settings |
| Launch activity | `com.assistant.archie/.shell.MainActivity` | `com.assistant.peripheral/.MainActivity` |
| Debug APK | `app-main/build/outputs/apk/debug/app-main-debug.apk` | `app-lite/build/outputs/apk/debug/app-lite-debug.apk` |
| Version file | `app-main/version.properties` | `app-lite/version.properties` (versionCode **≥ 100**) |

Both launcher labels read "Archie". The architecture is
[spec 14 — Android architecture](../specs/14-android-architecture.md); this doc
is the orientation map plus the operational rules. The voice and wake-word internals are in
[voice/architecture.md](../voice/architecture.md) and [voice/wake-word.md](../voice/wake-word.md).
The runbook for agents is the `/android-dev` skill (`context/skills/android-dev/SKILL.md`).

## Modules

`settings.gradle.kts` includes these modules. "Tier" is enforced by build-logic
(`ArchieTiers.kt`, `CatalogTierCheck.kt`): **legacy** modules compile for API 21 and may only use
legacy-tier catalog aliases and legacy modules (they ship in the lite app); **modern** modules
are minSdk 26 and main-app only.

| Module | Tier | One line |
|---|---|---|
| `core/model` | JVM | Immutable domain types (connection, sessions, voice state, settings, config) |
| `core/protocol` | JVM | `ProtocolCodec`, `ServerFrame`/`ClientFrame` for every wire message (spec 12) |
| `core/conversation` | JVM | Pure conversation reducer, history merge, tool-name normalisation, resume/rewind math; runs `apps/protocol-fixtures/` |
| `core/network` | legacy | OkHttp stack, `SocketClient` (no-drop frames, 30 s protocol ping, reconnect backoff 1 s → 15 s), REST `ArchieApi`, `VoiceApi`, LAN server discovery, streamed uploads |
| `core/settings` | legacy | DataStore `SettingsStore` (`null` until loaded), bounded resume checkpoints, service prefs |
| `core/session` | legacy | `OrchestratorChannel`: orchestrator socket, adoption via `pool/live`, reconnect/recovery, `start` with `resume_from` |
| `core/audio` | legacy | Mic, PCM sink, routing, audio focus, echo ducking |
| `core/voice` | legacy | Voice session controller, WebRTC and WS-PCM transports, provider parsers |
| `core/wakeword` | legacy | Vosk wake engine, Whisper confirm, phrase policies; the 68 MB `vosk-model-small-en-us-0.15` asset and the Lollipop `vosk-stderr-shim` (CMake) |
| `core/voice-host` | legacy | `VoiceHostRuntime` (process singleton owning channel + voice + wake), `VoiceHostService` foreground service, triggers, cues |
| `core/testing` | legacy (test only) | Fakes, fixture loader, old-constants loader for parity tests |
| `core/data` | modern | Repositories (conversation, open sessions, history, config, memory, visuals, uploads), agent socket pool |
| `core/design` | modern | Compose M3 theme from design tokens, shared components |
| `core/markdown` | modern | Incremental streaming markdown on commonmark-java, code blocks, tables |
| `feature/chat`, `feature/toolcards`, `feature/sessions`, `feature/memory`, `feature/visuals`, `feature/settings` | modern | Main-app screens |
| `app-main`, `app-lite` | modern / legacy | The two applications |
| `build-logic/` | — | Convention plugins: SDK levels (`ArchieBuild.kt`), signing, tiers, `LiteGuards`, `verifyNoCompose`, `verifyPatchedVosk` |
| `tools/native/` | — | `patch_vosk_weaken.py`, `verify_vosk_patch.py`, `check_elf_alignment.sh` |
| `tools/parity/` | — | `extract_old_constants.py` + `old_constants.json` (constants pinned from the old app) |

The main app's floating voice controls (over every non-Archie view during a call) are
`:feature:chat`'s `VoiceOverlay`, hosted by `app-main`'s `ShellVoiceOverlay`
([voice architecture](../voice/architecture.md#floating-voice-controls-web-and-android)).

`VERSIONS.md` lists every pinned library version (the voice-parity pins — stream-webrtc-android
1.1.1, vosk-android 0.3.47 — are deliberate).

## Build

**Laptop rule:** one heavy job at a time, niced, behind the shared locks. The laptop has
crashed from parallel Gradle/npm/emulator jobs. A reboot wipes `/tmp`, and **`flock` on a
missing lock file does nothing** (the command never runs), so create the locks first:

```bash
mkdir -p /tmp/archie-locks && touch /tmp/archie-locks/{gradle,npm,testenv}.lock

cd apps/android
flock /tmp/archie-locks/gradle.lock nice -n 15 ./gradlew :app-main:assembleDebug --max-workers=2
flock /tmp/archie-locks/gradle.lock nice -n 15 ./gradlew :app-lite:assembleDebug --max-workers=2

# Full gate: lint, unit tests, Roborazzi screenshot goldens, tier/signing/no-Compose/Vosk checks
flock /tmp/archie-locks/gradle.lock nice -n 15 ./gradlew check --max-workers=2

# One module's unit tests
flock /tmp/archie-locks/gradle.lock nice -n 15 ./gradlew :core:voice:testDebugUnitTest --max-workers=2

./gradlew --stop   # when done
```

At most one emulator or test browser at a time (`testenv.lock`). Emulator builds need
`-Parchie.emulatorAbis=true` (adds `x86_64` / `x86`); the API 21 x86 AVD also needs the patched
x86 `libvosk.so`, otherwise Vosk fails to load and the detector silently falls back to
SpeechRecognizer, which hides bugs.

## Signing and versions

- Signing comes from **`apps/android/keystore.properties`** (gitignored); the key files live in
  the private `context/secrets/android/`. The lite app **must** be signed with the
  `peripheral.*` key — the key the old A300M app was signed with — or the in-place install
  fails with a signature mismatch (`INSTALL_FAILED_UPDATE_INCOMPATIBLE`). The main app uses
  `archie.*` entries when present (the release build is signed with the same key as debug, so
  it updates in place). Release builds fail when the file is missing; debug builds fall back to
  the default debug key. API 21 needs v1 (JAR) signing, kept explicitly on for the lite app.
- versionCodes only go up. The A300M runs lite versionCode 100 (2.0.0); a lower one fails with
  `INSTALL_FAILED_VERSION_DOWNGRADE`. Bump `version.properties` for each release.

## Lite app: in-place install over the old peripheral app

The lite app kept the package `com.assistant.peripheral` because the companion app
([android-device.md](android-device.md)) hard-codes it (watchdog and boot launch, plus the FQCN
`com.assistant.peripheral.MainActivity` as explicit fallback).
`com.assistant.peripheral.service.ButtonAccessibilityService` also keeps its class name, or
Android forgets the accessibility grant on upgrade.

It was installed over the old app (v1.0.9, versionCode 10) on 2026-10-05 with
`adb install -r`. On first start a one-time **`LegacyMigration`**
(`app-lite/src/main/kotlin/com/assistant/peripheral/migration/LegacyMigration.kt`, logcat marker
`[LegacyMigration]`) keeps the DataStore `settings` file and keys, keeps
`assistant_service_prefs`, keeps `filesDir/vosk-model/` (same `.stamp`, so the 68 MB model is
not re-extracted), purges the unbounded `ws_resume_checkpoint:*` keys, and converts
`saved_servers` → `saved_servers_v2`. `pm clear` wipes settings and the migration never runs
again.

**Rollback** to the old app: `adb -s 06e4f224 install -r -d <archived peripheral-v1.0.9 APK>`
(kept in the private `context/secrets/android/a300m-backup-2026-10-05/`). A downgrade works
only because the installed lite build is debuggable — keep the A300M on a debug (or debuggable)
build until it has passed its soak. The full `adb backup` taken at the same time is truncated
(it holds only the Vosk model), but rollback does not need it: the old app reads the same
settings files the lite app left in place. Installing any `legacy/android` build on the A300M
replaces the lite app (same package).

## Install and launch

```bash
cd apps/android
# Main app on the POCO (confirm the HyperOS prompt on the phone)
adb -s HAOF4P8XLJ9DQCPJ install -r app-main/build/outputs/apk/debug/app-main-debug.apk
adb -s HAOF4P8XLJ9DQCPJ shell am start -n com.assistant.archie/.shell.MainActivity

# Lite app on the A300M (USB serial or WiFi ADB at the static IP)
adb connect 192.168.0.225:5555
adb -s 192.168.0.225:5555 install -r app-lite/build/outputs/apk/debug/app-lite-debug.apk
adb -s 192.168.0.225:5555 shell am start -n com.assistant.peripheral/.MainActivity
```

**HyperOS (POCO) quirks:** Developer options must have "Install via USB" and "USB debugging
(Security settings)" enabled; every install shows a confirmation prompt on the phone (tap it or
drive it with uiautomator — the first tap after a launch is often lost; re-read bounds after the
keyboard moves the dialog). After an app update the wake-word foreground service only restarts
once the app is opened (Android's background-microphone rule). The main app has no boot
receiver: after a reboot, wake word resumes the first time the app is opened. Settings →
Background reliability lists battery optimisation, Xiaomi Autostart and notification checks.

**A300M quirks:** USB on this unit is flaky (drops mid-push), so WiFi ADB at the static IP is
the preferred deploy path; WiFi ADB must be re-armed with `adb tcpip 5555` over USB after every
reboot. If the device shows only as MTP, restart the adb server
(`adb kill-server && adb start-server`). Slow WiFi installs: push the APK to `/sdcard/Download/`
and open it with an `android.intent.action.VIEW` intent
(`-t application/vnd.android.package-archive`).

On the POCO (Rodrigo's personal phone) check that the top activity is `com.assistant.archie`
before any screenshot, never capture the notification shade, and ask before device tests (they
take over his screen). Social-media apps on that phone are never automated without his explicit
request.

## A300M constraints

| Constraint | Consequence |
|---|---|
| Android 5.0.2 / API 21, Snapdragon 410, **888 MB real RAM** (`MemTotal` 908960 kB; sold as 1 GB), `MemFree` 60–79 MB | Lite app budget (spec 14 §5.6): ≤ 140 MB PSS idle with wake word armed, ≤ 190 MB in a WebRTC call, Java heap ≤ 24 MB. Measured 152 MB armed on the debug build (Vosk is ~100 MB native); re-measure on release. `onTrimMemory` drops non-essential state |
| Low-memory killer culls services under pressure | A clean `Process ... has died` after a long call with no tombstone is the LMK. 125 non-essential preinstalled packages were removed for user 0 (`pm uninstall --user 0 <pkg>`, reversible with `pm install-existing <pkg>`; `pm disable-user`/`pm hide` need privileges the shell lacks). Never remove Google TTS/GMS, the Samsung keyboard, launcher, phone/telephony, settings, media or system UI packages. `com.google.android.googlequicksearchbox` is only the wake-word fallback now |
| Small RenderThread stack + old GPU | A deep render tree rebuilt every frame overflowed `libhwui.so` natively (SIGSEGV on the RenderThread guard page, preceded by "Skipped N frames"). Root cause in the old app: the markdown renderer re-parsed the whole message on every streaming delta. Keep streaming render trees shallow and memoized: incremental parse of the stable prefix, wrapped (not horizontally scrolled) code blocks, inline depth cap `MAX_INLINE_DEPTH = 8` (ported to `core/markdown`), instant scroll while streaming. The lite app shows plain text only (markdown stripped) and draws its face on a `Canvas` |
| Samsung Lollipop audio HAL | Wake word and call must use the **same** `AudioSource` (`VOICE_RECOGNITION` below API 24; `VOICE_COMMUNICATION` is fine on ≥ 24) or AGC state halves the call's amplitude; `MODE_NORMAL`, not `MODE_IN_COMMUNICATION` (Samsung routes the latter to the earpiece even with wired speakers); cues on `STREAM_MUSIC` (`STREAM_NOTIFICATION` is muted during call audio); AudioFlinger "BUFFER TIMEOUT" warnings are normal underruns |
| No `logcat --pid` on API 21; small ring buffer | Filter by tag; pull logs early — an error flood can evict ~20 minutes of history |
| Patched `libvosk.so` | Below API 23 Vosk needs `stderr`/`stdin`/`stdout` weakened in `libvosk.so` (`tools/native/patch_vosk_weaken.py`; the copy in `app-lite/src/main/jniLibs/`). `verifyPatchedVosk` runs after every lite assemble and fails if an unpatched library reaches the APK |
| Model packaging | `androidResources.noCompress += "vosk-model-small-en-us-0.15"` must be set in `app-lite/build.gradle.kts` itself — the library module's setting does not reach the APK |
| 16 KB pages | Not relevant on the 32-bit A300M. For the main app, WebRTC 1.1.1 and Vosk 0.3.47 are still 4 KB-aligned (the POCO uses 4 KB pages); newer versions are aligned but need a voice retest |

## Open sessions follow the server

The server's pool is the one truth for what is open (spec 12 OPEN-1..4); every device shows the
same set and never revives a closed conversation.

- **"Open now"** (drawer, switcher, list pane, History) is `OpenSessionsRepository.items`: Archie,
  then every agent session in `pool/live` in pool order, with or without a view here (tapping one
  opens its view), then views not listed yet (a session the user just started), then memory and
  visuals. There is no "open on another device" row, no background view and no unread badge.
- **Reattach, never re-create** (OPEN-2). `ConversationState.userStart` marks the user's own first
  `start` (new session, History, fork, continue, rewind; Archie: new, resume, switch); every other
  `start` — reconnect, foreground, `not_started` recovery, adoption, the voice owner's
  `voice_start` re-arm (`VoiceStartRequest.reattach`) — carries `reattach: true`, so the server
  answers `error{session_closed}` instead of creating anything.
- **Closed = gone** (OPEN-3). `session_stopped` (not our own), `agent_session_closed` for the view,
  or `error{session_closed}` make the reducer emit `ConversationEffect.Closed`; the repository
  removes the view (no close request) and emits `ConversationEvent.Closed`, and
  `OpenSessionsRepository` moves focus to the neighbour as after an explicit close. If it was the
  active agent view, the snackbar says "<title> was closed elsewhere" or "<title> crashed: <detail>" (how it ended, by reason; spec 12 §6.13)
  for a terminated session (the web's wording; none for Archie). A terminated session is reopened
  from History; there is no "Continue in new session" card any more. Agent views
  also close on the watcher frame (`ConversationRepository`), and the row leaves `history.pool` at
  once (`dropFromPool`).
- **Reconcile on every read** (OPEN-4). `HistoryRepository.poolReads` carries each `pool/live`
  answer with its request time; agent views subscribed before that read and missing from it close.
  Reads happen on every orchestrator socket open (`MainAppGraph`), every foreground
  (`refreshAll`), after each agent `session_started` (ST-2, the same read) and on
  `agent_session_opened`. `OrchestratorChannel` probes the pool itself on every socket open and
  every foreground: its row is adopted, a missing one closes the Archie view (it publishes a
  synthesized `agent_session_closed`, so the lite face and every frame consumer see one signal).
- Tests: `OrchestratorChannelTest` (`open*`), `OpenRulesTest` (reducer), `DataLayerTest`
  (`open*`, against `FakeBackend`, which keeps its pool like the server and answers reattach starts).

## Agent notifications (main app)

Settings → This device → Notifications → "Agent session finished" (DataStore
`notify_agent_turns`, off by default; turning it on asks for POST_NOTIFICATIONS through the
usual rationale) posts a heads-up notification on the "Agent sessions" channel when any agent session
finishes a turn (spec 12 TURN-1/TURN-2). `app-main/.../system/TurnNotifier.kt` holds the decision
(`TurnAttention`: switch, Stop, "looking at it" = the approvals' `lookingAtFrom`), the notifier and
`SystemTurnSink`; the graph feeds it `orchestrator.frames`. "Looking at it" needs `MainActivity` to be
**resumed** (`approvals.foreground`, set in `onResume`/`onPause`), so it ends the moment Home is
pressed, the shade is pulled or the screen locks; it used the process lifecycle before, whose
`ON_STOP` arrives ~1 s late, and a turn finishing in that gap was wrongly treated as seen
(2026-10-10). The Settings home lists the page between Appearance and Permissions
(`SettingsUiTest.home_listsEveryDevicePage_includingNotifications` keeps every device page reachable;
it was missing from the home until 2026-10-10). A tap reuses the approval tap path
(`EXTRA_OPEN_AGENT`, plus `EXTRA_OPEN_AGENT_SDK` to reopen a session that left the pool).

**Background delivery.** Frames only arrive while the process runs and is not frozen.
`AgentWork` tracks in-flight agent turns (`agent_turn_started/finished`, reconciled with
`pool/live`); while the switch is on and one is in flight, the graph holds the voice host's
foreground service (`VoiceHostRuntime.setAgentWorkHold`; special-use type only, notification
"Waiting for N agent sessions"), released with the last turn. To keep a missed finish (backend
restart, dropped socket) from holding it forever, the graph re-reads `pool/live` on every
orchestrator reconnect and every 3 min while turns run; a finish newer than the read wins, and a
turn older than 2 h ages out. Android 12+ only starts that service from the foreground, so the hold
covers a turn that was running while Archie was open (send, pocket the phone). A turn started on
another device while Archie sat in the background reaches the phone only with "Stay connected in
background", the wake word or a live voice call (all of which run the same service). No push
server is involved.

## Debugging

Order of operations: **build → install → drive the real app on the device → only then the
gates.** When asked to "install and check the app", exercise the main flows by hand (adb input,
screenshots, logcat) against the real backend first; instrumented suites come afterwards as
gates for fixes. The hands-on pass finds the real bugs faster.

For crashes and voice/WS problems, **capture a real logcat (or tombstone) before theorising**.
Code reading and web searches produce plausible stories; one repro log usually shows a much
simpler cause. Pull the Jetson journal for the same time window too
([debugging.md](../operations/debugging.md)).

```bash
# Main app (Android 15 supports --pid)
adb -s HAOF4P8XLJ9DQCPJ logcat --pid=$(adb -s HAOF4P8XLJ9DQCPJ shell pidof com.assistant.archie)

# Lite app on the A300M: filter by tag
adb -s 06e4f224 logcat -v time LiteMain:* LiteGraph:* ArchieLite:* AssistantService:* VoiceHost:* \
  TriggerRouter:* VoiceCues:* WakeWordDetector:* VoskRecogEngine:* VoskModelLoader:* WhisperConfirmer:* \
  VoiceController:* VoiceDelivery:* OpenAIVoiceProvider:* AudioRouter:* EchoDuck:* PcmSink:* *:S
```

Tags by module: `app-main` `ArchieVoice`, `ArchieVIS`, `ArchieTile`, `ArchieShare`,
`ArchieApprovals`, `ArchieNotify` ("agent session finished" notifications: `posted` /
`suppressed … reason=` / `cleared` / `background hold on|off`), network `Archie/ws`, `Archie/orch`; `app-lite` `LiteMain`, `LiteGraph`,
`ArchieLite`, `AssistantVIS`; `core/voice-host` `AssistantService`, `VoiceHost`,
`TriggerRouter`, `PushToTalk`, `VoiceCues`, `ButtonAccessibility`; `core/wakeword`
`WakeWordDetector`, `VoskRecogEngine`, `VoskModelLoader`, `WhisperConfirmer`,
`SpeechRecogEngine`; `core/voice` `VoiceController`, `VoiceDelivery`, `VoiceApiBackend`,
`OpenAIVoiceProvider`; `core/audio` `AudioRouter`, `EchoDuck`, `PcmSink`, `VoiceManager`. Some
tags match the old app, so older diagnostic greps still mostly apply.

**Drive the UI with adb, don't ask the user to tap.** Scripted input is repeatable and can do
what a human cannot (two taps within 100 ms for race tests):

```bash
adb -s 06e4f224 shell "dumpsys window | grep mCurrentFocus"     # confirm the right activity is in front
adb -s 06e4f224 shell uiautomator dump /sdcard/ui.xml && adb -s 06e4f224 pull /sdcard/ui.xml /tmp/ui.xml
# find bounds="[x1,y1][x2,y2]" of the widget, tap its centre
adb -s 06e4f224 shell "input tap 540 1200; sleep 0.05; input tap 540 1200"
adb -s 06e4f224 shell "log -t TEST_MARKER 'phase 2: about to toggle wake word'"   # slice logcat later
adb -s 06e4f224 exec-out screencap -p > /tmp/phone.png
```

Re-dump after every build (layouts move). Ask the user only for what needs a human: speaking
the wake phrase, physical mic/speaker placement, judging UX feel.

**"I sent a prompt but the app never updated"** — the classic cause is a WebSocket closed by
OkHttp (`1011 keepalive ping timeout`, common after Doze or radio sleep) followed by a reconnect
that never re-sent `start`: the backend only broadcasts to sockets subscribed with `start`, so
every event in the gap is silently dropped. In logcat: a close, then `WebSocket connected`, but
no `"type":"start"` in between. The rule (spec 12) is that every (re)connect re-sends `start`
with the resume checkpoint; the backend replays from its 500-event ring or answers
`replay_overflow`, after which the client refetches over REST. Test with a short airplane-mode
blip.

## Harness settings (model + options per harness)

Same behaviour as the web app (see [web.md](web.md) "Harness settings"; spec 12 §6.14, §8.1):
Settings → Agent sessions and the session sheet (⋮ → Session settings) in `:app-main` render each
harness's model picker and options from `GET /api/config/harnesses`. Nothing is hard-coded per
harness. The model stays a select (plus a "Custom model id…" field); each option gets a control from
its catalog hints, or one inferred from its kind (`HarnessControls.optionControl`):

| Option | Control (`ui/HarnessFields.kt`) |
|---|---|
| `control` hint present (and suits the kind) | that control |
| toggle with a known `default` | **switch** (`SwitchOption`): the whole row toggles; "Use default" when set |
| toggle without `default` | **segmented**: Default · On · Off |
| select with `ordered: true` | **levels**: segments in order, the CLI-default level dotted ("CLI default") |
| select with ≤ 4 visible choices | **segmented** |
| other selects | **dropdown** (the select with Default (…) / CLI default / values) |
| number | **slider** (`LevelSlider`, log scale for `scale: "log"`, 1000 positions snapped to 2 significant digits then to `step`, ends exact) + number field (commits on Done / focus loss, clamped; a preset value such as -1 / 0 is kept). `presets` add Default · <presets> · Custom segments; the slider shows for Custom only, and choosing Custom saves its start value at once (the default if in range, else `custom_min`) |

- **Segments** (`Segments`): one M3 `SingleChoiceSegmentedButtonRow` when every label fits its equal
  share of the width, otherwise the segments wrap into pills (`FlowRow`, radio semantics) instead of
  cutting labels. "Default" is always first; a saved value the model lacks shows as a disabled extra
  segment. In the session sheet, a session forcing the CLI default over a global value checks no
  segment.
- **Supporting line**: global "CLI default · X" / "Overrides the CLI default (X)"; session
  "Default from Settings (X)" / "Default (CLI default · X)" / "CLI default for this session (X)" /
  "Set for this session" (a selected choice's description wins). "Use default" (switches and
  preset-less sliders, when set) and, in the session sheet only, "Use CLI default" when the global
  page sets the option.
- **`requires`**: an option whose dependency's effective value (session → global → CLI default) is
  not in the list is disabled with "Applies when <Label> is <values joined with " or ">"; its saved
  value is kept.
- **Claude in Chrome** (`ClaudeInChromeField`, `chrome_extension`): the last row of the Claude Code
  block on the global page (wherever that block is), and inside the harness section of the session
  sheet only while the effective harness is `claude`.
- **Global page** (`feature/settings/.../ui/ServerPages.kt` `AgentSessionsPage`): "New sessions"
  (default harness), the default harness's "<Harness> defaults" block, then collapsible rows for the
  other harnesses. Each control saves at once with a partial PUT (`{harness_model: {p: id}}`,
  `{harness_options: {p: {key: value | null}}}`, null = CLI default). Catalog `warnings` show as a
  notice; "Refresh models" refetches with `?refresh=true`.
- **Session sheet** (`ui/SessionSettingsSheet.kt`, `SessionSettingsController`): Harness, Model and
  options; "Default" inherits the global value. `harness_options` is sent as a whole map (absent
  key = inherit, null = CLI default, empty = null) and compared structurally for the dirty check;
  changing the harness resets model + options to inherit, switching back restores the saved values.
- **Logic**: `HarnessSettingsLogic.kt` (`HarnessLogic`, a port of the web `harness.ts`: gating by
  the effective model, labels, select rows, diffs) and `HarnessControls.kt` (`HarnessControls`, a
  port of `harnessControls.ts`: control choice, resolved values, `requires`, source lines, segments,
  log-slider mapping, number parse / format), unit-tested in `HarnessSettingsLogicTest` and
  `HarnessControlsTest` (mirroring the web tests).
- **Wire**: types in `core/model/Harness.kt` (`HarnessValue` = text / flag / number; hints:
  `HarnessControl`, `ordered`, `unit`, `scale`, `HarnessPreset`, `customMin`, `requires`), DTOs in
  `core/protocol/RestDto.kt` (hints read as raw JSON so a malformed one is dropped, never failing the
  catalog), mappers and the older-server fallback (`HarnessFallback`: `/api/config/providers` +
  `/api/config/harness/qwen/models`) in `core/protocol/HarnessMappers.kt`; `ServerSettingsModel`
  loads the catalogs with that fallback.
- **Provider labels**: `HarnessProvider` (`core/model/Sessions.kt`) is an open-ended id, not an
  enum — a Codex or Model Studio session keeps its provider. Tabs and history rows label it with
  `HarnessLabels`: short tag (Claude, Qwen, Gemini, Codex, Model Studio) → the registry label (kept
  by `ServerSettingsModel` when it loads the catalogs) → the id; never null.
- **Test data**: the JVM tests of `:core:protocol` and `:feature:settings` read the web mock
  catalogs (`apps/web/mock-server/data/harnesses.json`) through the `archie.harnessCatalogs`
  system property, so both clients test against the same data.

## Settings → Accounts

`feature/settings`: `AccountsModel.kt` (state + actions over `ArchieApi.accounts()` … `deleteEnv()`,
flow polling while the page is shown, pure helpers in its companion) and `ui/AccountsPage.kt` (one
card per service, the link flow — Open opens the URL in the browser via an `ACTION_VIEW` intent,
Copy puts it on the clipboard — pasted-back code, credentials paste, API-key fields, sign out,
Test, and the Environment keys list with reveal-on-demand / edit / delete / add). DTOs in
`core/protocol` `RestDto.kt` (`AccountServiceDto`, `LoginFlowDto`, `EnvKeyDto`, …). The AuthGate
(Claude only) is unchanged. See [authentication.md](../harnesses/authentication.md).

## Live visuals and internal links

- `:core:protocol` decodes `visualization_changed` / `memory_changed` (spec 12 §9.3);
  `:core:data` `ContentChangesRepository` (in `MainAppGraph`) bumps per-path counters, refreshes
  the lists and catches up after a reconnect (VZ-6). `VisualWebView` reloads the pooled WebView
  (`reload()`, keeps the scroll) when the counter passes the version the pool entry loaded, also
  after the tab was away; memory documents refetch in place. Both show a short "Updated" cue.
- Links (spec 12 §9.4): `:core:markdown` `InternalLinks` (same corpus as the web,
  `InternalLinksTest`), `autoLinkPaths` behind `MarkdownStyle.autoLinkPaths` (chat and memory
  documents). Chat links go through `rememberChatLinkHandler` (`shell/ContentWiring.kt`): visuals
  and memory files open as workspace items; memory documents open visuals in the app too (a
  `VisualDoc` screen on Compact).

## Rules for changing the apps

- **Tuned constants are load-bearing.** `*Tuning.kt` files (`core/audio/AudioTuning.kt`,
  `core/voice/VoiceTuning.kt`, `core/wakeword/WakeTuning.kt`, `core/voice-host/HostTuning.kt`,
  `core/network/NetworkTuning.kt`, `core/session/SessionTuning.kt`) hold timings and thresholds
  ported verbatim from the old app and pinned by parity tests. Don't change them in unrelated
  work.
- **Colors and dimens come from `apps/design-tokens/`** (Kotlin theme for Compose, generated XML
  resources for the lite app). No hex literals outside the token adapter.
- **Respect the tiers.** API above minSdk needs a `Build.VERSION` guard (lint `NewApi` is an
  error). Compose is allowed only in minSdk-26 modules; `verifyNoCompose` fails the lite build
  otherwise.
- **Protocol behaviour is shared with the web app** via `apps/protocol-fixtures/`; change the
  fixture and both clients together.
- The main app and the old peripheral app coexist on the POCO; their wake-word services would
  compete for the microphone, so the old app's wake word stays off there.

## History

- 2026-04 – 2026-10: a single-module Compose app, `com.assistant.peripheral` (v1.0.9), ran on
  both phones; it now lives in `legacy/android/` ([legacy-apps.md](legacy-apps.md)).
- 2026-10-03 – 10-05: rebuilt from scratch as `android-next/` on branch `frontend-refactory`
  (voice stack rewritten with the tuned constants ported verbatim and covered by parity tests).
  Cut over on 2026-10-05: main app on the POCO, lite app installed in place on the A300M, project
  moved to `apps/android/` the same day. Main release 1.0.0 (2) installed on the POCO on
  2026-10-05; the A300M stays on debug until its soak test.
