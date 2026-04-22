package com.assistant.peripheral.voice

import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothProfile
import android.content.Context
import android.media.AudioAttributes
import android.media.AudioDeviceInfo
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.NoiseSuppressor
import android.os.Build
import android.util.Log
import com.assistant.peripheral.data.AudioOutput
import com.assistant.peripheral.data.VoiceState
import com.assistant.peripheral.network.ApiClient
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.*
import org.json.JSONObject
import org.webrtc.*
import org.webrtc.audio.AudioRecordDataCallback
import org.webrtc.audio.JavaAudioDeviceModule
import org.webrtc.voiceengine.WebRtcAudioUtils
import java.nio.ByteBuffer
import java.nio.ByteOrder
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

        // RMS logging: emit one [AUDIO_RMS] line every N callback invocations (≈500ms at 100Hz)
        private const val RMS_LOG_INTERVAL = 50

        // PeerConnectionFactory.initialize() is process-wide — must only be called once.
        @Volatile private var peerConnectionFactoryInitialized = false
    }

    private var peerConnection: PeerConnection? = null
    private var dataChannel: DataChannel? = null
    private var localAudioTrack: AudioTrack? = null
    private var peerConnectionFactory: PeerConnectionFactory? = null
    private var audioManager: AudioManager? = null
    private var audioFocusRequest: AudioFocusRequest? = null

    // Microphone gain (0.0 to 2.0, default 1.0)
    private var micGainLevel: Float = 1.0f

    // Audio output routing: EARPIECE, LOUDSPEAKER (default), or BLUETOOTH
    private var audioOutput: AudioOutput = AudioOutput.LOUDSPEAKER

    // Gain saved before agent speech — restored when speech ends or user interrupts
    private var gainBeforeSpeaking: Float? = null
    private var micRestoreJob: kotlinx.coroutines.Job? = null
    // True while agent audio is actively playing (between output_audio_buffer.started and stopped/cleared)
    private var agentAudioPlaying: Boolean = false
    // Gain applied while agent is speaking (echo ducking level), default 5%
    private var echoDuckingGain: Float = 0.05f

    // RMS logging counter — incremented in audioRecordDataCallback
    private var rmsLogCounter: Int = 0
    // Session start time for relative timestamps in logs
    private var sessionStartMs: Long = 0L

    // Audio effects for echo cancellation
    private var acousticEchoCanceler: AcousticEchoCanceler? = null
    private var noiseSuppressor: NoiseSuppressor? = null


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
    /** Relative time since session start in ms, formatted as [+XXXXXms] */
    private fun t(): String {
        val elapsed = if (sessionStartMs > 0) System.currentTimeMillis() - sessionStartMs else 0L
        return "[+${elapsed}ms]"
    }

    /** Log a mic state transition with full context */
    private fun logMicState(action: String, extra: String = "") {
        Log.i(TAG, "[MIC_STATE] ${t()} $action | gain=$micGainLevel gainSaved=$gainBeforeSpeaking agentPlaying=$agentAudioPlaying trackEnabled=${localAudioTrack?.enabled()} $extra")
    }

    suspend fun start() = withContext(Dispatchers.IO) {
        if (_state.value != VoiceState.Off && _state.value !is VoiceState.Error) {
            Log.w(TAG, "[VM] Voice session already active, state=${_state.value}")
            return@withContext
        }

        sessionStartMs = System.currentTimeMillis()
        rmsLogCounter = 0
        _state.value = VoiceState.Connecting
        dcReady = false
        pendingCommands.clear()
        Log.i(TAG, "[VM] ===== SESSION START ===== epochMs=$sessionStartMs")

        try {
            // 1. Get ephemeral token from backend
            Log.d(TAG, "[VM] ${t()} Fetching ephemeral token...")
            val tokenResponse = apiClient.getVoiceToken()
            if (tokenResponse == null) {
                Log.e(TAG, "[VM] ${t()} ERROR: Failed to get voice token (null response)")
                _state.value = VoiceState.Error("Failed to get voice token from server")
                _events.tryEmit(VoiceEvent.Error("Failed to get voice token"))
                return@withContext
            }
            Log.i(TAG, "[VM] ${t()} Got voice token, expires in ${tokenResponse.expiresIn}s")

            // 2. Request audio focus
            Log.d(TAG, "[VM] ${t()} Requesting audio focus...")
            requestAudioFocus()

            // 3. Initialize WebRTC with timeout
            Log.d(TAG, "[VM] ${t()} Initializing WebRTC (timeout=${CONNECTION_TIMEOUT_MS}ms)...")
            val success = withTimeoutOrNull(CONNECTION_TIMEOUT_MS) {
                initializeWebRTC(tokenResponse.token)
            }

            if (success == null) {
                Log.e(TAG, "[VM] ${t()} ERROR: WebRTC connection timed out after ${CONNECTION_TIMEOUT_MS}ms")
                _state.value = VoiceState.Error("Voice connection timed out")
                _events.tryEmit(VoiceEvent.Error("Voice connection timed out"))
                cleanup()
            }

        } catch (e: Exception) {
            Log.e(TAG, "[VM] ${t()} ERROR: Failed to start voice session: ${e.javaClass.simpleName}: ${e.message}", e)
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

        // Always use MODE_IN_COMMUNICATION — WebRTC's JavaAudioDeviceModule forces this mode
        // internally when it starts recording, overriding MODE_NORMAL. Setting it here first
        // avoids a transient mode mismatch. Speaker routing is applied after WebRTC connects.
        audioManager?.mode = AudioManager.MODE_IN_COMMUNICATION
        applySpeakerRouting()

        // Ensure STREAM_VOICE_CALL is audible — WebRTC routes output through this stream
        // on Lollipop. If the user has phone volume at 0 or the stream is muted, voice
        // output will be silent. Raise to at least 50% of max if currently at 0.
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
        // Release the routing override we set in applySpeakerRouting() so the next app
        // gets default audio routing. Don't hardcode isSpeakerphoneOn = false here —
        // that leaves the system stuck in earpiece, which is exactly the bug we fixed.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            audioManager?.clearCommunicationDevice()
        } else {
            // Tear down legacy Bluetooth SCO if we started it, so BT doesn't remain
            // held open for the next app.
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

    private suspend fun initializeWebRTC(token: String): Boolean {
        // SOFTWARE-ONLY AEC: Disable hardware AEC, use only WebRTC software processing
        // Hardware AEC can interfere or not work properly with Bluetooth speakers

        // Enable WebRTC-based software AEC - this MUST be called before PeerConnectionFactory.initialize()
        // Reference: https://getstream.github.io/webrtc-android/stream-webrtc-android/org.webrtc.voiceengine/-web-rtc-audio-utils/
        WebRtcAudioUtils.setWebRtcBasedAcousticEchoCanceler(true)
        WebRtcAudioUtils.setWebRtcBasedNoiseSuppressor(true)
        WebRtcAudioUtils.setWebRtcBasedAutomaticGainControl(true)
        Log.d(TAG, ">>> Enabled WebRTC SOFTWARE AEC, NS, and AGC")

        // Initialize PeerConnectionFactory (process-wide singleton — only initialize once)
        if (!peerConnectionFactoryInitialized) {
            val initOptions = PeerConnectionFactory.InitializationOptions.builder(context)
                .setEnableInternalTracer(false)
                .createInitializationOptions()
            PeerConnectionFactory.initialize(initOptions)
            peerConnectionFactoryInitialized = true
        }

        // Disable hardware AEC - only use software
        val hwAecAvailable = AcousticEchoCanceler.isAvailable()
        val hwNsAvailable = NoiseSuppressor.isAvailable()
        Log.d(TAG, "Hardware AEC available: $hwAecAvailable (DISABLED), NS available: $hwNsAvailable (DISABLED)")

        // Use VOICE_RECOGNITION on Lollipop (API < 24): Samsung's HAL routes VOICE_COMMUNICATION
        // through aggressive noise processing that silences audio when MODE_NORMAL is active.
        // VOICE_RECOGNITION bypasses that processing pipeline and reliably captures mic audio.
        val micAudioSource = if (Build.VERSION.SDK_INT < Build.VERSION_CODES.N)
            MediaRecorder.AudioSource.VOICE_RECOGNITION
        else
            MediaRecorder.AudioSource.VOICE_COMMUNICATION

        val audioDeviceModule = JavaAudioDeviceModule.builder(context)
            .setUseHardwareAcousticEchoCanceler(false)  // Disabled - can interfere with software AEC
            .setUseHardwareNoiseSuppressor(false)       // Disabled - using software instead
            .setAudioRecordDataCallback(audioRecordDataCallback)  // Apply mic gain before WebRTC
            .setAudioSource(micAudioSource)
            .createAudioDeviceModule()
        Log.d(TAG, "Audio source: ${if (micAudioSource == MediaRecorder.AudioSource.VOICE_RECOGNITION) "VOICE_RECOGNITION" else "VOICE_COMMUNICATION"}")

        Log.d(TAG, ">>> Using SOFTWARE-ONLY AEC (hardware disabled)")

        peerConnectionFactory = PeerConnectionFactory.builder()
            .setOptions(PeerConnectionFactory.Options())
            .setAudioDeviceModule(audioDeviceModule)
            .createPeerConnectionFactory()

        val factory = peerConnectionFactory!!

        // Create audio source with WebRTC software-based audio processing
        // These constraints enable WebRTC's internal echo cancellation algorithms
        val audioConstraints = MediaConstraints().apply {
            // Standard WebRTC constraints
            mandatory.add(MediaConstraints.KeyValuePair("echoCancellation", "true"))
            mandatory.add(MediaConstraints.KeyValuePair("noiseSuppression", "true"))
            mandatory.add(MediaConstraints.KeyValuePair("autoGainControl", "true"))
            // Google-specific WebRTC AEC constraints for better echo cancellation
            mandatory.add(MediaConstraints.KeyValuePair("googEchoCancellation", "true"))
            mandatory.add(MediaConstraints.KeyValuePair("googEchoCancellation2", "true"))
            mandatory.add(MediaConstraints.KeyValuePair("googDAEchoCancellation", "true"))
            mandatory.add(MediaConstraints.KeyValuePair("googAutoGainControl", "true"))
            mandatory.add(MediaConstraints.KeyValuePair("googAutoGainControl2", "true"))
            mandatory.add(MediaConstraints.KeyValuePair("googNoiseSuppression", "true"))
            mandatory.add(MediaConstraints.KeyValuePair("googNoiseSuppression2", "true"))
            mandatory.add(MediaConstraints.KeyValuePair("googHighpassFilter", "true"))
            mandatory.add(MediaConstraints.KeyValuePair("googTypingNoiseDetection", "true"))
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
                // Re-apply routing one more time — WebRTC reconfigures the audio output
                // right before the first remote audio packet plays, and on some Lollipop
                // devices that reverts speaker routing silently. Running applySpeakerRouting()
                // here catches the flake observed on the Samsung A300M.
                applySpeakerRouting()
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
                Log.d(TAG, "[VM] ${t()} Data channel state: ${channel.state()}")
                // Web frontend: dc.onopen = () => { onConnectedRef.current() }
                if (channel.state() == DataChannel.State.OPEN) {
                    dcReady = true
                    _state.value = VoiceState.Active
                    _events.tryEmit(VoiceEvent.SessionCreated)
                    Log.i(TAG, "[VM] ${t()} ===== DATA CHANNEL OPEN — session ready =====")
                    logMicState("DC_OPEN initial state")
                    // Re-apply speaker routing: WebRTC may have reset MODE_IN_COMMUNICATION
                    // which reverts isSpeakerphoneOn to false (earpiece) on some devices.
                    applySpeakerRouting()

                    // Drain pending commands
                    val pending = pendingCommands.toList()
                    pendingCommands.clear()
                    if (pending.isNotEmpty()) {
                        Log.d(TAG, "[VM] ${t()} Draining ${pending.size} pending commands")
                    }
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

            // Log every event with timestamp — skip high-frequency audio delta events to avoid spam
            val isNoisyEvent = eventType == "response.audio.delta" || eventType == "response.audio_transcript.delta"
            if (!isNoisyEvent) {
                Log.d(TAG, "[VOICE_EVENT] ${t()} type=$eventType state=${_state.value} agentPlaying=$agentAudioPlaying gain=$micGainLevel gainSaved=$gainBeforeSpeaking")
            }

            // Mirror EVERY event to backend via WebSocket
            val eventMap = jsonToMap(json)
            onVoiceEvent?.invoke(eventMap)

            // Handle OpenAI error events
            if (eventType == "error") {
                val errorObj = json.optJSONObject("error")
                val code = errorObj?.optString("code") ?: "unknown"
                val errorMessage = errorObj?.optString("message") ?: "Unknown error"
                Log.e(TAG, "[VOICE_EVENT] ${t()} ===== OPENAI ERROR: code=$code msg=$errorMessage =====")
                if (code == "session_expired") {
                    _state.value = VoiceState.Error("Voice session expired — please restart")
                } else {
                    _state.value = VoiceState.Error("Voice error: $code")
                }
                _events.tryEmit(VoiceEvent.Error(errorMessage))
                cleanup()
                return
            }

            when (eventType) {
                "response.created" -> {
                    // Cancel any pending restore from the previous turn before ducking.
                    // This prevents a RESTORE_DONE from firing mid-response and wiping gainBeforeSpeaking.
                    micRestoreJob?.cancel()
                    micRestoreJob = null
                    Log.i(TAG, "[VOICE_EVENT] ${t()} RESPONSE CREATED → ducking mic (restore timer cancelled)")
                    _state.value = VoiceState.Speaking
                    duckMicForAgentSpeech()
                }
                "response.done" -> {
                    Log.i(TAG, "[VOICE_EVENT] ${t()} RESPONSE DONE — not restoring mic yet, waiting for audio buffer")
                    _state.value = VoiceState.Active
                    // Do NOT restore mic here — audio is still playing after response.done.
                    // Wait for output_audio_buffer.stopped instead.
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
                    Log.d(TAG, "[VOICE_EVENT] ${t()} TOOL_CALL name=$name callId=$callId")
                    try {
                        val args = JSONObject(argsStr)
                        _events.tryEmit(VoiceEvent.ToolUse(callId, name, jsonToMap(args)))
                    } catch (e: Exception) {
                        _events.tryEmit(VoiceEvent.ToolUse(callId, name, emptyMap()))
                    }
                }
                "input_audio_buffer.speech_started" -> {
                    if (agentAudioPlaying) {
                        // Audio still playing — almost certainly echo pickup, not the user.
                        Log.w(TAG, "[VOICE_EVENT] ${t()} ===== SPEECH_STARTED (SUPPRESSED — echo while agent playing) ===== gain=$micGainLevel trackEnabled=${localAudioTrack?.enabled()}")
                        // Keep mic ducked
                    } else {
                        Log.i(TAG, "[VOICE_EVENT] ${t()} ===== SPEECH_STARTED (user speaking) → restoring mic =====")
                        restoreMicImmediately()
                    }
                    _state.value = VoiceState.Active
                    _events.tryEmit(VoiceEvent.SpeechStarted)
                }
                "input_audio_buffer.speech_stopped" -> {
                    Log.i(TAG, "[VOICE_EVENT] ${t()} SPEECH_STOPPED")
                    _state.value = VoiceState.Thinking
                    _events.tryEmit(VoiceEvent.SpeechStopped)
                }
                "conversation.item.input_audio_transcription.completed" -> {
                    val transcript = json.optString("transcript", "")
                    Log.i(TAG, "[VOICE_EVENT] ${t()} USER_TRANSCRIPT: \"$transcript\"")
                    if (transcript.isNotEmpty()) {
                        _events.tryEmit(VoiceEvent.UserTranscript(transcript))
                    }
                }
                "response.audio_transcript.delta" -> {
                    val delta = json.optString("delta", "")
                    _events.tryEmit(VoiceEvent.TextDelta(delta))
                }
                "response.audio_transcript.done" -> {
                    val transcript = json.optString("transcript", "")
                    Log.i(TAG, "[VOICE_EVENT] ${t()} AGENT_TRANSCRIPT: \"$transcript\"")
                    _events.tryEmit(VoiceEvent.TextComplete(transcript))
                }
                "output_audio_buffer.started" -> {
                    // Cancel any pending restore — new audio is starting, keep mic ducked
                    micRestoreJob?.cancel()
                    micRestoreJob = null
                    Log.i(TAG, "[VOICE_EVENT] ${t()} ===== AUDIO BUFFER STARTED (agent speaking) ===== agentPlaying: false→true (restore timer cancelled)")
                    agentAudioPlaying = true
                    if (_state.value != VoiceState.Speaking) {
                        _state.value = VoiceState.Speaking
                    }
                    duckMicForAgentSpeech()
                }
                "output_audio_buffer.stopped" -> {
                    // Playback truly finished — safe to restore mic after tail delay
                    Log.i(TAG, "[VOICE_EVENT] ${t()} ===== AUDIO BUFFER STOPPED (playback done) ===== agentPlaying: true→false → restoring mic after 2000ms")
                    agentAudioPlaying = false
                    _state.value = VoiceState.Active
                    restoreMicAfterAgentSpeech(delayMs = 2000L)
                }
                "output_audio_buffer.cleared" -> {
                    // Buffer was cleared (interruption) — speaker may still be ringing for a moment
                    Log.i(TAG, "[VOICE_EVENT] ${t()} ===== AUDIO BUFFER CLEARED (interrupted) ===== agentPlaying: →false → restoring mic after 2000ms")
                    agentAudioPlaying = false
                    _state.value = VoiceState.Active
                    // Re-duck in case restore already fired from response.audio.done race
                    duckMicForAgentSpeech()
                    restoreMicAfterAgentSpeech(delayMs = 2000L)
                }
                "response.audio.done" -> {
                    // Audio data is done being SENT — speaker buffer may still be draining.
                    // Do NOT restore mic here; wait for output_audio_buffer.stopped/cleared.
                    Log.i(TAG, "[VOICE_EVENT] ${t()} RESPONSE.AUDIO.DONE — not restoring mic (waiting for buffer stopped/cleared)")
                }
            }

        } catch (e: Exception) {
            Log.e(TAG, "[VM] ${t()} ERROR parsing data channel message: ${e.message}", e)
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
     * Duck mic while agent is speaking: disable track + zero gain to minimize echo pickup.
     * Saves current gain for restore. No-op if already ducked.
     */
    private fun duckMicForAgentSpeech() {
        if (gainBeforeSpeaking == null) {
            gainBeforeSpeaking = micGainLevel
            micGainLevel = echoDuckingGain
            // Keep track enabled at low gain so loud user speech can still interrupt
            localAudioTrack?.setEnabled(true)
            Log.i(TAG, "[MIC_STATE] ${t()} DUCK → gain: ${gainBeforeSpeaking}→$echoDuckingGain trackEnabled: true agentPlaying=$agentAudioPlaying")
        } else {
            Log.d(TAG, "[MIC_STATE] ${t()} DUCK (already ducked, no-op) | gain=$micGainLevel gainSaved=$gainBeforeSpeaking trackEnabled=${localAudioTrack?.enabled()}")
        }
    }

    /**
     * Restore mic after agent speech ends — delay so echo tail dies out, then re-enable track.
     */
    private fun restoreMicAfterAgentSpeech(delayMs: Long = 2000L) {
        if (gainBeforeSpeaking == null) {
            Log.d(TAG, "[MIC_STATE] ${t()} RESTORE_DELAYED (no-op, not ducked) delayMs=$delayMs")
            return
        }
        micRestoreJob?.cancel()
        Log.i(TAG, "[MIC_STATE] ${t()} RESTORE_DELAYED scheduled in ${delayMs}ms | savedGain=$gainBeforeSpeaking")
        micRestoreJob = scope.launch {
            delay(delayMs)
            gainBeforeSpeaking?.let { saved ->
                micGainLevel = saved
                gainBeforeSpeaking = null
                Log.i(TAG, "[MIC_STATE] ${t()} RESTORE_DONE → gain: 0.05→$micGainLevel (after ${delayMs}ms delay)")
            }
        }
    }

    /**
     * Restore mic immediately (user interrupted — re-enable track right away).
     */
    private fun restoreMicImmediately() {
        micRestoreJob?.cancel()
        micRestoreJob = null
        gainBeforeSpeaking?.let { saved ->
            micGainLevel = saved
            gainBeforeSpeaking = null
            Log.i(TAG, "[MIC_STATE] ${t()} RESTORE_IMMEDIATE → gain: 0.05→$micGainLevel")
        } ?: Log.d(TAG, "[MIC_STATE] ${t()} RESTORE_IMMEDIATE (no-op, not ducked)")
    }

    /**
     * Set audio output routing. If called during an active session, routing is reapplied
     * immediately; otherwise it takes effect when the next session starts.
     */
    fun setAudioOutput(output: AudioOutput) {
        audioOutput = output
        Log.d(TAG, "Audio output set to: $output")
        // If we're mid-session (audioManager already initialized), apply right away so
        // the user hears the change without having to stop/start the voice session.
        if (audioManager != null) {
            applySpeakerRouting()
        }
    }

    /**
     * Whether a Bluetooth audio output device (A2DP/HEADSET/SCO/BLE) is currently available.
     * Used by the UI to enable/disable the BLUETOOTH segment.
     *
     * Safe to call at any time — lazily initializes AudioManager if needed.
     */
    fun isBluetoothAudioAvailable(): Boolean {
        val am = audioManager ?: (context.getSystemService(Context.AUDIO_SERVICE) as? AudioManager)
            ?.also { audioManager = it }
            ?: return false
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            // API 23+: enumerate output devices — this only returns currently connected ones.
            val devices = am.getDevices(AudioManager.GET_DEVICES_OUTPUTS)
            return devices.any { isBluetoothDevice(it.type) }
        }
        // API 21–22: AudioManager.isBluetoothScoAvailableOffCall reports the phone's
        // HARDWARE capability, not whether a device is actually connected — so it always
        // returns true on most phones. Use BluetoothAdapter's profile connection state
        // instead, which reflects live connection status.
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

    /**
     * Apply routing based on [audioOutput]. Called at session start, after data channel opens,
     * and whenever the user changes the setting mid-session.
     *
     * Two code paths:
     * - **API 31+**: prefer [AudioManager.setCommunicationDevice] — the only reliable API
     *   on Android 12+. `setSpeakerphoneOn()` is deprecated and often silently rejected.
     * - **API 21–30**: legacy path using `setSpeakerphoneOn` + `startBluetoothSco`.
     *
     * If the requested device isn't available (e.g. BLUETOOTH picked but no BT device
     * connected, or earpiece missing on a tablet), falls back to loudspeaker.
     */
    private fun applySpeakerRouting() {
        val am = audioManager ?: return
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            applyRoutingModern(am)
        } else {
            applyRoutingLegacy(am)
        }
        // Post-write verification — useful for debugging devices where the call silently no-ops.
        @Suppress("DEPRECATION")
        Log.d(TAG, "[ROUTE] after applySpeakerRouting: target=$audioOutput speakerOn=${am.isSpeakerphoneOn} scoOn=${am.isBluetoothScoOn} mode=${am.mode}")
    }

    @androidx.annotation.RequiresApi(Build.VERSION_CODES.S)
    private fun applyRoutingModern(am: AudioManager) {
        val devices = am.availableCommunicationDevices
        val target: android.media.AudioDeviceInfo? = when (audioOutput) {
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
                // BT sometimes refuses right after connect — retry once with SPEAKER as safety net
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

    /**
     * Set microphone gain level.
     * @param gain Gain level from 0.0 (silent) to 2.0 (double volume). Default is 1.0.
     */
    fun setMicGain(gain: Float) {
        micGainLevel = gain.coerceIn(0.0f, 2.0f)
        Log.d(TAG, "Mic gain set to: $micGainLevel")
    }

    /**
     * Get current mic gain level.
     */
    fun getMicGain(): Float = micGainLevel

    /**
     * Set the echo ducking gain — applied to mic while agent is speaking.
     * 0.0 = fully muted, 0.05 = 5% (default), 0.1 = 10%, etc.
     */
    fun setEchoDuckingGain(gain: Float) {
        echoDuckingGain = gain.coerceIn(0.0f, 1.0f)
        Log.d(TAG, "Echo ducking gain set to: $echoDuckingGain")
    }

    /**
     * Audio data callback that applies gain to microphone input.
     * Called by JavaAudioDeviceModule before audio is fed into WebRTC.
     * Also emits periodic [AUDIO_RMS] logs to confirm whether mic is live or muted.
     */
    private val audioRecordDataCallback = object : AudioRecordDataCallback {
        override fun onAudioDataRecorded(audioFormat: Int, channelCount: Int, sampleRate: Int, audioBuffer: ByteBuffer) {
            // Apply gain to the audio samples
            if (micGainLevel != 1.0f) {
                applyGainToBuffer(audioBuffer, micGainLevel)
            }

            // Periodic RMS log — every RMS_LOG_INTERVAL callbacks (~500ms) so we can confirm
            // whether audio reaching WebRTC is actually silent or live.
            rmsLogCounter++
            if (rmsLogCounter >= RMS_LOG_INTERVAL) {
                rmsLogCounter = 0
                val rms = computeRms(audioBuffer)
                Log.d(TAG, "[AUDIO_RMS] ${t()} rms=${"%.1f".format(rms)} gain=$micGainLevel trackEnabled=${localAudioTrack?.enabled()} agentPlaying=$agentAudioPlaying")
            }
        }
    }

    /** Compute RMS of 16-bit PCM samples in the buffer (does not advance buffer position). */
    private fun computeRms(buffer: ByteBuffer): Double {
        val originalOrder = buffer.order()
        buffer.order(ByteOrder.LITTLE_ENDIAN)
        val pos = buffer.position()
        val lim = buffer.limit()
        var sumSq = 0.0
        var count = 0
        var i = pos
        while (i < lim - 1) {
            val s = buffer.getShort(i).toDouble()
            sumSq += s * s
            count++
            i += 2
        }
        buffer.order(originalOrder)
        return if (count > 0) Math.sqrt(sumSq / count) else 0.0
    }

    /**
     * Apply gain to audio samples in a ByteBuffer.
     * Audio is in 16-bit PCM format (shorts).
     * Gain is applied directly: 1.0 = normal, 0.5 = half volume, 0.05 = 5%, etc.
     */
    private fun applyGainToBuffer(buffer: ByteBuffer, gain: Float) {
        // Audio samples are 16-bit signed PCM (shorts)
        val originalOrder = buffer.order()
        buffer.order(ByteOrder.LITTLE_ENDIAN)

        val position = buffer.position()
        val limit = buffer.limit()

        // Process each 16-bit sample
        var i = position
        while (i < limit - 1) {
            val sample = buffer.getShort(i).toInt()
            // Apply gain directly and clamp to prevent clipping
            val amplified = (sample * gain).toInt().coerceIn(Short.MIN_VALUE.toInt(), Short.MAX_VALUE.toInt())
            buffer.putShort(i, amplified.toShort())
            i += 2
        }

        buffer.order(originalOrder)
    }


    /**
     * Stop voice session and cleanup.
     * Web frontend: stopVoice() in useVoiceOrchestrator
     */
    fun stop() {
        Log.i(TAG, "[VM] ${t()} ===== SESSION STOP =====")
        cleanup()
        _state.value = VoiceState.Off
        _events.tryEmit(VoiceEvent.SessionEnded)
    }

    private fun cleanup() {
        Log.i(TAG, "[VM] ${t()} cleanup() | agentPlaying=$agentAudioPlaying gain=$micGainLevel gainSaved=$gainBeforeSpeaking")

        // Restore mic gain if session ends while agent was speaking
        micRestoreJob?.cancel()
        micRestoreJob = null
        gainBeforeSpeaking?.let { micGainLevel = it }
        gainBeforeSpeaking = null
        agentAudioPlaying = false

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
