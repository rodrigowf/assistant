package com.assistant.peripheral.chat

import com.assistant.peripheral.data.ChatMessage
import kotlinx.coroutines.flow.MutableStateFlow
import java.util.UUID

/**
 * Snapshot of the backend's typed ``session_terminated`` event.
 *
 * Distinct from a generic Error — drives a recovery affordance in the
 * UI that opens a fresh session resuming from [sdkSessionId] (the
 * on-disk JSONL is intact).  ``reason`` mirrors
 * ``manager.types.TerminationReason``.
 */
data class TerminationState(
    val reason: String,
    val detail: String?,
    val sdkSessionId: String?,
)

/**
 * Per-endpoint chat state. Two instances live in [ChatController] — one for
 * the orchestrator WS, one for the agent WS — so events from one socket
 * never write into the other tab's UI state. That isolation is the core
 * invariant the WS event router preserves.
 *
 * Moved verbatim from `AssistantViewModel.kt` (HEAD `ca3a5d6`, L78-100)
 * as part of Increment 3 of the viewmodel refactor. The fields are
 * `internal` so the controller can compose them into the public derived
 * flows without exposing them externally.
 */
internal class ChatStateBucket {
    /** Session id reported by the backend (sdk/JSONL id, or local_id on reconnect). */
    val currentSessionId = MutableStateFlow<String?>(null)
    /** True JSONL/SDK id, set from pendingResumeSessionId on SessionStarted. */
    var jsonlSessionId: String? = null
    /**
     * Local id — only meaningful for orchestrator (the pool is keyed by it);
     * for agent the live local_id is generated per loadSession.
     */
    val currentLocalId = MutableStateFlow(UUID.randomUUID().toString())
    /** Conversation displayed for this tab. */
    val messages = MutableStateFlow<List<ChatMessage>>(emptyList())
    /** Pagination state. */
    var currentSessionIdForPagination: String? = null
    var paginationStartIndex: Int = 0
    val hasMoreMessages = MutableStateFlow(false)
    /**
     * Streaming-message scratchpad — owned by this endpoint. Blocks are
     * mutated in arrival order on the message itself; see
     * [ChatController.mutateStreamingBlocks].
     */
    var streamingMessageId: String? = null
    /** Session lifecycle status — drives the small label above the input. */
    val sessionStatus = MutableStateFlow("idle")
    /**
     * SDK/JSONL id stashed by the connect probe so SessionStarted can fetch
     * history for the right id (the backend echoes local_id back as the
     * SessionStarted.sessionId on reconnect, which is not the JSONL key).
     */
    val pendingResumeSessionId = MutableStateFlow<String?>(null)
    /**
     * The original SDK/JSONL id this bucket's session was opened with —
     * preserved across mid-session WS reconnects so the Start message
     * can carry a stable ``resume_sdk_id``.  Distinct from [jsonlSessionId]
     * (which gets overwritten by the backend echoing local_id back on
     * re-subscribe) and from [pendingResumeSessionId] (which is cleared
     * after the first SessionStarted handles it).
     */
    var lastResumeSdkId: String? = null
    /**
     * Set when the backend signals the underlying session is permanently
     * gone (subprocess crashed, SSH transport closed, etc.).  Drives the
     * termination banner with auto-resume affordance.  Cleared when a
     * new session is opened in this bucket.
     */
    val termination = MutableStateFlow<TerminationState?>(null)
}
