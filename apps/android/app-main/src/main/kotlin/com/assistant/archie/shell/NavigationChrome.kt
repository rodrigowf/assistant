package com.assistant.archie.shell

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListScope
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.assistant.archie.feature.sessions.SessionsIntent
import com.assistant.archie.feature.sessions.ui.ListDensity
import com.assistant.archie.feature.sessions.ui.historySection
import com.assistant.archie.feature.sessions.ui.historyStatus
import com.assistant.archie.feature.sessions.ui.openNowSection
import com.assistant.core.design.components.ArchieIconButton
import com.assistant.core.design.components.ArchieListItem
import com.assistant.core.design.components.ArchieMark
import com.assistant.core.design.components.ListLeadingIcon
import com.assistant.core.design.icons.ArchieIcons
import com.assistant.core.design.theme.ArchieTheme

/*
 * Navigation chrome shared by the drawer (Compact) and the list pane (Medium/Expanded). The list
 * sections (Open now, history by date, row menus) and the session switcher are B-06's
 * (`:feature:sessions`); this file only lays them out.
 */

/** Open now + history + status line for a list (drawer one-line, pane two-line). */
private fun LazyListScope.sessionLists(
    state: ShellUiState,
    onAction: (ShellAction) -> Unit,
    density: ListDensity,
    after: () -> Unit,
) {
    val intent: (SessionsIntent) -> Unit = { i ->
        onAction(ShellAction.Sessions(i))
        if (i is SessionsIntent.OpenHistory) after()
    }
    openNowSection(
        state.items, state.active, density,
        onSelect = { onAction(ShellAction.Select(it)); after() },
        onIntent = intent,
    )
    historySection(state.history, density, intent)
    historyStatus(
        hasRows = state.history.isNotEmpty() || state.items.isNotEmpty(),
        loading = state.historyLoading,
        error = state.historyError,
        query = state.search,
    )
}

/**
 * The navigation drawer body (IA §5, mockup (b)): mark + connection, search, Open now, history by
 * date, footer Memory / Visuals / Settings.
 */
@Composable
fun DrawerContent(
    state: ShellUiState,
    onAction: (ShellAction) -> Unit,
    onNavigate: (ShellNav) -> Unit,
    onClose: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val c = ArchieTheme.colors
    Column(modifier.padding(start = 12.dp, end = 12.dp, top = 12.dp, bottom = 20.dp).testTag("drawer")) {
        Row(
            Modifier.padding(start = 12.dp, end = 8.dp, top = 4.dp, bottom = 16.dp),
            horizontalArrangement = Arrangement.spacedBy(12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            ArchieMark(size = 40.dp)
            Column(Modifier.weight(1f)) {
                Text("Archie", style = ArchieTheme.typography.titleLarge.copy(fontSize = 22.sp, lineHeight = 28.sp), color = c.onSurface)
                ConnectionLine(state.connection)
            }
        }
        SearchField(state.search, { onAction(ShellAction.Search(it)) }, "Search conversations")
        LazyColumn(Modifier.weight(1f).padding(top = 2.dp), contentPadding = PaddingValues(bottom = 8.dp)) {
            sessionLists(state, onAction, ListDensity.OneLine, after = onClose)
        }
        DrawerFooter(onNavigate, onClose)
    }
}

@Composable
private fun ColumnScope.DrawerFooter(onNavigate: (ShellNav) -> Unit, onClose: () -> Unit) {
    ArchieListItem("Memory", { onNavigate(ShellNav.Memory); onClose() }, leading = { ListLeadingIcon(ArchieIcons.Book2) })
    ArchieListItem("Visuals", { onNavigate(ShellNav.Visuals); onClose() }, leading = { ListLeadingIcon(ArchieIcons.BarChart) })
    ArchieListItem("Settings", { onNavigate(ShellNav.Settings); onClose() }, leading = { ListLeadingIcon(ArchieIcons.Settings) })
}

/** Where drawer / rail entries go (resolved per size class by the shell). */
enum class ShellNav { Chats, Memory, Visuals, Settings, History }

/** Expanded/Medium list pane for the Chats destination (IA §3, desktop mockup `.lp`). */
@Composable
fun ChatsPane(
    state: ShellUiState,
    onAction: (ShellAction) -> Unit,
    onCollapse: (() -> Unit)?,
    after: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(modifier.testTag("list-pane")) {
        PaneHeader("Chats", onCollapse)
        SearchField(state.search, { onAction(ShellAction.Search(it)) }, "Search conversations")
        LazyColumn(Modifier.weight(1f).padding(top = 2.dp), contentPadding = PaddingValues(bottom = 8.dp)) {
            sessionLists(state, onAction, ListDensity.TwoLine, after = after)
        }
    }
}

@Composable
fun PaneHeader(title: String, onCollapse: (() -> Unit)?) {
    Row(Modifier.fillMaxWidth().height(56.dp).padding(start = 16.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(title, Modifier.weight(1f), style = ArchieTheme.typography.titleLarge.copy(fontSize = 22.sp, lineHeight = 28.sp), color = ArchieTheme.colors.onSurface)
        if (onCollapse != null) ArchieIconButton(ArchieIcons.LeftPanelClose, "Collapse list", onCollapse)
    }
}
