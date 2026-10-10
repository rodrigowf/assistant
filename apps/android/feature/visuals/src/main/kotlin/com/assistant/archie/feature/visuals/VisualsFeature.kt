package com.assistant.archie.feature.visuals

import android.app.Activity
import android.app.DownloadManager
import android.content.ActivityNotFoundException
import android.content.Context
import android.content.ContextWrapper
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Environment
import androidx.browser.customtabs.CustomTabsIntent
import com.assistant.archie.feature.visuals.web.PendingDownload
import com.assistant.archie.feature.visuals.web.WebTrust
import com.assistant.archie.feature.visuals.web.WebViewPool
import com.assistant.core.data.LoadState
import com.assistant.core.data.VisualsRepository
import com.assistant.core.model.VisualInfo
import com.assistant.core.network.ApiResult
import com.assistant.core.network.UrlScheme
import com.assistant.core.data.ContentChangesRepository
import com.assistant.core.data.ContentStamp
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

/**
 * What the visuals screens need from the app (spec 14 §2.2: the shell wires it from the graph):
 * the list, rename, the cast controller, the process-scoped [WebViewPool] and the [WebTrust] the
 * WebView uses for the self-signed server certificate.
 */
interface VisualsDeps {
    val list: StateFlow<LoadState<List<VisualInfo>>>
    fun refresh()

    /** Optimistic `PATCH /api/visualizations/rename` (404 tolerated), then a refresh. */
    suspend fun rename(path: String, title: String): ApiResult<Unit>

    val cast: CastController
    val pool: WebViewPool
    val trust: WebTrust

    /** `http(s)://host[:port]` of the current server, no trailing slash. */
    fun origin(): String = trust.origin()

    /** Debug builds route a viz's `console.*` to logcat (`ArchieViz`). */
    val debug: Boolean get() = false

    /** Per-visualization change counters (spec 12 §9.3): an open page reloads when its entry moves. */
    val changes: StateFlow<Map<String, ContentStamp>> get() = NO_CHANGES
}

private val NO_CHANGES: StateFlow<Map<String, ContentStamp>> = MutableStateFlow(emptyMap())

/** [VisualsDeps] over B-03's [VisualsRepository]. */
class RepositoryVisualsDeps(
    private val repository: VisualsRepository,
    override val cast: CastController,
    override val pool: WebViewPool,
    override val trust: WebTrust,
    override val debug: Boolean = false,
    private val content: ContentChangesRepository? = null,
) : VisualsDeps {
    override val changes: StateFlow<Map<String, ContentStamp>> get() = content?.visuals ?: super.changes
    override val list: StateFlow<LoadState<List<VisualInfo>>> get() = repository.list
    override fun refresh() = repository.refresh()
    override suspend fun rename(path: String, title: String): ApiResult<Unit> = repository.rename(path, title)
}

/** Opening things outside the app (spec 14 §4.2: Custom Tab; ACTION_VIEW for other schemes). */
object ExternalLinks {
    /**
     * Schemes a markdown link may open. `file:` throws FileUriExposedException, and `content:`,
     * `javascript:` and `intent:` must never be launched from text an agent wrote.
     */
    private val ALLOWED = setOf("http", "https", "mailto", "tel", "sms", "geo")

    fun open(context: Context, url: String) {
        val uri = Uri.parse(url)
        val scheme = uri.scheme?.lowercase()
        if (scheme !in ALLOWED) return
        val newTask = context.findActivity() == null
        try {
            if (scheme == "http" || scheme == "https") {
                val tab = CustomTabsIntent.Builder().setShowTitle(true).build()
                if (newTask) tab.intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                tab.launchUrl(context, uri)
            } else {
                context.startActivity(Intent(Intent.ACTION_VIEW, uri).apply { if (newTask) addFlags(Intent.FLAG_ACTIVITY_NEW_TASK) })
            }
        } catch (_: ActivityNotFoundException) {
            // No browser / handler: nothing to open it with. The link stays inert rather than crashing.
        } catch (_: RuntimeException) {
            // A malformed or refused URI (SecurityException, …): inert too.
        }
    }

    /** `DownloadManager` after the user said yes (spec 14 §4.2: downloads ask first). */
    fun download(context: Context, d: PendingDownload): Boolean = try {
        val req = DownloadManager.Request(Uri.parse(d.url))
            .setTitle(d.fileName)
            .setMimeType(d.mimeType)
            .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
            .apply { d.userAgent?.let { addRequestHeader("User-Agent", it) } }
        // API 29+: the public Downloads folder needs no permission; 26–28: the app's own folder.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) req.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, d.fileName)
        else req.setDestinationInExternalFilesDir(context, Environment.DIRECTORY_DOWNLOADS, d.fileName)
        (context.getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager).enqueue(req)
        true
    } catch (_: RuntimeException) {
        false
    }
}

internal fun Context.findActivity(): Activity? {
    var c: Context? = this
    while (c != null) {
        if (c is Activity) return c
        c = (c as? ContextWrapper)?.baseContext
    }
    return null
}

/** `http(s)://host[:port]` for a stored server URL. */
fun originOf(serverUrl: String): String = UrlScheme.httpBase(serverUrl)
