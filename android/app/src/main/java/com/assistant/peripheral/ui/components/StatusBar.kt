package com.assistant.peripheral.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
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
 *
 * Fixed-height, transparent-background, no growth on activity changes
 * (only the dot + label change). The Stop affordance lives on the
 * chat input bar (replacing Send while streaming), so this bar no
 * longer trails an action button.
 */
@Composable
fun StatusBar(
    connectionState: ConnectionState,
    sessionStatus: String,
    modifier: Modifier = Modifier
) {
    val isActive = sessionStatus in listOf("streaming", "tool_use", "thinking", "processing")

    Surface(
        modifier = modifier.fillMaxWidth(),
        color = Color.Transparent,
        tonalElevation = 0.dp
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 16.dp, vertical = 6.dp),
            horizontalArrangement = Arrangement.Start,
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
                    sessionStatus == "processing" -> "Processing..."
                    else -> "Ready"
                },
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.8f)
            )
        }
    }
}
