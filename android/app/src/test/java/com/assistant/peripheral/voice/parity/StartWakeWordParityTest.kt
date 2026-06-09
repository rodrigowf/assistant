package com.assistant.peripheral.voice.parity

import com.assistant.peripheral.service.AssistantService
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Parity test for Increment 3 (AssistantService double-start dedupe) of the
 * wake-word refactor plan
 * (assistant/plans/wakeword_subsystem_refactor_plan_2026_06_09.md, §3 Inc 3
 * and §10).
 *
 * Refactor base: HEAD 76ae6a6 (assistant-context branch
 * voice-wakeword-refactor). Source: `AssistantService.kt:314-319` as of
 * 76ae6a6.
 *
 * Plan §3 Inc 3 spec: introduce dedupe keyed on
 * `(wakeWord, voiceWord, micGain)` with a 3 s window. If the same key
 * arrives within `DEDUPE_WINDOW_MS=3000`, short-circuit; otherwise update
 * the last-key/last-at fields and proceed with the existing
 * `startWakeWord` body.
 *
 * Same Option A+B pattern as Inc 1 and Inc 2: extract the dedupe
 * decision to a pure companion-object helper `shouldDedupeWakeStart`
 * for unit-testability without Robolectric, then call it from the
 * private `startWakeWord` method.
 *
 * Behavior preserved (verified by reading `startWakeWord` at HEAD 76ae6a6)
 *   - First call still tears down the previous detector via
 *     `wakeWordDetector?.stop()`.
 *   - First call still constructs `WakeWordDetector(this, wakeWord,
 *     voiceWord, micGain)` and calls `.start()`.
 *   - First call still logs `Wake word detection started — wake: "$w",
 *     voice: "$v", gain=$g` at `Log.d`.
 *   - All tuned constants untouched.
 *   - Detour 2's single-ingress behavior preserved — the dedupe is
 *     additive defense against the rare-race scenarios that survived
 *     detour 2 (Android START_REDELIVER_INTENT, sticky-restart races).
 *
 * Why 3 s and not the original 500 ms (per
 * `assistant/operational/voice_system_refactor_handoff_2026_06_08.md`)
 * — observed redelivery gaps included 1.3 s on the device. 3 s covers
 * both 20 ms and 1.3 s with margin without masking legitimate user
 * toggles (a human is unlikely to enable, disable, then enable wake-word
 * within 3 s with no intermediate config change).
 *
 * Note about the dedupe key including `micGain`: this was originally
 * spec'd in the plan back when the gain=1.5→1.0 redelivery storm was
 * unrooted. After detour 1 (`d6181b1`) fixed the gain corruption at
 * source, the key works correctly — legitimate gain-slider drags
 * produce a DIFFERENT key value and fall through the dedupe (their
 * own restart happens), while genuine redelivery with the same key
 * trips the dedupe. See log entry for Inc 3.
 */
class StartWakeWordParityTest {

    /**
     * First call ever on a fresh service: `lastStartKey=null`. The dedupe
     * MUST NOT fire — the full `startWakeWord` body must run to construct
     * the initial detector.
     */
    @Test
    fun `dedupeDoesNotFireOnFirstCallEver`() {
        assertFalse(
            "Fresh service (lastStartKey=null) must NOT dedupe",
            AssistantService.shouldDedupeWakeStart(
                key = Triple("my friend", "wake up", 1.5f),
                nowMs = 1_000L,
                lastKey = null,
                lastAtMs = 0L,
            )
        )
    }

    /**
     * Same key within the 3 s window — THIS is the case the dedupe
     * fixes. Must fire (return true) → caller short-circuits.
     */
    @Test
    fun `dedupeFiresOnSameKeyWithinWindow`() {
        assertTrue(
            "Same key 1.5 s after first call must dedupe (within 3 s window)",
            AssistantService.shouldDedupeWakeStart(
                key = Triple("my friend", "wake up", 1.5f),
                nowMs = 1_500L,
                lastKey = Triple("my friend", "wake up", 1.5f),
                lastAtMs = 0L,
            )
        )
    }

    /**
     * Same key BEYOND the window — must NOT dedupe. The detector may
     * have crashed or the user genuinely wants a restart.
     */
    @Test
    fun `dedupeDoesNotFireOnSameKeyOutsideWindow`() {
        assertFalse(
            "Same key 3.5 s after first call must NOT dedupe (window expired)",
            AssistantService.shouldDedupeWakeStart(
                key = Triple("my friend", "wake up", 1.5f),
                nowMs = 3_500L,
                lastKey = Triple("my friend", "wake up", 1.5f),
                lastAtMs = 0L,
            )
        )
    }

    /**
     * Different wake-word within the window — must NOT dedupe. The user
     * legitimately changed the wake phrase; a restart with the new
     * config is required.
     */
    @Test
    fun `dedupeDoesNotFireOnDifferentWakeWord`() {
        assertFalse(
            "Different wakeWord within 3 s must NOT dedupe — user changed config",
            AssistantService.shouldDedupeWakeStart(
                key = Triple("hey assistant", "wake up", 1.5f),
                nowMs = 1_500L,
                lastKey = Triple("my friend", "wake up", 1.5f),
                lastAtMs = 0L,
            )
        )
    }

    /**
     * Different voice-word within the window — must NOT dedupe.
     */
    @Test
    fun `dedupeDoesNotFireOnDifferentVoiceWord`() {
        assertFalse(
            "Different voiceWord within 3 s must NOT dedupe — user changed config",
            AssistantService.shouldDedupeWakeStart(
                key = Triple("my friend", "computer", 1.5f),
                nowMs = 1_500L,
                lastKey = Triple("my friend", "wake up", 1.5f),
                lastAtMs = 0L,
            )
        )
    }

    /**
     * Different mic-gain within the window — must NOT dedupe. The user
     * dragged the gain slider; a restart with the new gain is required.
     *
     * (After detour 1 commit `d6181b1` fixed the gain-corruption bug,
     * this case happens only when the user intentionally changes gain.
     * Pre-detour-1, EVERY UI toggle hit this case with gain 1.5 → 1.0
     * cascade, and the dedupe would have fallen through — which was
     * actually correct behavior given the corrupted second intent.)
     */
    @Test
    fun `dedupeDoesNotFireOnDifferentMicGain`() {
        assertFalse(
            "Different micGain within 3 s must NOT dedupe — user dragged slider",
            AssistantService.shouldDedupeWakeStart(
                key = Triple("my friend", "wake up", 1.2f),
                nowMs = 1_500L,
                lastKey = Triple("my friend", "wake up", 1.5f),
                lastAtMs = 0L,
            )
        )
    }

    /**
     * Boundary case: now - lastAt == DEDUPE_WINDOW_MS exactly (3000 ms).
     * The predicate uses strict `<` (per plan §4 Inc 3), so exactly at
     * the boundary must NOT dedupe.
     */
    @Test
    fun `dedupeDoesNotFireExactlyAtWindowBoundary`() {
        assertFalse(
            "Exact window boundary (now-last == DEDUPE_WINDOW_MS) must NOT dedupe (strict <)",
            AssistantService.shouldDedupeWakeStart(
                key = Triple("my friend", "wake up", 1.5f),
                nowMs = 3_000L,
                lastKey = Triple("my friend", "wake up", 1.5f),
                lastAtMs = 0L,
            )
        )
    }

    /**
     * Boundary case: 1 ms inside the window — must dedupe.
     */
    @Test
    fun `dedupeFiresJustInsideWindow`() {
        assertTrue(
            "Exact window minus 1 ms must dedupe (strict <)",
            AssistantService.shouldDedupeWakeStart(
                key = Triple("my friend", "wake up", 1.5f),
                nowMs = 2_999L,
                lastKey = Triple("my friend", "wake up", 1.5f),
                lastAtMs = 0L,
            )
        )
    }
}
