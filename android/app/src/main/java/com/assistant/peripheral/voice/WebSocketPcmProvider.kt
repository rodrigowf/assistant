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

/**
 * Abstract base for WebSocket-relayed PCM voice providers.
 *
 * The backend (`orchestrator/voice_relay.py`) owns the upstream WebSocket
 * — this class only handles the Android-side audio plumbing:
 *
 *   1. [VoiceManager] hands us [VoiceConnectionInfo] derived from the
 *      `session_started` payload.
 *   2. We open [AudioRecord] at the configured input sample rate and
 *      stream PCM chunks via `sendMicChunkToBackend`, which the
 *      orchestrator WS layer wraps in `voice_audio_in` messages.
 *   3. The backend relay's drain loop emits matching `voice_audio_out`
 *      messages, which [VoiceManager] hands us via [pushSpeakerChunk].
 *      We decode and feed [AudioTrack].
 *
 * What subclasses own: parsing upstream events into [VoiceEvent]s. The
 * wire format differs by provider — OpenAI-Realtime ([QwenVoiceProvider])
 * uses ``type``-tagged events, Gemini Live ([GeminiVoiceProvider]) uses
 * untyped ``serverContent`` / ``toolCall`` envelopes — so each subclass
 * implements [parseProviderEvent] and emits via the [emit] / [setState] /
 * [flushSpeakerOutput] helpers.
 *
 * What we do NOT own:
 *   - System audio focus / speaker routing — [VoiceManager]'s job.
 *   - The upstream WebSocket — backend's job.
 *   - High-level transcript/tool reconciliation — the ViewModel handles
 *     both our [events] flow and the WebSocket layer's event stream.
 */
abstract class WebSocketPcmProvider(
    private val context: Context,
    final override val providerId: String,
) : VoiceProvider {

    protected val tag: String get() = "${providerId.replaceFirstChar(Char::uppercase)}VoiceProvider"

    final override val connectionType: VoiceConnectionType = VoiceConnectionType.WEBSOCKET

    private val _state = MutableStateFlow<VoiceState>(VoiceState.Off)
    final override val state: StateFlow<VoiceState> = _state.asStateFlow()

    private val _events = MutableSharedFlow<VoiceEvent>(extraBufferCapacity = 64)
    final override val events: SharedFlow<VoiceEvent> = _events.asSharedFlow()

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

    companion object {
        // Mic chunk size: 20ms at 24kHz mono PCM16 = 480 frames = 960 bytes.
        // Matches the web frontend's 20ms cadence.
        private const val MIC_CHUNK_FRAMES = 480
    }

    // --- Subclass extension points ----------------------------------------

    /**
     * Parse a single upstream provider event mirrored from the backend.
     *
     * Implementations should emit [VoiceEvent]s via [emit], update state
     * via [setState], and call [flushSpeakerOutput] on barge-in.
     */
    protected abstract fun parseProviderEvent(event: Map<String, Any?>)

    // --- Subclass helpers --------------------------------------------------

    protected fun emit(event: VoiceEvent) {
        _events.tryEmit(event)
    }

    protected fun setState(newState: VoiceState) {
        _state.value = newState
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
    protected fun flushSpeakerOutput() {
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
            Log.w(tag, "AudioTrack flush failed: ${e.message}")
        }
        if (dropped > 0) Log.d(tag, "Barge-in: dropped $dropped queued speaker chunks")
    }

    // --- VoiceProvider implementation -------------------------------------

    final override suspend fun connect(
        info: VoiceConnectionInfo,
        mirrorEventToBackend: (Map<String, Any?>) -> Unit,
        sendMicChunkToBackend: (String) -> Unit,
    ) = withContext(Dispatchers.IO) {
        if (_state.value != VoiceState.Off && _state.value !is VoiceState.Error) {
            Log.w(tag, "Voice session already active, state=${_state.value}")
            return@withContext
        }

        if (ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED) {
            val msg = "RECORD_AUDIO permission not granted"
            Log.e(tag, msg)
            _state.value = VoiceState.Error(msg)
            _events.tryEmit(VoiceEvent.Error(msg))
            return@withContext
        }

        inSampleRate = info.audioInSampleRate
        outSampleRate = info.audioOutSampleRate
        sendMicChunk = sendMicChunkToBackend
        // mirrorEventToBackend is unused on the WS path (the backend
        // already sees its own upstream events directly).

        Log.i(tag, "Connecting $providerId voice: in=${inSampleRate}Hz out=${outSampleRate}Hz model=${info.model} voice=${info.voice}")
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
            Log.i(tag, "$providerId voice session ready")
        } catch (e: Exception) {
            Log.e(tag, "Failed to start $providerId voice: ${e.message}", e)
            _state.value = VoiceState.Error(e.message ?: "$providerId start failed")
            _events.tryEmit(VoiceEvent.Error(e.message ?: "$providerId start failed"))
            cleanup()
        }
    }

    final override suspend fun disconnect() {
        Log.i(tag, "Disconnecting $providerId voice")
        cleanup()
        _state.value = VoiceState.Off
        _events.tryEmit(VoiceEvent.SessionEnded)
    }

    /** No-op for WS path — the backend relay forwards commands directly upstream. */
    final override fun handleBackendCommand(command: Map<String, Any?>) {
        // Intentional: the backend's voice_relay.py handles upstream
        // commands.  Anything that needs to round-trip through the
        // client lands as a `voice_event` server message and is handled
        // by the WebSocket layer instead.
    }

    final override fun handleProviderEvent(event: Map<String, Any?>) {
        parseProviderEvent(event)
    }

    final override fun pushSpeakerChunk(audioB64: String) {
        // Fast path: decode here (cheap), then hand off to the
        // playback coroutine.  AudioTrack.write() blocks when the
        // buffer is full — calling it on the WS dispatch thread
        // (which is Main by default in viewModelScope) freezes the
        // UI.  The playback coroutine on Dispatchers.IO writes
        // without affecting frame timing.
        if (!running.get()) return
        try {
            val pcm = Base64.decode(audioB64, Base64.NO_WRAP)
            val result = speakerQueue.trySend(pcm)
            if (result.isFailure) {
                Log.w(tag, "speakerQueue full or closed; dropping chunk (${pcm.size}B)")
            }
        } catch (e: Exception) {
            Log.w(tag, "Failed to decode speaker chunk: ${e.message}")
        }
    }

    final override fun toggleMute(): Boolean {
        userMuted = !userMuted
        Log.i(tag, "[MIC] TOGGLE_MUTE → userMuted=$userMuted")
        return userMuted
    }

    final override fun isMuted(): Boolean = userMuted

    final override fun setMicGain(level: Float) {
        micGainLevel = level.coerceIn(0.0f, 2.0f)
        Log.d(tag, "Mic gain set to: $micGainLevel")
    }

    fun getMicGain(): Float = micGainLevel

    final override fun setEchoDuckingGain(gain: Float) {
        echoDuckingGain = gain.coerceIn(0.0f, 1.0f)
        Log.d(tag, "Echo ducking gain set to: $echoDuckingGain")
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
        // the bursty upstream delivery pattern.  Combined with the
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
        Log.d(tag, "Speaker started: rate=${outSampleRate}Hz bufSize=$bufSize")

        // Drain the speaker queue on a dedicated IO coroutine.  This
        // is the only thread that calls AudioTrack.write() — keeping
        // it off Main/WS threads is what prevents UI freezes during
        // playback.
        //
        // The hardware buffer (1.5s, see above) plus the unbounded
        // Channel act as the queue.  WRITE_NON_BLOCKING means a full
        // hardware buffer doesn't stall this coroutine — we park the
        // leftover and try again on the next loop tick after the
        // AudioTrack drains a bit.  On API 21/22 the 4-arg overload
        // doesn't exist; we fall back to the blocking 3-arg write,
        // which returns total bytes (never partial 0), so the parking
        // branch is unreachable on those versions.
        playbackJob = scope.launch {
            var pending: ByteArray? = null
            try {
                while (isActive) {
                    val t = audioTrack ?: break
                    if (t.state != AudioTrack.STATE_INITIALIZED) break

                    val data: ByteArray = if (pending != null) {
                        pending.also { pending = null }
                    } else {
                        val r = speakerQueue.receiveCatching()
                        if (r.isClosed) break
                        r.getOrNull() ?: continue
                    }

                    var offset = 0
                    while (offset < data.size) {
                        // 4-arg write with WRITE_NON_BLOCKING is API
                        // 23+. On API 21/22 the method doesn't exist
                        // and a direct call throws NoSuchMethodError
                        // (a Throwable, NOT an Exception) — naive
                        // ``catch (Exception)`` lets it kill the
                        // process. Branch on SDK_INT and catch
                        // Throwable defensively.
                        val written = try {
                            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                                t.write(
                                    data,
                                    offset,
                                    data.size - offset,
                                    AudioTrack.WRITE_NON_BLOCKING,
                                )
                            } else {
                                t.write(data, offset, data.size - offset)
                            }
                        } catch (e: Throwable) {
                            Log.w(tag, "AudioTrack.write failed: ${e.message}")
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
                            // drain a bit before we retry.
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
                Log.w(tag, "Playback loop ended with: ${e.message}")
            }
            Log.d(tag, "Playback loop exited")
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
        Log.d(tag, "Mic started: rate=${inSampleRate}Hz source=$source bufSize=$bufSize")

        captureJob = scope.launch {
            val chunkBytes = MIC_CHUNK_FRAMES * 2  // 16-bit samples
            val buf = ByteArray(chunkBytes)
            while (isActive && running.get()) {
                val read = record.read(buf, 0, chunkBytes)
                if (read <= 0) {
                    if (read == AudioRecord.ERROR_INVALID_OPERATION ||
                        read == AudioRecord.ERROR_BAD_VALUE) {
                        Log.w(tag, "AudioRecord.read error: $read — ending capture loop")
                        break
                    }
                    continue
                }
                if (userMuted) continue  // drop chunks while muted

                val gain = micGainLevel
                if (gain != 1.0f) applyGainPcm16(buf, 0, read, gain)

                val b64 = Base64.encodeToString(buf, 0, read, Base64.NO_WRAP)
                try {
                    sendMicChunk?.invoke(b64)
                } catch (e: Exception) {
                    Log.w(tag, "sendMicChunk failed: ${e.message}")
                }
            }
            Log.d(tag, "Mic capture loop exited")
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
            Log.w(tag, "Error stopping AudioRecord: ${e.message}")
        }
        audioRecord = null

        try {
            audioTrack?.let {
                if (it.playState == AudioTrack.PLAYSTATE_PLAYING) it.stop()
                it.release()
            }
        } catch (e: Exception) {
            Log.w(tag, "Error stopping AudioTrack: ${e.message}")
        }
        audioTrack = null

        gainBeforeSpeaking = null
        sendMicChunk = null
    }
}
