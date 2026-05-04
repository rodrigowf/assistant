package com.assistant.peripheral.voice

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.os.Build
import android.util.Base64
import android.util.Log
import androidx.core.content.ContextCompat
import com.assistant.peripheral.data.VoiceState
import kotlinx.coroutines.*
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.*
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.math.max
import kotlin.math.min

/**
 * Qwen-Omni Realtime voice provider — owns ALL WebSocket-path audio
 * plumbing on the Android side.
 *
 * Unlike [OpenAIVoiceProvider] (WebRTC), the upstream connection here
 * is owned entirely by the **backend** (`orchestrator/voice_relay.py`
 * on the server).  The Android client only ferries audio:
 *
 *   1. [VoiceManager] hands us [VoiceConnectionInfo] derived from the
 *      `session_started` payload.
 *   2. We open [AudioRecord] at the configured input sample rate and
 *      stream PCM chunks via [sendMicChunkToBackend], which the
 *      orchestrator WS layer wraps in `voice_audio_in` messages.
 *   3. The backend relay's drain loop emits matching `voice_audio_out`
 *      messages, which [VoiceManager] hands to us via [pushSpeakerChunk].
 *      We decode and feed [AudioTrack].
 *
 * What we do NOT own:
 *   - System audio focus / speaker routing — [VoiceManager]'s job.
 *   - The upstream Qwen WebSocket — backend's job.
 *   - JSON event parsing — backend already parses upstream events and
 *     mirrors them back to us via the orchestrator WS as `voice_event`
 *     messages, which the existing chat WebSocket handler converts to
 *     [VoiceEvent]s in [com.assistant.peripheral.network.WebSocketManager].
 *     So this provider's [events] flow primarily emits transport-level
 *     events ([VoiceEvent.SessionCreated], [VoiceEvent.SessionEnded],
 *     [VoiceEvent.Error]).  Higher-level transcript/tool events come
 *     from the WebSocket layer and the ViewModel reconciles both.
 *
 * Audio format defaults align with `qwen3.5-omni-plus-realtime` (24 kHz
 * PCM both directions).  Other Qwen variants use 16 kHz in / 24 kHz out
 * — read [VoiceConnectionInfo.audioInSampleRate] / `audioOutSampleRate`
 * to pick.
 */
class QwenVoiceProvider(
    private val context: Context,
) : VoiceProvider {

    companion object {
        private const val TAG = "QwenVoiceProvider"
        // Mic chunk size: 20ms at 24kHz mono PCM16 = 480 frames = 960 bytes.
        // The web frontend uses 20ms chunks too; matches Qwen's input cadence.
        private const val MIC_CHUNK_FRAMES = 480
    }

    override val providerId: String = "qwen"
    override val connectionType: VoiceConnectionType = VoiceConnectionType.WEBSOCKET

    private val _state = MutableStateFlow<VoiceState>(VoiceState.Off)
    override val state: StateFlow<VoiceState> = _state.asStateFlow()

    private val _events = MutableSharedFlow<VoiceEvent>(extraBufferCapacity = 64)
    override val events: SharedFlow<VoiceEvent> = _events.asSharedFlow()

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    // --- Audio I/O ---------------------------------------------------------
    private var audioRecord: AudioRecord? = null
    private var audioTrack: AudioTrack? = null
    private var captureJob: Job? = null
    private var playbackJob: Job? = null
    private val running = AtomicBoolean(false)
    /**
     * Queue of decoded PCM speaker chunks waiting to be written to
     * [AudioTrack].  Decoupling the WS receive thread from the
     * AudioTrack.write() blocking call is critical: writing on the
     * main thread (which the WS event dispatch runs on by default)
     * caused 4,500-frame UI freezes mid-conversation.
     *
     * UNLIMITED capacity intentionally — a 200ms hiccup might queue
     * ~10 chunks; we want to never drop audio at this layer (the
     * upstream provider already controls our backpressure).
     */
    private val speakerQueue = Channel<ByteArray>(capacity = Channel.UNLIMITED)

    // --- Mic gain + ducking -----------------------------------------------
    private var micGainLevel: Float = 1.0f
    private var echoDuckingGain: Float = 0.05f
    private var gainBeforeSpeaking: Float? = null
    private var userMuted: Boolean = false

    // --- Format (set in connect) ------------------------------------------
    private var inSampleRate: Int = 24000
    private var outSampleRate: Int = 24000

    // --- Bridge to the backend WS (set in connect) ------------------------
    private var sendMicChunk: ((String) -> Unit)? = null

    // --- Lifecycle ---------------------------------------------------------

    override suspend fun connect(
        info: VoiceConnectionInfo,
        mirrorEventToBackend: (Map<String, Any?>) -> Unit,
        sendMicChunkToBackend: (String) -> Unit,
    ) = withContext(Dispatchers.IO) {
        if (_state.value != VoiceState.Off && _state.value !is VoiceState.Error) {
            Log.w(TAG, "Voice session already active, state=${_state.value}")
            return@withContext
        }

        if (ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED) {
            val msg = "RECORD_AUDIO permission not granted"
            Log.e(TAG, msg)
            _state.value = VoiceState.Error(msg)
            _events.tryEmit(VoiceEvent.Error(msg))
            return@withContext
        }

        inSampleRate = info.audioInSampleRate
        outSampleRate = info.audioOutSampleRate
        sendMicChunk = sendMicChunkToBackend
        // mirrorEventToBackend is unused on the WS path (the backend
        // already sees its own upstream events directly).

        Log.i(TAG, "Connecting Qwen voice: in=${inSampleRate}Hz out=${outSampleRate}Hz model=${info.model} voice=${info.voice}")
        _state.value = VoiceState.Connecting
        userMuted = false
        gainBeforeSpeaking = null

        try {
            // Set `running` BEFORE startMic — the mic capture loop
            // checks `running.get()` in its condition, so flipping
            // the flag after the coroutine launches can race and
            // exit it immediately.
            running.set(true)
            startSpeaker()
            startMic()
            _state.value = VoiceState.Active
            _events.tryEmit(VoiceEvent.SessionCreated)
            Log.i(TAG, "Qwen voice session ready")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to start Qwen voice: ${e.message}", e)
            _state.value = VoiceState.Error(e.message ?: "Qwen start failed")
            _events.tryEmit(VoiceEvent.Error(e.message ?: "Qwen start failed"))
            cleanup()
        }
    }

    override suspend fun disconnect() {
        Log.i(TAG, "Disconnecting Qwen voice")
        cleanup()
        _state.value = VoiceState.Off
        _events.tryEmit(VoiceEvent.SessionEnded)
    }

    /** No-op for WS path — the backend relay forwards commands directly upstream. */
    override fun handleBackendCommand(command: Map<String, Any?>) {
        // Intentional: the backend's voice_relay.py handles upstream
        // commands.  Anything that needs to round-trip through the
        // client lands as a `voice_event` server message and is handled
        // by the WebSocket layer instead.
    }

    /**
     * Parse a provider event mirrored from the backend and emit the
     * appropriate [VoiceEvent]s.  Qwen-Omni uses byte-identical event
     * names to OpenAI Realtime, so the dispatch is structurally
     * similar.  Differs from OpenAI:
     *   - We don't have a local audio track to duck — the speaker is
     *     [AudioTrack] which the user can mute via the volume slider.
     *   - We don't manage mic gain via the WebRTC audio device module;
     *     gain is applied directly to PCM in the capture loop.
     */
    override fun handleProviderEvent(event: Map<String, Any?>) {
        val eventType = event["type"] as? String ?: return
        when (eventType) {
            "error" -> {
                // The WS layer hands us shallow maps for voice events,
                // so nested objects arrive as JSONObject (not Map).
                // Read both forms defensively in case future producers
                // send a fully-walked Map.
                val (code, msg) = readErrorFields(event["error"])
                Log.e(TAG, "Qwen upstream error code=$code msg=$msg")
                _events.tryEmit(VoiceEvent.Error(msg))
            }
            "response.created" -> {
                _state.value = VoiceState.Speaking
            }
            "response.done" -> {
                _state.value = VoiceState.Active
                _events.tryEmit(VoiceEvent.TurnComplete)
            }
            "response.output_item.added" -> {
                if (readNestedString(event["item"], "type") == "function_call") {
                    _state.value = VoiceState.ToolUse
                }
            }
            "response.function_call_arguments.done" -> {
                _state.value = VoiceState.Thinking
                val callId = event["call_id"] as? String ?: ""
                val name = event["name"] as? String ?: ""
                val argsStr = event["arguments"] as? String ?: "{}"
                val args = try {
                    @Suppress("UNCHECKED_CAST")
                    jsonObjectToMap(org.json.JSONObject(argsStr))
                } catch (_: Exception) {
                    emptyMap()
                }
                _events.tryEmit(VoiceEvent.ToolUse(callId, name, args))
            }
            "input_audio_buffer.speech_started" -> {
                // Server VAD detected user barge-in. Drop any speaker audio
                // we've buffered (channel + AudioTrack hardware buffer) so the
                // model's previous turn cuts immediately instead of playing
                // the residue while the new turn waits.
                flushSpeakerOutput()
                _state.value = VoiceState.Active
                _events.tryEmit(VoiceEvent.SpeechStarted)
            }
            "input_audio_buffer.speech_stopped" -> {
                _state.value = VoiceState.Thinking
                _events.tryEmit(VoiceEvent.SpeechStopped)
            }
            "conversation.item.input_audio_transcription.completed" -> {
                val transcript = event["transcript"] as? String ?: ""
                if (transcript.isNotEmpty()) _events.tryEmit(VoiceEvent.UserTranscript(transcript))
            }
            "response.audio_transcript.delta" -> {
                val delta = event["delta"] as? String ?: ""
                if (delta.isNotEmpty()) _events.tryEmit(VoiceEvent.TextDelta(delta))
            }
            "response.audio_transcript.done" -> {
                val transcript = event["transcript"] as? String ?: ""
                _events.tryEmit(VoiceEvent.TextComplete(transcript))
            }
            // session.created / session.updated / response.audio.* etc.
            // are noise from the client's perspective — backend persists
            // them; we don't need to react.
        }
    }

    /**
     * Drop all queued + in-flight speaker audio.
     *
     * Called on barge-in so the previous response cuts mid-sentence
     * instead of finishing on top of the new turn.  Pause + flush + play
     * is the documented dance for clearing the [AudioTrack] hardware
     * buffer in [AudioTrack.MODE_STREAM].
     *
     * Safe to call from any thread; no-ops if not running.
     */
    private fun flushSpeakerOutput() {
        if (!running.get()) return
        var dropped = 0
        while (true) {
            val r = speakerQueue.tryReceive()
            if (r.isFailure || r.isClosed) break
            dropped++
        }
        try {
            audioTrack?.let {
                if (it.state == AudioTrack.STATE_INITIALIZED) {
                    it.pause()
                    it.flush()
                    it.play()
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "AudioTrack flush failed: ${e.message}")
        }
        if (dropped > 0) Log.d(TAG, "Barge-in: dropped $dropped queued speaker chunks")
    }

    override fun pushSpeakerChunk(audioB64: String) {
        // Fast path: decode here (cheap), then hand off to the
        // playback coroutine.  AudioTrack.write() blocks when the
        // buffer is full — calling it on the WS dispatch thread
        // (which is Main by default in viewModelScope) freezes the
        // UI.  The playback coroutine on Dispatchers.IO writes
        // without affecting frame timing.
        if (!running.get()) return
        try {
            val pcm = Base64.decode(audioB64, Base64.NO_WRAP)
            // trySend is non-blocking; UNLIMITED channel means it
            // never returns Failure — but guard anyway.
            val result = speakerQueue.trySend(pcm)
            if (result.isFailure) {
                Log.w(TAG, "speakerQueue full or closed; dropping chunk (${pcm.size}B)")
            }
        } catch (e: Exception) {
            Log.w(TAG, "Failed to decode speaker chunk: ${e.message}")
        }
    }

    override fun toggleMute(): Boolean {
        userMuted = !userMuted
        Log.i(TAG, "[MIC] TOGGLE_MUTE → userMuted=$userMuted")
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

    fun release() {
        runBlocking { disconnect() }
        scope.cancel()
    }

    // --- AudioTrack (speaker) ---------------------------------------------

    private fun startSpeaker() {
        val minBuf = AudioTrack.getMinBufferSize(
            outSampleRate,
            AudioFormat.CHANNEL_OUT_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
        )
        // 1.5s hardware buffer — gives plenty of jitter headroom for
        // the bursty Qwen delivery pattern.  Combined with the
        // non-blocking write loop below, the buffer absorbs short
        // bursts naturally; barge-in flush() clears it instantly.
        val bytesPerSecond = outSampleRate * 2  // mono PCM16
        val bufSize = max(minBuf * 4, (bytesPerSecond * 1.5).toInt())

        val track = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            AudioTrack.Builder()
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build()
                )
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setSampleRate(outSampleRate)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .build()
                )
                .setBufferSizeInBytes(bufSize)
                .setTransferMode(AudioTrack.MODE_STREAM)
                .build()
        } else {
            @Suppress("DEPRECATION")
            AudioTrack(
                AudioManager.STREAM_VOICE_CALL,
                outSampleRate,
                AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                bufSize,
                AudioTrack.MODE_STREAM,
            )
        }
        if (track.state != AudioTrack.STATE_INITIALIZED) {
            track.release()
            throw IllegalStateException("AudioTrack failed to initialize (state=${track.state})")
        }
        track.play()
        audioTrack = track
        Log.d(TAG, "Speaker started: rate=${outSampleRate}Hz bufSize=$bufSize")

        // Drain the speaker queue on a dedicated IO coroutine.  This
        // is the only thread that calls AudioTrack.write() — keeping
        // it off Main/WS threads is what prevents UI freezes during
        // playback.
        //
        // Mirrors the web's PCMPlayer model (frontend/src/voice/audio/
        // pcmPlayer.ts) as closely as Android allows: chunks flow
        // straight into the playback graph as fast as it can accept
        // them, no artificial buffer fill, no silence padding.
        //
        // The hardware buffer (1.5s, see startSpeaker) plus the
        // unbounded Channel act as the queue.  WRITE_NON_BLOCKING means
        // a full hardware buffer doesn't stall this coroutine — we
        // park the leftover and try again on the next loop tick after
        // the AudioTrack drains a bit.  This keeps up with bursty
        // Qwen delivery (chunks arriving faster than real-time
        // playback): everything gets accepted into our queue and
        // drains into the AudioTrack as fast as it physically can.
        playbackJob = scope.launch {
            // Bytes from the previous chunk that didn't all fit yet.
            // Held in user-space until the hardware buffer has room.
            var pending: ByteArray? = null
            try {
                while (isActive) {
                    val t = audioTrack ?: break
                    if (t.state != AudioTrack.STATE_INITIALIZED) break

                    // If we have leftover bytes, finish those before
                    // pulling another chunk — order matters.
                    // Otherwise wait (suspending) for the next chunk
                    // from the channel.  When the channel closes
                    // (cleanup), receiveCatching returns isClosed and
                    // we exit cleanly.
                    val data: ByteArray = if (pending != null) {
                        pending!!.also { pending = null }
                    } else {
                        val r = speakerQueue.receiveCatching()
                        if (r.isClosed) break
                        r.getOrNull() ?: continue
                    }

                    var offset = 0
                    while (offset < data.size) {
                        val written = try {
                            t.write(
                                data,
                                offset,
                                data.size - offset,
                                AudioTrack.WRITE_NON_BLOCKING,
                            )
                        } catch (e: Exception) {
                            Log.w(TAG, "AudioTrack.write failed: ${e.message}")
                            -1
                        }
                        if (written < 0) {
                            // Permanent error — drop the rest of this
                            // chunk; next chunk may still play.
                            break
                        }
                        if (written == 0) {
                            // Hardware buffer is full.  Park the rest
                            // and yield; the delay lets the buffer
                            // drain a bit before we retry.  Without
                            // this we'd busy-loop until space opens.
                            pending = data.copyOfRange(offset, data.size)
                            delay(10)
                            break
                        }
                        offset += written
                    }
                }
            } catch (e: CancellationException) {
                // Normal shutdown
            } catch (e: Exception) {
                Log.w(TAG, "Playback loop ended with: ${e.message}")
            }
            Log.d(TAG, "Playback loop exited")
        }
    }

    // --- AudioRecord (mic) -------------------------------------------------

    private fun startMic() {
        val minBuf = AudioRecord.getMinBufferSize(
            inSampleRate,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
        )
        // 4x min for headroom against scheduling jitter.
        val bufSize = max(minBuf * 4, inSampleRate * 2 / 5)

        // VOICE_RECOGNITION on Lollipop (API < 24) — Samsung's HAL
        // routes VOICE_COMMUNICATION through aggressive processing
        // that silences audio.  Same compatibility note as
        // OpenAIVoiceProvider.
        val source = if (Build.VERSION.SDK_INT < Build.VERSION_CODES.N)
            MediaRecorder.AudioSource.VOICE_RECOGNITION
        else
            MediaRecorder.AudioSource.VOICE_COMMUNICATION

        val record = AudioRecord(
            source,
            inSampleRate,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            bufSize,
        )
        if (record.state != AudioRecord.STATE_INITIALIZED) {
            record.release()
            throw IllegalStateException("AudioRecord failed to initialize (state=${record.state})")
        }
        record.startRecording()
        audioRecord = record
        Log.d(TAG, "Mic started: rate=${inSampleRate}Hz source=$source bufSize=$bufSize")

        // Capture loop
        captureJob = scope.launch {
            val chunkBytes = MIC_CHUNK_FRAMES * 2  // 16-bit samples
            val buf = ByteArray(chunkBytes)
            while (isActive && running.get()) {
                val read = record.read(buf, 0, chunkBytes)
                if (read <= 0) {
                    if (read == AudioRecord.ERROR_INVALID_OPERATION ||
                        read == AudioRecord.ERROR_BAD_VALUE) {
                        Log.w(TAG, "AudioRecord.read error: $read — ending capture loop")
                        break
                    }
                    continue
                }
                if (userMuted) continue  // drop chunks while muted

                // Apply gain in place
                val gain = micGainLevel
                if (gain != 1.0f) applyGainPcm16(buf, 0, read, gain)

                val b64 = Base64.encodeToString(buf, 0, read, Base64.NO_WRAP)
                try {
                    sendMicChunk?.invoke(b64)
                } catch (e: Exception) {
                    Log.w(TAG, "sendMicChunk failed: ${e.message}")
                }
            }
            Log.d(TAG, "Mic capture loop exited")
        }
    }

    /**
     * Read `code` and `message` from an `error` value that may arrive
     * as either a fully-walked Map or a raw JSONObject (depending on
     * which conversion path the WS layer took).
     */
    private fun readErrorFields(value: Any?): Pair<String, String> {
        val code: String
        val msg: String
        when (value) {
            is org.json.JSONObject -> {
                code = value.optString("code", "unknown")
                msg = value.optString("message", "Unknown error")
            }
            is Map<*, *> -> {
                code = value["code"] as? String ?: "unknown"
                msg = value["message"] as? String ?: "Unknown error"
            }
            else -> {
                code = "unknown"
                msg = "Unknown error"
            }
        }
        return code to msg
    }

    /**
     * Read a single string field from a nested object that may arrive
     * as either a Map or a JSONObject.
     */
    private fun readNestedString(value: Any?, key: String): String? {
        return when (value) {
            is org.json.JSONObject -> value.optString(key, "").ifEmpty { null }
            is Map<*, *> -> value[key] as? String
            else -> null
        }
    }

    private fun applyGainPcm16(buf: ByteArray, offset: Int, length: Int, gain: Float) {
        var i = offset
        val end = offset + length
        while (i < end - 1) {
            val lo = buf[i].toInt() and 0xff
            val hi = buf[i + 1].toInt()
            val sample = ((hi shl 8) or lo).toShort().toInt()
            val amplified = (sample * gain).toInt().coerceIn(Short.MIN_VALUE.toInt(), Short.MAX_VALUE.toInt())
            buf[i] = (amplified and 0xff).toByte()
            buf[i + 1] = ((amplified shr 8) and 0xff).toByte()
            i += 2
        }
    }

    // --- Cleanup -----------------------------------------------------------

    private fun cleanup() {
        running.set(false)
        captureJob?.cancel()
        captureJob = null

        // Stop accepting new chunks; drain pending writes by
        // cancelling the playback job (it holds the only consumer).
        playbackJob?.cancel()
        playbackJob = null

        // Drain anything still queued so we don't leak buffers.
        while (true) {
            val r = speakerQueue.tryReceive()
            if (r.isFailure || r.isClosed) break
        }

        try {
            audioRecord?.let {
                if (it.recordingState == AudioRecord.RECORDSTATE_RECORDING) it.stop()
                it.release()
            }
        } catch (e: Exception) {
            Log.w(TAG, "Error stopping AudioRecord: ${e.message}")
        }
        audioRecord = null

        try {
            audioTrack?.let {
                if (it.playState == AudioTrack.PLAYSTATE_PLAYING) it.stop()
                it.release()
            }
        } catch (e: Exception) {
            Log.w(TAG, "Error stopping AudioTrack: ${e.message}")
        }
        audioTrack = null

        gainBeforeSpeaking = null
        sendMicChunk = null
    }
}
