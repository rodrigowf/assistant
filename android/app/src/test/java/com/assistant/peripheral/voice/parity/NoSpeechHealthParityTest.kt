package com.assistant.peripheral.voice.parity

import com.assistant.peripheral.voice.WakeWordDetector
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Parity test for Increment 8 (replace 2-hour rebuild with NO_SPEECH
 * health check) of the wake-word refactor plan
 * (assistant/plans/wakeword_subsystem_refactor_plan_2026_06_09.md, §3 Inc 8,
 * §10).
 *
 * Refactor base: HEAD `495b5d9` (assistant-context branch
 * voice-wakeword-refactor; post-Inc-7 deferred hand-off).
 * Naming per plan §0.5 (Detour 3, commit `d226027`).
 *
 * Plan §3 Inc 8 spec: replace the 2-hour `watchdogRunnable` /
 * `WATCHDOG_INTERVAL_MS = 2 * 60 * 60 * 1000L` periodic rebuild with a
 * NO_SPEECH-error-driven health check. Add `private var
 * consecutiveNoSpeechErrors: Int = 0` inside WakeWordDetector. Increment
 * on `onError(ERROR_NO_SPEECH)`; reset on `onResults` or any non-
 * NO_SPEECH error. When the count crosses `NO_SPEECH_HEALTH_THRESHOLD =
 * 8` (plan §9 decision 6), broadcast `wake_word_recognizer_unhealthy`.
 * AssistantService listens for that broadcast and re-invokes
 * `startWakeWord(lastTalkWord, lastWakeWord, lastWakeMicGain)` — which
 * goes through Inc 3's dedupe so a flapping recognizer can't melt the
 * service.
 *
 * Plan §9 decision 6 picked 8: roughly 30s+60s+... ≈ 4–5 min of
 * saturated backoff = clearly broken, not flaky.
 *
 * Pure-predicate extraction follows the Option A+B pattern: pull the
 * threshold decision into a companion-object helper
 * `shouldBroadcastRecognizerUnhealthy(consecutiveNoSpeechErrors)` that
 * the production `onError` consults. JUnit testable without
 * Robolectric / Dispatchers.Main / SpeechRecognizer plumbing.
 *
 * Tuned behaviors preserved (verified by reading WakeWordDetector at
 * HEAD `495b5d9`):
 *   - The existing `consecutiveMisses` counter (used for exponential
 *     backoff in `finishRecognition`) is UNCHANGED — it has different
 *     semantics from the new `consecutiveNoSpeechErrors`.
 *   - The CLIENT_ERROR_DELAY_MS=1000L flat-delay carve-out for both
 *     ERROR_CLIENT (7) and ERROR_NO_SPEECH (6) is preserved byte-for-byte.
 *   - `onError` body order: listenerFinished guard → watchdog cancel →
 *     log → delay calc → `finishRecognition`. Unchanged.
 *   - All tuned constants untouched.
 *
 * The 2 h periodic rebuild is REMOVED — its motivation (Samsung
 * Lollipop binder death after long uptime) is now handled by:
 *   - the on-demand health check that fires within ~4 min of trouble,
 *     rather than waiting up to 2 h
 *   - the dedupe from Inc 3 capping the rebuild rate at one per 3 s
 */
class NoSpeechHealthParityTest {

    /**
     * Fresh detector / fresh service: zero NO_SPEECH errors so far.
     * MUST NOT broadcast — the recognizer hasn't earned a rebuild.
     */
    @Test
    fun `unhealthyBroadcastNotFiredOnZeroErrors`() {
        assertFalse(
            "Zero NO_SPEECH errors must NOT broadcast",
            WakeWordDetector.shouldBroadcastRecognizerUnhealthy(0),
        )
    }

    /**
     * 1..7 consecutive NO_SPEECH errors — still in the noise floor.
     * MUST NOT broadcast. Catches off-by-one regressions.
     */
    @Test
    fun `unhealthyBroadcastNotFiredBelowThreshold`() {
        for (n in 1..7) {
            assertFalse(
                "$n NO_SPEECH errors must NOT broadcast",
                WakeWordDetector.shouldBroadcastRecognizerUnhealthy(n),
            )
        }
    }

    /**
     * Crossing exactly threshold (8) — broadcast. Plan §9 decision 6
     * picked 8 = ~4–5 min of saturated backoff.
     */
    @Test
    fun `unhealthyBroadcastFiresExactlyAtThreshold`() {
        assertTrue(
            "Exactly 8 NO_SPEECH errors must broadcast (plan §9 decision 6)",
            WakeWordDetector.shouldBroadcastRecognizerUnhealthy(8),
        )
    }

    /**
     * Counts beyond threshold also broadcast — but the broadcast
     * trigger is idempotent (the AssistantService listener funnels
     * through Inc 3's dedupe). The predicate itself returns true for
     * any value ≥ 8.
     */
    @Test
    fun `unhealthyBroadcastFiresAboveThreshold`() {
        for (n in 8..20) {
            assertTrue(
                "$n NO_SPEECH errors must broadcast",
                WakeWordDetector.shouldBroadcastRecognizerUnhealthy(n),
            )
        }
    }

    /**
     * Threshold value as a constant — the test pins the published value
     * matches plan §9 decision 6 so a future "let's bump this" edit
     * doesn't silently drift.
     */
    @Test
    fun `noSpeechHealthThresholdIsEight`() {
        assertEquals(
            "NO_SPEECH_HEALTH_THRESHOLD must be 8 per plan §9 decision 6",
            8,
            WakeWordDetector.noSpeechHealthThresholdForTest(),
        )
    }
}
