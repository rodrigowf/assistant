package com.assistant.peripheral.voice

import android.content.Context
import android.content.Intent
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Build
import android.speech.SpeechRecognizer
import android.util.Log
import androidx.localbroadcastmanager.content.LocalBroadcastManager
import com.assistant.peripheral.audio.WavUtils
import com.assistant.peripheral.service.AssistantService
import kotlinx.coroutines.*
import kotlin.math.sqrt

/**
 * Explicit lifecycle for the wake-word detector (plan §2.2, Inc 6).
 * Replaces three implicit booleans (`isActive`, `isPaused`, `isRecognizing`)
 * with a single sealed-class hierarchy so transitions are exhaustive and
 * the rearm logic in `AssistantService.rearmWakeWord` can read genuine
 * state instead of guessing from flag combinations.
 *
 * Lives top-level in this file (plan §9 decision 3 — no separate file).
 */
sealed class WakeWordState {
    object Stopped : WakeWordState()
    object Idle : WakeWordState()
    object SilenceMonitor : WakeWordState()
    data class Recognizing(
        val startedAtMs: Long,
        val beganSpeechAtMs: Long?,
    ) : WakeWordState()
    object Paused : WakeWordState()
}

/**
 * Wake word detector with two-stage pipeline:
 *
 * Stage 1 — Silence monitor (AudioRecord, lightweight):
 *   Continuously reads raw PCM and computes RMS. When audio exceeds the
 *   threshold, hands off to Stage 2.
 *
 * Stage 2 — Recognition engine (pluggable; see [WakeWordRecognitionEngine]):
 *   Today this is [SpeechRecognizerEngine]. V3b adds [VoskRecognitionEngine]
 *   selected based on `VoskModelLoader` availability. The engine owns its
 *   own per-cycle state (warm instance, watchdog, NO_SPEECH counter, etc.);
 *   the detector only orchestrates: stage-1 gate → engine.recognize() →
 *   post-cycle action (broadcast, backoff, re-arm).
 *
 * This avoids the constant Stage-2 start/stop cycle (and any beeps from
 * `SpeechRecognizer`) when the room is quiet.
 */
class WakeWordDetector(
    private val context: Context,
    // Per Detour 3 naming (plan §0.5):
    //   talkWord = turn-based single voice message ("push-to-talk")
    //   wakeWord = realtime WebRTC voice conversation ("wake up the assistant")
    private val talkWord: String,
    private val wakeWord: String = "", // empty = disabled
    private val micGain: Float = 1.0f, // scales RMS threshold
    // Backend base URL (ws://host:port). Used to fetch the OpenAI key for the
    // Whisper confirmation gate. Blank disables confirmation (fail-open — the
    // detector fires on the raw Vosk match). Normally always set.
    private val serverUrl: String = "",
) {
    companion object {
        private const val TAG = "WakeWordDetector"
        const val ACTION_TALK_WORD_DETECTED = "com.assistant.peripheral.TURN_TALK_WORD_DETECTED"
        const val ACTION_WAKE_WORD_DETECTED = "com.assistant.peripheral.REALTIME_WAKE_WORD_DETECTED"

        /**
         * Fired the instant Vosk flags a candidate — BEFORE the Whisper
         * confirmation round-trip. Drives the transient "heard something,
         * confirming…" UI so the user gets instant feedback during the
         * ~0.3–0.8s gate. Always followed by exactly one of:
         * ACTION_*_WORD_DETECTED (confirmed) or ACTION_WAKE_CONFIRM_FAILED
         * (rejected / gate error).
         */
        const val ACTION_WAKE_CONFIRMING = "com.assistant.peripheral.WAKE_CONFIRMING"

        /**
         * Fired when the Whisper gate rejects a candidate (or errors, since we
         * fail closed). The UI clears the transient confirming indicator; no
         * session/recording starts. `isRealtime` is passed as a boolean extra
         * so the UI can clear the matching indicator.
         */
        const val ACTION_WAKE_CONFIRM_FAILED = "com.assistant.peripheral.WAKE_CONFIRM_FAILED"

        const val EXTRA_IS_REALTIME = "is_realtime"

        /**
         * Talk-word only: the full turn message (wake phrase + spoken command)
         * was captured on the SAME mic and is ready to send. Carries the
         * base64-encoded WAV as [EXTRA_TALK_AUDIO_B64]. The app sends it as a
         * voice message — no mic switch, no button press. Fired instead of the
         * legacy ACTION_TALK_WORD_DETECTED when same-mic capture succeeds.
         *
         * LocalBroadcastManager is in-process (no Binder serialization), so a
         * few-hundred-KB base64 string in the extra is safe.
         */
        const val ACTION_TALK_MESSAGE_CAPTURED =
            "com.assistant.peripheral.TALK_MESSAGE_CAPTURED"
        const val EXTRA_TALK_AUDIO_B64 = "talk_audio_b64"

        /**
         * Command capture: silence (RMS below the same effective threshold)
         * must persist this long AFTER speech began before we stop and send.
         * 1.5s is forgiving of mid-sentence pauses without feeling laggy.
         */
        private const val COMMAND_SILENCE_MS = 1500L

        /**
         * Silence threshold as a fraction of the voice/activity threshold
         * (hysteresis). Audio must fall below voiceThreshold*this to count as
         * silence, so ambient noise sitting near the activity level doesn't
         * keep the capture open forever. 0.5 = "clearly quieter than speech".
         */
        private const val COMMAND_SILENCE_RATIO = 0.5

        /**
         * Hard cap on command length so a noisy room / never-ending speech
         * can't record forever. Sends whatever was captured at the cap.
         */
        private const val COMMAND_MAX_MS = 30_000L

        /**
         * Speech must be detected (RMS above threshold) within this window
         * after the talk-word, else we abort the capture (the user said the
         * talk phrase but no command followed).
         */
        private const val COMMAND_SPEECH_ONSET_TIMEOUT_MS = 4_000L

        private const val SAMPLE_RATE = 16000
        private const val CHANNEL_CONFIG = AudioFormat.CHANNEL_IN_MONO
        private const val AUDIO_FORMAT = AudioFormat.ENCODING_PCM_16BIT

        // RMS threshold — 0..32767 scale.
        //
        // Empirically retuned 2026-06-09 from 200 to 70 (Detour 5).
        //
        // Field observation: on the A300M with the post-Inc-9 detector, normal
        // conversational "wake up" at arm's-length produced winMaxRms values of
        // 35–82 across many test windows (gain=1.5, effective=133 with the old
        // threshold). The recognizer only fired once across an 18-minute test
        // session — the threshold was structurally too high for this device's
        // mic at any practical gain. Background floor sits at RMS 4–11 with
        // occasional spikes to 30–50 from ambient noise.
        //
        // New value of 70:
        //  - At 100% gain (default), effective=70 — catches raised speech.
        //  - At 130% gain, effective=54 — normal speech reliably crosses.
        //  - At 150% gain, effective=47 — conversational speech crosses.
        // The slider now provides a useful range from "raised voice only" to
        // "conversational" instead of bottoming out at "still requires leaning
        // in" as it did at threshold=200.
        //
        // Plan §7 originally forbade this change (cited the reverted 200→100
        // commit). That ban was a guardrail against speculative tuning during
        // the refactor. Empirical logcat capture during Detour 5's investigation
        // justified lifting it — the change is grounded in measured device data.
        private const val RMS_THRESHOLD = 70.0

        // How long audio must stay above threshold before we start the engine
        // (avoids clicks/pops).
        private const val ACTIVITY_HOLD_MS = 30L

        // Speech-energy floor for the pre-Whisper gate. A phantom Vosk match
        // (the constrained grammar snaps ambient noise/silence to "wake up" or
        // "my friend") produces a captured clip whose loudest 200ms window
        // still sits in the background/ambient band (RMS 4–30 on the A300M —
        // see RMS_THRESHOLD note). A genuine spoken phrase always contains at
        // least one window well above this (winMaxRms 35–82 measured). So if
        // NO window in the captured clip crosses this floor, the "match" was
        // noise — reject it WITHOUT spending a Whisper call. This is the first
        // line of defence against the "opens for no reason, even in silence"
        // false trigger: whisper-1 hallucinates canned phrases on near-silent
        // clips, so we must not hand it silence in the first place. Set to the
        // engine's own speech-started threshold (RMS_STARTED_THRESHOLD=30) so
        // the two stages agree on what counts as speech.
        private const val SPEECH_FLOOR_RMS = 30.0

        // After a successful wake word, pause before re-arming.
        private const val POST_WAKEWORD_DELAY_MS = 3000L

        // Base delay after a missed recognition — doubles on each consecutive
        // miss (backoff).
        private const val POST_RECOGNITION_BASE_MS = 1000L
        private const val POST_RECOGNITION_MAX_MS = 30_000L

        /**
         * Normalize a wake-word phrase. Inc 5 dropped the phonetic `wordSubs`
         * expansion table (plan §3 Inc 5): variants are now the configured
         * phrases verbatim. Per-phrase output is a single-element list
         * containing the lowercased + trimmed input. The split-by-comma
         * fan-out lives at the call site (`talkVariants` / `wakeVariants`
         * below).
         */
        fun buildVariants(phrase: String): List<String> =
            listOf(phrase.lowercase().trim())

        /**
         * Pure predicate for the `start()` idempotency guard (Increment 1).
         * Returns true when a redundant `start()` should short-circuit:
         * detector already active AND not paused.
         */
        internal fun shouldShortCircuitStart(isActive: Boolean, isPaused: Boolean): Boolean =
            isActive && !isPaused

        /**
         * Pure predicate for the Inc 2 `finishRecognition` idempotency guard.
         * Returns true when a redundant post-cycle cleanup should
         * short-circuit: recognition has finished AND the engine has no
         * pending state.
         *
         * Pre-V3a this read `(isRecognizing, hasSpeechRecognizer)`. V3a
         * generalized `hasSpeechRecognizer` → `engineHasPendingState`
         * (returned by [WakeWordRecognitionEngine.hasPendingState]). The
         * parameter name changed; the semantics are byte-identical for
         * the SR path (Inc 2 unchanged), and the parity test still pins
         * the truth table.
         */
        internal fun shouldShortCircuitFinishRecognition(
            isRecognizing: Boolean,
            hasSpeechRecognizer: Boolean,
        ): Boolean = !isRecognizing && !hasSpeechRecognizer

        /**
         * State→legacy-flag derived predicates (Inc 6). Exposed on the
         * companion so `WakeWordFSMParityTest` can pin the mapping
         * without instantiating the detector.
         */
        internal fun derivedIsActive(state: WakeWordState): Boolean =
            state !is WakeWordState.Stopped

        internal fun derivedIsPaused(state: WakeWordState): Boolean =
            state is WakeWordState.Paused

        internal fun derivedIsRecognizing(state: WakeWordState): Boolean =
            state is WakeWordState.Recognizing

        /**
         * Inc 8: LocalBroadcast action that fires when the engine declares
         * itself unhealthy and asks for a full detector rebuild.
         * AssistantService listens for this and re-invokes
         * `startWakeWord(lastTalkWord, lastWakeWord, lastWakeMicGain)`.
         */
        const val ACTION_RECOGNIZER_UNHEALTHY =
            "com.assistant.peripheral.RECOGNIZER_UNHEALTHY"

        /**
         * Pure predicate for the Inc 8 health check — delegated to the
         * SpeechRecognizer engine (the only engine that emits NO_SPEECH
         * errors). Re-exported here so existing parity tests continue
         * to reference `WakeWordDetector.shouldBroadcastRecognizerUnhealthy`.
         */
        internal fun shouldBroadcastRecognizerUnhealthy(
            consecutiveNoSpeechErrors: Int,
        ): Boolean = SpeechRecognizerEngine.shouldBroadcastRecognizerUnhealthy(
            consecutiveNoSpeechErrors,
        )

        internal fun noSpeechHealthThresholdForTest(): Int =
            SpeechRecognizerEngine.NO_SPEECH_HEALTH_THRESHOLD

        // ── Inc 9: mic-unavailable broadcasting. Stays in the detector
        //    because the silence-monitor mic acquisition loop is detector-
        //    owned (engine-independent).

        private const val MIC_RETRY_WARN_THRESHOLD = 8

        const val ACTION_MIC_UNAVAILABLE =
            "com.assistant.peripheral.MIC_UNAVAILABLE"

        const val ACTION_MIC_AVAILABLE =
            "com.assistant.peripheral.MIC_AVAILABLE"

        internal fun shouldBroadcastMicUnavailable(failures: Int): Boolean =
            failures >= MIC_RETRY_WARN_THRESHOLD

        internal fun micRetryWarnThresholdForTest(): Int = MIC_RETRY_WARN_THRESHOLD

        /**
         * Target pre-buffer duration. The silence monitor keeps this much
         * audio rolling so when activity is detected (which takes at least
         * ACTIVITY_HOLD_MS to confirm), the engine can replay the user's
         * leading edge. 500ms is enough to cover "hey wake up" since wake
         * onset is detected ~30-100ms into the phrase.
         */
        private const val PRE_BUFFER_MS = 500

        /**
         * How many reads of `readShorts` size fit in PRE_BUFFER_MS at the
         * silence-monitor sample rate. 16kHz mono → 8000 samples per 500ms.
         */
        internal fun computePreBufferCapacity(readShorts: Int): Int {
            val targetSamples = SAMPLE_RATE * PRE_BUFFER_MS / 1000
            return (targetSamples / readShorts).coerceAtLeast(1)
        }
    }

    /**
     * Single source of truth for the detector's lifecycle (Inc 6, plan §2.2).
     * @Volatile because the silence-monitor IO coroutine reads it without
     * a Main-dispatch round-trip on each iteration. All WRITES happen on
     * Main per the existing convention.
     */
    @Volatile
    private var state: WakeWordState = WakeWordState.Stopped

    /**
     * Public read-API preserved byte-compatibly (plan §3 Inc 6). Downstream
     * consumers (`AssistantService.rearmWakeWord`, `MainActivity:276`
     * `LaunchedEffect`) keep reading `isActive` / `isPaused` as today.
     */
    val isActive: Boolean get() = derivedIsActive(state)
    val isPaused: Boolean get() = derivedIsPaused(state)
    private val isRecognizing: Boolean get() = derivedIsRecognizing(state)

    private var consecutiveMisses = 0  // exponential backoff counter

    // talkWord / wakeWord may be comma-separated lists of phrases.
    private val talkVariants = talkWord.split(",")
        .map { it.trim() }.filter { it.isNotEmpty() }
        .flatMap { buildVariants(it) }.distinct()
    private val wakeVariants = if (wakeWord.isNotEmpty())
        wakeWord.split(",").map { it.trim() }.filter { it.isNotEmpty() }
            .flatMap { buildVariants(it) }.distinct()
    else emptyList()

    // Stage 1: silence monitor runs on a background IO thread.
    private var audioRecord: AudioRecord? = null
    private var silenceMonitorJob: Job? = null

    private val scope = CoroutineScope(Dispatchers.Main + SupervisorJob())

    private val recognitionCallbacks = object : RecognitionCallbacks {
        override fun onRecognitionStarted() {
            // Stamp beganSpeechAtMs on the Recognizing state for the
            // Inc 6 FSM observability contract. Idempotent if state has
            // already moved off Recognizing.
            (state as? WakeWordState.Recognizing)?.let {
                state = it.copy(
                    beganSpeechAtMs = android.os.SystemClock.elapsedRealtime(),
                )
            }
        }

        override fun onUnhealthy() {
            LocalBroadcastManager.getInstance(context)
                .sendBroadcast(Intent(ACTION_RECOGNIZER_UNHEALTHY))
        }
    }

    /**
     * Stage 2 — the recognition engine.
     *
     * V3b selection: Vosk if `VoskModelLoader.getModel()` returns non-null,
     * else SpeechRecognizer (legacy fallback). Resolved on first `warm()`
     * inside the silence monitor — by then `AssistantService.onCreate`'s
     * eager Vosk load (V2) has usually finished, so we get the cached model
     * with zero blocking. If the load is still in flight, `getModel`
     * suspends until done; if it failed (no native lib, no model, etc.),
     * we transparently fall back to SR.
     *
     * Once chosen, the engine is sticky for the detector's lifetime. A
     * full detector rebuild (via Inc 3 dedupe + ACTION_RECOGNIZER_UNHEALTHY)
     * is required to re-select.
     */
    @Volatile
    private var engine: WakeWordRecognitionEngine? = null

    /**
     * Whisper confirmation gate. Lazily built on first match so a detector
     * that never fires costs nothing. Null when [serverUrl] is blank — then
     * matches fire unconfirmed (fail-open on a misconfigured detector).
     */
    private val whisperConfirmer: WhisperConfirmer? by lazy {
        if (serverUrl.isBlank()) {
            Log.w(TAG, "No serverUrl — Whisper confirmation disabled (firing unconfirmed)")
            null
        } else {
            WhisperConfirmer(
                apiClient = com.assistant.peripheral.network.ApiClient(serverUrl),
                talkVariants = talkVariants,
                wakeVariants = wakeVariants,
            )
        }
    }

    private fun srEngine(): WakeWordRecognitionEngine = SpeechRecognizerEngine(
        context = context,
        talkVariants = talkVariants,
        wakeVariants = wakeVariants,
        callbacks = recognitionCallbacks,
        scope = scope,
    )

    /**
     * Resolve the engine on first use. Called from the silence monitor's
     * pre-warm step. Falls back to SR on any Vosk-side failure.
     */
    private suspend fun ensureEngine(): WakeWordRecognitionEngine {
        engine?.let { return it }
        val model = try {
            VoskModelLoader.getModel(context)
        } catch (t: Throwable) {
            Log.w(TAG, "VoskModelLoader threw — falling back to SpeechRecognizer", t)
            null
        }
        val resolved = if (model != null) {
            Log.d(TAG, "Engine selected: Vosk (model loaded)")
            VoskRecognitionEngine(
                model = model,
                talkVariants = talkVariants,
                wakeVariants = wakeVariants,
                callbacks = recognitionCallbacks,
            )
        } else {
            Log.d(TAG, "Engine selected: SpeechRecognizer (Vosk unavailable)")
            srEngine()
        }
        engine = resolved
        return resolved
    }

    // -------------------------------------------------------------------------
    // Public API
    // -------------------------------------------------------------------------

    fun start() {
        if (!SpeechRecognizer.isRecognitionAvailable(context)) {
            Log.w(TAG, "Speech recognition not available on this device")
            return
        }
        if (shouldShortCircuitStart(isActive, isPaused)) {
            Log.d(TAG, "start() ignored — already active")
            return
        }
        Log.d(TAG, "Starting — talk variants: $talkVariants")
        if (wakeVariants.isNotEmpty()) Log.d(TAG, "Wake variants: $wakeVariants")
        // Transition Stopped → Idle. startSilenceMonitor then promotes
        // Idle → SilenceMonitor once the mic is actually acquired.
        state = WakeWordState.Idle
        startSilenceMonitor()
    }

    /**
     * Temporarily suspend detection without fully stopping. Call resume() to
     * re-arm. Safe to call from any thread.
     */
    fun pause() {
        if (!isActive || isPaused) return
        Log.d(TAG, "Pausing wake word detection")
        state = WakeWordState.Paused
        silenceMonitorJob?.cancel()
        silenceMonitorJob = null
        stopAudioRecord()
        // Detour 6 (Option A): full teardown of the warm recognizer on
        // pause — voice session is taking the mic and may push the HAL
        // through MODE_IN_COMMUNICATION; we don't want stale state.
        engine?.let { e -> scope.launch { e.tearDown() } }
        engine?.markNeedsRefresh()
    }

    /**
     * Resume after pause(). Re-arms the silence monitor.
     */
    fun resume() {
        if (!isActive || !isPaused) return
        Log.d(TAG, "Resuming wake word detection")
        state = WakeWordState.Idle
        consecutiveMisses = 0
        // Detour 6 safeguard C: force a fresh recognizer post-voice.
        engine?.markNeedsRefresh()
        startSilenceMonitor()
    }

    fun stop() {
        state = WakeWordState.Stopped
        consecutiveMisses = 0
        silenceMonitorJob?.cancel()
        silenceMonitorJob = null
        stopAudioRecord()
        engine?.let { e -> scope.launch { e.tearDown() } }
        scope.coroutineContext.cancelChildren()
    }

    fun release() {
        stop()
        scope.cancel()
    }

    // -------------------------------------------------------------------------
    // Stage 1 — Silence monitor
    // -------------------------------------------------------------------------

    private fun startSilenceMonitor() {
        if (!isActive || isPaused) return
        stopAudioRecord()

        val bufferSize = AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL_CONFIG, AUDIO_FORMAT)
            .coerceAtLeast(3200)  // at least 100ms of audio at 16kHz 16-bit mono

        // Match the voice call's mic source. Originally MIC, which on
        // Samsung Lollipop left the HAL's AGC in a different state than
        // the call's VOICE_RECOGNITION expected — the first 30s of the
        // post-wake-word call came up with half the amplitude of a
        // cold-start call. Sharing the source keeps the HAL state continuous.
        @Suppress("DEPRECATION")
        val wakeWordSource = if (Build.VERSION.SDK_INT < Build.VERSION_CODES.N)
            MediaRecorder.AudioSource.VOICE_RECOGNITION
        else
            MediaRecorder.AudioSource.VOICE_COMMUNICATION

        silenceMonitorJob = scope.launch(Dispatchers.IO) {
            // Retry loop: mic may be held by AudioRecorder (turn-based
            // recording) for a few seconds. Keep trying until the mic is
            // free or we're no longer active. Inc 9: count failures and
            // broadcast ACTION_MIC_UNAVAILABLE at threshold so
            // AssistantService updates the notification text.
            var recorder: AudioRecord? = null
            var failures = 0
            var notifiedUnavailable = false
            while (isActive && recorder == null) {
                val candidate = try {
                    AudioRecord(
                        wakeWordSource,
                        SAMPLE_RATE,
                        CHANNEL_CONFIG,
                        AUDIO_FORMAT,
                        bufferSize,
                    )
                } catch (e: Exception) {
                    Log.w(TAG, "Failed to create AudioRecord (will retry): ${e.message}")
                    failures++
                    if (!notifiedUnavailable && shouldBroadcastMicUnavailable(failures)) {
                        Log.w(TAG, "Mic unavailable for $failures consecutive attempts — broadcasting warning")
                        LocalBroadcastManager.getInstance(context)
                            .sendBroadcast(Intent(ACTION_MIC_UNAVAILABLE))
                        notifiedUnavailable = true
                    }
                    delay(500L)
                    continue
                }

                if (candidate.state != AudioRecord.STATE_INITIALIZED) {
                    Log.w(TAG, "AudioRecord not initialized (mic busy, will retry)")
                    candidate.release()
                    failures++
                    if (!notifiedUnavailable && shouldBroadcastMicUnavailable(failures)) {
                        Log.w(TAG, "Mic unavailable for $failures consecutive attempts — broadcasting warning")
                        LocalBroadcastManager.getInstance(context)
                            .sendBroadcast(Intent(ACTION_MIC_UNAVAILABLE))
                        notifiedUnavailable = true
                    }
                    delay(500L)
                    continue
                }

                recorder = candidate
            }
            // Inc 9: clear any prior mic-unavailable warning now that
            // acquisition succeeded.
            if (notifiedUnavailable) {
                Log.d(TAG, "Mic acquired after $failures failures — broadcasting clear")
                LocalBroadcastManager.getInstance(context)
                    .sendBroadcast(Intent(ACTION_MIC_AVAILABLE))
            }
            if (recorder == null || !isActive) return@launch

            audioRecord = recorder
            recorder.startRecording()
            if (recorder.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
                Log.w(TAG, "AudioRecord.startRecording() failed — mic busy, will retry")
                recorder.release()
                audioRecord = null
                delay(500L)
                withContext(Dispatchers.Main) {
                    if (isActive && !isPaused) startSilenceMonitor()
                }
                return@launch
            }
            val effectiveThresholdLog =
                if (micGain > 0f) RMS_THRESHOLD / micGain else RMS_THRESHOLD
            Log.d(
                TAG,
                "Silence monitor started (threshold=$RMS_THRESHOLD, gain=$micGain, effective=${effectiveThresholdLog.toInt()})",
            )
            // Idle → SilenceMonitor. Detour 6 (Option A): pre-warm the engine
            // NOW, in parallel with the RMS read loop, so when activity is
            // detected the engine is ready to handle it immediately.
            val activeEngine = ensureEngine()
            withContext(Dispatchers.Main) {
                if (state is WakeWordState.Idle) state = WakeWordState.SilenceMonitor
                activeEngine.warm()
            }

            val buffer = ShortArray(bufferSize / 2)
            var activityStartMs = 0L

            // Rolling pre-buffer of the last ~PRE_BUFFER_MS of audio. When
            // activity is detected and we hand off to a Vosk-style engine,
            // these frames are fed FIRST so the leading edge of the user's
            // wake phrase isn't lost. Without this, the silence monitor
            // consumes the "wake" of "wake up" while watching RMS, then
            // hands the recognizer a stream that already started mid-word
            // — exactly the SpeechRecognizer leading-edge clip we're trying
            // to escape. Empty for engines with needsExclusiveMic=true (SR
            // ignores the pre-buffer because it opens its own mic).
            val preBuffer = ArrayDeque<ShortArray>()
            val maxPreBufferReads = computePreBufferCapacity(buffer.size)

            while (isActive && !isRecognizing) {
                val read = recorder.read(buffer, 0, buffer.size)
                if (read <= 0) continue

                // Snapshot the read into the rolling pre-buffer regardless
                // of RMS — pre-activity frames may carry the wake word's
                // onset that crossed the threshold just as we read this chunk.
                val snapshot = buffer.copyOf(read)
                preBuffer.addLast(snapshot)
                while (preBuffer.size > maxPreBufferReads) preBuffer.removeFirst()

                val rms = computeRms(buffer, read)

                // Scale threshold by mic gain so sensitivity stays constant
                // regardless of gain setting. Higher gain → louder audio →
                // lower effective threshold needed to trigger. If gain is 0,
                // fall back to base threshold (avoids division by zero).
                val effectiveThreshold =
                    if (micGain > 0f) RMS_THRESHOLD / micGain else RMS_THRESHOLD

                if (rms >= effectiveThreshold) {
                    if (activityStartMs == 0L) {
                        activityStartMs = System.currentTimeMillis()
                    } else if (System.currentTimeMillis() - activityStartMs >= ACTIVITY_HOLD_MS) {
                        Log.d(
                            TAG,
                            "Audio activity detected (rms=${"%.0f".format(rms)}) — starting recognizer",
                        )
                        // Hand off to the engine. SR engine needs exclusive
                        // mic — release the AudioRecord first, hop to Main,
                        // run the legacy cycle (unchanged).
                        if (activeEngine.needsExclusiveMic) {
                            stopAudioRecord()
                            withContext(Dispatchers.Main) {
                                if (isActive && !isRecognizing) {
                                    runRecognitionCycle(sharedAudioRecord = null)
                                }
                            }
                            return@launch
                        }
                        // Vosk path: run recognition INLINE on this same IO
                        // coroutine — NO dispatch hop, so the mic stream stays
                        // continuous from monitoring → recognition → (talk)
                        // command capture. The old IO→Main→IO hand-off dropped
                        // audio mid-phrase, so "hello my friend" often decoded
                        // as just "hello". Only Main-confined mutations (the
                        // FSM state) hop to Main; the reads never leave IO.
                        val ar = audioRecord ?: return@launch
                        val preFrames = preBuffer.toList()
                        runVoskRecognitionInline(ar, preFrames)
                        return@launch
                    }
                } else {
                    activityStartMs = 0L
                }
            }

            try {
                recorder.stop()
                recorder.release()
            } catch (e: Exception) {
                Log.w(TAG, "Error stopping recorder at end of loop: ${e.message}")
            }
            audioRecord = null
        }
    }

    private fun stopAudioRecord() {
        try {
            audioRecord?.stop()
            audioRecord?.release()
        } catch (e: Exception) {
            Log.w(TAG, "Error stopping AudioRecord: ${e.message}")
        }
        audioRecord = null
    }

    private fun computeRms(buffer: ShortArray, count: Int): Double {
        if (count <= 0) return 0.0
        var sum = 0.0
        for (i in 0 until count) {
            val v = buffer[i].toDouble()
            sum += v * v
        }
        return sqrt(sum / count)
    }

    /**
     * Loudest per-frame RMS across a captured clip — the clip's peak energy.
     * Used by the pre-Whisper speech-floor gate: a genuine spoken phrase has
     * at least one loud frame, whereas a phantom Vosk match on silence/ambient
     * noise stays uniformly quiet. Peak (not mean) so a short phrase inside a
     * mostly-quiet 2s window isn't averaged away.
     */
    private fun peakFrameRms(frames: List<ShortArray>): Double {
        var peak = 0.0
        for (frame in frames) {
            val r = computeRms(frame, frame.size)
            if (r > peak) peak = r
        }
        return peak
    }

    /**
     * True when the captured clip carries real speech energy (its peak frame
     * RMS crosses [SPEECH_FLOOR_RMS]). When false, the Vosk match was almost
     * certainly a constrained-grammar hallucination on silence/ambient noise —
     * the caller should reject it WITHOUT calling Whisper (whisper-1 itself
     * hallucinates canned phrases on near-silent audio, so silence must never
     * reach it). Empty clip → not speech.
     */
    private fun clipHasSpeech(frames: List<ShortArray>): Boolean {
        if (frames.isEmpty()) return false
        return peakFrameRms(frames) >= SPEECH_FLOOR_RMS
    }

    // -------------------------------------------------------------------------
    // Stage 2 — Recognition cycle (engine-driven)
    // -------------------------------------------------------------------------

    /**
     * Run one engine recognition cycle. Centralises the per-cycle FSM
     * transitions and post-cycle action (broadcast, audio mode/beep
     * post-processing happens INSIDE the engine; we only handle backoff +
     * re-arm here).
     *
     * Called on `Dispatchers.Main` from `startSilenceMonitor` after activity
     * has been detected.
     */
    private suspend fun runRecognitionCycle(
        sharedAudioRecord: AudioRecord?,
        preBuffer: List<ShortArray> = emptyList(),
    ) {
        if (!isActive || isRecognizing) return
        // ensureEngine() ran during the warm step; if it's somehow null here
        // a teardown raced us — exit without doing anything.
        val activeEngine = engine ?: return
        state = WakeWordState.Recognizing(
            startedAtMs = android.os.SystemClock.elapsedRealtime(),
            beganSpeechAtMs = null,
        )
        val result = activeEngine.recognize(sharedAudioRecord, preBuffer)
        // Inc 2 idempotency guard: short-circuit if recognition has finished
        // AND the engine has cleaned up its pending state. Mid-teardown
        // (recognizing=false but engine still holds state) still runs the
        // post-cycle body.
        if (shouldShortCircuitFinishRecognition(isRecognizing, activeEngine.hasPendingState)) {
            Log.d(TAG, "post-cycle ignored — already finished")
            return
        }
        // Recognizing → Idle. The deferred startSilenceMonitor() below will
        // promote Idle → SilenceMonitor when it acquires the mic. A
        // concurrent pause()/stop() may have already moved us to
        // Paused/Stopped, in which case the `if (isActive && !isPaused)`
        // guard below correctly no-ops the rearm.
        if (state is WakeWordState.Recognizing) state = WakeWordState.Idle

        val matched = result as? RecognitionResult.Matched
        // Whisper confirmation gate: a raw Vosk match is only a candidate. We
        // transcribe the exact flagged audio and fire only if a real wake/talk
        // variant is present. `confirmed` stays true for the unconfigured /
        // disabled path (fail-open) and for the SR fallback (no captured PCM).
        // This path is now SR-fallback only (Vosk runs inline via
        // runVoskRecognitionInline). SR matches carry no capturedPcm and open
        // their own mic, so same-mic capture doesn't apply — confirm (fail-open
        // for SR) then fire the legacy broadcast.
        var confirmed = false
        if (matched != null) {
            confirmed = confirmMatch(matched)
            if (confirmed) fireMatchBroadcast(matched)
        }

        val restartDelay = when {
            matched != null -> {
                // Both confirmed and rejected matches consumed the utterance;
                // apply the post-wakeword cooldown either way so a rejected
                // false-positive doesn't immediately re-fire on the same audio.
                consecutiveMisses = 0
                POST_WAKEWORD_DELAY_MS
            }
            result is RecognitionResult.NoSpeech ||
                (result is RecognitionResult.Error && result.flatDelay) -> {
                // CLIENT_ERROR / NO_SPEECH flat delay. Don't accumulate
                // backoff — the error usually resolves in ~1s and we
                // shouldn't wait 2s/4s/8s/30s for something that's not our
                // fault.
                SpeechRecognizerEngine.CLIENT_ERROR_DELAY_MS
            }
            result is RecognitionResult.Cancelled -> {
                // Engine was cancelled mid-cycle (pause / stop / teardown).
                // Do not re-arm. The pause / stop flow already cancelled
                // the silence-monitor job.
                return
            }
            else -> {
                // Exponential backoff: 1s, 2s, 4s, 8s, 16s, 30s (cap).
                val backoff = (POST_RECOGNITION_BASE_MS shl consecutiveMisses)
                    .coerceAtMost(POST_RECOGNITION_MAX_MS)
                consecutiveMisses++
                if (consecutiveMisses >= 10) {
                    Log.w(TAG, "No match — miss #$consecutiveMisses (at max backoff)")
                } else {
                    Log.d(TAG, "No match — miss #$consecutiveMisses, waiting ${backoff}ms")
                }
                backoff
            }
        }
        scope.launch {
            delay(restartDelay)
            if (isActive && !isPaused) startSilenceMonitor()
        }
    }

    /**
     * Vosk recognition + (talk) command capture, run INLINE on the silence
     * monitor's IO coroutine — no dispatch hop, so the shared AudioRecord
     * stream is continuous from monitoring through recognition through command
     * capture. This is the fix for the leading-edge loss that made
     * "hello my friend" decode as just "hello".
     *
     * Realtime wake match: confirm → broadcast → return (the app hands off to
     * the WebRTC voice conversation — unchanged behaviour). The re-arm after
     * POST_WAKEWORD_DELAY_MS still happens; if voice takes the mic, the
     * silence monitor's acquisition retry / pauseWakeWord handles it.
     *
     * Talk match: confirm → capture the command on THIS mic with VAD → auto-
     * send. Only after capture completes do we release + re-arm.
     */
    private suspend fun runVoskRecognitionInline(
        ar: AudioRecord,
        preBuffer: List<ShortArray>,
    ) {
        if (!isActive || isRecognizing) return
        val activeEngine = engine ?: return
        withContext(Dispatchers.Main) {
            state = WakeWordState.Recognizing(
                startedAtMs = android.os.SystemClock.elapsedRealtime(),
                beganSpeechAtMs = null,
            )
        }
        // Vosk's recognize() reads from `ar` on this same IO context — the
        // stream continues seamlessly from the monitor loop's last read.
        val result = activeEngine.recognize(ar, preBuffer)

        withContext(Dispatchers.Main) {
            if (state is WakeWordState.Recognizing) state = WakeWordState.Idle
        }

        val matched = result as? RecognitionResult.Matched
        if (matched != null) {
            if (!matched.isRealtime && matched.capturedPcm.isNotEmpty()) {
                // TALK path. Vosk fired early (possibly just on the phrase's
                // opening word), so confirming NOW would only see "hello". Do
                // the same-mic command capture FIRST — collecting the whole
                // "hello my friend <command>" utterance — THEN confirm with
                // Whisper over the FULL audio, and send only if the talk phrase
                // is actually present. fireMatchBroadcast gives the instant UI
                // ack (beep + recording indicator) before the capture.
                fireMatchBroadcast(matched)
                val fullAudio = captureTalkCommand(ar, matched.capturedPcm)
                if (fullAudio != null) {
                    val confirmed = confirmMatchAudio(fullAudio, isRealtime = false)
                    if (confirmed) {
                        sendTalkAudio(fullAudio)
                    } else {
                        // Whisper didn't hear the talk phrase in the full
                        // utterance — drop it and clear the recording UI.
                        LocalBroadcastManager.getInstance(context).sendBroadcast(
                            Intent(ACTION_WAKE_CONFIRM_FAILED).putExtra(EXTRA_IS_REALTIME, false),
                        )
                    }
                }
            } else {
                // REALTIME wake (or SR edge). Its snapshot already holds the
                // full "wake up", so confirm then broadcast + hand off to the
                // WebRTC voice conversation. Unchanged good behaviour.
                val confirmed = confirmMatch(matched)
                if (confirmed) fireMatchBroadcast(matched)
            }
            consecutiveMisses = 0
        }

        val restartDelay = when {
            matched != null -> POST_WAKEWORD_DELAY_MS
            result is RecognitionResult.Cancelled -> return  // pause/stop already handled
            result is RecognitionResult.NoSpeech ||
                (result is RecognitionResult.Error && result.flatDelay) ->
                SpeechRecognizerEngine.CLIENT_ERROR_DELAY_MS
            else -> {
                val backoff = (POST_RECOGNITION_BASE_MS shl consecutiveMisses)
                    .coerceAtMost(POST_RECOGNITION_MAX_MS)
                consecutiveMisses++
                if (consecutiveMisses >= 10) {
                    Log.w(TAG, "No match — miss #$consecutiveMisses (at max backoff)")
                } else {
                    Log.d(TAG, "No match — miss #$consecutiveMisses, waiting ${backoff}ms")
                }
                backoff
            }
        }
        scope.launch {
            delay(restartDelay)
            if (isActive && !isPaused) startSilenceMonitor()
        }
    }

    /**
     * Gate a raw Vosk candidate through Whisper. Returns true if it should
     * fire, false if it should be silently dropped.
     *
     * Fail-open (returns true without calling Whisper) when:
     *  - no confirmer (blank serverUrl), or
     *  - the engine handed back no PCM (SR fallback path — it can't expose its
     *    audio, so we trust its match as before).
     *
     * Otherwise transcribes the captured audio and returns Whisper's verdict.
     * Whisper itself is fail-CLOSED (network error/timeout → reject) — that
     * policy lives in [WhisperConfirmer].
     */
    private suspend fun confirmMatch(match: RecognitionResult.Matched): Boolean {
        val confirmer = whisperConfirmer ?: return true
        if (match.capturedPcm.isEmpty()) {
            Log.d(TAG, "No captured PCM (SR path?) — firing without Whisper confirmation")
            return true
        }
        // Speech-floor pre-gate: a phantom Vosk match on silence/ambient noise
        // is uniformly quiet. Reject it here — before the Whisper call — so we
        // don't (a) waste a network round-trip on noise, or (b) feed whisper-1
        // near-silent audio it would hallucinate a wake phrase into. This is
        // the primary fix for "opens the conversation for no reason, even in
        // silence": such triggers never carried speech energy.
        if (!clipHasSpeech(match.capturedPcm)) {
            Log.d(
                TAG,
                "Pre-Whisper gate: captured clip below speech floor " +
                    "(peak=${peakFrameRms(match.capturedPcm).toInt()} < ${SPEECH_FLOOR_RMS.toInt()}) " +
                    "— rejecting phantom \"${match.matchedPhrase}\" without Whisper",
            )
            LocalBroadcastManager.getInstance(context).sendBroadcast(
                Intent(ACTION_WAKE_CONFIRM_FAILED).putExtra(EXTRA_IS_REALTIME, match.isRealtime),
            )
            return false
        }
        // Announce the candidate so the UI shows "confirming…" during the gate.
        LocalBroadcastManager.getInstance(context).sendBroadcast(
            Intent(ACTION_WAKE_CONFIRMING).putExtra(EXTRA_IS_REALTIME, match.isRealtime),
        )
        val result = confirmer.confirm(match.capturedPcm)
        if (!result.confirmed) {
            Log.d(
                TAG,
                "Whisper gate rejected \"${match.matchedPhrase}\" " +
                    "(failed=${result.failed}, heard=\"${result.transcript}\")",
            )
            LocalBroadcastManager.getInstance(context).sendBroadcast(
                Intent(ACTION_WAKE_CONFIRM_FAILED).putExtra(EXTRA_IS_REALTIME, match.isRealtime),
            )
            return false
        }
        return true
    }

    private fun fireMatchBroadcast(match: RecognitionResult.Matched) {
        // Bring app to foreground and unlock screen before broadcasting.
        AssistantService.bringToForeground(context)
        val action = if (match.isRealtime)
            ACTION_WAKE_WORD_DETECTED else ACTION_TALK_WORD_DETECTED
        LocalBroadcastManager.getInstance(context).sendBroadcast(Intent(action))
    }

    /**
     * Capture the spoken command that follows a confirmed talk-word, on the
     * SAME [audioRecord] the detector is already holding — no mic switch, so
     * the wake phrase and command are one continuous stream. Ends on
     * [COMMAND_SILENCE_MS] of sustained silence after speech began (VAD), or
     * at [COMMAND_MAX_MS]. Returns the full utterance ([wakePhrasePcm] pre-roll
     * + command) as PCM frames for the caller to Whisper-confirm + send, or
     * null if no command was spoken (onset timeout) or the detector stopped.
     *
     * Runs on the same IO coroutine as the silence monitor + Vosk recognition
     * (called from runVoskRecognitionInline) — the mic stream is continuous
     * from the wake phrase straight into the command, no gap.
     */
    private suspend fun captureTalkCommand(
        audioRecord: AudioRecord,
        wakePhrasePcm: List<ShortArray>,
    ): List<ShortArray>? = withContext(Dispatchers.IO) {
        // Two thresholds with hysteresis:
        //  - voiceThreshold (the activity level) marks "the user is speaking",
        //    refreshing the silence timer.
        //  - silenceThreshold is LOWER — audio must fall below THIS to count as
        //    silence. Using the same level for both means ambient room noise
        //    that sits right at the activity threshold never reads as silence,
        //    so the capture never ends ("never stops listening").
        val voiceThreshold =
            (if (micGain > 0f) RMS_THRESHOLD / micGain else RMS_THRESHOLD)
        val silenceThreshold = voiceThreshold * COMMAND_SILENCE_RATIO
        val command = ArrayList<ShortArray>(wakePhrasePcm)  // pre-roll first
        val buffer = ShortArray(3200)  // 200ms at 16kHz
        val startedMs = System.currentTimeMillis()
        var speechBegan = false
        var lastVoiceMs = startedMs
        var loggedRmsAtMs = 0L

        Log.d(TAG, "Talk command capture started (same mic, voice≥${voiceThreshold.toInt()}, silence<${silenceThreshold.toInt()})")
        while (isActive) {
            val now = System.currentTimeMillis()
            if (now - startedMs >= COMMAND_MAX_MS) {
                Log.d(TAG, "Talk command hit max ${COMMAND_MAX_MS}ms — sending")
                break
            }
            if (!speechBegan && now - startedMs >= COMMAND_SPEECH_ONSET_TIMEOUT_MS) {
                Log.d(TAG, "No command spoken within ${COMMAND_SPEECH_ONSET_TIMEOUT_MS}ms — aborting")
                return@withContext null
            }
            val read = audioRecord.read(buffer, 0, buffer.size)
            if (read <= 0) { delay(10L); continue }
            command.add(buffer.copyOf(read))
            val rms = computeRms(buffer, read)
            // Diagnostic: log RMS ~2x/sec so we can see why silence does/doesn't trip.
            if (now - loggedRmsAtMs >= 500) {
                Log.d(TAG, "  cmd rms=${rms.toInt()} speechBegan=$speechBegan silenceFor=${if (speechBegan) now - lastVoiceMs else 0}ms")
                loggedRmsAtMs = now
            }
            // Speech begins once we cross the (higher) voice threshold.
            if (rms >= voiceThreshold) speechBegan = true
            // "Not silent" = above the (lower) silence threshold. Any such frame
            // refreshes the timer; only a continuous run BELOW it ends capture.
            if (rms >= silenceThreshold) {
                lastVoiceMs = now
            } else if (speechBegan && now - lastVoiceMs >= COMMAND_SILENCE_MS) {
                Log.d(TAG, "Command ended on ${COMMAND_SILENCE_MS}ms silence — sending")
                break
            }
        }
        if (!isActive) return@withContext null

        val totalSamples = command.sumOf { it.size }
        Log.d(TAG, "Talk command captured: ${totalSamples * 1000 / SAMPLE_RATE}ms — confirming")
        command
    }

    /**
     * Whisper-confirm an arbitrary captured utterance (used by the talk path,
     * which confirms over the FULL "phrase + command" audio rather than the
     * early Vosk snapshot). Returns true if the confirmer isn't configured
     * (fail-open) or Whisper hears a matching variant; false on rejection.
     */
    private suspend fun confirmMatchAudio(
        frames: List<ShortArray>,
        isRealtime: Boolean,
    ): Boolean {
        val confirmer = whisperConfirmer ?: return true
        // Same speech-floor pre-gate as the realtime path: don't hand Whisper a
        // clip with no speech energy (a phantom talk-prefix trigger that
        // captured only ambient noise). The onset-timeout in captureTalkCommand
        // usually nulls these first, but guard here too so nothing silent ever
        // reaches whisper-1.
        if (!clipHasSpeech(frames)) {
            Log.d(
                TAG,
                "Pre-Whisper gate (talk): captured clip below speech floor " +
                    "(peak=${peakFrameRms(frames).toInt()} < ${SPEECH_FLOOR_RMS.toInt()}) — rejecting",
            )
            return false
        }
        LocalBroadcastManager.getInstance(context).sendBroadcast(
            Intent(ACTION_WAKE_CONFIRMING).putExtra(EXTRA_IS_REALTIME, isRealtime),
        )
        val result = confirmer.confirm(frames)
        if (!result.confirmed) {
            Log.d(TAG, "Whisper gate rejected talk utterance (failed=${result.failed}, heard=\"${result.transcript}\")")
        }
        return result.confirmed
    }

    /** WAV-encode + base64 the captured talk utterance and broadcast it for the
     *  app to send as a voice message. */
    private fun sendTalkAudio(frames: List<ShortArray>) {
        val wav = WavUtils.shortFramesToWav(frames, SAMPLE_RATE)
        val b64 = android.util.Base64.encodeToString(wav, android.util.Base64.NO_WRAP)
        Log.d(TAG, "Talk message confirmed: ${wav.size} bytes — broadcasting for send")
        LocalBroadcastManager.getInstance(context).sendBroadcast(
            Intent(ACTION_TALK_MESSAGE_CAPTURED).putExtra(EXTRA_TALK_AUDIO_B64, b64),
        )
    }
}
