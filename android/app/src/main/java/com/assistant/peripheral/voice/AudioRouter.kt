package com.assistant.peripheral.voice

import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothProfile
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.os.Build
import android.util.Log
import com.assistant.peripheral.data.AudioOutput

/**
 * Picks the right audio-routing strategy for a voice session and
 * applies it to the system [AudioManager].
 *
 * Why this exists as its own module: routing logic used to live inline
 * in [VoiceManager] and grew into a thicket of mode/setSpeakerphoneOn/
 * setCommunicationDevice branches that couldn't represent the case
 * "user wants Bluetooth but the connected BT device only supports
 * A2DP media playback, not call audio". This module owns that
 * decision tree.
 *
 * Three input axes drive the decision:
 *
 *   1. ``audioOutput``     — what the user picked (EARPIECE /
 *                            LOUDSPEAKER / BLUETOOTH).
 *   2. ``providerKind``    — WEBSOCKET (we own the AudioTrack and can
 *                            switch its attributes) vs WEBRTC (audio
 *                            module is opaque, stuck in
 *                            communication-audio plane).
 *   3. connected BT class  — HFP-capable (has mic, can be a
 *                            communication device) vs A2DP-only
 *                            (media sink — speakers, JBL Flip).
 *
 * Routes:
 *
 *   - [Route.Earpiece]              — call audio plane, route to
 *                                     built-in earpiece.
 *   - [Route.Loudspeaker]           — call audio plane, route to
 *                                     built-in loudspeaker.
 *   - [Route.BluetoothCallAudio]    — call audio plane, route to a
 *                                     BT HFP device. Mic and speaker
 *                                     both on the headset.
 *   - [Route.BluetoothMedia]        — media audio plane (USAGE_MEDIA),
 *                                     route to a BT A2DP device. Only
 *                                     valid for WebSocket providers
 *                                     because we own the AudioTrack
 *                                     and can rebuild it with media
 *                                     attributes. Mic stays internal.
 *   - [Route.BluetoothUnsupported]  — user picked BT but the only
 *                                     connected device is A2DP-only
 *                                     AND the provider is WebRTC. Fall
 *                                     back to loudspeaker and let the
 *                                     UI surface a toast.
 */
class AudioRouter(private val context: Context) {

    companion object {
        private const val TAG = "AudioRouter"
    }

    enum class ProviderKind { WEBSOCKET, WEBRTC }

    /**
     * What kind of speaker output the active provider's [Route] wants.
     * Consumed by [WebSocketPcmProvider.setSpeakerMode] to pick
     * AudioTrack attributes.
     *
     *  - CALL: ``USAGE_VOICE_COMMUNICATION`` / ``STREAM_VOICE_CALL`` —
     *          routes via the communication-audio plane.  Cannot reach
     *          A2DP-only devices.
     *  - MEDIA: ``USAGE_MEDIA`` / ``STREAM_MUSIC`` — routes via the
     *           media-audio plane.  Required for A2DP-only BT speakers.
     */
    enum class SpeakerMode { CALL, MEDIA }

    /**
     * Reason a route was downgraded.  Surfaced to the user as a toast
     * via [VoiceEvent.RoutingFallback] so silent "JBL plugged but
     * audio still on phone" surprises don't happen.
     */
    enum class FallbackReason {
        BT_NOT_AVAILABLE,
        BT_A2DP_REQUIRES_WS_PROVIDER,
    }

    sealed class Route {
        object Earpiece : Route()
        object Loudspeaker : Route()
        // device is nullable because pre-Android-6 we can't enumerate
        // AudioDeviceInfo — we only know the BT profile is connected.
        data class BluetoothCallAudio(val device: AudioDeviceInfo?) : Route()
        data class BluetoothMedia(val device: AudioDeviceInfo?) : Route()
        data class BluetoothUnsupported(
            val device: AudioDeviceInfo?,
            val reason: FallbackReason,
        ) : Route()
        // Wired 3.5mm headphone/headset (or USB audio on devices that
        // support it). device is nullable for pre-M devices where we
        // can't enumerate AudioDeviceInfo — the sticky
        // ACTION_HEADSET_PLUG broadcast confirms presence instead.
        data class WiredHeadphone(val device: AudioDeviceInfo?) : Route()

        /** Which speaker mode the provider's AudioTrack should use. */
        val speakerMode: SpeakerMode
            get() = if (this is BluetoothMedia) SpeakerMode.MEDIA else SpeakerMode.CALL

        /** Human-readable label for logs. */
        val label: String
            get() = when (this) {
                is Earpiece -> "earpiece"
                is Loudspeaker -> "loudspeaker"
                is BluetoothCallAudio -> "bluetooth-call(${device?.productName ?: "unknown"})"
                is BluetoothMedia -> "bluetooth-media(${device?.productName ?: "unknown"})"
                is BluetoothUnsupported -> "bluetooth-unsupported(${reason.name})"
                is WiredHeadphone -> "wired(${device?.productName ?: "unknown"})"
            }
    }

    private val audioManager: AudioManager =
        context.getSystemService(Context.AUDIO_SERVICE) as AudioManager

    // -------------------------------------------------------------------------
    // Decision
    // -------------------------------------------------------------------------

    /**
     * Pick the route for the requested output, taking the connected
     * BT devices and provider kind into account.
     */
    fun pickRoute(desired: AudioOutput, providerKind: ProviderKind): Route {
        return when (desired) {
            AudioOutput.EARPIECE -> Route.Earpiece
            AudioOutput.LOUDSPEAKER -> Route.Loudspeaker
            AudioOutput.BLUETOOTH -> pickBluetoothRoute(providerKind)
            AudioOutput.WIRED -> Route.WiredHeadphone(findWiredHeadphoneDevice())
        }
    }

    private fun pickBluetoothRoute(providerKind: ProviderKind): Route {
        // Modern API path — Android 12+ has the proper "communication
        // device" abstraction.  Legacy path uses SCO start/stop.
        if (hasBluetoothCallAudio()) {
            return Route.BluetoothCallAudio(findBluetoothCallAudioDevice())
        }

        if (hasBluetoothMedia()) {
            val mediaDevice = findBluetoothMediaDevice()
            return if (providerKind == ProviderKind.WEBSOCKET) {
                Route.BluetoothMedia(mediaDevice)
            } else {
                // WebRTC's JavaAudioDeviceModule is pinned to the
                // communication-audio plane and can't be rerouted to
                // A2DP without a custom ADM rebuild.  Fall back to
                // loudspeaker so SOMETHING plays, and tell the user.
                Route.BluetoothUnsupported(
                    mediaDevice,
                    FallbackReason.BT_A2DP_REQUIRES_WS_PROVIDER,
                )
            }
        }
        return Route.BluetoothUnsupported(null, FallbackReason.BT_NOT_AVAILABLE)
    }

    /**
     * HFP-capable BT device — has a mic, listed in
     * ``availableCommunicationDevices`` on Android 12+.  These work
     * with the existing call-audio path on every provider.
     *
     * Returns null on Android 5.x even when a headset is connected:
     * `AudioManager.getDevices` was added in API 23, and there is no
     * pre-M way to enumerate `AudioDeviceInfo`.  Callers must rely on
     * [hasBluetoothCallAudio] for availability and treat null as
     * "available but device handle unavailable" on pre-M.
     */
    private fun findBluetoothCallAudioDevice(): AudioDeviceInfo? {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            return audioManager.availableCommunicationDevices.firstOrNull {
                isBluetoothType(it.type)
            }
        }
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) {
            return null
        }
        // API 23..30: SCO is the only call-audio path.  Probe the BT
        // adapter's HEADSET profile to decide whether to enumerate.
        if (!isHeadsetProfileConnected()) return null
        return audioManager.getDevices(AudioManager.GET_DEVICES_OUTPUTS).firstOrNull {
            it.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO
        }
    }

    /**
     * A2DP-only BT sink — media-class only, no mic.  Reachable through
     * the media audio plane on every Android version.
     *
     * Returns null on Android 5.x — see [findBluetoothCallAudioDevice]
     * for the same caveat.
     */
    private fun findBluetoothMediaDevice(): AudioDeviceInfo? {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) return null
        return audioManager.getDevices(AudioManager.GET_DEVICES_OUTPUTS).firstOrNull {
            it.type == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP ||
                (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S &&
                    (it.type == AudioDeviceInfo.TYPE_BLE_HEADSET ||
                        it.type == AudioDeviceInfo.TYPE_BLE_SPEAKER ||
                        it.type == AudioDeviceInfo.TYPE_BLE_BROADCAST))
        }
    }

    /**
     * Whether an HFP-capable BT device is currently connected.  Works
     * on every API level: pre-M uses BluetoothAdapter profile state
     * since `AudioManager.getDevices` is API 23+.
     */
    private fun hasBluetoothCallAudio(): Boolean {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            return findBluetoothCallAudioDevice() != null ||
                (Build.VERSION.SDK_INT < Build.VERSION_CODES.S &&
                    isHeadsetProfileConnected())
        }
        return isHeadsetProfileConnected()
    }

    /**
     * Whether an A2DP-only BT sink is currently connected.  Pre-M uses
     * BluetoothAdapter profile state for the same reason as
     * [hasBluetoothCallAudio].
     */
    private fun hasBluetoothMedia(): Boolean {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            return findBluetoothMediaDevice() != null || isA2dpProfileConnected()
        }
        return isA2dpProfileConnected()
    }

    private fun isHeadsetProfileConnected(): Boolean {
        val adapter = BluetoothAdapter.getDefaultAdapter() ?: return false
        if (!adapter.isEnabled) return false
        @Suppress("DEPRECATION")
        return adapter.getProfileConnectionState(BluetoothProfile.HEADSET) ==
            BluetoothProfile.STATE_CONNECTED
    }

    private fun isA2dpProfileConnected(): Boolean {
        val adapter = BluetoothAdapter.getDefaultAdapter() ?: return false
        if (!adapter.isEnabled) return false
        @Suppress("DEPRECATION")
        return adapter.getProfileConnectionState(BluetoothProfile.A2DP) ==
            BluetoothProfile.STATE_CONNECTED
    }

    private fun isBluetoothType(type: Int): Boolean = when (type) {
        AudioDeviceInfo.TYPE_BLUETOOTH_A2DP,
        AudioDeviceInfo.TYPE_BLUETOOTH_SCO -> true
        else -> if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S)
            type == AudioDeviceInfo.TYPE_BLE_HEADSET ||
                type == AudioDeviceInfo.TYPE_BLE_SPEAKER ||
                type == AudioDeviceInfo.TYPE_BLE_BROADCAST
        else false
    }

    /**
     * Whether any BT audio sink (HFP or A2DP) is currently connected.
     * Drives the UI's "enable BLUETOOTH option?" toggle.  Safe on
     * every API level: pre-M paths probe the BluetoothAdapter rather
     * than calling `getDevices` (which only exists on API 23+).
     */
    fun isBluetoothAudioAvailable(): Boolean {
        return hasBluetoothCallAudio() || hasBluetoothMedia()
    }

    /**
     * Wired 3.5mm headphone / headset device (or USB audio on devices
     * that support it).  Returns null on Android 5.x — see
     * [hasWiredHeadphone] for availability.
     */
    private fun findWiredHeadphoneDevice(): AudioDeviceInfo? {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) return null
        return audioManager.getDevices(AudioManager.GET_DEVICES_OUTPUTS).firstOrNull {
            it.type == AudioDeviceInfo.TYPE_WIRED_HEADSET ||
                it.type == AudioDeviceInfo.TYPE_WIRED_HEADPHONES ||
                (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O &&
                    it.type == AudioDeviceInfo.TYPE_USB_HEADSET)
        }
    }

    /**
     * Whether a wired headphone / headset is currently plugged in.
     * Pre-M reads the sticky [Intent.ACTION_HEADSET_PLUG] broadcast
     * (still supported on every API level) since `AudioManager.getDevices`
     * is API 23+.
     */
    private fun hasWiredHeadphone(): Boolean {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            return findWiredHeadphoneDevice() != null || isWiredHeadsetPlugged()
        }
        return isWiredHeadsetPlugged()
    }

    /**
     * Read the sticky `ACTION_HEADSET_PLUG` broadcast.  The OS retains
     * the last value of this broadcast and `registerReceiver(null, ...)`
     * returns it synchronously without subscribing.  The ``state`` extra
     * is 1 when plugged, 0 when unplugged.
     */
    private fun isWiredHeadsetPlugged(): Boolean {
        val intent = context.registerReceiver(
            null,
            IntentFilter(Intent.ACTION_HEADSET_PLUG),
        ) ?: return false
        return intent.getIntExtra("state", 0) == 1
    }

    /**
     * Whether a wired headphone/headset is plugged in.  Drives the UI's
     * "enable WIRED option?" toggle.  Safe on every API level.
     */
    fun isWiredHeadphoneAvailable(): Boolean = hasWiredHeadphone()

    // -------------------------------------------------------------------------
    // Apply
    // -------------------------------------------------------------------------

    /**
     * Push a [Route] into the system [AudioManager].  Returns the
     * [SpeakerMode] the active provider should use for its own
     * AudioTrack (call audio vs media audio).
     *
     * The [VoiceManager] is expected to:
     *   1. Call [apply] to set the system route.
     *   2. Forward the returned [SpeakerMode] to the active
     *      [VoiceProvider] (only [WebSocketPcmProvider] reacts; WebRTC
     *      providers ignore it because their AudioTrack is owned by
     *      JavaAudioDeviceModule).
     *   3. If the [Route] is [Route.BluetoothUnsupported], emit a
     *      [VoiceEvent.RoutingFallback] so the UI can toast.
     */
    fun apply(route: Route): SpeakerMode {
        // The communication-audio plane needs MODE_IN_COMMUNICATION.
        // The media-audio plane works in MODE_NORMAL — and forcing
        // IN_COMMUNICATION there would yank STREAM_VOICE_CALL back onto
        // the earpiece, so we deliberately switch.
        val targetMode = if (route is Route.BluetoothMedia)
            AudioManager.MODE_NORMAL
        else
            AudioManager.MODE_IN_COMMUNICATION
        try {
            if (audioManager.mode != targetMode) {
                audioManager.mode = targetMode
                Log.d(TAG, "audio mode → $targetMode for ${route.label}")
            }
        } catch (e: Exception) {
            Log.w(TAG, "set audio mode failed: ${e.message}")
        }

        when (route) {
            is Route.Earpiece -> applyCommunicationRoute(
                AudioDeviceInfo.TYPE_BUILTIN_EARPIECE,
                speakerphone = false,
            )
            is Route.Loudspeaker -> applyCommunicationRoute(
                AudioDeviceInfo.TYPE_BUILTIN_SPEAKER,
                speakerphone = true,
            )
            is Route.BluetoothCallAudio -> applyBluetoothCallAudio(route.device)
            is Route.BluetoothMedia -> applyBluetoothMedia()
            is Route.BluetoothUnsupported -> {
                // Fallback: route to loudspeaker so SOMETHING plays.
                applyCommunicationRoute(
                    AudioDeviceInfo.TYPE_BUILTIN_SPEAKER,
                    speakerphone = true,
                )
            }
            is Route.WiredHeadphone -> applyWiredHeadphone(route.device)
        }

        logFinalState(route)
        return route.speakerMode
    }

    /**
     * Apply a built-in (earpiece / loudspeaker) communication route.
     * Modern path uses setCommunicationDevice; legacy uses
     * setSpeakerphoneOn.  Both end up at the same place.
     */
    private fun applyCommunicationRoute(targetType: Int, speakerphone: Boolean) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            val device = audioManager.availableCommunicationDevices.firstOrNull {
                it.type == targetType
            }
            if (device != null) {
                val ok = audioManager.setCommunicationDevice(device)
                Log.d(TAG, "setCommunicationDevice(type=$targetType) → $ok")
                return
            }
            Log.w(TAG, "no communication device of type=$targetType; clearing + speakerphone=$speakerphone")
            audioManager.clearCommunicationDevice()
        }
        @Suppress("DEPRECATION")
        audioManager.isSpeakerphoneOn = speakerphone
        @Suppress("DEPRECATION")
        if (audioManager.isBluetoothScoOn) {
            audioManager.stopBluetoothSco()
            @Suppress("DEPRECATION")
            audioManager.isBluetoothScoOn = false
        }
    }

    /**
     * Wire the system route to a BT HFP device — modern API uses
     * setCommunicationDevice(scoDevice); legacy fires startBluetoothSco.
     */
    private fun applyBluetoothCallAudio(device: AudioDeviceInfo?) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            if (device == null) {
                Log.w(TAG, "applyBluetoothCallAudio: device is null on S+, skipping")
                return
            }
            val ok = audioManager.setCommunicationDevice(device)
            Log.d(TAG, "setCommunicationDevice(BT call, type=${device.type}) → $ok")
            return
        }
        @Suppress("DEPRECATION")
        audioManager.isSpeakerphoneOn = false
        @Suppress("DEPRECATION")
        audioManager.startBluetoothSco()
        @Suppress("DEPRECATION")
        audioManager.isBluetoothScoOn = true
    }

    /**
     * For A2DP-only routing, we don't tell the system anything
     * special: Android already routes STREAM_MUSIC / USAGE_MEDIA to
     * an active A2DP sink automatically.  We just need to ensure we
     * are NOT pinning a communication device that would force the
     * route somewhere else.
     */
    private fun applyBluetoothMedia() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            audioManager.clearCommunicationDevice()
        }
        @Suppress("DEPRECATION")
        audioManager.isSpeakerphoneOn = false
        @Suppress("DEPRECATION")
        if (audioManager.isBluetoothScoOn) {
            audioManager.stopBluetoothSco()
            @Suppress("DEPRECATION")
            audioManager.isBluetoothScoOn = false
        }
    }

    /**
     * Wire the system route to a wired 3.5mm headphone/headset.
     *
     * The kernel's hardware-priority policy snaps the communication-audio
     * plane to the wired plug automatically as long as we don't pin it
     * elsewhere — we just have to clear the conflicting hints:
     *   - speakerphone OFF (else the loudspeaker wins)
     *   - SCO OFF (else BT mic stays pinned and the HAL falls back to
     *     a `dummy` snd_device when wired + SCO are both requested)
     *   - clearCommunicationDevice on S+ (or, if the wired device shows
     *     up in availableCommunicationDevices, pin to it explicitly)
     */
    private fun applyWiredHeadphone(device: AudioDeviceInfo?) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            if (device != null) {
                val ok = audioManager.setCommunicationDevice(device)
                Log.d(TAG, "setCommunicationDevice(wired, type=${device.type}) → $ok")
                if (ok) return
            }
            // Wired plug isn't in availableCommunicationDevices (rare —
            // some ROMs only list earpiece/speaker/BT).  Fall through
            // and let the hardware-priority policy do its job.
            audioManager.clearCommunicationDevice()
        }
        @Suppress("DEPRECATION")
        audioManager.isSpeakerphoneOn = false
        @Suppress("DEPRECATION")
        if (audioManager.isBluetoothScoOn) {
            audioManager.stopBluetoothSco()
            @Suppress("DEPRECATION")
            audioManager.isBluetoothScoOn = false
        }
    }

    /**
     * Release any system-level routing we acquired.  Called on
     * session teardown.
     */
    fun release() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            try { audioManager.clearCommunicationDevice() } catch (_: Exception) {}
        }
        @Suppress("DEPRECATION")
        try {
            if (audioManager.isBluetoothScoOn) {
                audioManager.stopBluetoothSco()
                audioManager.isBluetoothScoOn = false
            }
        } catch (_: Exception) {}
        try {
            if (audioManager.mode != AudioManager.MODE_NORMAL) {
                audioManager.mode = AudioManager.MODE_NORMAL
            }
        } catch (_: Exception) {}
    }

    private fun logFinalState(route: Route) {
        @Suppress("DEPRECATION")
        Log.d(
            TAG,
            "[ROUTE] applied=${route.label} speakerOn=${audioManager.isSpeakerphoneOn} " +
                "scoOn=${audioManager.isBluetoothScoOn} mode=${audioManager.mode}",
        )
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            val cd = audioManager.communicationDevice
            Log.d(TAG, "[ROUTE] communicationDevice type=${cd?.type} name=${cd?.productName}")
        }
    }
}
