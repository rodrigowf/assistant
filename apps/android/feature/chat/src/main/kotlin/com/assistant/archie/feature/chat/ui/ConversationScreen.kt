package com.assistant.archie.feature.chat.ui

import android.net.Uri
import android.provider.OpenableColumns
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBars
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.Immutable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.assistant.archie.feature.chat.ChatAction
import com.assistant.archie.feature.chat.ChatEffect
import com.assistant.archie.feature.chat.ConversationUiState
import com.assistant.archie.feature.chat.ConversationViewModel
import com.assistant.archie.feature.chat.VoiceUi
import com.assistant.core.design.components.ArchieConfirmDialog
import com.assistant.core.design.components.ArchieListItem
import com.assistant.core.design.components.ArchieSnackbarHost
import com.assistant.core.design.components.EmptyState
import com.assistant.core.design.components.ListLeadingIcon
import com.assistant.core.design.components.Spinner
import com.assistant.core.design.components.SuggestionChip
import com.assistant.core.design.icons.ArchieIcons
import com.assistant.core.design.theme.ArchieTheme
import com.assistant.core.model.SessionRef
import com.assistant.core.network.UploadSource
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.layout.boundsInRoot
import androidx.compose.ui.layout.onGloballyPositioned
import okio.source
import java.util.Calendar

/** Navigation and system hooks the host (the shell, B-03/B-09) provides. */
@Immutable
data class ConversationCallbacks(
    /**
     * Raw href from markdown, including printed paths (spec 12 §9.4 LNK-5); the host opens internal
     * links in the app. null = open with the platform URI handler.
     */
    val onLink: ((String) -> Unit)? = null,
    /** The backend origin, so a server URL printed in backticks auto-links too (spec 12 LNK-3/LNK-5). */
    val linkOrigin: String? = null,
    /** A fork result to open, focused (§6.5). */
    val onOpenSession: (SessionRef) -> Unit = {},
    /** The empty state's "Start an agent session" suggestion (§6.10). */
    val onNewAgentSession: () -> Unit = {},
)

/** An empty-conversation suggestion chip (IA §6). [text] null = [ConversationCallbacks.onNewAgentSession]. */
@Immutable
data class Suggestion(val label: String, val icon: ImageVector, val text: String?)

val DefaultSuggestions = listOf(
    Suggestion("Plan the living-room TV setup", ArchieIcons.Tv, "Plan the living-room TV setup for movie night."),
    Suggestion("This week's energy use", ArchieIcons.Bolt, "How much energy did we use this week?"),
    Suggestion("What do I know about voice?", ArchieIcons.Book2, "What do I know about the voice subsystem?"),
    Suggestion("Start an agent session", ArchieIcons.Terminal, null),
)

/**
 * The conversation screen bound to its ViewModel (spec 14 §2.1). The shell hosts it as a workspace
 * item's content; the top app bar (title, status subtitle, ⋮) is the shell's.
 */
@Composable
fun ConversationScreen(
    viewModel: ConversationViewModel,
    modifier: Modifier = Modifier,
    toolCards: ToolCardRenderer = DefaultToolCardRenderer,
    callbacks: ConversationCallbacks = ConversationCallbacks(),
    suggestions: List<Suggestion> = DefaultSuggestions,
) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val draft by viewModel.draftText.collectAsStateWithLifecycle()
    // Only while the dock is up: observing the level is what turns the transport's meters on.
    val voiceLevel = if (state.voice is VoiceUi.Active) viewModel.voiceLevel.collectAsStateWithLifecycle() else null
    val snackbar = remember { SnackbarHostState() }
    LaunchedEffect(viewModel) {
        viewModel.effectFlow.collect { e ->
            when (e) {
                is ChatEffect.Snackbar -> snackbar.showSnackbar(e.message)
                is ChatEffect.OpenSession -> callbacks.onOpenSession(e.ref)
            }
        }
    }
    val context = LocalContext.current
    val picker = rememberLauncherForActivityResult(ActivityResultContracts.GetContent()) { uri: Uri? ->
        if (uri != null) uploadSource(context, uri)?.let { viewModel.onAction(ChatAction.Upload(it)) }
    }
    ConversationContent(
        state = state,
        draft = draft,
        onAction = viewModel::onAction,
        modifier = modifier,
        toolCards = toolCards,
        callbacks = callbacks,
        textOf = viewModel::textOf,
        onAttach = { picker.launch("*/*") },
        snackbar = snackbar,
        suggestions = suggestions,
        voiceLevel = voiceLevel?.let { l -> { l.value } },
    )
}

/** A streamed upload source over a content URI (never read whole into memory, A-3.4). */
private fun uploadSource(context: android.content.Context, uri: Uri): UploadSource? {
    val cr = context.contentResolver
    var name = uri.lastPathSegment ?: "file"
    var size = -1L
    cr.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE), null, null, null)?.use { c ->
        if (c.moveToFirst()) {
            c.getString(0)?.let { name = it }
            if (!c.isNull(1)) size = c.getLong(1)
        }
    }
    return UploadSource(name, cr.getType(uri), size) { cr.openInputStream(uri)!!.source() }
}

/**
 * The stateless screen (goldens and UI tests drive it directly): list or empty state, the busy
 * overlay, inline cards, the composer or voice dock, and the long-press message actions.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ConversationContent(
    state: ConversationUiState,
    draft: String,
    onAction: (ChatAction) -> Unit,
    modifier: Modifier = Modifier,
    toolCards: ToolCardRenderer = DefaultToolCardRenderer,
    callbacks: ConversationCallbacks = ConversationCallbacks(),
    textOf: (String) -> String? = { null },
    onAttach: () -> Unit = {},
    listState: LazyListState = rememberLazyListState(),
    snackbar: SnackbarHostState = remember { SnackbarHostState() },
    suggestions: List<Suggestion> = DefaultSuggestions,
    hour: Int = remember { Calendar.getInstance().get(Calendar.HOUR_OF_DAY) },
    clock: () -> Long = System::currentTimeMillis,
    /** The voice orb's live level (read only by the orb's frame loop); null keeps its pulse. */
    voiceLevel: (() -> Float?)? = null,
) {
    val uri = LocalUriHandler.current
    val onLink: (String) -> Unit = callbacks.onLink ?: { href -> runCatching { uri.openUri(href) } }
    var actionsFor by rememberSaveable { mutableStateOf<String?>(null) }
    var selectFor by rememberSaveable { mutableStateOf<String?>(null) }
    var confirmRewind by rememberSaveable { mutableStateOf<String?>(null) }

    Column(modifier.fillMaxSize().background(ArchieTheme.colors.surface).testTag("conversation")) {
        Box(Modifier.weight(1f).fillMaxWidth()) {
            when {
                !state.loaded && state.empty -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { Spinner(size = 24.dp) }
                state.empty && state.isArchie -> ArchieEmpty(hour, suggestions, onAction, callbacks)
                state.empty -> Text(
                    "Send a message to start.",
                    Modifier.align(Alignment.Center).padding(24.dp),
                    style = ArchieTheme.typography.bodyMedium,
                    color = ArchieTheme.colors.onSurfaceVariant,
                    textAlign = TextAlign.Center,
                )
                else -> ChatList(
                    items = state.items,
                    kind = state.kind,
                    toolCards = toolCards,
                    listState = listState,
                    onAction = onAction,
                    onLongPress = { actionsFor = it },
                    onLink = onLink,
                    linkOrigin = callbacks.linkOrigin,
                )
            }
            state.busyOverlay?.let { BusyOverlay(it) }
            ArchieSnackbarHost(snackbar, Modifier.align(Alignment.BottomCenter).widthIn(max = MessageColumnMaxWidth))
        }
        // The floating voice controls (over other views) stay above this area: it reports its bounds.
        val composerBounds = LocalComposerBounds.current
        val boundsOwner = remember { Any() }
        DisposableEffect(composerBounds) { onDispose { composerBounds?.clear(boundsOwner) } }
        Box(
            Modifier
                .fillMaxWidth()
                .then(if (composerBounds != null) Modifier.onGloballyPositioned { composerBounds.report(boundsOwner, it.boundsInRoot()) } else Modifier)
                .windowInsetsPadding(WindowInsets.navigationBars)
                .imePadding()
                .padding(start = 12.dp, end = 12.dp, top = 4.dp, bottom = 12.dp),
            contentAlignment = Alignment.Center,
        ) {
            Column(Modifier.widthIn(max = MessageColumnMaxWidth - 24.dp).fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                InlineCardsArea(state.cards, onAction, onLink)
                ComposerArea(state.composer, state.voice, state.counters, state.queue, draft, onAction, onAttach, clock = clock, voiceLevel = voiceLevel)
            }
        }
    }

    actionsFor?.let { id ->
        ModalBottomSheet(onDismissRequest = { actionsFor = null }, containerColor = ArchieTheme.colors.surfaceContainerLow) {
            val clipboard = LocalClipboardManager.current
            Column(Modifier.padding(bottom = 24.dp).testTag("message-actions")) {
                SheetRow("Copy", ArchieIcons.ContentCopy) { textOf(id)?.let { clipboard.setText(AnnotatedString(it)) }; actionsFor = null }
                SheetRow("Select text", ArchieIcons.Description) { selectFor = id; actionsFor = null }
                SheetRow("Rewind to here", ArchieIcons.History) { confirmRewind = id; actionsFor = null }
                SheetRow("Fork from here", ArchieIcons.CallSplit) { onAction(ChatAction.Fork(id)); actionsFor = null }
            }
        }
    }
    selectFor?.let { id ->
        ModalBottomSheet(onDismissRequest = { selectFor = null }, containerColor = ArchieTheme.colors.surfaceContainerLow) {
            SelectionContainer(Modifier.padding(start = 24.dp, end = 24.dp, bottom = 32.dp).verticalScroll(rememberScrollState())) {
                Text(textOf(id).orEmpty(), style = ArchieTheme.typography.bodyLarge, color = ArchieTheme.colors.onSurface)
            }
        }
    }
    confirmRewind?.let { id ->
        ArchieConfirmDialog(
            title = "Rewind to here?",
            text = "Messages after this one will be removed. This cannot be undone.",
            confirmLabel = "Rewind",
            onConfirm = { onAction(ChatAction.Rewind(id)); confirmRewind = null },
            onDismissRequest = { confirmRewind = null },
            destructive = true,
        )
    }
}

@Composable
private fun SheetRow(text: String, icon: ImageVector, onClick: () -> Unit) {
    ArchieListItem(headline = text, onClick = onClick, modifier = Modifier.padding(horizontal = 12.dp), leading = { ListLeadingIcon(icon) })
}

/** Mockup (i): greeting, the big voice button, suggestion chips. Never a blank screen (A6). */
@Composable
private fun ArchieEmpty(hour: Int, suggestions: List<Suggestion>, onAction: (ChatAction) -> Unit, callbacks: ConversationCallbacks) {
    val greeting = when (hour) {
        in 5..11 -> "Good morning."
        in 12..17 -> "Good afternoon."
        else -> "Good evening."
    }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).testTag("empty-state"), verticalArrangement = Arrangement.Center) {
        EmptyState(
            title = "$greeting\nWhat are we doing?",
            body = "Talk or type. Archie can hand work to an agent.",
            modifier = Modifier.align(Alignment.CenterHorizontally).widthIn(max = 560.dp),
            onVoice = { onAction(ChatAction.StartVoice) },
            suggestions = {
                suggestions.forEach { s ->
                    SuggestionChip(s.label, { if (s.text != null) onAction(ChatAction.SendText(s.text)) else callbacks.onNewAgentSession() }, icon = s.icon)
                }
            },
        )
    }
}

/** Covers the list during rewind / fork / upload (02 F-12). */
@Composable
private fun BusyOverlay(label: String) {
    val c = ArchieTheme.colors
    Box(
        Modifier
            .fillMaxSize()
            .background(c.scrim.copy(alpha = 0.32f))
            .clickable(enabled = true, onClick = {})
            .testTag("busy-overlay"),
        contentAlignment = Alignment.Center,
    ) {
        Column(
            Modifier.background(c.surfaceContainerHigh, androidx.compose.foundation.shape.RoundedCornerShape(20.dp)).padding(24.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Spinner(size = 24.dp)
            Text(label, style = ArchieTheme.typography.bodyMedium, color = c.onSurface)
        }
    }
}
