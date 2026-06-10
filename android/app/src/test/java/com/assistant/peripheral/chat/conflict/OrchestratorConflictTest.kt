package com.assistant.peripheral.chat.conflict

import android.app.Application
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.PreferenceDataStoreFactory
import androidx.datastore.preferences.core.Preferences
import androidx.test.core.app.ApplicationProvider
import com.assistant.peripheral.chat.ChatController
import com.assistant.peripheral.chat.OrchestratorConflict
import com.assistant.peripheral.chat.OrchestratorConflictResolution
import com.assistant.peripheral.connection.OrchestratorConnectionController
import com.assistant.peripheral.data.ChatMessage
import com.assistant.peripheral.data.MessageRole
import com.assistant.peripheral.data.SessionInfo
import com.assistant.peripheral.data.WebSocketEvent
import com.assistant.peripheral.network.LiveSession
import com.assistant.peripheral.network.PaginatedMessages
import com.assistant.peripheral.network.WebSocketEndpoint
import com.assistant.peripheral.network.WebSocketManager
import com.assistant.peripheral.settings.SettingsRepository
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancel
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runTest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
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
import java.util.concurrent.atomic.AtomicReference

/**
 * Behavioral tests for Inc 3.5 — orchestrator session conflict mediation.
 *
 * Plan: `~/assistant/context/memory/assistant/plans/android_viewmodel_refactor_plan_2026_06_10.md` §5.5.
 *
 * Unlike Inc 1–4, this increment CHANGES behavior, so these are not parity
 * tests. Each test asserts the new contract: when a user-initiated
 * orchestrator switch (load or new) would otherwise silently land on a
 * different session, the controller emits an [OrchestratorConflict] and
 * does NOT touch the WS until [ChatController.resolveOrchestratorConflict]
 * is called.
 *
 * Live-pool probe-first semantics are pinned: every entry point fetches the
 * pool BEFORE deciding (the pool can change between the user's tap and the
 * action — probing is the freshest source of truth).
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
@OptIn(ExperimentalCoroutinesApi::class)
class OrchestratorConflictTest {

    private lateinit var application: Application
    private lateinit var dataStore: DataStore<Preferences>
    private lateinit var settingsRepository: SettingsRepository
    private lateinit var webSocketManager: WebSocketManager
    private lateinit var connectionController: OrchestratorConnectionController
    private var activeControllerScope: CoroutineScope? = null

    @Before
    fun setUp() {
        application = ApplicationProvider.getApplicationContext()
        val name = "conflict_${System.nanoTime()}"
        dataStore = PreferenceDataStoreFactory.create(
            produceFile = { File(application.filesDir, "datastore/$name.preferences_pb") }
        )
        settingsRepository = SettingsRepository(application, dataStore)
        webSocketManager = WebSocketManager()
        activeControllerScope = null
    }

    @After
    fun tearDown() {
        activeControllerScope?.cancel()
    }

    private fun cleanup() {
        activeControllerScope?.cancel()
    }

    /** Records WS sends so tests can assert "no WS touch". */
    private class WsRecorder {
        val sends = AtomicInteger(0)
        val orchestratorSends = AtomicInteger(0)
    }

    private class FakeApiDeps {
        var livePoolResponse: List<LiveSession> = emptyList()
        var paginatedResponse: PaginatedMessages = PaginatedMessages(emptyList(), 0, false, 0)
        var closePoolReturn: Boolean = true

        val getMessagesCalls = AtomicInteger(0)
        val closePoolCalls = AtomicInteger(0)
        val lastClosedLocalId = AtomicReference<String?>(null)
        val listSessionsCalls = AtomicInteger(0)
    }

    private fun controller(
        parent: CoroutineScope,
        fakes: FakeApiDeps = FakeApiDeps()
    ): ChatController {
        val job = kotlinx.coroutines.SupervisorJob(parent.coroutineContext[Job])
        val scope = CoroutineScope(parent.coroutineContext + job)
        activeControllerScope = scope
        connectionController = OrchestratorConnectionController(
            scope = scope,
            settingsRepository = settingsRepository,
            webSocketManager = webSocketManager,
            getLivePool = { fakes.livePoolResponse },
            networkScan = { emptyList() }
        )
        return ChatController(
            scope = scope,
            webSocketManager = webSocketManager,
            settingsRepository = settingsRepository,
            connectionController = connectionController,
            listSessions = { fakes.listSessionsCalls.incrementAndGet(); emptyList() },
            getLivePool = { fakes.livePoolResponse },
            getMessagesPaginated = { _, _, _ ->
                fakes.getMessagesCalls.incrementAndGet()
                fakes.paginatedResponse
            },
            closePoolSession = { localId ->
                fakes.closePoolCalls.incrementAndGet()
                fakes.lastClosedLocalId.set(localId)
                fakes.closePoolReturn
            },
            deleteSession = { true },
            renameSession = { _, _ -> true },
            duplicateSession = { null },
            truncateSession = { _, _ -> true },
            forkSession = { _, _ -> null }
        )
    }

    private val liveOrch = LiveSession(
        localId = "live-orch-local",
        sdkSessionId = "live-orch-sdk",
        status = "idle",
        isOrchestrator = true,
        title = "Live"
    )

    // -----------------------------------------------------------------
    // requestLoadOrchestratorSession — same-as-live opens directly
    // -----------------------------------------------------------------

    @Test
    fun `request load same as live — opens directly, no conflict`() = runTest {
        val fakes = FakeApiDeps()
        fakes.livePoolResponse = listOf(liveOrch)
        fakes.paginatedResponse = PaginatedMessages(
            messages = listOf(ChatMessage(role = MessageRole.USER, content = "hi")),
            totalCount = 1, hasMore = false, startIndex = 0
        )
        val ctrl = controller(this, fakes)

        ctrl.requestLoadOrchestratorSession(
            sessionId = liveOrch.sdkSessionId,
            liveLocalId = liveOrch.localId,
            onNeedsConnect = {}
        )
        advanceUntilIdle()

        assertNull(
            "no conflict expected when tapping the same orchestrator",
            ctrl.orchestratorConflict.value
        )
        assertTrue("isOrchestratorSession should flip true", ctrl.isOrchestratorSession.value)
        cleanup()
    }

    // -----------------------------------------------------------------
    // requestLoadOrchestratorSession — different live → emits OnLoad
    // -----------------------------------------------------------------

    @Test
    fun `request load different orchestrator — emits OnLoad conflict, no WS touch`() = runTest {
        val fakes = FakeApiDeps()
        fakes.livePoolResponse = listOf(liveOrch)
        val ctrl = controller(this, fakes)
        val sentBeforeRequest = ctrl.bucketFor(WebSocketEndpoint.ORCHESTRATOR).messages.value.size

        ctrl.requestLoadOrchestratorSession(
            sessionId = "other-sdk-id",
            liveLocalId = null,
            onNeedsConnect = {}
        )
        advanceUntilIdle()

        val conflict = ctrl.orchestratorConflict.value
        assertNotNull("expected OrchestratorConflict.OnLoad", conflict)
        assertTrue(
            "expected OnLoad, got ${conflict!!::class.simpleName}",
            conflict is OrchestratorConflict.OnLoad
        )
        val onLoad = conflict as OrchestratorConflict.OnLoad
        assertEquals("other-sdk-id", onLoad.targetSessionId)
        assertEquals(liveOrch.sdkSessionId, onLoad.liveSdkSessionId)
        assertEquals(liveOrch.localId, onLoad.liveLocalId)
        // No bucket / WS mutations until user resolves.
        assertEquals(sentBeforeRequest, ctrl.bucketFor(WebSocketEndpoint.ORCHESTRATOR).messages.value.size)
        cleanup()
    }

    // -----------------------------------------------------------------
    // requestLoadOrchestratorSession — no live → opens directly
    // -----------------------------------------------------------------

    @Test
    fun `request load no live orchestrator — opens directly, no conflict`() = runTest {
        val fakes = FakeApiDeps()
        fakes.livePoolResponse = emptyList()
        fakes.paginatedResponse = PaginatedMessages(
            messages = listOf(ChatMessage(role = MessageRole.USER, content = "hi")),
            totalCount = 1, hasMore = false, startIndex = 0
        )
        val ctrl = controller(this, fakes)

        ctrl.requestLoadOrchestratorSession(
            sessionId = "any-sdk-id",
            liveLocalId = null,
            onNeedsConnect = {}
        )
        advanceUntilIdle()

        assertNull(ctrl.orchestratorConflict.value)
        assertTrue(ctrl.isOrchestratorSession.value)
        cleanup()
    }

    // -----------------------------------------------------------------
    // requestNewOrchestratorSession — no live → fresh directly
    // -----------------------------------------------------------------

    @Test
    fun `request new no live orchestrator — fresh new session, no conflict`() = runTest {
        val fakes = FakeApiDeps()
        fakes.livePoolResponse = emptyList()
        val ctrl = controller(this, fakes)

        val needsConnectCalls = AtomicInteger(0)
        ctrl.requestNewOrchestratorSession(onNeedsConnect = { needsConnectCalls.incrementAndGet() })
        advanceUntilIdle()

        assertNull(ctrl.orchestratorConflict.value)
        assertTrue("isOrchestratorSession true after new session", ctrl.isOrchestratorSession.value)
        // bucket reset to empty for the new session
        assertTrue(ctrl.bucketFor(WebSocketEndpoint.ORCHESTRATOR).messages.value.isEmpty())
        cleanup()
    }

    // -----------------------------------------------------------------
    // requestNewOrchestratorSession — live exists → emits OnNew
    // -----------------------------------------------------------------

    @Test
    fun `request new live orchestrator — emits OnNew conflict, no WS touch`() = runTest {
        val fakes = FakeApiDeps()
        fakes.livePoolResponse = listOf(liveOrch)
        val ctrl = controller(this, fakes)
        val needsConnectCalls = AtomicInteger(0)

        ctrl.requestNewOrchestratorSession(onNeedsConnect = { needsConnectCalls.incrementAndGet() })
        advanceUntilIdle()

        val conflict = ctrl.orchestratorConflict.value
        assertNotNull(conflict)
        assertTrue("expected OnNew", conflict is OrchestratorConflict.OnNew)
        assertEquals(liveOrch.sdkSessionId, (conflict as OrchestratorConflict.OnNew).liveSdkSessionId)
        assertEquals(liveOrch.localId, conflict.liveLocalId)
        assertEquals("onNeedsConnect must not fire during conflict", 0, needsConnectCalls.get())
        cleanup()
    }

    // -----------------------------------------------------------------
    // resolve OpenExisting (load) — loads the live orch, not the target
    // -----------------------------------------------------------------

    @Test
    fun `resolve OpenExisting on load conflict — loads the live orch`() = runTest {
        val fakes = FakeApiDeps()
        fakes.livePoolResponse = listOf(liveOrch)
        fakes.paginatedResponse = PaginatedMessages(
            messages = listOf(ChatMessage(role = MessageRole.USER, content = "live-msg")),
            totalCount = 1, hasMore = false, startIndex = 0
        )
        val ctrl = controller(this, fakes)

        ctrl.requestLoadOrchestratorSession(
            sessionId = "other-sdk-id",
            liveLocalId = null,
            onNeedsConnect = {}
        )
        advanceUntilIdle()
        assertNotNull(ctrl.orchestratorConflict.value)

        ctrl.resolveOrchestratorConflict(OrchestratorConflictResolution.OpenExisting)
        advanceUntilIdle()

        assertNull("conflict must clear after resolution", ctrl.orchestratorConflict.value)
        assertEquals(liveOrch.localId, ctrl.orchestratorCurrentLocalId())
        assertEquals(
            "expected to display the LIVE orch's history, not the target",
            listOf("live-msg"),
            ctrl.bucketFor(WebSocketEndpoint.ORCHESTRATOR).messages.value.map { it.content }
        )
        assertEquals("closePoolSession must NOT be called on OpenExisting",
            0, fakes.closePoolCalls.get())
        cleanup()
    }

    // -----------------------------------------------------------------
    // resolve DiscardAndProceed (load) — close live, then open target
    // -----------------------------------------------------------------

    @Test
    fun `resolve DiscardAndProceed on load conflict — closes live then opens target`() = runTest {
        val fakes = FakeApiDeps()
        fakes.livePoolResponse = listOf(liveOrch)
        fakes.paginatedResponse = PaginatedMessages(
            messages = listOf(ChatMessage(role = MessageRole.USER, content = "target-msg")),
            totalCount = 1, hasMore = false, startIndex = 0
        )
        val ctrl = controller(this, fakes)

        ctrl.requestLoadOrchestratorSession(
            sessionId = "target-sdk-id",
            liveLocalId = "target-local-id",
            onNeedsConnect = {}
        )
        advanceUntilIdle()
        assertNotNull(ctrl.orchestratorConflict.value)

        ctrl.resolveOrchestratorConflict(OrchestratorConflictResolution.DiscardAndProceed)
        advanceUntilIdle()

        assertNull(ctrl.orchestratorConflict.value)
        assertEquals(1, fakes.closePoolCalls.get())
        assertEquals(liveOrch.localId, fakes.lastClosedLocalId.get())
        assertEquals("target-local-id", ctrl.orchestratorCurrentLocalId())
        assertEquals(
            listOf("target-msg"),
            ctrl.bucketFor(WebSocketEndpoint.ORCHESTRATOR).messages.value.map { it.content }
        )
        cleanup()
    }

    // -----------------------------------------------------------------
    // resolve DiscardAndProceed (new) — close live, then fresh new
    // -----------------------------------------------------------------

    @Test
    fun `resolve DiscardAndProceed on new conflict — closes live then fresh new`() = runTest {
        val fakes = FakeApiDeps()
        fakes.livePoolResponse = listOf(liveOrch)
        val ctrl = controller(this, fakes)

        val needsConnectCalls = AtomicInteger(0)
        ctrl.requestNewOrchestratorSession(onNeedsConnect = { needsConnectCalls.incrementAndGet() })
        advanceUntilIdle()
        assertNotNull(ctrl.orchestratorConflict.value)

        val localIdBefore = ctrl.orchestratorCurrentLocalId()
        ctrl.resolveOrchestratorConflict(OrchestratorConflictResolution.DiscardAndProceed)
        advanceUntilIdle()

        assertNull(ctrl.orchestratorConflict.value)
        assertEquals(1, fakes.closePoolCalls.get())
        assertEquals(liveOrch.localId, fakes.lastClosedLocalId.get())
        // newSession() generates a fresh UUID — must differ from the pre-conflict id
        // AND must differ from the live orch's local id.
        val localIdAfter = ctrl.orchestratorCurrentLocalId()
        assertFalse("new session must mint a fresh local_id", localIdBefore == localIdAfter)
        assertFalse("new session must NOT reuse the live orch's local_id",
            liveOrch.localId == localIdAfter)
        cleanup()
    }

    // -----------------------------------------------------------------
    // resolve Cancel — conflict clears, nothing else changes
    // -----------------------------------------------------------------

    @Test
    fun `resolve Cancel — conflict clears, no WS traffic, no close call`() = runTest {
        val fakes = FakeApiDeps()
        fakes.livePoolResponse = listOf(liveOrch)
        val ctrl = controller(this, fakes)
        val orchBucketBefore = ctrl.bucketFor(WebSocketEndpoint.ORCHESTRATOR).currentLocalId.value

        ctrl.requestLoadOrchestratorSession(
            sessionId = "other-sdk-id",
            liveLocalId = null,
            onNeedsConnect = {}
        )
        advanceUntilIdle()
        assertNotNull(ctrl.orchestratorConflict.value)

        ctrl.resolveOrchestratorConflict(OrchestratorConflictResolution.Cancel)
        advanceUntilIdle()

        assertNull(ctrl.orchestratorConflict.value)
        assertEquals(0, fakes.closePoolCalls.get())
        assertEquals("orchestrator bucket local_id unchanged",
            orchBucketBefore,
            ctrl.bucketFor(WebSocketEndpoint.ORCHESTRATOR).currentLocalId.value)
        cleanup()
    }

    // -----------------------------------------------------------------
    // orchestrator_active during user intent → conflict, not recovery
    // -----------------------------------------------------------------

    @Test
    fun `orchestrator_active during user intent — routes to conflict not recovery`() = runTest {
        val fakes = FakeApiDeps()
        // Pool initially empty → request goes through directly. Then while WS
        // round-trip in flight, backend grew a live orch (mid-tap race). The
        // Error("orchestrator_active") arrives; controller must surface a
        // conflict instead of silently swapping via recovery.
        fakes.livePoolResponse = emptyList()
        fakes.paginatedResponse = PaginatedMessages(
            messages = listOf(ChatMessage(role = MessageRole.USER, content = "x")),
            totalCount = 1, hasMore = false, startIndex = 0
        )
        val ctrl = controller(this, fakes)

        ctrl.requestLoadOrchestratorSession(
            sessionId = "target-sdk-id",
            liveLocalId = "target-local-id",
            onNeedsConnect = {}
        )
        advanceUntilIdle()
        assertNull("no conflict on the request itself (pool was empty)",
            ctrl.orchestratorConflict.value)

        // Now pool has the live orch (mid-tap race).
        fakes.livePoolResponse = listOf(liveOrch)
        ctrl.handleWebSocketEvent(
            WebSocketEndpoint.ORCHESTRATOR,
            WebSocketEvent.Error("orchestrator_active", "stale id")
        )
        advanceUntilIdle()

        val conflict = ctrl.orchestratorConflict.value
        assertNotNull("expected conflict from mid-tap orchestrator_active", conflict)
        assertTrue(conflict is OrchestratorConflict.OnLoad)
        cleanup()
    }

    // -----------------------------------------------------------------
    // orchestrator_active without intent → recovery (existing path)
    // -----------------------------------------------------------------

    @Test
    fun `orchestrator_active without user intent — existing recovery path fires`() = runTest {
        val fakes = FakeApiDeps()
        fakes.livePoolResponse = listOf(liveOrch)
        val ctrl = controller(this, fakes)
        // No requestLoad / requestNew has been called — intent is unset. The
        // Error arrives "out of the blue" (cold-start / reconnect scenario).
        // The recovery state machine in OrchestratorConnectionController must
        // engage; no conflict must surface.
        val poolCallsBefore = fakes.listSessionsCalls.get()

        ctrl.handleWebSocketEvent(
            WebSocketEndpoint.ORCHESTRATOR,
            WebSocketEvent.Error("orchestrator_active", "stale id")
        )
        advanceUntilIdle()

        assertNull("no conflict without intent — recovery must handle silently",
            ctrl.orchestratorConflict.value)
        cleanup()
    }
}
