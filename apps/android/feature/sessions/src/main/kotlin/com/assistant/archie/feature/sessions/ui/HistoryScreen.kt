package com.assistant.archie.feature.sessions.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.WindowInsetsSides
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.only
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.Immutable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.assistant.archie.feature.sessions.SessionsIntent
import com.assistant.core.data.HistoryGroup
import com.assistant.core.data.ItemKey
import com.assistant.core.data.WorkspaceItem
import com.assistant.core.design.components.ArchieButton
import com.assistant.core.design.components.ArchieIconButton
import com.assistant.core.design.components.ArchieTopAppBar
import com.assistant.core.design.components.ButtonStyle
import com.assistant.core.design.icons.ArchieIcon
import com.assistant.core.design.icons.ArchieIcons
import com.assistant.core.design.theme.ArchieTheme

/** What the History screen and the list sections render (mapped by the shell from its state). */
@Immutable
data class HistoryListState(
    val items: List<WorkspaceItem> = emptyList(),
    val active: ItemKey? = null,
    val groups: List<HistoryGroup> = emptyList(),
    val loading: Boolean = false,
    val error: String? = null,
    val query: String = "",
)

/**
 * The History screen (IA §5: the switcher's "History"; replaces the old "Conversations" screen,
 * inv03 §1.5): search, Open now, then every past conversation by date with its stamp in local time,
 * a ⋮ menu per row (Rename, Duplicate, Delete), Refresh, and the two "new" actions when empty.
 * Opening a row returns to the workspace ([onOpened]).
 */
@Composable
fun HistoryScreen(
    state: HistoryListState,
    onQuery: (String) -> Unit,
    onSelect: (ItemKey) -> Unit,
    onIntent: (SessionsIntent) -> Unit,
    onRefresh: () -> Unit,
    onBack: () -> Unit,
    onOpened: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val c = ArchieTheme.colors
    val routed: (SessionsIntent) -> Unit = { i ->
        onIntent(i)
        if (i is SessionsIntent.OpenHistory || i is SessionsIntent.NewArchie || i is SessionsIntent.NewAgent) onOpened()
    }
    Column(
        modifier
            .fillMaxSize()
            .background(c.surface)
            .windowInsetsPadding(WindowInsets.safeDrawing.only(WindowInsetsSides.Top + WindowInsetsSides.Horizontal))
            .testTag("history-screen"),
    ) {
        ArchieTopAppBar(
            title = "History",
            navigationIcon = { ArchieIconButton(ArchieIcons.ArrowBack, "Back", onBack) },
        ) {
            ArchieIconButton(ArchieIcons.Refresh, "Refresh", onRefresh)
        }
        Box(Modifier.fillMaxWidth().padding(horizontal = 12.dp), contentAlignment = Alignment.TopCenter) {
            Column(Modifier.widthIn(max = 840.dp).fillMaxSize()) {
                SessionSearchField(state.query, onQuery, "Search conversations")
                val hasRows = state.groups.isNotEmpty() || state.items.isNotEmpty()
                LazyColumn(
                    Modifier.weight(1f).padding(top = 2.dp).testTag("history-list"),
                    contentPadding = PaddingValues(bottom = 24.dp),
                ) {
                    openNowSection(
                        state.items, state.active, ListDensity.TwoLine,
                        onSelect = { onSelect(it); onOpened() },
                        onIntent = routed,
                    )
                    historySection(state.groups, ListDensity.TwoLine, routed, showMenuButton = true)
                    historyStatus(hasRows, state.loading, state.error, state.query)
                    if (!hasRows && !state.loading && state.query.isBlank()) {
                        item(key = "new", contentType = "new") {
                            Row(Modifier.padding(horizontal = 12.dp, vertical = 8.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                ArchieButton("New Archie chat", { routed(SessionsIntent.NewArchie) }, style = ButtonStyle.Tonal, icon = ArchieIcons.AddComment)
                                ArchieButton("New agent session", { routed(SessionsIntent.NewAgent) }, style = ButtonStyle.Outlined, icon = ArchieIcons.Terminal)
                            }
                        }
                    }
                }
            }
        }
    }
}

/** The search pill as an editable field (mockup `.search`, 52 dp, surface-container-high). */
@Composable
fun SessionSearchField(value: String, onValueChange: (String) -> Unit, placeholder: String, modifier: Modifier = Modifier) {
    val c = ArchieTheme.colors
    val style = ArchieTheme.typography.bodyLarge.copy(letterSpacing = 0.sp, color = c.onSurface)
    Row(
        modifier
            .fillMaxWidth()
            .height(52.dp)
            .background(c.surfaceContainerHigh, RoundedCornerShape(26.dp))
            .padding(start = 16.dp, end = 4.dp),
        horizontalArrangement = Arrangement.spacedBy(12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        ArchieIcon(ArchieIcons.Search, null, tint = c.onSurfaceVariant)
        BasicTextField(
            value = value,
            onValueChange = onValueChange,
            modifier = Modifier.weight(1f).semantics { contentDescription = placeholder },
            singleLine = true,
            textStyle = style,
            cursorBrush = SolidColor(c.primary),
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
            decorationBox = { inner ->
                Box(contentAlignment = Alignment.CenterStart) {
                    if (value.isEmpty()) Text(placeholder, style = style, color = c.onSurfaceVariant, maxLines = 1)
                    inner()
                }
            },
        )
        if (value.isNotEmpty()) ArchieIconButton(ArchieIcons.Close, "Clear search", { onValueChange("") }, size = 40.dp)
    }
}
