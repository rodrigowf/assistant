package com.assistant.archie.feature.memory.ui

import android.content.ClipData
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
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
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.SnackbarDuration
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.SnackbarResult
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.Immutable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.ClipEntry
import androidx.compose.ui.platform.LocalClipboard
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.assistant.archie.feature.memory.MemoryDeps
import com.assistant.archie.feature.memory.MemoryLink
import com.assistant.archie.feature.memory.MemoryLinkResolver
import com.assistant.archie.feature.memory.fileName
import com.assistant.archie.feature.memory.filePaths
import com.assistant.archie.feature.memory.folderCrumb
import com.assistant.archie.feature.memory.frontmatterChipLabel
import com.assistant.archie.feature.memory.modifiedLine
import com.assistant.core.design.components.ArchieButton
import com.assistant.core.design.components.ArchieDropdownMenu
import com.assistant.core.design.components.ArchieIconButton
import com.assistant.core.design.components.ArchieMenuItem
import com.assistant.core.design.components.ArchieSnackbarHost
import com.assistant.core.design.components.ArchieTopAppBar
import com.assistant.core.design.components.ButtonSize
import com.assistant.core.design.components.ButtonStyle
import com.assistant.core.design.components.Spinner
import com.assistant.core.design.components.TopAppBarSubtitle
import com.assistant.core.design.icons.ArchieIcon
import com.assistant.core.design.icons.ArchieIcons
import com.assistant.core.design.theme.ArchieTheme
import com.assistant.core.data.ContentStamp
import com.assistant.core.markdown.Frontmatter
import com.assistant.core.markdown.InternalLinks
import com.assistant.core.markdown.MdNode
import com.assistant.core.markdown.Slugger
import com.assistant.core.markdown.ui.MarkdownBlock
import com.assistant.core.markdown.ui.MarkdownStyle
import com.assistant.core.markdown.ui.rememberMarkdown
import com.assistant.core.network.ApiResult
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import java.time.Instant

/**
 * What the document view shows for one path (web parity: MemoryDocument.tsx `DocState`).
 * [changedAt]: when a live refetch last brought different text (the "Updated" cue), 0 = never.
 */
@Immutable
data class MemoryDocState(val text: String? = null, val error: String? = null, val loading: Boolean = true, val changedAt: Long = 0L)

/** Changes closer together than this refetch once (spec 12 §9.3). */
const val LIVE_REFETCH_DEBOUNCE_MS = 300L

/**
 * Loads `GET /memory/<path>` on open and on every [reload] (spec 14 §4.1). The previous text of the
 * same file stays visible while a reload runs and after a failed one (inv02 F-37); a late answer
 * for an old path is dropped (the effect is keyed by path).
 *
 * Live (spec 12 §9.3, VZ-6): a new [changes] entry for [path], or a new [resync] epoch, refetches
 * the same way (debounced), so the list keeps its scroll position; [MemoryDocState.changedAt] is set
 * only when the text actually changed.
 */
@Composable
fun rememberMemoryDocument(
    path: String,
    load: suspend (String) -> ApiResult<String>,
    changes: StateFlow<Map<String, ContentStamp>> = NO_CHANGES,
    resync: StateFlow<Int> = NO_RESYNC,
): Pair<MemoryDocState, () -> Unit> {
    var tick by rememberSaveable(path) { mutableIntStateOf(0) }
    var state by remember(path) { mutableStateOf(MemoryDocState()) }
    val stamp = changes.collectAsStateWithLifecycle().value[path]
    val epoch by resync.collectAsStateWithLifecycle()
    val liveKey = "${stamp?.version ?: 0}:$epoch"
    // What this view already reflects (a change before it opened is in the first fetch).
    var handled by remember(path) { mutableStateOf(liveKey) }
    var liveTick by remember(path) { mutableIntStateOf(-1) }
    LaunchedEffect(path, liveKey) {
        if (liveKey == handled || stamp?.deleted == true) return@LaunchedEffect
        delay(LIVE_REFETCH_DEBOUNCE_MS)
        handled = liveKey
        liveTick = tick + 1
        tick++
    }
    LaunchedEffect(path, tick) {
        val before = state.text
        state = state.copy(loading = true, error = null)
        state = when (val r = load(path)) {
            is ApiResult.Ok -> {
                val changed = tick == liveTick && before != null && before != r.value
                MemoryDocState(r.value, null, loading = false, changedAt = if (changed) System.currentTimeMillis() else state.changedAt)
            }
            else -> state.copy(error = r.errorMessage(), loading = false)
        }
    }
    return state to { tick++ }
}

private val NO_CHANGES: StateFlow<Map<String, ContentStamp>> = MutableStateFlow(emptyMap())
private val NO_RESYNC: StateFlow<Int> = MutableStateFlow(0)

/** How long the "Updated" cue stays. */
const val UPDATED_CUE_MS = 2_500L

/** A short-lived "Updated" pill after a live refetch changed the text (web `.updatedCue`). */
@Composable
private fun UpdatedCue(changedAt: Long, modifier: Modifier = Modifier) {
    var visible by remember { mutableStateOf(false) }
    LaunchedEffect(changedAt) {
        if (changedAt == 0L) return@LaunchedEffect
        visible = true
        delay(UPDATED_CUE_MS)
        visible = false
    }
    if (!visible) return
    val c = ArchieTheme.colors
    Text(
        "Updated",
        modifier
            .testTag("memory-updated")
            .semantics { liveRegion = LiveRegionMode.Polite }
            .background(c.secondaryContainer, RoundedCornerShape(50))
            .padding(horizontal = 10.dp, vertical = 2.dp),
        style = ArchieTheme.typography.labelMedium,
        color = c.onSecondaryContainer,
    )
}

/**
 * Memory document on Compact (spec 14 §4.1 `MemoryDocumentScreen`, mockup phone (g2)): back arrow,
 * file name over its folder, Reload and ⋮ (Copy path, Open raw); the body as in [MemoryDocumentContent].
 * A link to another memory file calls [onOpenDoc] (the shell pushes a new entry, so Back returns).
 */
@Composable
fun MemoryDocumentScreen(
    deps: MemoryDeps,
    path: String,
    onBack: () -> Unit,
    onOpenDoc: (String) -> Unit,
    modifier: Modifier = Modifier,
    now: Instant? = null,
    /** A visualization link (spec 12 §9.4); null = [MemoryDeps.openVisual]. */
    onOpenVisual: ((String) -> Unit)? = null,
) {
    val (doc, reload) = rememberMemoryDocument(path, deps::document, deps.changes, deps.resyncEpoch)
    val snackbar = remember { SnackbarHostState() }
    Box(modifier.fillMaxSize().background(ArchieTheme.colors.surface).testTag("memory-doc:$path")) {
        Column(Modifier.fillMaxSize().windowInsetsPadding(WindowInsets.safeDrawing.only(WindowInsetsSides.Top + WindowInsetsSides.Horizontal))) {
            val crumb = folderCrumb(path)
            ArchieTopAppBar(
                title = fileName(path),
                navigationIcon = { ArchieIconButton(ArchieIcons.ArrowBack, "Back", onBack) },
                subtitle = { TopAppBarSubtitle(crumb.ifEmpty { "memory" }) },
            ) {
                UpdatedCue(doc.changedAt, Modifier.padding(end = 4.dp))
                if (doc.loading && doc.text != null) Box(Modifier.padding(12.dp)) { Spinner(size = 20.dp) }
                ArchieIconButton(ArchieIcons.Refresh, "Reload", reload)
                DocMenu(deps, path, snackbar)
            }
            MemoryDocumentBody(deps, path, doc, reload, onOpenDoc, snackbar, Modifier.weight(1f), now, onOpenVisual)
        }
        ArchieSnackbarHost(snackbar, Modifier.align(Alignment.BottomCenter).windowInsetsPadding(WindowInsets.navigationBars))
    }
}

/**
 * Memory document as a workspace tab on Medium/Expanded (IA §9.2): the tab strip carries the title,
 * so a slim bar shows the folder, Reload and ⋮; links open other files as tabs ([onOpenDoc]).
 */
@Composable
fun MemoryDocumentContent(deps: MemoryDeps, path: String, onOpenDoc: (String) -> Unit, modifier: Modifier = Modifier, now: Instant? = null) {
    val (doc, reload) = rememberMemoryDocument(path, deps::document, deps.changes, deps.resyncEpoch)
    val snackbar = remember { SnackbarHostState() }
    val c = ArchieTheme.colors
    Box(modifier.fillMaxSize().background(c.surface).testTag("memory-doc:$path")) {
        Column(Modifier.fillMaxSize()) {
            Row(
                Modifier.fillMaxWidth().height(48.dp).padding(start = 20.dp, end = 4.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                Text(
                    folderCrumb(path).ifEmpty { "memory" },
                    Modifier.weight(1f),
                    style = ArchieTheme.typography.bodySmall.copy(fontSize = 13.sp),
                    color = c.onSurfaceVariant,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                UpdatedCue(doc.changedAt)
                if (doc.loading && doc.text != null) Spinner(size = 18.dp)
                ArchieIconButton(ArchieIcons.Refresh, "Reload", reload, size = 40.dp, iconSize = 20.dp)
                DocMenu(deps, path, snackbar)
            }
            MemoryDocumentBody(deps, path, doc, reload, onOpenDoc, snackbar, Modifier.weight(1f), now)
        }
        ArchieSnackbarHost(snackbar, Modifier.align(Alignment.BottomCenter))
    }
}

@Composable
private fun DocMenu(deps: MemoryDeps, path: String, snackbar: SnackbarHostState) {
    var open by remember { mutableStateOf(false) }
    val clipboard = LocalClipboard.current
    val scope = rememberCoroutineScope()
    Box {
        ArchieIconButton(ArchieIcons.MoreVert, "More", { open = true })
        ArchieDropdownMenu(open, onDismissRequest = { open = false }) {
            ArchieMenuItem("Copy path", {
                open = false
                scope.launch {
                    clipboard.setClipEntry(ClipEntry(ClipData.newPlainText("memory path", path)))
                    snackbar.showSnackbar("Path copied")
                }
            }, icon = ArchieIcons.ContentCopy)
            ArchieMenuItem("Open raw", {
                open = false
                deps.openExternal(deps.origin() + MemoryLinkResolver.memoryFileUrl(path))
            }, icon = ArchieIcons.OpenInNew)
        }
    }
}

/**
 * The scrolling body: frontmatter chip (collapsed), the `# Title`, "Modified …", then the markdown,
 * one lazy item per block (a 2,000-line file scrolls like chat). Links go through
 * [MemoryLinkResolver]: another file → [onOpenDoc] (a path missing from the tree asks first:
 * "Not found: x.md" + "Open anyway"), `#anchor` → scroll, anything else → [MemoryDeps.openExternal].
 */
@Composable
internal fun MemoryDocumentBody(
    deps: MemoryDeps,
    path: String,
    doc: MemoryDocState,
    reload: () -> Unit,
    onOpenDoc: (String) -> Unit,
    snackbar: SnackbarHostState,
    modifier: Modifier = Modifier,
    now: Instant? = null,
    onOpenVisual: ((String) -> Unit)? = null,
) {
    val c = ArchieTheme.colors
    val tree by deps.tree.collectAsStateWithLifecycle()
    val known = remember(tree.value) { tree.value?.let { filePaths(it) } }
    val split = remember(doc.text) { doc.text?.let { Frontmatter.split(it) } }
    val body = split?.body
    val nodes = if (body == null) null else rememberMarkdown("memory:$path", body)
    val modified = remember(split?.frontmatter, now) { modifiedLine(split?.frontmatter, now ?: Instant.now()) }
    val list = rememberLazyListState()
    val scope = rememberCoroutineScope()
    val style = MarkdownStyle.fromTheme().copy(autoLinkPaths = true, linkOrigin = deps.origin())
    val linkCtx = remember(deps) { InternalLinks.Context(deps.origin()) }
    // Items before the markdown blocks: [frontmatter], [error], [loading] (see below).
    val hasFm = split?.frontmatter != null
    val titleFirst = nodes?.firstOrNull().let { it is MdNode.Heading && it.level == 1 }

    val scrollTo: (String) -> Unit = { id ->
        val target = Slugger.slugOf(id)
        val idx = nodes?.indexOfFirst { it is MdNode.Heading && (it.anchor == id || it.anchor == target) } ?: -1
        if (idx >= 0) {
            val extra = (if (hasFm) 1 else 0) + (if (doc.error != null) 1 else 0) +
                (if (modified != null && (!titleFirst || idx > 0)) 1 else 0)
            scope.launch { list.animateScrollToItem(idx + extra) }
        }
    }
    val onLink: (String) -> Unit = { href ->
        when (val l = MemoryLinkResolver.resolve(path, href, linkCtx)) {
            is MemoryLink.Anchor -> scrollTo(l.id)
            is MemoryLink.Document -> when {
                l.path == path -> l.fragment?.let(scrollTo)
                known != null && l.path !in known -> scope.launch {
                    val r = snackbar.showSnackbar("Not found: ${l.path}", actionLabel = "Open anyway", duration = SnackbarDuration.Long)
                    if (r == SnackbarResult.ActionPerformed) onOpenDoc(l.path)
                }
                else -> onOpenDoc(l.path)
            }
            is MemoryLink.Visual -> onOpenVisual?.invoke(l.path) ?: deps.openVisual(l.path)
            is MemoryLink.External -> deps.openExternal(l.url)
            is MemoryLink.Server -> deps.openExternal(deps.origin() + l.path)
        }
    }

    Box(modifier.fillMaxWidth(), contentAlignment = Alignment.TopCenter) {
        LazyColumn(
            Modifier.widthIn(max = 840.dp).fillMaxSize().testTag("memory-doc-list"),
            state = list,
            contentPadding = PaddingValues(start = 20.dp, end = 20.dp, top = 4.dp, bottom = 32.dp),
        ) {
            split?.frontmatter?.let { fm ->
                item(key = "fm", contentType = "fm") { FrontmatterChip(fm, Modifier.padding(bottom = 12.dp)) }
            }
            if (doc.error != null) {
                item(key = "error", contentType = "error") {
                    Row(Modifier.fillMaxWidth().padding(bottom = 12.dp), verticalAlignment = Alignment.CenterVertically) {
                        Text("Could not load $path — ${doc.error}", Modifier.weight(1f), style = ArchieTheme.typography.bodyMedium, color = c.error)
                        ArchieButton("Retry", reload, style = ButtonStyle.Text, size = ButtonSize.Small)
                    }
                }
            }
            if (doc.text == null && doc.loading) {
                item(key = "loading", contentType = "loading") {
                    Row(Modifier.padding(vertical = 16.dp), horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                        Spinner(size = 18.dp)
                        Text("Loading…", style = ArchieTheme.typography.bodyMedium, color = c.onSurfaceVariant)
                    }
                }
            }
            if (nodes != null) {
                // No `# Title`: the modified line sits at the top, after the chip.
                if (!titleFirst && modified != null) item(key = "modified", contentType = "modified") { ModifiedText(modified) }
                for (i in nodes.indices) {
                    val node = nodes[i]
                    item(key = "md:$i", contentType = node.contentType) {
                        val prev = if (i > 0) nodes[i - 1] else null
                        MarkdownBlock(node, Modifier.padding(top = style.spacingBefore(prev, node)), style, onLink)
                    }
                    if (i == 0 && titleFirst && modified != null) {
                        item(key = "modified", contentType = "modified") { ModifiedText(modified) }
                    }
                }
            }
        }
    }
}

@Composable
private fun ModifiedText(text: String) {
    Text(
        text,
        Modifier.padding(top = 4.dp),
        style = ArchieTheme.typography.bodySmall,
        color = ArchieTheme.colors.onSurfaceVariant,
    )
}

/**
 * The frontmatter folded into one chip (mockup g2, web `Disclosure variant="chip"`): "Frontmatter ·
 * architecture · 4 refs", expanding in place to the raw YAML in monospace. A parse failure still
 * shows the raw text.
 */
@Composable
fun FrontmatterChip(frontmatter: String, modifier: Modifier = Modifier, initiallyExpanded: Boolean = false) {
    val label = remember(frontmatter) { frontmatterChipLabel(frontmatter) }
    var expanded by rememberSaveable(frontmatter) { mutableStateOf(initiallyExpanded) }
    val c = ArchieTheme.colors
    val shape = RoundedCornerShape(8.dp)
    Column(modifier, verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Row(
            Modifier
                .height(32.dp)
                .clip(shape)
                .border(1.dp, c.outlineVariant, shape)
                .clickable(role = Role.Button) { expanded = !expanded }
                .semantics { stateDescription = if (expanded) "Expanded" else "Collapsed" }
                .testTag("frontmatter-chip")
                .padding(start = 8.dp, end = 8.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            ArchieIcon(ArchieIcons.DataObject, null, size = 18.dp, tint = c.onSurfaceVariant)
            Text(label, Modifier.weight(1f, fill = false), color = c.onSurfaceVariant, style = ArchieTheme.typography.labelLarge, maxLines = 1, overflow = TextOverflow.Ellipsis)
            ArchieIcon(if (expanded) ArchieIcons.KeyboardArrowUp else ArchieIcons.KeyboardArrowDown, null, size = 18.dp, tint = c.onSurfaceVariant)
        }
        if (expanded) {
            Text(
                frontmatter,
                Modifier
                    .fillMaxWidth()
                    .background(c.surfaceContainer, RoundedCornerShape(12.dp))
                    .padding(horizontal = 14.dp, vertical = 12.dp),
                color = c.onSurfaceVariant,
                style = ArchieTheme.text.codeSmall,
            )
        }
    }
}
