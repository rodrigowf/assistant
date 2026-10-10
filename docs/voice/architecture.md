---
name: architecture
category: archie/voice
tags: [voice, realtime, multi-provider, openai, qwen, gemini, webrtc, voice-relay, voice-provider, canonical-events, vad, silero, voice-error, reconnect, voice-persister, echo-ducking, web, android, run-script]
created: 2026-04-17
modified: 2026-10-09
summary: How realtime voice works across OpenAI, Qwen and Gemini; provider contract, the two transports, backend modules, clients, invariants, file map.
source: curated (consolidated from memory notes assistant/voice/voice-multimodel-plan.md, assistant/architecture/voice_subsystem.md, assistant/architecture/voice_command_device_control.md, assistant/voice/gemini_live_voice_adaptation.md, assistant/voice/qwen_omni_voice_adaptation.md, assistant/android/android_peripheral_project.md, assistant/providers/openai_audio_model_gpt_audio.md; verified against code 2026-10-06)
references:
  - lifecycle.md
  - prompt-budget.md
  - openai-realtime.md
  - qwen-omni.md
  - gemini-live.md
  - wake-word.md
  - ../architecture/system-overview.md
  - ../architecture/orchestrator.md
  - ../architecture/backend.md
  - ../clients/web.md
  - ../clients/android.md
  - ../operations/debugging.md
  - ../operations/refactor-methodology.md
  - ../specs/12-client-protocol.md
  - ../projects/frontend-refactor/inventory/04-android-voice-and-device.md
---

# Realtime voice architecture

Realtime voice is a **mode of the orchestrator**. The user talks to the
orchestrator agent (`OrchestratorSession`, see
[orchestrator](../architecture/orchestrator.md)), which has the same tools,
the same system prompt builder and the same JSONL in text and voice. Three
realtime providers plug in behind one contract:

| Provider id | Service | Transport | Audio path | Default model / voice | Doc |
|---|---|---|---|---|---|
| `openai` (default) | OpenAI Realtime | **WebRTC**, client ↔ OpenAI directly | never touches the backend | `gpt-realtime` / `cedar` | [openai-realtime.md](openai-realtime.md) |
| `qwen` | Alibaba DashScope Qwen-Omni | **WS relay** through the backend | client ↔ orchestrator WS ↔ backend ↔ DashScope | `qwen3.5-omni-plus-realtime` / `Aiden` | [qwen-omni.md](qwen-omni.md) |
| `google` | Gemini Live (Vertex or AI Studio) | **WS relay** through the backend | same as Qwen | `gemini-3.1-flash-live-preview` / `Puck` (backend `vertex`) | [gemini-live.md](gemini-live.md) |

Related docs: [lifecycle.md](lifecycle.md) (start/stop, ownership across
devices), [prompt-budget.md](prompt-budget.md) (the 16,384-token prompt cap)
and [wake-word.md](wake-word.md) (Android triggers). The client wire
contract is normative in
[spec 12 §7](../specs/12-client-protocol.md).

## The big picture

```
            ┌─────────── WebRTC (audio + data channel "oai-events") ──────────► OpenAI Realtime
Client ─────┤   every DC event mirrored ──► orchestrator WS `voice_event`
(web/       │   backend replies          ◄── `voice_command` (owner only)
 Android)   │
            └── orchestrator WS: voice_audio_in / voice_audio_out / voice_event ──► backend
                                                                               │
                         backend/api/routes/orchestrator.py  (owns no voice flags; reads the session)
                                                                               │
                         OrchestratorSession   VoiceLifecycle FSM, voice owner, VoicePersister,
                                               get_session_update(), tool execution
                                                                               │  (WS providers only)
                         VoiceRelay  ── upstream WS ──► DashScope / Gemini Live
                                     Silero VAD, gating, reconnect, keepalive, per-session log
                                                                               │
                         BaseVoiceProvider subclass (wire format, hooks, error classifier)
```

- **WebRTC path (OpenAI)**: the backend never sees audio. It mints an
  ephemeral token (`client_secrets`), builds the `session.update`, receives
  every data-channel event mirrored as `voice_event`, runs tools, and sends
  provider commands back as `voice_command`. It re-broadcasts the mirrored
  transcript events to the other devices (not the owner), so passive viewers
  see the live transcript as with WS providers (spec 12 VT-2). The
  `OPENAI_API_KEY` stays on the server.
- **WS relay path (Qwen, Gemini, any future provider)**: the backend owns the
  upstream socket. The client sends PCM16 mono mic chunks as
  `voice_audio_in` and plays `voice_audio_out`. `DASHSCOPE_API_KEY`,
  `GEMINI_API_KEY` and the GCP credentials never reach a client. It adds
  about one extra network hop (~50 ms).
- Whichever path, the provider's events go through
  `OrchestratorSession.process_voice_event` (persistence + commands) and are
  broadcast to the conversation's subscribers as `voice_event`.

## Provider contract (`backend/orchestrator/providers/`)

`voice_base.py` `BaseVoiceProvider` is the contract. **Adding a provider =
one subclass + one entry in `voice_registry.py`** (`_VOICE_PROVIDER_RESOLVERS`,
`VOICE_MODELS`). Provider quirks live in hooks on the subclass, not in the
relay.

| Group | Members |
|---|---|
| identity | `provider_name`, `connection_type` (`"webrtc"` / `"websocket"`), `model`, `voice` |
| session | `format_session_config(system, tools, voice, vad)` (also `get_session_update_payload`), `get_connection_info()` (shape: `connection_type, endpoint, ephemeral_token, expires_at, audio_in_format, audio_out_format, model, voice`) |
| translation | `translate_event(raw) → OrchestratorEvent`, driven by an `_EVENT_TRANSLATORS` table (a dict keyed by `type` for OpenAI and Qwen; an ordered tuple of `(predicate, method)` for Gemini, whose frames have no `type`) |
| commands | `format_tool_result(call_id, output)` (submit + ask for a reply), `format_text_input(text)` (silent injection, no reply) |
| relay hooks (WS) | `format_audio_in`, `extract_audio_out`, `audio_in_sample_rate`, `handshake_direction` (`server_first` / `client_first`), `open_upstream` |
| resilience | `is_recoverable_error` (**the** reconnect gate; Gemini's version also mutates the handle once), `classify_close_reason` (read-only, advisory; must agree on `recoverable`, pinned by a parity test), `accepts_upstream_event`, `should_close_after_event`, `build_keepalive_chunk`, `graceful_shutdown_frames` |
| response gate | `should_gate_event`, `on_inbound_event`, `gate_cleared`, `mark_response_create_sent` |
| manual VAD | `manual_vad_start_frames`, `manual_vad_stop_frames`, `manual_vad_safety_commit_frames`, `supports_manual_vad` (true iff stop frames are non-empty) |

`ToolCallAccumulator` (a mixin in the same file) tracks `call_id → name/args`
for all three providers: `register_call`, `accumulate_args`,
`pop_/peek_name`, `pop_/peek_args`, `clear_pending_calls`. Tool outputs sent
to any realtime API are cut to `VOICE_TOOL_OUTPUT_MAX_CHARS = 8000`
(`truncate_voice_tool_output`). For a `read_file` result, the cut suffix says
which `start_line` to read next. The APIs reject `function_call_output`
payloads above roughly 15–16 KB.

Schema and text sanitisers are in `schema_utils.py`:
`sanitize_schema_for_gemini` (Draft 7 → OpenAPI 3.0),
`sanitize_tool_for_qwen` (removes `[X, "null"]` unions) and
`sanitize_text_for_qwen` (wraps URL-shaped tokens in backticks). The provider
modules re-export them under their old underscore names.

### Registry and voice config

`voice_registry.py`: `VOICE_PROVIDERS` is a lazy registry, so a deployment
without the `openai` SDK still imports. `VOICE_MODELS` holds per-model entries
`{id, label, voice, voices, transcription_languages,
default_transcription_language, default}`. `resolve_voice_target()` falls
back to defaults at every level: unknown provider → `openai`; an unknown model
id is *accepted* (for live-discovered models) — OpenAI and Gemini use the
provider's default entry as a template, Qwen goes through `qwen_model_entry()`
(dated snapshot → its alias's entry, otherwise the Flash or Plus catalogue by
family name); unknown voice or language → the model's default.
`instantiate_provider(provider, model, voice, lang, endpoint)`; `endpoint`
picks Gemini's backend.

Voice catalogue: OpenAI 10 voices; Qwen Plus 55, Qwen Flash 49; Gemini 8 (all
static). `GET /api/orchestrator/voice/models` merges live model ids with the
static map (`discovery.py`, 10-minute cache, static fallback on error or
missing key): OpenAI's `*realtime*` ids from `/v1/models`, Qwen's
`qwen*-omni-*realtime` ids from DashScope's
`/compatible-mode/v1/models` (realtime TTS / ASR / livetranslate ids are
filtered out). `GET /api/config/voice/google/models?endpoint=…` lists
Gemini's live models.

Global defaults live in `assistant_config.json`: `default_voice_provider`,
`default_voice_model`, `default_voice_name`,
`default_voice_transcription_language`, `default_voice_endpoint` (Gemini:
`vertex` or `aistudio`), `voice_recording_enabled`, and the VAD knobs below.
The cascade in `PUT /api/config` (`backend/api/routes/config.py`): changing
the provider resets model, voice and language to that provider's defaults;
changing the model resets voice and language; changing only the voice or the
language keeps the rest. A `voice_start` that leaves out `voice_*` fields
gets these defaults. On a re-arm, a missing field means "keep the previous
value". `orchestrator_meta` in the JSONL records provider, model, voice and
language; when a field is missing, replay assumes openai / gpt-realtime /
cedar / "".

## Canonical events

`backend/orchestrator/types.py`: providers translate to `TextDelta`,
`TextComplete`, `ToolUseStart`, `TurnComplete`, `VoiceInterrupted` and
`ErrorEvent` (the agent loop also uses `ToolResultEvent`,
`ToolExecutingEvent`, `ToolProgressEvent`, `NestedSessionEvent`). Audio does
not use the typed channel: it goes out as `voice_audio_out` broadcasts (relay)
or straight over WebRTC.

## The WS relay (`backend/orchestrator/voice_relay.py`)

`VoiceRelay` owns one upstream WS for a voice connection. `OrchestratorSession.start_voice_relay` creates it in the background from `_attach_voice_payload`.

- **Start**: broadcast `voice_status: preparing` → `open_upstream()` →
  handshake (`server_first`: read `session.created`, then send the config;
  `client_first`: send `setup`, wait for `setupComplete`) → `voice_status: ready`.
  Outbound events and audio wait for the handshake (`_handshake_complete`, 15 s
  bound), because Gemini closes the WS with 1008 if audio arrives before
  `setupComplete`. `_setup_sent_for_ws` (a `WeakSet`) refuses to send setup
  twice on the same socket.
- **Drain loop**: for each inbound frame, call `on_inbound_event` (gating
  state), then `should_close_after_event`, then send audio
  (`extract_audio_out` → `voice_audio_out`) **and** forward the frame with
  the audio stripped (`_strip_audio_parts`, Gemini's bundled transcription),
  then pass it to `process_voice_event`. Tool calls
  (`response.function_call_arguments.done` or Gemini `toolCall`) are started
  as background tasks so the drain never blocks.
- **Gating**: a frame that `should_gate_event` holds back goes into
  `_deferred_events`. It is drained after inbound events **and** by a
  polling watchdog (`_arm_deferred_drain`, every 2 s, up to 45 s). See
  [lifecycle.md](lifecycle.md#rules) rule 6.
- **Reconnect** (`backend/orchestrator/voice_reconnect.py`): `ReconnectReason`
  is `PROVIDER_GOAWAY` (no cap, keeps the handle), `RECOVERABLE_ERROR` (capped
  by `max_reconnects`, default 2) or `STALE_HANDLE` (once, drops the handle,
  silent). One `_reconnect_lock` covers the whole close → rebuild → handshake →
  flush sequence; concurrent reconnects merge (`_reconnect_attempts_completed`
  counter). While reconnecting, outbound frames queue in `_held_outbound`
  (`HELD_OUTBOUND_CAP = 64`, oldest dropped first) and are replayed after the
  new handshake, skipping any frame that would trigger another close. The new
  `session.update` is rebuilt fresh (`rebuild_session_update`). After a
  reconnect Silero and the speech timer are reset. If a classifier result says
  `recoverable=False`, the relay stops trying at once.
- **Keepalive**: if the provider has a `build_keepalive_chunk()` (Qwen), a
  silent chunk is sent after every `keepalive_s = 30` s of mic silence.
- **Shutdown**: `send_shutdown_frames(graceful_shutdown_frames())` within
  `graceful_shutdown_s`, then `stop()`.
- **Errors**: on a fatal close the relay emits `voice_error{error: VoiceError}`
  **before** the legacy `error{code: "voice_relay_failed"}`, so newer clients
  can show the typed message instead of the generic banner. It then calls
  `on_fatal`, which runs `end_voice("error")`.

### Typed errors (`voice_errors.py`)

`VoiceError` is a frozen dataclass: `category`, `message`, `recoverable`,
`recovery_hint`, `provider_doc_url`, `raw_close_code`, `raw_close_reason`,
`provider`. `VoiceErrorCategory` values are wire strings that clients switch
on, so **never rename one** (appending is fine): `quota_exceeded`,
`rate_limit`, `auth`, `model_unavailable`, `context_full`, `network`,
`provider_internal`, `unknown`. Each provider's `classify_close_reason` maps
its close shapes to these. The per-provider mappings are in the provider docs.

### Voice activity detection

`voice_vad.py` `VoiceVAD`: Silero v5 ONNX via onnxruntime. The file
`backend/vendor/silero_vad_v5.onnx` is about 1.3 MB; it is the quantized
"half" variant, because the full model gave flat probabilities on this
onnxruntime. It runs on 512-sample (32 ms) windows at 16 kHz and resamples
other input rates.

| Setting | Default | Range | Where |
|---|---|---|---|
| on-threshold (off = on − 0.15) | **0.28** | [0.15, 0.50] | `assistant_config.json` `voice_vad_threshold` |
| min silence before `speech_stopped` | 2500 ms | [800, 5000] | `voice_vad_min_silence_ms` |
| min speech before `speech_started` | 200 ms | — | constructor |
| server-side mic gain | 1.0 | [0.5, 2.0] | `voice_mic_gain` (reserved, not applied yet) |

Manual VAD runs only if the provider supports it, its env switch is not
`0` (`QWEN_MANUAL_VAD`, `GEMINI_MANUAL_VAD`; both default on), and it
declares `audio_in_sample_rate`. On every transition the relay sends the
provider's frames, emits synthetic `input_audio_buffer.speech_started` /
`speech_stopped` to clients, and broadcasts
`voice_vad_state{state, duration_ms, silero_prob}`, repeated every
`vad_state_heartbeat_s = 1` s while listening. Clients show "Listening Ns"
after 3 s, so a stuck state is visible. The safety commit after
`manual_vad_safety_commit_s = 50` s exists only for Qwen. OpenAI uses its own
server VAD. **Don't add a hard cap on how long `speech_started` may last**:
the 2026-06-09 decision was to *show* the state (the heartbeat, the "Listening
Ns" indicator), not to force transitions.

### Timeouts (`voice_timeouts.py`)

`VoiceTimeouts` is a frozen dataclass passed into `OrchestratorSession` and
from there to `VoiceRelay`. `from_config` reads an optional `voice_timeouts`
block, but nothing loads one yet.

| Field | Default |
|---|---|
| `await_orchestrator_stop_s` | 5.0 |
| `graceful_shutdown_s` | 0.5 |
| `reconnect_handshake_s` | 15.0 |
| `manual_vad_safety_commit_s` | 50.0 |
| `keepalive_s` | 30.0 |
| `vad_state_heartbeat_s` | 1.0 |

## Backend session side

- **`OrchestratorSession`** (`backend/orchestrator/session.py`) owns all voice
  state: `VoiceLifecycle`, `_voice_owner_ws` (`register_voice_owner`,
  `clear_voice_owner_if`), `voice_config_drifts_from`, the provider, the
  relay, the deferred `response.create` slot, and `get_session_update()`.
  That method rebuilds the system prompt **from the JSONL on every voice
  start** ([prompt-budget.md](prompt-budget.md)). The route layer keeps only
  the per-`local_id` `_VOICE_START_LOCKS`.
- **`VoicePersister`** (`voice_persister.py`) turns provider events into JSONL
  lines. It is created lazily on the first `process_voice_event`.
  `handle_event` dispatches per provider: the OpenAI/Qwen `type` shape vs
  Gemini's camelCase. `persist_tool_use_and_result`, `clear_pending` (on
  reconnect). It writes `user` lines with `source: "voice_transcription"` and
  text prefixed `[voice]`, `assistant` lines with `source: "voice_response"`,
  `tool_use`/`tool_result` with `source: "voice"`, and `voice_interrupted`
  entries. Fragments of a cancelled turn are dropped. Tool *execution* stays
  on the session.
- **Silent injection**: `inject_text` during voice adds the text with
  `format_text_input` and asks for no reply. During an injection window
  (`is_injecting`, for example the `listen_recording` tool replaying past
  audio into the WS), the injected audio's VAD and transcription events are
  hidden from clients, and the mic is not recorded.
- **Recording** (`audio_recorder.py`, off unless
  `voice_recording_enabled`): `AudioRecorder` writes an interleaved
  `audio.pcm` plus a chunk/segment index under `context/recordings/<session>/`.
  WS providers are recorded on the server. For WebRTC the owner client sends
  `voice_recording_chunk{channel: user|assistant, audio}` every 5 s (AudioWorklet
  `pcm-capture`). The `listen_recording` tool plays recordings back.
- **Agent tools**: `end_voice_session` (`tools/voice_control.py`) lets the
  model hang up (reason `agent_end`). `run_script` lets voice act on devices
  (below).
- **Summary cache** (`summary_cache.py`) and **token budget**
  (`token_budget.py`): see [prompt-budget.md](prompt-budget.md).

### One-shot voice messages (not realtime)

`send_audio{audio, format, text?}` on the orchestrator WS is a normal
orchestrator **turn** with audio input. It is used by the Android talk word,
push-to-talk and the web voice-message button. `_send_audio_inner` sends it
to the audio chat model (`resolve_audio_model()`: Settings
`default_audio_model`, else `gpt-audio`) for that turn only, and records it in
the JSONL as `[audio:<fmt>] <prompt>`. It opens no realtime session, loads no
voice prompt and needs no WebRTC, so it is cheap and instant. See
[openai-realtime.md](openai-realtime.md#related-openai-paths-that-are-not-realtime).

### Voice to device control

Both voice paths reach the same tool registry, so both can act on the
physical world through the orchestrator's `run_script` tool (allowlisted
single-shot scripts, listed in `context/memory/ORCHESTRATOR_SCRIPTS.md`):

- **Talk word, no session**: "hello my friend, turn off the office lamps" →
  one audio turn → `run_script` (the lamps script) → spoken/text answer.
- **Inside a realtime conversation** (wake word "wake up"): the model calls
  the same tool mid-conversation; the result goes back over the data channel
  or relay.

This first worked end to end on 2026-07-22 with the LAN smart lamps. It is
the "just speak and things happen" surface the peripheral setup was built
for.

## Client side

### Web (`apps/web/src/voice/`, UI in `apps/web/src/features/voice/`)

| File | Role |
|---|---|
| `core/VoiceController.ts` | the signalling state machine per Archie conversation: FIFO command queue (no cap, never drops), initiator-only `session.update`, 30 s connection-info wait, 5 s ending timeout, `response.cancel` only while a response is in flight, passive-viewer handling, P-2 link-loss re-arm (30 s budget, cues) |
| `core/support.ts`, `core/types.ts` | provider → transport (`openai` → `webrtc`; `qwen`/`google` → `websocket`), feature detection |
| `transports/webrtc.ts` | peer connection + `oai-events` data channel, SDP to `connection_info.endpoint`, mirrors DC events |
| `transports/wsRelay.ts` | AudioWorklet `pcm-capture` → PCM16 at `audio_in_format.sample_rate`, 100 ms chunks → `voice_audio_in`; `PcmPlayer` for `voice_audio_out`; flush on barge-in |
| `audio/` | shared, gesture-unlocked `AudioContext`, `pcmPlayer.ts` (gapless, hard-stop flush), `capture/worklet.ts`, `meters.ts`, `cues.ts` |
| `recording/sessionRecorder.ts` | WebRTC recording chunks |
| `registry.ts` | one controller per conversation runtime |
| `features/voice/` | `VoiceDock.tsx` (+ `useVoiceDock`), `VoiceSlot.tsx`, `VoiceOverlay.tsx` + `overlay.ts` + `useActivityIdle.ts` (floating controls), `LevelOrb.tsx`, `useVoiceUi.ts` (+ `useLiveVoiceId`); the shell side is `app/shell/VoiceOverlayHost.tsx` |

`?debug=voice` in the page URL turns on `[voice]` console logs. The Safari 12
compat build has no `MediaRecorder`, so it has no voice-message button.

### Floating voice controls (web and Android)

While this device has a voice call, the same controls as the Archie
conversation's dock float above every other view (agent sessions, memory
documents, visuals, settings, the compact screens), so the call can be muted or
ended while reading or watching something else. On the Archie conversation
itself nothing floats: the dock in the composer slot is the control. "Voice
active on another device" never floats (not this device's microphone).

- **Same controls, same logic.** Web: `VoiceDockView` driven by
  `useVoiceDock(localId)`, shared with `VoiceDock`; the overlay itself is a lazy
  chunk, preloaded when a call starts. Android: `VoiceControls` (`:feature:chat`
  `ComposerArea.kt`), shared with the composer slot. One process-wide
  `VoiceDockModel` (mapping + reconnect timeline, `MainAppGraph.voiceDock`)
  feeds both `ConversationViewModel` and `VoiceOverlayModel`, so ending from one
  surface clears the outcome on the other and it survives rotation. The state
  text opens the Archie conversation.
- **Where.** Default: where the dock sits on the Archie page (bottom centre of
  the workspace, the dock's gutters), lifted above a visible composer (web: the
  panel's `[data-conversation-dock]`; Android: `LocalComposerBounds`, reported by
  `ConversationScreen`). Layering: above the rail, list pane, workspace and
  screens; below the modal layers (web: z-index 19 under `#overlay-root`'s
  drawer, sheets, dialogs, menus, snackbars; Android: drawn inside the shell
  under the modal drawer and the medium list overlay, while sheets, dialogs and
  menus are windows above it).
- **Drag.** Mouse or touch drag snaps to one of six anchors (top/bottom ×
  left/centre/right, thirds across and halves down by the dropped centre),
  persisted per device: web pref `voiceOverlayAnchor`, Android
  `DeviceSettings.voiceOverlayAnchor` (same keys, e.g. `bottom-center`). The
  anchor is committed and saved on release; only the offset eases home, so a
  new drag interrupting the snap never loses it.
- **Idle fade.** After 4 s without activity it shrinks to a faded pill (orb +
  mic / muted icon, opacity 0.55; never invisible, so a live microphone always
  shows). Activity: web — mousemove (real moves only), mousedown, touch, wheel
  and keys on the window and on every same-origin iframe document (visuals),
  picked up when added (MutationObserver), on view/screen change, on going
  idle, on window blur and on each frame's `load`; no `scroll` (streaming conversations scroll themselves). Android — every
  pointer event at `PointerEventPass.Initial` on the shell root (never
  consumed, so WebView touches count) and hardware keys. It never shrinks while
  hovered or with keyboard focus inside (web), mid-drag, or while the call needs
  the user (connecting, ending, errors, reconnecting / outcomes, banners); a mic
  mute change wakes it. A tap on the pill only expands it; on the web a press
  elsewhere that wakes it cannot click the controls for 600 ms (on Android
  Compose hit-tests on the down, so no guard is needed). Android uses the
  system's accessibility "time to take action" as the delay. Motion (fade,
  snap) is off under reduced motion / low-end.
- **A11y.** Web: region "Voice call", the pill is a button naming the state, a
  polite live region speaks state changes while it is a pill. A modal compact
  screen leaves it exposed and focusable (`useOverlayLayer({keepExposed})`);
  dialogs, sheets and menus hide it like the rest of the page. Android: pane title "Voice call", the pill's description
  names the state (polite live region).

### Android (`apps/android/core/`)

| Module | Role |
|---|---|
| `core/voice` | `session/DefaultVoiceSessionController.kt` (session machine, ownership, link-loss), `delivery/CommandDelivery.kt` (pre-transport queue + `session.update` cache/self-heal), `transport/WebRtcTransport.kt` (OpenAI; never disposes on a WebRTC callback thread), `transport/WsPcmTransport.kt` (Qwen/Gemini), `parse/ProviderParsers.kt` (OpenAI/Qwen/Gemini → events), `duck/OpenAiDucker.kt`, `VoiceTuning.kt` |
| `core/audio` | `duck/DrainEchoDucker.kt` (WS providers' drain-then-restore), `playback/PcmSink.kt`, mic source policy, `routing/` (`DefaultRouteDecider`, `PolicyRouteApplier`), audio focus, `AudioTuning.kt` |
| `core/voice-host` | `VoiceHostService` (microphone foreground service), `runtime/VoiceHostRuntime.kt` (owns channel wiring, voice session, wake service, cues, triggers), `HostTuning.kt` |
| `core/wakeword` | see [wake-word.md](wake-word.md) |

The voice session belongs to the **service-scoped host**, not to an
Activity. The UIs (`:app-main`, `:app-lite`) bind to `VoiceHost`
(`state`, `events`, `startVoice`, `stopVoice`, …). Values that must not
change:

- WS providers: 480-frame mic chunks (20 ms at 24 kHz); the 200 ms HAL settle,
  then the mic is opened **before** the speaker; a 1.5 s speaker buffer
  (72,000 B at 24 kHz).
- Echo ducking: **duck to 5% (not mute) while the agent speaks; restore only
  after the speaker has drained + a 1000 ms tail, with no timeout**. Never
  shortcut this with raw-mic VAD (see [wake-word.md](wake-word.md#rules)).
- OpenAI: software AEC/NS/AGC, restore 2 s after `output_audio_buffer.stopped`.
- Mic source: `VOICE_RECOGNITION` below API 24, `VOICE_COMMUNICATION` from 24.
- Route re-applied at +1 / +4 / +9 s after start. Cues on `STREAM_MUSIC`.

Every constant and every regression scenario is in
[inventory 04](../projects/frontend-refactor/inventory/04-android-voice-and-device.md).
The `*Tuning` objects are pinned by parity tests against the old app's values.

## Observability

| Where | What |
|---|---|
| `logs/voice/<ts>_<session_id>.log` (backend) | one log per relay session: sent/recv counters, sampled audio counts, a ring of the last 24 non-audio frames (`_FRAME_HISTORY_SIZE`), lifecycle markers, and a closing `voice_session_closed session_id=… reason=… ws_code=… audio_in/out …` summary (also in the journal) |
| journal (`journalctl -u agentic-backend.service`) | `voice_state … → …`, `voice_start_timing step=… dt_ms=…`, `voice_start coalesce`, `history summary cache HIT/STALE-REUSE/MISS`, `voice_command_deferred` / `voice_command_drain`, `voice_event_in` (deltas omitted). **WebSocket frames are not logged**; for those, use client logs or a direct WS probe |
| `VOICE_DEBUG_GEMINI_BODIES=1` | full Gemini frames, audio redacted |
| `VOICE_DEBUG_QWEN_BODIES=1` | Qwen session keys + a `.session_update.json` dump |
| `VOICE_DEBUG_DUMP_MIC=1` | `logs/voice/<ts>_<sid>.mic.wav`, every mic byte the relay received |
| `VOICE_DEBUG_VAD=1` | `VAD_PROBE` line about once a second (min/mean/max Silero probability, thresholds) |
| Android logcat | `[MIC_PROBE]` (mic RMS before gain, about once a second), `[MIC_STATE]` (duck/restore), `[AUDIO_RMS]` (WebRTC), `start: draining`, `session.update missing at DC_OPEN` |
| web | `?debug=voice` |

To place a "Silero heard nothing" fault, compare three points: Android
`[MIC_PROBE]` high but the WAV silent → transport bug; the WAV has signal but
`VAD_PROBE` shows low probabilities → threshold or resampling; Android RMS
low too → mic hardware or routing. The debug switches are temporary. Turn
them off on the Jetson when you are done. Workflow and commands:
[debugging](../operations/debugging.md).

## Key invariants

- Voice and text share **one orchestrator session, one `local_id`, one
  JSONL**. Ending voice keeps the session (see [lifecycle.md](lifecycle.md)).
- The voice system prompt is rebuilt fresh from the JSONL on every voice
  start. The summary cache keeps that off the critical path.
- **A voice cannot change mid-session.** WebRTC and `session.update` both lock
  it once audio has been emitted. A config change rebuilds the orchestrator.
- `restart_voice` always creates a fresh provider. No resumption handle
  survives an intentional teardown.
- Provider control frames go only to the voice owner; passive devices only
  render (spec 12 §7.5).
- Provider-bound frames are queued until the transport is ready, flushed in
  order with `session.update` first, and never dropped.
- Credentials: the raw `OPENAI_API_KEY` reaches LAN clients only through
  `GET /api/config/openai-key`, which Android uses for Whisper. DashScope and
  Gemini credentials never leave the backend.
- New provider checklist: a schema sanitiser if needed; a
  `classify_close_reason` that agrees with `is_recoverable_error`; clear any
  server-side handle on teardown; a `response.create` gate whose parked frames
  have a drain trigger that does not depend on an inbound event; manual-VAD
  frames (decide whether a commit-only safety chunk exists on that wire).

## File map

```
backend/orchestrator/
  providers/voice_base.py        BaseVoiceProvider, ToolCallAccumulator, truncate_voice_tool_output
  providers/voice_registry.py    VOICE_PROVIDERS, VOICE_MODELS, resolve_voice_target, instantiate_provider
  providers/openai_voice.py      OpenAIVoiceProvider (WebRTC)
  providers/qwen_voice.py        QwenVoiceProvider (DashScope WS)
  providers/gemini_voice_base.py GeminiVoiceProviderBase (protocol, resumption, goAway)
  providers/gemini_voice.py      VertexAIBackend, GeminiAIStudioBackend, select_backend
  providers/schema_utils.py      Gemini/Qwen schema + text sanitisers
  voice_relay.py                 VoiceRelay (WS providers)
  voice_vad.py                   VoiceVAD (Silero), is_enabled_for
  voice_reconnect.py             ReconnectReason, POLICIES, HELD_OUTBOUND_CAP
  voice_errors.py                VoiceError, VoiceErrorCategory
  voice_timeouts.py              VoiceTimeouts
  voice_persister.py             VoicePersister (events → JSONL)
  audio_recorder.py              AudioRecorder (context/recordings)
  summary_cache.py, token_budget.py, prompt.py
  session.py                     OrchestratorSession: VoiceLifecycle, end_voice, restart_voice,
                                 start/stop_voice_relay, get_session_update, process_voice_event, send_audio
  tools/voice_control.py         end_voice_session      tools/audio_playback.py  listen_recording
  tools/run_script.py            run_script
backend/api/routes/
  orchestrator.py                orchestrator_ws, _handle_start, _attach_voice_payload, _handle_voice_event,
                                 _send_voice_command, _dispatch_voice_commands, deferred-drain watchdog,
                                 _handle_voice_tool_call, _handle_gemini_voice_tool_call, _handle_send_audio
  voice.py                       POST /api/orchestrator/voice/session, GET /api/orchestrator/voice/models,
                                 GET /api/orchestrator/models(/audio)
  config.py                      default_voice_* + cascade, VAD knobs, GET /api/config/openai-key,
                                 GET /api/config/voice/google/models
backend/vendor/silero_vad_v5.onnx
backend/tests/                   test_voice_lifecycle.py, test_voice_start_lock.py, test_summary_cache.py,
                                 test_voice_timeouts.py, test_schema_utils.py, parity/ …
apps/web/src/voice/, apps/web/src/features/voice/
apps/android/core/{voice,audio,voice-host,wakeword}/
```

## History

- **2026-04/05**: OpenAI WebRTC first. Then the provider generalisation:
  Qwen-Omni (2026-05-01/16) and Gemini Live (2026-05-15) behind
  `BaseVoiceProvider`, the relay hooks and the registry.
- **2026-05-20 / 06-03**: client-side Silero VAD for Qwen, then generalised to
  Gemini.
- **2026-06-04/06**: the lifecycle refactor (see [lifecycle.md](lifecycle.md)).
- **2026-06-09, A→H refactor** (branch `voice-wakeword-refactor`; base `9c25e07`):
  A typed `VoiceError` (`5286baa`), B VAD observability + tunable settings
  (`d675187`), C reconnect policy + single lock + held queue (`94d8d56`),
  D voice state moved onto the session (`76ae6a6`), E `ToolCallAccumulator` +
  `_EVENT_TRANSLATORS` + `schema_utils` (`13ab064`), F `VoiceTimeouts`
  (`2f1c777`), G `VoicePersister` (`26425c6`), H the old Android
  `EchoDuckController`/`MicCapture`/`PcmPlayback` split + an unbounded command
  channel (`419813c`, `d8fbedf`, `91b4e92`).
- **2026-07-21/22**: the audio model moved to `gpt-audio`; owner-scoped
  commands; `session.update` self-heal; the prompt-budget rework; voice to
  device control.
- **2026-07-31 / 08-01**: deferred-`response.create` watchdogs.
- **2026-10-09**: floating voice controls over every non-Archie view (web +
  Android), with idle fade, drag-to-snap and per-device position.
- **2026-10-05**: the new web app (`apps/web`) and Android modules
  (`apps/android/core/*`) replaced the old clients (now in `legacy/`), written
  against spec 12 and inventory 04.
