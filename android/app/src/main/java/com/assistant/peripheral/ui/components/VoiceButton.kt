package com.assistant.peripheral.ui.components

import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.*
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.focusable
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.material.ripple.rememberRipple
import androidx.compose.runtime.remember
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.getValue
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import com.assistant.peripheral.data.VoiceState

/**
 * Voice button that shows the current voice state.
 * Matches the web frontend's VoiceButton and VoiceControls components.
 */
@Composable
fun VoiceButton(
    voiceState: VoiceState,
    onStart: () -> Unit,
    onStop: () -> Unit,
    modifier: Modifier = Modifier
) {
    val isActive = voiceState != VoiceState.Off && voiceState !is VoiceState.Error

    // Pulse animation for active states
    val infiniteTransition = rememberInfiniteTransition(label = "voice_pulse")
    val pulseScale by infiniteTransition.animateFloat(
        initialValue = 1f,
        targetValue = 1.15f,
        animationSpec = infiniteRepeatable(
            animation = tween(800, easing = EaseInOut),
            repeatMode = RepeatMode.Reverse
        ),
        label = "scale"
    )

    val backgroundColor by animateColorAsState(
        targetValue = when (voiceState) {
            is VoiceState.Off -> MaterialTheme.colorScheme.primaryContainer
            is VoiceState.Connecting -> MaterialTheme.colorScheme.tertiaryContainer
            is VoiceState.Active -> Color(0xFF4AAA7A).copy(alpha = 0.2f) // success
            is VoiceState.Speaking -> Color(0xFF5888CC).copy(alpha = 0.2f) // blue
            is VoiceState.Listening -> Color(0xFF4AAA7A).copy(alpha = 0.3f) // success
            is VoiceState.Thinking -> Color(0xFFC4923A).copy(alpha = 0.2f) // thinking
            is VoiceState.ToolUse -> Color(0xFF8B7ACC).copy(alpha = 0.2f) // purple
            is VoiceState.Error -> MaterialTheme.colorScheme.errorContainer
        },
        label = "backgroundColor"
    )

    val iconTint by animateColorAsState(
        targetValue = when (voiceState) {
            is VoiceState.Off -> MaterialTheme.colorScheme.onPrimaryContainer
            is VoiceState.Connecting -> MaterialTheme.colorScheme.onTertiaryContainer
            is VoiceState.Active, is VoiceState.Listening -> Color(0xFF4AAA7A)
            is VoiceState.Speaking -> Color(0xFF5888CC)
            is VoiceState.Thinking -> Color(0xFFC4923A)
            is VoiceState.ToolUse -> Color(0xFF8B7ACC)
            is VoiceState.Error -> MaterialTheme.colorScheme.error
        },
        label = "iconTint"
    )

    val borderColor by animateColorAsState(
        targetValue = when (voiceState) {
            is VoiceState.Listening -> Color(0xFF4AAA7A).copy(alpha = 0.5f)
            is VoiceState.Speaking -> Color(0xFF5888CC).copy(alpha = 0.5f)
            else -> Color.Transparent
        },
        label = "borderColor"
    )

    val scale = if (voiceState == VoiceState.Listening || voiceState == VoiceState.Speaking) {
        pulseScale
    } else {
        1f
    }

    // Focus state for Fire TV D-pad navigation
    var isFocused by remember { mutableStateOf(false) }
    val interactionSource = remember { MutableInteractionSource() }

    // Focus indicator border
    val focusBorderColor = if (isFocused) {
        MaterialTheme.colorScheme.primary
    } else {
        borderColor
    }

    val focusBorderWidth = if (isFocused) 3.dp else 2.dp

    Box(
        modifier = modifier
            .size(64.dp)
            .scale(scale)
            .clip(CircleShape)
            .background(backgroundColor)
            .border(focusBorderWidth, focusBorderColor, CircleShape)
            .focusable()
            .onFocusChanged { focusState ->
                isFocused = focusState.isFocused
            }
            .clickable(
                interactionSource = interactionSource,
                indication = rememberRipple(bounded = false, radius = 32.dp)
            ) {
                if (isActive) onStop() else onStart()
            },
        contentAlignment = Alignment.Center
    ) {
        when (voiceState) {
            is VoiceState.Connecting -> {
                CircularProgressIndicator(
                    modifier = Modifier.size(24.dp),
                    strokeWidth = 2.dp,
                    color = iconTint
                )
            }
            else -> {
                Icon(
                    imageVector = when (voiceState) {
                        is VoiceState.Off -> Icons.Default.Mic
                        is VoiceState.Listening -> Icons.Default.Mic
                        is VoiceState.Speaking -> Icons.Default.VolumeUp
                        is VoiceState.Thinking -> Icons.Default.Psychology
                        is VoiceState.ToolUse -> Icons.Default.Build
                        is VoiceState.Error -> Icons.Default.MicOff
                        else -> Icons.Default.Mic
                    },
                    contentDescription = when (voiceState) {
                        is VoiceState.Off -> "Start voice"
                        is VoiceState.Listening -> "Listening..."
                        is VoiceState.Speaking -> "Speaking..."
                        is VoiceState.Thinking -> "Thinking..."
                        is VoiceState.ToolUse -> "Using tools..."
                        is VoiceState.Error -> "Voice error"
                        else -> "Voice"
                    },
                    tint = iconTint,
                    modifier = Modifier.size(28.dp)
                )
            }
        }
    }
}

/**
 * Voice controls panel shown when voice is active.
 */
@Composable
fun VoiceControls(
    voiceState: VoiceState,
    isMuted: Boolean,
    onToggleMute: () -> Unit,
    onStop: () -> Unit,
    modifier: Modifier = Modifier
) {
    val isActive = voiceState != VoiceState.Off && voiceState !is VoiceState.Error

    if (!isActive) return

    Row(
        modifier = modifier
            .fillMaxWidth()
            .padding(16.dp),
        horizontalArrangement = Arrangement.SpaceEvenly,
        verticalAlignment = Alignment.CenterVertically
    ) {
        // Status label
        Column(
            horizontalAlignment = Alignment.CenterHorizontally,
            modifier = Modifier.weight(1f)
        ) {
            VoiceStatusIndicator(voiceState)
            Spacer(modifier = Modifier.height(4.dp))
            Text(
                text = when (voiceState) {
                    is VoiceState.Connecting -> "Connecting..."
                    is VoiceState.Active -> "Connected"
                    is VoiceState.Listening -> "Listening..."
                    is VoiceState.Speaking -> "Speaking..."
                    is VoiceState.Thinking -> "Thinking..."
                    is VoiceState.ToolUse -> "Using tools..."
                    else -> ""
                },
                style = MaterialTheme.typography.labelMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }

        // Mute button
        IconButton(
            onClick = onToggleMute,
            modifier = Modifier
                .size(48.dp)
                .clip(CircleShape)
                .background(
                    if (isMuted) MaterialTheme.colorScheme.errorContainer
                    else MaterialTheme.colorScheme.surfaceVariant
                )
        ) {
            Icon(
                imageVector = if (isMuted) Icons.Default.MicOff else Icons.Default.Mic,
                contentDescription = if (isMuted) "Unmute" else "Mute",
                tint = if (isMuted) MaterialTheme.colorScheme.error
                       else MaterialTheme.colorScheme.onSurfaceVariant
            )
        }

        Spacer(modifier = Modifier.width(16.dp))

        // End call button
        IconButton(
            onClick = onStop,
            modifier = Modifier
                .size(48.dp)
                .clip(CircleShape)
                .background(MaterialTheme.colorScheme.error)
        ) {
            Icon(
                imageVector = Icons.Default.CallEnd,
                contentDescription = "End voice session",
                tint = MaterialTheme.colorScheme.onError
            )
        }
    }
}

@Composable
private fun VoiceStatusIndicator(state: VoiceState) {
    val color = when (state) {
        is VoiceState.Connecting -> MaterialTheme.colorScheme.tertiary
        is VoiceState.Active -> Color(0xFF4AAA7A)
        is VoiceState.Listening -> Color(0xFF4AAA7A)
        is VoiceState.Speaking -> Color(0xFF5888CC)
        is VoiceState.Thinking -> Color(0xFFC4923A)
        is VoiceState.ToolUse -> Color(0xFF8B7ACC)
        else -> MaterialTheme.colorScheme.onSurfaceVariant
    }

    val infiniteTransition = rememberInfiniteTransition(label = "status_pulse")
    val alpha by infiniteTransition.animateFloat(
        initialValue = 1f,
        targetValue = 0.3f,
        animationSpec = infiniteRepeatable(
            animation = tween(600),
            repeatMode = RepeatMode.Reverse
        ),
        label = "alpha"
    )

    val isPulsing = state in listOf(
        VoiceState.Connecting,
        VoiceState.Listening,
        VoiceState.Speaking,
        VoiceState.Thinking
    )

    Box(
        modifier = Modifier
            .size(12.dp)
            .clip(CircleShape)
            .background(color.copy(alpha = if (isPulsing) alpha else 1f))
    )
}
