package com.assistant.archie.feature.settings.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import com.assistant.core.design.icons.ArchieIcon
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.WindowInsetsSides
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.only
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.assistant.archie.feature.settings.AppPermission
import com.assistant.archie.feature.settings.Format
import com.assistant.archie.feature.settings.NotifyLogic
import com.assistant.archie.feature.settings.McpLogic
import com.assistant.archie.feature.settings.ModelLogic
import com.assistant.archie.feature.settings.SettingsFeature
import com.assistant.archie.feature.settings.VoiceLogic
import com.assistant.archie.feature.settings.WorkingDirectoryLogic
import com.assistant.core.data.ConnectionRepository
import com.assistant.core.data.ConnectionStatus
import com.assistant.core.design.components.ArchieIconButton
import com.assistant.core.design.components.ArchieSnackbarHost
import com.assistant.core.design.components.ArchieTextField
import com.assistant.core.design.components.ArchieTopAppBar
import com.assistant.core.design.components.DotTone
import com.assistant.core.design.components.SettingsGroup
import com.assistant.core.design.components.SettingsGroupHeader
import com.assistant.core.design.components.SettingsRow
import com.assistant.core.design.components.StatusDot
import com.assistant.core.design.icons.ArchieIcons
import com.assistant.core.design.theme.ArchieTheme
import com.assistant.core.model.AudioOutput
import com.assistant.core.model.ThemeMode

/** The IA §7 pages, plus the two sub-pages of spec 14 §2.6 / §2.9. */
enum class SettingsPageKey(val title: String, val group: Group) {
    CONNECTION("Connection", Group.DEVICE),
    AUDIO("Audio", Group.DEVICE),
    WAKE_WORD("Wake word & triggers", Group.DEVICE),
    APPEARANCE("Appearance", Group.DEVICE),
    NOTIFICATIONS("Notifications", Group.DEVICE),
    PERMISSIONS("Permissions", Group.DEVICE),
    CONVERSATION_MODEL("Conversation model", Group.SERVER),
    VOICE("Voice", Group.SERVER),
    VOICE_TUNING("Voice tuning", Group.SERVER),
    AGENT_SESSIONS("Agent sessions", Group.SERVER),
    WORKING_DIRECTORIES("Working directories", Group.SERVER),
    MCP_SERVERS("MCP servers", Group.SERVER),
    ACCOUNT("Accounts", Group.SERVER),
    ABOUT("About Archie", Group.ABOUT),

    /** Wake word & triggers → Background (spec 14 §2.6). Not on the home list. */
    BACKGROUND("Background reliability", Group.SUB),
    ;

    enum class Group { DEVICE, SERVER, ABOUT, SUB }

    val icon: ImageVector
        get() = when (this) {
            CONNECTION -> ArchieIcons.Lan
            AUDIO -> ArchieIcons.VolumeUp
            WAKE_WORD -> ArchieIcons.Hearing
            APPEARANCE -> ArchieIcons.Palette
            NOTIFICATIONS -> SettingsIcons.Notifications
            PERMISSIONS -> ArchieIcons.Shield
            CONVERSATION_MODEL -> ArchieIcons.Forum
            VOICE -> ArchieIcons.RecordVoiceOver
            VOICE_TUNING -> ArchieIcons.Tune
            AGENT_SESSIONS -> ArchieIcons.SmartToy
            WORKING_DIRECTORIES -> ArchieIcons.Folder
            MCP_SERVERS -> ArchieIcons.Hub
            ACCOUNT -> ArchieIcons.AccountCircle
            ABOUT -> ArchieIcons.Info
            BACKGROUND -> SettingsIcons.BatteryFull
        }
}

/** Two-pane list/detail from this width (IA §7, Expanded ≥ 840 dp). */
private val TWO_PANE = 840.dp

/**
 * Settings → home (mockup phone (e)). On Expanded it is a two-pane list/detail and opening a row
 * selects it in place; otherwise [onOpenPage] pushes the page.
 */
@Composable
fun SettingsScreen(feature: SettingsFeature, onBack: (() -> Unit)?, onOpenPage: (SettingsPageKey) -> Unit, modifier: Modifier = Modifier) {
    LaunchedEffect(feature) {
        feature.server.refresh()
        feature.auth.check()
    }
    LifecycleResumeEffect(feature) {
        feature.permissions.refresh()
        feature.device.refreshSpeaker()
        onPauseOrDispose { }
    }
    BoxWithConstraints(modifier.fillMaxSize().background(ArchieTheme.colors.surface)) {
        if (maxWidth >= TWO_PANE) {
            var selected by rememberSaveable { mutableStateOf(SettingsPageKey.CONNECTION) }
            val host = remember { SnackbarHostState() }
            SettingsSnackbars(feature.messages, host)
            SnackbarHosted {
                Row(Modifier.fillMaxSize()) {
                    Column(Modifier.width(380.dp).fillMaxHeight()) {
                        SettingsHomeList(feature, onBack, selected = selected, onOpen = { selected = it })
                    }
                    Box(Modifier.weight(1f).fillMaxHeight().padding(end = 8.dp, bottom = 8.dp).clip(RoundedCornerShape(28.dp)).background(ArchieTheme.colors.surfaceContainerLow)) {
                        SettingsPageHost(feature, selected, onBack = null, key = selected.name)
                    }
                }
            }
            Box(Modifier.align(Alignment.BottomEnd).width(560.dp)) { ArchieSnackbarHost(host) }
        } else {
            val host = remember { SnackbarHostState() }
            SettingsSnackbars(feature.messages, host)
            SnackbarHosted { SettingsHomeList(feature, onBack, selected = null, onOpen = onOpenPage) }
            Box(Modifier.align(Alignment.BottomCenter)) { ArchieSnackbarHost(host) }
        }
    }
}

/** One settings page as its own screen (Compact / Medium), with in-page sub-pages (Background). */
@Composable
fun SettingsPageScreen(feature: SettingsFeature, page: SettingsPageKey, onBack: () -> Unit, modifier: Modifier = Modifier) {
    LaunchedEffect(feature, page) {
        if (page.group == SettingsPageKey.Group.SERVER || page == SettingsPageKey.ABOUT) feature.server.refresh()
    }
    LifecycleResumeEffect(feature) {
        feature.permissions.refresh()
        feature.device.refreshSpeaker()
        onPauseOrDispose { }
    }
    Box(modifier.fillMaxSize()) { SettingsPageHost(feature, page, onBack, key = page.name) }
}

/** A page plus its local sub-page stack (Back pops a sub-page before leaving). */
@Composable
internal fun SettingsPageHost(feature: SettingsFeature, root: SettingsPageKey, onBack: (() -> Unit)?, key: String) {
    var stack by rememberSaveable(key) { mutableStateOf(listOf(root.name)) }
    val top = SettingsPageKey.valueOf(stack.last())
    val pop: () -> Unit = { stack = stack.dropLast(1) }
    BackHandler(enabled = stack.size > 1, onBack = pop)
    val back: (() -> Unit)? = if (stack.size > 1) pop else onBack
    val open: (SettingsPageKey) -> Unit = { stack = stack + it.name }
    SettingsPage(feature, top, back, open)
}

@Composable
internal fun SettingsPage(feature: SettingsFeature, page: SettingsPageKey, onBack: (() -> Unit)?, open: (SettingsPageKey) -> Unit) {
    when (page) {
        SettingsPageKey.CONNECTION -> ConnectionPage(feature, onBack)
        SettingsPageKey.AUDIO -> AudioPage(feature, onBack)
        SettingsPageKey.WAKE_WORD -> WakeWordPage(feature, onBack, open)
        SettingsPageKey.APPEARANCE -> AppearancePage(feature, onBack)
        SettingsPageKey.NOTIFICATIONS -> NotificationsPage(feature, onBack, open)
        SettingsPageKey.PERMISSIONS -> PermissionsPage(feature, onBack, open)
        SettingsPageKey.BACKGROUND -> BackgroundReliabilityPage(feature, onBack)
        SettingsPageKey.CONVERSATION_MODEL -> ConversationModelPage(feature, onBack)
        SettingsPageKey.VOICE -> VoicePage(feature, onBack)
        SettingsPageKey.VOICE_TUNING -> VoiceTuningPage(feature, onBack)
        SettingsPageKey.AGENT_SESSIONS -> AgentSessionsPage(feature, onBack)
        SettingsPageKey.WORKING_DIRECTORIES -> WorkingDirectoriesPage(feature, onBack)
        SettingsPageKey.MCP_SERVERS -> McpServersPage(feature, onBack)
        SettingsPageKey.ACCOUNT -> AccountsPage(feature, onBack)
        SettingsPageKey.ABOUT -> AboutPage(feature, onBack)
    }
}

// ───────────────────────────── home ─────────────────────────────

internal fun phaseWord(p: ConnectionStatus.Phase) = when (p) {
    ConnectionStatus.Phase.CONNECTED -> "connected"
    ConnectionStatus.Phase.CONNECTING -> "connecting"
    ConnectionStatus.Phase.RECONNECTING -> "reconnecting"
    ConnectionStatus.Phase.OFFLINE -> "offline"
}

internal fun outputLabel(o: AudioOutput) = when (o) {
    AudioOutput.AUTO -> "Auto output"
    AudioOutput.LOUDSPEAKER -> "Speaker out"
    AudioOutput.EARPIECE -> "Earpiece out"
    AudioOutput.BLUETOOTH -> "Bluetooth out"
    AudioOutput.WIRED -> "Wired out"
}

internal fun themeLabel(t: ThemeMode) = when (t) {
    ThemeMode.SYSTEM -> "System theme"
    ThemeMode.DARK -> "Dark"
    ThemeMode.LIGHT -> "Light"
}

@Composable
internal fun SettingsHomeList(feature: SettingsFeature, onBack: (() -> Unit)?, selected: SettingsPageKey?, onOpen: (SettingsPageKey) -> Unit) {
    val conn by feature.connection.state.collectAsStateWithLifecycle()
    val server by feature.server.state.collectAsStateWithLifecycle()
    val auth by feature.auth.state.collectAsStateWithLifecycle()
    val accounts by feature.accounts.state.collectAsStateWithLifecycle()
    val appearance by feature.device.appearance.collectAsStateWithLifecycle()
    val speaker by feature.device.speakerLevel.collectAsStateWithLifecycle()
    val perms by feature.permissions.state.collectAsStateWithLifecycle()
    var searching by rememberSaveable { mutableStateOf(false) }
    var query by rememberSaveable { mutableStateOf("") }
    val s = conn.settings
    val status = conn.status
    val offline = status.phase == ConnectionStatus.Phase.OFFLINE
    val host = status.serverUrl?.let(ConnectionRepository::hostOf).orEmpty()

    val summaries = HashMap<SettingsPageKey, String>()
    summaries[SettingsPageKey.CONNECTION] = listOf(status.serverLabel, host).filter { it.isNotEmpty() }.distinct()
        .plus(phaseWord(status.phase)).joinToString(" · ")
    if (s != null) {
        summaries[SettingsPageKey.AUDIO] = listOfNotNull(
            "Mic ${Format.percent(s.micGainLevel)}",
            speaker?.let { "Speaker ${Format.percent(it)}" },
            outputLabel(s.audioOutput),
        ).joinToString(" · ")
        summaries[SettingsPageKey.WAKE_WORD] = if (!s.enableWakeWord) "Off" else listOfNotNull(
            "On",
            "“${s.wakeWord.split(',').first().trim()}”",
            if (perms.defaultAssistant == true) "assist gesture" else null,
        ).joinToString(" · ")
        summaries[SettingsPageKey.APPEARANCE] = "${themeLabel(s.themeMode)} · ${appearance.textSize.summary}"
        summaries[SettingsPageKey.NOTIFICATIONS] =
            NotifyLogic.summary(s.notifyAgentTurns, perms.granted(AppPermission.NOTIFICATIONS), perms.notificationsEnabled)
    }
    summaries[SettingsPageKey.PERMISSIONS] = perms.summary
    val cfg = server.config.value
    if (cfg == null) {
        val msg = if (server.config.error != null) "Couldn't load" else "Loading…"
        SettingsPageKey.entries.filter { it.group == SettingsPageKey.Group.SERVER }.forEach { summaries[it] = msg }
    } else {
        val c = server.catalogs
        val models = c.orchestratorModels?.models.orEmpty()
        val textModel = ModelLogic.find(models, cfg.defaultModel)
        val summ = cfg.summarizerModel?.takeIf { it.isNotEmpty() }
        summaries[SettingsPageKey.CONVERSATION_MODEL] =
            "${textModel?.provider?.let { "${ModelLogic.providerLabel(it)} · " } ?: ""}${ModelLogic.name(models, cfg.defaultModel)}" +
            (cfg.defaultAudioModel?.takeIf { it.isNotEmpty() } ?: c.orchestratorModels?.defaultAudioModel)
                ?.let { " · voice messages by ${ModelLogic.name(models, it)}" }.orEmpty() +
            " · summaries by ${summ?.let { ModelLogic.name(models, it) } ?: "server default"}"
        val v = cfg.voice
        val entry = VoiceLogic.catalog(c.voiceModels, server.google)[v.provider]?.firstOrNull { it.id == v.model }
        summaries[SettingsPageKey.VOICE] = listOfNotNull(
            v.provider?.let(VoiceLogic::providerLabel), v.model, v.voice,
            VoiceLogic.languageLabel(entry, v.transcriptionLanguage),
        ).filter { it.isNotEmpty() }.joinToString(" · ")
        summaries[SettingsPageKey.VOICE_TUNING] = "VAD ${Format.threshold(cfg.voiceVadThreshold ?: 0.28)} · " +
            "silence ${Format.ms((cfg.voiceVadMinSilenceMs ?: 2500).toDouble())} · gain ${Format.gain(cfg.voiceMicGain ?: 1.0)}"
        val harness = c.providers?.firstOrNull { it.id == cfg.provider }?.label?.takeIf { it.isNotEmpty() } ?: cfg.provider.orEmpty()
        summaries[SettingsPageKey.AGENT_SESSIONS] = "$harness by default · Chrome ${if (cfg.chromeExtension) "on" else "off"}"
        summaries[SettingsPageKey.WORKING_DIRECTORIES] = WorkingDirectoryLogic.summary(cfg.workingDirectoryHistory, cfg.workingDirectory)
        summaries[SettingsPageKey.MCP_SERVERS] = if (c.mcpServers != null) McpLogic.summary(cfg.enabledMcps, c.mcpNames)
        else if (cfg.enabledMcps.isNotEmpty()) "${cfg.enabledMcps.size} enabled" else "All enabled"
    }
    // Every service once Accounts has been opened; until then the Claude check the gate does anyway.
    summaries[SettingsPageKey.ACCOUNT] = accounts.summary ?: auth.summary
    val v = feature.platform.appVersion
    summaries[SettingsPageKey.ABOUT] = "App ${v.name} (${v.code}) · backend on ${host.ifEmpty { "—" }}"

    val q = query.trim().lowercase()
    fun visible(k: SettingsPageKey) = q.isEmpty() || k.title.lowercase().contains(q) || summaries[k].orEmpty().lowercase().contains(q)

    // Full screen on phones: keep the top bar below the status bar (as Memory/Visuals do).
    Column(Modifier.fillMaxSize().windowInsetsPadding(WindowInsets.safeDrawing.only(WindowInsetsSides.Top + WindowInsetsSides.Horizontal)).testTag("settings-home")) {
        ArchieTopAppBar(
            "Settings",
            navigationIcon = onBack?.let { back -> { ArchieIconButton(ArchieIcons.ArrowBack, "Back", back) } },
            actions = {
                ArchieIconButton(if (searching) ArchieIcons.Close else ArchieIcons.Search, if (searching) "Close search" else "Search settings", {
                    searching = !searching
                    if (!searching) query = ""
                })
            },
        )
        Column(
            Modifier.weight(1f).fillMaxWidth().verticalScroll(rememberScrollState()).padding(start = 16.dp, end = 16.dp, bottom = 96.dp),
            verticalArrangement = Arrangement.Top,
        ) {
            if (searching) {
                ArchieTextField(query, { query = it }, "Search settings", Modifier.fillMaxWidth().padding(bottom = 8.dp).testTag("settings-search"), leadingIcon = ArchieIcons.Search)
            }
            val needs = s?.enableWakeWord == true && (!perms.granted(AppPermission.MICROPHONE) || !perms.granted(AppPermission.NOTIFICATIONS))
            if (needs && q.isEmpty()) {
                Notice(
                    NoticeTone.WARNING,
                    "Wake word needs permissions",
                    Modifier.padding(top = 4.dp),
                    body = perms.summary + ". Without them Archie can't listen in the background.",
                    icon = ArchieIcons.Shield,
                    actions = { com.assistant.core.design.components.InlineCardAction("Review", { onOpen(SettingsPageKey.PERMISSIONS) }, primary = true) },
                )
            }
            HomeGroup("This device", null, listOf(SettingsPageKey.CONNECTION, SettingsPageKey.AUDIO, SettingsPageKey.WAKE_WORD, SettingsPageKey.APPEARANCE, SettingsPageKey.NOTIFICATIONS, SettingsPageKey.PERMISSIONS).filter(::visible), summaries, selected, false, onOpen, topPad = 4)
            HomeGroup(
                "Archie (server)",
                {
                    StatusDot(tone = when (status.phase) {
                        ConnectionStatus.Phase.CONNECTED -> DotTone.Success
                        ConnectionStatus.Phase.OFFLINE -> DotTone.Error
                        else -> DotTone.Warning
                    })
                    Text("${status.serverLabel.ifEmpty { host }} · ${if (status.phase == ConnectionStatus.Phase.CONNECTED) "online" else phaseWord(status.phase)}")
                },
                SettingsPageKey.entries.filter { it.group == SettingsPageKey.Group.SERVER && visible(it) },
                summaries, selected, offline, onOpen,
            )
            HomeGroup("About", null, listOf(SettingsPageKey.ABOUT).filter(::visible), summaries, selected, false, onOpen)
        }
    }
}

@Composable
private fun HomeGroup(
    title: String,
    meta: (@Composable androidx.compose.foundation.layout.RowScope.() -> Unit)?,
    pages: List<SettingsPageKey>,
    summaries: Map<SettingsPageKey, String>,
    selected: SettingsPageKey?,
    disabled: Boolean,
    onOpen: (SettingsPageKey) -> Unit,
    topPad: Int = 18,
) {
    if (pages.isEmpty()) return
    SettingsGroupHeader(title, if (topPad != 18) Modifier.padding(top = 0.dp) else Modifier, meta)
    SettingsGroup {
        for (p in pages) {
            val c = ArchieTheme.colors
            SettingsRow(
                title = p.title,
                icon = p.icon,
                value = summaries[p],
                onClick = { onOpen(p) },
                enabled = !disabled,
                modifier = Modifier.testTag("settings-row:${p.name}"),
                // Two-pane: the open page's chevron is drawn in primary.
                trailing = { ArchieIcon(ArchieIcons.ChevronRight, null, tint = if (p == selected) c.primary else c.onSurfaceVariant) },
            )
        }
    }
}
