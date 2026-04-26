package com.assistant.peripheral.ui.screens

import androidx.compose.animation.animateContentSize
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.clipToBounds
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.assistant.peripheral.data.*
import com.assistant.peripheral.ui.components.VoiceButton
import com.assistant.peripheral.ui.components.markdown.MdColors
import com.assistant.peripheral.ui.components.markdown.MarkdownText
import kotlinx.coroutines.launch

@Composable
fun ChatScreen(
    messages: List<ChatMessage>,
    hasMoreMessages: Boolean,
    isLoadingMoreMessages: Boolean,
    onLoadMoreMessages: () -> Unit,
    modifier: Modifier = Modifier
) {
    val listState = rememberLazyListState()
    val coroutineScope = rememberCoroutineScope()

    // Track if user is at bottom (within 2 items)
    val isAtBottom by remember {
        derivedStateOf {
            val lastVisibleIndex = listState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: 0
            val totalItems = listState.layoutInfo.totalItemsCount
            totalItems == 0 || lastVisibleIndex >= totalItems - 2
        }
    }

    // Track if user is at top (for loading more messages)
    val isAtTop by remember {
        derivedStateOf {
            val firstVisibleIndex = listState.firstVisibleItemIndex
            firstVisibleIndex <= 1
        }
    }

    // Load more messages when scrolling to top
    LaunchedEffect(isAtTop, hasMoreMessages, isLoadingMoreMessages) {
        if (isAtTop && hasMoreMessages && !isLoadingMoreMessages && messages.isNotEmpty()) {
            onLoadMoreMessages()
        }
    }

    // Remember last message count to detect new messages
    var lastMessageCount by remember { mutableStateOf(messages.size) }

    // Scroll to bottom on initial load or when messages change AND user is at bottom
    LaunchedEffect(messages.size, messages.lastOrNull()?.content) {
        if (messages.isNotEmpty()) {
            // If new message added and user is at bottom, scroll to bottom
            // Also scroll on first load (lastMessageCount was 0)
            if (lastMessageCount == 0 || (messages.size > lastMessageCount && isAtBottom)) {
                listState.animateScrollToItem(messages.size - 1)
            }
            lastMessageCount = messages.size
        }
    }

    Column(
        modifier = modifier.fillMaxSize()
    ) {
        // Messages list
        Box(modifier = Modifier.weight(1f)) {
            LazyColumn(
                state = listState,
                modifier = Modifier.fillMaxSize(),
                contentPadding = PaddingValues(horizontal = 8.dp, vertical = 8.dp),
                verticalArrangement = Arrangement.spacedBy(14.dp)
            ) {
                // Loading indicator at top when fetching older messages
                if (isLoadingMoreMessages) {
                    item(key = "loading_indicator") {
                        Box(
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(8.dp),
                            contentAlignment = Alignment.Center
                        ) {
                            CircularProgressIndicator(
                                modifier = Modifier.size(24.dp),
                                strokeWidth = 2.dp
                            )
                        }
                    }
                }

                // "Load more" indicator when there are more messages
                if (hasMoreMessages && !isLoadingMoreMessages) {
                    item(key = "load_more_indicator") {
                        Box(
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(8.dp),
                            contentAlignment = Alignment.Center
                        ) {
                            Text(
                                text = "↑ Scroll up for older messages",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant
                            )
                        }
                    }
                }

                items(messages, key = { it.id }) { message ->
                    MessageItem(message)
                }
            }

            // Scroll to bottom FAB when not at bottom
            if (!isAtBottom && messages.isNotEmpty()) {
                SmallFloatingActionButton(
                    onClick = {
                        coroutineScope.launch {
                            listState.animateScrollToItem(messages.size - 1)
                        }
                    },
                    containerColor = MaterialTheme.colorScheme.primaryContainer,
                    modifier = Modifier
                        .align(Alignment.BottomEnd)
                        .padding(16.dp)
                ) {
                    Icon(
                        Icons.Default.KeyboardArrowDown,
                        contentDescription = "Scroll to bottom"
                    )
                }
            }
        }
    }
}

// User bubble palette — middle ground between the original Material primaryContainer
// and the web's `--user-bg` (#18181F). Keeps the soft, muted feel of the web while
// retaining enough blue saturation to distinguish user turns from assistant prose.
private val UserBubbleBg = Color(0xFF1B2338)
private val UserBubbleBorder = Color(0xFF303852)
private val UserBubbleText = Color(0xFFEEEEF2)

// Foldable user message — match web .user-text-foldable behaviour
private const val USER_FOLD_LINE_THRESHOLD = 25
private val USER_FOLD_COLLAPSED_MAX_HEIGHT = 150.dp

@Composable
private fun MessageItem(message: ChatMessage) {
    val isUser = message.role == MessageRole.USER
    val isSystem = message.role == MessageRole.SYSTEM

    // Check for compact block (full-width divider)
    val compactBlock = message.blocks.filterIsInstance<MessageBlock.Compact>().firstOrNull()
    if (compactBlock != null) {
        CompactDivider(compactBlock.summary)
        return
    }

    // Skip empty user/system messages (e.g. tool_result protocol wrappers).
    // Assistant messages may legitimately be empty while streaming.
    if ((isUser || isSystem) && message.content.isEmpty() && message.blocks.isEmpty()) {
        return
    }

    Column(
        modifier = Modifier.fillMaxWidth(),
        horizontalAlignment = if (isUser) Alignment.End else Alignment.Start
    ) {
        if (isUser || isSystem) {
            // USER / SYSTEM: Keep bubble with rounded corners
            Surface(
                shape = RoundedCornerShape(
                    topStart = 16.dp,
                    topEnd = 16.dp,
                    bottomStart = if (isUser) 16.dp else 4.dp,
                    bottomEnd = if (isUser) 4.dp else 16.dp
                ),
                color = when {
                    isUser -> UserBubbleBg
                    else -> MaterialTheme.colorScheme.errorContainer.copy(alpha = 0.6f)
                },
                border = if (isUser) BorderStroke(1.dp, UserBubbleBorder) else null,
                modifier = Modifier
                    .widthIn(max = 340.dp)
                    .animateContentSize()
            ) {
                Column(modifier = Modifier.padding(12.dp)) {
                    if (message.blocks.isNotEmpty()) {
                        message.blocks.forEachIndexed { index, block ->
                            if (index > 0) Spacer(modifier = Modifier.height(8.dp))
                            MessageBlockView(block, isUser = isUser)
                        }
                    } else {
                        val text = message.content.ifEmpty { if (message.isStreaming) "..." else "" }
                        if (isUser) {
                            UserTextBlock(text)
                        } else {
                            Text(
                                text = text,
                                style = MaterialTheme.typography.bodyMedium,
                                color = MaterialTheme.colorScheme.onErrorContainer
                            )
                        }
                    }

                    if (message.isStreaming) {
                        Spacer(modifier = Modifier.height(8.dp))
                        LinearProgressIndicator(
                            modifier = Modifier.fillMaxWidth().height(2.dp),
                            color = MaterialTheme.colorScheme.primary
                        )
                    }
                }
            }
        } else {
            // ASSISTANT: Full-width prose, no bubble (matches web message-assistant)
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 4.dp)
                    .animateContentSize()
            ) {
                if (message.blocks.isNotEmpty()) {
                    message.blocks.forEachIndexed { index, block ->
                        if (index > 0) Spacer(modifier = Modifier.height(4.dp))
                        MessageBlockView(block, isUser = false)
                    }
                } else {
                    Text(
                        text = message.content.ifEmpty { if (message.isStreaming) "..." else "" },
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurface
                    )
                }

                if (message.isStreaming) {
                    Spacer(modifier = Modifier.height(8.dp))
                    LinearProgressIndicator(
                        modifier = Modifier.fillMaxWidth().height(2.dp),
                        color = MaterialTheme.colorScheme.primary
                    )
                }
            }
        }
    }
}

@Composable
private fun MessageBlockView(block: MessageBlock, isUser: Boolean = false) {
    when (block) {
        is MessageBlock.Text -> {
            val text = block.text.ifEmpty { if (block.isStreaming) "..." else "" }
            if (isUser) {
                // User messages: plain text, no markdown (matches web .user-text)
                // Foldable when over the line threshold (matches web .user-text-foldable)
                UserTextBlock(text)
            } else {
                // Assistant messages: full markdown rendering
                MarkdownText(text = text)
            }
        }

        is MessageBlock.Thinking -> {
            ThinkingBlock(block)
        }

        is MessageBlock.ToolUse -> {
            ToolUseBlock(block)
        }

        is MessageBlock.Compact -> {
            // Handled separately as full-width divider
        }
    }
}

/**
 * User-side text block. Mirrors web `.user-text` / `.user-text-foldable`:
 * messages with more than [USER_FOLD_LINE_THRESHOLD] hard line breaks render
 * collapsed (capped at [USER_FOLD_COLLAPSED_MAX_HEIGHT] with a bottom fade)
 * and offer a "Show all (N lines)" / "Show less" toggle.
 */
@Composable
private fun UserTextBlock(content: String) {
    val lineCount = remember(content) { content.count { it == '\n' } + 1 }
    val isTall = lineCount > USER_FOLD_LINE_THRESHOLD
    var expanded by remember(content) { mutableStateOf(false) }

    if (!isTall) {
        Text(
            text = content,
            style = MaterialTheme.typography.bodyMedium,
            color = UserBubbleText
        )
        return
    }

    Column(modifier = Modifier.animateContentSize()) {
        Box {
            Text(
                text = content,
                style = MaterialTheme.typography.bodyMedium,
                color = UserBubbleText,
                modifier = if (expanded) Modifier else Modifier
                    .heightIn(max = USER_FOLD_COLLAPSED_MAX_HEIGHT)
                    .clipToBounds()
            )
            if (!expanded) {
                // Bottom-of-bubble fade matching web `mask-image: linear-gradient(...)`
                Box(
                    modifier = Modifier
                        .matchParentSize()
                        .background(
                            Brush.verticalGradient(
                                colorStops = arrayOf(
                                    0.0f to UserBubbleBg.copy(alpha = 0f),
                                    0.6f to UserBubbleBg.copy(alpha = 0f),
                                    1.0f to UserBubbleBg
                                )
                            )
                        )
                )
            }
        }
        TextButton(
            onClick = { expanded = !expanded },
            contentPadding = PaddingValues(horizontal = 0.dp, vertical = 0.dp),
            modifier = Modifier
                .align(Alignment.End)
                .heightIn(min = 24.dp)
                .padding(top = 4.dp)
        ) {
            Text(
                text = if (expanded) "Show less" else "Show all ($lineCount lines)",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.primary,
                fontSize = 11.sp
            )
        }
    }
}

@Composable
private fun ThinkingBlock(block: MessageBlock.Thinking) {
    var expanded by remember { mutableStateOf(block.isStreaming) }

    // Left-border-only style matching web .thinking-block
    val borderColor = MdColors.thinkingBorder
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .drawBehind {
                // 2dp left border in amber
                drawLine(
                    color = borderColor,
                    start = Offset(0f, 0f),
                    end = Offset(0f, size.height),
                    strokeWidth = 2.dp.toPx()
                )
            }
            .background(
                color = MdColors.thinkingBg,
                shape = RoundedCornerShape(topEnd = 8.dp, bottomEnd = 8.dp)
            )
            .clickable { expanded = !expanded }
    ) {
        // Toggle header
        Row(
            modifier = Modifier.padding(horizontal = 14.dp, vertical = 9.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            // Streaming dots
            if (block.isStreaming) {
                Text(
                    text = "...",
                    style = MaterialTheme.typography.labelMedium,
                    color = MdColors.thinkingBorder
                )
                Spacer(modifier = Modifier.width(8.dp))
            }
            Text(
                text = if (block.isStreaming) "Thinking" else "Thought",
                style = MaterialTheme.typography.labelMedium.copy(
                    fontWeight = androidx.compose.ui.text.font.FontWeight.W600
                ),
                color = MdColors.thinkingBorder
            )
            Spacer(modifier = Modifier.weight(1f))
            Text(
                text = if (expanded) "\u2212" else "+",  // − or +
                style = MaterialTheme.typography.labelMedium,
                color = MdColors.textMuted,
                fontSize = 14.sp
            )
        }

        // Content (collapsible)
        if (expanded) {
            Text(
                text = block.text,
                style = MaterialTheme.typography.bodySmall.copy(
                    fontStyle = FontStyle.Italic,
                    fontSize = 13.sp,
                    lineHeight = 18.sp
                ),
                color = MdColors.textMuted,
                modifier = Modifier.padding(start = 14.dp, end = 14.dp, bottom = 10.dp)
            )
        }
    }
}

@Composable
private fun ToolUseBlock(block: MessageBlock.ToolUse) {
    var expanded by remember { mutableStateOf(false) }

    val category = getToolCategory(block.toolName)
    val toolColor = ToolPalette.colorFor(category, block.isError)
    val toolBg = ToolPalette.backgroundFor(category, block.isError)
    val summary = formatToolSummary(block.toolName, block.toolInput)

    // Left-border-only style matching web .tool-block
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .drawBehind {
                drawLine(
                    color = toolColor,
                    start = Offset(0f, 0f),
                    end = Offset(0f, size.height),
                    strokeWidth = 2.dp.toPx()
                )
            }
            .background(
                color = toolBg,
                shape = RoundedCornerShape(topEnd = 8.dp, bottomEnd = 8.dp)
            )
            .clickable { expanded = !expanded }
    ) {
        // Toggle header
        Row(
            modifier = Modifier.padding(horizontal = 14.dp, vertical = 9.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Icon(
                imageVector = getToolIcon(block.toolName, block.isError, block.isComplete),
                contentDescription = null,
                modifier = Modifier.size(15.dp),
                tint = toolColor
            )
            Spacer(modifier = Modifier.width(8.dp))
            Text(
                text = summary,
                style = MaterialTheme.typography.labelMedium.copy(
                    fontFamily = FontFamily.Monospace,
                    fontWeight = androidx.compose.ui.text.font.FontWeight.W600,
                    fontSize = 12.sp
                ),
                color = toolColor,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f)
            )

            // Status badge (matching web .tool-status)
            when {
                block.isExecuting -> {
                    CircularProgressIndicator(
                        modifier = Modifier.size(14.dp),
                        strokeWidth = 2.dp,
                        color = toolColor
                    )
                }
                block.isComplete -> {
                    Row(
                        modifier = Modifier
                            .background(
                                color = if (block.isError) MaterialTheme.colorScheme.errorContainer
                                        else MaterialTheme.colorScheme.tertiaryContainer,
                                shape = RoundedCornerShape(5.dp)
                            )
                            .padding(horizontal = 8.dp, vertical = 2.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Icon(
                            imageVector = if (block.isError) Icons.Default.Error else Icons.Default.CheckCircle,
                            contentDescription = null,
                            modifier = Modifier.size(10.dp),
                            tint = if (block.isError) MaterialTheme.colorScheme.error
                                    else MaterialTheme.colorScheme.tertiary
                        )
                        Spacer(modifier = Modifier.width(4.dp))
                        Text(
                            text = if (block.isError) "error" else "done",
                            style = MaterialTheme.typography.labelSmall.copy(
                                fontSize = 9.sp,
                                fontWeight = androidx.compose.ui.text.font.FontWeight.W600
                            ),
                            color = if (block.isError) MaterialTheme.colorScheme.error
                                    else MaterialTheme.colorScheme.tertiary
                        )
                    }
                }
            }

            Spacer(modifier = Modifier.width(8.dp))
            Text(
                text = if (expanded) "\u25BC" else "\u25B6",  // ▼ or ▶
                style = MaterialTheme.typography.labelSmall,
                color = MdColors.textMuted,
                fontSize = 10.sp
            )
        }

        if (expanded) {
            ToolBlockExpandedContent(block)
        }
    }
}

/**
 * Body shown when a tool block is expanded. Mirrors the web frontend layout:
 * an Input section followed by an Output section. Both are always rendered
 * once expanded, even if empty - the placeholder text matches what the user
 * sees on the web ("(no output)" rather than the bare string "null").
 */
@Composable
private fun ToolBlockExpandedContent(block: MessageBlock.ToolUse) {
    Column(modifier = Modifier.padding(start = 14.dp, end = 14.dp, bottom = 10.dp)) {
        // Input
        if (block.toolInput.isNotEmpty()) {
            Text(
                text = "Input",
                style = MaterialTheme.typography.labelSmall.copy(
                    fontWeight = androidx.compose.ui.text.font.FontWeight.W600,
                    fontSize = 10.sp,
                    letterSpacing = 0.4.sp
                ),
                color = MdColors.textMuted,
                modifier = Modifier.padding(bottom = 4.dp)
            )
            Surface(
                shape = RoundedCornerShape(4.dp),
                color = MdColors.bgElevated,
                modifier = Modifier.fillMaxWidth()
            ) {
                val inputScroll = rememberScrollState()
                Text(
                    text = formatToolInput(block.toolName, block.toolInput),
                    style = MaterialTheme.typography.bodySmall.copy(
                        fontFamily = FontFamily.Monospace,
                        fontSize = 11.sp,
                        lineHeight = 16.sp
                    ),
                    color = MdColors.text,
                    modifier = Modifier
                        .horizontalScroll(inputScroll)
                        .padding(8.dp)
                )
            }
            Spacer(modifier = Modifier.height(8.dp))
        }

        // Output
        Text(
            text = "Output",
            style = MaterialTheme.typography.labelSmall.copy(
                fontWeight = androidx.compose.ui.text.font.FontWeight.W600,
                fontSize = 10.sp,
                letterSpacing = 0.4.sp
            ),
            color = MdColors.textMuted,
            modifier = Modifier.padding(bottom = 4.dp)
        )
        Surface(
            shape = RoundedCornerShape(4.dp),
            color = MdColors.bgElevated,
            modifier = Modifier.fillMaxWidth()
        ) {
            val outputScroll = rememberScrollState()
            val resultText = block.result
            val (display, isPlaceholder) = when {
                resultText == null -> "(no output)" to true
                resultText.isEmpty() -> "(empty)" to true
                else -> resultText to false
            }
            Text(
                text = display,
                style = MaterialTheme.typography.bodySmall.copy(
                    fontFamily = FontFamily.Monospace,
                    fontSize = 11.sp,
                    lineHeight = 16.sp,
                    fontStyle = if (isPlaceholder) FontStyle.Italic else FontStyle.Normal
                ),
                color = if (isPlaceholder) MdColors.textMuted else MdColors.text,
                modifier = Modifier
                    .horizontalScroll(outputScroll)
                    .padding(8.dp)
            )
        }
    }
}

/**
 * Render a tool's input map as a key-value list. Long string values are
 * truncated so the expanded view stays usable on a phone screen.
 */
private fun formatToolInput(toolName: String, input: Map<String, Any?>): String {
    if (input.isEmpty()) return "(no input)"
    val builder = StringBuilder()
    for ((key, value) in input) {
        val rendered = when (value) {
            null -> "null"
            is String -> if (value.length > 400) value.take(400) + "…" else value
            is Map<*, *> -> value.toString()
            is List<*> -> value.toString()
            else -> value.toString()
        }
        builder.append(key).append(": ").append(rendered).append('\n')
    }
    return builder.toString().trimEnd()
}

@Composable
private fun CompactDivider(summary: String) {
    var expanded by remember { mutableStateOf(false) }

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 16.dp)
    ) {
        Divider(color = MaterialTheme.colorScheme.outlineVariant)
        Spacer(modifier = Modifier.height(8.dp))
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .clickable(enabled = summary.isNotBlank()) { expanded = !expanded },
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.Center
        ) {
            Text(
                text = "\u27F3",  // ⟳
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.5f)
            )
            Spacer(modifier = Modifier.width(8.dp))
            Text(
                text = "Context compacted",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.5f)
            )
            if (summary.isNotBlank()) {
                Text(
                    text = if (expanded) " \u25B2" else " \u25BC",  // ▲ or ▼
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.5f)
                )
            }
        }
        if (expanded && summary.isNotBlank()) {
            Spacer(modifier = Modifier.height(8.dp))
            Text(
                text = summary,
                style = MaterialTheme.typography.bodySmall.copy(
                    fontStyle = FontStyle.Italic,
                    lineHeight = 18.sp
                ),
                color = MdColors.textMuted,
                modifier = Modifier.padding(horizontal = 16.dp)
            )
        }
        Spacer(modifier = Modifier.height(8.dp))
        Divider(color = MaterialTheme.colorScheme.outlineVariant)
    }
}

@Composable
fun ChatInputBar(
    inputText: String,
    onInputChange: (String) -> Unit,
    onSend: () -> Unit,
    isRecording: Boolean,
    onStartRecording: () -> Unit,
    onStopRecording: () -> Unit,
    isConnected: Boolean,
    isStreaming: Boolean,
    voiceState: VoiceState,
    onStartVoice: () -> Unit,
    onStopVoice: () -> Unit,
    isOrchestratorSession: Boolean
) {
    Surface(
        modifier = Modifier.fillMaxWidth(),
        color = MaterialTheme.colorScheme.surfaceVariant,
        shadowElevation = 6.dp
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 12.dp, vertical = 6.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            // Voice conversation button (WebRTC realtime) - only for orchestrator
            if (isOrchestratorSession) {
                VoiceButton(
                    voiceState = voiceState,
                    onStart = onStartVoice,
                    onStop = onStopVoice,
                    modifier = Modifier.size(48.dp)
                )
                Spacer(modifier = Modifier.width(8.dp))
            }

            OutlinedTextField(
                value = inputText,
                onValueChange = onInputChange,
                modifier = Modifier.weight(1f),
                placeholder = { Text("Type a message...") },
                singleLine = false,
                maxLines = 4,
                enabled = isConnected && !isRecording && !isStreaming,
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                keyboardActions = KeyboardActions(onSend = { onSend() }),
                shape = RoundedCornerShape(24.dp)
            )

            Spacer(modifier = Modifier.width(8.dp))

            // Voice record button (push-to-talk for audio messages) - only for orchestrator
            if (isOrchestratorSession) {
                IconButton(
                    onClick = {
                        if (isRecording) onStopRecording() else onStartRecording()
                    },
                    enabled = isConnected && !isStreaming,
                    modifier = Modifier
                        .size(48.dp)
                        .clip(CircleShape)
                        .background(
                            if (isRecording) MaterialTheme.colorScheme.error
                            else MaterialTheme.colorScheme.surfaceVariant
                        )
                ) {
                    Icon(
                        imageVector = if (isRecording) Icons.Default.Stop else Icons.Default.Mic,
                        contentDescription = if (isRecording) "Stop recording" else "Voice message",
                        tint = if (isRecording) MaterialTheme.colorScheme.onError
                               else MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
                Spacer(modifier = Modifier.width(4.dp))
            }

            // Send button
            IconButton(
                onClick = onSend,
                enabled = isConnected && inputText.isNotBlank() && !isRecording && !isStreaming,
                modifier = Modifier
                    .size(48.dp)
                    .clip(CircleShape)
                    .background(
                        if (isConnected && inputText.isNotBlank() && !isRecording && !isStreaming) {
                            MaterialTheme.colorScheme.primary
                        } else {
                            MaterialTheme.colorScheme.primary.copy(alpha = 0.3f)
                        }
                    )
            ) {
                Icon(
                    imageVector = Icons.Default.Send,
                    contentDescription = "Send",
                    tint = MaterialTheme.colorScheme.onPrimary
                )
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Tool category + palette
// ---------------------------------------------------------------------------
//
// Mirrors the web frontend's semantic, action-based coloring (frontend/src/
// components/ToolUseBlock.tsx + index.css). Categories drive both the left
// border colour and the tinted background, so the same tool reads as the same
// colour family on web and mobile.
//
// HSL values from index.css were converted to ARGB on a per-channel basis;
// the alpha for backgrounds matches the web's `hsla(..., 0.07)` -> ~0x12.

private enum class ToolCategory { READ, WRITE, EXECUTE, SCRIPT, NAVIGATE, CAPTURE, INTERACT, TODO, TASK, AGENT, SEARCH, SYSTEM }

private fun getToolCategory(toolName: String): ToolCategory {
    // Read/inspect tools (passive observation)
    if (toolName in setOf("Read", "Glob", "Grep", "WebFetch", "WebSearch")) return ToolCategory.READ
    // Write/modify tools
    if (toolName in setOf("Write", "Edit", "NotebookEdit")) return ToolCategory.WRITE
    // Todo tracking
    if (toolName == "TodoWrite") return ToolCategory.TODO
    // Task delegation (subagents)
    if (toolName == "Task") return ToolCategory.TASK
    // Execute tools
    if (toolName in setOf("Bash", "Skill", "EnterPlanMode", "ExitPlanMode")) return ToolCategory.EXECUTE
    // User interaction
    if (toolName == "AskUserQuestion") return ToolCategory.INTERACT
    // Orchestrator agent session tools
    if (toolName in setOf(
            "list_agent_sessions", "open_agent_session", "close_agent_session",
            "read_agent_session", "send_to_agent_session", "interrupt_agent_session",
            "list_history"
        )) return ToolCategory.AGENT
    // Orchestrator search tools
    if (toolName in setOf("search_history", "search_memory")) return ToolCategory.SEARCH
    // Orchestrator file tools - reuse existing categories
    if (toolName == "read_file") return ToolCategory.READ
    if (toolName == "write_file") return ToolCategory.WRITE

    // Browser MCP tools - categorize by action type
    if (toolName.startsWith("mcp__chrome-devtools__")) {
        val action = toolName.removePrefix("mcp__chrome-devtools__")
        if (action in setOf("navigate_page", "click", "hover", "drag", "fill", "fill_form",
                "press_key", "handle_dialog", "new_page", "close_page", "select_page",
                "resize_page", "wait_for")) return ToolCategory.NAVIGATE
        if (action in setOf("take_screenshot", "take_snapshot", "performance_start_trace",
                "performance_stop_trace", "performance_analyze_insight")) return ToolCategory.CAPTURE
        if (action == "evaluate_script") return ToolCategory.SCRIPT
        if (action in setOf("list_console_messages", "list_network_requests", "get_console_message",
                "get_network_request", "list_pages", "emulate")) return ToolCategory.READ
    }

    return ToolCategory.SYSTEM
}

private object ToolPalette {
    // Border / icon / text colours per category (HSL -> ARGB from index.css).
    private val borderColors = mapOf(
        ToolCategory.READ     to Color(0xFF6E99C4),
        ToolCategory.WRITE    to Color(0xFF59B18C),
        ToolCategory.EXECUTE  to Color(0xFFC2975B),
        ToolCategory.SCRIPT   to Color(0xFFBD8156),
        ToolCategory.NAVIGATE to Color(0xFFB276A3),
        ToolCategory.CAPTURE  to Color(0xFF58ABBB),
        ToolCategory.INTERACT to Color(0xFFC78B57),
        ToolCategory.TODO     to Color(0xFF9976B2),
        ToolCategory.TASK     to Color(0xFF7B6DB0),
        ToolCategory.AGENT    to Color(0xFF4EB2BC),
        ToolCategory.SEARCH   to Color(0xFF876EB9),
        ToolCategory.SYSTEM   to Color(0xFF737F96),
    )

    // Translucent fill — same hue as the border but at ~7% opacity (matches
    // the web's `hsla(..., 0.07)` background).
    private val backgroundColors = mapOf(
        ToolCategory.READ     to Color(0x126E99C4),
        ToolCategory.WRITE    to Color(0x1259B18C),
        ToolCategory.EXECUTE  to Color(0x12C2975B),
        ToolCategory.SCRIPT   to Color(0x12BD8156),
        ToolCategory.NAVIGATE to Color(0x12B276A3),
        ToolCategory.CAPTURE  to Color(0x1258ABBB),
        ToolCategory.INTERACT to Color(0x12C78B57),
        ToolCategory.TODO     to Color(0x149976B2),
        ToolCategory.TASK     to Color(0x127B6DB0),
        ToolCategory.AGENT    to Color(0x124EB2BC),
        ToolCategory.SEARCH   to Color(0x12876EB9),
        ToolCategory.SYSTEM   to Color(0x12737F96),
    )

    private val errorColor = Color(0xFFD45F5F)        // --error
    private val errorBg = Color(0x1AD45F5F)           // --error-bg (~10% alpha)

    fun colorFor(category: ToolCategory, isError: Boolean): Color =
        if (isError) errorColor else borderColors.getValue(category)

    fun backgroundFor(category: ToolCategory, isError: Boolean): Color =
        if (isError) errorBg else backgroundColors.getValue(category)
}

// Format a contextual summary for the tool (matching web formatToolSummary)
private fun formatToolSummary(toolName: String, input: Map<String, Any?>): String {
    fun formatPath(path: Any?): String {
        val s = path?.toString() ?: return toolName
        val parts = s.split("/")
        return if (parts.size > 3) ".../${parts.takeLast(2).joinToString("/")}" else s
    }
    fun shortId(id: Any?): String = id?.toString()?.take(8) ?: ""

    when (toolName) {
        "Read" -> return input["file_path"]?.let { "Read ${formatPath(it)}" } ?: "Read"
        "Write" -> return input["file_path"]?.let { "Write ${formatPath(it)}" } ?: "Write"
        "Edit" -> return input["file_path"]?.let { "Edit ${formatPath(it)}" } ?: "Edit"
        "NotebookEdit" -> return input["notebook_path"]?.let { "Edit notebook ${formatPath(it)}" } ?: "Edit notebook"
        "Bash" -> {
            val desc = input["description"]?.toString()
            val cmd = input["command"]?.toString()
            return when {
                desc != null -> desc
                cmd != null -> cmd.take(60) + if (cmd.length > 60) "..." else ""
                else -> "Bash"
            }
        }
        "Glob" -> return input["pattern"]?.let { "Glob $it" } ?: "Glob"
        "Grep" -> return input["pattern"]?.let { "Grep \"$it\"" } ?: "Grep"
        "WebFetch" -> return input["url"]?.let { "Fetch $it" } ?: "WebFetch"
        "WebSearch" -> return input["query"]?.let { "Search \"$it\"" } ?: "WebSearch"
        "Task" -> return input["description"]?.let { "Task: $it" } ?: "Task"
        "TodoWrite" -> return "Update todos"
        "AskUserQuestion" -> return "Ask user"
        "Skill" -> return input["skill"]?.let { "/$it" } ?: "Skill"
        "EnterPlanMode" -> return "Enter plan mode"
        "ExitPlanMode" -> return "Exit plan mode"

        // Orchestrator tools
        "list_agent_sessions" -> return "List active sessions"
        "open_agent_session" -> return if (input["resume_sdk_id"] != null) "Resume session" else "Open agent session"
        "close_agent_session" -> return input["session_id"]?.let { "Close session ${shortId(it)}" } ?: "Close session"
        "read_agent_session" -> return input["session_id"]?.let { "Read session ${shortId(it)}" } ?: "Read session"
        "send_to_agent_session" -> {
            val msg = input["message"]?.toString()
            return msg?.take(60)?.let { it + if (msg.length > 60) "..." else "" } ?: "Send to agent"
        }
        "interrupt_agent_session" -> return input["session_id"]?.let { "Interrupt session ${shortId(it)}" } ?: "Interrupt session"
        "list_history" -> return "List session history"
        "search_history" -> return input["query"]?.let { "Search history \"$it\"" } ?: "Search history"
        "search_memory" -> return input["query"]?.let { "Search memory \"$it\"" } ?: "Search memory"
        "read_file" -> return input["path"]?.let { "Read ${formatPath(it)}" } ?: "Read file"
        "write_file" -> return input["path"]?.let { "Write ${formatPath(it)}" } ?: "Write file"
    }

    if (toolName.startsWith("mcp__chrome-devtools__")) {
        val action = toolName.removePrefix("mcp__chrome-devtools__")
        return when (action) {
            "navigate_page" -> when (input["type"]) {
                "reload" -> "Reload page"
                "back" -> "Go back"
                "forward" -> "Go forward"
                else -> input["url"]?.let { "Navigate to $it" } ?: "Navigate"
            }
            "click" -> if (input["dblClick"] == true) "Double click" else "Click"
            "fill" -> "Fill input"
            "fill_form" -> "Fill form"
            "take_screenshot" -> "Screenshot"
            "take_snapshot" -> "Snapshot"
            "evaluate_script" -> "Run script"
            "emulate" -> "Emulate device"
            "hover" -> "Hover"
            "drag" -> "Drag"
            "press_key" -> input["key"]?.let { "Press $it" } ?: "Press key"
            "wait_for" -> input["text"]?.let { "Wait for \"$it\"" } ?: "Wait"
            "list_pages" -> "List pages"
            "list_console_messages" -> "List console"
            "list_network_requests" -> "List network"
            "select_page" -> "Select page #${input["pageId"]}"
            "new_page" -> "New page"
            "close_page" -> "Close page"
            "resize_page" -> "Resize to ${input["width"]}x${input["height"]}"
            "performance_start_trace" -> "Start trace"
            "performance_stop_trace" -> "Stop trace"
            "get_console_message" -> "Get console message"
            "get_network_request" -> "Get network request"
            "handle_dialog" -> if (input["action"] == "accept") "Accept dialog" else "Dismiss dialog"
            "upload_file" -> "Upload file"
            else -> action.replace('_', ' ')
        }
    }

    if (toolName.startsWith("mcp__")) {
        val parts = toolName.split("__")
        return if (parts.size >= 3) parts[2].replace('_', ' ') else toolName
    }

    return toolName
}

// Tool icon mapping (matches web frontend, see ToolUseBlock.tsx getToolIcon)
private fun getToolIcon(
    toolName: String,
    isError: Boolean,
    isComplete: Boolean
): androidx.compose.ui.graphics.vector.ImageVector {
    if (!isComplete) return Icons.Default.MoreHoriz
    if (isError) return Icons.Default.Error

    when (toolName) {
        "Read" -> return Icons.Default.Description
        "Write" -> return Icons.Default.Code
        "Edit" -> return Icons.Default.Edit
        "Bash" -> return Icons.Default.Terminal
        "Glob", "Grep" -> return Icons.Default.Search
        "WebFetch" -> return Icons.Default.Language
        "WebSearch" -> return Icons.Default.Search
        "Task" -> return Icons.Default.SmartToy
        "TodoWrite" -> return Icons.Default.Checklist
        "AskUserQuestion" -> return Icons.Default.HelpOutline
        "Skill" -> return Icons.Default.AutoAwesome
        "EnterPlanMode", "ExitPlanMode" -> return Icons.Default.EditNote
        "NotebookEdit" -> return Icons.Default.MenuBook

        // Orchestrator tools
        "list_agent_sessions" -> return Icons.Default.List
        "open_agent_session" -> return Icons.Default.OpenInNew
        "close_agent_session" -> return Icons.Default.Close
        "read_agent_session" -> return Icons.Default.Visibility
        "send_to_agent_session" -> return Icons.Default.Send
        "interrupt_agent_session" -> return Icons.Default.Stop
        "list_history" -> return Icons.Default.History
        "search_history", "search_memory" -> return Icons.Default.Search
        "read_file" -> return Icons.Default.Description
        "write_file" -> return Icons.Default.Code
    }

    if (toolName.startsWith("mcp__chrome-devtools__")) {
        val action = toolName.removePrefix("mcp__chrome-devtools__")
        return when (action) {
            "navigate_page" -> Icons.Default.Navigation
            "click", "hover", "drag" -> Icons.Default.TouchApp
            "fill", "fill_form", "press_key" -> Icons.Default.Keyboard
            "take_screenshot" -> Icons.Default.CameraAlt
            "take_snapshot" -> Icons.Default.ContentCopy
            "evaluate_script" -> Icons.Default.Bolt
            "list_console_messages", "get_console_message" -> Icons.Default.List
            "list_network_requests", "get_network_request" -> Icons.Default.NetworkCheck
            "performance_start_trace", "performance_stop_trace" -> Icons.Default.Speed
            else -> Icons.Default.Build
        }
    }

    return Icons.Default.Build
}
