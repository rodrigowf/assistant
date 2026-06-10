package com.assistant.peripheral.system.parity

import com.assistant.peripheral.data.AssistantConfig
import com.assistant.peripheral.data.ConfigPatch
import com.assistant.peripheral.data.McpServerConfig
import com.assistant.peripheral.data.ModelInfo
import com.assistant.peripheral.data.QwenModelInfo
import com.assistant.peripheral.data.SessionProviderSpec
import com.assistant.peripheral.data.VoiceEntry
import com.assistant.peripheral.data.VoiceModelEntry
import com.assistant.peripheral.data.WorkingDirectoryEntry
import com.assistant.peripheral.system.SystemConfigController
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference

/**
 * Parity tests for Increment 5 (`SystemConfigController` extraction) of the
 * Android viewmodel refactor plan
 * (assistant/plans/android_viewmodel_refactor_plan_2026_06_10.md §6 + §10.5).
 *
 * Refactor base: HEAD `fe2210c` ("Inc 3.5 — orchestrator session conflict
 * mediation"). What moves into SystemConfigController: `_systemConfig` flow +
 * `savedFlashJob` field + `loadSystemConfig` / `updateSystemConfig` /
 * `toggleMcp` / `dismissVoiceModelAutoCorrected` / `maybeAutoCorrectVoiceModel`.
 *
 * What this test pins (byte-identical against HEAD AssistantViewModel.kt
 * ranges in §10.5):
 *
 *  1. **SystemConfigLoadParity** — `loadSystemConfig` fans out to all six
 *     ApiClient endpoints in parallel; merges the Google voice catalog over
 *     the static voice-providers map; auto-corrects the Gemini default voice
 *     model if the saved id is gone from the discovered catalog; flips
 *     `loading` true → false; surfaces `error` only when the main config
 *     call itself fails.
 *
 *  2. **SystemConfigSaveFlashParity** — `updateSystemConfig` triggers the
 *     `savedFlash` visual feedback after a successful patch; the flash
 *     clears after exactly 2000 ms (HEAD `delay(2000)`); failed saves do
 *     NOT flash and surface `error`.
 *
 *  3. **McpToggleOptimisticParity** — `toggleMcp(name)` reads the current
 *     `enabledMcps`, flips membership, and calls `updateAssistantConfig`
 *     with the new list. A no-op when `config == null`.
 */
/** Top-level helper so [FakeDeps] (nested class) can use it for field defaults. */
private fun defaultSampleConfig() = AssistantConfig(
    workingDirectory = "/home/u/work",
    workingDirectoryHistory = emptyList<WorkingDirectoryEntry>(),
    enabledMcps = listOf("chrome-devtools"),
    chromeExtension = false,
    provider = "anthropic",
    defaultModel = "claude-opus",
    harnessModel = emptyMap(),
    defaultVoiceProvider = "openai",
    defaultVoiceModel = "gpt-realtime",
    defaultVoiceName = "Puck",
    defaultVoiceTranscriptionLanguage = "",
    defaultVoiceEndpoint = "vertex",
    voiceRecordingEnabled = true,
)

@OptIn(ExperimentalCoroutinesApi::class)
class SystemConfigControllerParityTest {

    private var activeScope: CoroutineScope? = null

    @After
    fun tearDown() {
        // Cancel the child scope so any in-flight savedFlash timer doesn't
        // hold runTest open.
        activeScope?.cancel()
    }

    /** Tests must call this before returning from `runTest` body. */
    private fun cleanup() {
        activeScope?.cancel()
    }

    /** Records every ApiClient call so tests can assert call counts + arguments. */
    private class FakeDeps {
        val getConfigCalls = AtomicInteger(0)
        val listMcpCalls = AtomicInteger(0)
        val listModelsCalls = AtomicInteger(0)
        val listVoiceCalls = AtomicInteger(0)
        val listQwenCalls = AtomicInteger(0)
        val listProvidersCalls = AtomicInteger(0)
        val listGoogleVoiceCalls = AtomicInteger(0)
        val lastGoogleVoiceEndpoint = AtomicReference<String?>(null)
        val updateCalls = AtomicInteger(0)
        val lastPatch = AtomicReference<ConfigPatch?>(null)

        var assistantConfig: AssistantConfig? = defaultSampleConfig()
        var mcpServers: Map<String, McpServerConfig> = mapOf(
            "chrome-devtools" to McpServerConfig("stdio", "node", listOf("server.js"), emptyMap())
        )
        var orchestratorModels: List<ModelInfo> = emptyList()
        var voiceModels: Map<String, List<VoiceModelEntry>> = emptyMap()
        var qwenModels: List<QwenModelInfo> = emptyList()
        var sessionProviders: List<SessionProviderSpec> = emptyList()
        var googleVoiceModels: List<VoiceModelEntry> = emptyList()
        /** When non-null, `updateAssistantConfig` returns success with this. Null → failure. */
        var updateReturn: AssistantConfig? = defaultSampleConfig()
        var updateFailure: Throwable? = null
    }

    private fun sampleConfig(
        defaultVoiceProvider: String = "openai",
        defaultVoiceModel: String = "gpt-realtime",
        defaultVoiceEndpoint: String = "vertex",
        enabledMcps: List<String> = listOf("chrome-devtools"),
    ) = AssistantConfig(
        workingDirectory = "/home/u/work",
        workingDirectoryHistory = emptyList<WorkingDirectoryEntry>(),
        enabledMcps = enabledMcps,
        chromeExtension = false,
        provider = "anthropic",
        defaultModel = "claude-opus",
        harnessModel = emptyMap(),
        defaultVoiceProvider = defaultVoiceProvider,
        defaultVoiceModel = defaultVoiceModel,
        defaultVoiceName = "Puck",
        defaultVoiceTranscriptionLanguage = "",
        defaultVoiceEndpoint = defaultVoiceEndpoint,
        voiceRecordingEnabled = true,
    )

    private fun controller(
        parent: CoroutineScope,
        fakes: FakeDeps,
    ): SystemConfigController {
        val job = SupervisorJob(parent.coroutineContext[Job])
        val scope = CoroutineScope(parent.coroutineContext + job)
        activeScope = scope
        return SystemConfigController(
            scope = scope,
        getAssistantConfig = {
            fakes.getConfigCalls.incrementAndGet()
            fakes.assistantConfig
        },
        listMcpServers = { fakes.listMcpCalls.incrementAndGet(); fakes.mcpServers },
        listOrchestratorModels = { fakes.listModelsCalls.incrementAndGet(); fakes.orchestratorModels },
        listVoiceModels = { fakes.listVoiceCalls.incrementAndGet(); fakes.voiceModels },
        listQwenHarnessModels = { fakes.listQwenCalls.incrementAndGet(); fakes.qwenModels },
        listSessionProviders = { fakes.listProvidersCalls.incrementAndGet(); fakes.sessionProviders },
        listGoogleVoiceModels = { endpoint ->
            fakes.listGoogleVoiceCalls.incrementAndGet()
            fakes.lastGoogleVoiceEndpoint.set(endpoint)
            fakes.googleVoiceModels
        },
        updateAssistantConfig = { patch ->
            fakes.updateCalls.incrementAndGet()
            fakes.lastPatch.set(patch)
            val failure = fakes.updateFailure
            val ret = fakes.updateReturn
            when {
                failure != null -> Result.failure(failure)
                ret != null -> Result.success(ret)
                else -> Result.failure(IllegalStateException("no updateReturn configured"))
            }
        },
    )
    }

    // =================================================================
    // 1. SystemConfigLoadParity
    // =================================================================

    @Test
    fun `load — happy path fans out and fills SystemConfigState`() = runTest {
        val fakes = FakeDeps()
        val ctrl = controller(this, fakes)

        ctrl.loadSystemConfig()
        advanceUntilIdle()

        // Each ApiClient endpoint called exactly once in parallel.
        assertEquals(1, fakes.getConfigCalls.get())
        assertEquals(1, fakes.listMcpCalls.get())
        assertEquals(1, fakes.listModelsCalls.get())
        assertEquals(1, fakes.listVoiceCalls.get())
        assertEquals(1, fakes.listQwenCalls.get())
        assertEquals(1, fakes.listProvidersCalls.get())
        assertEquals(1, fakes.listGoogleVoiceCalls.get())

        val state = ctrl.systemConfig.value
        assertFalse("loading should be false after load", state.loading)
        assertNull("error should be null on happy path", state.error)
        assertNotNull(state.config)
        assertEquals(1, state.mcpServers.size)
        cleanup()
    }

    @Test
    fun `load — main config null surfaces error and loading=false`() = runTest {
        val fakes = FakeDeps()
        fakes.assistantConfig = null
        val ctrl = controller(this, fakes)

        ctrl.loadSystemConfig()
        advanceUntilIdle()

        val state = ctrl.systemConfig.value
        assertFalse(state.loading)
        assertEquals("Failed to load configuration", state.error)
        assertNull("config stays null when main call returned null", state.config)
        cleanup()
    }

    @Test
    fun `load — google voice catalog merges into voiceProviders map under 'google' key`() = runTest {
        val fakes = FakeDeps()
        fakes.assistantConfig = sampleConfig(defaultVoiceProvider = "google", defaultVoiceModel = "gemini-x")
        fakes.voiceModels = mapOf(
            "openai" to listOf(makeEntry("gpt-realtime", isDefault = true))
        )
        fakes.googleVoiceModels = listOf(makeEntry("gemini-x", isDefault = true))
        val ctrl = controller(this, fakes)

        ctrl.loadSystemConfig()
        advanceUntilIdle()

        val state = ctrl.systemConfig.value
        assertTrue(state.voiceProviders.containsKey("openai"))
        assertTrue("google catalog must be merged", state.voiceProviders.containsKey("google"))
        assertEquals(1, state.voiceProviders["google"]?.size)
        assertEquals("vertex", fakes.lastGoogleVoiceEndpoint.get())
        cleanup()
    }

    @Test
    fun `load — auto-corrects gemini default model when saved id is gone from discovered catalog`() = runTest {
        val fakes = FakeDeps()
        // Saved Gemini model id is stale: it is NOT in the discovered catalog.
        fakes.assistantConfig = sampleConfig(
            defaultVoiceProvider = "google",
            defaultVoiceModel = "gemini-stale-id",
        )
        // Discovered catalog has a different default — the controller must
        // patch defaultVoiceModel to the new default.
        fakes.googleVoiceModels = listOf(
            makeEntry("gemini-new-default", isDefault = true)
        )
        // The updateAssistantConfig call inside auto-correct returns the
        // corrected config (with defaultVoiceModel switched).
        fakes.updateReturn = sampleConfig(
            defaultVoiceProvider = "google",
            defaultVoiceModel = "gemini-new-default",
        )
        val ctrl = controller(this, fakes)

        ctrl.loadSystemConfig()
        advanceUntilIdle()

        val state = ctrl.systemConfig.value
        assertEquals(1, fakes.updateCalls.get())
        val patch = fakes.lastPatch.get()
        assertNotNull(patch)
        assertEquals("gemini-new-default", patch!!.defaultVoiceModel)
        assertNotNull("auto-correction banner must be set", state.voiceModelAutoCorrected)
        assertEquals("gemini-stale-id", state.voiceModelAutoCorrected!!.from)
        assertEquals("gemini-new-default", state.voiceModelAutoCorrected!!.to)
        assertEquals(
            "config must reflect the corrected model id",
            "gemini-new-default",
            state.config!!.defaultVoiceModel
        )
        cleanup()
    }

    @Test
    fun `load — auto-correct is a no-op when saved gemini id is still in discovered catalog`() = runTest {
        val fakes = FakeDeps()
        fakes.assistantConfig = sampleConfig(
            defaultVoiceProvider = "google",
            defaultVoiceModel = "gemini-still-there",
        )
        fakes.googleVoiceModels = listOf(
            makeEntry("gemini-still-there", isDefault = true)
        )
        val ctrl = controller(this, fakes)

        ctrl.loadSystemConfig()
        advanceUntilIdle()

        assertEquals("no patch needed when saved id is still there", 0, fakes.updateCalls.get())
        assertNull(ctrl.systemConfig.value.voiceModelAutoCorrected)
        cleanup()
    }

    @Test
    fun `load — auto-correct is a no-op when provider is not google`() = runTest {
        val fakes = FakeDeps()
        fakes.assistantConfig = sampleConfig(
            defaultVoiceProvider = "openai",
            defaultVoiceModel = "gpt-realtime",
        )
        fakes.googleVoiceModels = listOf(makeEntry("gemini-default", isDefault = true))
        val ctrl = controller(this, fakes)

        ctrl.loadSystemConfig()
        advanceUntilIdle()

        assertEquals(0, fakes.updateCalls.get())
        assertNull(ctrl.systemConfig.value.voiceModelAutoCorrected)
        cleanup()
    }

    @Test
    fun `load — auto-correct is a no-op when discovered catalog is empty`() = runTest {
        val fakes = FakeDeps()
        fakes.assistantConfig = sampleConfig(
            defaultVoiceProvider = "google",
            defaultVoiceModel = "gemini-x",
        )
        fakes.googleVoiceModels = emptyList()
        val ctrl = controller(this, fakes)

        ctrl.loadSystemConfig()
        advanceUntilIdle()

        assertEquals(0, fakes.updateCalls.get())
        assertNull(ctrl.systemConfig.value.voiceModelAutoCorrected)
        cleanup()
    }

    // =================================================================
    // 2. SystemConfigSaveFlashParity
    // =================================================================

    @Test
    fun `save — successful patch sets savedFlash true, then clears it after exactly 2000ms`() = runTest {
        val fakes = FakeDeps()
        fakes.updateReturn = sampleConfig()
        val ctrl = controller(this, fakes)

        ctrl.updateSystemConfig(ConfigPatch(workingDirectory = "/tmp"))
        // Run patch + initial state mutation without advancing past the 2000 ms flash.
        runCurrent()
        // We have to let the suspend patch call complete first.
        advanceTimeBy(1)
        runCurrent()

        // savedFlash should be true now; flag has NOT timed out yet.
        assertTrue(
            "savedFlash must be true immediately after successful patch",
            ctrl.systemConfig.value.savedFlash
        )
        assertFalse(ctrl.systemConfig.value.saving)
        assertNull(ctrl.systemConfig.value.error)

        // Advance just before the 2000ms boundary — flash still on.
        advanceTimeBy(1998)
        runCurrent()
        assertTrue(
            "savedFlash should remain true until the 2000ms timer fires",
            ctrl.systemConfig.value.savedFlash
        )

        // Cross the boundary — flash clears.
        advanceTimeBy(10)
        runCurrent()
        assertFalse(
            "savedFlash must clear after the 2000ms timer fires",
            ctrl.systemConfig.value.savedFlash
        )
        cleanup()
    }

    @Test
    fun `save — failed patch sets error and leaves savedFlash false`() = runTest {
        val fakes = FakeDeps()
        fakes.updateReturn = null
        fakes.updateFailure = RuntimeException("boom")
        val ctrl = controller(this, fakes)

        ctrl.updateSystemConfig(ConfigPatch(workingDirectory = "/tmp"))
        advanceUntilIdle()

        val state = ctrl.systemConfig.value
        assertFalse(state.saving)
        assertFalse(state.savedFlash)
        assertEquals("boom", state.error)
        cleanup()
    }

    @Test
    fun `save — endpoint change triggers google voice catalog refetch`() = runTest {
        val fakes = FakeDeps()
        fakes.assistantConfig = sampleConfig(defaultVoiceEndpoint = "vertex")
        fakes.updateReturn = sampleConfig(defaultVoiceEndpoint = "aistudio")
        val ctrl = controller(this, fakes)

        // Seed initial state.
        ctrl.loadSystemConfig()
        advanceUntilIdle()
        val refetchesBefore = fakes.listGoogleVoiceCalls.get()

        ctrl.updateSystemConfig(ConfigPatch(defaultVoiceEndpoint = "aistudio"))
        advanceUntilIdle()

        assertTrue(
            "endpoint switch must trigger a google voice refetch",
            fakes.listGoogleVoiceCalls.get() > refetchesBefore
        )
        assertEquals("aistudio", fakes.lastGoogleVoiceEndpoint.get())
        cleanup()
    }

    // =================================================================
    // 3. McpToggleOptimisticParity
    // =================================================================

    @Test
    fun `toggleMcp — flipping membership produces a patch with the new enabledMcps list`() = runTest {
        val fakes = FakeDeps()
        fakes.assistantConfig = sampleConfig(enabledMcps = listOf("chrome-devtools"))
        fakes.updateReturn = sampleConfig(enabledMcps = emptyList())
        val ctrl = controller(this, fakes)

        ctrl.loadSystemConfig()
        advanceUntilIdle()
        // Toggle off the only enabled MCP.
        ctrl.toggleMcp("chrome-devtools")
        advanceUntilIdle()

        val patch = fakes.lastPatch.get()
        assertNotNull(patch)
        assertEquals(emptyList<String>(), patch!!.enabledMcps)
        cleanup()
    }

    @Test
    fun `toggleMcp — adding a previously-disabled MCP patches with it included`() = runTest {
        val fakes = FakeDeps()
        fakes.assistantConfig = sampleConfig(enabledMcps = emptyList())
        fakes.updateReturn = sampleConfig(enabledMcps = listOf("filesystem"))
        val ctrl = controller(this, fakes)

        ctrl.loadSystemConfig()
        advanceUntilIdle()
        ctrl.toggleMcp("filesystem")
        advanceUntilIdle()

        val patch = fakes.lastPatch.get()
        assertNotNull(patch)
        assertEquals(listOf("filesystem"), patch!!.enabledMcps)
        cleanup()
    }

    @Test
    fun `toggleMcp — no-op when config is null`() = runTest {
        val fakes = FakeDeps()
        // Don't seed: config stays null.
        val ctrl = controller(this, fakes)

        ctrl.toggleMcp("anything")
        advanceUntilIdle()

        assertEquals(0, fakes.updateCalls.get())
        cleanup()
    }

    // =================================================================
    // Extra: dismissVoiceModelAutoCorrected clears the banner only
    // =================================================================

    @Test
    fun `dismissVoiceModelAutoCorrected — clears the banner without touching the rest of state`() = runTest {
        val fakes = FakeDeps()
        fakes.assistantConfig = sampleConfig(
            defaultVoiceProvider = "google",
            defaultVoiceModel = "gemini-stale",
        )
        fakes.googleVoiceModels = listOf(makeEntry("gemini-new", isDefault = true))
        fakes.updateReturn = sampleConfig(
            defaultVoiceProvider = "google",
            defaultVoiceModel = "gemini-new",
        )
        val ctrl = controller(this, fakes)

        ctrl.loadSystemConfig()
        advanceUntilIdle()
        val correction = ctrl.systemConfig.value.voiceModelAutoCorrected
        assertNotNull(correction)

        ctrl.dismissVoiceModelAutoCorrected()
        assertNull(ctrl.systemConfig.value.voiceModelAutoCorrected)
        // Config + flash + saving + error untouched.
        assertEquals("gemini-new", ctrl.systemConfig.value.config!!.defaultVoiceModel)
        cleanup()
    }

    private fun makeEntry(id: String, isDefault: Boolean) = VoiceModelEntry(
        id = id,
        label = id,
        voice = "Puck",
        voices = listOf(VoiceEntry("Puck", "Puck", "the puck voice")),
        transcriptionLanguages = emptyList(),
        defaultTranscriptionLanguage = "",
        isDefault = isDefault,
    )
}
