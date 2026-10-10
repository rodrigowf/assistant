package com.assistant.archie.feature.settings

import com.assistant.core.model.WorkingDirectory
import com.assistant.core.protocol.ModelInfoDto
import com.assistant.core.protocol.OrchestratorModelsDto
import com.assistant.core.protocol.VoiceModelEntryDto
import com.assistant.core.protocol.VoiceModelsDto
import com.assistant.core.protocol.VoiceOptionDto
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import java.util.Locale
import kotlin.math.abs
import kotlin.math.roundToInt

/*
 * Pure settings logic, a port of the committed web `frontend/src/features/settings/logic.ts`
 * (W-13) so both platforms behave and read the same: MCP "all enabled" semantics (CFG-4, fixes
 * inv03 §8 bug 1), working-directory coercion / validation / edits (F-33, CFG-7), the Google voice
 * model auto-correct (CFG-6, F-31 [LOAD-BEARING]), catalog helpers and the one-line summaries of
 * the Settings home. No Android, no I/O — unit-tested in SettingsLogicTest.
 */

// ───────────────────────── MCP servers (CFG-4) ─────────────────────────

object McpLogic {
    /** `enabled_mcps: []` means every server is enabled (`api/routes/config.py:125`). */
    fun isAllEnabled(enabled: List<String>?): Boolean = enabled.isNullOrEmpty()

    fun isEnabled(enabled: List<String>?, name: String): Boolean = isAllEnabled(enabled) || name in enabled!!

    fun enabledNames(enabled: List<String>?, all: List<String>): List<String> = all.filter { isEnabled(enabled, it) }

    /**
     * The list to save after switching one server. Unchecking X while the list is empty writes the
     * explicit list of every other server; checking every server writes `[]`. Names not in [all]
     * (servers removed from `.claude.json`) are dropped.
     */
    fun toggle(enabled: List<String>?, all: List<String>, name: String, on: Boolean): List<String> {
        val set = enabledNames(enabled, all).filter { it != name }.toMutableList()
        if (on) set += name
        val next = all.filter { it in set }
        return if (next.size == all.size) emptyList() else next
    }

    /**
     * The backend cannot express "none enabled" (an empty list means all), so the last server that
     * is on cannot be switched off. Returns the reason, or null when the switch may turn off.
     */
    fun offBlockedReason(enabled: List<String>?, all: List<String>, name: String): String? {
        val on = enabledNames(enabled, all)
        return if (on.size == 1 && on[0] == name) "Last one on (an empty list means all)" else null
    }

    fun summary(enabled: List<String>?, all: List<String>): String {
        if (all.isEmpty()) return "None configured"
        val n = enabledNames(enabled, all).size
        if (n == all.size) return if (all.size == 1) "All enabled (1 server)" else "All ${all.size} enabled"
        return "$n of ${all.size} enabled"
    }

    /** `npx -y chrome-devtools-mcp@latest …` from a `.claude.json` server entry. */
    fun commandLine(cfg: JsonObject?): String {
        if (cfg == null) return ""
        val command = (cfg["command"] as? JsonPrimitive)?.takeIf { it.isString }?.content
            ?: (cfg["url"] as? JsonPrimitive)?.takeIf { it.isString }?.content ?: ""
        val args = (cfg["args"] as? JsonArray).orEmpty().mapNotNull { (it as? JsonPrimitive)?.takeIf { p -> p.isString }?.content }
        val base = command.substringAfterLast('/')
        return (listOf(base) + args).joinToString(" ").trim()
    }
}

// ───────────────────────── working directories (F-33, CFG-7) ─────────────────────────

/** The add / edit dialog's fields. */
data class WorkingDirectoryDraft(
    val ssh: Boolean = false,
    val path: String = "",
    val label: String = "",
    val host: String = "",
    val user: String = "",
    val key: String = "",
    val configDir: String = "",
)

enum class DraftField { PATH, HOST, USER }

data class HistoryEdit(val history: List<WorkingDirectory>, val active: String)

object WorkingDirectoryLogic {
    const val MAX = 20

    /** `host:path` for SSH entries, the path for local ones (the backend's id rule). */
    fun idOf(path: String, sshHost: String?): String = if (!sshHost.isNullOrEmpty()) "$sshHost:$path" else path

    private fun String?.blankToNull(): String? = this?.trim()?.takeIf { it.isNotEmpty() }

    /**
     * Defensive coercion (**[LOAD-BEARING]** F-33, commit 01b24e3: malformed rows crashed the old
     * panel): rows without a path are dropped, SSH-only fields are cleared on local rows, duplicates
     * (same id) keep the first.
     */
    fun coerce(raw: List<WorkingDirectory>): List<WorkingDirectory> {
        val out = ArrayList<WorkingDirectory>()
        for (r in raw) {
            val path = r.path.trim()
            if (path.isEmpty()) continue
            val host = r.sshHost.blankToNull()
            val e = WorkingDirectory(
                id = r.id.trim().ifEmpty { idOf(path, host) },
                path = path,
                label = r.label?.trim().orEmpty(),
                sshHost = host,
                sshUser = if (host != null) r.sshUser.blankToNull() else null,
                sshKey = if (host != null) r.sshKey.blankToNull() else null,
                claudeConfigDir = if (host != null) r.claudeConfigDir.blankToNull() else null,
            )
            if (out.none { it.id == e.id }) out += e
        }
        return out
    }

    fun name(e: WorkingDirectory): String = e.label?.takeIf { it.isNotEmpty() } ?: if (e.sshHost != null) "${e.sshHost}:${e.path}" else e.path

    fun detail(e: WorkingDirectory): String =
        if (e.sshHost == null) e.path else "${e.sshUser?.let { "$it@" } ?: ""}${e.sshHost} · ${e.path}"

    /** The backend's default `CLAUDE_CONFIG_DIR` for an SSH entry. */
    fun defaultConfigDir(path: String): String = "${path.trimEnd('/')}/.claude_config"

    fun draftOf(e: WorkingDirectory): WorkingDirectoryDraft {
        val derived = e.sshHost != null && e.claudeConfigDir == defaultConfigDir(e.path)
        return WorkingDirectoryDraft(
            ssh = e.sshHost != null,
            path = e.path,
            label = e.label.orEmpty(),
            host = e.sshHost.orEmpty(),
            user = e.sshUser.orEmpty(),
            key = e.sshKey.orEmpty(),
            // An auto-derived value shows as the placeholder, so it follows a path change.
            configDir = if (derived) "" else e.claudeConfigDir.orEmpty(),
        )
    }

    fun validate(d: WorkingDirectoryDraft, history: List<WorkingDirectory>, editingId: String?): Map<DraftField, String> {
        val errors = LinkedHashMap<DraftField, String>()
        val path = d.path.trim()
        val host = d.host.trim()
        if (path.isEmpty()) errors[DraftField.PATH] = if (d.ssh) "Remote path is required" else "Path is required"
        else if (!(path.startsWith("/") || path.startsWith("~"))) errors[DraftField.PATH] = "Use an absolute path (starts with / or ~)"
        if (d.ssh) {
            if (host.isEmpty()) errors[DraftField.HOST] = "SSH host is required"
            else if (host.any { it.isWhitespace() }) errors[DraftField.HOST] = "The host has no spaces"
            else if ('@' in host) errors[DraftField.HOST] = "Put the user in the User field"
            val user = d.user.trim()
            if (user.any { it.isWhitespace() || it == '@' }) errors[DraftField.USER] = "Just the user name"
        }
        if (DraftField.PATH !in errors && DraftField.HOST !in errors && editingId != null) {
            val id = idOf(path, if (d.ssh) host else null)
            if (id != editingId && history.any { it.id == id }) errors[DraftField.PATH] = "Another directory already uses this location"
        }
        return errors
    }

    fun entryOf(d: WorkingDirectoryDraft): WorkingDirectory {
        val path = d.path.trim()
        val host = if (d.ssh) d.host.trim() else null
        return WorkingDirectory(
            // Always the computed id: the backend keeps a non-empty id it is given, so an edited SSH
            // entry would otherwise keep its old `host:path`.
            id = idOf(path, host),
            path = path,
            label = d.label.trim(),
            sshHost = host,
            sshUser = if (d.ssh) d.user.blankToNull() else null,
            sshKey = if (d.ssh) d.key.blankToNull() else null,
            // null → the backend derives `<path>/.claude_config`.
            claudeConfigDir = if (d.ssh) d.configDir.blankToNull() else null,
        )
    }

    /** Add: an entry that already exists is just selected (F-33); a new one is appended and becomes active. */
    fun add(history: List<WorkingDirectory>, d: WorkingDirectoryDraft): HistoryEdit {
        val entry = entryOf(d)
        val list = coerce(history)
        if (list.any { it.id == entry.id }) return HistoryEdit(list, entry.id)
        return HistoryEdit(list + entry, entry.id)
    }

    /** Edit: replace by the old id (the id may change); the edited entry becomes active. */
    fun edit(history: List<WorkingDirectory>, oldId: String, d: WorkingDirectoryDraft): HistoryEdit {
        val entry = entryOf(d)
        return HistoryEdit(coerce(history).map { if (it.id == oldId) entry else it }, entry.id)
    }

    /** Delete (never the only entry). If the active one goes, the first remaining becomes active. */
    fun delete(history: List<WorkingDirectory>, id: String, active: String): HistoryEdit? {
        val list = coerce(history)
        if (list.size <= 1) return null
        val next = list.filter { it.id != id }
        if (next.size == list.size) return null
        return HistoryEdit(next, if (next.any { it.id == active }) active else next.first().id)
    }

    fun summary(history: List<WorkingDirectory>, active: String?): String {
        val list = coerce(history)
        if (list.isEmpty()) return "None"
        val ssh = list.count { it.sshHost != null }
        val current = list.firstOrNull { it.id == active }
        val count = "${list.size} ${if (list.size == 1) "directory" else "directories"}${if (ssh > 0) " · $ssh over SSH" else ""}"
        return if (current != null) "${name(current)} · $count" else count
    }
}

// ───────────────────────── models (P-9, O-7) ─────────────────────────

enum class ModelAvailability { OK, RETIRED, UNKNOWN }

object ModelLogic {
    /** Ids the backend skips (O-7 `RETIRED_MODEL_IDS`): OpenAI answers 404 for them. */
    val RETIRED_MODEL_IDS = listOf("gpt-4o-audio-preview", "gpt-4o-mini-audio-preview")

    private val PROVIDER_LABELS = mapOf("anthropic" to "Anthropic", "openai" to "OpenAI", "google" to "Google")

    fun providerLabel(p: String): String = PROVIDER_LABELS[p] ?: p

    fun providers(models: List<ModelInfoDto>): List<String> = models.map { it.provider }.filter { it.isNotEmpty() }.distinct()

    fun find(models: List<ModelInfoDto>, id: String?): ModelInfoDto? = models.firstOrNull { it.modelId == id }

    fun name(models: List<ModelInfoDto>, id: String?): String = find(models, id)?.displayName?.takeIf { it.isNotEmpty() } ?: id.orEmpty()

    fun firstOf(models: List<ModelInfoDto>, provider: String): String? = models.firstOrNull { it.provider == provider && it.modelId.isNotEmpty() }?.modelId

    /**
     * Typed vs voice messages (2026-10-04): OpenAI's audio chat models (gpt-audio family) refuse
     * text-only turns and text models refuse audio, so Archie uses one model per input kind.
     */
    fun textModels(models: List<ModelInfoDto>): List<ModelInfoDto> = models.filter { !it.supportsAudio }

    fun audioModels(models: List<ModelInfoDto>): List<ModelInfoDto> = models.filter { it.supportsAudio }

    fun traits(m: ModelInfoDto): String = buildList {
        if (m.supportsAudio) add("audio")
        if (m.supportsVision) add("vision")
        m.contextWindow?.takeIf { it > 0 }?.let { add("${(it / 1000.0).roundToInt()}K context") }
    }.joinToString(" · ")

    /** Is the saved Archie model usable? (`UNKNOWN` only when a non-empty catalog lacks it.) */
    fun availability(id: String?, catalog: OrchestratorModelsDto?): ModelAvailability = when {
        id != null && id in RETIRED_MODEL_IDS -> ModelAvailability.RETIRED
        id != null && catalog != null && catalog.models.isNotEmpty() && find(catalog.models, id) == null -> ModelAvailability.UNKNOWN
        else -> ModelAvailability.OK
    }
}

// ───────────────────────── voice (F-31, CFG-5, CFG-6) ─────────────────────────

/** One choice of a select (label + optional one-line description). */
data class Option(val id: String, val label: String, val description: String? = null, val enabled: Boolean = true)

/** A CFG-6 correction: the saved model is gone from Google's live list. */
data class AutoCorrect(val from: String, val to: String, val voice: String)

object VoiceLogic {
    private val PROVIDER_LABELS = mapOf("openai" to "OpenAI", "qwen" to "Qwen (Alibaba)", "google" to "Google Gemini")

    fun providerLabel(p: String): String = PROVIDER_LABELS[p] ?: p

    val GOOGLE_ENDPOINTS = listOf(Option("vertex", "Vertex AI (recommended)"), Option("aistudio", "AI Studio (legacy)"))

    /**
     * Voice catalog per provider. The live `GET /api/orchestrator/voice/models` omits `google`
     * (G-33); a non-empty discovered Google list replaces whatever static one is there (F-31).
     */
    fun catalog(models: VoiceModelsDto?, google: List<VoiceModelEntryDto>?): Map<String, List<VoiceModelEntryDto>> {
        val out = LinkedHashMap<String, List<VoiceModelEntryDto>>()
        models?.providers?.forEach { (k, v) -> out[k] = v }
        if (!google.isNullOrEmpty()) out["google"] = google
        return out
    }

    fun languageOptions(entry: VoiceModelEntryDto?): List<Option> {
        val out = entry?.transcriptionLanguages.orEmpty().map { o: VoiceOptionDto ->
            Option(o.id, o.label.ifEmpty { if (o.id.isEmpty()) "Auto-detect" else o.id }, o.description.takeIf { it.isNotEmpty() })
        }.toMutableList()
        if (out.isNotEmpty() && out.none { it.id == "" }) out.add(0, Option("", "Auto-detect"))
        return out
    }

    fun voiceOptions(entry: VoiceModelEntryDto?): List<Option> =
        entry?.voices.orEmpty().map { Option(it.id, it.label.ifEmpty { it.id }, it.description.takeIf { d -> d.isNotEmpty() }) }

    fun languageLabel(entry: VoiceModelEntryDto?, id: String?): String {
        if (id.isNullOrEmpty()) return "Auto language"
        return languageOptions(entry).firstOrNull { it.id == id }?.label ?: id
    }

    /**
     * **[LOAD-BEARING]** F-31 (commit 6a4712f): Google renames Gemini Live model ids; a stale saved id
     * makes every voice session fail with WS 1008. When the provider is `google` and the saved model is
     * not in a NON-EMPTY discovered list, switch to the discovered default (keeping the voice when the
     * new model still offers it). No list = upstream unhealthy: leave the user's choice alone.
     */
    fun googleAutoCorrect(provider: String?, model: String?, voice: String?, discovered: List<VoiceModelEntryDto>?): AutoCorrect? {
        if (provider != "google") return null
        if (discovered.isNullOrEmpty()) return null
        if (discovered.any { it.id == model }) return null
        val target = discovered.firstOrNull { it.default } ?: discovered.first()
        val keepVoice = target.voices.any { it.id == voice }
        val fallback = target.voice ?: target.voices.firstOrNull()?.id ?: voice.orEmpty()
        return AutoCorrect(from = model.orEmpty(), to = target.id, voice = if (keepVoice) voice.orEmpty() else fallback)
    }
}

// ───────────────────────── formatting and ranges ─────────────────────────

/** A slider's range and step; [steps] is Compose's count of stops between the ends. */
data class SliderSpec(val min: Float, val max: Float, val step: Float) {
    val range: ClosedFloatingPointRange<Float> get() = min..max
    val steps: Int get() = ((max - min) / step).roundToInt() - 1

    fun snap(v: Float): Float = (min + ((v - min) / step).roundToInt() * step).coerceIn(min, max)
}

/** Settings → Notifications (spec 12 §8.2): the switch as it actually works (Android can veto it). */
object NotifyLogic {
    /**
     * [granted] = POST_NOTIFICATIONS granted (or not needed below API 33); [systemEnabled] = the
     * app's notifications are on in system settings.
     */
    fun summary(enabled: Boolean, granted: Boolean, systemEnabled: Boolean): String = when {
        !enabled -> "Off"
        !granted || !systemEnabled -> "On · blocked by Android"
        else -> "On · when an agent session finishes"
    }

    /** What the switch shows: on only when the notification can actually be posted. */
    fun effective(enabled: Boolean, granted: Boolean, systemEnabled: Boolean): Boolean = enabled && granted && systemEnabled
}

object Format {
    fun threshold(v: Double): String = String.format(Locale.US, "%.2f", v)
    fun ms(v: Double): String = "${v.roundToInt()} ms"
    fun gain(v: Double): String = String.format(Locale.US, "%.2f", v).removeSuffix("0") + "×"
    fun percent(fraction: Float): String = "${(fraction * 100).roundToInt()}%"
    fun duckPercent(fraction: Float): String = String.format(Locale.US, "%.1f%%", fraction * 100)
    fun multiplier(v: Float): String = String.format(Locale.US, "%.1f×", v)

    /** Same value within half a step: a slider release that did not move saves nothing. */
    fun same(a: Double, b: Double, step: Double): Boolean = abs(a - b) <= step / 1000
}

/**
 * Ranges. The server-tuning ones mirror the web (`TUNING`). The device ones are the old app's
 * **tuned** ranges and steps (`old/ui/screens/SettingsScreen.kt:311-350, 497-523`; inv03 §2.1) and
 * must not change: they feed the voice stack unchanged (inv04 §4).
 */
object Ranges {
    val VAD = SliderSpec(0.15f, 0.5f, 0.01f)
    val SILENCE = SliderSpec(800f, 5000f, 100f)
    val SERVER_GAIN = SliderSpec(0.5f, 2f, 0.05f)

    /** Mic level and wake-word sensitivity: 0–150 % in 10 % steps (fraction 0–1.5). */
    val LEVEL = SliderSpec(0f, 1.5f, 0.1f)

    /** Echo ducking: 0–10 % in 0.5 % steps (fraction 0–0.1). Stored as the fraction. */
    val DUCK = SliderSpec(0f, 0.1f, 0.005f)

    /** Talk auto-stop: 1.0–4.0× in 0.5 steps. */
    val TALK_SILENCE = SliderSpec(1f, 4f, 0.5f)

    /** Speaker (system call volume), percent of the stream maximum. */
    val SPEAKER = SliderSpec(0f, 1f, 0.1f)
}

/** Comma-separated phrases as the voice stack parses them (inv04 §4.1 "Talk/wake variant parse"). */
fun phrases(raw: String): List<String> = raw.split(',').map { it.trim().lowercase() }.filter { it.isNotEmpty() }.distinct()
