package com.assistant.peripheral.voice.parity

import com.assistant.peripheral.voice.WakeWordDetector
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Parity test for Increment 2 (`finishRecognition` idempotency guard) of the
 * wake-word refactor plan
 * (assistant/plans/wakeword_subsystem_refactor_plan_2026_06_09.md, §3 Inc 2
 * and §10).
 *
 * Refactor base: HEAD 9200d50 (assistant-context branch voice-wakeword-refactor).
 * Source: `WakeWordDetector.kt:481-511` as of 9200d50.
 *
 * Plan §3 Inc 2 spec: early-return if `!isRecognizing && speechRecognizer == null`.
 * The guard protects against the race where a late `onResults` / `onError`
 * fires after the listener path has already finished (e.g. partial-match
 * early-finish triggered first, then the SpeechRecognizer also delivers
 * a final callback). Without the guard, the body re-runs and launches a
 * SECOND silence-monitor restart coroutine, producing two parallel monitors.
 *
 * Same Option A+B pattern as Increment 1:
 * - Production code grows a pure helper `shouldShortCircuitFinishRecognition`
 *   in the companion object so the guard predicate is unit-testable without
 *   `Dispatchers.Main`, `SpeechRecognizer.isRecognitionAvailable`, or
 *   Robolectric plumbing — all of which collide with the detector's eager
 *   `CoroutineScope(Dispatchers.Main)` construction.
 * - The "first call still runs the full body" half of the parity assertion
 *   is verified on the device per plan §6 step 3 Inc 2 expected signal:
 *   "no double `Silence monitor started` within 100 ms."
 *
 * Behavior preserved (verified by reading the full `finishRecognition` body
 * at 9200d50):
 *   - `isRecognizing = false` is still set on every first call.
 *   - `destroyRecognizer()` is still called on every first call.
 *   - The audio-mode revert / `weChangedAudioMode` reset is still applied.
 *   - `unmuteBeep()` is still called.
 *   - The `restartDelay` calculation (CLIENT_ERROR / NO_SPEECH flat delay,
 *     wakeWordDetected POST_WAKEWORD_DELAY_MS, exponential backoff for
 *     misses) is byte-identical.
 *   - The `consecutiveMisses` counter mutation (reset on detected, ++ on
 *     miss) is byte-identical.
 *   - All tuned constants untouched (POST_RECOGNITION_BASE_MS=1000,
 *     POST_RECOGNITION_MAX_MS=30_000, CLIENT_ERROR_DELAY_MS=1000,
 *     POST_WAKEWORD_DELAY_MS=3000, exponential backoff schedule, the
 *     `consecutiveMisses >= 10` warning threshold).
 *   - The deferred `scope.launch { delay(restartDelay); ... startSilenceMonitor() }`
 *     still happens on every first call.
 *
 * Refactor delta authorized by plan §3 Increment 2: +2 lines in
 * `finishRecognition` body, plus the pure helper for testability.
 */
class FinishRecognitionParityTest {

    /**
     * Healthy first call: `isRecognizing=true, speechRecognizer != null`.
     * The guard must NOT short-circuit — `finishRecognition` must run the
     * full body. This is the dominant code path (every real onResults /
     * onError landing).
     */
    @Test
    fun `guardDoesNotShortCircuitHealthyFirstCall`() {
        assertFalse(
            "Healthy first call (isRecognizing=true, recognizer present) must NOT short-circuit",
            WakeWordDetector.shouldShortCircuitFinishRecognition(
                isRecognizing = true,
                hasSpeechRecognizer = true,
            )
        )
    }

    /**
     * The redundant-call case the guard fixes: a late callback arrives
     * AFTER the listener path already cleared `isRecognizing` and
     * destroyed the recognizer. Without the guard, the body would relaunch
     * a second silence-monitor coroutine racing the first.
     */
    @Test
    fun `guardShortCircuitsAfterFinishedRecognition`() {
        assertTrue(
            "Already-finished (isRecognizing=false, recognizer=null) must short-circuit a redundant finishRecognition()",
            WakeWordDetector.shouldShortCircuitFinishRecognition(
                isRecognizing = false,
                hasSpeechRecognizer = false,
            )
        )
    }

    /**
     * Mid-teardown defensive case: `isRecognizing=false` was just flipped
     * but the recognizer is not yet null (e.g., we're inside the brief
     * window before `destroyRecognizer()` finishes). The guard must NOT
     * fire here — the body still needs to call `destroyRecognizer()` to
     * complete the cleanup. AND on the second call within this window,
     * the SAME body must run because the recognizer is still held.
     *
     * This guard predicate is keyed on the AND of both conditions for
     * exactly this reason: do not skip cleanup just because one flag has
     * been flipped early.
     */
    @Test
    fun `guardDoesNotShortCircuitMidTeardown`() {
        assertFalse(
            "Mid-teardown (isRecognizing=false but recognizer still present) must NOT short-circuit — cleanup still needed",
            WakeWordDetector.shouldShortCircuitFinishRecognition(
                isRecognizing = false,
                hasSpeechRecognizer = true,
            )
        )
    }

    /**
     * Inverse edge case: `isRecognizing=true` but `speechRecognizer=null`.
     * This shouldn't happen under correct state, but if it does the guard
     * must NOT fire because the body needs to at least flip
     * `isRecognizing=false` and schedule the silence-monitor restart.
     * Captured as a fourth row of the truth table to nail the AND
     * semantics.
     */
    @Test
    fun `guardDoesNotShortCircuitIsRecognizingButNoRecognizer`() {
        assertFalse(
            "Anomalous (isRecognizing=true, recognizer=null) must NOT short-circuit — body still re-establishes state",
            WakeWordDetector.shouldShortCircuitFinishRecognition(
                isRecognizing = true,
                hasSpeechRecognizer = false,
            )
        )
    }
}
