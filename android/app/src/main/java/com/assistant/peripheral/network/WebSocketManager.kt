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

    /**
     * Cheap heuristic to skip logging audio frames.  Looks for the
     * `voice_audio_in` / `voice_audio_out` type prefix near the start
     * of the message — much faster than parsing JSON for every frame.
     */
    private fun isAudioPayload(text: String): Boolean {
        if (text.length < 32) return false
        val head = text.substring(0, minOf(64, text.length))
        return head.contains("\"voice_audio_in\"") || head.contains("\"voice_audio_out\"")
    }

    companion object {
        private const val TAG = "WebSocketManager"
        private const val RECONNECT_DELAY_MS = 3000L
        // OkHttp's WS protocol-level pingInterval — 30s is the okhttp
        // recommendation. The library sends a PING frame every interval
        // and closes the WS with "1011 keepalive ping timeout" if no
        // PONG arrives within the same window.
        private const val PING_INTERVAL_MS = 30000L
    }

    private data class Connection(
        var webSocket: WebSocket? = null,
        var client: OkHttpClient? = null,
        var url: String? = null,
        var localId: String? = null,
        var shouldReconnect: Boolean = false,
        val state: MutableStateFlow<ConnectionState> = MutableStateFlow(ConnectionState.Disconnected),
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

    /**
     * Per-endpoint event stream. Every emission carries the endpoint that
     * produced the event so consumers can route updates to the correct
     * per-tab state bucket and never cross-contaminate (e.g. orchestrator
     * voice text streaming into a Claude Code session view).
     */
    private val _events = MutableSharedFlow<Pair<WebSocketEndpoint, WebSocketEvent>>(extraBufferCapacity = 64)
    val events: SharedFlow<Pair<WebSocketEndpoint, WebSocketEvent>> = _events.asSharedFlow()

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
                _events.tryEmit(endpoint to WebSocketEvent.Connected)
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                // Skip logging high-rate audio frames; they're 4 KB each
                // at ~50 Hz and the logging cost alone causes UI freezes.
                if (!isAudioPayload(text)) {
                    Log.d(TAG, "Received text ($endpoint): $text")
                }
                parseMessage(text, endpoint)
            }

            override fun onMessage(webSocket: WebSocket, bytes: okio.ByteString) {
                val text = bytes.utf8()
                if (!isAudioPayload(text)) {
                    Log.d(TAG, "Received binary ($endpoint): $text")
                }
                parseMessage(text, endpoint)
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
                _events.tryEmit(endpoint to WebSocketEvent.Error(t.message ?: "Connection failed"))
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
        _events.tryEmit(endpoint to WebSocketEvent.Disconnected)

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
                // Voice provider/model/voice/language — when present,
                // the backend overrides the configured defaults.
                message.voiceProvider?.let { put("voice_provider", it) }
                message.voiceModel?.let { put("voice_model", it) }
                message.voiceName?.let { put("voice_name", it) }
                message.voiceTranscriptionLanguage?.let { put("voice_transcription_language", it) }
                message.voiceEndpoint?.takeIf { it.isNotBlank() }?.let {
                    put("voice_endpoint", it)
                }
            }
            is WebSocketMessage.VoiceStop -> JSONObject().apply {
                put("type", "voice_stop")
            }
            is WebSocketMessage.VoiceEvent -> JSONObject().apply {
                put("type", "voice_event")
                put("event", JSONObject(message.event))
            }
            is WebSocketMessage.VoiceAudioIn -> JSONObject().apply {
                put("type", "voice_audio_in")
                put("audio", message.audioBase64)
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
        // Don't log audio chunks — they fire ~50/s with ~4KB payloads
        // each, which floods logcat and causes UI freezes via the
        // logging subsystem alone.
        if (message !is WebSocketMessage.VoiceAudioIn) {
            Log.d(TAG, "Sending ($endpoint): $jsonString")
        }
        connections.getValue(endpoint).webSocket?.send(jsonString)
    }

    @Suppress("UNCHECKED_CAST")
    private fun parseMessage(text: String, endpoint: WebSocketEndpoint) {
        try {
            val json = JSONObject(text)
            val type = json.optString("type", "")
            fun emit(ev: WebSocketEvent) { _events.tryEmit(endpoint to ev) }

            when (type) {
                // Heartbeat — backend sends {"type":"ping"} every 15s to
                // keep the A300M's WiFi radio awake long enough for okhttp
                // to receive its WS PONG. We don't need to do anything
                // with it; receiving the bytes already wakes the radio.
                // Silently consume — don't emit, don't ack (the bytes
                // themselves are the ack).
                "ping" -> { /* no-op */ }
                "pong" -> { /* no-op — reserved for future client→server ping */ }

                // Session lifecycle
                "session_started" -> {
                    val sessionId = json.optString("session_id", "")
                    val voice = json.optBoolean("voice", false)
                    val voiceUpdate = json.optJSONObject("voice_session_update")?.let { jsonObjectToMap(it) }
                    emit(WebSocketEvent.SessionStarted(sessionId, voice, voiceUpdate))
                }
                "session_stopped" -> emit(WebSocketEvent.SessionStopped)

                // Status updates
                "status" -> emit(WebSocketEvent.Status(json.optString("status", "")))

                // Text streaming
                "text_delta" -> {
                    val delta = json.optString("text", "")
                    val messageId = json.optString("message_id", null)
                    emit(WebSocketEvent.TextDelta(delta, messageId))
                }
                "text_complete" -> emit(WebSocketEvent.TextComplete(json.optString("text", "")))

                // Thinking (extended thinking for o1 models)
                "thinking_delta" -> emit(WebSocketEvent.ThinkingDelta(json.optString("text", "")))
                "thinking_complete" -> emit(WebSocketEvent.ThinkingComplete(json.optString("text", "")))

                // Tool events
                "tool_use" -> {
                    val toolUseId = json.optString("tool_use_id", "")
                    val toolName = json.optString("tool_name", "")
                    val toolInput = jsonObjectToMap(json.optJSONObject("tool_input"))
                    emit(WebSocketEvent.ToolUse(toolUseId, toolName, toolInput))
                }
                "tool_executing" -> {
                    val toolUseId = json.optString("tool_use_id", "")
                    val toolName = json.optString("tool_name", "")
                    emit(WebSocketEvent.ToolExecuting(toolUseId, toolName))
                }
                "tool_progress" -> {
                    val toolUseId = json.optString("tool_use_id", "")
                    val message = json.optString("message", "")
                    emit(WebSocketEvent.ToolProgress(toolUseId, message))
                }
                "tool_result" -> {
                    val toolUseId = json.optString("tool_use_id", "")
                    val output = json.optString("output", "")
                    val isError = json.optBoolean("is_error", false)
                    emit(WebSocketEvent.ToolResult(toolUseId, output, isError))
                }

                // Turn complete
                "turn_complete" -> {
                    val inputTokens = json.optInt("input_tokens", 0)
                    val outputTokens = json.optInt("output_tokens", 0)
                    emit(WebSocketEvent.TurnComplete(inputTokens, outputTokens))
                }

                // Voice events
                "voice_command" -> {
                    val command = jsonObjectToMap(json.optJSONObject("command"))
                    emit(WebSocketEvent.VoiceCommand(command))
                }
                "voice_event" -> {
                    // Provider event mirrored from backend (WebSocket
                    // providers only — for WebRTC the data channel
                    // receives these directly).
                    //
                    // Drop high-frequency delta events HERE so the
                    // entire downstream pipeline (Flow emit, ViewModel
                    // dispatch, provider parse) doesn't wake up for
                    // them.  Qwen streams ~50–100 deltas per assistant
                    // response; we accumulate them in
                    // response.audio_transcript.done anyway.
                    val inner = json.optJSONObject("event")
                    val innerType = inner?.optString("type", "")
                    if (innerType == "response.output_audio_transcript.delta" ||
                        innerType == "response.audio_transcript.delta" ||
                        innerType == "response.text.delta" ||
                        innerType == "response.function_call_arguments.delta") {
                        // No-op — neither the UI nor the provider needs
                        // these.
                    } else {
                        // Use shallow conversion: top-level keys only,
                        // nested JSONObject/JSONArray values stay as-is
                        // and the provider casts them when it needs to.
                        // Avoids walking the full event twice (once
                        // here, once in the provider).
                        emit(WebSocketEvent.VoiceProviderEvent(jsonObjectToShallowMap(inner)))
                    }
                }
                "voice_audio_out" -> {
                    // Speaker chunk for WebSocket voice providers.
                    val audio = json.optString("audio", "")
                    if (audio.isNotEmpty()) {
                        emit(WebSocketEvent.VoiceAudioOut(audio))
                    }
                }
                "voice_ending" -> {
                    val reason = json.optString("reason", "")
                    emit(WebSocketEvent.VoiceEnding(reason))
                }
                "voice_ended" -> {
                    val reason = json.optString("reason", "")
                    emit(WebSocketEvent.VoiceEnded(reason))
                }
                "voice_stopped" -> emit(WebSocketEvent.VoiceStopped)

                // Compact
                "compact_complete" -> emit(WebSocketEvent.CompactComplete(json.optString("summary", "")))

                // Error
                "error" -> {
                    val errorMsg = json.optString("error", "Unknown error")
                    val detail = json.optString("detail", null)
                    emit(WebSocketEvent.Error(errorMsg, detail))
                }

                // Legacy compatibility - map old events to new ones
                "content_block_delta" -> {
                    val delta = json.optString("delta", json.optString("text", ""))
                    emit(WebSocketEvent.TextDelta(delta))
                }
                "message_start" -> {
                    val messageId = json.optString("message_id", System.currentTimeMillis().toString())
                    emit(WebSocketEvent.MessageStart(messageId))
                }
                "message_end", "message_stop" -> emit(WebSocketEvent.MessageEnd)
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

    /**
     * Top-level-only conversion — nested JSONObject / JSONArray values
     * are kept as-is.  Used for high-frequency events (voice provider
     * events) where the consumer reads only flat scalars and casts the
     * occasional nested object on demand.  ~3-5x faster than the
     * recursive variant for typical voice events.
     */
    private fun jsonObjectToShallowMap(json: JSONObject?): Map<String, Any?> {
        if (json == null) return emptyMap()
        val map = mutableMapOf<String, Any?>()
        val keys = json.keys()
        while (keys.hasNext()) {
            val key = keys.next()
            val value = json.opt(key)
            map[key] = if (value === JSONObject.NULL) null else value
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
