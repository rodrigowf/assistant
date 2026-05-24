package com.assistant.peripheral.voice

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
 * Three providers ship today:
 *   - [OpenAIVoiceProvider]   — WebRTC, owns the peer connection
 *   - [QwenVoiceProvider]     — WebSocket (OpenAI-Realtime event shape),
 *                               audio relayed via the orchestrator WS
 *                               (see [setMicChunkCallback] and
 *                               [pushSpeakerChunk])
 *   - [GeminiVoiceProvider]   — WebSocket (Gemini Live event shape),
 *                               same transport as Qwen
 *
 * The two WebSocket providers share their audio plumbing via
 * [WebSocketPcmProvider]; only their upstream-event parsers differ.
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

    /**
     * Owns the routing decision tree (HFP-vs-A2DP, call-audio vs
     * media-audio) plus all the AudioManager mode/communicationDevice
     * mutations.  Lifecycle is the same as VoiceManager — created in
     * [requestAudioFocus] and released in [releaseAudioFocus].
     */
    private val audioRouter: AudioRouter = AudioRouter(context)

    /**
     * Tracks devices changing during a session so the router can
     * re-evaluate.  Stays null when no session is active.
     */
    private var deviceCallback: android.media.AudioDeviceCallback? = null

    // --- Persisted-across-sessions settings (apply to whichever provider) -
    private var pendingMicGain: Float = 1.0f
    private var pendingEchoDuckingGain: Float = 0.05f

    // --- Active provider --------------------------------------------------
    private var currentProvider: VoiceProvider? = null
    private var providerJob: Job? = null   // collects state + events into our flows
    private var routeReapplyJob: Job? = null  // re-applies routing after provider audio stack init

    // Synchronous reentrancy gate.  `_state.value` only flips to non-Off
    // after the HTTP roundtrip in step 1, so concurrent callers (e.g.
    // two viewModelScope.launch blocks if a stale collector ever fires
    // start twice) would both slip past the `_state == Off` guard and
    // each spin up an independent provider + WebRTC peer connection.
    // Symptom: two SDP exchanges with different ufrag values, two
    // SESSION START log lines a few hundred ms apart, audio doubled,
    // and only one of the sessions gets stopped on disconnect.
    @Volatile private var starting: Boolean = false

    // --- Pre-provider command queue --------------------------------------
    // The backend replies to ``voice_start`` with a ``session_started``
    // payload that carries the system-prompt / tools / voice config in
    // ``voice_session_update``.  In the Android lifecycle, the WS reply
    // races ahead of provider creation: ``ViewModel.startVoiceSession``
    // sends the WS message first, then awaits ``apiClient.startVoiceSession``
    // (HTTP) before constructing the provider.  The ``session_started``
    // mirror arrives during that window, when ``currentProvider`` is
    // null — silently dropping the update there left the OpenAI session
    // running with its bare defaults (canned 505-char instructions, no
    // tools, no input transcription) and produced "voice mode is
    // isolated from the assistant" symptoms (no system prompt, hallucinated
    // history, no user transcripts persisted to JSONL).
    //
    // We queue here and drain in ``start()`` immediately after the
    // provider is wired but before ``provider.connect`` opens the data
    // channel, so the provider's own ``pendingCommands`` queue receives
    // the update and flushes it on data-channel-open.
    private val pendingBackendCommands = mutableListOf<Map<String, Any?>>()

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
        // Synchronous gate — prevents a second start() from sneaking past
        // while the first is awaiting the HTTP roundtrip below.  Cleared
        // once we've wired the provider's state flow, after which the
        // _state guard above takes over (provider flips to Connecting).
        synchronized(this) {
            if (starting) {
                Log.w(TAG, "start: already in progress (starting=true), ignoring duplicate call")
                return
            }
            starting = true
        }

        try {
        // 1. Fetch the connection metadata for this provider/model/voice.
        val info = apiClient.startVoiceSession(
            provider = cfg.provider,
            model = cfg.model,
            voice = cfg.voice,
            transcriptionLanguage = cfg.transcriptionLanguage,
            endpoint = cfg.endpoint.takeIf { it.isNotBlank() },
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

        // 4b. Drain any backend commands that arrived before the provider
        // was created (typically the session.update payload that races
        // the HTTP fetch in step 1).  The provider has its own
        // pendingCommands queue keyed on the data channel state, so
        // these will sit there until DC_OPEN and flush atomically.
        if (pendingBackendCommands.isNotEmpty()) {
            val drained = pendingBackendCommands.toList()
            pendingBackendCommands.clear()
            Log.i(TAG, "start: draining ${drained.size} pre-provider backend command(s)")
            for (cmd in drained) provider.handleBackendCommand(cmd)
        }

        // 5. Acquire OS-level audio resources (focus, routing) and connect.
        requestAudioFocus()

        // 5b. Schedule post-connect routing re-apply.  WebRTC's
        // JavaAudioDeviceModule (and Samsung Lollipop's HAL more
        // broadly) can pin the audio route to the call earpiece
        // during native audio-module init — well after our initial
        // applySpeakerRouting fires.  Re-assert at +1s/+3s/+5s.
        // Dynamic device-connect events are handled separately via
        // [registerDeviceCallback].
        routeReapplyJob?.cancel()
        routeReapplyJob = scope.launch {
            for (delayMs in longArrayOf(1000L, 3000L, 5000L)) {
                delay(delayMs)
                if (!isActive) return@launch
                Log.d(TAG, "[ROUTE] post-connect re-apply at +${delayMs}ms")
                applySpeakerRouting()
            }
        }

        provider.connect(
            info = info,
            mirrorEventToBackend = { event ->
                voiceEventCallback?.invoke(event)
            },
            sendMicChunkToBackend = { b64 ->
                micChunkCallback?.invoke(b64)
            },
        )
        } finally {
            // Clear the gate.  By here either provider.connect has
            // returned (success — _state guard now blocks reentry) or we
            // bailed early (error — caller should be able to retry).
            starting = false
        }
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
        routeReapplyJob?.cancel()
        routeReapplyJob = null
        currentProvider?.disconnect()
        currentProvider = null
        providerJob?.cancel()
        providerJob = null
        // Drop any backend commands queued for a provider that never
        // started — they belong to a session we just tore down.
        pendingBackendCommands.clear()
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
        val provider = currentProvider
        if (provider != null) {
            provider.handleBackendCommand(command)
        } else {
            // Queue until start() wires the provider; drained below.
            val cmdType = command["type"] as? String ?: "?"
            Log.d(TAG, "handleBackendCommand: no provider yet, queueing type=$cmdType")
            pendingBackendCommands.add(command)
        }
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
            "google" -> GeminiVoiceProvider(context)
            else -> {
                // Unknown provider — fall back based on the connection
                // type. WebSocket fallback uses the Qwen parser since
                // OpenAI-Realtime event shape is the de-facto standard
                // among third-party realtime APIs.
                Log.w(TAG, "Unknown provider '$providerId'; falling back by connection type")
                when (connectionType) {
                    VoiceConnectionType.WEBRTC -> OpenAIVoiceProvider(context, apiClient)
                    VoiceConnectionType.WEBSOCKET -> QwenVoiceProvider(context, providerId = providerId)
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

        applySpeakerRouting()
        ensureCallStreamAudible()
        registerDeviceCallback()
    }

    /**
     * STREAM_VOICE_CALL ships muted on some devices — bring it up to a
     * reasonable level so the user actually hears the agent.  Only
     * needed for routes on the communication-audio plane.
     */
    private fun ensureCallStreamAudible() {
        val am = audioManager ?: return
        val maxVoice = am.getStreamMaxVolume(AudioManager.STREAM_VOICE_CALL)
        val curVoice = am.getStreamVolume(AudioManager.STREAM_VOICE_CALL)
        if (curVoice == 0) {
            val target = (maxVoice * 0.75).toInt().coerceAtLeast(1)
            am.setStreamVolume(AudioManager.STREAM_VOICE_CALL, target, 0)
            Log.d(TAG, "STREAM_VOICE_CALL was 0, raised to $target/$maxVoice")
        }
    }

    private fun releaseAudioFocus() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            audioFocusRequest?.let { audioManager?.abandonAudioFocusRequest(it) }
        } else {
            @Suppress("DEPRECATION")
            audioManager?.abandonAudioFocus(null)
        }
        audioRouter.release()
        unregisterDeviceCallback()
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

    fun isBluetoothAudioAvailable(): Boolean = audioRouter.isBluetoothAudioAvailable()

    /**
     * Pick a [AudioRouter.Route] for the current audioOutput + active
     * provider, apply it to the system, and forward the resulting
     * [AudioRouter.SpeakerMode] to the provider's AudioTrack so the
     * media-vs-call decision propagates end-to-end.
     *
     * Also emits [VoiceEvent.RoutingFallback] when the router had to
     * downgrade (e.g. user picked BT but only an A2DP-only sink is
     * available on a WebRTC provider) — the ViewModel surfaces this
     * as a toast.
     */
    private fun applySpeakerRouting() {
        audioManager ?: return
        val provider = currentProvider
        val providerKind = when (provider?.connectionType) {
            VoiceConnectionType.WEBSOCKET -> AudioRouter.ProviderKind.WEBSOCKET
            VoiceConnectionType.WEBRTC -> AudioRouter.ProviderKind.WEBRTC
            null -> AudioRouter.ProviderKind.WEBRTC  // conservative default
        }
        val route = audioRouter.pickRoute(audioOutput, providerKind)
        val mode = audioRouter.apply(route)

        // Forward the speaker mode + preferred device to the provider so
        // its AudioTrack picks the right audio plane (WS providers
        // only; WebRTC ignores).
        val preferredDevice = when (route) {
            is AudioRouter.Route.BluetoothMedia -> route.device
            is AudioRouter.Route.BluetoothCallAudio -> route.device
            else -> null
        }
        provider?.setSpeakerMode(mode, preferredDevice)

        if (route is AudioRouter.Route.BluetoothUnsupported) {
            val msg = when (route.reason) {
                AudioRouter.FallbackReason.BT_NOT_AVAILABLE ->
                    "Bluetooth not available — using loudspeaker"
                AudioRouter.FallbackReason.BT_A2DP_REQUIRES_WS_PROVIDER ->
                    "OpenAI Realtime can't route to Bluetooth speakers " +
                        "(no mic on this device). Switch to Qwen or Gemini, " +
                        "or use a BT headset. Using loudspeaker for now."
            }
            _events.tryEmit(VoiceEvent.RoutingFallback(msg))
        }
    }

    // --- Device-change observer -----------------------------------------

    /**
     * Watch for BT devices connecting/disconnecting mid-session and
     * re-run the routing decision.  Without this, plugging in the JBL
     * mid-conversation leaves audio on the loudspeaker until the next
     * full session start.
     */
    private fun registerDeviceCallback() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) return
        if (deviceCallback != null) return
        val cb = object : android.media.AudioDeviceCallback() {
            override fun onAudioDevicesAdded(addedDevices: Array<out AudioDeviceInfo>) {
                Log.d(TAG, "[ROUTE] device added — re-applying routing")
                applySpeakerRouting()
            }
            override fun onAudioDevicesRemoved(removedDevices: Array<out AudioDeviceInfo>) {
                Log.d(TAG, "[ROUTE] device removed — re-applying routing")
                applySpeakerRouting()
            }
        }
        deviceCallback = cb
        audioManager?.registerAudioDeviceCallback(cb, null)
    }

    private fun unregisterDeviceCallback() {
        val cb = deviceCallback ?: return
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                audioManager?.unregisterAudioDeviceCallback(cb)
            }
        } catch (_: Exception) {}
        deviceCallback = null
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

    /**
     * The router downgraded the user's requested audio output (e.g. user
     * selected Bluetooth but only an A2DP-only speaker is connected on
     * a WebRTC provider).  Surfaced as a toast so silent route fallbacks
     * don't confuse the user.
     */
    data class RoutingFallback(val message: String) : VoiceEvent()
}
