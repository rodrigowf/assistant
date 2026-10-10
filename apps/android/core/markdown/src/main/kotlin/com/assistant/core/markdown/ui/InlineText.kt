package com.assistant.core.markdown.ui

import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.LinkAnnotation
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.TextLayoutResult
import androidx.compose.ui.text.TextLinkStyles
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.text.withLink
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.em
import com.assistant.core.markdown.MdInline
import com.assistant.core.markdown.InternalLinks
import com.assistant.core.markdown.autoLinkPaths

internal const val INLINE_CODE_TAG = "md-icode"

/** Narrow no-break space: pads inline code inside its rounded background without allowing a break. */
private const val PAD = " "

/**
 * Inline nodes → [AnnotatedString]. Recursion is bounded by the model's depth cap
 * ([com.assistant.core.markdown.MAX_INLINE_DEPTH]). Links are [LinkAnnotation.Clickable] whose tag
 * is the raw href; the host classifies it ([com.assistant.core.markdown.LinkTarget.classify]).
 */
internal fun buildInline(inlines: List<MdInline>, style: MarkdownStyle, onLink: (String) -> Unit): AnnotatedString =
    buildAnnotatedString {
        val linkStyles = TextLinkStyles(SpanStyle(color = style.linkColor, textDecoration = TextDecoration.Underline))
        val codeSpan = SpanStyle(fontFamily = style.code.fontFamily, fontSize = style.inlineCodeScale.em)

        fun emit(list: List<MdInline>) {
            for (n in list) {
                when (n) {
                    is MdInline.Text -> append(n.text)
                    is MdInline.Code -> {
                        val start = length
                        withStyle(codeSpan) { append(PAD + n.code + PAD) }
                        addStringAnnotation(INLINE_CODE_TAG, "", start, length)
                    }
                    is MdInline.Emphasis -> withStyle(SpanStyle(fontStyle = FontStyle.Italic)) { emit(n.children) }
                    is MdInline.Strong -> withStyle(SpanStyle(fontWeight = FontWeight.W600)) { emit(n.children) }
                    is MdInline.Strike -> withStyle(
                        SpanStyle(textDecoration = TextDecoration.LineThrough, color = style.mutedColor),
                    ) { emit(n.children) }
                    is MdInline.Link -> withLink(LinkAnnotation.Clickable(n.href, linkStyles) { onLink(n.href) }) {
                        emit(n.children)
                    }
                    is MdInline.Image -> withLink(LinkAnnotation.Clickable(n.src, linkStyles) { onLink(n.src) }) {
                        append(n.alt.ifEmpty { "image" })
                    }
                    MdInline.SoftBreak -> append(' ')
                    MdInline.HardBreak -> append('\n')
                }
            }
        }
        emit(if (style.autoLinkPaths) autoLinkPaths(inlines, InternalLinks.Context(style.linkOrigin)) else inlines)
    }

/** Plain text of inline nodes (semantics, tests, copy). */
fun plainText(inlines: List<MdInline>): String = buildString {
    fun emit(list: List<MdInline>) {
        for (n in list) {
            when (n) {
                is MdInline.Text -> append(n.text)
                is MdInline.Code -> append(n.code)
                is MdInline.Emphasis -> emit(n.children)
                is MdInline.Strong -> emit(n.children)
                is MdInline.Strike -> emit(n.children)
                is MdInline.Link -> emit(n.children)
                is MdInline.Image -> append(n.alt)
                MdInline.SoftBreak -> append(' ')
                MdInline.HardBreak -> append('\n')
            }
        }
    }
    emit(inlines)
}

/**
 * One run of inline markdown. Inline code gets a rounded surface-container-high background drawn
 * behind its glyph boxes (mockup `.icode`), which a [SpanStyle.background] cannot round.
 */
@Composable
internal fun InlineText(
    inlines: List<MdInline>,
    textStyle: TextStyle,
    color: Color,
    style: MarkdownStyle,
    onLink: (String) -> Unit,
    modifier: Modifier = Modifier,
    textAlign: TextAlign? = null,
) {
    val currentOnLink by rememberUpdatedState(onLink)
    val text = remember(inlines, style) { buildInline(inlines, style) { currentOnLink(it) } }
    val codeRanges = remember(text) {
        text.getStringAnnotations(INLINE_CODE_TAG, 0, text.length).map { it.start until it.end }
    }
    var layout by remember { mutableStateOf<TextLayoutResult?>(null) }
    val bg = style.inlineCodeBackground
    val drawMod = if (codeRanges.isEmpty()) {
        Modifier
    } else {
        Modifier.drawBehind { layout?.let { drawCodeBackgrounds(it, codeRanges, bg) } }
    }
    Text(
        text = text,
        modifier = modifier.then(drawMod),
        color = color,
        style = if (textAlign != null) textStyle.copy(textAlign = textAlign) else textStyle,
        onTextLayout = { layout = it },
    )
}

private fun DrawScope.drawCodeBackgrounds(layout: TextLayoutResult, ranges: List<IntRange>, color: Color) {
    val radius = CornerRadius(5.dp.toPx())
    val inset = 2.dp.toPx()
    val textLength = layout.layoutInput.text.length
    for (r in ranges) {
        val start = r.first.coerceIn(0, textLength)
        val end = (r.last + 1).coerceIn(start, textLength)
        if (end <= start) continue
        val firstLine = layout.getLineForOffset(start)
        val lastLine = layout.getLineForOffset(end - 1)
        for (line in firstLine..lastLine) {
            val left = if (line == firstLine) layout.getHorizontalPosition(start, true) else layout.getLineLeft(line)
            val right = if (line == lastLine) layout.getHorizontalPosition(end, true) else layout.getLineRight(line)
            val top = layout.getLineTop(line) + inset
            val bottom = layout.getLineBottom(line) - inset
            if (right > left && bottom > top) {
                drawRoundRect(color, Offset(left, top), Size(right - left, bottom - top), radius)
            }
        }
    }
}
