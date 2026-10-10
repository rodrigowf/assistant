package com.assistant.archie.feature.chat

import androidx.lifecycle.SavedStateHandle
import androidx.lifecycle.createSavedStateHandle
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.assistant.archie.feature.chat.model.ChatItem
import com.assistant.archie.feature.chat.model.ToolDescriptor
import com.assistant.core.conversation.AssistantEntry
import com.assistant.core.conversation.ConversationState
import com.assistant.core.conversation.TextBlock
import com.assistant.core.conversation.ToolBlock
import com.assistant.core.conversation.UserEntry
import com.assistant.core.data.ApprovalAnswer
import com.assistant.core.data.ConversationEvent
import com.assistant.core.data.CutResult
import com.assistant.core.model.ConnectionState
import com.assistant.core.model.SessionKind
import com.assistant.core.model.SessionRef
import com.assistant.core.model.UploadResult
import com.assistant.core.network.ApiResult
import com.assistant.core.network.SendResult
import com.assistant.core.network.UploadSource
import kotlinx.collections.immutable.toImmutableList
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.conflate
import kotlinx.coroutines.flow.filterNotNull
import kotlinx.coroutines.flow.flowOn
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.receiveAsFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.flow.transform
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/** What the screen can ask for (spec 14 §2.1: `onAction(a)`). */
sealed interface ChatAction {
    data class DraftChanged(val text: String) : ChatAction

    /** Composer primary button / IME Send. */
    data object Primary : ChatAction
    data object Send : ChatAction
    data object Stop : ChatAction
    data class SendText(val text: String) : ChatAction
    data object Compact : ChatAction
    data class ToggleGroup(val key: String, val expanded: Boolean) : ChatAction
    data class ToggleCard(val key: String, val expanded: Boolean) : ChatAction
    data object LoadOlder : ChatAction
    data class Permission(val requestId: String, val allow: Boolean) : ChatAction
    data class AgentApproval(val localId: String, val requestId: String, val allow: Boolean) : ChatAction
    data class DismissCard(val id: String) : ChatAction
    data class Retry(val kind: RetryKind, val cardId: String) : ChatAction
    data class Rewind(val entryId: String) : ChatAction
    data class Fork(val entryId: String) : ChatAction
    data class Upload(val source: UploadSource, val subject: String? = null) : ChatAction
    data object Reload : ChatAction

    // voice (Archie only)
    data object StartVoice : ChatAction
    data object EndVoice : ChatAction
    data object ToggleMic : ChatAction
    data object ToggleSpeaker : ChatAction
    data object TakeOverVoice : ChatAction
    data object ToggleRecording : ChatAction
}

/** One-shot effects (spec 14 §2.1: a buffered channel). */
sealed interface ChatEffect {
    data class Snackbar(val message: String) : ChatEffect
    data class OpenSession(val ref: SessionRef) : ChatEffect
}

/**
 * The conversation screen's ViewModel (spec 14 §2.1). Domain state stays in the repository; this
 * class maps it to [ConversationUiState] and holds UI-local state: the draft, group/card toggles,
 * answered permissions, dismissed cards, the voice reconnect timeline.
 *
 * Flattening runs on one confined background dispatcher ([flattenDispatcher]); while a block streams
 * the list is published at most every [SAMPLE_MS] (≈30 recompositions/s, §2.3).
 */
@OptIn(ExperimentalCoroutinesApi::class)
class ConversationViewModel(
    private val backend: ChatBackend,
    private val voice: ChatVoice,
    private val describe: (ToolBlock, SessionKind) -> ToolDescriptor,
    private val saved: SavedStateHandle = SavedStateHandle(),
    flattenDispatcher: CoroutineDispatcher = Dispatchers.Default.limitedParallelism(1),
    private val clock: () -> Long = System::currentTimeMillis,
    externalScope: CoroutineScope? = null,
    private val title: () -> String? = { null },
    /** The process-wide voice dock model (shared with the floating controls); null: a local one. */
    voiceDock: VoiceDockModel? = null,
) : ViewModel() {
    private val scope: CoroutineScope = externalScope ?: viewModelScope

    private val markdown = MarkdownStore()
    private val flattener = ChatListFlattener(describe) { id, text, streaming -> markdown.snapshot(id, text, streaming) }

    private val draft = MutableStateFlow(saved.get<String>(KEY_DRAFT) ?: "")
    val draftText: StateFlow<String> = draft

    /** The voice dock orb's live level, kept out of [state] so 15 Hz levels never recompose the screen. */
    val voiceLevel: StateFlow<Float?> get() = voice.level

    private val groupToggles = MutableStateFlow(readToggles(KEY_GROUPS))
    private val cardToggles = MutableStateFlow(readToggles(KEY_CARDS))
    private val loadingOlder = MutableStateFlow(false)
    private val answered = MutableStateFlow<Set<String>>(emptySet())
    private val dismissed = MutableStateFlow<Set<String>>(emptySet())
    private val transientErrors = MutableStateFlow<List<InlineCardUi.Error>>(emptyList())
    private val busy = MutableStateFlow<String?>(null)
    private var resendOnConnect: String? = null
    private var olderGuard: String? = null

    /** The voice dock's state and actions, with the reconnect timeline (shared with the floating controls). */
    private val dock = voiceDock ?: VoiceDockModel(voice, scope, clock)

    private val effects = Channel<ChatEffect>(Channel.BUFFERED)
    val effectFlow: Flow<ChatEffect> = effects.receiveAsFlow()

    private val options = combine(groupToggles, cardToggles, loadingOlder) { g, c, l ->
        FlattenOptions(groupToggles = g, cardToggles = c, loadingOlder = l)
    }

    private val flattened: Flow<Pair<ConversationState, FlattenResult>> =
        combine(backend.state.filterNotNull(), options, ::Pair)
            .conflate()
            .map { (s, o) ->
                val r = flattener.flatten(s, o)
                markdown.sweep()
                s to r
            }
            .flowOn(flattenDispatcher)
            .conflate()
            .transform { emit(it); if (it.second.streaming) delay(SAMPLE_MS) }

    private val voiceUi: Flow<VoiceUi> = dock.ui

    private data class Local(
        val answered: Set<String>,
        val dismissed: Set<String>,
        val transient: List<InlineCardUi.Error>,
        val busy: String?,
        val draftEmpty: Boolean,
    )

    private val local = combine(answered, dismissed, transientErrors, busy, draft) { a, d, t, b, dr ->
        Local(a, d, t, b, dr.isBlank())
    }

    val state: StateFlow<ConversationUiState> = combine(flattened, voiceUi, voice.recording, local) { (s, r), v, rec, l ->
        val archie = s.kind == SessionKind.ORCHESTRATOR
        val vu = if (archie) v else VoiceUi.Off
        ConversationUiState(
            kind = s.kind,
            loaded = s.history.loaded,
            items = r.items.toImmutableList(),
            oldestEntryId = s.entries.firstOrNull()?.id,
            hasMore = s.history.hasMore,
            empty = s.entries.isEmpty(),
            composer = ConversationUiMapper.composer(s, l.draftEmpty, vu, rec, title()),
            cards = ConversationUiMapper.cards(s, l.answered, l.dismissed, l.transient),
            queue = s.queue.toImmutableList(),
            voice = vu,
            counters = CountersUi(s.counters.cost, s.counters.turns, s.counters.contextTokens, s.counters.contextWindow),
            busyOverlay = l.busy,
        )
    }.stateIn(scope, SharingStarted.Eagerly, ConversationUiState())

    init {
        scope.launch { backend.events.collect(::onEvent) }
        scope.launch { backend.state.filterNotNull().collect(::onConversation) }
        dock.start()
        backend.touch()
    }

    fun onAction(a: ChatAction) {
        when (a) {
            is ChatAction.DraftChanged -> setDraft(a.text)
            ChatAction.Primary -> primary()
            ChatAction.Send -> sendDraft()
            ChatAction.Stop -> stop()
            is ChatAction.SendText -> sendText(a.text)
            ChatAction.Compact -> backend.compact()
            is ChatAction.ToggleGroup -> groupToggles.update { (it + (a.key to a.expanded)).also { m -> writeToggles(KEY_GROUPS, m) } }
            is ChatAction.ToggleCard -> cardToggles.update { (it + (a.key to a.expanded)).also { m -> writeToggles(KEY_CARDS, m) } }
            ChatAction.LoadOlder -> loadOlder()
            is ChatAction.Permission -> {
                if (a.requestId in answered.value) return
                answered.update { it + a.requestId }
                backend.respondToPermission(a.requestId, a.allow)
            }
            is ChatAction.AgentApproval -> {
                val id = "${a.localId}:${a.requestId}"
                if (id in answered.value) return
                answered.update { it + id }
                viewModelScope.launch {
                    val r = backend.respondToAgentApproval(a.localId, a.requestId, a.allow)
                    if (r is ApprovalAnswer.Failed) {
                        answered.update { it - id }
                        effects.trySend(ChatEffect.Snackbar(r.message))
                    }
                }
            }
            is ChatAction.DismissCard -> {
                dismissed.update { it + a.id }
                transientErrors.update { l -> l.filterNot { it.id == a.id } }
                if (a.id.startsWith("banner:")) backend.dismissBanner()
            }
            is ChatAction.Retry -> retry(a.kind, a.cardId)
            is ChatAction.Rewind -> cut(a.entryId, rewind = true)
            is ChatAction.Fork -> cut(a.entryId, rewind = false)
            is ChatAction.Upload -> upload(a.source, a.subject)
            ChatAction.Reload -> { dismissed.update { it + "gap" }; backend.reload() }
            ChatAction.StartVoice, ChatAction.EndVoice, ChatAction.ToggleMic, ChatAction.ToggleSpeaker,
            ChatAction.TakeOverVoice, ChatAction.ToggleRecording -> dock.onAction(a)
        }
    }

    /** The raw text of an entry for Copy / Select text (assistant: its text blocks, markdown kept). */
    fun textOf(entryId: String): String? = when (val e = backend.state.value?.entries?.firstOrNull { it.id == entryId }) {
        is UserEntry -> e.text
        is AssistantEntry -> e.blocks.filterIsInstance<TextBlock>().joinToString("\n\n") { it.text }.ifEmpty { null }
        else -> null
    }

    // ───────────────────────── composer ─────────────────────────

    private fun setDraft(text: String) {
        draft.value = text
        saved[KEY_DRAFT] = text
    }

    private fun primary() {
        val s = state.value
        when (s.composer.primary) {
            com.assistant.core.design.components.ComposerPrimary.Send -> sendDraft()
            com.assistant.core.design.components.ComposerPrimary.Stop -> stop()
            com.assistant.core.design.components.ComposerPrimary.Voice -> voice.start()
            com.assistant.core.design.components.ComposerPrimary.SendDisabled -> Unit
        }
    }

    private fun stop() {
        // In voice mode Stop is voice_stop, the voice host's job (§6.3); the composer is the dock then.
        backend.interrupt()
    }

    private fun sendDraft() {
        val text = draft.value.trim()
        if (text.isEmpty()) return
        if (sendText(text)) setDraft("")
    }

    /** Sends (or queues, while working: I-12) [text]. False = not sent; the draft is kept. */
    private fun sendText(text: String): Boolean {
        val s = backend.state.value ?: return false
        if (s.connection != ConnectionState.SUBSCRIBED && s.connection != ConnectionState.OPEN) {
            showUnsent(text)
            return false
        }
        val slash = s.kind == SessionKind.AGENT && SLASH.matches(text)
        val r = if (slash) backend.command(text) else backend.send(text)
        if (r != SendResult.SENT) {
            showUnsent(text, partial = !slash)
            return slash
        }
        transientErrors.update { l -> l.filterNot { it.id == UNSENT } }
        return true
    }

    private fun showUnsent(text: String, partial: Boolean = false) {
        resendOnConnect = text
        transientErrors.update { l ->
            l.filterNot { it.id == UNSENT } + InlineCardUi.Error(
                id = UNSENT,
                title = if (partial) "Message may not have been sent" else "Message not sent",
                body = if (partial) "The connection dropped while sending. Check the reply before retrying."
                else "Not connected to the server. Your message is kept and sends when you retry.",
                detail = null,
                retry = if (partial) null else RetryKind.Resend,
            )
        }
    }

    private fun retry(kind: RetryKind, cardId: String) {
        when (kind) {
            RetryKind.Reconnect -> backend.retry()
            RetryKind.Resend -> {
                backend.retry()
                transientErrors.update { l -> l.filterNot { it.id == cardId } }
                // Sent by onConversation once the view is subscribed again.
            }
            RetryKind.Reload -> backend.reload()
        }
    }

    // ───────────────────────── list ─────────────────────────

    private fun loadOlder() {
        val s = backend.state.value ?: return
        val oldest = s.entries.firstOrNull()?.id
        // Guard keyed on the oldest entry id: never ask twice for the same page (port of 5c029d6).
        if (!s.history.hasMore || oldest == olderGuard || loadingOlder.value) return
        olderGuard = oldest
        loadingOlder.value = true
        backend.loadOlder()
    }

    private fun onConversation(s: ConversationState) {
        if (loadingOlder.value && s.entries.firstOrNull()?.id != olderGuard) loadingOlder.value = false
        if (!s.history.hasMore) loadingOlder.value = false
        val pending = resendOnConnect
        if (pending != null && s.connection == ConnectionState.SUBSCRIBED) {
            resendOnConnect = null
            if (backend.send(pending) == SendResult.SENT) {
                if (draft.value.trim() == pending) setDraft("")
                transientErrors.update { l -> l.filterNot { it.id == UNSENT } }
            } else {
                showUnsent(pending)
            }
        }
        // A new stall instance (different elapsed) re-shows a card the user dismissed with Keep waiting.
        if (s.stall == null) dismissed.update { it - "stall" }
    }

    private fun onEvent(e: ConversationEvent) {
        when (e) {
            is ConversationEvent.SideError -> effects.trySend(ChatEffect.Snackbar(e.detail ?: e.code))
            is ConversationEvent.StartFailed -> Unit   // shown through the connection banner card
            else -> Unit
        }
    }

    // ───────────────────────── rewind / fork / upload ─────────────────────────

    private fun cut(entryId: String, rewind: Boolean) {
        if (busy.value != null) return
        busy.value = if (rewind) "Rewinding…" else "Forking…"
        scope.launch {
            val r = try {
                if (rewind) backend.rewind(entryId) else backend.fork(entryId)
            } finally {
                busy.value = null
            }
            when (r) {
                is CutResult.Done -> if (rewind) effects.send(ChatEffect.Snackbar("Rewound")) else effects.send(ChatEffect.OpenSession(r.ref))
                is CutResult.Failed -> effects.send(ChatEffect.Snackbar(r.message))
            }
        }
    }

    private fun upload(source: UploadSource, subject: String?) {
        if (busy.value != null) return
        busy.value = "Uploading ${source.fileName}…"
        scope.launch {
            val r = try {
                backend.upload(source) { sent, total ->
                    if (total > 0) busy.value = "Uploading ${source.fileName} · ${sent * 100 / total}%"
                }
            } finally {
                busy.value = null
            }
            when (r) {
                is ApiResult.Ok -> backend.inject(shareLine(r.value, subject))
                else -> effects.send(ChatEffect.Snackbar(r.errorMessage() ?: "Upload failed"))
            }
        }
    }

    // ───────────────────────── saved toggles ─────────────────────────

    private fun readToggles(key: String): Map<String, Boolean> =
        saved.get<ArrayList<String>>(key).orEmpty().associate { it.substring(1) to (it[0] == '1') }

    private fun writeToggles(key: String, m: Map<String, Boolean>) {
        saved[key] = ArrayList(m.map { (k, v) -> (if (v) "1" else "0") + k })
    }

    companion object {
        /** List publication interval while streaming (spec 14 §2.3). */
        const val SAMPLE_MS = 33L
        const val OUTCOME_MS = VoiceDockModel.OUTCOME_MS
        private const val KEY_DRAFT = "draft"
        private const val KEY_GROUPS = "groups"
        private const val KEY_CARDS = "cards"
        private const val UNSENT = "unsent"

        /** A one-word slash command (`/help`, `/cost model`), not a path like `/home/x`. */
        val SLASH = Regex("^/[A-Za-z][\\w:-]*(\\s[\\s\\S]*)?$")

        /** §6.15 share line (the Android format, `AssistantViewModel.kt:262-332`). */
        fun shareLine(u: UploadResult, subject: String?): String {
            val size = when {
                u.size < 1024 -> "${u.size} B"
                u.size < 1024 * 1024 -> String.format(java.util.Locale.US, "%.1f KB", u.size / 1024.0)
                else -> String.format(java.util.Locale.US, "%.1f MB", u.size / (1024.0 * 1024.0))
            }
            val sb = StringBuilder("[shared file] ${u.filename} ($size, ${u.contentType}) — ${u.url}")
            if (!subject.isNullOrBlank()) sb.append("\nNote: ").append(subject)
            sb.append("\nLocal path: ").append(u.path)
            return sb.toString()
        }

        /** Items in display order for the reversed list. */
        fun reversed(items: List<ChatItem>): List<ChatItem> = items.asReversed()
    }
}

/**
 * The factory the host uses (spec 14 §2.2: `viewModelFactory { initializer { … } }`). The shell keys
 * the ViewModel by the conversation key so each open view keeps its own UI-local state.
 */
fun conversationViewModelFactory(
    backend: () -> ChatBackend,
    voice: ChatVoice,
    toolCards: com.assistant.archie.feature.chat.ui.ToolCardRenderer = com.assistant.archie.feature.chat.ui.DefaultToolCardRenderer,
    title: () -> String? = { null },
    voiceDock: VoiceDockModel? = null,
): androidx.lifecycle.ViewModelProvider.Factory = androidx.lifecycle.viewmodel.viewModelFactory {
    initializer {
        ConversationViewModel(
            backend = backend(),
            voice = voice,
            describe = toolCards::describe,
            saved = createSavedStateHandle(),
            title = title,
            voiceDock = voiceDock,
        )
    }
}
