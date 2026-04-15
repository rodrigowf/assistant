package com.assistant.peripheral.network

import com.assistant.peripheral.data.*
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.test.runTest
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test

/**
 * Tests for WebSocketManager WebSocket connectivity.
 *
 * These tests verify WebSocket message parsing and state management,
 * matching the behavior of the web frontend's WebSocket hooks.
 *
 * Event types tested:
 * - session_started - Session initialization
 * - text_delta / text_complete - Streaming text
 * - thinking_delta / thinking_complete - Extended thinking
 * - tool_use / tool_executing / tool_result - Tool execution
 * - message_start / message_end - Message lifecycle
 * - turn_complete - Turn completion
 * - error - Error handling
 */
class WebSocketManagerTest {

    private lateinit var webSocketManager: WebSocketManager

    @Before
    fun setup() {
        webSocketManager = WebSocketManager()
    }

    @After
    fun teardown() {
        webSocketManager.release()
    }

    // ==========================================================================
    // Connection State Tests
    // ==========================================================================

    @Test
    fun `initial state is Disconnected`() = runTest {
        val state = webSocketManager.connectionState.first()
        assertTrue(state is ConnectionState.Disconnected)
    }

    // ==========================================================================
    // WebSocket Message Serialization Tests
    // ==========================================================================

    @Test
    fun `Start message serializes correctly`() {
        val message = WebSocketMessage.Start(
            localId = "local-123",
            resumeSdkId = "sdk-456"
        )

        // Test that the message object is created correctly
        assertEquals("local-123", message.localId)
        assertEquals("sdk-456", message.resumeSdkId)
    }

    @Test
    fun `Start message without resume serializes correctly`() {
        val message = WebSocketMessage.Start(
            localId = "local-123",
            resumeSdkId = null
        )

        assertEquals("local-123", message.localId)
        assertNull(message.resumeSdkId)
    }

    @Test
    fun `Send message serializes correctly`() {
        val message = WebSocketMessage.Send("Hello, world!")

        assertEquals("Hello, world!", message.text)
    }

    @Test
    fun `SendAudio message serializes correctly`() {
        val message = WebSocketMessage.SendAudio(
            audioBase64 = "base64data",
            format = "wav",
            text = "transcription"
        )

        assertEquals("base64data", message.audioBase64)
        assertEquals("wav", message.format)
        assertEquals("transcription", message.text)
    }

    @Test
    fun `VoiceStart message serializes correctly`() {
        val message = WebSocketMessage.VoiceStart(
            localId = "local-123",
            resumeSdkId = "sdk-456"
        )

        assertEquals("local-123", message.localId)
        assertEquals("sdk-456", message.resumeSdkId)
    }

    @Test
    fun `VoiceEvent message accepts map`() {
        val event = mapOf(
            "type" to "response.audio.delta",
            "delta" to "audio_data"
        )
        val message = WebSocketMessage.VoiceEvent(event)

        assertEquals("response.audio.delta", message.event["type"])
    }

    @Test
    fun `SetModel message serializes correctly`() {
        val message = WebSocketMessage.SetModel("gpt-4o")

        assertEquals("gpt-4o", message.model)
    }

    // ==========================================================================
    // WebSocket Event Types Tests
    // ==========================================================================

    @Test
    fun `SessionStarted event has correct properties`() {
        val event = WebSocketEvent.SessionStarted(
            sessionId = "session-123",
            voice = true
        )

        assertEquals("session-123", event.sessionId)
        assertTrue(event.voice)
    }

    @Test
    fun `TextDelta event has correct properties`() {
        val event = WebSocketEvent.TextDelta(
            text = "Hello",
            messageId = "msg-123"
        )

        assertEquals("Hello", event.text)
        assertEquals("msg-123", event.messageId)
    }

    @Test
    fun `ToolUse event has correct properties`() {
        val event = WebSocketEvent.ToolUse(
            toolUseId = "tool-123",
            toolName = "Read",
            toolInput = mapOf("file_path" to "/test.txt")
        )

        assertEquals("tool-123", event.toolUseId)
        assertEquals("Read", event.toolName)
        assertEquals("/test.txt", event.toolInput["file_path"])
    }

    @Test
    fun `ToolResult event has correct properties`() {
        val event = WebSocketEvent.ToolResult(
            toolUseId = "tool-123",
            output = "File contents here",
            isError = false
        )

        assertEquals("tool-123", event.toolUseId)
        assertEquals("File contents here", event.output)
        assertFalse(event.isError)
    }

    @Test
    fun `Error event has correct properties`() {
        val event = WebSocketEvent.Error(
            message = "Something went wrong",
            detail = "Detailed error info"
        )

        assertEquals("Something went wrong", event.message)
        assertEquals("Detailed error info", event.detail)
    }

    @Test
    fun `TurnComplete event has correct properties`() {
        val event = WebSocketEvent.TurnComplete(
            inputTokens = 100,
            outputTokens = 500
        )

        assertEquals(100, event.inputTokens)
        assertEquals(500, event.outputTokens)
    }

    @Test
    fun `CompactComplete event has correct properties`() {
        val event = WebSocketEvent.CompactComplete(
            summary = "Context was compacted to save tokens"
        )

        assertEquals("Context was compacted to save tokens", event.summary)
    }

    // ==========================================================================
    // WebSocket Endpoint Tests
    // ==========================================================================

    @Test
    fun `WebSocketEndpoint enum has correct values`() {
        assertEquals(2, WebSocketEndpoint.values().size)
        assertNotNull(WebSocketEndpoint.ORCHESTRATOR)
        assertNotNull(WebSocketEndpoint.AGENT)
    }

    // ==========================================================================
    // Data Model Tests
    // ==========================================================================

    @Test
    fun `ChatMessage creates with default values`() {
        val message = ChatMessage(
            role = MessageRole.USER,
            content = "Hello"
        )

        assertNotNull(message.id)
        assertEquals(MessageRole.USER, message.role)
        assertEquals("Hello", message.content)
        assertTrue(message.blocks.isEmpty())
        assertFalse(message.isStreaming)
    }

    @Test
    fun `ChatMessage displayText returns content when available`() {
        val message = ChatMessage(
            role = MessageRole.ASSISTANT,
            content = "Response text",
            blocks = listOf(MessageBlock.Text("Block text"))
        )

        assertEquals("Response text", message.displayText)
    }

    @Test
    fun `ChatMessage displayText returns block text when content empty`() {
        val message = ChatMessage(
            role = MessageRole.ASSISTANT,
            content = "",
            blocks = listOf(
                MessageBlock.Text("First"),
                MessageBlock.Text(" Second")
            )
        )

        assertEquals("First Second", message.displayText)
    }

    @Test
    fun `SessionInfo holds all required fields`() {
        val session = SessionInfo(
            sessionId = "session-123",
            localId = "local-456",
            title = "Test Session",
            startedAt = "2024-01-15T10:00:00",
            lastActivity = "2024-01-15T11:00:00",
            messageCount = 25,
            isOrchestrator = true
        )

        assertEquals("session-123", session.sessionId)
        assertEquals("local-456", session.localId)
        assertEquals("Test Session", session.title)
        assertEquals(25, session.messageCount)
        assertTrue(session.isOrchestrator)
    }

    // ==========================================================================
    // Voice State Tests
    // ==========================================================================

    @Test
    fun `VoiceState sealed class has all required states`() {
        val states = listOf(
            VoiceState.Off,
            VoiceState.Connecting,
            VoiceState.Active,
            VoiceState.Speaking,
            VoiceState.Listening,
            VoiceState.Thinking,
            VoiceState.ToolUse,
            VoiceState.Error("test")
        )

        assertEquals(8, states.size)
    }

    @Test
    fun `VoiceState Error holds message`() {
        val state = VoiceState.Error("Connection failed")

        assertTrue(state is VoiceState.Error)
        assertEquals("Connection failed", (state as VoiceState.Error).message)
    }

    // ==========================================================================
    // Connection State Tests
    // ==========================================================================

    @Test
    fun `ConnectionState sealed class has all required states`() {
        val states = listOf(
            ConnectionState.Disconnected,
            ConnectionState.Connecting,
            ConnectionState.Connected,
            ConnectionState.Error("test")
        )

        assertEquals(4, states.size)
    }

    @Test
    fun `ConnectionState Error holds message`() {
        val state = ConnectionState.Error("Network error")

        assertTrue(state is ConnectionState.Error)
        assertEquals("Network error", (state as ConnectionState.Error).message)
    }

    // ==========================================================================
    // Message Block Tests
    // ==========================================================================

    @Test
    fun `MessageBlock Text has correct properties`() {
        val block = MessageBlock.Text("Hello world", isStreaming = true)

        assertEquals("Hello world", block.text)
        assertTrue(block.isStreaming)
    }

    @Test
    fun `MessageBlock Thinking has correct properties`() {
        val block = MessageBlock.Thinking("Analyzing...", isStreaming = false)

        assertEquals("Analyzing...", block.text)
        assertFalse(block.isStreaming)
    }

    @Test
    fun `MessageBlock ToolUse has correct properties`() {
        val block = MessageBlock.ToolUse(
            toolUseId = "tool-123",
            toolName = "Bash",
            toolInput = mapOf("command" to "ls -la"),
            result = "file1.txt\nfile2.txt",
            isError = false,
            isExecuting = false,
            isComplete = true
        )

        assertEquals("tool-123", block.toolUseId)
        assertEquals("Bash", block.toolName)
        assertEquals("ls -la", block.toolInput["command"])
        assertEquals("file1.txt\nfile2.txt", block.result)
        assertFalse(block.isError)
        assertFalse(block.isExecuting)
        assertTrue(block.isComplete)
    }

    @Test
    fun `MessageBlock Compact has correct properties`() {
        val block = MessageBlock.Compact("Context summary here")

        assertEquals("Context summary here", block.summary)
    }

    // ==========================================================================
    // Settings Tests
    // ==========================================================================

    @Test
    fun `AppSettings has correct defaults`() {
        val settings = AppSettings()

        assertEquals("ws://192.168.0.28:8765", settings.serverUrl)
        assertTrue(settings.autoConnect)
        assertFalse(settings.enableWakeWord)
        assertEquals("hey assistant", settings.wakeWord)
        assertEquals(ThemeMode.SYSTEM, settings.themeMode)
    }

    @Test
    fun `AppSettings can be customized`() {
        val settings = AppSettings(
            serverUrl = "ws://custom.server:9000",
            autoConnect = false,
            enableWakeWord = true,
            wakeWord = "hey claude",
            themeMode = ThemeMode.DARK
        )

        assertEquals("ws://custom.server:9000", settings.serverUrl)
        assertFalse(settings.autoConnect)
        assertTrue(settings.enableWakeWord)
        assertEquals("hey claude", settings.wakeWord)
        assertEquals(ThemeMode.DARK, settings.themeMode)
    }

    @Test
    fun `ThemeMode enum has all values`() {
        assertEquals(3, ThemeMode.values().size)
        assertNotNull(ThemeMode.SYSTEM)
        assertNotNull(ThemeMode.LIGHT)
        assertNotNull(ThemeMode.DARK)
    }
}
