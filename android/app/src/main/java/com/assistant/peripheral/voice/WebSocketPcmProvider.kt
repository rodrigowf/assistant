package com.assistant.peripheral.voice

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.util.Base64
import android.util.Log
import androidx.core.content.ContextCompat
import com.assistant.peripheral.data.VoiceState
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.*
import java.util.concurrent.atomic.AtomicBoolean

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
    // Inc H: AudioRecord/AudioTrack lifecycle extracted to MicCapture
    // and PcmPlayback. This file holds only the orchestration and
    // session-state surface; the helpers are constructed lazily once
    // the in/out sample rates are known (after connect() reads them).
    private val running = AtomicBoolean(false)
    private var mic: MicCapture? = null
    private var playback: PcmPlayback? = null

    /**
     * Current speaker-side audio plane. Read when building the
     * AudioTrack via PcmPlayback; updated mid-session via
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
     * Optional output endpoint to pin the AudioTrack to. Set
     * mid-session by VoiceManager when device routing changes.
     */
    @Volatile private var preferredOutputDevice: android.media.AudioDeviceInfo? = null

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
            getPlaybackHeadPosition = { playback?.getPlaybackHeadPosition() },
            getTotalFramesWritten = { playback?.getTotalFramesWritten() ?: 0L },
            tag = tag,
        )
    }
    @Volatile private var agentSpeaking: Boolean = false
    @Volatile private var lastSpeakerChunkAtMs: Long = 0L
    private var userMuted: Boolean = false

    // --- Format (set in connect) ------------------------------------------
    private var inSampleRate: Int = 24000
    private var outSampleRate: Int = 24000

    // --- Bridge to the backend WS (passed into MicCapture in connect) -----

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
        val dropped = playback?.flush() ?: 0
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
        // mirrorEventToBackend is unused on the WS path (the backend
        // already sees its own upstream events directly).
        // sendMicChunkToBackend is passed directly into MicCapture below.

        Log.i(tag, "Connecting $providerId voice: in=${inSampleRate}Hz out=${outSampleRate}Hz model=${info.model} voice=${info.voice}")
        _state.value = VoiceState.Connecting
        userMuted = false
        agentSpeaking = false
        lastSpeakerChunkAtMs = 0L
        ducker.resetForNewSession()

        // Build the helpers now that sample rates + callback are known.
        playback = PcmPlayback(scope = scope, tag = tag, outSampleRate = outSampleRate)
        mic = MicCapture(
            scope = scope,
            tag = tag,
            inSampleRate = inSampleRate,
            running = running,
            isUserMuted = { userMuted },
            getMicGain = { ducker.currentMicGain },
            isAgentSpeaking = { agentSpeaking },
            getLastSpeakerChunkAtMs = { lastSpeakerChunkAtMs },
            isRestorePending = { ducker.isRestorePending },
            onStalenessTriggered = {
                agentSpeaking = false
                ducker.scheduleRestore("stale")
            },
            sendMicChunk = sendMicChunkToBackend,
        )

        try {
            // Set `running` BEFORE startMic — the mic capture loop
            // checks `running.get()` in its condition, so flipping
            // the flag after the coroutine launches can race and
            // exit it immediately.
            running.set(true)
            playback!!.start(speakerMode, preferredOutputDevice)
            // HAL settling delay between wake-word AudioRecord release
            // and the call's AudioRecord open. Observed 2026-06-04:
            // post-wake-word call mic came up with ~half the amplitude
            // of a cold-start call until ~30s into the session. Symptom
            // matches the Samsung HAL re-initialising AGC state between
            // sources too quickly. 200ms is enough to let the HAL settle
            // without a perceptible UX delay (the user already waited
            // for wake-word recognition + WS handshake).
            kotlinx.coroutines.delay(200L)
            mic!!.start()
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
        // playback coroutine. AudioTrack.write() blocks when the
        // buffer is full — calling it on the WS dispatch thread
        // (which is Main by default in viewModelScope) freezes the
        // UI. The playback coroutine on Dispatchers.IO writes
        // without affecting frame timing.
        if (!running.get()) return
        val pb = playback ?: return
        try {
            val pcm = Base64.decode(audioB64, Base64.NO_WRAP)
            if (!pb.enqueue(pcm)) return  // queue closed or full (logs inside)
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
        // PcmPlayback.setSpeakerMode is a no-op if no AudioTrack is
        // open yet (the staged values take effect on next start()).
        playback?.setSpeakerMode(mode, preferredDevice)
    }

    fun release() {
        runBlocking { disconnect() }
        scope.cancel()
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

    // --- Cleanup -----------------------------------------------------------

    private fun cleanup() {
        running.set(false)
        mic?.cleanup()
        mic = null
        playback?.cleanup()
        playback = null
        // If we were mid-duck, the controller restores the saved gain so
        // a re-connect doesn't start with the attenuated value as the
        // "real" one.
        ducker.cleanup()
        agentSpeaking = false
    }
}
