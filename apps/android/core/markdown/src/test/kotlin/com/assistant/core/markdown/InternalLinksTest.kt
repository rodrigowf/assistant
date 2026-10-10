package com.assistant.core.markdown

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.contentOrNull
import org.junit.Assert.assertEquals
import org.junit.Test
import java.io.File

/**
 * The shared internal-link corpus (spec 12 §9.4; `apps/protocol-fixtures/links/internal-links.json`,
 * also run by the web's `internalLinks.test.ts`), plus the inline auto-linking (LNK-5).
 */
class InternalLinksTest {
    private val corpus: JsonObject = Json.parseToJsonElement(
        File(System.getProperty("archie.internalLinks") ?: error("archie.internalLinks not set")).readText(),
    ).jsonObject
    private val origin = corpus["origin"]!!.jsonPrimitive.content

    private fun expected(e: kotlinx.serialization.json.JsonElement?): InternalLinks.Target? {
        if (e == null || e is JsonNull) return null
        val o = e.jsonObject
        val path = o["path"]!!.jsonPrimitive.content
        return when (o["kind"]!!.jsonPrimitive.content) {
            "visual" -> InternalLinks.Target.Visual(path)
            else -> InternalLinks.Target.Memory(path, o["fragment"]?.jsonPrimitive?.contentOrNull)
        }
    }

    @Test
    fun resolveMatchesTheSharedCorpus() {
        val failures = corpus["cases"]!!.jsonArray.mapNotNull { el ->
            val c = el.jsonObject
            val href = c["href"]!!.jsonPrimitive.content
            val visuals = c["context"]?.jsonObject?.get("visuals")?.jsonArray?.map { it.jsonPrimitive.content }.orEmpty()
            val got = InternalLinks.resolve(href, InternalLinks.Context(origin) { it in visuals })
            val want = expected(c["expect"])
            if (got != want) "${c["note"]}: $href → $got, expected $want" else null
        }
        assertEquals(emptyList<String>(), failures)
    }

    @Test
    fun inlineCodeMatchesTheSharedCorpus() {
        for (el in corpus["inline_code"]!!.jsonArray) {
            val c = el.jsonObject
            val code = c["code"]!!.jsonPrimitive.content
            assertEquals(code, c["linked"]!!.jsonPrimitive.boolean, InternalLinks.linkableCode(code, InternalLinks.Context(origin)) != null)
        }
    }

    @Test
    fun barePathsMatchTheSharedCorpus() {
        for (el in corpus["bare_text"]!!.jsonArray) {
            val c = el.jsonObject
            val text = c["text"]!!.jsonPrimitive.content
            val found = InternalLinks.findBarePaths(text)
            assertEquals(text, c["paths"]!!.jsonArray.map { it.jsonPrimitive.content }, found.map { it.path })
            for (f in found) assertEquals(f.path, text.substring(f.start, f.end))
        }
    }

    @Test
    fun autoLinkingWrapsCodeAndBarePathsOnly() {
        val inlines = listOf(
            MdInline.Text("Saved to context/public/a/index.html. See "),
            MdInline.Code("docs/specs/12-client-protocol.md:40"),
            MdInline.Text(" and "),
            MdInline.Code("backend/api/app.py"),
            MdInline.Link("/memory/x.md", null, listOf(MdInline.Code("context/memory/y.md"))),
        )
        val out = autoLinkPaths(inlines)
        assertEquals(
            listOf(
                MdInline.Text("Saved to "),
                // The canonical URL, not the printed path (a memory document resolves relative hrefs itself).
                MdInline.Link("/a/index.html", null, listOf(MdInline.Text("context/public/a/index.html"))),
                MdInline.Text(". See "),
                MdInline.Link("/memory/archie/specs/12-client-protocol.md", null, listOf(MdInline.Code("docs/specs/12-client-protocol.md:40"))),
                MdInline.Text(" and "),
                MdInline.Code("backend/api/app.py"),
                MdInline.Link("/memory/x.md", null, listOf(MdInline.Code("context/memory/y.md"))),
            ),
            out,
        )
        // Nothing to link: the same list instance (no allocation per render).
        val plain = listOf(MdInline.Text("hello"))
        assertEquals(true, autoLinkPaths(plain) === plain)
    }
}
