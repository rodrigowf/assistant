package com.assistant.core.data

import com.assistant.core.model.ConfigPatch
import com.assistant.core.model.MemoryNode
import com.assistant.core.model.ServerConfig
import com.assistant.core.model.UploadResult
import com.assistant.core.model.VisualInfo
import com.assistant.core.network.ApiResult
import com.assistant.core.network.ArchieApi
import com.assistant.core.network.NetworkMonitor
import com.assistant.core.network.DiscoveredServer
import com.assistant.core.network.ServerDiscovery
import com.assistant.core.network.UploadClient
import com.assistant.core.network.UploadSource
import com.assistant.core.protocol.CastProbeDto
import com.assistant.core.protocol.CastResponse
import com.assistant.core.voice.ports.VoiceSessionState
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/*
 * Thin pull-only stores for the later feature WPs (spec 12 §8, §9; spec 14 §1.2). They own the
 * cached copy and the refresh rules; the screens (B-07, B-08) own everything else.
 */

/** Memory (§9.2): tree + raw documents. Read-only (MEM-5); refreshed when the section opens. */
class MemoryRepository(private val api: ArchieApi, private val scope: CoroutineScope) {
    private val _tree = MutableStateFlow(LoadState<List<MemoryNode>>())
    val tree: StateFlow<LoadState<List<MemoryNode>>> = _tree.asStateFlow()

    fun refreshTree() {
        _tree.update { it.loading() }
        scope.launch { val r = api.memoryTree(); _tree.update { LoadState.of(r, it) } }
    }

    /** Raw markdown of one file (MEM-3 encoding is ArchieApi's). */
    suspend fun document(path: String): ApiResult<String> = api.memoryDocument(path)

    fun reset() { _tree.value = LoadState() }
}

/** Visuals (§9.1): list, rename, cast (BX-2). Refreshed on open and after any turn (debounced 2 s). */
class VisualsRepository(private val api: ArchieApi, private val scope: CoroutineScope) {
    private val _list = MutableStateFlow(LoadState<List<VisualInfo>>())
    val list: StateFlow<LoadState<List<VisualInfo>>> = _list.asStateFlow()
    private var debounce: Job? = null

    fun refresh() {
        scope.launch { refreshNow() }
    }

    /** [refresh], awaited (the reconnect catch-up compares the list before and after, VZ-6). */
    suspend fun refreshNow() {
        _list.update { it.loading() }
        val r = api.visualizations()
        _list.update { LoadState.of(r, it) }
    }

    /** After a view's `endTurn` and on `agent_session_opened/closed` (§9.1). */
    fun refreshSoon() {
        if (debounce?.isActive == true) return
        debounce = scope.launch { delay(REFRESH_DEBOUNCE_MS); refresh() }
    }

    /** Optimistic title, `PATCH` (404 tolerated), refresh. */
    suspend fun rename(path: String, title: String): ApiResult<Unit> {
        _list.update { s -> s.copy(value = s.value?.map { if (it.path == path) it.copy(title = title) else it }) }
        val r = api.renameVisualization(path, title)
        refresh()
        return if (r is ApiResult.HttpError && r.code == 404) ApiResult.Ok(Unit) else r
    }

    suspend fun castProbe(): ApiResult<CastProbeDto> = api.castProbe()
    suspend fun cast(path: String): ApiResult<CastResponse> = api.castVisualization(path)

    fun reset() { _list.value = LoadState() }

    companion object { const val REFRESH_DEBOUNCE_MS = 2_000L }
}

/** Server-global config (§8.1). Refetched on every settings screen open (CFG-3). */
class ServerConfigRepository(private val api: ArchieApi, private val scope: CoroutineScope) {
    private val _config = MutableStateFlow(LoadState<ServerConfig>())
    val config: StateFlow<LoadState<ServerConfig>> = _config.asStateFlow()

    fun load() {
        _config.update { it.loading() }
        scope.launch { val r = api.config(); _config.update { LoadState.of(r, it) } }
    }

    /** Partial PUT; the response replaces the local copy (CFG-1, CFG-5). Errors carry `detail` (CFG-2). */
    suspend fun update(patch: ConfigPatch): ApiResult<ServerConfig> {
        val r = api.updateConfig(patch)
        if (r is ApiResult.Ok) _config.value = LoadState(r.value)
        return r
    }

    fun reset() { _config.value = LoadState() }

    companion object {
        /**
         * CFG-4 (fixes inv03 §8 bug 1): `enabled = []` means all enabled. Unchecking one server while the
         * list is empty writes every other server; checking the last unchecked one writes `[]`.
         */
        fun toggleMcp(all: List<String>, enabled: List<String>, name: String, on: Boolean): List<String> {
            val current = if (enabled.isEmpty()) all.toSet() else enabled.toSet()
            val next = if (on) current + name else current - name
            return if (all.isNotEmpty() && next.containsAll(all)) emptyList() else all.filter { it in next } + (next - all.toSet()).sorted()
        }

        fun isMcpEnabled(enabled: List<String>, name: String) = enabled.isEmpty() || name in enabled
    }
}

/** Streamed uploads (§6.15); the 1 MiB nginx limit maps to a clear message (ApiResult.errorMessage). */
class UploadRepository(private val client: UploadClient) {
    suspend fun upload(source: UploadSource, onProgress: (Long, Long) -> Unit = { _, _ -> }): ApiResult<UploadResult> =
        client.upload(source, onProgress)
}

/** A share received by the Activity (inv03 §1.1 share intents). Kept here so a cold-launch share is not lost. */
sealed interface SharePayload {
    data class Text(val text: String, val subject: String?) : SharePayload
    data class Files(val uris: List<String>, val subject: String?) : SharePayload
}

/** Seam for B-09's `ShareSheet` (spec 14 §2.8): the shell parses intents into [offer]; the sheet [take]s them. */
class ShareRepository {
    private val _pending = MutableStateFlow<SharePayload?>(null)
    val pending: StateFlow<SharePayload?> = _pending.asStateFlow()

    fun offer(payload: SharePayload) { _pending.value = payload }

    fun take(): SharePayload? = _pending.value.also { _pending.value = null }
}

/**
 * What the shell shows of voice (top-bar speaker state, "Voice · Listening" subtitle). The real
 * source is the process-scoped voice host (A-08, wired by B-09); until then [Idle].
 */
interface VoicePresence {
    val state: StateFlow<VoiceSessionState>

    object Idle : VoicePresence {
        override val state: StateFlow<VoiceSessionState> = MutableStateFlow(VoiceSessionState())
    }
}

/** [ServerScanner] over the device's connected /24 subnets (DISC-1 probe in [ServerDiscovery]). */
class LanScanner(private val discovery: ServerDiscovery, private val network: NetworkMonitor) : ServerScanner {
    override suspend fun scan(): List<DiscoveredServer> =
        network.localSubnets().flatMap { discovery.scan(it) }.distinctBy { it.host to it.port }
}
