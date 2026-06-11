package com.assistant.peripheral.chat

/**
 * Surfaces a user-initiated orchestrator switch that conflicts with a
 * currently-live orchestrator session in the pool. ChatController emits
 * one of these on `requestLoadOrchestratorSession` / `requestNewOrchestratorSession`
 * when a different orchestrator is already live; the UI shows a dialog and
 * calls `resolveOrchestratorConflict` to pick the resolution.
 *
 * Inc 3.5 of the viewmodel refactor — replaces the recovery state machine's
 * silent "resync to whatever is live" behavior for user-initiated switches.
 * The recovery path remains for cold-start / reconnect (intent unset).
 */
sealed class OrchestratorConflict {
    /** sdk_session_id of the live orchestrator. */
    abstract val liveSdkSessionId: String
    /** local_id of the live orchestrator (the pool key). */
    abstract val liveLocalId: String

    /**
     * The user tapped a non-live orchestrator session in History. Resolving
     * with [OrchestratorConflictResolution.OpenExisting] loads the LIVE
     * orchestrator; with [OrchestratorConflictResolution.DiscardAndProceed]
     * closes the live one and opens [targetSessionId].
     */
    data class OnLoad(
        val targetSessionId: String,
        /** Live local_id for [targetSessionId] if the caller knew one (orchestrators usually don't). */
        val targetLiveLocalId: String?,
        override val liveSdkSessionId: String,
        override val liveLocalId: String,
    ) : OrchestratorConflict()

    /**
     * The user tapped the New Session FAB while a live orchestrator exists.
     * [OrchestratorConflictResolution.OpenExisting] loads the live one;
     * [OrchestratorConflictResolution.DiscardAndProceed] closes it and mints
     * a fresh orchestrator with a new local_id.
     */
    data class OnNew(
        override val liveSdkSessionId: String,
        override val liveLocalId: String,
    ) : OrchestratorConflict()
}

sealed class OrchestratorConflictResolution {
    /** Load the live orchestrator's session into the orchestrator bucket. */
    object OpenExisting : OrchestratorConflictResolution()
    /** Call `closePoolSession(liveLocalId)`, then proceed with the original intent. */
    object DiscardAndProceed : OrchestratorConflictResolution()
    /** Clear the conflict; no further action. */
    object Cancel : OrchestratorConflictResolution()
}
