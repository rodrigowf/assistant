package com.assistant.archie.feature.sessions.ui

import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyListScope
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Text
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.assistant.archie.feature.sessions.SessionTarget
import com.assistant.archie.feature.sessions.SessionTitles
import com.assistant.archie.feature.sessions.SessionsIntent
import com.assistant.core.data.HistoryGroup
import com.assistant.core.data.ItemKey
import com.assistant.core.data.ItemKind
import com.assistant.core.data.WorkspaceItem
import com.assistant.core.design.components.ListMeta
import com.assistant.core.design.components.ListSectionHeader
import com.assistant.core.design.components.ProviderChip
import com.assistant.core.design.components.StatusIndicator
import com.assistant.core.design.icons.ArchieIcons
import com.assistant.core.design.theme.ArchieTheme
import com.assistant.core.model.SessionSummary

/*
 * The list sections of the drawer (Compact), the Chats list pane (Medium / Expanded) and the History
 * screen (IA §3, §5; mockups phone (b), desktop list pane): "Open now" with live status, then the
 * history in date groups, each row with Rename / Duplicate / Delete.
 */

/** Row density: the drawer is one-line (mockup (b)); the list pane and History screen are two-line. */
enum class ListDensity { OneLine, TwoLine }

/** "Open now": the server's open set, the same on every device (OPEN-1), plus local documents. */
fun LazyListScope.openNowSection(
    items: List<WorkspaceItem>,
    active: ItemKey?,
    density: ListDensity,
    onSelect: (ItemKey) -> Unit,
    onIntent: (SessionsIntent) -> Unit,
) {
    val twoLine = density == ListDensity.TwoLine
    val count = items.size
    if (count == 0) return
    item(key = "open-h", contentType = "header") {
        ListSectionHeader("Open now", trailing = if (twoLine) ({ ListMeta("$count") }) else null)
    }
    items(items, key = { "open-" + it.key.toString() }, contentType = { "open" }) { item ->
        val conversation = item.kind == ItemKind.ARCHIE || item.kind == ItemKind.AGENT
        val target = SessionTarget.of(item)
        SessionRow(
            item.title,
            onClick = { onSelect(item.key) },
            modifier = Modifier.testTag("open-row"),
            twoLine = twoLine,
            selected = item.key == active,
            leading = { KindLeading(item.kind) },
            supporting = if (twoLine) ({
                item.provider?.let { ProviderChip(it.label) }
                val detail = item.detail.removePrefix("Archie · ").replaceFirstChar { c -> c.uppercase() }
                Text(if (item.kind == ItemKind.ARCHIE) "Archie · $detail" else detail, maxLines = 1)
            }) else null,
            trailing = {
                if (!twoLine) item.provider?.let { ProviderChip(it.label) }
                item.status.toLive()?.let { StatusIndicator(it) }
            },
            actions = buildList {
                if (conversation) add(RowAction("Rename", ArchieIcons.Edit, { onIntent(SessionsIntent.RequestRename(target)) }))
                add(RowAction("Close", ArchieIcons.Close, { onIntent(SessionsIntent.RequestClose(item)) }))
                if (conversation) add(RowAction("Delete", ArchieIcons.Delete, { onIntent(SessionsIntent.RequestDelete(target)) }, destructive = true))
            },
        )
    }
}

/** History in date groups (Today / Yesterday / Previous 7 days / Earlier), local time (inv03 §1.5 fix). */
fun LazyListScope.historySection(
    groups: List<HistoryGroup>,
    density: ListDensity,
    onIntent: (SessionsIntent) -> Unit,
    showMenuButton: Boolean = false,
) {
    val twoLine = density == ListDensity.TwoLine
    for (g in groups) {
        item(key = "h-" + g.bucket.name, contentType = "header") { ListSectionHeader(g.bucket.label) }
        items(g.rows, key = { "row-" + it.summary.sdkId }, contentType = { "history" }) { row ->
            val s = row.summary
            val title = SessionTitles.conversationTitle(s.title, s.isOrchestrator)
            SessionRow(
                title,
                onClick = { onIntent(SessionsIntent.OpenHistory(s)) },
                modifier = Modifier.testTag("history-row"),
                twoLine = twoLine,
                leading = { HistoryLeading(s.isOrchestrator) },
                supporting = if (twoLine) ({ Text(historySupporting(s), maxLines = 1) }) else null,
                trailing = { if (row.meta.isNotEmpty()) ListMeta(row.meta) },
                actions = historyActions(s, onIntent),
                menuLabel = "More actions for “$title”",
                showMenuButton = showMenuButton,
            )
        }
    }
}

/** Loading / error / empty / no match line under the lists. */
fun LazyListScope.historyStatus(
    hasRows: Boolean,
    loading: Boolean,
    error: String?,
    query: String,
) {
    val text = when {
        error != null -> "Couldn't load conversations: $error"
        hasRows -> return
        loading -> "Loading conversations…"
        query.isNotBlank() -> "No conversations match “${query.trim()}”"
        else -> "No conversations yet"
    }
    item(key = "h-status", contentType = "status") {
        Text(
            text,
            Modifier.padding(horizontal = 16.dp, vertical = 12.dp).testTag("history-status"),
            style = ArchieTheme.typography.bodyMedium,
            color = if (error != null) ArchieTheme.colors.error else ArchieTheme.colors.onSurfaceVariant,
        )
    }
}

/** "Archie · 12 messages" / "Claude · 6 messages". */
internal fun historySupporting(s: SessionSummary): String {
    val who = if (s.isOrchestrator) "Archie" else s.provider?.label ?: "Agent"
    val n = s.messageCount
    return "$who · $n ${if (n == 1) "message" else "messages"}"
}

internal fun historyActions(s: SessionSummary, onIntent: (SessionsIntent) -> Unit): List<RowAction> {
    val t = SessionTarget.of(s)
    return listOf(
        RowAction("Rename", ArchieIcons.Edit, { onIntent(SessionsIntent.RequestRename(t)) }),
        RowAction("Duplicate", ArchieIcons.ContentCopy, { onIntent(SessionsIntent.Duplicate(t)) }),
        RowAction("Delete", ArchieIcons.Delete, { onIntent(SessionsIntent.RequestDelete(t)) }, destructive = true),
    )
}
