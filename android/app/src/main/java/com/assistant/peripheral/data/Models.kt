package com.assistant.peripheral.data

import java.util.UUID

/**
 * Message block types for rich content (matches web frontend).
 */
sealed class MessageBlock {
    data class Text(
        val text: String,
        val isStreaming: Boolean = false
    ) : MessageBlock()

    data class Thinking(
        val text: String,
        val isStreaming: Boolean = false
    ) : MessageBlock()

    data class ToolUse(
        val toolUseId: String,
        val toolName: String,
        val toolInput: Map<String, Any?> = emptyMap(),
        val result: String? = null,
        val isError: Boolean = false,
        val isExecuting: Boolean = false,
        val isComplete: Boolean = false
    ) : MessageBlock()

    data class Compact(val summary: String) : MessageBlock()
}

/**
 * Represents a chat message in the conversation.
 */
data class ChatMessage(
    val id: String = UUID.randomUUID().toString(),
    val role: MessageRole,
    val content: String = "",
    val blocks: List<MessageBlock> = emptyList(),
    val timestamp: Long = System.currentTimeMillis(),
    val isStreaming: Boolean = false
) {
    // Convenience property to get text content from blocks
    val displayText: String
        get() = if (content.isNotEmpty()) content else blocks.filterIsInstance<MessageBlock.Text>()
            .joinToString("") { it.text }
}

enum class MessageRole {
    USER,
    ASSISTANT,
    SYSTEM
}

/**
 * Represents a session from the server (matches SessionInfo from REST API).
 */
data class SessionInfo(
    val sessionId: String,
    val localId: String? = null,
    val title: String,
    val startedAt: String,
    val lastActivity: String,
    val messageCount: Int,
    val isOrchestrator: Boolean = false
)

/**
 * WebSocket connection state.
 */
sealed class ConnectionState {
    object Disconnected : ConnectionState()
    object Connecting : ConnectionState()
    object Connected : ConnectionState()
    data class Error(val message: String) : ConnectionState()
}

/**
 * Voice connection state (matches web frontend VoiceButton states).
 */
sealed class VoiceState {
    object Off : VoiceState()
    object Connecting : VoiceState()
    object Active : VoiceState()
    object Speaking : VoiceState()
    object Listening : VoiceState()
    object Thinking : VoiceState()
    object ToolUse : VoiceState()
    data class Error(val message: String) : VoiceState()
}

/**
 * Events from the WebSocket (matches ServerEvent types from web).
 */
sealed class WebSocketEvent {
    // Text streaming
    data class TextDelta(val text: String, val messageId: String? = null) : WebSocketEvent()
    data class TextComplete(val text: String) : WebSocketEvent()

    // Thinking blocks (extended thinking for o1 models)
    data class ThinkingDelta(val text: String) : WebSocketEvent()
    data class ThinkingComplete(val text: String) : WebSocketEvent()

    // Message lifecycle
    data class MessageStart(val messageId: String) : WebSocketEvent()
    object MessageEnd : WebSocketEvent()

    // Tool events
    data class ToolUse(
        val toolUseId: String,
        val toolName: String,
        val toolInput: Map<String, Any?> = emptyMap()
    ) : WebSocketEvent()
    data class ToolExecuting(val toolUseId: String, val toolName: String) : WebSocketEvent()
    data class ToolProgress(val toolUseId: String, val message: String) : WebSocketEvent()
    data class ToolResult(val toolUseId: String, val output: String, val isError: Boolean) : WebSocketEvent()

    // Session events
    data class SessionStarted(
        val sessionId: String,
        val voice: Boolean = false,
        val voiceSessionUpdate: Map<String, Any?>? = null  // session.update payload for OpenAI
    ) : WebSocketEvent()
    object SessionStopped : WebSocketEvent()
    data class TurnComplete(val inputTokens: Int, val outputTokens: Int) : WebSocketEvent()

    // Status updates
    data class Status(val status: String) : WebSocketEvent()

    // Error
    data class Error(val message: String, val detail: String? = null) : WebSocketEvent()

    // Connection
    object Connected : WebSocketEvent()
    object Disconnected : WebSocketEvent()

    // Session list (from REST API via ViewModel)
    data class SessionList(val sessions: List<SessionInfo>) : WebSocketEvent()

    // History loaded (after loading session from REST API)
    data class HistoryLoaded(val messages: List<ChatMessage>) : WebSocketEvent()

    // Voice events (for WebRTC integration)
    data class VoiceCommand(val command: Map<String, Any?>) : WebSocketEvent()
    data class VoiceTranscript(val text: String, val isFinal: Boolean) : WebSocketEvent()
    object VoiceStopped : WebSocketEvent()  // AI-initiated clean session end

    // Compaction
    data class CompactComplete(val summary: String) : WebSocketEvent()
}

/**
 * Message to send to the WebSocket (matches client→server types from API).
 */
sealed class WebSocketMessage {
    // Session management
    data class Start(
        val localId: String? = null,
        val resumeSdkId: String? = null
    ) : WebSocketMessage()
    object Stop : WebSocketMessage()

    // Voice session (WebRTC)
    data class VoiceStart(
        val localId: String? = null,
        val resumeSdkId: String? = null
    ) : WebSocketMessage()
    object VoiceStop : WebSocketMessage()
    data class VoiceEvent(val event: Map<String, Any?>) : WebSocketMessage()

    // Chat messages
    data class Send(val text: String) : WebSocketMessage()
    data class SendAudio(val audioBase64: String, val format: String = "wav", val text: String? = null) : WebSocketMessage()

    // Control
    object Interrupt : WebSocketMessage()
    object Compact : WebSocketMessage()

    // Model selection
    data class SetModel(val model: String) : WebSocketMessage()
    object GetModel : WebSocketMessage()
    object GetModels : WebSocketMessage()
}

/**
 * Theme mode for the app.
 */
enum class ThemeMode {
    SYSTEM,
    LIGHT,
    DARK
}

/**
 * App settings stored in DataStore.
 */
data class AppSettings(
    val serverUrl: String = "ws://192.168.0.200:80",
    val autoConnect: Boolean = true,
    val enableWakeWord: Boolean = true,
    val wakeWord: String = "my friend",        // comma-separated, triggers turn-based voice input
    val voiceWord: String = "wake up",         // comma-separated, triggers realtime WebRTC voice session
    val themeMode: ThemeMode = ThemeMode.SYSTEM,
    val micGainLevel: Float = 1.0f,            // 0.0 to 1.5, where 1.0 is normal (voice session only)
    val wakeWordMicGainLevel: Float = 1.0f,    // 0.0 to 1.5, scales RMS threshold for wake word detection
    val speakerVolumeLevel: Float = 1.0f,      // 0.0 to 1.5, where 1.0 is 100%
    val echoDuckingGain: Float = 0.05f,        // 0.0 to 1.0, mic gain while agent is speaking (5% default)
    val useEarpiece: Boolean = false,          // false = loudspeaker (default), true = earpiece
    val enableButtonTrigger: Boolean = false   // long-press recents button starts voice session
)
