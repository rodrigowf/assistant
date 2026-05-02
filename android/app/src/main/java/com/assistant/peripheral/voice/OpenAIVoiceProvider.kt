package com.assistant.peripheral.voice

import android.content.Context
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.NoiseSuppressor
import android.os.Build
import android.util.Log
import com.assistant.peripheral.data.VoiceState
import com.assistant.peripheral.network.ApiClient
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.*
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import org.json.JSONObject
import org.webrtc.*
import org.webrtc.audio.AudioRecordDataCallback
import org.webrtc.audio.JavaAudioDeviceModule
import org.webrtc.voiceengine.WebRtcAudioUtils
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

/**
 * OpenAI Realtime voice provider — owns ALL WebRTC plumbing.
 *
 * The browser-side equivalent is `useVoiceSession` + the WebRTC
 * transport in `frontend/src/voice/transports/webrtc.ts`.
 *
 * Architecture:
 *   1. [VoiceManager] requests connection metadata from the backend
 *      (`POST /api/orchestrator/voice/session?provider=openai&...`).
 *   2. [VoiceManager] hands the resulting [VoiceConnectionInfo] to
 *      [connect], which:
 *        - Creates a WebRTC `PeerConnection` with mic capture
 *        - Exchanges SDP with OpenAI Realtime (`POST /v1/realtime/calls`)
 *        - Opens an `oai-events` data channel for events
 *   3. Every event from OpenAI is mirrored to the backend via
 *      `mirrorToBackend` so the orchestrator can persist transcripts
 *      and execute tools.
 *   4. Audio bypasses our backend entirely — the browser/phone <-> OpenAI
 *      voice path is direct.
 *
 * NOT this class's job:
 *   - System audio focus / speaker routing — that belongs to
 *     [VoiceManager] and lives there because it's OS-level, not
 *     transport-specific.
 *   - Backend WebSocket handling — also [VoiceManager]'s job.
 */
class OpenAIVoiceProvider(
    private val context: Context,
    private val apiClient: ApiClient,
) : VoiceProvider {

    companion object {
        private const val TAG = "OpenAIVoiceProvider"
        private const val CONNECTION_TIMEOUT_MS = 15_000L
        private const val RMS_LOG_INTERVAL = 50
        // PeerConnectionFactory.initialize() is process-wide; only call once.
        @Volatile private var peerConnectionFactoryInitialized = false
    }

    override val providerId: String = "openai"
    override val connectionType: VoiceConnectionType = VoiceConnectionType.WEBRTC

    private val _state = MutableStateFlow<VoiceState>(VoiceState.Off)
    override val state: StateFlow<VoiceState> = _state.asStateFlow()

    private val _events = MutableSharedFlow<VoiceEvent>(extraBufferCapacity = 64)
    override val events: SharedFlow<VoiceEvent> = _events.asSharedFlow()

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    // --- WebRTC plumbing ---------------------------------------------------
    private var peerConnection: PeerConnection? = null
    private var dataChannel: DataChannel? = null
    private var localAudioTrack: AudioTrack? = null
    private var peerConnectionFactory: PeerConnectionFactory? = null
    private var acousticEchoCanceler: AcousticEchoCanceler? = null
    private var noiseSuppressor: NoiseSuppressor? = null

    // Connection-level state set by the data channel observer.
    private var dcReady = false
    private val pendingCommands = mutableListOf<Map<String, Any?>>()

    // --- Mic gain + ducking -----------------------------------------------
    // These manipulate the mic stream BEFORE WebRTC processes it via the
    // JavaAudioDeviceModule callback — so they're inherently WebRTC-coupled
    // and live here, not in VoiceManager.
    private var micGainLevel: Float = 1.0f
    private var echoDuckingGain: Float = 0.05f
    private var gainBeforeSpeaking: Float? = null
    private var micRestoreJob: Job? = null
    private var agentAudioPlaying: Boolean = false
    private var userMuted: Boolean = false

    // --- Diagnostic timing -------------------------------------------------
    private var sessionStartMs: Long = 0L
    private var rmsLogCounter: Int = 0

    // --- Bridge to the backend WS (set in connect) ------------------------
    private var mirrorEventToBackend: ((Map<String, Any?>) -> Unit)? = null

    /** Endpoint the SDP exchange should POST to.  Provided by [VoiceConnectionInfo.endpoint]. */
    private var sdpEndpoint: String = "https://api.openai.com/v1/realtime/calls?model=gpt-realtime"
    /** Ephemeral token for the SDP `Authorization: Bearer` header. */
    private var ephemeralToken: String = ""

    private fun t(): String {
        val elapsed = if (sessionStartMs > 0) System.currentTimeMillis() - sessionStartMs else 0L
        return "[+${elapsed}ms]"
    }

    private fun logMicState(action: String, extra: String = "") {
        Log.i(TAG, "[MIC_STATE] ${t()} $action | gain=$micGainLevel gainSaved=$gainBeforeSpeaking " +
                "agentPlaying=$agentAudioPlaying trackEnabled=${localAudioTrack?.enabled()} $extra")
    }

    // --- VoiceProvider lifecycle ------------------------------------------

    override suspend fun connect(
        info: VoiceConnectionInfo,
        mirrorEventToBackend: (Map<String, Any?>) -> Unit,
        sendMicChunkToBackend: (String) -> Unit,
    ) = withContext(Dispatchers.IO) {
        if (_state.value != VoiceState.Off && _state.value !is VoiceState.Error) {
            Log.w(TAG, "[VM] Voice session already active, state=${_state.value}")
            return@withContext
        }

        if (info.ephemeralToken.isNullOrEmpty()) {
            val msg = "OpenAI voice session missing ephemeral token"
            Log.e(TAG, msg)
            _state.value = VoiceState.Error(msg)
            _events.tryEmit(VoiceEvent.Error(msg))
            return@withContext
        }

        this@OpenAIVoiceProvider.mirrorEventToBackend = mirrorEventToBackend
        // sendMicChunkToBackend is unused for WebRTC — audio bypasses us.
        this@OpenAIVoiceProvider.ephemeralToken = info.ephemeralToken
        // info.endpoint is already the full /v1/realtime/calls URL (with model query).
        if (info.endpoint.isNotEmpty()) {
            this@OpenAIVoiceProvider.sdpEndpoint = info.endpoint
        }

        sessionStartMs = System.currentTimeMillis()
        rmsLogCounter = 0
        _state.value = VoiceState.Connecting
        dcReady = false
        pendingCommands.clear()
        Log.i(TAG, "[VM] ===== SESSION START ===== epochMs=$sessionStartMs endpoint=${this@OpenAIVoiceProvider.sdpEndpoint}")

        try {
            val success = withTimeoutOrNull(CONNECTION_TIMEOUT_MS) {
                initializeWebRTC(this@OpenAIVoiceProvider.ephemeralToken)
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

    override suspend fun disconnect() {
        Log.i(TAG, "[VM] ${t()} ===== SESSION STOP =====")
        cleanup()
        _state.value = VoiceState.Off
        _events.tryEmit(VoiceEvent.SessionEnded)
    }

    override fun handleBackendCommand(command: Map<String, Any?>) {
        // Backend → OpenAI command — forward via data channel.
        sendToOpenAI(command)
    }

    override fun toggleMute(): Boolean {
        userMuted = !userMuted
        // Apply to the track. When unmuting mid-duck we still want the track enabled
        // so the user can interrupt; gain ducking continues independently.
        localAudioTrack?.setEnabled(!userMuted)
        Log.i(TAG, "[MIC_STATE] ${t()} TOGGLE_MUTE → userMuted=$userMuted trackEnabled=${!userMuted}")
        return userMuted
    }

    override fun isMuted(): Boolean = userMuted

    override fun setMicGain(level: Float) {
        micGainLevel = level.coerceIn(0.0f, 2.0f)
        Log.d(TAG, "Mic gain set to: $micGainLevel")
    }

    fun getMicGain(): Float = micGainLevel

    override fun setEchoDuckingGain(gain: Float) {
        echoDuckingGain = gain.coerceIn(0.0f, 1.0f)
        Log.d(TAG, "Echo ducking gain set to: $echoDuckingGain")
    }

    /** Cancel pending coroutines and tear down everything. */
    fun release() {
        runBlocking { disconnect() }
        scope.cancel()
    }

    // --- Mic ducking (couples to the gain callback below) -----------------

    private fun duckMicForAgentSpeech() {
        if (gainBeforeSpeaking == null) {
            gainBeforeSpeaking = micGainLevel
            micGainLevel = echoDuckingGain
            if (!userMuted) {
                localAudioTrack?.setEnabled(true)
            }
            Log.i(TAG, "[MIC_STATE] ${t()} DUCK → gain: ${gainBeforeSpeaking}→$echoDuckingGain " +
                    "trackEnabled=${localAudioTrack?.enabled()} userMuted=$userMuted agentPlaying=$agentAudioPlaying")
        } else {
            Log.d(TAG, "[MIC_STATE] ${t()} DUCK (already ducked, no-op) | gain=$micGainLevel " +
                    "gainSaved=$gainBeforeSpeaking trackEnabled=${localAudioTrack?.enabled()} userMuted=$userMuted")
        }
    }

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

    private fun restoreMicImmediately() {
        micRestoreJob?.cancel()
        micRestoreJob = null
        gainBeforeSpeaking?.let { saved ->
            micGainLevel = saved
            gainBeforeSpeaking = null
            Log.i(TAG, "[MIC_STATE] ${t()} RESTORE_IMMEDIATE → gain: 0.05→$micGainLevel")
        } ?: Log.d(TAG, "[MIC_STATE] ${t()} RESTORE_IMMEDIATE (no-op, not ducked)")
    }

    // --- Mic gain callback (WebRTC pre-processing hook) -------------------

    /** Called by JavaAudioDeviceModule before audio is fed into WebRTC. */
    private val audioRecordDataCallback = object : AudioRecordDataCallback {
        override fun onAudioDataRecorded(audioFormat: Int, channelCount: Int, sampleRate: Int, audioBuffer: ByteBuffer) {
            if (micGainLevel != 1.0f) {
                applyGainToBuffer(audioBuffer, micGainLevel)
            }
            rmsLogCounter++
            if (rmsLogCounter >= RMS_LOG_INTERVAL) {
                rmsLogCounter = 0
                val rms = computeRms(audioBuffer)
                Log.d(TAG, "[AUDIO_RMS] ${t()} rms=${"%.1f".format(rms)} gain=$micGainLevel " +
                        "trackEnabled=${localAudioTrack?.enabled()} agentPlaying=$agentAudioPlaying")
            }
        }
    }

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

    private fun applyGainToBuffer(buffer: ByteBuffer, gain: Float) {
        val originalOrder = buffer.order()
        buffer.order(ByteOrder.LITTLE_ENDIAN)
        val position = buffer.position()
        val limit = buffer.limit()
        var i = position
        while (i < limit - 1) {
            val sample = buffer.getShort(i).toInt()
            val amplified = (sample * gain).toInt().coerceIn(Short.MIN_VALUE.toInt(), Short.MAX_VALUE.toInt())
            buffer.putShort(i, amplified.toShort())
            i += 2
        }
        buffer.order(originalOrder)
    }

    // --- WebRTC initialization --------------------------------------------

    private suspend fun initializeWebRTC(token: String): Boolean {
        // SOFTWARE-ONLY AEC: Disable hardware AEC, use only WebRTC software processing.
        WebRtcAudioUtils.setWebRtcBasedAcousticEchoCanceler(true)
        WebRtcAudioUtils.setWebRtcBasedNoiseSuppressor(true)
        WebRtcAudioUtils.setWebRtcBasedAutomaticGainControl(true)
        Log.d(TAG, ">>> Enabled WebRTC SOFTWARE AEC, NS, and AGC")

        if (!peerConnectionFactoryInitialized) {
            val initOptions = PeerConnectionFactory.InitializationOptions.builder(context)
                .setEnableInternalTracer(false)
                .createInitializationOptions()
            PeerConnectionFactory.initialize(initOptions)
            peerConnectionFactoryInitialized = true
        }

        val hwAecAvailable = AcousticEchoCanceler.isAvailable()
        val hwNsAvailable = NoiseSuppressor.isAvailable()
        Log.d(TAG, "Hardware AEC available: $hwAecAvailable (DISABLED), NS available: $hwNsAvailable (DISABLED)")

        // Use VOICE_RECOGNITION on Lollipop (API < 24): Samsung's HAL routes VOICE_COMMUNICATION
        // through aggressive noise processing that silences audio when MODE_NORMAL is active.
        val micAudioSource = if (Build.VERSION.SDK_INT < Build.VERSION_CODES.N)
            MediaRecorder.AudioSource.VOICE_RECOGNITION
        else
            MediaRecorder.AudioSource.VOICE_COMMUNICATION

        val audioDeviceModule = JavaAudioDeviceModule.builder(context)
            .setUseHardwareAcousticEchoCanceler(false)
            .setUseHardwareNoiseSuppressor(false)
            .setAudioRecordDataCallback(audioRecordDataCallback)
            .setAudioSource(micAudioSource)
            .createAudioDeviceModule()
        Log.d(TAG, "Audio source: ${if (micAudioSource == MediaRecorder.AudioSource.VOICE_RECOGNITION) "VOICE_RECOGNITION" else "VOICE_COMMUNICATION"}")

        peerConnectionFactory = PeerConnectionFactory.builder()
            .setOptions(PeerConnectionFactory.Options())
            .setAudioDeviceModule(audioDeviceModule)
            .createPeerConnectionFactory()

        val factory = peerConnectionFactory!!

        val audioConstraints = MediaConstraints().apply {
            mandatory.add(MediaConstraints.KeyValuePair("echoCancellation", "true"))
            mandatory.add(MediaConstraints.KeyValuePair("noiseSuppression", "true"))
            mandatory.add(MediaConstraints.KeyValuePair("autoGainControl", "true"))
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
        userMuted = false
        localAudioTrack?.setEnabled(true)

        val rtcConfig = PeerConnection.RTCConfiguration(emptyList()).apply {
            sdpSemantics = PeerConnection.SdpSemantics.UNIFIED_PLAN
            bundlePolicy = PeerConnection.BundlePolicy.MAXBUNDLE
        }

        peerConnection = factory.createPeerConnection(rtcConfig, object : PeerConnection.Observer {
            override fun onSignalingChange(state: PeerConnection.SignalingState) {
                Log.d(TAG, "Signaling state: $state")
            }
            override fun onIceConnectionChange(state: PeerConnection.IceConnectionState) {
                Log.d(TAG, "ICE connection state: $state")
                when (state) {
                    PeerConnection.IceConnectionState.CONNECTED -> Log.d(TAG, "ICE connected")
                    PeerConnection.IceConnectionState.DISCONNECTED -> {
                        Log.w(TAG, "ICE disconnected")
                        handleConnectionClosed()
                    }
                    PeerConnection.IceConnectionState.FAILED -> {
                        Log.e(TAG, "ICE connection failed")
                        _state.value = VoiceState.Error("Connection failed")
                        _events.tryEmit(VoiceEvent.Error("Connection failed"))
                        scope.launch { disconnect() }
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
                Log.d(TAG, "Remote stream added with ${stream.audioTracks.size} audio tracks")
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

        localAudioTrack?.let { track ->
            peerConnection?.addTrack(track, listOf("stream0"))
        }
        peerConnection?.addTransceiver(
            MediaStreamTrack.MediaType.MEDIA_TYPE_AUDIO,
            RtpTransceiver.RtpTransceiverInit(RtpTransceiver.RtpTransceiverDirection.RECV_ONLY)
        )

        val dcInit = DataChannel.Init().apply { ordered = true }
        dataChannel = peerConnection?.createDataChannel("oai-events", dcInit)
        setupDataChannel(dataChannel!!)

        return suspendCancellableCoroutine { cont ->
            val offerConstraints = MediaConstraints()
            peerConnection?.createOffer(object : SdpObserver {
                override fun onCreateSuccess(sdp: SessionDescription) {
                    Log.d(TAG, "SDP offer created")
                    peerConnection?.setLocalDescription(object : SdpObserver {
                        override fun onSetSuccess() {
                            Log.d(TAG, "Local description set")
                            scope.launch {
                                val ok = exchangeSDP(sdp.description, token)
                                if (cont.isActive) cont.resume(ok)
                            }
                        }
                        override fun onSetFailure(error: String) {
                            Log.e(TAG, "Failed to set local description: $error")
                            _state.value = VoiceState.Error(error)
                            if (cont.isActive) cont.resume(false)
                        }
                        override fun onCreateSuccess(sdp: SessionDescription?) {}
                        override fun onCreateFailure(error: String?) {}
                    }, sdp)
                }
                override fun onCreateFailure(error: String) {
                    Log.e(TAG, "Failed to create SDP offer: $error")
                    _state.value = VoiceState.Error(error)
                    if (cont.isActive) cont.resume(false)
                }
                override fun onSetSuccess() {}
                override fun onSetFailure(error: String?) {}
            }, offerConstraints)
        }
    }

    private suspend fun exchangeSDP(localSdp: String, token: String): Boolean = withContext(Dispatchers.IO) {
        try {
            Log.d(TAG, "Exchanging SDP with OpenAI at $sdpEndpoint")

            // Reuse ApiClient's shared OkHttp instance — avoids
            // creating a third HTTP thread pool for a single one-shot
            // POST.  ApiClient's 30s read timeout matches what we need
            // for OpenAI's SDP response.
            val httpClient = apiClient.httpClient

            val body = okhttp3.RequestBody.create(
                "application/sdp".toMediaTypeOrNull(),
                localSdp
            )
            val request = okhttp3.Request.Builder()
                .url(sdpEndpoint)
                .post(body)
                .addHeader("Authorization", "Bearer $token")
                .addHeader("Content-Type", "application/sdp")
                .build()

            val response = httpClient.newCall(request).execute()

            if (!response.isSuccessful) {
                val errorBody = response.body?.string() ?: "no body"
                Log.e(TAG, "SDP exchange failed: ${response.code} - $errorBody")
                _state.value = VoiceState.Error("SDP exchange failed: ${response.code}")
                _events.tryEmit(VoiceEvent.Error("OpenAI SDP exchange failed: ${response.code}"))
                return@withContext false
            }

            val remoteSdp = response.body?.string()
            if (remoteSdp == null) {
                _state.value = VoiceState.Error("Empty SDP response")
                return@withContext false
            }

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

    // --- Data channel ------------------------------------------------------

    private fun setupDataChannel(channel: DataChannel) {
        channel.registerObserver(object : DataChannel.Observer {
            override fun onBufferedAmountChange(previousAmount: Long) {}
            override fun onStateChange() {
                Log.d(TAG, "[VM] ${t()} Data channel state: ${channel.state()}")
                if (channel.state() == DataChannel.State.OPEN) {
                    dcReady = true
                    _state.value = VoiceState.Active
                    _events.tryEmit(VoiceEvent.SessionCreated)
                    Log.i(TAG, "[VM] ${t()} ===== DATA CHANNEL OPEN — session ready =====")
                    logMicState("DC_OPEN initial state")

                    // Drain pending commands
                    val pending = pendingCommands.toList()
                    pendingCommands.clear()
                    if (pending.isNotEmpty()) {
                        Log.d(TAG, "[VM] ${t()} Draining ${pending.size} pending commands")
                    }
                    for (cmd in pending) sendToOpenAI(cmd)
                }
            }
            override fun onMessage(buffer: DataChannel.Buffer) {
                val data = ByteArray(buffer.data.remaining())
                buffer.data.get(data)
                handleDataChannelMessage(String(data))
            }
        })
    }

    private fun handleDataChannelMessage(message: String) {
        try {
            val json = JSONObject(message)
            val eventType = json.optString("type", "")

            val isNoisyEvent = eventType == "response.audio.delta" || eventType == "response.audio_transcript.delta"
            if (!isNoisyEvent) {
                Log.d(TAG, "[VOICE_EVENT] ${t()} type=$eventType state=${_state.value} agentPlaying=$agentAudioPlaying gain=$micGainLevel gainSaved=$gainBeforeSpeaking")
            }

            // Mirror EVERY event to the backend.
            mirrorEventToBackend?.invoke(jsonObjectToMap(json))

            if (eventType == "error") {
                val errorObj = json.optJSONObject("error")
                val code = errorObj?.optString("code") ?: "unknown"
                val errorMessage = errorObj?.optString("message") ?: "Unknown error"
                Log.e(TAG, "[VOICE_EVENT] ${t()} ===== OPENAI ERROR: code=$code msg=$errorMessage =====")
                _state.value = if (code == "session_expired")
                    VoiceState.Error("Voice session expired — please restart")
                else
                    VoiceState.Error("Voice error: $code")
                _events.tryEmit(VoiceEvent.Error(errorMessage))
                cleanup()
                return
            }

            when (eventType) {
                "response.created" -> {
                    micRestoreJob?.cancel()
                    micRestoreJob = null
                    Log.i(TAG, "[VOICE_EVENT] ${t()} RESPONSE CREATED → ducking mic (restore timer cancelled)")
                    _state.value = VoiceState.Speaking
                    duckMicForAgentSpeech()
                }
                "response.done" -> {
                    Log.i(TAG, "[VOICE_EVENT] ${t()} RESPONSE DONE — not restoring mic yet, waiting for audio buffer")
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
                    Log.d(TAG, "[VOICE_EVENT] ${t()} TOOL_CALL name=$name callId=$callId")
                    try {
                        val args = JSONObject(argsStr)
                        _events.tryEmit(VoiceEvent.ToolUse(callId, name, jsonObjectToMap(args)))
                    } catch (e: Exception) {
                        _events.tryEmit(VoiceEvent.ToolUse(callId, name, emptyMap()))
                    }
                }
                "input_audio_buffer.speech_started" -> {
                    if (agentAudioPlaying) {
                        Log.w(TAG, "[VOICE_EVENT] ${t()} ===== SPEECH_STARTED (SUPPRESSED — echo while agent playing) ===== gain=$micGainLevel trackEnabled=${localAudioTrack?.enabled()}")
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
                    if (transcript.isNotEmpty()) _events.tryEmit(VoiceEvent.UserTranscript(transcript))
                }
                "response.audio_transcript.delta" -> {
                    _events.tryEmit(VoiceEvent.TextDelta(json.optString("delta", "")))
                }
                "response.audio_transcript.done" -> {
                    val transcript = json.optString("transcript", "")
                    Log.i(TAG, "[VOICE_EVENT] ${t()} AGENT_TRANSCRIPT: \"$transcript\"")
                    _events.tryEmit(VoiceEvent.TextComplete(transcript))
                }
                "output_audio_buffer.started" -> {
                    micRestoreJob?.cancel()
                    micRestoreJob = null
                    Log.i(TAG, "[VOICE_EVENT] ${t()} ===== AUDIO BUFFER STARTED (agent speaking) ===== agentPlaying: false→true (restore timer cancelled)")
                    agentAudioPlaying = true
                    if (_state.value != VoiceState.Speaking) _state.value = VoiceState.Speaking
                    duckMicForAgentSpeech()
                }
                "output_audio_buffer.stopped" -> {
                    Log.i(TAG, "[VOICE_EVENT] ${t()} ===== AUDIO BUFFER STOPPED (playback done) ===== agentPlaying: true→false → restoring mic after 2000ms")
                    agentAudioPlaying = false
                    _state.value = VoiceState.Active
                    restoreMicAfterAgentSpeech(delayMs = 2000L)
                }
                "output_audio_buffer.cleared" -> {
                    Log.i(TAG, "[VOICE_EVENT] ${t()} ===== AUDIO BUFFER CLEARED (interrupted) ===== agentPlaying: →false → restoring mic after 2000ms")
                    agentAudioPlaying = false
                    _state.value = VoiceState.Active
                    duckMicForAgentSpeech()
                    restoreMicAfterAgentSpeech(delayMs = 2000L)
                }
                "response.audio.done" -> {
                    Log.i(TAG, "[VOICE_EVENT] ${t()} RESPONSE.AUDIO.DONE — not restoring mic (waiting for buffer stopped/cleared)")
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error handling data channel message", e)
        }
    }

    /** Send a command to OpenAI via the data channel. Queued if not yet open. */
    private fun sendToOpenAI(command: Map<String, Any?>) {
        if (dcReady && dataChannel?.state() == DataChannel.State.OPEN) {
            val json = JSONObject(command)
            val buffer = DataChannel.Buffer(
                java.nio.ByteBuffer.wrap(json.toString().toByteArray()),
                false
            )
            dataChannel?.send(buffer)
        } else {
            pendingCommands.add(command)
        }
    }

    private fun handleConnectionClosed() {
        if (peerConnection != null) {
            if (_state.value != VoiceState.Off && _state.value !is VoiceState.Error) {
                _state.value = VoiceState.Error("Voice connection lost")
                _events.tryEmit(VoiceEvent.Error("Voice connection lost"))
            }
            cleanup()
        }
    }

    // --- Cleanup -----------------------------------------------------------

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

        try {
            acousticEchoCanceler?.release()
            acousticEchoCanceler = null
            noiseSuppressor?.release()
            noiseSuppressor = null
        } catch (e: Exception) {
            Log.w(TAG, "Error releasing audio effects", e)
        }
    }
}
