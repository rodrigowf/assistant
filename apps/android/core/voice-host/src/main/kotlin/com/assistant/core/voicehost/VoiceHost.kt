package com.assistant.core.voicehost

import android.app.Activity
import com.assistant.core.audio.ports.OutputChoice
import com.assistant.core.model.AudioOutput
import com.assistant.core.model.DeviceSettings
import com.assistant.core.voice.ports.VoiceLevels
import com.assistant.core.voice.ports.VoiceSessionState
import com.assistant.core.voice.session.VoiceLinkState
import com.assistant.core.voicehost.cue.CueKind
import com.assistant.core.voicehost.ports.TalkUiState
import com.assistant.core.voicehost.ports.Trigger
import com.assistant.core.voicehost.ports.WakeServiceConfig
import com.assistant.core.wakeword.ports.WakePhase
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.StateFlow

/**
 * The contract both apps program against (spec 14 §2.5). UIs never own voice; they observe
 * [state] and send commands. Main-app ViewModels get it from the graph; system-started
 * components (VIS session, tile, lite Activity) bind `VoiceHostService.LocalBinder`.
 */
interface VoiceHost {
    val state: StateFlow<VoiceUiState>
    val events: Flow<VoiceUiEvent>

    /**
     * Live mic/speaker levels of this device's voice session while subscribed (~15 Hz), null
     * without one. For the dock's level orb only; observing it changes nothing in the session.
     */
    val levels: StateFlow<VoiceLevels?> get() = VoiceLevels.None

    /** Connect the orchestrator channel to the configured server (idempotent). */
    fun connect()

    /** Lifecycle disconnect: sends nothing to the backend (P-1). */
    fun disconnect()

    /** Every trigger enters here ([TriggerIngress][com.assistant.core.voicehost.trigger.TriggerIngress]). */
    fun startVoice(trigger: Trigger)
    fun stopVoice()

    /** P-2 manual "Reconnect" of the failure state. */
    fun reconnectVoice()
    fun toggleMute()
    fun toggleSpeakerMute()

    /** Main app: push-to-talk voice message. Pauses the wake word first (fixes inv04 B7/B8). */
    fun startPushToTalk()
    fun stopPushToTalk(send: Boolean)

    /** "Pause listening" / "Resume listening" (notification actions, settings). */
    fun pauseListening()
    fun resumeListening()

    /** Single settings ingress (inv04 RS-33): gains are always explicit, never defaulted. */
    fun updateSettings(s: VoiceHostSettings)
}

/** The host-relevant slice of [DeviceSettings]. */
data class VoiceHostSettings(
    val serverUrl: String,
    val autoConnect: Boolean = true,
    val enableWakeWord: Boolean,
    val talkWord: String,
    val wakeWord: String,
    val wakeGain: Float,
    val talkSilenceSensitivity: Float,
    val micGain: Float,
    val echoDuckingGain: Float,
    val audioOutput: AudioOutput,
    val buttonTriggerEnabled: Boolean,
    val stayConnectedInBackground: Boolean = false,
) {
    fun wakeConfig(): WakeServiceConfig =
        WakeServiceConfig(enableWakeWord, talkWord, wakeWord, wakeGain, talkSilenceSensitivity, serverUrl)

    companion object {
        fun from(d: DeviceSettings) = VoiceHostSettings(
            serverUrl = d.serverUrl,
            autoConnect = d.autoConnect,
            enableWakeWord = d.enableWakeWord,
            talkWord = d.talkWord,
            wakeWord = d.wakeWord,
            wakeGain = d.wakeWordMicGainLevel,
            talkSilenceSensitivity = d.talkSilenceSensitivity,
            micGain = d.micGainLevel,
            echoDuckingGain = d.echoDuckingGain,
            audioOutput = d.audioOutput,
            buttonTriggerEnabled = d.enableButtonTrigger,
            stayConnectedInBackground = d.stayConnectedInBackground,
        )
    }
}

internal fun AudioOutput.toChoice(): OutputChoice = when (this) {
    AudioOutput.AUTO -> OutputChoice.AUTO
    AudioOutput.EARPIECE -> OutputChoice.EARPIECE
    AudioOutput.LOUDSPEAKER -> OutputChoice.LOUDSPEAKER
    AudioOutput.WIRED -> OutputChoice.WIRED
    AudioOutput.BLUETOOTH -> OutputChoice.BLUETOOTH
}

/** Wake-word health as the UI and the notification show it (spec 14 §2.6, §2.7, §5.2). */
enum class WakeHealth {
    /** Wake word disabled in settings. */
    DISABLED,

    /** Armed (or between cycles). */
    ARMED,

    /** Paused while this device's voice session owns the mic. */
    PAUSED_FOR_VOICE,

    /** The user paused listening (notification / settings). */
    PAUSED_BY_USER,

    /** The mic is held by another app (8 consecutive open failures; inv04 §3.5 Inc 9). */
    MIC_STALLED,

    /** Android 14+: the service was (re)started from the background without the microphone type. */
    PAUSED_NEEDS_FOREGROUND,

    /** Android 12+: `ForegroundServiceStartNotAllowedException`; "Tap to resume listening". */
    BACKGROUND_RESTRICTED,
}

enum class HostConnection { OFFLINE, CONNECTING, CONNECTED }

/** Last user / assistant transcript (the lite face; each side capped at [MAX_CHARS]). */
data class LastExchange(val user: String? = null, val assistant: String? = null) {
    companion object {
        const val MAX_CHARS = 600
    }
}

/** The aggregated, immutable host state (spec 14 §2.5; inv04 §7.2 "State (read)"). */
data class VoiceUiState(
    val connection: HostConnection = HostConnection.OFFLINE,
    val serverUrl: String? = null,
    /** The probe found no orchestrator (lite: "No conversation open on the server"). */
    val noOrchestrator: Boolean = false,
    val session: VoiceSessionState = VoiceSessionState(),
    /** P-2 link health of live voice: Up / Reconnecting(elapsed) / Failed (manual Reconnect). */
    val link: VoiceLinkState = VoiceLinkState.Up,
    val talk: TalkUiState = TalkUiState(),
    val wake: WakeHealth = WakeHealth.DISABLED,
    val wakePhase: WakePhase? = null,
    val speakerMuted: Boolean = false,
    val pushToTalk: Boolean = false,
    val lastExchange: LastExchange = LastExchange(),
    /**
     * Main app: agent turns the service is held for when that hold is its only reason to run
     * (`VoiceHostRuntime.setAgentWorkHold`); the idle notification then says "Waiting for N agent
     * sessions" instead of "Connected". 0 otherwise.
     */
    val agentTurnsWaiting: Int = 0,
)

sealed interface VoiceUiEvent {
    data class Toast(val message: String) : VoiceUiEvent
    data class Cue(val kind: CueKind) : VoiceUiEvent
    data class Transcript(val role: Role, val text: String, val final: Boolean) : VoiceUiEvent

    enum class Role { USER, ASSISTANT, SYSTEM }
}

/**
 * What differs between the main app and the lite app (spec 14 §2.5, §5.4).
 *
 * @property launchActivity the Activity a notification tap opens.
 * @property trampolineActivity foreground-context entry for "Talk" / "Resume listening" (main:
 *   `VoiceTrampolineActivity`, B-09; lite: the launch Activity).
 * @property bringToFrontOnTrigger lite: wake the screen and start [launchActivity] on a wake/talk
 *   trigger (API 21 allows it). Main: never (background-activity-start limits, §2.6).
 * @property serviceRunsAlways lite: the FGS runs whenever the process runs (companion watchdog
 *   contract 3). Main: only while wake word, voice or "stay connected" needs it.
 * @property rawRecentsMonitor lite: the old `/dev/input/event2` recents monitor, now behind the toggle.
 */
data class HostConfig(
    val launchActivity: Class<out Activity>,
    val trampolineActivity: Class<out Activity> = launchActivity,
    val notification: NotificationSpec,
    val bringToFrontOnTrigger: Boolean,
    val serviceRunsAlways: Boolean,
    /** Lite: also run the raw `/dev/input/event2` recents monitor (decision E-7, pending Q8 evidence). */
    val rawRecentsMonitor: Boolean = false,
) {
    companion object {
        /** The A300M lite app (old notification texts, channel and id). */
        fun lite(launchActivity: Class<out Activity>, smallIcon: Int = android.R.drawable.ic_btn_speak_now) = HostConfig(
            launchActivity = launchActivity,
            notification = NotificationSpec.lite(smallIcon),
            bringToFrontOnTrigger = true,
            serviceRunsAlways = true,
            rawRecentsMonitor = true,
        )

        /** The main app (`com.assistant.archie`). */
        fun main(
            launchActivity: Class<out Activity>,
            trampolineActivity: Class<out Activity>,
            smallIcon: Int = android.R.drawable.ic_btn_speak_now,
        ) = HostConfig(
            launchActivity = launchActivity,
            trampolineActivity = trampolineActivity,
            notification = NotificationSpec.main(smallIcon),
            bringToFrontOnTrigger = false,
            serviceRunsAlways = false,
        )
    }
}

/**
 * Notification texts, channels and icon (spec 14 §2.7; lite = the old `AssistantService` strings).
 * The `host` and `voice` notifications share [HostTuning.NOTIFICATION_ID], updated in place.
 */
data class NotificationSpec(
    val smallIcon: Int,
    val hostChannelId: String,
    val hostChannelName: String,
    val hostChannelDescription: String,
    val voiceChannelId: String,
    val voiceChannelName: String,
    val title: String,
    /** Idle text while the wake word is armed; `%s` = the wake phrase (or a fixed text without it). */
    val armedText: String,
    val idleText: String,
    val pausedText: String,
    val needsForegroundText: String,
    /** "Archie" / "Assistant": the voice notification reads "<appName> · Listening". */
    val appName: String,
    /** Main: Pause/Resume listening, Talk, Mute, End. Lite: the old notification had none; End only. */
    val fullActions: Boolean,
) {
    companion object {
        fun lite(smallIcon: Int) = NotificationSpec(
            smallIcon = smallIcon,
            hostChannelId = HostTuning.NOTIFICATION_CHANNEL_ID,
            hostChannelName = "Assistant Service",
            hostChannelDescription = "Keeps the assistant running in background",
            voiceChannelId = HostTuning.NOTIFICATION_CHANNEL_ID,
            voiceChannelName = "Assistant Service",
            title = "Assistant Active",
            armedText = "Listening for commands",
            idleText = "Listening for commands",
            pausedText = "Wake word paused",
            needsForegroundText = "Tap to resume listening",
            appName = "Assistant",
            fullActions = false,
        )

        fun main(smallIcon: Int) = NotificationSpec(
            smallIcon = smallIcon,
            hostChannelId = "host",
            hostChannelName = "Listening",
            hostChannelDescription = "Wake word and background connection",
            voiceChannelId = "voice",
            voiceChannelName = "Voice conversation",
            title = "Archie",
            armedText = "Listening for \"%s\"",
            idleText = "Connected",
            pausedText = "Wake word paused",
            needsForegroundText = "Tap to resume listening",
            appName = "Archie",
            fullActions = true,
        )
    }
}
