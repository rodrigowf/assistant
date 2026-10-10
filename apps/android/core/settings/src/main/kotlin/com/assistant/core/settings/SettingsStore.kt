package com.assistant.core.settings

import com.assistant.core.model.DeviceSettings
import com.assistant.core.model.ThemeMode
import com.assistant.core.model.SavedServer
import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.MutablePreferences
import androidx.datastore.preferences.core.PreferenceDataStoreFactory
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.preferencesDataStoreFile
import com.assistant.core.model.AudioOutput
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.filterNotNull
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * Device settings over the DataStore file `settings` (spec 14 §1.2, §2.10).
 *
 * - [settings] is `null` until DataStore has emitted once, so nobody acts on defaults
 *   (fixes inv04 B4); use [awaitLoaded].
 * - Same file and key names as the old app (see [SettingsKeys]); the lite app upgrades in place.
 * - On first load: legacy `saved_servers` is converted to `saved_servers_v2` (legacy key kept for
 *   rollback), and the old per-token `ws_resume_checkpoint:*` keys are purged (inv03 §2.2).
 * - Writes happen only on user actions, never per streamed event (A-8.14).
 */
class SettingsStore(
    private val dataStore: DataStore<Preferences>,
    scope: CoroutineScope = CoroutineScope(SupervisorJob() + Dispatchers.IO),
) {
    private val _settings = MutableStateFlow<DeviceSettings?>(null)
    val settings: StateFlow<DeviceSettings?> = _settings.asStateFlow()

    private val _pins = MutableStateFlow<Map<String, String>>(emptyMap())
    /** TOFU pins `host:port → spki sha256 (base64)`; readable synchronously (TLS callbacks). */
    val pins: StateFlow<Map<String, String>> = _pins.asStateFlow()

    init {
        scope.launch {
            val current = dataStore.data.first()
            if (needsMigration(current)) dataStore.edit { migrate(it) }
            dataStore.data.collect { prefs ->
                _pins.value = decodePins(prefs[SettingsKeys.SERVER_PINS])
                _settings.value = decode(prefs)
            }
        }
    }

    suspend fun awaitLoaded(): DeviceSettings = settings.filterNotNull().first()

    // ───────────── setters (clamps as the old app) ─────────────
    suspend fun setServerUrl(url: String) = edit { it[SettingsKeys.SERVER_URL] = url.trim() }
    suspend fun setAutoConnect(v: Boolean) = edit { it[SettingsKeys.AUTO_CONNECT] = v }
    suspend fun setEnableWakeWord(v: Boolean) = edit { it[SettingsKeys.ENABLE_WAKE_WORD] = v }
    suspend fun setTalkWord(v: String) = edit { it[SettingsKeys.TALK_WORD] = v }
    suspend fun setWakeWord(v: String) = edit { it[SettingsKeys.WAKE_WORD] = v }
    suspend fun setThemeMode(v: ThemeMode) = edit { it[SettingsKeys.THEME_MODE] = v.name }
    suspend fun setMicGainLevel(v: Float) = edit { it[SettingsKeys.MIC_GAIN_LEVEL] = v.coerceIn(0f, 1.5f) }
    suspend fun setWakeWordMicGainLevel(v: Float) = edit { it[SettingsKeys.WAKE_WORD_MIC_GAIN_LEVEL] = v.coerceIn(0f, 1.5f) }
    suspend fun setTalkSilenceSensitivity(v: Float) = edit { it[SettingsKeys.TALK_SILENCE_SENSITIVITY] = v.coerceIn(1f, 4f) }
    suspend fun setEchoDuckingGain(v: Float) = edit { it[SettingsKeys.ECHO_DUCKING_GAIN] = v.coerceIn(0f, 1f) }
    suspend fun setAudioOutput(v: AudioOutput) = edit { it[SettingsKeys.AUDIO_OUTPUT] = v.name }
    /** The `assistant_service_prefs` mirror is [ServicePrefs]' job (the caller writes both). */
    suspend fun setEnableButtonTrigger(v: Boolean) = edit { it[SettingsKeys.ENABLE_BUTTON_TRIGGER] = v }
    suspend fun setListPaneCollapsed(v: Boolean) = edit { it[SettingsKeys.LIST_PANE_COLLAPSED] = v }
    suspend fun setStayConnectedInBackground(v: Boolean) = edit { it[SettingsKeys.STAY_CONNECTED] = v }
    suspend fun setNotifyAgentTurns(v: Boolean) = edit { it[SettingsKeys.NOTIFY_AGENT_TURNS] = v }
    suspend fun setVoiceOverlayAnchor(v: String) = edit { it[SettingsKeys.VOICE_OVERLAY_ANCHOR] = v }

    /** Adds or replaces (same URL) a saved server. Blank label/URL is ignored (old behaviour). */
    suspend fun addSavedServer(label: String, url: String) {
        val l = label.trim(); val u = url.trim()
        if (l.isEmpty() || u.isEmpty()) return
        edit { p ->
            val list = readServers(p).filterNot { it.url == u } + SavedServer(l, u)
            p[SettingsKeys.SAVED_SERVERS_V2] = encodeServers(list)
        }
    }

    suspend fun removeSavedServer(url: String) = edit { p ->
        p[SettingsKeys.SAVED_SERVERS_V2] = encodeServers(readServers(p).filterNot { it.url == url })
    }

    // ───────────── connection state (not user settings) ─────────────

    /** Last orchestrator `localId` — a hint only, validated by `syncPool` (spec 12 §8.2). */
    suspend fun orchestratorLocalId(): String? =
        dataStore.data.first()[SettingsKeys.ORCHESTRATOR_LOCAL_ID]?.takeIf { it.isNotBlank() }

    suspend fun setOrchestratorLocalId(localId: String) = edit { it[SettingsKeys.ORCHESTRATOR_LOCAL_ID] = localId }
    suspend fun clearOrchestratorLocalId() = edit { it.remove(SettingsKeys.ORCHESTRATOR_LOCAL_ID) }

    // ───────────── TOFU pins ─────────────
    fun pinFor(hostPort: String): String? = _pins.value[hostPort]

    /** In-memory first (the next TLS handshake sees it), then persisted. */
    suspend fun savePin(hostPort: String, spki: String) {
        _pins.value = _pins.value + (hostPort to spki)
        edit { p -> p[SettingsKeys.SERVER_PINS] = encodePins(decodePins(p[SettingsKeys.SERVER_PINS]) + (hostPort to spki)) }
    }

    suspend fun removePin(hostPort: String) {
        _pins.value = _pins.value - hostPort
        edit { p -> p[SettingsKeys.SERVER_PINS] = encodePins(decodePins(p[SettingsKeys.SERVER_PINS]) - hostPort) }
    }

    private suspend fun edit(block: (MutablePreferences) -> Unit) {
        dataStore.edit { block(it) }
    }

    companion object {
        private val json = Json { ignoreUnknownKeys = true }

        @Volatile private var processStore: DataStore<Preferences>? = null

        /**
         * The process-wide DataStore over `files/datastore/settings.preferences_pb` — the same file the
         * old app's `preferencesDataStore(name = "settings")` delegate used. Only one instance may
         * exist per file per process (DataStore enforces it), hence the singleton.
         */
        fun dataStore(context: Context): DataStore<Preferences> = processStore ?: synchronized(this) {
            processStore ?: PreferenceDataStoreFactory.create(
                produceFile = { context.applicationContext.preferencesDataStoreFile(SettingsKeys.FILE_NAME) },
            ).also { processStore = it }
        }

        fun create(context: Context, scope: CoroutineScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)) =
            SettingsStore(dataStore(context), scope)

        internal fun needsMigration(p: Preferences): Boolean =
            (p[SettingsKeys.SAVED_SERVERS_V2] == null && !p[SettingsKeys.SAVED_SERVERS_LEGACY].isNullOrEmpty()) ||
                p.asMap().keys.any { it.name.startsWith(SettingsKeys.LEGACY_CHECKPOINT_PREFIX) }

        internal fun migrate(p: MutablePreferences) {
            if (p[SettingsKeys.SAVED_SERVERS_V2] == null) {
                val legacy = decodeLegacyServers(p[SettingsKeys.SAVED_SERVERS_LEGACY])
                if (legacy.isNotEmpty()) p[SettingsKeys.SAVED_SERVERS_V2] = encodeServers(legacy)
            }
            p.asMap().keys.filter { it.name.startsWith(SettingsKeys.LEGACY_CHECKPOINT_PREFIX) }.forEach { p.remove(it) }
        }

        fun decode(p: Preferences): DeviceSettings {
            val d = DeviceSettings()
            return DeviceSettings(
                serverUrl = p[SettingsKeys.SERVER_URL] ?: d.serverUrl,
                savedServers = readServers(p),
                autoConnect = p[SettingsKeys.AUTO_CONNECT] ?: d.autoConnect,
                enableWakeWord = p[SettingsKeys.ENABLE_WAKE_WORD] ?: d.enableWakeWord,
                talkWord = p[SettingsKeys.TALK_WORD] ?: d.talkWord,
                wakeWord = p[SettingsKeys.WAKE_WORD] ?: d.wakeWord,
                themeMode = ThemeMode.entries.firstOrNull { it.name == p[SettingsKeys.THEME_MODE] } ?: DeviceSettings().themeMode, // D3 dark-first default
                micGainLevel = p[SettingsKeys.MIC_GAIN_LEVEL] ?: d.micGainLevel,
                wakeWordMicGainLevel = p[SettingsKeys.WAKE_WORD_MIC_GAIN_LEVEL] ?: d.wakeWordMicGainLevel,
                talkSilenceSensitivity = p[SettingsKeys.TALK_SILENCE_SENSITIVITY] ?: d.talkSilenceSensitivity,
                echoDuckingGain = p[SettingsKeys.ECHO_DUCKING_GAIN] ?: d.echoDuckingGain,
                audioOutput = AudioOutput.entries.firstOrNull { it.name == p[SettingsKeys.AUDIO_OUTPUT] } ?: AudioOutput.AUTO,
                enableButtonTrigger = p[SettingsKeys.ENABLE_BUTTON_TRIGGER] ?: d.enableButtonTrigger,
                listPaneCollapsed = p[SettingsKeys.LIST_PANE_COLLAPSED] ?: d.listPaneCollapsed,
                stayConnectedInBackground = p[SettingsKeys.STAY_CONNECTED] ?: d.stayConnectedInBackground,
                notifyAgentTurns = p[SettingsKeys.NOTIFY_AGENT_TURNS] ?: d.notifyAgentTurns,
                voiceOverlayAnchor = p[SettingsKeys.VOICE_OVERLAY_ANCHOR] ?: d.voiceOverlayAnchor,
            )
        }

        /** v2 when present, else the legacy encoding (before the one-time migration ran). */
        private fun readServers(p: Preferences): List<SavedServer> =
            p[SettingsKeys.SAVED_SERVERS_V2]?.let(::decodeServers) ?: decodeLegacyServers(p[SettingsKeys.SAVED_SERVERS_LEGACY])

        /** Old wire format `label\turl|…` (`SettingsRepository.kt:313-323`). */
        fun decodeLegacyServers(raw: String?): List<SavedServer> {
            if (raw.isNullOrEmpty()) return emptyList()
            return raw.split("|").mapNotNull { entry ->
                val parts = entry.split("\t", limit = 2)
                if (parts.size == 2 && parts[0].isNotBlank() && parts[1].isNotBlank()) SavedServer(parts[0], parts[1]) else null
            }
        }

        fun encodeServers(list: List<SavedServer>): String = buildJsonArray {
            list.forEach { s -> add(buildJsonObject { put("label", s.label); put("url", s.url) }) }
        }.toString()

        fun decodeServers(raw: String): List<SavedServer> = try {
            (json.parseToJsonElement(raw) as? JsonArray).orEmpty().mapNotNull { e ->
                val o = e as? JsonObject ?: return@mapNotNull null
                val l = (o["label"] as? JsonPrimitive)?.content
                val u = (o["url"] as? JsonPrimitive)?.content
                if (l.isNullOrBlank() || u.isNullOrBlank()) null else SavedServer(l, u)
            }
        } catch (_: Exception) {
            emptyList()
        }

        private fun encodePins(m: Map<String, String>): String = buildJsonObject { m.forEach { (k, v) -> put(k, v) } }.toString()

        private fun decodePins(raw: String?): Map<String, String> = try {
            if (raw == null) emptyMap() else (json.parseToJsonElement(raw) as JsonObject)
                .mapValues { (it.value as JsonPrimitive).content }
        } catch (_: Exception) {
            emptyMap()
        }
    }
}
