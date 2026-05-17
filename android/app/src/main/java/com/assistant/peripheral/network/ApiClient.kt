package com.assistant.peripheral.network

import android.util.Log
import com.assistant.peripheral.data.*
import com.assistant.peripheral.voice.VoiceConfig
import com.assistant.peripheral.voice.VoiceConnectionInfo
import com.assistant.peripheral.voice.VoiceConnectionType
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject
import java.net.URLEncoder
import java.util.concurrent.TimeUnit

/**
 * REST API client for session management.
 * Matches the web frontend's REST API endpoints.
 */
class ApiClient(private val baseUrl: String) {

    companion object {
        private const val TAG = "ApiClient"
    }

    /**
     * Shared OkHttp client used for all REST calls.  Exposed
     * (read-only) so other components that need a one-shot HTTP
     * request (e.g. [com.assistant.peripheral.voice.OpenAIVoiceProvider]
     * for the SDP exchange) can reuse the same connection pool /
     * thread pool instead of spinning up their own.
     */
    val httpClient: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()
    private val client = httpClient

    private fun buildHttpUrl(path: String): String {
        var url = baseUrl
            .replace("ws://", "http://")
            .replace("wss://", "https://")
            .trimEnd('/')

        // Remove /api/orchestrator/chat if present
        url = url.replace("/api/orchestrator/chat", "")
            .replace("/api/orchestrator", "")
            .replace("/api/sessions/chat", "")

        return "$url$path"
    }

    /**
     * List all sessions from JSONL history.
     * GET /api/sessions
     */
    suspend fun listSessions(): List<SessionInfo> = withContext(Dispatchers.IO) {
        try {
            val url = buildHttpUrl("/api/sessions")
            Log.d(TAG, "GET $url")

            val request = Request.Builder()
                .url(url)
                .get()
                .build()

            val response = client.newCall(request).execute()
            if (!response.isSuccessful) {
                Log.e(TAG, "listSessions failed: ${response.code}")
                return@withContext emptyList()
            }

            val body = response.body?.string() ?: return@withContext emptyList()
            val jsonArray = JSONArray(body)

            (0 until jsonArray.length()).map { i ->
                val json = jsonArray.getJSONObject(i)
                SessionInfo(
                    sessionId = json.getString("session_id"),
                    localId = if (json.isNull("local_id")) null else json.optString("local_id", null),
                    title = json.optString("title", "Untitled"),
                    startedAt = json.optString("started_at", ""),
                    lastActivity = json.optString("last_activity", ""),
                    messageCount = json.optInt("message_count", 0),
                    isOrchestrator = json.optBoolean("is_orchestrator", false),
                    provider = json.optString("provider", "claude")
                )
            }
        } catch (e: Exception) {
            Log.e(TAG, "listSessions error: ${e.message}", e)
            emptyList()
        }
    }

    /**
     * Get full session details with all messages.
     * GET /api/sessions/{session_id}
     */
    suspend fun getSession(sessionId: String): Pair<SessionInfo?, List<ChatMessage>> = withContext(Dispatchers.IO) {
        try {
            val url = buildHttpUrl("/api/sessions/$sessionId")
            Log.d(TAG, "GET $url")

            val request = Request.Builder()
                .url(url)
                .get()
                .build()

            val response = client.newCall(request).execute()
            if (!response.isSuccessful) {
                Log.e(TAG, "getSession failed: ${response.code}")
                return@withContext Pair(null, emptyList())
            }

            val body = response.body?.string() ?: return@withContext Pair(null, emptyList())
            val json = JSONObject(body)

            val sessionInfo = SessionInfo(
                sessionId = json.getString("session_id"),
                localId = json.optString("local_id", null),
                title = json.optString("title", "Untitled"),
                startedAt = json.optString("started_at", ""),
                lastActivity = json.optString("last_activity", ""),
                messageCount = json.optInt("message_count", 0),
                isOrchestrator = json.optBoolean("is_orchestrator", false),
                provider = json.optString("provider", "claude")
            )

            val messagesArray = json.optJSONArray("messages") ?: JSONArray()
            val messages = parseMessages(messagesArray)

            Pair(sessionInfo, messages)
        } catch (e: Exception) {
            Log.e(TAG, "getSession error: ${e.message}", e)
            Pair(null, emptyList())
        }
    }

    /**
     * Get paginated messages from a session.
     * GET /api/sessions/{session_id}/messages?limit=X&before=Y
     *
     * @param sessionId The session to load messages from
     * @param limit Maximum number of messages to return
     * @param beforeIndex Load messages before this index (for loading older messages)
     * @return PaginatedMessages with messages, total count, has_more flag, and start index
     */
    suspend fun getMessagesPaginated(
        sessionId: String,
        limit: Int = 50,
        beforeIndex: Int? = null
    ): PaginatedMessages = withContext(Dispatchers.IO) {
        try {
            var urlStr = "/api/sessions/$sessionId/messages?limit=$limit"
            if (beforeIndex != null) {
                urlStr += "&before=$beforeIndex"
            }
            val url = buildHttpUrl(urlStr)
            Log.d(TAG, "GET $url")

            val request = Request.Builder()
                .url(url)
                .get()
                .build()

            val response = client.newCall(request).execute()
            if (!response.isSuccessful) {
                Log.e(TAG, "getMessagesPaginated failed: ${response.code}")
                return@withContext PaginatedMessages(emptyList(), 0, false, 0)
            }

            val body = response.body?.string() ?: return@withContext PaginatedMessages(emptyList(), 0, false, 0)
            val json = JSONObject(body)

            val messagesArray = json.optJSONArray("messages") ?: JSONArray()
            val messages = parseMessages(messagesArray)

            PaginatedMessages(
                messages = messages,
                totalCount = json.optInt("total_count", 0),
                hasMore = json.optBoolean("has_more", false),
                startIndex = json.optInt("start_index", 0)
            )
        } catch (e: Exception) {
            Log.e(TAG, "getMessagesPaginated error: ${e.message}", e)
            PaginatedMessages(emptyList(), 0, false, 0)
        }
    }

    /**
     * Get live session pool (currently open sessions).
     * GET /api/sessions/pool/live
     */
    suspend fun getLivePool(): List<LiveSession> = withContext(Dispatchers.IO) {
        try {
            val url = buildHttpUrl("/api/sessions/pool/live")
            Log.d(TAG, "GET $url")

            val request = Request.Builder()
                .url(url)
                .get()
                .build()

            val response = client.newCall(request).execute()
            if (!response.isSuccessful) {
                Log.e(TAG, "getLivePool failed: ${response.code}")
                return@withContext emptyList()
            }

            val body = response.body?.string() ?: return@withContext emptyList()
            val jsonArray = JSONArray(body)

            (0 until jsonArray.length()).map { i ->
                val json = jsonArray.getJSONObject(i)
                LiveSession(
                    localId = json.getString("local_id"),
                    sdkSessionId = json.getString("sdk_session_id"),
                    status = json.optString("status", "idle"),
                    isOrchestrator = json.optBoolean("is_orchestrator", false),
                    title = json.optString("title", "")
                )
            }
        } catch (e: Exception) {
            Log.e(TAG, "getLivePool error: ${e.message}", e)
            emptyList()
        }
    }

    /**
     * Fetch the global assistant config — the source of truth for
     * default voice provider/model/voice/language. Used on session
     * start so the Android app always picks up whatever is configured
     * on the backend (toggled from the web frontend).
     *
     * `GET /api/config`
     */
    suspend fun getVoiceConfig(): VoiceConfig = withContext(Dispatchers.IO) {
        try {
            val url = buildHttpUrl("/api/config")
            Log.d(TAG, "GET $url")
            val request = Request.Builder().url(url).get().build()
            val response = client.newCall(request).execute()
            if (!response.isSuccessful) {
                Log.e(TAG, "getVoiceConfig failed: ${response.code}")
                return@withContext VoiceConfig.DEFAULT
            }
            val body = response.body?.string() ?: return@withContext VoiceConfig.DEFAULT
            val json = JSONObject(body)
            VoiceConfig(
                provider = json.optString("default_voice_provider", VoiceConfig.DEFAULT.provider),
                model = json.optString("default_voice_model", VoiceConfig.DEFAULT.model),
                voice = json.optString("default_voice_name", VoiceConfig.DEFAULT.voice),
                transcriptionLanguage = json.optString("default_voice_transcription_language", ""),
                endpoint = json.optString("default_voice_endpoint", ""),
            )
        } catch (e: Exception) {
            Log.e(TAG, "getVoiceConfig error: ${e.message}", e)
            VoiceConfig.DEFAULT
        }
    }

    /**
     * Open a voice session and return its connection metadata.
     *
     * `POST /api/orchestrator/voice/session?provider=...&model=...&voice=...&transcription_language=...`
     *
     * The backend response always contains a `connection_info` object —
     * the provider-agnostic shape covering both transports:
     *
     *   {
     *     "connection_info": {
     *       "connection_type": "webrtc" | "websocket",
     *       "endpoint": "...",
     *       "ephemeral_token": "ek_..." | null,
     *       "expires_at": 123456789 | null,
     *       "audio_in_format": {"sample_rate": 24000, "encoding": "pcm16"},
     *       "audio_out_format": {"sample_rate": 24000, "encoding": "pcm16"},
     *       "model": "...",
     *       "voice": "..."
     *     },
     *     // legacy fields for OpenAI WebRTC clients:
     *     "client_secret": {"value": "ek_...", "expires_at": ...},
     *     "model": "...", "voice": "..."
     *   }
     *
     * @param provider null = use backend default
     * @param model null = use provider default
     * @param voice null = use model default
     * @param transcriptionLanguage null = use model default; "" = auto-detect
     */
    suspend fun startVoiceSession(
        provider: String? = null,
        model: String? = null,
        voice: String? = null,
        transcriptionLanguage: String? = null,
        endpoint: String? = null,
    ): VoiceConnectionInfo? = withContext(Dispatchers.IO) {
        try {
            val q = mutableListOf<String>()
            provider?.let { q.add("provider=${URLEncoder.encode(it, "UTF-8")}") }
            model?.let { q.add("model=${URLEncoder.encode(it, "UTF-8")}") }
            voice?.let { q.add("voice=${URLEncoder.encode(it, "UTF-8")}") }
            transcriptionLanguage?.let {
                q.add("transcription_language=${URLEncoder.encode(it, "UTF-8")}")
            }
            endpoint?.takeIf { it.isNotBlank() }?.let {
                q.add("endpoint=${URLEncoder.encode(it, "UTF-8")}")
            }
            val qs = if (q.isEmpty()) "" else "?" + q.joinToString("&")
            val url = buildHttpUrl("/api/orchestrator/voice/session$qs")
            Log.d(TAG, "POST $url")

            val request = Request.Builder()
                .url(url)
                .post(okhttp3.RequestBody.create(null, ByteArray(0)))
                .build()

            val response = client.newCall(request).execute()
            if (!response.isSuccessful) {
                Log.e(TAG, "startVoiceSession failed: ${response.code}")
                return@withContext null
            }

            val body = response.body?.string() ?: return@withContext null
            Log.d(TAG, "Voice session response: ${body.take(300)}...")
            val json = JSONObject(body)

            val info = json.optJSONObject("connection_info")
                ?: run {
                    Log.e(TAG, "No connection_info in response")
                    return@withContext null
                }
            parseConnectionInfo(info)
        } catch (e: Exception) {
            Log.e(TAG, "startVoiceSession error: ${e.message}", e)
            null
        }
    }

    /** Back-compat alias. Returns just the OpenAI ephemeral token + TTL. */
    suspend fun getVoiceToken(): VoiceTokenResponse? {
        val info = startVoiceSession() ?: return null
        val token = info.ephemeralToken ?: return null
        val expiresIn = info.expiresAt?.let {
            val now = System.currentTimeMillis() / 1000
            (it - now).toInt().coerceAtLeast(1)
        } ?: 60
        return VoiceTokenResponse(token = token, expiresIn = expiresIn)
    }

    private fun parseConnectionInfo(o: JSONObject): VoiceConnectionInfo {
        val inFmt = o.optJSONObject("audio_in_format") ?: JSONObject()
        val outFmt = o.optJSONObject("audio_out_format") ?: JSONObject()
        return VoiceConnectionInfo(
            connectionType = VoiceConnectionType.fromWire(
                o.optString("connection_type", "webrtc")
            ),
            endpoint = o.optString("endpoint", ""),
            ephemeralToken = o.optString("ephemeral_token", "").ifEmpty { null },
            expiresAt = o.optLong("expires_at", 0L).takeIf { it > 0 },
            model = o.optString("model", ""),
            voice = o.optString("voice", ""),
            audioInSampleRate = inFmt.optInt("sample_rate", 24000),
            audioInEncoding = inFmt.optString("encoding", "pcm16"),
            audioOutSampleRate = outFmt.optInt("sample_rate", 24000),
            audioOutEncoding = outFmt.optString("encoding", "pcm16"),
        )
    }

    /**
     * Rename a session.
     * PATCH /api/sessions/{session_id}/rename
     */
    suspend fun renameSession(sessionId: String, title: String): Boolean = withContext(Dispatchers.IO) {
        try {
            val url = buildHttpUrl("/api/sessions/$sessionId/rename")
            Log.d(TAG, "PATCH $url")

            val body = JSONObject().apply {
                put("title", title)
            }

            val request = Request.Builder()
                .url(url)
                .patch(okhttp3.RequestBody.create(
                    "application/json".toMediaTypeOrNull(),
                    body.toString()
                ))
                .build()

            val response = client.newCall(request).execute()
            response.isSuccessful
        } catch (e: Exception) {
            Log.e(TAG, "renameSession error: ${e.message}", e)
            false
        }
    }

    /**
     * Close an active session in the pool (does NOT delete history).
     * POST /api/sessions/{local_id}/close
     *
     * The backend keys the pool by local_id, so this must be a live pool local_id
     * (from getLivePool()) — passing a JSONL session id will return 404.
     */
    suspend fun closePoolSession(localId: String): Boolean = withContext(Dispatchers.IO) {
        try {
            val url = buildHttpUrl("/api/sessions/$localId/close")
            Log.d(TAG, "POST $url")

            val request = Request.Builder()
                .url(url)
                .post(okhttp3.RequestBody.create(null, ByteArray(0)))
                .build()

            val response = client.newCall(request).execute()
            // 204 = closed, 404 = already gone (treat as success — desired end state)
            response.isSuccessful || response.code == 404
        } catch (e: Exception) {
            Log.e(TAG, "closePoolSession error: ${e.message}", e)
            false
        }
    }

    /**
     * Duplicate a session: copies its JSONL + title under a fresh UUID.
     * POST /api/sessions/{session_id}/duplicate
     *
     * Returns the new session_id on success, or null on failure.
     */
    suspend fun duplicateSession(sessionId: String): String? = withContext(Dispatchers.IO) {
        try {
            val url = buildHttpUrl("/api/sessions/$sessionId/duplicate")
            Log.d(TAG, "POST $url")

            val request = Request.Builder()
                .url(url)
                .post(okhttp3.RequestBody.create(null, ByteArray(0)))
                .build()

            val response = client.newCall(request).execute()
            if (!response.isSuccessful) {
                Log.w(TAG, "duplicateSession failed: ${response.code}")
                return@withContext null
            }
            val body = response.body?.string() ?: return@withContext null
            JSONObject(body).optString("session_id").takeIf { it.isNotEmpty() }
        } catch (e: Exception) {
            Log.e(TAG, "duplicateSession error: ${e.message}", e)
            null
        }
    }

    /**
     * Rewind a session by dropping the last ``dropLastN`` visible messages.
     * POST /api/sessions/{session_id}/truncate
     *
     * Bottom-relative so the action stays correct under pagination (the
     * frontend may only have the most recent page loaded). Rejected (409)
     * when the session is currently open in the pool — caller must close
     * the tab first.
     */
    suspend fun truncateSession(sessionId: String, dropLastN: Int): Boolean = withContext(Dispatchers.IO) {
        try {
            val url = buildHttpUrl("/api/sessions/$sessionId/truncate")
            Log.d(TAG, "POST $url drop_last_n=$dropLastN")

            val body = JSONObject().apply { put("drop_last_n", dropLastN) }
            val request = Request.Builder()
                .url(url)
                .post(okhttp3.RequestBody.create(
                    "application/json".toMediaTypeOrNull(),
                    body.toString()
                ))
                .build()

            val response = client.newCall(request).execute()
            response.isSuccessful
        } catch (e: Exception) {
            Log.e(TAG, "truncateSession error: ${e.message}", e)
            false
        }
    }

    /**
     * Fork a session: duplicate, then drop the last ``dropLastN`` messages in the copy.
     * POST /api/sessions/{session_id}/fork
     *
     * Returns the new session_id on success, or null on failure. The original
     * session is untouched.
     */
    suspend fun forkSession(sessionId: String, dropLastN: Int): String? = withContext(Dispatchers.IO) {
        try {
            val url = buildHttpUrl("/api/sessions/$sessionId/fork")
            Log.d(TAG, "POST $url drop_last_n=$dropLastN")

            val body = JSONObject().apply { put("drop_last_n", dropLastN) }
            val request = Request.Builder()
                .url(url)
                .post(okhttp3.RequestBody.create(
                    "application/json".toMediaTypeOrNull(),
                    body.toString()
                ))
                .build()

            val response = client.newCall(request).execute()
            if (!response.isSuccessful) {
                Log.w(TAG, "forkSession failed: ${response.code}")
                return@withContext null
            }
            val respBody = response.body?.string() ?: return@withContext null
            JSONObject(respBody).optString("session_id").takeIf { it.isNotEmpty() }
        } catch (e: Exception) {
            Log.e(TAG, "forkSession error: ${e.message}", e)
            null
        }
    }

    /**
     * Delete a session.
     * DELETE /api/sessions/{session_id}
     */
    suspend fun deleteSession(sessionId: String): Boolean = withContext(Dispatchers.IO) {
        try {
            val url = buildHttpUrl("/api/sessions/$sessionId")
            Log.d(TAG, "DELETE $url")

            val request = Request.Builder()
                .url(url)
                .delete()
                .build()

            val response = client.newCall(request).execute()
            response.isSuccessful
        } catch (e: Exception) {
            Log.e(TAG, "deleteSession error: ${e.message}", e)
            false
        }
    }

    /**
     * Tool result outcome harvested from a JSONL `tool_result` block, ready to be
     * folded back into its originating `tool_use` block by tool_use_id.
     */
    private data class ToolOutcome(val output: String?, val isError: Boolean)

    /**
     * Parse a list of message JSON objects into ChatMessages, attaching tool_result
     * blocks to their corresponding tool_use blocks by tool_use_id (matches the web
     * frontend's two-pass approach in useChat.ts LOAD_HISTORY).
     *
     * Tool results come as separate user messages in JSONL — they must be merged
     * back into the assistant's tool_use block so the UI can show input + output
     * together, and the protocol-only user wrappers must be dropped.
     */
    private fun parseMessages(messagesArray: JSONArray): List<ChatMessage> {
        val toolResults = collectToolResults(messagesArray)
        val out = mutableListOf<ChatMessage>()
        for (i in 0 until messagesArray.length()) {
            val msg = parseMessage(messagesArray.getJSONObject(i), toolResults) ?: continue
            out += msg
        }
        return out
    }

    private fun collectToolResults(messagesArray: JSONArray): Map<String, ToolOutcome> {
        val results = mutableMapOf<String, ToolOutcome>()
        for (i in 0 until messagesArray.length()) {
            val msg = messagesArray.getJSONObject(i)
            val blocks = msg.optJSONArray("blocks") ?: continue
            for (j in 0 until blocks.length()) {
                val b = blocks.getJSONObject(j)
                if (b.optString("type") != "tool_result") continue
                val id = b.optString("tool_use_id", "")
                if (id.isEmpty()) continue
                val output = if (b.isNull("output")) null else b.optString("output", "")
                results[id] = ToolOutcome(output, b.optBoolean("is_error", false))
            }
        }
        return results
    }

    private fun parseMessage(
        json: JSONObject,
        toolResults: Map<String, ToolOutcome> = emptyMap()
    ): ChatMessage? {
        val role = when (json.optString("role", "").lowercase()) {
            "user" -> MessageRole.USER
            "assistant" -> MessageRole.ASSISTANT
            "system" -> MessageRole.SYSTEM
            else -> return null
        }

        val text = json.optString("text", "")
        val blocksArray = json.optJSONArray("blocks")

        val blocks = if (blocksArray != null) {
            (0 until blocksArray.length()).mapNotNull { i ->
                parseBlock(blocksArray.getJSONObject(i), toolResults)
            }
        } else {
            if (text.isNotEmpty()) {
                listOf(MessageBlock.Text(text))
            } else {
                emptyList()
            }
        }

        // Skip protocol-only user/system messages (e.g. tool_result wrappers)
        // that have no displayable content. These appear in JSONL as user-role
        // entries containing only tool_result blocks, which we don't render
        // standalone — the result is shown inside the corresponding tool_use block.
        if (role != MessageRole.ASSISTANT && text.isEmpty() && blocks.isEmpty()) {
            return null
        }

        return ChatMessage(
            id = json.optString("id", java.util.UUID.randomUUID().toString()),
            role = role,
            content = text,
            blocks = blocks,
            timestamp = parseTimestamp(json.optString("timestamp", "")),
            isStreaming = false
        )
    }

    private fun parseBlock(
        json: JSONObject,
        toolResults: Map<String, ToolOutcome> = emptyMap()
    ): MessageBlock? {
        return when (json.optString("type", "")) {
            "text" -> MessageBlock.Text(
                text = json.optString("text", ""),
                isStreaming = false
            )
            "thinking" -> MessageBlock.Thinking(
                text = json.optString("text", ""),
                isStreaming = false
            )
            "tool_use" -> {
                val id = json.optString("tool_use_id", "")
                val outcome = toolResults[id]
                // Prefer the matched tool_result; fall back to an inline `output` on
                // the tool_use block itself when the history endpoint attached it
                // there directly. JSON null becomes Kotlin null (not the literal
                // string "null") — `optString(key, null)` is broken for that.
                val result: String? = outcome?.output
                    ?: if (json.isNull("output")) null else json.optString("output", "").ifEmpty { null }
                val isError = outcome?.isError ?: json.optBoolean("is_error", false)
                MessageBlock.ToolUse(
                    toolUseId = id,
                    toolName = json.optString("tool_name", ""),
                    toolInput = jsonObjectToMap(json.optJSONObject("tool_input")),
                    result = result,
                    isError = isError,
                    isComplete = true
                )
            }
            // tool_result blocks are folded into their corresponding tool_use above;
            // dropping them here mirrors the web frontend's behaviour.
            "tool_result" -> null
            "compact" -> MessageBlock.Compact(
                summary = json.optString("text", json.optString("summary", ""))
            )
            else -> null
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

    private fun parseTimestamp(timestamp: String): Long {
        return try {
            // Try parsing ISO format
            java.text.SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", java.util.Locale.US)
                .parse(timestamp.take(19))?.time ?: System.currentTimeMillis()
        } catch (e: Exception) {
            System.currentTimeMillis()
        }
    }

    // ─────────────────────────────────────────────────────────────────
    // System configuration (mirrors `frontend/src/api/rest.ts`).
    // Powers the System tab of the Settings screen.
    // ─────────────────────────────────────────────────────────────────

    /** GET /api/config — full assistant config. */
    suspend fun getAssistantConfig(): AssistantConfig? = withContext(Dispatchers.IO) {
        try {
            val url = buildHttpUrl("/api/config")
            val request = Request.Builder().url(url).get().build()
            val response = client.newCall(request).execute()
            if (!response.isSuccessful) {
                Log.e(TAG, "getAssistantConfig failed: ${response.code}")
                return@withContext null
            }
            parseAssistantConfig(JSONObject(response.body?.string() ?: return@withContext null))
        } catch (e: Exception) {
            Log.e(TAG, "getAssistantConfig error: ${e.message}", e)
            null
        }
    }

    /**
     * PUT /api/config — partial update. Returns the updated config on success,
     * `Pair(null, errorMessage)` on a 4xx where the backend provided a detail.
     */
    suspend fun updateAssistantConfig(patch: ConfigPatch): Result<AssistantConfig> = withContext(Dispatchers.IO) {
        try {
            val url = buildHttpUrl("/api/config")
            val body = JSONObject().apply {
                patch.workingDirectory?.let { put("working_directory", it) }
                patch.enabledMcps?.let { put("enabled_mcps", JSONArray(it)) }
                patch.chromeExtension?.let { put("chrome_extension", it) }
                patch.provider?.let { put("provider", it) }
                patch.defaultModel?.let { put("default_model", it) }
                patch.harnessModel?.let { put("harness_model", JSONObject(it as Map<*, *>)) }
                patch.defaultVoiceProvider?.let { put("default_voice_provider", it) }
                patch.defaultVoiceModel?.let { put("default_voice_model", it) }
                patch.defaultVoiceName?.let { put("default_voice_name", it) }
                patch.defaultVoiceTranscriptionLanguage?.let {
                    put("default_voice_transcription_language", it)
                }
                patch.defaultVoiceEndpoint?.let { put("default_voice_endpoint", it) }
                patch.voiceRecordingEnabled?.let { put("voice_recording_enabled", it) }
            }
            val request = Request.Builder()
                .url(url)
                .put(okhttp3.RequestBody.create(
                    "application/json".toMediaTypeOrNull(),
                    body.toString()
                ))
                .build()
            val response = client.newCall(request).execute()
            val responseBody = response.body?.string() ?: ""
            if (!response.isSuccessful) {
                val detail = try { JSONObject(responseBody).optString("detail", "") } catch (_: Exception) { "" }
                val msg = detail.ifBlank { "HTTP ${response.code}" }
                Log.e(TAG, "updateAssistantConfig failed: $msg")
                return@withContext Result.failure(Exception(msg))
            }
            val cfg = parseAssistantConfig(JSONObject(responseBody))
                ?: return@withContext Result.failure(Exception("Invalid response"))
            Result.success(cfg)
        } catch (e: Exception) {
            Log.e(TAG, "updateAssistantConfig error: ${e.message}", e)
            Result.failure(e)
        }
    }

    /** GET /api/mcp/servers */
    suspend fun listMcpServers(): Map<String, McpServerConfig> = withContext(Dispatchers.IO) {
        try {
            val url = buildHttpUrl("/api/mcp/servers")
            val request = Request.Builder().url(url).get().build()
            val response = client.newCall(request).execute()
            if (!response.isSuccessful) return@withContext emptyMap()
            val json = JSONObject(response.body?.string() ?: return@withContext emptyMap())
            val serversJson = json.optJSONObject("servers") ?: return@withContext emptyMap()
            val out = mutableMapOf<String, McpServerConfig>()
            serversJson.keys().forEach { name ->
                val s = serversJson.optJSONObject(name) ?: return@forEach
                val args = s.optJSONArray("args")?.let { arr ->
                    (0 until arr.length()).map { arr.optString(it, "") }
                } ?: emptyList()
                val env = s.optJSONObject("env")?.let { envObj ->
                    envObj.keys().asSequence().associateWith { k -> envObj.optString(k, "") }
                } ?: emptyMap()
                out[name] = McpServerConfig(
                    type = if (s.isNull("type")) null else s.optString("type"),
                    command = s.optString("command", ""),
                    args = args,
                    env = env,
                )
            }
            out
        } catch (e: Exception) {
            Log.e(TAG, "listMcpServers error: ${e.message}", e)
            emptyMap()
        }
    }

    /** GET /api/orchestrator/models */
    suspend fun listOrchestratorModels(): List<ModelInfo> = withContext(Dispatchers.IO) {
        try {
            val url = buildHttpUrl("/api/orchestrator/models")
            val request = Request.Builder().url(url).get().build()
            val response = client.newCall(request).execute()
            if (!response.isSuccessful) return@withContext emptyList()
            val json = JSONObject(response.body?.string() ?: return@withContext emptyList())
            val arr = json.optJSONArray("models") ?: return@withContext emptyList()
            (0 until arr.length()).map { i ->
                val m = arr.getJSONObject(i)
                ModelInfo(
                    provider = m.optString("provider", ""),
                    modelId = m.optString("model_id", ""),
                    displayName = m.optString("display_name", m.optString("model_id", "")),
                    supportsAudio = m.optBoolean("supports_audio", false),
                    supportsVision = m.optBoolean("supports_vision", false),
                    supportsTools = m.optBoolean("supports_tools", true),
                    maxTokens = m.optInt("max_tokens", 0),
                )
            }
        } catch (e: Exception) {
            Log.e(TAG, "listOrchestratorModels error: ${e.message}", e)
            emptyList()
        }
    }

    /** GET /api/orchestrator/voice/models */
    suspend fun listVoiceModels(): Map<String, List<VoiceModelEntry>> = withContext(Dispatchers.IO) {
        try {
            val url = buildHttpUrl("/api/orchestrator/voice/models")
            val request = Request.Builder().url(url).get().build()
            val response = client.newCall(request).execute()
            if (!response.isSuccessful) return@withContext emptyMap()
            val json = JSONObject(response.body?.string() ?: return@withContext emptyMap())
            val providers = json.optJSONObject("providers") ?: return@withContext emptyMap()
            val out = mutableMapOf<String, List<VoiceModelEntry>>()
            providers.keys().forEach { providerId ->
                val arr = providers.optJSONArray(providerId) ?: return@forEach
                out[providerId] = (0 until arr.length()).map { parseVoiceModelEntry(arr.getJSONObject(it)) }
            }
            out
        } catch (e: Exception) {
            Log.e(TAG, "listVoiceModels error: ${e.message}", e)
            emptyMap()
        }
    }

    /** GET /api/config/voice/google/models[?endpoint=vertex|aistudio] */
    suspend fun listGoogleVoiceModels(endpoint: String? = null): List<VoiceModelEntry> = withContext(Dispatchers.IO) {
        try {
            val qs = endpoint?.takeIf { it.isNotBlank() }
                ?.let { "?endpoint=${URLEncoder.encode(it, "UTF-8")}" } ?: ""
            val url = buildHttpUrl("/api/config/voice/google/models$qs")
            val request = Request.Builder().url(url).get().build()
            val response = client.newCall(request).execute()
            if (!response.isSuccessful) return@withContext emptyList()
            val json = JSONObject(response.body?.string() ?: return@withContext emptyList())
            val arr = json.optJSONArray("models") ?: return@withContext emptyList()
            (0 until arr.length()).map { parseVoiceModelEntry(arr.getJSONObject(it)) }
        } catch (e: Exception) {
            Log.e(TAG, "listGoogleVoiceModels error: ${e.message}", e)
            emptyList()
        }
    }

    /** GET /api/config/harness/qwen/models */
    suspend fun listQwenHarnessModels(): List<QwenModelInfo> = withContext(Dispatchers.IO) {
        try {
            val url = buildHttpUrl("/api/config/harness/qwen/models")
            val request = Request.Builder().url(url).get().build()
            val response = client.newCall(request).execute()
            if (!response.isSuccessful) return@withContext emptyList()
            val json = JSONObject(response.body?.string() ?: return@withContext emptyList())
            val arr = json.optJSONArray("models") ?: return@withContext emptyList()
            (0 until arr.length()).map { i ->
                val m = arr.getJSONObject(i)
                QwenModelInfo(
                    id = m.optString("id", ""),
                    displayName = m.optString("display_name", m.optString("id", "")),
                    provider = m.optString("provider", ""),
                    baseUrl = if (m.isNull("base_url")) null else m.optString("base_url"),
                    contextWindow = if (m.isNull("context_window")) null else m.optInt("context_window"),
                    supportsVision = m.optBoolean("supports_vision", false),
                    supportsVideo = m.optBoolean("supports_video", false),
                    supportsThinking = m.optBoolean("supports_thinking", false),
                )
            }
        } catch (e: Exception) {
            Log.e(TAG, "listQwenHarnessModels error: ${e.message}", e)
            emptyList()
        }
    }

    /** GET /api/config/providers */
    suspend fun listSessionProviders(): List<SessionProviderSpec> = withContext(Dispatchers.IO) {
        try {
            val url = buildHttpUrl("/api/config/providers")
            val request = Request.Builder().url(url).get().build()
            val response = client.newCall(request).execute()
            if (!response.isSuccessful) return@withContext emptyList()
            val json = JSONObject(response.body?.string() ?: return@withContext emptyList())
            val arr = json.optJSONArray("providers") ?: return@withContext emptyList()
            (0 until arr.length()).map { i ->
                val p = arr.getJSONObject(i)
                SessionProviderSpec(
                    id = p.optString("id", ""),
                    label = p.optString("label", p.optString("id", "")),
                    description = p.optString("description", ""),
                )
            }
        } catch (e: Exception) {
            Log.e(TAG, "listSessionProviders error: ${e.message}", e)
            emptyList()
        }
    }

    private fun parseAssistantConfig(json: JSONObject): AssistantConfig? {
        return try {
            val historyArr = json.optJSONArray("working_directory_history") ?: JSONArray()
            val history = (0 until historyArr.length()).map { i ->
                val e = historyArr.getJSONObject(i)
                WorkingDirectoryEntry(
                    id = e.optString("id", ""),
                    path = e.optString("path", ""),
                    label = if (e.isNull("label")) null else e.optString("label", null),
                    sshHost = if (e.isNull("ssh_host")) null else e.optString("ssh_host", null),
                    sshUser = if (e.isNull("ssh_user")) null else e.optString("ssh_user", null),
                )
            }
            val mcpsArr = json.optJSONArray("enabled_mcps") ?: JSONArray()
            val enabledMcps = (0 until mcpsArr.length()).map { mcpsArr.optString(it, "") }
            val harnessObj = json.optJSONObject("harness_model") ?: JSONObject()
            val harness = harnessObj.keys().asSequence().associateWith { k -> harnessObj.optString(k, "") }
            AssistantConfig(
                workingDirectory = json.optString("working_directory", ""),
                workingDirectoryHistory = history,
                enabledMcps = enabledMcps,
                chromeExtension = json.optBoolean("chrome_extension", false),
                provider = json.optString("provider", "claude"),
                defaultModel = json.optString("default_model", ""),
                harnessModel = harness,
                defaultVoiceProvider = json.optString("default_voice_provider", ""),
                defaultVoiceModel = json.optString("default_voice_model", ""),
                defaultVoiceName = json.optString("default_voice_name", ""),
                defaultVoiceTranscriptionLanguage = json.optString("default_voice_transcription_language", ""),
                defaultVoiceEndpoint = json.optString("default_voice_endpoint", "vertex"),
                voiceRecordingEnabled = json.optBoolean("voice_recording_enabled", false),
            )
        } catch (e: Exception) {
            Log.e(TAG, "parseAssistantConfig error: ${e.message}", e)
            null
        }
    }

    private fun parseVoiceModelEntry(m: JSONObject): VoiceModelEntry {
        val voicesArr = m.optJSONArray("voices") ?: JSONArray()
        val voices = (0 until voicesArr.length()).map { i ->
            val v = voicesArr.getJSONObject(i)
            VoiceEntry(
                id = v.optString("id", ""),
                label = v.optString("label", v.optString("id", "")),
                description = v.optString("description", ""),
            )
        }
        val langsArr = m.optJSONArray("transcription_languages") ?: JSONArray()
        val langs = (0 until langsArr.length()).map { i ->
            val l = langsArr.getJSONObject(i)
            TranscriptionLanguageEntry(
                id = l.optString("id", ""),
                label = l.optString("label", l.optString("id", "")),
                description = l.optString("description", ""),
            )
        }
        return VoiceModelEntry(
            id = m.optString("id", ""),
            label = m.optString("label", m.optString("id", "")),
            voice = m.optString("voice", ""),
            voices = voices,
            transcriptionLanguages = langs,
            defaultTranscriptionLanguage = m.optString("default_transcription_language", ""),
            isDefault = m.optBoolean("default", false),
        )
    }
}

/**
 * Voice token response from the server.
 */
data class VoiceTokenResponse(
    val token: String,
    val expiresIn: Int
)

/**
 * Live session from the pool.
 */
data class LiveSession(
    val localId: String,
    val sdkSessionId: String,
    val status: String,
    val isOrchestrator: Boolean,
    val title: String
)

/**
 * Paginated messages response.
 */
data class PaginatedMessages(
    val messages: List<ChatMessage>,
    val totalCount: Int,
    val hasMore: Boolean,
    val startIndex: Int
)
