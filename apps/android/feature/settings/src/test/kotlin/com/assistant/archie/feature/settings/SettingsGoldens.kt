package com.assistant.archie.feature.settings

import android.app.Application
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.unit.dp
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.emptyPreferences
import androidx.datastore.preferences.core.mutablePreferencesOf
import androidx.datastore.preferences.core.stringPreferencesKey
import com.assistant.archie.feature.settings.ui.AuthGate
import com.assistant.archie.feature.settings.ui.SessionSettingsSheet
import com.assistant.archie.feature.settings.ui.SettingsPageKey
import com.assistant.archie.feature.settings.ui.SettingsPageScreen
import com.assistant.archie.feature.settings.ui.SettingsScreen
import com.assistant.core.data.ConnectionStatus
import com.assistant.core.design.theme.ArchieTheme
import com.assistant.core.design.theme.ThemeMode
import com.assistant.core.model.AudioOutput
import com.assistant.core.network.DiscoveredServer
import com.github.takahirom.roborazzi.ExperimentalRoborazziApi
import com.github.takahirom.roborazzi.captureScreenRoboImage
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode

/**
 * Roborazzi goldens of every settings page, dark and light, against the live Jetson's settings
 * (served by MockWebServer). Compared with the approved mockups phone (e) Settings home and (f)
 * Audio. Scene names: `settings-<page>_<size>_<theme>.png`.
 *
 *   flock /tmp/archie-locks/gradle.lock ./gradlew :feature:settings:recordRoborazziDebug   record
 *   flock /tmp/archie-locks/gradle.lock ./gradlew :feature:settings:verifyRoborazziDebug   compare
 */
@OptIn(ExperimentalRoborazziApi::class)
abstract class SettingsGoldenBase(private val size: String) {
    @get:Rule val compose = createComposeRule()
    private var harness: Harness? = null

    @After fun tearDown() { harness?.close() }

    protected fun harness(
        prefs: Preferences = DEVICE,
        platform: FakePlatform = FakePlatform(),
        sessions: SessionControl = SessionControl.None,
        configure: (SettingsBackend) -> Unit = {},
    ): Harness {
        val backend = SettingsBackend().start().also(configure)
        return Harness(backend, prefs, platform, sessions = sessions).also { h ->
            harness = h
            runBlocking {
                h.feature.server.refreshNow()
                h.feature.auth.checkNow()
            }
        }
    }

    protected fun scene(name: String, h: Harness, before: (Harness) -> Unit = {}, content: @Composable () -> Unit) {
        var mode by mutableStateOf(ThemeMode.Dark)
        compose.setContent {
            ArchieTheme(mode = mode, reduceMotion = true) {
                Column(Modifier.fillMaxSize().background(ArchieTheme.colors.surface)) {
                    Spacer(Modifier.height(32.dp)) // mockup `.sbar`
                    content()
                }
            }
        }
        compose.waitForIdle()
        before(h)
        for (theme in listOf(ThemeMode.Dark, ThemeMode.Light)) {
            mode = theme
            Thread.sleep(400) // the screen's own refresh is real HTTP: let it land
            compose.mainClock.advanceTimeBy(1_000)
            compose.waitForIdle()
            captureScreenRoboImage("$DIR/${name}_${size}_${theme.name.lowercase()}.png")
        }
    }

    companion object {
        const val DIR = "src/test/screenshots"

        /** The POCO's device settings as in the mockups: saved jetson + laptop, wake word on. */
        val DEVICE: Preferences = mutablePreferencesOf().apply {
            this[stringPreferencesKey("server_url")] = "ws://192.168.0.200:80"
            this[stringPreferencesKey("saved_servers_v2")] =
                """[{"label":"jetson","url":"ws://192.168.0.200:80"},{"label":"laptop","url":"ws://192.168.0.28:8765"}]"""
            this[stringPreferencesKey("theme_mode")] = "DARK"
            this[stringPreferencesKey("audio_output")] = "LOUDSPEAKER"
            this[booleanPreferencesKey("enable_wake_word")] = true
            this[stringPreferencesKey("realtime_wake_word")] = "hey archie, wake up"
        }
    }
}

@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@Config(sdk = [36], qualifiers = "w443dp-h986dp-xxhdpi", application = Application::class)
class CompactSettingsGoldens : SettingsGoldenBase("compact") {
    private fun page(p: SettingsPageKey, name: String, h: Harness = harness(), before: (Harness) -> Unit = {}) =
        scene(name, h, before) { SettingsPageScreen(h.feature, p, onBack = {}) }

    /** Mockup phone (e). */
    @Test fun home() {
        val h = harness(platform = FakePlatform().apply { assistant = true })
        scene("settings-home", h) { SettingsScreen(h.feature, onBack = {}, onOpenPage = {}) }
    }

    /** Mockup phone (f), with the "Saved" snackbar of a slider release. */
    @Test fun audio() = page(SettingsPageKey.AUDIO, "settings-audio", before = { it.feature.messages.saved() })

    @Test fun connection() {
        val h = harness()
        h.connection.state.value = h.connection.state.value.copy(discovered = listOf(DiscoveredServer("192.168.0.31", 8765, false)))
        page(SettingsPageKey.CONNECTION, "settings-connection", h)
    }

    @Test fun wakeWord() = page(SettingsPageKey.WAKE_WORD, "settings-wake-word")
    @Test fun appearance() = page(SettingsPageKey.APPEARANCE, "settings-appearance")
    @Test fun notifications() = page(SettingsPageKey.NOTIFICATIONS, "settings-notifications")

    @Test fun permissions() {
        val p = FakePlatform().apply { granted.remove(AppPermission.NOTIFICATIONS); blocked += AppPermission.NOTIFICATIONS }
        page(SettingsPageKey.PERMISSIONS, "settings-permissions", harness(platform = p))
    }

    @Test fun background() = page(SettingsPageKey.BACKGROUND, "settings-background")
    @Test fun conversationModel() = page(SettingsPageKey.CONVERSATION_MODEL, "settings-conversation-model")
    @Test fun voice() = page(SettingsPageKey.VOICE, "settings-voice")
    @Test fun voiceTuning() = page(SettingsPageKey.VOICE_TUNING, "settings-voice-tuning")
    @Test fun agentSessions() = page(SettingsPageKey.AGENT_SESSIONS, "settings-agent-sessions")
    @Test fun workingDirectories() = page(SettingsPageKey.WORKING_DIRECTORIES, "settings-working-directories")

    /** Three servers, two on: the "only the servers switched on" semantics. */
    @Test fun mcpServers() = page(SettingsPageKey.MCP_SERVERS, "settings-mcp-servers", harness { b ->
        b.mcp = """{"servers":{"chrome-devtools":{"command":"/usr/bin/npx","args":["-y","chrome-devtools-mcp@latest"]},"github":{"command":"github-mcp-server","args":["stdio"]},"posthog":{"url":"https://mcp.posthog.com/sse"}}}"""
        b.config = kotlinx.serialization.json.JsonObject(b.config + ("enabled_mcps" to kotlinx.serialization.json.JsonArray(listOf("chrome-devtools", "github").map { kotlinx.serialization.json.JsonPrimitive(it) })))
    })

    /** Headless server, signed out: the paste-credentials flow. */
    @Test fun account() = page(SettingsPageKey.ACCOUNT, "settings-account", harness())

    @Test fun about() = page(SettingsPageKey.ABOUT, "settings-about")

    @Test fun sessionSheet() {
        val sessions = object : SessionControl {
            override fun session(localId: String) = flowOf(SessionInfo(localId, "SDK1", "Energy dashboard", busy = false))
            override suspend fun restart(localId: String) = true
        }
        val h = harness(sessions = sessions) { it.sessionConfig = """{"working_directory":"/home/rodrigo/assistant","enabled_mcps":null,"chrome_extension":null,"provider":null,"harness_model":null}""" }
        scene("settings-session-sheet", h) { SessionSettingsSheet(h.feature, "L1", onDismiss = {}) }
    }

    @Test fun authGate() {
        val h = harness { it.auth = """{"authenticated":false,"auth_url":null,"headless":false}""" }
        scene("settings-auth-gate", h) { AuthGate(h.feature) { } }
    }
}

@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@Config(sdk = [36], qualifiers = "w1280dp-h800dp-mdpi", application = Application::class)
class ExpandedSettingsGoldens : SettingsGoldenBase("expanded") {
    /** IA §7: two-pane list/detail on Expanded. */
    @Test fun home() {
        val h = harness(platform = FakePlatform().apply { assistant = true })
        scene("settings-home", h) { SettingsScreen(h.feature, onBack = {}, onOpenPage = {}) }
    }
}
