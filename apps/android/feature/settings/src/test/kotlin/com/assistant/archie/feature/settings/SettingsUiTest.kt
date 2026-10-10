package com.assistant.archie.feature.settings

import android.app.Application
import androidx.compose.ui.semantics.SemanticsActions
import androidx.compose.ui.test.SemanticsNodeInteraction
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.assertIsOff
import androidx.compose.ui.test.assertIsOn
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTextReplacement
import androidx.compose.ui.test.performImeAction
import androidx.compose.ui.test.performSemanticsAction
import androidx.datastore.preferences.core.Preferences
import com.assistant.archie.feature.settings.ui.SessionSettingsSheet
import com.assistant.archie.feature.settings.ui.SettingsPageKey
import com.assistant.archie.feature.settings.ui.SettingsPageScreen
import com.assistant.archie.feature.settings.ui.SettingsScreen
import com.assistant.core.data.ConnectionStatus
import com.assistant.core.design.theme.ArchieTheme
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * `SettingsSaveUiTest` of spec 14 §6.3 and the B-08 DoD: every save shows "Saved" or the server's
 * error verbatim + Retry; MCP "empty = all" semantics (bug 1) with the last-on server locked;
 * connection rows switch and reconnect; permission rationale, denied and blocked states; the
 * session sheet PUTs only the changed keys and restarts.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36], qualifiers = "w443dp-h986dp-xxhdpi", application = Application::class)
class SettingsUiTest {
    @get:Rule val compose = createComposeRule()
    private var harness: Harness? = null

    @After fun tearDown() { harness?.close() }

    private fun h(
        prefs: Preferences = SettingsGoldenBase.DEVICE,
        platform: FakePlatform = FakePlatform(),
        sessions: SessionControl = SessionControl.None,
        configure: (SettingsBackend) -> Unit = {},
    ): Harness = Harness(SettingsBackend().start().also(configure), prefs, platform, sessions = sessions).also {
        harness = it
        runBlocking { it.feature.server.refreshNow(); it.feature.auth.checkNow() }
    }

    private fun show(h: Harness, page: SettingsPageKey) {
        compose.setContent { ArchieTheme { SettingsPageScreen(h.feature, page, onBack = {}) } }
        compose.waitForIdle()
    }

    // 30 s: the full `check` runs every module's Robolectric tests at once; 10 s flaked under that load.
    private fun waitText(text: String, timeoutMs: Long = 30_000) =
        compose.waitUntil(timeoutMs) { compose.onAllNodes(hasText(text)).fetchSemanticsNodes().isNotEmpty() }

    @Test fun serverSave_showsSavedSnackbar() {
        val h = h()
        show(h, SettingsPageKey.AGENT_SESSIONS)
        compose.onNodeWithTag("chrome").performScrollTo().assertIsOn().performClick()
        waitText("Saved")
        eventually { h.backend.puts.singleOrNull() == """{"chrome_extension":false}""" }
        compose.waitUntil(5_000) { runCatching { compose.onNodeWithTag("chrome").assertIsOff() }.isSuccess }
    }

    @Test fun serverSave_failure_showsDetailVerbatim_andRetrySaves() {
        val detail = "Unknown provider 'claude' (CLI not installed on the server)"
        val h = h { it.failNextPut = 400 to detail }
        show(h, SettingsPageKey.AGENT_SESSIONS)
        compose.onNodeWithTag("chrome").performScrollTo().performClick()
        waitText(detail)
        compose.onNodeWithText("Retry").performClick()
        waitText("Saved")
        eventually { h.backend.puts.size == 2 && h.backend.puts[0] == h.backend.puts[1] }
    }

    @Test fun deviceSave_showsSaved_andWritesTheOldDataStoreKey() {
        val h = h()
        show(h, SettingsPageKey.APPEARANCE)
        compose.onNodeWithText("Light").performClick()
        waitText("Saved")
        eventually { h.settings.settings.value?.themeMode == com.assistant.core.model.ThemeMode.LIGHT }
    }

    /** Bug 1: `[]` shows every switch on; one off writes the others; Turn all on writes `[]`. */
    @Test fun mcp_emptyMeansAll_andSwitchingOneOffWritesTheOthers() {
        val h = h { b ->
            b.mcp = """{"servers":{"a":{"command":"x"},"b":{"command":"y"},"c":{"command":"z"}}}"""
            b.config = JsonObject(b.config + ("enabled_mcps" to JsonArray(emptyList())))
        }
        show(h, SettingsPageKey.MCP_SERVERS)
        listOf("a", "b", "c").forEach { compose.onNodeWithTag("mcp:$it").assertIsSelected() }
        compose.onNodeWithText("All servers are on, including any added to .claude.json later.").assertExists()
        compose.onNodeWithTag("mcp:b").performClick()
        eventually { h.backend.puts.lastOrNull() == """{"enabled_mcps":["a","c"]}""" }
        waitText("Turn all on")
        compose.onNodeWithTag("mcp-all-on").performClick()
        eventually { h.backend.puts.lastOrNull() == """{"enabled_mcps":[]}""" }
    }

    /** The live Jetson: one server, explicitly on — it is the last one on, so it can't go off. */
    @Test fun mcp_lastOnServer_isLocked() {
        val h = h()
        show(h, SettingsPageKey.MCP_SERVERS)
        compose.onNodeWithTag("mcp:chrome-devtools").assertIsNotEnabled()
        compose.onNodeWithText("Last one on (an empty list means all)").assertExists()
    }

    /** "No servers yet" while connected (inv03 §1.6) is gone; tapping another server switches to it. */
    @Test fun connection_listsCurrent_andTappingAServerSwitches() {
        val h = h()
        show(h, SettingsPageKey.CONNECTION)
        compose.onNodeWithText("Connected to jetson").assertExists()
        compose.onNodeWithTag("server:ws://192.168.0.200:80").assertExists()
        compose.onNodeWithTag("server:ws://192.168.0.28:8765").performClick()
        eventually { h.connection.changes == listOf("ws://192.168.0.28:8765") }
        waitText("Connecting to laptop…")
    }

    /** Wake word with the microphone denied: rationale first, nothing enabled behind the user's back. */
    @Test fun wakeWord_withoutMicrophone_showsRationale_andStaysOff() {
        val p = FakePlatform().apply { granted.remove(AppPermission.MICROPHONE) }
        val h = h(prefs = androidx.datastore.preferences.core.mutablePreferencesOf().apply {
            this[androidx.datastore.preferences.core.booleanPreferencesKey("enable_wake_word")] = false
        }, platform = p)
        show(h, SettingsPageKey.WAKE_WORD)
        compose.onNodeWithTag("wake-toggle").assertIsOff().performClick()
        compose.onNodeWithTag("rationale:MICROPHONE").assertExists()
        compose.onNodeWithText("Continue").assertExists()
        compose.onNodeWithText("Not now").performClick()
        compose.onNodeWithTag("rationale:MICROPHONE").assertDoesNotExist()
        Thread.sleep(200)
        assertEquals(false, h.settings.settings.value?.enableWakeWord)
    }

    /** Denials are not silent (inv03 §1.1): wake word on but no mic → a visible card with Allow. */
    @Test fun wakeWord_onButMicDenied_showsTheCard() {
        val p = FakePlatform().apply { granted.remove(AppPermission.MICROPHONE) }
        val h = h(platform = p)
        show(h, SettingsPageKey.WAKE_WORD)
        compose.onNodeWithText("Microphone permission needed").assertExists()
        compose.onNodeWithText("Needs the microphone permission.").assertExists()
        compose.onNodeWithTag("talk-phrases").assertIsNotEnabled()
    }

    @Test fun blockedPermission_offersAppSettings() {
        val p = FakePlatform().apply { granted.remove(AppPermission.NOTIFICATIONS); blocked += AppPermission.NOTIFICATIONS }
        val h = h(platform = p)
        show(h, SettingsPageKey.PERMISSIONS)
        compose.onNodeWithText("Blocked · open app settings", substring = true).assertExists()
        compose.onNodeWithTag("perm:NOTIFICATIONS").performClick()
        compose.onNodeWithTag("rationale:NOTIFICATIONS").assertExists()
        compose.onNodeWithText("Open app settings").assertExists()
        compose.onNodeWithText("Continue").assertDoesNotExist()
    }

    /** Notifications → "Agent session finished": allowed → saves at once; not allowed → the rationale first, still off. */
    @Test fun notifications_agentFinished_savesWhenAllowed_asksOtherwise() {
        val h = h()
        show(h, SettingsPageKey.NOTIFICATIONS)
        compose.onNodeWithTag("notify-agent-turns").assertIsOff().performClick()
        eventually { h.settings.settings.value?.notifyAgentTurns == true }
        waitText("Saved")
        compose.onNodeWithTag("notify-agent-turns").assertIsOn().performClick()
        eventually { h.settings.settings.value?.notifyAgentTurns == false }
    }

    @Test fun notifications_withoutPermission_showsRationale_andStaysOff() {
        val p = FakePlatform().apply { granted.remove(AppPermission.NOTIFICATIONS) }
        val h = h(platform = p)
        show(h, SettingsPageKey.NOTIFICATIONS)
        compose.onNodeWithTag("notify-agent-turns").assertIsOff().performClick()
        compose.onNodeWithTag("rationale:NOTIFICATIONS").assertExists()
        compose.onNodeWithText("Not now").performClick()
        Thread.sleep(200)
        assertEquals(false, h.settings.settings.value?.notifyAgentTurns)
    }

    @Test fun bluetoothOutput_asksForNearbyDevices() {
        val p = FakePlatform().apply {
            granted.remove(AppPermission.NEARBY_DEVICES)
            outputs.value = outputs.value + com.assistant.core.model.AudioOutput.BLUETOOTH
        }
        val h = h(platform = p)
        show(h, SettingsPageKey.AUDIO)
        compose.onNodeWithText("Bluetooth").performClick()
        compose.onNodeWithTag("rationale:NEARBY_DEVICES").assertExists()
        compose.onNodeWithText("Not now").performClick()
        Thread.sleep(200)
        assertEquals(com.assistant.core.model.AudioOutput.LOUDSPEAKER, h.settings.settings.value?.audioOutput)
    }

    @Test fun phrases_saveOnDone_andRefuseBlank() {
        val h = h()
        show(h, SettingsPageKey.WAKE_WORD)
        compose.onNodeWithTag("talk-phrases").performTextReplacement(" , ")
        compose.onNodeWithTag("talk-phrases").performImeAction()
        compose.onNodeWithText("Enter at least one phrase").assertExists()
        compose.onNodeWithTag("talk-phrases").performTextReplacement("hey buddy, my friend")
        compose.onNodeWithTag("talk-phrases").performImeAction()
        eventually { h.settings.settings.value?.talkWord == "hey buddy, my friend" }
        waitText("Saved")
    }

    @Test fun phrases_saveWhenThePageIsLeftWithoutDone() {
        val h = h()
        val open = androidx.compose.runtime.mutableStateOf(true)
        compose.setContent { ArchieTheme { if (open.value) SettingsPageScreen(h.feature, SettingsPageKey.WAKE_WORD, onBack = {}) } }
        compose.waitForIdle()
        compose.onNodeWithTag("talk-phrases").performTextReplacement("my friend, hey friend, listen up")
        open.value = false                              // Back without pressing Done
        compose.waitForIdle()
        eventually { h.settings.settings.value?.talkWord == "my friend, hey friend, listen up" }
    }

    @Test fun home_serverRowsDisabledWhenOffline() {
        val h = h()
        h.connection.state.value = h.connection.state.value.copy(phase = ConnectionStatus.Phase.OFFLINE)
        compose.setContent { ArchieTheme { SettingsScreen(h.feature, onBack = {}, onOpenPage = {}) } }
        compose.waitForIdle()
        compose.onNodeWithTag("settings-row:VOICE").assertIsNotEnabled()
        compose.onNodeWithTag("settings-row:AUDIO").assertIsEnabled()
        compose.onNodeWithText("jetson · offline").assertExists()
    }

    @Test fun home_listsEveryDevicePage_includingNotifications() {
        val h = h()
        var opened: SettingsPageKey? = null
        compose.setContent { ArchieTheme { SettingsScreen(h.feature, onBack = {}, onOpenPage = { opened = it }) } }
        compose.waitForIdle()
        // Every page of the device group must be reachable from the home (Notifications was not, 2026-10-10).
        SettingsPageKey.entries.filter { it.group == SettingsPageKey.Group.DEVICE }.forEach {
            compose.onNodeWithTag("settings-row:${it.name}").assertExists()
        }
        compose.onNodeWithTag("settings-row:NOTIFICATIONS").performClick()
        compose.waitForIdle()
        assert(opened == SettingsPageKey.NOTIFICATIONS) { "opened $opened" }
    }

    @Test fun authGate_pasteFlow_validatesThenSignsIn() {
        val h = h { it.auth = """{"authenticated":false,"auth_url":null,"headless":true}""" }
        compose.setContent { ArchieTheme { com.assistant.archie.feature.settings.ui.AuthGate(h.feature) { } } }
        compose.waitForIdle()
        compose.onNodeWithText("Paste credentials instead").performClick()
        compose.onNodeWithTag("auth-credentials").performTextReplacement("{not json")
        compose.onNodeWithTag("auth-set-credentials").performClick()
        waitText("That isn't valid JSON. Copy the whole file, including the braces.")
        compose.onNodeWithTag("auth-credentials").performTextReplacement("""{"claudeAiOauth":{"accessToken":"sk-x"}}""")
        compose.onNodeWithTag("auth-set-credentials").performClick()
        eventually { h.backend.requests.contains("POST /api/auth/credentials") }
    }

    @Test fun authGate_linkSignIn_showsTheUrl_andSendsTheCode() {
        val h = h { it.auth = """{"authenticated":false,"auth_url":null,"headless":true}""" }
        compose.setContent { ArchieTheme { com.assistant.archie.feature.settings.ui.AuthGate(h.feature) { } } }
        compose.waitForIdle()
        compose.onNodeWithTag("auth-sign-in").performClick()
        compose.waitUntil(10_000) { compose.onAllNodes(hasText("https://claude.com/cai/oauth/authorize?code=true&state=S")).fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithTag("auth-link-code").performTextReplacement("abc#def")
        compose.onNodeWithTag("auth-link-submit").performClick()
        eventually { h.backend.accountWrites.any { it.startsWith("POST /api/accounts/claude/login/code") && it.contains("abc#def") } }
        assertTrue(h.backend.requests.none { it == "POST /api/auth/login" })
    }

    @Test fun accounts_linkFlow_showsTheUrl_andSendsThePastedCode() {
        val h = h()
        show(h, SettingsPageKey.ACCOUNT)
        waitText("Claude Code")
        compose.onNodeWithTag("method:claude:token").performScrollTo().performClick()
        compose.waitUntil(10_000) { compose.onAllNodes(hasText("https://claude.com/cai/oauth/authorize?code=true&state=S")).fetchSemanticsNodes().isNotEmpty() }
        compose.onAllNodes(hasText("replaces CLAUDE_CODE_OAUTH_TOKEN only once the sign-in succeeds", substring = true)).fetchSemanticsNodes().let { assertTrue(it.isNotEmpty()) }
        compose.onNodeWithTag("flow-code").performScrollTo().performTextReplacement("abc#def")
        compose.onNodeWithTag("flow-submit").performScrollTo().performClick()
        eventually { h.backend.accountWrites.any { it.startsWith("POST /api/accounts/claude/login/code") && it.contains("abc#def") } }
    }

    @Test fun accounts_credentialsPaste_validatesLocally_thenShowsTheServerError() {
        val h = h()
        show(h, SettingsPageKey.ACCOUNT)
        waitText("Claude Code")
        compose.onNodeWithTag("method:claude:credentials").performScrollTo().performClick()
        compose.onNodeWithTag("credentials-input").performScrollTo().performTextReplacement("{nope")
        compose.onNodeWithTag("credentials-save").performScrollTo().performClick()
        waitText("That isn't valid JSON. Copy the whole file, including the braces.")
        assertTrue(h.backend.accountWrites.none { it.contains("/credentials") })
        compose.onNodeWithTag("credentials-input").performTextReplacement("""{"other":1}""")
        compose.onNodeWithTag("credentials-save").performScrollTo().performClick()
        waitText("Invalid credentials: the file has no claudeAiOauth.accessToken.")
    }

    @Test fun accounts_envKeys_revealOnDemand() {
        val h = h()
        show(h, SettingsPageKey.ACCOUNT)
        waitText("OPENAI_API_KEY")
        compose.onNodeWithTag("env-value:OPENAI_API_KEY").performScrollTo()
        waitText("••••abcd · 51 chars")
        assertTrue(compose.onAllNodes(hasText("sk-full-secret-value")).fetchSemanticsNodes().isEmpty())
        compose.onNodeWithTag("env-reveal:OPENAI_API_KEY").performScrollTo().performClick()
        waitText("sk-full-secret-value")
        compose.onNodeWithTag("env-reveal:OPENAI_API_KEY").performClick()
        compose.waitUntil(5_000) { compose.onAllNodes(hasText("sk-full-secret-value")).fetchSemanticsNodes().isEmpty() }
        assertEquals(1, h.backend.accountWrites.count { it.startsWith("POST /api/env/OPENAI_API_KEY/reveal") })
        // The Add-key dialog never settles under Robolectric (a Dialog window); its name validation is
        // AccountsModel.envNameError (AccountsModelTest) and the dialog is checked on a device.
    }

    @Test fun sessionSheet_putsOnlyChangedKeys_thenRestarts() {
        var restarted = 0
        val sessions = object : SessionControl {
            override fun session(localId: String) = flowOf(SessionInfo(localId, "SDK1", "Energy dashboard", busy = false))
            override suspend fun restart(localId: String): Boolean { restarted++; return true }
        }
        val h = h(sessions = sessions)
        var dismissed = false
        compose.setContent { ArchieTheme { SessionSettingsSheet(h.feature, "L1", onDismiss = { dismissed = true }) } }
        waitText("Restart")
        compose.onNodeWithTag("wd:/home/rodrigo/assistant").performScrollTo().performClick()
        waitText("Save and restart")
        compose.onNodeWithText("Changes apply after a restart.").assertExists()
        compose.onNodeWithTag("session-restart").performClick()
        eventually { restarted == 1 && dismissed }
        assertEquals("""{"working_directory":"/home/rodrigo/assistant"}""", h.backend.puts.single())
    }

    @Test fun sessionSheet_restartRefusedWhileBusy() {
        val sessions = object : SessionControl {
            override fun session(localId: String) = flowOf(SessionInfo(localId, "SDK1", "x", busy = true))
            override suspend fun restart(localId: String) = true
        }
        val h = h(sessions = sessions)
        compose.setContent { ArchieTheme { SessionSettingsSheet(h.feature, "L1", onDismiss = {}) } }
        waitText("A reply is running: stop it to restart.")
        compose.onNodeWithTag("session-restart").assertIsNotEnabled()
    }

    /** Settings → Agent sessions: the default harness's options save per key; other harnesses collapse. */
    @Test fun agentSessions_harnessOptionSavesOneKey_andOtherHarnessesExpand() {
        val h = h()
        show(h, SettingsPageKey.AGENT_SESSIONS)
        waitText("CLAUDE CODE DEFAULTS")
        // Expand first: the "Saved" snackbar would cover rows at the bottom of the viewport.
        compose.onNodeWithTag("harness-group:codex").performScrollTo().performClick()
        waitText("Codex: check the setup")
        compose.onNodeWithTag("harness:codex:verbosity", useUnmergedTree = true).assertExists()

        // Reasoning effort is a levels row now (catalog `ordered`): Default · Low … Max.
        compose.onNodeWithTag("harness:claude:effort:seg:max").performScrollTo().performClick()
        eventually { h.backend.puts.lastOrNull() == """{"harness_options":{"claude":{"effort":"max"}}}""" }
        waitText("Saved")
        compose.waitUntil(5_000) { runCatching { compose.onNodeWithTag("harness:claude:effort:seg:max").assertIsSelected() }.isSuccess }
        compose.onNodeWithTag("harness-refresh").performScrollTo().performClick()
        eventually { h.backend.requests.contains("GET /api/config/harnesses?refresh=true") }
    }

    /**
     * Option controls on the global page: the budget is disabled (with the reason) until Thinking is
     * Fixed budget; the number field commits on Done, clamped; a switch saves and "Use default"
     * drops the key; Claude in Chrome is the Claude Code block's last row.
     */
    @Test fun agentSessions_controls_requiresSwitchAndNumberField() {
        val h = h()
        show(h, SettingsPageKey.AGENT_SESSIONS)
        waitText("CLAUDE CODE DEFAULTS")
        compose.onNodeWithText("Applies when Thinking is Fixed budget").performScrollTo().assertExists()
        compose.onNodeWithTag("harness:claude:thinking_budget:value").assertIsNotEnabled()
        compose.onNodeWithTag("harness:claude:effort:dot:max", useUnmergedTree = true).assertDoesNotExist()

        compose.onNodeWithTag("harness:claude:thinking:seg:enabled").performScrollTo().tap()
        eventually(message = { h.backend.puts.toString() }) { h.backend.puts.lastOrNull() == """{"harness_options":{"claude":{"thinking":"enabled"}}}""" }
        compose.waitUntil(30_000) { runCatching { compose.onNodeWithTag("harness:claude:thinking_budget:value").assertIsEnabled() }.isSuccess }
        compose.onNodeWithTag("harness:claude:thinking_budget:value").performScrollTo().performTextReplacement("500000")
        compose.onNodeWithTag("harness:claude:thinking_budget:value").performImeAction()
        eventually(message = { h.backend.puts.toString() }) { h.backend.puts.lastOrNull() == """{"harness_options":{"claude":{"thinking_budget":128000}}}""" }

        compose.onNodeWithTag("harness:claude:todo_tools:switch").performScrollTo().assertIsOn().tap()
        eventually(message = { h.backend.puts.toString() }) { h.backend.puts.lastOrNull() == """{"harness_options":{"claude":{"todo_tools":false}}}""" }
        compose.waitUntil(30_000) { compose.onAllNodesWithTag("harness:claude:todo_tools:use-default").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithTag("harness:claude:todo_tools:use-default").performScrollTo().tap()
        eventually(message = { h.backend.puts.toString() }) { h.backend.puts.lastOrNull() == """{"harness_options":{"claude":{"todo_tools":null}}}""" }
        compose.onNodeWithTag("chrome").performScrollTo().assertIsOn()
    }

    /** Gemini's budget presets: Default · Dynamic · Off · Custom; Custom saves its start value at once. */
    @Test fun agentSessions_presets_customSavesTheStartValue() {
        val h = h()
        show(h, SettingsPageKey.AGENT_SESSIONS)
        compose.onNodeWithTag("harness-group:gemini").performScrollTo().tap()
        compose.onNodeWithTag("harness:gemini:thinking_budget:seg:preset:-1").performScrollTo().tap()
        eventually(message = { h.backend.puts.toString() }) { h.backend.puts.lastOrNull() == """{"harness_options":{"gemini":{"thinking_budget":-1}}}""" }
        compose.waitUntil(30_000) { runCatching { compose.onNodeWithTag("harness:gemini:thinking_budget:seg:preset:-1").assertIsSelected() }.isSuccess }
        compose.onNodeWithTag("harness:gemini:thinking_budget:seg:__custom__").performScrollTo().tap()
        eventually(message = { h.backend.puts.toString() }) { h.backend.puts.lastOrNull() == """{"harness_options":{"gemini":{"thinking_budget":8192}}}""" }
        compose.waitUntil(30_000) { compose.onAllNodesWithTag("harness:gemini:thinking_budget:slider").fetchSemanticsNodes().isNotEmpty() }
    }

    /** Session sheet: switching the harness resets model + options to inherit, and Save sends them. */
    @Test fun sessionSheet_harnessChangeResetsModelAndOptions() {
        val sessions = object : SessionControl {
            override fun session(localId: String) = flowOf(SessionInfo(localId, "SDK1", "x", busy = false))
            override suspend fun restart(localId: String) = true
        }
        val h = h(sessions = sessions) {
            it.sessionConfig = """{"working_directory":null,"enabled_mcps":null,"chrome_extension":null,"provider":"claude","harness_model":"opus","harness_options":{"effort":"max"}}"""
        }
        compose.setContent { ArchieTheme { SessionSettingsSheet(h.feature, "L1", onDismiss = {}) } }
        waitText("Restart")
        compose.onNodeWithText("Opus", substring = false).performScrollTo().assertExists()
        compose.onNodeWithTag("chrome").performScrollTo().assertIsOn() // Claude Code only
        compose.onNodeWithText("Default from Settings (On)").assertExists()
        compose.onNodeWithTag("select:Harness").performScrollTo().performClick()
        compose.onNodeWithTag("option:codex").performClick()
        waitText("Default (CLI default (GPT-6-Luna))")
        compose.onNodeWithTag("harness:codex:effort", useUnmergedTree = true).assertExists()
        compose.onNodeWithTag("chrome").assertDoesNotExist()
        compose.onNodeWithTag("session-save").performClick()
        eventually { h.backend.puts.lastOrNull() == """{"provider":"codex","harness_model":null,"harness_options":null}""" }
    }
}

/** A click through the semantics action: the "Saved" snackbar may cover a row scrolled to the bottom edge. */
private fun SemanticsNodeInteraction.tap(): SemanticsNodeInteraction = performSemanticsAction(SemanticsActions.OnClick)
