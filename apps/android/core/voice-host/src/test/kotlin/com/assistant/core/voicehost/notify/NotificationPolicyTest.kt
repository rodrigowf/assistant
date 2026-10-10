package com.assistant.core.voicehost.notify

import com.assistant.core.voice.ports.SessionPhase
import com.assistant.core.voice.ports.VoiceSessionState
import com.assistant.core.voice.session.VoiceLinkState
import com.assistant.core.voicehost.HostTuning
import com.assistant.core.voicehost.NotificationSpec
import com.assistant.core.voicehost.VoiceUiState
import com.assistant.core.voicehost.WakeHealth
import com.assistant.core.voicehost.notify.NotificationAction.END
import com.assistant.core.voicehost.notify.NotificationAction.MUTE
import com.assistant.core.voicehost.notify.NotificationAction.PAUSE_LISTENING
import com.assistant.core.voicehost.notify.NotificationAction.RECONNECT
import com.assistant.core.voicehost.notify.NotificationAction.RESUME_LISTENING
import com.assistant.core.voicehost.notify.NotificationAction.TALK
import com.assistant.core.voicehost.notify.NotificationAction.UNMUTE
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** Notification content (spec 14 §2.7; inv04 §3.5 mic-stalled; inv03 §1.8 lite texts). */
class NotificationPolicyTest {
    private val lite = NotificationSpec.lite(0)
    private val main = NotificationSpec.main(0)

    private fun model(s: VoiceUiState, spec: NotificationSpec = main) = NotificationPolicy.model(s, spec, "wake up")

    /** The lite app keeps the old service's channel, title and text (the A300M's behaviour). */
    @Test
    fun liteKeepsTheOldNotification() {
        val m = model(VoiceUiState(wake = WakeHealth.ARMED), lite)
        assertEquals(HostTuning.NOTIFICATION_CHANNEL_ID, m.channelId)
        assertEquals("Assistant Active", m.title)
        assertEquals("Listening for commands", m.text)
        assertTrue(m.actions.isEmpty())
    }

    /** F18 / Inc 9: mic held by another app → the stalled text, on both apps, until the mic is back. */
    @Test
    fun micStalledShowsTheStalledText() {
        for (spec in listOf(lite, main)) {
            assertEquals(HostTuning.MIC_STALLED_TEXT, model(VoiceUiState(wake = WakeHealth.MIC_STALLED), spec).text)
            assertEquals("Wake word stalled — mic held by another app", model(VoiceUiState(wake = WakeHealth.MIC_STALLED), spec).text)
        }
        assertEquals("Listening for commands", model(VoiceUiState(wake = WakeHealth.ARMED), lite).text)
    }

    @Test
    fun mainHostStates() {
        val armed = model(VoiceUiState(wake = WakeHealth.ARMED))
        assertEquals("host", armed.channelId)
        assertEquals("Listening for \"wake up\"", armed.text)
        assertEquals(listOf(PAUSE_LISTENING, TALK), armed.actions)
        val paused = model(VoiceUiState(wake = WakeHealth.PAUSED_BY_USER))
        assertEquals("Wake word paused", paused.text)
        assertEquals(listOf(RESUME_LISTENING, TALK), paused.actions)
        val degraded = model(VoiceUiState(wake = WakeHealth.PAUSED_NEEDS_FOREGROUND))
        assertEquals("Tap to resume listening", degraded.text)
        assertEquals(listOf(RESUME_LISTENING, TALK), degraded.actions)
        assertEquals(listOf(TALK), model(VoiceUiState(wake = WakeHealth.DISABLED)).actions)
    }

    @Test
    fun voiceTakesOverTheNotification() {
        val live = VoiceUiState(session = VoiceSessionState(phase = SessionPhase.SPEAKING, isOwner = true), wake = WakeHealth.PAUSED_FOR_VOICE)
        val m = model(live)
        assertEquals("voice", m.channelId)
        assertEquals("Archie · Speaking", m.text)
        assertEquals(listOf(MUTE, END), m.actions)
        assertTrue(m.voice)
        val muted = model(live.copy(session = live.session.copy(isMuted = true)))
        assertEquals(listOf(UNMUTE, END), muted.actions)
        assertEquals("Archie · Using tools", model(live.copy(session = live.session.copy(phase = SessionPhase.TOOL_USE))).text)
        assertEquals("lite: End only", listOf(END), model(live, lite).actions)
    }

    /** Another device's voice is not ours: the notification stays a host notification. */
    @Test
    fun remoteVoiceDoesNotShowAsOurs() {
        val remote = VoiceUiState(session = VoiceSessionState(phase = SessionPhase.ACTIVE, isOwner = false, remoteVoiceActive = true), wake = WakeHealth.ARMED)
        assertEquals("host", model(remote).channelId)
    }

    @Test
    fun p2_reconnectingAndFailedStates() {
        val reconnecting = VoiceUiState(
            session = VoiceSessionState(phase = SessionPhase.ACTIVE, isOwner = true),
            link = VoiceLinkState.Reconnecting(0, 3_000, 30_000, 1, false),
        )
        assertEquals("Archie · Reconnecting…", model(reconnecting).text)
        val failed = model(VoiceUiState(session = VoiceSessionState(phase = SessionPhase.ERROR), link = VoiceLinkState.Failed(30_000)))
        assertEquals("Archie · Voice connection lost", failed.text)
        assertEquals(listOf(RECONNECT), failed.actions)
    }

    /** Main app: the service held only for agent notifications says what it waits for. */
    @Test
    fun agentWorkHoldShowsWhatItWaitsFor() {
        assertEquals("Waiting for 1 agent session", model(VoiceUiState(agentTurnsWaiting = 1)).text)
        assertEquals("Waiting for 3 agent sessions", model(VoiceUiState(agentTurnsWaiting = 3)).text)
        assertEquals("Connected", model(VoiceUiState()).text)
        assertEquals("armed wake word wins", "Listening for \"wake up\"", model(VoiceUiState(wake = WakeHealth.ARMED, agentTurnsWaiting = 1)).text)
    }
}
