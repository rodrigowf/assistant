package com.assistant.peripheral.network

import android.util.Log
import com.assistant.peripheral.data.*
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.*
import okhttp3.*
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

enum class WebSocketEndpoint {
    ORCHESTRATOR,  // /api/orchestrator/chat - single session, multi-model
    AGENT          // /api/sessions/chat - Claude Code sessions
}

/**
 * Holds up to one socket per [WebSocketEndpoint] so the orchestrator (which may
 * be running a realtime voice conversation) keeps streaming even while the user
 * opens a Claude Code session in the agent tab.
 *
 * The public [connectionState] tracks the orchestrator socket — that's what the
 * top-level UI uses to mean "are we connected to the server". Per-endpoint
 * Connected/Disconnected events are emitted on [events] so callers that care
 * about the agent socket can react too.
 */
class WebSocketManager {

    companion object {
        private const val TAG = "WebSocketManager"
        private const val RECONNECT_DELAY_MS = 3000L
        private const val PING_INTERVAL_MS = 30000L
    }

    private data class Connection(
        var webSocket: WebSocket? = null,
        var client: OkHttpClient? = null,
        var url: String? = null,
        var localId: String? = null,
        var shouldReconnect: Boolean = false,
        val state: MutableStateFlow<ConnectionState> = MutableStateFlow(ConnectionState.Disconnected)
    )

    private val connections: Map<WebSocketEndpoint, Connection> = mapOf(
        WebSocketEndpoint.ORCHESTRATOR to Connection(),
        WebSocketEndpoint.AGENT to Connection()
    )

    /** Orchestrator connection state — preserves the legacy single-socket contract for the UI. */
    val connectionState: StateFlow<ConnectionState> =
        connections.getValue(WebSocketEndpoint.ORCHESTRATOR).state.asStateFlow()

    fun connectionState(endpoint: WebSocketEndpoint): StateFlow<ConnectionState> =
        connections.getValue(endpoint).state.asStateFlow()

    private val _events = MutableSharedFlow<WebSocketEvent>(extraBufferCapacity = 64)
    val events: SharedFlow<WebSocketEvent> = _events.asSharedFlow()

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    fun connect(
        url: String,
        localId: String? = null,
        endpoint: WebSocketEndpoint = WebSocketEndpoint.ORCHESTRATOR
    ) {
        val conn = connections.getValue(endpoint)
        if (conn.state.value is ConnectionState.Connected ||
            conn.state.value is ConnectionState.Connecting) {
            return
        }

        conn.url = url
        conn.localId = localId
        conn.shouldReconnect = true
        conn.state.value = ConnectionState.Connecting

        val client = OkHttpClient.Builder()
            .pingInterval(PING_INTERVAL_MS, TimeUnit.MILLISECONDS)
            .readTimeout(0, TimeUnit.MILLISECONDS)
            .build()
        conn.client = client

        val wsUrl = buildWebSocketUrl(url, endpoint)
        val request = Request.Builder()
            .url(wsUrl)
            .build()

        Log.d(TAG, "Connecting to: $wsUrl (endpoint: $endpoint)")

        conn.webSocket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                Log.d(TAG, "WebSocket connected ($endpoint)")
                conn.state.value = ConnectionState.Connected
                _events.tryEmit(WebSocketEvent.Connected(endpoint.name))
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                Log.d(TAG, "Received text ($endpoint): $text")
                parseMessage(text)
            }

            override fun onMessage(webSocket: WebSocket, bytes: okio.ByteString) {
                val text = bytes.utf8()
                Log.d(TAG, "Received binary ($endpoint): $text")
                parseMessage(text)
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                Log.d(TAG, "WebSocket closing ($endpoint): $code - $reason")
                webSocket.close(1000, null)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                Log.d(TAG, "WebSocket closed ($endpoint): $code - $reason")
                handleDisconnect(endpoint)
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.e(TAG, "WebSocket failure ($endpoint): ${t.message}", t)
                conn.state.value = ConnectionState.Error(t.message ?: "Connection failed")
                _events.tryEmit(WebSocketEvent.Error(t.message ?: "Connection failed"))
                handleDisconnect(endpoint)
            }
        })
    }

    private fun buildWebSocketUrl(baseUrl: String, endpoint: WebSocketEndpoint): String {
        var url = baseUrl
            .replace("http://", "ws://")
            .replace("https://", "wss://")

        url = url.trimEnd('/')

        url = url.replace("/api/orchestrator/chat", "")
            .replace("/api/sessions/chat", "")

        url = when (endpoint) {
            WebSocketEndpoint.ORCHESTRATOR -> "$url/api/orchestrator/chat"
            WebSocketEndpoint.AGENT -> "$url/api/sessions/chat"
        }

        return url
    }

    private fun handleDisconnect(endpoint: WebSocketEndpoint) {
        val conn = connections.getValue(endpoint)
        conn.state.value = ConnectionState.Disconnected
        _events.tryEmit(WebSocketEvent.Disconnected(endpoint.name))

        if (conn.shouldReconnect) {
            scope.launch {
                delay(RECONNECT_DELAY_MS)
                conn.url?.let { connect(it, conn.localId, endpoint) }
            }
        }
    }

    /** Disconnect a specific endpoint (or all when null). */
    fun disconnect(endpoint: WebSocketEndpoint? = null) {
        val targets = if (endpoint == null) connections.keys else setOf(endpoint)
        for (ep in targets) {
            val conn = connections.getValue(ep)
            conn.shouldReconnect = false
            conn.webSocket?.close(1000, "User disconnected")
            conn.webSocket = null
            conn.client?.dispatcher?.executorService?.shutdown()
            conn.client = null
            conn.state.value = ConnectionState.Disconnected
        }
    }

    /** Returns true if the given endpoint's socket is currently in [ConnectionState.Connected]. */
    fun isConnected(endpoint: WebSocketEndpoint): Boolean =
        connections.getValue(endpoint).state.value is ConnectionState.Connected

    fun send(
        message: WebSocketMessage,
        endpoint: WebSocketEndpoint = WebSocketEndpoint.ORCHESTRATOR
    ) {
        val json = when (message) {
            is WebSocketMessage.Start -> JSONObject().apply {
                put("type", "start")
                message.localId?.let { put("local_id", it) }
                message.resumeSdkId?.let { put("resume_sdk_id", it) }
            }
            is WebSocketMessage.Stop -> JSONObject().apply {
                put("type", "stop")
            }
            is WebSocketMessage.VoiceStart -> JSONObject().apply {
                put("type", "voice_start")
                message.localId?.let { put("local_id", it) }
                message.resumeSdkId?.let { put("resume_sdk_id", it) }
            }
            is WebSocketMessage.VoiceStop -> JSONObject().apply {
                put("type", "voice_stop")
            }
            is WebSocketMessage.VoiceEvent -> JSONObject().apply {
                put("type", "voice_event")
                put("event", JSONObject(message.event))
            }
            is WebSocketMessage.Send -> JSONObject().apply {
                put("type", "send")
                put("text", message.text)
            }
            is WebSocketMessage.SendAudio -> JSONObject().apply {
                put("type", "send_audio")
                put("audio", message.audioBase64)
                put("format", message.format)
                message.text?.let { put("text", it) }
            }
            is WebSocketMessage.Interrupt -> JSONObject().apply {
                put("type", "interrupt")
            }
            is WebSocketMessage.Compact -> JSONObject().apply {
                put("type", "compact")
            }
            is WebSocketMessage.SetModel -> JSONObject().apply {
                put("type", "set_model")
                put("model", message.model)
            }
            is WebSocketMessage.GetModel -> JSONObject().apply {
                put("type", "get_model")
            }
            is WebSocketMessage.GetModels -> JSONObject().apply {
                put("type", "get_models")
            }
        }

        val jsonString = json.toString()
        Log.d(TAG, "Sending ($endpoint): $jsonString")
        connections.getValue(endpoint).webSocket?.send(jsonString)
    }

    @Suppress("UNCHECKED_CAST")
    private fun parseMessage(text: String) {
        try {
            val json = JSONObject(text)
            val type = json.optString("type", "")

            when (type) {
                // Session lifecycle
                "session_started" -> {
                    val sessionId = json.optString("session_id", "")
                    val voice = json.optBoolean("voice", false)
                    val voiceUpdate = json.optJSONObject("voice_session_update")?.let { jsonObjectToMap(it) }
                    _events.tryEmit(WebSocketEvent.SessionStarted(sessionId, voice, voiceUpdate))
                }
                "session_stopped" -> {
                    _events.tryEmit(WebSocketEvent.SessionStopped)
                }

                // Status updates
                "status" -> {
                    val status = json.optString("status", "")
                    _events.tryEmit(WebSocketEvent.Status(status))
                }

                // Text streaming
                "text_delta" -> {
                    val delta = json.optString("text", "")
                    val messageId = json.optString("message_id", null)
                    _events.tryEmit(WebSocketEvent.TextDelta(delta, messageId))
                }
                "text_complete" -> {
                    val completeText = json.optString("text", "")
                    _events.tryEmit(WebSocketEvent.TextComplete(completeText))
                }

                // Thinking (extended thinking for o1 models)
                "thinking_delta" -> {
                    val delta = json.optString("text", "")
                    _events.tryEmit(WebSocketEvent.ThinkingDelta(delta))
                }
                "thinking_complete" -> {
                    val completeText = json.optString("text", "")
                    _events.tryEmit(WebSocketEvent.ThinkingComplete(completeText))
                }

                // Tool events
                "tool_use" -> {
                    val toolUseId = json.optString("tool_use_id", "")
                    val toolName = json.optString("tool_name", "")
                    val toolInput = jsonObjectToMap(json.optJSONObject("tool_input"))
                    _events.tryEmit(WebSocketEvent.ToolUse(toolUseId, toolName, toolInput))
                }
                "tool_executing" -> {
                    val toolUseId = json.optString("tool_use_id", "")
                    val toolName = json.optString("tool_name", "")
                    _events.tryEmit(WebSocketEvent.ToolExecuting(toolUseId, toolName))
                }
                "tool_progress" -> {
                    val toolUseId = json.optString("tool_use_id", "")
                    val message = json.optString("message", "")
                    _events.tryEmit(WebSocketEvent.ToolProgress(toolUseId, message))
                }
                "tool_result" -> {
                    val toolUseId = json.optString("tool_use_id", "")
                    val output = json.optString("output", "")
                    val isError = json.optBoolean("is_error", false)
                    _events.tryEmit(WebSocketEvent.ToolResult(toolUseId, output, isError))
                }

                // Turn complete
                "turn_complete" -> {
                    val inputTokens = json.optInt("input_tokens", 0)
                    val outputTokens = json.optInt("output_tokens", 0)
                    _events.tryEmit(WebSocketEvent.TurnComplete(inputTokens, outputTokens))
                }

                // Voice events
                "voice_command" -> {
                    val command = jsonObjectToMap(json.optJSONObject("command"))
                    _events.tryEmit(WebSocketEvent.VoiceCommand(command))
                }
                "voice_stopped" -> {
                    _events.tryEmit(WebSocketEvent.VoiceStopped)
                }

                // Compact
                "compact_complete" -> {
                    val summary = json.optString("summary", "")
                    _events.tryEmit(WebSocketEvent.CompactComplete(summary))
                }

                // Error
                "error" -> {
                    val errorMsg = json.optString("error", "Unknown error")
                    val detail = json.optString("detail", null)
                    _events.tryEmit(WebSocketEvent.Error(errorMsg, detail))
                }

                // Legacy compatibility - map old events to new ones
                "content_block_delta" -> {
                    val delta = json.optString("delta", json.optString("text", ""))
                    _events.tryEmit(WebSocketEvent.TextDelta(delta))
                }
                "message_start" -> {
                    val messageId = json.optString("message_id", System.currentTimeMillis().toString())
                    _events.tryEmit(WebSocketEvent.MessageStart(messageId))
                }
                "message_end", "message_stop" -> {
                    _events.tryEmit(WebSocketEvent.MessageEnd)
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error parsing message: ${e.message}", e)
        }
    }

    private fun jsonObjectToMap(json: JSONObject?): Map<String, Any?> {
        if (json == null) return emptyMap()
        val map = mutableMapOf<String, Any?>()
        val keys = json.keys()
        while (keys.hasNext()) {
            val key = keys.next()
            val value = json.opt(key)
            map[key] = when (value) {
                is JSONObject -> jsonObjectToMap(value)
                is JSONArray -> jsonArrayToList(value)
                JSONObject.NULL -> null
                else -> value
            }
        }
        return map
    }

    private fun jsonArrayToList(array: JSONArray): List<Any?> {
        val list = mutableListOf<Any?>()
        for (i in 0 until array.length()) {
            val value = array.opt(i)
            list.add(when (value) {
                is JSONObject -> jsonObjectToMap(value)
                is JSONArray -> jsonArrayToList(value)
                JSONObject.NULL -> null
                else -> value
            })
        }
        return list
    }

    fun release() {
        scope.cancel()
        disconnect()
    }
}
