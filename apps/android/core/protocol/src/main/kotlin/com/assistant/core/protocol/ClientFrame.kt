package com.assistant.core.protocol

import com.assistant.core.model.VoiceConfig
import kotlinx.serialization.json.JsonObject

/**
 * Every client→server frame (inv01 §4.1, §5.2, §7.4). Always sent as a TEXT frame (T-2).
 * There is deliberately no `ping` (T-4).
 */
sealed interface ClientFrame {
    val type: String

    /**
     * Chat or orchestrator `start` (spec 12 §3.3). `resumeFrom` only per T-10. [reattach] (OPEN-2) on
     * every automatic start: the server only subscribes to an open conversation, never creates one.
     */
    data class Start(
        val localId: String,
        val resumeSdkId: String? = null,
        val resumeFrom: ResumeCursor? = null,
        /** Chat only. */
        val fork: Boolean? = null,
        /** Chat only: literal `{name: config}` map overriding the MCP selection. */
        val mcpServers: JsonObject? = null,
        val reattach: Boolean? = null,
    ) : ClientFrame { override val type get() = "start" }

    /** Orchestrator voice start / re-arm (spec 12 §7.3). Null voice fields are omitted (V-1). */
    data class VoiceStart(
        val localId: String,
        val resumeSdkId: String? = null,
        val voice: VoiceConfig = VoiceConfig(),
        /** OPEN-2: the re-arm after a reconnect; a voice start the user pressed has none. */
        val reattach: Boolean? = null,
    ) : ClientFrame { override val type get() = "voice_start" }

    data class Send(val text: String) : ClientFrame { override val type get() = "send" }

    /** Orchestrator: shared text / uploaded-file link (§6.15). */
    data class InjectText(val text: String) : ClientFrame { override val type get() = "inject_text" }

    /** Orchestrator WS only (A-8.6). */
    data class SendAudio(val audio: String, val format: String, val text: String? = null) : ClientFrame {
        override val type get() = "send_audio"
    }

    data object Interrupt : ClientFrame { override val type get() = "interrupt" }

    /** Chat: slash command. */
    data class Command(val text: String) : ClientFrame { override val type get() = "command" }

    data object Compact : ClientFrame { override val type get() = "compact" }

    data class PermissionResponse(
        val requestId: String,
        /** `"allow"` | `"deny"`. */
        val decision: String,
        val message: String? = null,
        /** Agent `localId` when answering on a socket attached to another session (T-8). */
        val sessionId: String? = null,
    ) : ClientFrame { override val type get() = "permission_response" }

    /** Chat: detach only. Orchestrator: stop it. Never sent from lifecycle paths (decision P-1). */
    data object Stop : ClientFrame { override val type get() = "stop" }

    data object VoiceStop : ClientFrame { override val type get() = "voice_stop" }

    data class SetModel(val model: String) : ClientFrame { override val type get() = "set_model" }

    data object GetModel : ClientFrame { override val type get() = "get_model" }

    data object GetModels : ClientFrame { override val type get() = "get_models" }

    /** OpenAI: mirror of every data-channel event. WS providers: control frames forwarded upstream. */
    data class VoiceEvent(val event: JsonObject) : ClientFrame { override val type get() = "voice_event" }

    /** Base64 PCM16 LE mono at `audio_in_format.sample_rate`. */
    data class VoiceAudioIn(val audio: String) : ClientFrame { override val type get() = "voice_audio_in" }

    data class VoiceRecordingChunk(val channel: String, val audio: String) : ClientFrame {
        override val type get() = "voice_recording_chunk"
    }

    data object VoiceRecordingEnd : ClientFrame { override val type get() = "voice_recording_end" }
}
