package com.assistant.peripheral.voice

import android.content.Context
import android.content.Intent
import android.media.AudioManager
import android.media.AudioRecord
import android.os.Build
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.util.Log
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Android `SpeechRecognizer`-backed implementation of
 * [WakeWordRecognitionEngine]. The legacy recognizer used by every wake-
 * word increment Inc 1 through Detour 6.
 *
 * Plan: `assistant/plans/wakeword_vosk_migration_plan_2026_06_09.md`,
 * §4 V3a (engine abstraction; this file extracts the SR machinery from
 * `WakeWordDetector` with zero behavioral change).
 *
 * Owns:
 *   - the `SpeechRecognizer` instance (warm per Detour 6);
 *   - the reusable `RecognizerIntent`;
 *   - the per-cycle `RecognitionListener` (single instance, reused);
 *   - the Inc 4 post-`onBeginningOfSpeech` hang watchdog;
 *   - the Inc 8 NO_SPEECH-error health counter;
 *   - the Detour 6 warm-recognizer refresh book-keeping
 *     (`recognitionsSinceWarmedUp`, `recognizerNeedsRefresh`,
 *     `listenerCycleFinished`);
 *   - the system-beep mute/unmute around `startListening`;
 *   - the `MODE_IN_COMMUNICATION` audio-mode toggle for beep suppression.
 *
 * Threading:
 *   - `SpeechRecognizer.createSpeechRecognizer` and `startListening` are
 *     Main-thread-only on Lollipop. All `warm()`, `recognize()`, and
 *     `tearDown()` bodies use `withContext(Dispatchers.Main)` for those
 *     calls. Callers can invoke from any context.
 */
internal class SpeechRecognizerEngine(
    private val context: Context,
    private val talkVariants: List<String>,
    private val wakeVariants: List<String>,
    private val callbacks: RecognitionCallbacks,
    private val scope: CoroutineScope,
) : WakeWordRecognitionEngine {

    companion object {
        private const val TAG = "SpeechRecogEngine"

        // ── Tuned constants — preserved verbatim from WakeWordDetector
        //    at HEAD ddfb53f. DO NOT change without empirical justification.

        // Inc 4: post-onBeginningOfSpeech watchdog timeout (10s).
        private const val RECOGNIZER_HANG_WATCHDOG_MS = 10_000L

        // Detour 6: refresh after this many successful recognition cycles.
        private const val RECOGNIZER_REFRESH_AFTER_N = 20

        // Detour 6: refresh after this many consecutive NO_SPEECH errors.
        private const val RECOGNIZER_REFRESH_NO_SPEECH_SPIKE = 2

        // Inc 8: NO_SPEECH-error health threshold (8 errors ≈ 4-5 min of
        // saturated backoff = clearly broken, not flaky).
        const val NO_SPEECH_HEALTH_THRESHOLD = 8

        // ERROR_CLIENT (7) / ERROR_NO_SPEECH (6) flat-delay.
        const val CLIENT_ERROR_DELAY_MS = 1000L

        /**
         * Pure predicate for the Inc 8 health check. Exposed so the parity
         * test pins the threshold. Strict `>=`.
         */
        internal fun shouldBroadcastRecognizerUnhealthy(
            consecutiveNoSpeechErrors: Int,
        ): Boolean = consecutiveNoSpeechErrors >= NO_SPEECH_HEALTH_THRESHOLD
    }

    private val audioManager =
        context.getSystemService(Context.AUDIO_SERVICE) as AudioManager

    private val beepStreams = intArrayOf(
        AudioManager.STREAM_RING,
        AudioManager.STREAM_NOTIFICATION,
        AudioManager.STREAM_SYSTEM,
        AudioManager.STREAM_MUSIC,
    )

    /**
     * Pre-built `RecognizerIntent`. Stateless, reusable across cycles.
     * Constructed lazily on first `warm`.
     */
    private val recognizerIntent: Intent by lazy { buildRecognizerIntent() }

    private var speechRecognizer: SpeechRecognizer? = null

    /**
     * Detour 6 / safeguards B, C, D, E: mark the warm recognizer for
     * refresh on the next `warm`. Set by NO_SPEECH spike, watchdog fire,
     * pause/resume cycle, periodic refresh, etc.
     */
    @Volatile
    private var recognizerNeedsRefresh = true

    /**
     * Detour 6 safeguard D: counter for successful recognition cycles
     * since the last (re)construction.
     */
    private var recognitionsSinceWarmedUp = 0

    /**
     * Inc 8: consecutive `ERROR_NO_SPEECH` count. Incremented in `onError`,
     * reset on `onResults` or any non-NO_SPEECH error.
     */
    private var consecutiveNoSpeechErrors = 0

    /**
     * Detour 6: per-cycle guard for the reused listener. Set true when the
     * current cycle has resolved (onResults, onError, partial-match early
     * finish, or watchdog fire); reset at the top of each new
     * `startListening`. Prevents racing late callbacks from re-completing
     * the cycle.
     */
    private var listenerCycleFinished = false

    /**
     * Inc 4 hang watchdog job. Launched in `onBeginningOfSpeech`, cancelled
     * in `onResults` / `onError` / partial-match early-finish / teardown.
     */
    private var watchdogJob: Job? = null

    /**
     * True only after this engine pushed `audioManager.mode` into
     * MODE_IN_COMMUNICATION itself, so we know it's safe to revert.
     * If a voice session is also in MODE_IN_COMMUNICATION (its own
     * reason — routing), reverting to MODE_NORMAL would force
     * STREAM_VOICE_CALL to the earpiece.
     */
    private var weChangedAudioMode = false

    /**
     * The current cycle's result handoff. `recognize()` awaits this; the
     * listener completes it. Re-assigned at the start of each cycle.
     */
    private var pendingResult: CompletableDeferred<RecognitionResult>? = null

    override val needsExclusiveMic: Boolean
        get() = true

    override val hasPendingState: Boolean
        get() = speechRecognizer != null

    override fun markNeedsRefresh() {
        recognizerNeedsRefresh = true
    }

    override suspend fun warm() = withContext(Dispatchers.Main) {
        if (!SpeechRecognizer.isRecognitionAvailable(context)) {
            Log.w(TAG, "Speech recognition not available — cannot warm")
            return@withContext
        }
        // Safeguard D: periodic refresh.
        if (speechRecognizer != null
            && recognitionsSinceWarmedUp >= RECOGNIZER_REFRESH_AFTER_N
        ) {
            Log.d(TAG, "Recognizer refresh — $recognitionsSinceWarmedUp cycles since warm")
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
            Log.d(TAG, "Recognizer warmed and ready")
        }
    }

    override suspend fun recognize(
        sharedAudioRecord: AudioRecord?,
        preBuffer: List<ShortArray>,
    ): RecognitionResult {
        check(sharedAudioRecord == null) {
            "SpeechRecognizerEngine needs exclusive mic — sharedAudioRecord must be null"
        }
        // SR opens its own mic; pre-buffer is meaningless to it.
        if (preBuffer.isNotEmpty()) {
            Log.d(TAG, "Ignoring ${preBuffer.size} pre-buffer frames (SR uses its own mic)")
        }
        val deferred = CompletableDeferred<RecognitionResult>()
        pendingResult = deferred

        withContext(Dispatchers.Main) {
            muteBeep()
            try {
                audioManager.mode = AudioManager.MODE_IN_COMMUNICATION
                weChangedAudioMode = true
            } catch (_: Exception) { /* best effort */ }

            // Detour 6: per-cycle guard reset on the reused listener.
            listenerCycleFinished = false

            // Cold-start fallback: if `warm` was skipped or raced, construct
            // here. Restores the pre-Detour-6 behavior as defense-in-depth.
            if (speechRecognizer == null) {
                Log.w(TAG, "Recognizer not warm at recognize() — cold-start fallback")
                speechRecognizer = SpeechRecognizer.createSpeechRecognizer(context)
                speechRecognizer?.setRecognitionListener(recognitionListener)
                recognitionsSinceWarmedUp = 0
            }
            speechRecognizer?.startListening(recognizerIntent)
        }

        val result = deferred.await()

        // Per-cycle post-processing — same order as the old finishRecognition
        // path: cancel watchdog, cancel listening, update counters, revert
        // audio mode, unmute beep.
        watchdogJob?.cancel()
        watchdogJob = null
        withContext(Dispatchers.Main) {
            try {
                speechRecognizer?.cancel()
            } catch (e: Exception) {
                Log.w(TAG, "Error cancelling recognizer after cycle: ${e.message}")
            }
            recognitionsSinceWarmedUp++
            // Only reset audio mode if no wake word — if matched, the
            // outer VoiceManager will take ownership.
            if (result !is RecognitionResult.Matched) {
                revertAudioModeIfOurs()
            } else {
                weChangedAudioMode = false
            }
            unmuteBeep()
        }
        return result
    }

    override suspend fun tearDown() = withContext(Dispatchers.Main) {
        watchdogJob?.cancel()
        watchdogJob = null
        try {
            speechRecognizer?.cancel()
            speechRecognizer?.destroy()
        } catch (e: Exception) {
            Log.w(TAG, "Error destroying recognizer: ${e.message}")
        }
        speechRecognizer = null
        // Resolve any in-flight cycle as Cancelled so a pending `recognize`
        // unblocks cleanly when callers tear us down mid-cycle.
        pendingResult?.let {
            if (!it.isCompleted) it.complete(RecognitionResult.Cancelled)
        }
        pendingResult = null
        // Restore beep / audio mode in case we were cancelled mid-listen.
        revertAudioModeIfOurs()
        unmuteBeep()
    }

    /**
     * Only revert audioManager.mode to MODE_NORMAL if we were the ones who
     * set it. Otherwise we'd flip STREAM_VOICE_CALL to the earpiece during
     * a concurrent voice session.
     */
    private fun revertAudioModeIfOurs() {
        if (!weChangedAudioMode) return
        try { audioManager.mode = AudioManager.MODE_NORMAL } catch (_: Exception) {}
        weChangedAudioMode = false
    }

    @Suppress("DEPRECATION")
    private fun muteBeep() {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                for (s in beepStreams) {
                    audioManager.adjustStreamVolume(s, AudioManager.ADJUST_MUTE, 0)
                }
            } else {
                for (s in beepStreams) audioManager.setStreamMute(s, true)
            }
        } catch (e: Exception) {
            Log.w(TAG, "Failed to mute beep streams: ${e.message}")
        }
    }

    @Suppress("DEPRECATION")
    private fun unmuteBeep() {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                for (s in beepStreams) {
                    audioManager.adjustStreamVolume(s, AudioManager.ADJUST_UNMUTE, 0)
                }
            } else {
                for (s in beepStreams) audioManager.setStreamMute(s, false)
            }
        } catch (e: Exception) {
            Log.w(TAG, "Failed to unmute beep streams: ${e.message}")
        }
    }

    private fun buildRecognizerIntent(): Intent =
        Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, context.packageName)
            putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 5)
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            putExtra(RecognizerIntent.EXTRA_LANGUAGE, "en-US")
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_PREFERENCE, "en-US")
            putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)
            putExtra("android.speech.extra.DICTATION_MODE", true)
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_MINIMUM_LENGTH_MILLIS, 200L)
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS, 1500L)
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_POSSIBLY_COMPLETE_SILENCE_LENGTH_MILLIS, 1000L)
        }

    /**
     * Variant matching. Matches `WakeWordDetector.checkForWakeWord` at HEAD
     * `ddfb53f` lines 1015–1031: realtime wakeVariants checked FIRST,
     * `lower.contains(it)` substring semantics.
     *
     * Returns null when no variant matches.
     */
    private fun matchPhrase(results: List<String>): RecognitionResult.Matched? {
        for (raw in results) {
            val lower = raw.lowercase()
            wakeVariants.firstOrNull { lower.contains(it) }?.let {
                Log.d(TAG, "Wake word (realtime) detected in: \"$raw\"")
                return RecognitionResult.Matched(it, isRealtime = true, rawText = raw)
            }
            talkVariants.firstOrNull { lower.contains(it) }?.let {
                Log.d(TAG, "Talk word (turn-based) detected in: \"$raw\"")
                return RecognitionResult.Matched(it, isRealtime = false, rawText = raw)
            }
        }
        return null
    }

    /**
     * Resolve the current `pendingResult` exactly once. Idempotent — late
     * callbacks racing the watchdog cannot re-resolve.
     */
    private fun completeOnce(result: RecognitionResult) {
        pendingResult?.let {
            if (!it.isCompleted) it.complete(result)
        }
    }

    private val recognitionListener = object : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) {
            Log.d(TAG, "Recognizer ready")
        }

        override fun onBeginningOfSpeech() {
            Log.d(TAG, "Speech begun")
            callbacks.onRecognitionStarted()
            // Inc 4: launch the hang watchdog.
            watchdogJob?.cancel()
            watchdogJob = scope.launch {
                delay(RECOGNIZER_HANG_WATCHDOG_MS)
                if (!listenerCycleFinished) {
                    Log.w(
                        TAG,
                        "Recognizer watchdog fired — forcing finish after ${RECOGNIZER_HANG_WATCHDOG_MS}ms silence",
                    )
                    listenerCycleFinished = true
                    // Detour 6 safeguard E: watchdog fire means wedged.
                    recognizerNeedsRefresh = true
                    completeOnce(RecognitionResult.Error("watchdog", flatDelay = false))
                }
            }
        }

        override fun onRmsChanged(rmsdB: Float) {}
        override fun onBufferReceived(buffer: ByteArray?) {}
        override fun onEndOfSpeech() {}

        override fun onError(error: Int) {
            if (listenerCycleFinished) return
            listenerCycleFinished = true
            watchdogJob?.cancel()
            Log.d(TAG, "Recognizer error: $error")
            // Inc 8: NO_SPEECH consecutive tracking. Reset on any non-NO_SPEECH.
            if (error == 6 /* ERROR_NO_SPEECH (also defined in API 23+) */) {
                consecutiveNoSpeechErrors++
                if (consecutiveNoSpeechErrors >= RECOGNIZER_REFRESH_NO_SPEECH_SPIKE) {
                    Log.d(
                        TAG,
                        "Marking warm recognizer for refresh — $consecutiveNoSpeechErrors NO_SPEECH in a row",
                    )
                    recognizerNeedsRefresh = true
                }
                if (shouldBroadcastRecognizerUnhealthy(consecutiveNoSpeechErrors)) {
                    Log.w(
                        TAG,
                        "Recognizer unhealthy — $consecutiveNoSpeechErrors NO_SPEECH errors; broadcasting rebuild",
                    )
                    callbacks.onUnhealthy()
                }
                completeOnce(RecognitionResult.NoSpeech)
            } else {
                consecutiveNoSpeechErrors = 0
                val flat = error == SpeechRecognizer.ERROR_CLIENT
                completeOnce(RecognitionResult.Error("err $error", flatDelay = flat))
            }
        }

        override fun onResults(results: Bundle?) {
            if (listenerCycleFinished) return
            listenerCycleFinished = true
            watchdogJob?.cancel()
            val matches = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
            Log.d(TAG, "Results: $matches")
            // Inc 8: any successful onResults clears the NO_SPEECH counter.
            consecutiveNoSpeechErrors = 0
            val matched = matches?.let { matchPhrase(it) }
            completeOnce(matched ?: RecognitionResult.NoMatch)
        }

        override fun onPartialResults(partialResults: Bundle?) {
            if (listenerCycleFinished) return
            val partial =
                partialResults?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
            if (!partial.isNullOrEmpty()) {
                Log.d(TAG, "Partial: $partial")
                val matched = matchPhrase(partial)
                if (matched != null) {
                    // Early match on partial — stop immediately.
                    listenerCycleFinished = true
                    watchdogJob?.cancel()
                    consecutiveNoSpeechErrors = 0
                    completeOnce(matched)
                }
            }
        }

        override fun onEvent(eventType: Int, params: Bundle?) {}
    }
}
