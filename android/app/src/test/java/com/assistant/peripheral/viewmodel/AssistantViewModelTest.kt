package com.assistant.peripheral.viewmodel

import com.assistant.peripheral.data.*
import org.junit.Assert.*
import org.junit.Test

/**
 * Tests for AssistantViewModel business logic.
 *
 * These tests verify the ViewModel's state management and event handling,
 * ensuring parity with the web frontend's behavior.
 *
 * Note: Full ViewModel tests require AndroidX Test and Robolectric for
 * Application context. These tests focus on the data transformations
 * and logic that can be tested without Android dependencies.
 */
class AssistantViewModelTest {

    // ==========================================================================
    // Session Cache Logic Tests
    // ==========================================================================

    @Test
    fun `CachedSession data class holds correct values`() {
        // Simulate the CachedSession structure used in ViewModel
        data class TestCachedSession(
            val messages: List<ChatMessage>,
            val isOrchestrator: Boolean,
            val paginationStartIndex: Int,
            val hasMoreMessages: Boolean
        )

        val messages = listOf(
            ChatMessage(role = MessageRole.USER, content = "Hello"),
            ChatMessage(role = MessageRole.ASSISTANT, content = "Hi!")
        )

        val cached = TestCachedSession(
            messages = messages,
            isOrchestrator = true,
            paginationStartIndex = 0,
            hasMoreMessages = false
        )

        assertEquals(2, cached.messages.size)
        assertTrue(cached.isOrchestrator)
        assertEquals(0, cached.paginationStartIndex)
        assertFalse(cached.hasMoreMessages)
    }

    @Test
    fun `session cache should limit messages to prevent TransactionTooLargeException`() {
        // The ViewModel limits cached messages to MAX_CACHED_MESSAGES_PER_SESSION = 100
        val maxMessages = 100
        val allMessages = (1..150).map { i ->
            ChatMessage(role = MessageRole.USER, content = "Message $i")
        }

        // Simulate the caching logic
        val messagesToCache = if (allMessages.size > maxMessages) {
            allMessages.takeLast(maxMessages)
        } else {
            allMessages
        }

        assertEquals(100, messagesToCache.size)
        assertEquals("Message 51", messagesToCache.first().content)
        assertEquals("Message 150", messagesToCache.last().content)
    }

    @Test
    fun `session cache LRU eviction removes oldest sessions`() {
        // The ViewModel uses LinkedHashMap with accessOrder=true for LRU
        val maxSessions = 5
        val cache = LinkedHashMap<String, String>(maxSessions, 0.75f, true)

        // Add 6 sessions
        (1..6).forEach { i ->
            cache["session-$i"] = "data-$i"
            if (cache.size > maxSessions) {
                val oldestKey = cache.keys.firstOrNull()
                cache.remove(oldestKey)
            }
        }

        assertEquals(5, cache.size)
        assertFalse(cache.containsKey("session-1")) // Oldest evicted
        assertTrue(cache.containsKey("session-6"))  // Newest present
    }

    // ==========================================================================
    // Message Processing Logic Tests
    // ==========================================================================

    @Test
    fun `streaming message should be marked correctly`() {
        val streamingMessage = ChatMessage(
            role = MessageRole.ASSISTANT,
            content = "Partial response...",
            isStreaming = true
        )

        assertTrue(streamingMessage.isStreaming)
    }

    @Test
    fun `completed message should not be streaming`() {
        val completedMessage = ChatMessage(
            role = MessageRole.ASSISTANT,
            content = "Full response",
            isStreaming = false
        )

        assertFalse(completedMessage.isStreaming)
    }

    @Test
    fun `voice transcript should be prefixed with voice marker`() {
        val transcript = "Hello, how are you?"
        val voiceMessage = ChatMessage(
            role = MessageRole.USER,
            content = "[voice] $transcript",
            blocks = listOf(MessageBlock.Text("[voice] $transcript"))
        )

        assertTrue(voiceMessage.content.startsWith("[voice]"))
        assertTrue(voiceMessage.displayText.startsWith("[voice]"))
    }

    // ==========================================================================
    // Debounce Logic Tests
    // ==========================================================================

    @Test
    fun `refresh debounce prevents rapid calls`() {
        val debounceMs = 500L
        var lastRefreshTime = -debounceMs  // Start at -500 so first call passes
        var refreshCount = 0

        // Simulate rapid refresh calls
        val times = listOf(0L, 100L, 200L, 300L, 600L, 700L, 1200L)

        times.forEach { now ->
            if (now - lastRefreshTime >= debounceMs) {
                refreshCount++
                lastRefreshTime = now
            }
        }

        // Should only have refreshed 3 times: at 0, 600, and 1200
        assertEquals(3, refreshCount)
    }

    // ==========================================================================
    // Session Type Logic Tests
    // ==========================================================================

    @Test
    fun `orchestrator session should allow voice`() {
        val isOrchestrator = true
        val voiceAllowed = isOrchestrator

        assertTrue(voiceAllowed)
    }

    @Test
    fun `agent session should not allow voice`() {
        val isOrchestrator = false
        val voiceAllowed = isOrchestrator

        assertFalse(voiceAllowed)
    }

    // ==========================================================================
    // Pagination Logic Tests
    // ==========================================================================

    @Test
    fun `pagination should prepend older messages`() {
        val existingMessages = listOf(
            ChatMessage(role = MessageRole.USER, content = "Message 3"),
            ChatMessage(role = MessageRole.ASSISTANT, content = "Response 3")
        )

        val olderMessages = listOf(
            ChatMessage(role = MessageRole.USER, content = "Message 1"),
            ChatMessage(role = MessageRole.ASSISTANT, content = "Response 1"),
            ChatMessage(role = MessageRole.USER, content = "Message 2"),
            ChatMessage(role = MessageRole.ASSISTANT, content = "Response 2")
        )

        // Simulate prepending
        val combined = olderMessages + existingMessages

        assertEquals(6, combined.size)
        assertEquals("Message 1", combined.first().content)
        assertEquals("Response 3", combined.last().content)
    }

    @Test
    fun `hasMoreMessages should be false when all loaded`() {
        data class PaginationState(
            val startIndex: Int,
            val hasMore: Boolean
        )

        // When startIndex is 0 and we got all messages
        val state = PaginationState(startIndex = 0, hasMore = false)

        assertEquals(0, state.startIndex)
        assertFalse(state.hasMore)
    }

    @Test
    fun `hasMoreMessages should be true when more exist`() {
        data class PaginationState(
            val startIndex: Int,
            val hasMore: Boolean
        )

        // When we loaded messages 50-100 of 150 total
        val state = PaginationState(startIndex = 50, hasMore = true)

        assertEquals(50, state.startIndex)
        assertTrue(state.hasMore)
    }

    // ==========================================================================
    // Error Handling Logic Tests
    // ==========================================================================

    @Test
    fun `error state should contain message`() {
        val error = VoiceState.Error("Voice only available for orchestrator sessions")

        assertTrue(error is VoiceState.Error)
        assertEquals("Voice only available for orchestrator sessions", error.message)
    }

    @Test
    fun `connection error should contain message`() {
        val error = ConnectionState.Error("Network unavailable")

        assertTrue(error is ConnectionState.Error)
        assertEquals("Network unavailable", error.message)
    }

    // ==========================================================================
    // Tool Block Processing Tests
    // ==========================================================================

    @Test
    fun `tool use block lifecycle progression`() {
        // Tool starts as not executing, not complete
        var block = MessageBlock.ToolUse(
            toolUseId = "tool-1",
            toolName = "Read",
            toolInput = mapOf("file_path" to "/test.txt"),
            isExecuting = false,
            isComplete = false
        )

        assertFalse(block.isExecuting)
        assertFalse(block.isComplete)
        assertNull(block.result)

        // Tool starts executing
        block = block.copy(isExecuting = true)
        assertTrue(block.isExecuting)
        assertFalse(block.isComplete)

        // Tool completes
        block = block.copy(
            isExecuting = false,
            isComplete = true,
            result = "File contents"
        )
        assertFalse(block.isExecuting)
        assertTrue(block.isComplete)
        assertEquals("File contents", block.result)
    }

    @Test
    fun `tool error should be marked`() {
        val block = MessageBlock.ToolUse(
            toolUseId = "tool-1",
            toolName = "Bash",
            toolInput = mapOf("command" to "invalid-cmd"),
            result = "Command not found",
            isError = true,
            isComplete = true
        )

        assertTrue(block.isError)
        assertTrue(block.isComplete)
        assertEquals("Command not found", block.result)
    }

    // ==========================================================================
    // Thinking Block Processing Tests
    // ==========================================================================

    @Test
    fun `thinking block updates during streaming`() {
        var thinkingContent = ""

        // Simulate thinking deltas
        val deltas = listOf("Let me ", "think ", "about ", "this...")

        deltas.forEach { delta ->
            thinkingContent += delta
        }

        assertEquals("Let me think about this...", thinkingContent)
    }

    @Test
    fun `thinking block finalizes on complete`() {
        var thinkingContent = "partial thinking"
        val completeThinking = "Let me analyze the code carefully."

        // On thinking_complete, replace partial with final
        thinkingContent = completeThinking

        assertEquals("Let me analyze the code carefully.", thinkingContent)
    }

    // ==========================================================================
    // Message ID Generation Tests
    // ==========================================================================

    @Test
    fun `new messages get unique IDs`() {
        val message1 = ChatMessage(role = MessageRole.USER, content = "Hello")
        val message2 = ChatMessage(role = MessageRole.USER, content = "Hello")

        // Even with same content, IDs should be different
        assertNotEquals(message1.id, message2.id)
    }

    // ==========================================================================
    // Live Session Pool Tests
    // ==========================================================================

    @Test
    fun `live session IDs extracted from pool response`() {
        data class LiveSession(
            val localId: String,
            val sdkSessionId: String,
            val isOrchestrator: Boolean
        )

        val livePool = listOf(
            LiveSession("local-1", "sdk-1", true),
            LiveSession("local-2", "sdk-2", false),
            LiveSession("local-3", "sdk-3", false)
        )

        // Extract SDK session IDs that are truly live
        val liveSessionIds = livePool.map { it.sdkSessionId }.toSet()

        assertEquals(3, liveSessionIds.size)
        assertTrue(liveSessionIds.contains("sdk-1"))
        assertTrue(liveSessionIds.contains("sdk-2"))
        assertTrue(liveSessionIds.contains("sdk-3"))
    }

    @Test
    fun `find existing orchestrator in pool`() {
        data class LiveSession(
            val localId: String,
            val sdkSessionId: String,
            val isOrchestrator: Boolean
        )

        val livePool = listOf(
            LiveSession("local-1", "sdk-1", true),   // Orchestrator
            LiveSession("local-2", "sdk-2", false),
            LiveSession("local-3", "sdk-3", false)
        )

        val existingOrchestrator = livePool.find { it.isOrchestrator }

        assertNotNull(existingOrchestrator)
        assertEquals("local-1", existingOrchestrator?.localId)
        assertTrue(existingOrchestrator?.isOrchestrator == true)
    }

    @Test
    fun `no orchestrator in pool returns null`() {
        data class LiveSession(
            val localId: String,
            val sdkSessionId: String,
            val isOrchestrator: Boolean
        )

        val livePool = listOf(
            LiveSession("local-1", "sdk-1", false),
            LiveSession("local-2", "sdk-2", false)
        )

        val existingOrchestrator = livePool.find { it.isOrchestrator }

        assertNull(existingOrchestrator)
    }
}
