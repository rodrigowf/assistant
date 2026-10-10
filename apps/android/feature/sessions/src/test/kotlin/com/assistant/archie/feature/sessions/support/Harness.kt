package com.assistant.archie.feature.sessions.support

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import com.assistant.archie.feature.sessions.HistoryList
import com.assistant.archie.feature.sessions.RepositorySessionsBackend
import com.assistant.archie.feature.sessions.SessionsController
import com.assistant.archie.feature.sessions.SessionsIntent
import com.assistant.archie.feature.sessions.ui.HistoryListState
import com.assistant.archie.feature.sessions.ui.HistoryScreen
import com.assistant.archie.feature.sessions.ui.SessionMenuButton
import com.assistant.archie.feature.sessions.ui.SessionSwitcherContent
import com.assistant.archie.feature.sessions.ui.SessionsHost
import com.assistant.core.data.AgentSocketPool
import com.assistant.core.data.ConversationRepository
import com.assistant.core.data.HistoryRepository
import com.assistant.core.data.OpenSessionsRepository
import com.assistant.core.design.theme.ArchieTheme
import com.assistant.core.network.ArchieApi
import com.assistant.core.network.HttpStack
import com.assistant.core.network.ReconnectPolicy
import com.assistant.core.network.SocketClient
import com.assistant.core.session.ArchiePoolApi
import com.assistant.core.session.OrchestratorChannel
import com.assistant.core.session.OrchestratorIdStore
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import java.time.Instant
import java.time.ZoneId

/**
 * The real B-03 data layer (sockets, REST, reducer) against [FakeBackend], plus the B-06 controller
 * over it — the same wiring as the app's `ShellViewModel`.
 */
class Harness(val backend: FakeBackend) {
    val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val http = HttpStack()
    val api = ArchieApi(http) { backend.url }
    val orchestrator = OrchestratorChannel(SocketClient(http, scope, ReconnectPolicy { 200 }), ArchiePoolApi(api), MemIds(), scope)
    val history = HistoryRepository(api, scope)
    private val agentSockets = AgentSocketPool({ SocketClient(http, scope, ReconnectPolicy { 200 }) })
    val conversations = ConversationRepository(api, orchestrator, agentSockets, history, scope, { backend.url })
    val open = OpenSessionsRepository(conversations, history, orchestrator, scope)
    val controller = SessionsController(RepositorySessionsBackend(conversations, open, history, orchestrator, api), scope)

    /** Connects like `ConnectionRepository.connect` and waits until the probe settled. */
    fun connect(): Harness {
        orchestrator.connect(backend.url)
        history.refreshAll()
        eventually(message = { "channel=${orchestrator.state.value}" }) {
            val s = orchestrator.state.value
            s.noOrchestrator || s.subscribed
        }
        eventually { history.sessions.value.loaded }
        return this
    }

    fun close() = scope.cancel()

    private class MemIds : OrchestratorIdStore {
        var id: String? = null
        override suspend fun load() = id
        override suspend fun save(localId: String) { id = localId }
        override suspend fun clear() { id = null }
    }
}

enum class Surface { History, Switcher }

/**
 * A test screen: the ⋮ menu of the active item ("active-title" shows which one), the History screen
 * or the switcher, and the session host (dialogs, busy, snackbar) — all from the real state.
 */
@Composable
fun FlowScreen(h: Harness, surface: Surface) {
    val items by h.open.items.collectAsState()
    val active by h.open.active.collectAsState()
    val sessions by h.history.sessions.collectAsState()
    val ui by h.controller.state.collectAsState()
    val onIntent: (SessionsIntent) -> Unit = h.controller::onIntent
    val activeItem = items.firstOrNull { it.key == active }
    ArchieTheme(reduceMotion = true) {
        Box(Modifier.fillMaxSize()) {
            Column(Modifier.fillMaxSize()) {
                androidx.compose.foundation.layout.Row(Modifier.fillMaxWidth()) {
                    Text(activeItem?.title ?: "-", Modifier.weight(1f).testTag("active-title"))
                    SessionMenuButton(activeItem, onIntent, {})
                }
                when (surface) {
                    Surface.History -> HistoryScreen(
                        state = HistoryListState(
                            items = items, active = active,
                            groups = HistoryList.groups(sessions.value.orEmpty(), items, "", Instant.now(), ZoneId.systemDefault()),
                            loading = sessions.loading, error = sessions.error,
                        ),
                        onQuery = {}, onSelect = h.open::select, onIntent = onIntent,
                        onRefresh = {}, onBack = {}, onOpened = {},
                    )
                    Surface.Switcher -> SessionSwitcherContent(
                        items = items, active = active,
                        onSelect = h.open::select,
                        onRequestClose = { onIntent(SessionsIntent.RequestClose(it)) },
                        onNewArchie = { onIntent(SessionsIntent.NewArchie) },
                        onNewAgent = { onIntent(SessionsIntent.NewAgent) },
                        onHistory = {}, onDismiss = {},
                    )
                }
            }
            SessionsHost(ui, onIntent, autofocus = false)
        }
    }
}
