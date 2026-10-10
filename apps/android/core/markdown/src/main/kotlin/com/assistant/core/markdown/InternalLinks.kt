package com.assistant.core.markdown

/**
 * Internal links (spec 12 §9.4, LNK-1…LNK-6): which links point at a visualization or a memory
 * file, so the host opens them in its own viewer instead of a Custom Tab. Pure; web twin
 * `apps/web/src/features/links/internalLinks.ts`, and both pass the shared corpus
 * `apps/protocol-fixtures/links/internal-links.json` (`InternalLinksTest`).
 *
 * - LNK-1 filesystem paths: `context/public/<x>.html` → visual, `context/memory/<x>.md` → memory
 *   (relative, `./`, absolute or `~/`); `docs/<x>.md` and `…/assistant/docs/<x>.md` → memory
 *   `archie/<x>.md`. A trailing `:line` / `:line-line` is dropped.
 * - LNK-2 root-relative URLs: `/memory/<x>.md` (`/memory/` → MEMORY.md), the old
 *   `/markdown_reader.html?file=memory/<x>.md`, `/<x>.html` or `/<dir>/` (→ `<dir>/index.html`)
 *   outside the app's own prefixes, with no query.
 * - LNK-3 `http(s)` URLs on the backend's host (any port) → as LNK-2.
 * - LNK-4 other private-network hosts → LNK-2 only for `/memory/…`, the reader, `/visualizations/…`
 *   or a path in the visualization list.
 */
object InternalLinks {
    sealed interface Target {
        /** [path] is relative to context/public/ (a `GET /api/visualizations` path). */
        data class Visual(val path: String) : Target

        /** [path] is relative to context/memory/; [fragment] without '#'. */
        data class Memory(val path: String, val fragment: String? = null) : Target
    }

    /** [origin]: the backend (`https://192.168.0.200`); [isVisual]: the visualization list, for LNK-4. */
    data class Context(val origin: String? = null, val isVisual: (String) -> Boolean = { false })

    private val RESERVED = setOf("api", "assets", "compat", "legacy", "legacy_compat", "next", "next-compat", "memory", "uploads", "projects")
    private val SCHEME = Regex("^([a-zA-Z][a-zA-Z0-9+.-]*):")
    private val HTML = Regex("\\.html?$", RegexOption.IGNORE_CASE)
    private val MD = Regex("\\.md$", RegexOption.IGNORE_CASE)
    private val LINE_SUFFIX = Regex(":\\d+(?:-\\d+)?$")
    private val CONTEXT_ROOT = Regex("(?:^|/)context/(public|memory)/(.+)$")
    private val DOCS_ROOT = Regex("^(?:\\./)?docs/(.+)$|^(?:~|/\\S*)/assistant/docs/(.+)$")
    private val FS_ABSOLUTE = Regex("^(?:~/|/(?:home|Users|root|tmp|var|etc|usr|opt|srv|mnt|media|private|Volumes)/)")
    private val URL = Regex("^[a-zA-Z]+://(\\[[^\\]]+\\]|[^/:?#]+)(?::\\d+)?([/?#].*)?$")
    private val ORIGIN_HOST = Regex("^[a-zA-Z][a-zA-Z0-9+.-]*://(\\[[^\\]]+\\]|[^/:?#]+)")
    private val IPV4 = Regex("^(\\d{1,3})\\.(\\d{1,3})\\.\\d{1,3}\\.\\d{1,3}$")
    private val UNSAFE_CODE = Regex("[\\s*?{}<>|]")

    /** Where [href] goes inside the app, or null when it is not an internal link. */
    fun resolve(href: String, ctx: Context = Context()): Target? {
        val h = href.trim()
        if (h.isEmpty() || h.startsWith("#") || h.startsWith("//")) return null
        val scheme = SCHEME.find(h)
        if (scheme != null) {
            val proto = scheme.groupValues[1].lowercase()
            if (proto != "http" && proto != "https") return null
            val m = URL.find(h) ?: return null
            val host = m.groupValues[1].lowercase()
            val rest = m.groupValues[2].ifEmpty { "/" }.let { if (it.startsWith("/")) it else "/$it" }
            val own = ctx.origin?.let { ORIGIN_HOST.find(it.trim())?.groupValues?.get(1)?.lowercase() }
            if (own != null && host == own) return fromRootPath(rest, lan = false, ctx)
            if (isPrivateHost(host)) return fromRootPath(rest, lan = true, ctx)
            return null
        }
        fromFilesystem(h)?.let { return it }
        if (h.startsWith("/") && !FS_ABSOLUTE.containsMatchIn(h)) return fromRootPath(h, lan = false, ctx)
        return null
    }

    /** LAN, Tailscale (CGNAT 100.64/10, *.ts.net), loopback and mDNS hosts. */
    fun isPrivateHost(host: String): Boolean {
        val h = host.lowercase()
        if (h == "localhost" || h == "[::1]" || h.endsWith(".local") || h.endsWith(".ts.net")) return true
        val m = IPV4.find(h) ?: return false
        val a = m.groupValues[1].toInt()
        val b = m.groupValues[2].toInt()
        return a == 10 || a == 127 || (a == 192 && b == 168) || (a == 172 && b in 16..31) || (a == 100 && b in 64..127)
    }

    /** LNK-5: inline code that is exactly an internal path or URL. */
    fun linkableCode(code: String, ctx: Context = Context()): Target? {
        val c = code.trim()
        if (c.isEmpty() || UNSAFE_CODE.containsMatchIn(c.replaceFirst("?file=", ""))) return null
        return resolve(c, ctx)
    }

    /** A bare path found in plain text ([start] inclusive, [end] exclusive). */
    data class BarePath(val start: Int, val end: Int, val path: String)

    private val BARE_PATH = Regex(
        "(^|[\\s(\\[\"'`,;])((?:~/|/)?(?:[\\w.@+-]+/)*context/(?:public|memory)/[\\w.@+%/-]+?\\.(?:html?|md))" +
            "(?=$|[\\s)\\]\"'`,;:!?]|\\.(?:\\s|$))",
    )

    /** LNK-5: bare `context/public/….html` / `context/memory/….md` paths in plain text. */
    fun findBarePaths(text: String): List<BarePath> {
        if (!text.contains("context/")) return emptyList()
        return BARE_PATH.findAll(text).mapNotNull { m ->
            val path = m.groupValues[2]
            val start = m.range.first + m.groupValues[1].length
            if (fromFilesystem(path) != null) BarePath(start, start + path.length, path) else null
        }.toList()
    }

    /** The app URL of a target, root-relative and segment-encoded. */
    fun url(t: Target): String = when (t) {
        is Target.Memory -> "/memory/" + encodePath(t.path) + (t.fragment?.let { "#$it" } ?: "")
        is Target.Visual -> "/" + encodePath(t.path)
    }

    private fun encodePath(p: String) =
        p.split('/').joinToString("/") { java.net.URLEncoder.encode(it, "UTF-8").replace("+", "%20").replace("%7E", "~") }

    // ───────────────────────── internals ─────────────────────────

    private fun decode(s: String): String = try {
        java.net.URLDecoder.decode(s.replace("+", "%2B"), "UTF-8")
    } catch (_: IllegalArgumentException) {
        s
    }

    /** `null` when it climbs above the root or a segment decodes to a separator (`..%2F..`). */
    private fun normalize(path: String): String? {
        val out = ArrayList<String>()
        for (raw in path.split('/')) {
            val seg = decode(raw)
            if ('/' in seg || '\\' in seg) return null
            when (seg) {
                "", "." -> Unit
                ".." -> if (out.isEmpty()) return null else out.removeAt(out.size - 1)
                else -> out += seg
            }
        }
        return out.takeIf { it.isNotEmpty() }?.joinToString("/")
    }

    private fun splitHash(s: String): Pair<String, String?> {
        val i = s.indexOf('#')
        return if (i < 0) s to null else s.substring(0, i) to s.substring(i + 1).ifEmpty { null }
    }

    private fun memory(path: String?, fragment: String?): Target? =
        if (path != null && MD.containsMatchIn(path)) Target.Memory(path, fragment) else null

    private fun fromFilesystem(raw: String): Target? {
        val (pathPart, fragment) = splitHash(LINE_SUFFIX.replace(raw, ""))
        CONTEXT_ROOT.find(pathPart)?.let { m ->
            val rest = normalize(m.groupValues[2]) ?: return null
            return if (m.groupValues[1] == "memory") memory(rest, fragment)
            else if (HTML.containsMatchIn(rest)) Target.Visual(rest) else null
        }
        DOCS_ROOT.find(pathPart)?.let { m ->
            val rest = normalize(m.groupValues[1].ifEmpty { m.groupValues[2] })
            return memory(rest?.let { "archie/$it" }, fragment)
        }
        return null
    }

    private fun fromRootPath(pathAndMore: String, lan: Boolean, ctx: Context): Target? {
        val (beforeHash, fragment) = splitHash(pathAndMore)
        val q = beforeHash.indexOf('?')
        val pathname = if (q < 0) beforeHash else beforeHash.substring(0, q)
        val query = if (q < 0) "" else beforeHash.substring(q + 1)
        if (!pathname.startsWith("/")) return null
        val first = decode(pathname.split('/').getOrElse(1) { "" })

        if (first == "memory") {
            if (query.isNotEmpty()) return null
            if (pathname == "/memory" || pathname == "/memory/") return Target.Memory("MEMORY.md", fragment)
            return memory(normalize(pathname.removePrefix("/memory/")), fragment)
        }
        if (pathname.equals("/markdown_reader.html", ignoreCase = true)) {
            val file = Regex("(?:^|&)file=([^&]*)").find(query)?.groupValues?.get(1)
            val value = file?.let { decode(it.replace("+", " ")) } ?: ""
            if (!value.startsWith("memory/")) return null
            return memory(normalize(value.removePrefix("memory/")), fragment)
        }
        if (query.isNotEmpty() || first in RESERVED) return null
        var path = normalize(pathname)
        if (path != null && pathname.endsWith("/")) path = "$path/index.html"
        if (path == null || !HTML.containsMatchIn(path)) return null
        if (lan && !path.startsWith("visualizations/") && !ctx.isVisual(path)) return null
        return Target.Visual(path)
    }
}

/**
 * LNK-5: inline code that is exactly an internal path and bare `context/…` paths in text become
 * [MdInline.Link]s. The href is the target's canonical URL ([InternalLinks.url]: `/memory/archie/x.md`,
 * `/x/index.html`), never the printed path, which a memory document would resolve relative to
 * itself (MEM-2). Never inside an existing link. Returns [inlines] itself when nothing changed.
 */
fun autoLinkPaths(inlines: List<MdInline>, ctx: InternalLinks.Context = InternalLinks.Context()): List<MdInline> {
    var out: ArrayList<MdInline>? = null
    for ((i, n) in inlines.withIndex()) {
        val replaced: List<MdInline>? = when (n) {
            is MdInline.Code ->
                InternalLinks.linkableCode(n.code, ctx)?.let { listOf(MdInline.Link(InternalLinks.url(it), null, listOf(n))) }
            is MdInline.Text -> {
                val found = InternalLinks.findBarePaths(n.text)
                if (found.isEmpty()) null
                else buildList {
                    var at = 0
                    for (f in found) {
                        if (f.start > at) add(MdInline.Text(n.text.substring(at, f.start)))
                        val t = InternalLinks.resolve(f.path, ctx)
                        add(if (t != null) MdInline.Link(InternalLinks.url(t), null, listOf(MdInline.Text(f.path))) else MdInline.Text(f.path))
                        at = f.end
                    }
                    if (at < n.text.length) add(MdInline.Text(n.text.substring(at)))
                }
            }
            is MdInline.Emphasis -> autoLinkPaths(n.children, ctx).takeIf { it !== n.children }?.let { listOf(MdInline.Emphasis(it)) }
            is MdInline.Strong -> autoLinkPaths(n.children, ctx).takeIf { it !== n.children }?.let { listOf(MdInline.Strong(it)) }
            is MdInline.Strike -> autoLinkPaths(n.children, ctx).takeIf { it !== n.children }?.let { listOf(MdInline.Strike(it)) }
            else -> null
        }
        if (replaced != null && out == null) out = ArrayList<MdInline>(inlines.size + 4).apply { addAll(inlines.subList(0, i)) }
        if (out != null) { if (replaced != null) out.addAll(replaced) else out.add(n) }
    }
    return out ?: inlines
}
