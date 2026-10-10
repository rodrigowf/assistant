package com.assistant.archie.feature.settings

import com.assistant.core.model.AudioOutput
import com.assistant.core.model.DeviceSettings
import com.assistant.core.model.ThemeMode
import com.assistant.core.settings.SettingsStore
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/**
 * This device's settings (spec 12 §8.2: never sent to the backend). Every write goes through
 * `:core:settings` [SettingsStore] (the old DataStore keys) and confirms with "Saved" (IA §7). The
 * voice stack observes the same store, so values pass through unchanged (inv04 §4); this class
 * never rescales them.
 */
class DeviceSettingsModel(
    private val store: SettingsStore,
    private val appearanceStore: AppearanceStore,
    private val platform: DevicePlatform,
    private val messages: SettingsMessages,
    private val scope: CoroutineScope,
) {
    val settings: StateFlow<DeviceSettings?> get() = store.settings
    val appearance: StateFlow<Appearance> get() = appearanceStore.appearance

    private val _speaker = MutableStateFlow(platform.speakerLevel())
    /** Archie's voice volume, read from the system (never stored: fixes the drift of inv03 §8 bug 13). */
    val speakerLevel: StateFlow<Float?> = _speaker.asStateFlow()

    fun refreshSpeaker() { _speaker.value = platform.speakerLevel() }

    private fun write(block: suspend () -> Unit) {
        scope.launch {
            try {
                block()
                messages.saved()
            } catch (e: Exception) {
                messages.post(SettingsMessage(e.message ?: "Couldn't save", error = true))
            }
        }
    }

    // Audio
    fun setMicGain(v: Float) = write { store.setMicGainLevel(Ranges.LEVEL.snap(v)) }
    fun setEchoDucking(v: Float) = write { store.setEchoDuckingGain(Ranges.DUCK.snap(v)) }
    fun setAudioOutput(v: AudioOutput) = write { store.setAudioOutput(v) }
    fun setSpeakerLevel(v: Float) = write {
        platform.setSpeakerLevel(v.coerceIn(0f, 1f))
        refreshSpeaker()
    }

    // Wake word & triggers
    fun setWakeWordEnabled(v: Boolean) = write { store.setEnableWakeWord(v) }

    /** Blank phrases are refused (the old app hid Save for blank text). */
    fun setTalkPhrases(v: String): Boolean {
        if (phrases(v).isEmpty()) return false
        write { store.setTalkWord(v.trim()) }
        return true
    }

    fun setWakePhrases(v: String): Boolean {
        if (phrases(v).isEmpty()) return false
        write { store.setWakeWord(v.trim()) }
        return true
    }

    fun setWakeSensitivity(v: Float) = write { store.setWakeWordMicGainLevel(Ranges.LEVEL.snap(v)) }
    fun setTalkAutoStop(v: Float) = write { store.setTalkSilenceSensitivity(Ranges.TALK_SILENCE.snap(v)) }

    // Connection
    fun setAutoConnect(v: Boolean) = write { store.setAutoConnect(v) }
    fun setStayConnected(v: Boolean) = write { store.setStayConnectedInBackground(v) }

    // Notifications
    fun setNotifyAgentTurns(v: Boolean) = write { store.setNotifyAgentTurns(v) }

    // Appearance
    fun setTheme(v: ThemeMode) = write { store.setThemeMode(v) }
    fun setTextSize(v: TextSize) = write { appearanceStore.setTextSize(v) }
    fun setReduceMotion(v: Boolean) = write { appearanceStore.setReduceMotion(v) }
}
