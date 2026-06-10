package com.assistant.peripheral.voice

import android.util.Log
import com.assistant.peripheral.audio.AudioRecorder
import com.assistant.peripheral.chat.ChatController
import com.assistant.peripheral.connection.ConnectionEvent
import com.assistant.peripheral.connection.OrchestratorConnectionController
import com.assistant.peripheral.data.AppSettings
import com.assistant.peripheral.data.ChatMessage
import com.assistant.peripheral.data.MessageBlock
import com.assistant.peripheral.data.MessageRole
import com.assistant.peripheral.data.VoiceState
import com.assistant.peripheral.data.WebSocketEvent
import com.assistant.peripheral.data.WebSocketMessage
import com.assistant.peripheral.network.WebSocketEndpoint
import com.assistant.peripheral.network.WebSocketManager
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Owns the voice subsystem: the [VoiceManager] instance, voice state flows,
 * active config, the `voiceStopFinalized` dedupe guard, the WS-event voice
 * branches, the reconnect-beep AudioTrack lifecycle, push-to-talk recording,
 * and the `Reconnected` event subscription for voice continuity. Increment 4
 * of the viewmodel refactor.
 *
 * Refactor base: HEAD `4a53da7` ("Inc 3 — ChatController"). Pinned source
 * ranges from AssistantViewModel.kt at that SHA:
 *   - L94-95   _isRecording state
 *   - L98-105  _voiceState / _vadState / _vadDurationMs flows
 *   - L113-121 activeVoiceConfig, voiceStopFinalized
 *   - L143-147 _voiceReconnectBanner / _isMuted
 *   - L199-216 needNewVoiceManager rebuild gate (moves here)
 *   - L249-271 handleReconnectedForVoice (voice continuity branch)
 *   - L278-326 handleVoiceWebSocketEvent (voice WS forwards)
 *   - L329-355 setupVoiceManagerCallbacks
 *   - L357-440 handleVoiceEvent
 *   - L467-503 startRecording / stopRecording
 *   - L506-545 startVoiceSession
 *   - L546-580 stopVoiceSession + ENDING_ACK_TIMEOUT_MS
 *   - L571-600 finalizeVoiceStop
 *   - L602-606 toggleMute
 *   - L789-870 playReconnectBeep AudioTrack
 *
 * Design notes:
 *
 *  - [voiceManagerFactory] returns a fresh [VoiceManager] each time it's
 *    called. The controller calls it once on first construction and again
 *    on `serverUrlChanged` (the `needNewVoiceManager` gate). The ViewModel
 *    builds the ApiClient + factory; the controller never touches ApiClient
 *    directly. Same function-dep pattern as Inc 2/3.
 *  - [playBeep] is a function-typed dep so tests can verify the
 *    `ReconnectBeepParity` contract without mounting an AudioTrack. The
 *    actual AudioTrack implementation lives in the ViewModel's wiring; the
 *    controller just calls the lambda on `ReconnectWarning`. (Plan §10.4
 *    ReconnectBeepParity test the controller drives the beep — the beep
 *    body itself isn't behavioural state to pin here.)
 *  - [pauseWakeWord] / [resumeWakeWord] are function-typed deps because
 *    `AssistantService.pauseWakeWord(context)` requires a `Context`. The
 *    ViewModel closes over the Application context when constructing the
 *    controller.
 *  - The controller subscribes to
 *    [OrchestratorConnectionController.events] for `Reconnected` (voice
 *    continuity re-arm) — pinned from the ViewModel's
 *    `handleReconnectedForVoice` branch.
 *  - User transcripts + assistant text-complete write via
 *    [ChatController.appendOrchestratorMessage] — voice always belongs to
 *    the orchestrator bucket even if the user is looking at an agent tab.
 *
 * Test seam:
 *
 *  - [cancelForTest] cancels the controller's internal subscriptions so
 *    `runTest` doesn't hit `UncompletedCoroutinesError`. Mirrors the Inc 3
 *    `cancelForTest` pattern.
 *  - Tests use [handleConnectionEventForTest] /
 *    [handleVoiceWebSocketEventForTest] / [handleVoiceEventForTest] to
 *    drive the routing logic without going through the WS or VoiceManager
 *    SharedFlows.
 */
class VoiceController(
    private val scope: CoroutineScope,
    private val webSocketManager: WebSocketManager,
    private val chatController: ChatController,
    private val connectionController: OrchestratorConnectionController,
    private val audioRecorder: AudioRecorder,
    private val voiceManagerFactory: () -> VoiceManager?,
    private val getVoiceConfig: suspend () -> VoiceConfig?,
    private val pauseWakeWord: () -> CompletableDeferred<Unit>,
    private val resumeWakeWord: () -> CompletableDeferred<Unit>,
    private val playBeep: () -> Unit,
) {

    companion object {
        private const val TAG = "VoiceController"
        /** Pinned from HEAD AssistantViewModel.kt:547. */
        const val ENDING_ACK_TIMEOUT_MS = 5000L
        /** Pinned from HEAD AssistantViewModel.kt:617. */
        const val MIC_RELEASE_DELAY_MS = 1500L
        /** Pinned from HEAD AssistantViewModel.kt:537 + L626. */
        const val WAKE_WORD_ACK_TIMEOUT_MS = 2_000L
    }

    // ─────────────────────────────────────────────────────────────────
    // Public state flows
    // ─────────────────────────────────────────────────────────────────

    private val _voiceState = MutableStateFlow<VoiceState>(VoiceState.Off)
    val voiceState: StateFlow<VoiceState> = _voiceState.asStateFlow()

    private val _voiceReconnectBanner = MutableStateFlow<String?>(null)
    val voiceReconnectBanner: StateFlow<String?> = _voiceReconnectBanner.asStateFlow()

    private val _vadState = MutableStateFlow("idle")
    val vadState: StateFlow<String> = _vadState.asStateFlow()

    private val _vadDurationMs = MutableStateFlow(0L)
    val vadDurationMs: StateFlow<Long> = _vadDurationMs.asStateFlow()

    private val _isMuted = MutableStateFlow(false)
    val isMuted: StateFlow<Boolean> = _isMuted.asStateFlow()

    private val _isRecording = MutableStateFlow(false)
    val isRecording: StateFlow<Boolean> = _isRecording.asStateFlow()

    private val _toastMessage = MutableSharedFlow<String>(extraBufferCapacity = 8)
    /**
     * Toast channel — voice events (routing fallback) emit here. The
     * ViewModel mirrors this into its existing `_toastMessage` StateFlow
     * during Inc 4; Inc 7 may consolidate.
     */
    val toastMessages: SharedFlow<String> = _toastMessage.asSharedFlow()

    // ─────────────────────────────────────────────────────────────────
    // Internal state
    // ─────────────────────────────────────────────────────────────────

    /** Active voice manager — rebuilt on serverUrlChanged. */
    private var voiceManager: VoiceManager? = null

    /** Captured at [startVoiceSession]; replayed on Reconnected; cleared in [finalizeVoiceStop]. */
    private var activeVoiceConfig: VoiceConfig? = null

    /**
     * Voice-stop idempotency guard. Reset in [startVoiceSession], set in
     * [finalizeVoiceStop]. Pinned from HEAD AssistantViewModel.kt:121.
     */
    private var voiceStopFinalized: Boolean = false

    /** Safety timeout for "Ending..." → Off when VoiceEnded ack never arrives. */
    private var endingTimeoutJob: Job? = null

    /** Last cached serverUrl — drives the `needNewVoiceManager` gate. */
    private var lastServerUrl: String? = null

    /**
     * True once [onSettingsChanged] has called the factory at least once.
     * Tracked separately from `voiceManager != null` because tests may
     * stub the factory to return null while still wanting the
     * "first-emission-builds, same-URL-doesn't-rebuild" contract.
     */
    private var voiceManagerInitialized: Boolean = false

    /** Subscriptions to VoiceManager flows — recreated on rebuild. */
    private var voiceManagerStateJob: Job? = null
    private var voiceManagerEventsJob: Job? = null

    /** Subscription to ConnectionEvent.Reconnected. */
    private val connectionEventsJob: Job

    init {
        connectionEventsJob = scope.launch {
            connectionController.events.collect { ev ->
                if (ev is ConnectionEvent.Reconnected) {
                    handleReconnectedEvent(ev)
                }
            }
        }
    }

    // ─────────────────────────────────────────────────────────────────
    // Settings → VoiceManager rebuild gate
    // ─────────────────────────────────────────────────────────────────

    /**
     * Called from the ViewModel's settings observer. Rebuilds the
     * VoiceManager when the server URL changes (or on first emission);
     * otherwise just refreshes mutable tunables.
     *
     * Pinned from HEAD AssistantViewModel.kt:199-216.
     */
    fun onSettingsChanged(settings: AppSettings) {
        val newServerUrl = settings.serverUrl
        val serverUrlChanged = lastServerUrl != null && lastServerUrl != newServerUrl
        val needNewVoiceManager = !voiceManagerInitialized || serverUrlChanged
        lastServerUrl = newServerUrl

        if (needNewVoiceManager) {
            voiceManager?.release()
            val vm = voiceManagerFactory()
            voiceManager = vm
            voiceManagerInitialized = true
            vm?.let {
                it.setMicGain(settings.micGainLevel)
                it.setEchoDuckingGain(settings.echoDuckingGain)
                it.setAudioOutput(settings.audioOutput)
            }
            wireVoiceManagerCallbacks()
        } else {
            voiceManager?.let {
                it.setMicGain(settings.micGainLevel)
                it.setEchoDuckingGain(settings.echoDuckingGain)
                it.setAudioOutput(settings.audioOutput)
            }
        }
    }

    private fun wireVoiceManagerCallbacks() {
        voiceManagerStateJob?.cancel()
        voiceManagerEventsJob?.cancel()
        val vm = voiceManager ?: return

        voiceManagerStateJob = scope.launch {
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
        voiceManagerEventsJob = scope.launch {
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

    // ─────────────────────────────────────────────────────────────────
    // Voice WS event branches (called from the ViewModel's WS collector)
    // ─────────────────────────────────────────────────────────────────

    /**
     * Voice-bound WS event branches. The ChatController already handled the
     * chat-mutating branches; this method picks up the voice forwards.
     * Pinned from HEAD AssistantViewModel.kt handleVoiceWebSocketEvent.
     */
    fun handleVoiceWebSocketEvent(event: WebSocketEvent) {
        when (event) {
            is WebSocketEvent.SessionStarted -> {
                // If this is a voice session AND we initiated it, forward
                // the session.update payload to OpenAI (system prompt +
                // tool defs). When NOT the initiator, skip — we don't own a
                // provider transport on this device.
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
                    endingTimeoutJob = scope.launch {
                        delay(ENDING_ACK_TIMEOUT_MS)
                        Log.w(TAG, "voice_ended ack timeout after voice_ending")
                        finalizeVoiceStop()
                    }
                }
            }
            is WebSocketEvent.VoiceEnded,
            is WebSocketEvent.VoiceStopped -> {
                // Backend teardown finished. Finalize any in-progress
                // streaming message (TurnComplete never arrives in voice
                // mode), then do the local teardown.
                chatController.finalizeStreamingForVoiceEnd()
                finalizeVoiceStop()
            }
            else -> {
                // Non-voice events handled by ChatController.
            }
        }
    }

    /** Test seam — drives the WS event branch directly. */
    internal fun handleVoiceWebSocketEventForTest(event: WebSocketEvent) =
        handleVoiceWebSocketEvent(event)

    // ─────────────────────────────────────────────────────────────────
    // Connection events (Reconnected → voice continuity)
    // ─────────────────────────────────────────────────────────────────

    private fun handleReconnectedEvent(ev: ConnectionEvent.Reconnected) {
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

    /** Test seam — drives the Reconnected handler without the SharedFlow. */
    internal fun handleConnectionEventForTest(ev: ConnectionEvent.Reconnected) =
        handleReconnectedEvent(ev)

    // ─────────────────────────────────────────────────────────────────
    // VoiceEvent → transcript writes + UI banners
    // ─────────────────────────────────────────────────────────────────

    private fun handleVoiceEvent(event: VoiceEvent) {
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
                _toastMessage.tryEmit(event.message)
            }
            is VoiceEvent.ReconnectWarning -> {
                val secs = event.timeLeftSeconds
                _voiceReconnectBanner.value = if (secs != null) {
                    "Pausing in ~${secs}s to reconnect…"
                } else {
                    "Reconnecting shortly…"
                }
                playBeep()
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

    /** Test seam — drives the VoiceEvent handler directly. */
    internal fun handleVoiceEventForTest(event: VoiceEvent) = handleVoiceEvent(event)

    // ─────────────────────────────────────────────────────────────────
    // Public ops — push-to-talk + voice session lifecycle
    // ─────────────────────────────────────────────────────────────────

    fun startRecording() {
        scope.launch {
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
        scope.launch {
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

    /**
     * Begin a realtime voice session against the orchestrator. Pinned from
     * HEAD AssistantViewModel.kt:506-545.
     */
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
        val pauseAck = pauseWakeWord()
        voiceStopFinalized = false

        scope.launch {
            withTimeoutOrNull(WAKE_WORD_ACK_TIMEOUT_MS) { pauseAck.await() }
                ?: Log.w(TAG, "pauseWakeWord ack timeout — proceeding without confirmed release")
            val cfg = getVoiceConfig()
            if (cfg == null) {
                _voiceState.value = VoiceState.Error("Could not load voice config")
                return@launch
            }

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

    /**
     * User-initiated stop: ask the backend to end the voice connection
     * (keeping the orchestrator session alive in the pool for re-arm) and
     * show "Ending..." until VoiceEnded arrives. Pinned from HEAD
     * AssistantViewModel.kt:555-572.
     */
    fun stopVoiceSession() {
        webSocketManager.send(
            WebSocketMessage.VoiceStop,
            endpoint = WebSocketEndpoint.ORCHESTRATOR,
        )
        _voiceState.value = VoiceState.Ending
        endingTimeoutJob?.cancel()
        endingTimeoutJob = scope.launch {
            delay(ENDING_ACK_TIMEOUT_MS)
            Log.w(TAG, "voice_ended ack timeout — forcing local stop")
            finalizeVoiceStop()
        }
    }

    /**
     * Local teardown of the voice session. Called when the backend confirms
     * teardown ([WebSocketEvent.VoiceEnded] / legacy [WebSocketEvent.VoiceStopped])
     * or when the safety timeout fires. Idempotent — the `voiceStopFinalized`
     * guard short-circuits duplicate calls. Pinned from HEAD
     * AssistantViewModel.kt:574-630.
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
        scope.launch {
            voiceManager?.stop()
            _voiceState.value = VoiceState.Off
            _isMuted.value = false
            // Wait for WebRTC to release the mic before re-arming wake word.
            // Without this delay, AudioRecord fails 20+ times with "other
            // input already started" — the WebRTC AudioRecord is still held
            // by the system even after stop() returns.
            delay(MIC_RELEASE_DELAY_MS)
            val resumeAck = resumeWakeWord()
            withTimeoutOrNull(WAKE_WORD_ACK_TIMEOUT_MS) { resumeAck.await() }
                ?: Log.w(TAG, "resumeWakeWord ack timeout — service may be slow or short-circuited")
        }
    }

    /** Test seam — drives finalize without going through the WS event. */
    internal fun finalizeVoiceStopForTest() = finalizeVoiceStop()

    /** Test seam — read the dedupe guard for parity assertions. */
    internal val voiceStopFinalizedForTest: Boolean get() = voiceStopFinalized

    /** Test seam — read activeVoiceConfig. */
    internal val activeVoiceConfigForTest: VoiceConfig? get() = activeVoiceConfig

    /** Test seam — set activeVoiceConfig directly so reconnect tests don't need vm.start. */
    internal fun setActiveVoiceConfigForTest(cfg: VoiceConfig?) {
        activeVoiceConfig = cfg
    }

    fun toggleMute() {
        val newMuteState = voiceManager?.toggleMute() ?: !_isMuted.value
        _isMuted.value = newMuteState
    }

    /** Bluetooth + wired-headphone availability — delegated to VoiceManager. */
    fun isBluetoothAudioAvailable(): Boolean =
        voiceManager?.isBluetoothAudioAvailable() == true
    fun isWiredHeadphoneAvailable(): Boolean =
        voiceManager?.isWiredHeadphoneAvailable() == true

    // ─────────────────────────────────────────────────────────────────
    // Test infra
    // ─────────────────────────────────────────────────────────────────

    internal fun cancelForTest() {
        connectionEventsJob.cancel()
        voiceManagerStateJob?.cancel()
        voiceManagerEventsJob?.cancel()
    }

    /** Test-only — counts how many times the factory was invoked. */
    internal val voiceManagerForTest: VoiceManager? get() = voiceManager

    /**
     * Release the underlying [VoiceManager] and cancel internal subscriptions.
     * Called by the ViewModel's `onCleared`.
     */
    fun release() {
        voiceManager?.release()
        voiceManager = null
        connectionEventsJob.cancel()
        voiceManagerStateJob?.cancel()
        voiceManagerEventsJob?.cancel()
    }
}
