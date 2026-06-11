package com.assistant.peripheral.chat.parity

import android.app.Application
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.PreferenceDataStoreFactory
import androidx.datastore.preferences.core.Preferences
import androidx.test.core.app.ApplicationProvider
import com.assistant.peripheral.chat.ChatController
import com.assistant.peripheral.connection.ConnectionEvent
import com.assistant.peripheral.connection.OrchestratorConnectionController
import com.assistant.peripheral.data.ChatMessage
import com.assistant.peripheral.data.MessageBlock
import com.assistant.peripheral.data.MessageRole
import com.assistant.peripheral.data.SessionInfo
import com.assistant.peripheral.data.WebSocketEvent
import com.assistant.peripheral.data.WebSocketMessage
import com.assistant.peripheral.network.LiveSession
import com.assistant.peripheral.network.PaginatedMessages
import com.assistant.peripheral.network.WebSocketEndpoint
import com.assistant.peripheral.network.WebSocketManager
import com.assistant.peripheral.settings.SettingsRepository
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancel
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.After
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
 * Parity tests for Increment 3 (`ChatController` extraction) of the android
 * viewmodel refactor plan
 * (assistant/plans/android_viewmodel_refactor_plan_2026_06_10.md, §4 + §10.3).
 *
 * Refactor base: HEAD `ca3a5d6` ("Inc 2 — OrchestratorConnectionController").
 * What moves into ChatController: the `ChatStateBucket` + `buckets` map,
 * `_sessions`/`_liveSessionIds`/`_sdkToLocalId`/`_isOrchestratorSession`,
 * `mirrorActive` + derived flows, `sessionCache`, `mutateStreamingBlocks`,
 * the WS event router, `pendingAgentResume`/`pendingNewSessionStart`,
 * AGENT-endpoint connect, all session ops.
 *
 * What this test pins (byte-identical against HEAD AssistantViewModel.kt
 * ranges in §10.3):
 *
 *  1. **StreamingBlockOrderingParity** — `mutateStreamingBlocks` extends the
 *     trailing streaming text block on consecutive `text_delta`; slots new
 *     tool_use blocks after the current text (finalizing the trailing text);
 *     subsequent text_delta starts a new streaming text block after the tool.
 *     Mirrors the web frontend's reducer in `useChatInstance.ts` (the source
 *     of ordering truth — never rebuild blocks from per-type buffers).
 *
 *  2. **EndpointIsolationParity** — given a sequence of WS events tagged with
 *     mixed endpoints, each event writes only into its endpoint's bucket;
 *     the other bucket's flows do not emit.
 *
 *  3. **SessionCacheRestoreParity** — `loadSession` for a cached session
 *     restores from cache without an HTTP fetch; pagination state restores.
 *
 *  4. **SessionStartedSessionIdParity** — on reconnect (pendingResumeSessionId
 *     is set), `jsonlSessionId` becomes the pending value, NOT
 *     `event.sessionId` (which is the local_id echoed back).
 *
 *  5. **PendingAgentResumeParity** — `loadSession(AGENT)` when the AGENT WS
 *     is disconnected queues the Start via `pendingAgentResume`; the
 *     `WebSocketEvent.Connected` handler picks it up and fires Start.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
@OptIn(ExperimentalCoroutinesApi::class)
class ChatControllerParityTest {

    private lateinit var application: Application
    private lateinit var dataStore: DataStore<Preferences>
    private lateinit var settingsRepository: SettingsRepository
    private lateinit var webSocketManager: WebSocketManager
    private lateinit var connectionController: OrchestratorConnectionController
    private var activeController: ChatController? = null
    private var activeControllerJob: Job? = null
    private var activeControllerScope: CoroutineScope? = null

    @Before
    fun setUp() {
        application = ApplicationProvider.getApplicationContext()
        val name = "chat_parity_${System.nanoTime()}"
        dataStore = PreferenceDataStoreFactory.create(
            produceFile = { File(application.filesDir, "datastore/$name.preferences_pb") }
        )
        settingsRepository = SettingsRepository(application, dataStore)
        webSocketManager = WebSocketManager()
        activeController = null
        activeControllerJob = null
        activeControllerScope = null
    }

    @After
    fun tearDown() {
        // Last-resort cancel — tests must also cancel inside `runTest` (via
        // [cleanup]) because `runTest` checks for uncompleted children BEFORE
        // `@After` runs.
        activeControllerScope?.cancel()
    }

    /**
     * Cancel the child scope that owns the ChatController's long-running
     * collectors (`events.collect` + 5 `mirrorActive` `stateIn` flows). Must
     * be called at the end of every test body — `runTest` raises
     * `UncompletedCoroutinesError` if any child is still running when the
     * body returns, and `@After` runs too late.
     */
    private fun cleanup() {
        activeControllerScope?.cancel()
    }

    /**
     * Fake API deps that record every call so tests can assert on them. The
     * defaults return empty/false; tests override fields they care about.
     */
    private class FakeApiDeps {
        var listSessionsResponse: List<SessionInfo> = emptyList()
        var livePoolResponse: List<LiveSession> = emptyList()
        var paginatedResponse: PaginatedMessages = PaginatedMessages(emptyList(), 0, false, 0)
        var paginatedByBeforeIndex: MutableMap<Int, PaginatedMessages> = mutableMapOf()
        var closePoolReturn: Boolean = true
        var deleteReturn: Boolean = true
        var renameReturn: Boolean = true
        var duplicateReturn: String? = "new-sdk-id"
        var truncateReturn: Boolean = true
        var forkReturn: String? = "fork-sdk-id"

        val getMessagesCalls = AtomicInteger(0)
        val lastGetMessagesArgs = AtomicReference<Triple<String, Int, Int?>?>(null)
    }

    /**
     * Build a controller with the given fakes. The controller's scope is a
     * child scope we own, so [cleanup] (and tearDown) can cancel ALL its
     * long-running collectors (`events.collect` + 5 `mirrorActive`
     * `stateIn` Eager subscriptions) atomically.
     */
    private fun controller(
        parent: CoroutineScope,
        fakes: FakeApiDeps = FakeApiDeps()
    ): ChatController {
        val job = kotlinx.coroutines.SupervisorJob(parent.coroutineContext[Job])
        val scope = CoroutineScope(parent.coroutineContext + job)
        activeControllerJob = job
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
            listSessions = { fakes.listSessionsResponse },
            getLivePool = { fakes.livePoolResponse },
            getMessagesPaginated = { sid, limit, beforeIdx ->
                fakes.getMessagesCalls.incrementAndGet()
                fakes.lastGetMessagesArgs.set(Triple(sid, limit, beforeIdx))
                if (beforeIdx != null) {
                    fakes.paginatedByBeforeIndex[beforeIdx] ?: fakes.paginatedResponse
                } else fakes.paginatedResponse
            },
            closePoolSession = { fakes.closePoolReturn },
            deleteSession = { fakes.deleteReturn },
            renameSession = { _, _ -> fakes.renameReturn },
            duplicateSession = { fakes.duplicateReturn },
            truncateSession = { _, _ -> fakes.truncateReturn },
            forkSession = { _, _ -> fakes.forkReturn }
        ).also { activeController = it }
    }

    // =================================================================
    // 1. StreamingBlockOrderingParity
    // =================================================================

    @Test
    fun `streaming order — consecutive text_delta extends trailing streaming text block`() = runTest {
        val ctrl = controller(this)
        ctrl.handleWebSocketEvent(
            WebSocketEndpoint.ORCHESTRATOR,
            WebSocketEvent.MessageStart("msg-1")
        )
        ctrl.handleWebSocketEvent(
            WebSocketEndpoint.ORCHESTRATOR,
            WebSocketEvent.TextDelta("Hello")
        )
        ctrl.handleWebSocketEvent(
            WebSocketEndpoint.ORCHESTRATOR,
            WebSocketEvent.TextDelta(" world")
        )
        advanceUntilIdle()

        val msgs = ctrl.bucketFor(WebSocketEndpoint.ORCHESTRATOR).messages.value
        assertEquals(1, msgs.size)
        val blocks = msgs[0].blocks
        assertEquals("expected ONE text block extended in place, got $blocks", 1, blocks.size)
        val text = blocks[0] as MessageBlock.Text
        assertEquals("Hello world", text.text)
        assertTrue("text block should still be streaming", text.isStreaming)
        cleanup()
    }

    @Test
    fun `streaming order — tool_use slots after text, then next text_delta starts a new streaming block`() = runTest {
        val ctrl = controller(this)
        ctrl.handleWebSocketEvent(
            WebSocketEndpoint.ORCHESTRATOR,
            WebSocketEvent.MessageStart("msg-1")
        )
        ctrl.handleWebSocketEvent(
            WebSocketEndpoint.ORCHESTRATOR,
            WebSocketEvent.TextDelta("First ")
        )
        ctrl.handleWebSocketEvent(
            WebSocketEndpoint.ORCHESTRATOR,
            WebSocketEvent.ToolUse("tu-1", "Read", mapOf("path" to "/tmp"))
        )
        ctrl.handleWebSocketEvent(
            WebSocketEndpoint.ORCHESTRATOR,
            WebSocketEvent.TextDelta("Second")
        )
        advanceUntilIdle()

        val blocks = ctrl.bucketFor(WebSocketEndpoint.ORCHESTRATOR).messages.value[0].blocks
        assertEquals("expected text/tool/text in arrival order, got $blocks", 3, blocks.size)
        val t1 = blocks[0] as MessageBlock.Text
        assertEquals("First ", t1.text)
        assertFalse("first text should be finalized after tool_use slotted", t1.isStreaming)
        val tu = blocks[1] as MessageBlock.ToolUse
        assertEquals("tu-1", tu.toolUseId)
        assertEquals("Read", tu.toolName)
        val t2 = blocks[2] as MessageBlock.Text
        assertEquals("Second", t2.text)
        assertTrue("second text should be streaming", t2.isStreaming)
        cleanup()
    }

    @Test
    fun `streaming order — tool_result populates the matching tool_use block in place`() = runTest {
        val ctrl = controller(this)
        ctrl.handleWebSocketEvent(
            WebSocketEndpoint.ORCHESTRATOR,
            WebSocketEvent.MessageStart("msg-1")
        )
        ctrl.handleWebSocketEvent(
            WebSocketEndpoint.ORCHESTRATOR,
            WebSocketEvent.ToolUse("tu-1", "Read", emptyMap())
        )
        ctrl.handleWebSocketEvent(
            WebSocketEndpoint.ORCHESTRATOR,
            WebSocketEvent.ToolResult("tu-1", "file contents here", false)
        )
        advanceUntilIdle()

        val tu = ctrl.bucketFor(WebSocketEndpoint.ORCHESTRATOR)
            .messages.value[0].blocks[0] as MessageBlock.ToolUse
        assertEquals("file contents here", tu.result)
        assertTrue(tu.isComplete)
        assertFalse(tu.isExecuting)
        cleanup()
    }

    // =================================================================
    // 2. EndpointIsolationParity
    // =================================================================

    @Test
    fun `endpoint isolation — events on AGENT do not appear in ORCHESTRATOR bucket`() = runTest {
        val ctrl = controller(this)
        // Drive a complete streaming sequence on AGENT only.
        ctrl.handleWebSocketEvent(
            WebSocketEndpoint.AGENT,
            WebSocketEvent.MessageStart("agent-msg")
        )
        ctrl.handleWebSocketEvent(
            WebSocketEndpoint.AGENT,
            WebSocketEvent.TextDelta("agent text")
        )
        ctrl.handleWebSocketEvent(
            WebSocketEndpoint.AGENT,
            WebSocketEvent.Status("agent-streaming")
        )
        advanceUntilIdle()

        val orchBucket = ctrl.bucketFor(WebSocketEndpoint.ORCHESTRATOR)
        val agentBucket = ctrl.bucketFor(WebSocketEndpoint.AGENT)
        assertTrue(
            "ORCHESTRATOR messages should be empty, got ${orchBucket.messages.value}",
            orchBucket.messages.value.isEmpty()
        )
        assertEquals("idle", orchBucket.sessionStatus.value)
        assertEquals(1, agentBucket.messages.value.size)
        assertEquals("agent-streaming", agentBucket.sessionStatus.value)
        cleanup()
    }

    @Test
    fun `endpoint isolation — events on ORCHESTRATOR do not appear in AGENT bucket`() = runTest {
        val ctrl = controller(this)
        ctrl.handleWebSocketEvent(
            WebSocketEndpoint.ORCHESTRATOR,
            WebSocketEvent.MessageStart("orch-msg")
        )
        ctrl.handleWebSocketEvent(
            WebSocketEndpoint.ORCHESTRATOR,
            WebSocketEvent.TextDelta("orch text")
        )
        ctrl.handleWebSocketEvent(
            WebSocketEndpoint.ORCHESTRATOR,
            WebSocketEvent.Status("streaming")
        )
        advanceUntilIdle()

        val orchBucket = ctrl.bucketFor(WebSocketEndpoint.ORCHESTRATOR)
        val agentBucket = ctrl.bucketFor(WebSocketEndpoint.AGENT)
        assertEquals(1, orchBucket.messages.value.size)
        assertTrue(agentBucket.messages.value.isEmpty())
        assertEquals("idle", agentBucket.sessionStatus.value)
        cleanup()
    }

    // =================================================================
    // 3. SessionCacheRestoreParity
    // =================================================================

    @Test
    fun `session cache restore — second loadSession of same id restores from cache without HTTP fetch`() = runTest {
        val fakes = FakeApiDeps()
        // First load: server returns a single message page.
        val first = ChatMessage(role = MessageRole.USER, content = "hi")
        fakes.paginatedResponse = PaginatedMessages(
            messages = listOf(first),
            totalCount = 1,
            hasMore = false,
            startIndex = 0
        )
        val ctrl = controller(this, fakes)
        // Load session for the first time — HTTP fetch.
        ctrl.loadSession("sess-1", isOrchestrator = false, liveLocalId = null)
        advanceUntilIdle()
        val callsAfterFirst = fakes.getMessagesCalls.get()
        assertTrue("first load should have fetched", callsAfterFirst >= 1)
        assertEquals(listOf(first), ctrl.bucketFor(WebSocketEndpoint.AGENT).messages.value)

        // Switch away by loading a different session (cache the first).
        // We use a different session id; the second fetch happens once.
        fakes.paginatedResponse = PaginatedMessages(
            messages = listOf(ChatMessage(role = MessageRole.USER, content = "other")),
            totalCount = 1, hasMore = false, startIndex = 0
        )
        ctrl.loadSession("sess-2", isOrchestrator = false, liveLocalId = null)
        advanceUntilIdle()

        // Switch BACK to sess-1 — should hit the cache, NOT the HTTP fetch.
        val callsBeforeRestore = fakes.getMessagesCalls.get()
        ctrl.loadSession("sess-1", isOrchestrator = false, liveLocalId = null)
        advanceUntilIdle()
        assertEquals(
            "expected NO new HTTP fetch on cached restore",
            callsBeforeRestore,
            fakes.getMessagesCalls.get()
        )
        // Restored messages match the first load.
        assertEquals(listOf(first), ctrl.bucketFor(WebSocketEndpoint.AGENT).messages.value)
        cleanup()
    }

    // =================================================================
    // 4. SessionStartedSessionIdParity
    // =================================================================

    @Test
    fun `session_started — jsonlSessionId becomes pendingResumeSessionId when set (reconnect)`() = runTest {
        val ctrl = controller(this)
        val bucket = ctrl.bucketFor(WebSocketEndpoint.ORCHESTRATOR)
        // Simulate the connect-handler having stashed the SDK id from the
        // live pool while the backend echoes back local_id as session_id.
        bucket.pendingResumeSessionId.value = "real-sdk-id"
        ctrl.handleWebSocketEvent(
            WebSocketEndpoint.ORCHESTRATOR,
            WebSocketEvent.SessionStarted(sessionId = "local-id-echo", voice = false)
        )
        advanceUntilIdle()

        assertEquals("real-sdk-id", bucket.jsonlSessionId)
        // pendingResumeSessionId is consumed after the SessionStarted handler runs.
        assertNull(bucket.pendingResumeSessionId.value)
        cleanup()
    }

    @Test
    fun `session_started — jsonlSessionId is event sessionId when no pending resume`() = runTest {
        val ctrl = controller(this)
        val bucket = ctrl.bucketFor(WebSocketEndpoint.ORCHESTRATOR)
        ctrl.handleWebSocketEvent(
            WebSocketEndpoint.ORCHESTRATOR,
            WebSocketEvent.SessionStarted(sessionId = "fresh-sdk-id", voice = false)
        )
        advanceUntilIdle()

        assertEquals("fresh-sdk-id", bucket.jsonlSessionId)
        cleanup()
    }

    // =================================================================
    // 5. PendingAgentResumeParity
    // =================================================================

    @Test
    fun `pending agent resume — loadSession(AGENT) with WS disconnected queues, then Connected fires Start`() = runTest {
        val fakes = FakeApiDeps()
        // loadSession only opens the endpoint when there's actual history to
        // restore (see ChatController.loadSession: paginated.totalCount > 0).
        // Stub a non-empty page so the open path runs.
        fakes.paginatedResponse = PaginatedMessages(
            messages = listOf(ChatMessage(role = MessageRole.USER, content = "old")),
            totalCount = 1, hasMore = false, startIndex = 0
        )
        val ctrl = controller(this, fakes)
        // Persist a server URL the WS can't reach so connect() returns quickly
        // without actually opening a socket — we just need to verify the
        // pendingAgentResume bookkeeping.
        settingsRepository.updateServerUrl("ws://10.255.255.1:9999")
        advanceUntilIdle()
        ctrl.loadSession("agent-sdk-1", isOrchestrator = false, liveLocalId = null)
        advanceUntilIdle()

        assertNotNull(
            "pendingAgentResume should be set after loadSession on disconnected WS",
            ctrl.pendingAgentResumeForTest
        )
        val pending = ctrl.pendingAgentResumeForTest!!
        assertEquals("agent-sdk-1", pending.resumeSdkId)

        // Fire the Connected event — handler must clear pendingAgentResume.
        ctrl.handleWebSocketEvent(WebSocketEndpoint.AGENT, WebSocketEvent.Connected)
        advanceUntilIdle()
        assertNull(
            "pendingAgentResume should be cleared after Connected handler runs",
            ctrl.pendingAgentResumeForTest
        )
        cleanup()
    }

    // =================================================================
    // Extra: ConnectionEvent subscription wires bucket fields correctly
    // =================================================================

    @Test
    fun `OrchestratorAdopted event — sets bucket local_id and pendingResumeSessionId, flips isOrchestratorSession true`() = runTest {
        val ctrl = controller(this)
        val orchBucket = ctrl.bucketFor(WebSocketEndpoint.ORCHESTRATOR)
        // Drive the event through the subscription that ChatController owns.
        ctrl.handleConnectionEvent(
            ConnectionEvent.OrchestratorAdopted("adopted-local", "adopted-sdk")
        )
        advanceUntilIdle()
        assertEquals("adopted-local", orchBucket.currentLocalId.value)
        assertEquals("adopted-sdk", orchBucket.pendingResumeSessionId.value)
        assertTrue(
            "isOrchestratorSession should flip true after OrchestratorAdopted",
            ctrl.isOrchestratorSession.value
        )
        cleanup()
    }
}
