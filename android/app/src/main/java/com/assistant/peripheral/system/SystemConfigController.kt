package com.assistant.peripheral.system

import android.util.Log
import com.assistant.peripheral.data.AssistantConfig
import com.assistant.peripheral.data.ConfigPatch
import com.assistant.peripheral.data.McpServerConfig
import com.assistant.peripheral.data.ModelInfo
import com.assistant.peripheral.data.QwenModelInfo
import com.assistant.peripheral.data.SessionProviderSpec
import com.assistant.peripheral.data.SystemConfigState
import com.assistant.peripheral.data.VoiceModelAutoCorrection
import com.assistant.peripheral.data.VoiceModelEntry
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.async
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/**
 * Owns the backend system-config snapshot that drives the System Settings tab.
 * Increment 5 of the viewmodel refactor.
 *
 * Refactor base: HEAD `fe2210c` ("Inc 3.5 — orchestrator session conflict
 * mediation"). Pinned source ranges from AssistantViewModel.kt:
 *   - L136-138 `_systemConfig` flow + `savedFlashJob` field
 *   - L345-398 `loadSystemConfig`
 *   - L400-448 `updateSystemConfig`
 *   - L450-452 `dismissVoiceModelAutoCorrected`
 *   - L454-481 `maybeAutoCorrectVoiceModel`
 *   - L483-488 `toggleMcp`
 *
 * Design notes:
 *
 *  - Function-typed deps for ApiClient calls (same pattern as Inc 2/3/4).
 *    The ViewModel rebuilds `apiClient` on serverUrlChanged, so each lambda
 *    closes over the current client. Tests fake the lambdas — no Mockito.
 *
 *  - The controller exposes only `systemConfig: StateFlow<SystemConfigState>`
 *    + the 4 user-facing operations. No cross-controller events: System
 *    Settings is self-contained (load on tab open, save on user input).
 *
 *  - `savedFlashJob` is internal so a follow-up `update` cancels the prior
 *    flash timer cleanly (HEAD behavior: rapid saves don't accumulate).
 */
class SystemConfigController(
    private val scope: CoroutineScope,
    private val getAssistantConfig: suspend () -> AssistantConfig?,
    private val listMcpServers: suspend () -> Map<String, McpServerConfig>,
    private val listOrchestratorModels: suspend () -> List<ModelInfo>,
    private val listVoiceModels: suspend () -> Map<String, List<VoiceModelEntry>>,
    private val listQwenHarnessModels: suspend () -> List<QwenModelInfo>,
    private val listSessionProviders: suspend () -> List<SessionProviderSpec>,
    private val listGoogleVoiceModels: suspend (endpoint: String?) -> List<VoiceModelEntry>,
    private val updateAssistantConfig: suspend (ConfigPatch) -> Result<AssistantConfig>,
) {

    companion object {
        private const val TAG = "SystemConfigCtrl"
        /** Save-flash visual feedback duration. Pinned from HEAD AssistantViewModel.kt:436. */
        private const val SAVED_FLASH_MS = 2000L
    }

    private val _systemConfig = MutableStateFlow(SystemConfigState())
    val systemConfig: StateFlow<SystemConfigState> = _systemConfig.asStateFlow()

    private var savedFlashJob: Job? = null

    /**
     * Load the full backend system config (assistant config + MCP servers +
     * model catalog + voice models + session providers + Qwen harness models).
     * Fans out the sub-list calls in parallel; failures of individual lists
     * fall through to empty defaults. Only a full failure to load the main
     * config surfaces as an error.
     */
    fun loadSystemConfig() {
        scope.launch {
            _systemConfig.value = _systemConfig.value.copy(loading = true, error = null)
            try {
                // Fan out — these don't depend on each other. The real
                // ApiClient functions already `withContext(Dispatchers.IO)`
                // internally, so wrapping again here is redundant — and
                // would also pin tests to a real IO thread pool that doesn't
                // join `runTest`'s TestCoroutineScheduler.
                val cfgDef = async { getAssistantConfig() }
                val mcpDef = async { listMcpServers() }
                val modelsDef = async { listOrchestratorModels() }
                val voiceDef = async { listVoiceModels() }
                val qwenDef = async { listQwenHarnessModels() }
                val providersDef = async { listSessionProviders() }

                val cfg = cfgDef.await()
                if (cfg == null) {
                    _systemConfig.value = _systemConfig.value.copy(
                        loading = false,
                        error = "Failed to load configuration",
                    )
                    return@launch
                }
                // Merge dynamic Gemini Live list into static voice providers.
                // The endpoint (vertex / aistudio) decides which Google backend
                // the catalog is fetched from — mirrors the web ConfigPage.
                val googleVoice = listGoogleVoiceModels(cfg.defaultVoiceEndpoint)
                val voiceProviders = voiceDef.await().toMutableMap()
                if (googleVoice.isNotEmpty()) voiceProviders["google"] = googleVoice

                // Auto-correct: if the saved Gemini model is no longer in the
                // discovered catalog (Google renames Live ids periodically),
                // write through to the new default and surface a banner.
                val (correctedCfg, correction) = maybeAutoCorrectVoiceModel(cfg, googleVoice)

                _systemConfig.value = SystemConfigState(
                    config = correctedCfg,
                    mcpServers = mcpDef.await(),
                    models = modelsDef.await(),
                    voiceProviders = voiceProviders,
                    qwenHarnessModels = qwenDef.await(),
                    sessionProviders = providersDef.await(),
                    loading = false,
                    voiceModelAutoCorrected = correction,
                )
            } catch (e: Exception) {
                Log.e(TAG, "loadSystemConfig error: ${e.message}", e)
                _systemConfig.value = _systemConfig.value.copy(
                    loading = false,
                    error = e.message ?: "Failed to load configuration",
                )
            }
        }
    }

    /**
     * Apply a partial update to the backend config. On success the full updated
     * config is stored + a 2-second savedFlash banner fires. On failure the
     * current state is left unchanged and an error message surfaces.
     */
    fun updateSystemConfig(patch: ConfigPatch) {
        scope.launch {
            val prevEndpoint = _systemConfig.value.config?.defaultVoiceEndpoint
            _systemConfig.value = _systemConfig.value.copy(saving = true, error = null)
            val result = updateAssistantConfig(patch)
            result.fold(
                onSuccess = { newCfg ->
                    var effectiveCfg = newCfg
                    var correction: VoiceModelAutoCorrection? = _systemConfig.value.voiceModelAutoCorrected
                    val voiceProviders = if (
                        newCfg.defaultVoiceEndpoint != prevEndpoint
                    ) {
                        val googleVoice = listGoogleVoiceModels(newCfg.defaultVoiceEndpoint)
                        val merged = _systemConfig.value.voiceProviders.toMutableMap()
                        if (googleVoice.isNotEmpty()) {
                            merged["google"] = googleVoice
                            // The newly-fetched catalog may not include the
                            // saved model id (especially after flipping
                            // Vertex↔AI Studio, where the canonical id
                            // differs). Auto-correct here too.
                            val (corrected, c) = maybeAutoCorrectVoiceModel(newCfg, googleVoice)
                            effectiveCfg = corrected
                            if (c != null) correction = c
                        }
                        merged
                    } else {
                        _systemConfig.value.voiceProviders
                    }
                    _systemConfig.value = _systemConfig.value.copy(
                        config = effectiveCfg,
                        voiceProviders = voiceProviders,
                        saving = false,
                        savedFlash = true,
                        voiceModelAutoCorrected = correction,
                    )
                    savedFlashJob?.cancel()
                    savedFlashJob = scope.launch {
                        delay(SAVED_FLASH_MS)
                        _systemConfig.value = _systemConfig.value.copy(savedFlash = false)
                    }
                },
                onFailure = { e ->
                    _systemConfig.value = _systemConfig.value.copy(
                        saving = false,
                        error = e.message ?: "Failed to save",
                    )
                },
            )
        }
    }

    fun dismissVoiceModelAutoCorrected() {
        _systemConfig.value = _systemConfig.value.copy(voiceModelAutoCorrected = null)
    }

    /**
     * Snap the saved Gemini Live model to the discovered default when
     * the catalog no longer lists it. Returns the (possibly updated)
     * config and the correction record (null = no change needed).
     *
     * Empty catalog → no-op: we only correct when we have a known-good list.
     * Failures fall through silently, leaving the saved value as the source
     * of truth.
     */
    private suspend fun maybeAutoCorrectVoiceModel(
        cfg: AssistantConfig,
        discovered: List<VoiceModelEntry>,
    ): Pair<AssistantConfig, VoiceModelAutoCorrection?> {
        if (cfg.defaultVoiceProvider != "google") return cfg to null
        if (discovered.isEmpty()) return cfg to null
        if (discovered.any { it.id == cfg.defaultVoiceModel }) return cfg to null
        val newDefault = discovered.firstOrNull { it.isDefault } ?: discovered.first()
        val voiceListed = newDefault.voices.any { it.id == cfg.defaultVoiceName }
        val patch = ConfigPatch(
            defaultVoiceModel = newDefault.id,
            defaultVoiceName = if (voiceListed) null else newDefault.voice,
        )
        val result = updateAssistantConfig(patch)
        return result.fold(
            onSuccess = { updated ->
                updated to VoiceModelAutoCorrection(
                    from = cfg.defaultVoiceModel,
                    to = newDefault.id,
                )
            },
            onFailure = { e ->
                Log.w(TAG, "auto-correct voice model failed: ${e.message}")
                cfg to null
            },
        )
    }

    /** Toggle a single MCP server in `enabled_mcps`. */
    fun toggleMcp(name: String) {
        val cfg = _systemConfig.value.config ?: return
        val next = cfg.enabledMcps.toMutableList()
        if (next.contains(name)) next.remove(name) else next.add(name)
        updateSystemConfig(ConfigPatch(enabledMcps = next))
    }
}
