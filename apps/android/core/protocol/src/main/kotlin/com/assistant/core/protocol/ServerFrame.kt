package com.assistant.core.protocol

import com.assistant.core.model.ConnectionInfo
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject

/** `session_started.resume_state` (inv01 §4.5). */
data class ResumeState(val streamId: String, val nextSeq: Long)

/** `start.resume_from`: the last `(stream_id, seq)` this client processed. */
data class ResumeCursor(val streamId: String, val seq: Long)

/**
 * Every server→client frame of the chat WS (inv01 §4.2) and the orchestrator WS (inv01 §5.3, §7.4).
 *
 * Field names mirror the backend; optional/nullable fields are `null` when absent or of the wrong
 * type. `seq`/`streamId` are set only where the backend stamps them (Claude agent content frames,
 * and the `event_data` of a `nested_session_event`).
 *
 * Provider-native payloads (`voice_event.event`, `voice_command.command`, `voice_session_update`)
 * stay as full recursive [JsonObject] trees (fixes inv04 B1, the shallow map).
 */
sealed interface ServerFrame {
    val type: String
    val seq: Long?
    val streamId: String?

    // ───────────── session lifecycle ─────────────

    /** Chat and orchestrator. Voice fields are present only on the orchestrator. */
    data class SessionStarted(
        /** The conversation's `localId` (ID-1). */
        val sessionId: String?,
        val contextWindow: Long? = null,
        val resumeState: ResumeState? = null,
        val replayOverflow: Boolean = false,
        /** Orchestrator only (backend O-3): the jsonl / REST history id. */
        val jsonlId: String? = null,
        val voice: Boolean? = null,
        val modelInfo: OrchestratorModelInfoDto? = null,
        val voiceInitiator: Boolean? = null,
        val voiceProvider: String? = null,
        val voiceModel: String? = null,
        val voiceName: String? = null,
        val voiceTranscriptionLanguage: String? = null,
        val voiceRecordingEnabled: Boolean? = null,
        val voiceSessionUpdate: JsonObject? = null,
        val voiceConnectionInfo: ConnectionInfo? = null,
        val voiceConnectionError: String? = null,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "session_started" }

    /** `connecting`, `processing`, `retrying`, `interrupted` (chat); `streaming`, `idle`, `interrupted` (orchestrator). */
    data class Status(
        val status: String?,
        val detail: String? = null,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "status" }

    data class SessionStopped(
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "session_stopped" }

    data class SessionTerminated(
        val reason: String?,
        val detail: String? = null,
        val sdkSessionId: String? = null,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "session_terminated" }

    /** Top-level `error` is always a string code (the object form only exists inside `voice_event`, G-28). */
    data class Error(
        val error: String?,
        val detail: String? = null,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "error" }

    // ───────────── prompts and content ─────────────

    /**
     * `{text}`, `{text, queued: true}` (chat), `{text, source: "shared_inject"}` (orchestrator voice) or
     * `{text, source: "voice_message"}` (orchestrator: another device's `send_audio`; `text` = its prompt or "").
     */
    data class UserMessage(
        val text: String,
        val queued: Boolean = false,
        val source: String? = null,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "user_message" }

    data class TextDelta(
        val text: String,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "text_delta" }

    data class TextComplete(
        val text: String,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "text_complete" }

    data class ThinkingDelta(
        val text: String,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "thinking_delta" }

    data class ThinkingComplete(
        val text: String,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "thinking_complete" }

    data class ToolUse(
        val toolUseId: String?,
        val toolName: String?,
        val toolInput: JsonObject?,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "tool_use" }

    /**
     * `output` is kept as raw JSON: a string on every path except Gemini voice, where it may be an
     * object (spec 12 §4.5). The reducer normalises it (R-3).
     */
    data class ToolResult(
        val toolUseId: String?,
        val output: JsonElement?,
        val isError: Boolean?,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "tool_result" }

    data class ToolExecuting(
        val toolUseId: String?,
        val toolName: String? = null,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "tool_executing" }

    data class ToolProgress(
        val toolUseId: String?,
        val toolName: String? = null,
        val elapsedSeconds: Double? = null,
        val message: String? = null,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "tool_progress" }

    /** Chat: every field. Orchestrator: only `input_tokens`/`output_tokens`. */
    data class TurnComplete(
        val cost: Double? = null,
        val usage: JsonObject? = null,
        val inputTokens: Long? = null,
        val outputTokens: Long? = null,
        val numTurns: Int? = null,
        /** Chat only: the sdk id (ID-2). */
        val sessionId: String? = null,
        val isError: Boolean? = null,
        val result: String? = null,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "turn_complete" }

    /** Chat: `{trigger, summary}`. Orchestrator: `{trigger, tokens_before, tokens_after}`. */
    data class CompactComplete(
        val trigger: String? = null,
        val summary: String? = null,
        val tokensBefore: Long? = null,
        val tokensAfter: Long? = null,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "compact_complete" }

    /** Advisory. Exempt from seq dedupe (SEQ-2). */
    data class SessionStalled(
        val elapsedSeconds: Double?,
        val lastToolName: String? = null,
        val lastToolUseId: String? = null,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "session_stalled" }

    data class PermissionRequest(
        val requestId: String?,
        val toolName: String?,
        val toolInput: JsonObject?,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "permission_request" }

    data class PermissionResolved(
        val requestId: String?,
        val decision: String?,
        val responder: String? = null,
        val message: String? = null,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "permission_resolved" }

    // ───────────── orchestrator-only ─────────────

    data class ModelChanged(
        val modelInfo: OrchestratorModelInfoDto?,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "model_changed" }

    /** Direct reply to `get_model`. */
    data class ModelInfo(
        val modelInfo: OrchestratorModelInfoDto?,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "model_info" }

    /** Direct reply to `get_models`. */
    data class ModelsList(
        val models: List<ModelInfoDto>,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "models_list" }

    /** A delegated agent's permission event; `eventData` is the chat-WS payload (incl. seq). */
    data class NestedSessionEvent(
        /** Agent `localId`. */
        val sessionId: String?,
        val eventType: String?,
        val eventData: JsonObject?,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame {
        override val type get() = "nested_session_event"

        /** `eventData` decoded as the chat frame it wraps (its `type` is `eventType` when missing). */
        fun decodedEvent(): ServerFrame? {
            val data = eventData ?: return null
            val typed = if (data["type"] == null && eventType != null) {
                JsonObject(data + ("type" to kotlinx.serialization.json.JsonPrimitive(eventType)))
            } else data
            return ProtocolCodec.decodeServer(typed)
        }
    }

    /** Pool watcher event; arrives on every orchestrator WS, even unsubscribed (T-7). */
    data class AgentSessionOpened(
        /** `localId`. */
        val sessionId: String?,
        val sdkSessionId: String? = null,
        val isOrchestrator: Boolean = false,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "agent_session_opened" }

    data class AgentSessionClosed(
        val sessionId: String?,
        val isOrchestrator: Boolean = false,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "agent_session_closed" }

    /**
     * Turn watcher event (spec 12 §3.7): an agent session (any harness, never the orchestrator)
     * started a turn, from any device or delegated by the orchestrator. Pool watchers only (every
     * orchestrator WS); handled at the app level (device notifications), never by a reducer.
     */
    data class AgentTurnStarted(
        /** `localId`. */
        val sessionId: String?,
        val sdkSessionId: String? = null,
        val provider: String? = null,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "agent_turn_started" }

    /** The matching end of [AgentTurnStarted]: [status] `ok` | `error` | `interrupted` (a Stop: never notified). */
    data class AgentTurnFinished(
        /** `localId`. */
        val sessionId: String?,
        val sdkSessionId: String? = null,
        val provider: String? = null,
        /** The session's title when the server knows it. */
        val title: String? = null,
        val status: String = STATUS_OK,
        /** One line of the turn's final assistant text (≤ 200 chars). */
        val preview: String? = null,
        /** Failure detail when [status] is `error`. */
        val error: String? = null,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame {
        override val type get() = "agent_turn_finished"

        companion object {
            const val STATUS_OK = "ok"
            const val STATUS_ERROR = "error"
            const val STATUS_INTERRUPTED = "interrupted"
        }
    }

    /**
     * Spec 12 §6.11a: the orchestrator's `switch_conversation` stopped the live orchestrator
     * ([fromSessionId], its old `localId`) and asks THIS socket to resume [sdkSessionId] (a past
     * orchestrator jsonl id) and, when [voice], to start voice on it. Sent to one socket only;
     * handled at the channel level (SW-1), never by the conversation reducer.
     */
    data class OrchestratorSwitch(
        val sdkSessionId: String?,
        val title: String? = null,
        val voice: Boolean = false,
        val fromSessionId: String? = null,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "orchestrator_switch" }

    /**
     * Spec 12 §9.3: files under `context/public/` changed (the backend's content watcher, pushed to
     * every orchestrator WS like the pool watcher events). [visualizations] are list paths
     * (`GET /api/visualizations`) whose page or assets changed; [files] the raw changed paths.
     */
    data class VisualizationChanged(
        val visualizations: List<ContentChange> = emptyList(),
        val files: List<ContentChange> = emptyList(),
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "visualization_changed" }

    /** Spec 12 §9.3: markdown under the memory tree changed (paths as in `GET /api/memory/tree`). */
    data class MemoryChanged(
        val changes: List<ContentChange> = emptyList(),
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "memory_changed" }

    /** Legacy side effect of `POST /api/orchestrator/audio`; nothing consumes it. */
    data class AudioUpload(
        val audio: String?,
        val format: String? = null,
        val text: String? = null,
        val sizeBytes: Long? = null,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "audio_upload" }

    /** Orchestrator app-level heartbeat; MUST be ignored (T-4). */
    data class Ping(
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "ping" }

    // ───────────── voice (orchestrator WS) ─────────────

    /** Mirrored provider event or a backend-synthesised one (`voice_status`, `voice_vad_state`, `voice_error`, …). */
    data class VoiceEvent(
        val event: JsonObject,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "voice_event" }

    /** High-rate PCM; routed to the audio engine, never through the conversation queue (L-3). */
    data class VoiceAudioOut(
        val audio: String,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "voice_audio_out" }

    /** OpenAI Realtime client event for the owner's data channel. */
    data class VoiceCommand(
        val command: JsonObject?,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "voice_command" }

    data class VoiceConnectionError(
        val detail: String?,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "voice_connection_error" }

    data class VoiceOwnerActive(
        val active: Boolean,
        /** Shared orchestrator id, identical on every device: never use it to decide ownership (G-29). */
        val ownerLocalId: String? = null,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "voice_owner_active" }

    data class VoiceEnding(
        val reason: String?,
        val sessionId: String? = null,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "voice_ending" }

    data class VoiceEnded(
        val reason: String?,
        val sessionId: String? = null,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "voice_ended" }

    /** Legacy alias of `voice_ended`. */
    data class VoiceStopped(
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame { override val type get() = "voice_stopped" }

    /** Any other `type` (incl. the serializer fallback `unknown`). Kept whole. */
    data class Unknown(
        override val type: String,
        val raw: JsonObject,
        override val seq: Long? = null,
        override val streamId: String? = null,
    ) : ServerFrame
}

/** One entry of a §9.3 change frame. [kind] is advisory (an atomic save or an rsync reads as a create). */
data class ContentChange(val path: String, val kind: Kind) {
    enum class Kind(val wire: String) {
        CREATED("created"), MODIFIED("modified"), DELETED("deleted");

        companion object {
            /** Anything unknown counts as a modification. */
            fun of(wire: String?): Kind = entries.firstOrNull { it.wire == wire } ?: MODIFIED
        }
    }
}
