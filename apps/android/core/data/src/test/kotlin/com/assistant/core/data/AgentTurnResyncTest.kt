package com.assistant.core.data

import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.mutablePreferencesOf
import androidx.datastore.preferences.core.stringPreferencesKey
import com.assistant.core.model.ConnectionState
import com.assistant.core.model.SessionKind
import com.assistant.core.model.SessionRef
import com.assistant.core.network.ArchieApi
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
import org.junit.After
import org.junit.Test

/**
 * ST-2 after every (re)subscribe (2026-10-10): an agent turn that ended while the socket was away,
 * or ended without a terminal frame (the orchestrator's runner stopping it), must not stay
 * "Using tools…" forever — the view re-reads `pool/live` on each `session_started`.
 */
class AgentTurnResyncTest {
    private val backends = mutableListOf<FakeBackend>()
    private val scopes = mutableListOf<CoroutineScope>()

    @After fun tearDown() {
        scopes.forEach { it.cancel() }
        backends.forEach { it.shutdown() }
    }

    private class MemIds : OrchestratorIdStore {
        var id: String? = null
        override suspend fun load() = id
        override suspend fun save(localId: String) { id = localId }
        override suspend fun clear() { id = null }
    }

    private fun pool(agentStatus: String) =
        """[{"local_id":"ORCH","sdk_session_id":"JSONL","status":"idle","cost":0.0,"turns":0,"title":"Archie","is_orchestrator":true},""" +
            """{"local_id":"AG7","sdk_session_id":"SDK7","status":"$agentStatus","cost":0.0,"turns":3,"title":"Digest","is_orchestrator":false}]"""

    private fun conversations(b: FakeBackend): ConversationRepository {
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default).also { scopes += it }
        val settings = SettingsStore(
            MemoryDataStore(
                mutablePreferencesOf().apply {
                    this[stringPreferencesKey("server_url")] = b.url
                    this[booleanPreferencesKey("auto_connect")] = true
                },
            ),
            scope,
        )
        val url: () -> String = { settings.settings.value?.serverUrl ?: "ws://127.0.0.1:9" }
        val http = HttpStack()
        val api = ArchieApi(http, url)
        val orchestrator = OrchestratorChannel(SocketClient(http, scope, ReconnectPolicy { 200 }), ArchiePoolApi(api), MemIds(), scope)
        val history = HistoryRepository(api, scope)
        val agentSockets = AgentSocketPool({ SocketClient(http, scope, ReconnectPolicy { 200 }) })
        return ConversationRepository(api, orchestrator, agentSockets, history, scope, { settings.settings.value?.serverUrl })
    }

    @Test fun turnThatEndedWhileAway_endsOnResubscribe_forEverySettledStatus() {
        for (settled in listOf("idle", "interrupted", "disconnected")) {
            val b = FakeBackend().start().also { backends += it }
            b.poolJson = pool("tool_use")
            val convs = conversations(b)
            val key = ConversationKey.agent("AG7")

            convs.openAgent(SessionRef("AG7", "SDK7", SessionKind.AGENT, null))
            eventually(message = { "subscribed and in turn ($settled): ${convs.current(key)}" }) {
                val s = convs.current(key)
                s?.connection == ConnectionState.SUBSCRIBED && s.inTurn
            }

            // The turn ends server-side with no frame reaching this view, then the socket drops.
            b.poolJson = pool(settled)
            b.agentSockets.toList().forEach { it.close(1001, "going away") }

            eventually(message = { "turn still running after resubscribe ($settled): ${convs.current(key)}" }) {
                val s = convs.current(key)
                s?.connection == ConnectionState.SUBSCRIBED && !s.inTurn
            }
        }
    }
}
