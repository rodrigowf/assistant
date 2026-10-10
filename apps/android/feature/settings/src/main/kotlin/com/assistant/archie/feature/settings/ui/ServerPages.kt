package com.assistant.archie.feature.settings.ui

import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.padding
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.assistant.archie.feature.settings.Format
import com.assistant.archie.feature.settings.HarnessLogic
import com.assistant.archie.feature.settings.HarnessScope
import com.assistant.archie.feature.settings.ModelAvailability
import com.assistant.archie.feature.settings.ModelLogic
import com.assistant.archie.feature.settings.Option
import com.assistant.archie.feature.settings.Ranges
import com.assistant.archie.feature.settings.ServerSettingsModel
import com.assistant.archie.feature.settings.ServerSettingsState
import com.assistant.archie.feature.settings.SettingsFeature
import com.assistant.archie.feature.settings.VoiceLogic
import com.assistant.core.data.ConnectionRepository
import com.assistant.core.data.ConnectionStatus
import com.assistant.core.design.components.ArchieButton
import com.assistant.core.design.components.ButtonSize
import com.assistant.core.design.components.ButtonStyle
import com.assistant.core.design.components.InlineCardAction
import com.assistant.core.design.components.SettingsRow
import com.assistant.core.design.icons.ArchieIcon
import com.assistant.core.design.icons.ArchieIcons
import com.assistant.core.design.theme.ArchieTheme
import com.assistant.core.model.HarnessInfo
import com.assistant.core.model.ConfigPatch
import com.assistant.core.model.ServerConfig
import com.assistant.core.protocol.ModelInfoDto

/**
 * An Archie (server) page: the scope chip names the server; the body renders once the config is
 * loaded (web `WithConfig`): offline shows the last values with a notice, a failed load shows the
 * error verbatim with Retry, otherwise "Loading…".
 */
@Composable
internal fun ServerPageFrame(
    feature: SettingsFeature,
    title: String,
    onBack: (() -> Unit)?,
    content: @Composable ColumnScope.(cfg: ServerConfig, state: ServerSettingsState) -> Unit,
) {
    val st by feature.server.state.collectAsStateWithLifecycle()
    val conn by feature.connection.state.collectAsStateWithLifecycle()
    val host = conn.status.serverLabel.ifEmpty { conn.status.serverUrl?.let(ConnectionRepository::hostOf).orEmpty() }
    SettingsPageFrame(title, feature.messages, onBack, scope = ScopeLabel.server(host)) {
        val cfg = st.config.value
        when {
            cfg != null -> {
                if (conn.status.phase == ConnectionStatus.Phase.OFFLINE) {
                    Notice(NoticeTone.WARNING, "Offline", body = "These are the last values read from the server. Saving needs the connection.")
                }
                content(cfg, st)
            }
            st.config.error != null -> Notice(
                NoticeTone.ERROR, "Couldn't load server settings", body = st.config.error,
                actions = { InlineCardAction("Retry", feature.server::refresh, primary = true) },
            )
            else -> LoadingBody("Loading server settings…")
        }
    }
}

private fun modelOptions(models: List<ModelInfoDto>, provider: String, current: String?): List<Option> {
    val opts = models.filter { it.provider == provider && it.modelId.isNotEmpty() }.map { m ->
        val t = ModelLogic.traits(m)
        Option(m.modelId, m.displayName.ifEmpty { m.modelId }, if (t.isNotEmpty()) "${m.modelId} · $t" else m.modelId)
    }.toMutableList()
    if (!current.isNullOrEmpty() && opts.none { it.id == current }) opts.add(0, Option(current, "$current (unavailable)", enabled = false))
    return opts
}

// ───────────────────────────── Conversation model (P-9, O-7) ─────────────────────────────

@Composable
internal fun ConversationModelPage(feature: SettingsFeature, onBack: (() -> Unit)?) = ServerPageFrame(feature, "Conversation model", onBack) { cfg, st ->
    val m = feature.server
    val catalog = st.catalogs.orchestratorModels
    val saving = st.saving != null
    val models = catalog?.models.orEmpty()
    val typed = ModelLogic.textModels(models)
    val providers = ModelLogic.providers(typed)
    val current = cfg.defaultModel
    val provider = ModelLogic.find(models, current)?.provider ?: providers.firstOrNull().orEmpty()
    val availability = ModelLogic.availability(current, catalog)
    val providerOptions = providers.map { Option(it, ModelLogic.providerLabel(it)) }
    // The audio model has its own provider: audio-capable models don't exist for every text
    // provider (none at Anthropic), so it can't follow the text provider. Same shape as the summarizer.
    val audio = ModelLogic.audioModels(models)
    val audioProviders = ModelLogic.providers(audio)
    val audioCurrent = cfg.defaultAudioModel.orEmpty()
    val audioProvider = if (audioCurrent.isNotEmpty()) ModelLogic.find(models, audioCurrent)?.provider ?: audioProviders.firstOrNull().orEmpty() else ""
    val serverAudio = catalog?.defaultAudioModel?.takeIf { audioCurrent.isEmpty() }
    val audioProviderOptions = listOf(Option("", "Server default", serverAudio?.let { "Now $it" })) +
        audioProviders.map { Option(it, ModelLogic.providerLabel(it)) }
    val audioSupported = cfg.defaultAudioModel != null
    if (availability != ModelAvailability.OK) {
        Notice(
            NoticeTone.WARNING,
            if (availability == ModelAvailability.RETIRED) "This model was retired" else "This model is not in the catalog",
            body = "“$current” ${if (availability == ModelAvailability.RETIRED) "answers 404 at OpenAI" else "is not offered by the server"}, so new conversations skip it and use the server's fallback. Pick another model below.",
        )
    }
    if (ModelLogic.find(models, current)?.supportsAudio == true) {
        Notice(
            NoticeTone.INFO,
            "The text model is an audio model",
            body = "“$current” can't answer typed messages, so the server answers them with gpt-4o. Pick a text model below, and choose the audio model separately.",
        )
    }
    Section("Archie") {
        SelectRow("Text model provider", providerOptions, provider, { p ->
            ModelLogic.firstOf(typed, p)?.let { m.launchSave(ConfigPatch(defaultModel = it), "default_model") }
        }, enabled = !saving && providers.isNotEmpty())
        SelectRow("Text model", modelOptions(typed, provider, current), current, { id ->
            m.launchSave(ConfigPatch(defaultModel = id), "default_model")
        }, enabled = !saving && models.isNotEmpty(), supporting = if (catalog == null) "Loading the model list…" else null)
        FieldBlock {
            HelpLine(
                "Answers typed messages. New Archie conversations start on it.",
                "You can still switch models inside a conversation. If this model can't be used, the server falls back to its ORCHESTRATOR_MODEL setting, then gpt-audio.",
            )
        }
        SelectRow("Audio model provider", audioProviderOptions, audioProvider, { p ->
            if (p == "") m.launchSave(ConfigPatch(defaultAudioModel = ""), "default_audio_model")
            else ModelLogic.firstOf(audio, p)?.let { m.launchSave(ConfigPatch(defaultAudioModel = it), "default_audio_model") }
        }, enabled = !saving && models.isNotEmpty() && audioSupported,
            supporting = if (!audioSupported) "This server has no separate audio model setting yet." else null)
        if (audioProvider.isNotEmpty()) {
            SelectRow("Audio model", modelOptions(audio, audioProvider, audioCurrent), audioCurrent, { id ->
                m.launchSave(ConfigPatch(defaultAudioModel = id), "default_audio_model")
            }, enabled = !saving && audioSupported)
        }
        FieldBlock {
            HelpLine(
                "Answers voice messages (recorded clips).",
                "Audio models take audio but refuse typed messages, and text models refuse audio, so Archie uses one of each. Live voice conversations use the Voice page instead.",
            )
        }
    }
    val summ = cfg.summarizerModel.orEmpty()
    val summProvider = if (summ.isNotEmpty()) ModelLogic.find(models, summ)?.provider ?: providers.firstOrNull().orEmpty() else ""
    Section("History summaries") {
        SelectRow("Summarizer provider", listOf(Option("", "Server default")) + providerOptions, summProvider, { p ->
            if (p == "") m.launchSave(ConfigPatch(summarizerModel = ""), "summarizer_model")
            else ModelLogic.firstOf(models, p)?.let { m.launchSave(ConfigPatch(summarizerModel = it), "summarizer_model") }
        }, enabled = !saving)
        if (summProvider.isNotEmpty()) {
            SelectRow("Summarizer model", modelOptions(models, summProvider, summ), summ, { id ->
                m.launchSave(ConfigPatch(summarizerModel = id), "summarizer_model")
            }, enabled = !saving)
        }
        FieldBlock { HelpLine("Summarizes older history for Archie's voice prompt.") }
    }
}

// ───────────────────────────── Voice (F-31, CFG-5, CFG-6) ─────────────────────────────

@Composable
internal fun VoicePage(feature: SettingsFeature, onBack: (() -> Unit)?) = ServerPageFrame(feature, "Voice", onBack) { cfg, st ->
    val m = feature.server
    val saving = st.saving != null
    val v = cfg.voice
    val catalog = VoiceLogic.catalog(st.catalogs.voiceModels, st.google)
    val providerIds = catalog.keys.toMutableList()
    if (!v.provider.isNullOrEmpty() && v.provider !in providerIds) providerIds.add(0, v.provider!!)
    val entries = catalog[v.provider].orEmpty()
    val entry = entries.firstOrNull { it.id == v.model }
    val modelOptions = entries.map { Option(it.id, it.label.ifEmpty { it.id }, it.description?.takeIf { d -> d.isNotEmpty() && d != it.label }) }.toMutableList()
    if (entry == null && !v.model.isNullOrEmpty()) modelOptions.add(0, Option(v.model!!, "${v.model} (not listed)"))
    val voices = VoiceLogic.voiceOptions(entry).toMutableList()
    if (!v.voice.isNullOrEmpty() && voices.none { it.id == v.voice }) voices.add(0, Option(v.voice!!, v.voice!!))
    val langs = VoiceLogic.languageOptions(entry).toMutableList()
    val lang = v.transcriptionLanguage.orEmpty()
    if (langs.isNotEmpty() && langs.none { it.id == lang }) langs += Option(lang, lang)
    val save = { patch: ConfigPatch -> m.launchSave(patch, ServerSettingsModel.KEY_VOICE) }

    st.autoCorrected?.let { fix ->
        Notice(
            NoticeTone.WARNING, "Gemini Live model switched",
            body = "The previously saved Gemini Live model ${fix.from} is no longer available from Google. Switched to ${fix.to}.",
            onDismiss = m::dismissAutoCorrect,
        )
    }
    Section("Voice conversations") {
        SelectRow("Provider", providerIds.map { Option(it, VoiceLogic.providerLabel(it)) }, v.provider, { save(ConfigPatch(defaultVoiceProvider = it)) }, enabled = !saving)
        if (v.provider == "google") {
            SelectRow(
                "Google backend", VoiceLogic.GOOGLE_ENDPOINTS, v.endpoint, { save(ConfigPatch(defaultVoiceEndpoint = it)) }, enabled = !saving,
                supporting = "Vertex AI is the stable path; AI Studio may refuse preview models (error 1008).",
            )
        }
        SelectRow("Model", modelOptions, v.model, { save(ConfigPatch(defaultVoiceModel = it)) }, enabled = !saving && modelOptions.isNotEmpty())
        if (voices.isNotEmpty()) SelectRow("Voice", voices, v.voice, { save(ConfigPatch(defaultVoiceName = it)) }, enabled = !saving)
        if (langs.isNotEmpty()) {
            SelectRow(
                "Transcription language", langs, lang, { save(ConfigPatch(defaultVoiceTranscriptionLanguage = it)) }, enabled = !saving,
                supporting = "Auto-detect is best when you mix languages.",
            )
        }
        FieldBlock { HelpLine("Applies to the next voice conversation; one in progress keeps its settings.") }
    }
    Section(null) {
        ToggleField(
            "Record voice conversations", cfg.voiceRecordingEnabled,
            { m.launchSave(ConfigPatch(voiceRecordingEnabled = it), "voice_recording_enabled") },
            help = "Keeps the audio on the server.", info = "Saved in context/recordings/ on the server.", enabled = !saving,
        )
    }
}

// ───────────────────────────── Voice tuning ─────────────────────────────

@Composable
internal fun VoiceTuningPage(feature: SettingsFeature, onBack: (() -> Unit)?) = ServerPageFrame(feature, "Voice tuning", onBack) { cfg, st ->
    val m = feature.server
    val saving = st.saving != null
    com.assistant.core.design.components.SettingsGroup {
        CommitSlider(
            "Speech detection threshold", (cfg.voiceVadThreshold ?: 0.28).toFloat(), Ranges.VAD, { Format.threshold(it.toDouble()) },
            help = "Higher ignores more background noise. 0.15 to 0.50.",
            info = "Voice activity detection (VAD) threshold. Raise it in noisy rooms; lower it if Archie misses quiet speech.",
            onCommit = { m.launchSave(ConfigPatch(voiceVadThreshold = Math.round(it * 100) / 100.0), "voice_vad_threshold") },
            enabled = !saving, testTag = "vad",
        )
        CommitSlider(
            "Pause before replying", (cfg.voiceVadMinSilenceMs ?: 2500).toFloat(), Ranges.SILENCE, { Format.ms(it.toDouble()) },
            help = "Silence that ends your turn. 800 to 5000 ms.",
            info = "Minimum silence (VAD min silence) before Archie treats your turn as finished. Longer lets you pause mid-sentence.",
            onCommit = { m.launchSave(ConfigPatch(voiceVadMinSilenceMs = Math.round(it)), "voice_vad_min_silence_ms") },
            enabled = !saving, testTag = "silence",
        )
        CommitSlider(
            "Server mic gain", (cfg.voiceMicGain ?: 1.0).toFloat(), Ranges.SERVER_GAIN, { Format.gain(it.toDouble()) },
            help = "Saved, but the server does not use it yet.",
            info = "Reserved: the backend stores voice_mic_gain but does not apply it (G-37). This phone's mic level is under This device → Audio.",
            onCommit = { m.launchSave(ConfigPatch(voiceMicGain = Math.round(it * 100) / 100.0), "voice_mic_gain") },
            enabled = !saving, testTag = "server-gain",
        )
    }
}

// ───────────────────────────── Agent sessions ─────────────────────────────

/**
 * Settings → Agent sessions (web `AgentSessionsPage`): the default harness, then the default
 * harness's model + options, then a collapsible block per other harness. Every control saves on its
 * own (`harness_model` / `harness_options` partial PUTs; `null` = CLI default). Claude in Chrome is
 * the last row of the Claude Code block, wherever that block is.
 */
@Composable
internal fun AgentSessionsPage(feature: SettingsFeature, onBack: (() -> Unit)?) = ServerPageFrame(feature, "Agent sessions", onBack) { cfg, st ->
    val m = feature.server
    val saving = st.saving != null
    val list = st.catalogs.harnesses.orEmpty()
    val options = list.map { Option(it.id, it.label.ifEmpty { it.id }) }.toMutableList()
    if (!cfg.provider.isNullOrEmpty() && options.none { it.id == cfg.provider }) options.add(0, Option(cfg.provider!!, cfg.provider!!))
    val selected = HarnessLogic.info(list, cfg.provider)
    val others = list.filter { it.id != cfg.provider }
    Section("New sessions") {
        SelectRow(
            "Default harness", options, cfg.provider, { m.launchSave(ConfigPatch(provider = it), "provider") },
            enabled = !saving && options.isNotEmpty(),
            supporting = if (st.catalogs.harnesses == null) "Loading the harness list…" else selected?.description,
        )
        FieldBlock { HelpLine("Applies to new agent tabs. Per session: ⋮ → Session settings.") }
    }
    if (selected != null) {
        HarnessWarnings(selected)
        Section("${selected.label} defaults") {
            HarnessDefaults(m, cfg, selected, enabled = !saving)
            FieldBlock {
                HelpLine("Model lists are cached on the server for a few minutes.")
                ArchieButton(
                    if (st.refreshingHarnesses) "Refreshing…" else "Refresh models", m::refreshHarnesses,
                    style = ButtonStyle.Text, size = ButtonSize.Small, icon = ArchieIcons.Refresh,
                    enabled = !st.refreshingHarnesses, modifier = Modifier.testTag("harness-refresh"),
                )
            }
        }
    }
    if (others.isNotEmpty()) {
        SettingsGroupLabel("Other harnesses")
        HelpLine("Defaults for sessions you switch to another harness (⋮ → Session settings).", modifier = Modifier.padding(start = 4.dp, bottom = 8.dp))
        for (h in others) key(h.id) {
            var open by rememberSaveable { mutableStateOf(false) }
            if (open) HarnessWarnings(h)
            Section(null) {
                SettingsRow(
                    h.label.ifEmpty { h.id },
                    value = HarnessLogic.harnessDefaultsSummary(h.catalog, cfg.harnessModel[h.id].orEmpty(), cfg.harnessOptions[h.id]),
                    onClick = { open = !open },
                    trailing = { ArchieIcon(if (open) ArchieIcons.KeyboardArrowUp else ArchieIcons.KeyboardArrowDown, null, tint = ArchieTheme.colors.onSurfaceVariant) },
                    modifier = Modifier.testTag("harness-group:${h.id}"),
                )
                if (open) HarnessDefaults(m, cfg, h, enabled = !saving)
            }
        }
    }
}

/**
 * Model + options of one harness, each saved on its own (`harness_model` / `harness_options` partial
 * PUTs); Claude Code adds its "Claude in Chrome" switch (`chrome_extension`).
 */
@Composable
private fun ColumnScope.HarnessDefaults(m: ServerSettingsModel, cfg: ServerConfig, harness: HarnessInfo, enabled: Boolean) {
    val p = harness.id
    HarnessFields(
        harness, HarnessScope.GLOBAL,
        model = cfg.harnessModel[p].orEmpty(),
        inheritedModel = null,
        options = cfg.harnessOptions[p],
        inheritedOptions = null,
        enabled = enabled,
        onModel = { m.launchSave(HarnessLogic.globalModelPatch(p, it.orEmpty()), "harness_model") },
        onOption = { key, state -> m.launchSave(HarnessLogic.globalOptionPatch(p, key, state), "harness_options") },
    )
    if (p == "claude") {
        ClaudeInChromeField(HarnessScope.GLOBAL, cfg.chromeExtension, inherited = cfg.chromeExtension, enabled = enabled) {
            m.launchSave(ConfigPatch(chromeExtension = it == true), "chrome_extension")
        }
    }
}
