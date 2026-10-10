package com.assistant.archie.shell

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.Orientation
import androidx.compose.foundation.gestures.draggable
import androidx.compose.foundation.gestures.rememberDraggableState
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.WindowInsetsSides
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.only
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.DrawerValue
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.ModalDrawerSheet
import androidx.compose.material3.ModalNavigationDrawer
import androidx.compose.material3.NavigationRail
import androidx.compose.material3.NavigationRailItem
import androidx.compose.material3.NavigationRailItemDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.rememberDrawerState
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Rect
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.boundsInRoot
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.navigation3.rememberViewModelStoreNavEntryDecorator
import androidx.navigation3.runtime.NavKey
import androidx.navigation3.scene.DialogSceneStrategy
import androidx.compose.ui.window.DialogProperties
import androidx.navigation3.runtime.entryProvider
import androidx.navigation3.runtime.rememberSaveableStateHolderNavEntryDecorator
import androidx.navigation3.ui.NavDisplay
import com.assistant.archie.feature.chat.ui.observeVoiceOverlayActivity
import com.assistant.archie.feature.sessions.SessionsIntent
import com.assistant.archie.feature.sessions.ui.HistoryListState
import com.assistant.archie.feature.sessions.ui.HistoryScreen
import com.assistant.archie.feature.sessions.ui.SessionMenuButton
import com.assistant.archie.feature.sessions.ui.SessionSwitcherContent
import com.assistant.archie.feature.sessions.ui.SessionsHost
import com.assistant.core.data.ConnectionStatus
import com.assistant.core.data.ItemKey
import com.assistant.core.data.ItemKind
import com.assistant.core.data.WorkspaceItem
import com.assistant.core.design.components.ArchieButton
import com.assistant.core.design.components.ArchieDropdownMenu
import com.assistant.core.design.components.ArchieIconButton
import com.assistant.core.design.components.ArchieMenuItem
import com.assistant.core.design.components.ArchieTopAppBar
import com.assistant.core.design.components.ButtonStyle
import com.assistant.core.design.components.EmptyState
import com.assistant.core.design.components.StatusIndicator
import com.assistant.core.design.components.TabStrip
import com.assistant.core.design.components.TopAppBarSubtitle
import com.assistant.core.design.components.TopBarTab
import com.assistant.core.design.icons.ArchieIcon
import com.assistant.core.design.icons.ArchieIcons
import com.assistant.core.design.theme.ArchieTheme
import kotlinx.coroutines.launch

/** Window size classes of IA §2 (Compact < 600 dp, Medium 600–839 dp, Expanded ≥ 840 dp). */
enum class LayoutClass {
    Compact, Medium, Expanded;

    companion object {
        fun of(width: Dp): LayoutClass = when {
            width < 600.dp -> Compact
            width < 840.dp -> Medium
            else -> Expanded
        }
    }
}

/** Which list the rail shows (Medium/Expanded). */
enum class RailDestination { Chats, Memory, Visuals }

/** Initial overlay state (screenshot scenes start with the drawer or the switcher open). */
data class ShellOverlays(val drawerOpen: Boolean = false, val switcherOpen: Boolean = false, val listOverlayOpen: Boolean = false)

/**
 * The adaptive app shell (spec 14 §2.4, IA §2–§5): Compact = drawer + top app bar + switcher sheet,
 * no bottom bar; Medium = rail + overlay list pane; Expanded = rail + 320 dp list pane + workspace
 * with the tab strip in its top bar. Full-screen destinations live on a Navigation 3 back stack;
 * workspace tabs are state ([ShellUiState.items] / [ShellUiState.active]), never routes.
 */
@Composable
fun ArchieShell(
    state: ShellUiState,
    onAction: (ShellAction) -> Unit,
    backStack: MutableList<NavKey>,
    destinations: ShellDestinations,
    modifier: Modifier = Modifier,
    overlays: ShellOverlays = ShellOverlays(),
    voiceOverlay: ShellVoiceOverlay? = null,
) {
    // The floating voice controls fade when the app sits still: every touch is activity (observed, never consumed).
    val activity = voiceOverlay?.let { Modifier.observeVoiceOverlayActivity(it.activity) } ?: Modifier
    BoxWithConstraints(modifier.fillMaxSize().testTag("shell").then(activity)) {
        when (val layout = LayoutClass.of(maxWidth)) {
            LayoutClass.Compact -> CompactShell(state, onAction, backStack, destinations, overlays, voiceOverlay)
            else -> WideShell(layout, state, onAction, backStack, destinations, overlays, voiceOverlay)
        }
        // B-06: session dialogs (rename, delete, fork, close, the Archie conflict), busy overlay, snackbar.
        SessionsHost(state.sessions, { onAction(ShellAction.Sessions(it)) })
    }
}

/** The floating voice controls' state text: back to the workspace with the Archie conversation focused. */
private fun openArchie(backStack: MutableList<NavKey>, onAction: (ShellAction) -> Unit): () -> Unit = {
    while (backStack.size > 1) backStack.removeAt(backStack.lastIndex)
    onAction(ShellAction.Select(ItemKey.Archie))
}

/** ×, swipe, the ⋮ menu's Close: the B-06 flow decides whether to ask first (§6.7, P-1). */
private fun requestClose(onAction: (ShellAction) -> Unit): (WorkspaceItem) -> Unit =
    { item -> onAction(ShellAction.Sessions(SessionsIntent.RequestClose(item))) }

/** The History screen (B-06), fed by the shell state; opening a row returns to the workspace. */
@Composable
private fun ShellHistoryScreen(state: ShellUiState, onAction: (ShellAction) -> Unit, onBack: () -> Unit) {
    HistoryScreen(
        state = HistoryListState(
            items = state.items,
            active = state.active,
            groups = state.history,
            loading = state.historyLoading,
            error = state.historyError,
            query = state.search,
        ),
        onQuery = { onAction(ShellAction.Search(it)) },
        onSelect = { onAction(ShellAction.Select(it)) },
        onIntent = { onAction(ShellAction.Sessions(it)) },
        onRefresh = { onAction(ShellAction.RefreshHistory) },
        onBack = onBack,
        onOpened = onBack,
    )
}

// ───────────────────────────── Compact ─────────────────────────────

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun CompactShell(
    state: ShellUiState,
    onAction: (ShellAction) -> Unit,
    backStack: MutableList<NavKey>,
    destinations: ShellDestinations,
    overlays: ShellOverlays,
    voiceOverlay: ShellVoiceOverlay?,
) {
    val c = ArchieTheme.colors
    var region by remember { mutableStateOf<Rect?>(null) }
    val drawer = rememberDrawerState(if (overlays.drawerOpen) DrawerValue.Open else DrawerValue.Closed)
    val scope = rememberCoroutineScope()
    var switcherOpen by rememberSaveable { mutableStateOf(overlays.switcherOpen) }
    val atRoot = backStack.lastOrNull() == Workspace
    val closeDrawer: () -> Unit = { scope.launch { drawer.close() } }

    BackHandler(enabled = drawer.isOpen) { closeDrawer() }

    ModalNavigationDrawer(
        drawerState = drawer,
        gesturesEnabled = atRoot || drawer.isOpen,
        drawerContent = {
            ModalDrawerSheet(
                modifier = Modifier.width(344.dp),
                drawerShape = RoundedCornerShape(topEnd = 28.dp, bottomEnd = 28.dp),
                drawerContainerColor = c.surfaceContainerLow,
                drawerContentColor = c.onSurface,
                windowInsets = WindowInsets.safeDrawing.only(WindowInsetsSides.Vertical + WindowInsetsSides.Start),
            ) {
                DrawerContent(
                    state, onAction,
                    onNavigate = { nav -> navigateCompact(nav, backStack) },
                    onClose = closeDrawer,
                )
            }
        },
    ) {
        // The voice overlay floats over every compact screen; the modal drawer covers it.
        Box(Modifier.fillMaxSize().onGloballyPositioned { region = it.boundsInRoot() }) {
            ShellNavDisplay(backStack, destinations, history = { pop -> ShellHistoryScreen(state, onAction, pop) }) {
                CompactWorkspace(
                    state, onAction, destinations,
                    onMenu = { scope.launch { drawer.open() } },
                    onTitle = { switcherOpen = true },
                    switcherOpen = switcherOpen,
                    onSessionSettings = { backStack.add(SessionSettings(it)) },
                )
            }
            voiceOverlay?.Overlay(region, compact = true, state.active, backStack.lastOrNull(), openArchie(backStack, onAction))
        }
    }

    if (switcherOpen) {
        val sheet = rememberModalBottomSheetState(skipPartiallyExpanded = true)
        ModalBottomSheet(
            onDismissRequest = { switcherOpen = false },
            sheetState = sheet,
            shape = RoundedCornerShape(topStart = 28.dp, topEnd = 28.dp),
            containerColor = c.surfaceContainerLow,
            contentColor = c.onSurface,
            dragHandle = { SheetHandle() },
        ) {
            SessionSwitcherContent(
                items = state.items,
                active = state.active,
                onSelect = { onAction(ShellAction.Select(it)) },
                onRequestClose = requestClose(onAction),
                onNewArchie = { onAction(ShellAction.NewArchie) },
                onNewAgent = { onAction(ShellAction.NewAgent) },
                onHistory = { backStack.add(History) },
                onDismiss = { switcherOpen = false },
            )
        }
    }
}

private fun navigateCompact(nav: ShellNav, backStack: MutableList<NavKey>) {
    val key: NavKey = when (nav) {
        ShellNav.Chats -> return
        ShellNav.Memory -> MemoryTree
        ShellNav.Visuals -> VisualsList
        ShellNav.Settings -> SettingsHome
        ShellNav.History -> History
    }
    if (backStack.lastOrNull() != key) backStack.add(key)
}

@Composable
private fun SheetHandle() {
    Box(
        Modifier
            .padding(top = 16.dp, bottom = 14.dp)
            .size(width = 32.dp, height = 4.dp)
            .background(ArchieTheme.colors.onSurfaceVariant.copy(alpha = 0.4f), RoundedCornerShape(2.dp)),
    )
}

@Composable
private fun CompactWorkspace(
    state: ShellUiState,
    onAction: (ShellAction) -> Unit,
    destinations: ShellDestinations,
    onMenu: () -> Unit,
    onTitle: () -> Unit,
    switcherOpen: Boolean,
    onSessionSettings: (String) -> Unit,
) {
    val c = ArchieTheme.colors
    val item = state.activeItem
    val threshold = with(LocalDensity.current) { SWIPE_THRESHOLD.toPx() }
    var dragged by remember { mutableFloatStateOf(0f) }
    val drag = rememberDraggableState { dragged += it }
    Column(
        Modifier
            .fillMaxSize()
            .background(c.surface)
            .windowInsetsPadding(WindowInsets.safeDrawing.only(WindowInsetsSides.Top + WindowInsetsSides.Horizontal)),
    ) {
        val subtitle = subtitleFor(state)
        ArchieTopAppBar(
            title = item?.title ?: "Archie",
            // A horizontal drag on the title switches to the next / previous open session (IA §5).
            modifier = Modifier
                .testTag("top-app-bar")
                .draggable(
                    drag, Orientation.Horizontal,
                    onDragStopped = {
                        when {
                            dragged <= -threshold -> onAction(ShellAction.SelectRelative(+1))
                            dragged >= threshold -> onAction(ShellAction.SelectRelative(-1))
                        }
                        dragged = 0f
                    },
                ),
            navigationIcon = { ArchieIconButton(ArchieIcons.Menu, "Open navigation", onMenu) },
            onTitleClick = onTitle,
            titleExpanded = switcherOpen,
            showMark = item == null || item.kind == ItemKind.ARCHIE,
            subtitle = subtitle?.let { s -> { TopAppBarSubtitle(s.text, s.icon, s.tint()) } },
        ) {
            if (item?.kind == ItemKind.ARCHIE) {
                // Speaker state; the voice host (B-09) wires the toggle.
                ArchieIconButton(ArchieIcons.VolumeUp, "Speaker on", {})
            }
            SessionMenuButton(item, { onAction(ShellAction.Sessions(it)) }, onSessionSettings)
        }
        Box(Modifier.weight(1f).fillMaxWidth()) {
            if (item != null) destinations.WorkspaceItemContent(item.key, Modifier.fillMaxSize())
            else NoSessionContent(state, onAction)
        }
    }
}

private val SWIPE_THRESHOLD = 56.dp

@Composable
private fun Subtitle.tint(): Color = when (tone) {
    SubtitleTone.Warning -> ArchieTheme.extended.warning.color
    SubtitleTone.Primary -> ArchieTheme.colors.primary
    SubtitleTone.Normal -> Color.Unspecified
}

// ───────────────────────────── Medium / Expanded ─────────────────────────────

@Composable
private fun WideShell(
    layout: LayoutClass,
    state: ShellUiState,
    onAction: (ShellAction) -> Unit,
    backStack: MutableList<NavKey>,
    destinations: ShellDestinations,
    overlays: ShellOverlays,
    voiceOverlay: ShellVoiceOverlay?,
) {
    val c = ArchieTheme.colors
    var region by remember { mutableStateOf<Rect?>(null) }
    var rail by rememberSaveable { mutableStateOf(RailDestination.Chats) }
    var overlay by rememberSaveable { mutableStateOf(overlays.listOverlayOpen) }
    val expanded = layout == LayoutClass.Expanded
    val paneVisible = expanded && !state.listPaneCollapsed
    val inSettings = backStack.lastOrNull().let { it == SettingsHome || it is SettingsPage }
    val requestClose: (WorkspaceItem) -> Unit = requestClose(onAction)
    val toWorkspace = { while (backStack.size > 1) backStack.removeAt(backStack.lastIndex) }

    Box(Modifier.fillMaxSize().background(c.surfaceContainerLow)) {
        Row(Modifier.fillMaxSize().windowInsetsPadding(WindowInsets.safeDrawing)) {
            ShellRail(
                selected = if (inSettings) null else rail,
                settingsSelected = inSettings,
                showMenuButton = !expanded,
                onMenu = { overlay = true },
                onNewArchie = { toWorkspace(); onAction(ShellAction.NewArchie) },
                onNewAgent = { toWorkspace(); onAction(ShellAction.NewAgent) },
                onSelect = { dest ->
                    rail = dest
                    toWorkspace()
                    if (expanded) { if (state.listPaneCollapsed) onAction(ShellAction.ToggleListPane) } else overlay = true
                },
                onSettings = { if (!inSettings) backStack.add(SettingsHome) },
            )
            if (paneVisible) {
                Box(Modifier.width(320.dp).fillMaxHeight().padding(top = 12.dp, end = 12.dp, bottom = 12.dp)) {
                    ListPane(rail, state, onAction, destinations, onCollapse = { onAction(ShellAction.ToggleListPane) }, after = toWorkspace)
                }
            }
            Box(
                Modifier
                    .weight(1f)
                    .fillMaxHeight()
                    .padding(top = 8.dp, end = 8.dp, bottom = 8.dp)
                    .clip(RoundedCornerShape(24.dp))
                    .background(c.surface)
                    .onGloballyPositioned { region = it.boundsInRoot() }
                    .testTag("workspace"),
            ) {
                ShellNavDisplay(backStack, destinations, history = { pop -> ShellHistoryScreen(state, onAction, pop) }) {
                    WideWorkspace(
                        expanded, state, onAction, destinations,
                        showExpandPane = expanded && state.listPaneCollapsed,
                        onRequestClose = requestClose,
                        onSessionSettings = { backStack.add(SessionSettings(it)) },
                    )
                }
            }
        }
        // Above the rail, list pane and workspace (Settings included); under the medium list overlay.
        voiceOverlay?.Overlay(region, compact = false, state.active, backStack.lastOrNull(), openArchie(backStack, onAction))
        if (!expanded && overlay) {
            BackHandler { overlay = false }
            Box(
                Modifier
                    .fillMaxSize()
                    .background(c.scrim.copy(alpha = 0.32f))
                    .clickable(interactionSource = remember { MutableInteractionSource() }, indication = null) { overlay = false }
                    .semantics { contentDescription = "Close list" },
            )
            Box(
                Modifier
                    .windowInsetsPadding(WindowInsets.safeDrawing)
                    .padding(start = 80.dp, top = 8.dp, bottom = 8.dp)
                    .width(344.dp)
                    .fillMaxHeight()
                    .clip(RoundedCornerShape(28.dp))
                    .background(c.surfaceContainerLow)
                    .padding(start = 12.dp, end = 12.dp, top = 4.dp, bottom = 12.dp),
            ) {
                ListPane(rail, state, onAction, destinations, onCollapse = { overlay = false }, after = { overlay = false; toWorkspace() })
            }
        }
    }
}

@Composable
private fun ListPane(
    rail: RailDestination,
    state: ShellUiState,
    onAction: (ShellAction) -> Unit,
    destinations: ShellDestinations,
    onCollapse: () -> Unit,
    after: () -> Unit,
) {
    when (rail) {
        RailDestination.Chats -> ChatsPane(state, onAction, onCollapse, after, Modifier.fillMaxSize())
        RailDestination.Memory -> Column(Modifier.fillMaxSize()) {
            PaneHeader("Memory", onCollapse)
            destinations.MemoryPane(onOpenDoc = { onAction(ShellAction.OpenMemoryDoc(it)); after() }, Modifier.weight(1f))
        }
        RailDestination.Visuals -> Column(Modifier.fillMaxSize()) {
            PaneHeader("Visuals", onCollapse)
            destinations.VisualsPane(onOpen = { onAction(ShellAction.OpenVisual(it)); after() }, Modifier.weight(1f))
        }
    }
}

/** The 80 dp navigation rail (IA §3): New FAB + menu, Chats / Memory / Visuals, Settings pinned at the bottom. */
@Composable
private fun ShellRail(
    selected: RailDestination?,
    settingsSelected: Boolean,
    showMenuButton: Boolean,
    onMenu: () -> Unit,
    onNewArchie: () -> Unit,
    onNewAgent: () -> Unit,
    onSelect: (RailDestination) -> Unit,
    onSettings: () -> Unit,
) {
    val c = ArchieTheme.colors
    var newMenu by remember { mutableStateOf(false) }
    val colors = NavigationRailItemDefaults.colors(
        selectedIconColor = c.onSecondaryContainer,
        selectedTextColor = c.onSurface,
        indicatorColor = c.secondaryContainer,
        unselectedIconColor = c.onSurfaceVariant,
        unselectedTextColor = c.onSurfaceVariant,
    )
    NavigationRail(
        modifier = Modifier.fillMaxHeight().testTag("rail"),
        containerColor = Color.Transparent,
        header = {
            Column(Modifier.padding(top = if (showMenuButton) 0.dp else 12.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                if (showMenuButton) ArchieIconButton(ArchieIcons.Menu, "Open list", onMenu, Modifier.padding(bottom = 4.dp))
                Box {
                    Box(
                        Modifier
                            .padding(top = 6.dp, bottom = 18.dp)
                            .size(56.dp)
                            .clip(RoundedCornerShape(16.dp))
                            .background(c.primaryContainer)
                            .clickable { newMenu = true }
                            .semantics { contentDescription = "New: Archie conversation or agent session" },
                        contentAlignment = Alignment.Center,
                    ) { ArchieIcon(ArchieIcons.Add, null, tint = c.onPrimaryContainer) }
                    ArchieDropdownMenu(newMenu, onDismissRequest = { newMenu = false }) {
                        ArchieMenuItem("New Archie conversation", { newMenu = false; onNewArchie() }, icon = ArchieIcons.AddComment)
                        ArchieMenuItem("New agent session", { newMenu = false; onNewAgent() }, icon = ArchieIcons.Terminal)
                    }
                }
            }
        },
    ) {
        val item: @Composable (RailDestination, androidx.compose.ui.graphics.vector.ImageVector, androidx.compose.ui.graphics.vector.ImageVector, String) -> Unit =
            { dest, icon, filled, label ->
                NavigationRailItem(
                    selected = selected == dest,
                    onClick = { onSelect(dest) },
                    icon = { ArchieIcon(if (selected == dest) filled else icon, null) },
                    label = { Text(label, style = ArchieTheme.typography.labelMedium.copy(letterSpacing = 0.4.sp)) },
                    colors = colors,
                )
            }
        item(RailDestination.Chats, ArchieIcons.Forum, ArchieIcons.ForumFilled, "Chats")
        item(RailDestination.Memory, ArchieIcons.Book2, ArchieIcons.Book2Filled, "Memory")
        item(RailDestination.Visuals, ArchieIcons.BarChart, ArchieIcons.BarChartFilled, "Visuals")
        Spacer(Modifier.weight(1f))
        NavigationRailItem(
            selected = settingsSelected,
            onClick = onSettings,
            icon = { ArchieIcon(if (settingsSelected) ArchieIcons.SettingsFilled else ArchieIcons.Settings, null) },
            label = { Text("Settings", style = ArchieTheme.typography.labelMedium.copy(letterSpacing = 0.4.sp)) },
            colors = colors,
        )
    }
}

/** Workspace top bar with the integrated tab strip (IA §3, desktop mockup `.wtop`). */
@Composable
private fun WideWorkspace(
    expanded: Boolean,
    state: ShellUiState,
    onAction: (ShellAction) -> Unit,
    destinations: ShellDestinations,
    showExpandPane: Boolean,
    onRequestClose: (WorkspaceItem) -> Unit,
    onSessionSettings: (String) -> Unit,
) {
    val item = state.activeItem
    Column(Modifier.fillMaxSize()) {
        Row(
            Modifier.fillMaxWidth().height(64.dp).padding(start = 6.dp, end = 8.dp).testTag("tab-bar"),
            horizontalArrangement = Arrangement.spacedBy(4.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            if (showExpandPane) ArchieIconButton(ArchieIcons.LeftPanelOpen, "Expand list", { onAction(ShellAction.ToggleListPane) })
            TabStrip(Modifier.weight(1f)) {
                for (t in state.items) {
                    TopBarTab(
                        title = t.title,
                        lead = t.tabLead(),
                        active = t.key == state.active,
                        onClick = { onAction(ShellAction.Select(t.key)) },
                        onClose = { onRequestClose(t) },
                        modifier = Modifier.testTag("tab"),
                        provider = t.provider?.label,
                        status = t.status.toLive(),
                    )
                }
            }
            if (state.items.isNotEmpty()) AllTabsButton(state, onAction)
            if (expanded && item != null) WorkspaceStatus(item)
            SessionMenuButton(item, { onAction(ShellAction.Sessions(it)) }, onSessionSettings)
        }
        Box(Modifier.weight(1f).fillMaxWidth()) {
            if (item != null) destinations.WorkspaceItemContent(item.key, Modifier.fillMaxSize())
            else NoSessionContent(state, onAction)
        }
    }
}

/** The ⌄ tab counter: a menu of every open tab (IA §3 "All tabs"). */
@Composable
private fun AllTabsButton(state: ShellUiState, onAction: (ShellAction) -> Unit) {
    val c = ArchieTheme.colors
    var open by remember { mutableStateOf(false) }
    Box {
        Row(
            Modifier
                .height(36.dp)
                .clip(RoundedCornerShape(18.dp))
                .background(c.surfaceContainerHigh)
                .clickable { open = true }
                .semantics { contentDescription = "All tabs (${state.items.size})" }
                .padding(start = 12.dp, end = 6.dp),
            horizontalArrangement = Arrangement.spacedBy(2.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text("${state.items.size}", style = ArchieTheme.typography.labelLarge.copy(fontSize = 13.sp, fontFeatureSettings = "tnum"), color = c.onSurfaceVariant)
            ArchieIcon(ArchieIcons.KeyboardArrowDown, null, size = 20.dp, tint = c.onSurfaceVariant)
        }
        ArchieDropdownMenu(open, onDismissRequest = { open = false }) {
            for (t in state.items) {
                ArchieMenuItem(t.title, { open = false; onAction(ShellAction.Select(t.key)) }, icon = t.kind.icon(), current = t.key == state.active)
            }
        }
    }
}

/** "● Ready · 14 turns" next to the tabs (Expanded only). */
@Composable
private fun WorkspaceStatus(item: WorkspaceItem) {
    Row(
        Modifier.height(40.dp).widthIn(max = 220.dp).padding(horizontal = 12.dp),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        item.status.toLive()?.let { StatusIndicator(it) }
        Text(
            item.detail.removePrefix("Archie · ").replaceFirstChar { it.uppercase() },
            style = ArchieTheme.typography.labelLarge.copy(fontSize = 13.sp),
            color = ArchieTheme.colors.onSurfaceVariant,
            maxLines = 1,
        )
    }
}

// ───────────────────────────── shared ─────────────────────────────

/** Nothing open: never a blank screen (A6). No auto-navigation to History (FOCUS-1, inv03 A-1.1). */
@Composable
private fun NoSessionContent(state: ShellUiState, onAction: (ShellAction) -> Unit) {
    Box(Modifier.fillMaxSize().padding(horizontal = 16.dp).testTag("no-session"), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(16.dp)) {
            when (state.connection.phase) {
                ConnectionStatus.Phase.CONNECTED -> {
                    EmptyState(
                        title = "No conversation open",
                        body = "Start one, or pick a past conversation from the menu.",
                        modifier = Modifier.widthIn(max = 520.dp),
                    )
                    // Wraps to two lines on narrow phones instead of running into the screen edges.
                    FlowRow(
                        horizontalArrangement = Arrangement.spacedBy(8.dp, Alignment.CenterHorizontally),
                        verticalArrangement = Arrangement.spacedBy(8.dp),
                    ) {
                        ArchieButton("New Archie chat", { onAction(ShellAction.NewArchie) }, style = ButtonStyle.Tonal, icon = ArchieIcons.AddComment)
                        ArchieButton("New agent session", { onAction(ShellAction.NewAgent) }, style = ButtonStyle.Outlined, icon = ArchieIcons.Terminal)
                    }
                }
                ConnectionStatus.Phase.OFFLINE -> {
                    EmptyState(
                        title = if (state.connection.scanning) "Looking for Archie…" else "Not connected",
                        body = state.connection.serverUrl?.let { "Server: $it" } ?: "",
                        modifier = Modifier.widthIn(max = 520.dp),
                    )
                    ArchieButton("Connect", { onAction(ShellAction.Reconnect) }, style = ButtonStyle.Tonal, icon = ArchieIcons.Sync)
                }
                else -> EmptyState(
                    title = "Connecting…",
                    body = state.connection.serverLabel.takeIf { it.isNotEmpty() }?.let { "to $it" } ?: "",
                    modifier = Modifier.widthIn(max = 520.dp),
                )
            }
        }
    }
}

/** The Navigation 3 display shared by every size class; [workspace] renders the root. */
@Composable
private fun ShellNavDisplay(
    backStack: MutableList<NavKey>,
    destinations: ShellDestinations,
    history: @Composable (onBack: () -> Unit) -> Unit,
    workspace: @Composable () -> Unit,
) {
    val pop: () -> Unit = { if (backStack.size > 1) backStack.removeAt(backStack.lastIndex) }
    NavDisplay(
        backStack = backStack,
        onBack = pop,
        entryDecorators = listOf(
            rememberSaveableStateHolderNavEntryDecorator(),
            rememberViewModelStoreNavEntryDecorator(),
        ),
        // B-08: session settings is a sheet over the conversation, so it renders as a dialog scene.
        sceneStrategies = listOf(DialogSceneStrategy()),
        entryProvider = entryProvider {
            entry<Workspace> { workspace() }
            entry<History> { history(pop) }
            entry<MemoryTree> { destinations.MemoryScreen(onBack = pop, onOpenDoc = { backStack.add(MemoryDoc(it)) }) }
            entry<MemoryDoc> { k ->
                destinations.MemoryDocScreen(k.path, onBack = pop, onOpenDoc = { backStack.add(MemoryDoc(it)) }, onOpenVisual = { backStack.add(VisualDoc(it)) })
            }
            entry<VisualsList> { destinations.VisualsScreen(onBack = pop, onOpen = { backStack.add(VisualDoc(it)) }) }
            entry<VisualDoc> { k -> destinations.VisualScreen(k.path, onBack = pop) }
            entry<SettingsHome> { destinations.SettingsScreen(onBack = pop, onOpenPage = { backStack.add(SettingsPage(it)) }) }
            entry<SettingsPage> { k -> destinations.SettingsPageScreen(k.id, onBack = pop) }
            entry<SessionSettings>(metadata = DialogSceneStrategy.dialog(DialogProperties(usePlatformDefaultWidth = false, decorFitsSystemWindows = false))) { k ->
                destinations.SessionSettingsScreen(k.localId, onBack = pop)
            }
        },
    )
}
