package com.assistant.peripheral.voice

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.NoiseSuppressor
import android.os.Build
import android.util.Log
import com.assistant.peripheral.data.VoiceState
import com.assistant.peripheral.network.ApiClient
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.*
import org.json.JSONObject
import org.webrtc.*
import org.webrtc.audio.JavaAudioDeviceModule
import java.util.concurrent.TimeUnit

/**
 * VoiceManager — manages WebRTC connection to OpenAI Realtime API.
 *
 * This implementation mirrors the web frontend's useVoiceSession + useVoiceOrchestrator hooks:
 *
 * 1. Fetch ephemeral token from backend (POST /api/orchestrator/voice/session)
 * 2. Create RTCPeerConnection with microphone audio
 * 3. Exchange SDP offer/answer with OpenAI Realtime API
 * 4. Create data channel for sending/receiving events
 * 5. Mirror all OpenAI events to backend via onVoiceEvent callback
 * 6. Handle voice_command from backend via handleBackendCommand
 *
 * The orchestrator WebSocket should:
 * - Send "voice_start" message before calling start()
 * - Forward all voice events via "voice_event" messages
 * - Listen for "voice_command" messages and call handleBackendCommand()
 */
class VoiceManager(
    private val context: Context,
    private val apiClient: ApiClient
) {
    companion object {
        private const val TAG = "VoiceManager"
        // Match web frontend: OPENAI_REALTIME_URL and VOICE_MODEL
        private const val OPENAI_REALTIME_URL = "https://api.openai.com/v1/realtime"
        private const val VOICE_MODEL = "gpt-realtime"
        private const val CONNECTION_TIMEOUT_MS = 15_000L
    }

    private var peerConnection: PeerConnection? = null
    private var dataChannel: DataChannel? = null
    private var localAudioTrack: AudioTrack? = null
    private var peerConnectionFactory: PeerConnectionFactory? = null
    private var audioManager: AudioManager? = null
    private var audioFocusRequest: AudioFocusRequest? = null

    // Audio effects for echo cancellation
    private var acousticEchoCanceler: AcousticEchoCanceler? = null
    private var noiseSuppressor: NoiseSuppressor? = null

    // Echo suppression: reduce mic volume while assistant speaks
    // This helps prevent feedback loops when hardware AEC isn't sufficient
    private var isAssistantSpeaking = false
    private val MIC_VOLUME_WHILE_SPEAKING = 0.15f  // 15% volume while assistant speaks (allows loud interruptions)
    private val MIC_VOLUME_NORMAL = 1.0f

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    // State matching web frontend VoiceStatus: off, connecting, active, speaking, thinking, tool_use, error
    private val _state = MutableStateFlow<VoiceState>(VoiceState.Off)
    val state: StateFlow<VoiceState> = _state.asStateFlow()

    // Events for UI callbacks (user transcripts, assistant responses, etc.)
    private val _events = MutableSharedFlow<VoiceEvent>(extraBufferCapacity = 64)
    val events: SharedFlow<VoiceEvent> = _events.asSharedFlow()

    // Callback to mirror events to backend via WebSocket
    // Web frontend: wsRef.current?.send({ type: "voice_event", event })
    private var onVoiceEvent: ((Map<String, Any?>) -> Unit)? = null

    // Queue commands that arrive before data channel opens
    // Web frontend: pendingCommandsRef
    private val pendingCommands = mutableListOf<Map<String, Any?>>()
    private var dcReady = false

    /**
     * Set callback for mirroring events to backend.
     * The callback should send a WebSocket message: { type: "voice_event", event: <eventMap> }
     */
    fun setVoiceEventCallback(callback: (Map<String, Any?>) -> Unit) {
        onVoiceEvent = callback
    }

    /**
     * Start voice session — matches web frontend's startVoice().
     *
     * Prerequisites:
     * - Orchestrator WebSocket should already be connected
     * - "voice_start" message should be sent via WebSocket before calling this
     */
    suspend fun start() = withContext(Dispatchers.IO) {
        if (_state.value != VoiceState.Off && _state.value !is VoiceState.Error) {
            Log.w(TAG, "Voice session already active, state=${_state.value}")
            return@withContext
        }

        _state.value = VoiceState.Connecting
        dcReady = false
        pendingCommands.clear()
        Log.d(TAG, "Starting voice session...")

        try {
            // 1. Get ephemeral token from backend
            // Web frontend: const tokenData = await fetchEphemeralToken()
            Log.d(TAG, "Fetching ephemeral token from backend...")
            val tokenResponse = apiClient.getVoiceToken()
            if (tokenResponse == null) {
                Log.e(TAG, "Failed to get voice token - null response")
                _state.value = VoiceState.Error("Failed to get voice token from server")
                _events.tryEmit(VoiceEvent.Error("Failed to get voice token"))
                return@withContext
            }
            Log.d(TAG, "Got voice token, expires in ${tokenResponse.expiresIn}s")

            // 2. Request audio focus
            Log.d(TAG, "Requesting audio focus...")
            requestAudioFocus()

            // 3. Initialize WebRTC with timeout
            // Web frontend: handles = await Promise.race([connect(), timeout])
            Log.d(TAG, "Initializing WebRTC...")
            val success = withTimeoutOrNull(CONNECTION_TIMEOUT_MS) {
                initializeWebRTC(tokenResponse.token)
            }

            if (success == null) {
                Log.e(TAG, "WebRTC connection timed out")
                _state.value = VoiceState.Error("Voice connection timed out")
                _events.tryEmit(VoiceEvent.Error("Voice connection timed out"))
                cleanup()
            }

        } catch (e: Exception) {
            Log.e(TAG, "Failed to start voice session", e)
            _state.value = VoiceState.Error(e.message ?: "Unknown error: ${e.javaClass.simpleName}")
            _events.tryEmit(VoiceEvent.Error(e.message ?: "Unknown error"))
            cleanup()
        }
    }

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

        // Set speaker mode for voice chat
        audioManager?.mode = AudioManager.MODE_IN_COMMUNICATION
        @Suppress("DEPRECATION")
        audioManager?.isSpeakerphoneOn = true
    }

    private fun releaseAudioFocus() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            audioFocusRequest?.let { audioManager?.abandonAudioFocusRequest(it) }
        } else {
            @Suppress("DEPRECATION")
            audioManager?.abandonAudioFocus(null)
        }
        audioManager?.mode = AudioManager.MODE_NORMAL
        @Suppress("DEPRECATION")
        audioManager?.isSpeakerphoneOn = false
    }

    private suspend fun initializeWebRTC(token: String): Boolean {
        // Initialize PeerConnectionFactory with hardware AEC enabled
        // Web frontend: pc = new RTCPeerConnection()
        val initOptions = PeerConnectionFactory.InitializationOptions.builder(context)
            .setEnableInternalTracer(false)
            .createInitializationOptions()
        PeerConnectionFactory.initialize(initOptions)

        // Create audio device module with hardware AEC/NS enabled
        val audioDeviceModule = JavaAudioDeviceModule.builder(context)
            .setUseHardwareAcousticEchoCanceler(true)
            .setUseHardwareNoiseSuppressor(true)
            .createAudioDeviceModule()

        // Log AEC availability
        val aecAvailable = AcousticEchoCanceler.isAvailable()
        val nsAvailable = NoiseSuppressor.isAvailable()
        Log.d(TAG, "Hardware AEC available: $aecAvailable, NS available: $nsAvailable")

        peerConnectionFactory = PeerConnectionFactory.builder()
            .setOptions(PeerConnectionFactory.Options())
            .setAudioDeviceModule(audioDeviceModule)
            .createPeerConnectionFactory()

        val factory = peerConnectionFactory!!

        // Create audio source and track for microphone
        // Web frontend: micStream = await navigator.mediaDevices.getUserMedia({ audio: {...} })
        val audioConstraints = MediaConstraints().apply {
            mandatory.add(MediaConstraints.KeyValuePair("echoCancellation", "true"))
            mandatory.add(MediaConstraints.KeyValuePair("noiseSuppression", "true"))
            mandatory.add(MediaConstraints.KeyValuePair("autoGainControl", "true"))
        }

        val audioSource = factory.createAudioSource(audioConstraints)
        localAudioTrack = factory.createAudioTrack("audio0", audioSource)
        localAudioTrack?.setEnabled(true)

        // Create peer connection
        // Web frontend: pc = new RTCPeerConnection()
        val rtcConfig = PeerConnection.RTCConfiguration(emptyList())
        rtcConfig.sdpSemantics = PeerConnection.SdpSemantics.UNIFIED_PLAN
        rtcConfig.bundlePolicy = PeerConnection.BundlePolicy.MAXBUNDLE

        peerConnection = factory.createPeerConnection(rtcConfig, object : PeerConnection.Observer {
            override fun onSignalingChange(state: PeerConnection.SignalingState) {
                Log.d(TAG, "Signaling state: $state")
            }

            override fun onIceConnectionChange(state: PeerConnection.IceConnectionState) {
                Log.d(TAG, "ICE connection state: $state")
                // Web frontend: pc.onconnectionstatechange
                when (state) {
                    PeerConnection.IceConnectionState.CONNECTED -> {
                        // Connection established, but wait for data channel open
                        Log.d(TAG, "ICE connected")
                    }
                    PeerConnection.IceConnectionState.DISCONNECTED -> {
                        Log.w(TAG, "ICE disconnected")
                        handleConnectionClosed()
                    }
                    PeerConnection.IceConnectionState.FAILED -> {
                        Log.e(TAG, "ICE connection failed")
                        _state.value = VoiceState.Error("Connection failed")
                        _events.tryEmit(VoiceEvent.Error("Connection failed"))
                        stop()
                    }
                    else -> {}
                }
            }

            override fun onIceConnectionReceivingChange(receiving: Boolean) {}

            override fun onIceGatheringChange(state: PeerConnection.IceGatheringState) {
                Log.d(TAG, "ICE gathering state: $state")
            }

            override fun onIceCandidate(candidate: IceCandidate) {
                Log.d(TAG, "ICE candidate: ${candidate.sdp}")
            }

            override fun onIceCandidatesRemoved(candidates: Array<out IceCandidate>) {}

            override fun onAddStream(stream: MediaStream) {
                // Web frontend: pc.ontrack = (e) => { remoteStream = e.streams[0]; audioEl.srcObject = remoteStream }
                Log.d(TAG, "Remote stream added with ${stream.audioTracks.size} audio tracks")
                // Audio is automatically played through the device speaker
            }

            override fun onRemoveStream(stream: MediaStream) {}

            override fun onDataChannel(channel: DataChannel) {
                Log.d(TAG, "Data channel opened from remote: ${channel.label()}")
            }

            override fun onRenegotiationNeeded() {}

            override fun onAddTrack(receiver: RtpReceiver, streams: Array<out MediaStream>) {
                Log.d(TAG, "Track added: ${receiver.track()?.kind()}")
            }

            override fun onTrack(transceiver: RtpTransceiver) {
                Log.d(TAG, "Transceiver track: ${transceiver.receiver.track()?.kind()}")
            }
        })

        // Add local audio track
        // Web frontend: micStream.getTracks().forEach((track) => pc.addTrack(track, micStream))
        localAudioTrack?.let { track ->
            peerConnection?.addTrack(track, listOf("stream0"))
        }

        // Add transceiver for receiving audio
        peerConnection?.addTransceiver(
            MediaStreamTrack.MediaType.MEDIA_TYPE_AUDIO,
            RtpTransceiver.RtpTransceiverInit(RtpTransceiver.RtpTransceiverDirection.RECV_ONLY)
        )

        // Create data channel for OpenAI Realtime events
        // Web frontend: const dc = pc.createDataChannel("oai-events")
        val dcInit = DataChannel.Init().apply {
            ordered = true
        }
        dataChannel = peerConnection?.createDataChannel("oai-events", dcInit)
        setupDataChannel(dataChannel!!)

        // Create and set local SDP offer
        // Web frontend: const offer = await pc.createOffer(); await pc.setLocalDescription(offer)
        return suspendCancellableCoroutine { cont ->
            val offerConstraints = MediaConstraints()
            peerConnection?.createOffer(object : SdpObserver {
                override fun onCreateSuccess(sdp: SessionDescription) {
                    Log.d(TAG, "SDP offer created")
                    peerConnection?.setLocalDescription(object : SdpObserver {
                        override fun onSetSuccess() {
                            Log.d(TAG, "Local description set")
                            // Exchange SDP with OpenAI
                            scope.launch {
                                val success = exchangeSDP(sdp.description, token)
                                if (cont.isActive) {
                                    cont.resumeWith(Result.success(success))
                                }
                            }
                        }

                        override fun onSetFailure(error: String) {
                            Log.e(TAG, "Failed to set local description: $error")
                            _state.value = VoiceState.Error(error)
                            if (cont.isActive) {
                                cont.resumeWith(Result.success(false))
                            }
                        }

                        override fun onCreateSuccess(sdp: SessionDescription?) {}
                        override fun onCreateFailure(error: String?) {}
                    }, sdp)
                }

                override fun onCreateFailure(error: String) {
                    Log.e(TAG, "Failed to create SDP offer: $error")
                    _state.value = VoiceState.Error(error)
                    if (cont.isActive) {
                        cont.resumeWith(Result.success(false))
                    }
                }

                override fun onSetSuccess() {}
                override fun onSetFailure(error: String?) {}
            }, offerConstraints)
        }
    }

    private fun setupDataChannel(channel: DataChannel) {
        channel.registerObserver(object : DataChannel.Observer {
            override fun onBufferedAmountChange(previousAmount: Long) {}

            override fun onStateChange() {
                Log.d(TAG, "Data channel state: ${channel.state()}")
                // Web frontend: dc.onopen = () => { onConnectedRef.current() }
                if (channel.state() == DataChannel.State.OPEN) {
                    dcReady = true
                    _state.value = VoiceState.Active
                    _events.tryEmit(VoiceEvent.SessionCreated)

                    // Drain pending commands
                    // Web frontend: const pending = pendingCommandsRef.current.splice(0); for (const cmd of pending) { ... }
                    val pending = pendingCommands.toList()
                    pendingCommands.clear()
                    for (cmd in pending) {
                        sendToOpenAI(cmd)
                    }
                }
            }

            override fun onMessage(buffer: DataChannel.Buffer) {
                // Web frontend: dc.onmessage = (e) => { const event = JSON.parse(e.data); onEventRef.current(event) }
                val data = ByteArray(buffer.data.remaining())
                buffer.data.get(data)
                val message = String(data)
                handleDataChannelMessage(message)
            }
        })
    }

    private fun handleDataChannelMessage(message: String) {
        try {
            val json = JSONObject(message)
            val eventType = json.optString("type", "")

            Log.d(TAG, "Received OpenAI event: $eventType")

            // Mirror EVERY event to backend via WebSocket
            // Web frontend: wsRef.current?.send({ type: "voice_event", event })
            val eventMap = jsonToMap(json)
            onVoiceEvent?.invoke(eventMap)

            // Handle OpenAI error events
            // Web frontend: if (eventType === "error") { ... setVoiceError(...); cleanup(); updateStatus("error") }
            if (eventType == "error") {
                val errorObj = json.optJSONObject("error")
                val code = errorObj?.optString("code") ?: "unknown"
                val errorMessage = errorObj?.optString("message") ?: "Unknown error"

                Log.e(TAG, "OpenAI error: $code - $errorMessage")

                if (code == "session_expired") {
                    _state.value = VoiceState.Error("Voice session expired — please restart")
                } else {
                    _state.value = VoiceState.Error("Voice error: $code")
                }
                _events.tryEmit(VoiceEvent.Error(errorMessage))
                cleanup()
                return
            }

            // Update status and dispatch UI callbacks
            // Web frontend: switch on eventType for status updates
            when (eventType) {
                "response.created" -> {
                    _state.value = VoiceState.Speaking
                }
                "response.done" -> {
                    _state.value = VoiceState.Active
                    _events.tryEmit(VoiceEvent.TurnComplete)
                }
                "response.output_item.added" -> {
                    val item = json.optJSONObject("item")
                    if (item?.optString("type") == "function_call") {
                        _state.value = VoiceState.ToolUse
                    }
                }
                "response.function_call_arguments.done" -> {
                    _state.value = VoiceState.Thinking
                    val callId = json.optString("call_id", "")
                    val name = json.optString("name", "")
                    val argsStr = json.optString("arguments", "{}")
                    try {
                        val args = JSONObject(argsStr)
                        _events.tryEmit(VoiceEvent.ToolUse(callId, name, jsonToMap(args)))
                    } catch (e: Exception) {
                        _events.tryEmit(VoiceEvent.ToolUse(callId, name, emptyMap()))
                    }
                }
                "input_audio_buffer.speech_started" -> {
                    _state.value = VoiceState.Active
                    _events.tryEmit(VoiceEvent.SpeechStarted)
                }
                "input_audio_buffer.speech_stopped" -> {
                    _state.value = VoiceState.Thinking
                    _events.tryEmit(VoiceEvent.SpeechStopped)
                }
                // User speech transcript
                // Web frontend: if (eventType === "conversation.item.input_audio_transcription.completed")
                "conversation.item.input_audio_transcription.completed" -> {
                    val transcript = json.optString("transcript", "")
                    if (transcript.isNotEmpty()) {
                        _events.tryEmit(VoiceEvent.UserTranscript(transcript))
                    }
                }
                // Assistant transcript streaming
                // Web frontend: "response.audio_transcript.delta" / "response.audio_transcript.done"
                "response.audio_transcript.delta" -> {
                    val delta = json.optString("delta", "")
                    _events.tryEmit(VoiceEvent.TextDelta(delta))
                }
                "response.audio_transcript.done" -> {
                    val transcript = json.optString("transcript", "")
                    _events.tryEmit(VoiceEvent.TextComplete(transcript))
                }
                "output_audio_buffer.started" -> {
                    // Audio playback started - mute mic to prevent echo
                    if (_state.value != VoiceState.Speaking) {
                        _state.value = VoiceState.Speaking
                    }
                    if (!isAssistantSpeaking) {
                        isAssistantSpeaking = true
                        setMicVolume(MIC_VOLUME_WHILE_SPEAKING)
                        Log.d(TAG, ">>> Assistant speaking - MUTING mic to prevent echo")
                    }
                }
                "output_audio_buffer.cleared", "response.audio.done" -> {
                    // Audio finished or cleared - unmute mic
                    _state.value = VoiceState.Active
                    if (isAssistantSpeaking) {
                        isAssistantSpeaking = false
                        setMicVolume(MIC_VOLUME_NORMAL)
                        Log.d(TAG, ">>> Assistant done speaking - UNMUTING mic")
                    }
                }
            }

        } catch (e: Exception) {
            Log.e(TAG, "Failed to parse data channel message", e)
        }
    }

    private suspend fun exchangeSDP(localSdp: String, token: String): Boolean = withContext(Dispatchers.IO) {
        try {
            // Web frontend: exchangeSDP(ephemeralKey, offer.sdp!)
            val url = "$OPENAI_REALTIME_URL?model=$VOICE_MODEL"
            Log.d(TAG, "Exchanging SDP with OpenAI at $url")

            val client = okhttp3.OkHttpClient.Builder()
                .connectTimeout(30, TimeUnit.SECONDS)
                .readTimeout(30, TimeUnit.SECONDS)
                .build()

            val body = okhttp3.RequestBody.create(
                "application/sdp".toMediaTypeOrNull(),
                localSdp
            )

            val request = okhttp3.Request.Builder()
                .url(url)
                .post(body)
                .addHeader("Authorization", "Bearer $token")
                .addHeader("Content-Type", "application/sdp")
                .build()

            Log.d(TAG, "Sending SDP offer to OpenAI...")
            val response = client.newCall(request).execute()

            if (!response.isSuccessful) {
                val errorBody = response.body?.string() ?: "no body"
                Log.e(TAG, "SDP exchange failed: ${response.code} - $errorBody")
                _state.value = VoiceState.Error("SDP exchange failed: ${response.code}")
                _events.tryEmit(VoiceEvent.Error("OpenAI SDP exchange failed: ${response.code}"))
                return@withContext false
            }
            Log.d(TAG, "SDP exchange successful")

            val remoteSdp = response.body?.string()
            if (remoteSdp == null) {
                _state.value = VoiceState.Error("Empty SDP response")
                return@withContext false
            }

            // Set remote description
            // Web frontend: await pc.setRemoteDescription({ type: "answer", sdp: answerSdp })
            withContext(Dispatchers.Main) {
                val answer = SessionDescription(SessionDescription.Type.ANSWER, remoteSdp)
                peerConnection?.setRemoteDescription(object : SdpObserver {
                    override fun onSetSuccess() {
                        Log.d(TAG, "Remote description set successfully")
                    }

                    override fun onSetFailure(error: String) {
                        Log.e(TAG, "Failed to set remote description: $error")
                        _state.value = VoiceState.Error(error)
                    }

                    override fun onCreateSuccess(sdp: SessionDescription?) {}
                    override fun onCreateFailure(error: String?) {}
                }, answer)
            }

            true
        } catch (e: Exception) {
            Log.e(TAG, "SDP exchange error", e)
            _state.value = VoiceState.Error(e.message ?: "SDP exchange failed")
            false
        }
    }

    /**
     * Send a command to OpenAI via the data channel.
     * Web frontend: voiceHandlesRef.current.sendToOpenAI(event)
     *
     * If data channel is not yet open, the command is queued.
     */
    fun sendToOpenAI(command: Map<String, Any?>) {
        if (dcReady && dataChannel?.state() == DataChannel.State.OPEN) {
            val json = JSONObject(command)
            val buffer = DataChannel.Buffer(
                java.nio.ByteBuffer.wrap(json.toString().toByteArray()),
                false
            )
            dataChannel?.send(buffer)
        } else {
            // Queue for later
            pendingCommands.add(command)
        }
    }

    /**
     * Handle voice_command from backend.
     * Web frontend: case "voice_command": sendToOpenAI(event.command)
     *
     * This is called when the backend sends a voice_command via WebSocket
     * (e.g., function_call_output after tool execution).
     */
    fun handleBackendCommand(command: Map<String, Any?>) {
        sendToOpenAI(command)
    }

    /**
     * Handle connection closed (session expired, network drop).
     * Web frontend: handleConnectionClosed callback
     */
    private fun handleConnectionClosed() {
        if (peerConnection != null) {
            // Only act if voice is currently active
            if (_state.value != VoiceState.Off && _state.value !is VoiceState.Error) {
                _state.value = VoiceState.Error("Voice connection lost")
                _events.tryEmit(VoiceEvent.Error("Voice connection lost"))
            }
            cleanup()
        }
    }

    /**
     * Toggle microphone mute on/off.
     * Web frontend: toggleMute() in useVoiceOrchestrator
     */
    fun toggleMute(): Boolean {
        val currentEnabled = localAudioTrack?.enabled() ?: true
        val newEnabled = !currentEnabled
        localAudioTrack?.setEnabled(newEnabled)
        return !newEnabled  // Return muted state (inverse of enabled)
    }

    /**
     * Check if microphone is muted.
     */
    fun isMuted(): Boolean = !(localAudioTrack?.enabled() ?: true)

    /**
     * Set microphone volume (0.0 to 1.0).
     * Used to reduce echo feedback when assistant is speaking.
     *
     * Since WebRTC doesn't expose direct volume control on AudioTrack,
     * we mute the track when volume is very low (< 0.5) and unmute otherwise.
     * This prevents the assistant from hearing its own voice through the mic.
     */
    private fun setMicVolume(volume: Float) {
        localAudioTrack?.let { track ->
            try {
                if (volume < 0.5f) {
                    // Mute mic while assistant is speaking to prevent echo
                    track.setEnabled(false)
                } else {
                    // Restore mic when assistant stops speaking
                    track.setEnabled(true)
                }
            } catch (e: Exception) {
                Log.w(TAG, "Failed to adjust mic volume", e)
            }
        }
    }

    /**
     * Stop voice session and cleanup.
     * Web frontend: stopVoice() in useVoiceOrchestrator
     */
    fun stop() {
        Log.d(TAG, "Stopping voice session")
        cleanup()
        _state.value = VoiceState.Off
        _events.tryEmit(VoiceEvent.SessionEnded)
    }

    private fun cleanup() {
        Log.d(TAG, "Cleaning up voice resources")

        dcReady = false
        pendingCommands.clear()

        dataChannel?.close()
        dataChannel = null

        localAudioTrack?.setEnabled(false)
        localAudioTrack?.dispose()
        localAudioTrack = null

        peerConnection?.close()
        peerConnection = null

        peerConnectionFactory?.dispose()
        peerConnectionFactory = null

        // Release audio effects
        try {
            acousticEchoCanceler?.release()
            acousticEchoCanceler = null
            noiseSuppressor?.release()
            noiseSuppressor = null
        } catch (e: Exception) {
            Log.w(TAG, "Error releasing audio effects", e)
        }

        releaseAudioFocus()
    }

    fun release() {
        stop()
        scope.cancel()
    }

    @Suppress("UNCHECKED_CAST")
    private fun jsonToMap(json: JSONObject): Map<String, Any?> {
        val map = mutableMapOf<String, Any?>()
        val keys = json.keys()
        while (keys.hasNext()) {
            val key = keys.next()
            val value = json.opt(key)
            map[key] = when (value) {
                is JSONObject -> jsonToMap(value)
                is org.json.JSONArray -> jsonArrayToList(value)
                org.json.JSONObject.NULL -> null
                else -> value
            }
        }
        return map
    }

    private fun jsonArrayToList(array: org.json.JSONArray): List<Any?> {
        val list = mutableListOf<Any?>()
        for (i in 0 until array.length()) {
            val value = array.opt(i)
            list.add(when (value) {
                is JSONObject -> jsonToMap(value)
                is org.json.JSONArray -> jsonArrayToList(value)
                org.json.JSONObject.NULL -> null
                else -> value
            })
        }
        return list
    }
}

/**
 * Voice events emitted by VoiceManager.
 * These match the callbacks in the web frontend's useVoiceOrchestrator.
 */
sealed class VoiceEvent {
    /** Data channel opened, session ready. */
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
