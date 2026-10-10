package com.assistant.archie.feature.visuals.ui

import android.content.ClipData
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.WindowInsetsSides
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBars
import androidx.compose.foundation.layout.only
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.ClipEntry
import androidx.compose.ui.platform.LocalClipboard
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.assistant.archie.feature.visuals.CastCapability
import com.assistant.archie.feature.visuals.ExternalLinks
import com.assistant.archie.feature.visuals.VisualUrls
import com.assistant.archie.feature.visuals.VisualsDeps
import com.assistant.archie.feature.visuals.relativeTime
import com.assistant.core.design.components.ArchieButton
import com.assistant.core.design.components.ArchieDropdownMenu
import com.assistant.core.design.components.ArchieIconButton
import com.assistant.core.design.components.ArchieMenuItem
import com.assistant.core.design.components.ArchieSnackbarHost
import com.assistant.core.design.components.ArchieTopAppBar
import com.assistant.core.design.components.ButtonSize
import com.assistant.core.design.components.ButtonStyle
import com.assistant.core.design.components.TopAppBarSubtitle
import com.assistant.core.design.icons.ArchieIcons
import com.assistant.core.design.theme.ArchieTheme
import com.assistant.core.model.VisualInfo
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.time.Instant

/** How long the "Updated" cue stays after a live reload (spec 12 VZ-6). */
const val UPDATED_CUE_MS = 2_500L

/** The live-change counter of [path] for [VisualWebView] (`0` when unchanged or deleted), and whether it was deleted. */
@Composable
internal fun rememberLive(deps: VisualsDeps, path: String): Pair<Int, Boolean> {
    val changes by deps.changes.collectAsStateWithLifecycle()
    val stamp = changes[path]
    return (if (stamp == null || stamp.deleted) 0 else stamp.version) to (stamp?.deleted == true)
}

/** A short-lived "Updated" pill (web `.updatedCue`); [shownAt] restarts it. */
@Composable
internal fun UpdatedCue(shownAt: Long, modifier: Modifier = Modifier) {
    var visible by remember { mutableStateOf(false) }
    LaunchedEffect(shownAt) {
        if (shownAt == 0L) return@LaunchedEffect
        visible = true
        delay(UPDATED_CUE_MS)
        visible = false
    }
    if (!visible) return
    val c = ArchieTheme.colors
    Text(
        "Updated",
        modifier
            .testTag("visual-updated")
            .semantics { liveRegion = LiveRegionMode.Polite }
            .background(c.secondaryContainer, RoundedCornerShape(50))
            .padding(horizontal = 10.dp, vertical = 2.dp),
        style = ArchieTheme.typography.labelMedium,
        color = c.onSecondaryContainer,
    )
}

/** The list entry for [path], if the list is loaded. */
@Composable
internal fun rememberVisual(deps: VisualsDeps, path: String): VisualInfo? {
    val list by deps.list.collectAsStateWithLifecycle()
    return remember(list.value, path) { list.value?.firstOrNull { it.path == path } }
}

/**
 * A visual full screen on Compact (spec 14 §4.2 `VisualScreen`, IA §9.4, mockup phone (h)): ✕,
 * the title over "Visuals · updated 2 h ago", **Show on TV** (only when the BX-2 probe says
 * available — hidden, not disabled), and ⋮ Reload / Open in browser / Copy link. The page runs in
 * the pooled in-app WebView ([VisualWebView]).
 */
@Composable
fun VisualScreen(deps: VisualsDeps, path: String, onBack: () -> Unit, modifier: Modifier = Modifier, now: Instant? = null) {
    val item = rememberVisual(deps, path)
    val origin = deps.origin()
    val href = VisualUrls.href(origin, path, item?.url)
    var reload by rememberSaveable(path) { mutableIntStateOf(0) }
    val (live, deleted) = rememberLive(deps, path)
    var cueAt by remember(path) { mutableLongStateOf(0L) }
    val snackbar = remember { SnackbarHostState() }
    LaunchedEffect(deps) {
        if (deps.list.value.value == null) deps.refresh()
        deps.cast.ensure()
    }
    val title = item?.title?.takeIf { it.isNotBlank() } ?: path
    val age = relativeTime(item?.modified, now ?: Instant.now())
    Box(modifier.fillMaxSize().background(ArchieTheme.colors.surface).testTag("visual-screen:$path")) {
        Column(Modifier.fillMaxSize().windowInsetsPadding(WindowInsets.safeDrawing.only(WindowInsetsSides.Top + WindowInsetsSides.Horizontal))) {
            ArchieTopAppBar(
                title = title,
                navigationIcon = { ArchieIconButton(ArchieIcons.Close, "Close", onBack) },
                subtitle = { TopAppBarSubtitle(if (deleted) "Visuals · deleted" else if (age != null) "Visuals · updated $age" else "Visuals") },
            ) {
                UpdatedCue(cueAt, Modifier.padding(end = 4.dp))
                ShowOnTvButton(deps, path, title, snackbar, compact = true)
                VisualMenu(href, onReload = { reload++ }, snackbar)
            }
            VisualWebView(deps, path, href, reload, Modifier.weight(1f).fillMaxWidth(), live = live, onLiveReload = { cueAt = System.currentTimeMillis() })
        }
        ArchieSnackbarHost(snackbar, Modifier.align(Alignment.BottomCenter).windowInsetsPadding(WindowInsets.navigationBars))
    }
}

/**
 * A visual as a workspace tab on Medium/Expanded (IA §9.2; web `VisualViewer`): a slim bar with
 * "Updated 2 h ago · folder", Show on TV and ⋮, over the pooled WebView, which stays alive while the
 * tab is hidden (VZ-4).
 */
@Composable
fun VisualViewer(deps: VisualsDeps, path: String, modifier: Modifier = Modifier, now: Instant? = null) {
    val item = rememberVisual(deps, path)
    val href = VisualUrls.href(deps.origin(), path, item?.url)
    var reload by rememberSaveable(path) { mutableIntStateOf(0) }
    val (live, deleted) = rememberLive(deps, path)
    var cueAt by remember(path) { mutableLongStateOf(0L) }
    val snackbar = remember { SnackbarHostState() }
    LaunchedEffect(deps) {
        if (deps.list.value.value == null) deps.refresh()
        deps.cast.ensure()
    }
    val title = item?.title?.takeIf { it.isNotBlank() } ?: path
    val updated = if (deleted) "Deleted" else relativeTime(item?.modified, now ?: Instant.now())?.let { "Updated $it" }
    val meta = listOfNotNull(updated, VisualUrls.folder(path)).joinToString(" · ")
    val c = ArchieTheme.colors
    Box(modifier.fillMaxSize().background(c.surface).testTag("visual-viewer:$path")) {
        Column(Modifier.fillMaxSize()) {
            Row(
                Modifier.fillMaxWidth().height(52.dp).padding(start = 20.dp, end = 4.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(meta, Modifier.weight(1f), style = ArchieTheme.typography.bodySmall.copy(fontSize = 13.sp), color = c.onSurfaceVariant, maxLines = 1, overflow = TextOverflow.Ellipsis)
                UpdatedCue(cueAt)
                ShowOnTvButton(deps, path, title, snackbar, compact = false)
                VisualMenu(href, onReload = { reload++ }, snackbar)
            }
            VisualWebView(deps, path, href, reload, Modifier.weight(1f).fillMaxWidth(), live = live, onLiveReload = { cueAt = System.currentTimeMillis() })
        }
        ArchieSnackbarHost(snackbar, Modifier.align(Alignment.BottomCenter))
    }
}

/** "Show on TV" (tonal, cast icon), present only while the capability is Available. */
@Composable
internal fun ShowOnTvButton(deps: VisualsDeps, path: String, title: String, snackbar: SnackbarHostState, compact: Boolean) {
    val capability by deps.cast.capability.collectAsStateWithLifecycle()
    if (capability != CastCapability.Available) return
    var casting by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    ArchieButton(
        "Show on TV",
        {
            casting = true
            scope.launch {
                val r = deps.cast.cast(path, title)
                casting = false
                snackbar.showSnackbar(r.message)
            }
        },
        Modifier.testTag("show-on-tv").padding(end = 4.dp),
        style = ButtonStyle.Tonal,
        size = if (compact) ButtonSize.Medium else ButtonSize.Small,
        icon = ArchieIcons.Cast,
        enabled = !casting,
    )
}

/** ⋮ Reload / Open in browser / Copy link (mockup phone (h) menu). */
@Composable
internal fun VisualMenu(href: String, onReload: () -> Unit, snackbar: SnackbarHostState) {
    var open by remember { mutableStateOf(false) }
    val context = LocalContext.current
    val clipboard = LocalClipboard.current
    val scope = rememberCoroutineScope()
    Box {
        ArchieIconButton(ArchieIcons.MoreVert, "Visual menu", { open = true }, Modifier.testTag("visual-menu"))
        ArchieDropdownMenu(open, onDismissRequest = { open = false }) {
            ArchieMenuItem("Reload", { open = false; onReload() }, icon = ArchieIcons.Refresh)
            ArchieMenuItem("Open in browser", { open = false; ExternalLinks.open(context, href) }, icon = ArchieIcons.OpenInNew)
            ArchieMenuItem("Copy link", {
                open = false
                scope.launch {
                    clipboard.setClipEntry(ClipEntry(ClipData.newPlainText("visual link", href)))
                    snackbar.showSnackbar("Link copied")
                }
            }, icon = ArchieIcons.Link)
        }
    }
}
