package com.assistant.core.data

import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.emptyPreferences
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import okio.ByteString.Companion.encodeUtf8
import java.util.concurrent.CopyOnWriteArrayList

/**
 * A scripted Archie backend on MockWebServer: REST (`/api/sessions`, `pool/live`, `messages`, `close`)
 * and both WebSockets. `start` is answered with `session_started` (binary frame, as the server does,
 * T-1); a `reattach` start for a session missing from [poolJson] gets `error{session_closed}` (OPEN-2). Every request and every client frame is recorded so tests can assert what was (not) sent.
 */
class FakeBackend {
    val server = MockWebServer()

    /** `"METHOD /path"` of every HTTP request, in order. */
    val requests = CopyOnWriteArrayList<String>()

    /** `(endpoint, text)` of every client WS frame. */
    val frames = CopyOnWriteArrayList<Pair<String, String>>()
    val orchestratorSockets = CopyOnWriteArrayList<WebSocket>()
    val agentSockets = CopyOnWriteArrayList<WebSocket>()

    /** The scripted open set; sessions started here by a user `start` (no `reattach`) are added to it until closed. */
    @Volatile var poolJson = "[]"
    private val startedHere = java.util.concurrent.ConcurrentHashMap.newKeySet<String>()
    @Volatile var sessionsJson = "[]"
    @Volatile var visualizationsJson = "[]"
    @Volatile var memoryTreeJson = "[]"
    @Volatile var messagesJson = """{"messages":[],"total_count":0,"has_more":false,"start_index":0}"""

    /** `POST /api/sessions/{id}/permission` answer (§6.9): code + JSON body; bodies received are recorded. */
    @Volatile var permissionResponse: MockResponse? = null

    /** The orchestrator `session_started.jsonl_id` for a `start` with this `resume_sdk_id` (null: none). */
    @Volatile var orchJsonl: (resumeSdkId: String?) -> String = { "JSONL" }
    val permissionBodies = CopyOnWriteArrayList<String>()

    val url: String get() = "ws://${server.hostName}:${server.port}"

    fun start(): FakeBackend {
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                val path = request.path.orEmpty()
                requests += "${request.method} $path"
                return when {
                    path == "/api/orchestrator/chat" -> MockResponse().withWebSocketUpgrade(listener("orch", orchestratorSockets))
                    path == "/api/sessions/chat" -> MockResponse().withWebSocketUpgrade(listener("agent", agentSockets))
                    path == "/api/sessions/pool/live" -> json(poolBody())
                    path == "/api/sessions" -> json(sessionsJson)
                    path == "/api/visualizations" -> json(visualizationsJson)
                    path == "/api/memory/tree" -> json(memoryTreeJson)
                    path.startsWith("/api/sessions/") && path.contains("/messages") -> json(messagesJson)
                    path.endsWith("/close") -> {
                        startedHere.remove(path.removePrefix("/api/sessions/").removeSuffix("/close"))
                        MockResponse().setResponseCode(204)
                    }
                    path.endsWith("/permission") && request.method == "POST" -> {
                        permissionBodies += request.body.readUtf8()
                        permissionResponse ?: json("""{"detail":"Not Found"}""").setResponseCode(404)
                    }
                    path == "/api/auth/status" -> json("""{"authenticated":true,"headless":true}""")
                    else -> MockResponse().setResponseCode(404)
                }
            }
        }
        server.start()
        return this
    }

    fun shutdown() = runCatching { server.shutdown() }

    fun closeRequests(): List<String> = requests.filter { it.endsWith("/close") }

    fun stopFrames(): List<String> = frames.map { it.second }.filter { it.contains("\"type\":\"stop\"") || it.contains("\"type\":\"voice_stop\"") }

    /** Pushes a server frame to every orchestrator socket (watcher events, T-7). */
    fun pushOrchestrator(json: String) = orchestratorSockets.forEach { it.send(json.encodeUtf8()) }

    /** Another device closed [localId]: it leaves the open set (pass the matching [poolJson] too). */
    fun closedElsewhere(localId: String) { startedHere.remove(localId) }

    private fun inPool(localId: String) = localId in startedHere || poolJson.contains("\"local_id\":\"$localId\"")

    private fun poolBody(): String {
        val extra = startedHere.filterNot { poolJson.contains("\"local_id\":\"$it\"") }.joinToString(",") {
            """{"local_id":"$it","sdk_session_id":null,"status":"idle","cost":0.0,"turns":0,"title":null,"is_orchestrator":false}"""
        }
        if (extra.isEmpty()) return poolJson
        val base = poolJson.trim().removeSuffix("]").trimEnd()
        return if (base == "[") "[$extra]" else "$base,$extra]"
    }

    private fun json(body: String) = MockResponse().setHeader("Content-Type", "application/json").setBody(body)

    private fun listener(endpoint: String, sockets: MutableList<WebSocket>) = object : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) { sockets += webSocket }
        override fun onMessage(webSocket: WebSocket, text: String) {
            frames += endpoint to text
            if (text.contains("\"type\":\"start\"")) {
                val localId = Regex("\"local_id\":\"([^\"]+)\"").find(text)?.groupValues?.get(1) ?: "x"
                val resume = Regex("\"resume_sdk_id\":\"([^\"]+)\"").find(text)?.groupValues?.get(1)
                if (text.contains("\"reattach\":true")) {
                    if (!inPool(localId)) {
                        webSocket.send("""{"type":"error","error":"session_closed","detail":"not open"}""".encodeUtf8())
                        return
                    }
                } else if (endpoint == "agent") startedHere += localId
                val jsonl = if (endpoint == "orch") ""","jsonl_id":"${orchJsonl(resume)}"""" else ""
                webSocket.send("""{"type":"session_started","session_id":"$localId"$jsonl}""".encodeUtf8())
            }
        }
        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) { webSocket.close(1000, null) }
    }
}

/** In-memory DataStore for SettingsStore. */
class MemoryDataStore(initial: Preferences = emptyPreferences()) : DataStore<Preferences> {
    private val state = MutableStateFlow(initial)
    private val mutex = Mutex()
    override val data: Flow<Preferences> = state
    override suspend fun updateData(transform: suspend (t: Preferences) -> Preferences): Preferences = mutex.withLock {
        transform(state.value).also { state.value = it }
    }
}

/** Polls [cond] until true (real time; the sockets are real). */
fun eventually(timeoutMs: Long = 10_000, message: () -> String = { "condition not met" }, cond: () -> Boolean) {
    val end = System.currentTimeMillis() + timeoutMs
    while (System.currentTimeMillis() < end) {
        if (cond()) return
        Thread.sleep(20)
    }
    throw AssertionError(message())
}
