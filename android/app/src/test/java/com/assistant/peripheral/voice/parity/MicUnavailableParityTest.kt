package com.assistant.peripheral.voice.parity

import com.assistant.peripheral.voice.WakeWordDetector
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Parity test for Increment 9 (mic-acquisition failure broadcast) of the
 * wake-word refactor plan
 * (assistant/plans/wakeword_subsystem_refactor_plan_2026_06_09.md, §3 Inc 9,
 * §9 decision 7, §10).
 *
 * Refactor base: HEAD `39b1cec` (assistant-context branch
 * voice-wakeword-refactor; post-Inc-8 NO_SPEECH health check).
 * Naming per plan §0.5 (Detour 3, commit `d226027`).
 *
 * Plan §3 Inc 9 spec: in `startSilenceMonitor`'s mic-acquisition retry
 * loop (preserved per §1 non-goals), count failed attempts in a local
 * `var failures = 0`. After every `MIC_RETRY_WARN_THRESHOLD = 8` failed
 * acquisitions (4 s of churn at 500 ms / attempt, plan §9 decision 7),
 * broadcast `ACTION_MIC_UNAVAILABLE`. `AssistantService` updates the
 * foreground notification text to "Wake word stalled — mic held by
 * another app" while the condition persists; clear the warning on the
 * first successful acquisition (broadcast `ACTION_MIC_AVAILABLE`).
 *
 * Plan §9 decision 7 picked 8: 4 s of failure is "something's wrong";
 * 8 s is too late.
 *
 * Pure-predicate extraction follows the Option A+B pattern used across
 * all earlier increments. The mic-failure counter mutation happens
 * inside the IO retry coroutine (not JUnit testable without Robolectric
 * + a mock AudioRecord). The companion-level predicate
 * `shouldBroadcastMicUnavailable(failures)` is the test surface — and
 * the production retry loop consults it once per failed attempt.
 *
 * Tuned behaviors preserved (verified by reading WakeWordDetector at
 * HEAD `39b1cec`):
 *   - The 500 ms `delay(500L)` retry cadence is unchanged.
 *   - The AudioRecord(wakeWordSource, ...) constructor args are unchanged.
 *   - The STATE_INITIALIZED check is unchanged.
 *   - The recursive startSilenceMonitor() call on `startRecording()`
 *     failure is unchanged.
 *   - The Log.w lines are unchanged (the new broadcast is additive).
 *   - All tuned constants untouched.
 *
 * The broadcast is observability, not behavior — plan §3 Inc 9 risk is
 * low.
 */
class MicUnavailableParityTest {

    /**
     * Zero failures: no broadcast. Catches "first attempt fired"
     * regressions.
     */
    @Test
    fun `unavailableBroadcastNotFiredOnZeroFailures`() {
        assertFalse(
            "Zero mic-acquisition failures must NOT broadcast",
            WakeWordDetector.shouldBroadcastMicUnavailable(0),
        )
    }

    /**
     * 1..7 failures: under the threshold; no broadcast. Catches
     * off-by-one regressions.
     */
    @Test
    fun `unavailableBroadcastNotFiredBelowThreshold`() {
        for (n in 1..7) {
            assertFalse(
                "$n failures must NOT broadcast",
                WakeWordDetector.shouldBroadcastMicUnavailable(n),
            )
        }
    }

    /**
     * Exactly 8 failures: broadcast. Plan §9 decision 7 picked 8 =
     * ~4 s of churn at 500 ms / attempt.
     */
    @Test
    fun `unavailableBroadcastFiresExactlyAtThreshold`() {
        assertTrue(
            "Exactly 8 failures must broadcast (plan §9 decision 7)",
            WakeWordDetector.shouldBroadcastMicUnavailable(8),
        )
    }

    /**
     * Beyond threshold: each subsequent failure also broadcasts.
     * AssistantService's notification update is idempotent (setting
     * the same text twice is a no-op visually).
     */
    @Test
    fun `unavailableBroadcastFiresAboveThreshold`() {
        for (n in 8..20) {
            assertTrue(
                "$n failures must broadcast",
                WakeWordDetector.shouldBroadcastMicUnavailable(n),
            )
        }
    }

    /**
     * Threshold value pinned to plan §9 decision 7.
     */
    @Test
    fun `micRetryWarnThresholdIsEight`() {
        assertEquals(
            "MIC_RETRY_WARN_THRESHOLD must be 8 per plan §9 decision 7",
            8,
            WakeWordDetector.micRetryWarnThresholdForTest(),
        )
    }
}
