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
    val isOrchestrator: Boolean = false,
    // Which agent harness backed this session — "claude" or "qwen".
    // Orchestrator sessions don't go through either CLI; ignore for them.
    val provider: String = "claude"
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
    /** Backend is rebuilding the history summary (the LLM call inside
     *  get_session_update). Shown as a yellow "Preparing conversation"
     *  state so the user knows the wake-word landed but the session
     *  isn't ready yet. Distinct from [Connecting] (network handshake)
     *  because this can take 15-25s on long sessions. */
    object Summarizing : VoiceState()
    object Active : VoiceState()
    object Speaking : VoiceState()
    object Listening : VoiceState()
    object Thinking : VoiceState()
    object ToolUse : VoiceState()
    /** Backend is tearing the voice session down — flushing graceful
     *  shutdown frames, closing the upstream WS. Shown as "Ending..."
     *  with a spinner so the user knows the stop request is in flight
     *  and not just frozen. Flips to [Off] on the [WebSocketEvent.VoiceEnded]
     *  ack (or after a 5s safety timeout if the ack never arrives). */
    object Ending : VoiceState()
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
        val voiceSessionUpdate: Map<String, Any?>? = null,  // session.update payload for OpenAI
        // True when this WS is the one that started/owns the voice
        // session. False on reconnects where another client (a
        // different device on the same orchestrator) is the actual
        // initiator — we shouldn't spin up our own provider transport
        // in that case, only mirror the voice UI state.
        val voiceInitiator: Boolean = true
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
    /** Backend has begun teardown — UI should show "Ending..." until
     *  [VoiceEnded] arrives. */
    data class VoiceEnding(val reason: String) : WebSocketEvent()
    /** Backend teardown is complete — UI flips to Off. */
    data class VoiceEnded(val reason: String) : WebSocketEvent()
    /** Legacy: emitted alongside [VoiceEnded] by the backend for one
     *  release of the migration. Remove after the new path is verified. */
    object VoiceStopped : WebSocketEvent()

    /** Provider event mirrored from backend (WebSocket providers only). */
    data class VoiceProviderEvent(val event: Map<String, Any?>) : WebSocketEvent()
    /** Speaker chunk from WS-path voice providers.  Base64-encoded PCM. */
    data class VoiceAudioOut(val audioBase64: String) : WebSocketEvent()

    /**
     * Increment B (voice subsystem refactor) — Silero VAD state surfaced
     * from the backend ``voice_vad_state`` event. Additive to the
     * existing ``VoiceProviderEvent`` envelope; UI components watch a
     * ``VadState`` flow on the ViewModel to render a "listening Ns"
     * duration indicator when the user is stuck in speech_started.
     *
     * String value of [state] mirrors orchestrator's
     * ``VadState`` enum: "listening" | "thinking" | "idle".
     */
    data class VoiceVadState(
        val state: String,
        val durationMs: Long,
        val sileroProb: Double? = null,
    ) : WebSocketEvent()

    /**
     * Typed upstream-provider error from the backend ``voice_error`` event.
     *
     * Replaces the opaque ``Error("voice_relay_failed", ...)`` rendering
     * with a categorised envelope the UI can render with targeted
     * affordances (billing-cap deep link, auth banner, etc.). The legacy
     * ``Error`` event is still emitted alongside this for back-compat
     * with the existing system-message error path.
     *
     * String values mirror ``orchestrator.voice_errors.VoiceErrorCategory``.
     */
    data class VoiceError(
        val category: String,
        val message: String,
        val recoverable: Boolean,
        val recoveryHint: String? = null,
        val providerDocUrl: String? = null,
        val rawCloseCode: Int? = null,
        val rawCloseReason: String? = null,
        val provider: String,
    ) : WebSocketEvent()

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

    // Voice session (transport-agnostic — WebRTC for OpenAI, WebSocket for Qwen, etc.)
    data class VoiceStart(
        val localId: String? = null,
        val resumeSdkId: String? = null,
        // Voice provider/model/voice/language fields the backend uses to
        // pick the upstream provider.  Null/empty = use backend default
        // (resolved from assistant_config.json).
        val voiceProvider: String? = null,
        val voiceModel: String? = null,
        val voiceName: String? = null,
        val voiceTranscriptionLanguage: String? = null,
        // Google-only backend selector ("vertex" | "aistudio"); ignored by
        // other providers. Required for Gemini AI-Studio-only models.
        val voiceEndpoint: String? = null,
    ) : WebSocketMessage()
    object VoiceStop : WebSocketMessage()
    data class VoiceEvent(val event: Map<String, Any?>) : WebSocketMessage()
    /** Mic chunk for WS-path voice providers (Qwen).  Base64-encoded PCM16. */
    data class VoiceAudioIn(val audioBase64: String) : WebSocketMessage()

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
 * A user-saved server entry shown in Settings for quick switching.
 */
data class SavedServer(
    val label: String,
    val url: String
)

/**
 * App settings stored in DataStore.
 */
data class AppSettings(
    val serverUrl: String = "ws://192.168.0.200:80",
    val savedServers: List<SavedServer> = emptyList(),
    val autoConnect: Boolean = true,
    val enableWakeWord: Boolean = true,
    // Per Detour 3 naming convention (plan §0.5): `talkWord` triggers a single
    // turn-based voice message ("push-to-talk"-style); `wakeWord` triggers a
    // realtime WebRTC voice conversation ("wake up the assistant").
    val talkWord: String = "my friend",        // comma-separated, triggers single turn-based voice message
    val wakeWord: String = "wake up",          // comma-separated, triggers realtime WebRTC voice conversation
    val themeMode: ThemeMode = ThemeMode.SYSTEM,
    val micGainLevel: Float = 1.0f,            // 0.0 to 1.5, where 1.0 is normal (voice session only)
    val wakeWordMicGainLevel: Float = 1.0f,    // 0.0 to 1.5, scales RMS threshold for the wake-word detector (umbrella, both phrase types)
    val speakerVolumeLevel: Float = 1.0f,      // 0.0 to 1.5, where 1.0 is 100%
    val echoDuckingGain: Float = 0.05f,        // 0.0 to 1.0, mic gain while agent is speaking (5% default)
    val audioOutput: AudioOutput = AudioOutput.AUTO,  // where voice session audio is routed; AUTO lets the OS pick
    val enableButtonTrigger: Boolean = false   // long-press recents button starts voice session
)

/**
 * Audio output routing for voice sessions.
 *
 * AUTO is the default: hand routing to the Android system audio policy and let it pick
 * whatever output device is appropriate (wired headphone if plugged, BT A2DP if paired
 * and active, otherwise built-in loudspeaker). The OS automatically reacts to
 * plug/unplug events without app involvement, so this is the most robust choice for
 * the dedicated-device use case where the user just wants "use whatever's connected".
 *
 * The other modes are explicit overrides for power users:
 *   - LOUDSPEAKER: force built-in speaker even if a headphone is plugged.
 *   - EARPIECE: force the phone earpiece (private listening).
 *   - BLUETOOTH: force the call-audio plane through a BT HFP headset (gives you the
 *     BT device's mic too). Requires a connected BT audio device.
 *   - WIRED: force the 3.5mm jack via the call-audio plane. Mostly obsoleted by AUTO,
 *     kept for cases where MODE_NORMAL routing isn't what the user wants. Requires a
 *     wired plug.
 */
enum class AudioOutput {
    AUTO,
    LOUDSPEAKER,
    EARPIECE,
    BLUETOOTH,
    WIRED;

    companion object {
        /** Safe parse for DataStore — falls back to AUTO on unknown / null. */
        fun fromString(value: String?): AudioOutput =
            values().firstOrNull { it.name == value } ?: AUTO
    }
}
