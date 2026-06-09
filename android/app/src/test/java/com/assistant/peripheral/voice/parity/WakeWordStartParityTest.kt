package com.assistant.peripheral.voice.parity

import com.assistant.peripheral.voice.WakeWordDetector
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Parity test for Increment 1 (`start()` idempotency guard) of the wake-word
 * refactor plan
 * (assistant/plans/wakeword_subsystem_refactor_plan_2026_06_09.md, §10).
 *
 * Refactor base: HEAD 9c25e07 (assistant-context branch voice-wakeword-refactor).
 *
 * Plan §10 says the parity test must assert that a single `start()` invocation
 * on a fresh detector still triggers the `isRecognitionAvailable` check, the
 * `Starting — wake variants:` log, and `startSilenceMonitor()` exactly once,
 * and that the new guard only short-circuits the SECOND call.
 *
 * Increment 1 introduces a pure helper `WakeWordDetector.shouldShortCircuitStart`
 * that the production `start()` consults at its top (after the
 * `isRecognitionAvailable` check). Testing this helper directly captures the
 * "second call no-ops" half of the parity assertion at a layer that does NOT
 * require Robolectric, Mockito static mocking, or `Dispatchers.Main`
 * installation — all three of which collide with the detector's eager
 * `CoroutineScope(Dispatchers.Main)` construction and the
 * `SpeechRecognizer.isRecognitionAvailable(context)` PackageManager lookup
 * that is not wired up under plain JUnit.
 *
 * The "first call still runs the full body" half of the parity assertion is
 * verified on the device per plan §6 step 3, Inc 1 expected signal:
 *   "one `Starting — wake variants:` line per enable-toggle."
 *
 * Behavior preserved (verbatim from HEAD `start()` at WakeWordDetector.kt:158–168):
 *   - `SpeechRecognizer.isRecognitionAvailable(context)` is still consulted on
 *     every call, BEFORE the new guard.
 *   - `Log.d("Starting — wake variants: ...")` still fires on a genuine first
 *     start.
 *   - `startSilenceMonitor()` is still called exactly once per genuine start.
 *   - All tuned constants are untouched (RMS_THRESHOLD=200.0, ACTIVITY_HOLD_MS=30,
 *     POST_WAKEWORD_DELAY_MS=3000, POST_RECOGNITION_BASE_MS=1000,
 *     POST_RECOGNITION_MAX_MS=30_000, CLIENT_ERROR_DELAY_MS=1000, exponential
 *     backoff schedule).
 *   - Mic source choice (`VOICE_COMMUNICATION` post-N / `VOICE_RECOGNITION`
 *     pre-N) is untouched.
 *   - `revertAudioModeIfOurs` single-owner semantics for MODE_IN_COMMUNICATION
 *     are untouched.
 *   - The resume path (`isActive && isPaused` → `resume()`) is NOT affected by
 *     the guard — see `guardDoesNotShortCircuitPausedDetector`.
 *
 * Refactor delta authorized by plan §3 Increment 1: +4 lines, −0 lines in
 * `start()`, plus the pure `shouldShortCircuitStart` helper.
 */
class WakeWordStartParityTest {

    // -------------------------------------------------------------------------
    // shouldShortCircuitStart — pure predicate exercised on every `start()` call
    // -------------------------------------------------------------------------

    /**
     * Fresh detector: `isActive=false, isPaused=false`. The guard must NOT
     * short-circuit — `start()` must run the full body so a first call still
     * triggers `isRecognitionAvailable`, `Starting — wake variants:`, and
     * `startSilenceMonitor()` exactly as it does at HEAD.
     */
    @Test
    fun `guardDoesNotShortCircuitFreshDetector`() {
        assertFalse(
            "Fresh detector (isActive=false, isPaused=false) must NOT short-circuit start()",
            WakeWordDetector.shouldShortCircuitStart(isActive = false, isPaused = false)
        )
    }

    /**
     * Genuine redundant start: detector already running, not paused. THIS is
     * the case the guard fixes — the second `start()` returns early with a
     * `Log.d("start() ignored — already active")` line and does not relaunch
     * the silence monitor.
     */
    @Test
    fun `guardShortCircuitsActiveDetector`() {
        assertTrue(
            "Already-active, not-paused detector must short-circuit a redundant start()",
            WakeWordDetector.shouldShortCircuitStart(isActive = true, isPaused = false)
        )
    }

    /**
     * Paused detector: `isActive=true, isPaused=true`. Calling `start()` on a
     * paused detector at HEAD does NOT short-circuit — it runs the full body
     * (which re-flips `isPaused=false` and re-arms the silence monitor). The
     * guard must preserve that behavior: short-circuiting here would strand
     * the detector in `Paused` forever if a caller (e.g., `rearmWakeWord`)
     * uses `start()` to wake it.
     *
     * This is the subtlest invariant Increment 1 must not break.
     */
    @Test
    fun `guardDoesNotShortCircuitPausedDetector`() {
        assertFalse(
            "Paused detector (isActive=true, isPaused=true) must NOT short-circuit start() — " +
                "the full body re-arms the silence monitor",
            WakeWordDetector.shouldShortCircuitStart(isActive = true, isPaused = true)
        )
    }

    /**
     * Defensive: a detector that has been `stop()`'d carries `isActive=false`,
     * `isPaused=false`. Identical to the fresh case — must NOT short-circuit.
     * Captured separately because `stop()` is a real call site users hit (the
     * service may stop and re-start the detector across configuration changes).
     */
    @Test
    fun `guardDoesNotShortCircuitStoppedDetector`() {
        assertFalse(
            "Stopped detector (isActive=false, isPaused=false) must NOT short-circuit start()",
            WakeWordDetector.shouldShortCircuitStart(isActive = false, isPaused = false)
        )
    }
}
