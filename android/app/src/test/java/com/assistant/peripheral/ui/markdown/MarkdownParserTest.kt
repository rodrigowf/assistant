package com.assistant.peripheral.ui.markdown

import com.assistant.peripheral.ui.components.markdown.MdBlock
import com.assistant.peripheral.ui.components.markdown.MdInline
import com.assistant.peripheral.ui.components.markdown.parseBlocks
import com.assistant.peripheral.ui.components.markdown.parseInline
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pins the pure markdown parser after the 2026-07-21 RenderThread-crash fix.
 *
 * Context: on the A300M (Android 5.0, old GPU) an assistant message crashed the
 * app with a native SIGSEGV — a stack overflow inside libhwui's display-list
 * recursion — because the renderer re-parsed + re-laid-out the whole streaming
 * message every delta and could recurse unboundedly on malformed/streaming
 * inline markdown. Two invariants matter for correctness of the fixes:
 *
 *  1. `parseInline` recursion is DEPTH-BOUNDED — pathological input can't blow
 *     the parse/render stack.
 *  2. The incremental-parse optimization in `rememberMarkdownBlocks` (parse the
 *     stable prefix up to the last blank line once, only re-parse the tail) is
 *     only valid if `parseBlocks(prefix) + parseBlocks(tail)` equals
 *     `parseBlocks(prefix + tail)` when the split is on a `\n\n` boundary. This
 *     pins that equivalence.
 */
class MarkdownParserTest {

    // ── Depth guard ──────────────────────────────────────────────────────────

    @Test
    fun `deeply nested emphasis does not overflow the stack`() {
        // 200 levels of nested bold would recurse 200 deep without the cap.
        // With MAX_INLINE_DEPTH the parse returns normally (no StackOverflow).
        val deep = "*".repeat(200) + "x" + "*".repeat(200)
        val result = parseInline(deep) // must not throw
        assertTrue("parse returns something", result.isNotEmpty())
    }

    @Test
    fun `malformed streaming markdown parses without throwing`() {
        // Mid-stream fragments the renderer sees token-by-token: unbalanced
        // markers, stray em-dashes, an open link. None may crash the parser.
        listOf(
            "All the lamps in your office—about 70",
            "**bold that never clos",
            "here is `code and _italic and [link](",
            "***",
            "____",
            "text — more — text",
        ).forEach { frag ->
            parseInline(frag) // must not throw
            parseBlocks(frag)
        }
    }

    @Test
    fun `simple bold and italic still parse correctly`() {
        val spans = parseInline("a **b** c")
        // text "a ", Bold(["b"]), text " c"
        assertEquals(3, spans.size)
        assertTrue(spans[1] is MdInline.Bold)
    }

    // ── Incremental-parse equivalence (the rememberMarkdownBlocks invariant) ──

    @Test
    fun `parsing prefix and tail separately equals parsing the whole on a blank-line split`() {
        val whole = "First paragraph.\n\nSecond paragraph.\n\nThird streaming ta"
        val splitAt = whole.lastIndexOf("\n\n") + 2
        val prefix = whole.substring(0, splitAt)
        val tail = whole.substring(splitAt)

        val combined = parseBlocks(prefix) + parseBlocks(tail)
        val direct = parseBlocks(whole)

        assertEquals("same block count", direct.size, combined.size)
        // Compare rendered paragraph text so we don't depend on data-class
        // equality of nested inline nodes.
        assertEquals(paragraphTexts(direct), paragraphTexts(combined))
    }

    @Test
    fun `incremental split equals whole across many streaming lengths`() {
        // Simulate streaming: for each growing prefix of the message, the
        // renderer would split at the last blank line. The union must always
        // match a full parse. (Only holds when the split is on \n\n, which is
        // exactly what rememberMarkdownBlocks does.)
        val full = "Intro line.\n\n- item one\n- item two\n\nClosing remark here."
        for (n in 1..full.length) {
            val text = full.substring(0, n)
            val idx = text.lastIndexOf("\n\n")
            val splitAt = if (idx < 0) 0 else idx + 2
            val combined = parseBlocks(text.substring(0, splitAt)) + parseBlocks(text.substring(splitAt))
            val direct = parseBlocks(text)
            assertEquals("mismatch at len=$n", paragraphTexts(direct), paragraphTexts(combined))
        }
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private fun paragraphTexts(blocks: List<MdBlock>): List<String> =
        blocks.map { b ->
            when (b) {
                is MdBlock.Paragraph -> "P:" + flatten(b.spans)
                is MdBlock.Heading -> "H${b.level}:" + flatten(b.spans)
                is MdBlock.CodeBlock -> "CODE:" + b.code
                is MdBlock.UnorderedList -> "UL:" + b.items.joinToString("|") { flatten(it) }
                is MdBlock.OrderedList -> "OL:" + b.items.joinToString("|") { flatten(it) }
                is MdBlock.Blockquote -> "Q:" + flatten(b.spans)
                is MdBlock.Table -> "T"
                is MdBlock.HorizontalRule -> "HR"
            }
        }

    private fun flatten(spans: List<MdInline>): String =
        spans.joinToString("") { s ->
            when (s) {
                is MdInline.Text -> s.text
                is MdInline.Bold -> flatten(s.children)
                is MdInline.Italic -> flatten(s.children)
                is MdInline.BoldItalic -> flatten(s.children)
                is MdInline.Code -> s.text
                is MdInline.Link -> flatten(s.children)
            }
        }
}
