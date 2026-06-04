package com.assistant.peripheral.viewmodel

import android.app.Application
import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.*
import androidx.datastore.preferences.preferencesDataStore
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import android.util.Log
import com.assistant.peripheral.audio.AudioRecorder
import com.assistant.peripheral.data.*
import com.assistant.peripheral.network.ApiClient
import com.assistant.peripheral.network.DiscoveredServer
import com.assistant.peripheral.network.LiveSession
import com.assistant.peripheral.network.NetworkScanner
import com.assistant.peripheral.network.WebSocketEndpoint
import com.assistant.peripheral.network.WebSocketManager
import com.assistant.peripheral.service.AssistantService
import com.assistant.peripheral.voice.VoiceEvent
import com.assistant.peripheral.voice.VoiceManager
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.flow.*
import kotlinx.coroutines.launch
import java.util.UUID

// DataStore for app settings
private val Context.dataStore: DataStore<Preferences> by preferencesDataStore(name = "settings")

class AssistantViewModel(application: Application) : AndroidViewModel(application) {

    companion object {
        private const val TAG = "AssistantViewModel"
        private const val MAX_CACHED_SESSIONS = 5  // Keep at most 5 sessions in cache
        private const val MAX_CACHED_MESSAGES_PER_SESSION = 100  // Limit messages per cached session
    }

    private val dataStore = application.dataStore
    private val webSocketManager = WebSocketManager()
    private val audioRecorder = AudioRecorder(application.applicationContext)

    // API client (created when server URL is known)
    private var apiClient: ApiClient? = null

    // Voice manager for WebRTC (created lazily when apiClient is available)
    private var voiceManager: VoiceManager? = null

    // Connection state
    val connectionState: StateFlow<ConnectionState> = webSocketManager.connectionState

    // Per-endpoint chat state buckets — ensures events from the orchestrator
    // socket and the agent socket never write into each other's UI state.
    // The visible flows (messages, sessionStatus, hasMoreMessages, etc.) are
    // derived: they mirror whichever bucket the user is currently looking at,
    // chosen by [_isOrchestratorSession].
    private class ChatStateBucket {
        // The session id reported by the backend (sdk/JSONL id, or local_id on
        // reconnect). Distinct per endpoint.
        val currentSessionId = MutableStateFlow<String?>(null)
        // The true JSONL/SDK id, set from pendingResumeSessionId on SessionStarted.
        var jsonlSessionId: String? = null
        // Local id — only meaningful for orchestrator (the pool is keyed by it);
        // for agent the live local_id is generated per loadSession.
        val currentLocalId = MutableStateFlow(UUID.randomUUID().toString())
        // The conversation displayed for this tab.
        val messages = MutableStateFlow<List<ChatMessage>>(emptyList())
        // Pagination state for message history.
        var currentSessionIdForPagination: String? = null
        var paginationStartIndex: Int = 0
        val hasMoreMessages = MutableStateFlow(false)
        // Streaming-message scratchpad — owned by this endpoint. Blocks are
        // mutated in arrival order on the message itself; see mutateStreamingBlocks.
        var streamingMessageId: String? = null
        // Session lifecycle.
        val sessionStatus = MutableStateFlow("idle")
        // Used so SessionStarted can fetch history for the right sdk id.
        val pendingResumeSessionId = MutableStateFlow<String?>(null)
    }

    private val buckets: Map<WebSocketEndpoint, ChatStateBucket> = mapOf(
        WebSocketEndpoint.ORCHESTRATOR to ChatStateBucket(),
        WebSocketEndpoint.AGENT to ChatStateBucket()
    )

    private fun bucket(endpoint: WebSocketEndpoint): ChatStateBucket = buckets.getValue(endpoint)
    private fun activeBucket(): ChatStateBucket = bucket(currentEndpoint())

    // All sessions (from REST API)
    private val _sessions = MutableStateFlow<List<SessionInfo>>(emptyList())
    val sessions: StateFlow<List<SessionInfo>> = _sessions.asStateFlow()

    private val _sessionsLoading = MutableStateFlow(false)
    val sessionsLoading: StateFlow<Boolean> = _sessionsLoading.asStateFlow()

    // Live session pool (truly open sessions)
    private val _liveSessionIds = MutableStateFlow<Set<String>>(emptySet())
    val liveSessionIds: StateFlow<Set<String>> = _liveSessionIds.asStateFlow()

    // Map from SDK/JSONL session id -> local_id so we can call /close (which is
    // keyed by local_id) when the user closes from the sessions list (which keys
    // by JSONL session id).
    private val _sdkToLocalId = MutableStateFlow<Map<String, String>>(emptyMap())

    // Whether current session is orchestrator — drives which bucket the UI sees.
    private val _isOrchestratorSession = MutableStateFlow(false)
    val isOrchestratorSession: StateFlow<Boolean> = _isOrchestratorSession.asStateFlow()

    // ---- Public state, mirrored from the active bucket -------------------
    // Each public flow follows _isOrchestratorSession and re-emits whichever
    // bucket's flow matches. flatMapLatest cancels the previous inner
    // collection on switch, so the UI never reads stale data from the
    // background tab.
    @OptIn(kotlinx.coroutines.ExperimentalCoroutinesApi::class)
    private fun <T> mirrorActive(initial: T, pick: (ChatStateBucket) -> StateFlow<T>): StateFlow<T> =
        _isOrchestratorSession
            .flatMapLatest { isOrch ->
                pick(bucket(if (isOrch) WebSocketEndpoint.ORCHESTRATOR else WebSocketEndpoint.AGENT))
            }
            .stateIn(viewModelScope, SharingStarted.Eagerly, initial)

    val currentSessionId: StateFlow<String?> =
        mirrorActive(null) { it.currentSessionId }

    /**
     * Local id for the *currently displayed* tab. The orchestrator's local id
     * is the pool key on the backend; the agent's is generated per loadSession.
     */
    val currentLocalId: StateFlow<String> =
        mirrorActive(buckets.getValue(WebSocketEndpoint.ORCHESTRATOR).currentLocalId.value) { it.currentLocalId }

    val messages: StateFlow<List<ChatMessage>> =
        mirrorActive(emptyList()) { it.messages }

    val hasMoreMessages: StateFlow<Boolean> =
        mirrorActive(false) { it.hasMoreMessages }

    val sessionStatus: StateFlow<String> =
        mirrorActive("idle") { it.sessionStatus }

    private val _isLoadingMoreMessages = MutableStateFlow(false)
    val isLoadingMoreMessages: StateFlow<Boolean> = _isLoadingMoreMessages.asStateFlow()

    // Session cache - keeps messages and state for multiple sessions in memory

    private data class CachedSession(
        val messages: List<ChatMessage>,
        val isOrchestrator: Boolean,
        val paginationStartIndex: Int,
        val hasMoreMessages: Boolean
    )
    private val sessionCache = LinkedHashMap<String, CachedSession>(MAX_CACHED_SESSIONS, 0.75f, true)

    /**
     * Ensure a streaming message exists in the given bucket. Creates one if needed.
     */
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

    // Recording state
    private val _isRecording = MutableStateFlow(false)
    val isRecording: StateFlow<Boolean> = _isRecording.asStateFlow()

    // Voice state
    private val _voiceState = MutableStateFlow<VoiceState>(VoiceState.Off)
    val voiceState: StateFlow<VoiceState> = _voiceState.asStateFlow()

    /**
     * One-shot transient message for the UI to display as a toast/snackbar.
     * Set to a non-null string when something noteworthy happens (e.g. the
     * audio router downgrades the user's requested output).  The UI consumes
     * it and calls [clearToast] when it's done showing.
     */
    private val _toastMessage = MutableStateFlow<String?>(null)
    val toastMessage: StateFlow<String?> = _toastMessage.asStateFlow()

    fun clearToast() {
        _toastMessage.value = null
    }

    /**
     * Transient banner for the voice reconnect lifecycle. null = no
     * banner. Set by [VoiceEvent.ReconnectWarning] (early heads-up
     * from Gemini's goAway) and [VoiceEvent.Reconnecting] (relay is
     * actively cycling). Cleared when [VoiceState] flips back to
     * Active after the new upstream's setupComplete arrives.
     */
    private val _voiceReconnectBanner = MutableStateFlow<String?>(null)
    val voiceReconnectBanner: StateFlow<String?> = _voiceReconnectBanner.asStateFlow()

    // Muted state for voice
    private val _isMuted = MutableStateFlow(false)
    val isMuted: StateFlow<Boolean> = _isMuted.asStateFlow()

    // True when we're connected to the server but no orchestrator session is live.
    // The UI uses this to redirect from the Chat tab to History so the user can
    // pick or create a session — we no longer auto-spawn one on connect.
    private val _noActiveOrchestrator = MutableStateFlow(false)
    val noActiveOrchestrator: StateFlow<Boolean> = _noActiveOrchestrator.asStateFlow()

    // Settings
    private val _settings = MutableStateFlow(AppSettings())
    val settings: StateFlow<AppSettings> = _settings.asStateFlow()

    // Network scan
    private val _discoveredServers = MutableStateFlow<List<DiscoveredServer>>(emptyList())
    val discoveredServers: StateFlow<List<DiscoveredServer>> = _discoveredServers.asStateFlow()

    private val _isScanning = MutableStateFlow(false)
    val isScanning: StateFlow<Boolean> = _isScanning.asStateFlow()

    // System (backend) configuration — drives the System Settings tab.
    // Loaded on demand when the System tab is opened and refreshed after
    // each save. Independent of the App-side AppSettings.
    private val _systemConfig = MutableStateFlow(SystemConfigState())
    val systemConfig: StateFlow<SystemConfigState> = _systemConfig.asStateFlow()
    private var savedFlashJob: kotlinx.coroutines.Job? = null

    // Preference keys
    private object PreferenceKeys {
        val SERVER_URL = stringPreferencesKey("server_url")
        val AUTO_CONNECT = booleanPreferencesKey("auto_connect")
        val ENABLE_WAKE_WORD = booleanPreferencesKey("enable_wake_word")
        val WAKE_WORD = stringPreferencesKey("wake_word")
        val VOICE_WORD = stringPreferencesKey("voice_word")
        val THEME_MODE = stringPreferencesKey("theme_mode")
        val MIC_GAIN_LEVEL = floatPreferencesKey("mic_gain_level")
        val WAKE_WORD_MIC_GAIN_LEVEL = floatPreferencesKey("wake_word_mic_gain_level")
        val SPEAKER_VOLUME_LEVEL = floatPreferencesKey("speaker_volume_level")
        val ECHO_DUCKING_GAIN = floatPreferencesKey("echo_ducking_gain")
        val AUDIO_OUTPUT = stringPreferencesKey("audio_output")  // enum: EARPIECE / LOUDSPEAKER / BLUETOOTH
        val ENABLE_BUTTON_TRIGGER = booleanPreferencesKey("enable_button_trigger")
        val SAVED_SERVERS = stringPreferencesKey("saved_servers")
        // Persisted across app restarts so we reattach to the same orchestrator
        // session instead of forking a new one when getLivePool() races on launch.
        val ORCHESTRATOR_LOCAL_ID = stringPreferencesKey("orchestrator_local_id")
    }

    // Saved servers are persisted as "label\turl|label\turl|..." — no quoting needed
    // since labels/urls never contain tab or pipe in practice.
    private fun encodeSavedServers(servers: List<SavedServer>): String =
        servers.joinToString("|") { "${it.label}\t${it.url}" }

    private fun decodeSavedServers(raw: String?): List<SavedServer> {
        if (raw.isNullOrEmpty()) return emptyList()
        return raw.split("|").mapNotNull { entry ->
            val parts = entry.split("\t", limit = 2)
            if (parts.size == 2 && parts[0].isNotBlank() && parts[1].isNotBlank())
                SavedServer(parts[0], parts[1]) else null
        }
    }

    init {
        // Load settings from DataStore
        viewModelScope.launch {
            var previousServerUrl: String? = null
            var firstEmission = true
            dataStore.data.collect { preferences ->
                val newServerUrl = preferences[PreferenceKeys.SERVER_URL] ?: AppSettings().serverUrl
                val serverUrlChanged = previousServerUrl != null && previousServerUrl != newServerUrl
                previousServerUrl = newServerUrl

                // On first emission, restore the persisted orchestrator local_id so
                // we reattach to the same session across app restarts. Without this,
                // each launch generates a fresh UUID and forks a new orchestrator
                // when getLivePool() races (e.g. backend slow on cold start).
                if (firstEmission) {
                    firstEmission = false
                    preferences[PreferenceKeys.ORCHESTRATOR_LOCAL_ID]?.takeIf { it.isNotBlank() }?.let {
                        bucket(WebSocketEndpoint.ORCHESTRATOR).currentLocalId.value = it
                    }
                }

                _settings.value = AppSettings(
                    serverUrl = newServerUrl,
                    savedServers = decodeSavedServers(preferences[PreferenceKeys.SAVED_SERVERS]),
                    autoConnect = preferences[PreferenceKeys.AUTO_CONNECT] ?: AppSettings().autoConnect,
                    enableWakeWord = preferences[PreferenceKeys.ENABLE_WAKE_WORD] ?: AppSettings().enableWakeWord,
                    wakeWord = preferences[PreferenceKeys.WAKE_WORD] ?: AppSettings().wakeWord,
                    voiceWord = preferences[PreferenceKeys.VOICE_WORD] ?: AppSettings().voiceWord,
                    themeMode = try {
                        ThemeMode.valueOf(preferences[PreferenceKeys.THEME_MODE] ?: ThemeMode.SYSTEM.name)
                    } catch (e: Exception) {
                        ThemeMode.SYSTEM
                    },
                    micGainLevel = preferences[PreferenceKeys.MIC_GAIN_LEVEL] ?: 1.0f,
                    wakeWordMicGainLevel = preferences[PreferenceKeys.WAKE_WORD_MIC_GAIN_LEVEL] ?: 1.0f,
                    speakerVolumeLevel = preferences[PreferenceKeys.SPEAKER_VOLUME_LEVEL] ?: 1.0f,
                    echoDuckingGain = preferences[PreferenceKeys.ECHO_DUCKING_GAIN] ?: AppSettings().echoDuckingGain,
                    audioOutput = AudioOutput.fromString(preferences[PreferenceKeys.AUDIO_OUTPUT]),
                    enableButtonTrigger = preferences[PreferenceKeys.ENABLE_BUTTON_TRIGGER] ?: false
                )
                // Sync button trigger setting to SharedPreferences so ButtonAccessibilityService can read it
                getApplication<Application>().getSharedPreferences("assistant_service_prefs", Context.MODE_PRIVATE)
                    .edit().putBoolean("button_trigger_enabled", _settings.value.enableButtonTrigger).apply()
                // Update API client when server URL changes
                val needNewVoiceManager = voiceManager == null || serverUrlChanged
                if (needNewVoiceManager) {
                    apiClient = ApiClient(_settings.value.serverUrl)
                    voiceManager?.release()
                    voiceManager = VoiceManager(getApplication(), apiClient!!).also {
                        it.setMicGain(_settings.value.micGainLevel)
                        it.setEchoDuckingGain(_settings.value.echoDuckingGain)
                        it.setAudioOutput(_settings.value.audioOutput)
                    }
                    setupVoiceManagerCallbacks()
                } else {
                    // Same server — just refresh tunables on the existing manager.
                    // Rebuilding here on every DataStore emission was creating
                    // overlapping VoiceManager instances + stale flow collectors,
                    // which produced phantom duplicate sessions when the wake
                    // word fired (two .start() calls slipping past the off-state
                    // guard during the HTTP await).
                    voiceManager?.let {
                        it.setMicGain(_settings.value.micGainLevel)
                        it.setEchoDuckingGain(_settings.value.echoDuckingGain)
                        it.setAudioOutput(_settings.value.audioOutput)
                    }
                }

                // Clear all session state when switching servers
                if (serverUrlChanged) {
                    webSocketManager.disconnect()
                    _sessions.value = emptyList()
                    _liveSessionIds.value = emptySet()
                    // The persisted id belongs to the previous server's pool — drop it
                    // so we don't try to reattach to a session that doesn't exist here.
                    clearOrchestratorLocalId()
                    _isOrchestratorSession.value = false
                    sessionCache.clear()
                    // Wipe both per-endpoint buckets.
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
            }
        }

        // Collect WebSocket events — every event is tagged with the endpoint
        // that emitted it so we can route into the correct per-tab bucket.
        //
        // Run the dispatch on Dispatchers.Default so JSON parsing,
        // bucket updates, and provider event handling don't compete
        // with the UI thread.  All bucket fields are MutableStateFlow,
        // which Compose's collectAsState() observes safely from any
        // thread — no Main hop is needed for the writes themselves.
        viewModelScope.launch(Dispatchers.Default) {
            webSocketManager.events.collect { (endpoint, event) ->
                handleWebSocketEvent(endpoint, event)
            }
        }
    }

    private fun setupVoiceManagerCallbacks() {
        voiceManager?.let { vm ->
            // Collect voice state changes
            viewModelScope.launch {
                vm.state.collect { state ->
                    _voiceState.value = state
                    // Clear the reconnect banner once we're back in Active —
                    // setupComplete on the new upstream re-fires
                    // voice_status:ready which flips state here.
                    if (state == VoiceState.Active && _voiceReconnectBanner.value != null) {
                        _voiceReconnectBanner.value = null
                    }
                }
            }

            // Collect voice events for transcription and messages
            viewModelScope.launch {
                vm.events.collect { event ->
                    handleVoiceEvent(event)
                }
            }

            // Set callback for mirroring OpenAI events to backend via WebSocket
            // Web frontend: wsRef.current?.send({ type: "voice_event", event })
            vm.setVoiceEventCallback { eventMap ->
                // Voice runs on the orchestrator socket only.
                webSocketManager.send(
                    WebSocketMessage.VoiceEvent(eventMap),
                    endpoint = WebSocketEndpoint.ORCHESTRATOR
                )
            }

            // Set callback for forwarding mic chunks to backend (WebSocket
            // voice providers — Qwen and friends).  WebRTC providers
            // bypass this; their audio goes peer-to-peer.
            vm.setMicChunkCallback { audioB64 ->
                webSocketManager.send(
                    WebSocketMessage.VoiceAudioIn(audioB64),
                    endpoint = WebSocketEndpoint.ORCHESTRATOR
                )
            }
        }
    }

    private fun handleVoiceEvent(event: VoiceEvent) {
        // Voice always belongs to the orchestrator bucket — even if the user is
        // currently looking at a Claude Code session in the agent tab.
        val b = bucket(WebSocketEndpoint.ORCHESTRATOR)
        when (event) {
            is VoiceEvent.UserTranscript -> {
                // Add user voice transcript as a message
                // Web frontend: optsRef.current.onUserTranscript?.(transcript)
                val userMessage = ChatMessage(
                    role = MessageRole.USER,
                    content = "[voice] ${event.text}",
                    blocks = listOf(MessageBlock.Text("[voice] ${event.text}"))
                )
                b.messages.update { it + userMessage }
            }
            is VoiceEvent.TextComplete -> {
                // Add assistant response as a message
                // Web frontend: optsRef.current.onAssistantComplete?.(transcript)
                if (event.text.isNotEmpty()) {
                    val assistantMessage = ChatMessage(
                        role = MessageRole.ASSISTANT,
                        content = event.text,
                        blocks = listOf(MessageBlock.Text(event.text))
                    )
                    b.messages.update { it + assistantMessage }
                }
            }
            is VoiceEvent.ToolUse -> {
                // Tool call from assistant
                // Web frontend: optsRef.current.onToolUse?.(callId, name, args)
                Log.d(TAG, "Voice tool use: ${event.name}")
                // Tool results are handled by backend and sent back via voice_command
            }
            is VoiceEvent.TurnComplete -> {
                // Turn completed
                // Web frontend: optsRef.current.onTurnComplete?.()
                b.sessionStatus.value = "idle"
            }
            is VoiceEvent.Error -> {
                Log.e(TAG, "Voice error: ${event.message}")
                val errorMessage = ChatMessage(
                    role = MessageRole.SYSTEM,
                    content = "Voice error: ${event.message}"
                )
                b.messages.update { it + errorMessage }
            }
            is VoiceEvent.RoutingFallback -> {
                // Surface as a toast so the user knows the requested
                // audio output was downgraded (e.g. JBL on OpenAI).
                Log.w(TAG, "Routing fallback: ${event.message}")
                _toastMessage.value = event.message
            }
            is VoiceEvent.ReconnectWarning -> {
                // Upstream goAway — fired ~30-60s before the actual cut.
                // Light banner + a soft beep so the user can pause speech.
                val secs = event.timeLeftSeconds
                _voiceReconnectBanner.value = if (secs != null) {
                    "Pausing in ~${secs}s to reconnect…"
                } else {
                    "Reconnecting shortly…"
                }
                playReconnectBeep()
            }
            is VoiceEvent.Reconnecting -> {
                // Active cutover — mic chunks are being dropped right now.
                _voiceReconnectBanner.value = "Pausing for a second to reconnect…"
            }
            is VoiceEvent.SessionEnded -> {
                _voiceState.value = VoiceState.Off
                _isMuted.value = false
                _voiceReconnectBanner.value = null
            }
            is VoiceEvent.SessionCreated -> {
                Log.d(TAG, "Voice session created")
            }
            is VoiceEvent.SpeechStarted -> {
                Log.d(TAG, "User speech started")
            }
            is VoiceEvent.SpeechStopped -> {
                Log.d(TAG, "User speech stopped")
            }
            is VoiceEvent.TextDelta -> {
                // Streaming assistant text - could update UI incrementally
                // For now, we wait for TextComplete
            }
        }
    }

    private fun handleWebSocketEvent(endpoint: WebSocketEndpoint, event: WebSocketEvent) {
        // Every chat-mutating event writes into THIS endpoint's bucket — never
        // into whichever tab the user happens to be looking at. That's the core
        // isolation guarantee.
        val b = bucket(endpoint)
        when (event) {
            is WebSocketEvent.Connected -> {
                // Agent socket: the orchestrator-probe is irrelevant here. Just send the
                // pending Start so the backend resumes/loads the requested Claude session.
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

                // Orchestrator socket below.
                val orchBucket = bucket(WebSocketEndpoint.ORCHESTRATOR)
                // If newSession() armed a pending Start (because it had to connect first),
                // honour it and skip the resume-existing lookup.
                if (pendingNewSessionStart) {
                    pendingNewSessionStart = false
                    _noActiveOrchestrator.value = false
                    _isOrchestratorSession.value = true
                    persistOrchestratorLocalId(orchBucket.currentLocalId.value)
                    webSocketManager.send(
                        WebSocketMessage.Start(localId = orchBucket.currentLocalId.value),
                        endpoint = WebSocketEndpoint.ORCHESTRATOR
                    )
                    return
                }

                // Check for an existing orchestrator on the server and reconnect to it.
                // If there isn't one, do NOT auto-spawn one — the UI will route the
                // user to History so they can pick or explicitly create a session.
                //
                // The pool lookup is retried once on miss because the backend can be
                // slow to publish pool state on cold start; without the retry, a
                // transient empty response would falsely trigger the empty-state UI.
                viewModelScope.launch {
                    suspend fun findOrchestrator(): LiveSession? =
                        apiClient?.getLivePool()?.find { it.isOrchestrator }

                    var existing = findOrchestrator()
                    if (existing == null) {
                        kotlinx.coroutines.delay(400L)
                        existing = findOrchestrator()
                    }

                    if (existing != null) {
                        // Reuse the existing orchestrator's local_id so the backend
                        // recognises this as a reconnect (not a new/conflicting session)
                        orchBucket.currentLocalId.value = existing.localId
                        _isOrchestratorSession.value = true
                        persistOrchestratorLocalId(existing.localId)
                        // Also track the sdk session id so we can load history
                        orchBucket.pendingResumeSessionId.value = existing.sdkSessionId
                        _noActiveOrchestrator.value = false
                        webSocketManager.send(
                            WebSocketMessage.Start(
                                localId = existing.localId,
                                resumeSdkId = existing.sdkSessionId
                            ),
                            endpoint = WebSocketEndpoint.ORCHESTRATOR
                        )
                    } else {
                        // No live orchestrator. Stay idle — UI will switch to History.
                        orchBucket.pendingResumeSessionId.value = null
                        _noActiveOrchestrator.value = true
                        // Make sure stale session list is loaded so History has something to show.
                        refreshSessions()
                    }
                }
            }

            is WebSocketEvent.SessionStarted -> {
                b.currentSessionId.value = event.sessionId
                b.sessionStatus.value = "idle"
                // Only clear noActiveOrchestrator if THIS is the orchestrator endpoint —
                // an agent SessionStarted shouldn't change the orchestrator's empty-state flag.
                if (endpoint == WebSocketEndpoint.ORCHESTRATOR) {
                    _noActiveOrchestrator.value = false
                }

                // Track the true JSONL session ID for voice resume.
                // On reconnect the backend returns local_id as session_id — use
                // pendingResumeSessionId (the actual SDK/JSONL id) instead.
                b.jsonlSessionId = b.pendingResumeSessionId.value ?: event.sessionId

                // If this is a voice session, forward the session.update payload to OpenAI
                // This sends the system prompt + tool definitions so the voice session
                // has full context (matches web frontend's useVoiceOrchestrator)
                event.voiceSessionUpdate?.let { update ->
                    voiceManager?.handleBackendCommand(update)
                }

                // Load/refresh messages when reconnecting to an existing session.
                // Always re-fetch from server so any messages that arrived while the
                // WebSocket was disconnected are not lost.
                val resumeId = b.pendingResumeSessionId.value
                if (resumeId != null) {
                    viewModelScope.launch {
                        try {
                            val paginated = apiClient?.getMessagesPaginated(resumeId, limit = 50)
                                ?: com.assistant.peripheral.network.PaginatedMessages(emptyList(), 0, false, 0)
                            // Always update — server is the source of truth
                            b.currentSessionIdForPagination = resumeId
                            b.paginationStartIndex = paginated.startIndex
                            b.hasMoreMessages.value = paginated.hasMore
                            b.messages.value = paginated.messages
                        } catch (_: Exception) {
                            // Best-effort — keep existing messages if fetch fails
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
                // Reset the disconnected endpoint's streaming scratchpad. Other
                // endpoint's bucket is untouched.
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
                // Ensure streaming message exists (orchestrator doesn't send message_start)
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
                // Finalize any trailing streaming text/thinking block so its
                // progress indicator clears when the model switches to a tool.
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

                // Update cache with new messages — only meaningful for the
                // currently-displayed tab (cache is keyed by sdk session id).
                if (endpoint == currentEndpoint()) {
                    saveCurrentSessionToCache()
                }
            }

            is WebSocketEvent.CompactComplete -> {
                // Add compact divider
                val compactMessage = ChatMessage(
                    role = MessageRole.SYSTEM,
                    content = "",
                    blocks = listOf(MessageBlock.Compact(event.summary))
                )
                b.messages.update { it + compactMessage }
            }

            is WebSocketEvent.Error -> {
                val errorMessage = ChatMessage(
                    role = MessageRole.SYSTEM,
                    content = "Error: ${event.message}${event.detail?.let { "\n$it" } ?: ""}"
                )
                b.messages.update { it + errorMessage }
                b.sessionStatus.value = "error"
                // If the orchestrator rejected our Start because another orchestrator
                // is already active (stale local_id), recover by refreshing the live
                // pool and re-Starting against the actual pool key.
                if (endpoint == WebSocketEndpoint.ORCHESTRATOR && event.message == "orchestrator_active") {
                    recoverFromOrchestratorActive()
                }
            }

            is WebSocketEvent.VoiceCommand -> {
                // Forward voice_command from backend to OpenAI via VoiceManager.
                // Voice runs only on the orchestrator socket.
                @Suppress("UNCHECKED_CAST")
                val command = event.command as? Map<String, Any?> ?: return
                voiceManager?.handleBackendCommand(command)
            }

            is WebSocketEvent.VoiceProviderEvent -> {
                // Provider event mirrored from backend (WebSocket
                // voice providers — Qwen et al.).  The provider parses
                // it and emits VoiceEvent on its own flow.
                voiceManager?.handleProviderEvent(event.event)
            }

            is WebSocketEvent.VoiceAudioOut -> {
                // Speaker chunk for WebSocket voice providers.
                voiceManager?.pushSpeakerChunk(event.audioBase64)
            }

            is WebSocketEvent.VoiceEnding -> {
                // Backend has begun teardown — show "Ending..." until
                // VoiceEnded confirms. If the user already pressed stop,
                // we're already in Ending and the timer is running;
                // otherwise (agent-initiated end) we set state here.
                if (_voiceState.value !is VoiceState.Ending) {
                    _voiceState.value = VoiceState.Ending
                    endingTimeoutJob?.cancel()
                    endingTimeoutJob = viewModelScope.launch {
                        kotlinx.coroutines.delay(ENDING_ACK_TIMEOUT_MS)
                        Log.w(TAG, "voice_ended ack timeout after voice_ending")
                        finalizeVoiceStop()
                    }
                }
            }

            is WebSocketEvent.VoiceEnded,
            is WebSocketEvent.VoiceStopped -> {
                // Backend teardown finished (or AI-initiated end via the
                // legacy voice_stopped event). Finalize any in-progress
                // streaming message (TurnComplete never arrives in voice
                // mode), then do the local teardown via finalizeVoiceStop.
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
                finalizeVoiceStop()
            }

            is WebSocketEvent.VoiceTranscript -> {
                // Handle voice transcripts (not used with realtime API)
            }

            // These are handled by ViewModel directly, not from WebSocket
            is WebSocketEvent.SessionList,
            is WebSocketEvent.HistoryLoaded,
            is WebSocketEvent.ToolProgress -> {}
        }
    }

    /**
     * Mutate the in-flight assistant message's block list in place, preserving
     * arrival order. The web frontend's reducer in useChatInstance.ts works
     * the same way: each new text delta either extends the trailing streaming
     * text block or starts a new one after whatever tool blocks were emitted
     * since the last text. This is the source of truth for ordering — never
     * rebuild blocks from per-type scratchpad buffers, which loses the order
     * between text and tool calls.
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

    /**
     * Backend rejected our orchestrator Start because the pool already has a
     * different orchestrator. This happens when our local_id is stale (e.g.
     * the live orchestrator was created from another client). Refresh the
     * pool and re-Start with the live local_id.
     */
    private fun recoverFromOrchestratorActive() {
        viewModelScope.launch {
            val live = apiClient?.getLivePool()?.find { it.isOrchestrator } ?: return@launch
            val orchBucket = bucket(WebSocketEndpoint.ORCHESTRATOR)
            orchBucket.currentLocalId.value = live.localId
            orchBucket.pendingResumeSessionId.value = live.sdkSessionId
            persistOrchestratorLocalId(live.localId)
            webSocketManager.send(
                WebSocketMessage.Start(localId = live.localId, resumeSdkId = live.sdkSessionId),
                endpoint = WebSocketEndpoint.ORCHESTRATOR
            )
        }
    }

    fun connect() {
        viewModelScope.launch {
            // The implicit "connect" is for the orchestrator socket — the agent
            // socket is opened lazily in loadSession when the user picks a Claude session.
            webSocketManager.connect(
                _settings.value.serverUrl,
                bucket(WebSocketEndpoint.ORCHESTRATOR).currentLocalId.value
            )
        }
    }

    fun disconnect() {
        webSocketManager.disconnect()
    }

    /**
     * Persist the orchestrator local_id so reopening the app reattaches to the same
     * session instead of forking a new one. Called whenever we learn the current
     * orchestrator id (from getLivePool() or a session_started event).
     */
    private fun persistOrchestratorLocalId(localId: String) {
        viewModelScope.launch {
            dataStore.edit { preferences ->
                preferences[PreferenceKeys.ORCHESTRATOR_LOCAL_ID] = localId
            }
        }
    }

    private fun clearOrchestratorLocalId() {
        viewModelScope.launch {
            dataStore.edit { preferences ->
                preferences.remove(PreferenceKeys.ORCHESTRATOR_LOCAL_ID)
            }
        }
    }

    /**
     * Re-establish the WebSocket connection if currently disconnected.
     * Call from MainActivity.onResume() so the app reconnects after screen lock/unlock
     * or switching back from another app.
     */
    fun reconnectIfNeeded() {
        val state = connectionState.value
        if (state is ConnectionState.Disconnected || state is ConnectionState.Error) {
            Log.d(TAG, "Reconnecting WebSocket on foreground (was $state)")
            connect()
        }
    }

    fun sendMessage(text: String) {
        if (text.isBlank()) return

        // Add user message to the active tab's bucket.
        val userMessage = ChatMessage(
            role = MessageRole.USER,
            content = text,
            blocks = listOf(MessageBlock.Text(text))
        )
        activeBucket().messages.update { it + userMessage }

        // Send to server — route to whichever socket owns the current chat tab.
        webSocketManager.send(WebSocketMessage.Send(text), endpoint = currentEndpoint())
    }

    fun interrupt() {
        webSocketManager.send(WebSocketMessage.Interrupt, endpoint = currentEndpoint())
        activeBucket().sessionStatus.value = "interrupted"
    }

    fun compact() {
        webSocketManager.send(WebSocketMessage.Compact, endpoint = currentEndpoint())
    }

    // Session management - debounced to prevent rapid duplicate refreshes
    private var lastRefreshTime = 0L
    private val refreshDebounceMs = 500L

    fun refreshSessions() {
        val now = System.currentTimeMillis()
        if (now - lastRefreshTime < refreshDebounceMs) {
            // Skip if we just refreshed
            return
        }
        lastRefreshTime = now

        viewModelScope.launch {
            _sessionsLoading.value = true

            // Fetch both sessions and live pool in parallel
            val sessions = apiClient?.listSessions() ?: emptyList()
            val livePool = apiClient?.getLivePool() ?: emptyList()

            // Extract SDK session IDs that are truly live
            _liveSessionIds.value = livePool.map { it.sdkSessionId }.toSet()
            _sdkToLocalId.value = livePool.associate { it.sdkSessionId to it.localId }

            _sessions.value = sessions.sortedByDescending { it.lastActivity }
            _sessionsLoading.value = false
        }
    }

    /**
     * Close a live (open) pool session without deleting its history.
     * Called from the session list "Close" dropdown action. The session id passed
     * in is the JSONL/SDK id; we look up its local_id from the live pool because
     * /close is keyed by local_id.
     *
     * If closing the currently-loaded session, also clears the in-memory chat so
     * the UI doesn't keep showing a session that's no longer running.
     */
    fun closeSession(sessionId: String) {
        viewModelScope.launch {
            // Look up local_id; refresh the pool first if we don't have one cached.
            var localId = _sdkToLocalId.value[sessionId]
            if (localId == null) {
                val livePool = apiClient?.getLivePool() ?: emptyList()
                _sdkToLocalId.value = livePool.associate { it.sdkSessionId to it.localId }
                localId = _sdkToLocalId.value[sessionId]
            }
            if (localId == null) {
                Log.w(TAG, "closeSession: no live local_id for $sessionId — already closed?")
                return@launch
            }

            val ok = apiClient?.closePoolSession(localId) ?: false
            if (!ok) {
                Log.w(TAG, "closeSession: backend rejected close for $localId")
                return@launch
            }

            // Optimistic UI update so the user sees the "open" badge disappear
            // without waiting for the next refresh.
            _liveSessionIds.update { it - sessionId }
            _sdkToLocalId.update { it - sessionId }

            // If we just closed the current session in either bucket, clear that
            // bucket's chat. If the closed session was the orchestrator, also drop
            // the persisted local_id so we don't try to reattach on next launch.
            for ((ep, b) in buckets) {
                if (b.currentSessionIdForPagination == sessionId || b.currentLocalId.value == localId) {
                    b.messages.value = emptyList()
                    b.currentSessionId.value = null
                    b.currentSessionIdForPagination = null
                    b.hasMoreMessages.value = false
                    b.currentLocalId.value = UUID.randomUUID().toString()
                    if (ep == WebSocketEndpoint.ORCHESTRATOR) {
                        clearOrchestratorLocalId()
                        _isOrchestratorSession.value = false
                    }
                }
            }
            sessionCache.remove(sessionId)

            // Refresh in the background to reconcile with server state
            refreshSessions()
        }
    }

    /**
     * Open a session in the appropriate tab. [liveLocalId] is the live pool's
     * local_id for the session if it's currently running on the backend
     * (passed from the History list, where SessionInfo.localId carries it).
     * For orchestrator reconnect this is *required* — the backend's pool is
     * keyed by local_id, so generating a fresh UUID here would be rejected
     * with `orchestrator_active`.
     */
    fun loadSession(
        sessionId: String,
        isOrchestrator: Boolean = false,
        liveLocalId: String? = null
    ) {
        viewModelScope.launch {
            // Save current session to cache before switching
            saveCurrentSessionToCache()

            val endpoint = if (isOrchestrator) WebSocketEndpoint.ORCHESTRATOR else WebSocketEndpoint.AGENT
            val b = bucket(endpoint)
            // For orchestrator reattach we must reuse the live local_id; for agent
            // sessions the local_id can be fresh per switch (the backend keys agent
            // sessions by local_id but each switch is a new pool entry).
            val localIdForStart = liveLocalId ?: UUID.randomUUID().toString()

            // Check if session is already cached
            val cached = sessionCache[sessionId]
            if (cached != null) {
                // Restore from cache - instant switch!
                b.currentSessionIdForPagination = sessionId
                b.paginationStartIndex = cached.paginationStartIndex
                b.hasMoreMessages.value = cached.hasMoreMessages
                b.messages.value = cached.messages
                b.currentLocalId.value = localIdForStart
                _isOrchestratorSession.value = cached.isOrchestrator
                if (isOrchestrator) _noActiveOrchestrator.value = false

                openSessionOnEndpoint(endpoint, localIdForStart, sessionId)
                return@launch
            }

            // Not cached - fetch from server with pagination
            val paginated = apiClient?.getMessagesPaginated(sessionId, limit = 50)
                ?: com.assistant.peripheral.network.PaginatedMessages(emptyList(), 0, false, 0)

            if (paginated.totalCount > 0 || paginated.messages.isNotEmpty()) {
                // Store pagination state for loading more
                b.currentSessionIdForPagination = sessionId
                b.paginationStartIndex = paginated.startIndex
                b.hasMoreMessages.value = paginated.hasMore
                b.messages.value = paginated.messages
                b.currentLocalId.value = localIdForStart
                _isOrchestratorSession.value = isOrchestrator
                if (isOrchestrator) _noActiveOrchestrator.value = false

                openSessionOnEndpoint(endpoint, localIdForStart, sessionId)
            }
        }
    }

    /**
     * Connect (if needed) the given endpoint and Start the session on it.
     *
     * Crucially, this does NOT touch the *other* endpoint's socket: opening a
     * Claude Code (agent) session must not tear down the orchestrator socket,
     * which may be running an active realtime voice conversation.
     *
     * If the target socket is already connected we re-Start it on the new
     * local_id immediately. Otherwise we queue the Start via pendingAgentResume
     * (or the bucket's pendingResumeSessionId for orchestrator) and the
     * Connected handler sends it once the handshake completes.
     */
    private fun openSessionOnEndpoint(
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
                // The orchestrator-probe in the Connected handler will pick up
                // the live orchestrator on the server and resume it. We don't
                // need pendingNewSessionStart — that path is for fresh sessions.
                bucket(WebSocketEndpoint.ORCHESTRATOR).pendingResumeSessionId.value = resumeSdkId
            }
        }
        webSocketManager.connect(_settings.value.serverUrl, localId, endpoint)
    }

    /**
     * Save the *active* tab's session state to cache for quick restoration later.
     * Limits messages to prevent TransactionTooLargeException.
     */
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

    /**
     * Load older messages when user scrolls up (reverse infinite scroll).
     * Messages are prepended to the existing list.
     */
    fun loadMoreMessages() {
        val b = activeBucket()
        if (_isLoadingMoreMessages.value || !b.hasMoreMessages.value) return
        val sessionId = b.currentSessionIdForPagination ?: return

        viewModelScope.launch {
            _isLoadingMoreMessages.value = true
            try {
                val paginated = apiClient?.getMessagesPaginated(
                    sessionId,
                    limit = 50,
                    beforeIndex = b.paginationStartIndex
                ) ?: return@launch

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

    fun newSession() {
        // newSession is orchestrator-only — operate on the orchestrator bucket.
        val b = bucket(WebSocketEndpoint.ORCHESTRATOR)

        // Save current session to cache before starting new one
        saveCurrentSessionToCache()

        // Generate new local ID for the orchestrator's pool entry
        b.currentLocalId.value = UUID.randomUUID().toString()
        b.messages.value = emptyList()
        b.currentSessionIdForPagination = null
        b.paginationStartIndex = 0
        b.hasMoreMessages.value = false
        _noActiveOrchestrator.value = false

        // Persist so a later reconnect finds this same session instead of forking.
        persistOrchestratorLocalId(b.currentLocalId.value)

        // Mark the active tab as orchestrator so subsequent send() routes there.
        _isOrchestratorSession.value = true

        // (Re)connect WebSocket and explicitly start the new session.
        if (webSocketManager.isConnected(WebSocketEndpoint.ORCHESTRATOR)) {
            // Already connected — the Connected handler won't re-fire, so send Start ourselves.
            webSocketManager.send(WebSocketMessage.Stop, endpoint = WebSocketEndpoint.ORCHESTRATOR)
            webSocketManager.send(
                WebSocketMessage.Start(localId = b.currentLocalId.value),
                endpoint = WebSocketEndpoint.ORCHESTRATOR
            )
        } else {
            // Not connected — connect, then send Start once Connected fires.
            // We arm pendingNewSessionStart so the Connected handler picks it up.
            pendingNewSessionStart = true
            connect()
        }
    }

    // Set by newSession() when we need to (re)connect first; consumed in the Connected handler.
    private var pendingNewSessionStart: Boolean = false

    // When loadSession() opens an agent session but the AGENT socket isn't connected yet,
    // we stash the resume sdk id here. The Connected(AGENT) handler picks it up and
    // sends the Start. Avoids racing send() against an in-flight WS handshake.
    private data class PendingAgentResume(val localId: String, val resumeSdkId: String)
    private var pendingAgentResume: PendingAgentResume? = null

    /** Pick the WebSocket endpoint that owns the currently-displayed session. */
    private fun currentEndpoint(): WebSocketEndpoint =
        if (_isOrchestratorSession.value) WebSocketEndpoint.ORCHESTRATOR else WebSocketEndpoint.AGENT

    fun deleteSession(sessionId: String) {
        viewModelScope.launch {
            val success = apiClient?.deleteSession(sessionId) ?: false
            if (success) {
                _sessions.update { it.filter { s -> s.sessionId != sessionId } }
                // Also remove from cache
                sessionCache.remove(sessionId)
            } else {
                Log.w(TAG, "deleteSession: backend rejected $sessionId")
                _toastMessage.value = "Delete failed."
            }
        }
    }

    fun renameSession(sessionId: String, title: String) {
        viewModelScope.launch {
            val success = apiClient?.renameSession(sessionId, title) ?: false
            if (success) {
                _sessions.update { sessions ->
                    sessions.map { s ->
                        if (s.sessionId == sessionId) s.copy(title = title) else s
                    }
                }
            }
        }
    }

    /**
     * Duplicate a session: copies its JSONL + title under a fresh UUID.
     * Refreshes the session list so the copy appears at the top.
     */
    fun duplicateSession(sessionId: String) {
        viewModelScope.launch {
            val newId = apiClient?.duplicateSession(sessionId)
            if (newId != null) {
                lastRefreshTime = 0L  // bypass refresh debounce
                refreshSessions()
                _toastMessage.value = "Conversation duplicated."
            } else {
                Log.w(TAG, "duplicateSession: backend rejected $sessionId")
                _toastMessage.value = "Duplicate failed."
            }
        }
    }

    /**
     * Rewind a session: drop the last [dropLastN] visible messages.
     * The session must be closed first (backend rejects truncate on live
     * sessions, including the orchestrator). [explicitLocalId] is the live
     * pool's local_id for [sessionId] when known by the caller — required
     * for orchestrator rewinds, since `_sdkToLocalId` only reliably tracks
     * agent sessions.
     */
    fun truncateSession(
        sessionId: String,
        dropLastN: Int,
        explicitLocalId: String? = null,
    ) {
        viewModelScope.launch {
            // Close the live session first if we can identify one. Prefer the
            // explicit local_id (from the active bucket); fall back to the
            // SDK→local map for callers that only know the JSONL id.
            val localId = explicitLocalId ?: _sdkToLocalId.value[sessionId]
            if (localId != null) {
                apiClient?.closePoolSession(localId)
                _liveSessionIds.update { it - sessionId }
                _sdkToLocalId.update { it - sessionId }
            }

            val ok = apiClient?.truncateSession(sessionId, dropLastN) ?: false
            if (!ok) {
                Log.w(TAG, "truncateSession: backend rejected $sessionId drop=$dropLastN")
                _toastMessage.value = "Rewind failed — session may still be open."
                return@launch
            }

            // If we were displaying this session in either bucket, clear it; user
            // can re-open from the session list to see the rewound state.
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

            _toastMessage.value = "Conversation rewound."
            lastRefreshTime = 0L
            refreshSessions()
        }
    }

    /**
     * Fork a session: duplicate, then drop the last [dropLastN] messages in the copy.
     * The original session is untouched.
     */
    fun forkSession(sessionId: String, dropLastN: Int) {
        viewModelScope.launch {
            val newId = apiClient?.forkSession(sessionId, dropLastN)
            if (newId != null) {
                lastRefreshTime = 0L
                refreshSessions()
                _toastMessage.value = "Conversation forked."
            } else {
                Log.w(TAG, "forkSession: backend rejected $sessionId drop=$dropLastN")
                _toastMessage.value = "Fork failed."
            }
        }
    }

    /**
     * Rewind the *currently displayed* session at the UI-visible message
     * position [uiIndex]. Converts to a bottom-relative drop count so the
     * action survives pagination — the frontend may only have the most
     * recent page loaded, so an absolute top-index would be unreliable.
     *
     * Passes the bucket's live local_id through to [truncateSession] so the
     * orchestrator path (which isn't reliably indexed in `_sdkToLocalId`)
     * can still close the pool session before the truncate call.
     */
    fun rewindCurrentSessionAt(uiIndex: Int) {
        val b = activeBucket()
        val sessionId = b.jsonlSessionId ?: b.currentSessionIdForPagination ?: b.currentSessionId.value
        if (sessionId == null) {
            Log.w(TAG, "rewindCurrentSessionAt: no session id on active bucket")
            return
        }
        val total = b.messages.value.size
        val dropLastN = (total - 1 - uiIndex).coerceAtLeast(0)
        truncateSession(sessionId, dropLastN, explicitLocalId = b.currentLocalId.value)
    }

    /**
     * Fork the *currently displayed* session at the UI-visible message
     * position [uiIndex]. See [rewindCurrentSessionAt] for the index
     * mapping; the original session is left untouched.
     */
    fun forkCurrentSessionAt(uiIndex: Int) {
        val b = activeBucket()
        val sessionId = b.jsonlSessionId ?: b.currentSessionIdForPagination ?: b.currentSessionId.value
        if (sessionId == null) {
            Log.w(TAG, "forkCurrentSessionAt: no session id on active bucket")
            return
        }
        val total = b.messages.value.size
        val dropLastN = (total - 1 - uiIndex).coerceAtLeast(0)
        forkSession(sessionId, dropLastN)
    }

    // Recording
    fun startRecording() {
        viewModelScope.launch {
            val success = audioRecorder.startRecording()
            if (success) {
                _isRecording.value = true
            } else {
                val errorMessage = ChatMessage(
                    role = MessageRole.SYSTEM,
                    content = "Failed to start recording. Check microphone permission."
                )
                activeBucket().messages.update { it + errorMessage }
            }
        }
    }

    fun stopRecording() {
        viewModelScope.launch {
            val base64Audio = audioRecorder.stopRecording()
            _isRecording.value = false

            if (base64Audio != null) {
                val userMessage = ChatMessage(
                    role = MessageRole.USER,
                    content = "[Voice message]",
                    blocks = listOf(MessageBlock.Text("[Voice message]"))
                )
                activeBucket().messages.update { it + userMessage }

                // Send audio to server — route to whichever socket owns the current chat tab.
                webSocketManager.send(
                    WebSocketMessage.SendAudio(base64Audio, "wav"),
                    endpoint = currentEndpoint()
                )
            }
        }
    }

    // Voice session controls (WebRTC)
    fun startVoiceSession() {
        // Voice only works with orchestrator sessions
        if (!_isOrchestratorSession.value) {
            _voiceState.value = VoiceState.Error("Voice only available for orchestrator sessions")
            return
        }

        val vm = voiceManager
        if (vm == null) {
            _voiceState.value = VoiceState.Error("Voice manager not initialized")
            return
        }

        // Pause wake word detection while voice session is active — the mic is owned
        // by WebRTC and we don't want keywords triggering extra recordings or new sessions.
        AssistantService.pauseWakeWord(getApplication())

        val orchBucket = bucket(WebSocketEndpoint.ORCHESTRATOR)
        viewModelScope.launch {
            // Fetch the backend's configured voice defaults — provider,
            // model, voice, transcription language.  The Android app
            // doesn't carry its own preferences; the source of truth is
            // assistant_config.json (toggled from the web frontend).
            val cfg = apiClient!!.getVoiceConfig()

            // Send voice_start with the orchestrator bucket's local_id and the true
            // JSONL session id so the backend resumes from the correct history file.
            // The voice fields come from the backend config we just fetched —
            // sending them explicitly keeps the WS handler and the REST endpoint
            // pinned to the same values for this session.
            webSocketManager.send(
                WebSocketMessage.VoiceStart(
                    localId = orchBucket.currentLocalId.value,
                    resumeSdkId = orchBucket.jsonlSessionId ?: orchBucket.currentSessionId.value,
                    voiceProvider = cfg.provider,
                    voiceModel = cfg.model,
                    voiceName = cfg.voice,
                    voiceTranscriptionLanguage = cfg.transcriptionLanguage,
                    voiceEndpoint = cfg.endpoint.takeIf { it.isNotBlank() },
                ),
                endpoint = WebSocketEndpoint.ORCHESTRATOR
            )
            // Then connect using the matching transport (WebRTC for OpenAI,
            // WebSocket for Qwen, etc).
            vm.start(cfg)
        }
    }

    // Job for the "Ending..." → Off safety timeout. The backend emits
    // VoiceEnded once teardown finishes; this fires only if the ack
    // never arrives so the UI doesn't stay stuck on "Ending" forever.
    private var endingTimeoutJob: kotlinx.coroutines.Job? = null
    private val ENDING_ACK_TIMEOUT_MS = 5000L

    /**
     * User-initiated stop: ask the backend to tear the session down and
     * show "Ending..." until VoiceEnded arrives. The previous design
     * tore down locally immediately and flipped to Off, which masked
     * backend hangs and caused "frontend shows ended but backend still
     * running" desync. The full local cleanup now happens in
     * [finalizeVoiceStop] when the ack arrives (or the safety timeout
     * fires).
     */
    fun stopVoiceSession() {
        // Tell the backend we want to stop. The session_stopped reply
        // happens on the WS route handler; the voice_ended broadcast is
        // what we wait for here to confirm the relay is actually down.
        webSocketManager.send(
            WebSocketMessage.Stop,
            endpoint = WebSocketEndpoint.ORCHESTRATOR,
        )
        _voiceState.value = VoiceState.Ending
        // Safety timeout — covers server crash, dropped WS, etc.
        endingTimeoutJob?.cancel()
        endingTimeoutJob = viewModelScope.launch {
            kotlinx.coroutines.delay(ENDING_ACK_TIMEOUT_MS)
            Log.w(TAG, "voice_ended ack timeout — forcing local stop")
            finalizeVoiceStop()
        }
    }

    /**
     * Local teardown of the voice session. Called when the backend
     * confirms teardown ([WebSocketEvent.VoiceEnded] / legacy
     * [WebSocketEvent.VoiceStopped]) or when the safety timeout fires.
     * Idempotent — safe to call after the session is already Off.
     */
    private fun finalizeVoiceStop() {
        endingTimeoutJob?.cancel()
        endingTimeoutJob = null
        viewModelScope.launch {
            voiceManager?.stop()
            _voiceState.value = VoiceState.Off
            _isMuted.value = false
            // Wait for WebRTC to release the mic before re-arming wake word.
            // Without this delay, AudioRecord fails 20+ times with "other input already
            // started" — the WebRTC AudioRecord is still held by the system even after
            // stop() returns, causing the wake word detector process to crash.
            kotlinx.coroutines.delay(1500L)
            AssistantService.resumeWakeWord(getApplication())
        }
    }

    fun toggleMute() {
        val newMuteState = voiceManager?.toggleMute() ?: !_isMuted.value
        _isMuted.value = newMuteState
    }

    // Network discovery
    fun scanForServers() {
        if (_isScanning.value) return
        viewModelScope.launch {
            _isScanning.value = true
            _discoveredServers.value = emptyList()
            try {
                val servers = NetworkScanner.scan(getApplication())
                _discoveredServers.value = servers
                // Auto-connect to first discovered server only if using the default URL
                // (don't overwrite a user-configured server URL)
                val currentUrl = _settings.value.serverUrl
                val defaultUrl = AppSettings().serverUrl
                if (servers.isNotEmpty() && connectionState.value !is ConnectionState.Connected && currentUrl == defaultUrl) {
                    connectToDiscoveredServer(servers.first())
                }
            } finally {
                _isScanning.value = false
            }
        }
    }

    fun connectToDiscoveredServer(server: DiscoveredServer) {
        viewModelScope.launch {
            dataStore.edit { preferences ->
                preferences[PreferenceKeys.SERVER_URL] = server.wsUrl
            }
            // connect() will be triggered by settings update via DataStore flow
        }
    }

    // Settings
    fun updateServerUrl(url: String) {
        viewModelScope.launch {
            dataStore.edit { preferences ->
                preferences[PreferenceKeys.SERVER_URL] = url
            }
        }
    }

    fun addSavedServer(label: String, url: String) {
        val cleanLabel = label.trim()
        val cleanUrl = url.trim()
        if (cleanLabel.isEmpty() || cleanUrl.isEmpty()) return
        viewModelScope.launch {
            dataStore.edit { preferences ->
                val existing = decodeSavedServers(preferences[PreferenceKeys.SAVED_SERVERS])
                // Replace any entry with the same url, else append.
                val updated = existing.filterNot { it.url == cleanUrl } + SavedServer(cleanLabel, cleanUrl)
                preferences[PreferenceKeys.SAVED_SERVERS] = encodeSavedServers(updated)
            }
        }
    }

    fun removeSavedServer(url: String) {
        viewModelScope.launch {
            dataStore.edit { preferences ->
                val existing = decodeSavedServers(preferences[PreferenceKeys.SAVED_SERVERS])
                val updated = existing.filterNot { it.url == url }
                preferences[PreferenceKeys.SAVED_SERVERS] = encodeSavedServers(updated)
            }
        }
    }

    fun selectSavedServer(server: SavedServer) {
        viewModelScope.launch {
            dataStore.edit { preferences ->
                preferences[PreferenceKeys.SERVER_URL] = server.url
            }
        }
    }

    fun updateThemeMode(mode: ThemeMode) {
        viewModelScope.launch {
            dataStore.edit { preferences ->
                preferences[PreferenceKeys.THEME_MODE] = mode.name
            }
        }
    }

    fun updateAutoConnect(enabled: Boolean) {
        viewModelScope.launch {
            dataStore.edit { preferences ->
                preferences[PreferenceKeys.AUTO_CONNECT] = enabled
            }
        }
    }

    fun updateMicGainLevel(level: Float) {
        viewModelScope.launch {
            dataStore.edit { preferences ->
                preferences[PreferenceKeys.MIC_GAIN_LEVEL] = level.coerceIn(0.0f, 1.5f)
            }
            voiceManager?.setMicGain(level)
        }
    }

    fun updateEchoDuckingGain(gain: Float) {
        viewModelScope.launch {
            dataStore.edit { preferences ->
                preferences[PreferenceKeys.ECHO_DUCKING_GAIN] = gain.coerceIn(0.0f, 1.0f)
            }
            voiceManager?.setEchoDuckingGain(gain)
        }
    }

    fun updateWakeWordMicGainLevel(level: Float) {
        viewModelScope.launch {
            dataStore.edit { preferences ->
                preferences[PreferenceKeys.WAKE_WORD_MIC_GAIN_LEVEL] = level.coerceIn(0.0f, 1.5f)
            }
            // Apply to wake word detector via AssistantService (restart with new gain)
            val s = _settings.value
            if (s.enableWakeWord) {
                AssistantService.updateWakeWord(getApplication(), true, s.wakeWord, s.voiceWord, level)
            }
        }
    }

    fun updateAudioOutput(output: AudioOutput) {
        viewModelScope.launch {
            dataStore.edit { preferences ->
                preferences[PreferenceKeys.AUDIO_OUTPUT] = output.name
            }
            // Apply immediately to VoiceManager so next session picks it up
            voiceManager?.setAudioOutput(output)
        }
    }

    /**
     * Whether a Bluetooth audio output device is currently available (paired + connected).
     * UI should call this to decide whether to enable the BLUETOOTH segment. Safe to call
     * on any thread; returns false if VoiceManager hasn't been initialized yet.
     */
    fun isBluetoothAudioAvailable(): Boolean =
        voiceManager?.isBluetoothAudioAvailable() == true

    /**
     * Whether a wired 3.5mm headphone/headset is currently plugged in.
     * UI should call this to decide whether to enable the WIRED segment.
     */
    fun isWiredHeadphoneAvailable(): Boolean =
        voiceManager?.isWiredHeadphoneAvailable() == true

    fun updateSpeakerVolumeLevel(level: Float) {
        viewModelScope.launch {
            val clamped = level.coerceIn(0.0f, 1.5f)
            dataStore.edit { preferences ->
                preferences[PreferenceKeys.SPEAKER_VOLUME_LEVEL] = clamped
            }
            // Apply to system audio
            val audioManager = getApplication<Application>().getSystemService(Context.AUDIO_SERVICE) as AudioManager
            val maxVolume = audioManager.getStreamMaxVolume(AudioManager.STREAM_MUSIC)
            val newVolume = (clamped * maxVolume).toInt().coerceIn(0, maxVolume)
            audioManager.setStreamVolume(AudioManager.STREAM_MUSIC, newVolume, 0)
        }
    }

    fun updateEnableButtonTrigger(enabled: Boolean) {
        viewModelScope.launch {
            dataStore.edit { preferences ->
                preferences[PreferenceKeys.ENABLE_BUTTON_TRIGGER] = enabled
            }
            // Write to shared prefs so ButtonAccessibilityService can read it without a Context ref
            getApplication<Application>().getSharedPreferences("assistant_service_prefs", Context.MODE_PRIVATE)
                .edit().putBoolean("button_trigger_enabled", enabled).apply()
        }
    }

    fun updateEnableWakeWord(enabled: Boolean) {
        viewModelScope.launch {
            dataStore.edit { preferences ->
                preferences[PreferenceKeys.ENABLE_WAKE_WORD] = enabled
            }
            val s = _settings.value
            AssistantService.updateWakeWord(getApplication(), enabled, s.wakeWord, s.voiceWord, s.wakeWordMicGainLevel)
        }
    }

    fun updateWakeWord(word: String) {
        viewModelScope.launch {
            dataStore.edit { preferences ->
                preferences[PreferenceKeys.WAKE_WORD] = word
            }
            val s = _settings.value
            if (s.enableWakeWord) {
                AssistantService.updateWakeWord(getApplication(), true, word, s.voiceWord, s.wakeWordMicGainLevel)
            }
        }
    }

    fun updateVoiceWord(word: String) {
        viewModelScope.launch {
            dataStore.edit { preferences ->
                preferences[PreferenceKeys.VOICE_WORD] = word
            }
            val s = _settings.value
            if (s.enableWakeWord) {
                AssistantService.updateWakeWord(getApplication(), true, s.wakeWord, word, s.wakeWordMicGainLevel)
            }
        }
    }

    // ─────────────────────────────────────────────────────────────────
    // System (backend) configuration — System Settings tab
    // ─────────────────────────────────────────────────────────────────

    /**
     * Load the full backend system config (assistant config + MCP servers +
     * model catalog + voice models + session providers + Qwen harness models).
     *
     * Each sub-list is fetched in parallel. Failures of individual lists fall
     * through to empty defaults — only a full failure to load the main config
     * surfaces as an error.
     */
    fun loadSystemConfig() {
        val client = apiClient
        if (client == null) {
            _systemConfig.value = _systemConfig.value.copy(
                error = "Not connected to a server",
                loading = false,
            )
            return
        }
        viewModelScope.launch {
            _systemConfig.value = _systemConfig.value.copy(loading = true, error = null)
            try {
                // Fan out — these don't depend on each other.
                val cfgDef = async(Dispatchers.IO) { client.getAssistantConfig() }
                val mcpDef = async(Dispatchers.IO) { client.listMcpServers() }
                val modelsDef = async(Dispatchers.IO) { client.listOrchestratorModels() }
                val voiceDef = async(Dispatchers.IO) { client.listVoiceModels() }
                val qwenDef = async(Dispatchers.IO) { client.listQwenHarnessModels() }
                val providersDef = async(Dispatchers.IO) { client.listSessionProviders() }

                val cfg = cfgDef.await()
                if (cfg == null) {
                    _systemConfig.value = _systemConfig.value.copy(
                        loading = false,
                        error = "Failed to load configuration",
                    )
                    return@launch
                }
                // Merge dynamic Gemini Live list into static voice providers.
                // The endpoint (vertex / aistudio) decides which Google backend
                // the catalog is fetched from — mirrors the web ConfigPage.
                val googleVoice = client.listGoogleVoiceModels(cfg.defaultVoiceEndpoint)
                val voiceProviders = voiceDef.await().toMutableMap()
                if (googleVoice.isNotEmpty()) voiceProviders["google"] = googleVoice

                // Auto-correct: if the saved Gemini model is no longer in the
                // discovered catalog (Google renames Live ids periodically),
                // write through to the new default and surface a banner.
                val (correctedCfg, correction) = maybeAutoCorrectVoiceModel(
                    client, cfg, googleVoice,
                )

                _systemConfig.value = SystemConfigState(
                    config = correctedCfg,
                    mcpServers = mcpDef.await(),
                    models = modelsDef.await(),
                    voiceProviders = voiceProviders,
                    qwenHarnessModels = qwenDef.await(),
                    sessionProviders = providersDef.await(),
                    loading = false,
                    voiceModelAutoCorrected = correction,
                )
            } catch (e: Exception) {
                Log.e(TAG, "loadSystemConfig error: ${e.message}", e)
                _systemConfig.value = _systemConfig.value.copy(
                    loading = false,
                    error = e.message ?: "Failed to load configuration",
                )
            }
        }
    }

    /**
     * Apply a partial update to the backend config. The full updated config
     * is returned and stored on success; on failure the current state is left
     * unchanged and an error message is surfaced.
     */
    fun updateSystemConfig(patch: ConfigPatch) {
        val client = apiClient ?: return
        viewModelScope.launch {
            val prevEndpoint = _systemConfig.value.config?.defaultVoiceEndpoint
            _systemConfig.value = _systemConfig.value.copy(saving = true, error = null)
            val result = client.updateAssistantConfig(patch)
            result.fold(
                onSuccess = { newCfg ->
                    // Refetch the Google voice catalog when the user flips the
                    // backend, since the model list is per-endpoint. Mirrors
                    // the web ConfigPage useEffect.
                    var effectiveCfg = newCfg
                    var correction: VoiceModelAutoCorrection? = _systemConfig.value.voiceModelAutoCorrected
                    val voiceProviders = if (
                        newCfg.defaultVoiceEndpoint != prevEndpoint
                    ) {
                        val googleVoice = client.listGoogleVoiceModels(newCfg.defaultVoiceEndpoint)
                        val merged = _systemConfig.value.voiceProviders.toMutableMap()
                        if (googleVoice.isNotEmpty()) {
                            merged["google"] = googleVoice
                            // The newly-fetched catalog may not include the
                            // saved model id (especially after flipping
                            // Vertex↔AI Studio, where the canonical id
                            // differs). Auto-correct here too.
                            val (corrected, c) = maybeAutoCorrectVoiceModel(
                                client, newCfg, googleVoice,
                            )
                            effectiveCfg = corrected
                            if (c != null) correction = c
                        }
                        merged
                    } else {
                        _systemConfig.value.voiceProviders
                    }
                    _systemConfig.value = _systemConfig.value.copy(
                        config = effectiveCfg,
                        voiceProviders = voiceProviders,
                        saving = false,
                        savedFlash = true,
                        voiceModelAutoCorrected = correction,
                    )
                    savedFlashJob?.cancel()
                    savedFlashJob = viewModelScope.launch {
                        kotlinx.coroutines.delay(2000)
                        _systemConfig.value = _systemConfig.value.copy(savedFlash = false)
                    }
                },
                onFailure = { e ->
                    _systemConfig.value = _systemConfig.value.copy(
                        saving = false,
                        error = e.message ?: "Failed to save",
                    )
                },
            )
        }
    }

    fun dismissVoiceModelAutoCorrected() {
        _systemConfig.value = _systemConfig.value.copy(voiceModelAutoCorrected = null)
    }

    /**
     * Snap the saved Gemini Live model to the discovered default when
     * the catalog no longer lists it. Returns the (possibly updated)
     * config and the correction record (null = no change needed).
     *
     * Empty catalog → no-op: we only correct when we have a known-good
     * list. Failures fall through silently, leaving the saved value as
     * the source of truth.
     */
    private suspend fun maybeAutoCorrectVoiceModel(
        client: ApiClient,
        cfg: AssistantConfig,
        discovered: List<VoiceModelEntry>,
    ): Pair<AssistantConfig, VoiceModelAutoCorrection?> {
        if (cfg.defaultVoiceProvider != "google") return cfg to null
        if (discovered.isEmpty()) return cfg to null
        if (discovered.any { it.id == cfg.defaultVoiceModel }) return cfg to null
        val newDefault = discovered.firstOrNull { it.isDefault } ?: discovered.first()
        val voiceListed = newDefault.voices.any { it.id == cfg.defaultVoiceName }
        val patch = ConfigPatch(
            defaultVoiceModel = newDefault.id,
            defaultVoiceName = if (voiceListed) null else newDefault.voice,
        )
        val result = client.updateAssistantConfig(patch)
        return result.fold(
            onSuccess = { updated ->
                updated to VoiceModelAutoCorrection(
                    from = cfg.defaultVoiceModel,
                    to = newDefault.id,
                )
            },
            onFailure = { e ->
                Log.w(TAG, "auto-correct voice model failed: ${e.message}")
                cfg to null
            },
        )
    }

    /** Toggle a single MCP server in `enabled_mcps`. */
    fun toggleMcp(name: String) {
        val cfg = _systemConfig.value.config ?: return
        val next = cfg.enabledMcps.toMutableList()
        if (next.contains(name)) next.remove(name) else next.add(name)
        updateSystemConfig(ConfigPatch(enabledMcps = next))
    }

    /**
     * Play a short two-tone cue (~300ms total) signalling that the voice
     * upstream is about to cycle. The first attempt at this (single tone
     * on STREAM_NOTIFICATION, 2026-06-04) was inaudible on the A300M
     * during an active call because the notification stream is
     * effectively muted by the call audio plane. Switched to STREAM_MUSIC
     * which is the same stream the voice playback is using — guaranteed
     * audible whenever the agent's voice is.
     *
     * Two tones (880Hz → 660Hz) chosen because a single beep blended too
     * easily with the agent's own speech. The descending interval is a
     * clear "wrap up" cue without sounding alarming.
     *
     * Fire-and-forget — we don't block the caller. If audio init fails
     * (very old device, unusual route, etc.) we log and continue; the
     * banner still appears.
     */
    private fun playReconnectBeep() {
        viewModelScope.launch(kotlinx.coroutines.Dispatchers.IO) {
            try {
                val sr = 22050
                val toneMs = 130
                val gapMs = 40
                val toneFrames = sr * toneMs / 1000
                val gapFrames = sr * gapMs / 1000
                val totalFrames = toneFrames * 2 + gapFrames
                val pcm = ShortArray(totalFrames)
                val fadeFrames = sr * 15 / 1000
                // Volume — 50% amplitude. We're sharing the music stream
                // with the agent's voice (which clips at 100%) so being
                // distinctly audible matters more than being polite.
                val amplitude = 0.50
                fun fillTone(offset: Int, freq: Double) {
                    val twoPiF = 2.0 * Math.PI * freq
                    for (i in 0 until toneFrames) {
                        val env = when {
                            i < fadeFrames -> i.toDouble() / fadeFrames
                            i > toneFrames - fadeFrames -> (toneFrames - i).toDouble() / fadeFrames
                            else -> 1.0
                        }
                        val sample = (Math.sin(twoPiF * i / sr) * env * amplitude * Short.MAX_VALUE).toInt()
                        pcm[offset + i] = sample.toShort()
                    }
                }
                fillTone(0, 880.0)
                // Gap stays as zero-filled silence.
                fillTone(toneFrames + gapFrames, 660.0)

                val bufSize = AudioTrack.getMinBufferSize(
                    sr, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT
                ).coerceAtLeast(totalFrames * 2)
                @Suppress("DEPRECATION")
                val track = AudioTrack(
                    AudioManager.STREAM_MUSIC,
                    sr,
                    AudioFormat.CHANNEL_OUT_MONO,
                    AudioFormat.ENCODING_PCM_16BIT,
                    bufSize,
                    AudioTrack.MODE_STATIC,
                )
                if (track.state != AudioTrack.STATE_INITIALIZED) {
                    Log.w(TAG, "playReconnectBeep: AudioTrack init failed state=${track.state}")
                    track.release()
                    return@launch
                }
                val written = track.write(pcm, 0, totalFrames)
                if (written < 0) {
                    Log.w(TAG, "playReconnectBeep: AudioTrack write failed code=$written")
                } else {
                    Log.i(TAG, "playReconnectBeep: starting tone (frames=$totalFrames, stream=STREAM_MUSIC)")
                }
                track.play()
                val playMs = ((toneFrames * 2 + gapFrames) * 1000L) / sr
                kotlinx.coroutines.delay(playMs + 80)
                try { track.stop() } catch (_: Exception) {}
                track.release()
            } catch (e: Exception) {
                Log.w(TAG, "playReconnectBeep failed: ${e.message}", e)
            }
        }
    }

    override fun onCleared() {
        super.onCleared()
        voiceManager?.release()
        webSocketManager.release()
        audioRecorder.release()
    }
}
