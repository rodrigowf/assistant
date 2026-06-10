package com.assistant.peripheral.viewmodel

import android.app.Application
import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import android.util.Log
import com.assistant.peripheral.audio.AudioRecorder
import com.assistant.peripheral.chat.ChatController
import com.assistant.peripheral.data.*
import com.assistant.peripheral.network.ApiClient
import com.assistant.peripheral.network.DiscoveredServer
import com.assistant.peripheral.network.WebSocketEndpoint
import com.assistant.peripheral.voice.VoiceConfig
import com.assistant.peripheral.network.WebSocketManager
import com.assistant.peripheral.service.AssistantService
import com.assistant.peripheral.voice.VoiceEvent
import com.assistant.peripheral.voice.VoiceManager
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.flow.*
import kotlinx.coroutines.launch
import java.util.UUID

// DataStore I/O lives in SettingsRepository (Inc 1). Chat state + WS event
// router lives in ChatController (Inc 3 of the viewmodel refactor — see
// ~/assistant/context/memory/assistant/plans/android_viewmodel_refactor_plan_2026_06_10.md).
// The ViewModel keeps voice subsystem fields, settings observer, and a thin
// public facade for Compose until Inc 4 (VoiceController) and Inc 7
// (thinning) absorb the rest.

class AssistantViewModel(application: Application) : AndroidViewModel(application) {

    companion object {
        private const val TAG = "AssistantViewModel"
    }

    private val settingsRepository = com.assistant.peripheral.settings.SettingsRepository.create(application)
    private val webSocketManager = WebSocketManager()
    private val audioRecorder = AudioRecorder(application.applicationContext)

    // API client (created when server URL is known)
    private var apiClient: ApiClient? = null

    // Voice manager for WebRTC (created lazily when apiClient is available)
    private var voiceManager: VoiceManager? = null

    private val connectionController = com.assistant.peripheral.connection.OrchestratorConnectionController(
        scope = viewModelScope,
        settingsRepository = settingsRepository,
        webSocketManager = webSocketManager,
        getLivePool = { apiClient?.getLivePool() ?: emptyList() },
        networkScan = { com.assistant.peripheral.network.NetworkScanner.scan(application) }
    )

    private val chatController = ChatController(
        scope = viewModelScope,
        webSocketManager = webSocketManager,
        settingsRepository = settingsRepository,
        connectionController = connectionController,
        listSessions = { apiClient?.listSessions() ?: emptyList() },
        getLivePool = { apiClient?.getLivePool() ?: emptyList() },
        getMessagesPaginated = { sid, limit, beforeIdx ->
            apiClient?.getMessagesPaginated(sid, limit = limit, beforeIndex = beforeIdx)
        },
        closePoolSession = { localId -> apiClient?.closePoolSession(localId) ?: false },
        deleteSession = { sid -> apiClient?.deleteSession(sid) ?: false },
        renameSession = { sid, title -> apiClient?.renameSession(sid, title) ?: false },
        duplicateSession = { sid -> apiClient?.duplicateSession(sid) },
        truncateSession = { sid, drop -> apiClient?.truncateSession(sid, drop) ?: false },
        forkSession = { sid, drop -> apiClient?.forkSession(sid, drop) }
    )

    // Connection state — pass-through from the controller.
    val connectionState: StateFlow<ConnectionState> = connectionController.connectionState

    // Chat state — pass-throughs to ChatController (Inc 3).
    val sessions: StateFlow<List<SessionInfo>> = chatController.sessions
    val sessionsLoading: StateFlow<Boolean> = chatController.sessionsLoading
    val liveSessionIds: StateFlow<Set<String>> = chatController.liveSessionIds
    val isOrchestratorSession: StateFlow<Boolean> = chatController.isOrchestratorSession
    val currentSessionId: StateFlow<String?> = chatController.currentSessionId
    val currentLocalId: StateFlow<String> = chatController.currentLocalId
    val messages: StateFlow<List<ChatMessage>> = chatController.messages
    val hasMoreMessages: StateFlow<Boolean> = chatController.hasMoreMessages
    val sessionStatus: StateFlow<String> = chatController.sessionStatus
    val isLoadingMoreMessages: StateFlow<Boolean> = chatController.isLoadingMoreMessages

    // Recording state
    private val _isRecording = MutableStateFlow(false)
    val isRecording: StateFlow<Boolean> = _isRecording.asStateFlow()

    // Voice state
    private val _voiceState = MutableStateFlow<VoiceState>(VoiceState.Off)
    val voiceState: StateFlow<VoiceState> = _voiceState.asStateFlow()

    // Silero VAD state surfaced from the backend ``voice_vad_state`` broadcast.
    private val _vadState = MutableStateFlow("idle")
    val vadState: StateFlow<String> = _vadState.asStateFlow()
    private val _vadDurationMs = MutableStateFlow(0L)
    val vadDurationMs: StateFlow<Long> = _vadDurationMs.asStateFlow()

    /**
     * The voice config in use for the *current* voice session, or null if voice
     * isn't active. Captured at [startVoiceSession] time and replayed on WS
     * reconnect via the `Reconnected` ConnectionEvent (moves to VoiceController
     * at Inc 4). Cleared in [finalizeVoiceStop].
     */
    private var activeVoiceConfig: VoiceConfig? = null

    /**
     * Inc 7 (voice refactor): idempotency guard for [finalizeVoiceStop].
     * Closes the duplicate-resume-intent race surfaced by voice teardown
     * having three call sites that can fire for the same session-end.
     * Reset in [startVoiceSession].
     */
    private var voiceStopFinalized: Boolean = false

    /**
     * One-shot transient message for the UI to display as a toast.
     */
    private val _toastMessage = MutableStateFlow<String?>(null)
    val toastMessage: StateFlow<String?> = _toastMessage.asStateFlow()

    fun clearToast() {
        _toastMessage.value = null
    }

    init {
        // ChatController's own toast channel (history op feedback) → UI toast.
        viewModelScope.launch {
            chatController.toastMessages.collect { _toastMessage.value = it }
        }
    }

    /**
     * Transient banner for the voice reconnect lifecycle.
     */
    private val _voiceReconnectBanner = MutableStateFlow<String?>(null)
    val voiceReconnectBanner: StateFlow<String?> = _voiceReconnectBanner.asStateFlow()

    private val _isMuted = MutableStateFlow(false)
    val isMuted: StateFlow<Boolean> = _isMuted.asStateFlow()

    val noActiveOrchestrator: StateFlow<Boolean> = connectionController.noActiveOrchestrator

    // Settings — non-nullable view over the SettingsRepository's
    // `StateFlow<AppSettings?>`. Defaults to `AppSettings()` until the
    // repository emits, matching the pre-refactor UI contract.
    private val _settings = MutableStateFlow(AppSettings())
    val settings: StateFlow<AppSettings> = _settings.asStateFlow()

    val discoveredServers: StateFlow<List<DiscoveredServer>> = connectionController.discoveredServers
    val isScanning: StateFlow<Boolean> = connectionController.isScanning

    // System (backend) configuration — drives the System Settings tab.
    // Stays on the ViewModel until Inc 5 (SystemConfigController).
    private val _systemConfig = MutableStateFlow(SystemConfigState())
    val systemConfig: StateFlow<SystemConfigState> = _systemConfig.asStateFlow()
    private var savedFlashJob: kotlinx.coroutines.Job? = null

    init {
        // Observe settings from the repository. First emission restores the
        // persisted orchestrator local_id (so cold start reattaches instead of
        // forking a new UUID) and constructs the ApiClient + VoiceManager
        // against the loaded serverUrl. Subsequent emissions refresh tunables;
        // if the serverUrl changed we tear down session state and rebuild the
        // voice manager. Inc 4 (VoiceController) will absorb the voice glue.
        viewModelScope.launch {
            var previousServerUrl: String? = null
            var firstEmission = true
            settingsRepository.settings.collect { loaded ->
                if (loaded == null) return@collect
                val newServerUrl = loaded.serverUrl
                val serverUrlChanged = previousServerUrl != null && previousServerUrl != newServerUrl
                previousServerUrl = newServerUrl

                if (firstEmission) {
                    firstEmission = false
                    // Restore the persisted orchestrator local_id BEFORE any
                    // WS opens. Without this, each launch generates a fresh
                    // UUID and forks a new orchestrator when getLivePool()
                    // races (e.g. backend slow on cold start).
                    settingsRepository.persistedOrchestratorLocalId()?.let {
                        chatController.setOrchestratorLocalIdForRestore(it)
                    }
                }

                _settings.value = loaded
                // Sync button trigger flag through to SharedPreferences so
                // ButtonAccessibilityService can read it without a Context ref.
                getApplication<Application>().getSharedPreferences("assistant_service_prefs", Context.MODE_PRIVATE)
                    .edit().putBoolean("button_trigger_enabled", loaded.enableButtonTrigger).apply()

                val needNewVoiceManager = voiceManager == null || serverUrlChanged
                if (needNewVoiceManager) {
                    apiClient = ApiClient(loaded.serverUrl)
                    voiceManager?.release()
                    voiceManager = VoiceManager(getApplication(), apiClient!!).also {
                        it.setMicGain(loaded.micGainLevel)
                        it.setEchoDuckingGain(loaded.echoDuckingGain)
                        it.setAudioOutput(loaded.audioOutput)
                    }
                    setupVoiceManagerCallbacks()
                } else {
                    voiceManager?.let {
                        it.setMicGain(loaded.micGainLevel)
                        it.setEchoDuckingGain(loaded.echoDuckingGain)
                        it.setAudioOutput(loaded.audioOutput)
                    }
                }

                // Clear all session state when switching servers.
                if (serverUrlChanged) {
                    webSocketManager.disconnect()
                    settingsRepository.clearOrchestratorLocalId()
                    chatController.onServerUrlChanged()
                }
            }
        }

        // Collect WebSocket events on Dispatchers.Default so JSON parsing and
        // bucket updates don't compete with the UI thread. Chat events go to
        // the controller; voice events stay here until Inc 4.
        viewModelScope.launch(Dispatchers.Default) {
            webSocketManager.events.collect { (endpoint, event) ->
                chatController.handleWebSocketEvent(endpoint, event)
                handleVoiceWebSocketEvent(endpoint, event)
            }
        }

        // Subscribe to ConnectionController events — only the voice-continuity
        // branch lives here now. Chat-side bucket coordination is handled by
        // the controller's own subscription. Inc 4 (VoiceController) absorbs
        // this remaining branch.
        viewModelScope.launch {
            connectionController.events.collect { ev ->
                if (ev is com.assistant.peripheral.connection.ConnectionEvent.Reconnected) {
                    handleReconnectedForVoice(ev)
                }
            }
        }
    }

    private fun handleReconnectedForVoice(ev: com.assistant.peripheral.connection.ConnectionEvent.Reconnected) {
        val voiceCfg = activeVoiceConfig
        if (voiceCfg != null) {
            Log.i(TAG, "WS reconnect during live voice — re-arming via voice_start")
            webSocketManager.send(
                WebSocketMessage.VoiceStart(
                    localId = ev.localId,
                    resumeSdkId = ev.sdkSessionId,
                    voiceProvider = voiceCfg.provider,
                    voiceModel = voiceCfg.model,
                    voiceName = voiceCfg.voice,
                    voiceTranscriptionLanguage = voiceCfg.transcriptionLanguage,
                    voiceEndpoint = voiceCfg.endpoint.takeIf { it.isNotBlank() },
                ),
                endpoint = WebSocketEndpoint.ORCHESTRATOR
            )
        } else {
            webSocketManager.send(
                WebSocketMessage.Start(localId = ev.localId, resumeSdkId = ev.sdkSessionId),
                endpoint = WebSocketEndpoint.ORCHESTRATOR
            )
        }
    }

    /**
     * Voice-bound WS event branches. The ChatController already handled the
     * chat-mutating branches; this method picks up the residual voice
     * forwards. Inc 4 (VoiceController) absorbs this whole method.
     */
    private fun handleVoiceWebSocketEvent(@Suppress("UNUSED_PARAMETER") endpoint: WebSocketEndpoint, event: WebSocketEvent) {
        when (event) {
            is WebSocketEvent.SessionStarted -> {
                // If this is a voice session AND we initiated it, forward the
                // session.update payload to OpenAI (system prompt + tool defs).
                if (event.voiceInitiator) {
                    event.voiceSessionUpdate?.let { update ->
                        voiceManager?.handleBackendCommand(update)
                    }
                }
            }
            is WebSocketEvent.VoiceVadState -> {
                _vadState.value = event.state
                _vadDurationMs.value = event.durationMs
            }
            is WebSocketEvent.VoiceCommand -> {
                @Suppress("UNCHECKED_CAST")
                val command = event.command as? Map<String, Any?> ?: return
                voiceManager?.handleBackendCommand(command)
            }
            is WebSocketEvent.VoiceProviderEvent -> {
                voiceManager?.handleProviderEvent(event.event)
            }
            is WebSocketEvent.VoiceAudioOut -> {
                voiceManager?.pushSpeakerChunk(event.audioBase64)
            }
            is WebSocketEvent.VoiceEnding -> {
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
                // Backend teardown finished. Finalize the in-progress streaming
                // message (TurnComplete never arrives in voice mode), then do
                // the local teardown.
                chatController.finalizeStreamingForVoiceEnd()
                finalizeVoiceStop()
            }
            else -> {
                // Other events were handled by ChatController.
            }
        }
    }

    private fun setupVoiceManagerCallbacks() {
        voiceManager?.let { vm ->
            viewModelScope.launch {
                vm.state.collect { state ->
                    _voiceState.value = state
                    if (state == VoiceState.Active && _voiceReconnectBanner.value != null) {
                        _voiceReconnectBanner.value = null
                    }
                }
            }
            viewModelScope.launch {
                vm.events.collect { event -> handleVoiceEvent(event) }
            }
            vm.setVoiceEventCallback { eventMap ->
                webSocketManager.send(
                    WebSocketMessage.VoiceEvent(eventMap),
                    endpoint = WebSocketEndpoint.ORCHESTRATOR
                )
            }
            vm.setMicChunkCallback { audioB64 ->
                webSocketManager.send(
                    WebSocketMessage.VoiceAudioIn(audioB64),
                    endpoint = WebSocketEndpoint.ORCHESTRATOR
                )
            }
        }
    }

    private fun handleVoiceEvent(event: VoiceEvent) {
        // Voice always belongs to the orchestrator bucket — even if the user
        // is currently looking at a Claude Code session in the agent tab.
        when (event) {
            is VoiceEvent.UserTranscript -> {
                val userMessage = ChatMessage(
                    role = MessageRole.USER,
                    content = "[voice] ${event.text}",
                    blocks = listOf(MessageBlock.Text("[voice] ${event.text}"))
                )
                chatController.appendOrchestratorMessage(userMessage)
            }
            is VoiceEvent.TextComplete -> {
                if (event.text.isNotEmpty()) {
                    val assistantMessage = ChatMessage(
                        role = MessageRole.ASSISTANT,
                        content = event.text,
                        blocks = listOf(MessageBlock.Text(event.text))
                    )
                    chatController.appendOrchestratorMessage(assistantMessage)
                }
            }
            is VoiceEvent.ToolUse -> {
                Log.d(TAG, "Voice tool use: ${event.name}")
            }
            is VoiceEvent.TurnComplete -> {
                chatController.setOrchestratorSessionStatus("idle")
            }
            is VoiceEvent.Error -> {
                Log.e(TAG, "Voice error: ${event.message}")
                chatController.appendOrchestratorMessage(
                    ChatMessage(
                        role = MessageRole.SYSTEM,
                        content = "Voice error: ${event.message}"
                    )
                )
            }
            is VoiceEvent.RoutingFallback -> {
                Log.w(TAG, "Routing fallback: ${event.message}")
                _toastMessage.value = event.message
            }
            is VoiceEvent.ReconnectWarning -> {
                val secs = event.timeLeftSeconds
                _voiceReconnectBanner.value = if (secs != null) {
                    "Pausing in ~${secs}s to reconnect…"
                } else {
                    "Reconnecting shortly…"
                }
                playReconnectBeep()
            }
            is VoiceEvent.Reconnecting -> {
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
                // Streaming assistant text — waiting for TextComplete.
            }
        }
    }

    fun connect() {
        viewModelScope.launch {
            connectionController.connect(chatController.orchestratorCurrentLocalId())
        }
    }

    fun disconnect() {
        connectionController.disconnect()
    }

    fun reconnectIfNeeded() {
        connectionController.reconnectIfNeeded(chatController.orchestratorCurrentLocalId())
    }

    // ─────────────────────────────────────────────────────────────────
    // Chat operations — pass-throughs to ChatController.
    // ─────────────────────────────────────────────────────────────────

    fun sendMessage(text: String) = chatController.sendMessage(text)
    fun interrupt() = chatController.interrupt()
    fun compact() = chatController.compact()
    fun refreshSessions() = chatController.refreshSessions()
    fun closeSession(sessionId: String) = chatController.closeSession(sessionId)
    fun loadSession(sessionId: String, isOrchestrator: Boolean = false, liveLocalId: String? = null) =
        chatController.loadSession(sessionId, isOrchestrator, liveLocalId)
    fun loadMoreMessages() = chatController.loadMoreMessages()
    fun newSession() = chatController.newSession(onNeedsConnect = { connect() })
    fun deleteSession(sessionId: String) = chatController.deleteSessionById(sessionId)
    fun renameSession(sessionId: String, title: String) = chatController.renameSessionById(sessionId, title)
    fun duplicateSession(sessionId: String) = chatController.duplicateSessionById(sessionId)
    fun truncateSession(sessionId: String, dropLastN: Int, explicitLocalId: String? = null) =
        chatController.truncateSessionById(sessionId, dropLastN, explicitLocalId)
    fun forkSession(sessionId: String, dropLastN: Int) = chatController.forkSessionById(sessionId, dropLastN)
    fun rewindCurrentSessionAt(uiIndex: Int) = chatController.rewindCurrentSessionAt(uiIndex)
    fun forkCurrentSessionAt(uiIndex: Int) = chatController.forkCurrentSessionAt(uiIndex)

    // Recording
    fun startRecording() {
        viewModelScope.launch {
            val success = audioRecorder.startRecording()
            if (success) {
                _isRecording.value = true
            } else {
                chatController.appendOrchestratorMessage(
                    ChatMessage(
                        role = MessageRole.SYSTEM,
                        content = "Failed to start recording. Check microphone permission."
                    )
                )
            }
        }
    }

    fun stopRecording() {
        viewModelScope.launch {
            val base64Audio = audioRecorder.stopRecording()
            _isRecording.value = false

            if (base64Audio != null) {
                chatController.appendOrchestratorMessage(
                    ChatMessage(
                        role = MessageRole.USER,
                        content = "[Voice message]",
                        blocks = listOf(MessageBlock.Text("[Voice message]"))
                    )
                )
                webSocketManager.send(
                    WebSocketMessage.SendAudio(base64Audio, "wav"),
                    endpoint = if (chatController.isOrchestratorSession.value)
                        WebSocketEndpoint.ORCHESTRATOR else WebSocketEndpoint.AGENT
                )
            }
        }
    }

    // Voice session controls (WebRTC)
    fun startVoiceSession() {
        if (!chatController.isOrchestratorSession.value) {
            _voiceState.value = VoiceState.Error("Voice only available for orchestrator sessions")
            return
        }

        val vm = voiceManager
        if (vm == null) {
            _voiceState.value = VoiceState.Error("Voice manager not initialized")
            return
        }

        // Pause wake word detection while voice session is active.
        val pauseAck = AssistantService.pauseWakeWord(getApplication())
        voiceStopFinalized = false

        viewModelScope.launch {
            kotlinx.coroutines.withTimeoutOrNull(2_000L) { pauseAck.await() }
                ?: Log.w(TAG, "pauseWakeWord ack timeout — proceeding without confirmed release")
            val cfg = apiClient!!.getVoiceConfig()

            activeVoiceConfig = cfg

            webSocketManager.send(
                WebSocketMessage.VoiceStart(
                    localId = chatController.orchestratorCurrentLocalId(),
                    resumeSdkId = chatController.orchestratorJsonlSessionId()
                        ?: chatController.orchestratorCurrentSessionId(),
                    voiceProvider = cfg.provider,
                    voiceModel = cfg.model,
                    voiceName = cfg.voice,
                    voiceTranscriptionLanguage = cfg.transcriptionLanguage,
                    voiceEndpoint = cfg.endpoint.takeIf { it.isNotBlank() },
                ),
                endpoint = WebSocketEndpoint.ORCHESTRATOR
            )
            vm.start(cfg)
        }
    }

    private var endingTimeoutJob: kotlinx.coroutines.Job? = null
    private val ENDING_ACK_TIMEOUT_MS = 5000L

    /**
     * User-initiated stop: ask the backend to end only the voice connection
     * (keeping the orchestrator session alive in the pool for re-arm) and show
     * "Ending..." until VoiceEnded arrives. The full local cleanup happens in
     * [finalizeVoiceStop].
     */
    fun stopVoiceSession() {
        webSocketManager.send(
            WebSocketMessage.VoiceStop,
            endpoint = WebSocketEndpoint.ORCHESTRATOR,
        )
        _voiceState.value = VoiceState.Ending
        endingTimeoutJob?.cancel()
        endingTimeoutJob = viewModelScope.launch {
            kotlinx.coroutines.delay(ENDING_ACK_TIMEOUT_MS)
            Log.w(TAG, "voice_ended ack timeout — forcing local stop")
            finalizeVoiceStop()
        }
    }

    /**
     * Local teardown of the voice session. Called when the backend confirms
     * teardown ([WebSocketEvent.VoiceEnded] / legacy [WebSocketEvent.VoiceStopped])
     * or when the safety timeout fires. Idempotent.
     */
    private fun finalizeVoiceStop() {
        if (voiceStopFinalized) {
            Log.d(TAG, "finalizeVoiceStop ignored — already finalized for this session")
            return
        }
        voiceStopFinalized = true
        endingTimeoutJob?.cancel()
        endingTimeoutJob = null
        activeVoiceConfig = null
        _vadState.value = "idle"
        _vadDurationMs.value = 0L
        viewModelScope.launch {
            voiceManager?.stop()
            _voiceState.value = VoiceState.Off
            _isMuted.value = false
            // Wait for WebRTC to release the mic before re-arming wake word.
            // Without this delay, AudioRecord fails 20+ times with "other
            // input already started" — the WebRTC AudioRecord is still held
            // by the system even after stop() returns.
            kotlinx.coroutines.delay(1500L)
            val resumeAck = AssistantService.resumeWakeWord(getApplication())
            kotlinx.coroutines.withTimeoutOrNull(2_000L) { resumeAck.await() }
                ?: Log.w(TAG, "resumeWakeWord ack timeout — service may be slow or short-circuited")
        }
    }

    fun toggleMute() {
        val newMuteState = voiceManager?.toggleMute() ?: !_isMuted.value
        _isMuted.value = newMuteState
    }

    // Network discovery — pass-throughs to the controller (Inc 2).
    fun scanForServers() = connectionController.scanForServers()
    fun connectToDiscoveredServer(server: DiscoveredServer) =
        connectionController.connectToDiscoveredServer(server)

    // ─────────────────────────────────────────────────────────────────
    // Settings setters — every one delegates to SettingsRepository. Side
    // effects beyond DataStore (voiceManager updates, etc.) stay here.
    // ─────────────────────────────────────────────────────────────────

    fun updateServerUrl(url: String) {
        viewModelScope.launch { settingsRepository.updateServerUrl(url) }
    }

    fun addSavedServer(label: String, url: String) {
        viewModelScope.launch { settingsRepository.addSavedServer(label, url) }
    }

    fun removeSavedServer(url: String) {
        viewModelScope.launch { settingsRepository.removeSavedServer(url) }
    }

    fun selectSavedServer(server: SavedServer) {
        viewModelScope.launch { settingsRepository.selectSavedServer(server) }
    }

    fun updateThemeMode(mode: ThemeMode) {
        viewModelScope.launch { settingsRepository.updateThemeMode(mode) }
    }

    fun updateAutoConnect(enabled: Boolean) {
        viewModelScope.launch { settingsRepository.updateAutoConnect(enabled) }
    }

    fun updateMicGainLevel(level: Float) {
        viewModelScope.launch {
            settingsRepository.updateMicGainLevel(level)
            voiceManager?.setMicGain(level)
        }
    }

    fun updateEchoDuckingGain(gain: Float) {
        viewModelScope.launch {
            settingsRepository.updateEchoDuckingGain(gain)
            voiceManager?.setEchoDuckingGain(gain)
        }
    }

    fun updateWakeWordMicGainLevel(level: Float) {
        viewModelScope.launch { settingsRepository.updateWakeWordMicGainLevel(level) }
    }

    fun updateAudioOutput(output: AudioOutput) {
        viewModelScope.launch {
            settingsRepository.updateAudioOutput(output)
            voiceManager?.setAudioOutput(output)
        }
    }

    fun isBluetoothAudioAvailable(): Boolean =
        voiceManager?.isBluetoothAudioAvailable() == true

    fun isWiredHeadphoneAvailable(): Boolean =
        voiceManager?.isWiredHeadphoneAvailable() == true

    fun updateSpeakerVolumeLevel(level: Float) {
        viewModelScope.launch { settingsRepository.updateSpeakerVolumeLevel(level) }
    }

    fun updateEnableButtonTrigger(enabled: Boolean) {
        viewModelScope.launch { settingsRepository.updateEnableButtonTrigger(enabled) }
    }

    fun updateEnableWakeWord(enabled: Boolean) {
        viewModelScope.launch { settingsRepository.updateEnableWakeWord(enabled) }
    }

    fun updateTalkWord(word: String) {
        viewModelScope.launch { settingsRepository.updateTalkWord(word) }
    }

    fun updateWakeWord(word: String) {
        viewModelScope.launch { settingsRepository.updateWakeWord(word) }
    }

    // ─────────────────────────────────────────────────────────────────
    // System (backend) configuration — System Settings tab. Stays on the
    // ViewModel until Inc 5 (SystemConfigController).
    // ─────────────────────────────────────────────────────────────────

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
                val googleVoice = client.listGoogleVoiceModels(cfg.defaultVoiceEndpoint)
                val voiceProviders = voiceDef.await().toMutableMap()
                if (googleVoice.isNotEmpty()) voiceProviders["google"] = googleVoice

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

    fun updateSystemConfig(patch: ConfigPatch) {
        val client = apiClient ?: return
        viewModelScope.launch {
            val prevEndpoint = _systemConfig.value.config?.defaultVoiceEndpoint
            _systemConfig.value = _systemConfig.value.copy(saving = true, error = null)
            val result = client.updateAssistantConfig(patch)
            result.fold(
                onSuccess = { newCfg ->
                    var effectiveCfg = newCfg
                    var correction: VoiceModelAutoCorrection? = _systemConfig.value.voiceModelAutoCorrected
                    val voiceProviders = if (
                        newCfg.defaultVoiceEndpoint != prevEndpoint
                    ) {
                        val googleVoice = client.listGoogleVoiceModels(newCfg.defaultVoiceEndpoint)
                        val merged = _systemConfig.value.voiceProviders.toMutableMap()
                        if (googleVoice.isNotEmpty()) {
                            merged["google"] = googleVoice
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

    fun toggleMcp(name: String) {
        val cfg = _systemConfig.value.config ?: return
        val next = cfg.enabledMcps.toMutableList()
        if (next.contains(name)) next.remove(name) else next.add(name)
        updateSystemConfig(ConfigPatch(enabledMcps = next))
    }

    /**
     * Two-tone reconnect cue (~300ms) on STREAM_MUSIC so it's audible while
     * the agent is speaking. Inc 4 will move this into VoiceController.
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
