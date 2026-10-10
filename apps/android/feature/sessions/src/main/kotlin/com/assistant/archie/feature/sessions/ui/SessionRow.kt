package com.assistant.archie.feature.sessions.ui

import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.RowScope
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.ProvideTextStyle
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.semantics.CustomAccessibilityAction
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.customActions
import androidx.compose.ui.semantics.selected
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.assistant.core.data.ItemKind
import com.assistant.core.data.TabStatus
import com.assistant.core.design.components.ArchieDropdownMenu
import com.assistant.core.design.components.ArchieIconButton
import com.assistant.core.design.components.ArchieMark
import com.assistant.core.design.components.ArchieMenuItem
import com.assistant.core.design.components.ListLeadingIcon
import com.assistant.core.design.components.LiveStatus
import com.assistant.core.design.icons.ArchieIcons
import com.assistant.core.design.theme.ArchieTheme

/* Shared pieces of the session lists (drawer, list pane, History screen, switcher). */

/** One action of a row's menu (long press, or the ⋮ button on the History screen). */
data class RowAction(val label: String, val icon: ImageVector, val onSelect: () -> Unit, val destructive: Boolean = false)

/**
 * A history / list row with a menu (web `ActionRow`, inv02 F-19): the row is one button; a long
 * press (or the accessibility action, or the optional ⋮ button) opens the row menu. Same look as
 * the design system's list item (mockup `.li`). No animation of its own: per-row infinite
 * transitions were a cost on low-end devices (inv03 §1.5).
 */
@OptIn(ExperimentalFoundationApi::class)
@Composable
internal fun SessionRow(
    headline: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    twoLine: Boolean = false,
    selected: Boolean = false,
    leading: @Composable () -> Unit,
    supporting: (@Composable RowScope.() -> Unit)? = null,
    trailing: (@Composable RowScope.() -> Unit)? = null,
    actions: List<RowAction> = emptyList(),
    menuLabel: String = "More actions for “$headline”",
    showMenuButton: Boolean = false,
) {
    val c = ArchieTheme.colors
    var menuOpen by remember { mutableStateOf(false) }
    val shape = RoundedCornerShape(if (twoLine) 30.dp else 24.dp)
    val fg = if (selected) c.onSecondaryContainer else c.onSurface
    val sub = if (selected) c.onSecondaryContainer else c.onSurfaceVariant
    Box {
        Row(
            modifier
                .fillMaxWidth()
                .heightIn(min = if (twoLine) 60.dp else 48.dp)
                .clip(shape)
                .then(if (selected) Modifier.background(c.secondaryContainer) else Modifier)
                .combinedClickable(
                    role = Role.Button,
                    onClick = onClick,
                    onLongClick = if (actions.isNotEmpty()) ({ menuOpen = true }) else null,
                    onLongClickLabel = if (actions.isNotEmpty()) menuLabel else null,
                )
                .semantics {
                    this.selected = selected
                    if (actions.isNotEmpty()) {
                        customActions = listOf(CustomAccessibilityAction(menuLabel) { menuOpen = true; true })
                    }
                }
                .padding(start = 16.dp, end = if (showMenuButton) 4.dp else 12.dp, top = 6.dp, bottom = 6.dp),
            horizontalArrangement = Arrangement.spacedBy(12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            CompositionLocalProvider(LocalContentColor provides fg) {
                leading()
                Column(Modifier.weight(1f)) {
                    Text(headline, style = ArchieTheme.typography.bodyMedium, maxLines = 1, overflow = TextOverflow.Ellipsis)
                    if (supporting != null) {
                        CompositionLocalProvider(LocalContentColor provides sub) {
                            ProvideTextStyle(ArchieTheme.typography.bodySmall) {
                                Row(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.CenterVertically, content = supporting)
                            }
                        }
                    }
                }
                if (trailing != null) {
                    CompositionLocalProvider(LocalContentColor provides sub) {
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically, content = trailing)
                    }
                }
                if (showMenuButton && actions.isNotEmpty()) {
                    ArchieIconButton(ArchieIcons.MoreVert, menuLabel, { menuOpen = true }, size = 40.dp, iconSize = 20.dp)
                }
            }
        }
        if (actions.isNotEmpty()) {
            Box(Modifier.align(Alignment.BottomEnd)) {
                ArchieDropdownMenu(menuOpen, onDismissRequest = { menuOpen = false }) {
                    for (a in actions) {
                        ArchieMenuItem(a.label, { menuOpen = false; a.onSelect() }, icon = a.icon, destructive = a.destructive)
                    }
                }
            }
        }
    }
}

/** 24 dp leading slot: the Archie mark or the item's type icon. */
@Composable
internal fun KindLeading(kind: ItemKind, markSize: androidx.compose.ui.unit.Dp = 24.dp) {
    if (kind == ItemKind.ARCHIE) ArchieMark(size = markSize) else ListLeadingIcon(kind.icon())
}

/** History rows: a 20 dp Archie mark (mockup (b)) or the terminal icon. */
@Composable
internal fun HistoryLeading(isArchie: Boolean) {
    if (isArchie) Box(Modifier.padding(2.dp)) { ArchieMark(size = 20.dp) } else ListLeadingIcon(ArchieIcons.Terminal)
}

internal fun TabStatus.toLive(): LiveStatus? = when (this) {
    TabStatus.IDLE -> LiveStatus.Idle
    TabStatus.WORKING, TabStatus.CONNECTING -> LiveStatus.Working
    TabStatus.NEEDS_YOU -> LiveStatus.NeedsYou
    TabStatus.DISCONNECTED -> LiveStatus.Disconnected
    TabStatus.STOPPED -> LiveStatus.Off
    TabStatus.NONE -> null
}

internal fun ItemKind.icon(): ImageVector = when (this) {
    ItemKind.ARCHIE -> ArchieIcons.Forum
    ItemKind.AGENT -> ArchieIcons.Terminal
    ItemKind.MEMORY -> ArchieIcons.Description
    ItemKind.VISUAL -> ArchieIcons.BarChart
}
