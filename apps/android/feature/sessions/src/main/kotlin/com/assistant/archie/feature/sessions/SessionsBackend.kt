package com.assistant.archie.feature.sessions

import com.assistant.core.data.ConversationEvent
import com.assistant.core.data.ConversationKey
import com.assistant.core.data.ConversationRepository
import com.assistant.core.data.HistoryRepository
import com.assistant.core.data.ItemKey
import com.assistant.core.data.OpenSessionsRepository
import com.assistant.core.data.WorkspaceItem
import com.assistant.core.model.ConnectionState
import com.assistant.core.model.HarnessProvider
import com.assistant.core.model.PoolSession
import com.assistant.core.model.SessionKind
import com.assistant.core.model.SessionRef
import com.assistant.core.model.SessionStatus
import com.assistant.core.model.SessionSummary
import com.assistant.core.network.ApiResult
import com.assistant.core.network.ArchieApi
import com.assistant.core.session.OrchestratorChannel
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.filterIsInstance
import kotlinx.coroutines.flow.map
import java.util.UUID

/**
 * Everything the session flows ask of the data layer. Production: [RepositorySessionsBackend] over
 * B-03's repositories; tests drive the same implementation against a MockWebServer backend.
 *
 * Every call here is a direct user action, so it may take focus (P-6). Closing (P-1): only [close],
 * [closeArchieHere], [closePoolSession] and [delete] end a session on the server, and only because
 * the user asked for it.
 */
interface SessionsBackend {
    val items: StateFlow<List<WorkspaceItem>>
    val sessions: StateFlow<com.assistant.core.data.LoadState<List<SessionSummary>>>
    val pool: StateFlow<List<PoolSession>>

    /** `error{orchestrator_active}` after a start/attach the user asked for (§6.11). */
    val conflicts: Flow<Unit>

    /** "<title> was closed elsewhere" / "<title> ended: <why>" for the active agent view (OPEN-3). */
    val notices: Flow<String>

    /** The Archie view of this device while it is a live conversation (not ended, not failed). */
    fun liveArchieHere(): RunningArchie?

    /** The Archie view's ids in any state (`null` when there is none). */
    fun archieViewIds(): Pair<String, String?>?

    /** The orchestrator the socket was last attached to (offline fallback). */
    fun lastKnownOrchestrator(): RunningArchie?
    suspend fun syncPool(): List<PoolSession>?

    /** §6.11 new: `start{local_id: uuid}` on the orchestrator socket; focuses the Archie view. */
    fun newArchie()

    /** §6.11 resume (`attachLocalId == null`, new pool key) or attach (the running one's key). Focuses. */
    fun resumeArchie(sdkId: String, attachLocalId: String?)
    fun focusArchie()

    /** Drops the Archie view locally after its start lost the race (it never existed server-side). */
    fun dropArchieView()

    /** Explicit close of this device's Archie (for every device, P-1). */
    suspend fun closeArchieHere(): Boolean

    /** `POST /api/sessions/{localId}/close` for a session that has no view here (P-1: user asked). */
    suspend fun closePoolSession(localId: String): Boolean

    /** §6.10, focused. Provider, working directory and MCPs come from the global config. */
    fun newAgent()
    fun openAgent(summary: SessionSummary)
    fun openAgent(sdkId: String, provider: HarnessProvider?)
    suspend fun close(key: ItemKey): Boolean
    fun compact(key: ItemKey)
    fun storedTitle(sdkId: String): String?
    fun isLive(sdkId: String): Boolean
    suspend fun rename(sdkId: String, title: String): ApiResult<Unit>
    suspend fun duplicate(sdkId: String): ApiResult<String>

    /** The ⋮ menu's Fork: a copy of the whole conversation (`drop_last_n: 0`, §6.5). */
    suspend fun fork(sdkId: String): ApiResult<String>

    /** §6.8: open views close first, then the live pool entry, then `DELETE`. */
    suspend fun delete(sdkId: String, localId: String?): ApiResult<Unit>
}

/** [SessionsBackend] over the process-scoped repositories of `:core:data`. */
class RepositorySessionsBackend(
    private val conversations: ConversationRepository,
    private val open: OpenSessionsRepository,
    private val history: HistoryRepository,
    private val orchestrator: OrchestratorChannel,
    private val api: ArchieApi,
    private val newId: () -> String = { UUID.randomUUID().toString() },
) : SessionsBackend {
    override val items: StateFlow<List<WorkspaceItem>> get() = open.items
    override val sessions get() = history.sessions
    override val pool: StateFlow<List<PoolSession>> get() = history.pool
    override val notices: Flow<String> get() = open.notices
    override val conflicts: Flow<Unit> =
        conversations.events.filterIsInstance<ConversationEvent.OrchestratorConflict>().map { }

    override fun liveArchieHere(): RunningArchie? {
        val st = conversations.current(ConversationKey.ARCHIE) ?: return null
        if (st.status == SessionStatus.STOPPED || st.status == SessionStatus.TERMINATED) return null
        if (st.termination != null || st.connection == ConnectionState.FAILED) return null
        return RunningArchie(st.ref.localId, st.ref.sdkId, here = true)
    }

    override fun archieViewIds(): Pair<String, String?>? =
        conversations.current(ConversationKey.ARCHIE)?.ref?.let { it.localId to it.sdkId }

    override fun lastKnownOrchestrator(): RunningArchie? =
        orchestrator.state.value.orchestrator?.let { RunningArchie(it.localId, it.sdkId, here = false) }

    override suspend fun syncPool(): List<PoolSession>? = history.syncPool()

    override fun newArchie() = open.newArchieConversation()

    override fun resumeArchie(sdkId: String, attachLocalId: String?) {
        conversations.resumeArchie(sdkId, attachLocalId)
        open.openRef(SessionRef(attachLocalId ?: newId(), null, SessionKind.ORCHESTRATOR, null))   // focus only (sdkId null: no second start)
    }

    override fun focusArchie() = open.openRef(SessionRef(newId(), null, SessionKind.ORCHESTRATOR, null))

    override fun dropArchieView() = conversations.forget(ConversationKey.ARCHIE)

    override suspend fun closeArchieHere(): Boolean = open.close(ItemKey.Archie)

    override suspend fun closePoolSession(localId: String): Boolean {
        val ok = api.closePoolSession(localId) is ApiResult.Ok
        history.syncPool()
        return ok
    }

    override fun newAgent() = open.newAgentSession()

    override fun openAgent(summary: SessionSummary) = open.openSession(summary)

    override fun openAgent(sdkId: String, provider: HarnessProvider?) =
        open.openRef(SessionRef(newId(), sdkId, SessionKind.AGENT, provider))

    override suspend fun close(key: ItemKey): Boolean = open.close(key)

    override fun compact(key: ItemKey) {
        when (key) {
            ItemKey.Archie -> conversations.compact(ConversationKey.ARCHIE)
            is ItemKey.Agent -> conversations.compact(key.conversation)
            else -> Unit
        }
    }

    override fun storedTitle(sdkId: String): String? =
        history.sessions.value.value?.firstOrNull { it.sdkId == sdkId }?.title

    override fun isLive(sdkId: String): Boolean = history.pool.value.any { it.sdkId == sdkId }

    override suspend fun rename(sdkId: String, title: String) = history.rename(sdkId, title)

    override suspend fun duplicate(sdkId: String) = history.duplicate(sdkId)

    override suspend fun fork(sdkId: String): ApiResult<String> {
        val r = api.fork(sdkId, 0)
        history.refreshListSoon()
        return r
    }

    override suspend fun delete(sdkId: String, localId: String?): ApiResult<Unit> {
        history.syncPool()
        val poolLocal = history.pool.value.firstOrNull { it.sdkId == sdkId }?.localId
        // Every open view of the session closes first (fixes W-8: the tab used to stay behind).
        open.items.value
            .filter { it.sdkId == sdkId || (it.localId != null && (it.localId == localId || it.localId == poolLocal)) }
            .forEach { open.close(it.key) }
        return history.delete(sdkId)
    }
}
