---
name: openai-realtime
category: archie/voice
tags: [voice, openai, openai-realtime, gpt-realtime, webrtc, ephemeral-token, vad, whisper-1, gpt-audio, response-gate]
created: 2026-04-17
modified: 2026-10-06
summary: OpenAI Realtime over WebRTC (ephemeral token, session.update schema, VAD, response gate) and the separate gpt-audio and whisper-1 paths.
source: curated (consolidated from memory notes assistant/voice/voice-multimodel-plan.md, assistant/providers/openai_audio_model_gpt_audio.md, assistant/architecture/voice_subsystem.md, auto-memory reference_openai_audio_model_gpt_audio, project_qwen_voice_gate_no_staleness_clear, project_voice_session_update_restart_race_2026_07_21; verified against code 2026-10-06)
references:
  - architecture.md
  - lifecycle.md
  - prompt-budget.md
  - wake-word.md
  - ../architecture/orchestrator.md
  - ../specs/12-client-protocol.md
---

# OpenAI Realtime

OpenAI is Archie's **default voice provider** (`DEFAULT_VOICE_PROVIDER =
"openai"`, model `gpt-realtime`, voice `cedar`). It is the only provider whose
audio does **not** pass through the backend: the client opens a WebRTC peer
connection straight to OpenAI. The backend only mints the credential, builds
the `session.update`, runs tools and persists the conversation. For the shared
design see [architecture.md](architecture.md).

Code: `backend/orchestrator/providers/openai_voice.py` (`OpenAIVoiceProvider`),
the WebRTC branch of `backend/api/routes/orchestrator.py`
(`_dispatch_voice_commands`, `_send_voice_command`, the deferred
`response.create` slot), and the clients `apps/web/src/voice/transports/webrtc.ts`
and `apps/android/core/voice/.../transport/WebRtcTransport.kt`.

## Connection

1. The client sends `voice_start` on the orchestrator WS.
2. `_attach_voice_payload` calls `get_connection_info()`, which POSTs
   `{"session": {"type": "realtime", "model": …}}` to
   `https://api.openai.com/v1/realtime/client_secrets` with the server-side
   `OPENAI_API_KEY`. The answer carries an **ephemeral token**
   (`value`, `expires_at`). The raw key never leaves the backend on this path.
3. `session_started` carries `voice_connection_info`
   (`connection_type: "webrtc"`, `endpoint:
   https://api.openai.com/v1/realtime/calls?model=<model>`, `ephemeral_token`,
   24 kHz PCM16 in and out) and `voice_session_update`.
4. The client creates the peer connection, the mic track and the data channel
   **`oai-events`**. It POSTs the SDP offer to `endpoint` with
   `Authorization: Bearer <ephemeral_token>` and
   `Content-Type: application/sdp`.
5. When the data channel opens, the client sends queued frames in order, the
   `session.update` first.

`POST /api/orchestrator/voice/session?provider&model&voice&transcription_language&endpoint`
(`backend/api/routes/voice.py`) returns the same `connection_info`, plus the
legacy top-level `client_secret`. Clients use it only as a fallback when
`session_started` has no `voice_connection_info`, and must then pass all five
parameters (spec 12 V-3).

The legacy endpoints `/v1/realtime/sessions` and `/v1/realtime` still work,
but the code uses the GA `client_secrets` + `calls` pair.

## `session.update` (GA schema)

```json
{"type": "session.update",
 "session": {"type": "realtime", "model": "gpt-realtime",
   "instructions": "<system prompt>", "tools": [...], "tool_choice": "auto",
   "output_modalities": ["audio"],
   "audio": {
     "input":  {"format": {"type": "audio/pcm", "rate": 24000},
                "transcription": {"model": "whisper-1"},
                "turn_detection": {"type": "server_vad", "threshold": 0.5,
                                   "prefix_padding_ms": 300, "silence_duration_ms": 800}},
     "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": "cedar"}}}}
```

- The GA model **silently ignores the old beta shape** (`modalities`, flat
  `voice` and `input_audio_transcription`, top-level `turn_detection`). It then
  runs on defaults: no system prompt, no tools, no transcription. That looks
  like "voice mode is isolated from my system". The tell-tale is
  `"voice":"alloy"` in OpenAI's `session.updated` / `response.done` echo; a
  configured session says `cedar`.
- `instructions` must stay under **16,384 tokens**. See
  [prompt-budget.md](prompt-budget.md).
- Input transcription uses `whisper-1`. The `language` hint is supported, but
  the UI doesn't expose it for OpenAI (`_OPENAI_TRANSCRIPTION_LANGUAGES = []`).
- VAD is **server-side** (`DEFAULT_VAD` above), not Silero: the backend never
  sees OpenAI's audio.
- The `session.update` is delivered **idempotently on Android**. The client
  caches the last one and sends it again at DC-open, or at the
  `session.updated` echo, if none was sent. See
  [lifecycle.md](lifecycle.md#sessionupdate-self-heal-after-restart).

## Models and voices

| Model | Notes |
|---|---|
| `gpt-realtime` | default |
| `gpt-realtime-mini` | cheaper |
| any other live-discovered id | accepted; voices and languages come from the default entry (`get_model_entry`) |

Voices: `cedar` (default), `marin` (both Realtime-only, recommended), `alloy`,
`ash`, `ballad`, `coral`, `echo`, `sage`, `shimmer`, `verse`. A voice cannot
change mid-session: once audio has been emitted, it is locked.

## Event translation and mirroring

The client mirrors every data-channel event (inbound and outbound) to the
backend as `voice_event`. `OpenAIVoiceProvider._EVENT_TRANSLATORS` maps them to
canonical events:

| OpenAI event | Canonical |
|---|---|
| `response.output_audio_transcript.delta` / legacy `response.audio_transcript.delta` | `TextDelta` |
| `response.output_audio_transcript.done` / legacy `…audio_transcript.done` | `TextComplete` |
| `response.function_call_arguments.done` | `ToolUseStart` (the route also starts `_handle_voice_tool_call`) |
| `response.done` | `TurnComplete` (with usage) |
| `input_audio_buffer.speech_started` | `VoiceInterrupted` |
| `error` | `ErrorEvent` |

Tool results go back as `voice_command` frames: `conversation.item.create`
(`function_call_output`, truncated to 8,000 characters by
`truncate_voice_tool_output`), then `response.create`. `voice_command` is
sent **only to the voice owner's socket** (`_send_voice_command`). It is
broadcast only as a fallback when the owner's socket is gone.

## The `response.create` gate

The model may call several tools at once. OpenAI rejects a second
`response.create` while a response is in flight
(`conversation_already_has_active_response`). The gate:

- `on_inbound_event` sets "active" on `response.created`. It clears it on
  `response.done` / `.cancelled` / `.failed`, **and on barge-in signals**
  (`input_audio_buffer.speech_started`, `output_audio_buffer.cleared`), because
  the terminal event after a cancel is the fragile one.
- `mark_response_create_sent()` sets "active" optimistically at dispatch, so
  a parallel tool result can't slip through before `response.created` comes
  back.
- A gated `response.create` waits in the **single-slot**
  `session._deferred_response_create` (N parallel results collapse into one
  response). It is drained by `_drain_deferred_response_create` after every
  mirrored event, **and by a polling watchdog** (`_arm_deferred_drain_watchdog`:
  every 2 s, for up to 45 s).
- A gate active for ≥ 15 s (`_RESPONSE_ACTIVE_STALE_SECONDS`) is force-cleared
  inside `should_gate_event` / `gate_cleared`.

Why the watchdog exists: a parked `response.create` for a tool result is
exactly what makes the model produce its next event. With only event-driven
drains, nothing ever shipped it. "The agent cannot use any tools anymore after
some specific tool call." It was fixed in `9ef6dc1` (2026-07-31). The general
rule is in [lifecycle.md](lifecycle.md#rules).

Diagnostic signature: many `voice_command_deferred … reason=provider_gate`
lines in the journal and **no** `voice_command_drain` lines.

## Client-side echo handling (Android)

WebRTC uses software AEC, NS and AGC (hardware AEC/NS are off), with all the
`goog*` constraints set. The mic is ducked on `response.created` /
`output_audio_buffer.started`. It is restored 2000 ms after
`output_audio_buffer.stopped` or `.cleared` (`OpenAiDucker`), **never on
`response.done`**, because audio can still play 6 s or more after it. This
path is separate from the WS providers' drain-then-restore ducker. See
[architecture.md](architecture.md#client-side).

## Error classification

`classify_close_reason`: `insufficient_quota` / "exceeded your current
quota" → `QUOTA_EXCEEDED` (fatal); `rate_limit_exceeded` / 429 →
`RATE_LIMIT` (recoverable); "Incorrect API key" / 401 → `AUTH`;
`model_not_found` / "Unsupported model" → `MODEL_UNAVAILABLE`. Because there
is no backend WS, most errors reach the backend as mirrored `error` events.

## Related OpenAI paths that are not Realtime

| Path | Model | Where |
|---|---|---|
| **One-shot voice message** (`send_audio`: the talk word, push-to-talk, the web voice-message button) | **`gpt-audio`** family via Chat Completions with `input_audio` | `OrchestratorSession._send_audio_inner` → `resolve_audio_model()` = `default_audio_model` from `assistant_config.json` (Settings → Conversation model → Audio model provider + Audio model; "Server default" clears it), else `AUDIO_FALLBACK_MODEL_ID = "gpt-audio"` |
| **Wake-word confirmation** | `whisper-1` transcription, called directly from Android | [wake-word.md](wake-word.md) |
| **History summarizer** | `summarizer_model`, default `gpt-5.1` | [prompt-budget.md](prompt-budget.md) |

**`gpt-4o-audio-preview` is gone.** It returns 404 `model_not_found` on
Rodrigo's account, because OpenAI renamed the audio-input chat models to
`gpt-audio` / `gpt-audio-mini`. Verified with real `input_audio` calls on
2026-07-21; switched in `254c818`. The code keeps the old ids in
`RETIRED_MODEL_IDS` (`backend/orchestrator/config.py`) and ignores them if a
config names them. Where the family is wired:

- `backend/orchestrator/config.py`: `AVAILABLE_MODELS["gpt-audio" | "gpt-audio-mini"]`,
  `DEFAULT_MODEL_ID = "gpt-audio"`, `AUDIO_FALLBACK_MODEL_ID`,
  `TEXT_FALLBACK_MODEL_ID = "gpt-4o"`, `model_requires_audio()`;
- `backend/orchestrator/providers/openai_text.py`: `OpenAIModel.GPT_AUDIO` / `GPT_AUDIO_MINI`;
- `backend/orchestrator/providers/discovery.py`: `_OPENAI_AUDIO_INPUT_RE` matches the
  `^gpt-audio` prefix explicitly, because `gpt-audio-mini` does not end in "audio".

`gpt-audio` models reject text-only requests ("This model requires that
either input content or output modality contain audio", verified 2026-10-04).
Text models reject `input_audio`. So every turn goes to a model that accepts
it: typed turns go to `TEXT_FALLBACK_MODEL_ID`, audio turns to the audio
model. If audio chat 404s again, list the account's models (`GET /v1/models`,
filter for `audio`) and update the spots above. Don't assume a hard-coded id
still exists.

Before you run paid evaluations that call realtime or audio models, estimate
the cost and confirm with Rodrigo. On 2026-10-06 such evals used up the
OpenAI credits that voice mode depends on.

## Environment

- `OPENAI_API_KEY` in `context/.env` (sourced by `context/scripts/run.sh`).
- `GET /api/config/openai-key` also serves this key to LAN peripherals for
  Whisper (see [wake-word.md](wake-word.md)).
