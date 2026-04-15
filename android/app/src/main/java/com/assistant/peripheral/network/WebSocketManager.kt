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

class WebSocketManager {

    companion object {
        private const val TAG = "WebSocketManager"
        private const val RECONNECT_DELAY_MS = 3000L
        private const val PING_INTERVAL_MS = 30000L
    }

    private var webSocket: WebSocket? = null
    private var client: OkHttpClient? = null
    private var currentUrl: String? = null
    private var shouldReconnect = false
    private var currentLocalId: String? = null
    private var currentEndpoint: WebSocketEndpoint = WebSocketEndpoint.ORCHESTRATOR

    private val _connectionState = MutableStateFlow<ConnectionState>(ConnectionState.Disconnected)
    val connectionState: StateFlow<ConnectionState> = _connectionState.asStateFlow()

    private val _events = MutableSharedFlow<WebSocketEvent>(extraBufferCapacity = 64)
    val events: SharedFlow<WebSocketEvent> = _events.asSharedFlow()

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    fun connect(url: String, localId: String? = null, endpoint: WebSocketEndpoint = WebSocketEndpoint.ORCHESTRATOR) {
        if (_connectionState.value is ConnectionState.Connected ||
            _connectionState.value is ConnectionState.Connecting) {
            return
        }

        currentUrl = url
        currentLocalId = localId
        currentEndpoint = endpoint
        shouldReconnect = true
        _connectionState.value = ConnectionState.Connecting

        client = OkHttpClient.Builder()
            .pingInterval(PING_INTERVAL_MS, TimeUnit.MILLISECONDS)
            .readTimeout(0, TimeUnit.MILLISECONDS)
            .build()

        val wsUrl = buildWebSocketUrl(url, endpoint)
        val request = Request.Builder()
            .url(wsUrl)
            .build()

        Log.d(TAG, "Connecting to: $wsUrl (endpoint: $endpoint)")

        webSocket = client?.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                Log.d(TAG, "WebSocket connected")
                _connectionState.value = ConnectionState.Connected
                _events.tryEmit(WebSocketEvent.Connected)
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                Log.d(TAG, "Received text: $text")
                parseMessage(text)
            }

            override fun onMessage(webSocket: WebSocket, bytes: okio.ByteString) {
                val text = bytes.utf8()
                Log.d(TAG, "Received binary: $text")
                parseMessage(text)
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                Log.d(TAG, "WebSocket closing: $code - $reason")
                webSocket.close(1000, null)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                Log.d(TAG, "WebSocket closed: $code - $reason")
                handleDisconnect()
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.e(TAG, "WebSocket failure: ${t.message}", t)
                _connectionState.value = ConnectionState.Error(t.message ?: "Connection failed")
                _events.tryEmit(WebSocketEvent.Error(t.message ?: "Connection failed"))
                handleDisconnect()
            }
        })
    }

    private fun buildWebSocketUrl(baseUrl: String, endpoint: WebSocketEndpoint = WebSocketEndpoint.ORCHESTRATOR): String {
        // Convert HTTP to WS and ensure correct API path
        var url = baseUrl
            .replace("http://", "ws://")
            .replace("https://", "wss://")

        // Remove trailing slash
        url = url.trimEnd('/')

        // Remove any existing API paths
        url = url.replace("/api/orchestrator/chat", "")
            .replace("/api/sessions/chat", "")

        // Add WebSocket path based on endpoint type
        url = when (endpoint) {
            WebSocketEndpoint.ORCHESTRATOR -> "$url/api/orchestrator/chat"
            WebSocketEndpoint.AGENT -> "$url/api/sessions/chat"
        }

        return url
    }

    private fun handleDisconnect() {
        _connectionState.value = ConnectionState.Disconnected
        _events.tryEmit(WebSocketEvent.Disconnected)

        if (shouldReconnect) {
            scope.launch {
                delay(RECONNECT_DELAY_MS)
                currentUrl?.let { connect(it, currentLocalId, currentEndpoint) }
            }
        }
    }

    fun disconnect() {
        shouldReconnect = false
        webSocket?.close(1000, "User disconnected")
        webSocket = null
        client?.dispatcher?.executorService?.shutdown()
        client = null
        _connectionState.value = ConnectionState.Disconnected
    }

    fun send(message: WebSocketMessage) {
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
        Log.d(TAG, "Sending: $jsonString")
        webSocket?.send(jsonString)
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
