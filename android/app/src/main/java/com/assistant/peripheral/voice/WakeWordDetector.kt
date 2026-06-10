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
        // justified lifting it — the change is grounded in measured device data,
        // not theory.
        private const val RMS_THRESHOLD = 70.0

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

        // Detour 6: refresh the warm SpeechRecognizer after this many
        // successful recognition cycles. Defense against long-term state
        // accumulation (binder leaks, AGC drift, Google service connection
        // staleness on long uptimes). 20 cycles ≈ 20 user wake-word
        // triggers — far less frequent than the old 2-hour timer but still
        // enough to keep the recognizer fresh in heavy use.
        private const val RECOGNIZER_REFRESH_AFTER_N = 20

        // Detour 6: refresh after this many consecutive NO_SPEECH errors
        // within the LOCAL detector (separate from Inc 8's service-level
        // rebuild threshold of 8). Lower bar because rebuilding the
        // recognizer is cheap (~500ms-1s) vs full detector teardown.
        private const val RECOGNIZER_REFRESH_NO_SPEECH_SPIKE = 2

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

        /**
         * Inc 9: mic-acquisition retry warn threshold. After this many
         * consecutive failed AudioRecord acquisitions in
         * startSilenceMonitor's retry loop, fire ACTION_MIC_UNAVAILABLE
         * so AssistantService can update the foreground notification.
         * 8 per plan §9 decision 7 — 4 s of churn at 500 ms / attempt.
         * 4 s is "something's wrong"; 8 s is too late.
         */
        private const val MIC_RETRY_WARN_THRESHOLD = 8

        /**
         * Inc 9: LocalBroadcast action fired when mic-acquisition has
         * been failing for MIC_RETRY_WARN_THRESHOLD attempts in a row.
         * AssistantService updates the notification text.
         */
        const val ACTION_MIC_UNAVAILABLE =
            "com.assistant.peripheral.MIC_UNAVAILABLE"

        /**
         * Inc 9: LocalBroadcast action fired when the FIRST successful
         * mic acquisition follows a stretch where ACTION_MIC_UNAVAILABLE
         * had been broadcast. Clears the warning notification.
         */
        const val ACTION_MIC_AVAILABLE =
            "com.assistant.peripheral.MIC_AVAILABLE"

        /**
         * Pure predicate for the Inc 9 mic-unavailable broadcast.
         * Strict `>=` so we fire AT the threshold, not after.
         */
        internal fun shouldBroadcastMicUnavailable(failures: Int): Boolean =
            failures >= MIC_RETRY_WARN_THRESHOLD

        internal fun micRetryWarnThresholdForTest(): Int = MIC_RETRY_WARN_THRESHOLD
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

    /**
     * Detour 6: counter for successful recognition cycles since the last
     * SpeechRecognizer (re)construction. Used by the periodic-refresh
     * safeguard (D). After RECOGNIZER_REFRESH_AFTER_N successful cycles,
     * the recognizer is destroyed and recreated on the next silence-monitor
     * arm — defends against long-term state accumulation (binder leaks,
     * AGC drift, etc).
     */
    private var recognitionsSinceWarmedUp = 0

    /**
     * Detour 6: pre-built RecognizerIntent so `startListening` can be
     * called with zero allocation. Reused across cycles. Constructed
     * lazily on first use.
     */
    private val warmRecognizerIntent: Intent by lazy { buildRecognizerIntent() }

    /**
     * Detour 6: tells `startSilenceMonitor` that the next silence→recognizer
     * transition should destroy the warm recognizer before listening.
     * Set true on:
     *  - watchdog fires (recognizer wedged)
     *  - consecutive NO_SPEECH spike (safeguard B)
     *  - pause/resume cycle (safeguard C)
     * Cleared once `ensureRecognizerWarm` actually rebuilds.
     */
    @Volatile private var recognizerNeedsRefresh = true

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
        // Detour 6 (Option A): full teardown of the warm recognizer on
        // pause — voice session is taking the mic and may push the HAL
        // through MODE_IN_COMMUNICATION; we don't want the warm recognizer
        // hanging onto stale state. Safeguard C.
        scope.launch {
            destroyRecognizer()
            if (wasRecognizing) unmuteBeep()
        }
        recognizerNeedsRefresh = true
    }

    /**
     * Resume after pause(). Re-arms the silence monitor.
     */
    fun resume() {
        if (!isActive || !isPaused) return
        Log.d(TAG, "Resuming wake word detection")
        // Paused → Idle. startSilenceMonitor promotes to SilenceMonitor
        // once mic acquisition succeeds and pre-warms the recognizer.
        state = WakeWordState.Idle
        consecutiveMisses = 0
        // Detour 6 safeguard C: force a fresh recognizer post-voice. The
        // voice session may have left Google's recognizer service in an
        // odd state; the warm recognizer on the next cycle should be brand
        // new.
        recognizerNeedsRefresh = true
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
            // Inc 9: count failed acquisitions and broadcast ACTION_MIC_UNAVAILABLE
            // at threshold so AssistantService updates the notification text. The
            // retry mechanism itself is preserved (per §1 non-goals) — only the
            // silence is broken. `notifiedUnavailable` ensures the broadcast is
            // emitted ONCE per stalled-stretch (idempotent at the receiver too,
            // but cheaper to gate at the source).
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
                        bufferSize
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
                    kotlinx.coroutines.delay(500L)
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
                    kotlinx.coroutines.delay(500L)
                    continue
                }

                recorder = candidate
            }
            // Inc 9: clear any prior mic-unavailable warning now that
            // acquisition succeeded. Only fire ACTION_MIC_AVAILABLE if we
            // had previously fired ACTION_MIC_UNAVAILABLE — avoids spamming
            // the notification on the steady-state happy path.
            if (notifiedUnavailable) {
                Log.d(TAG, "Mic acquired after $failures failures — broadcasting clear")
                LocalBroadcastManager.getInstance(context)
                    .sendBroadcast(Intent(ACTION_MIC_AVAILABLE))
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
            // Detour 6 (Option A): pre-warm the SpeechRecognizer NOW, in
            // parallel with the RMS read loop. On Lollipop, construction
            // takes 800ms-3s — running it during the silence-monitor stage
            // means activity → startListening fires in ~50ms instead of
            // burning the leading edge of the user's "wake up" on cold-start.
            withContext(Dispatchers.Main) {
                if (state is WakeWordState.Idle) state = WakeWordState.SilenceMonitor
                ensureRecognizerWarm()
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

    /**
     * Detour 6: pre-build the RecognizerIntent ONCE. The intent is
     * stateless and reusable across recognition cycles — the constants
     * inside (language, partial-results, timing extras) don't change
     * between cycles. Moved out of startRecognizer to support the warm-
     * recognizer pattern (Option A).
     */
    private fun buildRecognizerIntent(): Intent =
        Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
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

    /**
     * Detour 6 (Option A): ensure a SpeechRecognizer instance exists and
     * is ready to accept `startListening()` with minimal latency.
     *
     * Called from `startSilenceMonitor` once the AudioRecord is acquired —
     * this pre-warms the recognizer in parallel with the silence-monitor
     * loop, so when activity is detected, `startListening` returns in ~50ms
     * instead of the 800ms-3s cold-start observed on Lollipop.
     *
     * Honours `recognizerNeedsRefresh` (safeguards B, C, E) and the
     * `RECOGNIZER_REFRESH_AFTER_N` counter (safeguard D). When either
     * fires, destroy the existing instance before recreating.
     *
     * Must run on `Dispatchers.Main` — SpeechRecognizer construction
     * is Main-thread-only on Lollipop.
     */
    private fun ensureRecognizerWarm() {
        if (!SpeechRecognizer.isRecognitionAvailable(context)) return
        if (!isActive || isPaused) return
        // Safeguard D: periodic refresh.
        if (speechRecognizer != null && recognitionsSinceWarmedUp >= RECOGNIZER_REFRESH_AFTER_N) {
            Log.d(TAG, "Recognizer refresh — $recognitionsSinceWarmedUp cycles since last warm")
            recognizerNeedsRefresh = true
        }
        if (recognizerNeedsRefresh && speechRecognizer != null) {
            try {
                speechRecognizer?.cancel()
                speechRecognizer?.destroy()
            } catch (e: Exception) {
                Log.w(TAG, "Error destroying stale recognizer: ${e.message}")
            }
            speechRecognizer = null
        }
        if (speechRecognizer == null) {
            speechRecognizer = SpeechRecognizer.createSpeechRecognizer(context)
            speechRecognizer?.setRecognitionListener(recognitionListener)
            recognizerNeedsRefresh = false
            recognitionsSinceWarmedUp = 0
            Log.d(TAG, "Recognizer warmed and ready (will respond to startListening immediately)")
        }
    }

    /**
     * Detour 6: single reusable RecognitionListener instance. The
     * `listenerFinished` per-cycle guard moved to an instance field
     * (`listenerCycleFinished`) so the listener body can stay stateless.
     */
    private var listenerCycleFinished: Boolean = false

    private val recognitionListener = object : RecognitionListener {
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
            // the cycle so the silence monitor re-arms. The listenerCycleFinished
            // guard ensures we don't race a real callback that arrives
            // within ε of the watchdog firing — Inc 2's finishRecognition
            // idempotency guard is the belt-and-suspenders on the
            // finishRecognition body itself.
            recognizerWatchdogJob?.cancel()
            recognizerWatchdogJob = scope.launch {
                delay(RECOGNIZER_HANG_WATCHDOG_MS)
                if (!listenerCycleFinished) {
                    Log.w(TAG, "Recognizer watchdog fired — forcing finish after ${RECOGNIZER_HANG_WATCHDOG_MS}ms silence after onBeginningOfSpeech")
                    listenerCycleFinished = true
                    // Detour 6 safeguard E: watchdog fire means the recognizer
                    // is wedged. Force a refresh on the next cycle.
                    recognizerNeedsRefresh = true
                    finishRecognition(wakeWordDetected = false)
                }
            }
        }

        override fun onRmsChanged(rmsdB: Float) {}
        override fun onBufferReceived(buffer: ByteArray?) {}
        override fun onEndOfSpeech() {}

        override fun onError(error: Int) {
            if (listenerCycleFinished) return
            listenerCycleFinished = true
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
                // Detour 6 safeguard B: refresh the WARM recognizer locally
                // after a smaller NO_SPEECH spike — cheaper than the service-
                // level rebuild and addresses the "first wake-up clipped"
                // pattern when the warm recognizer hasn't re-armed cleanly.
                if (consecutiveNoSpeechErrors >= RECOGNIZER_REFRESH_NO_SPEECH_SPIKE) {
                    Log.d(TAG, "Marking warm recognizer for refresh — $consecutiveNoSpeechErrors NO_SPEECH errors in a row")
                    recognizerNeedsRefresh = true
                }
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
            if (listenerCycleFinished) return
            listenerCycleFinished = true
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
            if (listenerCycleFinished) return
            val partial = partialResults?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
            if (!partial.isNullOrEmpty()) {
                Log.d(TAG, "Partial: $partial")
                if (checkForWakeWord(partial)) {
                    // Early match on partial — stop immediately
                    listenerCycleFinished = true
                    recognizerWatchdogJob?.cancel()
                    finishRecognition(wakeWordDetected = true)
                }
            }
        }

        override fun onEvent(eventType: Int, params: Bundle?) {}
    }

    /**
     * Detour 6 (Option A): start the recognizer using the WARM instance.
     *
     * The SpeechRecognizer was constructed during silence-monitor arming
     * (`ensureRecognizerWarm`), so `startListening` returns in ~50 ms
     * instead of the 800ms-3s observed with the cold-start path.
     *
     * If the warm recognizer is missing for any reason (shouldn't happen
     * in normal flow but defends against init races), fall back to the
     * cold-start path inline.
     */
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

        // Detour 6: reset the per-cycle listener guard before calling
        // startListening — the listener instance is reused across cycles.
        listenerCycleFinished = false

        // Cold-start fallback: if the warm recognizer is missing (e.g. a
        // race during init), construct one here and proceed. This restores
        // the pre-Detour-6 behavior end-to-end as a defense in depth.
        if (speechRecognizer == null) {
            Log.w(TAG, "Recognizer not warm at startRecognizer — falling back to cold start")
            speechRecognizer = SpeechRecognizer.createSpeechRecognizer(context)
            speechRecognizer?.setRecognitionListener(recognitionListener)
            recognitionsSinceWarmedUp = 0
        }

        speechRecognizer?.startListening(warmRecognizerIntent)
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
        // Detour 6 (Option A): stop the warm recognizer's CURRENT listening
        // session but KEEP THE INSTANCE alive for the next cycle. cancel()
        // stops without delivering onResults/onError — appropriate here
        // because we've already consumed the result. destroyRecognizer() is
        // only invoked from full-teardown paths (stop/pause/release) or the
        // refresh safeguards.
        recognizerWatchdogJob?.cancel()
        recognizerWatchdogJob = null
        try {
            speechRecognizer?.cancel()
        } catch (e: Exception) {
            Log.w(TAG, "Error cancelling recognizer in finishRecognition: ${e.message}")
        }
        recognitionsSinceWarmedUp++
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
