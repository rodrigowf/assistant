package com.assistant.peripheral.ui.components.markdown

import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.ClickableText
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.*
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay

/**
 * Renders markdown text as Compose UI.
 * Parses the text into blocks and renders each with the appropriate composable.
 */
@Composable
fun MarkdownText(
    text: String,
    modifier: Modifier = Modifier
) {
    val blocks = remember(text) { parseBlocks(text) }

    Column(
        modifier = modifier,
        verticalArrangement = Arrangement.spacedBy(2.dp)
    ) {
        blocks.forEach { block ->
            when (block) {
                is MdBlock.Paragraph -> ParagraphBlock(block.spans)
                is MdBlock.Heading -> HeadingBlock(block.level, block.spans)
                is MdBlock.CodeBlock -> CodeBlockView(block.language, block.code)
                is MdBlock.UnorderedList -> UnorderedListBlock(block.items)
                is MdBlock.OrderedList -> OrderedListBlock(block.items)
                is MdBlock.Blockquote -> BlockquoteView(block.spans)
                is MdBlock.Table -> TableBlock(block.headers, block.rows)
                is MdBlock.HorizontalRule -> Divider(
                    modifier = Modifier.padding(vertical = 8.dp),
                    color = MdColors.border
                )
            }
        }
    }
}

// ── Inline rendering ─────────────────────────────────────────────────────────

/**
 * Build an AnnotatedString with styled spans from parsed inline elements.
 * Returns a pair of (AnnotatedString, hasLinks) so callers know whether to
 * use ClickableText.
 */
@Composable
private fun buildInlineString(spans: List<MdInline>): Pair<AnnotatedString, Boolean> {
    var hasLinks = false
    val annotated = buildAnnotatedString {
        spans.forEach { span ->
            when (span) {
                is MdInline.Text -> append(span.text)
                is MdInline.Bold -> {
                    withStyle(SpanStyle(fontWeight = FontWeight.Bold, color = MdColors.textBright)) {
                        append(span.text)
                    }
                }
                is MdInline.Italic -> {
                    withStyle(SpanStyle(fontStyle = FontStyle.Italic)) {
                        append(span.text)
                    }
                }
                is MdInline.BoldItalic -> {
                    withStyle(SpanStyle(fontWeight = FontWeight.Bold, fontStyle = FontStyle.Italic, color = MdColors.textBright)) {
                        append(span.text)
                    }
                }
                is MdInline.Code -> {
                    withStyle(SpanStyle(
                        fontFamily = FontFamily.Monospace,
                        fontSize = 13.sp,
                        color = MdColors.textBright,
                        background = MdColors.inlineCodeBg
                    )) {
                        append("\u00A0${span.text}\u00A0")
                    }
                }
                is MdInline.Link -> {
                    hasLinks = true
                    pushStringAnnotation(tag = "URL", annotation = span.url)
                    withStyle(SpanStyle(
                        color = MdColors.accent,
                        textDecoration = TextDecoration.Underline
                    )) {
                        append(span.text)
                    }
                    pop()
                }
            }
        }
    }
    return annotated to hasLinks
}

/**
 * Render inline spans as a Text or ClickableText composable.
 */
@Composable
private fun InlineText(
    spans: List<MdInline>,
    style: TextStyle,
    modifier: Modifier = Modifier
) {
    val (annotated, hasLinks) = buildInlineString(spans)
    val context = LocalContext.current

    if (hasLinks) {
        ClickableText(
            text = annotated,
            style = style,
            modifier = modifier,
            onClick = { offset ->
                annotated.getStringAnnotations(tag = "URL", start = offset, end = offset)
                    .firstOrNull()?.let { annotation ->
                        try {
                            context.startActivity(
                                Intent(Intent.ACTION_VIEW, Uri.parse(annotation.item))
                            )
                        } catch (_: Exception) { /* ignore bad URIs */ }
                    }
            }
        )
    } else {
        Text(text = annotated, style = style, modifier = modifier)
    }
}

// ── Block composables ────────────────────────────────────────────────────────

@Composable
private fun ParagraphBlock(spans: List<MdInline>) {
    InlineText(
        spans = spans,
        style = TextStyle(
            color = MdColors.text,
            fontSize = 15.sp,
            lineHeight = 22.sp
        ),
        modifier = Modifier.padding(vertical = 4.dp)
    )
}

@Composable
private fun HeadingBlock(level: Int, spans: List<MdInline>) {
    val (fontSize, topPad, bottomPad) = when (level) {
        1 -> Triple(20.sp, 14.dp, 6.dp)
        2 -> Triple(17.sp, 10.dp, 4.dp)
        else -> Triple(15.sp, 7.dp, 3.dp)
    }
    InlineText(
        spans = spans,
        style = TextStyle(
            color = MdColors.textBright,
            fontSize = fontSize,
            fontWeight = FontWeight.W600,
            lineHeight = fontSize * 1.3
        ),
        modifier = Modifier.padding(top = topPad, bottom = bottomPad)
    )
}

@Composable
private fun CodeBlockView(language: String, code: String) {
    val context = LocalContext.current
    val clipboardManager = LocalClipboardManager.current
    var copied by remember { mutableStateOf(false) }

    // Adaptive syntax highlighting
    val highlightEnabled = remember { MdCapabilities.canUseSyntaxHighlighting(context) }
    val styledCode = remember(code, language) {
        if (highlightEnabled && language.isNotBlank()) {
            highlightCode(language, code)
        } else {
            AnnotatedString(code, SpanStyle(color = MdColors.codeText))
        }
    }

    Surface(
        shape = RoundedCornerShape(8.dp),
        color = MdColors.codeBlockBg,
        border = BorderStroke(1.dp, MdColors.border),
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 4.dp)
    ) {
        Column {
            // Header bar: language label + copy button
            if (language.isNotBlank()) {
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .background(MdColors.codeHeaderBg)
                        .padding(start = 12.dp, end = 4.dp, top = 2.dp, bottom = 2.dp),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text(
                        text = language.uppercase(),
                        style = TextStyle(
                            fontSize = 10.sp,
                            fontWeight = FontWeight.W600,
                            letterSpacing = 0.8.sp,
                            color = MdColors.textMuted
                        )
                    )
                    TextButton(
                        onClick = {
                            clipboardManager.setText(AnnotatedString(code))
                            copied = true
                        },
                        contentPadding = PaddingValues(horizontal = 8.dp, vertical = 0.dp),
                        modifier = Modifier.height(28.dp)
                    ) {
                        Text(
                            text = if (copied) "Copied" else "Copy",
                            style = TextStyle(fontSize = 10.sp, color = MdColors.textMuted)
                        )
                    }
                }
                Divider(color = MdColors.borderSubtle)
            }

            // Code content with horizontal scroll
            val scrollState = rememberScrollState()
            Text(
                text = styledCode,
                style = TextStyle(
                    fontFamily = FontFamily.Monospace,
                    fontSize = 12.sp,
                    lineHeight = 18.sp
                ),
                modifier = Modifier
                    .horizontalScroll(scrollState)
                    .padding(12.dp)
            )
        }
    }

    // Reset "Copied" after 2 seconds
    if (copied) {
        LaunchedEffect(Unit) {
            delay(2000)
            copied = false
        }
    }
}

@Composable
private fun UnorderedListBlock(items: List<List<MdInline>>) {
    Column(modifier = Modifier.padding(start = 16.dp, top = 4.dp, bottom = 4.dp)) {
        items.forEach { item ->
            Row(modifier = Modifier.padding(vertical = 3.dp)) {
                Text(
                    text = "\u2022",
                    style = TextStyle(color = MdColors.text, fontSize = 15.sp),
                    modifier = Modifier.width(16.dp)
                )
                InlineText(
                    spans = item,
                    style = TextStyle(color = MdColors.text, fontSize = 15.sp, lineHeight = 22.sp),
                    modifier = Modifier.weight(1f)
                )
            }
        }
    }
}

@Composable
private fun OrderedListBlock(items: List<List<MdInline>>) {
    Column(modifier = Modifier.padding(start = 16.dp, top = 4.dp, bottom = 4.dp)) {
        items.forEachIndexed { index, item ->
            Row(modifier = Modifier.padding(vertical = 3.dp)) {
                Text(
                    text = "${index + 1}.",
                    style = TextStyle(color = MdColors.text, fontSize = 15.sp),
                    modifier = Modifier.width(24.dp)
                )
                InlineText(
                    spans = item,
                    style = TextStyle(color = MdColors.text, fontSize = 15.sp, lineHeight = 22.sp),
                    modifier = Modifier.weight(1f)
                )
            }
        }
    }
}

@Composable
private fun BlockquoteView(spans: List<MdInline>) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .height(IntrinsicSize.Min)
            .padding(vertical = 4.dp)
    ) {
        // Left accent bar (3dp, matching web: border-left: 3px solid --border-strong)
        Box(
            modifier = Modifier
                .width(3.dp)
                .fillMaxHeight()
                .background(MdColors.borderStrong)
        )
        InlineText(
            spans = spans,
            style = TextStyle(
                color = MdColors.textMuted,
                fontStyle = FontStyle.Italic,
                fontSize = 15.sp,
                lineHeight = 22.sp
            ),
            modifier = Modifier.padding(start = 14.dp, top = 4.dp, bottom = 4.dp)
        )
    }
}

@Composable
private fun TableBlock(headers: List<String>, rows: List<List<String>>) {
    Surface(
        shape = RoundedCornerShape(8.dp),
        border = BorderStroke(1.dp, MdColors.border),
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 4.dp)
    ) {
        val scrollState = rememberScrollState()
        Column(modifier = Modifier.horizontalScroll(scrollState)) {
            // Header row
            Row(modifier = Modifier.background(MdColors.bgElevated)) {
                headers.forEach { header ->
                    Text(
                        text = header,
                        style = TextStyle(
                            fontWeight = FontWeight.W600,
                            fontSize = 11.sp,
                            color = MdColors.textBright,
                            letterSpacing = 0.4.sp
                        ),
                        modifier = Modifier
                            .border(0.5.dp, MdColors.border)
                            .padding(horizontal = 12.dp, vertical = 7.dp)
                            .defaultMinSize(minWidth = 60.dp)
                    )
                }
            }
            // Data rows
            rows.forEach { row ->
                Row {
                    row.forEach { cell ->
                        Text(
                            text = cell,
                            style = TextStyle(fontSize = 12.sp, color = MdColors.text),
                            modifier = Modifier
                                .border(0.5.dp, MdColors.border)
                                .padding(horizontal = 12.dp, vertical = 7.dp)
                                .defaultMinSize(minWidth = 60.dp)
                        )
                    }
                }
            }
        }
    }
}
