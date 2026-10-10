package com.assistant.archie.shell

import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.platform.LocalContext
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.snapshotFlow
import androidx.core.splashscreen.SplashScreen.Companion.installSplashScreen
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import androidx.navigation3.runtime.rememberNavBackStack
import com.assistant.archie.feature.chat.VoiceOverlayModel
import com.assistant.archie.feature.chat.ui.ComposerBounds
import com.assistant.archie.feature.chat.ui.LocalComposerBounds
import com.assistant.archie.feature.chat.ui.OverlayAnchor
import com.assistant.archie.feature.chat.ui.VoiceOverlayActivity
import com.assistant.archie.graph.GraphOwner
import com.assistant.archie.graph.MainAppGraph
import com.assistant.archie.system.ShellCommand
import com.assistant.archie.system.SystemApprovalSink
import com.assistant.archie.system.SystemIntents
import com.assistant.archie.system.SystemTurnSink
import com.assistant.archie.system.SystemOverlays
import com.assistant.archie.system.applyAppNightMode
import com.assistant.core.data.ConversationEvent
import com.assistant.core.data.SharePayload
import com.assistant.core.model.SessionKind
import com.assistant.core.model.SessionRef
import com.assistant.core.design.theme.ArchieTheme
import com.assistant.archie.feature.settings.ui.AuthGate
import com.assistant.archie.feature.settings.ui.ProvideTextSize
import kotlinx.coroutines.launch

/**
 * The single Activity (spec 14 §2.8). It holds no domain state: everything lives in the
 * process-scoped [MainAppGraph], so finishing or recreating the Activity never closes a session
 * (decision P-1) and never loses chat or voice state (inv03 §0).
 *
 * Seams kept for B-09 (inv03 §1.1): share intents land in `graph.share` (a StateFlow, so a cold-launch
 * share is not lost) and the share sheet (`SystemOverlays`) takes them; wake-word callbacks and the
 * screen-on flags for wake triggers go through the voice host, not this Activity (wake → voice runs
 * headless, spec 14 §2.6); the launch effects (auto-connect, scan on the default URL) run in
 * `ConnectionRepository.start()`.
 */
class MainActivity : ComponentActivity() {
    private val graph: MainAppGraph get() = (application as GraphOwner).graph

    override fun onCreate(savedInstanceState: Bundle?) {
        val splash = installSplashScreen()
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        // Nobody acts on default settings (inv04 B4): keep the splash until DataStore has loaded.
        splash.setKeepOnScreenCondition { graph.settings.settings.value == null }
        graph.connection.start()
        if (savedInstanceState == null) handleIntent(intent)
        setContent { ArchieApp(graph) }
    }

    // "Looking at it" (AN-1, agent-finished and approval notifications) follows the activity being
    // resumed: it ends the moment Home is pressed, the shade is pulled or the screen locks. The
    // process lifecycle's ON_STOP arrives ~1 s later, and a turn finishing in that gap was wrongly
    // treated as seen (2026-10-10).
    override fun onResume() {
        super.onResume()
        graph.approvals.foreground.value = true
    }

    override fun onPause() {
        graph.approvals.foreground.value = false
        super.onPause()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleIntent(intent)
    }

    private fun handleIntent(intent: Intent?) {
        ShellIntents.parseShare(intent)?.let { graph.share.offer(it) }
        // OI-6: a tap on an approval or "agent finished" notification focuses that agent session.
        intent?.getStringExtra(SystemApprovalSink.EXTRA_OPEN_AGENT)?.let { localId ->
            val sdkId = intent.getStringExtra(SystemTurnSink.EXTRA_OPEN_AGENT_SDK)
            intent.removeExtra(SystemApprovalSink.EXTRA_OPEN_AGENT)
            intent.removeExtra(SystemTurnSink.EXTRA_OPEN_AGENT_SDK)
            graph.approvals.requestOpen(localId, sdkId)
        }
        // B-09: launcher shortcuts, and a voice start deferred until the mic is allowed (tile /
        // assist / shortcut without RECORD_AUDIO land here; spec 14 §2.8-§2.9).
        SystemIntents.handleInMain(intent, graph.commands) { trigger ->
            graph.mic.withMicrophone { graph.voiceHost?.startVoice(trigger) }
        }
    }
}

/** The app's root composable: theme from device settings, the shell, and its ViewModel. */
@Composable
fun ArchieApp(graph: MainAppGraph) {
    val vm: ShellViewModel = viewModel(factory = viewModelFactory { initializer { ShellViewModel(graph) } })
    val state by vm.state.collectAsStateWithLifecycle()
    val backStack = rememberNavBackStack(Workspace)
    val destinations = remember(graph, vm) { GraphDestinations(graph, vm.sessions) }
    // B-08: Appearance → text size / reduce motion, and the AuthGate over the shell.
    val settings = rememberSettingsFeature(graph)
    val appearance by settings.device.appearance.collectAsStateWithLifecycle()
    // B-09: the system splash of the next cold start follows the app's theme (API 31+), applied
    // only once the real settings are loaded (never the pre-load default).
    val context = LocalContext.current
    val loadedTheme = graph.settings.settings.collectAsStateWithLifecycle().value?.themeMode
    LaunchedEffect(loadedTheme) { loadedTheme?.let { applyAppNightMode(context, it) } }
    // OI-6: approval notifications only for views the user is not looking at (AN-1), and a tap on one
    // returns to the workspace with that agent focused.
    LaunchedEffect(backStack) {
        snapshotFlow { backStack.lastOrNull() == Workspace }.collect { graph.approvals.workspaceOnTop.value = it }
    }
    val openRequest by graph.approvals.openRequest.collectAsStateWithLifecycle()
    LaunchedEffect(openRequest) {
        val req = graph.approvals.takeOpenRequest() ?: return@LaunchedEffect
        while (backStack.size > 1) backStack.removeAt(backStack.lastIndex)
        if (!graph.openSessions.openAgentByLocalId(req.localId) && req.sdkId != null) {
            // It left the pool (closed, backend restart): reopen it from history (`start{resume_sdk_id}`).
            graph.openSessions.openRef(SessionRef(req.localId, req.sdkId, SessionKind.AGENT, provider = null))
        }
    }
    // §6.11a: Archie switched to a past conversation at the user's request; the Archie view is
    // focused by OpenSessionsRepository, and a screen on top of the workspace (Settings, …) goes.
    LaunchedEffect(graph) {
        graph.conversations.events.collect { e ->
            if (e is ConversationEvent.ArchieSwitched) while (backStack.size > 1) backStack.removeAt(backStack.lastIndex)
        }
    }
    // The floating voice controls (over every view but the Archie conversation while a call runs).
    val overlayScope = rememberCoroutineScope()
    val overlayModel = remember(graph) { VoiceOverlayModel(graph.voiceDock, overlayScope) }
    val overlayActivity = remember { VoiceOverlayActivity() }
    val composerBounds = remember { ComposerBounds() }
    val anchorKey = graph.settings.settings.collectAsStateWithLifecycle().value?.voiceOverlayAnchor
    val voiceOverlay = ShellVoiceOverlay(
        overlayModel, overlayActivity, composerBounds,
        anchor = OverlayAnchor.parse(anchorKey),
        onAnchorChange = { a -> graph.scope.launch { graph.settings.setVoiceOverlayAnchor(a.key) } },
    )
    ArchieTheme(mode = state.themeMode.toDesign(), reduceMotion = appearance.reduceMotion) {
        ProvideTextSize(appearance.textSize) {
            CompositionLocalProvider(LocalComposerBounds provides composerBounds) {
                AuthGate(settings) { ArchieShell(state, vm::onAction, backStack, destinations, voiceOverlay = voiceOverlay) }
            }
        }
        // B-09: system-bar icon contrast (OI-1), share sheet, mic rationale, shortcut commands.
        SystemOverlays(graph) { cmd ->
            when (cmd) {
                ShellCommand.NEW_ARCHIE -> vm.onAction(ShellAction.NewArchie)
                ShellCommand.NEW_AGENT -> vm.onAction(ShellAction.NewAgent)
            }
        }
    }
}

/** Intent parsing for the shell (pure enough to unit-test with Robolectric intents). */
object ShellIntents {
    /** `SEND` text → text; `SEND`/`SEND_MULTIPLE` streams → files (inv03 §1.8 adds multiple files). */
    fun parseShare(intent: Intent?): SharePayload? {
        intent ?: return null
        val subject = intent.getStringExtra(Intent.EXTRA_SUBJECT)
        return when (intent.action) {
            Intent.ACTION_SEND -> {
                val stream = intent.streamExtra()
                val text = intent.getStringExtra(Intent.EXTRA_TEXT)
                when {
                    stream != null -> SharePayload.Files(listOf(stream.toString()), subject)
                    !text.isNullOrBlank() -> SharePayload.Text(text, subject)
                    else -> null
                }
            }
            Intent.ACTION_SEND_MULTIPLE -> {
                val uris = intent.streamListExtra().map { it.toString() }
                if (uris.isEmpty()) null else SharePayload.Files(uris, subject)
            }
            else -> null
        }
    }

    @Suppress("DEPRECATION")
    private fun Intent.streamExtra(): Uri? =
        if (Build.VERSION.SDK_INT >= 33) getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java) else getParcelableExtra(Intent.EXTRA_STREAM)

    @Suppress("DEPRECATION")
    private fun Intent.streamListExtra(): List<Uri> =
        (if (Build.VERSION.SDK_INT >= 33) getParcelableArrayListExtra(Intent.EXTRA_STREAM, Uri::class.java) else getParcelableArrayListExtra(Intent.EXTRA_STREAM))
            .orEmpty()
}
