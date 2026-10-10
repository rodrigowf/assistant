package com.assistant.core.settings

import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.floatPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey

/**
 * Keys of the DataStore file `settings`. The legacy block is **wire format**: exactly the names of
 * `old/settings/SettingsRepository.kt:325-343`, so the lite app inherits the A300M's settings in
 * place (spec 14 §1.2, §5.7). Never rename them.
 */
object SettingsKeys {
    const val FILE_NAME = "settings"

    // ── legacy (old app) ──
    val SERVER_URL = stringPreferencesKey("server_url")
    val AUTO_CONNECT = booleanPreferencesKey("auto_connect")
    val ENABLE_WAKE_WORD = booleanPreferencesKey("enable_wake_word")
    val TALK_WORD = stringPreferencesKey("turn_talk_word")
    val WAKE_WORD = stringPreferencesKey("realtime_wake_word")
    val THEME_MODE = stringPreferencesKey("theme_mode")
    val MIC_GAIN_LEVEL = floatPreferencesKey("mic_gain_level")
    val WAKE_WORD_MIC_GAIN_LEVEL = floatPreferencesKey("wake_word_mic_gain_level")
    val TALK_SILENCE_SENSITIVITY = floatPreferencesKey("talk_silence_sensitivity")
    /** Read-never (no-op slider, inv04 B9); listed so tests prove it is tolerated. */
    val SPEAKER_VOLUME_LEVEL = floatPreferencesKey("speaker_volume_level")
    val ECHO_DUCKING_GAIN = floatPreferencesKey("echo_ducking_gain")
    val AUDIO_OUTPUT = stringPreferencesKey("audio_output")
    val ENABLE_BUTTON_TRIGGER = booleanPreferencesKey("enable_button_trigger")
    /** `label\turl|label\turl|…`; read once and converted to [SAVED_SERVERS_V2] (inv03 §2.1). */
    val SAVED_SERVERS_LEGACY = stringPreferencesKey("saved_servers")
    val ORCHESTRATOR_LOCAL_ID = stringPreferencesKey("orchestrator_local_id")
    /** Prefix of the old per-token checkpoint keys (inv03 §2.2); purged on load, never written. */
    const val LEGACY_CHECKPOINT_PREFIX = "ws_resume_checkpoint:"

    /** Every legacy key name, for the migration test. */
    val LEGACY_NAMES = listOf(
        "server_url", "saved_servers", "auto_connect", "enable_wake_word", "turn_talk_word",
        "realtime_wake_word", "theme_mode", "mic_gain_level", "wake_word_mic_gain_level",
        "talk_silence_sensitivity", "speaker_volume_level", "echo_ducking_gain", "audio_output",
        "enable_button_trigger", "orchestrator_local_id",
    )

    // ── new ──
    /** JSON `[{"label":…,"url":…}]` (spec 14 §2.10). */
    val SAVED_SERVERS_V2 = stringPreferencesKey("saved_servers_v2")
    /** JSON `{"host:port": "<spki sha256 base64>"}` (TOFU pins, spec 14 §4.3). */
    val SERVER_PINS = stringPreferencesKey("server_pins_v1")
    val LIST_PANE_COLLAPSED = booleanPreferencesKey("list_pane_collapsed")
    val STAY_CONNECTED = booleanPreferencesKey("stay_connected_background")
    /** Settings → Notifications → "Agent session finished" (spec 12 §8.2). */
    val NOTIFY_AGENT_TURNS = booleanPreferencesKey("notify_agent_turns")
    val VOICE_OVERLAY_ANCHOR = stringPreferencesKey("voice_overlay_anchor")
}
