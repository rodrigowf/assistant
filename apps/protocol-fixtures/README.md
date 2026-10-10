# Protocol conformance fixtures

Shared test vectors for the client data layer specified in
`docs/specs/12-client-protocol.md` (§3.6 connection manager, §4 reducer, §5 history).
The web client (TypeScript, vitest) and the Android client (Kotlin `:core`, JUnit) MUST both pass every
fixture in this directory. A fixture that fails on one platform is a spec bug or a platform bug, never an
accepted "platform difference".

All current fixtures are `"source": "synthetic"`: hand-written from the backend code. Recorded fixtures
(real WebSocket transcripts from the Jetson) will be added later with `"source": "recorded"` and the same schema.

## File schema

```jsonc
{
  "name": "replay_after_reconnect_overlap",          // == file name without .json
  "description": "What the fixture proves, and which rule/bug it covers.",
  "source": "synthetic",                             // "synthetic" | "recorded"
  "session": {                                       // builds the initial Conversation
    "kind": "agent",                                 // "agent" | "orchestrator"
    "provider": "claude",                            // "claude" | "qwen" | "gemini" | null (orchestrator)
    "local_id": "L1",
    "sdk_id": "sdk-1",                               // null for a new agent session
    "live_status": "tool_use",                       // optional: pool/live status at open (spec ST-2)
    "voice_active": true                             // optional: voice is live on some device (orchestrator)
  },
  "initial_history": null | {                        // optional: GET /api/sessions/{id}/messages response
    "messages": [ MessagePreview... ], "total_count": 5, "has_more": false, "start_index": 0
  },
  "events": [ ...server frames, exactly as the backend sends them, in arrival order... ],
  "client_actions": [ { "at": 3, "type": "send", "text": "..." }, ... ],   // optional
  "expected": {
    "entries": [ ...normalised entries... ],         // compared exactly
    "orphan_results": [ ... ],                       // compared exactly (missing = [])
    "unattributed_results": [ ... ],                 // compared exactly (missing = [])
    "queue": [ ... ],                                // compared exactly, only when present
    "state": { ... },                                // only the keys present are compared
    "controller": { ... }                            // optional; voice-controller tests only (see below)
  }
}
```

Server frames use the exact field names of the backend (`backend/api/serializers.py`, `backend/api/pool.py`,
`backend/api/routes/chat.py`, `backend/api/routes/orchestrator.py`), including `seq`/`stream_id` where Claude agent
sessions stamp them. History messages use the REST `MessagePreviewResponse` shape
(`{role, text, blocks: [{type, text, tool_use_id, tool_name, tool_input, output, is_error}], timestamp}`).

## Runner algorithm

```
conv = new Conversation(fixture.session)           // spec §2.3 initial values; live_status per ST-2;
                                                   // the socket is already subscribed (no start pending)
if fixture.initial_history != null:
    reduce(history_page{mode: "replace", response: fixture.initial_history})   // spec §5.1
conv.history.loaded = true
for i in 0 .. events.length:                       // note: inclusive of events.length
    for a in client_actions where a.at == i, in file order:
        apply(a)
    if i < events.length:
        onFrame(conv, events[i])                   // spec §3.6 (seq dedupe, pre-start hold, reload buffer) → §4.3
actual = normalise(conv)
compare(actual, fixture.expected)
```

`initial_history` is applied before the first frame. This models the cold-open order of spec §5.2: the
client subscribes, holds frames, applies the REST page, then applies the held frames in arrival order.
So `events` that follow `initial_history` may legitimately overlap it (see `history_live_overlap_dedupe`).

### `client_actions` vocabulary

| `type` | Fields | Effect (spec reference) |
|---|---|---|
| `send` | `text` | `local_send` (§4.3, §6.1/6.2) |
| `send_audio` | — | `local_send_audio` (§6.16) |
| `inject` | `text` | `local_inject` (§6.15) |
| `interrupt` | — | `local_interrupt` (§6.3) |
| `compact` | — | `local_compact` (§6.4) |
| `stop` | — | `local_stop`: the next `session_stopped` is our own ack |
| `permission_response` | `request_id`, `decision` | no reducer effect (the resolve comes from the server) |
| `datachannel_event` | `event` | OpenAI data-channel inbound event, owner only (§4.7) |
| `voice_local_end` | — | voice torn down locally (§7.6) |
| `ws_closed` | — | the socket closed: entries are untouched; `gapPossible` per §3.3/SEQ-7 |
| `ws_open` | — | the socket reopened: `sendStart` (§3.3) records the `start` message (with `resume_from` when allowed, and `reattach: true`: an automatic re-start of a conversation that was subscribed, OPEN-2) and starts holding frames until `session_started` |
| `rest_page` | `mode` (`replace` \| `prepend` \| `reconcile`), `response` | `reduce(history_page{mode, response})` (§5.1, §5.5). A `replace` while a reload is pending (after `replay_overflow`) completes the canonical reload and flushes the held frames (§5.6). |

## Normalisation

`normalise(conv)` produces exactly these shapes. Nothing else is compared.

```jsonc
// entries, in timeline order
{ "kind": "user", "text": "...", "origin": "local|echo|voice|audio|inject|history", "state": "sent|pending" }
      // + "streaming": true only while a Gemini transcript is still growing (omitted when false)
{ "kind": "notice", "notice": "error|interrupted|compaction|background|command", "text": "..." }
{ "kind": "assistant", "blocks": [
    { "type": "text",     "text": "...", "streaming": false },
    { "type": "thinking", "text": "...", "streaming": false },
    { "type": "tool", "tool_use_id": "t1", "tool_name": "Bash", "tool_input": {...},
      "status": "running|done|error|no_result", "output": "..." | null },
      // + "inferred": true only when attached by position (spec R-4)
    { "type": "permission", "request_id": "r1", "tool_name": "ExitPlanMode",
      "state": "pending|allowed|denied", "responder": "user|orchestrator|system" | null, "message": "..." | null }
] }

// orphan_results: insertion order of the orphan map
{ "tool_use_id": "c99", "output": "stray", "is_error": false }
// unattributed_results: arrival order
{ "output": "", "is_error": false }
// queue (the tray): order of the tray
{ "text": "second", "owner": "local|remote" }
```

Rules:

- **No ids are compared.** Client-generated entry/block ids, timestamps, object identity and
  `scope`/`origin`/`implicitlyClosed`/`continuationOf`/`executing`/`progress` are internal and not part of
  the normalised form. Only server-provided ids (`tool_use_id`, `request_id`) appear.
- `output` is the normalised string (spec `normalizeOutput`): objects and arrays are serialised with
  `JSON.stringify` (no whitespace, key order preserved). `null` means the card never received a result.
- `tool_input` is compared as a JSON value (key order irrelevant).
- Numbers are compared as JSON numbers (`0.0123` == `0.0123`).

### `expected.state` keys

Only the keys present in a fixture are compared.

| Key | Meaning |
|---|---|
| `status` | `SessionStatus` (spec §2.5) |
| `in_turn` | `conv.inTurn` |
| `stall` | `{elapsed_seconds, last_tool_name, last_tool_use_id}` or `null` |
| `termination` | `{reason, detail, sdk_session_id}` or `null` |
| `checkpoint` | `{stream_id, seq}` or `null` |
| `context_tokens`, `cost`, `turns` | counters |
| `sdk_id`, `local_id` | identity after the run |
| `voice_active` | `conv.voiceActive` |
| `gap_possible`, `reloading` | connection-manager flags |
| `history_has_more`, `history_start_index` | pagination state |
| `last_start` | the last `start` message produced by `ws_open` (exact JSON) |
| `connection_banner` | `conv.connectionBanner`: `{code, detail}` or `null` (§3.3, SEQ-8) |
| `agent_approvals` | `conv.agentApprovals` in list order, each normalised as `{local_id, request_id, tool_name, tool_input}` (PM-5) |

### `expected.controller`

Some voice fixtures also list expectations for the voice controller (spec §7), for example
`{"voice_state": "off", "remote_active": false, "input_enabled": true}`. The reducer runner ignores this
key. The voice-controller test harness of each platform replays the same `events` and checks it.

## Adding fixtures

1. Derive `expected` by hand from the spec pseudo-code, not by running a client. If the spec does not
   determine the outcome, fix the spec first.
2. Keep frames byte-for-byte compatible with the backend (field names, `seq`/`stream_id` only where the
   backend stamps them, `stream_id` format `"<local_id>:<epoch_ms>"`).
3. One behaviour per fixture; say in `description` which rule or bug ID it covers, and add it to the table
   in spec §11.
4. Recorded fixtures: capture both the frames and the REST pages the client fetched, strip secrets
   (ephemeral tokens, API keys, base64 audio), keep `seq` values as recorded.
