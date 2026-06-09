package com.assistant.peripheral.voice

import android.content.Context
import android.content.Intent
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Build
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.util.Log
import androidx.localbroadcastmanager.content.LocalBroadcastManager
import com.assistant.peripheral.MainActivity
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
 *   Continuously reads raw PCM and computes RMS. When audio exceeds
 *   the threshold, releases the mic and hands off to Stage 2.
 *
 * Stage 2 — Speech recognizer (SpeechRecognizer, heavy):
 *   Runs a single recognition cycle. If the wake word is found in
 *   the results (exact or phonetic variant), fires a broadcast.
 *   Either way, returns to Stage 1.
 *
 * This avoids the constant SpeechRecognizer start/stop cycle (and beeps)
 * when the room is quiet.
 */
class WakeWordDetector(
    private val context: Context,
    // Per Detour 3 naming (plan §0.5):
    //   talkWord = turn-based single voice message ("push-to-talk")
    //   wakeWord = realtime WebRTC voice conversation ("wake up the assistant")
    private val talkWord: String,
    private val wakeWord: String = "", // empty = disabled
    private val micGain: Float = 1.0f, // scales RMS threshold (independent of voice session gain)
) {
    companion object {
        private const val TAG = "WakeWordDetector"
        const val ACTION_TALK_WORD_DETECTED = "com.assistant.peripheral.TURN_TALK_WORD_DETECTED"
        const val ACTION_WAKE_WORD_DETECTED = "com.assistant.peripheral.REALTIME_WAKE_WORD_DETECTED"

        private const val SAMPLE_RATE = 16000
        private const val CHANNEL_CONFIG = AudioFormat.CHANNEL_IN_MONO
        private const val AUDIO_FORMAT = AudioFormat.ENCODING_PCM_16BIT

        // RMS threshold — 0..32767 scale. ~200 catches normal speech in a quiet room.
        // Lowered from 300 after device cleanup reduced background noise — fewer ambient
        // processes means the mic is quieter at rest, so the old threshold was rarely
        // breached, giving fewer recognition opportunities per minute.
        private const val RMS_THRESHOLD = 200.0

        // How long audio must stay above threshold before we start recognizer (avoids clicks/pops)
        private const val ACTIVITY_HOLD_MS = 30L

        // After a successful wake word, pause before re-arming
        private const val POST_WAKEWORD_DELAY_MS = 3000L

        // Base delay after a missed recognition — doubles on each consecutive miss (backoff)
        private const val POST_RECOGNITION_BASE_MS = 1000L
        private const val POST_RECOGNITION_MAX_MS = 30_000L

        private const val CLIENT_ERROR_DELAY_MS = 1000L

        // Inc 4: post-`onBeginningOfSpeech` watchdog timeout. The SpeechRecognizer
        // sometimes hangs silently after `onBeginningOfSpeech` without ever
        // delivering `onResults` or `onError` (Bug 3 in the structural analysis;
        // reproduced live during Inc 2 device testing). 10 s is generous — wake
        // words are short and EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS
        // (1500 ms) already bounds normal completion well under this value.
        private const val RECOGNIZER_HANG_WATCHDOG_MS = 10_000L

        /**
         * Normalize a wake-word phrase. Inc 5 dropped the phonetic `wordSubs`
         * expansion table (plan §3 Inc 5): variants are now the configured
         * phrases verbatim. Per-phrase output is a single-element list
         * containing the lowercased + trimmed input. The split-by-comma
         * fan-out lives at the call site (`talkVariants` / `wakeVariants`
         * below) so a user can still configure multiple alternatives via
         * comma separation.
         */
        fun buildVariants(phrase: String): List<String> =
            listOf(phrase.lowercase().trim())

        /**
         * Pure predicate for the `start()` idempotency guard (Increment 1).
         * Returns true when a redundant `start()` should short-circuit:
         * detector already active AND not paused. A paused detector must
         * still run the full `start()` body so it re-arms (see
         * `WakeWordStartParityTest.guardDoesNotShortCircuitPausedDetector`).
         */
        internal fun shouldShortCircuitStart(isActive: Boolean, isPaused: Boolean): Boolean =
            isActive && !isPaused

        /**
         * Pure predicate for the `finishRecognition` idempotency guard
         * (Increment 2). Returns true when a redundant `finishRecognition`
         * call should short-circuit: the listener path has already cleared
         * `isRecognizing` AND `destroyRecognizer()` has nulled the recognizer.
         *
         * Both conditions are required: mid-teardown (one flag flipped but
         * not the other) MUST still run the body to complete cleanup. See
         * `FinishRecognitionParityTest.guardDoesNotShortCircuitMidTeardown`.
         *
         * This guard prevents a late `onResults` / `onError` racing a
         * partial-match early-finish from launching a second silence-monitor
         * coroutine. It is also a precondition for Increment 4's recognizer
         * hang watchdog — the watchdog firing concurrently with a late
         * callback is exactly the race this guard protects against.
         */
        internal fun shouldShortCircuitFinishRecognition(
            isRecognizing: Boolean,
            hasSpeechRecognizer: Boolean,
        ): Boolean = !isRecognizing && !hasSpeechRecognizer

        /**
         * State→legacy-flag derived predicates (Inc 6). The three booleans
         * `isActive`/`isPaused`/`isRecognizing` become functions of `state`
         * so the public read-API (`AssistantService.rearmWakeWord` at
         * `AssistantService.kt:173`, `MainActivity:276` LaunchedEffect, etc.)
         * keeps working byte-compatibly. Exposed on the companion so
         * `WakeWordFSMParityTest` can pin the mapping without instantiating
         * the detector (which eagerly constructs a Main-dispatched scope).
         */
        internal fun derivedIsActive(state: WakeWordState): Boolean =
            state !is WakeWordState.Stopped

        internal fun derivedIsPaused(state: WakeWordState): Boolean =
            state is WakeWordState.Paused

        internal fun derivedIsRecognizing(state: WakeWordState): Boolean =
            state is WakeWordState.Recognizing

        /**
         * Inc 8: NO_SPEECH-error health threshold. After this many
         * CONSECUTIVE `ERROR_NO_SPEECH` errors, the recognizer is
         * declared unhealthy and a `ACTION_RECOGNIZER_UNHEALTHY`
         * LocalBroadcast is fired so AssistantService can rebuild the
         * detector. 8 per plan §9 decision 6 — roughly 30s+60s+... ≈
         * 4–5 min of saturated backoff = clearly broken, not flaky.
         *
         * The rebuild is funnelled through Inc 3's dedupe so a flapping
         * recognizer can't trigger a rebuild storm.
         */
        private const val NO_SPEECH_HEALTH_THRESHOLD = 8

        /**
         * Inc 8: LocalBroadcast action that fires when the consecutive
         * NO_SPEECH-error count crosses NO_SPEECH_HEALTH_THRESHOLD.
         * AssistantService listens for this and re-invokes
         * `startWakeWord(lastTalkWord, lastWakeWord, lastWakeMicGain)`.
         */
        const val ACTION_RECOGNIZER_UNHEALTHY =
            "com.assistant.peripheral.RECOGNIZER_UNHEALTHY"

        /**
         * Pure predicate for the Inc 8 health check. Returns true when
         * the consecutive NO_SPEECH count has crossed
         * NO_SPEECH_HEALTH_THRESHOLD and the recognizer should be
         * declared unhealthy. Strict `>=` so we fire AT the threshold,
         * not after.
         */
        internal fun shouldBroadcastRecognizerUnhealthy(
            consecutiveNoSpeechErrors: Int,
        ): Boolean = consecutiveNoSpeechErrors >= NO_SPEECH_HEALTH_THRESHOLD

        internal fun noSpeechHealthThresholdForTest(): Int = NO_SPEECH_HEALTH_THRESHOLD
    }

    /**
     * Single source of truth for the detector's lifecycle (Inc 6, plan §2.2).
     * @Volatile because the silence-monitor IO coroutine reads it without
     * a Main-dispatch round-trip on each iteration (see startSilenceMonitor
     * inner loop). All WRITES happen on Main per the existing convention.
     */
    @Volatile
    private var state: WakeWordState = WakeWordState.Stopped

    /**
     * Public read-API preserved byte-compatibly (plan §3 Inc 6). Downstream
     * consumers (`AssistantService.rearmWakeWord`, `MainActivity:276`
     * `LaunchedEffect`) keep reading `isActive` / `isPaused` as today.
     * The `Idle` / `SilenceMonitor` distinction is internal to the FSM.
     */
    val isActive: Boolean get() = derivedIsActive(state)
    val isPaused: Boolean get() = derivedIsPaused(state)
    private val isRecognizing: Boolean get() = derivedIsRecognizing(state)

    private var consecutiveMisses = 0  // exponential backoff counter

    /**
     * Inc 8: consecutive `ERROR_NO_SPEECH` count. Incremented in the
     * recognizer listener when `onError(6)` fires; reset on `onResults`
     * or any non-NO_SPEECH error. When it crosses
     * `NO_SPEECH_HEALTH_THRESHOLD` we fire the
     * `ACTION_RECOGNIZER_UNHEALTHY` broadcast so AssistantService can
     * rebuild. Separate from `consecutiveMisses` — different semantic:
     * `consecutiveMisses` drives the exponential backoff schedule (any
     * non-detection counts); `consecutiveNoSpeechErrors` specifically
     * detects the Samsung Lollipop binder-death symptom where the
     * recognizer flaps with NO_SPEECH errors and never recovers without
     * a rebuild.
     */
    private var consecutiveNoSpeechErrors = 0

    // Pre-computed phonetic variants for faster matching.
    // talkWord / wakeWord may be comma-separated lists of phrases.
    //   talkVariants = phrases that trigger a single turn-based voice message
    //   wakeVariants = phrases that trigger a realtime WebRTC conversation
    private val talkVariants = talkWord.split(",")
        .map { it.trim() }.filter { it.isNotEmpty() }
        .flatMap { buildVariants(it) }.distinct()
    private val wakeVariants = if (wakeWord.isNotEmpty())
        wakeWord.split(",").map { it.trim() }.filter { it.isNotEmpty() }
            .flatMap { buildVariants(it) }.distinct()
    else emptyList()

    // Stage 1: silence monitor runs on a background IO thread
    private var audioRecord: AudioRecord? = null
    private var silenceMonitorJob: Job? = null

    // Stage 2: speech recognizer always runs on Main
    private var speechRecognizer: SpeechRecognizer? = null

    // Inc 4: post-`onBeginningOfSpeech` hang watchdog. Cancelled in `onResults`,
    // `onError`, the partial-match early-finish branch, and `destroyRecognizer`.
    // If the recognizer never calls back after `onBeginningOfSpeech`, this
    // job fires after RECOGNIZER_HANG_WATCHDOG_MS and force-finishes the
    // cycle so the silence monitor re-arms instead of staying stuck.
    private var recognizerWatchdogJob: Job? = null

    private val scope = CoroutineScope(Dispatchers.Main + SupervisorJob())
    private val audioManager = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager

    // True only after the recognizer pushed audioManager.mode into
    // MODE_IN_COMMUNICATION itself, so we know it's safe to revert.
    // If a voice session is also in MODE_IN_COMMUNICATION, we'd otherwise
    // flip the route to earpiece by resetting to MODE_NORMAL after a
    // non-match recognition cycle.
    private var weChangedAudioMode: Boolean = false

    private val BEEP_STREAMS = intArrayOf(
        AudioManager.STREAM_RING,
        AudioManager.STREAM_NOTIFICATION,
        AudioManager.STREAM_SYSTEM,
        AudioManager.STREAM_MUSIC,
    )

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
     * Temporarily suspend detection without fully stopping. Call resume() to re-arm.
     * Safe to call from any thread.
     */
    fun pause() {
        if (!isActive || isPaused) return
        Log.d(TAG, "Pausing wake word detection")
        // Snapshot whether we were mid-recognition BEFORE the transition;
        // the old code keyed the recognizer-teardown branch on the old
        // `isRecognizing` value. The FSM derives the predicate from
        // `state`, so we must capture it before assigning Paused.
        val wasRecognizing = isRecognizing
        state = WakeWordState.Paused
        // Stop mic + recognizer so they don't compete with voice session
        silenceMonitorJob?.cancel()
        silenceMonitorJob = null
        stopAudioRecord()
        if (wasRecognizing) {
            scope.launch { destroyRecognizer(); unmuteBeep() }
        }
    }

    /**
     * Resume after pause(). Re-arms the silence monitor.
     */
    fun resume() {
        if (!isActive || !isPaused) return
        Log.d(TAG, "Resuming wake word detection")
        // Paused → Idle. startSilenceMonitor promotes to SilenceMonitor
        // once mic acquisition succeeds.
        state = WakeWordState.Idle
        consecutiveMisses = 0
        startSilenceMonitor()
    }

    fun stop() {
        state = WakeWordState.Stopped
        consecutiveMisses = 0
        revertAudioModeIfOurs()
        unmuteBeep()
        silenceMonitorJob?.cancel()
        silenceMonitorJob = null
        stopAudioRecord()
        destroyRecognizer()
        scope.coroutineContext.cancelChildren()
    }

    /**
     * Only revert audioManager.mode to MODE_NORMAL if we were the ones
     * who set it to MODE_IN_COMMUNICATION.  If a voice session is
     * concurrently active, it set the mode itself for its own routing,
     * and flipping it to NORMAL here would force STREAM_VOICE_CALL to
     * the earpiece (Android pins that stream to the earpiece outside
     * MODE_IN_COMMUNICATION).  Symptom: dedicated phone's audio
     * silently routes to the earpiece regardless of speakerphone state.
     */
    private fun revertAudioModeIfOurs() {
        if (!weChangedAudioMode) return
        try { audioManager.mode = AudioManager.MODE_NORMAL } catch (_: Exception) {}
        weChangedAudioMode = false
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
        // cold-start call (observed 2026-06-04: max RMS 989 vs 1972 in
        // back-to-back sessions with same speaker distance). Sharing
        // the source keeps the HAL state continuous.
        @Suppress("DEPRECATION")
        val wakeWordSource = if (Build.VERSION.SDK_INT < Build.VERSION_CODES.N)
            MediaRecorder.AudioSource.VOICE_RECOGNITION
        else
            MediaRecorder.AudioSource.VOICE_COMMUNICATION

        silenceMonitorJob = scope.launch(Dispatchers.IO) {
            // Retry loop: mic may be held by AudioRecorder (turn-based recording) for a few seconds.
            // Keep trying until the mic is free or we're no longer active.
            var recorder: AudioRecord? = null
            while (isActive && recorder == null) {
                val candidate = try {
                    AudioRecord(
                        wakeWordSource,
                        SAMPLE_RATE,
                        CHANNEL_CONFIG,
                        AUDIO_FORMAT,
                        bufferSize
                    )
                } catch (e: Exception) {
                    Log.w(TAG, "Failed to create AudioRecord (will retry): ${e.message}")
                    kotlinx.coroutines.delay(500L)
                    continue
                }

                if (candidate.state != AudioRecord.STATE_INITIALIZED) {
                    Log.w(TAG, "AudioRecord not initialized (mic busy, will retry)")
                    candidate.release()
                    kotlinx.coroutines.delay(500L)
                    continue
                }

                recorder = candidate
            }
            if (recorder == null || !isActive) return@launch

            audioRecord = recorder
            recorder.startRecording()
            if (recorder.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
                // startRecording() failed (mic still held by another process, e.g. WebRTC)
                Log.w(TAG, "AudioRecord.startRecording() failed — mic busy, will retry")
                recorder.release()
                audioRecord = null
                kotlinx.coroutines.delay(500L)
                // Restart the whole monitor so we retry mic acquisition from scratch
                withContext(Dispatchers.Main) {
                    if (isActive && !isPaused) startSilenceMonitor()
                }
                return@launch
            }
            val effectiveThresholdLog = if (micGain > 0f) RMS_THRESHOLD / micGain else RMS_THRESHOLD
            Log.d(TAG, "Silence monitor started (threshold=$RMS_THRESHOLD, gain=$micGain, effective=${effectiveThresholdLog.toInt()})")
            // Idle → SilenceMonitor. The mic is acquired and recording; the
            // RMS read loop below is the SilenceMonitor stage. Only promote
            // if we're still in Idle — a concurrent pause()/stop() may have
            // transitioned away while we were waiting on AudioRecord.
            withContext(Dispatchers.Main) {
                if (state is WakeWordState.Idle) state = WakeWordState.SilenceMonitor
            }

            val buffer = ShortArray(bufferSize / 2)
            var activityStartMs = 0L

            while (isActive && !isRecognizing) {
                val read = recorder.read(buffer, 0, buffer.size)
                if (read <= 0) continue

                val rms = computeRms(buffer, read)

                // Scale threshold by mic gain so sensitivity stays constant regardless of gain setting.
                // Higher gain → louder audio → lower effective threshold needed to trigger.
                // If gain is 0, fall back to base threshold (avoids division by zero).
                val effectiveThreshold = if (micGain > 0f) RMS_THRESHOLD / micGain else RMS_THRESHOLD

                if (rms >= effectiveThreshold) {
                    if (activityStartMs == 0L) {
                        activityStartMs = System.currentTimeMillis()
                    } else if (System.currentTimeMillis() - activityStartMs >= ACTIVITY_HOLD_MS) {
                        Log.d(TAG, "Audio activity detected (rms=${"%.0f".format(rms)}) — starting recognizer")
                        // Release mic so SpeechRecognizer can use it
                        stopAudioRecord()
                        withContext(Dispatchers.Main) {
                            if (isActive && !isRecognizing) {
                                startRecognizer()
                            }
                        }
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
        var sum = 0.0
        for (i in 0 until count) {
            val s = buffer[i].toDouble()
            sum += s * s
        }
        return sqrt(sum / count)
    }

    // -------------------------------------------------------------------------
    // Stage 2 — Speech recognizer
    // -------------------------------------------------------------------------

    private fun startRecognizer() {
        if (!isActive || isRecognizing) return
        // SilenceMonitor → Recognizing(startedAtMs=now, beganSpeechAtMs=null).
        // `beganSpeechAtMs` is set later inside `onBeginningOfSpeech` by
        // re-assigning state with the same `startedAtMs`.
        state = WakeWordState.Recognizing(
            startedAtMs = android.os.SystemClock.elapsedRealtime(),
            beganSpeechAtMs = null,
        )

        muteBeep()
        // Set communication mode to suppress system beep on older devices.
        // Track that the change is ours so we don't fight a concurrent
        // voice session (which also sets MODE_IN_COMMUNICATION but for
        // routing, not beep suppression).
        try {
            audioManager.mode = AudioManager.MODE_IN_COMMUNICATION
            weChangedAudioMode = true
        } catch (_: Exception) {}
        destroyRecognizer()
        speechRecognizer = SpeechRecognizer.createSpeechRecognizer(context)

        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, context.packageName)
            putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 5)
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            // Force English so the wake word phrase is recognized correctly
            // regardless of the device's system language
            putExtra(RecognizerIntent.EXTRA_LANGUAGE, "en-US")
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_PREFERENCE, "en-US")
            // Prefer offline recognition for lower latency (falls back to online if unavailable)
            putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)
            // Suppress the start/stop beep on most Android devices
            putExtra("android.speech.extra.DICTATION_MODE", true)
            // Wait longer for silence so speech isn't cut off mid-phrase
            // Reduced minimum from 500ms — on this slow device the recognizer takes time to
            // bind, so by the time it's "ready", the wake word may already be partially spoken.
            // A lower minimum lets it accept short captures without timing out (ERROR_NO_SPEECH).
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_MINIMUM_LENGTH_MILLIS, 200L)
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS, 1500L)
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_POSSIBLY_COMPLETE_SILENCE_LENGTH_MILLIS, 1000L)
        }

        // Guard against double finishRecognition calls (onPartialResults early-exit + onResults/onError)
        var listenerFinished = false

        speechRecognizer?.setRecognitionListener(object : RecognitionListener {
            override fun onReadyForSpeech(params: Bundle?) {
                Log.d(TAG, "Recognizer ready")
            }

            override fun onBeginningOfSpeech() {
                Log.d(TAG, "Speech begun")
                // Stamp beganSpeechAtMs on the Recognizing state for future
                // watchdog/observability consumers (plan §2.3 transition row
                // "Recognizing → Recognizing(startedAtMs, now)"). Idempotent
                // if state has already moved off Recognizing (a late
                // onBeginningOfSpeech racing a watchdog-fired finish).
                (state as? WakeWordState.Recognizing)?.let {
                    state = it.copy(beganSpeechAtMs = android.os.SystemClock.elapsedRealtime())
                }
                // Inc 4: launch the hang watchdog. If neither onResults nor
                // onError fires within RECOGNIZER_HANG_WATCHDOG_MS, force-finish
                // the cycle so the silence monitor re-arms. The listenerFinished
                // guard ensures we don't race a real callback that arrives
                // within ε of the watchdog firing — Inc 2's finishRecognition
                // idempotency guard is the belt-and-suspenders on the
                // finishRecognition body itself.
                recognizerWatchdogJob?.cancel()
                recognizerWatchdogJob = scope.launch {
                    delay(RECOGNIZER_HANG_WATCHDOG_MS)
                    if (!listenerFinished) {
                        Log.w(TAG, "Recognizer watchdog fired — forcing finish after ${RECOGNIZER_HANG_WATCHDOG_MS}ms silence after onBeginningOfSpeech")
                        listenerFinished = true
                        finishRecognition(wakeWordDetected = false)
                    }
                }
            }

            override fun onRmsChanged(rmsdB: Float) {}
            override fun onBufferReceived(buffer: ByteArray?) {}
            override fun onEndOfSpeech() {}

            override fun onError(error: Int) {
                if (listenerFinished) return
                listenerFinished = true
                recognizerWatchdogJob?.cancel()
                Log.d(TAG, "Recognizer error: $error")
                // Inc 8: track consecutive NO_SPEECH errors. Sustained flaps
                // (8+ in a row, plan §9 decision 6) indicate the recognizer
                // is wedged — Samsung Lollipop binder-death after long
                // uptime is the canonical case. Reset on any non-NO_SPEECH
                // error so a single bad cycle doesn't permanently mark the
                // recognizer unhealthy. The broadcast fires AT the threshold
                // (once) and on each subsequent NO_SPEECH; AssistantService
                // funnels through Inc 3's dedupe so the rebuild rate is
                // capped at one per 3 s regardless.
                if (error == 6 /* ERROR_NO_SPEECH, added in API 23 */) {
                    consecutiveNoSpeechErrors++
                    if (shouldBroadcastRecognizerUnhealthy(consecutiveNoSpeechErrors)) {
                        Log.w(TAG, "Recognizer unhealthy — $consecutiveNoSpeechErrors consecutive NO_SPEECH errors; broadcasting rebuild request")
                        LocalBroadcastManager.getInstance(context)
                            .sendBroadcast(Intent(ACTION_RECOGNIZER_UNHEALTHY))
                    }
                } else {
                    consecutiveNoSpeechErrors = 0
                }
                // ERROR_CLIENT (7): double-call or internal SDK error — use flat delay, no backoff.
                // ERROR_NO_SPEECH (6): Google Recognition Service crash or audio routing issue —
                //   also use flat delay. Accumulating backoff here is wrong because the service
                //   will recover in ~1s; we don't want to wait 2s/4s/8s/30s for something
                //   that's not our fault and resolves quickly.
                val delay = if (error == SpeechRecognizer.ERROR_CLIENT ||
                                error == 6 /* ERROR_NO_SPEECH, added in API 23 */)
                    CLIENT_ERROR_DELAY_MS else -1L
                finishRecognition(wakeWordDetected = false, delay = delay)
            }

            override fun onResults(results: Bundle?) {
                if (listenerFinished) return
                listenerFinished = true
                recognizerWatchdogJob?.cancel()
                val matches = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
                Log.d(TAG, "Results: $matches")
                // Inc 8: any successful onResults clears the NO_SPEECH health
                // counter — the recognizer is alive even if the user didn't
                // say the wake word.
                consecutiveNoSpeechErrors = 0
                val detected = matches != null && checkForWakeWord(matches)
                finishRecognition(wakeWordDetected = detected)
            }

            override fun onPartialResults(partialResults: Bundle?) {
                if (listenerFinished) return
                val partial = partialResults?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
                if (!partial.isNullOrEmpty()) {
                    Log.d(TAG, "Partial: $partial")
                    if (checkForWakeWord(partial)) {
                        // Early match on partial — stop immediately
                        listenerFinished = true
                        recognizerWatchdogJob?.cancel()
                        finishRecognition(wakeWordDetected = true)
                    }
                }
            }

            override fun onEvent(eventType: Int, params: Bundle?) {}
        })

        speechRecognizer?.startListening(intent)
    }

    private fun finishRecognition(wakeWordDetected: Boolean, delay: Long = -1L) {
        if (shouldShortCircuitFinishRecognition(isRecognizing, speechRecognizer != null)) {
            Log.d(TAG, "finishRecognition() ignored — already finished")
            return
        }
        // Recognizing → Idle. The deferred `startSilenceMonitor()` call at
        // the bottom of this function (after `delay(restartDelay)`) will
        // promote Idle → SilenceMonitor when it acquires the mic. Only
        // transition off Recognizing — a concurrent pause()/stop() may
        // have already moved us to Paused/Stopped, in which case the
        // existing `if (isActive && !isPaused) startSilenceMonitor()`
        // guard below correctly no-ops the rearm.
        if (state is WakeWordState.Recognizing) state = WakeWordState.Idle
        destroyRecognizer()
        // Only reset audio mode if no wake word — if detected, VoiceManager will take ownership.
        // Guarded: only revert if we set the mode ourselves (see revertAudioModeIfOurs).
        if (!wakeWordDetected) revertAudioModeIfOurs() else weChangedAudioMode = false
        unmuteBeep()
        val restartDelay = when {
            delay >= 0 -> delay
            wakeWordDetected -> {
                consecutiveMisses = 0
                POST_WAKEWORD_DELAY_MS
            }
            else -> {
                // Exponential backoff: 1s, 2s, 4s, 8s, 16s, 30s (cap)
                val backoff = (POST_RECOGNITION_BASE_MS shl consecutiveMisses)
                    .coerceAtMost(POST_RECOGNITION_MAX_MS)
                consecutiveMisses++
                if (consecutiveMisses >= 10) {
                    Log.w(TAG, "No match — miss #$consecutiveMisses (at max backoff, recognizer may be stale)")
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

    private fun destroyRecognizer() {
        // Inc 4: cancel the hang watchdog defensively. Normal cycles cancel
        // it inside the listener (onResults/onError/partial-match), but a
        // teardown initiated from elsewhere (pause, stop) must also clear it
        // so a stale watchdog can't fire 10 s later against a detector that's
        // no longer holding a recognizer.
        recognizerWatchdogJob?.cancel()
        recognizerWatchdogJob = null
        try {
            speechRecognizer?.cancel()
            speechRecognizer?.destroy()
        } catch (e: Exception) {
            Log.w(TAG, "Error destroying recognizer: ${e.message}")
        }
        speechRecognizer = null
    }

    // -------------------------------------------------------------------------
    // Helpers
    // -------------------------------------------------------------------------

    @Suppress("DEPRECATION")
    private fun muteBeep() {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                // API 23+: ADJUST_MUTE is reference-counted and safe
                for (stream in BEEP_STREAMS) {
                    audioManager.adjustStreamVolume(stream, AudioManager.ADJUST_MUTE, 0)
                }
            } else {
                // API 21-22 (Lollipop): setStreamMute is reference-counted (safe — unmute reverses it)
                for (stream in BEEP_STREAMS) {
                    audioManager.setStreamMute(stream, true)
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Failed to mute beep streams: ${e.message}")
        }
    }

    @Suppress("DEPRECATION")
    private fun unmuteBeep() {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                for (stream in BEEP_STREAMS) {
                    audioManager.adjustStreamVolume(stream, AudioManager.ADJUST_UNMUTE, 0)
                }
            } else {
                for (stream in BEEP_STREAMS) {
                    audioManager.setStreamMute(stream, false)
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Failed to unmute beep streams: ${e.message}")
        }
    }

    private fun checkForWakeWord(results: List<String>): Boolean {
        for (result in results) {
            val lower = result.lowercase()
            // Check the realtime wake word FIRST — when a result contains both
            // a wake-word match and a talk-word match, the realtime conversation
            // takes precedence (more capable interaction). This preserves the
            // pre-Detour-3 precedence ordering exactly.
            if (wakeVariants.isNotEmpty() && wakeVariants.any { lower.contains(it) }) {
                Log.d(TAG, "Wake word (realtime) detected in: \"$result\"")
                // Bring app to foreground and unlock screen before broadcasting
                AssistantService.bringToForeground(context)
                LocalBroadcastManager.getInstance(context)
                    .sendBroadcast(Intent(ACTION_WAKE_WORD_DETECTED))
                return true
            }
            if (talkVariants.any { lower.contains(it) }) {
                Log.d(TAG, "Talk word (turn-based) detected in: \"$result\"")
                // Bring app to foreground and unlock screen before broadcasting
                AssistantService.bringToForeground(context)
                LocalBroadcastManager.getInstance(context)
                    .sendBroadcast(Intent(ACTION_TALK_WORD_DETECTED))
                return true
            }
        }
        return false
    }
}
