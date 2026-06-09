package com.assistant.peripheral.voice.parity

import com.assistant.peripheral.voice.WakeWordDetector
import com.assistant.peripheral.voice.WakeWordState
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Parity test for Increment 6 (`WakeWordState` sealed-class FSM) of the
 * wake-word refactor plan
 * (assistant/plans/wakeword_subsystem_refactor_plan_2026_06_09.md, §2.2,
 * §2.3, §3 Inc 6, §10).
 *
 * Refactor base: HEAD `e31d4fc` (assistant-context branch
 * voice-wakeword-refactor; post-Inc-5 `wordSubs` removal).
 * Naming per plan §0.5 (Detour 3, commit `d226027`).
 *
 * Plan §2.3 enumerates a transition table. Plan §3 Inc 6 says the public
 * read-API (`isActive`, `isPaused`) must remain byte-compatible so
 * `AssistantService.rearmWakeWord` and `AssistantViewModel` keep working
 * without their own change. Concretely:
 *  - `isActive`  ≡ `state !is Stopped`
 *  - `isPaused`  ≡ `state is Paused`
 *  - `isRecognizing` (private) ≡ `state is Recognizing`
 *
 * Today's three booleans collapse to these derived predicates. This test
 * pins the mapping for every state in the table so a future edit can't
 * silently break `isActive`/`isPaused` semantics that downstream code
 * (`AssistantService.rearmWakeWord` at line 173, the LaunchedEffect at
 * `MainActivity:276`, etc.) reads through.
 *
 * Plan §6 step 3 Inc 6 signal: "no behavior change visible in logs;
 * transition log lines available at Log.v". This test enforces that
 * "no behavior change" property at the type level — every state the FSM
 * can be in produces the exact `(isActive, isPaused, isRecognizing)` tuple
 * the old code would have set the three flags to.
 *
 * The transition-table rows themselves (Stopped→Idle→SilenceMonitor on
 * `start()`, SilenceMonitor→Recognizing on RMS-hit, etc.) are exercised by
 * the live silence-monitor coroutine and the SpeechRecognizer listener;
 * they involve `Dispatchers.Main`, AudioRecord, and the SpeechRecognizer
 * IPC, none of which are plain-JUnit testable. The on-device logcat
 * verification covers those rows. THIS test pins the state→predicates
 * mapping that those rows rely on.
 *
 * Tuned behaviors preserved (verified by reading WakeWordDetector at HEAD
 * `e31d4fc`):
 *   - `start()` → silence-monitor spawn order unchanged.
 *   - `pause()` → cancel monitor, stop AudioRecord, destroy recognizer
 *     order unchanged.
 *   - `resume()` → `consecutiveMisses = 0` reset preserved.
 *   - `stop()` → full teardown order: revertAudioModeIfOurs, unmuteBeep,
 *     cancel monitor, stop AudioRecord, destroy recognizer, cancel
 *     children. Unchanged.
 *   - `startSilenceMonitor` mic-acquisition retry loop preserved (the
 *     state stays Idle during retry; transitions to SilenceMonitor on
 *     successful `startRecording`).
 *   - All tuned constants untouched.
 */
class WakeWordFSMParityTest {

    /**
     * Stopped state — `start()` has not been called or `stop()` was the
     * last transition. Old flags: all three false.
     */
    @Test
    fun `stoppedStateMapsToAllFalse`() {
        val s: WakeWordState = WakeWordState.Stopped
        assertFalse("Stopped → isActive == false", WakeWordDetector.derivedIsActive(s))
        assertFalse("Stopped → isPaused == false", WakeWordDetector.derivedIsPaused(s))
        assertFalse("Stopped → isRecognizing == false", WakeWordDetector.derivedIsRecognizing(s))
    }

    /**
     * Idle state — `start()` has been called but the mic isn't acquired
     * yet (the brief interstitial during `startSilenceMonitor`'s retry
     * loop). Old flags: isActive=true, isPaused=false, isRecognizing=false.
     */
    @Test
    fun `idleStateMapsToActiveOnly`() {
        val s: WakeWordState = WakeWordState.Idle
        assertTrue("Idle → isActive == true", WakeWordDetector.derivedIsActive(s))
        assertFalse("Idle → isPaused == false", WakeWordDetector.derivedIsPaused(s))
        assertFalse("Idle → isRecognizing == false", WakeWordDetector.derivedIsRecognizing(s))
    }

    /**
     * SilenceMonitor state — mic acquired, Stage-1 RMS read loop running.
     * Old flags: isActive=true, isPaused=false, isRecognizing=false (same
     * tuple as Idle externally — the distinction is private to the FSM
     * and visible only via Log.v transitions).
     */
    @Test
    fun `silenceMonitorStateMapsToActiveOnly`() {
        val s: WakeWordState = WakeWordState.SilenceMonitor
        assertTrue("SilenceMonitor → isActive == true", WakeWordDetector.derivedIsActive(s))
        assertFalse("SilenceMonitor → isPaused == false", WakeWordDetector.derivedIsPaused(s))
        assertFalse(
            "SilenceMonitor → isRecognizing == false",
            WakeWordDetector.derivedIsRecognizing(s),
        )
    }

    /**
     * Recognizing state — Stage-2 SpeechRecognizer running. Before
     * `onBeginningOfSpeech`, beganSpeechAtMs is null. Old flags:
     * isActive=true, isPaused=false, isRecognizing=true.
     */
    @Test
    fun `recognizingStateBeforeSpeechMapsToActiveAndRecognizing`() {
        val s: WakeWordState = WakeWordState.Recognizing(
            startedAtMs = 1_000L,
            beganSpeechAtMs = null,
        )
        assertTrue("Recognizing → isActive == true", WakeWordDetector.derivedIsActive(s))
        assertFalse("Recognizing → isPaused == false", WakeWordDetector.derivedIsPaused(s))
        assertTrue(
            "Recognizing → isRecognizing == true",
            WakeWordDetector.derivedIsRecognizing(s),
        )
    }

    /**
     * Recognizing state after `onBeginningOfSpeech` — beganSpeechAtMs is
     * set. Same derived tuple as the pre-onBeginningOfSpeech case (the
     * inner timestamp is FSM-only state for the Inc 4 watchdog).
     */
    @Test
    fun `recognizingStateAfterSpeechBeganMapsSameAsBefore`() {
        val s: WakeWordState = WakeWordState.Recognizing(
            startedAtMs = 1_000L,
            beganSpeechAtMs = 2_500L,
        )
        assertTrue(WakeWordDetector.derivedIsActive(s))
        assertFalse(WakeWordDetector.derivedIsPaused(s))
        assertTrue(WakeWordDetector.derivedIsRecognizing(s))
    }

    /**
     * Paused state — `pause()` was called while active. Old flags:
     * isActive=true, isPaused=true, isRecognizing=false (pause clears
     * isRecognizing explicitly in the old code, lines 207-209).
     */
    @Test
    fun `pausedStateMapsToActiveAndPaused`() {
        val s: WakeWordState = WakeWordState.Paused
        assertTrue("Paused → isActive == true", WakeWordDetector.derivedIsActive(s))
        assertTrue("Paused → isPaused == true", WakeWordDetector.derivedIsPaused(s))
        assertFalse(
            "Paused → isRecognizing == false (pause() destroys recognizer)",
            WakeWordDetector.derivedIsRecognizing(s),
        )
    }

    /**
     * Inc 1 `shouldShortCircuitStart` predicate must still produce the
     * same answer when fed derived-from-state booleans as it did when
     * fed the legacy flags. This pins the cross-Inc-1/Inc-6 contract.
     *
     *  - Stopped → (false, false) → guard does NOT short-circuit (run body).
     *  - Idle / SilenceMonitor → (true, false) → guard SHORT-CIRCUITS.
     *  - Paused → (true, true) → guard does NOT short-circuit (must re-arm).
     *  - Recognizing → (true, false) → guard SHORT-CIRCUITS (same as monitor).
     */
    @Test
    fun `shouldShortCircuitStartAgreesWithLegacyFlagsAcrossEveryState`() {
        val expectations: List<Pair<WakeWordState, Boolean>> = listOf(
            WakeWordState.Stopped to false,
            WakeWordState.Idle to true,
            WakeWordState.SilenceMonitor to true,
            WakeWordState.Recognizing(1L, null) to true,
            WakeWordState.Paused to false,
        )
        for ((state, expected) in expectations) {
            assertEquals(
                "shouldShortCircuitStart disagreed for state=$state",
                expected,
                WakeWordDetector.shouldShortCircuitStart(
                    WakeWordDetector.derivedIsActive(state),
                    WakeWordDetector.derivedIsPaused(state),
                ),
            )
        }
    }
}
