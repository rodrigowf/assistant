package com.assistant.core.voicehost.notify

import com.assistant.core.voice.ports.SessionPhase
import com.assistant.core.voice.session.VoiceLinkState
import com.assistant.core.voicehost.HostTuning
import com.assistant.core.voicehost.NotificationSpec
import com.assistant.core.voicehost.VoiceUiState
import com.assistant.core.voicehost.WakeHealth

enum class NotificationAction(val label: String) {
    PAUSE_LISTENING("Pause listening"),
    RESUME_LISTENING("Resume listening"),
    TALK("Talk"),
    MUTE("Mute"),
    UNMUTE("Unmute"),
    END("End"),
    RECONNECT("Reconnect"),
}

/** What the one ongoing notification shows (id [HostTuning.NOTIFICATION_ID], updated in place). */
data class NotificationModel(
    val channelId: String,
    val title: String,
    val text: String,
    val actions: List<NotificationAction>,
    /** A voice session is live: show a chronometer, keep it silent. */
    val voice: Boolean,
)

/**
 * Pure mapping host state → notification (spec 14 §2.7; inv04 §3.5 mic-stalled text; inv03 §6
 * "add actions: stop listening, end voice"). Voice takes precedence over wake health; mic-stalled
 * takes precedence over every other wake text (the old Inc 9 behaviour).
 */
object NotificationPolicy {
    /** The idle text while the service runs only to deliver "agent session finished" notifications. */
    fun waitingText(turns: Int): String = if (turns == 1) "Waiting for 1 agent session" else "Waiting for $turns agent sessions"

    fun model(state: VoiceUiState, spec: NotificationSpec, wakePhrase: String): NotificationModel {
        val s = state.session
        val voiceLive = s.isOwner && s.phase != SessionPhase.OFF && s.phase != SessionPhase.ERROR
        val link = state.link
        if (voiceLive || link is VoiceLinkState.Reconnecting) {
            val word = when {
                link is VoiceLinkState.Reconnecting -> "Reconnecting…"
                s.phase == SessionPhase.CONNECTING || s.phase == SessionPhase.SUMMARIZING -> "Connecting…"
                s.phase == SessionPhase.SPEAKING -> "Speaking"
                s.phase == SessionPhase.THINKING -> "Thinking"
                s.phase == SessionPhase.TOOL_USE -> "Using tools"
                s.phase == SessionPhase.ENDING -> "Ending…"
                else -> "Listening"
            }
            val actions = if (spec.fullActions) {
                listOf(if (s.isMuted) NotificationAction.UNMUTE else NotificationAction.MUTE, NotificationAction.END)
            } else {
                listOf(NotificationAction.END)
            }
            return NotificationModel(spec.voiceChannelId, spec.title, "${spec.appName} · $word", actions, voice = true)
        }
        if (link is VoiceLinkState.Failed) {
            return NotificationModel(
                spec.voiceChannelId, spec.title, "${spec.appName} · Voice connection lost",
                listOf(NotificationAction.RECONNECT), voice = false,
            )
        }
        val text = when (state.wake) {
            WakeHealth.MIC_STALLED -> HostTuning.MIC_STALLED_TEXT
            WakeHealth.PAUSED_NEEDS_FOREGROUND, WakeHealth.BACKGROUND_RESTRICTED -> spec.needsForegroundText
            WakeHealth.PAUSED_BY_USER -> spec.pausedText
            WakeHealth.ARMED, WakeHealth.PAUSED_FOR_VOICE ->
                if (spec.armedText.contains("%s")) spec.armedText.format(wakePhrase) else spec.armedText
            WakeHealth.DISABLED -> if (state.agentTurnsWaiting > 0) waitingText(state.agentTurnsWaiting) else spec.idleText
        }
        val actions = if (!spec.fullActions) {
            emptyList()
        } else {
            when (state.wake) {
                WakeHealth.PAUSED_NEEDS_FOREGROUND, WakeHealth.BACKGROUND_RESTRICTED, WakeHealth.PAUSED_BY_USER ->
                    listOf(NotificationAction.RESUME_LISTENING, NotificationAction.TALK)
                WakeHealth.DISABLED -> listOf(NotificationAction.TALK)
                else -> listOf(NotificationAction.PAUSE_LISTENING, NotificationAction.TALK)
            }
        }
        return NotificationModel(spec.hostChannelId, spec.title, text, actions, voice = false)
    }
}
