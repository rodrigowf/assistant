package com.assistant.core.data

import androidx.datastore.preferences.core.mutablePreferencesOf
import androidx.datastore.preferences.core.stringPreferencesKey
import com.assistant.core.network.ArchieApi
import com.assistant.core.network.DiscoveredServer
import com.assistant.core.network.HttpStack
import com.assistant.core.network.ReconnectPolicy
import com.assistant.core.network.SocketClient
import com.assistant.core.session.ArchiePoolApi
import com.assistant.core.session.OrchestratorChannel
import com.assistant.core.session.OrchestratorIdStore
import com.assistant.core.settings.SettingsStore
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The data layer end to end against [FakeBackend] (real sockets, real REST): adoption opens the
 * Archie item, "Open now" follows the server's pool (OPEN-1..4) without stealing focus, lifecycle paths
 * never close or stop anything (P-1), an explicit close is the one `POST …/close`, and adopting a
 * scanned server connects (inv03 §8 bug 3).
 */
class DataLayerTest {
    private val backends = mutableListOf<FakeBackend>()
    private val scopes = mutableListOf<CoroutineScope>()

    @After fun tearDown() {
        scopes.forEach { it.cancel() }
        backends.forEach { it.shutdown() }
    }

    private fun backend() = FakeBackend().start().also { backends += it }

    private class Graph(serverUrl: String?, scanner: ServerScanner = ServerScanner { emptyList() }, autoConnect: Boolean = true) {
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
        val settings = SettingsStore(
            MemoryDataStore(
                mutablePreferencesOf().apply {
                    if (serverUrl != null) this[stringPreferencesKey("server_url")] = serverUrl
                    this[androidx.datastore.preferences.core.booleanPreferencesKey("auto_connect")] = autoConnect
                },
            ),
            scope,
        )
        private val url: () -> String = { settings.settings.value?.serverUrl ?: "ws://127.0.0.1:9" }
        val http = HttpStack()
        val api = ArchieApi(http, url)
        val orchestrator = OrchestratorChannel(
            SocketClient(http, scope, ReconnectPolicy { 200 }), ArchiePoolApi(api), MemIds(), scope,
        )
        val history = HistoryRepository(api, scope)
        val agentSockets = AgentSocketPool({ SocketClient(http, scope, ReconnectPolicy { 200 }) })
        val conversations = ConversationRepository(api, orchestrator, agentSockets, history, scope, { settings.settings.value?.serverUrl })
        val open = OpenSessionsRepository(conversations, history, orchestrator, scope)
        val connection = ConnectionRepository(settings, orchestrator, conversations, open, history, scanner, scope)
    }

    private class MemIds : OrchestratorIdStore {
        var id: String? = null
        override suspend fun load() = id
        override suspend fun save(localId: String) { id = localId }
        override suspend fun clear() { id = null }
    }

    private fun graph(serverUrl: String?, scanner: ServerScanner = ServerScanner { emptyList() }, autoConnect: Boolean = true) =
        Graph(serverUrl, scanner, autoConnect).also { scopes += it.scope }

    private val orchPool = """[{"local_id":"ORCH","sdk_session_id":"JSONL","status":"idle","cost":0.0,"turns":0,"title":"Living-room TV","is_orchestrator":true}]"""

    @Test fun adoptionOpensArchieTab_titleFromList_subscribed() {
        val b = backend()
        b.poolJson = orchPool
        b.sessionsJson = """[{"session_id":"JSONL","started_at":"2026-10-03T10:00:00+00:00","last_activity":"2026-10-03T11:00:00+00:00","title":"Living-room TV","message_count":4,"is_orchestrator":true,"provider":"claude","local_id":"ORCH"}]"""
        val g = graph(b.url)
        g.connection.start()

        eventually(message = { "items=${g.open.items.value}" }) { g.open.items.value.any { it.key == ItemKey.Archie && it.title == "Living-room TV" } }
        assertEquals(ItemKey.Archie, g.open.active.value)
        eventually { g.conversations.current(ConversationKey.ARCHIE)?.connection == com.assistant.core.model.ConnectionState.SUBSCRIBED }
        val start = b.frames.first { it.first == "orch" && it.second.contains("\"type\":\"start\"") }.second
        assertTrue(start, start.contains("\"local_id\":\"ORCH\"") && start.contains("\"resume_sdk_id\":\"JSONL\""))
        // Exactly one start for one adoption (no double start, inv03 §3.3).
        assertEquals(1, b.frames.count { it.first == "orch" && it.second.contains("\"type\":\"start\"") })
        assertEquals(ConnectionStatus.Phase.CONNECTED, g.connection.status.value.phase)
    }

    @Test fun talkPhraseVoiceMessage_isThisDevicesTurn_notABackgroundRun() {
        val b = backend()
        b.poolJson = orchPool
        val g = graph(b.url)
        g.connection.start()
        eventually { g.conversations.current(ConversationKey.ARCHIE)?.connection == com.assistant.core.model.ConnectionState.SUBSCRIBED }

        g.conversations.voiceMessageSent()          // what the voice host reports after send_audio
        eventually(message = { "entries=${g.conversations.current(ConversationKey.ARCHIE)?.entries}" }) {
            g.conversations.current(ConversationKey.ARCHIE)?.entries?.any {
                it is com.assistant.core.conversation.UserEntry && it.origin == com.assistant.core.conversation.UserOrigin.AUDIO
            } == true
        }
        val entries = g.conversations.current(ConversationKey.ARCHIE)!!.entries
        assertTrue("no BG-1 notice for this device's own voice message", entries.none {
            it is com.assistant.core.conversation.NoticeEntry && it.notice == com.assistant.core.conversation.NoticeKind.BACKGROUND
        })
    }

    private val agentRow = """{"local_id":"AG1","sdk_session_id":"SDK1","status":"tool_use","cost":0.0,"turns":2,"title":"Energy dashboard","is_orchestrator":false}"""
    private val orchAndAgentPool = orchPool.dropLast(1) + ",$agentRow]"

    private fun FakeBackend.agentStarts(localId: String) =
        frames.filter { it.first == "agent" && it.second.contains("\"type\":\"start\"") && it.second.contains("\"local_id\":\"$localId\"") }.map { it.second }

    @Test fun open1_aPoolSessionIsListedWithoutAView_watcherEventsKeepItCurrent_noFocusChange() {
        val b = backend()
        b.poolJson = orchPool
        val g = graph(b.url)
        g.connection.start()
        eventually { g.open.active.value == ItemKey.Archie && b.orchestratorSockets.isNotEmpty() }

        b.poolJson = orchAndAgentPool
        b.pushOrchestrator("""{"type":"agent_session_opened","session_id":"AG1","sdk_session_id":"SDK1","is_orchestrator":false}""")
        eventually(message = { "items=${g.open.items.value}" }) { g.open.items.value.any { it.kind == ItemKind.AGENT && it.localId == "AG1" } }
        val row = g.open.items.value.first { it.localId == "AG1" }
        assertEquals(TabStatus.WORKING, row.status)                              // the pool row's status
        assertEquals("focus never moves on a server event (FOCUS-1)", ItemKey.Archie, g.open.active.value)
        Thread.sleep(300)
        assertTrue("listed, not opened: no socket for it", b.agentStarts("AG1").isEmpty())

        b.poolJson = orchPool
        b.pushOrchestrator("""{"type":"agent_session_closed","session_id":"AG1","is_orchestrator":false}""")
        eventually(message = { "items=${g.open.items.value}" }) { g.open.items.value.none { it.localId == "AG1" } }
    }

    @Test fun open2_selectingAPoolSessionOpensItsViewWithAReattachStart() {
        val b = backend()
        b.poolJson = orchAndAgentPool                                            // e.g. restored after a backend restart
        val g = graph(b.url)
        g.connection.start()
        eventually(message = { "items=${g.open.items.value}" }) { g.open.items.value.any { it.localId == "AG1" } }
        g.open.select(g.open.items.value.first { it.localId == "AG1" }.key)
        eventually(message = { "frames=${b.frames}" }) { b.agentStarts("AG1").isNotEmpty() }
        assertTrue(b.agentStarts("AG1").single(), b.agentStarts("AG1").single().contains("\"reattach\":true"))
        val key = g.open.active.value as ItemKey.Agent
        eventually { g.conversations.current(key.conversation)?.connection == com.assistant.core.model.ConnectionState.SUBSCRIBED }
    }

    @Test fun open2_historyOpenAndNewSessionSendNoReattach_theirReconnectsDo() {
        val b = backend()
        b.poolJson = orchPool
        val g = graph(b.url)
        g.connection.start()
        eventually { g.open.active.value == ItemKey.Archie }
        g.open.openSession(com.assistant.core.model.SessionSummary("PAST", null, null, "Old work", 3, false, null, "H1"))
        eventually(message = { "frames=${b.frames}" }) { b.agentStarts("H1").isNotEmpty() }
        assertTrue("the user's open may resume it", !b.agentStarts("H1").single().contains("reattach"))
        eventually { g.conversations.current(ConversationKey.agent("H1"))?.connection == com.assistant.core.model.ConnectionState.SUBSCRIBED }

        b.agentSockets.toList().forEach { it.close(1001, "going away") }      // a drop: the reconnect reattaches
        eventually(message = { "frames=${b.frames}" }) { b.agentStarts("H1").size == 2 }
        assertTrue(b.agentStarts("H1")[1], b.agentStarts("H1")[1].contains("\"reattach\":true"))
    }

    @Test fun open3_closedElsewhereWhileActive_viewClosesAndFocusMoves_noCloseRequest() {
        val b = backend()
        b.poolJson = orchPool
        val g = graph(b.url)
        g.connection.start()
        eventually { g.open.active.value == ItemKey.Archie }
        g.open.newAgentSession()
        val key = g.open.active.value as ItemKey.Agent
        val localId = g.conversations.current(key.conversation)!!.ref.localId
        eventually { g.conversations.current(key.conversation)?.connection == com.assistant.core.model.ConnectionState.SUBSCRIBED }
        assertEquals(key, g.open.active.value)

        val notices = java.util.concurrent.CopyOnWriteArrayList<String>()
        g.scope.launch { g.open.notices.collect { notices += it } }
        Thread.sleep(100)
        b.closedElsewhere(localId)
        b.pushOrchestrator("""{"type":"agent_session_closed","session_id":"$localId","is_orchestrator":false}""")
        eventually(message = { "items=${g.open.items.value}" }) { g.open.items.value.none { it.key == key } }
        assertEquals("focus moves as after an explicit close", ItemKey.Archie, g.open.active.value)
        eventually(message = { "notices=$notices" }) { notices == listOf("New agent session was closed elsewhere") }
        assertEquals(null, g.conversations.current(key.conversation))
        assertTrue(b.closeRequests().isEmpty())

        // The Archie conversation too: its view closes, nothing is left open.
        b.pushOrchestrator("""{"type":"agent_session_closed","session_id":"ORCH","is_orchestrator":true}""")
        eventually(message = { "items=${g.open.items.value}" }) { g.open.items.value.none { it.key == ItemKey.Archie } }
        assertEquals(null, g.conversations.current(ConversationKey.ARCHIE))
        assertTrue(b.closeRequests().isEmpty())
        Thread.sleep(200)
        assertEquals("no notice for Archie", 1, notices.size)
    }

    @Test fun open4_closedWhileAway_aPoolReadClosesTheView() {
        val b = backend()
        b.poolJson = orchPool
        val g = graph(b.url)
        g.connection.start()
        eventually { g.open.active.value == ItemKey.Archie }
        g.open.newAgentSession(); val first = g.open.active.value as ItemKey.Agent
        g.open.newAgentSession(); val second = g.open.active.value as ItemKey.Agent
        val firstId = g.conversations.current(first.conversation)!!.ref.localId
        val secondId = g.conversations.current(second.conversation)!!.ref.localId
        eventually { listOf(first, second).all { g.conversations.current(it.conversation)?.connection == com.assistant.core.model.ConnectionState.SUBSCRIBED } }

        // Both closed on another device while this one was not listening (no watcher frame).
        b.closedElsewhere(firstId); b.closedElsewhere(secondId)               // then e.g. the foreground read:
        val notices = java.util.concurrent.CopyOnWriteArrayList<String>()
        g.scope.launch { g.open.notices.collect { notices += it } }
        Thread.sleep(100)
        runBlocking { g.history.syncPool() }
        eventually(message = { "items=${g.open.items.value}" }) { g.open.items.value.none { it.key == first || it.key == second } }
        assertEquals("only the active view gets a notice", 1, notices.size)
        assertTrue(b.closeRequests().isEmpty())
        assertEquals("no re-start: it would re-open them", 1, b.agentStarts(firstId).size)
    }

    @Test fun open3_aReconnectFindingItClosed_sessionClosed_closesTheView() {
        val b = backend()
        b.poolJson = orchPool
        val g = graph(b.url)
        g.connection.start()
        eventually { g.open.active.value == ItemKey.Archie }
        g.open.newAgentSession()
        val key = g.open.active.value as ItemKey.Agent
        val localId = g.conversations.current(key.conversation)!!.ref.localId
        eventually { g.conversations.current(key.conversation)?.connection == com.assistant.core.model.ConnectionState.SUBSCRIBED }

        b.closedElsewhere(localId)
        b.agentSockets.toList().forEach { it.close(1001, "going away") }
        eventually(message = { "items=${g.open.items.value}" }) { g.open.items.value.none { it.key == key } }
        assertEquals(2, b.agentStarts(localId).size)                            // the user's, then the reattach
        assertTrue(b.agentStarts(localId)[1].contains("\"reattach\":true"))
    }

    @Test fun lifecycleNeverClosesOrStops_explicitCloseDoes_P1() = runBlocking {
        val b = backend()
        b.poolJson = orchPool
        val g = graph(b.url)
        g.connection.start()
        eventually { g.open.active.value == ItemKey.Archie }
        val agentKey = ItemKey.Agent(g.conversations.newAgent())
        g.open.select(agentKey)
        eventually { b.frames.any { it.first == "agent" && it.second.contains("\"type\":\"start\"") } }

        // Background, foreground, background again, then the process scope dies (app swiped away).
        g.conversations.onBackground(keepOrchestratorAlive = false)
        g.conversations.onForeground()
        g.conversations.onBackground(keepOrchestratorAlive = false)
        g.orchestrator.disconnect()
        Thread.sleep(500)
        assertTrue("no close on lifecycle: ${b.closeRequests()}", b.closeRequests().isEmpty())
        assertTrue("no stop on lifecycle: ${b.stopFrames()}", b.stopFrames().isEmpty())

        // Explicit close of the agent tab = exactly one pool close for its localId, for every device.
        val localId = g.conversations.current(agentKey.conversation)!!.ref.localId
        assertTrue(g.open.close(agentKey))
        assertEquals(listOf("POST /api/sessions/$localId/close"), b.closeRequests())
        assertTrue(g.open.items.value.none { it.key == agentKey })
        assertEquals(ItemKey.Archie, g.open.active.value)
        assertTrue(b.stopFrames().isEmpty())
    }

    @Test fun adoptingScannedServerConnects_bug3() {
        val found = backend()
        found.poolJson = orchPool
        val host = found.server.hostName
        val port = found.server.port
        // Fresh install on the default URL. Auto-connect is off here so the test never dials the real
        // default host (the Jetson); the launch scan finds the backend and adopting it must connect.
        val g = graph(serverUrl = null, scanner = ServerScanner { listOf(DiscoveredServer(host, port, secure = false)) }, autoConnect = false)
        g.connection.start()

        eventually(message = { "requests=${found.requests}" }) { found.requests.any { it == "GET /api/orchestrator/chat" } }
        eventually { g.settings.settings.value?.serverUrl == "ws://$host:$port" }
        eventually { g.open.active.value == ItemKey.Archie }
        assertEquals(ConnectionStatus.Phase.CONNECTED, g.connection.status.value.phase)
    }

    @Test fun changeServer_tearsDownLocally_connectsToNewServer_closesNothing_T15() = runBlocking {
        val a = backend(); a.poolJson = orchPool
        val bb = backend(); bb.poolJson = orchPool.replace("ORCH", "ORCH2")
        val g = graph(a.url)
        g.connection.start()
        eventually { g.conversations.current(ConversationKey.ARCHIE)?.connection == com.assistant.core.model.ConnectionState.SUBSCRIBED }

        g.connection.changeServer(bb.url)
        eventually(message = { "b frames=${bb.frames}" }) { bb.frames.any { it.second.contains("\"local_id\":\"ORCH2\"") } }
        eventually { g.conversations.current(ConversationKey.ARCHIE)?.ref?.localId == "ORCH2" }
        assertTrue(a.closeRequests().isEmpty() && a.stopFrames().isEmpty())
        assertNotNull(g.open.items.value.firstOrNull { it.key == ItemKey.Archie })
    }

    @Test fun agentApproval_restWithoutAView_socketWithOne_section6_9() = runBlocking {
        val b = backend(); b.poolJson = orchPool
        val g = graph(b.url)
        g.connection.start()
        eventually { g.conversations.current(ConversationKey.ARCHIE)?.connection == com.assistant.core.model.ConnectionState.SUBSCRIBED }

        // No view of AG7 here: REST.
        b.permissionResponse = okhttp3.mockwebserver.MockResponse().setHeader("Content-Type", "application/json").setBody("""{"ok":true}""")
        assertEquals(ApprovalAnswer.Sent, g.conversations.answerAgentApproval("AG7", "r1", allow = true))
        assertEquals(listOf("""{"request_id":"r1","decision":"allow"}"""), b.permissionBodies.toList())
        assertTrue(b.requests.contains("POST /api/sessions/AG7/permission"))

        // 409: someone answered first. 404 "Not Found": a server without the route.
        b.permissionResponse = okhttp3.mockwebserver.MockResponse().setResponseCode(409).setHeader("Content-Type", "application/json").setBody("""{"detail":"No pending permission request 'r2'"}""")
        assertEquals(ApprovalAnswer.AlreadyAnswered, g.conversations.answerAgentApproval("AG7", "r2", allow = false))
        b.permissionResponse = null
        val old = g.conversations.answerAgentApproval("AG7", "r3", allow = true)
        assertTrue("$old", old is ApprovalAnswer.Failed && old.message.contains("Open that session"))
        b.permissionResponse = okhttp3.mockwebserver.MockResponse().setResponseCode(404).setHeader("Content-Type", "application/json").setBody("""{"detail":"No live pool session with local_id='AG7'"}""")
        val gone = g.conversations.answerAgentApproval("AG7", "r4", allow = true)
        assertEquals(ApprovalAnswer.Failed("That agent session is no longer running"), gone)

        // With AG7's view open (subscribed), the answer rides its own socket; preferSocket=false forces REST.
        g.conversations.openAgent(com.assistant.core.model.SessionRef("AG7", null, com.assistant.core.model.SessionKind.AGENT, null))
        eventually { g.conversations.current(ConversationKey.agent("AG7"))?.connection == com.assistant.core.model.ConnectionState.SUBSCRIBED }
        val restCalls = b.permissionBodies.size
        assertEquals(ApprovalAnswer.Sent, g.conversations.answerAgentApproval("AG7", "r5", allow = false))
        eventually { b.frames.any { it.first == "agent" && it.second.contains("\"permission_response\"") && it.second.contains("\"r5\"") } }
        assertEquals(restCalls, b.permissionBodies.size)
        b.permissionResponse = okhttp3.mockwebserver.MockResponse().setHeader("Content-Type", "application/json").setBody("""{"ok":true}""")
        assertEquals(ApprovalAnswer.Sent, g.conversations.answerAgentApproval("AG7", "r6", allow = true, preferSocket = false))
        assertEquals(restCalls + 1, b.permissionBodies.size)
    }

    /**
     * §6.11a: Archie's `switch_conversation`. The server closed the old orchestrator (OPEN-3) and
     * sends `orchestrator_switch` to this socket: the Archie view is replaced in place by the past
     * conversation (new local id, `resume_sdk_id`, history cold-opened) and focused (SW-2), with no
     * conflict dialog, no close and no stop; a duplicate frame does nothing (SW-1).
     */
    @Test fun orchestratorSwitch_resumesThePastConversation_andFocusesIt_section6_11a() {
        val b = backend()
        b.poolJson = orchPool
        b.orchJsonl = { it ?: "JSONL" }
        val g = graph(b.url)
        val seen = java.util.Collections.synchronizedList(mutableListOf<ConversationEvent>())
        g.scope.launch { g.conversations.events.collect { seen += it } }
        g.connection.start()
        eventually { g.conversations.current(ConversationKey.ARCHIE)?.connection == com.assistant.core.model.ConnectionState.SUBSCRIBED }
        val agentKey = ItemKey.Agent(g.conversations.newAgent())
        g.open.select(agentKey)                                                    // the user is elsewhere

        b.poolJson = "[]"
        b.pushOrchestrator("""{"type":"agent_session_closed","session_id":"ORCH","is_orchestrator":true}""")
        b.pushOrchestrator("""{"type":"orchestrator_switch","sdk_session_id":"PAST","title":"Lamps","voice":false,"from_session_id":"ORCH"}""")

        val isResume = { f: Pair<String, String> -> f.first == "orch" && f.second.contains("\"type\":\"start\"") && f.second.contains("\"resume_sdk_id\":\"PAST\"") }
        eventually(message = { "frames=${b.frames}" }) { b.frames.any(isResume) }
        eventually(message = { "archie=${g.conversations.current(ConversationKey.ARCHIE)?.ref}" }) {
            val st = g.conversations.current(ConversationKey.ARCHIE)
            st?.ref?.sdkId == "PAST" && st.connection == com.assistant.core.model.ConnectionState.SUBSCRIBED
        }
        val ref = g.conversations.current(ConversationKey.ARCHIE)!!.ref
        assertTrue("a new local id, never the stopped one: $ref", ref.localId != "ORCH")
        assertEquals(ref.localId, g.orchestrator.state.value.orchestrator?.localId)
        assertTrue(b.frames.first(isResume).second.contains("\"local_id\":\"${ref.localId}\""))
        eventually { g.open.active.value == ItemKey.Archie }
        eventually { seen.any { it == ConversationEvent.ArchieSwitched("PAST", "Lamps") } }
        assertTrue("history of the past conversation", b.requests.any { it.startsWith("GET /api/sessions/PAST/messages") })
        assertTrue(seen.none { it is ConversationEvent.OrchestratorConflict })

        b.pushOrchestrator("""{"type":"orchestrator_switch","sdk_session_id":"PAST","title":"Lamps","voice":false,"from_session_id":"ORCH"}""")
        Thread.sleep(300)
        assertEquals("SW-1: once", 1, b.frames.count(isResume))
        assertEquals(ref, g.conversations.current(ConversationKey.ARCHIE)!!.ref)
        assertTrue(b.closeRequests().isEmpty())
        assertTrue(b.stopFrames().isEmpty())
    }
}
