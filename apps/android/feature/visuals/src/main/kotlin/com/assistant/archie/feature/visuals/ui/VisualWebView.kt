package com.assistant.archie.feature.visuals.ui

import android.webkit.URLUtil
import android.widget.FrameLayout
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.setValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalInspectionMode
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import com.assistant.archie.feature.visuals.ExternalLinks
import com.assistant.archie.feature.visuals.VisualsDeps
import com.assistant.archie.feature.visuals.findActivity
import com.assistant.archie.feature.visuals.web.ArchieWebViewConfig
import com.assistant.archie.feature.visuals.web.PageListener
import com.assistant.archie.feature.visuals.web.PendingDownload
import com.assistant.archie.feature.visuals.web.WebViewPool
import com.assistant.core.design.components.ArchieButton
import com.assistant.core.design.components.ArchieConfirmDialog
import com.assistant.core.design.components.ButtonStyle
import com.assistant.core.design.icons.ArchieIcon
import com.assistant.core.design.icons.ArchieIcons
import com.assistant.core.design.theme.ArchieTheme
import androidx.compose.material3.LinearProgressIndicator
import kotlinx.coroutines.delay

/** Changes closer together than this reload once (spec 12 VZ-6). */
const val LIVE_RELOAD_DEBOUNCE_MS = 300L

/**
 * The pooled, sandboxed WebView of one visual (spec 14 §4.2). [key] is the visual's path: the same
 * WebView comes back after a tab switch or rotation ([WebViewPool]), so the page keeps its state
 * (VZ-4). [reload] is a counter: each increment reloads the visual's URL once (the web remounts its
 * iframe). External links leave for a Custom Tab; downloads ask first; a refused certificate or a
 * failed load shows an error with Retry instead of a blank page.
 *
 * [live] is the visual's change counter (spec 12 §9.3, `0` = none or deleted): when it is ahead of
 * what the pooled page loaded, the page reloads once per burst (debounced) with `reload()`, which
 * keeps the scroll position, and [onLiveReload] fires (the "Updated" cue). The counter lives on the
 * pool entry, so a change that arrived while the tab was hidden reloads it when it shows again.
 */
@Composable
fun VisualWebView(
    deps: VisualsDeps,
    key: String,
    url: String,
    reload: Int,
    modifier: Modifier = Modifier,
    live: Int = 0,
    onLiveReload: () -> Unit = {},
) {
    if (LocalInspectionMode.current) {
        Box(modifier.fillMaxSize().background(ArchieTheme.colors.surfaceContainerLowest))
        return
    }
    val context = LocalContext.current
    val host = remember(context) { context.findActivity() ?: context }
    val pool = deps.pool
    // A dead renderer (onRenderProcessGone) bumps the generation: the pooled WebView is evicted and a new one built.
    var generation by remember(key) { mutableIntStateOf(0) }
    val token = remember(key, generation) { Any() }
    val entry = remember(key, host, generation) {
        pool.acquire(key, host, token) { e ->
            ArchieWebViewConfig.configure(e.webView, deps.trust, { e.listener }, deps.debug, onGone = { pool.evict(e.key) })
            e.webView.setDownloadListener { dlUrl, userAgent, contentDisposition, mimeType, _ ->
                e.page.pendingDownload = PendingDownload(dlUrl, URLUtil.guessFileName(dlUrl, contentDisposition, mimeType), mimeType, userAgent)
            }
            e.liveVersion = live
            e.webView.loadUrl(url)
        }
    }
    val page = entry.page
    DisposableEffect(entry) {
        entry.listener = object : PageListener {
            override fun onExternal(url: String) = ExternalLinks.open(host, url)
            override fun onProgress(percent: Int) { page.progress = percent }
            override fun onPageStarted(url: String) { page.loading = true; page.error = null }
            override fun onPageFinished(url: String) { page.loading = false }
            override fun onMainFrameError(url: String, description: String, certificate: Boolean) {
                page.error = description
                page.loading = false
            }
            override fun onRenderProcessGone() { generation++ }
        }
        onDispose { pool.release(key, token) }
    }
    LaunchedEffect(entry, reload) {
        if (reload != entry.handledReload) {
            entry.handledReload = reload
            page.error = null
            entry.webView.loadUrl(url)
        }
    }
    LaunchedEffect(entry, live) {
        if (live > entry.liveVersion) {
            delay(LIVE_RELOAD_DEBOUNCE_MS) // a newer change restarts the effect: one reload per burst
            entry.liveVersion = live
            page.error = null
            entry.webView.reload()
            onLiveReload()
        }
    }
    Box(modifier.fillMaxSize().testTag("visual-webview:$key")) {
        AndroidView(
            factory = { ctx ->
                FrameLayout(ctx).apply {
                    (entry.webView.parent as? android.view.ViewGroup)?.removeView(entry.webView)
                    addView(entry.webView, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
                }
            },
            modifier = Modifier.fillMaxSize(),
        )
        if (page.loading && page.progress in 1..99) {
            LinearProgressIndicator(
                progress = { page.progress / 100f },
                modifier = Modifier.fillMaxWidth().height(2.dp).align(Alignment.TopCenter),
                color = ArchieTheme.colors.primary,
                trackColor = ArchieTheme.colors.surfaceContainerHighest,
                drawStopIndicator = {},
            )
        }
        page.error?.let { err ->
            Column(
                Modifier.fillMaxSize().background(ArchieTheme.colors.surface).padding(24.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.spacedBy(12.dp, Alignment.CenterVertically),
            ) {
                ArchieIcon(ArchieIcons.CloudOff, null, size = 32.dp, tint = ArchieTheme.colors.onSurfaceVariant)
                Text("Could not load this visual", style = ArchieTheme.typography.titleMedium, color = ArchieTheme.colors.onSurface)
                Text(err, Modifier.widthIn(max = 420.dp), style = ArchieTheme.typography.bodyMedium, color = ArchieTheme.colors.onSurfaceVariant, textAlign = TextAlign.Center)
                ArchieButton("Retry", { page.error = null; entry.webView.loadUrl(url) }, style = ButtonStyle.Tonal, icon = ArchieIcons.Refresh)
            }
        }
    }
    page.pendingDownload?.let { d ->
        ArchieConfirmDialog(
            title = "Download file?",
            text = "“${d.fileName}” from this visual will be saved to Downloads.",
            confirmLabel = "Download",
            onConfirm = {
                page.pendingDownload = null
                ExternalLinks.download(host, d)
            },
            onDismissRequest = { page.pendingDownload = null },
        )
    }
}
