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
     * Current speaker-side audio plane.  Read by [startSpeaker] when
     * building the [AudioTrack]; updated mid-session via
     * [setSpeakerMode] when [VoiceManager]'s routing changes (e.g.
     * user plugs in / picks a BT speaker mid-call).
     *
     * Defaults to [AudioRouter.SpeakerMode.CALL] — the same as the
     * legacy behaviour, so anything that doesn't call [setSpeakerMode]
     * gets the old code path.
     */
    @Volatile private var speakerMode: AudioRouter.SpeakerMode =
        AudioRouter.SpeakerMode.CALL

    /**
     * Optional output endpoint to pin the [AudioTrack] to via
     * [AudioTrack.setPreferredDevice].  When set, makes the route
     * deterministic — critical when multiple A2DP devices are paired.
     */
    @Volatile private var preferredOutputDevice: android.media.AudioDeviceInfo? = null

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
    //
    // While the agent is speaking we attenuate the mic to ``echoDuckingGain``
    // (0 = full mute) so the speaker→mic feedback path doesn't trip Gemini
    // Live / Qwen server-side VAD into a "user is interrupting" barge-in.
    //
    // The agent-speaking signal is taken from the speaker queue itself: a
    // new chunk arriving via [pushSpeakerChunk] means the agent is mid-turn;
    // the capture loop polls a stale-timeout to decide when speech ended.
    // This works the same way for both Gemini and Qwen WS providers — no
    // wire-format-specific event hooks needed.
    //
    // Increment H: extracted to [EchoDuckController]. The controller owns
    // micGainLevel, echoDuckingGain, gainBeforeSpeaking, and micRestoreJob.
    // The remaining state below (agentSpeaking, lastSpeakerChunkAtMs,
    // userMuted) is owned here because the capture/dispatch loops produce
    // these signals; the controller observes them via the ducker's
    // public API and callable accessors.
    private val ducker: EchoDuckController by lazy {
        EchoDuckController(
            scope = scope,
            getPlaybackHeadPosition = {
                audioTrack?.takeIf { it.state == AudioTrack.STATE_INITIALIZED }
                    ?.playbackHeadPosition?.toLong()
            },
            getTotalFramesWritten = { totalFramesWritten },
            tag = tag,
        )
    }
    @Volatile private var agentSpeaking: Boolean = false
    @Volatile private var lastSpeakerChunkAtMs: Long = 0L
    private var userMuted: Boolean = false

    // Total frames written to the [AudioTrack] over this session, used
    // to compare against `audioTrack.playbackHeadPosition` to decide
    // when the hardware buffer is truly empty (i.e. the speaker is
    // really silent, not just "we stopped queueing").  Resets on
    // [flushSpeakerOutput] since `flush()` resets the head position too.
    @Volatile private var totalFramesWritten: Long = 0L

    companion object {
        // Mic chunk size: 20ms at 24kHz mono PCM16 = 480 frames = 960 bytes.
        // Matches the web frontend's 20ms cadence.
        private const val MIC_CHUNK_FRAMES = 480

        // Agent-speaking → idle detection.  If no speaker chunk has been
        // pushed in this many ms, treat the agent's turn as ended.
        // 800ms covers the natural gaps between Gemini Live's bursty
        // chunk delivery without falsely ending mid-turn.
        private const val AGENT_SPEECH_STALE_MS = 800L

        // Drain-restore timing constants moved to [EchoDuckController]
        // (Inc H). The controller owns the no-timeout drain loop and
        // emits the canonical `[MIC_STATE] RESTORE_DRAIN(*)` log lines.
    }

    // --- Format (set in connect) ------------------------------------------
    private var inSampleRate: Int = 24000
    private var outSampleRate: Int = 24000

    // --- Bridge to the backend WS (set in connect) ------------------------
    private var sendMicChunk: ((String) -> Unit)? = null

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
                    // flush() resets playbackHeadPosition to 0 — keep
                    // our write counter in sync so the drain loop's
                    // comparison stays valid.
                    totalFramesWritten = 0L
                }
            }
        } catch (e: Exception) {
            Log.w(tag, "AudioTrack flush failed: ${e.message}")
        }
        if (dropped > 0) Log.d(tag, "Barge-in: dropped $dropped queued speaker chunks")
        // Barge-in means the agent's audio is gone — restore mic right
        // away so the user can be heard without waiting out the stale
        // timeout.
        agentSpeaking = false
        ducker.restoreImmediately("flush")
    }

    // --- Echo ducking -----------------------------------------------------
    // Inc H: extracted to [EchoDuckController]. The capture loop reads
    // ducker.currentMicGain for per-chunk gain application; the dispatch
    // path calls ducker.duck()/cancelPendingRestore() on chunk arrival;
    // the capture loop calls ducker.scheduleRestore("stale") when the
    // staleness detector trips. Logging shape preserved verbatim — see
    // EchoDuckController for the parity contract.

    // --- VoiceProvider implementation -------------------------------------

    final override suspend fun connect(
        info: VoiceConnectionInfo,
        mirrorEventToBackend: (Map<String, Any?>) -> Unit,
        sendMicChunkToBackend: (String) -> Unit,
    ) = withContext(Dispatchers.IO) {
        // The "already active" guard only fires if we're actually running
        // (mic/speaker loops alive).  Without the `running` check, a benign
        // race trips it: the backend can deliver `voice_status: ready` —
        // which flips `_state` to Active via [handleProviderEvent] — before
        // `VoiceManager.start()` reaches this connect() call.  That used to
        // happen rarely; once the Gemini Live upstream got faster, it
        // started happening every session, leaving the provider stuck in
        // Active without ever having opened the mic.
        if (running.get() &&
            _state.value != VoiceState.Off &&
            _state.value !is VoiceState.Error
        ) {
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
        agentSpeaking = false
        lastSpeakerChunkAtMs = 0L
        totalFramesWritten = 0L
        ducker.resetForNewSession()

        try {
            // Set `running` BEFORE startMic — the mic capture loop
            // checks `running.get()` in its condition, so flipping
            // the flag after the coroutine launches can race and
            // exit it immediately.
            running.set(true)
            startSpeaker()
            // HAL settling delay between wake-word AudioRecord release
            // and the call's AudioRecord open. Observed 2026-06-04:
            // post-wake-word call mic came up with ~half the amplitude
            // of a cold-start call until ~30s into the session. Symptom
            // matches the Samsung HAL re-initialising AGC state between
            // sources too quickly. 200ms is enough to let the HAL settle
            // without a perceptible UX delay (the user already waited
            // for wake-word recognition + WS handshake).
            kotlinx.coroutines.delay(200L)
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
        // Backend-synthesised handshake status — sent before the upstream
        // provider's own greeting. "preparing" keeps the spinner; "ready"
        // flips to Active so the user knows they can talk.
        if (event["type"] == "voice_status") {
            when (event["status"] as? String) {
                "preparing" -> setState(VoiceState.Connecting)
                "summarizing" -> {
                    // ``summarizing`` is a *pre-connect* status. If we're
                    // already past Connecting/Summarizing (i.e. mid-call),
                    // this is a stale broadcast from a reconnect probe
                    // that ran ``_attach_voice_payload`` again — ignore it
                    // so the UI doesn't bounce back to yellow mid-talk.
                    val s = state.value
                    if (s == VoiceState.Off || s is VoiceState.Error ||
                        s == VoiceState.Connecting || s == VoiceState.Summarizing
                    ) {
                        setState(VoiceState.Summarizing)
                    } else {
                        Log.d(tag, "ignoring stale voice_status:summarizing (state=$s)")
                    }
                }
                "ready" -> setState(VoiceState.Active)
                "reconnect_warning" -> {
                    // Gemini's goAway.timeLeft is a Go-style duration
                    // string like "50s", "30m0s", "1h30m0s" — NOT a
                    // plain number. Parse defensively: also accept a
                    // bare Number on the off-chance the backend gets
                    // updated to send seconds directly.
                    val tl: Int? = when (val v = event["time_left"]) {
                        is Number -> v.toInt()
                        is String -> parseGoDurationToSeconds(v)
                        else -> null
                    }
                    Log.i(tag, "Reconnect warning (timeLeft=${tl}s, raw=${event["time_left"]})")
                    _events.tryEmit(VoiceEvent.ReconnectWarning(tl))
                }
                "reconnecting" -> {
                    Log.i(tag, "Reconnecting (upstream cycling)")
                    _events.tryEmit(VoiceEvent.Reconnecting)
                }
            }
            return
        }
        // Backend-synthesised relay error — the upstream provider WS
        // died and the relay gave up (e.g. Gemini 1008 "session expired"
        // after a stale resumption-handle retry, or AI Studio quota
        // denial). Without explicit handling here, the local mic+
        // speaker stay live and the UI sits "active" with no audio
        // flowing — the user sees a silent dead session. Surface it as
        // an Error state and tear down so the mic icon flips red.
        if (event["type"] == "error") {
            @Suppress("UNCHECKED_CAST")
            val err = event["error"] as? Map<String, Any?>
            val msg = (err?.get("message") as? String)
                ?: "Voice relay closed by backend"
            val code = err?.get("code") as? String
            Log.w(tag, "Backend relay error code=$code msg=$msg — tearing down voice session")
            setState(VoiceState.Error(msg))
            _events.tryEmit(VoiceEvent.Error(msg))
            cleanup()
            _events.tryEmit(VoiceEvent.SessionEnded)
            return
        }
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
                return
            }
            // Agent-speaking signal: every chunk arrival refreshes the
            // staleness timer and, on the rising edge, ducks the mic.
            // The capture loop polls the staleness timeout to decide
            // when to schedule the restore.
            //
            // Any chunk arrival ALSO cancels a pending restore job —
            // the agent is still talking, even if the capture loop
            // already flipped agentSpeaking to false during the
            // staleness window. Without this cancel, the queued
            // 2s-delayed restore fires mid-speech and the next
            // residual speaker burst trips Gemini's VAD into a
            // self-interrupt loop.
            lastSpeakerChunkAtMs = System.currentTimeMillis()
            ducker.cancelPendingRestore()
            if (!agentSpeaking) {
                agentSpeaking = true
                ducker.duck()
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
        ducker.setMicGain(level)
    }

    fun getMicGain(): Float = ducker.getEffectiveMicGain()

    final override fun setEchoDuckingGain(gain: Float) {
        ducker.setEchoDuckingGain(gain)
    }

    /**
     * Switch the speaker's [AudioTrack] between communication-audio
     * and media-audio planes.  Rebuilds the AudioTrack iff the mode
     * actually changes — silent no-op otherwise so the router can
     * fire this freely on every re-apply tick.
     *
     * Safe to call before / after [connect].  Before connect it just
     * stages the mode; the actual AudioTrack is built in
     * [startSpeaker].
     */
    final override fun setSpeakerMode(
        mode: AudioRouter.SpeakerMode,
        preferredDevice: android.media.AudioDeviceInfo?,
    ) {
        val deviceChanged = preferredOutputDevice?.id != preferredDevice?.id
        val modeChanged = speakerMode != mode
        speakerMode = mode
        preferredOutputDevice = preferredDevice
        if (!modeChanged && !deviceChanged) return
        // If we're not yet playing, the staged values will take effect
        // when startSpeaker() runs.
        if (audioTrack == null) return
        // Mid-session rebuild: tear down the current track and start
        // fresh.  The playback queue is preserved — the playback
        // coroutine will pick up the next chunk against the new
        // AudioTrack on its next loop iteration.
        Log.i(tag, "setSpeakerMode → $mode (rebuilding AudioTrack)")
        stopSpeakerOnly()
        try {
            startSpeaker()
        } catch (e: Exception) {
            Log.e(tag, "AudioTrack rebuild failed: ${e.message}", e)
        }
    }

    /**
     * Tear down the current speaker [AudioTrack] without touching
     * the playback queue or coroutine.  The next [startSpeaker] call
     * brings up a new track and the playback loop seamlessly resumes.
     */
    private fun stopSpeakerOnly() {
        val t = audioTrack ?: return
        audioTrack = null
        try {
            if (t.playState == AudioTrack.PLAYSTATE_PLAYING) t.stop()
            t.release()
        } catch (e: Exception) {
            Log.w(tag, "stopSpeakerOnly failed: ${e.message}")
        }
    }

    fun release() {
        runBlocking { disconnect() }
        scope.cancel()
    }

    // --- AudioTrack (speaker) ---------------------------------------------

    /**
     * Build an [AudioTrack] for the current [speakerMode].
     *
     *  - CALL  → ``USAGE_VOICE_COMMUNICATION`` / ``CONTENT_TYPE_SPEECH``
     *            (legacy: ``STREAM_VOICE_CALL``).  Routes through the
     *            communication-audio plane; cooperates with AEC.
     *  - MEDIA → ``USAGE_MEDIA`` / ``CONTENT_TYPE_MUSIC`` (legacy:
     *            ``STREAM_MUSIC``).  Routes through the media-audio
     *            plane — reaches A2DP sinks that the call plane can't.
     */
    private fun buildAudioTrack(
        bufSize: Int,
        mode: AudioRouter.SpeakerMode,
    ): AudioTrack {
        val track = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            val attrs = when (mode) {
                AudioRouter.SpeakerMode.CALL -> AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build()
                AudioRouter.SpeakerMode.MEDIA -> AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                    .build()
            }
            AudioTrack.Builder()
                .setAudioAttributes(attrs)
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
            val legacyStream = when (mode) {
                AudioRouter.SpeakerMode.CALL -> AudioManager.STREAM_VOICE_CALL
                AudioRouter.SpeakerMode.MEDIA -> AudioManager.STREAM_MUSIC
            }
            @Suppress("DEPRECATION")
            AudioTrack(
                legacyStream,
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
        return track
    }

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

        val track = buildAudioTrack(bufSize, speakerMode)
        // Pin the output endpoint when the router gave us a specific
        // device (e.g. the JBL).  Without this Android's default
        // routing may snap the stream to the wrong sink the moment a
        // new device connects.  ``setPreferredDevice`` is API 23+.
        val pinned = preferredOutputDevice
        if (pinned != null && Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            try {
                val ok = track.setPreferredDevice(pinned)
                Log.d(tag, "AudioTrack pinned to ${pinned.productName} (type=${pinned.type}) → $ok")
            } catch (e: Exception) {
                Log.w(tag, "AudioTrack.setPreferredDevice failed: ${e.message}")
            }
        }
        track.play()
        audioTrack = track
        Log.d(tag, "Speaker started: rate=${outSampleRate}Hz bufSize=$bufSize mode=$speakerMode")

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
                        // PCM16 mono → 2 bytes per frame.  Track
                        // cumulative frames so the restore loop can
                        // compare against playbackHeadPosition.
                        totalFramesWritten += (written / 2).toLong()
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
            // Per-second RMS probe: accumulate over ~50 chunks (1s at 20ms each)
            // then emit one line. Lets us see ground-truth mic amplitude
            // without spamming the log. Pre-gain so it reflects the raw
            // signal from AudioRecord, not what ducking did to it.
            var probeRmsAccum = 0.0
            var probeChunks = 0
            var probePeak = 0
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

                // Detect agent-speech-ended via staleness of the
                // speaker queue.  Cheap to evaluate per-chunk and
                // avoids needing wire-format-specific event hooks.
                if (agentSpeaking) {
                    val sinceLastChunk = System.currentTimeMillis() - lastSpeakerChunkAtMs
                    if (sinceLastChunk > AGENT_SPEECH_STALE_MS && !ducker.isRestorePending) {
                        agentSpeaking = false
                        ducker.scheduleRestore("stale")
                    }
                }

                // Pre-gain RMS probe. We compute over the raw bytes so
                // duck attenuation doesn't distort the diagnostic.
                val chunkStats = computeRmsAndPeakPcm16(buf, 0, read)
                probeRmsAccum += chunkStats.first
                probePeak = maxOf(probePeak, chunkStats.second)
                probeChunks++
                if (probeChunks >= 50) {
                    val avgRms = probeRmsAccum / probeChunks
                    Log.i(tag, "[MIC_PROBE] rms_avg=${avgRms.toInt()} peak=$probePeak gain=${ducker.currentMicGain} ducking=${agentSpeaking}")
                    probeRmsAccum = 0.0
                    probeChunks = 0
                    probePeak = 0
                }

                val gain = ducker.currentMicGain
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

    /** Parse a Go-style duration string ("50s", "30m0s", "1h30m0s",
     *  "500ms") to seconds. Returns null on anything we don't recognise.
     *  Used for Gemini Live's goAway.timeLeft field. */
    private fun parseGoDurationToSeconds(s: String): Int? {
        if (s.isBlank()) return null
        var total = 0.0
        var i = 0
        while (i < s.length) {
            // Read a number (may include a decimal point).
            val numStart = i
            while (i < s.length && (s[i].isDigit() || s[i] == '.')) i++
            if (numStart == i) return null  // expected a digit
            val num = s.substring(numStart, i).toDoubleOrNull() ?: return null
            // Read the unit.
            val unitStart = i
            while (i < s.length && s[i].isLetter()) i++
            val unit = s.substring(unitStart, i)
            val mult = when (unit) {
                "ns" -> 1e-9
                "us", "µs" -> 1e-6
                "ms" -> 1e-3
                "s" -> 1.0
                "m" -> 60.0
                "h" -> 3600.0
                else -> return null
            }
            total += num * mult
        }
        return total.toInt()
    }

    /** Returns (rms, peak) of a PCM16 little-endian buffer slice.
     *  RMS is the diagnostic for "is there signal here at all"; peak
     *  catches transients that average away. Both in raw int16 units. */
    private fun computeRmsAndPeakPcm16(buf: ByteArray, offset: Int, length: Int): Pair<Double, Int> {
        var i = offset
        val end = offset + length
        var sumSq = 0.0
        var peak = 0
        var n = 0
        while (i < end - 1) {
            val lo = buf[i].toInt() and 0xff
            val hi = buf[i + 1].toInt()
            val sample = ((hi shl 8) or lo).toShort().toInt()
            sumSq += (sample * sample).toDouble()
            val abs = if (sample < 0) -sample else sample
            if (abs > peak) peak = abs
            n++
            i += 2
        }
        val rms = if (n > 0) kotlin.math.sqrt(sumSq / n) else 0.0
        return Pair(rms, peak)
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

        // If we were mid-duck, the controller restores the saved gain so
        // a re-connect doesn't start with the attenuated value as the
        // "real" one. Matches pre-Inc-H L1042–L1049.
        ducker.cleanup()
        agentSpeaking = false
        totalFramesWritten = 0L

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

        sendMicChunk = null
    }
}
