package com.assistant.peripheral.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Stop
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import com.assistant.peripheral.data.ConnectionState

/**
 * Thin connection / session-status bar.
 * Sits globally above the chat input + voice controls.
 */
@Composable
fun StatusBar(
    connectionState: ConnectionState,
    sessionStatus: String,
    onInterrupt: () -> Unit,
    modifier: Modifier = Modifier
) {
    val isActive = sessionStatus in listOf("streaming", "tool_use", "thinking")

    Surface(
        modifier = modifier.fillMaxWidth(),
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
