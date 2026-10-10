package com.assistant.archie.shell

import androidx.compose.runtime.Immutable
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.assistant.archie.feature.sessions.HistoryList
import com.assistant.archie.feature.sessions.RepositorySessionsBackend
import com.assistant.archie.feature.sessions.SessionsController
import com.assistant.archie.feature.sessions.SessionsIntent
import com.assistant.archie.feature.sessions.SessionsUiState
import com.assistant.archie.graph.MainAppGraph
import com.assistant.core.data.ConnectionStatus
import com.assistant.core.data.HistoryGroup
import com.assistant.core.data.ItemKey
import com.assistant.core.data.WorkspaceItem
import com.assistant.core.model.SessionSummary
import com.assistant.core.model.ThemeMode
import com.assistant.core.voice.ports.SessionPhase
import com.assistant.core.voice.ports.VoiceSessionState
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import java.time.Clock

/** Everything the shell chrome renders (spec 14 §2.1: immutable UI state from domain state). */
@Immutable
data class ShellUiState(
    val connection: ConnectionStatus = ConnectionStatus(),
    val items: List<WorkspaceItem> = emptyList(),
    val active: ItemKey? = null,
    val history: List<HistoryGroup> = emptyList(),
    val historyLoading: Boolean = false,
    val historyError: String? = null,
    val search: String = "",
    val voice: VoiceSessionState = VoiceSessionState(),
    val listPaneCollapsed: Boolean = false,
    val themeMode: ThemeMode = ThemeMode.SYSTEM,
    /** B-06: session dialogs, busy overlay and snackbar. */
    val sessions: SessionsUiState = SessionsUiState(),
) {
    val activeItem: WorkspaceItem? get() = items.firstOrNull { it.key == active }
    val voiceActive: Boolean get() = voice.phase != SessionPhase.OFF && voice.phase != SessionPhase.ERROR
}

/** Shell intents (UDF: UI → ViewModel). */
sealed interface ShellAction {
    data class Select(val key: ItemKey) : ShellAction
    data class SelectRelative(val delta: Int) : ShellAction
    data class OpenHistory(val session: SessionSummary) : ShellAction
    data object NewArchie : ShellAction
    data object NewAgent : ShellAction
    /** Explicit close by the user (P-1: the only path that closes on the server). */
    data class Close(val key: ItemKey) : ShellAction
    data class Search(val query: String) : ShellAction
    data object ToggleListPane : ShellAction
    data object RefreshHistory : ShellAction
    data object Reconnect : ShellAction
    data class OpenMemoryDoc(val path: String) : ShellAction
    data class OpenVisual(val path: String) : ShellAction
    data class Compact(val key: ItemKey) : ShellAction

    /** B-06 session flows (menus, dialogs, history rows, snackbar). */
    data class Sessions(val intent: SessionsIntent) : ShellAction
}

/**
 * The `WorkspaceViewModel` of spec 14 §2.1: maps [com.assistant.core.data.OpenSessionsRepository]
 * (tabs / switcher), the session directory (drawer history, grouped by local day with UTC-correct
 * parsing), the connection and the voice presence to [ShellUiState]. Domain state stays in the
 * process-scoped repositories; this only maps it. The session flows (B-06: every Archie entry point
 * through the §6.11 conflict dialog, new agent, rename / duplicate / fork / close / delete) run in
 * [sessions].
 */
class ShellViewModel(
    private val graph: MainAppGraph,
    private val clock: Clock = Clock.systemDefaultZone(),
) : ViewModel() {
    private val search = MutableStateFlow("")

    /** B-06 flows over the process-scoped repositories. */
    val sessions = SessionsController(
        RepositorySessionsBackend(graph.conversations, graph.openSessions, graph.history, graph.orchestrator, graph.api),
        viewModelScope,
    )

    /** Date groups and stamps follow the clock (web: a minute clock). */
    private val minute = flow {
        while (true) {
            emit(clock.instant())
            delay(60_000)
        }
    }

    private val base = combine(
        graph.openSessions.items,
        graph.openSessions.active,
        graph.connection.status,
        graph.voice.state,
    ) { items, active, conn, voice ->
        ShellUiState(connection = conn, items = items, active = active, voice = voice)
    }

    private val withHistory = combine(base, graph.history.sessions, search, minute) { b, list, q, now ->
        b.copy(
            history = HistoryList.groups(list.value.orEmpty(), b.items, q, now, clock.zone),
            historyLoading = list.loading,
            historyError = list.error,
            search = q,
        )
    }

    val state: StateFlow<ShellUiState> = combine(withHistory, graph.settings.settings, sessions.state) { b, s, ui ->
        b.copy(
            listPaneCollapsed = s?.listPaneCollapsed == true,
            themeMode = s?.themeMode ?: ThemeMode.SYSTEM,
            sessions = ui,
        )
    }.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), ShellUiState())

    fun onAction(a: ShellAction) {
        val open = graph.openSessions
        when (a) {
            is ShellAction.Select -> open.select(a.key)
            is ShellAction.SelectRelative -> open.selectRelative(a.delta)
            is ShellAction.OpenHistory -> sessions.openFromHistory(a.session)
            ShellAction.NewArchie -> sessions.requestNewArchie()
            ShellAction.NewAgent -> sessions.onIntent(SessionsIntent.NewAgent)
            is ShellAction.Close -> viewModelScope.launch { open.close(a.key) }
            is ShellAction.Search -> search.value = a.query
            ShellAction.ToggleListPane -> viewModelScope.launch {
                graph.settings.setListPaneCollapsed(!(graph.settings.settings.value?.listPaneCollapsed ?: false))
            }
            ShellAction.RefreshHistory -> graph.history.refreshAll()
            ShellAction.Reconnect -> graph.connection.connect()
            is ShellAction.OpenMemoryDoc -> open.openMemory(a.path)
            is ShellAction.OpenVisual -> open.openVisual(a.path)
            is ShellAction.Compact -> sessions.onIntent(SessionsIntent.Compact(a.key))
            is ShellAction.Sessions -> sessions.onIntent(a.intent)
        }
    }
}
