package com.assistant.archie.shell

import android.content.Context
import android.content.pm.ApplicationInfo
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.platform.LocalContext
import com.assistant.archie.feature.memory.MemoryDeps
import com.assistant.archie.feature.memory.RepositoryMemoryDeps
import com.assistant.archie.feature.visuals.CastController
import com.assistant.archie.feature.visuals.ExternalLinks
import com.assistant.archie.feature.visuals.RepositoryVisualsDeps
import com.assistant.archie.feature.visuals.VisualsDeps
import com.assistant.archie.feature.visuals.web.WebTrust
import com.assistant.archie.feature.visuals.web.WebViewPool
import com.assistant.archie.graph.MainAppGraph
import com.assistant.core.data.ConnectionStatus
import com.assistant.core.markdown.InternalLinks
import com.assistant.core.model.DeviceSettings
import com.assistant.core.network.UrlScheme
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.filter
import kotlinx.coroutines.flow.launchIn
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.onEach
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.util.WeakHashMap

/*
 * B-07 wiring: Memory and Visuals built from the process-scoped graph (spec 14 §2.2). One
 * VisualsDeps per graph (process): the WebViewPool and the cast capability are process-scoped
 * (spec 14 §4.2). B-09 may move this into MainAppGraph when it takes the graph over.
 */

private val visualsByGraph = WeakHashMap<MainAppGraph, VisualsDeps>()

private fun MainAppGraph.serverUrl(): String = settings.settings.value?.serverUrl ?: DeviceSettings.DEFAULT_SERVER_URL

fun visualsDepsOf(graph: MainAppGraph, context: Context): VisualsDeps = synchronized(visualsByGraph) {
    visualsByGraph.getOrPut(graph) {
        val app = context.applicationContext
        val cast = CastController(graph.visuals::castProbe, graph.visuals::cast, graph.scope)
        // BX-2 capability: resolved once per server connection (spec 14 §4.2).
        graph.connection.status
            .map { (it.phase == ConnectionStatus.Phase.CONNECTED) to it.serverUrl }
            .distinctUntilChanged()
            .filter { it.first }
            .onEach { cast.onConnected() }
            .launchIn(graph.scope)
        RepositoryVisualsDeps(
            repository = graph.visuals,
            cast = cast,
            pool = WebViewPool(app),
            trust = WebTrust(serverUrl = { graph.serverUrl() }, pinFor = graph.settings::pinFor),
            debug = (app.applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE) != 0,
            content = graph.content,
        )
    }
}

@Composable
fun rememberVisualsDeps(graph: MainAppGraph): VisualsDeps {
    val context = LocalContext.current
    return remember(graph) { visualsDepsOf(graph, context) }
}

/** Memory deps for this composition: external links open from the current Activity (Custom Tab). */
@Composable
fun rememberMemoryDeps(graph: MainAppGraph): MemoryDeps {
    val context = LocalContext.current
    return remember(graph, context) {
        RepositoryMemoryDeps(
            repository = graph.memory,
            originOf = { UrlScheme.httpBase(graph.serverUrl()) },
            external = { url -> ExternalLinks.open(context, url) },
            visual = { path -> graph.openVisualChecked(path) { ExternalLinks.open(context, it) } },
            content = graph.content,
        )
    }
}

/**
 * Opens a visualization in the app only when it is a real page (spec 12 §9.4): in the list, or in
 * the list after a refresh (a page written a moment ago). Otherwise the
 * URL goes to [external] rather than framing a 404 in the viewer.
 */
fun MainAppGraph.openVisualChecked(path: String, external: (String) -> Unit) {
    fun listed() = visuals.list.value.value?.any { it.path == path } == true
    if (listed()) {
        openSessions.openVisual(path)
        return
    }
    scope.launch {
        visuals.refreshNow()
        withContext(Dispatchers.Main) {
            if (listed()) openSessions.openVisual(path)
            else external(UrlScheme.httpBase(serverUrl()) + InternalLinks.url(InternalLinks.Target.Visual(path)))
        }
    }
}

/**
 * Chat links (spec 12 §9.4): a visualization or memory file opens as a workspace item, like a
 * memory document's links do; anything else leaves the app (a root-relative path against the
 * current server; only web / mail / phone schemes, `ExternalLinks`).
 */
@Composable
fun rememberChatLinkHandler(graph: MainAppGraph): (String) -> Unit {
    val context = LocalContext.current
    return remember<(String) -> Unit>(graph, context) {
        { href ->
            val origin = UrlScheme.httpBase(graph.serverUrl())
            val listed = graph.visuals.list.value.value
            val ctx = InternalLinks.Context(origin) { p -> listed?.any { it.path == p } == true }
            when (val t = InternalLinks.resolve(href, ctx)) {
                is InternalLinks.Target.Visual -> graph.openVisualChecked(t.path) { ExternalLinks.open(context, it) }
                is InternalLinks.Target.Memory -> graph.openSessions.openMemory(t.path)
                null -> ExternalLinks.open(context, if (href.startsWith("/") && !href.startsWith("//")) origin + href else href)
            }
        }
    }
}
