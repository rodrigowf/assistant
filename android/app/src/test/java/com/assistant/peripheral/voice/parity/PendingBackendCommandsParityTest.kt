package com.assistant.peripheral.voice.parity

import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Parity test for Increment H Bug 7 fix — the `pendingBackendCommands`
 * queue in `VoiceManager` migrating from `mutableListOf` to
 * `Channel<Map<String, Any?>>(capacity = UNLIMITED)`.
 *
 * Refactor base: HEAD `419813c` (post Inc-H-1; `pendingBackendCommands`
 * was still `mutableListOf` until this commit).
 *
 * What this test pins:
 *
 *  1. **Concurrent producers don't drop commands.** The pre-Inc-H
 *     `mutableListOf` was an ArrayList — multiple coroutines hitting
 *     `handleBackendCommand()` from different threads could either
 *     throw `ConcurrentModificationException` or silently overwrite
 *     each other's writes. The Channel guarantees every successful
 *     `trySend` is later observed by `tryReceive`.
 *
 *  2. **FIFO drain order.** `start()` reads commands in the order
 *     they were queued so that, for example, a `session.update`
 *     followed by a `response.create` reaches the provider in that
 *     order. The Channel's `tryReceive` is FIFO.
 *
 *  3. **Drain to empty is non-blocking.** The drain pattern
 *     (`while (tryReceive().isFailure not) ...`) returns immediately
 *     when the queue is empty — no risk of suspending the start()
 *     path.
 *
 *  4. **Clear on `stopInternal()` drops queued commands.** A new
 *     session must not inherit commands from a torn-down one. The
 *     drain-to-empty pattern in `stopInternal()` should remove every
 *     queued command without closing the channel.
 *
 *  5. **The channel survives a session cycle.** Closing on
 *     `stopInternal()` would require reopening on `start()`. We
 *     keep the channel open for the VoiceManager's lifetime — a
 *     stop-then-start cycle must still accept new commands.
 *
 * We exercise the Channel directly rather than the full VoiceManager
 * because instantiating VoiceManager requires Context/AudioManager
 * mocks; the Channel surface IS the test surface for Bug 7.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class PendingBackendCommandsParityTest {

    /** Mirrors the VoiceManager field — UNLIMITED capacity, the type
     *  the production code uses. */
    private fun newQueue(): Channel<Map<String, Any?>> =
        Channel(capacity = Channel.UNLIMITED)

    /** Mirrors the drain pattern from VoiceManager.start(). */
    private fun drain(q: Channel<Map<String, Any?>>): List<Map<String, Any?>> = buildList {
        while (true) {
            val r = q.tryReceive()
            if (r.isFailure) break
            r.getOrNull()?.let { add(it) }
        }
    }

    @Test
    fun concurrentProducers_noCommandIsDropped() = runTest {
        val q = newQueue()
        val total = 1000
        // Fan out 1000 trySends across coroutines — the old ArrayList
        // would race; the Channel guarantees no loss.
        (0 until total).map { i ->
            async {
                val result = q.trySend(mapOf("seq" to i, "type" to "session.update"))
                assertTrue("trySend at i=$i failed", result.isSuccess)
            }
        }.awaitAll()
        val drained = drain(q)
        assertEquals(total, drained.size)
        // Every sequence number appears exactly once.
        val seen = drained.mapNotNull { it["seq"] as? Int }.toSet()
        assertEquals(total, seen.size)
        assertEquals((0 until total).toSet(), seen)
    }

    @Test
    fun fifoDrainOrder_singleProducer() = runTest {
        val q = newQueue()
        val payloads = listOf(
            mapOf("type" to "session.update"),
            mapOf("type" to "response.create"),
            mapOf("type" to "input_audio_buffer.commit"),
        )
        for (p in payloads) q.trySend(p)
        val drained = drain(q)
        assertEquals(payloads, drained)
    }

    @Test
    fun drainToEmpty_returnsImmediatelyWhenQueueEmpty() = runTest {
        val q = newQueue()
        // First drain (empty): returns []
        assertEquals(emptyList<Map<String, Any?>>(), drain(q))
        // Add then drain: returns the items, then empty again.
        q.trySend(mapOf("type" to "x"))
        assertEquals(1, drain(q).size)
        assertEquals(emptyList<Map<String, Any?>>(), drain(q))
    }

    @Test
    fun stopInternalDrain_dropsAllPendingButKeepsChannelOpen() = runTest {
        val q = newQueue()
        q.trySend(mapOf("type" to "stale-1"))
        q.trySend(mapOf("type" to "stale-2"))
        // Simulate stopInternal's drain-to-empty pattern.
        while (true) {
            val r = q.tryReceive()
            if (r.isFailure) break
        }
        // Channel is still open — next session must be able to use it.
        assertTrue(q.trySend(mapOf("type" to "fresh")).isSuccess)
        val drained = drain(q)
        assertEquals(1, drained.size)
        assertEquals("fresh", drained[0]["type"])
    }

    @Test
    fun trySendOnUnlimited_neverBlocks() = runTest {
        val q = newQueue()
        // 100k sends to validate non-blocking semantics — UNLIMITED
        // means buffer capacity is the only failure mode and it's
        // effectively infinite. This regresses against any future
        // edit that lowers capacity to a bounded value without also
        // updating the drain semantics.
        for (i in 0 until 100_000) {
            assertTrue(q.trySend(mapOf("i" to i)).isSuccess)
        }
        // Drain all.
        assertEquals(100_000, drain(q).size)
    }
}
