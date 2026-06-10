package com.assistant.peripheral.chat

import android.util.Log
import com.assistant.peripheral.connection.ConnectionEvent
import com.assistant.peripheral.connection.OrchestratorConnectionController
import com.assistant.peripheral.data.ChatMessage
import com.assistant.peripheral.data.MessageBlock
import com.assistant.peripheral.data.MessageRole
import com.assistant.peripheral.data.SessionInfo
import com.assistant.peripheral.data.WebSocketEvent
import com.assistant.peripheral.data.WebSocketMessage
import com.assistant.peripheral.network.LiveSession
import com.assistant.peripheral.network.PaginatedMessages
import com.assistant.peripheral.network.WebSocketEndpoint
import com.assistant.peripheral.network.WebSocketManager
import com.assistant.peripheral.settings.SettingsRepository
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.flatMapLatest
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import java.util.UUID

/**
 * Owns chat session state, per-endpoint buckets, the WS event router, the
 * session cache, and all session-history operations. Increment 3 of the
 * viewmodel refactor.
 *
 * Refactor base: HEAD `ca3a5d6` ("Inc 2 — OrchestratorConnectionController").
 * Pinned source ranges from AssistantViewModel.kt:
 *   - L78-100  ChatStateBucket data shape (moved to [ChatStateBucket])
 *   - L102-108 buckets map + accessors
 *   - L111-173 _sessions, _liveSessionIds, _sdkToLocalId, _isOrchestratorSession, mirrorActive, sessionCache
 *   - L178-191 ensureStreamingMessage
 *   - L586-955 handleWebSocketEvent (minus voice forwards which stay in VM until Inc 4)
 *   - L966-976 mutateStreamingBlocks
 *   - L1003-1025 sendMessage / interrupt / compact
 *   - L1031-1110 refreshSessions / closeSession
 *   - L1120-1268 loadSession / openSessionOnEndpoint / saveCurrentSessionToCache / loadMoreMessages
 *   - L1270-1316 newSession / pendingAgentResume / currentEndpoint
 *   - L1318-1467 deleteSession / renameSession / duplicateSession / truncateSession / forkSession / rewindCurrentSessionAt / forkCurrentSessionAt
 *
 * Design notes:
 *
 *  - Function-typed dependencies for HTTP calls (mirroring Inc 2's pattern).
 *    The `apiClient` field on the ViewModel is rebuilt on serverUrlChanged, so
 *    a controller-owned `ApiClient` reference would go stale. Function deps
 *    let the ViewModel always read the current client.
 *
 *  - VoiceController (Inc 4) and the voice-related branches of the WS event
 *    router still run inside the ViewModel for now — the router delegates
 *    chat-mutating branches to this controller and the ViewModel handles
 *    voice forwards (`VoiceCommand`, `VoiceProviderEvent`, `VoiceAudioOut`,
 *    `VoiceEnding`, `VoiceEnded`, `VoiceVadState`) until Inc 4 absorbs them.
 *    See [handleWebSocketEvent] for the exact split.
 *
 *  - Cross-controller signalling: subscribes to
 *    [OrchestratorConnectionController.events] for `OrchestratorAdopted` /
 *    `NoOrchestratorFound` / `NewSessionAdopted`. Voice continuity (the
 *    `Reconnected` event) is the ViewModel's concern during Inc 3; Inc 4
 *    moves it to VoiceController.
 *
 *  - The orchestrator-active error (`WebSocketEvent.Error("orchestrator_active")`)
 *    is routed to [OrchestratorConnectionController.onOrchestratorActiveError]
 *    directly from this controller's WS router — the dispatcher hop in HEAD
 *    AssistantViewModel.kt:876 moves here cleanly.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class ChatController(
    private val scope: CoroutineScope,
    private val webSocketManager: WebSocketManager,
    private val settingsRepository: SettingsRepository,
    private val connectionController: OrchestratorConnectionController,
    private val listSessions: suspend () -> List<SessionInfo>,
    private val getLivePool: suspend () -> List<LiveSession>,
    private val getMessagesPaginated: suspend (
        sessionId: String, limit: Int, beforeIndex: Int?
    ) -> PaginatedMessages?,
    private val closePoolSession: suspend (localId: String) -> Boolean,
    private val deleteSession: suspend (sessionId: String) -> Boolean,
    private val renameSession: suspend (sessionId: String, title: String) -> Boolean,
    private val duplicateSession: suspend (sessionId: String) -> String?,
    private val truncateSession: suspend (sessionId: String, dropLastN: Int) -> Boolean,
    private val forkSession: suspend (sessionId: String, dropLastN: Int) -> String?,
) {

    companion object {
        private const val TAG = "ChatController"
        /** Pinned from HEAD AssistantViewModel.kt:40. */
        const val MAX_CACHED_SESSIONS = 5
        /** Pinned from HEAD AssistantViewModel.kt:41. */
        const val MAX_CACHED_MESSAGES_PER_SESSION = 100
        /** Pinned from HEAD AssistantViewModel.kt:1029. */
        private const val REFRESH_DEBOUNCE_MS = 500L
    }

    // ─────────────────────────────────────────────────────────────────
    // Per-endpoint buckets
    // ─────────────────────────────────────────────────────────────────

    private val buckets: Map<WebSocketEndpoint, ChatStateBucket> = mapOf(
        WebSocketEndpoint.ORCHESTRATOR to ChatStateBucket(),
        WebSocketEndpoint.AGENT to ChatStateBucket()
    )

    private fun bucket(endpoint: WebSocketEndpoint): ChatStateBucket = buckets.getValue(endpoint)

    /** Test/cross-controller accessor — package-internal so VoiceController (Inc 4) can read voice transcripts target. */
    internal fun bucketFor(endpoint: WebSocketEndpoint): ChatStateBucket = bucket(endpoint)

    private fun activeBucket(): ChatStateBucket = bucket(currentEndpoint())

    // ─────────────────────────────────────────────────────────────────
    // Session list + live pool
    // ─────────────────────────────────────────────────────────────────

    private val _sessions = MutableStateFlow<List<SessionInfo>>(emptyList())
    val sessions: StateFlow<List<SessionInfo>> = _sessions.asStateFlow()

    private val _sessionsLoading = MutableStateFlow(false)
    val sessionsLoading: StateFlow<Boolean> = _sessionsLoading.asStateFlow()

    private val _liveSessionIds = MutableStateFlow<Set<String>>(emptySet())
    val liveSessionIds: StateFlow<Set<String>> = _liveSessionIds.asStateFlow()

    private val _sdkToLocalId = MutableStateFlow<Map<String, String>>(emptyMap())

    private val _isOrchestratorSession = MutableStateFlow(false)
    val isOrchestratorSession: StateFlow<Boolean> = _isOrchestratorSession.asStateFlow()

    private val _isLoadingMoreMessages = MutableStateFlow(false)
    val isLoadingMoreMessages: StateFlow<Boolean> = _isLoadingMoreMessages.asStateFlow()

    // ─────────────────────────────────────────────────────────────────
    // Derived flows mirroring whichever bucket is currently active
    // ─────────────────────────────────────────────────────────────────

    private fun <T> mirrorActive(initial: T, pick: (ChatStateBucket) -> StateFlow<T>): StateFlow<T> =
        _isOrchestratorSession
            .flatMapLatest { isOrch ->
                pick(bucket(if (isOrch) WebSocketEndpoint.ORCHESTRATOR else WebSocketEndpoint.AGENT))
            }
            .stateIn(scope, SharingStarted.Eagerly, initial)

    val currentSessionId: StateFlow<String?> =
        mirrorActive(null) { it.currentSessionId }

    val currentLocalId: StateFlow<String> =
        mirrorActive(bucket(WebSocketEndpoint.ORCHESTRATOR).currentLocalId.value) { it.currentLocalId }

    val messages: StateFlow<List<ChatMessage>> =
        mirrorActive(emptyList()) { it.messages }

    val hasMoreMessages: StateFlow<Boolean> =
        mirrorActive(false) { it.hasMoreMessages }

    val sessionStatus: StateFlow<String> =
        mirrorActive("idle") { it.sessionStatus }

    // ─────────────────────────────────────────────────────────────────
    // Session cache
    // ─────────────────────────────────────────────────────────────────

    private data class CachedSession(
        val messages: List<ChatMessage>,
        val isOrchestrator: Boolean,
        val paginationStartIndex: Int,
        val hasMoreMessages: Boolean
    )

    private val sessionCache = LinkedHashMap<String, CachedSession>(MAX_CACHED_SESSIONS, 0.75f, true)

    // ─────────────────────────────────────────────────────────────────
    // Pending-resume state for the AGENT-endpoint open path
    // ─────────────────────────────────────────────────────────────────

    internal data class PendingAgentResume(val localId: String, val resumeSdkId: String)
    private var pendingAgentResume: PendingAgentResume? = null
    internal val pendingAgentResumeForTest: PendingAgentResume? get() = pendingAgentResume

    // ─────────────────────────────────────────────────────────────────
    // Inc 3.5 — orchestrator session conflict mediation
    // ─────────────────────────────────────────────────────────────────

    private val _orchestratorConflict = MutableStateFlow<OrchestratorConflict?>(null)
    /**
     * Non-null when a user-initiated orchestrator switch (`requestLoadOrchestratorSession`
     * / `requestNewOrchestratorSession`) needs the user to decide between the
     * live orchestrator and their request. UI observes this and shows a
     * dialog; resolve via [resolveOrchestratorConflict].
     */
    val orchestratorConflict: StateFlow<OrchestratorConflict?> = _orchestratorConflict.asStateFlow()

    private val _orchestratorOpenedToChat = MutableSharedFlow<Unit>(extraBufferCapacity = 4)
    /**
     * One-shot signal emitted when an orchestrator session was actually opened
     * (the user's intent went through — directly or after resolving a
     * conflict). MainActivity observes this and navigates to Chat; we
     * deliberately do NOT navigate up-front because that would show the
     * existing live orchestrator's chat behind the conflict dialog, which
     * confuses the user (the dialog should overlay where they tapped, i.e.
     * History).
     */
    val orchestratorOpenedToChat: SharedFlow<Unit> = _orchestratorOpenedToChat.asSharedFlow()

    /**
     * Captures the local_id the user *intended* to operate on. Set when a
     * `request*` entry point proceeds past the probe; cleared on the matching
     * orchestrator `session_started`. The WS-error router consults this when
     * `orchestrator_active` arrives: with intent set we surface a conflict
     * (mid-tap race), without intent we let the existing recovery path fire
     * (cold-start / reconnect).
     */
    private val intendedOrchestratorLocalId =
        java.util.concurrent.atomic.AtomicReference<String?>(null)

    /** The original tap that prompted a conflict. Used by [resolveOrchestratorConflict]. */
    private var pendingConflictAction: PendingConflictAction? = null

    private sealed class PendingConflictAction {
        data class Load(val sessionId: String, val liveLocalId: String?, val onNeedsConnect: () -> Unit) :
            PendingConflictAction()
        data class New(val onNeedsConnect: () -> Unit) : PendingConflictAction()
    }

    // ─────────────────────────────────────────────────────────────────
    // Toast / refresh debounce
    // ─────────────────────────────────────────────────────────────────

    private val _toastMessage = MutableSharedFlow<String>(extraBufferCapacity = 8)
    /** Cross-controller toast channel — VoiceController will produce too (Inc 4). */
    val toastMessages: SharedFlow<String> = _toastMessage.asSharedFlow()

    private var lastRefreshTime = 0L

    private var eventsJob: kotlinx.coroutines.Job? = null

    init {
        eventsJob = scope.launch {
            connectionController.events.collect { ev -> handleConnectionEvent(ev) }
        }
    }

    /**
     * Cancel the events subscription. Tests use this in `@After` cleanup
     * because the `collect` runs forever and `runTest` flags uncompleted
     * children. Production callers don't need this — the ViewModel's scope
     * cancellation tears everything down via `onCleared`.
     */
    internal fun cancelForTest() {
        eventsJob?.cancel()
    }

    // ─────────────────────────────────────────────────────────────────
    // ConnectionEvent subscription — orchestrator bucket coordination
    // ─────────────────────────────────────────────────────────────────

    /** Internal so tests can drive this directly without spinning up the controller's event flow. */
    internal fun handleConnectionEvent(ev: ConnectionEvent) {
        val orchBucket = bucket(WebSocketEndpoint.ORCHESTRATOR)
        when (ev) {
            is ConnectionEvent.OrchestratorAdopted -> {
                orchBucket.currentLocalId.value = ev.localId
                orchBucket.pendingResumeSessionId.value = ev.sdkSessionId
                _isOrchestratorSession.value = true
            }
            is ConnectionEvent.NoOrchestratorFound -> {
                orchBucket.pendingResumeSessionId.value = null
                refreshSessions()
            }
            is ConnectionEvent.NewSessionAdopted -> {
                _isOrchestratorSession.value = true
                scope.launch { settingsRepository.persistOrchestratorLocalId(orchBucket.currentLocalId.value) }
                webSocketManager.send(
                    WebSocketMessage.Start(localId = orchBucket.currentLocalId.value),
                    endpoint = WebSocketEndpoint.ORCHESTRATOR
                )
            }
            is ConnectionEvent.Reconnected,
            is ConnectionEvent.OrchestratorActiveCapHit -> {
                // Reconnected: voice continuity branch lives in the ViewModel
                // until Inc 4 (VoiceController) absorbs it.
                // OrchestratorActiveCapHit: informational — the controller has
                // already flipped noActiveOrchestrator true, routing the UI
                // to History.
            }
        }
    }

    // ─────────────────────────────────────────────────────────────────
    // WS event router
    // ─────────────────────────────────────────────────────────────────

    /**
     * Internal so the ViewModel can keep collecting WS events at the top level
     * (and route voice-bound branches into the voice subsystem) until Inc 4.
     * After Inc 4 the collector itself moves into ChatController.
     *
     * Returns true if the event was fully handled here; false if the
     * ViewModel still needs to do voice-specific routing on top (the VM
     * checks the event type and forwards to VoiceManager — see
     * AssistantViewModel.handleWebSocketEvent voice branches).
     */
    fun handleWebSocketEvent(endpoint: WebSocketEndpoint, event: WebSocketEvent) {
        val b = bucket(endpoint)
        when (event) {
            is WebSocketEvent.Connected -> {
                if (endpoint == WebSocketEndpoint.AGENT) {
                    pendingAgentResume?.let { pending ->
                        pendingAgentResume = null
                        webSocketManager.send(
                            WebSocketMessage.Start(
                                localId = pending.localId,
                                resumeSdkId = pending.resumeSdkId
                            ),
                            endpoint = WebSocketEndpoint.AGENT
                        )
                    }
                    return
                }
                // Orchestrator: delegate to the controller. The probe runs
                // there; this controller handles the emitted events via the
                // ConnectionEvent subscription in init.
                connectionController.onWsConnected()
            }

            is WebSocketEvent.SessionStarted -> {
                b.currentSessionId.value = event.sessionId
                b.sessionStatus.value = "idle"
                if (endpoint == WebSocketEndpoint.ORCHESTRATOR) {
                    connectionController.onSessionStartedForOrchestrator()
                    // Inc 3.5: user-initiated switch converged — clear the
                    // intent flag so a later cold-start / reconnect
                    // `orchestrator_active` falls through to the recovery
                    // path instead of opening a stale conflict dialog.
                    intendedOrchestratorLocalId.set(null)
                }

                // Track the true JSONL session ID for voice resume. On
                // reconnect the backend returns local_id as session_id — use
                // pendingResumeSessionId (the actual SDK/JSONL id) instead.
                b.jsonlSessionId = b.pendingResumeSessionId.value ?: event.sessionId

                // Load/refresh messages when reconnecting to an existing session.
                // Always re-fetch from server so any messages that arrived while
                // the WebSocket was disconnected are not lost.
                val resumeId = b.pendingResumeSessionId.value
                if (resumeId != null) {
                    scope.launch {
                        try {
                            val paginated = getMessagesPaginated(resumeId, 50, null)
                                ?: PaginatedMessages(emptyList(), 0, false, 0)
                            b.currentSessionIdForPagination = resumeId
                            b.paginationStartIndex = paginated.startIndex
                            b.hasMoreMessages.value = paginated.hasMore
                            b.messages.value = paginated.messages
                        } catch (_: Exception) {
                            // Best-effort — keep existing messages on fetch failure.
                        }
                    }
                }
                b.pendingResumeSessionId.value = null
                refreshSessions()
            }

            is WebSocketEvent.SessionStopped -> {
                b.sessionStatus.value = "disconnected"
            }

            is WebSocketEvent.Status -> {
                b.sessionStatus.value = event.status
            }

            is WebSocketEvent.Disconnected -> {
                b.streamingMessageId = null
                b.sessionStatus.value = "disconnected"
            }

            is WebSocketEvent.MessageStart -> {
                b.streamingMessageId = event.messageId
                val newMessage = ChatMessage(
                    id = event.messageId,
                    role = MessageRole.ASSISTANT,
                    content = "",
                    blocks = emptyList(),
                    isStreaming = true
                )
                b.messages.update { it + newMessage }
                b.sessionStatus.value = "streaming"
            }

            is WebSocketEvent.TextDelta -> {
                ensureStreamingMessage(b)
                mutateStreamingBlocks(b) { blocks ->
                    val last = blocks.lastOrNull()
                    if (last is MessageBlock.Text && last.isStreaming) {
                        blocks.dropLast(1) + last.copy(text = last.text + event.text)
                    } else {
                        blocks + MessageBlock.Text(event.text, isStreaming = true)
                    }
                }
            }

            is WebSocketEvent.TextComplete -> {
                ensureStreamingMessage(b)
                mutateStreamingBlocks(b) { blocks ->
                    val last = blocks.lastOrNull()
                    if (last is MessageBlock.Text && last.isStreaming) {
                        blocks.dropLast(1) + MessageBlock.Text(event.text, isStreaming = false)
                    } else {
                        blocks + MessageBlock.Text(event.text, isStreaming = false)
                    }
                }
            }

            is WebSocketEvent.ThinkingDelta -> {
                ensureStreamingMessage(b)
                mutateStreamingBlocks(b) { blocks ->
                    val last = blocks.lastOrNull()
                    if (last is MessageBlock.Thinking && last.isStreaming) {
                        blocks.dropLast(1) + last.copy(text = last.text + event.text)
                    } else {
                        blocks + MessageBlock.Thinking(event.text, isStreaming = true)
                    }
                }
            }

            is WebSocketEvent.ThinkingComplete -> {
                ensureStreamingMessage(b)
                mutateStreamingBlocks(b) { blocks ->
                    val last = blocks.lastOrNull()
                    if (last is MessageBlock.Thinking && last.isStreaming) {
                        blocks.dropLast(1) + MessageBlock.Thinking(event.text, isStreaming = false)
                    } else {
                        blocks + MessageBlock.Thinking(event.text, isStreaming = false)
                    }
                }
            }

            is WebSocketEvent.ToolUse -> {
                ensureStreamingMessage(b)
                mutateStreamingBlocks(b) { blocks ->
                    val finalized = when (val last = blocks.lastOrNull()) {
                        is MessageBlock.Text -> if (last.isStreaming)
                            blocks.dropLast(1) + last.copy(isStreaming = false) else blocks
                        is MessageBlock.Thinking -> if (last.isStreaming)
                            blocks.dropLast(1) + last.copy(isStreaming = false) else blocks
                        else -> blocks
                    }
                    finalized + MessageBlock.ToolUse(
                        toolUseId = event.toolUseId,
                        toolName = event.toolName,
                        toolInput = event.toolInput,
                        isExecuting = false,
                        isComplete = false
                    )
                }
                b.sessionStatus.value = "tool_use"
            }

            is WebSocketEvent.ToolExecuting -> {
                mutateStreamingBlocks(b) { blocks ->
                    blocks.map { block ->
                        if (block is MessageBlock.ToolUse && block.toolUseId == event.toolUseId) {
                            block.copy(isExecuting = true)
                        } else block
                    }
                }
            }

            is WebSocketEvent.ToolResult -> {
                mutateStreamingBlocks(b) { blocks ->
                    blocks.map { block ->
                        if (block is MessageBlock.ToolUse && block.toolUseId == event.toolUseId) {
                            block.copy(
                                result = event.output,
                                isError = event.isError,
                                isExecuting = false,
                                isComplete = true
                            )
                        } else block
                    }
                }
            }

            is WebSocketEvent.MessageEnd, is WebSocketEvent.TurnComplete -> {
                b.streamingMessageId?.let { messageId ->
                    b.messages.update { messages ->
                        messages.map { msg ->
                            if (msg.id == messageId) {
                                msg.copy(
                                    isStreaming = false,
                                    blocks = msg.blocks.map { block ->
                                        when (block) {
                                            is MessageBlock.Text -> block.copy(isStreaming = false)
                                            is MessageBlock.Thinking -> block.copy(isStreaming = false)
                                            else -> block
                                        }
                                    }
                                )
                            } else msg
                        }
                    }
                }
                b.streamingMessageId = null
                b.sessionStatus.value = "idle"
                if (endpoint == currentEndpoint()) {
                    saveCurrentSessionToCache()
                }
            }

            is WebSocketEvent.CompactComplete -> {
                val compactMessage = ChatMessage(
                    role = MessageRole.SYSTEM,
                    content = "",
                    blocks = listOf(MessageBlock.Compact(event.summary))
                )
                b.messages.update { it + compactMessage }
            }

            is WebSocketEvent.VoiceError -> {
                // Typed voice-provider error. Render a categorised system
                // message in the orchestrator bucket. The legacy `Error`
                // event arrives behind this one (back-compat).
                val hintLine = event.recoveryHint?.let { "\n$it" } ?: ""
                val docLine = event.providerDocUrl?.let { "\n$it" } ?: ""
                val errorMessage = ChatMessage(
                    role = MessageRole.SYSTEM,
                    content = "Voice error (${event.category}): ${event.message}$hintLine$docLine"
                )
                b.messages.update { it + errorMessage }
                if (!event.recoverable) {
                    b.sessionStatus.value = "error"
                }
            }

            is WebSocketEvent.Error -> {
                val errorMessage = ChatMessage(
                    role = MessageRole.SYSTEM,
                    content = "Error: ${event.message}${event.detail?.let { "\n$it" } ?: ""}"
                )
                b.messages.update { it + errorMessage }
                b.sessionStatus.value = "error"
                if (endpoint == WebSocketEndpoint.ORCHESTRATOR && event.message == "orchestrator_active") {
                    // Inc 3.5: with a user-initiated intent in flight, route to
                    // the conflict mediator instead of silently resyncing via
                    // recovery. The mediator probes the live pool and emits an
                    // OnLoad conflict; if no intent is set, fall through to
                    // the legacy recovery path (cold-start / reconnect).
                    scope.launch {
                        val routed = maybeRouteOrchestratorActiveToConflict()
                        if (!routed) connectionController.onOrchestratorActiveError()
                    }
                }
            }

            // Voice-bound events — stay in ViewModel during Inc 3. The
            // ViewModel checks the event type before delegating to this
            // controller. We list them here as no-ops so the `when` stays
            // exhaustive for compile-time safety.
            is WebSocketEvent.VoiceCommand,
            is WebSocketEvent.VoiceProviderEvent,
            is WebSocketEvent.VoiceAudioOut,
            is WebSocketEvent.VoiceEnding,
            is WebSocketEvent.VoiceEnded,
            is WebSocketEvent.VoiceStopped,
            is WebSocketEvent.VoiceTranscript,
            is WebSocketEvent.VoiceVadState -> {
                // Forwarded by the ViewModel — see AssistantViewModel.handleWebSocketEvent.
            }

            // Backend doesn't currently emit these on the WS path; kept
            // exhaustive.
            is WebSocketEvent.SessionList,
            is WebSocketEvent.HistoryLoaded,
            is WebSocketEvent.ToolProgress -> {}
        }
    }

    private fun ensureStreamingMessage(b: ChatStateBucket) {
        if (b.streamingMessageId == null) {
            val newId = UUID.randomUUID().toString()
            b.streamingMessageId = newId
            val newMessage = ChatMessage(
                id = newId,
                role = MessageRole.ASSISTANT,
                content = "",
                blocks = emptyList(),
                isStreaming = true
            )
            b.messages.update { it + newMessage }
        }
    }

    /**
     * Mutate the in-flight assistant message's block list in place, preserving
     * arrival order. Mirrors the web frontend's reducer in `useChatInstance.ts`:
     * each new text delta either extends the trailing streaming text block or
     * starts a new one after whatever tool blocks were emitted since the last
     * text. This is the source of truth for ordering — never rebuild blocks
     * from per-type scratchpad buffers, which loses the order between text
     * and tool calls.
     */
    private fun mutateStreamingBlocks(
        b: ChatStateBucket,
        transform: (List<MessageBlock>) -> List<MessageBlock>
    ) {
        val messageId = b.streamingMessageId ?: return
        b.messages.update { messages ->
            messages.map { msg ->
                if (msg.id == messageId) msg.copy(blocks = transform(msg.blocks)) else msg
            }
        }
    }

    // ─────────────────────────────────────────────────────────────────
    // Public ops — send / interrupt / compact / refresh / close / load
    // ─────────────────────────────────────────────────────────────────

    fun sendMessage(text: String) {
        if (text.isBlank()) return
        val userMessage = ChatMessage(
            role = MessageRole.USER,
            content = text,
            blocks = listOf(MessageBlock.Text(text))
        )
        activeBucket().messages.update { it + userMessage }
        webSocketManager.send(WebSocketMessage.Send(text), endpoint = currentEndpoint())
    }

    fun interrupt() {
        webSocketManager.send(WebSocketMessage.Interrupt, endpoint = currentEndpoint())
        activeBucket().sessionStatus.value = "interrupted"
    }

    fun compact() {
        webSocketManager.send(WebSocketMessage.Compact, endpoint = currentEndpoint())
    }

    fun refreshSessions() {
        val now = System.currentTimeMillis()
        if (now - lastRefreshTime < REFRESH_DEBOUNCE_MS) return
        lastRefreshTime = now

        scope.launch {
            _sessionsLoading.value = true
            val sessionList = listSessions()
            val livePool = getLivePool()
            _liveSessionIds.value = livePool.map { it.sdkSessionId }.toSet()
            _sdkToLocalId.value = livePool.associate { it.sdkSessionId to it.localId }
            _sessions.value = sessionList.sortedByDescending { it.lastActivity }
            _sessionsLoading.value = false
        }
    }

    fun closeSession(sessionId: String) {
        scope.launch {
            var localId = _sdkToLocalId.value[sessionId]
            if (localId == null) {
                val livePool = getLivePool()
                _sdkToLocalId.value = livePool.associate { it.sdkSessionId to it.localId }
                localId = _sdkToLocalId.value[sessionId]
            }
            if (localId == null) {
                Log.w(TAG, "closeSession: no live local_id for $sessionId — already closed?")
                return@launch
            }

            val ok = closePoolSession(localId)
            if (!ok) {
                Log.w(TAG, "closeSession: backend rejected close for $localId")
                return@launch
            }

            _liveSessionIds.update { it - sessionId }
            _sdkToLocalId.update { it - sessionId }

            for ((ep, b) in buckets) {
                if (b.currentSessionIdForPagination == sessionId || b.currentLocalId.value == localId) {
                    b.messages.value = emptyList()
                    b.currentSessionId.value = null
                    b.currentSessionIdForPagination = null
                    b.hasMoreMessages.value = false
                    b.currentLocalId.value = UUID.randomUUID().toString()
                    if (ep == WebSocketEndpoint.ORCHESTRATOR) {
                        settingsRepository.clearOrchestratorLocalId()
                        _isOrchestratorSession.value = false
                    }
                }
            }
            sessionCache.remove(sessionId)

            refreshSessions()
        }
    }

    fun loadSession(
        sessionId: String,
        isOrchestrator: Boolean = false,
        liveLocalId: String? = null
    ) {
        scope.launch {
            saveCurrentSessionToCache()

            val endpoint = if (isOrchestrator) WebSocketEndpoint.ORCHESTRATOR else WebSocketEndpoint.AGENT
            val b = bucket(endpoint)
            val localIdForStart = liveLocalId ?: UUID.randomUUID().toString()

            val cached = sessionCache[sessionId]
            if (cached != null) {
                b.currentSessionIdForPagination = sessionId
                b.paginationStartIndex = cached.paginationStartIndex
                b.hasMoreMessages.value = cached.hasMoreMessages
                b.messages.value = cached.messages
                b.currentLocalId.value = localIdForStart
                _isOrchestratorSession.value = cached.isOrchestrator
                if (isOrchestrator) connectionController.setNoActiveOrchestrator(false)

                openSessionOnEndpoint(endpoint, localIdForStart, sessionId)
                return@launch
            }

            val paginated = getMessagesPaginated(sessionId, 50, null)
                ?: PaginatedMessages(emptyList(), 0, false, 0)

            if (paginated.totalCount > 0 || paginated.messages.isNotEmpty()) {
                b.currentSessionIdForPagination = sessionId
                b.paginationStartIndex = paginated.startIndex
                b.hasMoreMessages.value = paginated.hasMore
                b.messages.value = paginated.messages
                b.currentLocalId.value = localIdForStart
                _isOrchestratorSession.value = isOrchestrator
                if (isOrchestrator) connectionController.setNoActiveOrchestrator(false)

                openSessionOnEndpoint(endpoint, localIdForStart, sessionId)
            }
        }
    }

    /**
     * Connect (if needed) the given endpoint and Start the session on it.
     * Crucially, this does NOT touch the *other* endpoint's socket: opening a
     * Claude Code (agent) session must not tear down the orchestrator socket,
     * which may be running an active realtime voice conversation.
     */
    private suspend fun openSessionOnEndpoint(
        endpoint: WebSocketEndpoint,
        localId: String,
        resumeSdkId: String
    ) {
        if (webSocketManager.isConnected(endpoint)) {
            webSocketManager.send(
                WebSocketMessage.Start(localId = localId, resumeSdkId = resumeSdkId),
                endpoint = endpoint
            )
            return
        }
        when (endpoint) {
            WebSocketEndpoint.AGENT -> {
                pendingAgentResume = PendingAgentResume(localId, resumeSdkId)
            }
            WebSocketEndpoint.ORCHESTRATOR -> {
                bucket(WebSocketEndpoint.ORCHESTRATOR).pendingResumeSessionId.value = resumeSdkId
            }
        }
        // Use awaitLoaded() — reading `_settings.value` would race the first
        // emission and route the WS to the default server URL.
        webSocketManager.connect(settingsRepository.awaitLoaded().serverUrl, localId, endpoint)
    }

    private fun saveCurrentSessionToCache() {
        val b = activeBucket()
        val sessionId = b.currentSessionIdForPagination ?: return
        if (b.messages.value.isEmpty()) return

        val messagesToCache = if (b.messages.value.size > MAX_CACHED_MESSAGES_PER_SESSION) {
            b.messages.value.takeLast(MAX_CACHED_MESSAGES_PER_SESSION)
        } else {
            b.messages.value
        }

        sessionCache[sessionId] = CachedSession(
            messages = messagesToCache,
            isOrchestrator = _isOrchestratorSession.value,
            paginationStartIndex = b.paginationStartIndex,
            hasMoreMessages = b.hasMoreMessages.value || b.messages.value.size > MAX_CACHED_MESSAGES_PER_SESSION
        )

        while (sessionCache.size > MAX_CACHED_SESSIONS) {
            val oldestKey = sessionCache.keys.firstOrNull() ?: break
            sessionCache.remove(oldestKey)
        }
    }

    fun loadMoreMessages() {
        val b = activeBucket()
        if (_isLoadingMoreMessages.value || !b.hasMoreMessages.value) return
        val sessionId = b.currentSessionIdForPagination ?: return

        scope.launch {
            _isLoadingMoreMessages.value = true
            try {
                val paginated = getMessagesPaginated(sessionId, 50, b.paginationStartIndex)
                    ?: return@launch
                if (paginated.messages.isNotEmpty()) {
                    b.messages.update { paginated.messages + it }
                    b.paginationStartIndex = paginated.startIndex
                    b.hasMoreMessages.value = paginated.hasMore
                }
            } finally {
                _isLoadingMoreMessages.value = false
            }
        }
    }

    /**
     * Fresh orchestrator session. Caller is responsible for invoking
     * `connectionController.armNewSessionStart()` + the ViewModel's `connect()`
     * when the WS isn't connected; this controller handles the local bucket
     * + connected-but-need-Start branch.
     */
    fun newSession(onNeedsConnect: () -> Unit) {
        val b = bucket(WebSocketEndpoint.ORCHESTRATOR)

        saveCurrentSessionToCache()

        b.currentLocalId.value = UUID.randomUUID().toString()
        b.messages.value = emptyList()
        b.currentSessionIdForPagination = null
        b.paginationStartIndex = 0
        b.hasMoreMessages.value = false
        connectionController.setNoActiveOrchestrator(false)

        scope.launch { settingsRepository.persistOrchestratorLocalId(b.currentLocalId.value) }

        _isOrchestratorSession.value = true

        if (webSocketManager.isConnected(WebSocketEndpoint.ORCHESTRATOR)) {
            webSocketManager.send(WebSocketMessage.Stop, endpoint = WebSocketEndpoint.ORCHESTRATOR)
            webSocketManager.send(
                WebSocketMessage.Start(localId = b.currentLocalId.value),
                endpoint = WebSocketEndpoint.ORCHESTRATOR
            )
        } else {
            connectionController.armNewSessionStart()
            onNeedsConnect()
        }
    }

    /** Pick the WebSocket endpoint that owns the currently-displayed session. */
    private fun currentEndpoint(): WebSocketEndpoint =
        if (_isOrchestratorSession.value) WebSocketEndpoint.ORCHESTRATOR else WebSocketEndpoint.AGENT

    // ─────────────────────────────────────────────────────────────────
    // Inc 3.5 — conflict-mediated orchestrator entry points
    // ─────────────────────────────────────────────────────────────────

    /**
     * User intent: open an orchestrator session from History. Probes the live
     * pool first, then either proceeds directly (same orch / no live orch) or
     * emits an [OrchestratorConflict.OnLoad] for the UI to resolve.
     */
    fun requestLoadOrchestratorSession(
        sessionId: String,
        liveLocalId: String?,
        onNeedsConnect: () -> Unit
    ) {
        scope.launch {
            val live = getLivePool().firstOrNull { it.isOrchestrator }
            when {
                live == null -> {
                    // No live orchestrator — proceed directly with the user's intent.
                    intendedOrchestratorLocalId.set(liveLocalId)
                    loadSession(sessionId, isOrchestrator = true, liveLocalId = liveLocalId)
                    _orchestratorOpenedToChat.tryEmit(Unit)
                }
                live.sdkSessionId == sessionId -> {
                    // Tapped the same orchestrator that's already live — proceed.
                    intendedOrchestratorLocalId.set(live.localId)
                    loadSession(sessionId, isOrchestrator = true, liveLocalId = live.localId)
                    _orchestratorOpenedToChat.tryEmit(Unit)
                }
                else -> {
                    pendingConflictAction = PendingConflictAction.Load(
                        sessionId = sessionId,
                        liveLocalId = liveLocalId,
                        onNeedsConnect = onNeedsConnect,
                    )
                    _orchestratorConflict.value = OrchestratorConflict.OnLoad(
                        targetSessionId = sessionId,
                        targetLiveLocalId = liveLocalId,
                        liveSdkSessionId = live.sdkSessionId,
                        liveLocalId = live.localId,
                    )
                    // No navigation — dialog must overlay History (where the
                    // user tapped). MainActivity navigates only after the
                    // user resolves the conflict to a proceed action.
                }
            }
        }
    }

    /**
     * User intent: create a fresh orchestrator session. Probes the live pool
     * first, then either proceeds directly or emits an
     * [OrchestratorConflict.OnNew] for the UI to resolve.
     */
    fun requestNewOrchestratorSession(onNeedsConnect: () -> Unit) {
        scope.launch {
            val live = getLivePool().firstOrNull { it.isOrchestrator }
            if (live == null) {
                intendedOrchestratorLocalId.set(null) // newSession mints its own
                newSession(onNeedsConnect)
                _orchestratorOpenedToChat.tryEmit(Unit)
            } else {
                pendingConflictAction = PendingConflictAction.New(onNeedsConnect)
                _orchestratorConflict.value = OrchestratorConflict.OnNew(
                    liveSdkSessionId = live.sdkSessionId,
                    liveLocalId = live.localId,
                )
                // No navigation — dialog must overlay where the user tapped.
            }
        }
    }

    /**
     * Resolve a pending [orchestratorConflict]. The exact semantics depend on
     * which conflict variant was emitted and the chosen resolution; see
     * [OrchestratorConflictResolution] for the contract.
     */
    fun resolveOrchestratorConflict(decision: OrchestratorConflictResolution) {
        val conflict = _orchestratorConflict.value ?: return
        val action = pendingConflictAction
        // Clear up front so subsequent ops don't see stale state.
        _orchestratorConflict.value = null
        pendingConflictAction = null

        when (decision) {
            OrchestratorConflictResolution.Cancel -> {
                // No-op beyond clearing the conflict + action.
                Log.d(TAG, "Orchestrator conflict cancelled by user")
            }
            OrchestratorConflictResolution.OpenExisting -> {
                // Load the LIVE orchestrator's history into the orchestrator
                // bucket — irrespective of which variant of conflict fired.
                intendedOrchestratorLocalId.set(conflict.liveLocalId)
                loadSession(
                    sessionId = conflict.liveSdkSessionId,
                    isOrchestrator = true,
                    liveLocalId = conflict.liveLocalId,
                )
                _orchestratorOpenedToChat.tryEmit(Unit)
            }
            OrchestratorConflictResolution.DiscardAndProceed -> {
                scope.launch {
                    val ok = closePoolSession(conflict.liveLocalId)
                    if (!ok) {
                        Log.w(TAG, "Discard-and-proceed: closePoolSession rejected ${conflict.liveLocalId}")
                        _toastMessage.tryEmit("Couldn't close the active session.")
                        return@launch
                    }
                    // Mirror the optimistic UI update closeSession does so a
                    // stale entry doesn't linger in the live-id map.
                    _liveSessionIds.update { it - conflict.liveSdkSessionId }
                    _sdkToLocalId.update { it - conflict.liveSdkSessionId }
                    when (action) {
                        is PendingConflictAction.Load -> {
                            intendedOrchestratorLocalId.set(action.liveLocalId)
                            loadSession(
                                sessionId = action.sessionId,
                                isOrchestrator = true,
                                liveLocalId = action.liveLocalId,
                            )
                            _orchestratorOpenedToChat.tryEmit(Unit)
                        }
                        is PendingConflictAction.New -> {
                            intendedOrchestratorLocalId.set(null)
                            newSession(action.onNeedsConnect)
                            _orchestratorOpenedToChat.tryEmit(Unit)
                        }
                        null -> {
                            Log.w(TAG, "Discard-and-proceed: pendingConflictAction was null")
                        }
                    }
                }
            }
        }
    }

    /**
     * Called by the WS-error router when `orchestrator_active` arrives. With a
     * user intent in flight, surface a conflict (mid-tap race). Without it,
     * fall through to the existing recovery path.
     *
     * Returns true if the conflict path consumed the event (caller should NOT
     * call recovery), false if the caller should proceed with recovery.
     */
    private suspend fun maybeRouteOrchestratorActiveToConflict(): Boolean {
        if (intendedOrchestratorLocalId.get() == null) return false
        val live = getLivePool().firstOrNull { it.isOrchestrator } ?: return false
        // Build a conflict from the live orch + whatever the user originally
        // intended. We don't always have the target sessionId at this point
        // (recovery fires from within loadSession's openSessionOnEndpoint),
        // so default to OnLoad with a synthetic empty target — the dialog
        // text reads the live side anyway.
        val intendedLocal = intendedOrchestratorLocalId.get()
        _orchestratorConflict.value = OrchestratorConflict.OnLoad(
            targetSessionId = bucket(WebSocketEndpoint.ORCHESTRATOR).currentSessionIdForPagination
                ?: bucket(WebSocketEndpoint.ORCHESTRATOR).currentSessionId.value
                ?: "",
            targetLiveLocalId = intendedLocal,
            liveSdkSessionId = live.sdkSessionId,
            liveLocalId = live.localId,
        )
        return true
    }

    // ─────────────────────────────────────────────────────────────────
    // History operations
    // ─────────────────────────────────────────────────────────────────

    fun deleteSessionById(sessionId: String) {
        scope.launch {
            val success = deleteSession(sessionId)
            if (success) {
                _sessions.update { it.filter { s -> s.sessionId != sessionId } }
                sessionCache.remove(sessionId)
            } else {
                Log.w(TAG, "deleteSession: backend rejected $sessionId")
                _toastMessage.tryEmit("Delete failed.")
            }
        }
    }

    fun renameSessionById(sessionId: String, title: String) {
        scope.launch {
            val success = renameSession(sessionId, title)
            if (success) {
                _sessions.update { sessions ->
                    sessions.map { s ->
                        if (s.sessionId == sessionId) s.copy(title = title) else s
                    }
                }
            }
        }
    }

    fun duplicateSessionById(sessionId: String) {
        scope.launch {
            val newId = duplicateSession(sessionId)
            if (newId != null) {
                lastRefreshTime = 0L
                refreshSessions()
                _toastMessage.tryEmit("Conversation duplicated.")
            } else {
                Log.w(TAG, "duplicateSession: backend rejected $sessionId")
                _toastMessage.tryEmit("Duplicate failed.")
            }
        }
    }

    fun truncateSessionById(
        sessionId: String,
        dropLastN: Int,
        explicitLocalId: String? = null,
    ) {
        scope.launch {
            val localId = explicitLocalId ?: _sdkToLocalId.value[sessionId]
            if (localId != null) {
                closePoolSession(localId)
                _liveSessionIds.update { it - sessionId }
                _sdkToLocalId.update { it - sessionId }
            }

            val ok = truncateSession(sessionId, dropLastN)
            if (!ok) {
                Log.w(TAG, "truncateSession: backend rejected $sessionId drop=$dropLastN")
                _toastMessage.tryEmit("Rewind failed — session may still be open.")
                return@launch
            }

            for ((_, b) in buckets) {
                if (b.currentSessionIdForPagination == sessionId) {
                    b.messages.value = emptyList()
                    b.currentSessionId.value = null
                    b.currentSessionIdForPagination = null
                    b.hasMoreMessages.value = false
                    b.jsonlSessionId = null
                }
            }
            sessionCache.remove(sessionId)

            _toastMessage.tryEmit("Conversation rewound.")
            lastRefreshTime = 0L
            refreshSessions()
        }
    }

    fun forkSessionById(sessionId: String, dropLastN: Int) {
        scope.launch {
            val newId = forkSession(sessionId, dropLastN)
            if (newId != null) {
                lastRefreshTime = 0L
                refreshSessions()
                _toastMessage.tryEmit("Conversation forked.")
            } else {
                Log.w(TAG, "forkSession: backend rejected $sessionId drop=$dropLastN")
                _toastMessage.tryEmit("Fork failed.")
            }
        }
    }

    fun rewindCurrentSessionAt(uiIndex: Int) {
        val b = activeBucket()
        val sessionId = b.jsonlSessionId ?: b.currentSessionIdForPagination ?: b.currentSessionId.value
        if (sessionId == null) {
            Log.w(TAG, "rewindCurrentSessionAt: no session id on active bucket")
            return
        }
        val total = b.messages.value.size
        val dropLastN = (total - 1 - uiIndex).coerceAtLeast(0)
        truncateSessionById(sessionId, dropLastN, explicitLocalId = b.currentLocalId.value)
    }

    fun forkCurrentSessionAt(uiIndex: Int) {
        val b = activeBucket()
        val sessionId = b.jsonlSessionId ?: b.currentSessionIdForPagination ?: b.currentSessionId.value
        if (sessionId == null) {
            Log.w(TAG, "forkCurrentSessionAt: no session id on active bucket")
            return
        }
        val total = b.messages.value.size
        val dropLastN = (total - 1 - uiIndex).coerceAtLeast(0)
        forkSessionById(sessionId, dropLastN)
    }

    // ─────────────────────────────────────────────────────────────────
    // Voice-bound transcript append (called by ViewModel during Inc 3,
    // VoiceController in Inc 4). Always writes to the orchestrator bucket
    // — voice always belongs there.
    // ─────────────────────────────────────────────────────────────────

    fun appendOrchestratorMessage(message: ChatMessage) {
        bucket(WebSocketEndpoint.ORCHESTRATOR).messages.update { it + message }
    }

    /**
     * Finalize the active streaming message in the orchestrator bucket — voice
     * sessions don't emit TurnComplete, so VoiceEnded handler in the ViewModel
     * calls this before doing voice cleanup.
     */
    fun finalizeStreamingForVoiceEnd() {
        val b = bucket(WebSocketEndpoint.ORCHESTRATOR)
        b.streamingMessageId?.let { messageId ->
            b.messages.update { messages ->
                messages.map { msg ->
                    if (msg.id == messageId) {
                        msg.copy(
                            isStreaming = false,
                            blocks = msg.blocks.map { block ->
                                when (block) {
                                    is MessageBlock.Text -> block.copy(isStreaming = false)
                                    is MessageBlock.Thinking -> block.copy(isStreaming = false)
                                    else -> block
                                }
                            }
                        )
                    } else msg
                }
            }
        }
        b.streamingMessageId = null
        b.sessionStatus.value = "idle"
    }

    /** Used by the ViewModel for voice-only error messages routed into the orchestrator bucket. */
    fun setOrchestratorSessionStatus(status: String) {
        bucket(WebSocketEndpoint.ORCHESTRATOR).sessionStatus.value = status
    }

    // ─────────────────────────────────────────────────────────────────
    // serverUrlChanged teardown — called from the settings observer
    // ─────────────────────────────────────────────────────────────────

    fun onServerUrlChanged() {
        _sessions.value = emptyList()
        _liveSessionIds.value = emptySet()
        _sdkToLocalId.value = emptyMap()
        _isOrchestratorSession.value = false
        sessionCache.clear()
        pendingAgentResume = null
        for (b in buckets.values) {
            b.messages.value = emptyList()
            b.currentSessionId.value = null
            b.currentLocalId.value = UUID.randomUUID().toString()
            b.pendingResumeSessionId.value = null
            b.jsonlSessionId = null
            b.hasMoreMessages.value = false
            b.currentSessionIdForPagination = null
            b.paginationStartIndex = 0
            b.streamingMessageId = null
            b.sessionStatus.value = "idle"
        }
    }

    /** Read access to the currently-active bucket's jsonl id — voice startup needs this. */
    fun orchestratorJsonlSessionId(): String? = bucket(WebSocketEndpoint.ORCHESTRATOR).jsonlSessionId
    /** Read access to the orchestrator bucket's current session id — voice startup fallback. */
    fun orchestratorCurrentSessionId(): String? = bucket(WebSocketEndpoint.ORCHESTRATOR).currentSessionId.value
    /** Read access to the orchestrator bucket's current local id — voice startup needs this. */
    fun orchestratorCurrentLocalId(): String = bucket(WebSocketEndpoint.ORCHESTRATOR).currentLocalId.value

    /**
     * Restore the persisted orchestrator local_id BEFORE any WS opens —
     * called from the ViewModel's settings observer on first emission.
     * Pinned from HEAD AssistantViewModel.kt:319-321.
     */
    fun setOrchestratorLocalIdForRestore(localId: String) {
        bucket(WebSocketEndpoint.ORCHESTRATOR).currentLocalId.value = localId
    }
}
