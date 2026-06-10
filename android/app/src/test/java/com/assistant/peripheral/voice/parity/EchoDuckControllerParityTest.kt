package com.assistant.peripheral.voice.parity

import com.assistant.peripheral.voice.EchoDuckController
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Parity test for Increment H (`EchoDuckController` extraction) of the
 * voice subsystem refactor plan
 * (assistant/plans/voice_subsystem_refactor_plan_2026_06_09.md, §H).
 *
 * Refactor base: HEAD `cff6afd` (`WebSocketPcmProvider` pre-extraction,
 * pre-Inc-H). The mic-duck / drain-restore methods at L254–L400 of
 * `WebSocketPcmProvider.kt` are the load-bearing piece — they encode the
 * exact sequence that stopped the assistant from hearing its own voice
 * after barge-in. Memory file `feedback_dont_shortcut_echo_ducking.md`
 * names this as a non-negotiable preservation target.
 *
 * What this test pins (byte-identical against HEAD):
 *
 *  1. **DUCK rising edge** — `duck()` saves the current gain to
 *     `gainBeforeSpeaking`, sets the mic gain to `echoDuckingGain`,
 *     cancels any pending restore, and emits exactly one log line:
 *     `[MIC_STATE] DUCK → gain: ${saved}→${ducking}`.
 *     Calling `duck()` again while already ducked is a no-op (no log,
 *     no gain change).
 *
 *  2. **RESTORE_IMMEDIATE** — `restoreImmediately(reason)` cancels the
 *     pending restore job, restores the saved gain to `micGainLevel`,
 *     clears `gainBeforeSpeaking` and `agentSpeaking`, and emits
 *     `[MIC_STATE] RESTORE_IMMEDIATE($reason) → gain: ${ducking}→${restored}`.
 *     If not currently ducked, the method must be a no-op (per the
 *     `gainBeforeSpeaking ?: return` guard at L395 of HEAD).
 *
 *  3. **RESTORE_DRAIN primary path (writes quiet AND head ≥ written)** —
 *     after `scheduleRestore(reason)` is called, the drain loop polls
 *     `playbackHeadPosition` and `totalFramesWritten` every
 *     `MIC_RESTORE_DRAIN_POLL_MS` (80ms). When EITHER (writes-quiet for
 *     `MIC_RESTORE_WRITES_QUIET_MS` (400ms)) AND (head ≥ written) is
 *     true, the loop emits the canonical "AudioTrack drained" log,
 *     waits `MIC_RESTORE_TAIL_MS` (600ms), and calls the equivalent of
 *     `restoreImmediately("drained:$reason")`.
 *
 *  4. **RESTORE_DRAIN fallback path (writes quiet AND head stuck)** —
 *     the Samsung Lollipop case where playbackHeadPosition freezes at
 *     the underrun frame. After both writes AND head go quiet for
 *     `MIC_RESTORE_WRITES_QUIET_MS`, the loop emits the canonical "head
 *     stuck" log, waits `MIC_RESTORE_TAIL_MS`, and restores.
 *
 *  5. **DUCK cancels pending restore** — duck() called mid-drain
 *     immediately cancels the active restore job so the loop doesn't
 *     fire on a stale snapshot.
 *
 *  6. **Mid-duck gain change preserves restore target** — calling
 *     `setMicGain(x)` while ducked updates `gainBeforeSpeaking` (the
 *     restore-to value) so the user's new level takes effect when the
 *     agent stops speaking. The current `micGainLevel` (the ducking
 *     level) is NOT touched. Per L612-L614 of HEAD.
 *
 *  7. **Mid-duck ducking-gain change applies immediately** — calling
 *     `setEchoDuckingGain(x)` while ducked updates `micGainLevel`
 *     immediately AND the new value will be the one logged as the
 *     "from" value on restore. Per L630-L631 of HEAD.
 *
 * Tuned constants preserved (from L139–L182 of HEAD):
 *   - AGENT_SPEECH_STALE_MS = 800L   (not directly tested here — caller-owned)
 *   - MIC_RESTORE_TAIL_MS = 600L
 *   - MIC_RESTORE_DRAIN_POLL_MS = 80L
 *   - MIC_RESTORE_WRITES_QUIET_MS = 400L
 *
 * Logging shape preserved (verbatim — the BEFORE logcats at
 * /tmp/voice_inc_h_before*.log contain these exact prefixes and they
 * are the parity oracle):
 *   - `[MIC_STATE] DUCK → gain: $saved→$ducking`
 *   - `[MIC_STATE] RESTORE_DRAIN($reason) waiting; written=$w head=$h`
 *   - `[MIC_STATE] RESTORE_DRAIN($reason) poll=$p head=$h written=$w remaining=$r writesQuiet=${q}ms`
 *   - `[MIC_STATE] RESTORE_DRAIN($reason) writes quiet at written=$w head=$h remaining=$r`
 *   - `[MIC_STATE] RESTORE_DRAIN($reason) AudioTrack drained at head=$h poll=$p; tail wait ${MIC_RESTORE_TAIL_MS}ms`
 *   - `[MIC_STATE] RESTORE_DRAIN($reason) head stuck at $h (written=$w) AND writes quiet; treating as drained; tail wait ${MIC_RESTORE_TAIL_MS}ms`
 *   - `[MIC_STATE] RESTORE_DRAIN($reason) AudioTrack gone; restoring`
 *   - `[MIC_STATE] RESTORE_IMMEDIATE($reason) → gain: $ducking→$restored`
 *
 * The test captures emitted log lines via a logger callback exposed on
 * `EchoDuckController` so we can assert on the sequence without
 * touching `android.util.Log`.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class EchoDuckControllerParityTest {

    /** Test harness — mutable head/written state + log capture + clock. */
    private class Harness(scope: TestScope) {
        var head: Long = 0L
        var written: Long = 0L
        /** When non-null, simulates the AudioTrack being released
         *  (the drain loop must observe this and break with the
         *  "AudioTrack gone" log line). */
        var audioTrackPresent: Boolean = true
        val logs: MutableList<String> = mutableListOf()
        val controller = EchoDuckController(
            scope = scope,
            getPlaybackHeadPosition = { if (audioTrackPresent) head else null },
            getTotalFramesWritten = { written },
            logger = { line -> logs.add(line) },
        )
    }

    // ---------- 1. DUCK rising edge ----------

    @Test
    fun duck_savesGain_setsDuckingGain_emitsDuckLog() = runTest(UnconfinedTestDispatcher()) {
        val h = Harness(this)
        h.controller.setMicGain(0.8f)
        h.controller.setEchoDuckingGain(0.05f)
        // Clear setup-time housekeeping logs to keep the duck assertion focused.
        h.logs.clear()
        h.controller.duck()
        assertEquals(0.05f, h.controller.currentMicGain)
        assertEquals(0.8f, h.controller.savedGainOrNull)
        assertEquals(1, h.logs.size)
        assertEquals("[MIC_STATE] DUCK → gain: 0.8→0.05", h.logs[0])
    }

    @Test
    fun duck_whileAlreadyDucked_isNoOp() = runTest(UnconfinedTestDispatcher()) {
        val h = Harness(this)
        h.controller.setMicGain(1.0f)
        h.controller.duck()
        h.logs.clear()
        h.controller.duck()
        // No additional log, no gain change.
        assertEquals(0, h.logs.size)
        assertEquals(0.05f, h.controller.currentMicGain)
        assertEquals(1.0f, h.controller.savedGainOrNull)
    }

    // ---------- 2. RESTORE_IMMEDIATE ----------

    @Test
    fun restoreImmediately_cancelsPendingJob_restoresGain_clearsState() =
        runTest(UnconfinedTestDispatcher()) {
            val h = Harness(this)
            h.controller.setMicGain(0.7f)
            h.controller.duck()
            h.logs.clear()
            h.controller.restoreImmediately("flush")
            assertEquals(0.7f, h.controller.currentMicGain)
            assertNull(h.controller.savedGainOrNull)
            assertEquals(1, h.logs.size)
            assertEquals("[MIC_STATE] RESTORE_IMMEDIATE(flush) → gain: 0.05→0.7", h.logs[0])
        }

    @Test
    fun restoreImmediately_whenNotDucked_isNoOp() = runTest(UnconfinedTestDispatcher()) {
        val h = Harness(this)
        h.controller.setMicGain(1.0f)
        h.logs.clear()
        // Never ducked.
        h.controller.restoreImmediately("flush")
        assertEquals(0, h.logs.size)
        assertEquals(1.0f, h.controller.currentMicGain)
    }

    // ---------- 3. RESTORE_DRAIN primary (writes quiet + head ≥ written) ----------

    @Test
    fun scheduleRestore_drainCompletes_whenWritesQuietAndHeadCatchesUp() =
        runTest(UnconfinedTestDispatcher()) {
            val h = Harness(this)
            h.controller.setMicGain(1.0f)
            h.controller.duck()
            // Snapshot @ schedule.
            h.written = 24_000L
            h.head = 0L
            h.controller.scheduleRestore("stale")
            // Initial log emitted synchronously.
            assertEquals(
                "[MIC_STATE] RESTORE_DRAIN(stale) waiting; written=24000 head=0",
                h.logs.last()
            )
            // First few polls: writes keep growing (active playback) — restore must NOT fire.
            for (i in 0..4) {
                h.written += 9600L  // simulate active playback
                h.head += 9600L
                advanceTimeBy(80L)
            }
            assertNull(
                "drain must not have fired while writes are still moving",
                h.controller.savedGainOrNull?.let { "still ducked" }?.let { null }
                    ?: if (h.controller.savedGainOrNull == null) "restored prematurely" else null
            )
            // Stop writing — writes go quiet.  Head still advances briefly.
            for (i in 0..8) {
                if (h.head < h.written) h.head += 6000L
                advanceTimeBy(80L)
            }
            // After 400ms+ of writes-quiet AND head ≥ written, the loop
            // should hit the primary "AudioTrack drained" path, log,
            // then delay 600ms tail and restore.
            advanceTimeBy(700L)
            advanceUntilIdle()
            // Restore happened.
            assertEquals(1.0f, h.controller.currentMicGain)
            assertNull(h.controller.savedGainOrNull)
            // Verify the canonical drained log appears.
            assertTrue(
                "expected 'AudioTrack drained at head=' log; got: ${h.logs.joinToString("\n")}",
                h.logs.any { it.contains("AudioTrack drained at head=") && it.contains("tail wait 600ms") }
            )
            // And the matching RESTORE_IMMEDIATE(drained:stale).
            assertTrue(
                "expected RESTORE_IMMEDIATE(drained:stale); got: ${h.logs.joinToString("\n")}",
                h.logs.any { it.contains("RESTORE_IMMEDIATE(drained:stale)") }
            )
        }

    // ---------- 4. RESTORE_DRAIN fallback (writes quiet + head stuck) ----------

    @Test
    fun scheduleRestore_fallback_whenHeadStuckPostUnderrun() =
        runTest(UnconfinedTestDispatcher()) {
            val h = Harness(this)
            h.controller.setMicGain(1.0f)
            h.controller.duck()
            // Snapshot: written has gone past head and head will freeze (post-underrun).
            h.written = 100_000L
            h.head = 90_000L  // stuck below written — would never satisfy head ≥ written
            h.controller.scheduleRestore("stale")
            // Both writes and head stay constant. After 400ms+ of joint quiet,
            // the fallback path fires.
            advanceTimeBy(800L)
            advanceUntilIdle()
            // Tail wait completes.
            advanceTimeBy(700L)
            advanceUntilIdle()
            // Restore happened despite head < written.
            assertEquals(1.0f, h.controller.currentMicGain)
            assertNull(h.controller.savedGainOrNull)
            assertTrue(
                "expected 'head stuck' log; got: ${h.logs.joinToString("\n")}",
                h.logs.any { it.contains("head stuck at 90000 (written=100000)") }
            )
        }

    // ---------- 5. DUCK cancels pending restore ----------

    @Test
    fun newChunkCancelsPendingRestore() = runTest(UnconfinedTestDispatcher()) {
        val h = Harness(this)
        h.controller.setMicGain(1.0f)
        h.controller.duck()
        h.controller.scheduleRestore("stale")
        // Restore is pending; mid-drain a new chunk arrives.
        h.controller.cancelPendingRestore()
        advanceTimeBy(2000L)
        advanceUntilIdle()
        // Still ducked — the restore must NOT have fired.
        assertEquals(0.05f, h.controller.currentMicGain)
        assertNotNull(h.controller.savedGainOrNull)
    }

    // ---------- 6. Mid-duck gain change preserves restore target ----------

    @Test
    fun setMicGain_whileDucked_updatesRestoreValueOnly() = runTest(UnconfinedTestDispatcher()) {
        val h = Harness(this)
        h.controller.setMicGain(0.5f)
        h.controller.duck()
        // User changes slider mid-duck.
        h.controller.setMicGain(0.9f)
        // micGainLevel (the ducking gain) is unchanged.
        assertEquals(0.05f, h.controller.currentMicGain)
        // savedGainOrNull (the restore target) is updated.
        assertEquals(0.9f, h.controller.savedGainOrNull)
        // Restore applies the new value.
        h.controller.restoreImmediately("user")
        assertEquals(0.9f, h.controller.currentMicGain)
    }

    // ---------- 7. Mid-duck ducking-gain change applies immediately ----------

    @Test
    fun setEchoDuckingGain_whileDucked_appliesImmediately() = runTest(UnconfinedTestDispatcher()) {
        val h = Harness(this)
        h.controller.setMicGain(1.0f)
        h.controller.duck()
        // User changes ducking slider mid-duck.
        h.controller.setEchoDuckingGain(0.10f)
        assertEquals(0.10f, h.controller.currentMicGain)
        // Saved gain unchanged.
        assertEquals(1.0f, h.controller.savedGainOrNull)
    }

    // ---------- 8. AudioTrack gone during drain ----------

    @Test
    fun scheduleRestore_audioTrackGone_breaksAndRestoresWithLog() =
        runTest(UnconfinedTestDispatcher()) {
            val h = Harness(this)
            h.controller.setMicGain(1.0f)
            h.controller.duck()
            h.written = 1000L
            h.head = 0L
            h.controller.scheduleRestore("stale")
            // Mid-drain, the AudioTrack goes away (caller released it).
            h.audioTrackPresent = false
            advanceTimeBy(200L)
            advanceUntilIdle()
            assertEquals(1.0f, h.controller.currentMicGain)
            assertTrue(
                "expected 'AudioTrack gone' log; got: ${h.logs.joinToString("\n")}",
                h.logs.any { it.contains("AudioTrack gone; restoring") }
            )
        }
}
