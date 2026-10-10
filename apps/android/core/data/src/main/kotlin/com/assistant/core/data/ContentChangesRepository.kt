package com.assistant.core.data

import com.assistant.core.network.SocketState
import com.assistant.core.protocol.ContentChange
import com.assistant.core.protocol.ServerFrame
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.launchIn
import kotlinx.coroutines.flow.onEach
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/** One file's change counter (spec 12 §9.3). [deleted]: the last change removed it. */
data class ContentStamp(val version: Int, val deleted: Boolean = false)

/**
 * Live reload of visualizations and memory documents (spec 12 §9.3; web `services/contentChanges.ts`).
 *
 * - `visualization_changed` / `memory_changed` arrive on the orchestrator socket ([frames], every
 *   frame of the channel, Archie open or not) and bump per-path counters ([visuals], [memory]); an
 *   open viewer reloads when its counter moves (the viewers debounce). The visuals list refreshes
 *   once per burst; the memory tree only when it is loaded and a file appeared or went away.
 * - VZ-6 fallback: on every socket open after the first, the visuals list is refetched and each
 *   visualization whose `modified` moved is bumped; [resyncEpoch] tells open memory documents to
 *   refetch quietly.
 */
class ContentChangesRepository(
    frames: Flow<ServerFrame>,
    socket: Flow<SocketState>,
    private val visualsList: VisualsRepository,
    private val memoryTree: MemoryRepository,
    private val scope: CoroutineScope,
) {
    private val _visuals = MutableStateFlow<Map<String, ContentStamp>>(emptyMap())
    private val _memory = MutableStateFlow<Map<String, ContentStamp>>(emptyMap())
    private val _resyncEpoch = MutableStateFlow(0)

    /** Keyed by visualization path (relative to context/public/). */
    val visuals: StateFlow<Map<String, ContentStamp>> = _visuals.asStateFlow()

    /** Keyed by memory path (relative to context/memory/). */
    val memory: StateFlow<Map<String, ContentStamp>> = _memory.asStateFlow()
    val resyncEpoch: StateFlow<Int> = _resyncEpoch.asStateFlow()

    private var visualsSoon: Job? = null
    private var treeSoon: Job? = null
    private var everOpen = false
    private var open = false

    init {
        frames.onEach(::onFrame).launchIn(scope)
        socket.onEach { s ->
            val nowOpen = s is SocketState.Open
            if (nowOpen && !open) {
                if (everOpen) scope.launch { resync() }
                everOpen = true
            }
            open = nowOpen
        }.launchIn(scope)
    }

    fun onFrame(f: ServerFrame) {
        when (f) {
            is ServerFrame.VisualizationChanged -> {
                bump(_visuals, f.visualizations)
                visualsSoon = soon(visualsSoon) { visualsList.refresh() }
            }
            is ServerFrame.MemoryChanged -> {
                bump(_memory, f.changes)
                if (memoryTree.tree.value.loaded && f.changes.any { it.kind != ContentChange.Kind.MODIFIED }) {
                    treeSoon = soon(treeSoon) { memoryTree.refreshTree() }
                }
            }
            else -> Unit
        }
    }

    /** The socket came back after a drop: catch up on what its frames would have said (VZ-6). */
    suspend fun resync() {
        _resyncEpoch.update { it + 1 }
        if (memoryTree.tree.value.loaded) memoryTree.refreshTree()
        val before = visualsList.list.value.value?.associate { it.path to it.modified } ?: return
        visualsList.refreshNow()
        val after = visualsList.list.value.value ?: return
        val present = after.map { it.path }.toSet()
        val moved = after.filter { v -> before[v.path].let { it != null && it != v.modified } }
            .map { ContentChange(it.path, ContentChange.Kind.MODIFIED) } +
            before.keys.filter { it !in present }.map { ContentChange(it, ContentChange.Kind.DELETED) }
        bump(_visuals, moved)
    }

    private fun bump(target: MutableStateFlow<Map<String, ContentStamp>>, changes: List<ContentChange>) {
        if (changes.isEmpty()) return
        target.update { m ->
            val next = m.toMutableMap()
            for (c in changes) next[c.path] = ContentStamp((m[c.path]?.version ?: 0) + 1, c.kind == ContentChange.Kind.DELETED)
            next
        }
    }

    private fun soon(job: Job?, block: () -> Unit): Job =
        job?.takeIf { it.isActive } ?: scope.launch { delay(REFRESH_DEBOUNCE_MS); block() }

    companion object {
        /** One list refresh per burst of change frames. */
        const val REFRESH_DEBOUNCE_MS = 400L
    }
}
