---
name: lifecycle
category: archie/voice
tags: [voice, lifecycle, voice-lifecycle, end_voice, restart_voice, wake-after-stop, ownership, voice_initiator, voice_owner_active, multi-device, session-update, self-heal, deferred-response-create, resumption-handle]
created: 2026-06-04
modified: 2026-10-08
summary: The voice lifecycle state machine, end and restart paths, owner-scoped voice across devices, the session.update self-heal, and the rules they enforce.
source: curated (consolidated from memory notes assistant/voice/voice_lifecycle_and_wake_after_stop.md, assistant/architecture/voice_subsystem.md, assistant/voice/feedback_voice_debug_diagnose_before_patching.md, auto-memory project_voice_initiator_flag_2026_06_06, project_voice_ghost_state_fix_2026_06_30, project_multidevice_voice_ownership_2026_07_21, project_voice_session_update_restart_race_2026_07_21, project_qwen_voice_gate_no_staleness_clear, project_voice_lifecycle_refactor_branch, feedback_clear_provider_state_on_rebuild, feedback_voice_debug_diagnose_before_patching; verified against code 2026-10-06)
references:
  - architecture.md
  - prompt-budget.md
  - openai-realtime.md
  - qwen-omni.md
  - gemini-live.md
  - wake-word.md
  - ../architecture/orchestrator.md
  - ../operations/debugging.md
  - ../operations/troubleshooting.md
  - ../specs/12-client-protocol.md
---

# Voice lifecycle

Voice is one **mode** of the orchestrator conversation, not a separate
session. One `OrchestratorSession` (keyed by the tab's `local_id`, one JSONL)
lives in the pool. A voice connection is attached to it, torn down and
attached again many times, and the JSONL and agent context survive every
cycle. This doc covers that cycle on the backend and the client rules that
keep several devices consistent. The client-side wire contract is normative
in [spec 12 §7](../specs/12-client-protocol.md).

Code: `backend/orchestrator/session.py` (`VoiceLifecycle`, `end_voice`,
`restart_voice`, `start_voice_relay`, `stop_voice_relay`, ownership helpers)
and `backend/api/routes/orchestrator.py` (`orchestrator_ws`, `_handle_start`,
`_attach_voice_payload`, `_send_voice_command`, the deferred
`response.create` slot).

## `VoiceLifecycle`

```
IDLE ──► STARTING ──► ACTIVE ──► ENDING ──► ENDED ──(restart_voice)──► IDLE
  └──────────┴──────────────────────►┘
```

| From | Allowed targets (`_VALID_VOICE_TRANSITIONS`) |
|---|---|
| IDLE | STARTING, ENDING |
| STARTING | ACTIVE, ENDING |
| ACTIVE | ENDING |
| ENDING | ENDED |
| ENDED | IDLE (only through `restart_voice`) |

Every transition goes through `_set_voice_state_unlocked` while holding
`_voice_lock`. An invalid transition raises, because it is a bug. A stray
double call (a user stop racing the agent's `end_voice_session` tool)
becomes a no-op instead of a corrupt double teardown.

- `start_voice_relay` moves IDLE → STARTING → ACTIVE, **for WS providers only**
  (`needs_voice_relay`). For OpenAI WebRTC it does nothing, because the client
  owns the peer connection. So an OpenAI session stays in IDLE with
  `_voice=True` for its whole life.
- If the relay start fails, the state goes STARTING → ENDING → ENDED with
  reason `start_failed`.
- A fatal relay error (already reported as `voice_error` and the legacy
  `error{voice_relay_failed}`) runs `end_voice("error")` on its own task
  (`_on_voice_relay_fatal`), and only if that relay is still the live one.

## `end_voice(reason)`: the one teardown path

Reasons: `user_stop`, `agent_end`, `client_disconnect`, `error`, `shutdown`,
`switch` (a conversation switch, spec 12 §6.11a). It is idempotent, and
concurrent callers wait on `_voice_ended`.

1. **Ghost-voice guard**: return early only if the state is IDLE **and**
   `_voice` is False. (Before `f5b339a` it returned on IDLE alone. OpenAI
   sessions never leave IDLE, so they were never cleaned up and stayed
   `is_voice=True` for the life of the process. Every new subscriber then got
   `voice: true` + `summarizing` and showed "Preparing conversation…" with no
   one speaking.)
2. → ENDING; broadcast `voice_ending{reason, session_id}`.
3. Send the provider's `graceful_shutdown_frames()` best-effort, within
   `graceful_shutdown_s = 0.5` (Qwen: commit; Gemini: `activityEnd`; OpenAI: none).
4. `stop_voice_relay()`: stop the drain, keepalive and deferred watchdog,
   close the upstream WS, **clear the provider's resumption handle**.
5. Stop the `AudioRecorder`; `_voice_provider = None`; `_voice = False`;
   clear the deferred `response.create` slot and cancel its watchdog.
6. → ENDED; set `_voice_ended`; broadcast `voice_ended{reason, session_id}`,
   the legacy alias `voice_stopped`, and `voice_owner_active{active: false}`.
7. Start a **background summary refresh** (`refresh_summary_cache_if_stale`),
   so the next voice start finds the cache warm (see
   [prompt-budget.md](prompt-budget.md)).

The `OrchestratorSession` **stays in the pool**. The tab is the lasting thing.

## `restart_voice(...)`: re-arm on the same session

When `voice_start` arrives for a `local_id` whose session is not in voice
mode, `_handle_start` calls `restart_voice`. That moves ENDED → IDLE with a
new `_voice_ended` event. It applies any non-None voice fields (None means
"keep the previous value"), resolves them with `resolve_voice_target`, and
creates a **new provider**, so no Gemini resumption handle carries over. It
also re-arms the recorder if recording is on. Continuity comes from the
system prompt and history, not from provider state. The route then sends a
new `session_started` with `voice_initiator: true`.

### `_handle_start` decision table (for a `local_id` already in the pool)

| Request | Session state | Action |
|---|---|---|
| `voice_start` | voice ENDING | stop the orchestrator, wait, start a **new** session |
| `voice_start` | not voice | `restart_voice` + `session_started{voice_initiator:true}` (the wake-after-stop path) |
| `start` (text) | voice live, from the **owner's** socket | keep ownership: `session_started{voice:true, voice_initiator:true}` with voice metadata only (no `voice_session_update`, no new token). Clients re-send `start` on every foreground (spec 12 T-9) |
| `start` (text) | voice live, any other socket | subscribe as a passive viewer: `session_started{voice:true, voice_initiator:false}` |
| `voice_start` | voice live, same config | reconnect to the live relay; send the cached payload with `voice_initiator: true` (Android WS reconnect mid-call) |
| `voice_start` | voice live, **config drift** (`voice_config_drifts_from`) | while a turn runs → `error{voice_config_busy}`; otherwise stop and rebuild the orchestrator. Every subscriber is dropped, so clients must re-sync (spec 12 V-2) |

Before any of this, `_handle_start` waits for an orchestrator that is still
stopping (`await_orchestrator_stop_s = 5`) and answers
`error{orchestrator_stopping}` on timeout.

## Duplicate-handshake guard

A reconnecting Android client can send `voice_start` 2–3 times within a
second. Each one used to run through `_handle_start` on its own and open its
own upstream handshake. `orchestrator_ws` now serialises `voice_start` per
`local_id` with `_VOICE_START_LOCKS`, a `weakref.WeakValueDictionary` of
`asyncio.Lock`, so locks of finished sessions are garbage-collected. When the
guard fires, the journal says `voice_start coalesce: local_id=… already in
flight`. The relay has a second guard: `_setup_sent_for_ws` (a `WeakSet`)
refuses to send `setup` twice on the same upstream socket. Reconnects are
serialised by one `_reconnect_lock`. (Commit `5eb7804` plus the relay
refactor.)

## Wake after stop is instant

The second wake word after a call used to freeze for 60–90 s. The voice
prompt was rebuilt with a **synchronous** history summary on the critical
path between `voice_start` and `session_started`, and every new turn
invalidated the cache. The fix is the STALE-REUSE path plus the pre-warm in
`end_voice` (`2f8c8ac`, `0f34923`). A wake after stop now reaches
`session_started` in about 130 ms. While a summary really is being computed,
the route sends `voice_status: summarizing` **to the initiator only**; the
UI shows "Preparing conversation…". `voice_start_timing step=… dt_ms=…` log
lines time each step of the start path.

## Ownership across devices

Several clients (the A300M face, the POCO phone, the iPad, a browser) can be
subscribed to the same orchestrator conversation. Voice belongs to **one**
of them.

| Mechanism | What it does |
|---|---|
| `session.voice_owner_ws` | `register_voice_owner(ws)` after a successful `voice_start`. `clear_voice_owner_if(ws)` clears it, idempotent and checked by identity. The route's `finally` clears ownership **first** and only then calls `end_voice("client_disconnect")`, so a raise can't leave the pointer dangling. A passive subscriber's disconnect (an iPad refresh) never ends voice. |
| `voice_initiator` on `session_started` | `true` only for the socket that asked for voice. Only the initiator forwards `voice_session_update` to its transport (`71f19ce`). Clients treat a missing flag as **non-owner** (Android: `9b24d1a`), so a cold start never starts voice by itself. |
| `voice_command` | sent **only to the owner** (`_send_voice_command`). Broadcast only as a fallback when the owner's send fails or there is no owner. |
| `voice_owner_active{active, owner_local_id}` | broadcast after `voice_start` (`true`) and in `voice_ended` (`false`). Non-owners set `remoteActive` and show a disabled "Active elsewhere" button. They don't change their own voice state, play audio or send `voice_*` frames. `owner_local_id` is the shared orchestrator id, the same on every device, so it **must not** be used to decide ownership. |
| `voice_ending` / `voice_ended` / `voice_stopped` | broadcast to all subscribers. Only the owner acts on them. Non-owners log "Ignoring voice_ending — not the voice owner". |
| ownership loss (spec 12 V-13) | an owner that receives `voice_owner_active{active:true}` while not waiting on its own start knows another device took over. It tears down locally **without** sending `voice_stop`. (Web `VoiceController.takeOver()` is the explicit take-over.) |

Client implementations:

- Web: `apps/web/src/voice/core/VoiceController.ts` (`pendingStart`,
  `remoteActive`, `swallowOwnerActive`, 5 s ending timeout).
- Android: `apps/android/core/voice/.../session/DefaultVoiceSessionController.kt`
  (`isOwner`, `remoteVoiceActive`; it ignores `voice_command` and
  `voice_ending/ended` unless it is the owner). `ownerEchoesPending` counts
  its own `voice_start`s so their `voice_owner_active` echo is not taken for
  a takeover. A device that holds the call never becomes "not owner and
  still talking": `session_started{voice_initiator:false}` with no
  `voice_start` in flight, or a takeover `voice_owner_active`, ends the
  local call (V-13, no `voice_stop`). A `voice_ended` always tears down a
  transport that is still held.

The bug behind this design (2026-07-21, `b6d184f` + `afe77a4`): with the A300M
and the phone on one conversation, starting voice on one made the other show
"Connecting…" and then hang in "Ending…". Control frames and lifecycle events
were broadcast without owner scoping. The first fixed Android build crashed
the passive phone: an early `return@Box` inside a Compose content lambda
changed the group structure when the state flipped
(`IndexOutOfBoundsException` at `Stack.pop`). In `@Composable` content lambdas,
use `if/else`, never an early `return`. `adb logcat -b crash -d` keeps the
trace after the main buffer has rolled over.

## Client end and re-arm rules

- `stopVoice()` sends `voice_stop` and waits up to **5 s** for `voice_ended`,
  then finalizes locally. Every end path, including timeouts and errors,
  must deliver `voice_ended` or a local end to the conversation reducer.
  `voice_stop` ends only the voice connection; `stop` closes the whole tab.
  Voice lifecycle paths never send `stop`.
- On Android a **terminal** socket drop (`Disconnected(willReconnect=false)`)
  finalizes and re-arms the wake word. A transient drop keeps the session.
  On a real reconnect the owner sends `voice_start` again with the same
  config (`activeVoiceConfig`), not a plain `start`. If the link stays down
  past the 30 s retry budget, the client ends locally and offers a manual
  Reconnect.
- The server silently drops `voice_audio_in` that arrives before voice is
  ready, which covers mic chunks sent during a reconnect.
- The wake word re-arms 1500 ms after finalize (see
  [wake-word.md](wake-word.md#hand-off-to-realtime-voice-and-re-arm)).

## `session.update` self-heal after restart

On Android, a voice restart in a tab that had recently had voice (WS drop then
auto-reconnect, or end then re-arm) could open OpenAI on **bare defaults**:
voice `alloy`, a canned 505-character prompt, no history, no tools. The
mirrored `voice_session_update` arrived in a timing window where it reached
neither the new transport's queue nor the one-shot drain at start. The
first session in a tab always worked; only restarts failed. The fix makes
delivery idempotent instead of timing-dependent (`d4bc698`, carried into
`core/voice/.../delivery/CommandDelivery.kt`):

- the pre-transport command queue is unbounded under one lock (no command is
  lost or delivered twice), and it **caches the last `session.update`**;
- when the data channel opens, if no `session.update` was sent, the cached one
  is sent (log `session.update missing at DC_OPEN — re-asserting cached
  payload`);
- if `session.updated` echoes back while none was sent, the cached one is sent
  once;
- the "sent" flag is reset on cleanup.

The web client meets the same contract with its FIFO command queue
(spec 12 V-4: no cap, never drops, `session.update` first).

The diagnostic that found it: the Jetson journal logs HTTP requests but **not
WebSocket frames**, so a missing `voice_start` in journald proves nothing. The
truth was in on-device logcat (`pendingCommands=0`, no `start: draining N`
line), plus `"voice":"alloy"` in OpenAI's echo.

## Rules

1. **There is one teardown path.** Every voice end goes through
   `OrchestratorSession.end_voice(reason)`. Don't add ad-hoc teardowns, and
   don't drop the session from the pool to end voice.
2. **Ownership is decided by socket identity**, never by `local_id`. Only the
   owner's disconnect ends voice.
3. **Provider control frames go only to the owner.** Lifecycle broadcasts go to
   everyone, but only the owner acts on them. Pre-connect status
   (`summarizing`) goes only to the initiator.
4. **Every new flag defaults to the safe legacy behaviour** on the server
   (`voice_initiator: true` when sent), and clients default a missing flag to
   non-owner.
5. **A provider-bound config frame must reach the transport exactly when it is
   ready, whatever the timing.** Cache the last `session.update` and send it
   again at readiness; don't rely on a one-shot drain.
6. **A parked frame needs a trigger that does not depend on an event only that
   frame can cause.** A `response.create` gated behind an in-flight response
   is what makes the model produce its next event. If every drain waits for an
   inbound event, it waits forever: "tools execute but their results never
   reach the model". There are two layers with two queues: OpenAI WebRTC uses
   the route-layer single slot `session._deferred_response_create`
   (`9ef6dc1`); Qwen and any WS provider use the relay's `_deferred_events`
   (2026-08-01). Fixing one does not fix the other. Both now have a polling
   watchdog (every 2 s, up to 45 s), and the providers force-clear a gate
   that is ≥ 15 s old and clear it on barge-in. Signature: many
   `voice_command_deferred … reason=provider_gate` with no
   `voice_command_drain`. Verify any fix by parking a frame, clearing the gate
   *without* an inbound event, and checking that the frame ships.
7. <a id="clear-provider-server-side-state-on-rebuild"></a>**Clear the provider's
   server-side state on rebuild.** When a WS is torn down but the session
   stays, also clear any opaque handles the provider held for it (Gemini's
   resumption handle). Most realtime APIs invalidate them when the socket
   closes, and showing them again gets a successful handshake followed by an
   abort. Clear it in the lowest-level teardown (`stop_voice_relay`) so every
   caller benefits. Only a protocol-guaranteed path (Gemini's goAway
   reconnect inside the relay) may keep the handle. Check this for every new
   WS provider that has a continuation handle.
8. **The voice config cannot change mid-session.** A changed config rebuilds
   the orchestrator, or is refused with `voice_config_busy` during a turn.
9. **Diagnose from real logs before patching.** Line up the failure window on
   both sides: the Jetson journal (`journalctl -u agentic-backend.service`,
   especially `voice_session_closed` lines with `reason=`, `ws_code=`, audio
   counts) and Android logcat. Pull logcat early, because the A300M's buffer
   is short. "Stopped suddenly mid-call" almost always means there is a
   `voice_session_closed` line; read its reason first. If several hypotheses
   survive, say so. Tag diagnostic logging `DIAG` and remove it in the same
   session. If a fix breaks something else, the hypothesis was probably wrong:
   revert and diagnose again. See [debugging](../operations/debugging.md).

### Do not reintroduce (reverted 2026-06-04)

A long session spent effort on the WS keepalive layer when the real cause of
the wake-after-stop freeze was the 78 s synchronous summary
(`voice_status: summarizing` followed by no `session_started`). All of these
were reverted (−368 / +14 lines) and should stay out unless there is strong
new evidence:

- a server-side 45 s client-silence timeout (the web client sends no pings,
  so every web WS reconnected every 45 s);
- a server-side `{"type":"ping"}` heartbeat task tied to that timeout;
- an Android okhttp `pingInterval = 0` override, and an app-level two-way
  heartbeat;
- a custom uvicorn launcher that turned off protocol PING.

These were kept: `_safe_send_bytes` (no crash when sending after close),
`ping`/`pong` messages that are accepted and ignored, and Android's
voice re-arm on reconnect. The revert commit is `f98bc24`. It also removed
the backend's 15 s app-level `ping` (`f77cd62`, `44f1634`), so the current
backend sends **no** app-level pings. The Android client still accepts and
ignores a `ping` frame; that is harmless. Spec inventory 04 §4.6 still says
"backend pings every 15 s", which is out of date.

## History

- 2026-06-04: the `voice-lifecycle-refactor` branch: `VoiceLifecycle`,
  `end_voice`, `restart_voice`, STALE-REUSE + pre-warm. Field-tested on the
  A300M and a Xiaomi ("everything feels solid, stable and natural").
- 2026-06-05: duplicate-handshake guard + handle clearing (`5eb7804`).
- 2026-06-06: `voice_initiator` (`71f19ce`).
- 2026-06-09: the A→H relay refactor (typed errors, reconnect policy, voice
  state moved onto the session, persister). See
  [architecture.md](architecture.md#history).
- 2026-06-30: ghost-voice fix (`f5b339a`).
- 2026-07-21: owner-scoped commands + `voice_owner_active` (`b6d184f`,
  `afe77a4`); `session.update` self-heal (`d4bc698`).
- 2026-07-31 / 2026-08-01: deferred `response.create` watchdogs (OpenAI
  `9ef6dc1`, then Qwen and the relay).
- 2026-10: the new web and Android clients implement all of this from
  spec 12 §7.
- 2026-10-08: the owner's plain `start` no longer demotes it. Unlocking
  the phone mid-call re-sent `start` (T-9); the backend answered
  `voice_initiator:false` and Android flipped to non-owner while its WebRTC
  call ran on. It dropped the `voice_command`s carrying a `search_history`
  result (the model kept saying the search was "still running"), showed
  "Voice active on another device", and ignored `voice_ended`, so the call
  outlived `end_voice_session`. Fixed on both sides (see the table and the
  Android notes above). The Android takeover (V-13) is new in the same
  change.
