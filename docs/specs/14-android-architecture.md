# 14 — Android architecture (main app + lite app + shared core)

Status: **DRAFT for Rodrigo's review** · 2026-10-03 · branch `frontend-refactory` (HEAD `e871d05`)

Incorporates Rodrigo's IA decisions of 2026-10-03 (`spec/11` §9): no bottom bar on phones (drawer + session
switcher), Memory docs and Visuals as tabs on Expanded, "N steps" tool grouping, full-screen Visuals with
"Show on TV" (pending backend endpoint), main app named **Archie** with applicationId **`com.assistant.archie`**.

This spec covers how the new Android code is built: the module layout, toolchain, process and
service model, navigation, chat rendering, Memory and Visuals, the lite app, testing, and the work
breakdown. It does **not** restate behaviour that other chapters already pin down:

| Topic | Normative source | This spec's role |
|---|---|---|
| Wire protocol, reducer, stable ids, ordering invariants (R4, R7) | `spec/12-client-protocol.md` + `shared/protocol-fixtures/*.json` (in progress) | Where the reducer lives, how fixtures run on Android |
| Colors, type, shapes | `design/tokens/` → `dist/Tokens.kt` (in progress) | How `:core:design` consumes it |
| Screens and navigation | `spec/11-information-architecture.md` (draft) | How Compose implements it |
| Voice constants, FSMs, regression scenarios | `inventory/04-android-voice-and-device.md` §3, §4, §10, §11 | Module boundaries and the parity gate |
| Current-app bugs and parity gaps | `inventory/03-android-app.md` §4, §5, §8 | Where each fix lands |

**Path conventions.** `N/` = `android-next/`. `old/` = `android/app/src/main/java/com/assistant/peripheral/`.
Inventory references are written `inv03 §x` / `inv04 §x`.

**Facts verified while writing this spec** (2026-10-03):

- `android/` uses AGP 8.2.0, Kotlin 1.9.20, Gradle 8.2, Compose BOM 2023.10.01, compileSdk/targetSdk 34,
  minSdk 21, Java 8, versionCode 10 / versionName 1.0.9 (`android/app/build.gradle.kts`, `android/build.gradle.kts`,
  `android/gradle/wrapper/gradle-wrapper.properties`). There is no version catalog.
- **There is no `signingConfigs` block.** Every build the devices have received is signed with the laptop's
  default debug keystore (§1.8).
- `android/app/src/main/jniLibs/{armeabi-v7a,arm64-v8a}/libvosk.so` are the patched Vosk 0.3.47 libraries
  (`android/app/scripts/patch_vosk_weaken.py`). The model asset `vosk-model-small-en-us-0.15` is 68 MB. The current
  debug APK is 74 MB.
- The release `proguard-rules.pro` has keep rules for `org.vosk.**` and `org.kaldi.**`, but **none for JNA or WebRTC**.
  Release builds have never been deployed.
- **Current AndroidX releases have dropped API 21** (AAR manifests checked): core 1.18+, lifecycle 2.10+,
  appcompat 1.8+, activity 1.12+ (1.11.0 is still 21), datastore 1.2+, Material Components 1.14+ and Compose UI 1.10+
  all declare minSdk 23 or higher. webkit 1.15+ declares 24. This drives the two-tier catalog in §1.5.
- **Robolectric now supports API 23–37 only**, so API 21/22 code paths cannot run under Robolectric (§6.1).
- **16 KB page alignment:** WebRTC `stream-webrtc-android` 1.1.1 and Vosk 0.3.47 (including our patched copy) have
  `LOAD` alignment `0x1000` (4 KB). Versions 1.3.10 and 0.3.75 are aligned to `0x4000` (§1.9).
- The AVD `POCO_X7` is x86_64, API 36 `google_apis_playstore`, 1220×2712, 440 dpi, 3 GB RAM. The SDK has platforms
  34–36 and NDKs 25.1, 26.1 and 27.1. The host has 8 cores, 15 GB RAM and JDK 17.

---

## 1. Project layout

### 1.1 Directory tree

`android-next/` is a standalone Gradle build next to `android/` (D5). Nothing in `android/` changes until
cutover. At cutover `android-next/` is renamed to `android/` and the old tree is deleted (WP D-01).

```
android-next/
├── settings.gradle.kts            includeBuild("build-logic"); include(...) for every module below
├── build.gradle.kts               plugin aliases only (apply false)
├── gradle.properties              org.gradle.jvmargs=-Xmx3g, org.gradle.workers.max=4, android.useAndroidX=true,
│                                  org.gradle.configuration-cache=true, archie.emulatorAbis=false
├── gradle/libs.versions.toml      single version catalog (§1.4)
├── gradle/wrapper/                Gradle 9.8.0
├── keystore.properties            GITIGNORED — signing paths/passwords (§1.8)
├── build-logic/convention/        convention plugins (§1.3)
├── core/
│   ├── model/                     :core:model
│   ├── protocol/                  :core:protocol
│   ├── conversation/              :core:conversation
│   ├── network/                   :core:network
│   ├── settings/                  :core:settings
│   ├── session/                   :core:session
│   ├── audio/                     :core:audio
│   ├── voice/                     :core:voice
│   ├── wakeword/                  :core:wakeword   (+ src/main/cpp shim, assets model)
│   ├── voice-host/                :core:voice-host
│   ├── testing/                   :core:testing     (test fixtures + fakes, never shipped)
│   ├── data/                      :core:data        (main app only)
│   ├── design/                    :core:design      (main app only, Compose)
│   └── markdown/                  :core:markdown    (main app only, Compose)
├── feature/
│   ├── chat/                      :feature:chat
│   ├── toolcards/                 :feature:toolcards
│   ├── sessions/                  :feature:sessions
│   ├── memory/                    :feature:memory
│   ├── visuals/                   :feature:visuals
│   └── settings/                  :feature:settings
├── app-main/                      :app-main  (com.assistant.archie)
├── app-lite/                      :app-lite  (com.assistant.peripheral)
└── tools/
    ├── parity/                    extract_old_constants.py → old_constants.json (§6.2)
    ├── native/                    verify_vosk_patch.py, check_elf_alignment.sh (§1.9)
    └── visual-qa/                 side_by_side.py (§6.4)
```

### 1.2 Modules

"21" in the minSdk column means the module uses the **legacy dependency tier** (§1.5) and may not use any API above
21 without a `Build.VERSION.SDK_INT` guard. Lint `NewApi` is set to error.

| Module | Type · minSdk | Depends on | Public API (Kotlin package `com.assistant.core.<m>` unless noted) |
|---|---|---|---|
| `:core:model` | JVM (Kotlin 2.4, JVM 17) | — | Immutable data: `ServerEndpoint`, `ConnectionState`, `SessionKind{ORCHESTRATOR, AGENT}`, `PoolSession`, `SessionSummary`, `VoicePhase`, `VoiceUiState`, `WakeHealth`, `AudioOutput`, `VoiceConfig`, `ConnectionInfo`, `WakeSignal`, `DeviceSettings`, `VoiceSettings`, `ServerConfig` + `ConfigPatch`, `MemoryNode`, `VisualInfo`, `UploadResult`. No Android, no JSON. |
| `:core:protocol` | JVM | `:core:model`, kotlinx-serialization-json | `ProtocolCodec` (`decodeServer(bytes|text): ServerFrame`, `encodeClient(ClientFrame): String`). `sealed interface ServerFrame` / `ClientFrame` covering every message in inv01 §4–§7 and inv04 §2.1. `voice_event` payloads stay as `JsonObject` trees (recursive; fixes inv04 B1). The delta drop list from `old/network/WebSocketManager.kt:465-472` lives here as `VoiceEventFilter.DROPPED_TYPES`. `RestDto` types for every REST body in inv01 §3. Spec 12 is the authority on shapes; this module implements it. |
| `:core:conversation` | JVM | `:core:model`, `:core:protocol` | The spec-12 data layer, pure: `ConversationReducer.reduce(state, input): ConversationState`, `ConversationState(messages: PersistentList<Message>, status, pagination, …)`, `Message(id: MessageId, role, blocks: PersistentList<Block>)`, `Block` = `Text | Thinking | ToolUse | Compact | VoiceTranscript | Notice`. `HistoryMerger` (REST page ↔ live tail), `ToolNameNormalizer` (Qwen→Claude, inv02 F-05), `ResumeCheckpoint` math, `RewindIndex` (fixes inv03 §4.4.3). **Every invariant is exercised by the shared fixtures** (§6.1). |
| `:core:network` | Android lib · 21 | `:core:model`, `:core:protocol`, okhttp 4.12 | `HttpStack` (one `OkHttpClient`, disk cache, `TrustStore`), `SocketClient` (one WS endpoint: `connect(url)`, `frames: Flow<ServerFrame>` with **no drop** (UNLIMITED channel, replaces `tryEmit` at `old/network/WebSocketManager.kt:77`), `send(ClientFrame): SendResult`, `state: StateFlow<SocketState>` incl. `Disconnected(willReconnect)`; ping 30000 ms, reconnect 3000 ms fixed (inv04 §4.6 **LB**); audio-frame log suppression), `ArchieApi` (every REST call in inv01 §3, returning `ApiResult<T>` instead of swallowing errors), `VoiceApi` (the 3 voice endpoints, used by the voice core), `ServerDiscovery` (/24 probe on ports 80/443/8765 **plus** a JSON probe of `GET /api/auth/status`, fixes inv03 §8 bug 15), `UrlScheme` (ws/http mapping, inv03 §3.1), `UploadClient` (streamed multipart from a `Source`, never reads the whole file, inv03 §3.4). |
| `:core:settings` | Android lib · 21 | `:core:model`, datastore-preferences 1.1.7 | `SettingsStore` (`settings: StateFlow<DeviceSettings?>` — `null` until loaded, so nobody acts on defaults (fixes inv04 B4); `awaitLoaded()`; typed setters). **Uses the same DataStore file `settings` and the same key names as `old/settings/SettingsRepository.kt:325-352`** so the lite app inherits the A300M's settings in place (§5.7). `CheckpointStore` — a separate DataStore file `resume_checkpoints`, LRU-bounded to 32 entries, writes debounced to ≥500 ms and flushed on `turn_complete` (fixes inv03 §2.2 growth and the per-token write). `ServicePrefs` mirrors `assistant_service_prefs` (inv04 §4.5). |
| `:core:session` | Android lib · 21 (logic pure) | `:core:network`, `:core:settings` | `OrchestratorChannel` — owns the orchestrator socket: adoption via `GET /api/sessions/pool/live` with the 400 ms retry, genuine-reconnect gating (`initialConnectionDone`, reset on server change — fixes the dead `teardownForServerUrlChange`, inv03 §8), recovery at 0/500/1000 ms (inv04 §4.6 **LB**; corrected per inv04 §12 errata), `start` builder with `resume_from`, `events: Flow<ChannelEvent{Adopted, Reconnected, NoOrchestrator, Recovered, GaveUp}>`, `frames` (fan-out, no drop), `send`. Ports the intent of `connection/parity/OrchestratorConnectionControllerParityTest`. |
| `:core:audio` | Android lib · 21 | `:core:model` | inv04 §8 row verbatim. Pure: `EchoDucker`, `RouteDecider`, `Pcm` utils (RMS, gain, WAV, base64). Adapters: `MicSource`, `PcmSink` (single writer, fixes B3), `RouteApplier`, `AudioFocus`, `DeviceWatcher`. **Every API-level branch is a pure policy taking `sdkInt: Int`** (§6.1). |
| `:core:voice` | Android lib · 21 | `:core:audio`, `:core:protocol`, `:core:network` (VoiceApi), stream-webrtc-android 1.1.1 | inv04 §8 row verbatim: `VoiceSessionMachine`, ownership, `SessionUpdateDelivery`, provider parsers, `OpenAiDuckPolicy`, `WebRtcTransport`, `WsPcmTransport`. Facade: `VoiceSessionController` (commands + `StateFlow<VoicePhase>` + events). |
| `:core:wakeword` | Android lib · 21; NDK 26.1.10909125, CMake 3.22.1 | `:core:audio`, `:core:network` (Whisper over the shared `HttpStack`), vosk-android 0.3.47 (+ JNA 5.13.0 transitively) | inv04 §8 row verbatim: `WakeLoopMachine` with explicit CONFIRMING/CAPTURING/COOLDOWN states, `RmsGate`, `PreBuffer`, `NoiseFloorTracker`, `TalkVad`, `VariantMatcher`, `WhisperDecision`, `BackoffPolicy`; adapters `VoskEngine` (single-thread confinement, fixes R4), `SpeechRecognizerEngine` (kept, V6), `VoskModelStore` (**same extract dir `filesDir/vosk-model/` and `.stamp` value `vosk-model-small-en-us-0.15`** as `old/voice/VoskModelLoader.kt:44-52`, so the lite upgrade does not re-extract 68 MB), `WhisperClient`. Facade `WakeWordEngine`. The CMake `vosk-stderr-shim` lives here; the patched `libvosk.so` does **not** (§1.9). |
| `:core:voice-host` | Android lib · 21 | `:core:session`, `:core:voice`, `:core:wakeword`, `:core:settings`, androidx.core 1.17.0 | `VoiceHostRuntime` (process singleton: owns `OrchestratorChannel`, `VoiceSessionController`, `WakeWordEngine`, cues, pause/resume hand-off with ack, screen re-arm that **never restarts CONFIRMING/CAPTURING** (R3)), `VoiceHostService` (FGS; lifetime anchor; notification; FGS-type policy §2.6), `VoiceHost` interface (§2.5), `TranscriptSink`, `VoiceCues` (default tones on STREAM_MUSIC, inv04 §4.10), `HostConfig`, `TriggerIngress` (accessibility, assist, tile, notification actions → one entry point). |
| `:core:testing` | Android lib · 21 (test-only consumers) | `:core:model`, `:core:protocol`, coroutines-test | `FakeClock`, `FakeMic`, `FakePcmSink`, `FakeAudioManagerPort`, `FakeVoiceTransport`, `FakeSocketClient`, `RecordedTranscript` player, `ProtocolFixtures` loader (§6.1), `OldConstants` loader (§6.2). Consumed only via `testImplementation`. |
| `:core:data` | Android lib · **26** | `:core:conversation`, `:core:network`, `:core:session`, `:core:settings`, `:core:voice-host` (interfaces only) | Repositories implementing spec 12 against the network: `ConversationRepository` (per open session: `state(sessionKey): StateFlow<ConversationState>`, `send`, `queue`, `interrupt`, `compact`, `command`, `respondToPermission`, `loadOlder`, `rewind`, `fork`), `OpenSessionsRepository` (the workspace "tabs": pool sessions + open memory docs + open visuals), `HistoryRepository`, `ServerConfigRepository`, `MemoryRepository`, `VisualsRepository`, `UploadRepository`, `AgentSocketPool` (one `SocketClient` per open agent session; idle sessions beyond 6 disconnect LRU), `VoiceTranscriptBridge : TranscriptSink` (voice transcripts become blocks of the tail assistant message, inv03 §4.3 fix). |
| `:core:design` | Android lib · 26 · Compose | Compose BOM, material3, `design/tokens/dist/Tokens.kt` | `ArchieTheme(mode: ThemeMode, content)`, `LocalExtendedColors`, `ArchieIcons`, components: `ArchieTopAppBar`, `TabStrip`, `StatusIndicator`, `ComposerShell`, `InlineCard` (permission/stall/error/termination shells), `SettingsGroup`/`SettingsRow`/`LevelSlider`/`SwitchRow`, `EmptyState`, `ToolCardShell`, `CodeSurface`, `SnackbarHost` helpers. No business logic. |
| `:core:markdown` | Android lib · 26 · Compose | `:core:design`, commonmark 0.30.0 (+ gfm-tables, gfm-strikethrough, task-list-items, autolink, yaml-front-matter), dev.snipme:highlights 1.1.0 | `MarkdownDocument` (incremental, §3.2), `MarkdownBlockItem(block, style, onLink)`, `CodeBlock`, `MarkdownTable`, `Frontmatter.split(text)`, `LinkTarget` classification. Used by chat and memory. |
| `:feature:chat` | Android lib · 26 · Compose | `:core:data`, `:core:design`, `:core:markdown`, `:feature:toolcards` | `ConversationScreen(sessionKey)`, `ConversationViewModel`, `ChatListFlattener`, `Composer`, `VoiceDock` / `VoiceControls`, `VoiceDockModel`, the floating `VoiceOverlay` (+ `VoiceOverlayModel`; hosted by app-main's `ShellVoiceOverlay`), inline cards. Package `com.assistant.archie.feature.chat`. |
| `:feature:toolcards` | Android lib · 26 · Compose | `:core:design`, `:core:markdown`, `:core:conversation`, java-diff-utils 4.17 | `ToolCard(block: Block.ToolUse, sessionKind, expanded, onToggle)`, `ToolCatalog` (category, icon, summary — port of `frontend/src/components/ToolUseBlock.tsx:101-450`), per-tool renderers, `EditDiffView`. |
| `:feature:sessions` | Android lib · 26 · Compose | `:core:data`, `:core:design` | `SessionSwitcherSheet`, `ChatsListPane` (Open now + history), `HistoryScreen`, `NewSessionMenu`, `OrchestratorConflictDialog`, rename/duplicate/delete/fork/rewind flows. |
| `:feature:memory` | Android lib · 26 · Compose | `:core:data`, `:core:design`, `:core:markdown` | `MemoryTreePane`, `MemoryDocumentScreen(path)`, `MemoryLinkResolver`. |
| `:feature:visuals` | Android lib · 26 · Compose | `:core:data`, `:core:design`, androidx.webkit 1.17.1, androidx.browser 1.10.0 | `VisualsListPane`, `VisualScreen(path)`, `ArchieWebView` (security config §4.2), `WebViewPool`. |
| `:feature:settings` | Android lib · 26 · Compose | `:core:data`, `:core:design`, `:core:voice-host` (VoiceHost) | Settings hierarchy of IA §7, `PermissionCenter`, `BackgroundReliabilityPage`, `ServerTrustDialog`. |
| `:app-main` | Android app · minSdk 26 · targetSdk 36 · compileSdk 37 | everything except `:app-lite` | `ArchieApplication`, `MainAppGraph`, `MainActivity` (single Activity), adaptive shell + Navigation 3 back stack, system entry points (§2.8). |
| `:app-lite` | Android app · minSdk 21 · targetSdk 36 · compileSdk 37 · **Views only** | `:core:model`, `:core:network`, `:core:settings`, `:core:session`, `:core:audio`, `:core:voice`, `:core:wakeword`, `:core:voice-host` | `LiteApplication`, `LiteGraph`, `com.assistant.peripheral.MainActivity` (state face + settings), `StateFaceView`, trigger components (§5). |

### 1.3 Dependency rules (enforced)

```
:app-main ─► :feature:* ─► :core:data ─► :core:conversation ─► :core:protocol ─► :core:model
     │            └──────► :core:design ◄── :core:markdown
     └──────────────────────────────────► :core:voice-host ─► :core:session ─► :core:network ─► :core:protocol
:app-lite ───────────────────────────────► :core:voice-host ─► :core:voice ─► :core:audio
                                                         └──► :core:wakeword ─► :core:audio
```

1. `:core:*` never depends on `:feature:*` or an app. `:feature:*` never depends on another feature, except
   `:feature:chat → :feature:toolcards`.
2. No module in the legacy tier (minSdk 21) depends on a minSdk-26 module. A convention plugin fails the build if one
   does (`archie.android.library.legacy21` checks `project.dependencies` against a `modernModules` set).
3. Compose appears only in `:core:design`, `:core:markdown`, `:feature:*` and `:app-main`. `:app-lite` has no Compose
   on its classpath (D8). A `dependencyGuard`-style check in `:app-lite` (`./gradlew :app-lite:verifyNoCompose`) fails
   the build if any `androidx.compose` artifact resolves in `releaseRuntimeClasspath`.
4. Convention plugins in `build-logic/convention`:
   - `archie.jvm.library` — Kotlin JVM, JVM 17 toolchain, JUnit 4.
   - `archie.android.library.legacy21` — minSdk 21, compileSdk 37, core library desugaring (`desugar_jdk_libs` 2.1.5,
     needed for `java.time` in timestamp parsing — fixes the UTC skew in inv03 §1.5), lint `NewApi` = error,
     consumer ProGuard file.
   - `archie.android.library` — minSdk 26.
   - `archie.android.compose` — Compose compiler plugin and BOM, Compose lint checks.
   - `archie.android.app.main` / `archie.android.app.lite` — signing (§1.8), ABIs (§1.6), R8 (§1.10), version info
     from `app-*/version.properties`.

### 1.4 Toolchain and version catalog

Latest stable versions as of 2026-10-03, read from Google Maven, Maven Central and services.gradle.org.

| Tool | Version | Notes |
|---|---|---|
| Gradle | 9.8.0 | Wrapper. Needs JDK 17+, which the laptop has (17.0.20). |
| AGP | 9.4.1 | AGP 9 has **built-in Kotlin**: Android modules do not apply `org.jetbrains.kotlin.android`, and only the new DSL exists. If a third-party Gradle plugin (Roborazzi) is not AGP-9-ready, A-01 falls back to the newest AGP 9.x it supports and records why. |
| Kotlin | 2.4.20 | `kotlin("jvm")`, `plugin.serialization`, `plugin.compose` all at 2.4.20. **No KSP or kapt anywhere** (no Hilt or Room). |
| JDK target | 17 | D8 desugars language features down to API 21. |
| compileSdk / targetSdk | 37 / 36 | API 37 (Android 17) is stable. Target 36 for the first releases; moving to 37 is a separate decision (local-network permission, §2.6, decision Q6). |
| NDK / CMake | 26.1.10909125 / 3.22.1 | Same as today. NDK 26 is required by the Vosk shim to link `__sF` against the API 21 sysroot (`android/app/build.gradle.kts:34-36`). |
| Compose BOM | 2026.09.00 | ui/foundation 1.12.1, material3 1.4.0, material3-adaptive 1.3.0, material3-adaptive-navigation-suite 1.4.0. |
| Navigation | androidx.navigation3 1.2.0 + `adaptive-navigation3` 1.3.0 + `lifecycle-viewmodel-navigation3` 2.11.0 | §2.4 |

`N/gradle/libs.versions.toml` (excerpt; A-01 writes the full file):

```toml
[versions]
agp = "9.4.1"
kotlin = "2.4.20"
composeBom = "2026.09.00"          # material3 1.4.0, adaptive 1.3.0, ui 1.12.1
navigation3 = "1.2.0"
adaptiveNav3 = "1.3.0"
# ---- modern tier (minSdk 26 modules only) ----
coreKtx = "1.19.1"
lifecycle = "2.11.0"
activity = "1.13.0"
datastore = "1.2.1"
webkit = "1.17.1"
browser = "1.10.0"
splashscreen = "1.2.0"
profileinstaller = "1.4.1"
# ---- legacy tier (minSdk 21 modules + :app-lite); NEVER bump past these ----
coreKtxLegacy = "1.17.0"           # 1.18.0 declares minSdk 23
datastoreLegacy = "1.1.7"          # 1.2.0 declares minSdk 23
recyclerviewLegacy = "1.4.0"       # only if the lite settings list needs it
# ---- pinned for voice parity (inv04 §8 risk 5); bump only via decision Q4 ----
okhttp = "4.12.0"
webrtc = "1.1.1"                   # io.getstream:stream-webrtc-android
vosk = "0.3.47"                    # com.alphacephei:vosk-android (pulls JNA 5.13.0)
# ---- shared ----
coroutines = "1.11.0"
serialization = "1.11.0"
collectionsImmutable = "0.5.2"
commonmark = "0.30.0"
highlights = "1.1.0"               # dev.snipme:highlights
diffUtils = "4.17"                 # io.github.java-diff-utils
desugar = "2.1.5"
# ---- test ----
junit4 = "4.13.2"
turbine = "1.2.1"
mockk = "1.14.11"
robolectric = "4.17"
roborazzi = "1.76.0"
androidxTestRunner = "1.7.0"
androidxTestExt = "1.3.0"
espresso = "3.7.0"
uiautomator = "2.4.0"
benchmark = "1.5.0"
```

Rules: the catalog is the only place versions appear. A `catalogTierCheck` task (A-01) fails when a `*Legacy` alias is
used from a modern module or a modern alias from a legacy module. Bumping a legacy alias requires checking the new
AAR's manifest `minSdkVersion` (`unzip -p x.aar AndroidManifest.xml`). AGP's manifest merger also fails the
`:app-lite` build if any transitive dependency declares minSdk > 21, so transitive bumps are caught too.

### 1.5 Why two dependency tiers

Inside one Gradle build, the main app resolves the **highest** requested version of each AndroidX artifact. That is
fine: AndroidX is binary-compatible across minor versions, and `:app-main` (minSdk 26) can take core-ktx 1.19.1 even
though `:core:voice-host` compiled against 1.17.0. `:app-lite` resolves only the legacy versions, because no modern
module is on its classpath. The shared code is compiled against the **oldest** API it will meet, so it cannot
accidentally call something that does not exist on the A300M.

Consequences:
- The shared core uses **no** AppCompat, Material Components, lifecycle-service, activity or Compose. Services are
  plain `android.app.Service` with their own `CoroutineScope`. The `ServiceCompat`/`NotificationCompat`/`ContextCompat`
  helpers come from core 1.17.0.
- `:core:settings` uses DataStore 1.1.7 for both apps. The file format is unchanged between 1.1.x and 1.2.x.
- If a future AndroidX core drops API 21 support entirely (no more 1.17.x patches), the legacy tier stays frozen.
  That is acceptable: the A300M is a fixed target.

### 1.6 Build types, ABIs, variants

No product flavors (they double the variant count and test time).

| Build type | Apps | Minify | Signing | Notes |
|---|---|---|---|---|
| `debug` | both | off | §1.8 key | `isDebuggable = true`. The main app gets **no** `applicationIdSuffix`. Two installs of the main app would run two wake-word services competing for the microphone. |
| `release` | both | R8 full mode + resource shrinking | §1.8 key | Must pass the release smoke test (§1.10) before it ships to a device. |
| `benchmark` | main | R8, `initWith(release)`, debuggable=false, debug key | — | Macrobenchmark target for streaming-chat jank (§3.6). Added in B-04, not before. |

**ABIs.**

| App | Device ABIs | With `-Parchie.emulatorAbis=true` (emulator work only) |
|---|---|---|
| main | `arm64-v8a` (the POCO X7 is 64-bit only) | adds `x86_64` for the `POCO_X7` AVD |
| lite | `armeabi-v7a` (the A300M runs 32-bit userspace, inv04 §5.2) | adds `x86` for the `A300M_API21` AVD (§6.5) |

Vosk and WebRTC AARs ship all four ABIs, so emulator builds work. One exception: on the API 21 x86 AVD, Vosk needs
the stderr patch for x86 too (§1.9).

### 1.7 applicationIds, namespaces, component names

| | Main app | Lite app |
|---|---|---|
| applicationId | **`com.assistant.archie`** (approved by Rodrigo 2026-10-03, IA §9.5) | **`com.assistant.peripheral`** (D7, forced by `android-device/.../receiver/BootReceiver.kt:43-44` and `service/WatchdogService.kt:52-53`) |
| Kotlin namespace | `com.assistant.archie` | `com.assistant.peripheral` |
| Launcher label | "Archie" | "Archie" (as today — the A300M home screen) |
| Must-keep FQCNs | none | `com.assistant.peripheral.MainActivity` (the companion's explicit fallback, `BootReceiver.kt:116`, `WatchdogService.kt:139`); `com.assistant.peripheral.service.ButtonAccessibilityService` (if the class name changes, Android forgets the user's accessibility grant after the upgrade) |
| versionCode / versionName | start at 1 / `0.1.0` | **≥ 11** (the installed app is 10; a lower value fails with `INSTALL_FAILED_VERSION_DOWNGRADE`). Proposed: 100 / `2.0.0`. |

Core module namespaces: `com.assistant.core.<module>` (for example `com.assistant.core.voicehost`). Feature namespaces:
`com.assistant.archie.feature.<name>`.

The main app and the old app can coexist on the POCO during the transition. Their wake-word services would then
compete for the microphone, so **before any POCO field test, wake word is disabled in the old app, or the old app is
uninstalled** (field-test precondition in §6.6, decision Q11).

### 1.8 Signing

**Facts.**
- Neither `android/` nor `android-device/` declares `signingConfigs`. Every build is signed with the SDK default
  debug key at `~/.android/debug.keystore` on the laptop: alias `androiddebugkey`, store and key password `android`,
  `CN=Android Debug, O=Android, C=US`, created 2025-09-13 and valid until 2055.
  Certificate SHA-256 `F4:D6:91:4D:49:B5:96:18:07:89:46:6F:CB:F2:3A:60:DB:63:DF:80:3E:FD:41:7A:79:0B:AC:2B:54:0F:1E:3D`.
- `android/app/build/outputs/apk/debug/app-debug.apk` (built 2026-07-22) verifies with exactly that certificate
  (`apksigner verify --print-certs`).
- The deploy path is `adb install -r app/build/outputs/apk/debug/app-debug.apk` (`context/skills/android-dev/SKILL.md:66`).
  Release builds have never been installed.

**To be verified on the device before A-01 closes** (the A300M could carry an APK built on another machine):
```
adb -s 192.168.0.225:5555 shell pm path com.assistant.peripheral      # → /data/app/.../base.apk
adb -s 192.168.0.225:5555 pull <path> /tmp/installed.apk
apksigner verify --print-certs /tmp/installed.apk                    # expect SHA-256 f4d6914d…1e3d
```

**Consequences for the lite app (installed over `com.assistant.peripheral`):**
1. It **must be signed with that same key**, in both debug and release builds. Any other key fails with
   `INSTALL_FAILED_UPDATE_INCOMPATIBLE`. The only way around it is to uninstall first, which loses:
   - the DataStore settings (server list, wake phrases, gains, audio output);
   - `assistant_service_prefs`;
   - the extracted Vosk model (68 MB, slow to re-extract on the A300M);
   - the user's grant for `ButtonAccessibilityService`;
   - the default-assist selection.

   The companion watchdog would also relaunch-loop during the gap.
2. versionCode must be ≥ 11 (§1.7).
3. **Rollback:** `adb install -r -d <old apk>` downgrades only when the installed app is debuggable. Until the lite
   app passes field tests, ship it as **debug** (or a debuggable release build) so we can roll back. Archive the
   current APK first (e.g. `~/apk-archive/peripheral-1.0.9-debug.apk`).
4. **Key custody:** the key exists only on the laptop. If `~/.android/debug.keystore` is ever regenerated (SDK
   reinstall, new laptop), the lite app can no longer be updated in place. A-01 copies it to
   `context/secrets/android/peripheral-signing.keystore` (private repo, synced to the Jetson). Both apps read signing
   from `N/keystore.properties` (gitignored):
   ```properties
   peripheral.storeFile=/home/rodrigo/assistant/context/secrets/android/peripheral-signing.keystore
   peripheral.storePassword=android
   peripheral.keyAlias=androiddebugkey
   peripheral.keyPassword=android
   archie.storeFile=...            # decision Q2
   ```
   The convention plugin fails a `release` build when the file is missing, and falls back to the default debug key for
   `debug` builds.
5. API 21 needs v1 (JAR) signing. AGP enables v1 automatically when minSdk < 24. Keep it on for the lite app.

**Main app:** it has a new id, so any key works. Options (decision Q2): (a) reuse the same key (simplest; one key to
guard), or (b) create `context/secrets/android/archie-release.jks`. Either way, never sign with a key that exists only
in `~/.android/`.

### 1.9 Native packaging

| Item | Where | Rule |
|---|---|---|
| `vosk-stderr-shim` (CMake, `vosk_stderr_shim.c`) | `:core:wakeword/src/main/cpp/` (ported from `android/app/src/main/cpp/`) | Built for every ABI. `VoskModelStore` dlopens it only when `sdkInt < 23` (`old/voice/VoskModelLoader.kt:78-111`). |
| Patched `libvosk.so` (stderr/stdin/stdout weakened) | **`:app-lite/src/main/jniLibs/armeabi-v7a/`** only, plus `x86/` for the AVD | Patched by `tools/native/patch_vosk_weaken.py` (moved from `android/app/scripts/`). It only matters below API 23, so the main app uses the stock AAR library. `:app-lite` declares `packaging.jniLibs.pickFirsts += "lib/*/libvosk.so"`. Its `verifyPatchedVosk` task unzips the merged APK and fails unless `stderr`/`stdin`/`stdout` are `STB_WEAK` in every `libvosk.so` (inv04 §8 risk 2). |
| x86 patched copy (AVD only) | `:app-lite/src/emulator/jniLibs/x86/` added only with `-Parchie.emulatorAbis=true` | The script supports ELF32. Without it, Vosk fails to load on the API 21 AVD and the detector falls back to SpeechRecognizer, which hides bugs. |
| Model asset `vosk-model-small-en-us-0.15` | `:core:wakeword/src/main/assets/` | `androidResources.noCompress += "vosk-model-small-en-us-0.15"`. Both APKs carry the 68 MB model. |
| **16 KB page alignment** | `tools/native/check_elf_alignment.sh` runs on both APKs in CI | Today WebRTC 1.1.1 and Vosk 0.3.47 are 4 KB-aligned. On a 16 KB-page device they load only through Android 16's compatibility mode, or not at all. The POCO X7's page size is unknown: run `adb shell getconf PAGE_SIZE` in D-01 prep. For the main app only, WebRTC 1.3.10 and Vosk 0.3.75 are 16 KB-aligned. Upgrading is decision Q4 and must be re-gated by F8/F19 (WebRTC ADM behaviour) and F1/F22 (Vosk accuracy). The lite app stays on the pinned versions. |

### 1.10 R8 / minify

`release` is minified for both apps. The current rules (`android/app/proguard-rules.pro`) were never exercised on a
device (inv04 R7), so each native-facing module ships **consumer rules**, and a release smoke test gates G-01.

`:core:wakeword/consumer-rules.pro`
```proguard
# Vosk calls into libvosk through JNA direct mapping; class and method names must survive.
-keep class org.vosk.** { *; }
-keep class org.kaldi.** { *; }
# JNA: reflection over Structure fields, Native.register, callbacks.
-keep class com.sun.jna.** { *; }
-keep class * implements com.sun.jna.** { *; }
-keepclassmembers class * extends com.sun.jna.Structure { public *; <fields>; }
-dontwarn java.awt.**
-dontwarn com.sun.jna.platform.**
```
`:core:voice/consumer-rules.pro`
```proguard
-keep class org.webrtc.** { *; }            # JNI looks classes and methods up by name
-keepclasseswithmembernames,includedescriptorclasses class * { native <methods>; }
```
`:core:network/consumer-rules.pro`
```proguard
-dontwarn okhttp3.internal.platform.**
-dontwarn org.conscrypt.**
-dontwarn org.bouncycastle.**
-dontwarn org.openjsse.**
```
kotlinx-serialization ships its own rules. `@Serializable` DTOs need no manual keeps. The old blanket
`-keep class …data.** { *; }` is dropped.

**Release smoke test (part of G-01):** `./gradlew :app-lite:assembleRelease :app-main:assembleRelease`, install on the
matching AVD, and run the instrumented `NativeSmokeTest`. It asserts:
1. `VoskModelStore.load()` returns a model and a `Recognizer` accepts 1 s of silence.
2. `PeerConnectionFactory.initialize` plus `createPeerConnectionFactory()` succeeds once.
3. One JSON round trip through `ProtocolCodec` works.

Then repeat the same three steps on the physical devices in D-01.

---

## 2. Main app architecture

### 2.1 Layers and unidirectional data flow

```
UI (Compose)  ──Action──►  ViewModel  ──calls──►  Repository (:core:data)  ──►  SocketClient / ArchieApi / VoiceHost
     ▲                         │                          │
     └──── StateFlow<UiState> ◄┘◄──── StateFlow<domain> ◄─┘◄──── ServerFrame flow ─► ConversationReducer (pure)
```

- **One ViewModel per screen or pane**: `WorkspaceViewModel`, `ConversationViewModel(sessionKey)`,
  `SessionsViewModel`, `MemoryTreeViewModel`, `MemoryDocumentViewModel(path)`, `VisualsViewModel`,
  `VisualViewModel(path)`, `SettingsViewModel` plus one per settings page, and `PermissionCenterViewModel`.
- Each exposes `val state: StateFlow<XUiState>` (immutable, `@Immutable`, using `kotlinx.collections.immutable`
  persistent collections so Compose treats them as stable) and `fun onAction(a: XAction)`.
- One-shot effects (snackbar, navigate, open URL) go through `Channel<XEffect>(BUFFERED)` exposed as a `Flow`. They are
  collected in a `LaunchedEffect` with `repeatOnLifecycle(STARTED)`.
- UI collects with `collectAsStateWithLifecycle()`.
- **Domain state lives in process-scoped repositories, not in ViewModels.** A ViewModel only maps domain state to UI
  state and holds UI-local state (expanded cards, scroll anchors). That is why closing the Activity no longer kills
  chat or voice state (inv03 §0, "the chat and voice sessions die with the Activity's ViewModel").
- Saved state: navigation keys are `@Serializable` (Navigation 3 saves them). Per-screen UI state that must survive
  process death (draft text, expanded tool ids) goes in `SavedStateHandle`.

### 2.2 Dependency injection — manual, not Hilt

**Choice:** a hand-written graph per app. `MainAppGraph` lives in `:app-main`, `LiteGraph` in `:app-lite`. Each is
built lazily in `Application.onCreate` and reached through `(application as GraphOwner).graph`. ViewModels are created
with `viewModelFactory { initializer { ConversationViewModel(graph.conversations, key) } }`.

**Why:**
1. The shared core must also wire into a Views-only, memory-tight lite app (888 MB device). Hilt would add
   kapt/KSP, generated components and a Hilt runtime to the A300M build for no gain.
2. The object lifetimes that matter are few and explicit: **process** (`VoiceHostRuntime`, sockets, repositories),
   **ViewModel**, and **WebView pool**. Around 25 bindings fit in one ~250-line file per app.
3. The build has no annotation processing at all, which avoids Hilt/KSP lockstep with Kotlin 2.4 and AGP 9 (risk
   X20).
4. It matches the project's "transparency over polish" rule (CLAUDE.md): construction is plain code you can read.
5. Tests construct classes directly with fakes, with no test component.

Cost: wiring changes are manual edits to `MainAppGraph.kt`. Mitigation: graph files are owned by a single WP at a time
(§7).

```kotlin
// :app-main — sketch of the contract, not code to copy
class MainAppGraph(app: Application) {
    val settings = SettingsStore(app)
    val http = HttpStack(app, TrustStore(app))
    val voiceHost: VoiceHostRuntime by lazy { VoiceHostRuntime.create(app, settings, http, HostConfig.main(app), transcripts) }
    val conversations by lazy { ConversationRepository(http, voiceHost.orchestrator, agentSockets, settings.checkpoints) }
    val transcripts by lazy { VoiceTranscriptBridge { conversations } }   // breaks the cycle lazily
    // …sessions, history, config, memory, visuals, uploads
}
```

### 2.3 Threading

- **Reducer confinement.** Each conversation's reducer runs on `Dispatchers.Default.limitedParallelism(1)`, one per
  `ConversationRepository` entry. Socket frames, local sends, voice transcripts and REST merges are all
  `ConversationInput`s posted to that one serialized consumer. This fixes the unsynchronized mutation of
  `streamingMessageId` from Main and Default (inv03 §4.4.7, inv04 R8).
- **No dropped frames.** `SocketClient` hands frames to an unlimited channel. Voice audio frames (`voice_audio_out`,
  `voice_event`) are routed by `:core:session` straight to the voice core and never enter the chat reducer. That
  removes the 50 frames/s of audio that today shares the 64-slot bus with text deltas (inv03 §3.3).
- **Delta coalescing.** `ConversationRepository` publishes state through a `StateFlow`, which conflates naturally. In
  addition, while streaming, the UI mapping is `sample`d at 33 ms so the list recomposes at most ~30 times a second.
- The voice core keeps its own scopes and threading rules (inv04 §8).

### 2.4 Navigation and adaptive layout (implements IA §2–§6)

**Library:** Navigation 3 (`androidx.navigation3` 1.2.0), `adaptive-navigation3` 1.3.0 (`ListDetailSceneStrategy`),
and `material3-adaptive-navigation-suite` 1.4.0 (`NavigationSuiteScaffold`).

Why Navigation 3: the back stack is plain observable state (`SnapshotStateList<NavKey>`), which suits a workspace
where "tabs" are data rather than routes. List-detail panes also come as a scene strategy instead of hand-built
`if (expanded)` trees.

Fallback if Nav3 adaptive scenes misbehave (risk X22): `navigation-compose` 2.10.2 with
`NavigableListDetailPaneScaffold`. Only the shell (B-03) has to change.

**Keys** (`@Serializable data class/object … : NavKey`, in `:app-main/shell/Routes.kt`):
`Workspace`, `History`, `MemoryTree`, `MemoryDoc(path)`, `VisualsList`, `VisualDoc(path)`, `SettingsHome`,
`SettingsPage(id: SettingsPageId)`, `SessionSettings(localId)`.

**Workspace tabs are not navigation.** `OpenSessionsRepository.items: StateFlow<List<WorkspaceItem>>` holds the open
Archie conversation, open agent sessions, and (on Expanded) open memory docs and visuals. `active: StateFlow<ItemKey>`
holds the selected one. Selecting a tab changes state; it does not push a route. This keeps back navigation sane: Back
from a chat closes the drawer or app, and does not walk through previously viewed tabs.

**Scaffold by window size class** (`currentWindowAdaptiveInfo().windowSizeClass`; breakpoints from IA: Compact <600 dp,
Medium 600–839 dp, Expanded ≥840 dp):

| Size | Shell | Notes |
|---|---|---|
| Compact (POCO portrait, 443 dp wide) | `ModalNavigationDrawer` + `Scaffold` with `ArchieTopAppBar`. `NavigationSuiteScaffold(layoutType = NavigationSuiteType.None)`. **No bottom bar** (IA §5). | ☰ opens the drawer (header with mark + connection, search, *Open now*, history by date, footer Memory/Visuals/Settings). The top-bar title (session name + ⌄) opens `SessionSwitcherSheet` (`ModalBottomSheet`). A horizontal drag on the title (`Modifier.draggable`, threshold 56 dp) switches to the next or previous open session. Memory, Visuals and Settings are full screens with a back arrow. |
| Medium | `NavigationSuiteScaffold(layoutType = NavigationRail)` | The rail's destination opens its list pane as a modal overlay (IA §4). The tab strip appears in the top bar. |
| Expanded | `NavigationSuiteScaffold(layoutType = NavigationRail)` + `NavDisplay(sceneStrategy = ListDetailSceneStrategy())` | The list pane is 320 dp and collapsible (state in `DeviceSettings.listPaneCollapsed`). The tab strip (`SecondaryScrollableTabRow`, 40 dp tabs, 48 dp touch targets) sits in the top app bar, with an overflow "All tabs" menu and keyboard shortcuts (Ctrl+Tab, Ctrl+W, Ctrl+1…9) handled with `onPreviewKeyEvent`. |

- **Edge-to-edge** (targetSdk 36 enforces it): `enableEdgeToEdge()` in `MainActivity`. Every scaffold consumes
  `WindowInsets.safeDrawing`. The composer uses `imePadding()`. `windowSoftInputMode="adjustResize"`.
- **Predictive back** is on by default (API 36): `NavDisplay`, the drawer, sheets and `BackHandler`s all participate.
- **Splash and theme:** `core-splashscreen` 1.2.0 with `Theme.Archie.Starting`, whose window background comes from
  the tokens' dark surface. This removes the purple/white launch flash (inv03 §8 bug 10).

### 2.5 Process and service model — `VoiceHost`

**Problem being fixed:** today voice works only while `MainActivity` is alive. The wake broadcast is lost on a cold
start, and the WS and voice session live in an Activity-scoped ViewModel (inv04 R2, §6.3; inv03 §6).

**Model:** one process. A **process-scoped** `VoiceHostRuntime` owns the orchestrator channel, the voice session and
the wake-word engine. `VoiceHostService`, a foreground service, keeps the process alive and carries the FGS type that
allows microphone use in the background. UIs never own voice; they observe it.

```kotlin
// :core:voice-host — the contract both apps program against
interface VoiceHost {
    val state: StateFlow<VoiceUiState>      // connection, phase, ownership, mute, wake health, VAD, banner, outputs, lastExchange
    val events: Flow<VoiceUiEvent>          // Toast(msg), Cue(kind), Transcript(role, text, final), ErrorDetail(envelope)
    fun connect(); fun disconnect()
    fun startVoice(trigger: Trigger)        // Trigger = Button | WakeWord | Assist | Tile | Notification | Recents
    fun stopVoice()
    fun toggleMute(); fun toggleSpeakerMute()
    fun startPushToTalk(); fun stopPushToTalk(send: Boolean)   // main only; pauses wake first (fixes inv04 B7/B8)
    fun updateSettings(s: VoiceSettings)    // single ingress; gain always explicit (inv04 RS-33)
}
interface TranscriptSink { fun userTranscript(t: String, final: Boolean); fun assistantTranscript(t: String, final: Boolean)
                           fun system(t: String); fun voiceMessageSent(); fun turnComplete(); fun voiceEnded() }
```

| Component | Lifetime | Owns | Started by |
|---|---|---|---|
| `VoiceHostRuntime` | process (lazy in the graph) | `OrchestratorChannel`, `VoiceSessionController`, `WakeWordEngine`, `VoiceCues`, `TriggerIngress`, the aggregated `VoiceUiState` | First access. The UI and the service both access it. |
| `VoiceHostService` | while it should stay alive (below) | notification; FGS type; `PARTIAL_WAKE_LOCK` only during an active voice session; nothing else | `ContextCompat.startForegroundService` **from a foreground context only** (§2.6) |
| `MainActivity` + ViewModels | UI | nothing voice-related | user |
| `AgentSocketPool` | process | agent sockets | `ConversationRepository` |

**When the service runs (main app):** whenever wake word is enabled, **or** a voice session is active, **or** the
optional "Stay connected in background" setting is on (needed for permission-request notifications, §2.7), **or**
"Agent session finished" notifications are on and an agent turn is in flight (`VoiceHostRuntime.setAgentWorkHold`,
driven by `AgentWork` in `app-main/system/TurnNotifier.kt` from the `agent_turn_started/finished` watcher frames,
spec 12 TURN-1; re-synced with `pool/live` on every orchestrator reconnect and every 3 min while turns run, so a missed
finish cannot hold the service forever). When it is the only reason the service runs, the service takes the special-use
FGS type only (never the microphone type: `VoiceHostRuntime.micTypeWanted`; wake word or voice promote it later from a
foreground start) and its notification reads "Waiting for N agent sessions". That last hold lets a turn started while the app was open notify with the phone in a pocket; like
every reason it only *starts* the service from a foreground context, so turns started elsewhere while the app sits in
the background need "Stay connected". Otherwise
the service stops itself, and the runtime keeps the socket only while the UI is started
(`ProcessLifecycleOwner` ON_START/ON_STOP). The runtime disconnects 60 s after ON_STOP when the service is not
running.

**Binding:** in-process only. `VoiceHostService.LocalBinder.host: VoiceHost` serves the components that the system
starts without the Activity: the VIS session, the tile, and the lite Activity. Main-app ViewModels get `VoiceHost` from
the graph directly. No AIDL, no second process.

**Wake → voice without an Activity:** `WakeWordEngine` emits `WakeSignal.Wake` → `TriggerIngress` → cue (synchronous,
inv04 RS-45) → `VoiceSessionController.start()`. The Activity is not in the loop. If an Activity is visible, it simply
renders the new state. This is inv04 §8 design rule 3. Re-validating the timing is G-01's job (inv04 §8 risk 3).

### 2.6 Android 12–16 restrictions on the POCO (HyperOS)

| Restriction | Rule in this design |
|---|---|
| Android 12+: no FGS start from the background | The service is started only from foreground contexts: Activity ON_START, a notification-action `PendingIntent`, a tile click via a trampoline Activity, the VIS session `onShow`. Every `startForegroundService` call is wrapped; on `ForegroundServiceStartNotAllowedException`, `WakeHealth.BackgroundRestricted` is set and a regular notification "Tap to resume listening" is posted. |
| Android 14: FGS types required; microphone is a while-in-use type | Manifest: `foregroundServiceType="microphone|specialUse"` with `<property android:name="android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE" android:value="persistent assistant connection"/>`. Permissions: `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_MICROPHONE`, `FOREGROUND_SERVICE_SPECIAL_USE`. `ServiceCompat.startForeground(this, 1001, n, types)` with `types = MICROPHONE or SPECIAL_USE` when started from the foreground with `RECORD_AUDIO` granted; `SPECIAL_USE` only otherwise. Never call `startForeground` again from a background intent (inv04 R5). |
| START_STICKY restart after a kill | `onStartCommand(null)` → `SPECIAL_USE` only. The mic is **not** re-acquired in the background. Wake health shows `PausedNeedsForeground`, and the host notification gets a **Resume listening** action. Its `PendingIntent.getActivity` targets `VoiceTrampolineActivity`, which promotes the service to MICROPHONE from the foreground and finishes. |
| Android 15: `BOOT_COMPLETED` cannot start a microphone FGS | The main app has **no boot receiver** (decision Q7). After a reboot, wake word resumes the first time the app is opened. |
| Background activity launch (API 29+) | The main app **never** calls `startActivity` from the service. Wake → voice runs headless, and a `voice` notification (Mute/End) appears. If the screen is locked and "Show Archie over lock screen on wake" is on, a **full-screen-intent** notification opens `MainActivity` (`showWhenLocked`/`turnScreenOn`). On API 34+ this requires `USE_FULL_SCREEN_INTENT`, which is only auto-granted to calling and alarm apps. Check `NotificationManager.canUseFullScreenIntent()`; if false, the setting row deep-links to `Settings.ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT`. |
| Android 16 / targetSdk 36 | Edge-to-edge and predictive back (§2.4). Orientation and resizability locks are ignored on large screens, so the layout must not assume portrait. |
| targetSdk 37 (later) | Android 17 adds a local-network permission (`ACCESS_LOCAL_NETWORK`) for apps that target it. The whole app talks to `192.168.0.x`. **Stay on targetSdk 36** until a WP adds the runtime request (decision Q6; verify the exact permission name against the API 37 docs at that time). |
| HyperOS background killing (no field data yet, inv04 §5.2) | `BackgroundReliabilityPage` (Settings → This device → Wake word & triggers → Background) is a checklist with live status: battery optimisation (`isIgnoringBatteryOptimizations` → `ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`, permission `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`), Xiaomi Autostart (`Intent().setClassName("com.miui.securitycenter", "com.miui.permcenter.autostart.AutoStartManagementActivity")`, falling back to `ACTION_APPLICATION_DETAILS_SETTINGS`), notification channel enabled, and a "lock Archie in Recents" hint. Field test M-F2 measures the effect. |

### 2.7 Notifications

| Channel id | Importance | Shown when | Content | Actions |
|---|---|---|---|---|
| `host` | LOW, ongoing | the service runs without voice | "Listening for "wake up"" · "Wake word paused" · "Wake word stalled — mic held by another app" (inv04 §3.5) · "Tap to resume listening" | **Pause/Resume listening**, **Talk** (starts voice via the trampoline) |
| `voice` | DEFAULT, silent, ongoing, chronometer | a voice session is active (owner) | "Archie · Listening/Speaking/Thinking/Using tools" | **Mute/Unmute**, **End** (direct `PendingIntent.getService` to the running service; allowed because the service is already in the foreground) |
| `attention` | HIGH | only while the app is not visible: an agent `permission_request` (e.g. `ExitPlanMode`), `session_stalled` for >2 min, `session_terminated` | Session title + one line | **Approve**, **Reject** (with `RemoteInput` "Reply with feedback" → deny with reason, the web's semantics, inv02 F-14), **Open** |
| `background` | LOW | `BackgroundRestricted` | "Archie can't listen in the background" | **Fix** → `BackgroundReliabilityPage` |
| `agent_turns` ("Agent sessions") | HIGH (heads-up; opt-in) | an agent turn finished (`agent_turn_finished`, spec 12 TURN-2), Settings → Notifications on, the user not looking at that session | Session title + the reply's first line, or "Failed: …"; id 1004, tag `turn:<localId>` (one per session, replaced) | tap → focus that session (`EXTRA_OPEN_AGENT` + `EXTRA_OPEN_AGENT_SDK`); cleared when the session is opened |

The FGS notification id is 1001 (inv04 §4.5). The `host` and `voice` notifications are the same notification id
updated in place, so only one ongoing notification is shown.

### 2.8 System entry points (main app)

| Entry | Implementation | Notes |
|---|---|---|
| Launcher | `MainActivity` (single Activity, `launchMode="singleTask"`) | |
| **Share target** | `MainActivity` intent filters: `SEND` and `SEND_MULTIPLE` for `text/*` and `*/*` (adds multiple files, inv03 §1.8) | The `ShareSheet` composable asks for the target (default: the open Archie conversation; shows *Open now* sessions). Text → `inject_text`. Files → `UploadClient` streams each one with a progress bar, then `inject_text` with the returned path. Errors: nginx's **HTML 413 above 1 MiB** (inv01 G-1) is detected by status and shown as "File is larger than the server's 1 MB upload limit". The payload lives in `ShareRepository` (a `StateFlow`), so a cold-launch share is not lost (parity, `old/MainActivity.kt:86-108`). |
| **Assist gesture / digital assistant** | `ArchieVoiceInteractionService` + `ArchieVoiceInteractionSessionService` + `ArchieVoiceInteractionSession` + a stub `ArchieRecognitionService`. `res/xml/archie_interaction_service.xml`: `sessionService`, `recognitionService`, `supportsAssist="true"`, `supportsLaunchVoiceAssistFromKeyguard="true"` | Fixes inv04 B6. These are the components that make the app selectable as "Default digital assistant app". `Session.onShow` → `voiceHost.startVoice(Trigger.Assist)` (the session window is a foreground context, so the mic FGS is allowed), and shows a compact voice orb panel (Compose in the session window) with End and "Open Archie". `onLaunchVoiceAssistFromKeyguard` behaves the same. |
| `ACTION_ASSIST` fallback | `VoiceTrampolineActivity` (transparent, `excludeFromRecents`, `taskAffinity=""`) | Also the target for the tile, the shortcut and the notification "Talk"/"Resume" actions. It promotes the service and starts voice, then finishes. |
| **Quick Settings tile** | `ArchieTalkTileService` (`ACTIVE_TILE` meta-data) | Tile state is active while voice is active. Tap when off → `startActivityAndCollapse(PendingIntent)` (the PendingIntent overload is required on API 34+) → trampoline. Tap when on → `voiceHost.stopVoice()`. Long-press → app settings (`ACTION_QS_TILE_PREFERENCES`). |
| App shortcuts | `res/xml/shortcuts.xml`: "Talk to Archie" (trampoline), "New Archie conversation", "New agent session" | |
| Recents-button accessibility trigger | **Not in the main app** | It exists for the A300M's capacitive key. Modern phones use the assist gesture or the tile. |

### 2.9 Permissions UX (fixes "denials are silent", inv03 §1.1)

`PermissionCenter` (in `:feature:settings`, state from a small `PermissionsRepository` in `:core:data`) tracks:

| Permission | Asked when | Denied state shown |
|---|---|---|
| `RECORD_AUDIO` | first tap on Voice, PTT or wake-word enable, with a one-screen rationale | The voice dock shows "Microphone permission needed · Grant". Wake-word rows are disabled with the reason. Permanently denied → "Open app settings" (`ACTION_APPLICATION_DETAILS_SETTINGS`). |
| `POST_NOTIFICATIONS` (33+) | when wake word, "Stay connected" or "Agent session finished" is enabled | A card on Settings → This device. Without it the FGS still runs, but its notification is hidden. |
| `BLUETOOTH_CONNECT` (31+) | when the user picks the BT output | The BT output option is disabled with "Allow Nearby devices". A `SecurityException` is caught (inv04 RS-28). |
| Battery / autostart | from `BackgroundReliabilityPage` | §2.6 |
| Full-screen intent (34+) | when "Show over lock screen on wake" is enabled | §2.6 |

There is no first-run wizard. Each permission is requested in context, and Settings → This device has a
"Permissions" row with status chips for all of them.

### 2.10 Settings storage

- **Device settings** go through `:core:settings` `SettingsStore`. The main app has a new applicationId, so it starts
  with defaults (no migration). The default server URL is `ws://192.168.0.200:80` (as today), and a LAN scan runs when
  the URL is the default, but **adopting a scanned server now connects** (fixes inv03 §8 bug 3).
- `savedServers` is stored as a JSON list under a new key `saved_servers_v2`. The legacy `label\turl|…` key is read
  once and converted (fixes the tab/pipe encoding bug, inv03 §2.1).
- The `speakerVolumeLevel` slider is removed: it is a no-op (inv04 B9). System volume keys handle loudness.
- **Server config** (`/api/config`) is read and written only through `ServerConfigRepository`. The `enabled_mcps`
  semantics are fixed: an empty list means "all enabled" and is shown that way. Toggling one off writes the full list
  minus that one (fixes inv03 §8 bug 1).

---

## 3. Chat rendering

### 3.1 From conversation state to `LazyColumn` items

`ChatListFlattener` (pure Kotlin, `:feature:chat`, unit-tested) turns
`ConversationState.messages` into `List<ChatItem>`. Each assistant message becomes **several** list items, one per
renderable unit, so a streaming delta invalidates exactly one item.

| `ChatItem` | `key` (stable, from spec-12 ids) | `contentType` |
|---|---|---|
| `LoadOlder(state)` | `"load-older"` | `"load-older"` |
| `UserBubble(msgId, text, folded)` | `"m:$msgId"` | `"user"` |
| `MdBlock(msgId, blockId, mdIndex, node, streaming)` | `"m:$msgId/b:$blockId/md:$mdIndex"` | `"md-paragraph"`, `"md-heading"`, `"md-list"`, `"md-quote"`, `"md-code"`, `"md-table"`, `"md-rule"`, `"md-tail"` |
| `Thinking(msgId, blockId)` | `"m:$msgId/b:$blockId"` | `"thinking"` |
| `ToolCard(toolUseId)` | `"t:$toolUseId"` (the tool_use_id is globally unique and survives history refetch) | `"tool-${catalog.renderer(name)}"` (e.g. `tool-bash`, `tool-edit`, `tool-generic`) |
| `ToolGroup(firstToolUseId, count, expanded)` | `"g:$firstToolUseId"` | `"tool-group"` (IA §9.3, approved) |
| `CompactDivider(msgId)` | `"m:$msgId/compact"` | `"compact"` |
| `VoiceTranscript(msgId, blockId)` | `"m:$msgId/b:$blockId"` | `"voice-transcript"` |
| `Notice(id, kind)` | `"n:$id"` | `"notice"` |
| `TurnFooter(msgId, streaming)` | `"m:$msgId/footer"` | `"footer"` (the 2 dp progress bar and the per-message actions) |

- **Stable ids are required from spec 12.** `MessageId` and `BlockId` must be identical for the same content whether
  it arrived live or from a REST page. Today every refetch mints new UUIDs (`old/network/ApiClient.kt:698`) and
  re-keys the whole list (inv03 §4.2). If spec 12 cannot guarantee that for some case (e.g. orchestrator history, which
  has no ids — inv01 §6.3), `HistoryMerger` keeps the live ids for the tail it already holds, and only REST-only items
  get derived ids (`"h:$sessionId:$absoluteIndex:$blockIndex"`).
- **Errors are not messages.** Connection errors, reconnect failures and voice errors go to `ConversationUiState.banner`
  or the inline card area, not into the list. This removes the "Error: Failed to connect" bubble every 3 s
  (inv03 §8 bug 2) and keeps rewind indices correct (inv03 §4.4.3).
- **"N steps" tool grouping (IA §9.3, approved).** The flattener folds every run of ≥ 2 consecutive `ToolUse`
  blocks inside one assistant message (no text or thinking block between them) into a `ToolGroup` header followed by
  its member `ToolCard` items. Rules:
  - The group is **expanded while live**: the run is still the tail of a streaming message, or any member is executing.
  - It **auto-collapses** to the one-line summary ("5 steps · Read, Edit ×3, Bash", with an error count if any)
    as soon as a text or thinking block follows the run, or the turn completes. History groups start collapsed.
  - A user toggle wins over the automatic state, kept per group key in the ViewModel (`SavedStateHandle`).
  - Every card inside stays individually expandable. When a group is collapsed, its members are simply not emitted as
    items. Keys stay stable, so expanding a group does not disturb the scroll anchor.
  - A single tool call is never grouped.
  - `ChatListFlattenerTest` covers the expand/collapse transitions with the `r4` fixtures.
- **Per-message actions** (copy, select text, rewind to here, fork from here) are reached by **long-pressing** the
  message's footer or bubble. They are not a ⋮ on every message (inv03 §8). System notices have no actions.
- **Text selection:** `SelectionContainer` cannot span `LazyColumn` items. "Select text" opens a bottom sheet with the
  message's full text in a selectable `Text`. "Copy" copies the raw markdown.

### 3.2 Streaming markdown — custom renderer on commonmark-java

**The field bug this must never reproduce** (`project_wakeword_turnbased_unified_loop_2026_07_21.md` item 8, commit
`31c2fbf`): SIGSEGV stack overflow in `libhwui.so` on the A300M after "Skipped 344 frames". The custom Compose renderer
re-parsed and re-laid-out the whole message on every delta, an open ``` fence turned the rest of the message into one
giant horizontally scrolling RenderNode, and the inline recursion had no depth limit. The current mitigations are
incremental prefix parsing, wrapping code blocks, `MAX_INLINE_DEPTH = 8` and non-animated scrolling
(`old/ui/components/markdown/MarkdownText.kt:60-96`). The main app runs on a fast phone, but the same failure shape
(work proportional to message length on every delta) causes jank there too. So the design makes per-delta work
**O(tail)**.

**Library decision.**

| Option | Verdict |
|---|---|
| `com.mikepenz:multiplatform-markdown-renderer-m3` 0.45.0 | Rejected. It parses the full string and renders the document as one composable. We can't split it into lazy items, and it re-parses on every change. |
| compose-richtext | Rejected for the same reason, and its maintenance is uncertain. |
| Markwon | Rejected. It is Views/`TextView`-span based and effectively unmaintained. |
| **commonmark-java 0.30.0 + our Compose renderer** | **Chosen.** A mature CommonMark + GFM parser (tables, strikethrough, task lists, autolinks, YAML front matter as extensions) with source spans, pure Java, no Android dependency, so it is JVM-testable. We control block splitting, so each top-level block is its own lazy item. |

**Algorithm (`:core:markdown` `MarkdownDocument`).**

1. `BlockSplitter` is a line scanner that tracks fence state (``` and ~~~ with length), list or quote continuation,
   and table rows. It maintains `stableEnd`, the end offset of the last **closed** top-level block: a blank line
   outside any fence, or a closing fence.
2. `append(delta)` scans only the new characters. When `stableEnd` advances, only the newly closed segment is parsed
   with commonmark (one `Parser.parse` per closed block). The resulting `MdNode`s are **appended** to an immutable
   `stableBlocks` list. Old blocks are never re-parsed.
3. The **tail** (from `stableEnd` to the end) is the only thing re-processed per update, and its rendering is cheap
   and capped:
   - inside an open fence → `MdNode.CodeTail(lang, text)`, plain monospace text with `softWrap = true`, **no
     horizontal scroll, no highlighting**;
   - a table header seen but the table not closed → plain monospace text of the raw rows;
   - otherwise → parse the tail as one paragraph-level block (it is small by construction).
4. When the message completes (`streaming = false`), the tail is parsed one last time and the document is frozen.
   History messages are parsed once, off the main thread (`Dispatchers.Default`), and cached in an LRU
   (`MarkdownCache`, 200 entries keyed by `blockId` + text length + hash).
5. Inline content → `AnnotatedString` with `SpanStyle`s and `LinkAnnotation`s. The recursion depth cap is 8 (port of
   `MAX_INLINE_DEPTH`); deeper nesting is flattened to plain text.
6. Known limitation, documented: splitting at blank lines means a reference-style link definition `[x]: url` only
   resolves within its own block, and a loose list split by blank lines renders as consecutive lists. CommonMark keeps
   the start number, so the numbering still reads correctly. LLM output almost never uses reference links.

**Proof obligations (tests in `:core:markdown`):**
- `StreamingEquivalenceTest`: for 40 recorded assistant messages (taken from fixtures and JSONL), feeding the text in
  random-sized deltas yields the same final `MdNode` list as parsing it all at once.
- `TailWorkBoundTest`: with a 20 KB stable prefix, appending one character parses ≤ 1 block (instrumented parser
  counter).
- `OpenFenceTest`: a 5,000-line unterminated fence renders as a wrapped `CodeTail` with no horizontal-scroll modifier
  (semantics check in a Compose test).
- Port the 5 regression cases of `android/app/src/test/.../ui/markdown/MarkdownParserTest.kt`, including the
  `hrRegex` backreference case.

### 3.3 Code, tables, links, images

- **Code blocks (closed):** `CodeBlock` shows a header with the language label and Copy ("Copied" for 2 s, as on the
  web, inv02 F-03). The body is wrapped by default. A "No wrap" toggle enables `horizontalScroll` **for closed blocks
  only**. Height is capped at 480 dp, with "Show all (N lines)" beyond that.
  - Highlighting uses `dev.snipme:highlights` 1.1.0 (pure Kotlin, many languages, theme-able). It runs on
    `Dispatchers.Default` after the fence closes and is cached by `(lang, hash)`.
  - Blocks over 16 KB or with unknown languages stay plain. Colors come from the tokens' extended code palette
    (`LocalExtendedColors.code*`); there are no hard-coded hex values (fixes inv03 §8 bug 10).
  - Fenced code **without** a language is still a code block (the web's no-language bug, inv02 F-03, is not copied).
- **Tables (closed):** `MarkdownTable` measures each column by the max intrinsic width, capped at 280 dp, inside one
  `horizontalScroll`. Cells render the **full inline AST** (bold, code, links), because a cell reconstructed from plain
  text renders blank (the lesson of `project_compat_remark_gfm_shim.md`). Header row on `surfaceContainerHigh`.
- **Task lists** become read-only checkboxes. **Strikethrough** and **autolinks** are supported. Headings are h1–h6
  (inv03 §5 lists h4–h6 as missing today).
- **Links:** `LinkTarget.classify(href, context)` returns one of:
  - `Memory(path)` → in-app navigation (§4.1);
  - `BackendPath(path)` (e.g. `/uploads/…`, `/<viz>.html`) → in-app Visual or Custom Tab against the current server
    origin;
  - `External(url)` → Custom Tab (`androidx.browser` 1.10.0);
  - `Anchor(id)` → scroll to the heading in the same document.
- **Images:** not rendered in v1. The alt text is shown as a link (parity: the web renders none beyond the markdown
  default, inv02 F-03). Adding Coil 3 later is a separate decision.

### 3.4 Tool cards (`:feature:toolcards`, parity with `frontend/src/components/ToolUseBlock.tsx`)

`ToolCatalog` ports, table by table, the web's name normalisation (`:58-93`), categories (`:101-150`), summary
(`:209-350`) and icons (`:356-450`). A JVM test (`ToolCatalogParityTest`) checks the summary string for one input per
row of inv02 F-05.

**Orchestrator vs Qwen name clash:** the orchestrator's `read_file`/`write_file` are told apart from Qwen's by
`sessionKind == ORCHESTRATOR`, not by name. This fixes the web's unreachable branch (inv02 F-05 note). Orchestrator
tools added since then (`respond_to_agent_permission`, `run_script`, `end_voice_session`,
`get_assistant_config`/`update_assistant_config`, `listen_recording`) get explicit summaries instead of falling into
SYSTEM with a raw name (inv03 §1.2).

| Renderer | Tools | Collapsed | Expanded |
|---|---|---|---|
| `TaskCard` | Task / agent | always expanded: description, agent type, prompt | output as markdown, collapsed when over 2,000 chars |
| `TodoCard` | TodoWrite | always expanded checklist (pending / in progress (bold activeForm) / completed (strike, 55% alpha)) | — |
| `BashCard` | Bash, run_shell_command | description + highlighted command, 5 lines + "… N more lines" | full command + output. **While running, shows "Running…" with a spinner, never empty** (fixes the web gap in inv02 §6.4 and R7). |
| `EditCard` | Edit, MultiEdit, replace | path + "+a −r" counts | `EditDiffView` (§3.5) |
| `WriteCard` | Write, write_file (agent) | path + line count | highlighted content (language from the extension map, `ToolUseBlock.tsx:165-198`), 400 dp cap |
| `ReadCard` | Read, read_file (agent) | "Read …/a/b.kt" + range | range line; output as monospace with line numbers if present |
| `SearchCard` | Grep, Glob, WebSearch, search_history, search_memory | pattern or query | options (path, glob, mode, ±context); output |
| `FetchCard` | WebFetch | URL host + path | URL, prompt, output as markdown |
| `AgentSendCard` | send_to_agent_session, read_agent_session, list/open/close/interrupt | "session <id8>" + message preview | session id, message, output |
| `PlanCard` | ExitPlanMode, EnterPlanMode | "Exit plan mode" | plan as markdown |
| `ScriptCard` | chrome-devtools evaluate_script, run_script | function/script name | highlighted JS (200 dp cap), args |
| `GenericCard` | everything else (incl. `mcp__*`) | summary from catalog | input as key: value (values over 400 chars collapse) or pretty JSON; output |

Common `ToolCardShell` (`:core:design`): a leading category icon on a category-tinted container (tokens' extended
`tool*` colors), a one-line summary (bodyMedium), and a status. The status is a 16 dp spinner while executing, a check
when done, and an error icon on error, using labelSmall text at ≥ 11 sp (today's pill uses 9 sp, inv03 §8). Tap toggles
expansion. Output is plain monospace; error output uses the `error` role. **Output is shown whenever `result != null`,
whatever the expanded or streaming state.** The "no output" text appears only when the result is an empty string; this
is part of R7's acceptance test.

### 3.5 Edit diff view

- `EditDiffView(old, new)`: `com.github.difflib.DiffUtils.diff(oldLines, newLines)` (java-diff-utils 4.17), computed
  on `Dispatchers.Default` and cached by `toolUseId`. It is rendered as unified rows with `+`/`−`/` ` markers and
  `diffAdded`/`diffRemoved` extended token colors, the same semantics as the web's `Diff.diffLines`
  (`ToolUseBlock.tsx:485-529`).
- Improvements over the web: unchanged runs longer than 6 lines collapse to "⋯ N unchanged lines" (tap to expand).
  Gutter line numbers come from the diff's positions. `replace_all` shows a "Replace all occurrences" chip.
- MultiEdit renders one `EditDiffView` per edit, with a separator.
- Height cap 480 dp + "Show full diff". Lines wrap (no horizontal scroll), so long lines never create wide
  RenderNodes.

### 3.6 Scrolling and performance budget

- `LazyColumn(reverseLayout = true)` over the reversed item list. Bottom anchoring is native, so streaming growth stays
  pinned without the per-delta `scrollToItem` calls that today's code needs (`ChatScreen.kt:104-149`). A **new** item at
  the bottom (the next markdown block, a tool card) is not covered by that: LazyList keeps its position on the first
  visible item's key, so the list would stay on the old item with the new one below the fold. `ChatList`'s
  `FollowNewest` calls `requestScrollToItem(0)` when the newest key changes while the list is at the bottom (within
  24 dp, no scroll in progress); test `FollowNewestTest`. When the user has scrolled up, keys preserve their position. The "Jump to latest" FAB uses `animateScrollToItem(0)` (user
  initiated only; nothing animates during streaming, per `06fe06f`).
- Load older: when the last visible index is within 3 of the end, call `loadOlder()`. A guard keyed on the oldest
  message id prevents the infinite load loop (port of `5c029d6`).
- Fold long user messages: more than 25 lines collapses to 150 dp with "Show all (N lines)" (parity, inv03 §1.2).
- Budget (POCO X7, `benchmark` build, macrobenchmark `StreamingChatBenchmark` replaying a recorded 2,000-token turn
  with 12 tool calls): P95 frame time ≤ 16 ms, no frame > 48 ms, and zero recompositions of non-tail `MdBlock` items
  while streaming (checked in a Compose test with a recomposition counter).

---

## 4. Memory and Visuals (new on Android, R6)

### 4.1 Memory

**Data:** `MemoryRepository.tree(): StateFlow<Resource<List<MemoryNode>>>` from `GET /api/memory/tree`.
`document(path)` comes from `GET /memory/<path>`, with each path segment percent-encoded. The web doesn't encode
(inv02 F-37); we do. OkHttp's disk cache (10 MB) is used with `If-None-Match`/ETag (the backend sends `etag` and
`last-modified`, inv01 §3.4), so reopening a document is instant when nothing changed. Refresh: on screen entry, by
pull-to-refresh, and by the Reload action. There are no push events for memory (inv01 G-27).

**Tree browser** (`MemoryTreePane`): the tree is flattened into `LazyColumn` rows, one per visible node, keyed by
`path`.
- Folders: chevron + name + a file-count badge. Top-level folders start expanded and deeper ones collapsed (web
  parity). Expanded state persists in `SavedStateHandle`.
- Files: a document icon + the name without `.md` (web parity).
- A **filter field** at the top (client-side, substring on path) flattens the tree to matches with their parent path
  as a subtitle. The web has no search here (inv02 F-37); the IA asks for one (IA §3).
- Compact: a full screen with a back arrow. Expanded: opening a document **adds it as a workspace tab** (IA §9.2,
  today's web behaviour; dedup by path). The Memory destination's list pane stays visible beside it.

**Document view** (`MemoryDocumentScreen`):
- `Frontmatter.split(text)` splits a leading `---\n…\n---` block (CRLF tolerant, the same rule as
  `frontend/src/components/MemoryPanel.tsx:88-100`).
- The frontmatter shows as a **collapsed** card labelled "Frontmatter". Its summary line shows `category`, `modified`
  and up to 3 `tags`, parsed leniently with `commonmark-ext-yaml-front-matter` (scalar and list values only). Expanding
  it shows the raw YAML in monospace. A parse failure still shows the raw text.
- The body is rendered by `:core:markdown`, non-streaming, with the same styles as chat. Long documents use the same
  per-block lazy items, so a 2,000-line memory file scrolls smoothly.
- **Links between memory files** (`[x.md](../projects/frontend-refactor/folder/x.md)`): `MemoryLinkResolver.resolve(currentPath, href)`
  normalises `.` and `..` against `dirname(currentPath)`, strips `#fragment`, and rejects paths that escape the root.
  - A `.md` target → push `MemoryDoc(resolved)` (Compact) or replace the detail pane (Expanded). Back returns to the
    previous document.
  - A fragment → scroll to the heading whose slug matches (GitHub slug rules).
  - A resolved path missing from the tree → snackbar "Not found: folder/x.md" with "Open anyway" (the tree may be
    stale).
  - `http(s)` → Custom Tab. `/memory/...` absolute → in-app.
  - Unit-tested against every link form found in `context/memory/**` (WP B-07 greps them into a test fixture list).
- Toolbar: title (file name), Reload, Copy path, Open raw (Custom Tab to `<origin>/memory/<path>`).
  States: loading skeleton, error with Retry ("Could not load <path> — <error>"), content.

### 4.2 Visuals

**Data:** `VisualsRepository.list()` from `GET /api/visualizations` (sorted by `modified` desc).
- Refetched on entry, by pull-to-refresh, and **after any `turn_complete`** in any session, debounced to 2 s
  (web parity, inv02 F-36).
- Rename: `PATCH /api/visualizations/rename {path, title}` from the item's ⋮ menu. Optimistic, rolled back on error.
- There is no delete (none exists on the backend).
- The URL is `serverOrigin + "/" + path` with each segment percent-encoded (inv01 §3.3: `url` is not encoded).

**List** (`VisualsListPane`): cards with title, parent folder name ("public" for root files), and relative modified
time computed from UTC (`java.time`). The empty state uses the web's copy.

**Detail** (`VisualScreen`, IA §9.4): **full-screen in-app WebView** on Compact (top app bar collapses on scroll;
the system bars stay). On Expanded it opens as a workspace tab (IA §9.2), deduplicated by `viz:<path>`. Toolbar:
title, **Reload**, **Open in browser** (Custom Tab, or `ACTION_VIEW` if the user prefers), **Share link**
(`ACTION_SEND` text with the full URL), and **Show on TV**.

**Show on TV — pending backend endpoint.** The proposed additive endpoint is `POST /api/visualizations/cast {path}`,
wrapping the existing TV display script. It is in the backend-changes proposal and **not approved yet**. The client
must work with or without it:

- `VisualsRepository.castCapability: StateFlow<CastCapability>`, with values `Unknown`, `Available` or `Unavailable`,
  is resolved **once per server connection**. The repository first reads a capability flag if the backend
  later exposes one (spec 12 / backend proposal decides, e.g. a `capabilities` list in `GET /api/config`).
- Otherwise it probes with `POST /api/visualizations/cast` and body `{}`:
  - 400 or 422 (endpoint present, `path` missing) → `Available`;
  - 404 or 405 → `Unavailable`. Today a POST to an unknown path matches only the GET catch-all, so it returns 405
    (inv01 §1.4);
  - network error → stay `Unknown` and retry on the next connect.
- The action is **hidden** unless `Available`. It is not shown greyed out, so nothing hints at a feature the
  server lacks.
- Tapping it posts `{path}` and shows a snackbar: "Showing on TV" on 2xx, or the server's `detail` verbatim on an
  error. A 404/405 at that point flips the capability to `Unavailable` and hides the action.
- The cast itself never navigates the app or blocks the WebView.
- The probe and the call live in `ArchieApi.castVisualization(path)` (`:core:network`). The capability lives in
  `:core:data`, so the web and Android follow the same rule. `CastCapabilityTest` (MockWebServer: 422 → Available,
  405 → Unavailable, 404 → Unavailable, IOException → Unknown) and a UI test asserting that the action is absent when
  `Unavailable` are part of B-07's definition of done.

**`ArchieWebView` security configuration** (`:feature:visuals`):

| Setting | Value | Why |
|---|---|---|
| `javaScriptEnabled` | true | Visualizations are interactive HTML with inline JS (inv01 §3.3). |
| `domStorageEnabled` | true | Some vizzes keep state in localStorage (the web iframe allows same-origin for this, inv02 F-36). |
| `allowFileAccess`, `allowContentAccess` | false | No local file access. |
| `setSupportMultipleWindows(false)`, `javaScriptCanOpenWindowsAutomatically = false` | — | No popups. The web's iframe allows popups; a WebView can't host them, so `target=_blank` goes through `shouldOverrideUrlLoading`. |
| `mixedContentMode` | `NEVER_ALLOW` | — |
| `addJavascriptInterface` | **never** | No native bridge. A viz is arbitrary agent-written code. |
| `shouldOverrideUrlLoading` | same origin as the server → load in place; anything else → Custom Tab | Equivalent to the web's "no top navigation". |
| `DownloadListener` | asks first, then `DownloadManager` | The web blocks downloads; here we allow them only with a prompt. |
| `WebChromeClient.onPermissionRequest` | deny all (camera, mic, MIDI) | — |
| `onGeolocationPermissionsShowPrompt` | deny | — |
| `onShowFileChooser` | return false | — |
| Algorithmic darkening | `WebSettingsCompat.setAlgorithmicDarkeningAllowed(settings, false)` | Vizzes ship their own palettes. |
| Console | `onConsoleMessage` → logcat tag `ArchieViz` in debug builds only | — |
| TLS errors | `onReceivedSslError` → `TrustStore.isPinned(host, error.certificate)` ? `proceed()` : `cancel()` + error page | §4.3. **Never** a blanket `proceed()`. |

**State retention:** the web keeps the iframe mounted while its tab is hidden, which is **load-bearing** (inv02 F-36).
`WebViewPool` (process-scoped, max 3 entries, LRU by path) keeps WebViews alive across tab switches and rotation. Each
is created with a `MutableContextWrapper` whose base context is swapped to the current Activity on attach and to the
Application on detach, so no Activity leaks. `onTrimMemory(RUNNING_LOW)` and above evict all but the visible one.

**Risk noted:** a viz runs with the backend's origin and the backend has no auth (inv01 §1.3), so a viz's JS can call
any `/api` endpoint, including `GET /api/config/openai-key`. This is the same exposure as the web iframe with
`allow-same-origin`. Mitigation would be a backend change (serve vizzes from a separate origin or port); it is listed
as a risk, not designed here.

### 4.3 TLS: today and target

**Today:** the backend's nginx serves the same app on `:80` (HTTP) and `:443` (self-signed certificate,
`server_name 192.168.0.200 server.local`, inv01 §1.1). The Android app connects to the default
`ws://192.168.0.200:80` in cleartext. `network_security_config.xml` allows cleartext globally and trusts **system CAs
only**, so `wss://`/`https://` to the Jetson fails, and there is no pinning or user-CA support (inv03 §3.7). Everything
goes over cleartext on the LAN, including the raw OpenAI key from `GET /api/config/openai-key` (inv04 R1). The web
needs HTTPS only because browsers require a secure context for the mic; native Android does not.

**Target design — `TrustStore` in `:core:network`, three modes per saved server:**

| Mode | When | OkHttp | WebView |
|---|---|---|---|
| `Cleartext` | `ws://`/`http://` URLs (LAN default, parity) | plain | plain |
| `PinnedSelfSigned` | an `https`/`wss` server whose certificate fails system validation | On first connect, `ServerTrustDialog` shows the certificate subject, validity and **SHA-256 SPKI fingerprint**, and the user confirms (trust on first use). The pin is stored per `host:port` in `SettingsStore`. `X509TrustManager` composite: system trust first, then exact-pin match. `HostnameVerifier`: default, or accept when the leaf matches the pin (self-signed certs often lack an IP SAN). | `onReceivedSslError`: extract the leaf from `SslError.certificate` (`getX509Certificate()` on API 29+; on API 26–28 via `SslCertificate.saveState(...).getByteArray("x509-certificate")`), compare its SPKI hash with the pin → `proceed()` only on match. |
| `SystemTrusted` | `https`/`wss` that validates (e.g. Tailscale MagicDNS with `tailscale cert`) | default | default |

- A changed certificate on a pinned host is treated as an error, with "Review new certificate" (re-pin), never a
  silent proceed.
- `network_security_config.xml` (main) keeps `cleartextTrafficPermitted="true"` on the base config: LAN IPs can't be
  enumerated in NSC, and the user can add any server. It adds `<debug-overrides><trust-anchors><certificates
  src="user"/></trust-anchors></debug-overrides>` for debugging with a proxy.
- The lite app stays on `Cleartext` (LAN only, API 21).
- Recommendation (decision Q5): either keep LAN cleartext (status quo) and use Tailscale with
  `tailscale cert` (real certificate, `SystemTrusted`) for anything off-LAN, or issue the nginx certificate from a
  small private CA with IP SANs, so pinning becomes CA-based. Both work with this design; neither blocks any WP.

---

## 5. Lite app (`:app-lite`, `com.assistant.peripheral`, Samsung A300M)

UX copy and visuals belong in `15-lite-app.md` (todo). This section fixes the architecture.

### 5.1 Shape

- **One Activity**, `com.assistant.peripheral.MainActivity`: plain framework `android.app.Activity` with
  `Theme.Material` (API 21). **No AppCompat, no Material Components, no Compose, no WebView, no RecyclerView.** It has
  two content states, *Face* and *Settings*, swapped inside one root `FrameLayout`. Back from Settings returns to the
  Face.
  - It handles `MAIN/LAUNCHER` and `ACTION_ASSIST`, with `launchMode="singleTask"`,
    `showWhenLocked`/`turnScreenOn` in the manifest, plus pre-O_MR1 window flags in code
    (`FLAG_SHOW_WHEN_LOCKED | FLAG_TURN_SCREEN_ON | FLAG_DISMISS_KEYGUARD | FLAG_KEEP_SCREEN_ON` while voice is
    active), per inv04 §5.1.
  - The old `VoiceShortcutActivity` trampoline is dropped. Only `ACTION_ASSIST` needs to resolve. The user re-selects
    the assist handler once after the upgrade (L-F2).
- `ButtonAccessibilityService` keeps its FQCN (§1.7) and checks `enableButtonTrigger` (600 ms long-press, inv04
  §4.5) before firing `TriggerIngress.recents()`.
- **Dropped:** the raw `/dev/input/event2` monitor. It ignores the toggle (B5), and without root it most likely fails
  with EACCES (inv03 §1.8). A-08's port verifies this first: if the old app's logcat on the A300M shows
  `/dev/input/event2` failing with EACCES, the monitor is gone. If it shows the monitor working, it is ported behind the
  toggle (decision Q8).
- `AssistantVoiceInteractionService` is fixed per B6 only if field test L-F5 shows the A300M routes long-press home to
  a VIS rather than to `ACTION_ASSIST`. Otherwise it is dropped.

### 5.2 State face

`StateFaceView` is one custom `View` drawn on a `Canvas`. Text uses `TextView`s laid out in a vertical `LinearLayout`.

```
┌──────────────────────────── 540×960 (360×640 dp) ────────────────────────────┐
│ ● Connected · Jetson                          [reconnect banner if any]  ⚙   │  status row, 18 sp
│                                                                               │
│                        ( large orb, 200 dp, level ring )                      │  state color + icon
│                               LISTENING                                       │  one word, 44 sp
│                         "Say "wake up" or "my friend""                        │  hint, 18 sp
│                                                                               │
│  You: what's the weather tomorrow                                             │  last user, 20 sp, 3 lines max
│  Archie: Tomorrow in Rio it'll be 27° and sunny…                              │  last assistant, 24 sp, 5 lines max
│                                                                               │
│ [        START / END  (96 dp, full width)        ]  [ 🔇 ]                     │
└───────────────────────────────────────────────────────────────────────────────┘
```

| `VoiceUiState` → face | Word | Color role (tokens) | Icon |
|---|---|---|---|
| wake `Armed` | Ready | `surface` + `primary` orb outline | mic |
| wake `Confirming` | Listening… | `secondary` | ear |
| wake `Capturing` / `Sending` | Recording / Sending | `tertiary` | record / send |
| voice `Connecting` / `Summarizing` | Connecting / Preparing | `tertiaryContainer` | spinner |
| voice `Active` / `Listening` | Listening (+ "Ns" after 3,000 ms, inv04 §4.10) | extended `voiceListening` | mic |
| voice `Speaking` | Speaking | extended `voiceSpeaking` | speaker |
| voice `Thinking` / `ToolUse` | Thinking / Working | extended `voiceThinking` / `voiceTool` | brain / build |
| voice `Ending` | Ending | `outline` | — |
| remote active | Active elsewhere | `surfaceVariant`, button disabled | phone-ring |
| `Error(msg)` | Error + the message (shown, fixes inv03 §8 bug 7) | `error` | alert |
| offline / mic stalled / Whisper unreachable / wake disabled | Offline / Mic busy / Can't confirm / Wake off | `errorContainer` / `outline` | per state |

- **Animation:** the orb's level ring is driven by `vad` and audio level, updated at ≤ 20 fps through a single
  `ValueAnimator`. It stops when `!isShown`, when the screen is off, or when idle (no animation in `Armed`). There is
  no `rememberInfiniteTransition`-style per-item animation anywhere (inv03 §1.5 perf).
- **Colors:** the token generator produces Compose `Color`s in `Tokens.kt`, which Views can't use. Request to the
  `design/tokens` owner: also emit `dist/android/values/archie_colors.xml`, `values-night/archie_colors.xml` and
  `archie_dimens.xml`. If that output does not exist when C-01 starts, C-01 hand-copies the ~20 color roles it needs
  into `:app-lite/src/main/res/values/colors.xml`, with a JVM test that compares them with `Tokens.kt` hex values.
  The A300M face is **dark only** and ignores the system day/night setting, for across-the-room legibility (D3
  dark-first).
- **Text:** the last exchange comes from `VoiceHost.state.lastExchange` (fed by the lite `LastExchangeSink :
  TranscriptSink`). Plain text only: markdown is stripped with a regex pass (`**`, backticks, `#`, list markers, link
  syntax → text), and there is no parser on the A300M. Each side is truncated to 600 chars.

### 5.3 Settings view

A `ScrollView` of hand-built rows (`SettingsRowView`: label + one control + one-line helper), bound to `SettingsStore`.
The groups mirror IA §7 "This device", which is the whole lite scope (inv04 §9.2):

| Group | Rows |
|---|---|
| Connection | current server + status + Connect/Disconnect; saved and discovered servers (≤ 10 rows, `LinearLayout`); Scan; Add (dialog: label + URL); Auto-connect |
| Audio | Mic gain (`SeekBar` 0–150%, 10% steps), Echo ducking (0–10%, 0.5% steps), Output (`RadioGroup` Auto/Speaker/Earpiece/BT/Wired, availability live from `DeviceWatcher`) |
| Wake word | Enable; Talk phrases; Wake phrases (both `EditText`, **commit on IME Done or focus loss**, which removes the separate Save buttons, inv03 §8); Wake sensitivity; Talk auto-stop sensitivity |
| Triggers | Recents long-press (+ "Open Accessibility settings" button) |
| About | real versionName/versionCode (fixes inv03 §8 bug 9), server host, backend version if available |

Sliders commit on release (`onStopTrackingTouch`). Every change goes through `VoiceHost.updateSettings` (single ingress,
inv04 RS-33).

### 5.4 Consuming the shared core

`LiteGraph` builds `SettingsStore`, `HttpStack`, `VoiceHostRuntime` (with
`HostConfig(launchActivity = MainActivity::class.java, notification = liteNotificationSpec)`), and the lite
`LastExchangeSink`. That is all. The lite app does **not** use `:core:conversation`, `:core:data` or any Compose
module.
- Orchestrator adoption is automatic (`OrchestratorChannel`). There is no conflict dialog (inv04 §9.2 item 6).
- `NoOrchestrator` → the face shows "No conversation open on the server", and the Start button sends `start` without
  a local id. The backend creates one (same as the old History FAB path, without the UI).

### 5.5 What runs where

| Piece | Runs in | Notes |
|---|---|---|
| `LiteApplication.onCreate` | process start | Builds the graph and **always** calls `startService(VoiceHostService)` (API 21: no background limits). So *any* process start re-arms the host: watchdog launch, accessibility rebind, sticky restart. |
| `VoiceHostService` | FGS, `START_STICKY`, notification **id 1001, channel `assistant_service_channel`** (same ids as today, inv04 §4.5) | Owns nothing itself; holds the runtime alive. It is the "long-running service in package" that the companion's watchdog looks for (inv04 §6.4 contract 3). |
| `VoiceHostRuntime` | process | WS, adoption, voice, wake, cues, screen re-arm, trigger ingress. **On a wake or talk trigger it acquires the 3,000 ms `SCREEN_BRIGHT \| ACQUIRE_CAUSES_WAKEUP` wake lock** (inv04 §4.5 **LB**) **and starts `MainActivity`** with `FLAG_ACTIVITY_NEW_TASK \| FLAG_ACTIVITY_REORDER_TO_FRONT` (allowed on API 21). Voice itself starts in the runtime, so a slow Activity start no longer loses the trigger (R2). |
| `MainActivity` | UI | Binds `LocalBinder`, renders `VoiceHost.state`, sends commands. No receivers, no voice logic. |
| `ButtonAccessibilityService` | system-bound | Calls `TriggerIngress`. |

### 5.6 Memory budget (888 MB device; `MemFree` 60–79 MB, inv04 §11.4)

Baseline today: the app idles at ~85 MB without Vosk, and Vosk adds ~80–100 MB resident (inv04 §9.1). Targets,
measured with `adb shell dumpsys meminfo com.assistant.peripheral` (TOTAL PSS):

| Scenario | Budget | How it is met |
|---|---|---|
| Idle, Face visible, wake word armed (Vosk loaded) | **≤ 140 MB** | no Compose (−15 to 25 MB vs today), no Material/AppCompat, Canvas face, no images |
| Same without Vosk (wake disabled) | ≤ 50 MB | — |
| Active WebRTC voice session | ≤ 190 MB | WebRTC 1.1.1 as today; `PeerConnectionFactory` initialized once per process (RS-08); ADM released at session end (fixes R9) |
| Java heap (`dalvik-heap`) | ≤ 24 MB | transcripts capped (§5.2); no caches |

Also:
- `onTrimMemory(TRIM_MEMORY_RUNNING_LOW)` and above drop the discovered-server list and any non-active buffers.
- R8 on release removes unused classes (dex RSS). Shipping release is decision Q3, weighed against rollback (§1.8).
- The budget is a field-test pass criterion (L-F1). It is not enforceable in unit tests.

### 5.7 Interaction with `android-device` (unchanged companion)

| Companion contract (inv04 §6.4) | How the lite app satisfies it |
|---|---|
| 1. applicationId `com.assistant.peripheral` | §1.7 |
| 2. A LAUNCHER activity, or the FQCN `com.assistant.peripheral.MainActivity` | Both: `MainActivity` is the launcher activity at exactly that FQCN. |
| 3. A long-running service whenever healthy | `VoiceHostService` (FGS, sticky), restarted by any process start (§5.5). Caveat: an enabled `ButtonAccessibilityService` also counts as "a service of the package", so a process where the accessibility service lives but the FGS died would look healthy. `LiteApplication.onCreate` restarting the FGS covers this, because the accessibility rebind recreates the process. |
| 4. Cues on STREAM_MUSIC (DND total silence) | `VoiceCues` default implementation (inv04 §4.10) |
| 5. No WRITE_SECURE_SETTINGS / boot permission needed | The lite manifest declares neither. Boot launch comes from `BootReceiver` (+3 s). |

**In-place upgrade procedure** (C-01 DoD, field test L-F2):
1. Archive the current APK.
2. Verify the signing certificate (§1.8).
3. `adb install -r app-lite-debug.apk` (versionCode ≥ 11).
4. On first start, a one-time `LegacyMigration` runs:
   - It keeps the DataStore `settings` file and its keys.
   - It purges every `ws_resume_checkpoint:*` key (they move to the bounded `resume_checkpoints` store), which shrinks
     a file that grew without bound (inv03 §2.2).
   - It converts `saved_servers` to `saved_servers_v2`.
   - It keeps `assistant_service_prefs`.
   - It keeps `filesDir/vosk-model/` (same stamp, so no re-extract).
5. Re-select the assist handler if asked.
6. Confirm the companion watchdog stays quiet for 10 minutes (no relaunch log lines).

---

## 6. Testing

### 6.1 JVM unit tests (run on every PR: `./gradlew test`)

| Suite | Module | What |
|---|---|---|
| **Protocol fixture conformance** | `:core:conversation` (`ProtocolFixtureConformanceTest`) | Parameterized over every `shared/protocol-fixtures/*.json`. Gradle wiring: `sourceSets.test.resources.srcDir(rootProject.file("../shared/protocol-fixtures"))`, plus a generated `fixtures-index.txt` (task `generateFixtureIndex`), so tests enumerate fixtures without scanning the classpath. Each fixture feeds its inputs through `ConversationReducer` and compares the final state, and any intermediate checkpoints the fixture declares, with the expected JSON. **The test fails if zero fixtures are found** (guards against a broken path). The fixture format is owned by spec 12; Android owns only the runner. The same fixtures run in `frontend-next`, which is what makes R4 and R7 cross-platform guarantees. |
| Codec round-trip | `:core:protocol` | every frame type; recorded transcripts from the live Jetson (read-only captures) decode without loss; binary and text frames; `voice_event` recursive trees (B1) |
| Ported controller parity | `:core:session`, `:core:settings`, `:core:data` | intent of `connection/parity/*` (11), `settings/parity/*` (12), `system/parity/*` (14), `chat/conflict/*`, `chat/parity/*` (20, re-expressed as fixtures where they test reducer behaviour) |
| **Voice parity — constants** (inv04 §10.1) | each voice module | `WakeTuningTest`, `VoskTuningTest`, `SrTuningTest`, `WhisperTuningTest`, `ServiceTuningTest`, `SessionTuningTest`, `AudioTuningTest`, `EchoDuckTuningTest`, `WebRtcTuningTest`: one assertion per **LB**/*wire* constant. Plus `OldConstantsCrossCheckTest` (§6.2). |
| **Voice parity — transitions** (inv04 §10.2) | each voice module | all listed scenarios, with `:core:testing` fakes and `kotlinx-coroutines-test` virtual time (`StandardTestDispatcher` + `FakeClock` bound to `testScheduler.currentTime`). Ports the intent of the 15 `voice/parity/*` files (126 `@Test`s today). Each of RS-01…RS-46 (inv04 §11.1) maps to at least one named test, `rs01_sessionUpdateQueuedBeforeProvider()`…, and a `RegressionScenarioCoverageTest` fails if a number from 01–46 has no test. |
| API-level branches | `:core:audio`, `:core:voice`, `:core:wakeword` | **Robolectric only supports API 23–37**, so API 21/22 branches are pure policies taking `sdkInt` (e.g. `PlaybackWritePolicy.forSdk(21)` → blocking 3-arg write; `MicSourcePolicy.forSdk(22)` → `VOICE_RECOGNITION`; `RouteApplierPolicy.forSdk(21)` → no `getDevices`). They are tested on the JVM at 21, 22, 23, 26, 30, 31, 34 and 36. The real API 21 calls are covered by instrumented tests on the A300M AVD (§6.5). |
| Markdown | `:core:markdown` | §3.2 proof obligations |
| Tool catalog, flattener, link resolver, diff | features | `ToolCatalogParityTest`, `ChatListFlattenerTest` (keys stable across a live→REST refetch, R4 ordering), `MemoryLinkResolverTest`, `EditDiffTest` |

### 6.2 The voice parity gate (G-01)

The old code is in another Gradle build (`android/`) and can't be linked into `android-next/`. Parity is pinned in
three ways:
1. **Constants cross-check.** `tools/parity/extract_old_constants.py` reads every constant listed in inv04 §4 from
   `android/` at commit `e871d05` (file:line from the inventory; regex per row) and writes
   `tools/parity/old_constants.json` (checked in). `OldConstantsCrossCheckTest` asserts that every new `Tuning` value
   equals the old one. The script fails if a cited line no longer matches.
2. **Behaviour.** The §10.2 transition tests are written **before** the implementations (A-04), against the ports'
   interfaces, from inv04 §3 FSM tables. They start red and turn green as A-05…A-08 land.
3. **Logs.** Every log marker in inv04 §10.3 (`[MIC_STATE] DUCK`, `[MIC_PROBE]`, `Vosk match`, `Whisper CONFIRMED`,
   …) is a `const val` in a `LogMarkers` object, asserted by tests through a fake logger, so field logcat greps keep
   working.

**G-01 passes when:** all of §6.1's voice suites are green; `OldConstantsCrossCheckTest` is green; the release smoke
test (§1.10) is green on both AVDs; `verifyPatchedVosk` and `check_elf_alignment.sh` (report only) have run; and the
lite app on the `A300M_API21` AVD completes this scripted sequence via `adb shell input` + logcat markers (memory
`feedback_use_adb_input_for_device_tests.md`):
- arm the wake word;
- inject "wake up" audio through the emulator's virtual mic (`-allow-host-audio`), playing a recorded WAV on the host;
- see `Whisper CONFIRMED`, or a stubbed `WhisperClient` in a debug-only config;
- reach voice `Connecting` against a local mock backend (`mockwebserver3` scripted `/api/orchestrator/voice/session`
  returning an error);
- see voice `Error` → finalize → wake re-armed after 1,500 ms (RS-09, RS-30).

**No device field test (D-01, or any WP's "field" step) may start before G-01 passes.** The live Jetson is used
read-only, and voice and orchestrator sessions are opened on it only after telling Rodrigo (charter working rules).

### 6.3 Compose UI tests

These run on the JVM with Robolectric (`createComposeRule`, `@Config(sdk = [36], qualifiers = "w443dp-h986dp-xxhdpi")`).
A small instrumented smoke subset runs on `POCO_X7`.

| Test | Asserts |
|---|---|
| `ToolOrderingFixtureUiTest` | for each fixture tagged `r4`, rendered item order equals the fixture's expected order (text/tool interleaving in orchestrator voice and text turns) |
| `ToolResultVisibleUiTest` | for fixtures tagged `r7`, every completed tool card shows its output, live and after refetch |
| `ComposerStatesUiTest` | Voice → Send → Stop morph; queued send while working; disabled states with reasons |
| `InlineCardsUiTest` | permission Approve, Reject, and type-to-reject; stall Interrupt (the termination card was removed 2026-10-10: a terminated session's view closes, spec 12 OPEN-3; it had fixed inv03 §8 bug 5) |
| `SessionSwitcherUiTest`, `DrawerUiTest` | Open-now list, close, new Archie/agent session |
| `MemoryLinkNavigationUiTest` | tapping `../folder/x.md` opens that doc; Back returns |
| `SettingsSaveUiTest` | snackbar "Saved" / server error verbatim + Retry; MCP toggle semantics |
| `VisualWebViewSecurityTest` (instrumented) | JS on; file access off; foreign navigation leaves the WebView; pinned-cert proceed / unpinned cancel against a local HTTPS `mockwebserver3` with a self-signed cert |

### 6.4 Screenshot tests (visual QA, R1/R2)

- **Tool: Roborazzi 1.76.0** on Robolectric Native Graphics (`@GraphicsMode(NATIVE)`). Why Roborazzi and not
  Paparazzi: it shares the Robolectric runtime with the UI tests (one infrastructure), captures Compose **and** Views
  (for lite), and supports compare/verify tasks in CI. Native graphics require SDK ≥ 26, so the lite app's Views are
  captured at `sdk=26` with `w360dp-h640dp-hdpi` qualifiers. The real API 21 look is checked on the AVD and device.
- **Golden sets**: main app at Compact (`w443dp-h986dp-xxhdpi`, the POCO), Medium (`w700dp-h1000dp`) and Expanded
  (`w1280dp-h800dp`), each in dark and light, at font scale 1.0 and 1.3. Lite at `w360dp-h640dp-hdpi`, dark only.
- **Scenes** are named identically to the web's Playwright scenes (`chat-streaming-tools`, `chat-voice-active`,
  `permission-card`, `session-switcher`, `drawer`, `settings-home`, `settings-audio`, `memory-tree`, `memory-doc`,
  `visuals-list`, `lite-face-*`). Each is driven by the **same** `shared/protocol-fixtures` state, so both platforms
  render identical content.
- Goldens live in `N/<module>/src/test/screenshots/`. `./gradlew recordRoborazziDebug` records them;
  `verifyRoborazziDebug` runs in `check`.
- **Side by side:** `tools/visual-qa/side_by_side.py` pairs `*/build/outputs/roborazzi/<scene>_<size>_<theme>.png` with
  the web's `<scene>_<size>_<theme>.png` and writes one HTML contact sheet to
  `docs/projects/frontend-refactor/qa/android-vs-web.html` (gitignored output, like `audit/screenshots`). Rodrigo reviews it per
  phase.

### 6.5 Emulator profiles

| AVD | Spec | Status | Used for |
|---|---|---|---|
| `POCO_X7` | x86_64, API 36 `google_apis_playstore`, 1220×2712, 440 dpi, 3 GB | exists | main app smoke, instrumented tests, release smoke, macrobenchmark |
| `A300M_API21` (new) | `system-images;android-21;google_apis;x86` (download ≈ 400 MB), 540×960, `hw.lcd.density=240` (hdpi), `hw.ramSize=1024` (the closest the emulator handles; 888 MB is real on the device), `vm.heapSize=96`, `hw.cpu.ncore=2`, audio input enabled | to create in A-01 | lite app smoke, API 21 instrumented tests (RS-25 `getDevices`/4-arg-write paths), release smoke |

Creation (A-01):
```
sdkmanager "system-images;android-21;google_apis;x86"
avdmanager create avd -n A300M_API21 -k "system-images;android-21;google_apis;x86" -d "Nexus S"
# then set in ~/.android/avd/A300M_API21.avd/config.ini:
hw.lcd.width=540  hw.lcd.height=960  hw.lcd.density=240  hw.ramSize=1024  vm.heapSize=96  hw.cpu.ncore=2  hw.audioInput=yes
```
API ambiguity to settle first: Android 5.0.2 is API **21**, but inv04 §5.1/§5.2 call the A300M "API 22". Run
`adb -s 192.168.0.225:5555 shell getprop ro.build.version.sdk`. If it returns 22, use the `android-22` image instead.
Both exist.

Emulator builds need `-Parchie.emulatorAbis=true` (§1.6). The API 21 AVD also needs the x86 patched `libvosk.so`
(§1.9).

**Resource rule (charter):** at most **one** emulator *or* one browser running at a time; never both.
- Start emulators with `-no-snapshot-save -no-boot-anim -memory <3072|1024> -cores 2 -gpu swiftshader_indirect`.
- Kill them when idle (`adb emu kill`).
- Gradle runs with `org.gradle.workers.max=4` and `-Xmx3g`. Don't run `recordRoborazzi` while an emulator is up.
- Watch with `uptime`/`free -g`. If the load average exceeds 8 or available RAM drops below 3 GB, stop the emulator
  first.

### 6.6 Device field tests (after G-01 only)

Preconditions:
- Logcat capture running (`adb logcat -v threadtime > field-<id>.log`).
- On the POCO, the old app's wake word is disabled (§1.7).
- Rodrigo has been told before any voice or orchestrator session is opened on the Jetson.

| Set | Device | Scenarios |
|---|---|---|
| Voice parity | A300M (lite), POCO (main), Xiaomi | inv04 §10.3 **F1–F22**, same pass criteria |
| Main app | POCO | **M-F1** screen off 60 min, then "wake up" → voice starts headless, `voice` notification shown, no Activity launch; **M-F2** HyperOS: after 8 h idle with the reliability checklist done, the service is still alive (`dumpsys activity services com.assistant.archie`); repeat with the checklist undone to document the failure mode; **M-F3** assist gesture → session overlay → voice; **M-F4** QS tile on/off; **M-F5** share a 3 MB file → clear 1 MB-limit message; share text → injected; **M-F6** agent permission request while backgrounded → notification Approve works; **M-F7** streaming a long markdown answer with a 400-line code block: no jank warnings ("Skipped N frames" with N > 30) |
| Lite app | A300M | **L-F1** memory budget (§5.6) at idle, armed, in voice, after 1 h; **L-F2** in-place upgrade (§5.7) keeps settings, wake phrases, accessibility grant and Vosk model (no re-extract log); **L-F3** reboot → companion boot launch → wake armed within 60 s; `am kill` → watchdog relaunch ≤ 30 s (F20); **L-F4** 24 h soak: no LMK kill of the FGS, no stuck state; **L-F5** long-press home and recents trigger → voice |

---

## 7. Work breakdown

Sizes: **S** ≈ ≤ 800 LOC incl. tests · **M** ≈ 800–2,000 · **L** ≈ 2,000–4,000 · **XL** > 4,000.
"Boundary" is the only set of paths the WP may create or edit. `N/settings.gradle.kts` and
`N/gradle/libs.versions.toml` belong to A-01. Later WPs request catalog additions by appending to
`N/gradle/catalog-requests.md`; A-01's owner or the coordinator merges them between waves. This avoids two agents
editing the catalog at once.

**Waves (safe parallelism):**
```
W0: A-01
W1: A-02 · A-04 · B-01 · B-02            (A-02 ships :core:model + :core:protocol first, milestone M1)
W2: A-03 · A-05 · A-07 · B-03            (A-03 needs A-02 M1; A-05/A-07 need A-04; B-03 needs A-02 + B-01)
W3: A-06 · B-04 · B-05 · B-06 · B-07 · B-08 · C-01(UI on fakes)
W4: A-08 → G-01 (voice-parity gate)
W5: B-09 · C-01(wire to real host) → D-01 (field tests, release, cutover)
```

| ID | Title | Goal | Inputs | Boundary | Depends on | Definition of done | Size |
|---|---|---|---|---|---|---|---|
| **A-01** | Gradle skeleton, catalog, signing, AVD | An empty multi-module build that compiles, tests and assembles both apps. Conventions in place. | §1, inv03 build facts | `N/` root files, `N/build-logic/**`, `N/gradle/**`, empty `build.gradle.kts` + `AndroidManifest.xml` for every module, `N/app-*/version.properties`, `N/tools/native/**`, `.gitignore` entry for `N/keystore.properties`, `context/secrets/android/` (key copy) | — | `./gradlew assembleDebug test lint` green. `:app-lite` debug APK installs over a copy of the old app on the AVD (signing proven). Tier check, `verifyNoCompose` and `verifyPatchedVosk` tasks exist and fail on seeded violations. `A300M_API21` AVD created. Signing certificate on the A300M verified (§1.8). | M |
| **A-02** | Model, protocol codec, conversation reducer | Pure-Kotlin data layer of spec 12 + fixture runner | spec 12, `shared/protocol-fixtures`, inv01 §4–§7, inv03 §4 | `N/core/model/**`, `N/core/protocol/**`, `N/core/conversation/**` | A-01; spec 12 frozen for M2 | **M1:** all DTOs + codec + round-trip tests. **M2:** `ConversationReducer` + `HistoryMerger` + `ToolNameNormalizer`; `ProtocolFixtureConformanceTest` green on 100% of fixtures, including all `r4`/`r7` tags; ported `chat/parity` intent. | L |
| **A-03** | Network, settings, session | Sockets, REST, trust, discovery, settings and checkpoints, orchestrator channel | §1.2 rows, inv03 §2–§3, inv04 §4.6, §4.3 | `N/core/network/**`, `N/core/settings/**`, `N/core/session/**` | A-02 M1 | MockWebServer tests: reconnect 3,000 ms, ping 30 s, `willReconnect`, no dropped frames under 10k frames/s, outbox for `inject_text`. Adoption + 400 ms retry + recovery 0/500/1,000 (inv04 §12 errata). Reads the old DataStore `settings` file (fixture copied from a real device dump) with the exact keys. Checkpoint store bounded at 32. TOFU pin flow unit-tested with a self-signed cert. | L |
| **A-04** | Voice parity harness (tests first) | Fakes + every inv04 §10.1/§10.2 test, written against port interfaces; constant extractor | inv04 §3, §4, §10, §11.1 | `N/core/testing/**`; `src/test/**` of `:core:audio`, `:core:voice`, `:core:wakeword`, `:core:voice-host`; **interface-only** files `…/ports/*.kt` in those 4 modules; `N/tools/parity/**` | A-01 | Every RS-01…46 has a named test (coverage test green). Every inv04 §4 **LB**/*wire* constant has an assertion. `old_constants.json` generated, and the script verified against `e871d05`. Tests compile; implementation-dependent ones are red, marked with `@Ignore("A-05")` etc. so `check` stays green. A-05…A-08 un-ignore them. | L |
| **A-05** | Audio core | `:core:audio` per inv04 §8 | inv04 §3.3, §3.4, §4.7, §4.8, §4.10, §5.1 | `N/core/audio/src/main/**` (not `ports/`, which A-04 owns; changes go through the coordinator) | A-04 | All `:core:audio` parity tests un-ignored and green. Single-writer `PcmSink` (B3) test. API-branch policies tested at 21/22/23/26/31/34/36. | L |
| **A-06** | Voice session core | `:core:voice` per inv04 §8 | inv04 §2.2–§2.4, §3.2, §4.6, §4.9 | `N/core/voice/src/main/**`, `consumer-rules.pro` | A-04, A-05, A-02 M1 | All `:core:voice` parity tests green, including the `session.update` delivery chain, the 100k `trySend` stress test (RS-04), the B1/B2 fixes, and RS-06/07/08 WebRTC adapter tests (fake PC factory). | XL |
| **A-07** | Wake-word core | `:core:wakeword` per inv04 §8 | inv04 §3.1, §4.1–§4.4, §5.1 | `N/core/wakeword/**` except `src/test` (A-04) | A-04, A-05 | All `:core:wakeword` parity tests green: explicit CONFIRMING/CAPTURING states, Vosk single-thread confinement (R4), no backoff on NoMatch (RS-43), Whisper fail-closed. Shim builds for all ABIs. `VoskModelStore` uses the old dir/stamp. | XL |
| **A-08** | Voice host | `VoiceHostRuntime`, `VoiceHostService`, `VoiceHost` API, triggers, cues, notifications (lite spec + main spec via `HostConfig`) | §2.5–§2.7, inv04 §3.5, §4.5, §6 | `N/core/voice-host/**` except `src/test` (A-04) | A-03, A-05, A-06, A-07 | Service parity tests green (dedupe strict `<3000`, RESUME ignored-but-acked, screen re-arm skips CONFIRMING/CAPTURING (R3), mic-stalled notification). FGS-type policy tested with a fake `SdkInt` + permission state. `TriggerIngress` single entry. `/dev/input` evidence collected for Q8. | L |
| **G-01** | Voice-parity gate | Prove parity before any device time | §6.2 | read-only; fixes go back to the owning WP | A-08 (+ C-01 wired for the lite script) | §6.2 "passes when" list, recorded in `docs/projects/frontend-refactor/plan/` (by the coordinator) | S |
| **B-01** | Design system | `ArchieTheme` from `Tokens.kt` + M3 components | `design/tokens/dist/Tokens.kt`, IA §3–§7, audit | `N/core/design/**` | A-01; tokens dist exists | Theme dark and light (no dynamic color, D3). A `TokenAdapter.kt` isolates generated names. Each component has a preview + Roborazzi golden at 3 sizes × 2 themes. No hex literals outside `TokenAdapter.kt` (lint rule `HardcodedColor` via a custom check, or a grep test). | M |
| **B-02** | Markdown engine | `:core:markdown` (§3.2–§3.3) | §3, `MarkdownParserTest` cases, memory note on `31c2fbf` | `N/core/markdown/**` | A-01 (B-01 for final styles; it may start with M3 defaults behind a `MarkdownStyle` parameter) | §3.2 proof obligations green; highlighting, tables and links; goldens for 12 markdown samples. | L |
| **B-03** | Data layer + app shell + navigation | `:core:data` repositories; `:app-main` Application, graph, `MainActivity`, Nav3 shell, drawer, rail, tab strip, switcher host, splash, edge-to-edge | §2.1–§2.4, IA §2–§5, spec 12 | `N/core/data/**`, `N/app-main/src/main/kotlin/**/{shell,graph}/**`, `N/app-main/src/main/AndroidManifest.xml` (initial), `N/app-main/src/main/res/**` (theme, splash) | A-02, A-03, B-01 | Shell renders Compact, Medium and Expanded (goldens). Workspace tabs from `OpenSessionsRepository`. Connects to a MockWebServer backend in instrumented smoke. Adopting a scanned server connects (bug 3). | L |
| **B-04** | Conversation screen | `:feature:chat`: flattener (incl. "N steps" grouping), list, composer, inline cards, voice dock, queue, compact, slash commands, rewind/fork, upload button | §3.1, §3.6, IA §6, inv03 §1.2–§1.4, §5 | `N/feature/chat/**` | B-02, B-03 (A-08 API stubs via the `VoiceHost` interface, faked until W5) | §6.3 tests for R4/R7/composer/cards green. Streaming benchmark budget (§3.6) met on the POCO AVD (`benchmark` build). | XL |
| **B-05** | Tool cards + diff | `:feature:toolcards` | §3.4–§3.5, inv02 F-05/F-06 | `N/feature/toolcards/**` | B-01, B-02, A-02 M1 | `ToolCatalogParityTest` covers every F-05 row; goldens per renderer (collapsed and expanded, running, done, error). The running Bash card is never empty. | L |
| **B-06** | Sessions & history | Switcher sheet, chats list pane, history screen, new Archie/agent session, conflict dialog (3 actions as a proper dialog with stacked buttons), rename/duplicate/close/delete (snackbar Undo where the backend allows), UTC-correct times | IA §3, §5, inv03 §1.5, §1.7 | `N/feature/sessions/**` | B-03 | UI tests for every flow; relative-time test across time zones (fixes inv03 §1.5). | M |
| **B-07** | Memory & Visuals | §4.1 and §4.2, including docs/visuals as tabs on Expanded and the capability-gated Show on TV | §4, IA §9.2/§9.4, inv01 §3.3–§3.4, inv02 F-36/F-37, backend-changes proposal (cast endpoint) | `N/feature/memory/**`, `N/feature/visuals/**` | B-02, B-03 | Link-resolver test over real memory links; WebView security test (§6.3); WebViewPool leak test (LeakCanary in the debug instrumented run); `CastCapabilityTest` + hidden-action UI test; goldens. | L |
| **B-08** | Settings & permissions | IA §7 hierarchy, both scopes; server management + TOFU dialog; working-directory CRUD with SSH fields; MCP fix; Account (AuthGate flows: status, login (non-headless), paste credentials); About with real version; PermissionCenter; BackgroundReliabilityPage | IA §7, §2.6, §2.9, inv03 §1.6, §2 | `N/feature/settings/**` | B-03 (A-08 API via interface) | Save snackbar and error-verbatim tests; MCP semantics test (bug 1); goldens for every page. | L |
| **B-09** | System integration (main) | Wire the real `VoiceHost`; notifications + actions; share target + streamed uploads; VIS/assist; QS tile; shortcuts; trampoline; manifest permissions and FGS types | §2.5–§2.8 | `N/app-main/src/main/kotlin/**/system/**`, `N/app-main/src/main/AndroidManifest.xml`, `N/app-main/src/main/res/xml/**`, `MainAppGraph.kt` (graph handed over from B-03 after W3) | A-08, B-03, B-04, **G-01** | Instrumented tests on the POCO AVD: FGS start types, sticky restart degrades to SPECIAL_USE, tile toggles, share of text and a 3 MB file. Release smoke passes. | L |
| **C-01** | Lite app | §5 complete | §5, inv04 §6.4, §9 | `N/app-lite/**` | A-03; A-08 for wiring (UI starts in W3 against a `FakeVoiceHost` from `:core:testing`) | Goldens of all face states and settings; instrumented smoke on `A300M_API21`; `LegacyMigration` test with a real `settings` DataStore dump; `verifyNoCompose` + `verifyPatchedVosk` green; G-01 lite script passes. | L |
| **D-01** | Field tests, release, cutover readiness | Run §6.6; fix routing; produce release builds; cutover checklist (rename `android-next/` → `android/`, update the `android-dev` skill paths, archive old APKs) | §6.6, charter P4–P5 | `docs/projects/frontend-refactor/plan/field-*.md` (results), fixes via the owning WPs' boundaries | G-01, B-09, C-01 | All F1–F22, M-F1–7, L-F1–5 pass or are waived by Rodrigo in writing. The A300M has run the lite app for 48 h. Rodrigo signs off. | M |

---

## 8. Risk register

| # | Risk | Mitigation | Owner WP |
|---|---|---|---|
| X1 | AndroidX dropped API 21. A transitive bump silently breaks the lite build or the shared core. | Two-tier catalog + tier check + manifest-merger guard (§1.4–§1.5). | A-01 |
| X2 | Robolectric can't run API 21/22, so Lollipop paths are untested on the JVM. | `sdkInt` policies tested on the JVM + instrumented tests on the A300M AVD (§6.1, §6.5). | A-04/A-05 |
| X3 | Native libs are 4 KB-aligned and fail on 16 KB-page devices. | Alignment check; decision Q4 to upgrade the main app to WebRTC 1.3.10 / Vosk 0.3.75 after the gate, re-run F1/F8/F19/F22. | A-01, D-01 |
| X4 | The patched `libvosk.so` loses to the AAR copy in the merged APK. | `pickFirsts` + `verifyPatchedVosk` (§1.9). | A-01/C-01 |
| X5 | R8 strips JNA/WebRTC/Vosk members (release has never been tested). | Consumer rules + release smoke on both AVDs and devices (§1.10). | A-06/A-07/G-01 |
| X6 | Signing key mismatch or loss blocks updating the lite app in place. | Verify the device certificate; back the key up to `context/secrets`; explicit signing config (§1.8). | A-01 |
| X7 | versionCode ≤ 10 → downgrade error; a non-debuggable release blocks rollback. | versionCode 100; ship debug until L-F2 passes (§1.8, Q3). | C-01 |
| X8 | Changed component names lose the accessibility grant or the assist selection. | Keep FQCNs (§1.7); L-F2 checks; one-time reselect documented. | C-01 |
| X9 | Android 14+ FGS microphone rules; a sticky restart can't re-acquire the mic. | Type policy + degraded state + Resume action (§2.6). | A-08/B-09 |
| X10 | HyperOS kills the service. | Reliability checklist page; M-F2 measures it. | B-08/D-01 |
| X11 | No background activity launch on wake (UX expects the screen to show the app). | Headless voice + notification; optional full-screen intent (§2.6). | B-09 |
| X12 | Mic contention: two wake-word services on one phone (old app + main). | Field precondition; no debug id suffix (§1.6–§1.7). | D-01 |
| X13 | Spec 12 not final → reducer churn. | Fixture-driven reducer; A-02 M2 waits for the spec-12 freeze; the runner is format-agnostic. | A-02 |
| X14 | Tokens.kt symbol names unknown; no XML output for Views. | `TokenAdapter.kt` single seam; request XML output; C-01 fallback (§5.2). | B-01/C-01 |
| X15 | Markdown block splitter edge cases (lists, ref links, nested fences). | Streaming-equivalence test over 40 real messages; documented limitations (§3.2). | B-02 |
| X16 | Viz JS can call any backend API (no auth), including the OpenAI key. | Same as the web; flagged for a backend decision (§4.2). | — |
| X17 | Cleartext LAN traffic incl. the raw OpenAI key (inv04 R1). | TrustStore modes; decision Q5. | A-03 |
| X18 | The x86 API 21 AVD can't load unpatched Vosk → falls back to SR → masks bugs. | Patch the x86 lib for emulator builds (§1.9). | A-01/C-01 |
| X19 | Laptop overload (8 cores / 15 GB) from emulator + Gradle + browser. | One-environment rule, worker cap, emulator flags (§6.5). | all |
| X20 | AGP 9.4 / Kotlin 2.4 are new; plugins (Roborazzi) may lag. | A-01 verifies on day 1; documented fallback to the newest compatible AGP 9.x. | A-01 |
| X21 | nginx 1 MiB body limit makes most shared files fail. | Clear error now; backend fix (`client_max_body_size`) is inv01 §8.2's call. | B-09 |
| X22 | Navigation 3 adaptive scenes are young. | Fallback to navigation-compose 2.10 + `NavigableListDetailPaneScaffold`; only the shell changes. | B-03 |
| X23 | targetSdk 37 local-network permission would block LAN traffic. | Stay on 36 (Q6). | A-01 |
| X24 | Moving voice ownership into the service changes timing (inv04 §8 risk 3). | G-01 AVD script + F1/F3/F6/F7 logcat markers. | G-01/D-01 |
| X25 | Bumping OkHttp/WebRTC/coroutines alters tuned keepalive and ADM behaviour. | OkHttp 4.12 and WebRTC 1.1.1 pinned; coroutines bump is covered by the virtual-time parity suite. | A-03/A-06 |
| X26 | The A300M's API level is ambiguous (5.0.2 = 21 vs inventory "22"). | `getprop` check before creating the AVD (§6.5). | A-01 |
| X28 | The cast endpoint is never approved, or its shape changes. | Capability probe hides the action; only `ArchieApi.castVisualization` and the probe change. | B-07 |
| X27 | `ButtonAccessibilityService` keeps the package "alive" for the watchdog while the FGS is dead. | FGS restarted on every process start (§5.7). | C-01 |

---

## 9. Decisions for Rodrigo

| # | Question | Recommendation |
|---|---|---|
| ~~Q1~~ | Main app applicationId | **Decided:** `com.assistant.archie`, launcher name "Archie" (IA §9.5) |
| Q2 | Main app signing key: reuse the existing debug key, or a new `archie-release.jks`? (Both kept in `context/secrets/android/`.) | Reuse the existing key (one key to guard). The lite app has no choice (§1.8). |
| Q3 | Lite app on the A300M: debuggable build or minified release? | Debug until L-F2/L-F4 pass (rollback possible), then release for RAM. |
| Q4 | Native versions: stay on WebRTC 1.1.1 / Vosk 0.3.47 (4 KB-aligned) or upgrade the **main** app to 1.3.10 / 0.3.75 (16 KB-aligned) after G-01? | Pinned first. Upgrade only if `getconf PAGE_SIZE` on the POCO is 16384, or before the next Android major update. |
| Q5 | TLS: LAN cleartext (today), TOFU-pinned self-signed HTTPS, Tailscale certs, or a private CA on nginx? | Keep cleartext on the LAN for v1; add Tailscale HTTPS for off-LAN use. The pinning code ships either way. |
| Q6 | targetSdk 36 now, 37 later? | Yes. |
| Q7 | Main app: no boot receiver (wake word resumes on first open after reboot)? Optional "Stay connected in background" (needed for permission notifications)? | No boot receiver. "Stay connected" off by default. |
| Q8 | Lite triggers: drop the `/dev/input` recents monitor (B5) and the VIS (B6) unless field evidence says they're needed? | Drop both, pending the A-08 and L-F5 evidence. |
| ~~Q9~~ | IA open questions 2–4 | **Decided** (IA §9): tabs on Expanded; Show on TV via the pending cast endpoint (capability-gated); "N steps" grouping, expanded while live. Still open for the backend owner: approve `POST /api/visualizations/cast`. |
| Q10 | Ask the token owner for an Android XML resource output for the Views-based lite app? | Yes. |
| Q11 | During the transition on the POCO: uninstall the old app, or keep it with wake word disabled? | Keep it with wake word disabled until M-F1 passes, then uninstall. |
| Q12 | Screenshot goldens: plain git or Git LFS? | Plain git with PNG-optimized goldens (~150 images × ~60 KB ≈ 9 MB); revisit if they exceed 50 MB. |

## 10. Assumptions to reconcile when spec 12 and the tokens land

1. Spec 12 defines `MessageId`/`BlockId` that are stable across live ↔ REST (§3.1). If not, `HistoryMerger` derives
   them as described.
2. Spec 12 puts voice transcripts into the tail assistant message as blocks (inv03 §4.3 fix direction), and errors
   outside the message list.
3. Fixture files are self-describing JSON (`inputs[]`, `expected`, optional `checkpoints[]`, `tags[]` including
   `r4`/`r7`). If the format differs, only `ProtocolFixtureConformanceTest`'s loader changes.
4. `Tokens.kt` provides dark and light `ColorScheme`s, an extended-color holder (status, tool categories, code, diff,
   voice states), `Typography`, and `Shapes`. Any naming difference is absorbed by `TokenAdapter.kt`.
5. Fonts: if the tokens name a non-system font, `:core:design` bundles it under `res/font`. The lite app uses the
   system font (Roboto on the A300M).
