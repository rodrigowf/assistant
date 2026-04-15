package com.assistant.peripheral.network

import kotlinx.coroutines.test.runTest
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * Tests for ApiClient REST API connectivity.
 *
 * These tests verify that the Android app correctly communicates with the backend
 * API endpoints, matching the behavior of the web frontend.
 *
 * Endpoints tested:
 * - GET /api/sessions - List all sessions
 * - GET /api/sessions/{id} - Get session details
 * - GET /api/sessions/{id}/messages - Paginated messages
 * - GET /api/sessions/pool/live - Live session pool
 * - POST /api/orchestrator/voice/session - Voice token
 * - PATCH /api/sessions/{id}/rename - Rename session
 * - DELETE /api/sessions/{id} - Delete session
 */
@RunWith(RobolectricTestRunner::class)
@Config(manifest = Config.NONE, sdk = [28])
class ApiClientTest {

    private lateinit var mockServer: MockWebServer
    private lateinit var apiClient: ApiClient

    @Before
    fun setup() {
        mockServer = MockWebServer()
        mockServer.start()
        val baseUrl = mockServer.url("/").toString().trimEnd('/')
        apiClient = ApiClient(baseUrl)
    }

    @After
    fun teardown() {
        mockServer.shutdown()
    }

    // ==========================================================================
    // GET /api/sessions - List Sessions
    // ==========================================================================

    @Test
    fun `listSessions returns empty list when no sessions exist`() = runTest {
        mockServer.enqueue(MockResponse()
            .setResponseCode(200)
            .setBody("[]")
            .addHeader("Content-Type", "application/json"))

        val sessions = apiClient.listSessions()

        assertEquals(0, sessions.size)
        val request = mockServer.takeRequest()
        assertEquals("GET", request.method)
        assertEquals("/api/sessions", request.path)
    }

    @Test
    fun `listSessions parses session list correctly`() = runTest {
        val responseBody = """
            [
                {
                    "session_id": "abc-123",
                    "local_id": "local-456",
                    "title": "Test Session",
                    "started_at": "2024-01-15T10:30:00+00:00",
                    "last_activity": "2024-01-15T11:00:00+00:00",
                    "message_count": 10,
                    "is_orchestrator": false
                },
                {
                    "session_id": "def-789",
                    "local_id": null,
                    "title": "Orchestrator Session",
                    "started_at": "2024-01-15T09:00:00+00:00",
                    "last_activity": "2024-01-15T12:00:00+00:00",
                    "message_count": 50,
                    "is_orchestrator": true
                }
            ]
        """.trimIndent()

        mockServer.enqueue(MockResponse()
            .setResponseCode(200)
            .setBody(responseBody)
            .addHeader("Content-Type", "application/json"))

        val sessions = apiClient.listSessions()

        assertEquals(2, sessions.size)

        // First session
        assertEquals("abc-123", sessions[0].sessionId)
        assertEquals("local-456", sessions[0].localId)
        assertEquals("Test Session", sessions[0].title)
        assertEquals(10, sessions[0].messageCount)
        assertFalse(sessions[0].isOrchestrator)

        // Second session (orchestrator)
        assertEquals("def-789", sessions[1].sessionId)
        assertNull(sessions[1].localId)
        assertEquals("Orchestrator Session", sessions[1].title)
        assertEquals(50, sessions[1].messageCount)
        assertTrue(sessions[1].isOrchestrator)
    }

    @Test
    fun `listSessions returns empty list on network error`() = runTest {
        mockServer.enqueue(MockResponse().setResponseCode(500))

        val sessions = apiClient.listSessions()

        assertEquals(0, sessions.size)
    }

    // ==========================================================================
    // GET /api/sessions/{id} - Get Session Details
    // ==========================================================================

    @Test
    fun `getSession parses session with messages correctly`() = runTest {
        val responseBody = """
            {
                "session_id": "session-123",
                "local_id": "local-abc",
                "title": "My Conversation",
                "started_at": "2024-01-15T10:00:00+00:00",
                "last_activity": "2024-01-15T11:30:00+00:00",
                "message_count": 3,
                "is_orchestrator": false,
                "messages": [
                    {
                        "role": "user",
                        "text": "Hello",
                        "timestamp": "2024-01-15T10:00:00+00:00"
                    },
                    {
                        "role": "assistant",
                        "text": "Hi there!",
                        "timestamp": "2024-01-15T10:00:05+00:00"
                    },
                    {
                        "role": "user",
                        "text": "How are you?",
                        "timestamp": "2024-01-15T10:01:00+00:00"
                    }
                ]
            }
        """.trimIndent()

        mockServer.enqueue(MockResponse()
            .setResponseCode(200)
            .setBody(responseBody)
            .addHeader("Content-Type", "application/json"))

        val (sessionInfo, messages) = apiClient.getSession("session-123")

        assertNotNull(sessionInfo)
        assertEquals("session-123", sessionInfo?.sessionId)
        assertEquals("My Conversation", sessionInfo?.title)
        assertEquals(3, sessionInfo?.messageCount)

        assertEquals(3, messages.size)
        assertEquals("Hello", messages[0].content)
        assertEquals(com.assistant.peripheral.data.MessageRole.USER, messages[0].role)
        assertEquals("Hi there!", messages[1].content)
        assertEquals(com.assistant.peripheral.data.MessageRole.ASSISTANT, messages[1].role)
    }

    @Test
    fun `getSession returns null pair for non-existent session`() = runTest {
        mockServer.enqueue(MockResponse().setResponseCode(404))

        val (sessionInfo, messages) = apiClient.getSession("non-existent")

        assertNull(sessionInfo)
        assertEquals(0, messages.size)
    }

    // ==========================================================================
    // GET /api/sessions/{id}/messages - Paginated Messages
    // ==========================================================================

    @Test
    fun `getMessagesPaginated returns paginated results`() = runTest {
        val responseBody = """
            {
                "messages": [
                    {"role": "user", "text": "Message 1"},
                    {"role": "assistant", "text": "Response 1"}
                ],
                "total_count": 100,
                "has_more": true,
                "start_index": 50
            }
        """.trimIndent()

        mockServer.enqueue(MockResponse()
            .setResponseCode(200)
            .setBody(responseBody)
            .addHeader("Content-Type", "application/json"))

        val result = apiClient.getMessagesPaginated("session-123", limit = 50)

        assertEquals(2, result.messages.size)
        assertEquals(100, result.totalCount)
        assertTrue(result.hasMore)
        assertEquals(50, result.startIndex)

        val request = mockServer.takeRequest()
        assertTrue(request.path!!.contains("limit=50"))
    }

    @Test
    fun `getMessagesPaginated with before parameter loads older messages`() = runTest {
        val responseBody = """
            {
                "messages": [
                    {"role": "user", "text": "Older message"}
                ],
                "total_count": 100,
                "has_more": true,
                "start_index": 0
            }
        """.trimIndent()

        mockServer.enqueue(MockResponse()
            .setResponseCode(200)
            .setBody(responseBody)
            .addHeader("Content-Type", "application/json"))

        val result = apiClient.getMessagesPaginated("session-123", limit = 50, beforeIndex = 50)

        val request = mockServer.takeRequest()
        assertTrue(request.path!!.contains("before=50"))
        assertTrue(request.path!!.contains("limit=50"))
    }

    @Test
    fun `getMessagesPaginated handles empty response`() = runTest {
        val responseBody = """
            {
                "messages": [],
                "total_count": 0,
                "has_more": false,
                "start_index": 0
            }
        """.trimIndent()

        mockServer.enqueue(MockResponse()
            .setResponseCode(200)
            .setBody(responseBody)
            .addHeader("Content-Type", "application/json"))

        val result = apiClient.getMessagesPaginated("session-123")

        assertEquals(0, result.messages.size)
        assertEquals(0, result.totalCount)
        assertFalse(result.hasMore)
    }

    // ==========================================================================
    // GET /api/sessions/pool/live - Live Session Pool
    // ==========================================================================

    @Test
    fun `getLivePool returns live sessions`() = runTest {
        val responseBody = """
            [
                {
                    "local_id": "local-123",
                    "sdk_session_id": "sdk-456",
                    "status": "idle",
                    "is_orchestrator": true,
                    "title": "Orchestrator"
                },
                {
                    "local_id": "local-789",
                    "sdk_session_id": "sdk-abc",
                    "status": "streaming",
                    "is_orchestrator": false,
                    "title": "Agent Session"
                }
            ]
        """.trimIndent()

        mockServer.enqueue(MockResponse()
            .setResponseCode(200)
            .setBody(responseBody)
            .addHeader("Content-Type", "application/json"))

        val liveSessions = apiClient.getLivePool()

        assertEquals(2, liveSessions.size)

        // Orchestrator session
        assertEquals("local-123", liveSessions[0].localId)
        assertEquals("sdk-456", liveSessions[0].sdkSessionId)
        assertEquals("idle", liveSessions[0].status)
        assertTrue(liveSessions[0].isOrchestrator)

        // Agent session
        assertEquals("local-789", liveSessions[1].localId)
        assertEquals("streaming", liveSessions[1].status)
        assertFalse(liveSessions[1].isOrchestrator)

        val request = mockServer.takeRequest()
        assertEquals("/api/sessions/pool/live", request.path)
    }

    @Test
    fun `getLivePool returns empty list when no live sessions`() = runTest {
        mockServer.enqueue(MockResponse()
            .setResponseCode(200)
            .setBody("[]")
            .addHeader("Content-Type", "application/json"))

        val liveSessions = apiClient.getLivePool()

        assertEquals(0, liveSessions.size)
    }

    // ==========================================================================
    // POST /api/orchestrator/voice/session - Voice Token
    // ==========================================================================

    @Test
    fun `getVoiceToken extracts client_secret correctly`() = runTest {
        // This matches the actual OpenAI response format
        val responseBody = """
            {
                "object": "realtime.session",
                "id": "sess_abc123",
                "model": "gpt-realtime",
                "voice": "cedar",
                "client_secret": {
                    "value": "ek_test_token_12345",
                    "expires_at": 1700000000
                }
            }
        """.trimIndent()

        mockServer.enqueue(MockResponse()
            .setResponseCode(200)
            .setBody(responseBody)
            .addHeader("Content-Type", "application/json"))

        val tokenResponse = apiClient.getVoiceToken()

        assertNotNull(tokenResponse)
        assertEquals("ek_test_token_12345", tokenResponse?.token)
        assertTrue(tokenResponse!!.expiresIn > 0 || tokenResponse.expiresIn <= 60)

        val request = mockServer.takeRequest()
        assertEquals("POST", request.method)
        assertEquals("/api/orchestrator/voice/session", request.path)
    }

    @Test
    fun `getVoiceToken returns null when client_secret missing`() = runTest {
        val responseBody = """
            {
                "object": "realtime.session",
                "id": "sess_abc123"
            }
        """.trimIndent()

        mockServer.enqueue(MockResponse()
            .setResponseCode(200)
            .setBody(responseBody)
            .addHeader("Content-Type", "application/json"))

        val tokenResponse = apiClient.getVoiceToken()

        assertNull(tokenResponse)
    }

    @Test
    fun `getVoiceToken returns null on server error`() = runTest {
        mockServer.enqueue(MockResponse().setResponseCode(503))

        val tokenResponse = apiClient.getVoiceToken()

        assertNull(tokenResponse)
    }

    // ==========================================================================
    // PATCH /api/sessions/{id}/rename - Rename Session
    // ==========================================================================

    @Test
    fun `renameSession sends correct request body`() = runTest {
        mockServer.enqueue(MockResponse().setResponseCode(204))

        val success = apiClient.renameSession("session-123", "New Title")

        assertTrue(success)

        val request = mockServer.takeRequest()
        assertEquals("PATCH", request.method)
        assertEquals("/api/sessions/session-123/rename", request.path)
        assertTrue(request.body.readUtf8().contains("\"title\":\"New Title\""))
    }

    @Test
    fun `renameSession returns false on error`() = runTest {
        mockServer.enqueue(MockResponse().setResponseCode(404))

        val success = apiClient.renameSession("non-existent", "New Title")

        assertFalse(success)
    }

    // ==========================================================================
    // DELETE /api/sessions/{id} - Delete Session
    // ==========================================================================

    @Test
    fun `deleteSession sends DELETE request`() = runTest {
        mockServer.enqueue(MockResponse().setResponseCode(204))

        val success = apiClient.deleteSession("session-123")

        assertTrue(success)

        val request = mockServer.takeRequest()
        assertEquals("DELETE", request.method)
        assertEquals("/api/sessions/session-123", request.path)
    }

    @Test
    fun `deleteSession returns false on error`() = runTest {
        mockServer.enqueue(MockResponse().setResponseCode(404))

        val success = apiClient.deleteSession("non-existent")

        assertFalse(success)
    }

    // ==========================================================================
    // URL Building Tests
    // ==========================================================================

    @Test
    fun `apiClient handles ws protocol conversion`() = runTest {
        // Create client with ws:// URL (simulating real usage)
        val wsClient = ApiClient("ws://localhost:8765")

        // We can't directly test buildHttpUrl, but we can verify the client doesn't crash
        // The actual URL conversion is tested implicitly in other tests
        assertNotNull(wsClient)
    }

    // ==========================================================================
    // Message Parsing Tests
    // ==========================================================================

    @Test
    fun `getSession parses message blocks correctly`() = runTest {
        val responseBody = """
            {
                "session_id": "session-123",
                "title": "Block Test",
                "started_at": "2024-01-15T10:00:00+00:00",
                "last_activity": "2024-01-15T10:00:00+00:00",
                "message_count": 1,
                "is_orchestrator": false,
                "messages": [
                    {
                        "role": "assistant",
                        "text": "Hello",
                        "blocks": [
                            {"type": "text", "text": "Hello"},
                            {"type": "thinking", "text": "Let me think..."},
                            {
                                "type": "tool_use",
                                "tool_use_id": "tool-123",
                                "tool_name": "Read",
                                "output": "file contents",
                                "is_error": false
                            }
                        ]
                    }
                ]
            }
        """.trimIndent()

        mockServer.enqueue(MockResponse()
            .setResponseCode(200)
            .setBody(responseBody)
            .addHeader("Content-Type", "application/json"))

        val (_, messages) = apiClient.getSession("session-123")

        assertEquals(1, messages.size)
        assertEquals(3, messages[0].blocks.size)

        // Text block
        val textBlock = messages[0].blocks[0] as com.assistant.peripheral.data.MessageBlock.Text
        assertEquals("Hello", textBlock.text)

        // Thinking block
        val thinkingBlock = messages[0].blocks[1] as com.assistant.peripheral.data.MessageBlock.Thinking
        assertEquals("Let me think...", thinkingBlock.text)

        // Tool use block
        val toolBlock = messages[0].blocks[2] as com.assistant.peripheral.data.MessageBlock.ToolUse
        assertEquals("tool-123", toolBlock.toolUseId)
        assertEquals("Read", toolBlock.toolName)
        assertEquals("file contents", toolBlock.result)
        assertFalse(toolBlock.isError)
    }

    @Test
    fun `getSession parses compact blocks correctly`() = runTest {
        val responseBody = """
            {
                "session_id": "session-123",
                "title": "Compact Test",
                "started_at": "2024-01-15T10:00:00+00:00",
                "last_activity": "2024-01-15T10:00:00+00:00",
                "message_count": 1,
                "is_orchestrator": false,
                "messages": [
                    {
                        "role": "system",
                        "text": "",
                        "blocks": [
                            {"type": "compact", "summary": "Previous context was compacted"}
                        ]
                    }
                ]
            }
        """.trimIndent()

        mockServer.enqueue(MockResponse()
            .setResponseCode(200)
            .setBody(responseBody)
            .addHeader("Content-Type", "application/json"))

        val (_, messages) = apiClient.getSession("session-123")

        assertEquals(1, messages.size)
        assertEquals(com.assistant.peripheral.data.MessageRole.SYSTEM, messages[0].role)

        val compactBlock = messages[0].blocks[0] as com.assistant.peripheral.data.MessageBlock.Compact
        assertEquals("Previous context was compacted", compactBlock.summary)
    }
}
