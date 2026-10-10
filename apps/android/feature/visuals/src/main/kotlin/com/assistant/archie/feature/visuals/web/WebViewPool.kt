package com.assistant.archie.feature.visuals.web

import android.content.ComponentCallbacks2
import android.content.Context
import android.content.MutableContextWrapper
import android.content.res.Configuration
import android.view.ViewGroup
import android.webkit.WebView
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue

/** Observable page state of one pooled WebView (written by its clients, read by Compose). */
class PageState {
    var progress by mutableIntStateOf(0)
    var loading by mutableStateOf(true)
    /** The main frame's error ("The server's certificate is not trusted", net::ERR_…); null when fine. */
    var error by mutableStateOf<String?>(null)
    var pendingDownload by mutableStateOf<PendingDownload?>(null)
}

data class PendingDownload(val url: String, val fileName: String, val mimeType: String?, val userAgent: String?)

/**
 * Process-scoped pool of visualization WebViews (spec 14 §4.2 "State retention"; VZ-4). The web
 * keeps a visual's iframe mounted while its tab is hidden, which is load-bearing (interactive vizzes
 * keep their state, inv02 F-36); here the WebView outlives its composable instead: tab switches,
 * rotation and back-and-forth keep the page alive.
 *
 * - At most [maxEntries] (3) WebViews, LRU by key (the visual's path). A WebView on screen
 *   ([Entry.attached]) is never evicted.
 * - Each WebView is built on a [MutableContextWrapper] whose base is the current Activity while
 *   attached and the Application while detached, so a pooled WebView never pins an Activity. It is
 *   *constructed* while the base is the Application: Chromium keeps what it looks up from the context
 *   at construction (e.g. `AwContents` → `AutofillProvider` → the Activity's `AutofillManager`, whose
 *   `mContext` is the Activity; heap dump on the POCO X7, Android 15), so a WebView built on an
 *   Activity keeps that Activity alive whatever the base is later. Popups that need an Activity
 *   (`<select>`, `alert`/`confirm`) look it up when they open; by then the base is the host.
 * - `onTrimMemory(RUNNING_LOW)` and above evict every detached WebView. `UI_HIDDEN` (the app just
 *   went to the background) does not: switching apps for a moment must not lose a viz's state.
 *
 * Main thread only.
 */
class WebViewPool(
    private val app: Context,
    val maxEntries: Int = 3,
    private val create: (Context) -> WebView = { WebView(it) },
) : ComponentCallbacks2 {

    class Entry internal constructor(val key: String, val webView: WebView, internal val context: MutableContextWrapper) {
        val page = PageState()
        var attached: Boolean = false
            internal set

        /** Reload requests already applied (a saved reload counter must not replay after rotation). */
        var handledReload: Int = 0

        /** The live-change counter (spec 12 §9.3) the page reflects; a newer one reloads it, even after the tab was away. */
        var liveVersion: Int = 0

        /** The host currently showing this page; the WebView's clients report to it. */
        @Volatile var listener: PageListener? = null

        /** Token of the composable that attached it last; a stale host's release is ignored. */
        internal var owner: Any? = null

        /** The base context the WebView currently sees (tests: Activity while attached, Application after). */
        val baseContext: Context get() = context.baseContext
    }

    private val entries = LinkedHashMap<String, Entry>(8, 0.75f, true)

    init {
        app.applicationContext.registerComponentCallbacks(this)
    }

    val size: Int get() = entries.size
    val keys: List<String> get() = entries.keys.toList()

    fun peek(key: String): Entry? = entries[key]

    /**
     * The WebView for [key], created on first use ([configure] runs once, on creation) and attached
     * to [host] (the Activity). It is removed from any old parent, so the caller can add it. [owner]
     * identifies the caller: during a screen transition the old host's [release] comes after the new
     * host's acquire and must not detach the page from it.
     */
    fun acquire(key: String, host: Context, owner: Any = host, configure: (Entry) -> Unit = {}): Entry {
        val existing = entries[key]
        val entry = existing ?: run {
            // Constructed on the Application, never on [host] (see the class doc); the base becomes [host] below.
            val wrapper = MutableContextWrapper(app.applicationContext)
            Entry(key, create(wrapper), wrapper).also {
                entries[key] = it
                configure(it)
            }
        }
        entry.context.baseContext = host
        entry.attached = true
        entry.owner = owner
        (entry.webView.parent as? ViewGroup)?.removeView(entry.webView)
        trim()
        return entry
    }

    /** The host left (tab hidden, screen closed, Activity destroyed): keep the page, drop the Activity. */
    fun release(key: String, owner: Any? = null) {
        val e = entries[key] ?: return
        if (owner != null && e.owner !== owner) return
        e.owner = null
        e.attached = false
        e.listener = null
        (e.webView.parent as? ViewGroup)?.removeView(e.webView)
        e.context.baseContext = app.applicationContext
        trim()
    }

    /** Destroys the WebView of [key] (closing its tab, memory pressure). */
    fun evict(key: String) {
        val e = entries.remove(key) ?: return
        (e.webView.parent as? ViewGroup)?.removeView(e.webView)
        e.context.baseContext = app.applicationContext
        e.listener = null
        e.webView.stopLoading()
        e.webView.destroy()
    }

    /** Keeps only the attached entries (memory pressure). */
    fun evictDetached() {
        entries.values.filter { !it.attached }.map { it.key }.forEach(::evict)
    }

    private fun trim() {
        while (entries.size > maxEntries) {
            val lru = entries.values.firstOrNull { !it.attached } ?: return
            evict(lru.key)
        }
    }

    override fun onTrimMemory(level: Int) {
        @Suppress("DEPRECATION")
        val pressure = level == ComponentCallbacks2.TRIM_MEMORY_RUNNING_LOW ||
            level == ComponentCallbacks2.TRIM_MEMORY_RUNNING_CRITICAL ||
            level >= ComponentCallbacks2.TRIM_MEMORY_BACKGROUND
        if (pressure) evictDetached()
    }

    override fun onConfigurationChanged(newConfig: Configuration) = Unit

    @Deprecated("Deprecated in Java")
    override fun onLowMemory() = evictDetached()
}
