package com.assistant.peripheral.connection

/**
 * Typed events emitted by [OrchestratorConnectionController] so other
 * subsystems (chat-state buckets, voice continuity, UI) can react without
 * reaching into the controller's fields.
 *
 * Why typed events instead of direct field-poking: during Increment 2 of the
 * viewmodel refactor (see the plan in `assistant/plans/`), the
 * `OrchestratorConnectionController` is extracted but `ChatController` and
 * `VoiceController` haven't been split out yet. Those increments will subscribe
 * to this `events` flow as their boundary to the connection layer — same flow,
 * different subscribers — instead of the god-class pattern where the
 * Connected-handler block reached into every neighbouring subsystem's state.
 */
sealed class ConnectionEvent {
    /**
     * Cold-start probe found an existing orchestrator on the server. The
     * controller has already adopted [localId] (written to the persisted
     * preference); subscribers should write it into the orchestrator bucket
     * and load the SDK session's messages.
     *
     * This event ALSO drives the voice continuity branch (see [Reconnected]).
     */
    data class OrchestratorAdopted(
        val localId: String,
        val sdkSessionId: String
    ) : ConnectionEvent()

    /**
     * Emitted in the same situation as [OrchestratorAdopted] — distinct only
     * so the voice subsystem can subscribe specifically. If `activeVoiceConfig`
     * is non-null when this fires, the subscriber sends `voice_start` to
     * re-arm voice; otherwise `start` is enough.
     *
     * Today (Inc 2) the ViewModel handles both events in one branch; once
     * VoiceController lands (Inc 4) it subscribes to [Reconnected] directly
     * and the ViewModel stops handling it.
     */
    data class Reconnected(
        val localId: String,
        val sdkSessionId: String
    ) : ConnectionEvent()

    /**
     * Cold-start probe came up empty (after the 400ms retry-once). The UI
     * routes the user to History to pick or explicitly create a session.
     * Subscribers should set `noActiveOrchestrator = true` and refresh the
     * sessions list.
     */
    object NoOrchestratorFound : ConnectionEvent()

    /**
     * `newSession()` armed a fresh-orchestrator start. The Connected handler
     * skipped the probe; subscribers should send a plain `start` with the
     * current bucket's local_id (no resume).
     */
    object NewSessionAdopted : ConnectionEvent()

    /**
     * `recoverFromOrchestratorActive` exhausted its retry budget without
     * converging. The controller has already flipped `noActiveOrchestrator`
     * to true; this event is informational for downstream subscribers
     * (e.g. surfacing a toast).
     */
    object OrchestratorActiveCapHit : ConnectionEvent()
}
