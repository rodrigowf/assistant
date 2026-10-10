package com.assistant.archie.feature.chat.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.isShiftPressed
import androidx.compose.ui.input.key.key
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.input.key.type
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import com.assistant.archie.feature.chat.ChatAction
import com.assistant.archie.feature.chat.ComposerUi
import com.assistant.archie.feature.chat.CountersUi
import com.assistant.archie.feature.chat.VoiceUi
import com.assistant.core.conversation.QueueOwner
import com.assistant.core.conversation.QueuedPrompt
import com.assistant.core.design.Corner
import com.assistant.core.design.components.ArchieButton
import com.assistant.core.design.components.ArchieDialogSurface
import com.assistant.core.design.components.ArchieDropdownMenu
import com.assistant.core.design.components.ArchieIconButton
import com.assistant.core.design.components.ArchieMenuItem
import com.assistant.core.design.components.ArchieMenuSeparator
import com.assistant.core.design.components.ButtonStyle
import com.assistant.core.design.components.ComposerShell
import com.assistant.core.design.components.ComposerTextField
import com.assistant.core.design.components.ContextRing
import com.assistant.core.design.components.IconButtonStyle
import com.assistant.core.design.components.OPEN_CONVERSATION_LABEL
import com.assistant.core.design.components.OrbTone
import com.assistant.core.design.components.ReconnectOutcome
import com.assistant.core.design.components.VoiceDock
import com.assistant.core.design.components.VoiceDockControls
import com.assistant.core.design.components.VoiceDockElsewhere
import com.assistant.core.design.components.VoiceDockOutcome
import com.assistant.core.design.components.VoiceDockReconnecting
import com.assistant.core.design.components.VoiceDockState
import com.assistant.core.design.components.VoiceOrb
import com.assistant.core.design.icons.ArchieIcons
import com.assistant.core.design.theme.ArchieTheme
import com.assistant.core.voice.ports.SessionPhase
import kotlinx.collections.immutable.ImmutableList
import kotlinx.coroutines.delay
import java.util.Locale

/** Slash commands offered on agent sessions (`command` frame). */
val SlashCommands = listOf("/help", "/cost", "/context", "/model", "/status")

/**
 * The composer, or the voice dock that replaces it while voice is on (IA §6). The primary button
 * morphs Voice → Send → Stop; while the agent works, Send queues (I-12). [onAttach] opens the
 * file picker (Archie: upload + inject, §6.15).
 */
@Composable
fun ComposerArea(
    composer: ComposerUi,
    voice: VoiceUi,
    counters: CountersUi,
    queue: ImmutableList<QueuedPrompt>,
    draft: String,
    onAction: (ChatAction) -> Unit,
    onAttach: () -> Unit,
    modifier: Modifier = Modifier,
    clock: () -> Long = System::currentTimeMillis,
    voiceLevel: (() -> Float?)? = null,
) {
    Column(modifier, verticalArrangement = Arrangement.spacedBy(10.dp)) {
        if (queue.isNotEmpty()) QueueTray(queue)
        when (voice) {
            is VoiceUi.Active, is VoiceUi.Connecting, is VoiceUi.Reconnecting, is VoiceUi.Reconnected ->
                VoiceControls(voice, onAction, clock = clock, voiceLevel = voiceLevel)
            else -> {
                if (voice is VoiceUi.Elsewhere) {
                    VoiceDockElsewhere(
                        voice.device,
                        onTakeOver = { onAction(ChatAction.TakeOverVoice) },
                        hint = if (voice.mirrored) "Transcripts mirror here" else "Live transcript is only on the device that started voice",
                    )
                }
                if (voice is VoiceUi.ReconnectFailed) VoiceControls(voice, onAction, clock = clock)
                Composer(composer, counters, draft, onAction, onAttach)
            }
        }
    }
}

/**
 * This device's voice controls for [voice]: the dock (Active), Connecting / Preparing / Ending,
 * Reconnecting… with its timer, and the two reconnect outcomes. Shared by the composer slot
 * ([ComposerArea]) and the floating controls ([VoiceOverlay]), so mute, speaker, End and
 * Reconnect act the same in both. [onOpenConversation] (floating only) makes the state text open
 * the Archie conversation. Off and "Active elsewhere" render nothing.
 */
@Composable
fun VoiceControls(
    voice: VoiceUi,
    onAction: (ChatAction) -> Unit,
    modifier: Modifier = Modifier,
    clock: () -> Long = System::currentTimeMillis,
    voiceLevel: (() -> Float?)? = null,
    onOpenConversation: (() -> Unit)? = null,
) {
    when (voice) {
        is VoiceUi.Active -> VoiceDock(
            state = dockState(voice.phase),
            hint = dockHint(voice.phase, voice.micMuted),
            modifier = modifier.testTag("voice-dock"),
            level = voiceLevel,
            onLabelClick = onOpenConversation,
        ) {
            VoiceDockControls(
                micMuted = voice.micMuted,
                onToggleMic = { onAction(ChatAction.ToggleMic) },
                speakerMuted = voice.speakerMuted,
                onToggleSpeaker = { onAction(ChatAction.ToggleSpeaker) },
                onEnd = { onAction(ChatAction.EndVoice) },
            )
        }
        is VoiceUi.Connecting -> ConnectingDock(voice.label, modifier, onOpenConversation) { onAction(ChatAction.EndVoice) }
        is VoiceUi.Reconnecting -> {
            var now by remember { mutableLongStateOf(clock()) }
            LaunchedEffect(voice.since) { while (true) { now = clock(); delay(1_000) } }
            VoiceDockReconnecting(elapsed = mmss(((now - voice.since) / 1000).coerceAtLeast(0)), onEnd = { onAction(ChatAction.EndVoice) }, modifier = modifier)
        }
        is VoiceUi.Reconnected -> VoiceDockOutcome(
            ReconnectOutcome.Reconnected,
            "Reconnected",
            "Back after ${mmss(voice.afterSeconds.toLong())} · dock returns to Listening",
            modifier,
        )
        is VoiceUi.ReconnectFailed -> VoiceDockOutcome(
            ReconnectOutcome.Failed,
            "Couldn't reconnect",
            voice.message ?: "Retries ran out",
            modifier,
            onReconnect = { onAction(ChatAction.StartVoice) },
            onEnd = { onAction(ChatAction.EndVoice) },
        )
        VoiceUi.Off, is VoiceUi.Elsewhere -> Unit
    }
}

@Composable
private fun Composer(
    composer: ComposerUi,
    counters: CountersUi,
    draft: String,
    onAction: (ChatAction) -> Unit,
    onAttach: () -> Unit,
) {
    var focused by remember { mutableStateOf(false) }
    var menu by remember { mutableStateOf(false) }
    var contextDialog by remember { mutableStateOf(false) }
    val send = { onAction(ChatAction.Send) }
    ComposerShell(
        primary = composer.primary,
        onPrimary = { onAction(ChatAction.Primary) },
        modifier = Modifier.testTag("composer"),
        focused = focused,
        leading = {
            Box {
                ArchieIconButton(ArchieIcons.Add, "Attach, voice message or slash command", { menu = true }, enabled = composer.enabled)
                ArchieDropdownMenu(menu, { menu = false }) {
                    if (composer.canAttach) {
                        ArchieMenuItem("Attach file", { menu = false; onAttach() }, icon = ArchieIcons.AttachFile)
                    }
                    if (composer.canRecord) {
                        ArchieMenuItem(
                            if (composer.recording) "Stop and send voice message" else "Record voice message",
                            { menu = false; onAction(ChatAction.ToggleRecording) },
                            icon = ArchieIcons.Mic,
                            enabled = composer.recordDisabledReason == null || composer.recording,
                        )
                    }
                    if (composer.slashCommands) {
                        SlashCommands.forEach { cmd ->
                            ArchieMenuItem(cmd, { menu = false; onAction(ChatAction.DraftChanged("$cmd ")) }, icon = ArchieIcons.Terminal)
                        }
                    }
                    ArchieMenuSeparator()
                    ArchieMenuItem("Compact context", { menu = false; onAction(ChatAction.Compact) }, icon = ArchieIcons.Compress, enabled = composer.canCompact)
                }
            }
        },
        trailing = {
            composer.context?.let { f -> ContextRing(f, { contextDialog = true }) }
            if (composer.canRecord) {
                ArchieIconButton(
                    if (composer.recording) ArchieIcons.StopFilled else ArchieIcons.Mic,
                    when {
                        composer.recording -> "Stop and send voice message"
                        composer.recordDisabledReason != null -> "Record voice message (${composer.recordDisabledReason})"
                        else -> "Record voice message"
                    },
                    { onAction(ChatAction.ToggleRecording) },
                    style = if (composer.recording) IconButtonStyle.Error else IconButtonStyle.Standard,
                    enabled = composer.recording || composer.recordDisabledReason == null,
                )
            }
        },
    ) {
        ComposerTextField(
            value = draft,
            onValueChange = { onAction(ChatAction.DraftChanged(it)) },
            placeholder = composer.disabledReason ?: composer.placeholder,
            modifier = Modifier
                .testTag("composer-field")
                .onFocusChanged { focused = it.isFocused }
                .onPreviewKeyEvent { e ->
                    // Hardware Enter sends (queues while working); Shift+Enter is a newline.
                    if (e.type == KeyEventType.KeyDown && e.key == Key.Enter && !e.isShiftPressed) { send(); true } else false
                },
            onSend = send,
            enabled = composer.enabled,
        )
    }
    if (contextDialog) {
        ContextDialog(counters, composer.canCompact, onCompact = { contextDialog = false; onAction(ChatAction.Compact) }) { contextDialog = false }
    }
}

/** Tap on the context ring: usage, cost and turns (inv03 §5 parity), and Compact now. */
@Composable
private fun ContextDialog(counters: CountersUi, canCompact: Boolean, onCompact: () -> Unit, onDismiss: () -> Unit) {
    Dialog(onDismissRequest = onDismiss) {
        ArchieDialogSurface(
            "Context",
            body = {
                val pct = counters.percent?.let { "$it%" } ?: "?"
                val tokens = if (counters.contextTokens != null && counters.contextWindow != null) {
                    " · ${counters.contextTokens / 1000}k of ${counters.contextWindow / 1000}k tokens"
                } else {
                    ""
                }
                Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text("$pct used$tokens")
                    Text("Cost ${String.format(Locale.US, "$%.2f", counters.cost)} · ${counters.turns} turns")
                    Text("Compacting summarizes older turns to free up context.")
                }
            },
        ) {
            ArchieButton("Close", onDismiss, style = ButtonStyle.Text)
            ArchieButton("Compact now", onCompact, style = ButtonStyle.Text, enabled = canCompact)
        }
    }
}

/** Prompts the server queued behind the running turn (I-12); they enter the timeline when dispatched. */
@Composable
private fun QueueTray(queue: ImmutableList<QueuedPrompt>) {
    val c = ArchieTheme.colors
    Column(
        Modifier
            .fillMaxWidth()
            .testTag("queue-tray")
            .background(c.surfaceContainer, RoundedCornerShape(Corner.Large))
            .padding(horizontal = 12.dp, vertical = 8.dp),
        verticalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        queue.forEach { q ->
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                Box(
                    Modifier
                        .background(c.secondaryContainer, RoundedCornerShape(6.dp))
                        .padding(horizontal = 6.dp, vertical = 2.dp),
                ) {
                    Text(
                        if (q.owner == QueueOwner.LOCAL) "Queued" else "Queued elsewhere",
                        style = ArchieTheme.typography.labelSmall,
                        color = c.onSecondaryContainer,
                    )
                }
                Text(
                    q.text,
                    Modifier.weight(1f),
                    style = ArchieTheme.typography.bodyMedium,
                    color = c.onSurfaceVariant,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                )
            }
        }
    }
}

/** Connecting / preparing / ending: the dock's frame with a still orb and End. */
@Composable
private fun ConnectingDock(label: String, modifier: Modifier, onOpen: (() -> Unit)?, onEnd: () -> Unit) {
    val c = ArchieTheme.colors
    Row(
        modifier
            .fillMaxWidth()
            .heightIn(min = 84.dp)
            .testTag("voice-dock")
            .background(c.surfaceContainerHigh, RoundedCornerShape(32.dp))
            .padding(start = 12.dp, end = 10.dp, top = 10.dp, bottom = 10.dp),
        horizontalArrangement = Arrangement.spacedBy(6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        VoiceOrb(OrbTone.Idle)
        val open = if (onOpen != null) {
            Modifier.clip(RoundedCornerShape(16.dp)).clickable(onClickLabel = OPEN_CONVERSATION_LABEL, role = Role.Button, onClick = onOpen)
        } else {
            Modifier
        }
        Column(Modifier.weight(1f).then(open).padding(start = 6.dp)) {
            Text(label, style = ArchieTheme.typography.titleMedium, color = c.onSurface)
            Text("Voice starts in a moment", style = ArchieTheme.typography.bodySmall, color = c.onSurfaceVariant)
        }
        ArchieIconButton(ArchieIcons.CallEndFilled, "End voice", onEnd, style = IconButtonStyle.Error)
    }
}

internal fun dockState(p: SessionPhase): VoiceDockState = when (p) {
    SessionPhase.SPEAKING -> VoiceDockState.Speaking
    SessionPhase.THINKING -> VoiceDockState.Thinking
    SessionPhase.TOOL_USE -> VoiceDockState.UsingTools
    else -> VoiceDockState.Listening
}

private fun dockHint(p: SessionPhase, muted: Boolean): String = when {
    muted -> "Microphone muted"
    p == SessionPhase.SPEAKING -> "Speak to interrupt"
    p == SessionPhase.THINKING -> "Working on it"
    p == SessionPhase.TOOL_USE -> "Running a tool"
    else -> "Speak any time"
}

internal fun mmss(seconds: Long): String = "${seconds / 60}:${(seconds % 60).toString().padStart(2, '0')}"
