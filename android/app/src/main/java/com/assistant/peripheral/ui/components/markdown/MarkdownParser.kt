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
    data class Bold(val text: String) : MdInline()
    data class Italic(val text: String) : MdInline()
    data class BoldItalic(val text: String) : MdInline()
    data class Code(val text: String) : MdInline()
    data class Link(val text: String, val url: String) : MdInline()
}

// ── Block parser ─────────────────────────────────────────────────────────────

private val headingRegex = Regex("^(#{1,3})\\s+(.+)")
private val ulRegex = Regex("^\\s*[-*+]\\s+(.*)")
private val olRegex = Regex("^\\s*\\d+\\.\\s+(.*)")
private val hrRegex = Regex("^\\s*([-*_])\\s*\\1\\s*\\1[\\s\\1]*$")
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

fun parseInline(text: String): List<MdInline> {
    if (text.isEmpty()) return listOf(MdInline.Text(""))

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

        // Determine which group matched
        when {
            match.groups[1] != null -> result.add(MdInline.BoldItalic(match.groups[1]!!.value))
            match.groups[2] != null -> result.add(MdInline.Bold(match.groups[2]!!.value))
            match.groups[3] != null -> result.add(MdInline.Bold(match.groups[3]!!.value))
            match.groups[4] != null -> result.add(MdInline.Italic(match.groups[4]!!.value))
            match.groups[5] != null -> result.add(MdInline.Italic(match.groups[5]!!.value))
            match.groups[6] != null -> result.add(MdInline.Code(match.groups[6]!!.value))
            match.groups[7] != null -> result.add(
                MdInline.Link(match.groups[7]!!.value, match.groups[8]!!.value)
            )
        }

        remaining = remaining.substring(match.range.last + 1)
    }

    return result
}
