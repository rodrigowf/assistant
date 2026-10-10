package com.assistant.core.protocol

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject

/**
 * REST bodies of inv01 §3 (requests and responses), field names exactly as the backend.
 * Decode with [RestJson]: unknown keys are ignored, wrong-typed optional fields fall back to
 * their defaults. On encode, `null` fields are omitted (so partial `PUT`s only carry the set fields)
 * and non-null defaults are written (so a re-encoded response loses nothing).
 */
val RestJson: Json = Json {
    ignoreUnknownKeys = true
    coerceInputValues = true
    explicitNulls = false
    encodeDefaults = true
}

// ───────────── sessions (inv01 §3.2) ─────────────

@Serializable
data class SessionInfoDto(
    @SerialName("session_id") val sessionId: String,
    @SerialName("started_at") val startedAt: String? = null,
    @SerialName("last_activity") val lastActivity: String? = null,
    val title: String = "",
    @SerialName("message_count") val messageCount: Int = 0,
    @SerialName("is_orchestrator") val isOrchestrator: Boolean = false,
    val provider: String? = null,
    @SerialName("local_id") val localId: String? = null,
)

@Serializable
data class PoolSessionDto(
    @SerialName("local_id") val localId: String,
    @SerialName("sdk_session_id") val sdkSessionId: String? = null,
    val status: String? = null,
    val cost: Double = 0.0,
    val turns: Int = 0,
    val title: String? = null,
    @SerialName("is_orchestrator") val isOrchestrator: Boolean = false,
)

/** `ContentBlockResponse`. `type`: `text` | `thinking` (backend O-2) | `tool_use` | `tool_result`. */
@Serializable
data class ContentBlockDto(
    val type: String,
    val text: String? = null,
    @SerialName("tool_use_id") val toolUseId: String? = null,
    @SerialName("tool_name") val toolName: String? = null,
    @SerialName("tool_input") val toolInput: JsonObject? = null,
    /** Always a string from the backend; kept as JSON so a non-string never fails the page. */
    val output: JsonElement? = null,
    @SerialName("is_error") val isError: Boolean = false,
)

/** `MessagePreviewResponse`. No ids: the only identity is the absolute index (inv01 §6.2). */
@Serializable
data class MessagePreviewDto(
    val role: String,
    val text: String = "",
    val blocks: List<ContentBlockDto> = emptyList(),
    val timestamp: String? = null,
)

/** `GET /api/sessions/{sdkId}/messages`. */
@Serializable
data class PaginatedMessagesDto(
    val messages: List<MessagePreviewDto> = emptyList(),
    @SerialName("total_count") val totalCount: Int = 0,
    @SerialName("has_more") val hasMore: Boolean = false,
    @SerialName("start_index") val startIndex: Int = 0,
)

/** `GET /api/sessions/{id}` (prefer `/messages`; inv01 §3.2). */
@Serializable
data class SessionDetailDto(
    @SerialName("session_id") val sessionId: String,
    @SerialName("started_at") val startedAt: String? = null,
    @SerialName("last_activity") val lastActivity: String? = null,
    val title: String = "",
    @SerialName("message_count") val messageCount: Int = 0,
    @SerialName("is_orchestrator") val isOrchestrator: Boolean = false,
    val provider: String? = null,
    @SerialName("local_id") val localId: String? = null,
    val messages: List<MessagePreviewDto> = emptyList(),
)

/** `GET/PUT /api/sessions/{sdkId}/config`. On PUT a JSON `null` means "inherit"; build PUT bodies with `SessionConfig.toPutBody`. */
@Serializable
data class SessionConfigDto(
    @SerialName("working_directory") val workingDirectory: String? = null,
    @SerialName("enabled_mcps") val enabledMcps: List<String>? = null,
    @SerialName("chrome_extension") val chromeExtension: Boolean? = null,
    val provider: String? = null,
    @SerialName("harness_model") val harnessModel: String? = null,
    /** `{key: value | null}` overlay; read leniently (see `harnessOptionsOverlay`). */
    @SerialName("harness_options") val harnessOptions: JsonElement? = null,
)

@Serializable
data class RenameRequest(val title: String)

/** `truncate` / `fork` body. */
@Serializable
data class DropLastNRequest(@SerialName("drop_last_n") val dropLastN: Int)

/** `fork` / `duplicate` / `truncate` response. */
@Serializable
data class SessionIdResponse(@SerialName("session_id") val sessionId: String)

@Serializable
data class InjectRequest(
    val text: String,
    @SerialName("local_id") val localId: String? = null,
    @SerialName("sdk_session_id") val sdkSessionId: String? = null,
)

@Serializable
data class InjectResponse(val ok: Boolean = false, @SerialName("local_id") val localId: String? = null)

// ───────────── auth (inv01 §3.1) ─────────────

@Serializable
data class AuthStatusDto(
    val authenticated: Boolean = false,
    @SerialName("auth_url") val authUrl: String? = null,
    val headless: Boolean = false,
)

@Serializable
data class CredentialsRequest(@SerialName("credentials_json") val credentialsJson: String)

// ───────────── visualizations (inv01 §3.3, backend BX-2) ─────────────

@Serializable
data class VisualizationInfoDto(
    val path: String,
    val url: String,
    val title: String = "",
    val created: String? = null,
    val modified: String? = null,
    val size: Long = 0,
)

@Serializable
data class VisualizationRenameRequest(val path: String, val title: String)

/** `GET /api/visualizations/cast`. */
@Serializable
data class CastProbeDto(val available: Boolean = false, val reason: String? = null)

@Serializable
data class CastRequest(val path: String)

@Serializable
data class CastResponse(val ok: Boolean = false, val message: String? = null)

// ───────────── memory (inv01 §3.4) ─────────────

@Serializable
data class MemoryNodeDto(
    val name: String,
    val path: String,
    @SerialName("is_dir") val isDir: Boolean = false,
    val children: List<MemoryNodeDto>? = null,
)

// ───────────── uploads (inv01 §3.5) ─────────────

@Serializable
data class UploadResultDto(
    val filename: String,
    val path: String,
    val url: String,
    val size: Long = 0,
    @SerialName("content_type") val contentType: String = "application/octet-stream",
)

// ───────────── global config (inv01 §3.6) ─────────────

@Serializable
data class WorkingDirectoryDto(
    val id: String = "",
    val path: String,
    val label: String? = null,
    @SerialName("ssh_host") val sshHost: String? = null,
    @SerialName("ssh_user") val sshUser: String? = null,
    @SerialName("ssh_key") val sshKey: String? = null,
    @SerialName("claude_config_dir") val claudeConfigDir: String? = null,
)

@Serializable
data class ConfigDto(
    @SerialName("working_directory") val workingDirectory: String? = null,
    @SerialName("working_directory_history") val workingDirectoryHistory: List<WorkingDirectoryDto> = emptyList(),
    @SerialName("enabled_mcps") val enabledMcps: List<String> = emptyList(),
    @SerialName("chrome_extension") val chromeExtension: Boolean = false,
    val provider: String? = null,
    @SerialName("default_model") val defaultModel: String? = null,
    @SerialName("summarizer_model") val summarizerModel: String? = null,
    @SerialName("harness_model") val harnessModel: Map<String, String> = emptyMap(),
    @SerialName("default_voice_provider") val defaultVoiceProvider: String? = null,
    @SerialName("default_voice_model") val defaultVoiceModel: String? = null,
    @SerialName("default_voice_name") val defaultVoiceName: String? = null,
    @SerialName("default_voice_transcription_language") val defaultVoiceTranscriptionLanguage: String? = null,
    @SerialName("default_voice_endpoint") val defaultVoiceEndpoint: String? = null,
    @SerialName("voice_recording_enabled") val voiceRecordingEnabled: Boolean = false,
    @SerialName("voice_vad_threshold") val voiceVadThreshold: Double? = null,
    @SerialName("voice_vad_min_silence_ms") val voiceVadMinSilenceMs: Int? = null,
    @SerialName("voice_mic_gain") val voiceMicGain: Double? = null,
    @SerialName("default_audio_model") val defaultAudioModel: String? = null,
    /** `{provider: {key: value}}`; read leniently (a wrong-typed value is dropped, never fails the config). */
    @SerialName("harness_options") val harnessOptions: JsonElement? = null,
)

/** Partial `PUT /api/config` (`ConfigUpdate`). Null fields are not sent. */
@Serializable
data class ConfigUpdateDto(
    @SerialName("working_directory") val workingDirectory: String? = null,
    @SerialName("working_directory_history") val workingDirectoryHistory: List<WorkingDirectoryDto>? = null,
    @SerialName("enabled_mcps") val enabledMcps: List<String>? = null,
    @SerialName("chrome_extension") val chromeExtension: Boolean? = null,
    val provider: String? = null,
    @SerialName("default_model") val defaultModel: String? = null,
    @SerialName("summarizer_model") val summarizerModel: String? = null,
    @SerialName("harness_model") val harnessModel: Map<String, String>? = null,
    @SerialName("default_voice_provider") val defaultVoiceProvider: String? = null,
    @SerialName("default_voice_model") val defaultVoiceModel: String? = null,
    @SerialName("default_voice_name") val defaultVoiceName: String? = null,
    @SerialName("default_voice_transcription_language") val defaultVoiceTranscriptionLanguage: String? = null,
    @SerialName("default_voice_endpoint") val defaultVoiceEndpoint: String? = null,
    @SerialName("voice_recording_enabled") val voiceRecordingEnabled: Boolean? = null,
    @SerialName("voice_vad_threshold") val voiceVadThreshold: Double? = null,
    @SerialName("voice_vad_min_silence_ms") val voiceVadMinSilenceMs: Int? = null,
    @SerialName("voice_mic_gain") val voiceMicGain: Double? = null,
    @SerialName("default_audio_model") val defaultAudioModel: String? = null,
    /** `{provider: {key: value | null}}`: merged per key, JSON `null` deletes the key (= CLI default). */
    @SerialName("harness_options") val harnessOptions: JsonObject? = null,
)

@Serializable
data class OpenAiKeyDto(@SerialName("api_key") val apiKey: String)

@Serializable
data class HarnessProviderDto(val id: String, val label: String = "", val description: String = "")

@Serializable
data class HarnessProvidersDto(val providers: List<HarnessProviderDto> = emptyList())

@Serializable
data class QwenModelDto(
    val id: String,
    @SerialName("display_name") val displayName: String = "",
    val provider: String = "",
    @SerialName("base_url") val baseUrl: String? = null,
    @SerialName("context_window") val contextWindow: Long? = null,
    @SerialName("supports_vision") val supportsVision: Boolean = false,
    @SerialName("supports_video") val supportsVideo: Boolean = false,
    @SerialName("supports_thinking") val supportsThinking: Boolean = false,
)

@Serializable
data class QwenModelsDto(val models: List<QwenModelDto> = emptyList())

// ───────────── harness catalogs (spec 12 §6.14, §8.1; backend/manager/harness_catalog.py) ─────────────

@Serializable
data class HarnessChoiceDto(
    val value: String,
    val label: String = "",
    val description: String? = null,
    val models: List<String>? = null,
)

/** `kind`: `select` | `toggle` | `number`; `default` is a string, boolean or number. */
@Serializable
data class HarnessOptionDto(
    val key: String,
    val label: String = "",
    val kind: String = "select",
    val choices: List<HarnessChoiceDto> = emptyList(),
    val default: JsonElement? = null,
    val help: String? = null,
    val models: List<String>? = null,
    val min: Double? = null,
    val max: Double? = null,
    val step: Double? = null,
    // Presentation hints (all optional, read leniently in `HarnessMappers`): a malformed hint is
    // dropped and the UI infers the control, never failing the whole catalog.
    val control: JsonElement? = null,
    val ordered: JsonElement? = null,
    val unit: JsonElement? = null,
    val scale: JsonElement? = null,
    val presets: JsonElement? = null,
    @SerialName("custom_min") val customMin: JsonElement? = null,
    val requires: JsonElement? = null,
)

@Serializable
data class HarnessCatalogModelDto(
    val id: String,
    val label: String = "",
    val source: String = "builtin",
    val description: String? = null,
    @SerialName("context_window") val contextWindow: Long? = null,
    @SerialName("supports_thinking") val supportsThinking: Boolean? = null,
    @SerialName("supports_vision") val supportsVision: Boolean? = null,
    val efforts: List<String>? = null,
    @SerialName("default_effort") val defaultEffort: String? = null,
)

@Serializable
data class HarnessCatalogDto(
    val provider: String = "",
    val models: List<HarnessCatalogModelDto> = emptyList(),
    val options: List<HarnessOptionDto> = emptyList(),
    @SerialName("default_model") val defaultModel: String? = null,
    @SerialName("allow_custom_model") val allowCustomModel: Boolean = true,
    val warnings: List<String> = emptyList(),
)

@Serializable
data class HarnessInfoDto(
    val id: String,
    val label: String = "",
    val description: String? = null,
    val catalog: HarnessCatalogDto? = null,
)

/** `GET /api/config/harnesses[?refresh=true]`. */
@Serializable
data class HarnessesDto(val harnesses: List<HarnessInfoDto> = emptyList())

@Serializable
data class VoiceOptionDto(val id: String, val label: String = "", val description: String = "")

/** `VoiceModelEntry` (inv01 §7.1) and the Google discovery entry (§3.6). */
@Serializable
data class VoiceModelEntryDto(
    val id: String,
    val label: String = "",
    val voice: String? = null,
    val voices: List<VoiceOptionDto> = emptyList(),
    @SerialName("transcription_languages") val transcriptionLanguages: List<VoiceOptionDto> = emptyList(),
    @SerialName("default_transcription_language") val defaultTranscriptionLanguage: String? = null,
    val default: Boolean = false,
    val description: String? = null,
)

@Serializable
data class GoogleVoiceModelsDto(val models: List<VoiceModelEntryDto> = emptyList())

// ───────────── skills / agents / MCP (inv01 §3.7) ─────────────

@Serializable
data class SkillDto(val name: String, val description: String = "", val dir: String? = null)

@Serializable
data class SkillsDto(val skills: List<SkillDto> = emptyList())

@Serializable
data class AgentDto(val name: String, val description: String = "", val file: String? = null)

@Serializable
data class AgentsDto(val agents: List<AgentDto> = emptyList())

@Serializable
data class McpServersDto(
    val servers: Map<String, JsonObject> = emptyMap(),
    @SerialName("project_dir") val projectDir: String? = null,
)

@Serializable
data class McpServerDto(val name: String, val config: JsonObject = JsonObject(emptyMap()))

// ───────────── orchestrator models and voice (inv01 §3.8, §7.2) ─────────────

/** `ModelInfo` (orchestrator/config.py:42-54). */
@Serializable
data class ModelInfoDto(
    val provider: String = "",
    @SerialName("model_id") val modelId: String,
    @SerialName("display_name") val displayName: String = "",
    @SerialName("supports_audio") val supportsAudio: Boolean = false,
    @SerialName("supports_vision") val supportsVision: Boolean = false,
    @SerialName("supports_tools") val supportsTools: Boolean = false,
    @SerialName("max_tokens") val maxTokens: Long? = null,
    @SerialName("context_window") val contextWindow: Long? = null,
)

/** `OrchestratorModelInfo` (orchestrator/config.py:278-286), carried by `session_started`/`model_*`. */
@Serializable
data class OrchestratorModelInfoDto(
    val model: String? = null,
    val provider: String? = null,
    @SerialName("max_tokens") val maxTokens: Long? = null,
    @SerialName("supports_audio") val supportsAudio: Boolean = false,
    @SerialName("model_info") val modelInfo: ModelInfoDto? = null,
)

@Serializable
data class OrchestratorModelsDto(
    val models: List<ModelInfoDto> = emptyList(),
    @SerialName("audio_capable_models") val audioCapableModels: List<String> = emptyList(),
    @SerialName("default_model") val defaultModel: String? = null,
    /** The model the server uses for voice messages (Settings value or its fallback). */
    @SerialName("default_audio_model") val defaultAudioModel: String? = null,
)

@Serializable
data class AudioModelsDto(val models: List<ModelInfoDto> = emptyList())

@Serializable
data class VoiceModelsDto(
    val providers: Map<String, List<VoiceModelEntryDto>> = emptyMap(),
    @SerialName("default_provider") val defaultProvider: String? = null,
    @SerialName("default_model") val defaultModel: String? = null,
)

@Serializable
data class AudioFormatDto(
    @SerialName("sample_rate") val sampleRate: Int = 24_000,
    val encoding: String? = null,
)

@Serializable
data class ConnectionInfoDto(
    @SerialName("connection_type") val connectionType: String? = null,
    val endpoint: String? = null,
    @SerialName("ephemeral_token") val ephemeralToken: String? = null,
    @SerialName("expires_at") val expiresAt: Long? = null,
    @SerialName("audio_in_format") val audioInFormat: AudioFormatDto? = null,
    @SerialName("audio_out_format") val audioOutFormat: AudioFormatDto? = null,
    val model: String? = null,
    val voice: String? = null,
    @SerialName("audio_relay") val audioRelay: String? = null,
)

@Serializable
data class ClientSecretDto(val value: String? = null, @SerialName("expires_at") val expiresAt: Long? = null)

/** `POST /api/orchestrator/voice/session` (fallback only, V-3). */
@Serializable
data class VoiceSessionDto(
    @SerialName("connection_info") val connectionInfo: ConnectionInfoDto? = null,
    @SerialName("client_secret") val clientSecret: ClientSecretDto? = null,
    val model: String? = null,
    val voice: String? = null,
)

/** Legacy `POST /api/orchestrator/audio` response. */
@Serializable
data class AudioQueuedDto(
    val status: String? = null,
    @SerialName("audio_format") val audioFormat: String? = null,
    @SerialName("size_bytes") val sizeBytes: Long? = null,
)

// ───────────── debug (inv01 §3.9) and errors ─────────────

@Serializable
data class DebugLogRequest(val level: String, val msg: String, val ts: String? = null)

/** `{"detail": "<string>"}` (HTTPException) or `{"detail": [ … ]}` (422 validation). */
@Serializable
data class ErrorDetailDto(val detail: JsonElement? = null) {
    /** Human text for CFG-2 / W-6.2: the string detail, or the first validation `msg`. */
    fun message(): String? = when (val d = detail) {
        is kotlinx.serialization.json.JsonPrimitive -> if (d.isString) d.content else d.toString()
        is kotlinx.serialization.json.JsonArray -> d.firstNotNullOfOrNull { item ->
            ((item as? JsonObject)?.get("msg") as? kotlinx.serialization.json.JsonPrimitive)?.content
        }
        else -> null
    }
}

// ───────────── Settings → Accounts (`/api/accounts`, `/api/env`; spec 12 §8.1) ─────────────

@Serializable
data class AccountsDto(val services: List<AccountServiceDto> = emptyList(), @SerialName("env_path") val envPath: String = "")

@Serializable
data class AccountServiceDto(
    val id: String,
    val label: String = "",
    /** `harness` | `api` | `other`. */
    val group: String = "other",
    val description: String = "",
    /** `signed_in` | `signed_out` | `expired` | `unavailable` | `unknown`. */
    val state: String = "unknown",
    val method: String? = null,
    val account: String? = null,
    val plan: String? = null,
    @SerialName("expires_at") val expiresAt: String? = null,
    val detail: String? = null,
    val warnings: List<String> = emptyList(),
    val methods: List<AccountMethodDto> = emptyList(),
    @SerialName("used_by") val usedBy: List<String> = emptyList(),
    val flow: LoginFlowDto? = null,
    val verified: AccountVerificationDto? = null,
    @SerialName("can_verify") val canVerify: Boolean = false,
)

@Serializable
data class AccountMethodDto(
    val id: String,
    /** `link` | `credentials` | `env` | `signout`. */
    val kind: String,
    val label: String = "",
    val description: String = "",
    val recommended: Boolean = false,
    val active: Boolean = false,
    val available: Boolean = true,
    @SerialName("unavailable_reason") val unavailableReason: String = "",
    @SerialName("needs_code") val needsCode: Boolean = false,
    @SerialName("code_label") val codeLabel: String = "",
    @SerialName("code_help") val codeHelp: String = "",
    /** credentials: `json` (a file) or `secret` (one masked line). */
    val input: String = "json",
    val path: String = "",
    @SerialName("source_hint") val sourceHint: String = "",
    val placeholder: String = "",
    val warning: String = "",
    val fields: List<AccountEnvFieldDto> = emptyList(),
)

@Serializable
data class AccountEnvFieldDto(
    val name: String,
    val label: String = "",
    val secret: Boolean = true,
    val help: String = "",
    val placeholder: String = "",
    val choices: List<AccountChoiceDto>? = null,
    val set: Boolean = false,
    /** Masked; never the value of a secret field. */
    val preview: String = "",
    /** Non-secret fields only. */
    val value: String? = null,
)

@Serializable
data class AccountChoiceDto(val value: String, val label: String)

@Serializable
data class LoginFlowDto(
    val id: String = "",
    val service: String = "",
    val method: String = "",
    /** `starting` | `waiting` | `verifying` | `succeeded` | `failed` | `cancelled` | `expired`. */
    val status: String = "starting",
    val url: String? = null,
    @SerialName("user_code") val userCode: String? = null,
    @SerialName("needs_code") val needsCode: Boolean = false,
    @SerialName("code_label") val codeLabel: String = "Code",
    @SerialName("code_help") val codeHelp: String = "",
    val message: String = "",
    @SerialName("expires_at") val expiresAt: String? = null,
) {
    val active: Boolean get() = status == "starting" || status == "waiting" || status == "verifying"
}

@Serializable
data class AccountVerificationDto(val ok: Boolean = false, val message: String = "")

@Serializable
data class AccountActionDto(val message: String = "", val service: AccountServiceDto)

@Serializable
data class LoginRequest(val method: String)

@Serializable
data class LoginCodeRequest(val code: String)

@Serializable
data class AccountCredentialsRequest(val method: String, val content: String)

@Serializable
data class EnvListDto(val path: String = "", val exists: Boolean = true, val keys: List<EnvKeyDto> = emptyList())

@Serializable
data class EnvKeyDto(
    val name: String,
    val set: Boolean = false,
    /** `••••abcd` (long values) or `••••`; never the value. */
    val preview: String = "",
    val length: Int = 0,
    val line: Int = 0,
    val exported: Boolean = false,
    val duplicates: Int = 0,
    @SerialName("in_process") val inProcess: Boolean = true,
)

@Serializable
data class EnvRevealDto(val name: String, val value: String)

@Serializable
data class EnvValueRequest(val value: String)

@Serializable
data class EnvCreateRequest(val name: String, val value: String)

@Serializable
data class EnvChangeDto(
    /** `now` | `backend_restart`. */
    val applies: String = "now",
    val note: String = "",
)
