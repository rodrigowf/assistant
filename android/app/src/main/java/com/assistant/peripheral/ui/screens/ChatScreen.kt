package com.assistant.peripheral.ui.screens

import androidx.compose.animation.animateContentSize
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
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
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.assistant.peripheral.data.*
import com.assistant.peripheral.ui.components.VoiceButton
import com.assistant.peripheral.ui.components.VoiceControls
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatScreen(
    messages: List<ChatMessage>,
    connectionState: ConnectionState,
    sessionStatus: String,
    isRecording: Boolean,
    voiceState: VoiceState,
    isOrchestratorSession: Boolean,
    hasMoreMessages: Boolean,
    isLoadingMoreMessages: Boolean,
    onSendMessage: (String) -> Unit,
    onStartRecording: () -> Unit,
    onStopRecording: () -> Unit,
    onInterrupt: () -> Unit,
    onStartVoice: () -> Unit,
    onStopVoice: () -> Unit,
    onToggleMute: () -> Unit,
    onLoadMoreMessages: () -> Unit,
    isMuted: Boolean,
    modifier: Modifier = Modifier
) {
    var inputText by remember { mutableStateOf("") }
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

    // Voice mode active
    val isVoiceActive = voiceState != VoiceState.Off && voiceState !is VoiceState.Error

    Column(
        modifier = modifier
            .fillMaxSize()
            .padding(horizontal = 8.dp)
    ) {
        // Connection and status bar
        StatusBar(connectionState, sessionStatus, onInterrupt)

        // Voice controls overlay when active
        if (isVoiceActive) {
            VoiceControls(
                voiceState = voiceState,
                isMuted = isMuted,
                onToggleMute = onToggleMute,
                onStop = onStopVoice,
                modifier = Modifier.fillMaxWidth()
            )
        }

        // Messages list
        Box(modifier = Modifier.weight(1f)) {
            LazyColumn(
                state = listState,
                modifier = Modifier.fillMaxSize(),
                contentPadding = PaddingValues(vertical = 8.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp)
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

        // Input area with voice button (only for orchestrator)
        ChatInputBar(
            inputText = inputText,
            onInputChange = { inputText = it },
            onSend = {
                if (inputText.isNotBlank()) {
                    onSendMessage(inputText)
                    inputText = ""
                }
            },
            isRecording = isRecording,
            onStartRecording = onStartRecording,
            onStopRecording = onStopRecording,
            isConnected = connectionState is ConnectionState.Connected,
            isStreaming = sessionStatus == "streaming" || sessionStatus == "tool_use",
            voiceState = voiceState,
            onStartVoice = onStartVoice,
            onStopVoice = onStopVoice,
            isOrchestratorSession = isOrchestratorSession
        )
    }
}

@Composable
private fun StatusBar(
    connectionState: ConnectionState,
    sessionStatus: String,
    onInterrupt: () -> Unit
) {
    val isActive = sessionStatus in listOf("streaming", "tool_use", "thinking")

    Surface(
        modifier = Modifier.fillMaxWidth(),
        color = when {
            connectionState is ConnectionState.Error -> MaterialTheme.colorScheme.errorContainer
            connectionState is ConnectionState.Disconnected -> MaterialTheme.colorScheme.errorContainer.copy(alpha = 0.5f)
            connectionState is ConnectionState.Connecting -> MaterialTheme.colorScheme.tertiaryContainer
            isActive -> MaterialTheme.colorScheme.primaryContainer
            else -> Color.Transparent
        },
        tonalElevation = if (connectionState !is ConnectionState.Connected || isActive) 1.dp else 0.dp
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 16.dp, vertical = 8.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Row(
                verticalAlignment = Alignment.CenterVertically
            ) {
                // Status dot
                val dotColor = when {
                    connectionState is ConnectionState.Connected && isActive -> MaterialTheme.colorScheme.primary
                    connectionState is ConnectionState.Connected -> MaterialTheme.colorScheme.primary.copy(alpha = 0.5f)
                    connectionState is ConnectionState.Connecting -> MaterialTheme.colorScheme.tertiary
                    else -> MaterialTheme.colorScheme.error
                }

                Box(
                    modifier = Modifier
                        .size(8.dp)
                        .clip(CircleShape)
                        .background(dotColor)
                )

                Spacer(modifier = Modifier.width(8.dp))

                Text(
                    text = when {
                        connectionState is ConnectionState.Error -> "Error: ${connectionState.message}"
                        connectionState is ConnectionState.Disconnected -> "Disconnected"
                        connectionState is ConnectionState.Connecting -> "Connecting..."
                        sessionStatus == "streaming" -> "Generating..."
                        sessionStatus == "tool_use" -> "Using tools..."
                        sessionStatus == "thinking" -> "Thinking..."
                        else -> "Connected"
                    },
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.8f)
                )
            }

            // Interrupt button (only show when streaming)
            if (isActive) {
                TextButton(
                    onClick = onInterrupt,
                    colors = ButtonDefaults.textButtonColors(
                        contentColor = MaterialTheme.colorScheme.error
                    )
                ) {
                    Icon(
                        Icons.Default.Stop,
                        contentDescription = null,
                        modifier = Modifier.size(16.dp)
                    )
                    Spacer(modifier = Modifier.width(4.dp))
                    Text("Stop")
                }
            }
        }
    }
}

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

    Column(
        modifier = Modifier.fillMaxWidth(),
        horizontalAlignment = if (isUser) Alignment.End else Alignment.Start
    ) {
        // Message bubble
        Surface(
            shape = RoundedCornerShape(
                topStart = 16.dp,
                topEnd = 16.dp,
                bottomStart = if (isUser) 16.dp else 4.dp,
                bottomEnd = if (isUser) 4.dp else 16.dp
            ),
            color = when {
                isUser -> MaterialTheme.colorScheme.primaryContainer
                isSystem -> MaterialTheme.colorScheme.errorContainer.copy(alpha = 0.6f)
                else -> MaterialTheme.colorScheme.surfaceVariant
            },
            modifier = Modifier
                .widthIn(max = 320.dp)
                .animateContentSize()
        ) {
            Column(modifier = Modifier.padding(12.dp)) {
                // Render blocks
                if (message.blocks.isNotEmpty()) {
                    message.blocks.forEachIndexed { index, block ->
                        if (index > 0) Spacer(modifier = Modifier.height(8.dp))
                        MessageBlockView(block)
                    }
                } else {
                    // Fallback to content
                    Text(
                        text = message.content.ifEmpty { if (message.isStreaming) "..." else "" },
                        style = MaterialTheme.typography.bodyMedium,
                        color = when {
                            isUser -> MaterialTheme.colorScheme.onPrimaryContainer
                            isSystem -> MaterialTheme.colorScheme.onErrorContainer
                            else -> MaterialTheme.colorScheme.onSurfaceVariant
                        }
                    )
                }

                // Streaming indicator
                if (message.isStreaming) {
                    Spacer(modifier = Modifier.height(8.dp))
                    LinearProgressIndicator(
                        modifier = Modifier
                            .fillMaxWidth()
                            .height(2.dp),
                        color = MaterialTheme.colorScheme.primary
                    )
                }
            }
        }
    }
}

@Composable
private fun MessageBlockView(block: MessageBlock) {
    when (block) {
        is MessageBlock.Text -> {
            Text(
                text = block.text.ifEmpty { if (block.isStreaming) "..." else "" },
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurface
            )
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

@Composable
private fun ThinkingBlock(block: MessageBlock.Thinking) {
    var expanded by remember { mutableStateOf(false) }

    Surface(
        modifier = Modifier
            .fillMaxWidth()
            .clickable { expanded = !expanded },
        shape = RoundedCornerShape(8.dp),
        color = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.5f),
        border = androidx.compose.foundation.BorderStroke(
            1.dp,
            Color(0xFFC4923A).copy(alpha = 0.3f)
        )
    ) {
        Column(modifier = Modifier.padding(8.dp)) {
            Row(
                verticalAlignment = Alignment.CenterVertically
            ) {
                Icon(
                    imageVector = Icons.Default.Psychology,
                    contentDescription = null,
                    modifier = Modifier.size(16.dp),
                    tint = Color(0xFFC4923A)
                )
                Spacer(modifier = Modifier.width(8.dp))
                Text(
                    text = "Thinking",
                    style = MaterialTheme.typography.labelMedium,
                    color = Color(0xFFC4923A)
                )
                if (block.isStreaming) {
                    Spacer(modifier = Modifier.width(8.dp))
                    CircularProgressIndicator(
                        modifier = Modifier.size(12.dp),
                        strokeWidth = 2.dp,
                        color = Color(0xFFC4923A)
                    )
                }
                Spacer(modifier = Modifier.weight(1f))
                Icon(
                    imageVector = if (expanded) Icons.Default.ExpandLess else Icons.Default.ExpandMore,
                    contentDescription = if (expanded) "Collapse" else "Expand",
                    modifier = Modifier.size(20.dp),
                    tint = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }

            if (expanded) {
                Spacer(modifier = Modifier.height(8.dp))
                Text(
                    text = block.text,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.8f)
                )
            }
        }
    }
}

@Composable
private fun ToolUseBlock(block: MessageBlock.ToolUse) {
    var expanded by remember { mutableStateOf(false) }

    val toolColor = getToolColor(block.toolName)

    Surface(
        modifier = Modifier
            .fillMaxWidth()
            .clickable { expanded = !expanded },
        shape = RoundedCornerShape(8.dp),
        color = toolColor.copy(alpha = 0.07f),
        border = androidx.compose.foundation.BorderStroke(
            1.dp,
            toolColor.copy(alpha = 0.3f)
        )
    ) {
        Column(modifier = Modifier.padding(8.dp)) {
            Row(
                verticalAlignment = Alignment.CenterVertically
            ) {
                Icon(
                    imageVector = getToolIcon(block.toolName),
                    contentDescription = null,
                    modifier = Modifier.size(16.dp),
                    tint = toolColor
                )
                Spacer(modifier = Modifier.width(8.dp))
                Text(
                    text = block.toolName,
                    style = MaterialTheme.typography.labelMedium.copy(
                        fontFamily = FontFamily.Monospace
                    ),
                    color = toolColor,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f)
                )

                // Status indicator
                when {
                    block.isExecuting -> {
                        CircularProgressIndicator(
                            modifier = Modifier.size(14.dp),
                            strokeWidth = 2.dp,
                            color = toolColor
                        )
                    }
                    block.isComplete -> {
                        Icon(
                            imageVector = if (block.isError) Icons.Default.Error else Icons.Default.CheckCircle,
                            contentDescription = null,
                            modifier = Modifier.size(16.dp),
                            tint = if (block.isError) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.primary
                        )
                    }
                }

                Spacer(modifier = Modifier.width(4.dp))
                Icon(
                    imageVector = if (expanded) Icons.Default.ExpandLess else Icons.Default.ExpandMore,
                    contentDescription = if (expanded) "Collapse" else "Expand",
                    modifier = Modifier.size(20.dp),
                    tint = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }

            if (expanded && block.result != null) {
                Spacer(modifier = Modifier.height(8.dp))
                Surface(
                    shape = RoundedCornerShape(4.dp),
                    color = MaterialTheme.colorScheme.surface
                ) {
                    Text(
                        text = block.result.take(500) + if (block.result.length > 500) "..." else "",
                        style = MaterialTheme.typography.bodySmall.copy(
                            fontFamily = FontFamily.Monospace,
                            fontSize = 11.sp
                        ),
                        color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.8f),
                        modifier = Modifier.padding(8.dp)
                    )
                }
            }
        }
    }
}

@Composable
private fun CompactDivider(summary: String) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 16.dp)
    ) {
        Divider(color = MaterialTheme.colorScheme.outlineVariant)
        Spacer(modifier = Modifier.height(8.dp))
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.Center
        ) {
            Icon(
                imageVector = Icons.Default.Compress,
                contentDescription = null,
                modifier = Modifier.size(16.dp),
                tint = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.5f)
            )
            Spacer(modifier = Modifier.width(8.dp))
            Text(
                text = "Conversation compacted",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.5f)
            )
        }
        Spacer(modifier = Modifier.height(8.dp))
        Divider(color = MaterialTheme.colorScheme.outlineVariant)
    }
}

@Composable
private fun ChatInputBar(
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
    val isVoiceActive = voiceState != VoiceState.Off && voiceState !is VoiceState.Error

    Surface(
        modifier = Modifier.fillMaxWidth(),
        tonalElevation = 2.dp
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(8.dp),
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

            // Text input (hide when voice is active)
            if (!isVoiceActive) {
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
            } else {
                // When voice is active, show status text
                Column(
                    modifier = Modifier.weight(1f),
                    horizontalAlignment = Alignment.CenterHorizontally
                ) {
                    Text(
                        text = when (voiceState) {
                            is VoiceState.Connecting -> "Connecting voice..."
                            is VoiceState.Active -> "Voice connected - speak naturally"
                            is VoiceState.Listening -> "Listening..."
                            is VoiceState.Speaking -> "Speaking..."
                            is VoiceState.Thinking -> "Thinking..."
                            is VoiceState.ToolUse -> "Using tools..."
                            else -> ""
                        },
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
            }
        }
    }
}

// Tool color mapping (matches web frontend)
private fun getToolColor(toolName: String): Color {
    return when {
        toolName.contains("Read", ignoreCase = true) ||
        toolName.contains("Glob", ignoreCase = true) ||
        toolName.contains("Grep", ignoreCase = true) -> Color(0xFF5888CC) // tool-read

        toolName.contains("Write", ignoreCase = true) ||
        toolName.contains("Edit", ignoreCase = true) -> Color(0xFF4AAA7A) // tool-write

        toolName.contains("Bash", ignoreCase = true) ||
        toolName.contains("Execute", ignoreCase = true) -> Color(0xFFD4A04A) // tool-execute

        toolName.contains("Task", ignoreCase = true) -> Color(0xFF8B7ACC) // tool-task

        toolName.contains("WebFetch", ignoreCase = true) ||
        toolName.contains("WebSearch", ignoreCase = true) -> Color(0xFF9B7ACC) // tool-search

        else -> Color(0xFF7888AA) // tool-system
    }
}

private fun getToolIcon(toolName: String): androidx.compose.ui.graphics.vector.ImageVector {
    return when {
        toolName.contains("Read", ignoreCase = true) -> Icons.Default.Description
        toolName.contains("Write", ignoreCase = true) ||
        toolName.contains("Edit", ignoreCase = true) -> Icons.Default.Edit
        toolName.contains("Bash", ignoreCase = true) -> Icons.Default.Terminal
        toolName.contains("Task", ignoreCase = true) -> Icons.Default.Assignment
        toolName.contains("WebFetch", ignoreCase = true) ||
        toolName.contains("WebSearch", ignoreCase = true) -> Icons.Default.Search
        toolName.contains("Glob", ignoreCase = true) ||
        toolName.contains("Grep", ignoreCase = true) -> Icons.Default.FindInPage
        else -> Icons.Default.Build
    }
}
