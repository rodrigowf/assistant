package com.assistant.core.markdown.ui

import androidx.compose.runtime.Composable
import androidx.compose.runtime.Immutable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.assistant.core.design.theme.ArchieTheme
import com.assistant.core.markdown.CodeTokenKind
import com.assistant.core.markdown.MdNode

/** Syntax colors, all theme roles (web parity: frontend `CodeBlock.module.css`). */
@Immutable
data class SyntaxColors(
    val keyword: Color,
    val string: Color,
    val literal: Color,
    val comment: Color,
    val annotation: Color,
) {
    fun of(kind: CodeTokenKind): Color = when (kind) {
        CodeTokenKind.Keyword -> keyword
        CodeTokenKind.String -> string
        CodeTokenKind.Literal -> literal
        CodeTokenKind.Comment -> comment
        CodeTokenKind.Annotation -> annotation
    }
}

/**
 * Every visual decision of the markdown renderer. [MarkdownStyle.fromTheme] derives it from
 * ArchieTheme (B-01); hosts may override fields (tool cards use a smaller body, for instance).
 */
@Immutable
data class MarkdownStyle(
    val body: TextStyle,
    val headings: List<TextStyle>,
    val textColor: Color,
    val mutedColor: Color,
    val linkColor: Color,
    val inlineCodeBackground: Color,
    val inlineCodeScale: Float,
    val code: TextStyle,
    val codeColor: Color,
    val codeSurface: Color,
    val codeHeaderSurface: Color,
    val codeHeaderText: TextStyle,
    val actionColor: Color,
    val syntax: SyntaxColors,
    val ruleColor: Color,
    val quoteBar: Color,
    val tableBorder: Color,
    val tableHeaderSurface: Color,
    val tableText: TextStyle,
    val checkboxOn: Color,
    val checkboxOnContent: Color,
    val checkboxOff: Color,
    val blockSpacing: Dp = 12.dp,
    val headingSpacing: Dp = 20.dp,
    val afterHeadingSpacing: Dp = 8.dp,
    val itemSpacing: Dp = 4.dp,
    val listIndent: Dp = 24.dp,
    val codeCorner: Dp = 12.dp,
    /** Code blocks taller than this collapse to "Show all (N lines)" (spec 14 §3.3). */
    val codeMaxHeight: Dp = 480.dp,
    val tableMaxColumnWidth: Dp = 280.dp,
    /**
     * LNK-5 (spec 12 §9.4): printed visualization / memory paths become links. Only for hosts whose
     * link handler resolves them with [com.assistant.core.markdown.InternalLinks] (chat, memory).
     */
    val autoLinkPaths: Boolean = false,
    /** The backend origin for [autoLinkPaths] (an `https://<server>/…` URL in backticks is internal, LNK-3). */
    val linkOrigin: String? = null,
) {
    fun heading(level: Int): TextStyle = headings[(level - 1).coerceIn(0, headings.size - 1)]

    /** Gap above node [node] when it follows [previous] in the same document. */
    fun spacingBefore(previous: MdNode?, node: MdNode): Dp = when {
        previous == null -> 0.dp
        node is MdNode.ListBlock && node.continuation -> itemSpacing
        node is MdNode.CodeTail && node.chunk > 0 -> 0.dp
        node is MdNode.Heading -> headingSpacing
        previous is MdNode.Heading -> afterHeadingSpacing
        else -> blockSpacing
    }

    companion object {
        @Composable
        fun fromTheme(): MarkdownStyle {
            val c = ArchieTheme.colors
            val x = ArchieTheme.extended
            val t = ArchieTheme.typography
            // Mockup `.a`: 16/24, tracking 0.2.
            val body = t.bodyLarge.copy(letterSpacing = 0.2.sp, color = Color.Unspecified)
            val w500 = FontWeight.W500
            return MarkdownStyle(
                body = body,
                headings = listOf(
                    t.headlineSmall.copy(fontWeight = w500),
                    t.titleLarge.copy(fontWeight = w500),
                    t.titleMedium.copy(fontWeight = w500),
                    body.copy(fontWeight = FontWeight.W600),
                    t.titleSmall,
                    t.titleSmall,
                ),
                textColor = c.onSurface,
                mutedColor = c.onSurfaceVariant,
                linkColor = c.primary,
                inlineCodeBackground = c.surfaceContainerHigh,
                inlineCodeScale = 0.875f,
                code = ArchieTheme.text.code,
                codeColor = c.onSurface,
                codeSurface = c.surfaceContainer,
                codeHeaderSurface = c.surfaceContainerHigh,
                codeHeaderText = t.labelMedium.copy(fontFamily = ArchieTheme.text.code.fontFamily),
                actionColor = c.primary,
                syntax = SyntaxColors(
                    keyword = c.primary,
                    string = x.success.color,
                    literal = c.tertiary,
                    comment = c.onSurfaceVariant,
                    annotation = x.info.color,
                ),
                ruleColor = c.outlineVariant,
                quoteBar = c.outlineVariant,
                tableBorder = c.outlineVariant,
                tableHeaderSurface = c.surfaceContainerHigh,
                tableText = t.bodyMedium,
                checkboxOn = c.primary,
                checkboxOnContent = c.onPrimary,
                checkboxOff = c.onSurfaceVariant,
            )
        }
    }
}
