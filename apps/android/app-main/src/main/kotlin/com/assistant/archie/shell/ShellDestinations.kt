package com.assistant.archie.shell

import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.compose.runtime.Stable
import androidx.compose.ui.Modifier
import androidx.lifecycle.viewmodel.compose.viewModel
import com.assistant.archie.feature.chat.ConversationViewModel
import com.assistant.archie.feature.chat.RepositoryChatBackend
import com.assistant.archie.feature.chat.conversationViewModelFactory
import com.assistant.archie.feature.chat.ui.CatalogToolCardRenderer
import com.assistant.archie.feature.chat.ui.ConversationCallbacks
import com.assistant.archie.feature.chat.ui.ConversationScreen
import com.assistant.archie.feature.memory.ui.MemoryDocumentContent
import com.assistant.archie.feature.memory.ui.MemoryDocumentScreen
import com.assistant.archie.feature.visuals.ui.VisualViewer
import com.assistant.archie.feature.settings.ui.SettingsPageKey
import com.assistant.archie.feature.sessions.SessionsController
import com.assistant.archie.feature.sessions.SessionsIntent
import com.assistant.archie.graph.MainAppGraph
import com.assistant.core.data.ConversationKey
import com.assistant.core.data.ItemKey
import com.assistant.core.model.DeviceSettings
import com.assistant.core.model.SessionKind
import com.assistant.core.network.UrlScheme

/**
 * The content the shell hosts (spec 14 §7: "navigation destinations as clearly-marked placeholder
 * composables"). The shell owns chrome, navigation and focus; a destination owns its screen. Later
 * WPs (B-04…B-08) replace the bodies of [GraphDestinations] with their feature screens.
 */
@Stable
interface ShellDestinations {
    /** The active workspace item's content (conversation, or a memory doc / visual opened as a tab). */
    @Composable fun WorkspaceItemContent(key: ItemKey, modifier: Modifier)

    @Composable fun MemoryScreen(onBack: () -> Unit, onOpenDoc: (String) -> Unit)
    @Composable fun MemoryDocScreen(path: String, onBack: () -> Unit)

    /** A memory document whose links to other files open [onOpenDoc] (B-07); defaults to the link-less screen. */
    @Composable fun MemoryDocScreen(path: String, onBack: () -> Unit, onOpenDoc: (String) -> Unit) = MemoryDocScreen(path, onBack)

    /** ...and whose visualization links open [onOpenVisual] (spec 12 §9.4); defaults to the screen above. */
    @Composable fun MemoryDocScreen(path: String, onBack: () -> Unit, onOpenDoc: (String) -> Unit, onOpenVisual: (String) -> Unit) =
        MemoryDocScreen(path, onBack, onOpenDoc)
    @Composable fun VisualsScreen(onBack: () -> Unit, onOpen: (String) -> Unit)
    @Composable fun VisualScreen(path: String, onBack: () -> Unit)
    @Composable fun SettingsScreen(onBack: () -> Unit, onOpenPage: (SettingsPageId) -> Unit)
    @Composable fun SettingsPageScreen(id: SettingsPageId, onBack: () -> Unit)
    @Composable fun SessionSettingsScreen(localId: String, onBack: () -> Unit)

    /** Expanded / Medium list pane bodies for the Memory and Visuals rail destinations (B-07). */
    @Composable fun MemoryPane(onOpenDoc: (String) -> Unit, modifier: Modifier)
    @Composable fun VisualsPane(onOpen: (String) -> Unit, modifier: Modifier)
}

/**
 * Production destinations, fed by the process-scoped graph. [sessions] is the
 * shell's B-06 controller, so the conversation screen's "open a session" / "new agent session"
 * entry points go through the same flows (an Archie fork or continuation asks first, §6.11).
 */
class GraphDestinations(private val graph: MainAppGraph, private val sessions: SessionsController? = null) : ShellDestinations {
    @Composable
    override fun WorkspaceItemContent(key: ItemKey, modifier: Modifier) {
        when (key) {
            ItemKey.Archie -> Conversation(ConversationKey.ARCHIE, true, modifier)
            is ItemKey.Agent -> Conversation(key.conversation, false, modifier)
            // B-07: a memory document / visual opened as a tab (IA §9.2); links open further tabs.
            is ItemKey.Memory -> MemoryDocumentContent(
                rememberMemoryDeps(graph), key.path, onOpenDoc = { graph.openSessions.openMemory(it) }, modifier,
            )
            is ItemKey.Visual -> VisualViewer(rememberVisualsDeps(graph), key.path, modifier)
        }
    }

    /**
     * B-04's conversation screen (`:feature:chat`), one ViewModel per conversation key, fed by B-03's
     * process-scoped repositories. Voice comes from the process-scoped voice host (`graph.chatVoice`, B-09).
     */
    @Composable
    private fun Conversation(key: ConversationKey, archie: Boolean, modifier: Modifier) {
        val item = if (archie) ItemKey.Archie else ItemKey.Agent(key)
        val vm: ConversationViewModel = viewModel(
            key = "conversation:${key.value}",
            factory = conversationViewModelFactory(
                backend = { RepositoryChatBackend(key, graph.conversations, graph.uploads) },
                voice = graph.chatVoice, // B-09: the real voice host (HostChatVoice)
                voiceDock = graph.voiceDock, // one dock state, shared with the floating controls
                toolCards = CatalogToolCardRenderer,
                title = { graph.openSessions.items.value.firstOrNull { it.key == item }?.title },
            ),
        )
        ConversationScreen(
            viewModel = vm,
            modifier = modifier,
            toolCards = CatalogToolCardRenderer,
            callbacks = ConversationCallbacks(
                // Spec 12 §9.4: visualization / memory links (and printed paths) open in the app.
                onLink = rememberChatLinkHandler(graph),
                linkOrigin = UrlScheme.httpBase(graph.settings.settings.value?.serverUrl ?: DeviceSettings.DEFAULT_SERVER_URL),
                onOpenSession = { ref ->
                    val sdk = ref.sdkId
                    if (sessions != null && ref.kind == SessionKind.ORCHESTRATOR && sdk != null) sessions.requestResumeArchie(sdk)
                    else graph.openSessions.openRef(ref)
                },
                onNewAgentSession = { sessions?.onIntent(SessionsIntent.NewAgent) ?: graph.openSessions.newAgentSession() },
            ),
        )
    }

    /** B-07 `:feature:memory` (spec 14 §4.1): the tree, full screen on Compact. */
    @Composable
    override fun MemoryScreen(onBack: () -> Unit, onOpenDoc: (String) -> Unit) =
        com.assistant.archie.feature.memory.ui.MemoryScreen(rememberMemoryDeps(graph), onBack, onOpenDoc)

    @Composable
    override fun MemoryDocScreen(path: String, onBack: () -> Unit) = MemoryDocScreen(path, onBack) { graph.openSessions.openMemory(it) }

    @Composable
    override fun MemoryDocScreen(path: String, onBack: () -> Unit, onOpenDoc: (String) -> Unit) =
        MemoryDocumentScreen(rememberMemoryDeps(graph), path, onBack, onOpenDoc)

    @Composable
    override fun MemoryDocScreen(path: String, onBack: () -> Unit, onOpenDoc: (String) -> Unit, onOpenVisual: (String) -> Unit) =
        MemoryDocumentScreen(rememberMemoryDeps(graph), path, onBack, onOpenDoc, onOpenVisual = onOpenVisual)

    /** B-07 `:feature:visuals` (spec 14 §4.2): the list and the full-screen in-app WebView on Compact. */
    @Composable
    override fun VisualsScreen(onBack: () -> Unit, onOpen: (String) -> Unit) =
        com.assistant.archie.feature.visuals.ui.VisualsScreen(rememberVisualsDeps(graph), onBack, onOpen)

    @Composable
    override fun VisualScreen(path: String, onBack: () -> Unit) =
        com.assistant.archie.feature.visuals.ui.VisualScreen(rememberVisualsDeps(graph), path, onBack)

    /** B-08 `:feature:settings` (IA §7). Page ids map by name onto the feature's [SettingsPageKey]. */
    @Composable
    override fun SettingsScreen(onBack: () -> Unit, onOpenPage: (SettingsPageId) -> Unit) =
        com.assistant.archie.feature.settings.ui.SettingsScreen(
            rememberSettingsFeature(graph), onBack, onOpenPage = { onOpenPage(SettingsPageId.valueOf(it.name)) },
        )

    @Composable
    override fun SettingsPageScreen(id: SettingsPageId, onBack: () -> Unit) =
        com.assistant.archie.feature.settings.ui.SettingsPageScreen(rememberSettingsFeature(graph), SettingsPageKey.valueOf(id.name), onBack)

    /** B-08 session settings: a bottom sheet (Compact) / side sheet over the conversation (a dialog scene). */
    @Composable
    override fun SessionSettingsScreen(localId: String, onBack: () -> Unit) =
        com.assistant.archie.feature.settings.ui.SessionSettingsSheet(rememberSettingsFeature(graph), localId, onDismiss = onBack)

    /** B-07 list panes (Medium/Expanded): the row of the active tab is highlighted. */
    @Composable
    override fun MemoryPane(onOpenDoc: (String) -> Unit, modifier: Modifier) {
        val active by graph.openSessions.active.collectAsStateWithLifecycle()
        com.assistant.archie.feature.memory.ui.MemoryPane(rememberMemoryDeps(graph), onOpenDoc, (active as? ItemKey.Memory)?.path, modifier)
    }

    @Composable
    override fun VisualsPane(onOpen: (String) -> Unit, modifier: Modifier) {
        val active by graph.openSessions.active.collectAsStateWithLifecycle()
        com.assistant.archie.feature.visuals.ui.VisualsPane(rememberVisualsDeps(graph), onOpen, (active as? ItemKey.Visual)?.path, modifier)
    }
}
