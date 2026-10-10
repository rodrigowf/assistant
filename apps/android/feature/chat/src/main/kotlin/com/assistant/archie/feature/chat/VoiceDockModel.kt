package com.assistant.archie.feature.chat

import com.assistant.core.voice.ports.SessionPhase
import com.assistant.core.voice.ports.VoiceSessionState
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

/**
 * The voice controls' state and actions for one surface (IA §6): [ConversationUiMapper.voice] over
 * the voice host plus the reconnect timeline (mockup k: Reconnecting… → Reconnected for
 * [OUTCOME_MS], or Couldn't reconnect until voice runs again or the user ends it). ONE instance
 * per process in the app (next to the voice host, in the app graph), shared by the Archie
 * conversation's composer slot ([ConversationViewModel]) and the floating controls
 * ([VoiceOverlayModel]): ending from either clears the outcome on both, and the timeline survives
 * rotation. Tests and previews may give a view model its own.
 */
class VoiceDockModel(
    private val voice: ChatVoice,
    private val scope: CoroutineScope,
    private val clock: () -> Long,
) {
    private val reconnectSince = MutableStateFlow<Long?>(null)
    private val outcome = MutableStateFlow<VoiceUi?>(null)
    private var outcomeJob: Job? = null
    private var wasReconnecting = false
    private var following: Job? = null

    /** The orb's live level (measured only while a dock observes it). */
    val level: StateFlow<Float?> get() = voice.level

    val ui: Flow<VoiceUi> = combine(
        combine(voice.state, voice.speakerMuted, voice.remoteDevice, voice.remoteTranscriptMirrored, ::Inputs),
        reconnectSince,
        outcome,
    ) { v, since, out ->
        ConversationUiMapper.voice(v.state, v.speakerMuted, v.device, v.mirrored, since, out)
    }

    private data class Inputs(
        val state: VoiceSessionState,
        val speakerMuted: Boolean,
        val device: String?,
        val mirrored: Boolean,
    )

    /** Follow the voice host. Idempotent: every surface sharing this model may call it. */
    fun start() {
        if (following == null) following = scope.launch { voice.state.collect { v -> onVoiceState(v.reconnectBanner != null, v.phase) } }
    }

    /** The voice actions of [ChatAction]; false for any other action. */
    fun onAction(a: ChatAction): Boolean {
        when (a) {
            ChatAction.StartVoice -> voice.start()
            ChatAction.EndVoice -> { outcome.value = null; voice.stop() }
            ChatAction.ToggleMic -> voice.toggleMute()
            ChatAction.ToggleSpeaker -> voice.toggleSpeaker()
            ChatAction.TakeOverVoice -> voice.takeOver()
            ChatAction.ToggleRecording -> voice.toggleRecording()
            else -> return false
        }
        return true
    }

    private fun onVoiceState(reconnecting: Boolean, phase: SessionPhase) {
        if (reconnecting && !wasReconnecting) {
            outcomeJob?.cancel()
            outcome.value = null
            reconnectSince.value = clock()
        }
        if (!reconnecting && wasReconnecting) {
            val since = reconnectSince.value
            reconnectSince.value = null
            val ok = phase == SessionPhase.ACTIVE || phase == SessionPhase.SPEAKING ||
                phase == SessionPhase.THINKING || phase == SessionPhase.TOOL_USE
            if (ok) {
                val secs = if (since != null) ((clock() - since) / 1000).toInt() else 0
                outcome.value = VoiceUi.Reconnected(secs)
                outcomeJob = scope.launch { delay(OUTCOME_MS); outcome.value = null }
            } else if (phase == SessionPhase.ERROR || phase == SessionPhase.OFF) {
                outcome.value = VoiceUi.ReconnectFailed(voice.state.value.errorMessage)
            }
        }
        if (!reconnecting && outcome.value is VoiceUi.ReconnectFailed && phase != SessionPhase.ERROR && phase != SessionPhase.OFF) {
            outcome.value = null
        }
        wasReconnecting = reconnecting
    }

    companion object {
        /** How long "Reconnected" shows before the dock returns to Listening. */
        const val OUTCOME_MS = 3_000L
    }
}

/**
 * The floating voice controls' state (spec 14 §2.4; the web app's `VoiceOverlay`): the shared
 * [dock] model, independent of any conversation screen, so the controls work over agent sessions,
 * memory, visuals and settings. [scope]: the shell's.
 */
class VoiceOverlayModel(private val dock: VoiceDockModel, scope: CoroutineScope) {
    init {
        dock.start()
    }

    val ui: StateFlow<VoiceUi> = dock.ui.stateIn(scope, SharingStarted.Eagerly, VoiceUi.Off)

    val level: StateFlow<Float?> get() = dock.level

    fun onAction(a: ChatAction) {
        dock.onAction(a)
    }

    companion object {
        /** This device has a call: anything but off and "Active elsewhere" (not this device's microphone). */
        fun floats(v: VoiceUi): Boolean = v !is VoiceUi.Off && v !is VoiceUi.Elsewhere
    }
}
