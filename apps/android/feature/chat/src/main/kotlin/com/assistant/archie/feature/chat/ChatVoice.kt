package com.assistant.archie.feature.chat

import com.assistant.core.data.VoicePresence
import com.assistant.core.voice.ports.VoiceSessionState
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

/**
 * The voice controls the conversation screen drives (IA §6 voice dock, spec 12 §6.16 voice message).
 * B-09 implements it over the process-scoped voice host (A-08); until then [PresenceChatVoice] only
 * mirrors the [VoicePresence] state and the controls are no-ops. Only the Archie view uses it.
 */
interface ChatVoice {
    val state: StateFlow<VoiceSessionState>

    /**
     * Live visual level 0..1 for the dock orb (mic while listening, speaker while Archie speaks),
     * or null when nothing live is measured (the orb keeps its own pulse).
     */
    val level: StateFlow<Float?>
    val speakerMuted: StateFlow<Boolean>

    /** Name of the device that owns voice when it runs elsewhere ("Pixel 8"), if known. */
    val remoteDevice: StateFlow<String?>

    /**
     * Whether passive viewers get the transcript mirrored. True for every provider: WS providers are
     * mirrored from the relay, OpenAI (WebRTC) from the owner's `voice_event` mirror (VT-2).
     */
    val remoteTranscriptMirrored: StateFlow<Boolean>

    /** Talk-mode voice message being recorded (§6.16). */
    val recording: StateFlow<Boolean>

    fun start()
    fun stop()
    fun toggleMute()
    fun toggleSpeaker()

    /** Take voice over from another device. */
    fun takeOver()

    /** Push-to-record voice message: first call starts, second stops and sends on the orchestrator WS. */
    fun toggleRecording()
}

/** State-only voice until B-09 wires the voice host: shows what [presence] reports, does nothing. */
class PresenceChatVoice(presence: VoicePresence = VoicePresence.Idle) : ChatVoice {
    override val state: StateFlow<VoiceSessionState> = presence.state
    override val level: StateFlow<Float?> = MutableStateFlow(null)
    override val speakerMuted: StateFlow<Boolean> = MutableStateFlow(false)
    override val remoteDevice: StateFlow<String?> = MutableStateFlow(null)
    override val remoteTranscriptMirrored: StateFlow<Boolean> = MutableStateFlow(true)
    override val recording: StateFlow<Boolean> = MutableStateFlow(false)
    override fun start() = Unit
    override fun stop() = Unit
    override fun toggleMute() = Unit
    override fun toggleSpeaker() = Unit
    override fun takeOver() = Unit
    override fun toggleRecording() = Unit
}
