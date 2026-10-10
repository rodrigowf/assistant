package com.assistant.core.data

import com.assistant.core.model.PoolSession
import com.assistant.core.model.SessionSummary
import com.assistant.core.network.ApiResult
import com.assistant.core.network.ArchieApi
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

/**
 * The spec-12 `SessionDirectory` (§3.7): the conversation list (`GET /api/sessions`) and the live
 * pool (`GET /api/sessions/pool/live`), plus the list mutations of §6.6/§6.8.
 *
 * - Mutations are not broadcast by the backend (MC-1): every mutation here refreshes the list itself.
 * - [refreshListSoon] debounces to one refresh per [LIST_DEBOUNCE_MS] (MC-2: after every `endTurn`).
 * - Titles are derived by [titleFor] (MC-2), never stored on a view.
 */
class HistoryRepository(
    private val api: ArchieApi,
    private val scope: CoroutineScope,
) {
    private val _sessions = MutableStateFlow(LoadState<List<SessionSummary>>())
    val sessions: StateFlow<LoadState<List<SessionSummary>>> = _sessions.asStateFlow()

    private val _pool = MutableStateFlow<List<PoolSession>>(emptyList())

    /** The server's open set (spec 12 OPEN-1): the last `pool/live` read, kept current by watcher events. */
    val pool: StateFlow<List<PoolSession>> = _pool.asStateFlow()

    /** One `pool/live` answer as read (OPEN-4); [requestedAt] is `System.nanoTime()` at the request. */
    data class PoolRead(val rows: List<PoolSession>, val requestedAt: Long)

    private val _poolReads = MutableSharedFlow<PoolRead>(extraBufferCapacity = 16)

    /** Every successful [syncPool], for the OPEN-4 reconcile of open views. */
    val poolReads: SharedFlow<PoolRead> = _poolReads.asSharedFlow()

    private val listMutex = Mutex()
    private val poolMutex = Mutex()
    private var debounceJob: Job? = null
    suspend fun refreshList(): ApiResult<List<SessionSummary>> = listMutex.withLock {
        _sessions.update { it.loading() }
        val r = api.listSessions()
        _sessions.update { LoadState.of(r, it) }
        r
    }

    fun refreshListSoon() {
        if (debounceJob?.isActive == true) return
        debounceJob = scope.launch {
            delay(LIST_DEBOUNCE_MS)
            refreshList()
        }
    }

    /** `syncPool()` of §3.7: on app start, on visible, after any session mutation. */
    suspend fun syncPool(): List<PoolSession>? = poolMutex.withLock {
        val requestedAt = System.nanoTime()
        val rows = api.livePool().getOrNull() ?: return null
        _pool.value = rows
        _poolReads.tryEmit(PoolRead(rows, requestedAt))
        rows
    }

    /** `agent_session_closed` (OPEN-4): the row leaves the open set at once, before the next read. */
    fun dropFromPool(localId: String) = _pool.update { rows -> rows.filterNot { it.localId == localId } }

    /** Both stores (start, foreground, watcher events). */
    fun refreshAll() {
        scope.launch { syncPool() }
        scope.launch { refreshList() }
    }

    /** Server change (T-15): forget everything learned from the old server. */
    fun reset() {
        debounceJob?.cancel()
        _sessions.value = LoadState()
        _pool.value = emptyList()
    }

    /** MC-2: `list[sdkId]?.title ?: list[localId]?.title ?: pool title ?: placeholder`. */
    fun titleFor(sdkId: String?, localId: String?, placeholder: String): String {
        val list = _sessions.value.value.orEmpty()
        sdkId?.let { id -> list.firstOrNull { it.sdkId == id }?.title?.takeIf(::isRealTitle)?.let { return it } }
        localId?.let { id -> list.firstOrNull { it.localId == id }?.title?.takeIf(::isRealTitle)?.let { return it } }
        val p = _pool.value.firstOrNull { (localId != null && it.localId == localId) || (sdkId != null && it.sdkId == sdkId) }
        return p?.title?.takeIf(::isRealTitle) ?: placeholder
    }

    // ───────────── mutations (§6.6, §6.8) ─────────────

    /** Optimistic rename, then refresh (404 tolerated). */
    suspend fun rename(sdkId: String, title: String): ApiResult<Unit> {
        _sessions.update { s -> s.copy(value = s.value?.map { if (it.sdkId == sdkId) it.copy(title = title) else it }) }
        val r = api.rename(sdkId, title)
        refreshList()
        return if (r is ApiResult.HttpError && r.code == 404) ApiResult.Ok(Unit) else r
    }

    /** `POST …/duplicate` ⇒ new sdk id; refresh, do not open (web parity). */
    suspend fun duplicate(sdkId: String): ApiResult<String> {
        val r = api.duplicate(sdkId)
        refreshList()
        return r
    }

    /**
     * `DELETE /api/sessions/{sdkId}` after closing the live pool entry, if any (the pool close MUST
     * come first, §6.8). Callers close their own open views before this (OpenSessionsRepository).
     */
    suspend fun delete(sdkId: String): ApiResult<Unit> {
        val live = _pool.value.firstOrNull { it.sdkId == sdkId }
        if (live != null) api.closePoolSession(live.localId)
        val r = api.delete(sdkId)
        _sessions.update { s -> s.copy(value = s.value?.filterNot { it.sdkId == sdkId }) }
        refreshList()
        syncPool()
        return if (r is ApiResult.HttpError && r.code == 404) ApiResult.Ok(Unit) else r
    }

    companion object {
        const val LIST_DEBOUNCE_MS = 2_000L

        /**
         * The backend lists a live session that has no history file yet as "(active session)"
         * (`api/routes/sessions.py`). It is not a title: the caller's placeholder shows instead.
         */
        const val ACTIVE_SESSION_PLACEHOLDER = "(active session)"

        fun isRealTitle(t: String): Boolean = t.isNotBlank() && t.trim() != ACTIVE_SESSION_PLACEHOLDER
    }
}
