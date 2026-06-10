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
import kotlinx.coroutines.flow.*
import kotlinx.coroutines.launch

// Post-refactor (Inc 7): a thin coordinator. State lives in five
// controllers — SettingsRepository (Inc 1), OrchestratorConnectionController
// (Inc 2), ChatController (Inc 3), VoiceController (Inc 4),
// SystemConfigController (Inc 5). The ViewModel:
//   - constructs and wires the controllers
//   - exposes their flows to Compose (every public flow is a pass-through
//     or a `stateIn`/`shareIn` projection — no mutable flow fields)
//   - drives the WS event collector that fans out to chat + voice
//   - handles two pieces of Android glue: SharedPreferences mirror for
//     AssistantService's button-trigger flag, and the AudioTrack body
//     for the reconnect beep (Compose-free Android API).

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

    private val systemConfigController = com.assistant.peripheral.system.SystemConfigController(
        scope = viewModelScope,
        getAssistantConfig = { apiClient?.getAssistantConfig() },
        listMcpServers = { apiClient?.listMcpServers() ?: emptyMap() },
        listOrchestratorModels = { apiClient?.listOrchestratorModels() ?: emptyList() },
        listVoiceModels = { apiClient?.listVoiceModels() ?: emptyMap() },
        listQwenHarnessModels = { apiClient?.listQwenHarnessModels() ?: emptyList() },
        listSessionProviders = { apiClient?.listSessionProviders() ?: emptyList() },
        listGoogleVoiceModels = { endpoint -> apiClient?.listGoogleVoiceModels(endpoint) ?: emptyList() },
        updateAssistantConfig = { patch ->
            apiClient?.updateAssistantConfig(patch)
                ?: Result.failure(IllegalStateException("Not connected to a server"))
        },
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

    val voiceState: StateFlow<VoiceState> = voiceController.voiceState
    val voiceReconnectBanner: StateFlow<String?> = voiceController.voiceReconnectBanner
    val vadState: StateFlow<String> = voiceController.vadState
    val vadDurationMs: StateFlow<Long> = voiceController.vadDurationMs
    val isMuted: StateFlow<Boolean> = voiceController.isMuted
    val isRecording: StateFlow<Boolean> = voiceController.isRecording

    /**
     * One-shot transient toast strings for the UI. Merged from chat +
     * voice controllers. Compose collects via LaunchedEffect — there is
     * no "clear" because each emission is a new event, not a held state.
     */
    val toastMessage: SharedFlow<String> = merge(
        chatController.toastMessages,
        voiceController.toastMessages
    ).shareIn(viewModelScope, SharingStarted.Eagerly, replay = 0)

    val noActiveOrchestrator: StateFlow<Boolean> = connectionController.noActiveOrchestrator

    /**
     * Non-null view of the SettingsRepository's `StateFlow<AppSettings?>`.
     * Defaults to `AppSettings()` until the repository emits, matching the
     * pre-refactor UI contract. Compose treats `settings` as always present.
     */
    val settings: StateFlow<AppSettings> = settingsRepository.settings
        .filterNotNull()
        .stateIn(viewModelScope, SharingStarted.Eagerly, AppSettings())

    val discoveredServers: StateFlow<List<DiscoveredServer>> = connectionController.discoveredServers
    val isScanning: StateFlow<Boolean> = connectionController.isScanning

    val systemConfig: StateFlow<SystemConfigState> = systemConfigController.systemConfig

    init {
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

    // Inc 3.5 — conflict-mediated orchestrator entry points.
    val orchestratorConflict: StateFlow<com.assistant.peripheral.chat.OrchestratorConflict?> =
        chatController.orchestratorConflict

    /** One-shot signal: an orchestrator session was actually opened — navigate to Chat. */
    val orchestratorOpenedToChat: SharedFlow<Unit> = chatController.orchestratorOpenedToChat

    fun requestLoadOrchestratorSession(sessionId: String, liveLocalId: String?) =
        chatController.requestLoadOrchestratorSession(sessionId, liveLocalId, onNeedsConnect = { connect() })

    fun requestNewOrchestratorSession() =
        chatController.requestNewOrchestratorSession(onNeedsConnect = { connect() })

    fun resolveOrchestratorConflict(decision: com.assistant.peripheral.chat.OrchestratorConflictResolution) =
        chatController.resolveOrchestratorConflict(decision)
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
    // System (backend) configuration — pass-throughs to SystemConfigController.
    // ─────────────────────────────────────────────────────────────────

    fun loadSystemConfig() = systemConfigController.loadSystemConfig()
    fun updateSystemConfig(patch: ConfigPatch) = systemConfigController.updateSystemConfig(patch)
    fun dismissVoiceModelAutoCorrected() = systemConfigController.dismissVoiceModelAutoCorrected()
    fun toggleMcp(name: String) = systemConfigController.toggleMcp(name)

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
