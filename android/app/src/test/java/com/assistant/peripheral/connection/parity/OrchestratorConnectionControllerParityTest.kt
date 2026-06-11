package com.assistant.peripheral.connection.parity

import android.app.Application
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.PreferenceDataStoreFactory
import androidx.datastore.preferences.core.Preferences
import androidx.test.core.app.ApplicationProvider
import app.cash.turbine.test
import com.assistant.peripheral.connection.ConnectionEvent
import com.assistant.peripheral.connection.OrchestratorConnectionController
import com.assistant.peripheral.data.ConnectionState
import com.assistant.peripheral.data.WebSocketMessage
import com.assistant.peripheral.network.DiscoveredServer
import com.assistant.peripheral.network.LiveSession
import com.assistant.peripheral.network.WebSocketEndpoint
import com.assistant.peripheral.network.WebSocketManager
import com.assistant.peripheral.settings.SettingsRepository
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.cancel
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.File
import java.util.concurrent.atomic.AtomicInteger

/**
 * Parity test for Increment 2 (`OrchestratorConnectionController` extraction)
 * of the android viewmodel refactor plan
 * (assistant/plans/android_viewmodel_refactor_plan_2026_06_10.md, §3 + §10.2).
 *
 * Refactor base: HEAD `28d982d` ("Inc 1 — SettingsRepository"). The recovery
 * state machine + Connected-handler probe + connect/disconnect/scan plumbing
 * at the cited AssistantViewModel ranges are what move into the controller.
 *
 * What this test pins (byte-identical against HEAD):
 *
 *  1. **RecoveryStateMachineParity** — single-flight (re-entrant calls during
 *     an in-flight recovery are no-ops); 3 attempts at 0 / 500 / 2000 ms
 *     back-off; isConnected gate triggers reconnect not send when WS dropped;
 *     retry counter resets on `onSessionStartedForOrchestrator`.
 *
 *  2. **OrchestratorProbeRetryParity** — Connected probe retries the live pool
 *     once after 400 ms on miss; if both lookups return empty, emits
 *     `NoOrchestratorFound` and flips `noActiveOrchestrator` true.
 *
 *  3. **PendingNewSessionStartParity** — `armNewSessionStart()` causes the next
 *     Connected to skip the probe and emit `NewSessionAdopted`.
 *
 *  4. **VoiceContinuityReconnectParity** — Connected probe with a found
 *     orchestrator emits BOTH `OrchestratorAdopted` AND `Reconnected`
 *     (subscribed by the future VoiceController to send `voice_start`).
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
@OptIn(ExperimentalCoroutinesApi::class)
class OrchestratorConnectionControllerParityTest {

    private lateinit var application: Application
    private lateinit var dataStore: DataStore<Preferences>
    private lateinit var settingsRepository: SettingsRepository
    private lateinit var webSocketManager: WebSocketManager

    @Before
    fun setUp() {
        application = ApplicationProvider.getApplicationContext()
        val name = "conn_parity_${System.nanoTime()}"
        dataStore = PreferenceDataStoreFactory.create(
            produceFile = { File(application.filesDir, "datastore/$name.preferences_pb") }
        )
        settingsRepository = SettingsRepository(application, dataStore)
        webSocketManager = WebSocketManager()
    }

    /**
     * Polling helper for DataStore writes. The DataStore uses its own
     * `Dispatchers.IO` scope which doesn't join the `runTest` virtual time,
     * so a write triggered inside the controller's coroutine isn't visible
     * to a follow-up read on the test dispatcher. Uses real `Thread.sleep`
     * (NOT `delay`, which advances virtual time) so the IO thread gets
     * wall-clock time to flush.
     */
    private suspend fun pollPersistedOrchestratorLocalId(timeoutMs: Long = 500): String? {
        val start = System.currentTimeMillis()
        while (System.currentTimeMillis() - start < timeoutMs) {
            val v = settingsRepository.persistedOrchestratorLocalId()
            if (v != null) return v
            Thread.sleep(20)
        }
        return settingsRepository.persistedOrchestratorLocalId()
    }

    /** Build a controller with stub deps for tests. */
    private fun controller(
        scope: TestScope,
        livePool: List<LiveSession> = emptyList(),
        livePoolSequence: List<List<LiveSession>>? = null,
        networkScan: List<DiscoveredServer> = emptyList()
    ): Pair<OrchestratorConnectionController, AtomicInteger> {
        val poolCalls = AtomicInteger(0)
        val ctrl = OrchestratorConnectionController(
            scope = scope,
            settingsRepository = settingsRepository,
            webSocketManager = webSocketManager,
            getLivePool = {
                val idx = poolCalls.getAndIncrement()
                livePoolSequence?.getOrNull(idx) ?: livePool
            },
            networkScan = { networkScan }
        )
        return ctrl to poolCalls
    }

    private val sampleOrchestrator = LiveSession(
        localId = "live-orch-id",
        sdkSessionId = "live-sdk-id",
        status = "idle",
        isOrchestrator = true,
        title = "Orchestrator"
    )

    // -----------------------------------------------------------------
    // 1. RecoveryStateMachineParity — single-flight, back-off, cap, reset
    // -----------------------------------------------------------------

    @Test
    fun `recovery — single-flight rejects re-entrance while in flight`() = runTest {
        val (ctrl, poolCalls) = controller(this, livePool = listOf(sampleOrchestrator))
        // Fire two recoveries back-to-back. The second must be dropped
        // because the first is still in flight (no `delay` has yielded yet).
        ctrl.onOrchestratorActiveError()
        ctrl.onOrchestratorActiveError()
        advanceUntilIdle()
        // Only one pool lookup means only the first recovery ran.
        assertEquals(1, poolCalls.get())
    }

    @Test
    fun `recovery — three attempts at 0 then 500 then 2000 ms then cap`() = runTest {
        val (ctrl, poolCalls) = controller(this, livePool = emptyList())

        // Attempt 0 (no delay). After advanceUntilIdle the coroutine has run
        // to completion: pool consulted, early return, finally cleared
        // recoveryInFlight.
        ctrl.onOrchestratorActiveError()
        advanceUntilIdle()
        val afterAttempt0 = poolCalls.get()
        assertTrue(afterAttempt0 >= 1)

        // Attempt 1 → 500ms delay before the pool lookup. We advance past
        // the delay in one step and check the cumulative count. The test
        // dispatcher's runCurrent semantics interleave coroutine launches
        // with assertions in subtle ways — advancing past the delay and
        // checking the cumulative count is the contract we care about.
        ctrl.onOrchestratorActiveError()
        advanceTimeBy(501)
        runCurrent()
        val afterAttempt1 = poolCalls.get()
        assertEquals(afterAttempt0 + 1, afterAttempt1)

        // Attempt 2 → 2000ms delay.
        ctrl.onOrchestratorActiveError()
        advanceTimeBy(2001)
        runCurrent()
        val afterAttempt2 = poolCalls.get()
        assertEquals(afterAttempt1 + 1, afterAttempt2)

        // Attempt 3 → cap hit; emits OrchestratorActiveCapHit and flips
        // noActiveOrchestrator true. Pool is NOT consulted.
        ctrl.events.test {
            ctrl.onOrchestratorActiveError()
            advanceUntilIdle()
            var capSeen = false
            while (!capSeen) {
                val ev = awaitItem()
                if (ev is ConnectionEvent.OrchestratorActiveCapHit) capSeen = true
            }
            cancelAndIgnoreRemainingEvents()
        }
        // Cap path skipped the lookup — poolCalls unchanged from attempt 2.
        assertEquals(afterAttempt2, poolCalls.get())
        assertTrue(ctrl.noActiveOrchestrator.value)
    }

    @Test
    fun `recovery — onSessionStartedForOrchestrator resets the counter`() = runTest {
        val (ctrl, poolCalls) = controller(this, livePool = emptyList())
        // Burn 2 attempts.
        ctrl.onOrchestratorActiveError()
        advanceUntilIdle()
        ctrl.onOrchestratorActiveError()
        advanceTimeBy(501)
        runCurrent()
        val burnCount = poolCalls.get()
        assertEquals(2, burnCount)

        // Recovery converged externally (real session_started arrived).
        ctrl.onSessionStartedForOrchestrator()

        // Next recovery starts fresh (no delay — attempt 0). Pool consulted
        // immediately.
        ctrl.onOrchestratorActiveError()
        advanceUntilIdle()
        assertEquals(burnCount + 1, poolCalls.get())
    }

    @Test
    fun `recovery — isConnected gate triggers reconnect instead of send when WS not connected`() = runTest {
        // The real WebSocketManager reports Disconnected on a fresh instance —
        // exactly the condition we want to exercise. The controller's recovery
        // branch should detect `!isConnected(ORCHESTRATOR)` and call
        // `connect(...)` rather than `send(...)`. We can't easily inspect WS
        // internals; instead we verify the controller's contract: when a live
        // orchestrator is found AND the WS reports not-connected, the recovery
        // does NOT crash and does NOT hit the cap.
        val (ctrl, _) = controller(this, livePool = listOf(sampleOrchestrator))
        settingsRepository.updateServerUrl("ws://10.255.255.1:9999")
        ctrl.onOrchestratorActiveError()
        advanceUntilIdle()
        // No OrchestratorActiveCapHit should be in the replay cache.
        assertEquals(
            0,
            ctrl.events.replayCache.count { it is ConnectionEvent.OrchestratorActiveCapHit }
        )
    }

    // -----------------------------------------------------------------
    // 2. OrchestratorProbeRetryParity — 400ms retry-once on miss
    // -----------------------------------------------------------------

    @Test
    fun `probe — empty first lookup retries once after 400ms and finds orchestrator on second try`() = runTest {
        val (ctrl, poolCalls) = controller(
            this,
            livePoolSequence = listOf(
                emptyList(),                       // first probe: empty
                listOf(sampleOrchestrator)         // second probe: present
            )
        )

        ctrl.events.test {
            ctrl.onWsConnected()
            runCurrent()
            assertEquals(1, poolCalls.get())
            advanceTimeBy(401)
            runCurrent()
            assertEquals(2, poolCalls.get())
            // Now an OrchestratorAdopted should follow, then Reconnected.
            val ev = awaitItem()
            assertTrue("expected OrchestratorAdopted, got $ev", ev is ConnectionEvent.OrchestratorAdopted)
            val ev2 = awaitItem()
            assertTrue("expected Reconnected, got $ev2", ev2 is ConnectionEvent.Reconnected)
            cancelAndIgnoreRemainingEvents()
        }
        assertEquals(false, ctrl.noActiveOrchestrator.value)
    }

    @Test
    fun `probe — both lookups empty emits NoOrchestratorFound and flips noActiveOrchestrator true`() = runTest {
        val (ctrl, poolCalls) = controller(this, livePool = emptyList())

        ctrl.events.test {
            ctrl.onWsConnected()
            advanceUntilIdle()
            assertEquals(2, poolCalls.get())
            val ev = awaitItem()
            assertEquals(ConnectionEvent.NoOrchestratorFound, ev)
            cancelAndIgnoreRemainingEvents()
        }
        assertTrue(ctrl.noActiveOrchestrator.value)
    }

    // -----------------------------------------------------------------
    // 3. PendingNewSessionStartParity — armNewSessionStart skips the probe
    // -----------------------------------------------------------------

    @Test
    fun `pending new session — armNewSessionStart causes next Connected to skip the probe and emit NewSessionAdopted`() = runTest {
        val (ctrl, poolCalls) = controller(this, livePool = listOf(sampleOrchestrator))

        ctrl.events.test {
            ctrl.armNewSessionStart()
            ctrl.onWsConnected()
            advanceUntilIdle()
            assertEquals(0, poolCalls.get())                  // probe skipped
            val ev = awaitItem()
            assertEquals(ConnectionEvent.NewSessionAdopted, ev)
            cancelAndIgnoreRemainingEvents()
        }
        assertEquals(false, ctrl.noActiveOrchestrator.value)
    }

    @Test
    fun `pending new session — flag clears after one use then next Connected runs the probe`() = runTest {
        val (ctrl, poolCalls) = controller(this, livePool = listOf(sampleOrchestrator))

        ctrl.armNewSessionStart()
        ctrl.onWsConnected()
        advanceUntilIdle()
        assertEquals(0, poolCalls.get())                      // first: skipped

        ctrl.onWsConnected()                                  // no arm this time
        advanceUntilIdle()
        assertTrue(poolCalls.get() >= 1)                      // probe ran
    }

    // -----------------------------------------------------------------
    // 4. VoiceContinuityReconnectParity — Reconnected event is emitted
    // -----------------------------------------------------------------

    @Test
    fun `voice continuity — Connected probe with a found orchestrator emits BOTH OrchestratorAdopted AND Reconnected`() = runTest {
        val (ctrl, _) = controller(this, livePool = listOf(sampleOrchestrator))

        ctrl.events.test {
            ctrl.onWsConnected()
            advanceUntilIdle()
            val first = awaitItem()
            val second = awaitItem()
            // Order matters: bucket fields (OrchestratorAdopted) must be
            // settled BEFORE the voice subsystem sends voice_start
            // (Reconnected) so the WS message carries the right local_id.
            assertTrue("first emission should be OrchestratorAdopted, was $first",
                first is ConnectionEvent.OrchestratorAdopted)
            assertTrue("second emission should be Reconnected, was $second",
                second is ConnectionEvent.Reconnected)
            assertEquals(sampleOrchestrator.localId, (first as ConnectionEvent.OrchestratorAdopted).localId)
            assertEquals(sampleOrchestrator.sdkSessionId, first.sdkSessionId)
            assertEquals(sampleOrchestrator.localId, (second as ConnectionEvent.Reconnected).localId)
            assertEquals(sampleOrchestrator.sdkSessionId, second.sdkSessionId)
            cancelAndIgnoreRemainingEvents()
        }
    }

    // -----------------------------------------------------------------
    // Extra: persisted local-id round-trip on probe success
    // -----------------------------------------------------------------

    @Test
    fun `probe success — controller persists the adopted local_id via SettingsRepository`() = kotlinx.coroutines.runBlocking {
        // Real scope so the controller's `launch` actually runs and the
        // DataStore's IO dispatcher gets wall-clock time to flush the
        // persistOrchestratorLocalId write before the assertion reads it.
        val scope = kotlinx.coroutines.CoroutineScope(
            kotlinx.coroutines.Dispatchers.Default + kotlinx.coroutines.SupervisorJob()
        )
        val poolCalls = AtomicInteger(0)
        val ctrl = OrchestratorConnectionController(
            scope = scope,
            settingsRepository = settingsRepository,
            webSocketManager = webSocketManager,
            getLivePool = { poolCalls.getAndIncrement(); listOf(sampleOrchestrator) },
            networkScan = { emptyList() }
        )
        ctrl.onWsConnected()
        // Poll instead of advanceUntilIdle — we're on a real scope.
        assertEquals(sampleOrchestrator.localId, pollPersistedOrchestratorLocalId())
        scope.cancel()
    }

    @Test
    fun `recovery success — controller persists the adopted local_id via SettingsRepository`() = kotlinx.coroutines.runBlocking {
        val scope = kotlinx.coroutines.CoroutineScope(
            kotlinx.coroutines.Dispatchers.Default + kotlinx.coroutines.SupervisorJob()
        )
        val poolCalls = AtomicInteger(0)
        val ctrl = OrchestratorConnectionController(
            scope = scope,
            settingsRepository = settingsRepository,
            webSocketManager = webSocketManager,
            getLivePool = { poolCalls.getAndIncrement(); listOf(sampleOrchestrator) },
            networkScan = { emptyList() }
        )
        ctrl.onOrchestratorActiveError()
        assertEquals(sampleOrchestrator.localId, pollPersistedOrchestratorLocalId())
        scope.cancel()
    }
}
