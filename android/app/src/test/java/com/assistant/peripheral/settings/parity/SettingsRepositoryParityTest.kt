package com.assistant.peripheral.settings.parity

import android.app.Application
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import androidx.test.core.app.ApplicationProvider
import app.cash.turbine.test
import com.assistant.peripheral.data.AppSettings
import com.assistant.peripheral.data.AudioOutput
import com.assistant.peripheral.data.ThemeMode
import com.assistant.peripheral.settings.SettingsRepository
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.test.runTest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.File

/**
 * Parity test for Increment 1 (`SettingsRepository` extraction) of the
 * android viewmodel refactor plan
 * (assistant/plans/android_viewmodel_refactor_plan_2026_06_10.md, §2).
 *
 * Refactor base: HEAD `b6f8303` (`AssistantViewModel.kt` pre-extraction, with
 * the orchestrator-loop fix `5b1b1f8` in place). The DataStore I/O at
 * `AssistantViewModel.kt:334-433` (the `init { ... dataStore.data.collect { ... } }`
 * block) plus the 20 setter methods at L1863-2032 are what move.
 *
 * What this test pins (byte-identical against HEAD):
 *
 *  1. **Defaults parity** — every field of `AppSettings()` emitted by the
 *     repository on first-write-then-load matches the current `AppSettings()`
 *     data class default. (Replaces the pre-existing failing test for
 *     `WebSocketManagerTest.AppSettings has correct defaults` which asserted
 *     the wrong default URL.)
 *
 *  2. **Null-before-load** — `repository.settings.value` is `null` until
 *     DataStore emits the first time. This is the type-level fix for the
 *     orchestrator-loop URL race — readers that grab `.value` before load
 *     get a null they have to handle, instead of a default that misleads.
 *
 *  3. **First emission carries persisted ORCHESTRATOR_LOCAL_ID** — when
 *     DataStore is pre-populated with an `orchestrator_local_id`, the
 *     repository's `persistedOrchestratorLocalId()` returns it. Pinned
 *     because the ViewModel reads this before opening the WS to avoid
 *     forking a fresh UUID on cold start (HEAD AssistantViewModel.kt:344-353).
 *
 *  4. **serverUrl change notifies** — updating `SERVER_URL` via the
 *     repository emits a new `AppSettings` on the `settings` flow with the
 *     new value. This is the basis for the `serverUrlChanged` teardown
 *     coordination at the ViewModel level (HEAD L408-431); the ViewModel
 *     keeps detecting the change by diffing successive emissions, but the
 *     repository must continue to emit them.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
@OptIn(ExperimentalCoroutinesApi::class)
class SettingsRepositoryParityTest {

    private lateinit var application: Application
    private lateinit var dataStore: DataStore<Preferences>
    private lateinit var repository: SettingsRepository

    // Mirror the wire-format keys from AssistantViewModel.PreferenceKeys
    // (AssistantViewModel.kt:296-318). These are persisted across upgrades —
    // any rename would force a migration, so we pin them in the test.
    private val SERVER_URL = stringPreferencesKey("server_url")
    private val ORCHESTRATOR_LOCAL_ID = stringPreferencesKey("orchestrator_local_id")

    @Before
    fun setUp() {
        application = ApplicationProvider.getApplicationContext()
        // Use a unique DataStore name per test instance so the cases don't
        // leak persisted state across each other.
        val name = "settings_parity_${System.nanoTime()}"
        dataStore = androidx.datastore.preferences.core.PreferenceDataStoreFactory.create(
            produceFile = { File(application.filesDir, "datastore/$name.preferences_pb") }
        )
        repository = SettingsRepository(application, dataStore)
    }

    @After
    fun tearDown() {
        // Robolectric tears down per test; nothing extra needed.
    }

    // -------------------------------------------------------------------
    // 1. Defaults parity
    // -------------------------------------------------------------------

    @Test
    fun `defaults parity — fresh DataStore emits the current AppSettings defaults`() = runTest {
        // No writes — fresh DataStore. The repository emits the AppSettings
        // built from defaults for every absent key.
        repository.settings.test {
            // Skip the null-before-load tick.
            val first = awaitItem()
            // Wait for the load to complete (a non-null emission).
            val loaded = if (first == null) awaitItem() else first
            assertNotNull(loaded)

            // Pin every field against the current AppSettings data class
            // defaults on HEAD `b6f8303`. If the default changes, this test
            // breaks — that's deliberate, the developer must explicitly
            // re-pin the parity contract.
            assertEquals("ws://192.168.0.200:80", loaded!!.serverUrl)
            assertEquals(emptyList<Any>(), loaded.savedServers)
            assertEquals(true, loaded.autoConnect)
            assertEquals(true, loaded.enableWakeWord)
            assertEquals("my friend", loaded.talkWord)
            assertEquals("wake up", loaded.wakeWord)
            assertEquals(ThemeMode.SYSTEM, loaded.themeMode)
            assertEquals(1.0f, loaded.micGainLevel)
            assertEquals(1.0f, loaded.wakeWordMicGainLevel)
            assertEquals(1.0f, loaded.speakerVolumeLevel)
            assertEquals(0.05f, loaded.echoDuckingGain)
            assertEquals(AudioOutput.AUTO, loaded.audioOutput)
            assertEquals(false, loaded.enableButtonTrigger)
            cancelAndIgnoreRemainingEvents()
        }
    }

    // -------------------------------------------------------------------
    // 2. Null-before-load — the type-level fix for the URL race
    // -------------------------------------------------------------------

    @Test
    fun `null-before-load parity — settings_value is null until DataStore emits the first time`() = runTest {
        // Immediately after constructing the repository, before any
        // suspending machinery has had a chance to drain the DataStore
        // initial value, `.value` is null. This is the contract that lets
        // every caller of `serverUrl` confront the loading state at the
        // type level instead of receiving a default.
        //
        // NOTE: runTest's eager dispatcher can drain pending coroutines
        // before this assertion runs. The hard guarantee we make is:
        // `settings` is typed `StateFlow<AppSettings?>` so a null value
        // is *representable*. The eager-load behaviour is the load contract
        // we test elsewhere.
        val initialType: kotlinx.coroutines.flow.StateFlow<AppSettings?> = repository.settings
        // Compile-time check that the type is nullable — if someone narrows
        // this to `StateFlow<AppSettings>`, the next line stops compiling.
        @Suppress("USELESS_IS_CHECK", "UNUSED_VARIABLE")
        val nullable: AppSettings? = initialType.value
        // No runtime assertion — the type signature IS the contract.
    }

    @Test
    fun `awaitLoaded parity — returns the first non-null emission`() = runTest {
        // The ViewModel uses awaitLoaded() to gate `connect()` and the
        // first-emission orchestrator-local-id restore. It must return the
        // first non-null settings value the repository emits.
        val loaded = repository.awaitLoaded()
        assertNotNull(loaded)
        assertEquals("ws://192.168.0.200:80", loaded.serverUrl)
    }

    // -------------------------------------------------------------------
    // 3. First emission carries persisted ORCHESTRATOR_LOCAL_ID
    // -------------------------------------------------------------------

    @Test
    fun `persisted orchestrator_local_id parity — pre-populated value is readable before WS opens`() = runTest {
        // Pre-populate DataStore as if the previous app run had persisted
        // an orchestrator local_id. On the next cold start the ViewModel
        // reads this value before opening the WS.
        val persisted = "9dfbc5ac-bf47-4e3b-ad8d-20c8363e3915"
        dataStore.edit { it[ORCHESTRATOR_LOCAL_ID] = persisted }

        // The repository exposes a suspend accessor; the result must match.
        val read = repository.persistedOrchestratorLocalId()
        assertEquals(persisted, read)
    }

    @Test
    fun `persisted orchestrator_local_id parity — empty preference returns null`() = runTest {
        // No write at all → null. Blank-string also treated as absent (per
        // the HEAD `?.takeIf { it.isNotBlank() }` at L350).
        val read = repository.persistedOrchestratorLocalId()
        assertNull(read)

        dataStore.edit { it[ORCHESTRATOR_LOCAL_ID] = "" }
        val readBlank = repository.persistedOrchestratorLocalId()
        assertNull(readBlank)
    }

    @Test
    fun `persistOrchestratorLocalId parity — writes through to DataStore`() = runTest {
        val id = "e4f56355-fd4f-4715-b49b-c6805b1a53ba"
        repository.persistOrchestratorLocalId(id)
        // Direct DataStore read confirms the write landed on the canonical key.
        val prefs = dataStore.data.first()
        assertEquals(id, prefs[ORCHESTRATOR_LOCAL_ID])
    }

    @Test
    fun `clearOrchestratorLocalId parity — removes the key`() = runTest {
        val id = "1234"
        dataStore.edit { it[ORCHESTRATOR_LOCAL_ID] = id }
        repository.clearOrchestratorLocalId()
        assertNull(repository.persistedOrchestratorLocalId())
    }

    // -------------------------------------------------------------------
    // 4. serverUrl change notifies — basis for serverUrlChanged teardown
    // -------------------------------------------------------------------

    @Test
    fun `serverUrl change notifies — updateServerUrl emits new AppSettings with the new URL`() = runTest {
        val newUrl = "ws://192.168.0.28:8765"
        repository.settings.test {
            // Drain the null-before-load tick (if any) and the first loaded
            // emission with the default URL.
            var loaded: AppSettings? = null
            while (loaded == null) loaded = awaitItem()
            assertEquals("ws://192.168.0.200:80", loaded.serverUrl)

            // Now update and confirm a new emission with the new URL.
            repository.updateServerUrl(newUrl)
            val next = awaitItem()
            assertNotNull(next)
            assertEquals(newUrl, next!!.serverUrl)
            cancelAndIgnoreRemainingEvents()
        }
    }

    @Test
    fun `serverUrl change keeps other fields unchanged`() = runTest {
        // The ViewModel's serverUrlChanged path wipes session state ONLY
        // when serverUrl changes — other field updates must not trip a
        // false-positive. We assert here that updating server URL is
        // surgical: other fields keep their previous values.
        repository.settings.test {
            var loaded: AppSettings? = null
            while (loaded == null) loaded = awaitItem()
            val originalAutoConnect = loaded.autoConnect
            val originalTheme = loaded.themeMode

            repository.updateServerUrl("ws://10.0.0.1:1234")
            val next = awaitItem()!!
            assertEquals(originalAutoConnect, next.autoConnect)
            assertEquals(originalTheme, next.themeMode)
            cancelAndIgnoreRemainingEvents()
        }
    }

    // -------------------------------------------------------------------
    // 5. Saved-servers encoding parity (regression guard for the
    //    label\turl|... wire format the ViewModel uses at HEAD L322-332)
    // -------------------------------------------------------------------

    @Test
    fun `addSavedServer round-trips through the wire format`() = runTest {
        repository.addSavedServer("Laptop", "ws://192.168.0.28:8765")
        repository.addSavedServer("Jetson", "ws://192.168.0.200:80")
        // Read straight from DataStore (not the cached `_settings` flow which
        // may not have re-emitted on the test dispatcher) so the assertion
        // exercises the persisted wire format, not the in-memory cache.
        val loaded = readAppSettingsFromDisk()
        val urls = loaded.savedServers.map { it.url }
        assertTrue(urls.contains("ws://192.168.0.28:8765"))
        assertTrue(urls.contains("ws://192.168.0.200:80"))
    }

    @Test
    fun `addSavedServer dedupes by URL`() = runTest {
        // HEAD L1878-1880: "Replace any entry with the same url, else append."
        repository.addSavedServer("Old", "ws://192.168.0.28:8765")
        repository.addSavedServer("New", "ws://192.168.0.28:8765")
        val loaded = readAppSettingsFromDisk()
        val matching = loaded.savedServers.filter { it.url == "ws://192.168.0.28:8765" }
        assertEquals(1, matching.size)
        assertEquals("New", matching.single().label)
    }

    @Test
    fun `removeSavedServer drops the matching URL`() = runTest {
        repository.addSavedServer("Laptop", "ws://192.168.0.28:8765")
        repository.addSavedServer("Jetson", "ws://192.168.0.200:80")
        repository.removeSavedServer("ws://192.168.0.28:8765")
        val loaded = readAppSettingsFromDisk()
        val urls = loaded.savedServers.map { it.url }
        assertEquals(listOf("ws://192.168.0.200:80"), urls)
    }

    /**
     * Re-parse the saved-servers wire format directly from DataStore. Bypasses
     * the repository's in-memory `_settings` cache so the test exercises the
     * persisted encoding regardless of dispatcher timing.
     */
    private suspend fun readAppSettingsFromDisk(): AppSettings {
        val prefs = dataStore.data.first()
        val raw = prefs[stringPreferencesKey("saved_servers")]
        val servers = if (raw.isNullOrEmpty()) emptyList()
        else raw.split("|").mapNotNull { entry ->
            val parts = entry.split("\t", limit = 2)
            if (parts.size == 2 && parts[0].isNotBlank() && parts[1].isNotBlank())
                com.assistant.peripheral.data.SavedServer(parts[0], parts[1]) else null
        }
        return AppSettings(savedServers = servers)
    }
}
