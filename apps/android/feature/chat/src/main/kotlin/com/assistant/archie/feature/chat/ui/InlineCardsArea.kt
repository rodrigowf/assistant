package com.assistant.archie.feature.chat.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.assistant.archie.feature.chat.ChatAction
import com.assistant.archie.feature.chat.ConversationUiMapper
import com.assistant.archie.feature.chat.InlineCardUi
import com.assistant.archie.feature.chat.model.BasicToolCatalog
import com.assistant.core.design.components.InlineCard
import com.assistant.core.design.components.InlineCardAction
import com.assistant.core.design.components.InlineCardKind
import com.assistant.core.design.icons.ArchieIcons
import com.assistant.core.markdown.ui.Markdown
import com.assistant.core.markdown.ui.MarkdownStyle
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.LocalTextStyle
import androidx.compose.foundation.layout.padding
import kotlinx.collections.immutable.ImmutableList
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/**
 * Cards above the composer (IA §6): permission (Approve / Reject / type to give feedback), agent
 * approvals (PM-5), stall (Interrupt), errors (Retry + dismiss + detail), gap (Reload). A session
 * closed on the server closes its view instead (spec 12 OPEN-3). Never pinned, never modal, never an entry.
 */
@Composable
fun InlineCardsArea(
    cards: ImmutableList<InlineCardUi>,
    onAction: (ChatAction) -> Unit,
    onLink: (String) -> Unit,
    modifier: Modifier = Modifier,
) {
    if (cards.isEmpty()) return
    Column(modifier, verticalArrangement = Arrangement.spacedBy(10.dp)) {
        for (card in cards) {
            val m = Modifier.testTag("card:${card.id}")
            when (card) {
                is InlineCardUi.Permission -> PermissionCard(card, onAction, onLink, m)
                is InlineCardUi.AgentApprovalCard -> AgentApprovalCard(card, onAction, m)
                is InlineCardUi.Stall -> InlineCard(
                    InlineCardKind.Stall,
                    ConversationUiMapper.stallTitle(card.toolName, card.elapsedSeconds),
                    m,
                    body = { Text(ConversationUiMapper.stallBody(card.toolName, card.elapsedSeconds)) },
                    actions = {
                        InlineCardAction("Keep waiting", { onAction(ChatAction.DismissCard("stall")) }, primary = false)
                        InlineCardAction("Interrupt", { onAction(ChatAction.Stop) }, primary = true)
                    },
                )
                is InlineCardUi.Error -> ErrorCard(card, onAction, m)
                InlineCardUi.GapPossible -> InlineCard(
                    InlineCardKind.Error,
                    "Some messages may be missing",
                    m,
                    icon = ArchieIcons.Sync,
                    onDismiss = { onAction(ChatAction.DismissCard("gap")) },
                    body = { Text("The live stream skipped ahead. Reload to fetch the full conversation.") },
                    actions = { InlineCardAction("Reload", { onAction(ChatAction.Reload) }, primary = true, icon = ArchieIcons.Refresh) },
                )
            }
        }
    }
}

@Composable
private fun PermissionCard(card: InlineCardUi.Permission, onAction: (ChatAction) -> Unit, onLink: (String) -> Unit, modifier: Modifier) {
    val b = card.block
    val plan = (b.toolInput["plan"] as? JsonPrimitive)?.takeIf { it.isString }?.content
    InlineCard(
        InlineCardKind.Permission,
        if (plan != null) "Plan ready: exit plan mode?" else "${b.toolName} wants to run",
        modifier,
        onDismiss = { onAction(ChatAction.DismissCard(card.id)) },
        hint = "Or type below to give feedback",
        body = {
            if (plan != null) {
                Column(Modifier.heightIn(max = 240.dp).verticalScroll(rememberScrollState())) {
                    Markdown(plan, style = cardMarkdownStyle(), onLinkClick = onLink, cacheId = "perm-plan:${b.requestId}")
                }
            } else {
                Text(inputSummary(b.toolName, b.toolInput))
            }
        },
        actions = {
            // One click only; the card closes when the block leaves `pending` (PM-3), never before.
            InlineCardActionGate(!card.answered) {
                InlineCardAction("Reject", { onAction(ChatAction.Permission(b.requestId, allow = false)) }, primary = false)
                InlineCardAction("Approve", { onAction(ChatAction.Permission(b.requestId, allow = true)) }, primary = true)
            }
        },
    )
}

@Composable
private fun AgentApprovalCard(
    card: InlineCardUi.AgentApprovalCard,
    onAction: (ChatAction) -> Unit,
    modifier: Modifier,
) {
    val a = card.approval
    val plan = (a.toolInput["plan"] as? JsonPrimitive)?.takeIf { it.isString }?.content
    InlineCard(
        InlineCardKind.Permission,
        if (plan != null) "An agent finished planning" else ConversationUiMapper.agentApprovalTitle(a),
        modifier,
        onDismiss = { onAction(ChatAction.DismissCard(card.id)) },
        body = {
            if (plan != null) {
                Column(Modifier.heightIn(max = 240.dp).verticalScroll(rememberScrollState())) {
                    Text("It asks to exit plan mode:")
                    Markdown(plan, Modifier.padding(top = 6.dp), style = cardMarkdownStyle(), onLinkClick = {}, cacheId = "agent-plan:${a.localId}:${a.requestId}")
                }
            } else {
                Text("Agent session ${a.localId.take(8)} · " + inputSummary(a.toolName, a.toolInput))
            }
        },
        actions = {
            // Answered from here whether or not the agent's view is open (REST, §6.9).
            InlineCardActionGate(!card.answered) {
                InlineCardAction("Reject", { onAction(ChatAction.AgentApproval(a.localId, a.requestId, false)) }, primary = false)
                InlineCardAction("Approve", { onAction(ChatAction.AgentApproval(a.localId, a.requestId, true)) }, primary = true)
            }
        },
    )
}

@Composable
private fun ErrorCard(card: InlineCardUi.Error, onAction: (ChatAction) -> Unit, modifier: Modifier) {
    var details by rememberSaveable(card.id) { mutableStateOf(false) }
    InlineCard(
        InlineCardKind.Error,
        card.title,
        modifier,
        onDismiss = { onAction(ChatAction.DismissCard(card.id)) },
        body = { Text(if (details && card.detail != null) "${card.body}\n\n${card.detail}" else card.body) },
        actions = {
            if (card.detail != null) InlineCardAction(if (details) "Hide details" else "Details", { details = !details }, primary = false)
            card.retry?.let { k -> InlineCardAction("Retry", { onAction(ChatAction.Retry(k, card.id)) }, primary = true) }
        },
    )
}

/** Shows the buttons only until answered; afterwards a short "Sent" state (buttons disabled, §6.9). */
@Composable
private fun InlineCardActionGate(enabled: Boolean, content: @Composable () -> Unit) {
    if (enabled) content() else Text("Answer sent…")
}

/** Markdown in a card's tone (the card sets the content color and body text style). */
@Composable
private fun cardMarkdownStyle(): MarkdownStyle {
    val fg = LocalContentColor.current
    val body = LocalTextStyle.current
    return MarkdownStyle.fromTheme().copy(body = body, textColor = fg, mutedColor = fg, blockSpacing = 4.dp, itemSpacing = 0.dp)
}

/** One readable line of a tool input for a permission prompt. */
internal fun inputSummary(toolName: String, input: JsonObject): String {
    val first = input.entries.firstOrNull { (_, v) -> v is JsonPrimitive && v.isString }
    val v = first?.let { (it.value as JsonPrimitive).content.lineSequence().firstOrNull() }
    return when {
        v == null -> toolName
        first.key.contains("path") -> BasicToolCatalog.shortPath(v)
        else -> v.take(160)
    }
}
