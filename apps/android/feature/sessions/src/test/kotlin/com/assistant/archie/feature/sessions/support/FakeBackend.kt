package com.assistant.archie.feature.sessions.support

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
 * A scripted Archie backend on MockWebServer (extends the :core:data test backend with the B-06
 * mutations): REST list / pool / messages / close / rename / duplicate / fork / delete, and both
 * WebSockets. `start` is answered with `session_started`; with [raceOrchestrator] set, an orchestrator
 * `start` for another id loses the race (`error{orchestrator_active}`) and that orchestrator appears in
 * the pool, as when another device started Archie after our pool check (§6.11). Like the server
 * (spec 12 OPEN-2) a user `start` opens its session in the pool, and a `reattach` start for a
 * session the pool does not have gets `error{session_closed}`.
 *
 * Every request (with its body) and every client frame is recorded. Nothing here talks to a real
 * server: B-06 mutations are never run against the live Jetson.
 */
class FakeBackend {
    val server = MockWebServer()

    /** `"METHOD /path"` of every HTTP request, in order. */
    val requests = CopyOnWriteArrayList<String>()
    val bodies = CopyOnWriteArrayList<Pair<String, String>>()

    /** `(endpoint, text)` of every client WS frame. */
    val frames = CopyOnWriteArrayList<Pair<String, String>>()
    val orchestratorSockets = CopyOnWriteArrayList<WebSocket>()
    val agentSockets = CopyOnWriteArrayList<WebSocket>()

    @Volatile var poolJson = "[]"
    @Volatile var sessionsJson = "[]"
    @Volatile var messagesJson = """{"messages":[],"total_count":0,"has_more":false,"start_index":0}"""
    @Volatile var duplicateId = "DUP1"
    @Volatile var forkId = "FORK1"
    @Volatile var renameCode = 204

    /** Pool row of an orchestrator that wins the next start race (see the class comment). */
    @Volatile var raceOrchestrator: String? = null

    val url: String get() = "ws://${server.hostName}:${server.port}"

    fun start(): FakeBackend {
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                val path = request.path.orEmpty()
                val line = "${request.method} $path"
                requests += line
                val body = request.body.readUtf8()
                if (body.isNotEmpty()) bodies += line to body
                return when {
                    path == "/api/orchestrator/chat" -> MockResponse().withWebSocketUpgrade(listener("orch", orchestratorSockets))
                    path == "/api/sessions/chat" -> MockResponse().withWebSocketUpgrade(listener("agent", agentSockets))
                    path == "/api/sessions/pool/live" -> json(poolJson)
                    path == "/api/sessions" -> json(sessionsJson)
                    path.contains("/messages") -> json(messagesJson)
                    path.endsWith("/close") -> {
                        val id = path.removePrefix("/api/sessions/").removeSuffix("/close")
                        dropFromPool(id)
                        MockResponse().setResponseCode(204)
                    }
                    path.endsWith("/rename") -> MockResponse().setResponseCode(renameCode).apply {
                        if (renameCode >= 400) setHeader("Content-Type", "application/json").setBody("""{"detail":"Title is locked"}""")
                    }
                    path.endsWith("/duplicate") -> json("""{"session_id":"$duplicateId"}""")
                    path.endsWith("/fork") -> json("""{"session_id":"$forkId"}""")
                    request.method == "DELETE" -> MockResponse().setResponseCode(204)
                    else -> MockResponse().setResponseCode(404)
                }
            }
        }
        server.start()
        return this
    }

    fun shutdown() = runCatching { server.shutdown() }

    fun closeRequests(): List<String> = requests.filter { it.endsWith("/close") }

    fun starts(endpoint: String): List<String> =
        frames.filter { it.first == endpoint && it.second.contains("\"type\":\"start\"") }.map { it.second }

    private fun dropFromPool(localId: String) {
        poolJson = Regex("\\{[^{}]*\"local_id\":\"${Regex.escape(localId)}\"[^{}]*\\}").replace(poolJson, "")
            .replace(Regex(",\\s*,"), ",").replace("[,", "[").replace(",]", "]")
    }

    private fun addToPool(localId: String, sdkId: String?, orch: Boolean) {
        val row = """{"local_id":"$localId","sdk_session_id":${(sdkId ?: localId.takeIf { orch })?.let { "\"$it\"" } ?: "null"},"status":"idle","cost":0.0,"turns":0,"title":null,"is_orchestrator":$orch}"""
        val base = poolJson.trim().removeSuffix("]").trimEnd()
        poolJson = if (base == "[") "[$row]" else "$base,$row]"
    }

    private fun json(body: String) = MockResponse().setHeader("Content-Type", "application/json").setBody(body)

    private fun listener(endpoint: String, sockets: MutableList<WebSocket>) = object : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) { sockets += webSocket }
        override fun onMessage(webSocket: WebSocket, text: String) {
            frames += endpoint to text
            if (!text.contains("\"type\":\"start\"")) return
            val localId = Regex("\"local_id\":\"([^\"]+)\"").find(text)?.groupValues?.get(1) ?: "x"
            val race = raceOrchestrator
            if (endpoint == "orch" && race != null && race != localId) {
                raceOrchestrator = null
                poolJson = orchRow(race, "${race}_JSONL")
                webSocket.send("""{"type":"error","error":"orchestrator_active","detail":"An orchestrator is already active"}""".encodeUtf8())
                return
            }
            val resume = Regex("\"resume_sdk_id\":\"([^\"]+)\"").find(text)?.groupValues?.get(1)
            val listed = poolJson.contains("\"local_id\":\"$localId\"")
            if (text.contains("\"reattach\":true") && !listed) {
                webSocket.send("""{"type":"error","error":"session_closed","detail":"not open"}""".encodeUtf8())
                return
            }
            if (!listed) addToPool(localId, resume, orch = endpoint == "orch")
            val jsonl = if (endpoint == "orch") ""","jsonl_id":"${resume ?: localId}"""" else ""
            webSocket.send("""{"type":"session_started","session_id":"$localId"$jsonl}""".encodeUtf8())
        }
        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) { webSocket.close(1000, null) }
    }

    companion object {
        fun orchRow(localId: String, sdkId: String, title: String = "Living-room TV") =
            """[{"local_id":"$localId","sdk_session_id":"$sdkId","status":"idle","cost":0.0,"turns":0,"title":"$title","is_orchestrator":true}]"""

        fun session(id: String, title: String, last: String, orch: Boolean, count: Int = 6, provider: String = "claude", localId: String? = null) =
            """{"session_id":"$id","started_at":"$last","last_activity":"$last","title":"$title","message_count":$count,"is_orchestrator":$orch,"provider":"$provider","local_id":${localId?.let { "\"$it\"" } ?: "null"}}"""
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
