package com.assistant.peripheral.settings

import android.app.Application
import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.floatPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import com.assistant.peripheral.data.AppSettings
import com.assistant.peripheral.data.AudioOutput
import com.assistant.peripheral.data.SavedServer
import com.assistant.peripheral.data.ThemeMode
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.filterNotNull
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch

/**
 * Owns DataStore I/O for app settings. The single source of truth for the
 * persisted preferences that drive the UI and the connection layer.
 *
 * Replaces the inline `dataStore.data.collect { ... }` block in
 * `AssistantViewModel.init` (HEAD `b6f8303`, AssistantViewModel.kt:334-433)
 * and the 20-odd setter methods that wrap `dataStore.edit { ... }` at
 * L1863-2032.
 *
 * Increment 1 contract (refactor plan §2):
 *
 *   1. [settings] is `StateFlow<AppSettings?>` — null until DataStore emits.
 *      Readers that need the URL must call [awaitLoaded] (or filter null);
 *      the `settingsLoaded: CompletableDeferred<Unit>` band-aid in the
 *      ViewModel goes away because this type makes the load gate explicit.
 *
 *   2. The persisted `ORCHESTRATOR_LOCAL_ID` is exposed via
 *      [persistedOrchestratorLocalId] / [persistOrchestratorLocalId] /
 *      [clearOrchestratorLocalId]. It's NOT part of [AppSettings] —
 *      it's connection state, not user settings. The ViewModel reads it
 *      once on first emission to restore the orchestrator bucket's local_id
 *      before opening the WS, and writes it from the recovery / SessionStarted
 *      paths.
 *
 *   3. Every public setter method on [AssistantViewModel] (`updateServerUrl`,
 *      `updateThemeMode`, etc.) delegates to a `suspend fun` on this
 *      repository. The ViewModel's public surface is unchanged.
 *
 *   4. Wire-format keys (`server_url`, `turn_talk_word`, `realtime_wake_word`,
 *      `orchestrator_local_id`, …) are pinned. Renames would force a DataStore
 *      migration — out of scope.
 *
 * Constructor takes an explicit [DataStore] for testability; the default
 * factory uses the per-Application singleton DataStore created at the
 * file-level `dataStore` property (the same one the ViewModel used).
 */
class SettingsRepository(
    private val application: Application,
    private val dataStore: DataStore<Preferences>,
    scope: CoroutineScope? = null
) {

    private val ownScope: CoroutineScope = scope ?: CoroutineScope(Dispatchers.IO + SupervisorJob())

    private val _settings = MutableStateFlow<AppSettings?>(null)

    /**
     * `null` until DataStore has emitted at least once. After the first
     * emission, mirrors the persisted (or default-when-absent) `AppSettings`.
     *
     * Most readers should not poll `.value` directly — they should either
     * collect this flow or call [awaitLoaded]. The nullable type is a
     * type-level reminder that a "default before load" read would be a bug.
     */
    val settings: StateFlow<AppSettings?> = _settings.asStateFlow()

    init {
        ownScope.launch {
            dataStore.data.collect { preferences ->
                _settings.value = preferences.toAppSettings()
            }
        }
    }

    /**
     * Suspends until the first non-null [AppSettings] is available, then
     * returns it. Subsequent reads are immediate (the flow stays non-null
     * once loaded).
     *
     * Use this from any code path that needs `serverUrl` before the WS
     * opens. Replaces the `settingsLoaded.await()` band-aid in
     * `AssistantViewModel.connect()` at HEAD AssistantViewModel.kt:1121.
     */
    suspend fun awaitLoaded(): AppSettings = settings.filterNotNull().first()

    // ----------------------------------------------------------------
    // Orchestrator local-id (connection state, not user settings)
    // ----------------------------------------------------------------

    /**
     * Returns the persisted orchestrator local_id, or null if absent / blank.
     * Pinned semantics from HEAD AssistantViewModel.kt:350
     * (`?.takeIf { it.isNotBlank() }`).
     */
    suspend fun persistedOrchestratorLocalId(): String? {
        val raw = dataStore.data.first()[ORCHESTRATOR_LOCAL_ID]
        return raw?.takeIf { it.isNotBlank() }
    }

    /** Persist the orchestrator local_id so cold start can reattach. */
    suspend fun persistOrchestratorLocalId(localId: String) {
        dataStore.edit { it[ORCHESTRATOR_LOCAL_ID] = localId }
    }

    /** Drop the persisted orchestrator local_id (e.g. on serverUrlChanged). */
    suspend fun clearOrchestratorLocalId() {
        dataStore.edit { it.remove(ORCHESTRATOR_LOCAL_ID) }
    }

    // ----------------------------------------------------------------
    // WebSocket resume protocol — per-session (stream_id, seq) checkpoint
    //
    // The backend stamps every broadcast event with a monotonic ``seq``
    // within a ``stream_id`` (changes when the SDK subprocess (re)connects).
    // We persist the latest pair per session so a reconnecting socket can
    // ask the backend to replay events newer than that seq.  Older
    // backends without the protocol simply never emit the fields, and
    // the client falls back to its prior behaviour (full REST refetch).
    //
    // Keyed by the session's stable ``local_id`` to survive across app
    // restarts and (for the orchestrator) cold starts.
    // ----------------------------------------------------------------

    /** Read the persisted checkpoint for a session, or null if none/malformed. */
    suspend fun readResumeCheckpoint(localId: String): ResumeCheckpoint? {
        if (localId.isBlank()) return null
        val raw = dataStore.data.first()[resumeCheckpointKey(localId)] ?: return null
        // Stored as "<streamId>|<seq>" — compact and unambiguous since
        // the stream id format never contains a '|'.
        val parts = raw.split('|', limit = 2)
        if (parts.size != 2) return null
        val seq = parts[1].toIntOrNull() ?: return null
        return ResumeCheckpoint(streamId = parts[0], seq = seq)
    }

    /**
     * Persist a checkpoint for [localId].  Monotonic within the same
     * [ResumeCheckpoint.streamId] — a write that would move seq backward
     * is silently dropped (defensive against out-of-order delivery).  A
     * different stream id always overwrites (the old checkpoint is
     * meaningless after a backend restart).
     */
    suspend fun writeResumeCheckpoint(localId: String, checkpoint: ResumeCheckpoint) {
        if (localId.isBlank()) return
        val existing = readResumeCheckpoint(localId)
        if (
            existing != null &&
            existing.streamId == checkpoint.streamId &&
            existing.seq >= checkpoint.seq
        ) {
            return
        }
        dataStore.edit {
            it[resumeCheckpointKey(localId)] = "${checkpoint.streamId}|${checkpoint.seq}"
        }
    }

    /** Drop a session's checkpoint (e.g. after [WebSocketEvent.ReplayOverflow]). */
    suspend fun clearResumeCheckpoint(localId: String) {
        if (localId.isBlank()) return
        dataStore.edit { it.remove(resumeCheckpointKey(localId)) }
    }

    private fun resumeCheckpointKey(localId: String) =
        stringPreferencesKey("ws_resume_checkpoint:$localId")

    /** Local mirror of WebSocketMessage.ResumeCheckpointSnapshot, decoupled
     *  from the network layer so settings code doesn't pull in JSON deps. */
    data class ResumeCheckpoint(val streamId: String, val seq: Int)

    // ----------------------------------------------------------------
    // Setters — one suspend fun per ViewModel setter on HEAD
    // ----------------------------------------------------------------

    suspend fun updateServerUrl(url: String) {
        dataStore.edit { it[SERVER_URL] = url }
    }

    suspend fun addSavedServer(label: String, url: String) {
        val cleanLabel = label.trim()
        val cleanUrl = url.trim()
        if (cleanLabel.isEmpty() || cleanUrl.isEmpty()) return
        dataStore.edit { preferences ->
            val existing = decodeSavedServers(preferences[SAVED_SERVERS])
            // Replace any entry with the same url, else append.
            val updated = existing.filterNot { it.url == cleanUrl } + SavedServer(cleanLabel, cleanUrl)
            preferences[SAVED_SERVERS] = encodeSavedServers(updated)
        }
    }

    suspend fun removeSavedServer(url: String) {
        dataStore.edit { preferences ->
            val existing = decodeSavedServers(preferences[SAVED_SERVERS])
            val updated = existing.filterNot { it.url == url }
            preferences[SAVED_SERVERS] = encodeSavedServers(updated)
        }
    }

    suspend fun selectSavedServer(server: SavedServer) {
        dataStore.edit { it[SERVER_URL] = server.url }
    }

    suspend fun updateThemeMode(mode: ThemeMode) {
        dataStore.edit { it[THEME_MODE] = mode.name }
    }

    suspend fun updateAutoConnect(enabled: Boolean) {
        dataStore.edit { it[AUTO_CONNECT] = enabled }
    }

    suspend fun updateMicGainLevel(level: Float) {
        dataStore.edit { it[MIC_GAIN_LEVEL] = level.coerceIn(0.0f, 1.5f) }
    }

    suspend fun updateEchoDuckingGain(gain: Float) {
        dataStore.edit { it[ECHO_DUCKING_GAIN] = gain.coerceIn(0.0f, 1.0f) }
    }

    suspend fun updateWakeWordMicGainLevel(level: Float) {
        dataStore.edit { it[WAKE_WORD_MIC_GAIN_LEVEL] = level.coerceIn(0.0f, 1.5f) }
    }

    suspend fun updateAudioOutput(output: AudioOutput) {
        dataStore.edit { it[AUDIO_OUTPUT] = output.name }
    }

    /**
     * Speaker volume is the only setter with a side-effect beyond DataStore:
     * it pushes the level through to the system AudioManager so the change
     * applies immediately, not just on next session. Preserved from HEAD
     * AssistantViewModel.updateSpeakerVolumeLevel at L1973-1985.
     */
    suspend fun updateSpeakerVolumeLevel(level: Float) {
        val clamped = level.coerceIn(0.0f, 1.5f)
        dataStore.edit { it[SPEAKER_VOLUME_LEVEL] = clamped }
        val audioManager = application.getSystemService(Context.AUDIO_SERVICE) as android.media.AudioManager
        val maxVolume = audioManager.getStreamMaxVolume(android.media.AudioManager.STREAM_MUSIC)
        val newVolume = (clamped * maxVolume).toInt().coerceIn(0, maxVolume)
        audioManager.setStreamVolume(android.media.AudioManager.STREAM_MUSIC, newVolume, 0)
    }

    /**
     * Mirrors the persisted flag into the side-prefs file the
     * ButtonAccessibilityService reads (it can't hold a Context ref).
     * Preserved from HEAD AssistantViewModel.updateEnableButtonTrigger at
     * L1987-1996 — same `assistant_service_prefs` key.
     */
    suspend fun updateEnableButtonTrigger(enabled: Boolean) {
        dataStore.edit { it[ENABLE_BUTTON_TRIGGER] = enabled }
        application.getSharedPreferences("assistant_service_prefs", Context.MODE_PRIVATE)
            .edit().putBoolean("button_trigger_enabled", enabled).apply()
    }

    suspend fun updateEnableWakeWord(enabled: Boolean) {
        dataStore.edit { it[ENABLE_WAKE_WORD] = enabled }
    }

    suspend fun updateTalkWord(word: String) {
        dataStore.edit { it[TALK_WORD] = word }
    }

    suspend fun updateWakeWord(word: String) {
        dataStore.edit { it[WAKE_WORD] = word }
    }

    // ----------------------------------------------------------------
    // Decoding — preferences → AppSettings
    // ----------------------------------------------------------------

    private fun Preferences.toAppSettings(): AppSettings = AppSettings(
        serverUrl = this[SERVER_URL] ?: AppSettings().serverUrl,
        savedServers = decodeSavedServers(this[SAVED_SERVERS]),
        autoConnect = this[AUTO_CONNECT] ?: AppSettings().autoConnect,
        enableWakeWord = this[ENABLE_WAKE_WORD] ?: AppSettings().enableWakeWord,
        talkWord = this[TALK_WORD] ?: AppSettings().talkWord,
        wakeWord = this[WAKE_WORD] ?: AppSettings().wakeWord,
        themeMode = try {
            ThemeMode.valueOf(this[THEME_MODE] ?: ThemeMode.SYSTEM.name)
        } catch (e: Exception) {
            ThemeMode.SYSTEM
        },
        micGainLevel = this[MIC_GAIN_LEVEL] ?: 1.0f,
        wakeWordMicGainLevel = this[WAKE_WORD_MIC_GAIN_LEVEL] ?: 1.0f,
        speakerVolumeLevel = this[SPEAKER_VOLUME_LEVEL] ?: 1.0f,
        echoDuckingGain = this[ECHO_DUCKING_GAIN] ?: AppSettings().echoDuckingGain,
        audioOutput = AudioOutput.fromString(this[AUDIO_OUTPUT]),
        enableButtonTrigger = this[ENABLE_BUTTON_TRIGGER] ?: false
    )

    // Saved servers wire format — pinned from HEAD AssistantViewModel.kt:322-332.
    // "label\turl|label\turl|..." — no quoting needed since labels/URLs never
    // contain tab or pipe in practice.
    private fun encodeSavedServers(servers: List<SavedServer>): String =
        servers.joinToString("|") { "${it.label}\t${it.url}" }

    private fun decodeSavedServers(raw: String?): List<SavedServer> {
        if (raw.isNullOrEmpty()) return emptyList()
        return raw.split("|").mapNotNull { entry ->
            val parts = entry.split("\t", limit = 2)
            if (parts.size == 2 && parts[0].isNotBlank() && parts[1].isNotBlank())
                SavedServer(parts[0], parts[1]) else null
        }
    }

    companion object {
        // Wire-format keys — pinned from HEAD AssistantViewModel.PreferenceKeys
        // (AssistantViewModel.kt:296-318). Renaming any of these would force a
        // DataStore migration. Out of scope.
        private val SERVER_URL = stringPreferencesKey("server_url")
        private val AUTO_CONNECT = booleanPreferencesKey("auto_connect")
        private val ENABLE_WAKE_WORD = booleanPreferencesKey("enable_wake_word")
        private val TALK_WORD = stringPreferencesKey("turn_talk_word")
        private val WAKE_WORD = stringPreferencesKey("realtime_wake_word")
        private val THEME_MODE = stringPreferencesKey("theme_mode")
        private val MIC_GAIN_LEVEL = floatPreferencesKey("mic_gain_level")
        private val WAKE_WORD_MIC_GAIN_LEVEL = floatPreferencesKey("wake_word_mic_gain_level")
        private val SPEAKER_VOLUME_LEVEL = floatPreferencesKey("speaker_volume_level")
        private val ECHO_DUCKING_GAIN = floatPreferencesKey("echo_ducking_gain")
        private val AUDIO_OUTPUT = stringPreferencesKey("audio_output")
        private val ENABLE_BUTTON_TRIGGER = booleanPreferencesKey("enable_button_trigger")
        private val SAVED_SERVERS = stringPreferencesKey("saved_servers")
        private val ORCHESTRATOR_LOCAL_ID = stringPreferencesKey("orchestrator_local_id")

        // The single DataStore instance the ViewModel used to own. Exposed
        // as an extension property on Context (matching the original) so the
        // SettingsRepository factory can grab it. Name="settings" matches the
        // file the original `Context.dataStore` delegate in
        // `AssistantViewModel.kt` pointed at — **only one delegate may exist
        // per file name** (DataStore enforces this at runtime), so the
        // ViewModel's delegate is removed and this is the only one.
        internal val Context.settingsDataStore: DataStore<Preferences> by preferencesDataStore(name = "settings")

        /**
         * Factory that constructs a repository against the per-Application
         * DataStore singleton. The ViewModel uses this.
         */
        fun create(application: Application): SettingsRepository =
            SettingsRepository(application, application.settingsDataStore)
    }
}
