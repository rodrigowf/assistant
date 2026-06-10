package com.assistant.peripheral.viewmodel

import android.app.Application
import android.content.Context
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
import com.assistant.peripheral.network.WebSocketManager
import com.assistant.peripheral.service.AssistantService
import com.assistant.peripheral.voice.VoiceController
import com.assistant.peripheral.voice.VoiceManager
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.flow.*
import kotlinx.coroutines.launch

// DataStore I/O lives in SettingsRepository (Inc 1). Chat state + WS event
// router lives in ChatController (Inc 3). Voice subsystem (state, lifecycle,
// VoiceManager construction gate, reconnect-beep, push-to-talk recording)
// lives in VoiceController (Inc 4).
// The ViewModel now coordinates controllers, owns the System Config tab
// (Inc 5 will absorb), and exposes a thin pass-through facade for Compose.

class AssistantViewModel(application: Application) : AndroidViewModel(application) {

    companion object {
        private const val TAG = "AssistantViewModel"
    }

    private val settingsRepository = com.assistant.peripheral.settings.SettingsRepository.create(application)
    private val webSocketManager = WebSocketManager()
    private val audioRecorder = AudioRecorder(application.applicationContext)

    // API client (created when server URL is known)
    private var apiClient: ApiClient? = null

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

    private val voiceController = VoiceController(
        scope = viewModelScope,
        webSocketManager = webSocketManager,
        chatController = chatController,
        connectionController = connectionController,
        audioRecorder = audioRecorder,
        voiceManagerFactory = {
            // Voice manager needs the current ApiClient — rebuilt on
            // serverUrlChanged via the settings observer below. The
            // factory closes over `apiClient` so the controller never
            // touches it directly.
            apiClient?.let { VoiceManager(application, it) }
        },
        getVoiceConfig = { apiClient?.getVoiceConfig() },
        pauseWakeWord = { AssistantService.pauseWakeWord(application) },
        resumeWakeWord = { AssistantService.resumeWakeWord(application) },
        playBeep = { playReconnectBeep() }
    )

    // ─────────────────────────────────────────────────────────────────
    // Public facade — pass-throughs to controllers.
    // ─────────────────────────────────────────────────────────────────

    val connectionState: StateFlow<ConnectionState> = connectionController.connectionState

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

    // Voice state — pass-throughs to VoiceController (Inc 4).
    val voiceState: StateFlow<VoiceState> = voiceController.voiceState
    val voiceReconnectBanner: StateFlow<String?> = voiceController.voiceReconnectBanner
    val vadState: StateFlow<String> = voiceController.vadState
    val vadDurationMs: StateFlow<Long> = voiceController.vadDurationMs
    val isMuted: StateFlow<Boolean> = voiceController.isMuted
    val isRecording: StateFlow<Boolean> = voiceController.isRecording

    /** One-shot transient toast string for the UI. */
    private val _toastMessage = MutableStateFlow<String?>(null)
    val toastMessage: StateFlow<String?> = _toastMessage.asStateFlow()

    fun clearToast() {
        _toastMessage.value = null
    }

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
        // Mirror controller toast channels into the UI's _toastMessage flow.
        viewModelScope.launch {
            chatController.toastMessages.collect { _toastMessage.value = it }
        }
        viewModelScope.launch {
            voiceController.toastMessages.collect { _toastMessage.value = it }
        }

        // Observe settings. First emission restores the persisted orchestrator
        // local_id (so cold start reattaches instead of forking a new UUID)
        // and triggers `voiceController.onSettingsChanged` which builds the
        // VoiceManager against the loaded serverUrl. Subsequent emissions
        // refresh tunables; serverUrl changes rebuild apiClient + cascade
        // through chat teardown + voice rebuild.
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
                    settingsRepository.persistedOrchestratorLocalId()?.let {
                        chatController.setOrchestratorLocalIdForRestore(it)
                    }
                }

                _settings.value = loaded
                getApplication<Application>().getSharedPreferences("assistant_service_prefs", Context.MODE_PRIVATE)
                    .edit().putBoolean("button_trigger_enabled", loaded.enableButtonTrigger).apply()

                // Rebuild ApiClient on serverUrlChanged (or first emission).
                if (apiClient == null || serverUrlChanged) {
                    apiClient = ApiClient(loaded.serverUrl)
                }
                voiceController.onSettingsChanged(loaded)

                if (serverUrlChanged) {
                    webSocketManager.disconnect()
                    settingsRepository.clearOrchestratorLocalId()
                    chatController.onServerUrlChanged()
                }
            }
        }

        // Collect WS events on Dispatchers.Default so JSON parsing and
        // bucket updates don't compete with the UI thread. Chat events go
        // to ChatController; voice-bound events go to VoiceController.
        viewModelScope.launch(Dispatchers.Default) {
            webSocketManager.events.collect { (endpoint, event) ->
                chatController.handleWebSocketEvent(endpoint, event)
                voiceController.handleVoiceWebSocketEvent(event)
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

    // ─────────────────────────────────────────────────────────────────
    // Voice operations — pass-throughs to VoiceController (Inc 4).
    // ─────────────────────────────────────────────────────────────────

    fun startRecording() = voiceController.startRecording()
    fun stopRecording() = voiceController.stopRecording()
    fun startVoiceSession() = voiceController.startVoiceSession()
    fun stopVoiceSession() = voiceController.stopVoiceSession()
    fun toggleMute() = voiceController.toggleMute()

    fun isBluetoothAudioAvailable(): Boolean = voiceController.isBluetoothAudioAvailable()
    fun isWiredHeadphoneAvailable(): Boolean = voiceController.isWiredHeadphoneAvailable()

    // Network discovery — pass-throughs to the connection controller (Inc 2).
    fun scanForServers() = connectionController.scanForServers()
    fun connectToDiscoveredServer(server: DiscoveredServer) =
        connectionController.connectToDiscoveredServer(server)

    // ─────────────────────────────────────────────────────────────────
    // Settings setters — delegate to SettingsRepository. Side effects
    // beyond DataStore (voice manager + audio router updates) live in
    // VoiceController via [VoiceController.onSettingsChanged].
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
        viewModelScope.launch { settingsRepository.updateMicGainLevel(level) }
        // Side-effect — voice manager rebuild gate consumes the settings
        // flow and refreshes the live mic gain. The setter delegate above
        // emits a new AppSettings; VoiceController.onSettingsChanged picks
        // it up via the settings observer (Inc 4).
    }

    fun updateEchoDuckingGain(gain: Float) {
        viewModelScope.launch { settingsRepository.updateEchoDuckingGain(gain) }
    }

    fun updateWakeWordMicGainLevel(level: Float) {
        viewModelScope.launch { settingsRepository.updateWakeWordMicGainLevel(level) }
    }

    fun updateAudioOutput(output: AudioOutput) {
        viewModelScope.launch { settingsRepository.updateAudioOutput(output) }
    }

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
     * the agent is speaking. Inc 4 moved the trigger (ReconnectWarning →
     * playBeep) into VoiceController; the AudioTrack body stays here as a
     * lambda dep so Robolectric tests don't need to mount audio HW.
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
        voiceController.release()
        webSocketManager.release()
        audioRecorder.release()
    }
}
