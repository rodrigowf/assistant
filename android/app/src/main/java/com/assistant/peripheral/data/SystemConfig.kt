package com.assistant.peripheral.data

/**
 * Backend system configuration (mirrors `frontend/src/api/rest.ts`).
 *
 * These types back the System tab in Settings, which exposes the same
 * controls as the web `ConfigPage` (orchestrator model, voice mode,
 * session provider, working directory, MCP toggles, etc.).
 */

data class WorkingDirectoryEntry(
    val id: String,
    val path: String,
    val label: String? = null,
    val sshHost: String? = null,
    val sshUser: String? = null,
)

data class AssistantConfig(
    val workingDirectory: String,
    val workingDirectoryHistory: List<WorkingDirectoryEntry>,
    val enabledMcps: List<String>,
    val chromeExtension: Boolean,
    val provider: String,
    val defaultModel: String,
    val harnessModel: Map<String, String>,
    val defaultVoiceProvider: String,
    val defaultVoiceModel: String,
    val defaultVoiceName: String,
    val defaultVoiceTranscriptionLanguage: String,
    // For the ``google`` voice provider only: "vertex" or "aistudio".
    // Other providers ignore this field. Default is "vertex".
    val defaultVoiceEndpoint: String,
    val voiceRecordingEnabled: Boolean,
)

data class McpServerConfig(
    val type: String?,
    val command: String,
    val args: List<String>,
    val env: Map<String, String>,
)

data class ModelInfo(
    val provider: String,
    val modelId: String,
    val displayName: String,
    val supportsAudio: Boolean,
    val supportsVision: Boolean,
    val supportsTools: Boolean,
    val maxTokens: Int,
)

data class QwenModelInfo(
    val id: String,
    val displayName: String,
    val provider: String,
    val baseUrl: String?,
    val contextWindow: Int?,
    val supportsVision: Boolean,
    val supportsVideo: Boolean,
    val supportsThinking: Boolean,
)

data class VoiceEntry(
    val id: String,
    val label: String,
    val description: String,
)

data class TranscriptionLanguageEntry(
    val id: String,
    val label: String,
    val description: String,
)

data class VoiceModelEntry(
    val id: String,
    val label: String,
    val voice: String,
    val voices: List<VoiceEntry>,
    val transcriptionLanguages: List<TranscriptionLanguageEntry>,
    val defaultTranscriptionLanguage: String,
    val isDefault: Boolean,
)

data class SessionProviderSpec(
    val id: String,
    val label: String,
    val description: String,
)

/**
 * The full system-config snapshot the System Settings tab renders from.
 * Each sub-list is loaded independently from its REST endpoint.
 */
data class SystemConfigState(
    val config: AssistantConfig? = null,
    val mcpServers: Map<String, McpServerConfig> = emptyMap(),
    val models: List<ModelInfo> = emptyList(),
    val voiceProviders: Map<String, List<VoiceModelEntry>> = emptyMap(),
    val qwenHarnessModels: List<QwenModelInfo> = emptyList(),
    val sessionProviders: List<SessionProviderSpec> = emptyList(),
    val loading: Boolean = false,
    val saving: Boolean = false,
    val error: String? = null,
    val savedFlash: Boolean = false,
)

/**
 * Patch payload for `PUT /api/config`. Mirrors `ConfigUpdate` in
 * `api/routes/config.py` — only the fields that are set get sent.
 */
data class ConfigPatch(
    val workingDirectory: String? = null,
    val enabledMcps: List<String>? = null,
    val chromeExtension: Boolean? = null,
    val provider: String? = null,
    val defaultModel: String? = null,
    val harnessModel: Map<String, String>? = null,
    val defaultVoiceProvider: String? = null,
    val defaultVoiceModel: String? = null,
    val defaultVoiceName: String? = null,
    val defaultVoiceTranscriptionLanguage: String? = null,
    val defaultVoiceEndpoint: String? = null,
    val voiceRecordingEnabled: Boolean? = null,
)
