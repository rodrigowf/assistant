package com.assistant.peripheral.connection

import android.util.Log
import com.assistant.peripheral.data.ConnectionState
import com.assistant.peripheral.data.WebSocketMessage
import com.assistant.peripheral.network.DiscoveredServer
import com.assistant.peripheral.network.LiveSession
import com.assistant.peripheral.network.WebSocketEndpoint
import com.assistant.peripheral.network.WebSocketManager
import com.assistant.peripheral.settings.SettingsRepository
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

/**
 * Owns the orchestrator-side WS connection lifecycle: cold-start probe,
 * orchestrator_active recovery state machine, network scan + auto-connect,
 * server-URL teardown, persisted local-id storage.
 *
 * Increment 2 of the viewmodel refactor — extracts the recovery state +
 * Connected-handler probe + `connect/disconnect/reconnectIfNeeded` from
 * `AssistantViewModel` (HEAD `28d982d` ranges L48-72, L519-622, L990-1050,
 * L1051-1112, L1140-1156, L1769-1801).
 *
 * Design notes:
 *
 *  - The controller takes functional dependencies for the WS operations
 *    instead of a `WebSocketManager` directly. This keeps it testable
 *    without a Mockito mock-maker plugin (the project doesn't have
 *    mockito-inline configured). Tests substitute trivial counter-based
 *    fakes for each function.
 *  - State the controller owns is final: the recovery machine fields,
 *    `noActiveOrchestrator`. Anything it needs from outside (the live
 *    pool, the persisted server URL) comes via the function deps so the
 *    caller can swap them in tests.
 *  - Cross-subsystem signalling is via [ConnectionEvent]s on the [events]
 *    flow. The ViewModel subscribes and routes to the chat bucket / voice
 *    layer until those become controllers in Inc 3/4.
 *
 * The controller does NOT:
 *  - touch the orchestrator [ChatStateBucket] (chat-state — Inc 3)
 *  - read `activeVoiceConfig` or decide between `voice_start`/`start`
 *    (voice — Inc 4 subscribes to [ConnectionEvent.Reconnected])
 *  - own the WS event collector itself; the ViewModel still routes
 *    `WebSocketEvent.Connected/Error/SessionStarted` for the ORCHESTRATOR
 *    endpoint into this controller's `onWsConnected` /
 *    `onOrchestratorActiveError` / `onSessionStartedForOrchestrator`
 *    callbacks during Inc 2-3. The collector moves into ChatController
 *    at Inc 3.
 */
class OrchestratorConnectionController(
    private val scope: CoroutineScope,
    private val settingsRepository: SettingsRepository,
    private val webSocketManager: WebSocketManager,
    /**
     * Returns the current live-pool snapshot. Today the ViewModel's
     * `apiClient` is rebuilt on serverUrlChanged so a function dep keeps
     * the controller pointing at the right backend.
     */
    private val getLivePool: suspend () -> List<LiveSession>,
    /**
     * Subnet sweep — defaults to the production [NetworkScanner] dependency
     * via the ViewModel's `scanForServers()` call. Function form lets tests
     * substitute a deterministic discovery list.
     */
    private val networkScan: suspend () -> List<DiscoveredServer>
) {

    companion object {
        private const val TAG = "OrchConnectionCtrl"
        // orchestrator_active recovery: 3 attempts at 0 / 500 / 2000 ms before
        // giving up and surfacing the empty-state UI. Pinned from HEAD
        // AssistantViewModel.MAX_RECOVERY_RETRIES at L46-47.
        const val MAX_RECOVERY_RETRIES = 3
        // Pool-probe retry delay on cold-start miss — the backend can be
        // slow to publish pool state. Pinned from HEAD L572.
        private const val POOL_PROBE_RETRY_MS = 400L
    }

    // ─────────────────────────────────────────────────────────────────
    // Public surface
    // ─────────────────────────────────────────────────────────────────

    /** Pass-through from [WebSocketManager.connectionState] for the orchestrator endpoint. */
    val connectionState: StateFlow<ConnectionState> = webSocketManager.connectionState

    private val _noActiveOrchestrator = MutableStateFlow(false)
    /** True after the cap-hit branch or NoOrchestratorFound — UI routes to History. */
    val noActiveOrchestrator: StateFlow<Boolean> = _noActiveOrchestrator.asStateFlow()

    /**
     * Set the flag from outside the controller (used by the ViewModel's
     * `loadSession`/`newSession` paths which adopt an orchestrator without
     * going through the Connected probe). Inc 3 (ChatController) will absorb
     * those paths and this becomes internal.
     */
    fun setNoActiveOrchestrator(value: Boolean) {
        _noActiveOrchestrator.value = value
    }

    private val _events = MutableSharedFlow<ConnectionEvent>(extraBufferCapacity = 16)
    /** Typed events for cross-subsystem subscribers. */
    val events: SharedFlow<ConnectionEvent> = _events.asSharedFlow()

    private val _discoveredServers = MutableStateFlow<List<DiscoveredServer>>(emptyList())
    val discoveredServers: StateFlow<List<DiscoveredServer>> = _discoveredServers.asStateFlow()

    private val _isScanning = MutableStateFlow(false)
    val isScanning: StateFlow<Boolean> = _isScanning.asStateFlow()

    // ─────────────────────────────────────────────────────────────────
    // Recovery state machine
    // ─────────────────────────────────────────────────────────────────

    // Bounded, single-flight state for the orchestrator_active recovery.
    // Pinned from HEAD AssistantViewModel.kt:60-64 — the loop fix that
    // shipped in commit 5b1b1f8.
    private val recoveryInFlight = AtomicBoolean(false)
    private val recoveryAttempt = AtomicInteger(0)

    // Set by [armNewSessionStart], read+cleared by [onWsConnected]. Mirrors
    // HEAD AssistantViewModel's `pendingNewSessionStart` field.
    private val pendingNewSessionStart = AtomicBoolean(false)

    // ─────────────────────────────────────────────────────────────────
    // Connect / disconnect / reconnect
    // ─────────────────────────────────────────────────────────────────

    /**
     * Open the orchestrator WS using the persisted serverUrl. Suspends
     * until [SettingsRepository.awaitLoaded] returns — see commit `28d982d`
     * for why the URL must come from the repository, not a default-before-load
     * `.value` read.
     *
     * The `localId` parameter lets the caller (today, the ViewModel) pass
     * the orchestrator bucket's current local_id. Inc 3 (ChatController)
     * will keep that responsibility; this controller never owns the bucket.
     */
    suspend fun connect(localId: String) {
        val loaded = settingsRepository.awaitLoaded()
        webSocketManager.connect(loaded.serverUrl, localId, WebSocketEndpoint.ORCHESTRATOR)
    }

    fun disconnect() {
        webSocketManager.disconnect()
    }

    /**
     * Re-establish the WS if currently disconnected. The ViewModel calls this
     * from `MainActivity.onResume()`.
     */
    fun reconnectIfNeeded(localId: String) {
        val state = connectionState.value
        if (state is ConnectionState.Disconnected || state is ConnectionState.Error) {
            Log.d(TAG, "Reconnecting WebSocket on foreground (was $state)")
            scope.launch { connect(localId) }
        }
    }

    // ─────────────────────────────────────────────────────────────────
    // WS event hooks (called from the ViewModel's collector during Inc 2-3)
    // ─────────────────────────────────────────────────────────────────

    /**
     * Called when the orchestrator WS handshake completes. Runs the
     * `pendingNewSessionStart`-or-probe branch and emits the appropriate
     * [ConnectionEvent]. Pinned from HEAD AssistantViewModel.kt:543-622.
     */
    fun onWsConnected() {
        // Honour `newSession()`'s pending-start before the probe — the user
        // explicitly chose to fork a fresh orchestrator.
        if (pendingNewSessionStart.compareAndSet(true, false)) {
            _noActiveOrchestrator.value = false
            _events.tryEmit(ConnectionEvent.NewSessionAdopted)
            return
        }
        scope.launch {
            var existing = getLivePool().find { it.isOrchestrator }
            if (existing == null) {
                // Backend can be slow to publish pool state on cold start;
                // without the retry-once, a transient empty response would
                // falsely trigger the empty-state UI. Pinned from HEAD L571-574.
                delay(POOL_PROBE_RETRY_MS)
                existing = getLivePool().find { it.isOrchestrator }
            }

            if (existing != null) {
                settingsRepository.persistOrchestratorLocalId(existing.localId)
                _noActiveOrchestrator.value = false
                _events.tryEmit(ConnectionEvent.OrchestratorAdopted(existing.localId, existing.sdkSessionId))
                _events.tryEmit(ConnectionEvent.Reconnected(existing.localId, existing.sdkSessionId))
            } else {
                _noActiveOrchestrator.value = true
                _events.tryEmit(ConnectionEvent.NoOrchestratorFound)
            }
        }
    }

    /**
     * Called from `newSession()` to skip the next Connected probe and start
     * a fresh orchestrator with the bucket's pre-set local_id.
     */
    fun armNewSessionStart() {
        pendingNewSessionStart.set(true)
        _noActiveOrchestrator.value = false
    }

    /**
     * Called when an orchestrator `SessionStarted` event arrives. Recovery
     * converged — reset the back-off so a later legitimate reconnect doesn't
     * carry over a partial counter and trip the cap.
     */
    fun onSessionStartedForOrchestrator() {
        _noActiveOrchestrator.value = false
        recoveryAttempt.set(0)
    }

    /**
     * Called when the backend rejects our orchestrator Start because the
     * pool already holds a different orchestrator. Pinned from HEAD
     * `recoverFromOrchestratorActive` at L1007-1049 — single-flight,
     * 0/500/2000 ms back-off, retry cap at [MAX_RECOVERY_RETRIES],
     * isConnected gate triggers reconnect not send.
     */
    fun onOrchestratorActiveError() {
        if (!recoveryInFlight.compareAndSet(false, true)) {
            Log.d(TAG, "recoverFromOrchestratorActive already in flight; ignoring")
            return
        }
        scope.launch {
            try {
                val attempt = recoveryAttempt.getAndIncrement()
                if (attempt >= MAX_RECOVERY_RETRIES) {
                    Log.w(TAG, "recoverFromOrchestratorActive hit retry cap ($MAX_RECOVERY_RETRIES); routing user to History")
                    _noActiveOrchestrator.value = true
                    _events.tryEmit(ConnectionEvent.OrchestratorActiveCapHit)
                    return@launch
                }
                if (attempt > 0) {
                    // 500ms after the first failure, 2000ms after the second.
                    delay(500L shl (attempt - 1))
                }
                val live = getLivePool().find { it.isOrchestrator } ?: return@launch
                settingsRepository.persistOrchestratorLocalId(live.localId)
                // Emit the adopt event so the bucket gets the new ids before
                // we send/reconnect. The ViewModel handles the event
                // synchronously (writes bucket state); we then proceed.
                _events.tryEmit(ConnectionEvent.OrchestratorAdopted(live.localId, live.sdkSessionId))
                if (!webSocketManager.isConnected(WebSocketEndpoint.ORCHESTRATOR)) {
                    // WS dropped silently — open a fresh one and let the next
                    // Connected handler resume. Sending into a stale socket
                    // would put us right back into this loop.
                    Log.i(TAG, "WS not connected during recovery (attempt=$attempt); reconnecting")
                    webSocketManager.connect(
                        settingsRepository.awaitLoaded().serverUrl,
                        live.localId,
                        WebSocketEndpoint.ORCHESTRATOR
                    )
                    return@launch
                }
                webSocketManager.send(
                    WebSocketMessage.Start(localId = live.localId, resumeSdkId = live.sdkSessionId),
                    endpoint = WebSocketEndpoint.ORCHESTRATOR
                )
            } finally {
                recoveryInFlight.set(false)
            }
        }
    }

    // ─────────────────────────────────────────────────────────────────
    // Network scan + auto-connect
    // ─────────────────────────────────────────────────────────────────

    /**
     * Subnet sweep for backend autodiscovery. Mirrors HEAD
     * AssistantViewModel.scanForServers at L1769-1789 — auto-connects to
     * the first discovered server only if the persisted URL is still the
     * default (don't overwrite a user-configured server URL).
     */
    fun scanForServers() {
        if (_isScanning.value) return
        scope.launch {
            _isScanning.value = true
            _discoveredServers.value = emptyList()
            try {
                val servers = networkScan()
                _discoveredServers.value = servers
                val currentUrl = settingsRepository.awaitLoaded().serverUrl
                val defaultUrl = com.assistant.peripheral.data.AppSettings().serverUrl
                if (servers.isNotEmpty() &&
                    connectionState.value !is ConnectionState.Connected &&
                    currentUrl == defaultUrl
                ) {
                    connectToDiscoveredServer(servers.first())
                }
            } finally {
                _isScanning.value = false
            }
        }
    }

    fun connectToDiscoveredServer(server: DiscoveredServer) {
        scope.launch { settingsRepository.updateServerUrl(server.wsUrl) }
        // connect() is driven by the settings observer on URL change.
    }

    // ─────────────────────────────────────────────────────────────────
    // serverUrlChanged teardown coordination
    // ─────────────────────────────────────────────────────────────────

    /**
     * Called from the settings observer when the serverUrl changes. Drops
     * the WS so the next `connect()` opens against the new URL. The chat
     * bucket wipe stays at the ViewModel level for now (moves to
     * ChatController at Inc 3).
     */
    fun teardownForServerUrlChange() {
        webSocketManager.disconnect()
        scope.launch { settingsRepository.clearOrchestratorLocalId() }
    }
}
