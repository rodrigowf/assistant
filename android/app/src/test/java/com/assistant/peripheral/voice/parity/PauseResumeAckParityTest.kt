package com.assistant.peripheral.voice.parity

import com.assistant.peripheral.service.AssistantService
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Parity test for Increment 7 (`pauseWakeWord` / `resumeWakeWord` return
 * `CompletableDeferred<Unit>`) of the wake-word refactor plan
 * (assistant/plans/wakeword_subsystem_refactor_plan_2026_06_09.md, §3 Inc 7,
 * §10).
 *
 * Refactor base: HEAD `738f5aa` (assistant-context branch
 * voice-wakeword-refactor; post-Inc-6 FSM).
 * Naming per plan §0.5 (Detour 3, commit `d226027`).
 *
 * Plan §3 Inc 7 turns the fire-and-forget intent shape into an awaitable
 * `CompletableDeferred<Unit>` hand-off. The deferred completes when the
 * service has finished draining the detector's pause/resume path. The
 * ViewModel can then `withTimeoutOrNull(2000L) { ack.await() }` to know
 * the mic is genuinely released before voice starts.
 *
 * The plan §3 Inc 7 risk note specifies a 2s ack timeout (plan §9 decision
 * 5). The deferred contract is internal hand-off plumbing, not a tuned
 * wake-word constant.
 *
 * The token registry is testable in plain JUnit because it's a pure
 * companion-level data structure (no Android Service needed). This test
 * pins the registry's correctness contract:
 *   - Each `nextAckToken()` call returns a unique strictly-increasing Long.
 *   - `stashAck(token, deferred)` round-trips: `takeAck(token)` returns
 *     the same Deferred instance and removes it from the map.
 *   - `takeAck` for an unknown token returns null — safe for late-arriving
 *     callbacks after the service short-circuited.
 *   - Pause/resume don't conflict on the same token (they use the same
 *     registry but the deferreds are independent).
 *
 * The downstream contract — "intent fires, service drains, deferred
 * completes" — is exercised by the on-device verification, not by this
 * unit test (it would need a real Service runtime).
 *
 * Companion-level extraction is consistent with the Option A+B pattern
 * used for Inc 1, 2, 3, 6 — pure data structure + pure functions in the
 * companion so JUnit doesn't need Robolectric.
 *
 * Tuned behaviors preserved (verified by reading AssistantService at HEAD
 * `738f5aa`):
 *   - The existing `pauseWakeWord` / `resumeWakeWord` Intent firing
 *     (startForegroundService on O+, startService on pre-O) is unchanged.
 *   - The `voiceSessionActive` flag-toggle inside `onStartCommand` is
 *     preserved (still set at the entry of pause, cleared at entry of
 *     resume).
 *   - The `EXTRA_PAUSE_WAKE_WORD` / `EXTRA_RESUME_WAKE_WORD` literal keys
 *     are unchanged; `EXTRA_ACK_TOKEN` is purely additive.
 *   - All tuned constants untouched.
 */
class PauseResumeAckParityTest {

    @After
    fun cleanup() {
        AssistantService.clearPendingAcksForTest()
    }

    /**
     * Tokens are strictly increasing — collisions would silently swap
     * unrelated deferreds and break the contract.
     */
    @Test
    fun `ackTokensAreStrictlyIncreasing`() {
        val a = AssistantService.nextAckTokenForTest()
        val b = AssistantService.nextAckTokenForTest()
        val c = AssistantService.nextAckTokenForTest()
        assertTrue("Token $b should be > $a", b > a)
        assertTrue("Token $c should be > $b", c > b)
        assertNotEquals(a, b)
        assertNotEquals(b, c)
    }

    /**
     * `stashAck`/`takeAck` round-trip — the registry returns the exact
     * same `CompletableDeferred` instance that was stashed.
     */
    @Test
    fun `stashedAckRoundTripsThroughTakeAck`() {
        val token = AssistantService.nextAckTokenForTest()
        val deferred = CompletableDeferred<Unit>()
        AssistantService.stashAckForTest(token, deferred)
        val retrieved = AssistantService.takeAckForTest(token)
        assertSame("takeAck should return the exact stashed deferred", deferred, retrieved)
    }

    /**
     * After `takeAck`, the token is gone — second takeAck must return
     * null. Prevents a late callback from completing a stale deferred.
     */
    @Test
    fun `takeAckOnlyReturnsTheDeferredOnce`() {
        val token = AssistantService.nextAckTokenForTest()
        val deferred = CompletableDeferred<Unit>()
        AssistantService.stashAckForTest(token, deferred)
        val first = AssistantService.takeAckForTest(token)
        val second = AssistantService.takeAckForTest(token)
        assertSame(deferred, first)
        assertNull("Second takeAck for same token must be null", second)
    }

    /**
     * `takeAck` for an unknown token returns null. This is the
     * "service short-circuited before stashing" case: the late
     * onStartCommand callback finds nothing to complete and just no-ops.
     */
    @Test
    fun `takeAckForUnknownTokenReturnsNull`() {
        val unseen = Long.MAX_VALUE
        assertNull(AssistantService.takeAckForTest(unseen))
    }

    /**
     * Pause and resume deferreds are independent — completing one
     * doesn't affect the other even if they share the same registry.
     */
    @Test
    fun `pauseAndResumeAcksAreIndependent`() {
        val pauseToken = AssistantService.nextAckTokenForTest()
        val pauseAck = CompletableDeferred<Unit>()
        AssistantService.stashAckForTest(pauseToken, pauseAck)

        val resumeToken = AssistantService.nextAckTokenForTest()
        val resumeAck = CompletableDeferred<Unit>()
        AssistantService.stashAckForTest(resumeToken, resumeAck)

        val retrievedPause = AssistantService.takeAckForTest(pauseToken)
        assertSame(pauseAck, retrievedPause)

        // pauseAck still uncompleted because we only retrieved it; the
        // caller completes it. resumeAck is still in the registry.
        val retrievedResume = AssistantService.takeAckForTest(resumeToken)
        assertSame(resumeAck, retrievedResume)
    }

    /**
     * A completed Deferred allows `.await()` to return immediately —
     * baseline kotlinx.coroutines contract sanity check so the
     * downstream `ack.await()` calls in AssistantViewModel can rely on
     * it without timing out.
     */
    @Test
    fun `completedDeferredAwaitsImmediately`() = runBlocking {
        val ack = CompletableDeferred<Unit>()
        ack.complete(Unit)
        ack.await()
        assertEquals(true, ack.isCompleted)
    }
}
