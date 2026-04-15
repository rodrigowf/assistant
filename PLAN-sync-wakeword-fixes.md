# Plan: Fix Wake Word & Sync Issues

## Problems

1. **Wake word stops after screen lock / app restart**
2. **Android app gets out of sync with server (tabs, orchestrator conversation)**
3. **Web frontend (normal + compat/iPad) gets out of sync after screen lock or tab switch**

---

## Root Cause Analysis

### Problem 1 — Wake word stops after screen lock

**Primary cause:** `AssistantService` uses `START_STICKY`, so Android restarts it after being killed — but the sticky restart sends a **null intent**. In `onStartCommand`, when `intent == null`, none of the branches execute, so `wakeWordDetector` is never recreated after a restart.

**Secondary cause:** When the screen locks while a voice session is active, `pause()` was called (setting `isPaused=true`). If the voice session cleanup races with a screen lock event, `resume()` may never be called. Once `isPaused=true` and `isActive=false` (e.g. if the detector fully stopped due to an audio error), `resume()` silently returns (`if (!isActive || !isPaused) return`) — there's no way out.

**Tertiary cause:** There is no `BroadcastReceiver` for `Intent.ACTION_USER_PRESENT` (screen-unlock after keyguard/PIN). The service has no way to re-arm detection when the user unlocks the phone.

**Fix:**
- Store the last-used wake word config in the service (in-memory fields: `lastWakeWord`, `lastVoiceWord`, `lastEnabled`).
- When `onStartCommand` receives a null intent (sticky restart), re-apply the stored config.
- Register a `BroadcastReceiver` for `Intent.ACTION_USER_PRESENT` in `AssistantService.onCreate()`. On receive, call `wakeWordDetector?.resume()` — and if the detector is stopped/null and `lastEnabled` is true, re-create and start it.
- Make `resume()` fall back to `start()` when `!isActive` (i.e., treat a fully-stopped detector as resumable).

---

### Problem 2 — Android app out of sync

**Primary cause — messages lost during disconnect:** In `AssistantViewModel.handleWebSocketEvent(SessionStarted)`, history is only loaded when `_messages.value.isEmpty()` (line 355). After reconnect, messages are already in memory from before the disconnect, so history is never refreshed. Any messages that completed (or turns that ran) while the WebSocket was down are silently dropped.

**Secondary cause — sessions list not refreshed on reconnect:** `refreshSessions()` is called in `SessionStarted` handler (line 368), but that's only reached after a `Start` message is sent and the backend responds. If the reconnect fails or the `Start` is sent before the pool is in a clean state, sessions can be stale.

**Fix:**
- In `handleWebSocketEvent(SessionStarted)`, after reconnecting to an existing session, always fetch the latest messages from the server and append any that are newer than what's in `_messages`. Use the JSONL session ID to fetch paginated messages and compare count/content.
- More practically: after `SessionStarted`, if we're reconnecting (i.e., `resumeId != null`), always re-fetch and replace messages regardless of whether `_messages` is empty. This is safe because the fetch is idempotent and the server is the source of truth.

---

### Problem 3 — Web frontend out of sync after background/tab switch

**Primary cause — `start` not re-sent after WebSocket reconnects:** In `useChatInstance.ts`, `handleOpen` only sends a `start` message if `pendingStartRef.current != null` (line 519). After the initial mount, `pendingStartRef.current` is set to `null` (line 572) and never re-set. So when `useWebSocket` reconnects the socket after a visibility change, `handleOpen` fires but does nothing — the backend never gets a re-subscription `start` message for this client. The socket is technically open but the backend has no active subscription for this WebSocket.

**Secondary cause — `useReconnectPoolSessions` only runs on mount:** `useEffect([], [])` (line 45) means sessions created while the browser tab was in the background are never synced. When the user returns to the tab, the tab list shows stale state from before backgrounding.

**Fix:**
- In `useChatInstance.ts`: always store the last `resumeSdkId` in a stable ref. In `handleOpen`, always send a `start` message (using the stored localId and resumeSdkId), not just when `pendingStartRef.current` is set. This ensures every reconnect properly re-subscribes to the backend session.
- In `useReconnectPoolSessions.ts`: add a `visibilitychange` event listener. When `document.hidden` becomes `false`, re-run the `listPoolSessions()` logic to sync any sessions created while the tab was hidden.

The compat (iPad) frontend shares these hooks, so both get fixed automatically.

---

## Implementation Plan

### Step 1 — Android: Fix sticky restart in `AssistantService`
- Add `private var lastWakeWord`, `lastVoiceWord`, `lastEnabled` fields.
- In `onStartCommand`, when the enable/update branch fires, save the values to these fields.
- When `intent == null` and `lastEnabled == true`, call `startWakeWord(lastWakeWord, lastVoiceWord)`.

### Step 2 — Android: Add screen-unlock receiver in `AssistantService`
- In `onCreate`, register a `BroadcastReceiver` for `Intent.ACTION_USER_PRESENT`.
- In the receiver: if `wakeWordDetector?.isPaused == true`, call `wakeWordDetector?.resume()`. If detector is null and `lastEnabled`, call `startWakeWord(...)`.
- Unregister in `onDestroy`.
- Note: `ACTION_USER_PRESENT` must be registered dynamically (not in manifest) — already the correct approach.

### Step 3 — Android: Make `WakeWordDetector.resume()` handle fully-stopped state
- In `resume()`, if `!isActive` but `lastEnabled` context exists, fall back to calling `start()` instead of returning silently. This covers edge cases where the detector fully stopped instead of just pausing.
- Simpler alternative: in `AssistantService`, the `ACTION_USER_PRESENT` receiver calls `startWakeWord()` unconditionally (stopping any existing detector first), which is cleaner than patching `resume()`.

### Step 4 — Android: Re-fetch messages after reconnect
- In `AssistantViewModel.handleWebSocketEvent(SessionStarted)`:
  - Remove the `&& _messages.value.isEmpty()` guard on the history-fetch block.
  - Always re-fetch when `resumeId != null`, but do a smart merge: compare fetched message count vs current count; only update `_messages` if server has more.

### Step 5 — Web frontend: Always re-send `start` on WebSocket reconnect
- In `useChatInstance.ts`:
  - Change `pendingStartRef` from nullable to always holding the latest `{ resumeSdkId, localId }`.
  - In `handleOpen`, always send the `start` message unconditionally (using `localIdRef.current` and `resumeSdkIdRef.current`).
  - Remove the `if (pending)` guard — replace with always-send logic.

### Step 6 — Web frontend: Sync sessions list on tab visibility change
- In `useReconnectPoolSessions.ts`:
  - Extract the fetch logic into a named function `syncPoolSessions`.
  - Add `document.addEventListener("visibilitychange", handler)` where handler calls `syncPoolSessions()` when `!document.hidden`.
  - Clean up in the effect's return function.

---

## Files to Change

| File | Steps |
|------|-------|
| `android/app/src/main/java/com/assistant/peripheral/service/AssistantService.kt` | 1, 2 |
| `android/app/src/main/java/com/assistant/peripheral/viewmodel/AssistantViewModel.kt` | 4 |
| `frontend/src/hooks/useChatInstance.ts` | 5 |
| `frontend/src/hooks/useReconnectPoolSessions.ts` | 6 |

Step 3 is absorbed into Step 2 (the receiver calls `startWakeWord()` which handles both cases cleanly).

---

## Testing Checklist

- [ ] Wake word triggers after screen lock + unlock (no voice session active)
- [ ] Wake word triggers after screen lock + unlock (voice session was active before lock)
- [ ] Wake word triggers after app is killed and restarted by Android
- [ ] Android: after reconnecting, any messages that came in during disconnect are visible
- [ ] Android: sessions list is up to date after reconnect
- [ ] Web: switching browser tabs and returning shows current orchestrator conversation
- [ ] Web: new sessions created by Android app appear in web tab list after returning from background
- [ ] Compat (iPad): same as web tests above
- [ ] No duplicate `start` messages causing session restarts on web
- [ ] No regression in normal (no-disconnect) flow

---

## Implementation Notes (actual fixes)

### Files changed
- `android/.../service/AssistantService.kt` — SharedPreferences persistence; `ACTION_SCREEN_ON` + `ACTION_USER_PRESENT` receivers; `resumeWakeWord` does full restart
- `android/.../viewmodel/AssistantViewModel.kt` — `reconnectIfNeeded()`; always re-fetch messages on reconnect
- `android/.../MainActivity.kt` — `onResume()` calls `reconnectIfNeeded()`
- `android/.../voice/WakeWordDetector.kt` — Check `recordingState` after `startRecording()`; retry if mic busy
- `frontend/src/hooks/useWebSocket.ts` — Re-send `start` on visibility change even when socket is OPEN
- `frontend/src/hooks/useChatInstance.ts` — Comment clarification
- `frontend/src/hooks/useReconnectPoolSessions.ts` — Already had visibility sync (confirmed correct)

### Key root cause discovered during testing
When `ACTION_SCREEN_ON` fired mid-voice-session, `rearmWakeWord()` restarted the silence monitor. But `AudioRecord.startRecording()` silently returned an error (WebRTC held the mic). The monitor looped forever reading 0 bytes. When voice ended and `resumeWakeWord()` was called, `isPaused=false` already → `resume()` was a silent no-op. Fixed with recordingState check + full restart on `resumeWakeWord`.

## Status — All Complete ✅
