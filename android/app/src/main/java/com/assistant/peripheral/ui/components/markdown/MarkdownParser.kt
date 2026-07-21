package com.assistant.peripheral.ui.components.markdown

/**
 * Lightweight markdown parser for Jetpack Compose.
 * Two-phase: block-level (line-by-line) then inline-level (regex).
 * No external dependencies.
 */

// ── Block-level types ────────────────────────────────────────────────────────

sealed class MdBlock {
    data class Paragraph(val spans: List<MdInline>) : MdBlock()
    data class Heading(val level: Int, val spans: List<MdInline>) : MdBlock()
    data class CodeBlock(val language: String, val code: String) : MdBlock()
    data class UnorderedList(val items: List<List<MdInline>>) : MdBlock()
    data class OrderedList(val items: List<List<MdInline>>) : MdBlock()
    data class Blockquote(val spans: List<MdInline>) : MdBlock()
    data class Table(val headers: List<String>, val rows: List<List<String>>) : MdBlock()
    object HorizontalRule : MdBlock()
}

// ── Inline-level types ───────────────────────────────────────────────────────

sealed class MdInline {
    data class Text(val text: String) : MdInline()
    // Emphasis variants hold nested inline children so a link (or code, or
    // nested emphasis) inside bold/italic stays a first-class node — see the
    // 2026-06-18 fix for "links inside **bold** not clickable on Android".
    data class Bold(val children: List<MdInline>) : MdInline()
    data class Italic(val children: List<MdInline>) : MdInline()
    data class BoldItalic(val children: List<MdInline>) : MdInline()
    data class Code(val text: String) : MdInline()
    data class Link(val children: List<MdInline>, val url: String) : MdInline()
}

// ── Block parser ─────────────────────────────────────────────────────────────

private val headingRegex = Regex("^(#{1,3})\\s+(.+)")
private val ulRegex = Regex("^\\s*[-*+]\\s+(.*)")
private val olRegex = Regex("^\\s*\\d+\\.\\s+(.*)")
// Horizontal rule: 3+ of the same marker (-, *, _) with optional spaces
// between/after, e.g. "---", "* * *", "___  ". The old pattern used `[\s\1]`
// (a backreference INSIDE a character class), which is a no-op on Android's
// regex engine but a hard PatternSyntaxException on the standard JVM — so it
// crashed unit tests at class-init. This form matches marker + (spaces + same
// marker){2,} + trailing spaces, using the backreference outside a class.
private val hrRegex = Regex("^\\s*([-*_])(?:\\s*\\1){2,}\\s*$")
private val tableSepRegex = Regex("^[\\s|:-]+$")

fun parseBlocks(input: String): List<MdBlock> {
    if (input.isBlank()) return emptyList()

    val lines = input.split("\n")
    val blocks = mutableListOf<MdBlock>()
    var i = 0

    while (i < lines.size) {
        val line = lines[i]
        val trimmed = line.trimStart()

        // ── Fenced code block ────────────────────────────────────────────
        if (trimmed.startsWith("```") || trimmed.startsWith("~~~")) {
            val fence = trimmed.substring(0, 3)
            val language = trimmed.removePrefix(fence).trim()
            val codeLines = mutableListOf<String>()
            i++
            while (i < lines.size) {
                if (lines[i].trimStart().startsWith(fence) &&
                    lines[i].trimStart().removePrefix(fence).isBlank()) {
                    i++ // skip closing fence
                    break
                }
                codeLines.add(lines[i])
                i++
            }
            blocks.add(MdBlock.CodeBlock(language, codeLines.joinToString("\n")))
            continue
        }

        // ── Horizontal rule ──────────────────────────────────────────────
        if (hrRegex.matches(line)) {
            blocks.add(MdBlock.HorizontalRule)
            i++
            continue
        }

        // ── Heading ──────────────────────────────────────────────────────
        val headingMatch = headingRegex.find(trimmed)
        if (headingMatch != null) {
            val level = headingMatch.groupValues[1].length
            val content = headingMatch.groupValues[2]
            blocks.add(MdBlock.Heading(level, parseInline(content)))
            i++
            continue
        }

        // ── Table ────────────────────────────────────────────────────────
        if (line.contains("|") && i + 1 < lines.size && tableSepRegex.matches(lines[i + 1])) {
            val headers = line.split("|").map { it.trim() }.filter { it.isNotEmpty() }
            i += 2 // skip header + separator
            val rows = mutableListOf<List<String>>()
            while (i < lines.size && lines[i].contains("|")) {
                val row = lines[i].split("|").map { it.trim() }.filter { it.isNotEmpty() }
                if (row.isNotEmpty()) rows.add(row)
                i++
            }
            blocks.add(MdBlock.Table(headers, rows))
            continue
        }

        // ── Unordered list ───────────────────────────────────────────────
        val ulMatch = ulRegex.find(trimmed)
        if (ulMatch != null) {
            val items = mutableListOf<List<MdInline>>()
            while (i < lines.size) {
                val m = ulRegex.find(lines[i].trimStart()) ?: break
                items.add(parseInline(m.groupValues[1]))
                i++
            }
            blocks.add(MdBlock.UnorderedList(items))
            continue
        }

        // ── Ordered list ─────────────────────────────────────────────────
        val olMatch = olRegex.find(trimmed)
        if (olMatch != null) {
            val items = mutableListOf<List<MdInline>>()
            while (i < lines.size) {
                val m = olRegex.find(lines[i].trimStart()) ?: break
                items.add(parseInline(m.groupValues[1]))
                i++
            }
            blocks.add(MdBlock.OrderedList(items))
            continue
        }

        // ── Blockquote ───────────────────────────────────────────────────
        if (trimmed.startsWith(">")) {
            val quoteLines = mutableListOf<String>()
            while (i < lines.size && lines[i].trimStart().startsWith(">")) {
                quoteLines.add(lines[i].trimStart().removePrefix(">").removePrefix(" "))
                i++
            }
            blocks.add(MdBlock.Blockquote(parseInline(quoteLines.joinToString(" "))))
            continue
        }

        // ── Blank line ───────────────────────────────────────────────────
        if (line.isBlank()) {
            i++
            continue
        }

        // ── Paragraph (accumulate consecutive non-blank lines) ───────────
        val paraLines = mutableListOf<String>()
        while (i < lines.size && lines[i].isNotBlank()) {
            val pl = lines[i].trimStart()
            // Stop if next line starts a different block type
            if (pl.startsWith("```") || pl.startsWith("~~~") ||
                headingRegex.matches(pl) ||
                ulRegex.matches(pl) ||
                olRegex.matches(pl) ||
                pl.startsWith(">") ||
                hrRegex.matches(lines[i]) ||
                (pl.contains("|") && i + 1 < lines.size && tableSepRegex.matches(lines[i + 1]))
            ) break
            paraLines.add(lines[i])
            i++
        }
        if (paraLines.isNotEmpty()) {
            blocks.add(MdBlock.Paragraph(parseInline(paraLines.joinToString("\n"))))
        }
    }

    return blocks
}

// ── Inline parser ────────────────────────────────────────────────────────────

private val inlinePattern = Regex(
    "\\*\\*\\*(.+?)\\*\\*\\*" +           // group 1: ***bold italic***
    "|\\*\\*(.+?)\\*\\*" +                // group 2: **bold**
    "|__(.+?)__" +                         // group 3: __bold__
    "|(?<!\\w)\\*(.+?)\\*(?!\\w)" +       // group 4: *italic* (not mid-word)
    "|(?<!\\w)_(.+?)_(?!\\w)" +           // group 5: _italic_ (not mid-word)
    "|`([^`]+)`" +                         // group 6: `inline code`
    "|\\[([^\\]]+)]\\(([^)]+)\\)"         // group 7+8: [text](url)
)

// Cap on inline emphasis/link nesting. Well-formed markdown never nests inline
// styles more than a couple deep; malformed or mid-stream markdown (stray
// `*`/`_`, em-dashes, an unclosed `**`) can otherwise make the lazy regexes
// re-segment and recurse arbitrarily deep, which both burns CPU per delta and
// deepens the AnnotatedString span tree libhwui has to traverse. Past the cap
// we stop recursing and emit the inner content as literal text.
private const val MAX_INLINE_DEPTH = 8

fun parseInline(text: String): List<MdInline> = parseInline(text, 0)

private fun parseInline(text: String, depth: Int): List<MdInline> {
    if (text.isEmpty()) return listOf(MdInline.Text(""))
    // Depth guard: stop recursing into emphasis/link children and treat the
    // remaining text literally. Bounds recursion regardless of input.
    if (depth >= MAX_INLINE_DEPTH) return listOf(MdInline.Text(text))

    val result = mutableListOf<MdInline>()
    var remaining = text

    while (remaining.isNotEmpty()) {
        val match = inlinePattern.find(remaining)
        if (match == null) {
            result.add(MdInline.Text(remaining))
            break
        }

        // Text before the match
        if (match.range.first > 0) {
            result.add(MdInline.Text(remaining.substring(0, match.range.first)))
        }

        // Determine which group matched. For emphasis types we recursively
        // parse the inner content so `**[label](url)**` produces
        // Bold(children=[Link(...)]) instead of Bold(text="[label](url)").
        val d = depth + 1
        when {
            match.groups[1] != null -> result.add(MdInline.BoldItalic(parseInline(match.groups[1]!!.value, d)))
            match.groups[2] != null -> result.add(MdInline.Bold(parseInline(match.groups[2]!!.value, d)))
            match.groups[3] != null -> result.add(MdInline.Bold(parseInline(match.groups[3]!!.value, d)))
            match.groups[4] != null -> result.add(MdInline.Italic(parseInline(match.groups[4]!!.value, d)))
            match.groups[5] != null -> result.add(MdInline.Italic(parseInline(match.groups[5]!!.value, d)))
            match.groups[6] != null -> result.add(MdInline.Code(match.groups[6]!!.value))
            match.groups[7] != null -> result.add(
                MdInline.Link(parseInline(match.groups[7]!!.value, d), match.groups[8]!!.value)
            )
        }

        remaining = remaining.substring(match.range.last + 1)
    }

    return result
}
