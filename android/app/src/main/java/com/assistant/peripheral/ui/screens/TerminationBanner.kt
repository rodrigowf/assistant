package com.assistant.peripheral.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

/**
 * Termination banner — distinct from a generic error toast.
 *
 * Rendered when the backend signals the underlying session is gone
 * for an actionable reason (subprocess crashed, SSH transport closed,
 * etc.).  Offers a "continue in new session" affordance that opens a
 * fresh local session resuming from the on-disk JSONL.
 *
 * Mirrors the web frontend's ``.termination-banner`` styling: error
 * colour family + a clearly-bordered action button.  Headline language
 * matches the web side so users see the same wording on any device.
 *
 * @param reason       Backend ``TerminationReason`` enum value.
 * @param detail       Human-readable detail (e.g. "exit code 255"); may be null.
 * @param canRecover   True when the backend reported a [sdkSessionId]
 *                     the UI can resume from.  False hides the action button.
 * @param onRecover    Click handler for the "Continue" button.
 */
@Composable
fun TerminationBanner(
    reason: String,
    detail: String?,
    canRecover: Boolean,
    onRecover: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Surface(
        modifier = modifier.fillMaxWidth(),
        color = MaterialTheme.colorScheme.errorContainer,
        tonalElevation = 2.dp,
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 16.dp, vertical = 10.dp),
            horizontalArrangement = Arrangement.spacedBy(12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    text = terminationHeadline(reason),
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onErrorContainer,
                )
                if (!detail.isNullOrBlank()) {
                    Text(
                        text = detail,
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onErrorContainer.copy(alpha = 0.85f),
                    )
                }
            }
            if (canRecover) {
                OutlinedButton(
                    onClick = onRecover,
                    colors = ButtonDefaults.outlinedButtonColors(
                        contentColor = MaterialTheme.colorScheme.onErrorContainer,
                    ),
                ) {
                    Text("Continue")
                }
            }
        }
    }
}

/**
 * Mirrors the web ``terminationHeadline`` so the wording is consistent
 * across devices.  Keep in sync with ``frontend/src/components/ChatPanel.tsx``.
 */
private fun terminationHeadline(reason: String): String = when (reason) {
    "subprocess_crashed" -> "This session crashed"
    "subprocess_lost" -> "This session ended unexpectedly"
    "unreachable" -> "The host is unreachable"
    "replaced" -> "This session was replaced"
    "closed_by_user" -> "This session was closed"
    else -> "Session ended"
}
