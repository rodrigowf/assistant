package com.assistant.peripheral.voice

import com.assistant.peripheral.data.VoiceState
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow

/**
 * Connection topology for a voice provider.  Mirrors backend
 * `BaseVoiceProvider.connection_type` so the Android client can dispatch
 * to the right transport with the same field name.
 */
enum class VoiceConnectionType(val wireValue: String) {
    WEBRTC("webrtc"),
    WEBSOCKET("websocket");

    companion object {
        fun fromWire(value: String?): VoiceConnectionType =
            values().firstOrNull { it.wireValue == value } ?: WEBRTC
    }
}

/**
 * Connection metadata returned by the backend's `_attach_voice_payload`,
 * arriving on the `session_started` orchestrator event.  Different
 * transports use different fields:
 *
 * - WebRTC (OpenAI): `endpoint`, `ephemeralToken`, `model` go into the
 *   SDP exchange.  Audio bypasses our backend entirely.
 * - WebSocket (Qwen + future locals): the backend already owns the
 *   upstream WS — `audioInFormat` / `audioOutFormat` describe the PCM
 *   format the relay expects.  `endpoint` is informational only.
 */
data class VoiceConnectionInfo(
    val connectionType: VoiceConnectionType,
    val endpoint: String,
    val ephemeralToken: String?,
    val expiresAt: Long?,
    val model: String,
    val voice: String,
    val audioInSampleRate: Int,
    val audioInEncoding: String,    // "pcm16" | "pcm" | etc.
    val audioOutSampleRate: Int,
    val audioOutEncoding: String,
    /**
     * For WebSocket transport, the session.update payload the backend has
     * already sent upstream — surfaced for diagnostics, not for re-send.
     */
    val sessionUpdate: Map<String, Any?>? = null,
)

// Note: [VoiceEvent] is the existing sealed class declared in
// VoiceManager.kt — providers reuse it so the ViewModel doesn't need a
// second observer.  Adding new variants there is preferred over forking.

/**
 * Snapshot of the backend's `default_voice_*` fields from
 * `assistant_config.json`.  The Android app fetches this on connect and
 * passes the values to [VoiceProvider.connect] so the source of truth
 * stays the backend (toggled from the web frontend).
 */
data class VoiceConfig(
    val provider: String,           // e.g. "openai", "qwen"
    val model: String,              // provider-specific model id
    val voice: String,              // voice/speaker id
    val transcriptionLanguage: String,  // ISO-639-1; "" = auto-detect
    // Google-only backend selector: "vertex" or "aistudio". Empty/null for
    // other providers — they ignore it. Critical for Gemini Live because
    // some Live models (e.g. ``gemini-3.1-flash-live-preview``) only exist
    // on AI Studio and get rejected by Vertex with a policy error.
    val endpoint: String = "",
) {
    companion object {
        /** Conservative default — matches the backend's hard-coded fallback. */
        val DEFAULT = VoiceConfig(
            provider = "openai",
            model = "gpt-realtime",
            voice = "cedar",
            transcriptionLanguage = "",
            endpoint = "",
        )
    }
}

/**
 * Provider-agnostic interface for a realtime voice session.
 *
 * Mirrors `orchestrator/providers/voice_base.py:BaseVoiceProvider` on the
 * backend.  Each provider hides its transport plumbing behind these
 * methods so [VoiceManager] can switch providers without knowing the
 * details.
 *
 * Lifecycle:
 *   1. `connect(info, sendUpstream)` — open the upstream connection and
 *      wire the callback for events that need to round-trip through the
 *      orchestrator WebSocket (typically the WebRTC mirror; WebSocket
 *      providers don't use it).
 *   2. `state` and `events` flow updates to the UI.
 *   3. `handleBackendCommand(cmd)` — voice_command from backend (WebRTC
 *      forwards via data channel; WebSocket no-ops since the backend
 *      relay sends directly).
 *   4. `setMuted(b)` / `setMicGain(g)` / `setEchoDuckingGain(g)` /
 *      `setAudioOutput(o)` — UI affordances.
 *   5. `disconnect()` — tear down.
 */
interface VoiceProvider {
    /** Stable identifier for logging (e.g. "openai", "qwen"). */
    val providerId: String

    /** Transport this provider implements. */
    val connectionType: VoiceConnectionType

    /** Lifecycle state — drives the UI mic-button colour. */
    val state: StateFlow<VoiceState>

    /** Conversation events — drives transcript bubbles and tool cards. */
    val events: SharedFlow<VoiceEvent>

    /**
     * Open the upstream connection and start ferrying audio.
     *
     * Two of the four hooks are direction-specific:
     *
     * - WebRTC (OpenAI): only [mirrorEventToBackend] is used — every
     *   data-channel event is mirrored to the orchestrator WS so the
     *   backend can persist transcripts.  Audio bypasses our backend.
     * - WebSocket (Qwen, future locals): only [sendMicChunkToBackend]
     *   is used — captured PCM mic chunks are base64-encoded and sent
     *   to the backend as `voice_audio_in` messages.  The backend
     *   relays them upstream and the matching `voice_audio_out` chunks
     *   arrive via [pushSpeakerChunk] for playback.
     *
     * @param info connection metadata from the backend's
     *   `/api/orchestrator/voice/session` response (or, for WebSocket
     *   providers, [VoiceConnectionInfo] derived from the same
     *   payload).
     * @param mirrorEventToBackend WebRTC: invoked for every provider
     *   event so it can be persisted upstream.
     *   WebSocket: typically unused (backend already sees the upstream
     *   directly via its own relay).
     * @param sendMicChunkToBackend WebSocket: invoked for every
     *   captured mic chunk.  The argument is base64-encoded PCM in the
     *   provider's input format.
     *   WebRTC: unused (audio doesn't pass through us).
     */
    suspend fun connect(
        info: VoiceConnectionInfo,
        mirrorEventToBackend: (Map<String, Any?>) -> Unit,
        sendMicChunkToBackend: (String) -> Unit = {},
    )

    /**
     * Forward a backend-originated command to the provider.
     *
     * For WebRTC: send over the data channel.
     * For WebSocket: no-op (the backend relay forwards directly upstream).
     */
    fun handleBackendCommand(command: Map<String, Any?>)

    /**
     * Push a base64-encoded PCM speaker chunk for playback.
     *
     * Called for `voice_audio_out` server messages on the WebSocket
     * path.  WebRTC providers ignore this — speaker audio arrives via
     * the peer connection.
     */
    fun pushSpeakerChunk(audioB64: String) {
        // Default no-op — only WebSocket providers override.
    }

    /**
     * Handle a provider event mirrored from the backend.
     *
     * For WebSocket providers, the backend's voice_relay forwards every
     * upstream provider event back to us as `voice_event` messages.
     * The provider parses the event (transcripts, response state, tool
     * calls) and emits the appropriate [VoiceEvent]s on its own flow.
     *
     * For WebRTC providers this is a no-op — events arrive via the
     * data channel directly, not over the orchestrator WS.
     */
    fun handleProviderEvent(event: Map<String, Any?>) {
        // Default no-op — only WebSocket providers override.
    }

    /** Toggle mic mute.  Returns the new muted state. */
    fun toggleMute(): Boolean

    fun isMuted(): Boolean

    /**
     * Mic gain multiplier. 1.0 = unity, 0.0 = silence, >1.0 = amplify.
     * Implementations may clamp to a sane range.
     */
    fun setMicGain(level: Float)

    /**
     * Gain applied while the agent is speaking, to suppress acoustic
     * echo bleeding back into the mic.  0.05 = -26dB.  Set to 1.0 to
     * disable ducking.
     */
    fun setEchoDuckingGain(gain: Float)

    /** Tear down the upstream connection and release resources. */
    suspend fun disconnect()
}
