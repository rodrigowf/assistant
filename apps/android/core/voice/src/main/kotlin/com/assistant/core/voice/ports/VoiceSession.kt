package com.assistant.core.voice.ports

import com.assistant.core.audio.ports.AppliedRoute
import com.assistant.core.audio.ports.MonotonicClock
import com.assistant.core.audio.ports.OutputChoice
import com.assistant.core.audio.ports.ProviderKind
import com.assistant.core.audio.ports.VoiceLog
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.serialization.json.JsonObject

/*
 * Realtime voice session (inv04 §2.4, §3.2). Interface-only (A-04).
 *
 * The data classes below mirror `:core:model` types (`VoiceConfig`, `ConnectionInfo`) that A-02 is
 * writing in parallel; they exist so the port compiles now. The coordinator may replace them with
 * typealiases once A-02 lands.
 */

/** `GET /api/config` voice fields (on any error the API returns openai / gpt-realtime / cedar). */
data class VoiceStartConfig(
    val provider: String,
    val model: String,
    val voice: String,
    val transcriptionLanguage: String,
    /** Blank = backend default. */
    val endpoint: String,
)

enum class ConnectionType { WEBRTC, WEBSOCKET }

/** `connection_info` of `POST /api/orchestrator/voice/session` (sample rates default to 24000). */
data class VoiceConnection(
    val connectionType: ConnectionType,
    val endpoint: String,
    val ephemeralToken: String,
    val model: String,
    val voice: String,
    val inSampleRateHz: Int,
    val outSampleRateHz: Int,
)

/** `voice_start` payload (inv04 §2.1). [endpoint] is null when the config endpoint is blank. */
data class VoiceStartRequest(
    val localId: String,
    /** `orchestratorJsonlSessionId ?: orchestratorCurrentSessionId`. */
    val resumeSdkId: String?,
    val provider: String,
    val model: String,
    val voiceName: String,
    val transcriptionLanguage: String,
    val endpoint: String?,
    /** Spec 12 OPEN-2: the re-arm after a reconnect only re-subscribes; never re-creates a closed conversation. */
    val reattach: Boolean = false,
)

/** Orchestrator-socket writes the voice core needs (encoded by :core:protocol). */
interface VoiceWire {
    fun sendVoiceStart(request: VoiceStartRequest)
    fun sendVoiceStop()

    /** `voice_event{event}` — the WebRTC mirror of every data-channel event. */
    fun sendVoiceEvent(event: JsonObject)

    /** `voice_audio_in{audio}` — WS-provider mic chunk. */
    fun sendVoiceAudioIn(audioB64: String)

    /** Plain `start` with the resume checkpoint (reconnect without a live voice config). */
    fun sendResumeStart(localId: String, sdkSessionId: String?)
}

/** The two voice REST calls (`VoiceApi` in :core:network). */
interface VoiceBackendApi {
    suspend fun getVoiceConfig(): VoiceStartConfig?
    suspend fun startVoiceSession(config: VoiceStartConfig): VoiceConnection?
}

/** Which orchestrator session voice binds to. */
interface OrchestratorContext {
    val isOrchestratorSession: Boolean
    val localId: String
    val jsonlSessionId: String?
    val currentSessionId: String?
}

/** Pause/resume hand-off with the wake-word engine (ack-based, inv04 §3.2 PAUSING_WAKE, §3.5). */
interface WakeHandoff {
    fun pauseWake(): Deferred<Unit>
    fun resumeWake(): Deferred<Unit>
}

/** Where voice transcripts go (main app: the orchestrator conversation; lite: last exchange). */
interface TranscriptSink {
    fun userTranscript(text: String, final: Boolean)
    fun assistantTranscript(text: String, final: Boolean)
    fun system(text: String)
    fun voiceMessageSent()
    fun turnComplete()

    /** Backend ended voice: finalize any streaming text (RS-17). */
    fun voiceEnded()
}

/** Audible cues; default tones on STREAM_MUSIC (inv04 §4.10, RS-19, RS-45). */
interface VoiceCues {
    fun wakeAck()
    fun talkAck()
    fun reconnect()
}

/**
 * OS audio session for a call: focus (`AudioFocusPolicy`), routing (`RouteDecider` + `RouteApplier`),
 * STREAM_VOICE_CALL raise, device callback. [applyRoute] is called at start, at each scheduled
 * re-apply and on device/setting changes; the controller forwards the speaker mode to the transport
 * and toasts a `BluetoothUnsupported` fallback.
 */
interface AudioSessionPort {
    fun acquireFocus()
    fun applyRoute(provider: ProviderKind, desired: OutputChoice): AppliedRoute
    fun release()
}

/** Backend → client voice frames (decoded by :core:protocol). */
sealed interface VoiceInbound {
    /** `voice_initiator` is false when the field is missing (`9b24d1a`). */
    data class SessionStarted(
        val voice: Boolean,
        val voiceInitiator: Boolean,
        val voiceSessionUpdate: JsonObject?,
    ) : VoiceInbound

    data class Command(val command: JsonObject) : VoiceInbound
    data class ProviderEvent(val event: JsonObject) : VoiceInbound
    data class AudioOut(val audioB64: String) : VoiceInbound
    data class Ending(val reason: String?) : VoiceInbound
    data class Ended(val reason: String?) : VoiceInbound
    data object Stopped : VoiceInbound
    data class OwnerActive(val active: Boolean, val ownerLocalId: String?) : VoiceInbound
    data class VadState(val state: String, val durationMs: Long) : VoiceInbound
}

/** Orchestrator-channel lifecycle as the voice core sees it. */
sealed interface ConnectionSignal {
    /** First connect / adoption: never auto-starts voice (RS-11). */
    data object Connected : ConnectionSignal

    /** A genuine reconnect (`initialConnectionDone` gate). */
    data class Reconnected(val localId: String, val sdkSessionId: String?) : ConnectionSignal
    data class Disconnected(val willReconnect: Boolean) : ConnectionSignal
}

/** UI phase (old `VoiceState` minus the dead `Listening`). */
enum class SessionPhase { OFF, CONNECTING, SUMMARIZING, ACTIVE, SPEAKING, THINKING, TOOL_USE, ENDING, ERROR }

data class VoiceSessionState(
    val phase: SessionPhase = SessionPhase.OFF,
    val errorMessage: String? = null,
    val isOwner: Boolean = false,
    val remoteVoiceActive: Boolean = false,
    val isMuted: Boolean = false,
    val vadState: String = "idle",
    val vadDurationMs: Long = 0L,
    val reconnectBanner: String? = null,
)

sealed interface VoiceSessionEvent {
    data class Toast(val message: String) : VoiceSessionEvent
}

/**
 * The voice-session facade (inv04 §3.2). Ownership: start or `session_started(voice, initiator)`
 * → owner; finalize → not owner. Owner-only: command, ending, ended/stopped, the
 * `voice_session_update` forward. Finalize is idempotent and re-arms the wake word 1500 ms later.
 */
interface VoiceSessionController {
    val state: StateFlow<VoiceSessionState>
    val events: SharedFlow<VoiceSessionEvent>

    /** The current transport's [VoiceLevels] while subscribed; null without a transport. Observation only. */
    val levels: StateFlow<VoiceLevels?> get() = VoiceLevels.None

    /** Synchronous UI flip to CONNECTING on wake detection, before any network work (RS-45). */
    fun markConnecting()
    fun startVoice()
    fun stopVoice()
    fun toggleMute()
    fun setMicGain(level: Float)
    fun setEchoDuckingGain(gain: Float)
    fun setAudioOutput(output: OutputChoice)
    fun onInbound(frame: VoiceInbound)
    fun onConnection(signal: ConnectionSignal)
    fun release()
}

class VoiceSessionDeps(
    val scope: CoroutineScope,
    val clock: MonotonicClock,
    val log: VoiceLog,
    val api: VoiceBackendApi,
    val wire: VoiceWire,
    val orchestrator: OrchestratorContext,
    val wake: WakeHandoff,
    val transports: VoiceTransportFactory,
    val audio: AudioSessionPort,
    val transcripts: TranscriptSink,
    val cues: VoiceCues,
)
