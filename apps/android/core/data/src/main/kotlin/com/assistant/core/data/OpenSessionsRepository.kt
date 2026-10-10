package com.assistant.core.data

import com.assistant.core.conversation.ConversationState
import com.assistant.core.conversation.Termination
import com.assistant.core.model.HarnessProvider
import com.assistant.core.model.LiveStatus
import com.assistant.core.model.PoolSession
import com.assistant.core.model.SessionKind
import com.assistant.core.model.SessionRef
import com.assistant.core.model.SessionStatus
import com.assistant.core.model.SessionSummary
import com.assistant.core.protocol.ServerFrame
import com.assistant.core.session.OrchestratorChannel
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.channels.consumeEach
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.flatMapLatest
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.flow.launchIn
import kotlinx.coroutines.flow.onEach
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/** What a workspace tab holds (spec 14 §2.4: tabs are data, not routes). */
sealed interface ItemKey {
    /** The Archie conversation (always first). */
    data object Archie : ItemKey

    data class Agent(val conversation: ConversationKey) : ItemKey

    /** A memory document (a tab on Expanded, a detail screen on Compact; IA §2). */
    data class Memory(val path: String) : ItemKey

    data class Visual(val path: String) : ItemKey
}

enum class ItemKind { ARCHIE, AGENT, MEMORY, VISUAL }

/** The live indicator of a tab / switcher row (IA §3, ST-3). */
enum class TabStatus { IDLE, WORKING, NEEDS_YOU, CONNECTING, DISCONNECTED, STOPPED, NONE }

/** One workspace item, derived (titles per MC-2, status from the conversation state). */
data class WorkspaceItem(
    val key: ItemKey,
    val kind: ItemKind,
    val title: String,
    /** Agent harness chip ("Claude", "Qwen", "Gemini"); `null` for Archie, memory and visuals. */
    val provider: HarnessProvider? = null,
    val status: TabStatus = TabStatus.NONE,
    /** One-line state for the switcher / subtitle ("Ready · 14 turns", "Using Bash…"). */
    val detail: String = "",
    /** Pool key / history key of a conversation item (`null` for memory and visuals, or not known yet). */
    val localId: String? = null,
    val sdkId: String? = null,
)

/**
 * The workspace "tabs" (spec 14 §2.4) and "Open now". The server owns the open set (spec 12 OPEN-1):
 * [items] are the Archie conversation, then **every** agent session in `pool/live`, in pool order,
 * whether or not this device has its view, then views opened here that the pool does not list yet
 * (a new or resumed session before its first `start`), then memory documents and visuals. [active]
 * is the selected one; selecting a pool session with no view here opens its view.
 *
 * Focus (FOCUS-1): server events never change [active]; only user calls ([select], [openSession],
 * [newAgentSession], [openMemory], [openVisual], …) and Archie's `orchestrator_switch` (§6.11a, the
 * user's own request) do. A session closed on the server (OPEN-3, [ConversationEvent.Closed]) leaves
 * like an explicit close: its view goes and focus moves to the neighbour, Archie first.
 *
 * Decision P-1: [close] is the only path that ends a session on the server, and only for an
 * explicit user close. There is deliberately no lifecycle hook here that closes anything.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class OpenSessionsRepository(
    private val conversations: ConversationRepository,
    private val history: HistoryRepository,
    private val orchestrator: OrchestratorChannel,
    private val scope: CoroutineScope,
) {
    /** What is open **here**, in open order: the Archie view, agent views, memory and visuals. */
    private val local = MutableStateFlow<List<ItemKey>>(emptyList())
    private val _active = MutableStateFlow<ItemKey?>(null)
    val active: StateFlow<ItemKey?> = _active.asStateFlow()

    private val _items = MutableStateFlow<List<WorkspaceItem>>(emptyList())
    val items: StateFlow<List<WorkspaceItem>> = _items.asStateFlow()

    private val _notices = MutableSharedFlow<String>(extraBufferCapacity = 8)

    /** One-line notices for the snackbar: the active agent view closed on the server (OPEN-3). */
    val notices: SharedFlow<String> = _notices.asSharedFlow()

    init {
        // The Archie item exists while an Archie conversation is open (adopted, armed or resumed).
        conversations.openKeys.onEach { keys ->
            if (ConversationKey.ARCHIE in keys) {
                local.update { l -> if (ItemKey.Archie in l) l else listOf(ItemKey.Archie) + l }
                _active.compareAndSet(null, ItemKey.Archie)
            }
        }.launchIn(scope)
        conversations.events.onEach {
            when (it) {
                // §6.11a SW-2: Archie moved this device to a past conversation at the user's request.
                is ConversationEvent.ArchieSwitched -> focus(ItemKey.Archie)
                is ConversationEvent.Closed -> onClosedByServer(it)
                else -> Unit
            }
        }.launchIn(scope)
        // Watcher events (T-7): a session opened anywhere is listed at once (OPEN-4); closes are
        // handled by the conversation repository (the view) and the pool (the row).
        val frames = orchestrator.subscribeFrames()
        scope.launch {
            frames.consumeEach { f ->
                when (f) {
                    is ServerFrame.AgentSessionOpened -> { history.syncPool(); history.refreshListSoon() }
                    is ServerFrame.AgentSessionClosed -> history.refreshListSoon()
                    else -> Unit
                }
            }
        }
        // Derived items: local keys × their conversation state × the pool × titles.
        local.flatMapLatest { keys ->
            val states = keys.map { k ->
                when (k) {
                    ItemKey.Archie -> conversations.state(ConversationKey.ARCHIE)
                    is ItemKey.Agent -> conversations.state(k.conversation)
                    else -> flowOf(null)
                }
            }
            if (states.isEmpty()) flowOf(emptyList())
            else combine(states) { arr -> keys.mapIndexed { i, k -> k to arr[i] } }
        }.let { views ->
            combine(views, history.pool, history.sessions) { v, pool, _ -> layout(v, pool) }
        }.onEach { _items.value = it }.launchIn(scope)
    }

    // ───────────── user actions ─────────────

    /** Selects [key]; a pool session with no view here gets its view first. */
    fun select(key: ItemKey) {
        if (key !in local.value) {
            val row = (key as? ItemKey.Agent)?.let { k -> history.pool.value.firstOrNull { ConversationKey.agent(it.localId) == k.conversation } }
                ?: return
            openLive(row)
            return
        }
        _active.value = key
        (key as? ItemKey.Agent)?.let { conversations.touch(it.conversation) }
    }

    /** The next / previous item (Compact title swipe, Ctrl+Tab). Wraps around. */
    fun selectRelative(delta: Int) {
        val list = _items.value
        if (list.isEmpty()) return
        val i = list.indexOfFirst { it.key == _active.value }.coerceAtLeast(0)
        select(list[(i + delta).mod(list.size)].key)
    }

    /**
     * Opens a history row: an Archie conversation resumes on the one orchestrator socket, an agent
     * opens its view. A user action (OPEN-2): its `start` may resume the session.
     */
    fun openSession(summary: SessionSummary) {
        if (summary.isOrchestrator) {
            val cur = conversations.current(ConversationKey.ARCHIE)?.ref
            if (cur?.sdkId != summary.sdkId) conversations.resumeArchie(summary.sdkId)
            focus(ItemKey.Archie)
            return
        }
        val live = history.pool.value.firstOrNull { it.sdkId == summary.sdkId && !it.isOrchestrator }
        val ref = SessionRef(
            localId = live?.localId ?: summary.localId ?: java.util.UUID.randomUUID().toString(),
            sdkId = summary.sdkId,
            kind = SessionKind.AGENT,
            provider = summary.provider,
            live = live != null,
            liveStatus = live?.status,
        )
        focus(ItemKey.Agent(conversations.openAgent(ref, userStart = true)))
    }

    /** A pool session as a focused view (its `start` reattaches). */
    fun openLive(pool: PoolSession) {
        if (pool.isOrchestrator) { focus(ItemKey.Archie); return }
        focus(ItemKey.Agent(conversations.openAgent(pool.toRef())))
    }

    /**
     * A tap on an approval notification (§6.9): focuses the agent view of pool key [localId],
     * opening it from the live pool when it is not open here. False = no such live agent session.
     */
    suspend fun openAgentByLocalId(localId: String): Boolean {
        viewKeyOf(localId)?.let { select(it); return true }
        val live = history.pool.value.firstOrNull { it.localId == localId && !it.isOrchestrator }
            ?: history.syncPool()?.firstOrNull { it.localId == localId && !it.isOrchestrator }
            ?: return false
        openLive(live)
        return true
    }

    /** Opens a fork / continuation result (user-initiated, focused). */
    fun openRef(ref: SessionRef) {
        if (ref.kind == SessionKind.ORCHESTRATOR) {
            ref.sdkId?.let { conversations.resumeArchie(it) }
            focus(ItemKey.Archie)
        } else {
            focus(ItemKey.Agent(conversations.openAgent(ref)))
        }
    }

    fun newAgentSession(provider: HarnessProvider? = null) = focus(ItemKey.Agent(conversations.newAgent(provider)))

    fun newArchieConversation() {
        conversations.newArchie()
        focus(ItemKey.Archie)
    }

    fun openMemory(path: String) = focus(ItemKey.Memory(path))
    fun openVisual(path: String) = focus(ItemKey.Visual(path))

    /**
     * Explicit close (§6.7, P-1): agent and Archie conversations close for everyone, with or without
     * a view here; memory and visual tabs just go away. Focus moves to the neighbour, Archie first.
     */
    suspend fun close(key: ItemKey): Boolean {
        val localId = _items.value.firstOrNull { it.key == key }?.localId
        localId?.let(history::dropFromPool)                                   // gone from "Open now" at once
        val ok = when (key) {
            ItemKey.Archie -> conversations.close(ConversationKey.ARCHIE)
            is ItemKey.Agent ->
                if (key in local.value) conversations.close(key.conversation)
                else localId?.let { conversations.closePoolSession(it) } ?: false
            else -> true
        }
        dropLocal(key)
        history.syncPool()
        return ok
    }

    /** After a delete (§6.8): views of that session go away (their pool entry was closed). */
    fun forgetSession(sdkId: String) {
        local.value.filterIsInstance<ItemKey.Agent>().filter { conversations.current(it.conversation)?.ref?.sdkId == sdkId }
            .forEach { conversations.forget(it.conversation); dropLocal(it) }
    }

    /** T-15: a new server. Everything local goes; nothing is closed on either server. */
    fun resetForServer() {
        local.value = emptyList()
        _active.value = null
    }

    // ───────────── internals ─────────────

    /**
     * OPEN-3: the view leaves like an explicit close. Only the agent view the user is looking at
     * gets a notice; Archie's close is followed by its own switch announcement (§6.11a).
     */
    private fun onClosedByServer(e: ConversationEvent.Closed) {
        val key = if (e.key == ConversationKey.ARCHIE) ItemKey.Archie else ItemKey.Agent(e.key)
        if (key is ItemKey.Agent && _active.value == key) {
            _notices.tryEmit(closedNotice(history.titleFor(e.ref.sdkId, e.ref.localId, AGENT_PLACEHOLDER), e.termination))
        }
        dropLocal(key)
    }

    private fun viewKeyOf(localId: String): ItemKey.Agent? =
        local.value.filterIsInstance<ItemKey.Agent>().firstOrNull { conversations.current(it.conversation)?.ref?.localId == localId }

    private fun focus(key: ItemKey) {
        local.update { l ->
            when {
                key in l -> l
                key == ItemKey.Archie -> listOf(key) + l
                else -> l + key
            }
        }
        select(key)
    }

    /** [key] leaves; if it was active, focus moves to the previous item open here (Archie first, §6.7). */
    private fun dropLocal(key: ItemKey) {
        if (key !in local.value) return
        val here = _items.value.map { it.key }.filter { it in local.value }
        val idx = here.indexOf(key)
        local.update { it - key }
        if (_active.value == key) _active.value = here.filterNot { it == key }.getOrNull((idx - 1).coerceAtLeast(0)) ?: local.value.firstOrNull()
    }

    /** OPEN-1: Archie, the pool's agent sessions in pool order, views not listed yet, then documents. */
    private fun layout(views: List<Pair<ItemKey, ConversationState?>>, pool: List<PoolSession>): List<WorkspaceItem> {
        val agents = views.filter { it.first is ItemKey.Agent }
        val byLocalId = agents.mapNotNull { v -> v.second?.let { it.ref.localId to v } }.toMap()
        val listed = HashSet<ItemKey>()
        val out = ArrayList<WorkspaceItem>(views.size + pool.size)
        views.firstOrNull { it.first == ItemKey.Archie }?.let { out += derive(it.first, it.second) }
        for (row in pool) {
            if (row.isOrchestrator) continue
            val v = byLocalId[row.localId]
            if (v != null) { out += derive(v.first, v.second); listed += v.first } else out += rowItem(row)
        }
        agents.filter { it.first !in listed }.forEach { out += derive(it.first, it.second) }
        views.filter { it.first is ItemKey.Memory || it.first is ItemKey.Visual }.forEach { out += derive(it.first, it.second) }
        return out
    }

    private fun derive(k: ItemKey, st: ConversationState?): WorkspaceItem = when (k) {
        ItemKey.Archie -> WorkspaceItem(
            key = k, kind = ItemKind.ARCHIE,
            title = archieTitle(history.titleFor(st?.ref?.sdkId, st?.ref?.localId, ARCHIE_PLACEHOLDER)),
            status = statusOf(st), detail = detailOf(st, "Archie"),
            localId = st?.ref?.localId, sdkId = st?.ref?.sdkId,
        )
        is ItemKey.Agent -> WorkspaceItem(
            key = k, kind = ItemKind.AGENT,
            title = history.titleFor(st?.ref?.sdkId, st?.ref?.localId, AGENT_PLACEHOLDER),
            provider = st?.ref?.provider,
            status = statusOf(st), detail = detailOf(st, null),
            localId = st?.ref?.localId, sdkId = st?.ref?.sdkId,
        )
        is ItemKey.Memory -> WorkspaceItem(k, ItemKind.MEMORY, k.path.substringAfterLast('/'), detail = memoryDetail(k.path))
        is ItemKey.Visual -> WorkspaceItem(k, ItemKind.VISUAL, k.path.substringAfterLast('/').substringBeforeLast('.'), detail = "Visual")
    }

    /** A pool session with no view here (OPEN-1): listed like any other, from its row. */
    private fun rowItem(row: PoolSession) = WorkspaceItem(
        key = ItemKey.Agent(ConversationKey.agent(row.localId)), kind = ItemKind.AGENT,
        title = history.titleFor(row.sdkId, row.localId, AGENT_PLACEHOLDER),
        provider = providerOf(row.sdkId),
        status = statusOf(row.status),
        detail = if (statusOf(row.status) == TabStatus.WORKING) "Working…" else "Ready",
        localId = row.localId, sdkId = row.sdkId,
    )

    private fun providerOf(sdkId: String?) = sdkId?.let { id -> history.sessions.value.value?.firstOrNull { it.sdkId == id }?.provider }

    private fun PoolSession.toRef() = SessionRef(
        localId = localId, sdkId = sdkId, kind = if (isOrchestrator) SessionKind.ORCHESTRATOR else SessionKind.AGENT,
        provider = providerOf(sdkId), live = true, liveStatus = status,
    )

    companion object {
        const val ARCHIE_PLACEHOLDER = "Archie"
        const val NEW_CONVERSATION = "New conversation"
        private val GENERIC_ARCHIE = Regex("^\\s*(orchestrator|archie)?\\s*$", RegexOption.IGNORE_CASE)

        /**
         * IA §1 / CR-9: the UI never says "Orchestrator". The backend titles an untitled Archie
         * conversation "Orchestrator", and the placeholder is "Archie": both show as
         * "New conversation" (same rule as `SessionTitles.conversationTitle` and the web).
         */
        /** Same wording as the web (spec 12 OPEN-3, §6.13). */
        fun closedNotice(title: String, termination: Termination?): String {
            if (termination == null) return "$title was closed elsewhere"
            val how = ENDED[termination.reason] ?: "ended"
            return if (termination.detail.isNullOrBlank()) "$title $how" else "$title $how: ${termination.detail}"
        }

        /** How a terminated session ended, by `session_terminated.reason`. */
        private val ENDED = mapOf(
            "subprocess_crashed" to "crashed",
            "subprocess_lost" to "ended unexpectedly",
            "unreachable" to "can't reach its host",
            "replaced" to "was replaced",
            "closed_by_user" to "was closed",
        )

        fun archieTitle(raw: String): String = raw.trim().let { if (GENERIC_ARCHIE.matches(it)) NEW_CONVERSATION else it }
        const val AGENT_PLACEHOLDER = "New agent session"

        /** ST-3: connecting, subscribed-idle, busy, needs-you, stopped/terminated, disconnected/failed. */
        fun statusOf(st: ConversationState?): TabStatus = when {
            st == null -> TabStatus.NONE
            st.newestPendingPermission() != null || st.agentApprovals.isNotEmpty() -> TabStatus.NEEDS_YOU
            st.status == SessionStatus.STOPPED || st.status == SessionStatus.TERMINATED -> TabStatus.STOPPED
            st.connection == com.assistant.core.model.ConnectionState.FAILED -> TabStatus.DISCONNECTED
            st.connection == com.assistant.core.model.ConnectionState.OFFLINE && st.connectionBanner != null -> TabStatus.DISCONNECTED
            st.status.busy || st.inTurn -> TabStatus.WORKING
            st.status == SessionStatus.CONNECTING -> TabStatus.CONNECTING
            else -> TabStatus.IDLE
        }

        /** The live status line (Compact subtitle, switcher rows). */
        fun detailOf(st: ConversationState?, prefix: String?): String {
            val text = when {
                st == null -> "Not connected"
                st.newestPendingPermission() != null || st.agentApprovals.isNotEmpty() -> "Waiting for your approval"
                st.status == SessionStatus.TERMINATED -> "Session ended"
                st.status == SessionStatus.STOPPED -> "Stopped"
                st.connectionBanner != null && st.connection != com.assistant.core.model.ConnectionState.SUBSCRIBED -> "Reconnecting…"
                st.status == SessionStatus.THINKING -> "Thinking…"
                st.status == SessionStatus.TOOL_USE -> "Using tools…"
                st.status == SessionStatus.STREAMING || st.status == SessionStatus.PROCESSING -> "Working…"
                st.status == SessionStatus.RETRYING -> "Retrying…"
                st.status == SessionStatus.COMPACTING -> "Compacting…"
                st.status == SessionStatus.CONNECTING -> "Connecting…"
                st.counters.turns > 0 -> "Ready · ${st.counters.turns} turns"
                else -> "Ready"
            }
            return if (prefix != null) "$prefix · ${text.replaceFirstChar { it.lowercase() }}" else text
        }

        private fun memoryDetail(path: String): String {
            val dir = path.substringBeforeLast('/', "")
            return if (dir.isEmpty()) "Memory" else "Memory · $dir"
        }

        /** Pool `status` → tab status for rows that have no open view (drawer Open now). */
        fun statusOf(live: LiveStatus?): TabStatus = when (live) {
            LiveStatus.STREAMING, LiveStatus.TOOL_USE, LiveStatus.THINKING -> TabStatus.WORKING
            LiveStatus.DISCONNECTED -> TabStatus.DISCONNECTED
            LiveStatus.IDLE, LiveStatus.INTERRUPTED -> TabStatus.IDLE
            null -> TabStatus.NONE
        }
    }
}
