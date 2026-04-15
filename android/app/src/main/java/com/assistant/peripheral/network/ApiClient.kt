package com.assistant.peripheral.network

import android.util.Log
import com.assistant.peripheral.data.*
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * REST API client for session management.
 * Matches the web frontend's REST API endpoints.
 */
class ApiClient(private val baseUrl: String) {

    companion object {
        private const val TAG = "ApiClient"
    }

    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

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
                    isOrchestrator = json.optBoolean("is_orchestrator", false)
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
                isOrchestrator = json.optBoolean("is_orchestrator", false)
            )

            val messagesArray = json.optJSONArray("messages") ?: JSONArray()
            val messages = (0 until messagesArray.length()).mapNotNull { i ->
                parseMessage(messagesArray.getJSONObject(i))
            }

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
            val messages = (0 until messagesArray.length()).mapNotNull { i ->
                parseMessage(messagesArray.getJSONObject(i))
            }

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
     * Get voice session ephemeral token.
     * POST /api/orchestrator/voice/session
     *
     * Response format from OpenAI:
     * {
     *   "client_secret": {
     *     "value": "ek_...",
     *     "expires_at": 1234567890
     *   },
     *   ...
     * }
     */
    suspend fun getVoiceToken(): VoiceTokenResponse? = withContext(Dispatchers.IO) {
        try {
            val url = buildHttpUrl("/api/orchestrator/voice/session")
            Log.d(TAG, "POST $url")

            val request = Request.Builder()
                .url(url)
                .post(okhttp3.RequestBody.create(null, ByteArray(0)))
                .build()

            val response = client.newCall(request).execute()
            if (!response.isSuccessful) {
                Log.e(TAG, "getVoiceToken failed: ${response.code}")
                return@withContext null
            }

            val body = response.body?.string() ?: return@withContext null
            Log.d(TAG, "Voice token response: ${body.take(200)}...")
            val json = JSONObject(body)

            // Extract token from client_secret.value (OpenAI format)
            val clientSecret = json.optJSONObject("client_secret")
            if (clientSecret == null) {
                Log.e(TAG, "No client_secret in response")
                return@withContext null
            }

            val token = clientSecret.optString("value", "")
            if (token.isEmpty()) {
                Log.e(TAG, "Empty token value in client_secret")
                return@withContext null
            }

            val expiresAt = clientSecret.optLong("expires_at", 0)
            val now = System.currentTimeMillis() / 1000
            val expiresIn = if (expiresAt > 0) (expiresAt - now).toInt() else 60

            Log.d(TAG, "Got ephemeral token, expires in ${expiresIn}s")
            VoiceTokenResponse(
                token = token,
                expiresIn = expiresIn
            )
        } catch (e: Exception) {
            Log.e(TAG, "getVoiceToken error: ${e.message}", e)
            null
        }
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

    private fun parseMessage(json: JSONObject): ChatMessage? {
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
                parseBlock(blocksArray.getJSONObject(i))
            }
        } else {
            if (text.isNotEmpty()) {
                listOf(MessageBlock.Text(text))
            } else {
                emptyList()
            }
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

    private fun parseBlock(json: JSONObject): MessageBlock? {
        return when (json.optString("type", "")) {
            "text" -> MessageBlock.Text(
                text = json.optString("text", ""),
                isStreaming = false
            )
            "thinking" -> MessageBlock.Thinking(
                text = json.optString("text", ""),
                isStreaming = false
            )
            "tool_use" -> MessageBlock.ToolUse(
                toolUseId = json.optString("tool_use_id", ""),
                toolName = json.optString("tool_name", ""),
                toolInput = emptyMap(), // Simplified for now
                result = json.optString("output", null),
                isError = json.optBoolean("is_error", false),
                isComplete = true
            )
            "compact" -> MessageBlock.Compact(
                summary = json.optString("text", json.optString("summary", ""))
            )
            else -> null
        }
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
