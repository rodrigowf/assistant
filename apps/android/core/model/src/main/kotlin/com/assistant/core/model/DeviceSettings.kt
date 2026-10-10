package com.assistant.core.model

/** Theme choice (device-local, spec 12 §8.2). Stored by enum name, as the old app. */
enum class ThemeMode { SYSTEM, LIGHT, DARK }

/** A user-saved server (Settings → Servers). */
data class SavedServer(val label: String, val url: String)

/**
 * Device-local settings (spec 12 §8.2). Field defaults are the old app's (`data/Models.kt:417-435`).
 * The removed `speaker_volume_level` slider is a no-op (inv04 B9) and is not surfaced.
 * Persisted by `:core:settings` `SettingsStore` (same DataStore keys as the old app).
 */
data class DeviceSettings(
    val serverUrl: String = DEFAULT_SERVER_URL,
    val savedServers: List<SavedServer> = emptyList(),
    val autoConnect: Boolean = true,
    val enableWakeWord: Boolean = true,
    /** Comma-separated; triggers one turn-based voice message. */
    val talkWord: String = "my friend",
    /** Comma-separated; triggers a realtime voice conversation. */
    val wakeWord: String = "wake up",
    val themeMode: ThemeMode = ThemeMode.DARK, // D3: dark-first; System and Light are opt-in
    /** 0.0–1.5; voice session mic gain. */
    val micGainLevel: Float = 1.0f,
    /** 0.0–1.5; scales the wake-word RMS gate. */
    val wakeWordMicGainLevel: Float = 1.0f,
    /** 1.0–4.0; talk-capture end-of-utterance VAD multiplier. */
    val talkSilenceSensitivity: Float = 2.0f,
    /** 0.0–1.0; mic gain while the assistant speaks (duck to 5 %, not mute). */
    val echoDuckingGain: Float = 0.05f,
    val audioOutput: AudioOutput = AudioOutput.AUTO,
    val enableButtonTrigger: Boolean = false,
    /** Expanded layout list pane (spec 14 §2.4). New key. */
    val listPaneCollapsed: Boolean = false,
    /** Keep the orchestrator socket in the background (spec 14 §2.5, Q7: off). New key. */
    val stayConnectedInBackground: Boolean = false,
    /**
     * A notification when an agent session finishes a turn (spec 12 §3.7, §8.2). Off by default;
     * turning it on asks for POST_NOTIFICATIONS (API 33+). New key.
     */
    val notifyAgentTurns: Boolean = false,
    /**
     * Where the floating voice controls snap to (`top-left` … `bottom-right`, as the web app's
     * `voiceOverlayAnchor` pref). New key; bottom-center is where the dock sits on the Archie view.
     */
    val voiceOverlayAnchor: String = "bottom-center",
) {
    val isDefaultServer: Boolean get() = serverUrl == DEFAULT_SERVER_URL

    companion object {
        /** The Jetson on the LAN, as today (spec 14 §2.10). */
        const val DEFAULT_SERVER_URL = "ws://192.168.0.200:80"
    }
}
