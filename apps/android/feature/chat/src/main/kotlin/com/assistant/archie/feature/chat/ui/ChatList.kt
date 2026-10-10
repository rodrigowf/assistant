package com.assistant.archie.feature.chat.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.assistant.archie.feature.chat.ChatAction
import com.assistant.archie.feature.chat.model.ChatItem
import com.assistant.archie.feature.chat.model.ChatItem.Gap
import com.assistant.archie.feature.chat.model.ChatItem.GroupPosition
import com.assistant.core.conversation.NoticeKind
import com.assistant.core.conversation.PermissionState
import com.assistant.core.conversation.UserOrigin
import com.assistant.core.design.Corner
import com.assistant.core.design.components.ArchieButton
import com.assistant.core.design.components.ArchieFab
import com.assistant.core.design.components.ButtonStyle
import com.assistant.core.design.components.FabSize
import com.assistant.core.design.components.Spinner
import com.assistant.core.design.components.SystemLine
import com.assistant.core.design.components.ToolGroupHeader
import com.assistant.core.design.icons.ArchieIcon
import com.assistant.core.design.icons.ArchieIcons
import com.assistant.core.design.theme.ArchieTheme
import com.assistant.core.markdown.ui.Markdown
import com.assistant.core.markdown.ui.MarkdownBlock
import com.assistant.core.markdown.ui.MarkdownStyle
import com.assistant.core.model.SessionKind
import kotlinx.collections.immutable.ImmutableList
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.filter
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonPrimitive

/** Width of the message column on large screens (IA §6). */
val MessageColumnMaxWidth: Dp = 840.dp

/**
 * The conversation list (spec 14 §3.1, §3.6): `LazyColumn(reverseLayout = true)` over the reversed
 * items, so the newest item is index 0 and streaming growth stays pinned without per-delta scroll calls.
 * Keys are the items' spec-12 keys; nothing animates while streaming. Load-older fires when the oldest
 * visible item is within 3 of the end (the ViewModel guards repeats on the oldest entry id).
 *
 * New items below the newest one (a new markdown block, a tool card) need one more step: LazyList keeps
 * its position on the first visible item's **key**, so without it the list stays on the old item and the
 * new one lands below the fold. [FollowNewest] re-anchors on index 0 when the list was at the bottom; a
 * user who scrolled up keeps the key anchoring, so the text they are reading stays where it is.
 */
@Composable
fun ChatList(
    items: ImmutableList<ChatItem>,
    kind: SessionKind,
    toolCards: ToolCardRenderer,
    listState: LazyListState,
    onAction: (ChatAction) -> Unit,
    onLongPress: (entryId: String) -> Unit,
    onLink: (String) -> Unit,
    modifier: Modifier = Modifier,
    linkOrigin: String? = null,
) {
    // LNK-5 (spec 12 §9.4): printed visualization / memory paths are links; the host resolves them.
    val style = MarkdownStyle.fromTheme().copy(autoLinkPaths = true, linkOrigin = linkOrigin)
    val reversed = remember(items) { items.asReversed() }
    FollowNewest(listState, reversed.firstOrNull()?.key)
    LaunchedEffect(listState) {
        snapshotFlow {
            val info = listState.layoutInfo
            val last = info.visibleItemsInfo.lastOrNull()?.index ?: -1
            info.totalItemsCount > 0 && last >= info.totalItemsCount - 1 - 3
        }.distinctUntilChanged().filter { it }.collect { onAction(ChatAction.LoadOlder) }
    }
    Box(modifier.fillMaxSize(), contentAlignment = Alignment.TopCenter) {
        LazyColumn(
            Modifier
                .widthIn(max = MessageColumnMaxWidth)
                .fillMaxSize()
                .testTag("chat-list"),
            state = listState,
            reverseLayout = true,
            contentPadding = PaddingValues(start = 16.dp, end = 16.dp, top = 8.dp, bottom = 12.dp),
        ) {
            items(
                count = reversed.size,
                key = { reversed[it].key },
                contentType = { reversed[it].contentType },
            ) { i ->
                val item = reversed[i]
                ChatItemView(item, kind, style, toolCards, onAction, onLongPress, onLink)
            }
        }
        // Mockup `.fadetop`: the list fades into the surface over its top 32 dp. A gradient overlay,
        // not an offscreen layer over the whole list (that would cost a full-list buffer every frame).
        val surface = ArchieTheme.colors.surface
        Box(
            Modifier
                .align(Alignment.TopCenter)
                .fillMaxWidth()
                .height(32.dp)
                .drawWithContent { drawRect(Brush.verticalGradient(listOf(surface, surface.copy(alpha = 0f)))) },
        )
        JumpToLatest(listState, Modifier.align(Alignment.BottomEnd).padding(end = 16.dp, bottom = 12.dp))
    }
}

/**
 * Keeps a list that is at the bottom there when the newest item changes. Runs in a [SideEffect], after
 * the composition that brings the new items and before the remeasure that applies them, so the state it
 * reads is still the previous layout's. `requestScrollToItem` overrides the key anchoring for exactly
 * that remeasure (no animation, no extra frame). A scroll in progress is the user's and is left alone.
 */
@Composable
private fun FollowNewest(listState: LazyListState, newestKey: Any?) {
    val slop = with(LocalDensity.current) { FollowSlop.roundToPx() }
    val last = remember { arrayOfNulls<Any>(1) }
    SideEffect {
        if (newestKey == last[0]) return@SideEffect
        last[0] = newestKey
        val atBottom = listState.firstVisibleItemIndex == 0 && listState.firstVisibleItemScrollOffset <= slop
        if (atBottom && !listState.isScrollInProgress) listState.requestScrollToItem(0)
    }
}

/** How far above the bottom still counts as "at the bottom" for [FollowNewest]. */
private val FollowSlop: Dp = 24.dp

@Composable
private fun JumpToLatest(listState: LazyListState, modifier: Modifier) {
    val scope = rememberCoroutineScope()
    val away by remember { androidx.compose.runtime.derivedStateOf { listState.firstVisibleItemIndex > 1 } }
    if (away) {
        // User initiated only: nothing animates during streaming (06fe06f).
        ArchieFab(ArchieIcons.ArrowDownward, "Jump to latest", { scope.launch { listState.animateScrollToItem(0) } }, modifier, size = FabSize.Small)
    }
}

@Composable
private fun ChatItemView(
    item: ChatItem,
    kind: SessionKind,
    style: MarkdownStyle,
    toolCards: ToolCardRenderer,
    onAction: (ChatAction) -> Unit,
    onLongPress: (String) -> Unit,
    onLink: (String) -> Unit,
) {
    val top = when (item.gap) {
        Gap.None -> 0.dp
        Gap.Tight -> if (item is ChatItem.MdBlock) style.spacingBefore(item.previous, item.node) else 2.dp
        Gap.Group -> 6.dp
        Gap.Item -> 16.dp
    }
    val m = Modifier.fillMaxWidth().padding(top = top).testTag("item:${item.key}")
    val press = item.entryId?.let { id -> Modifier.longPress { onLongPress(id) } } ?: Modifier
    when (item) {
        is ChatItem.LoadOlder -> Box(m.height(40.dp), contentAlignment = Alignment.Center) {
            if (item.loading) Spinner(size = 18.dp) else ArchieButton("Load older messages", { onAction(ChatAction.LoadOlder) }, style = ButtonStyle.Text)
        }
        ChatItem.HistoryFootnote -> Text(
            "Background updates from agents aren’t kept in history.",
            m.padding(horizontal = 8.dp),
            style = ArchieTheme.typography.labelMedium.copy(letterSpacing = 0.sp),
            color = ArchieTheme.colors.onSurfaceVariant,
            textAlign = TextAlign.Center,
        )
        is ChatItem.UserBubble -> UserBubble(item, m, press)
        is ChatItem.MdBlock -> MarkdownBlock(item.node, m.then(press), style, onLink)
        is ChatItem.Thinking -> ThinkingView(item, m.then(press))
        is ChatItem.ToolGroup -> ToolGroupHeader(
            count = item.count,
            tiles = item.tiles.map { it.category to it.icon },
            summary = item.summary,
            status = item.status.shell(),
            expanded = item.expanded,
            onToggle = { onAction(ChatAction.ToggleGroup(item.key, !item.expanded)) },
            // Mockup `.tg-h { align-self: flex-start }`: the header hugs its content.
            modifier = Modifier.padding(top = top).testTag("item:${item.key}"),
        )
        is ChatItem.ToolCard -> {
            val outer = when (item.position) {
                GroupPosition.Solo -> null
                GroupPosition.First -> RoundedCornerShape(topStart = Corner.Large, topEnd = Corner.Large, bottomStart = Corner.ExtraSmall, bottomEnd = Corner.ExtraSmall)
                GroupPosition.Last -> RoundedCornerShape(topStart = Corner.ExtraSmall, topEnd = Corner.ExtraSmall, bottomStart = Corner.Large, bottomEnd = Corner.Large)
                GroupPosition.Middle -> null
                GroupPosition.Only -> RoundedCornerShape(Corner.Large)
            }
            toolCards.Card(
                item,
                kind,
                { expanded -> onAction(ChatAction.ToggleCard(item.key, expanded)) },
                if (outer != null) m.clip(outer) else m,
            )
        }
        is ChatItem.Permission -> PermissionRecord(item, m, onLink)
        is ChatItem.CompactDivider -> CompactDivider(item, m)
        is ChatItem.Notice -> NoticeLine(item, m)
        is ChatItem.UnmatchedResults -> UnmatchedResults(item, m)
    }
}

/** User prompt (mockup `.u`): tonal bubble, right-aligned, max 80 %, 22/22/6/22 corners. */
@Composable
private fun UserBubble(item: ChatItem.UserBubble, modifier: Modifier, press: Modifier) {
    val c = ArchieTheme.colors
    var expanded by rememberSaveable(item.key) { mutableStateOf(false) }
    val fold = item.lineCount > FOLD_LINES && !expanded
    Row(modifier, horizontalArrangement = Arrangement.End) {
        Box(Modifier.fillMaxWidth(0.8f), contentAlignment = Alignment.CenterEnd) {
            Column(
                press
                    .clip(RoundedCornerShape(topStart = 22.dp, topEnd = 22.dp, bottomEnd = 6.dp, bottomStart = 22.dp))
                    .background(c.surfaceContainerHigh)
                    .padding(horizontal = 16.dp, vertical = 10.dp),
                verticalArrangement = Arrangement.spacedBy(2.dp),
            ) {
                val voice = item.origin == UserOrigin.VOICE
                if (voice || item.origin == UserOrigin.AUDIO) {
                    Row(horizontalArrangement = Arrangement.spacedBy(4.dp), verticalAlignment = Alignment.CenterVertically) {
                        ArchieIcon(ArchieIcons.Mic, null, size = 16.dp, tint = c.onSurfaceVariant)
                        Text(
                            when {
                                item.origin == UserOrigin.AUDIO -> "VOICE MESSAGE"
                                item.live -> "VOICE · LIVE"
                                else -> "VOICE"
                            },
                            style = ArchieTheme.typography.labelSmall.copy(fontSize = 11.sp, lineHeight = 16.sp, letterSpacing = 0.5.sp, fontWeight = FontWeight.W500),
                            color = c.onSurfaceVariant,
                        )
                    }
                }
                val text = item.text.ifEmpty { if (item.origin == UserOrigin.AUDIO) "Voice message" else "" }
                Text(
                    text,
                    Modifier.then(if (fold) Modifier.heightIn(max = 150.dp) else Modifier),
                    style = ArchieTheme.typography.bodyLarge.copy(letterSpacing = 0.2.sp),
                    color = if (item.live || item.pending) c.onSurfaceVariant else c.onSurface,
                    overflow = TextOverflow.Clip,
                )
                if (item.pending) {
                    Text("Sending…", style = ArchieTheme.typography.labelSmall, color = c.onSurfaceVariant)
                }
                if (item.lineCount > FOLD_LINES) {
                    Text(
                        if (expanded) "Show less" else "Show all (${item.lineCount} lines)",
                        Modifier.clickable(role = Role.Button) { expanded = !expanded }.padding(top = 4.dp),
                        style = ArchieTheme.typography.labelLarge,
                        color = c.primary,
                    )
                }
            }
        }
    }
}

/** Thinking: collapsed to "Thought" once done; a muted italic body when opened. */
@Composable
private fun ThinkingView(item: ChatItem.Thinking, modifier: Modifier) {
    val c = ArchieTheme.colors
    var open by rememberSaveable(item.key) { mutableStateOf(false) }
    Column(modifier) {
        Row(
            Modifier
                .clip(RoundedCornerShape(16.dp))
                .clickable(role = Role.Button) { open = !open }
                .semantics { stateDescription = if (open) "Expanded" else "Collapsed" }
                .padding(horizontal = 4.dp, vertical = 6.dp),
            horizontalArrangement = Arrangement.spacedBy(6.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            if (item.streaming) Spinner(size = 14.dp) else ArchieIcon(ArchieIcons.StarShine, null, size = 18.dp, tint = c.onSurfaceVariant)
            Text(if (item.streaming) "Thinking…" else "Thought", style = ArchieTheme.typography.labelLarge, color = c.onSurfaceVariant)
            ArchieIcon(if (open) ArchieIcons.KeyboardArrowUp else ArchieIcons.KeyboardArrowDown, null, size = 18.dp, tint = c.onSurfaceVariant)
        }
        if (open) {
            Row(Modifier.padding(start = 10.dp, top = 4.dp)) {
                Box(Modifier.width(2.dp).heightIn(min = 20.dp).background(c.outlineVariant))
                Text(
                    capOutput(item.text),
                    Modifier.padding(start = 12.dp),
                    style = ArchieTheme.typography.bodyMedium.copy(fontStyle = FontStyle.Italic),
                    color = c.onSurfaceVariant,
                )
            }
        }
    }
}

/** The timeline record of a permission request; ExitPlanMode shows its plan here (PM-1). */
@Composable
private fun PermissionRecord(item: ChatItem.Permission, modifier: Modifier, onLink: (String) -> Unit) {
    val c = ArchieTheme.colors
    val b = item.block
    val plan = (b.toolInput["plan"] as? JsonPrimitive)?.takeIf { it.isString }?.content
    var open by rememberSaveable(item.key) { mutableStateOf(false) }
    val state = when (b.state) {
        PermissionState.PENDING -> "Waiting for approval"
        PermissionState.ALLOWED -> "Approved" + responderSuffix(b.responder)
        PermissionState.DENIED -> (if (b.responder == "system") "Expired" else "Rejected" + responderSuffix(b.responder)) +
            (b.message?.takeIf { it.isNotBlank() && b.responder != "system" }?.let { " · “$it”" } ?: "")
    }
    Column(modifier.clip(RoundedCornerShape(Corner.Large)).background(c.surfaceContainer)) {
        Row(
            Modifier
                .fillMaxWidth()
                .heightIn(min = 48.dp)
                .then(if (plan != null) Modifier.clickable(role = Role.Button) { open = !open } else Modifier)
                .padding(horizontal = 12.dp, vertical = 8.dp),
            horizontalArrangement = Arrangement.spacedBy(12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            val tint = when (b.state) {
                PermissionState.PENDING -> ArchieTheme.extended.warning.color
                PermissionState.ALLOWED -> ArchieTheme.extended.success.color
                PermissionState.DENIED -> c.onSurfaceVariant
            }
            ArchieIcon(if (b.state == PermissionState.PENDING) ArchieIcons.FrontHand else ArchieIcons.Shield, null, size = 20.dp, tint = tint)
            Text(b.toolName, style = ArchieTheme.typography.labelLarge, color = c.onSurface, maxLines = 1)
            Text(
                state,
                Modifier.weight(1f),
                style = ArchieTheme.typography.bodyMedium,
                color = c.onSurfaceVariant,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
            )
            if (plan != null) ArchieIcon(if (open) ArchieIcons.KeyboardArrowUp else ArchieIcons.KeyboardArrowDown, null, size = 20.dp, tint = c.onSurfaceVariant)
        }
        if (open && plan != null) {
            Markdown(plan, Modifier.padding(start = 16.dp, end = 16.dp, bottom = 12.dp), onLinkClick = onLink, cacheId = "plan:${b.requestId}")
        }
    }
}

private fun responderSuffix(r: String?) = when (r) {
    "user" -> " by you"
    "orchestrator" -> " by Archie"
    else -> ""
}

/** "Context compacted" between rules, expandable when there is a summary (inv03 §1.2). */
@Composable
private fun CompactDivider(item: ChatItem.CompactDivider, modifier: Modifier) {
    val c = ArchieTheme.colors
    var open by rememberSaveable(item.key) { mutableStateOf(false) }
    val tokens = if (item.tokensBefore != null && item.tokensAfter != null) " · ${item.tokensBefore} → ${item.tokensAfter} tokens" else ""
    Column(modifier, horizontalAlignment = Alignment.CenterHorizontally) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.weight(1f).height(1.dp).background(c.outlineVariant))
            Row(
                Modifier
                    .padding(horizontal = 8.dp)
                    .clip(RoundedCornerShape(14.dp))
                    .then(if (item.summary.isNotBlank()) Modifier.clickable(role = Role.Button) { open = !open } else Modifier)
                    .padding(horizontal = 8.dp, vertical = 4.dp),
                horizontalArrangement = Arrangement.spacedBy(6.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                ArchieIcon(ArchieIcons.Compress, null, size = 16.dp, tint = c.onSurfaceVariant)
                Text("Context compacted$tokens", style = ArchieTheme.typography.labelMedium, color = c.onSurfaceVariant)
            }
            Box(Modifier.weight(1f).height(1.dp).background(c.outlineVariant))
        }
        if (open) {
            Text(
                item.summary,
                Modifier.padding(top = 8.dp).fillMaxWidth(),
                style = ArchieTheme.typography.bodyMedium,
                color = c.onSurfaceVariant,
            )
        }
    }
}

@Composable
private fun NoticeLine(item: ChatItem.Notice, modifier: Modifier) {
    val c = ArchieTheme.colors
    Box(modifier, contentAlignment = Alignment.Center) {
        when (item.kind) {
            NoticeKind.ERROR -> Row(
                Modifier
                    .clip(RoundedCornerShape(14.dp))
                    .background(c.errorContainer)
                    .padding(horizontal = 12.dp, vertical = 6.dp),
                horizontalArrangement = Arrangement.spacedBy(6.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                ArchieIcon(ArchieIcons.Error, null, size = 16.dp, tint = c.onErrorContainer)
                Text(item.text.ifBlank { "The reply failed" }, style = ArchieTheme.typography.labelMedium, color = c.onErrorContainer)
            }
            NoticeKind.INTERRUPTED -> SystemLine("Interrupted", icon = ArchieIcons.Stop)
            NoticeKind.BACKGROUND -> SystemLine(item.text.ifBlank { "Archie started on its own" }.lineSequence().first(), icon = ArchieIcons.Bolt)
            NoticeKind.COMMAND -> SystemLine(item.text.lineSequence().firstOrNull().orEmpty().take(120), icon = ArchieIcons.Terminal)
            NoticeKind.COMPACTION -> SystemLine("Context compacted", icon = ArchieIcons.Compress)
        }
    }
}

/** R-9: results the reducer could not attach stay visible here, never dropped. */
@Composable
private fun UnmatchedResults(item: ChatItem.UnmatchedResults, modifier: Modifier) {
    val c = ArchieTheme.colors
    var open by rememberSaveable(item.key) { mutableStateOf(false) }
    Column(modifier.clip(RoundedCornerShape(Corner.Large)).background(c.surfaceContainer)) {
        Row(
            Modifier.fillMaxWidth().heightIn(min = 48.dp).clickable(role = Role.Button) { open = !open }.padding(horizontal = 12.dp),
            horizontalArrangement = Arrangement.spacedBy(12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            ArchieIcon(ArchieIcons.Info, null, size = 20.dp, tint = c.onSurfaceVariant)
            Text(
                "Tool output not matched to a call (${item.results.size})",
                Modifier.weight(1f),
                style = ArchieTheme.typography.labelLarge,
                color = c.onSurfaceVariant,
            )
            ArchieIcon(if (open) ArchieIcons.KeyboardArrowUp else ArchieIcons.KeyboardArrowDown, null, size = 20.dp, tint = c.onSurfaceVariant)
        }
        if (open) {
            Column(Modifier.padding(start = 8.dp, end = 8.dp, bottom = 8.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                item.results.forEachIndexed { i, r ->
                    com.assistant.core.design.components.ToolOutput {
                        item.ids[i]?.let { Text(it, style = ArchieTheme.typography.labelSmall) }
                        CompositionLocalProvider(LocalContentColor provides if (r.isError) c.error else LocalContentColor.current) {
                            Text(capOutput(r.output).ifEmpty { "No output" })
                        }
                    }
                }
            }
        }
    }
}

private const val FOLD_LINES = 25

private fun Modifier.longPress(onLongPress: () -> Unit): Modifier =
    pointerInput(onLongPress) { detectTapGestures(onLongPress = { onLongPress() }) }
