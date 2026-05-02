package com.assistant.peripheral.voice

import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothProfile
import android.content.Context
import android.media.AudioAttributes
import android.media.AudioDeviceInfo
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.os.Build
import android.util.Log
import com.assistant.peripheral.data.AudioOutput
import com.assistant.peripheral.data.VoiceState
import com.assistant.peripheral.network.ApiClient
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.*

/**
 * Thin dispatcher that owns OS-level concerns (audio focus, speaker
 * routing, Bluetooth detection) and delegates all transport-specific
 * work to a [VoiceProvider] implementation.
 *
 * Two providers ship today:
 *   - [OpenAIVoiceProvider]   — WebRTC, owns the peer connection
 *   - [QwenVoiceProvider]     — WebSocket, audio relayed via the
 *                               orchestrator WS (see [setMicChunkCallback]
 *                               and [pushSpeakerChunk])
 *
 * The provider is selected on every [start] call based on the
 * [VoiceConfig] fetched from the backend.  Switching providers requires
 * a full session restart (the underlying audio resources are different).
 *
 * ViewModel API contract (preserved from the prior monolithic
 * implementation): the ViewModel observes [state] and [events],
 * registers a [setVoiceEventCallback] for OpenAI event mirroring, and
 * uses [setMicChunkCallback] / [pushSpeakerChunk] to ferry audio for
 * the WebSocket path.  All other affordances ([toggleMute],
 * [setMicGain], [setEchoDuckingGain], [setAudioOutput],
 * [isBluetoothAudioAvailable]) are pass-through.
 */
class VoiceManager(
    private val context: Context,
    private val apiClient: ApiClient,
) {
    companion object {
        private const val TAG = "VoiceManager"
    }

    // --- OS-cross-cutting state (NOT provider-specific) -------------------
    private var audioManager: AudioManager? = null
    private var audioFocusRequest: AudioFocusRequest? = null
    private var audioOutput: AudioOutput = AudioOutput.LOUDSPEAKER

    // --- Persisted-across-sessions settings (apply to whichever provider) -
    private var pendingMicGain: Float = 1.0f
    private var pendingEchoDuckingGain: Float = 0.05f

    // --- Active provider --------------------------------------------------
    private var currentProvider: VoiceProvider? = null
    private var providerJob: Job? = null   // collects state + events into our flows

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    // --- Public flows the ViewModel observes ------------------------------
    private val _state = MutableStateFlow<VoiceState>(VoiceState.Off)
    val state: StateFlow<VoiceState> = _state.asStateFlow()

    private val _events = MutableSharedFlow<VoiceEvent>(extraBufferCapacity = 64)
    val events: SharedFlow<VoiceEvent> = _events.asSharedFlow()

    // --- Bridge callbacks set by the ViewModel ---------------------------
    /**
     * Wires WebRTC providers' event mirror to the orchestrator WS.
     * Match the existing API.
     */
    private var voiceEventCallback: ((Map<String, Any?>) -> Unit)? = null

    /**
     * Wires WebSocket providers' captured mic chunks to the orchestrator
     * WS as `voice_audio_in` messages.
     */
    private var micChunkCallback: ((String) -> Unit)? = null

    fun setVoiceEventCallback(callback: (Map<String, Any?>) -> Unit) {
        voiceEventCallback = callback
    }

    /**
     * Set the mic-chunk forwarding callback.  Called by the ViewModel
     * once the orchestrator WebSocket is wired so the Qwen path can
     * push captured PCM chunks upstream.
     */
    fun setMicChunkCallback(callback: (String) -> Unit) {
        micChunkCallback = callback
    }

    /**
     * Hand a base64-encoded PCM speaker chunk to the active provider
     * for playback.  Used by the WebSocket path; no-op if the active
     * provider is WebRTC or no provider is active.
     */
    fun pushSpeakerChunk(audioB64: String) {
        currentProvider?.pushSpeakerChunk(audioB64)
    }

    /**
     * Hand a backend-mirrored upstream provider event to the active
     * provider for parsing.  Used by the WebSocket path
     * (`voice_event` server messages); no-op for WebRTC providers,
     * which receive their events via the data channel directly.
     */
    fun handleProviderEvent(event: Map<String, Any?>) {
        currentProvider?.handleProviderEvent(event)
    }

    // --- Lifecycle --------------------------------------------------------

    /**
     * Start a voice session using the backend's currently-configured
     * voice provider/model/voice/language.  The Android app does not
     * carry its own preferences — the source of truth is the backend
     * (toggled from the web frontend).
     */
    suspend fun start() {
        val cfg = apiClient.getVoiceConfig()
        Log.i(TAG, "start: fetched voice config provider=${cfg.provider} model=${cfg.model} voice=${cfg.voice} lang=${cfg.transcriptionLanguage}")
        start(cfg)
    }

    /**
     * Start a voice session with explicit configuration.  Useful for
     * tests and for skipping the config fetch when the caller already
     * has the values.
     */
    suspend fun start(cfg: VoiceConfig) {
        if (_state.value != VoiceState.Off && _state.value !is VoiceState.Error) {
            Log.w(TAG, "start: already active state=${_state.value}")
            return
        }

        // 1. Fetch the connection metadata for this provider/model/voice.
        val info = apiClient.startVoiceSession(
            provider = cfg.provider,
            model = cfg.model,
            voice = cfg.voice,
            transcriptionLanguage = cfg.transcriptionLanguage,
        )
        if (info == null) {
            val msg = "Failed to start voice session (no connection info)"
            Log.e(TAG, msg)
            _state.value = VoiceState.Error(msg)
            _events.tryEmit(VoiceEvent.Error(msg))
            return
        }

        // 2. Pick the provider for this connection type.
        val provider = providerFor(cfg.provider, info.connectionType)
        Log.i(TAG, "start: using provider=${provider.providerId} (${provider.connectionType})")

        // 3. Apply persisted settings to the new provider before connect.
        provider.setMicGain(pendingMicGain)
        provider.setEchoDuckingGain(pendingEchoDuckingGain)

        // 4. Wire state + events.
        currentProvider = provider
        providerJob?.cancel()
        providerJob = scope.launch {
            launch {
                provider.state.collect { _state.value = it }
            }
            launch {
                provider.events.collect { _events.tryEmit(it) }
            }
        }

        // 5. Acquire OS-level audio resources (focus, routing) and connect.
        requestAudioFocus()
        provider.connect(
            info = info,
            mirrorEventToBackend = { event ->
                voiceEventCallback?.invoke(event)
            },
            sendMicChunkToBackend = { b64 ->
                micChunkCallback?.invoke(b64)
            },
        )
    }

    /**
     * Tear down the current voice session.  Releases audio focus and
     * disposes the provider.
     */
    fun stop() {
        Log.i(TAG, "stop")
        scope.launch { stopInternal() }
    }

    private suspend fun stopInternal() {
        currentProvider?.disconnect()
        currentProvider = null
        providerJob?.cancel()
        providerJob = null
        releaseAudioFocus()
        _state.value = VoiceState.Off
    }

    /** Synchronous tear-down for app shutdown.  Releases the scope. */
    fun release() {
        scope.launch { stopInternal() }.invokeOnCompletion {
            scope.cancel()
        }
    }

    // --- Backend command + audio routing pass-through --------------------

    fun handleBackendCommand(command: Map<String, Any?>) {
        currentProvider?.handleBackendCommand(command)
    }

    fun toggleMute(): Boolean {
        return currentProvider?.toggleMute() ?: false
    }

    fun isMuted(): Boolean = currentProvider?.isMuted() ?: false

    fun setMicGain(level: Float) {
        pendingMicGain = level.coerceIn(0.0f, 2.0f)
        currentProvider?.setMicGain(pendingMicGain)
    }

    fun getMicGain(): Float = pendingMicGain

    fun setEchoDuckingGain(gain: Float) {
        pendingEchoDuckingGain = gain.coerceIn(0.0f, 1.0f)
        currentProvider?.setEchoDuckingGain(pendingEchoDuckingGain)
    }

    // --- Provider factory -------------------------------------------------

    private fun providerFor(providerId: String, connectionType: VoiceConnectionType): VoiceProvider {
        return when (providerId) {
            "openai" -> OpenAIVoiceProvider(context, apiClient)
            "qwen" -> QwenVoiceProvider(context)
            else -> {
                // Unknown provider — fall back based on the connection type.
                Log.w(TAG, "Unknown provider '$providerId'; falling back by connection type")
                when (connectionType) {
                    VoiceConnectionType.WEBRTC -> OpenAIVoiceProvider(context, apiClient)
                    VoiceConnectionType.WEBSOCKET -> QwenVoiceProvider(context)
                }
            }
        }
    }

    // --- OS-level audio focus + speaker routing (cross-cutting) ----------

    private fun requestAudioFocus() {
        audioManager = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            audioFocusRequest = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_EXCLUSIVE)
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build()
                )
                .build()
            audioManager?.requestAudioFocus(audioFocusRequest!!)
        } else {
            @Suppress("DEPRECATION")
            audioManager?.requestAudioFocus(
                null,
                AudioManager.STREAM_VOICE_CALL,
                AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_EXCLUSIVE
            )
        }

        // MODE_IN_COMMUNICATION matters for both transports — WebRTC
        // forces it internally; AudioRecord on the WS path also works
        // best in this mode on Lollipop devices.
        audioManager?.mode = AudioManager.MODE_IN_COMMUNICATION
        applySpeakerRouting()

        // Ensure STREAM_VOICE_CALL is audible — voice output routes through
        // this stream; if at 0 the user hears nothing.
        val am = audioManager
        if (am != null) {
            val maxVoice = am.getStreamMaxVolume(AudioManager.STREAM_VOICE_CALL)
            val curVoice = am.getStreamVolume(AudioManager.STREAM_VOICE_CALL)
            if (curVoice == 0) {
                val target = (maxVoice * 0.75).toInt().coerceAtLeast(1)
                am.setStreamVolume(AudioManager.STREAM_VOICE_CALL, target, 0)
                Log.d(TAG, "STREAM_VOICE_CALL was 0, raised to $target/$maxVoice")
            }
        }
        Log.d(TAG, "Audio routed to ${audioOutput.name.lowercase()}")
    }

    private fun releaseAudioFocus() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            audioFocusRequest?.let { audioManager?.abandonAudioFocusRequest(it) }
        } else {
            @Suppress("DEPRECATION")
            audioManager?.abandonAudioFocus(null)
        }
        audioManager?.mode = AudioManager.MODE_NORMAL
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            audioManager?.clearCommunicationDevice()
        } else {
            @Suppress("DEPRECATION")
            audioManager?.let {
                if (it.isBluetoothScoOn) {
                    it.stopBluetoothSco()
                    it.isBluetoothScoOn = false
                }
            }
        }
        audioFocusRequest = null
    }

    /**
     * Set audio output routing.  If a session is active the change is
     * applied immediately; otherwise it's stored and applied when the
     * next session starts.
     */
    fun setAudioOutput(output: AudioOutput) {
        audioOutput = output
        Log.d(TAG, "Audio output set to: $output")
        if (audioManager != null) applySpeakerRouting()
    }

    fun isBluetoothAudioAvailable(): Boolean {
        val am = audioManager ?: (context.getSystemService(Context.AUDIO_SERVICE) as? AudioManager)
            ?.also { audioManager = it }
            ?: return false
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            val devices = am.getDevices(AudioManager.GET_DEVICES_OUTPUTS)
            return devices.any { isBluetoothDevice(it.type) }
        }
        val adapter = BluetoothAdapter.getDefaultAdapter() ?: return false
        if (!adapter.isEnabled) return false
        @Suppress("DEPRECATION")
        val headsetState = adapter.getProfileConnectionState(BluetoothProfile.HEADSET)
        @Suppress("DEPRECATION")
        val a2dpState = adapter.getProfileConnectionState(BluetoothProfile.A2DP)
        return headsetState == BluetoothProfile.STATE_CONNECTED ||
               a2dpState == BluetoothProfile.STATE_CONNECTED
    }

    private fun isBluetoothDevice(type: Int): Boolean = when (type) {
        AudioDeviceInfo.TYPE_BLUETOOTH_A2DP,
        AudioDeviceInfo.TYPE_BLUETOOTH_SCO -> true
        else -> if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S)
            type == AudioDeviceInfo.TYPE_BLE_HEADSET ||
            type == AudioDeviceInfo.TYPE_BLE_SPEAKER ||
            type == AudioDeviceInfo.TYPE_BLE_BROADCAST
        else false
    }

    private fun applySpeakerRouting() {
        val am = audioManager ?: return
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            applyRoutingModern(am)
        } else {
            applyRoutingLegacy(am)
        }
        @Suppress("DEPRECATION")
        Log.d(TAG, "[ROUTE] after applySpeakerRouting: target=$audioOutput speakerOn=${am.isSpeakerphoneOn} scoOn=${am.isBluetoothScoOn} mode=${am.mode}")
    }

    @androidx.annotation.RequiresApi(Build.VERSION_CODES.S)
    private fun applyRoutingModern(am: AudioManager) {
        val devices = am.availableCommunicationDevices
        val target: AudioDeviceInfo? = when (audioOutput) {
            AudioOutput.EARPIECE ->
                devices.firstOrNull { it.type == AudioDeviceInfo.TYPE_BUILTIN_EARPIECE }
            AudioOutput.LOUDSPEAKER ->
                devices.firstOrNull { it.type == AudioDeviceInfo.TYPE_BUILTIN_SPEAKER }
            AudioOutput.BLUETOOTH ->
                devices.firstOrNull { isBluetoothDevice(it.type) }
        }
        if (target != null) {
            val ok = am.setCommunicationDevice(target)
            Log.d(TAG, "setCommunicationDevice(${audioOutput.name}, type=${target.type}) → $ok")
            if (!ok && audioOutput == AudioOutput.BLUETOOTH) {
                Log.w(TAG, "Bluetooth setCommunicationDevice failed; falling back to loudspeaker")
                devices.firstOrNull { it.type == AudioDeviceInfo.TYPE_BUILTIN_SPEAKER }
                    ?.let { am.setCommunicationDevice(it) }
            }
        } else {
            Log.w(TAG, "No communication device for $audioOutput in $devices — clearing + falling back to speaker")
            am.clearCommunicationDevice()
            @Suppress("DEPRECATION")
            am.isSpeakerphoneOn = true
        }
    }

    private fun applyRoutingLegacy(am: AudioManager) {
        @Suppress("DEPRECATION")
        when (audioOutput) {
            AudioOutput.EARPIECE -> {
                am.stopBluetoothSco()
                am.isBluetoothScoOn = false
                am.isSpeakerphoneOn = false
            }
            AudioOutput.LOUDSPEAKER -> {
                am.stopBluetoothSco()
                am.isBluetoothScoOn = false
                am.isSpeakerphoneOn = true
            }
            AudioOutput.BLUETOOTH -> {
                if (isBluetoothAudioAvailable()) {
                    am.isSpeakerphoneOn = false
                    am.startBluetoothSco()
                    am.isBluetoothScoOn = true
                } else {
                    Log.w(TAG, "BLUETOOTH requested but no BT device available; falling back to loudspeaker")
                    am.stopBluetoothSco()
                    am.isBluetoothScoOn = false
                    am.isSpeakerphoneOn = true
                }
            }
        }
    }
}

/**
 * Voice events emitted by [VoiceManager] — pass-through from the
 * active [VoiceProvider].  Variants match what the ViewModel's
 * `handleVoiceEvent` already expects.
 */
sealed class VoiceEvent {
    /** Data channel opened (WebRTC) or transport ready (WebSocket). */
    object SessionCreated : VoiceEvent()

    /** Voice session ended (cleanup complete). */
    object SessionEnded : VoiceEvent()

    /** User started speaking (VAD detected speech). */
    object SpeechStarted : VoiceEvent()

    /** User stopped speaking (VAD detected silence). */
    object SpeechStopped : VoiceEvent()

    /** Turn completed (response.done received). */
    object TurnComplete : VoiceEvent()

    /** Assistant transcript delta (streaming). */
    data class TextDelta(val text: String) : VoiceEvent()

    /** Assistant transcript complete. */
    data class TextComplete(val text: String) : VoiceEvent()

    /** User speech transcription completed. */
    data class UserTranscript(val text: String) : VoiceEvent()

    /** Tool call received from assistant. */
    data class ToolUse(val callId: String, val name: String, val args: Map<String, Any?>) : VoiceEvent()

    /** Error occurred. */
    data class Error(val message: String) : VoiceEvent()
}
