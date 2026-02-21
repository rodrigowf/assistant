# Voice-Enabled Orchestrator: Integration Plan

## Final Decisions (Confirmed)

| Decision | Choice |
|----------|--------|
| Model | `gpt-4o-realtime-2024-12-17` (latest non-preview) |
| Voice | `cedar` |
| Tools | Same full toolset as text agent — modularized for reuse |
| Input mode | Server-side VAD (no push-to-talk) |
| Text + voice | Voice-first for initial iteration — no simultaneous text input |
| Token endpoint auth | None for now (local-only) — can add later |

---

## Goal

Turn the current text-based orchestrator into a voice-enabled agent by integrating the OpenAI Realtime Voice API. The frontend establishes a direct WebRTC connection to OpenAI for low-latency audio. All transcription events, tool calls, and conversation state flow back to the orchestrator backend, which replaces the current Anthropic text model as the "brain" of the agent loop.

---

## Architecture Overview

```
FRONTEND                          BACKEND (FastAPI)                  OPENAI
──────────────────────────────    ──────────────────────────────     ───────────

  [Mic/Speaker]
       │
  [WebRTC Audio] ──────────────────────────────────────────────────► [Realtime API]
                                                                          │
  [Signal Channel]◄─────── ws /api/orchestrator/voice ──► (signaling)    │
       │                         │                                        │
       │   (transcripts,         │   (tool calls from model)             │
       │    tool_calls,          │◄──────────────────────────────────────┘
       │    voice_events)        │
       │                    [OrchestratorSession]
       │                         │
       │                    [Tool Registry]
       │                         │
       └─────────────────────────┘
         (tool results injected
          back via REST/signal)
```

**Key principle**: The WebRTC audio path is always frontend ↔ OpenAI directly (for sub-100ms latency). The backend only handles signaling, tool execution, and state persistence. The frontend sends a "mirror" of every realtime event back to the orchestrator WebSocket so the backend stays in sync.

---

## Step 1: Tool Modularization

### Why
Both the text agent (AnthropicProvider) and voice agent (OpenAIVoiceProvider) must share the same tools. The current `ToolRegistry.get_definitions()` returns Anthropic-format tool definitions. We need a second format for OpenAI.

### Changes to `orchestrator/tools/__init__.py`

Add `get_openai_definitions()` that converts from Anthropic format to OpenAI function format:

```python
def get_openai_definitions(self) -> list[dict]:
    """Return tools in OpenAI function calling format for Realtime API."""
    openai_tools = []
    for tool in self.get_definitions():
        openai_tools.append({
            "type": "function",
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"],  # JSON Schema — same format
        })
    return openai_tools
```

No changes to tool implementations — they remain in `agent_sessions.py`, `files.py`, `search.py`.

---

## Step 2: WebRTC Signaling Setup

### Backend: Ephemeral Token Endpoint

**New file**: `api/routes/voice.py`

```python
@router.post("/api/orchestrator/voice/session")
async def create_voice_session():
    """Exchange OPENAI_API_KEY for a short-lived ephemeral token."""
    # POST https://api.openai.com/v1/realtime/sessions
    # Body: { model: "gpt-4o-realtime-2024-12-17", voice: "cedar", ... }
    # Returns ephemeral token (TTL ~60s) — browser never sees OPENAI_API_KEY
```

No authentication on this endpoint (local-only for now).

**Environment variable needed**: `OPENAI_API_KEY`

Register in `api/app.py`.

### Frontend: WebRTC Setup

**New hook**: `frontend/src/hooks/useVoiceSession.ts`

```typescript
// Phase 1: Get ephemeral token from backend
const res = await fetch("/api/orchestrator/voice/session", { method: "POST" });
const { client_secret: { value: ephemeralKey } } = await res.json();

// Phase 2: Create RTCPeerConnection + data channel
const pc = new RTCPeerConnection();
const dc = pc.createDataChannel("oai-events");

// Phase 3: Add microphone track (VAD — server-side, no push-to-talk)
const stream = await navigator.mediaDevices.getUserMedia({
  audio: { echoCancellation: true, noiseSuppression: true }
});
pc.addTrack(stream.getTracks()[0]);

// Phase 4: SDP offer → OpenAI
const offer = await pc.createOffer();
await pc.setLocalDescription(offer);
const sdpResp = await fetch(
  "https://api.openai.com/v1/realtime?model=gpt-4o-realtime-2024-12-17",
  {
    method: "POST",
    headers: { Authorization: `Bearer ${ephemeralKey}`, "Content-Type": "application/sdp" },
    body: offer.sdp,
  }
);
await pc.setRemoteDescription({ type: "answer", sdp: await sdpResp.text() });

// Phase 5: Audio output
pc.ontrack = (e) => audioElement.srcObject = e.streams[0];
```

---

## Step 3: Event Loop Design

### New `VoiceProvider` class

**New file**: `orchestrator/providers/openai_voice.py`

The VoiceProvider does NOT call the model directly. Instead, the frontend mirrors all OpenAI Realtime events to the backend, and the VoiceProvider exposes an `inject_event()` method that feeds them into the agent loop.

```python
class OpenAIVoiceProvider(ModelProvider):
    async def inject_event(self, event: dict) -> None:
        await self._queue.put(event)

    async def create_message(self, messages, tools, system) -> AsyncIterator[OrchestratorEvent]:
        while True:
            event = await self._queue.get()
            yield self._translate(event)
            if event["type"] in ("response.done", "error"):
                break
```

### Session configuration (session.update)

When voice session starts, backend sends `session.update` to OpenAI via the frontend data channel:

```json
{
  "type": "voice_command",
  "command": "session.update",
  "payload": {
    "session": {
      "model": "gpt-4o-realtime-2024-12-17",
      "voice": "cedar",
      "instructions": "<orchestrator system prompt>",
      "tools": [...],
      "tool_choice": "auto",
      "modalities": ["text", "audio"],
      "turn_detection": {
        "type": "server_vad",
        "threshold": 0.5,
        "prefix_padding_ms": 300,
        "silence_duration_ms": 800
      }
    }
  }
}
```

Key VAD settings:
- `type: "server_vad"` — OpenAI manages speech detection (no push-to-talk)
- `silence_duration_ms: 800` — 800ms silence ends a turn

---

## Step 4: Tool Call Execution Flow

### Protocol

1. OpenAI fires `response.function_call_arguments.done` → frontend mirrors to backend
2. Backend executes tool via `ToolRegistry.execute()`
3. Backend sends `voice_command` with `conversation.item.create` (function_call_output) + `response.create`
4. Frontend forwards both to OpenAI via data channel
5. OpenAI resumes and speaks the answer

### Backend WebSocket handler addition

In `api/routes/orchestrator.py`, add handling for `voice_event` message type. The handler injects the event into the active `VoiceProvider`.

### Frontend bridge

```typescript
// Forward all data channel events to backend
dc.onmessage = (e) => {
  const event = JSON.parse(e.data);
  orchestratorWs.send(JSON.stringify({ type: "voice_event", event }));
  handleLocalEvent(event);  // UI updates
};

// Forward backend commands to OpenAI
case "voice_command":
  dc.send(JSON.stringify({ type: msg.command, ...msg.payload }));
  break;
```

---

## Step 5: Frontend UI

### VoiceButton component

States: `off` | `connecting` | `active` | `speaking` (model) | `thinking` | `tool_use`

- Added to `ChatInput.tsx` (alongside send/interrupt)
- In voice mode, the text input area is hidden (no simultaneous text input)

### Transcript display

Voice turns appear as normal messages in MessageList using the existing reducer:
- `conversation.item.created` (user speech) → USER_MESSAGE action
- `response.audio_transcript.delta` → TEXT_DELTA action
- `response.audio_transcript.done` → TEXT_COMPLETE action

### Interruption handling

When `input_audio_buffer.speech_started` is received:
- Stop audio element playback immediately (prevents echo)
- Mark current streaming assistant message as complete
- Backend marks turn as interrupted in JSONL

### New hook: `useVoiceOrchestrator.ts`

Wraps the WebRTC session and orchestrator WebSocket bridge:
- `startVoice()`, `stopVoice()`
- `voiceStatus`: off | connecting | active | ...
- Routes voice events into existing message reducer

---

## Step 6: JSONL Persistence

Voice turns are persisted using the same format as text sessions with added `source` field:

```json
{ "type": "voice_meta", "voice": true, "openai_model": "gpt-4o-realtime-2024-12-17", "voice_name": "cedar", "timestamp": "..." }
{ "type": "user", "message": { "role": "user", "content": "[voice] Can you list my sessions?" }, "source": "voice_transcription", "timestamp": "..." }
{ "type": "assistant", "message": { "role": "assistant", "content": "Here are your sessions..." }, "source": "voice_response", "timestamp": "..." }
```

---

## Implementation Order

1. **Tool modularization** — `get_openai_definitions()` in ToolRegistry
2. **Backend ephemeral token endpoint** — `api/routes/voice.py`, register in `api/app.py`
3. **VoiceProvider** — `orchestrator/providers/openai_voice.py`
4. **Backend WebSocket voice_event handling** — `api/routes/orchestrator.py`
5. **Frontend WebRTC hook** — `useVoiceSession.ts`
6. **Frontend voice bridge** — `useVoiceOrchestrator.ts`
7. **Frontend VoiceButton** — `VoiceButton.tsx`, wired into `ChatInput.tsx`
8. **Session.update injection** — tools + system prompt sent on connect
9. **JSONL persistence** — voice turns persisted in `OrchestratorSession`
10. **Interruption + VAD handling** — frontend stops playback on barge-in

---

## New Files

```
api/routes/voice.py
orchestrator/providers/openai_voice.py
frontend/src/hooks/useVoiceSession.ts
frontend/src/hooks/useVoiceOrchestrator.ts
frontend/src/components/VoiceButton.tsx
frontend/src/api/voice.ts
```

## Modified Files

```
api/app.py                          # Register voice router
api/routes/orchestrator.py          # Handle voice_event, send voice_command
orchestrator/tools/__init__.py      # Add get_openai_definitions()
orchestrator/session.py             # Voice mode support, voice JSONL persistence
orchestrator/config.py              # Add voice: bool field
orchestrator/types.py               # Add voice-related event types
frontend/src/types.ts               # Add RealtimeEvent types
frontend/src/components/ChatInput.tsx  # Add VoiceButton
frontend/src/api/rest.ts            # Add fetchEphemeralToken()
```

---

## Key Challenges

### Tool result timing
OpenAI stalls if tool results aren't returned promptly. Add per-tool timeout; on timeout send `"Tool is running..."` immediately and a follow-up.

### WebSocket drop mid-turn
Buffer pending `call_id` values in frontend. On reconnect, replay outstanding tool events or send error result to unblock OpenAI.

### Concurrent tool calls
OpenAI can issue multiple tool calls in one response. Execute in parallel with `asyncio.gather()`, send all results before `response.create`.

### Audio echo
Use `echoCancellation: true` in getUserMedia. Server-side VAD also suppresses assistant audio from being re-transcribed.

---

## Dependencies

```
# Python — add to requirements.txt
openai>=1.50.0   # Only for ephemeral token REST call (or use httpx directly)

# Frontend — no new packages (WebRTC is native)
```
