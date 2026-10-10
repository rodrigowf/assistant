package com.assistant.core.protocol

import com.assistant.core.model.VoiceConfig
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.decodeFromJsonElement
import kotlinx.serialization.json.encodeToJsonElement

/**
 * Wire codec for both WebSockets (spec 12 §3.1).
 *
 * - Server frames are binary (UTF-8 JSON) but text frames are accepted too (T-1).
 * - A frame that is not a JSON object, or has no string `type`, decodes to `null`: the caller logs
 *   and drops it; it never reaches the reducer (T-3).
 * - Client frames are encoded as JSON text, always sent as text frames (T-2).
 */
object ProtocolCodec {
    private val json = Json { ignoreUnknownKeys = true }

    // ───────────────────────── server → client ─────────────────────────

    fun decodeServer(bytes: ByteArray): ServerFrame? = decodeServer(bytes.toString(Charsets.UTF_8))

    fun decodeServer(text: String): ServerFrame? {
        val element = try {
            json.parseToJsonElement(text)
        } catch (_: Exception) {
            return null
        }
        return decodeServer(element)
    }

    fun decodeServer(element: JsonElement): ServerFrame? {
        val o = element as? JsonObject ?: return null
        val type = o.str("type") ?: return null
        val seq = o.long("seq")
        val sid = o.str("stream_id")
        return when (type) {
            "session_started" -> ServerFrame.SessionStarted(
                sessionId = o.str("session_id"),
                contextWindow = o.long("context_window"),
                resumeState = o.obj("resume_state")?.let { rs ->
                    val s = rs.str("stream_id")
                    val n = rs.long("next_seq")
                    if (s != null && n != null) ResumeState(s, n) else null
                },
                replayOverflow = o.bool("replay_overflow") == true,
                jsonlId = o.str("jsonl_id"),
                voice = o.bool("voice"),
                modelInfo = o.obj("model_info")?.let { decodeDto<OrchestratorModelInfoDto>(it) },
                voiceInitiator = o.bool("voice_initiator"),
                voiceProvider = o.str("voice_provider"),
                voiceModel = o.str("voice_model"),
                voiceName = o.str("voice_name"),
                voiceTranscriptionLanguage = o.str("voice_transcription_language"),
                voiceRecordingEnabled = o.bool("voice_recording_enabled"),
                voiceSessionUpdate = o.obj("voice_session_update"),
                voiceConnectionInfo = o.obj("voice_connection_info")
                    ?.let { decodeDto<ConnectionInfoDto>(it) }?.toModel(),
                voiceConnectionError = o.str("voice_connection_error"),
                seq = seq, streamId = sid,
            )
            "status" -> ServerFrame.Status(o.str("status"), o.str("detail"), seq, sid)
            "session_stopped" -> ServerFrame.SessionStopped(seq, sid)
            "session_terminated" -> ServerFrame.SessionTerminated(
                o.str("reason"), o.str("detail"), o.str("sdk_session_id"), seq, sid,
            )
            "error" -> ServerFrame.Error(o.str("error"), o.str("detail"), seq, sid)
            "user_message" -> ServerFrame.UserMessage(
                o.str("text") ?: "", o.bool("queued") == true, o.str("source"), seq, sid,
            )
            "text_delta" -> ServerFrame.TextDelta(o.str("text") ?: "", seq, sid)
            "text_complete" -> ServerFrame.TextComplete(o.str("text") ?: "", seq, sid)
            "thinking_delta" -> ServerFrame.ThinkingDelta(o.str("text") ?: "", seq, sid)
            "thinking_complete" -> ServerFrame.ThinkingComplete(o.str("text") ?: "", seq, sid)
            "tool_use" -> ServerFrame.ToolUse(o.str("tool_use_id"), o.str("tool_name"), o.obj("tool_input"), seq, sid)
            "tool_result" -> ServerFrame.ToolResult(o.str("tool_use_id"), o.present("output"), o.bool("is_error"), seq, sid)
            "tool_executing" -> ServerFrame.ToolExecuting(o.str("tool_use_id"), o.str("tool_name"), seq, sid)
            "tool_progress" -> ServerFrame.ToolProgress(
                o.str("tool_use_id"), o.str("tool_name"), o.double("elapsed_seconds"), o.str("message"), seq, sid,
            )
            "turn_complete" -> ServerFrame.TurnComplete(
                cost = o.double("cost"),
                usage = o.obj("usage"),
                inputTokens = o.long("input_tokens"),
                outputTokens = o.long("output_tokens"),
                numTurns = o.int("num_turns"),
                sessionId = o.str("session_id"),
                isError = o.bool("is_error"),
                result = o.str("result"),
                seq = seq, streamId = sid,
            )
            "compact_complete" -> ServerFrame.CompactComplete(
                o.str("trigger"), o.str("summary"), o.long("tokens_before"), o.long("tokens_after"), seq, sid,
            )
            "session_stalled" -> ServerFrame.SessionStalled(
                o.double("elapsed_seconds"), o.str("last_tool_name"), o.str("last_tool_use_id"), seq, sid,
            )
            "permission_request" -> ServerFrame.PermissionRequest(
                o.str("request_id"), o.str("tool_name"), o.obj("tool_input"), seq, sid,
            )
            "permission_resolved" -> ServerFrame.PermissionResolved(
                o.str("request_id"), o.str("decision"), o.str("responder"), o.str("message"), seq, sid,
            )
            "model_changed" -> ServerFrame.ModelChanged(o.obj("model_info")?.let { decodeDto<OrchestratorModelInfoDto>(it) }, seq, sid)
            "model_info" -> ServerFrame.ModelInfo(o.obj("model_info")?.let { decodeDto<OrchestratorModelInfoDto>(it) }, seq, sid)
            "models_list" -> ServerFrame.ModelsList(
                o.arr("models")?.mapNotNull { (it as? JsonObject)?.let { m -> decodeDto<ModelInfoDto>(m) } } ?: emptyList(),
                seq, sid,
            )
            "nested_session_event" -> ServerFrame.NestedSessionEvent(
                o.str("session_id"), o.str("event_type"), o.obj("event_data"), seq, sid,
            )
            "agent_session_opened" -> ServerFrame.AgentSessionOpened(
                o.str("session_id"), o.str("sdk_session_id"), o.bool("is_orchestrator") == true, seq, sid,
            )
            "agent_session_closed" -> ServerFrame.AgentSessionClosed(
                o.str("session_id"), o.bool("is_orchestrator") == true, seq, sid,
            )
            "orchestrator_switch" -> ServerFrame.OrchestratorSwitch(
                o.str("sdk_session_id"), o.str("title"), o.bool("voice") == true, o.str("from_session_id"), seq, sid,
            )
            "agent_turn_started" -> ServerFrame.AgentTurnStarted(o.str("session_id"), o.str("sdk_session_id"), o.str("provider"), seq, sid)
            "agent_turn_finished" -> ServerFrame.AgentTurnFinished(
                o.str("session_id"), o.str("sdk_session_id"), o.str("provider"), o.str("title"),
                o.str("status") ?: ServerFrame.AgentTurnFinished.STATUS_OK, o.str("preview"), o.str("error"), seq, sid,
            )
            "visualization_changed" -> ServerFrame.VisualizationChanged(changes(o.arr("visualizations")), changes(o.arr("files")), seq, sid)
            "memory_changed" -> ServerFrame.MemoryChanged(changes(o.arr("changes")), seq, sid)
            "audio_upload" -> ServerFrame.AudioUpload(o.str("audio"), o.str("format"), o.str("text"), o.long("size_bytes"), seq, sid)
            "ping" -> ServerFrame.Ping(seq, sid)
            "voice_event" -> {
                val event = o.obj("event") ?: return ServerFrame.Unknown(type, o, seq, sid)
                ServerFrame.VoiceEvent(event, seq, sid)
            }
            "voice_audio_out" -> ServerFrame.VoiceAudioOut(o.str("audio") ?: "", seq, sid)
            "voice_command" -> ServerFrame.VoiceCommand(o.obj("command"), seq, sid)
            "voice_connection_error" -> ServerFrame.VoiceConnectionError(o.str("detail"), seq, sid)
            "voice_owner_active" -> ServerFrame.VoiceOwnerActive(o.bool("active") == true, o.str("owner_local_id"), seq, sid)
            "voice_ending" -> ServerFrame.VoiceEnding(o.str("reason"), o.str("session_id"), seq, sid)
            "voice_ended" -> ServerFrame.VoiceEnded(o.str("reason"), o.str("session_id"), seq, sid)
            "voice_stopped" -> ServerFrame.VoiceStopped(seq, sid)
            else -> ServerFrame.Unknown(type, o, seq, sid)
        }
    }

    /** §9.3 change entries; malformed ones (no string `path`) are dropped. */
    private fun changes(list: JsonArray?): List<ContentChange> =
        list?.mapNotNull { e ->
            val c = e as? JsonObject ?: return@mapNotNull null
            val path = c.str("path")?.takeIf { it.isNotEmpty() } ?: return@mapNotNull null
            ContentChange(path, ContentChange.Kind.of(c.str("kind")))
        } ?: emptyList()

    private fun encodeChanges(list: List<ContentChange>): JsonArray =
        JsonArray(list.map { jsonObject { put("path", it.path); put("kind", it.kind.wire) } })

    /**
     * Canonical JSON of a server frame (used by fakes and the round-trip tests). Fields that are
     * `null` are omitted; `decodeServer(encodeServer(f)) == f` for every frame.
     */
    fun encodeServer(frame: ServerFrame): JsonObject {
        if (frame is ServerFrame.Unknown) return frame.raw
        return jsonObject {
            put("type", frame.type)
            when (frame) {
                is ServerFrame.SessionStarted -> {
                    put("session_id", frame.sessionId)
                    put("context_window", frame.contextWindow)
                    frame.resumeState?.let {
                        put("resume_state", jsonObject { put("stream_id", it.streamId); put("next_seq", it.nextSeq) })
                    }
                    if (frame.replayOverflow) put("replay_overflow", true)
                    put("jsonl_id", frame.jsonlId)
                    put("voice", frame.voice)
                    put("model_info", frame.modelInfo?.let { encodeDto(it) })
                    put("voice_initiator", frame.voiceInitiator)
                    put("voice_provider", frame.voiceProvider)
                    put("voice_model", frame.voiceModel)
                    put("voice_name", frame.voiceName)
                    put("voice_transcription_language", frame.voiceTranscriptionLanguage)
                    put("voice_recording_enabled", frame.voiceRecordingEnabled)
                    put("voice_session_update", frame.voiceSessionUpdate)
                    put("voice_connection_info", frame.voiceConnectionInfo?.toJson())
                    put("voice_connection_error", frame.voiceConnectionError)
                }
                is ServerFrame.Status -> { put("status", frame.status); put("detail", frame.detail) }
                is ServerFrame.SessionStopped, is ServerFrame.Ping, is ServerFrame.VoiceStopped -> Unit
                is ServerFrame.SessionTerminated -> {
                    put("reason", frame.reason); put("detail", frame.detail); put("sdk_session_id", frame.sdkSessionId)
                }
                is ServerFrame.Error -> { put("error", frame.error); put("detail", frame.detail) }
                is ServerFrame.UserMessage -> {
                    put("text", frame.text)
                    if (frame.queued) put("queued", true)
                    put("source", frame.source)
                }
                is ServerFrame.TextDelta -> put("text", frame.text)
                is ServerFrame.TextComplete -> put("text", frame.text)
                is ServerFrame.ThinkingDelta -> put("text", frame.text)
                is ServerFrame.ThinkingComplete -> put("text", frame.text)
                is ServerFrame.ToolUse -> {
                    put("tool_use_id", frame.toolUseId); put("tool_name", frame.toolName); put("tool_input", frame.toolInput)
                }
                is ServerFrame.ToolResult -> {
                    put("tool_use_id", frame.toolUseId); put("output", frame.output); put("is_error", frame.isError)
                }
                is ServerFrame.ToolExecuting -> { put("tool_use_id", frame.toolUseId); put("tool_name", frame.toolName) }
                is ServerFrame.ToolProgress -> {
                    put("tool_use_id", frame.toolUseId); put("tool_name", frame.toolName)
                    put("elapsed_seconds", frame.elapsedSeconds); put("message", frame.message)
                }
                is ServerFrame.TurnComplete -> {
                    put("cost", frame.cost); put("usage", frame.usage)
                    put("input_tokens", frame.inputTokens); put("output_tokens", frame.outputTokens)
                    put("num_turns", frame.numTurns); put("session_id", frame.sessionId)
                    put("is_error", frame.isError); put("result", frame.result)
                }
                is ServerFrame.CompactComplete -> {
                    put("trigger", frame.trigger); put("summary", frame.summary)
                    put("tokens_before", frame.tokensBefore); put("tokens_after", frame.tokensAfter)
                }
                is ServerFrame.SessionStalled -> {
                    put("elapsed_seconds", frame.elapsedSeconds)
                    put("last_tool_name", frame.lastToolName); put("last_tool_use_id", frame.lastToolUseId)
                }
                is ServerFrame.PermissionRequest -> {
                    put("request_id", frame.requestId); put("tool_name", frame.toolName); put("tool_input", frame.toolInput)
                }
                is ServerFrame.PermissionResolved -> {
                    put("request_id", frame.requestId); put("decision", frame.decision)
                    put("responder", frame.responder); put("message", frame.message)
                }
                is ServerFrame.ModelChanged -> put("model_info", frame.modelInfo?.let { encodeDto(it) })
                is ServerFrame.ModelInfo -> put("model_info", frame.modelInfo?.let { encodeDto(it) })
                is ServerFrame.ModelsList -> put("models", JsonArray(frame.models.map { encodeDto(it) }))
                is ServerFrame.NestedSessionEvent -> {
                    put("session_id", frame.sessionId); put("event_type", frame.eventType); put("event_data", frame.eventData)
                }
                is ServerFrame.AgentSessionOpened -> {
                    put("session_id", frame.sessionId); put("sdk_session_id", frame.sdkSessionId)
                    put("is_orchestrator", frame.isOrchestrator)
                }
                is ServerFrame.AgentSessionClosed -> {
                    put("session_id", frame.sessionId); put("is_orchestrator", frame.isOrchestrator)
                }
                is ServerFrame.OrchestratorSwitch -> {
                    put("sdk_session_id", frame.sdkSessionId); put("title", frame.title)
                    put("voice", frame.voice); put("from_session_id", frame.fromSessionId)
                }
                is ServerFrame.AgentTurnStarted -> {
                    put("session_id", frame.sessionId); put("sdk_session_id", frame.sdkSessionId); put("provider", frame.provider)
                }
                is ServerFrame.AgentTurnFinished -> {
                    put("session_id", frame.sessionId); put("sdk_session_id", frame.sdkSessionId); put("provider", frame.provider)
                    put("title", frame.title); put("status", frame.status); put("preview", frame.preview); put("error", frame.error)
                }
                is ServerFrame.VisualizationChanged -> {
                    put("visualizations", encodeChanges(frame.visualizations)); put("files", encodeChanges(frame.files))
                }
                is ServerFrame.MemoryChanged -> put("changes", encodeChanges(frame.changes))
                is ServerFrame.AudioUpload -> {
                    put("audio", frame.audio); put("format", frame.format); put("text", frame.text); put("size_bytes", frame.sizeBytes)
                }
                is ServerFrame.VoiceEvent -> put("event", frame.event)
                is ServerFrame.VoiceAudioOut -> put("audio", frame.audio)
                is ServerFrame.VoiceCommand -> put("command", frame.command)
                is ServerFrame.VoiceConnectionError -> put("detail", frame.detail)
                is ServerFrame.VoiceOwnerActive -> { put("active", frame.active); put("owner_local_id", frame.ownerLocalId) }
                is ServerFrame.VoiceEnding -> { put("reason", frame.reason); put("session_id", frame.sessionId) }
                is ServerFrame.VoiceEnded -> { put("reason", frame.reason); put("session_id", frame.sessionId) }
                is ServerFrame.Unknown -> Unit
            }
            put("seq", frame.seq)
            put("stream_id", frame.streamId)
        }
    }

    // ───────────────────────── client → server ─────────────────────────

    /** The JSON text to send as a TEXT frame. */
    fun encodeClient(frame: ClientFrame): String = encodeClientJson(frame).toString()

    fun encodeClientJson(frame: ClientFrame): JsonObject = jsonObject {
        put("type", frame.type)
        when (frame) {
            is ClientFrame.Start -> {
                put("local_id", frame.localId)
                put("resume_sdk_id", frame.resumeSdkId)
                frame.resumeFrom?.let {
                    put("resume_from", jsonObject { put("stream_id", it.streamId); put("seq", it.seq) })
                }
                put("fork", frame.fork)
                put("mcp_servers", frame.mcpServers)
                put("reattach", frame.reattach)
            }
            is ClientFrame.VoiceStart -> {
                put("local_id", frame.localId)
                put("resume_sdk_id", frame.resumeSdkId)
                put("voice_provider", frame.voice.provider)
                put("voice_model", frame.voice.model)
                put("voice_name", frame.voice.voice)
                put("voice_transcription_language", frame.voice.transcriptionLanguage)
                put("voice_endpoint", frame.voice.endpoint)
                put("reattach", frame.reattach)
            }
            is ClientFrame.Send -> put("text", frame.text)
            is ClientFrame.InjectText -> put("text", frame.text)
            is ClientFrame.SendAudio -> { put("audio", frame.audio); put("format", frame.format); put("text", frame.text) }
            is ClientFrame.Command -> put("text", frame.text)
            is ClientFrame.PermissionResponse -> {
                put("request_id", frame.requestId); put("decision", frame.decision)
                put("message", frame.message); put("session_id", frame.sessionId)
            }
            is ClientFrame.SetModel -> put("model", frame.model)
            is ClientFrame.VoiceEvent -> put("event", frame.event)
            is ClientFrame.VoiceAudioIn -> put("audio", frame.audio)
            is ClientFrame.VoiceRecordingChunk -> { put("channel", frame.channel); put("audio", frame.audio) }
            ClientFrame.Interrupt, ClientFrame.Compact, ClientFrame.Stop, ClientFrame.VoiceStop,
            ClientFrame.GetModel, ClientFrame.GetModels, ClientFrame.VoiceRecordingEnd -> Unit
        }
    }

    /** Inverse of [encodeClient], for fake servers and tests. `null` for unknown/malformed frames. */
    fun decodeClient(text: String): ClientFrame? {
        val o = try {
            json.parseToJsonElement(text) as? JsonObject
        } catch (_: Exception) {
            null
        } ?: return null
        return when (o.str("type")) {
            "start" -> ClientFrame.Start(
                localId = o.str("local_id") ?: return null,
                resumeSdkId = o.str("resume_sdk_id") ?: o.str("session_id"),
                resumeFrom = o.obj("resume_from")?.let { rf ->
                    val s = rf.str("stream_id"); val n = rf.long("seq")
                    if (s != null && n != null) ResumeCursor(s, n) else null
                },
                fork = o.bool("fork"),
                mcpServers = o.obj("mcp_servers"),
                reattach = o.bool("reattach"),
            )
            "voice_start" -> ClientFrame.VoiceStart(
                localId = o.str("local_id") ?: return null,
                resumeSdkId = o.str("resume_sdk_id") ?: o.str("session_id"),
                voice = VoiceConfig(
                    provider = o.str("voice_provider"),
                    model = o.str("voice_model"),
                    voice = o.str("voice_name"),
                    transcriptionLanguage = o.str("voice_transcription_language"),
                    endpoint = o.str("voice_endpoint"),
                ),
                reattach = o.bool("reattach"),
            )
            "send" -> ClientFrame.Send(o.str("text") ?: "")
            "inject_text" -> ClientFrame.InjectText(o.str("text") ?: "")
            "send_audio" -> ClientFrame.SendAudio(o.str("audio") ?: "", o.str("format") ?: "webm", o.str("text"))
            "interrupt" -> ClientFrame.Interrupt
            "command" -> ClientFrame.Command(o.str("text") ?: "")
            "compact" -> ClientFrame.Compact
            "permission_response" -> ClientFrame.PermissionResponse(
                o.str("request_id") ?: return null, o.str("decision") ?: return null, o.str("message"), o.str("session_id"),
            )
            "stop" -> ClientFrame.Stop
            "voice_stop" -> ClientFrame.VoiceStop
            "set_model" -> ClientFrame.SetModel(o.str("model") ?: return null)
            "get_model" -> ClientFrame.GetModel
            "get_models" -> ClientFrame.GetModels
            "voice_event" -> ClientFrame.VoiceEvent(o.obj("event") ?: return null)
            "voice_audio_in" -> ClientFrame.VoiceAudioIn(o.str("audio") ?: "")
            "voice_recording_chunk" -> ClientFrame.VoiceRecordingChunk(o.str("channel") ?: "user", o.str("audio") ?: "")
            "voice_recording_end" -> ClientFrame.VoiceRecordingEnd
            else -> null
        }
    }

    // ───────────────────────── helpers ─────────────────────────

    private inline fun <reified T> decodeDto(o: JsonObject): T? =
        try {
            RestJson.decodeFromJsonElement<T>(o)
        } catch (_: Exception) {
            null
        }

    private inline fun <reified T> encodeDto(value: T): JsonElement = RestJson.encodeToJsonElement(value)
}
