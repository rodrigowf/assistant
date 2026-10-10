package com.assistant.archie.feature.settings

import com.assistant.core.protocol.ModelInfoDto
import com.assistant.core.data.ConnectionStatus
import com.assistant.core.model.DeviceSettings
import com.assistant.core.model.SavedServer
import com.assistant.core.model.WorkingDirectory
import com.assistant.core.network.DiscoveredServer
import com.assistant.core.protocol.VoiceModelEntryDto
import com.assistant.core.protocol.VoiceOptionDto
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The pure settings logic: parity with the web's `logic.test.ts` plus the Android device rules. */
class SettingsLogicTest {
    private val all = listOf("chrome-devtools", "github", "posthog")

    // ───────── MCP (CFG-4, fixes inv03 §8 bug 1) ─────────

    @Test fun mcp_emptyMeansAllEnabled() {
        assertTrue(McpLogic.isAllEnabled(emptyList()))
        all.forEach { assertTrue(McpLogic.isEnabled(emptyList(), it)) }
        assertEquals("All 3 enabled", McpLogic.summary(emptyList(), all))
    }

    /** The old app wrote `["github"]` here: only the server you switched off ended up enabled. */
    @Test fun mcp_switchingOneOffFromAll_writesTheOthers_notTheOne() {
        assertEquals(listOf("chrome-devtools", "posthog"), McpLogic.toggle(emptyList(), all, "github", on = false))
    }

    @Test fun mcp_switchingTheLastOneBackOn_writesEmpty() {
        assertEquals(emptyList<String>(), McpLogic.toggle(listOf("chrome-devtools", "posthog"), all, "github", on = true))
        assertEquals("2 of 3 enabled", McpLogic.summary(listOf("chrome-devtools", "posthog"), all))
    }

    @Test fun mcp_lastOnServerCantBeSwitchedOff() {
        assertEquals("Last one on (an empty list means all)", McpLogic.offBlockedReason(listOf("github"), all, "github"))
        assertNull(McpLogic.offBlockedReason(listOf("github", "posthog"), all, "github"))
        // A single configured server (the live Jetson): it is "all", so it can never go off.
        assertEquals("Last one on (an empty list means all)", McpLogic.offBlockedReason(emptyList(), listOf("chrome-devtools"), "chrome-devtools"))
        assertEquals("All enabled (1 server)", McpLogic.summary(listOf("chrome-devtools"), listOf("chrome-devtools")))
    }

    @Test fun mcp_staleNamesAreDropped() {
        assertEquals(listOf("github"), McpLogic.toggle(listOf("github", "gone", "posthog"), all, "posthog", on = false))
    }

    @Test fun mcp_commandLine() {
        val cfg = kotlinx.serialization.json.Json.parseToJsonElement(fixture("mcp_servers.json")) as kotlinx.serialization.json.JsonObject
        val chrome = (cfg["servers"] as kotlinx.serialization.json.JsonObject)["chrome-devtools"] as kotlinx.serialization.json.JsonObject
        assertEquals("npx -y chrome-devtools-mcp@latest --isolated --viewport 1440x900", McpLogic.commandLine(chrome))
    }

    // ───────── working directories (F-33, CFG-7) ─────────

    private fun wd(path: String, host: String? = null, label: String? = null, id: String = WorkingDirectoryLogic.idOf(path, host)) =
        WorkingDirectory(id, path, label, host, if (host != null) "rodrigo" else null, null, null)

    @Test fun wd_coerceDropsBadRowsAndDuplicates() {
        val raw = listOf(wd("/a"), WorkingDirectory("", "  ", null, null, null, null, null), wd("/a"), WorkingDirectory("", "/b", null, " ", "x", "k", "c"))
        val out = WorkingDirectoryLogic.coerce(raw)
        assertEquals(listOf("/a", "/b"), out.map { it.id })
        assertNull("SSH-only fields cleared on a local row", out[1].sshUser)
    }

    @Test fun wd_validate() {
        val h = listOf(wd("/a"), wd("/home/x", "192.168.0.28"))
        assertEquals("Path is required", WorkingDirectoryLogic.validate(WorkingDirectoryDraft(), h, null)[DraftField.PATH])
        assertEquals("Use an absolute path (starts with / or ~)", WorkingDirectoryLogic.validate(WorkingDirectoryDraft(path = "rel"), h, null)[DraftField.PATH])
        val ssh = WorkingDirectoryDraft(ssh = true, path = "/p", host = "rodrigo@host")
        assertEquals("Put the user in the User field", WorkingDirectoryLogic.validate(ssh, h, null)[DraftField.HOST])
        assertEquals("Just the user name", WorkingDirectoryLogic.validate(ssh.copy(host = "h", user = "a b"), h, null)[DraftField.USER])
        assertEquals(
            "Another directory already uses this location",
            WorkingDirectoryLogic.validate(WorkingDirectoryDraft(path = "/a"), h, editingId = "192.168.0.28:/home/x")[DraftField.PATH],
        )
        assertTrue(WorkingDirectoryLogic.validate(WorkingDirectoryDraft(path = "~/p"), h, null).isEmpty())
    }

    @Test fun wd_addEditDelete() {
        val h = listOf(wd("/a"), wd("/b"))
        val added = WorkingDirectoryLogic.add(h, WorkingDirectoryDraft(ssh = true, path = "/srv/", host = "jetson", user = "rodrigo"))
        assertEquals("jetson:/srv/", added.active)
        assertEquals(3, added.history.size)
        assertNull("config dir null → server derives it", added.history.last().claudeConfigDir)
        val same = WorkingDirectoryLogic.add(h, WorkingDirectoryDraft(path = "/a"))
        assertEquals("existing entry is just selected", listOf("/a", "/b") to "/a", same.history.map { it.id } to same.active)
        val edited = WorkingDirectoryLogic.edit(h, "/b", WorkingDirectoryDraft(ssh = true, path = "/b", host = "h"))
        assertEquals("h:/b", edited.active)
        assertEquals(listOf("/a", "h:/b"), edited.history.map { it.id })
        assertNull("the only entry can't go", WorkingDirectoryLogic.delete(listOf(wd("/a")), "/a", "/a"))
        val del = WorkingDirectoryLogic.delete(h, "/a", "/a")!!
        assertEquals(listOf("/b") to "/b", del.history.map { it.id } to del.active)
        assertEquals("Laptop · 2 directories · 1 over SSH", WorkingDirectoryLogic.summary(listOf(wd("/a"), wd("/p", "h", "Laptop")), "h:/p"))
        assertEquals("rodrigo@h · /p", WorkingDirectoryLogic.detail(wd("/p", "h")))
    }

    @Test fun wd_draftHidesTheDerivedConfigDir() {
        val e = WorkingDirectory("h:/p", "/p", "L", "h", "u", null, "/p/.claude_config")
        assertEquals("", WorkingDirectoryLogic.draftOf(e).configDir)
        assertEquals("/custom", WorkingDirectoryLogic.draftOf(e.copy(claudeConfigDir = "/custom")).configDir)
    }

    // ───────── voice (CFG-6, F-31 LOAD-BEARING) ─────────

    private fun entry(id: String, default: Boolean = false, voices: List<String> = listOf("Puck", "Kore")) =
        VoiceModelEntryDto(id = id, voice = voices.first(), voices = voices.map { VoiceOptionDto(it) }, default = default)

    @Test fun googleAutoCorrect_switchesAStaleModelToTheDiscoveredDefault_keepingTheVoice() {
        val fix = VoiceLogic.googleAutoCorrect("google", "gemini-old", "Kore", listOf(entry("a"), entry("b", default = true)))
        assertEquals(AutoCorrect("gemini-old", "b", "Kore"), fix)
    }

    @Test fun googleAutoCorrect_leavesItAlone_whenListedEmptyOrNotGoogle() {
        assertNull(VoiceLogic.googleAutoCorrect("google", "a", "Puck", listOf(entry("a"))))
        assertNull("no list = upstream unhealthy", VoiceLogic.googleAutoCorrect("google", "x", "Puck", emptyList()))
        assertNull(VoiceLogic.googleAutoCorrect("openai", "x", "cedar", listOf(entry("a"))))
        assertEquals("Charon", VoiceLogic.googleAutoCorrect("google", "x", "Gone", listOf(entry("a", voices = listOf("Charon"))))?.voice)
    }

    @Test fun languages_addAutoDetectFirst() {
        val e = VoiceModelEntryDto(id = "m", transcriptionLanguages = listOf(VoiceOptionDto("en", "English")))
        assertEquals(listOf("", "en"), VoiceLogic.languageOptions(e).map { it.id })
        assertEquals("Auto language", VoiceLogic.languageLabel(e, ""))
        assertEquals("English", VoiceLogic.languageLabel(e, "en"))
    }

    @Test fun modelAvailability() {
        val models = listOf(
            ModelInfoDto(provider = "openai", modelId = "gpt-4o", supportsAudio = false),
            ModelInfoDto(provider = "openai", modelId = "gpt-audio-mini", supportsAudio = true),
            ModelInfoDto(provider = "anthropic", modelId = "claude-sonnet-4-5-20250929", supportsAudio = false),
        )
        assertEquals(listOf("gpt-4o", "claude-sonnet-4-5-20250929"), ModelLogic.textModels(models).map { it.modelId })
        assertEquals(listOf("gpt-audio-mini"), ModelLogic.audioModels(models).map { it.modelId })
        assertEquals(ModelAvailability.RETIRED, ModelLogic.availability("gpt-4o-audio-preview", null))
        assertEquals(ModelAvailability.OK, ModelLogic.availability("anything", null))
    }

    @Test fun formats_matchTheWeb() {
        assertEquals("0.28", Format.threshold(0.28))
        assertEquals("1800 ms", Format.ms(1800.0))
        assertEquals("1.0×", Format.gain(1.0))
        assertEquals("0.75×", Format.gain(0.75))
        assertEquals("5.0%", Format.duckPercent(0.05f))
        assertEquals("2.0×", Format.multiplier(2f))
    }

    // ───────── device ranges: the old app's tuned values, unchanged (inv04 §4) ─────────

    @Test fun deviceSliders_keepTheOldRangesAndSteps() {
        // old/ui/screens/SettingsScreen.kt:311 (0..150 step 10 → 16 stops), :342 (0..20 × 0.5 % → 21), :515 (1.0..4.0 step 0.5 → 7)
        assertEquals(16, Ranges.LEVEL.steps + 2)
        assertEquals(0f..1.5f, Ranges.LEVEL.range)
        assertEquals(21, Ranges.DUCK.steps + 2)
        assertEquals(0f..0.1f, Ranges.DUCK.range)
        assertEquals(7, Ranges.TALK_SILENCE.steps + 2)
        assertEquals(1f..4f, Ranges.TALK_SILENCE.range)
        assertEquals(0.05f, Ranges.DUCK.snap(0.051f), 1e-6f)
        assertEquals(1.5f, Ranges.LEVEL.snap(9f), 1e-6f)
        // server tuning (web TUNING)
        assertEquals(36, Ranges.VAD.steps + 2)
        assertEquals(43, Ranges.SILENCE.steps + 2)
        assertEquals(31, Ranges.SERVER_GAIN.steps + 2)
    }

    @Test fun phrases_parseLikeTheVoiceStack() {
        assertEquals(listOf("my friend", "hey assistant"), phrases(" My Friend , hey assistant,, my friend"))
        assertTrue(phrases(" , ").isEmpty())
    }

    // ───────── connection ─────────

    @Test fun serverUrl_normalized() {
        assertEquals("ws://192.168.0.200:80", ConnectionModel.normalizeServerUrl("192.168.0.200:80"))
        assertEquals("https://archie.local", ConnectionModel.normalizeServerUrl("https://archie.local/"))
        assertNull(ConnectionModel.normalizeServerUrl("ftp://x"))
        assertNull(ConnectionModel.normalizeServerUrl("a b"))
        assertNull(ConnectionModel.normalizeServerUrl("ws://"))
    }

    /** inv03 §1.6: the old page said "No servers yet" while connected to an unsaved server. */
    @Test fun rows_alwaysListTheCurrentServer() {
        val s = DeviceSettings(serverUrl = "ws://192.168.0.200:80")
        val rows = ConnectionModel.rows(ConnectionStatus(serverUrl = s.serverUrl, phase = ConnectionStatus.Phase.CONNECTED), s)
        assertEquals(1, rows.size)
        assertTrue(rows[0].current)
        assertFalse(rows[0].saved)
        assertEquals("192.168.0.200", rows[0].label)
    }

    @Test fun rows_currentFirst_thenSaved_thenDiscoveredWithoutDuplicates() {
        val s = DeviceSettings(serverUrl = "ws://10.0.0.2:80", savedServers = listOf(SavedServer("laptop", "ws://10.0.0.5:8765"), SavedServer("jetson", "ws://10.0.0.2:80")))
        val st = ConnectionStatus(discovered = listOf(DiscoveredServer("10.0.0.2", 80, false), DiscoveredServer("10.0.0.9", 8765, false)))
        val rows = ConnectionModel.rows(st, s)
        assertEquals(listOf("ws://10.0.0.2:80", "ws://10.0.0.5:8765", "ws://10.0.0.9:8765"), rows.map { it.url })
        assertEquals("jetson", rows[0].label)
        assertTrue(rows[0].discovered)
        assertEquals("ws://10.0.0.9:8765 · found on this network", rows[2].detail)
    }

    @Test fun permissionsSummary() {
        val st = PermissionsState(mapOf(AppPermission.MICROPHONE to PermissionStatus.GRANTED, AppPermission.NOTIFICATIONS to PermissionStatus.BLOCKED, AppPermission.NEARBY_DEVICES to PermissionStatus.GRANTED))
        assertEquals("Notifications not allowed", st.summary)
        assertEquals("1 of 3 set", st.copy(isXiaomiFamily = true).checklistSummary)
    }

    @Test fun notify_summaryAndSwitchFollowWhatAndroidAllows() {
        assertEquals("Off", NotifyLogic.summary(enabled = false, granted = true, systemEnabled = true))
        assertEquals("On · when an agent session finishes", NotifyLogic.summary(true, true, true))
        assertEquals("On · blocked by Android", NotifyLogic.summary(true, granted = false, systemEnabled = true))
        assertEquals("On · blocked by Android", NotifyLogic.summary(true, granted = true, systemEnabled = false))
        assertTrue(NotifyLogic.effective(true, true, true))
        assertFalse(NotifyLogic.effective(true, false, true))
        assertFalse(NotifyLogic.effective(false, true, true))
    }
}
