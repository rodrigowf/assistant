package com.assistant.archie.feature.sessions.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.SwipeToDismissBox
import androidx.compose.material3.SwipeToDismissBoxValue
import androidx.compose.material3.Text
import androidx.compose.material3.rememberSwipeToDismissBoxState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.key
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.CustomAccessibilityAction
import androidx.compose.ui.semantics.customActions
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import com.assistant.core.data.ItemKey
import com.assistant.core.data.ItemKind
import com.assistant.core.data.WorkspaceItem
import com.assistant.core.design.components.ArchieButton
import com.assistant.core.design.components.ArchieIconButton
import com.assistant.core.design.components.ArchieListItem
import com.assistant.core.design.components.ArchieMark
import com.assistant.core.design.components.ButtonStyle
import com.assistant.core.design.components.ListItemSize
import com.assistant.core.design.components.ListLeadingTile
import com.assistant.core.design.components.ListSectionHeader
import com.assistant.core.design.components.ProviderChip
import com.assistant.core.design.components.StatusIndicator
import com.assistant.core.design.icons.ArchieIcon
import com.assistant.core.design.icons.ArchieIcons
import com.assistant.core.design.theme.ArchieTheme
import kotlinx.coroutines.launch

/**
 * The session switcher sheet body (IA §5, mockup phone (c)): the phone version of tabs. "Open now"
 * with live status, each closable by × **or a horizontal swipe** (both go through the same close
 * flow: Archie and a working agent ask first, P-1) — the server's open set, the same on every device — then
 * "New Archie chat" / "New agent session"; "History" opens the History screen.
 */
@Composable
fun SessionSwitcherContent(
    items: List<WorkspaceItem>,
    active: ItemKey?,
    onSelect: (ItemKey) -> Unit,
    onRequestClose: (WorkspaceItem) -> Unit,
    onNewArchie: () -> Unit,
    onNewAgent: () -> Unit,
    onHistory: () -> Unit,
    onDismiss: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(modifier.padding(start = 12.dp, end = 12.dp, bottom = 30.dp).testTag("switcher")) {
        Row(Modifier.fillMaxWidth().padding(start = 16.dp, end = 8.dp, bottom = 8.dp), verticalAlignment = Alignment.CenterVertically) {
            Text("Sessions", Modifier.weight(1f), style = ArchieTheme.typography.titleLarge, color = ArchieTheme.colors.onSurface)
            ArchieButton("History", { onHistory(); onDismiss() }, style = ButtonStyle.Text)
        }
        Column(Modifier.weight(1f, fill = false).verticalScroll(rememberScrollState())) {
            ListSectionHeader("Open now")
            if (items.isEmpty()) {
                Text(
                    "No open sessions. Start one below.",
                    Modifier.padding(horizontal = 16.dp, vertical = 8.dp),
                    style = ArchieTheme.typography.bodyMedium,
                    color = ArchieTheme.colors.onSurfaceVariant,
                )
            }
            for (item in items) {
                key(item.key) { SwipeToCloseRow(item, item.key == active, { onSelect(item.key); onDismiss() }, onRequestClose) }
            }
        }
        Row(Modifier.fillMaxWidth().padding(top = 14.dp, start = 4.dp, end = 4.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            ArchieButton("New Archie chat", { onNewArchie(); onDismiss() }, Modifier.weight(1f), ButtonStyle.Tonal, icon = ArchieIcons.AddComment)
            ArchieButton("New agent session", { onNewAgent(); onDismiss() }, Modifier.weight(1f), ButtonStyle.Outlined, icon = ArchieIcons.Terminal)
        }
    }
}

/** A switcher row that closes on a horizontal swipe either way; the row snaps back while a confirmation asks. */
@Composable
private fun SwipeToCloseRow(
    item: WorkspaceItem,
    selected: Boolean,
    onClick: () -> Unit,
    onRequestClose: (WorkspaceItem) -> Unit,
) {
    val state = rememberSwipeToDismissBoxState()
    val scope = rememberCoroutineScope()
    SwipeToDismissBox(
        state = state,
        modifier = Modifier.testTag("switcher-swipe"),
        backgroundContent = { SwipeBackground(state.dismissDirection) },
        onDismiss = {
            onRequestClose(item)
            scope.launch { state.reset() }
        },
    ) {
        ArchieListItem(
            item.title,
            onClick = onClick,
            modifier = Modifier
                .background(ArchieTheme.colors.surfaceContainerLow, RoundedCornerShape(20.dp))
                .testTag("switcher-row")
                .semantics { customActions = listOf(CustomAccessibilityAction("Close ${item.title}") { onRequestClose(item); true }) },
            size = ListItemSize.Large,
            selected = selected,
            leading = {
                if (item.kind == ItemKind.ARCHIE) ListLeadingTile { ArchieMark(size = 40.dp) } else ListLeadingTile(item.kind.icon())
            },
            supporting = {
                item.provider?.let { ProviderChip(it.label) }
                item.status.toLive()?.let { StatusIndicator(it) }
                Text(item.detail, maxLines = 1)
            },
            trailing = {
                ArchieIconButton(ArchieIcons.Close, "Close ${item.title}", { onRequestClose(item) })
            },
        )
    }
}

@Composable
private fun SwipeBackground(direction: SwipeToDismissBoxValue) {
    val c = ArchieTheme.colors
    if (direction == SwipeToDismissBoxValue.Settled) return
    Box(
        Modifier.fillMaxSize().background(c.errorContainer, RoundedCornerShape(20.dp)).padding(horizontal = 20.dp),
        contentAlignment = if (direction == SwipeToDismissBoxValue.StartToEnd) Alignment.CenterStart else Alignment.CenterEnd,
    ) {
        ArchieIcon(ArchieIcons.Close, null, tint = c.onErrorContainer)
    }
}
