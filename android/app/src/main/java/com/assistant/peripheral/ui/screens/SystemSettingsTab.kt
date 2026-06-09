package com.assistant.peripheral.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import com.assistant.peripheral.data.ConfigPatch
import com.assistant.peripheral.data.ConnectionState
import com.assistant.peripheral.data.SystemConfigState

private val VOICE_PROVIDER_LABELS = mapOf(
    "openai" to "OpenAI",
    "qwen" to "Qwen (Alibaba)",
    "google" to "Google Gemini",
)

// Backends for the Google voice provider only — selects between
// Vertex AI (recommended) and AI Studio (legacy). Mirrors the web
// ConfigPage dropdown.
private val GOOGLE_VOICE_ENDPOINTS = listOf("vertex", "aistudio")
private val GOOGLE_VOICE_ENDPOINT_LABELS = mapOf(
    "vertex" to "Vertex AI (recommended)",
    "aistudio" to "AI Studio (legacy)",
)

/**
 * Mirrors the web frontend's `ConfigPage` — exposes backend (assistant_config.json)
 * settings: orchestrator text/voice model, voice recording, session provider,
 * working directory selector, session flags, and MCP toggles.
 *
 * Only meaningful when the app is connected to a backend; otherwise we show a
 * compact "connect first" state.
 */
@Composable
fun SystemSettingsTabContent(
    connectionState: ConnectionState,
    state: SystemConfigState,
    onReload: () -> Unit,
    onUpdate: (ConfigPatch) -> Unit,
    onToggleMcp: (String) -> Unit,
    onDismissVoiceModelAutoCorrected: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val cfg = state.config
    Column(
        modifier = modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp)
    ) {
        // ── Save-status banner ─────────────────────────────────────
        if (state.saving || state.savedFlash || state.error != null) {
            Surface(
                color = when {
                    state.error != null -> MaterialTheme.colorScheme.errorContainer
                    state.savedFlash -> MaterialTheme.colorScheme.primaryContainer
                    else -> MaterialTheme.colorScheme.surfaceVariant
                },
                shape = MaterialTheme.shapes.medium,
                modifier = Modifier.fillMaxWidth()
            ) {
                Row(
                    modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    when {
                        state.error != null -> {
                            Icon(Icons.Default.Error, contentDescription = null,
                                tint = MaterialTheme.colorScheme.onErrorContainer)
                            Spacer(Modifier.width(8.dp))
                            Text(
                                state.error,
                                style = MaterialTheme.typography.bodyMedium,
                                color = MaterialTheme.colorScheme.onErrorContainer,
                                modifier = Modifier.weight(1f),
                            )
                            TextButton(onClick = onReload) { Text("Retry") }
                        }
                        state.saving -> {
                            CircularProgressIndicator(modifier = Modifier.size(14.dp), strokeWidth = 2.dp)
                            Spacer(Modifier.width(8.dp))
                            Text("Saving…", style = MaterialTheme.typography.bodyMedium)
                        }
                        state.savedFlash -> {
                            Icon(Icons.Default.CheckCircle, contentDescription = null,
                                tint = MaterialTheme.colorScheme.primary)
                            Spacer(Modifier.width(8.dp))
                            Text("Saved", style = MaterialTheme.typography.bodyMedium)
                        }
                    }
                }
            }
        }

        state.voiceModelAutoCorrected?.let { correction ->
            Surface(
                color = MaterialTheme.colorScheme.tertiaryContainer,
                shape = MaterialTheme.shapes.medium,
                modifier = Modifier.fillMaxWidth(),
            ) {
                Row(
                    modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Icon(
                        Icons.Default.Info, contentDescription = null,
                        tint = MaterialTheme.colorScheme.onTertiaryContainer,
                    )
                    Spacer(Modifier.width(8.dp))
                    Text(
                        "Gemini model \"${correction.from}\" was deprecated by Google. " +
                            "Switched to \"${correction.to}\".",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onTertiaryContainer,
                        modifier = Modifier.weight(1f),
                    )
                    TextButton(onClick = onDismissVoiceModelAutoCorrected) { Text("Dismiss") }
                }
            }
        }

        when {
            connectionState !is ConnectionState.Connected -> NotConnectedNotice()
            state.loading && cfg == null -> Box(
                modifier = Modifier.fillMaxWidth().padding(top = 32.dp),
                contentAlignment = Alignment.Center
            ) { CircularProgressIndicator() }
            cfg == null -> Box(
                modifier = Modifier.fillMaxWidth().padding(top = 32.dp),
                contentAlignment = Alignment.Center
            ) {
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    Text("Couldn't load configuration", style = MaterialTheme.typography.bodyMedium)
                    Spacer(Modifier.height(8.dp))
                    Button(onClick = onReload) { Text("Reload") }
                }
            }
            else -> {
                OrchestratorCard(state, onUpdate)
                SessionProviderCard(state, onUpdate)
                WorkingDirectoryCard(state, onUpdate)
                SessionFlagsCard(state, onUpdate)
                McpServersCard(state, onToggleMcp)
            }
        }
    }
}

@Composable
private fun NotConnectedNotice() {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(
            modifier = Modifier.padding(16.dp),
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            Icon(
                Icons.Default.CloudOff,
                contentDescription = null,
                modifier = Modifier.size(40.dp),
                tint = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.height(12.dp))
            Text(
                "Connect to a server to manage system settings",
                style = MaterialTheme.typography.bodyMedium,
            )
            Spacer(Modifier.height(4.dp))
            Text(
                "These settings live on the backend and apply to all clients.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────
// Orchestrator card — text + voice + recording toggle
// ─────────────────────────────────────────────────────────────────────────

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun OrchestratorCard(state: SystemConfigState, onUpdate: (ConfigPatch) -> Unit) {
    val cfg = state.config ?: return
    SectionCard(icon = Icons.Default.AutoAwesome, title = "Orchestrator") {
        Text(
            "Defaults the orchestrator uses for new sessions. Text mode also drives history summarization.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        // Text mode
        Spacer(Modifier.height(12.dp))
        Text("Text mode", style = MaterialTheme.typography.titleSmall)
        Text(
            "Used for typed conversations. Can be changed mid-conversation.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(8.dp))
        val providers = state.models.map { it.provider }.distinct()
        val selectedModel = state.models.firstOrNull { it.modelId == cfg.defaultModel }
        val selectedProvider = selectedModel?.provider ?: providers.firstOrNull() ?: ""
        val providerModels = state.models.filter { it.provider == selectedProvider }

        if (state.models.isEmpty()) {
            EmptyHint("No models available")
        } else {
            DropdownField(
                label = "Provider",
                options = providers,
                selected = selectedProvider,
                optionLabel = { p -> when (p) { "anthropic" -> "Anthropic"; "openai" -> "OpenAI"; else -> p } },
                enabled = !state.saving,
                onSelect = { p ->
                    val first = state.models.firstOrNull { it.provider == p }
                    if (first != null) onUpdate(ConfigPatch(defaultModel = first.modelId))
                },
            )
            Spacer(Modifier.height(8.dp))
            DropdownField(
                label = "Model",
                options = providerModels.map { it.modelId },
                selected = cfg.defaultModel,
                optionLabel = { id ->
                    val m = providerModels.firstOrNull { it.modelId == id }
                    val name = m?.displayName ?: id
                    val flags = buildString {
                        if (m?.supportsAudio == true) append(" 🎤")
                        if (m?.supportsVision == true) append(" 👁")
                    }
                    "$name$flags"
                },
                enabled = !state.saving,
                onSelect = { id -> onUpdate(ConfigPatch(defaultModel = id)) },
            )
        }

        // Voice mode
        Spacer(Modifier.height(16.dp))
        Divider()
        Spacer(Modifier.height(12.dp))
        Text("Voice mode", style = MaterialTheme.typography.titleSmall)
        Text(
            "Used for realtime voice sessions. Cannot be changed mid-session.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(8.dp))
        val voiceProviderIds = state.voiceProviders.keys.toList()
        if (voiceProviderIds.isEmpty()) {
            EmptyHint("No voice providers available")
        } else {
            val vProv = cfg.defaultVoiceProvider.ifBlank { voiceProviderIds.first() }
            val vModels = state.voiceProviders[vProv].orEmpty()
            val vModel = vModels.firstOrNull { it.id == cfg.defaultVoiceModel } ?: vModels.firstOrNull()
            val vVoices = vModel?.voices.orEmpty()
            val vLangs = vModel?.transcriptionLanguages.orEmpty()
            val vName = cfg.defaultVoiceName.ifBlank { vModel?.voice ?: "" }
            val vLang = cfg.defaultVoiceTranscriptionLanguage.ifBlank { vModel?.defaultTranscriptionLanguage ?: "" }

            DropdownField(
                label = "Provider",
                options = voiceProviderIds,
                selected = vProv,
                optionLabel = { VOICE_PROVIDER_LABELS[it] ?: it },
                enabled = !state.saving,
                onSelect = { onUpdate(ConfigPatch(defaultVoiceProvider = it)) },
            )
            if (vProv == "google") {
                Spacer(Modifier.height(8.dp))
                val vEndpoint = cfg.defaultVoiceEndpoint.ifBlank { "vertex" }
                DropdownField(
                    label = "Backend",
                    options = GOOGLE_VOICE_ENDPOINTS,
                    selected = vEndpoint,
                    optionLabel = { GOOGLE_VOICE_ENDPOINT_LABELS[it] ?: it },
                    enabled = !state.saving,
                    onSelect = { onUpdate(ConfigPatch(defaultVoiceEndpoint = it)) },
                )
            }
            Spacer(Modifier.height(8.dp))
            DropdownField(
                label = "Model",
                options = vModels.map { it.id },
                selected = vModel?.id ?: "",
                optionLabel = { id -> vModels.firstOrNull { it.id == id }?.label ?: id },
                enabled = !state.saving && vModels.isNotEmpty(),
                onSelect = { onUpdate(ConfigPatch(defaultVoiceModel = it)) },
            )
            Spacer(Modifier.height(8.dp))
            DropdownField(
                label = "Voice",
                options = vVoices.map { it.id },
                selected = vName,
                optionLabel = { id ->
                    val v = vVoices.firstOrNull { it.id == id }
                    val base = v?.label ?: id
                    if (!v?.description.isNullOrBlank()) "$base — ${v?.description}" else base
                },
                enabled = !state.saving && vVoices.isNotEmpty(),
                onSelect = { onUpdate(ConfigPatch(defaultVoiceName = it)) },
            )
            if (vLangs.isNotEmpty()) {
                Spacer(Modifier.height(8.dp))
                DropdownField(
                    label = "Transcription language",
                    options = vLangs.map { it.id },
                    selected = vLang,
                    optionLabel = { id ->
                        val l = vLangs.firstOrNull { it.id == id }
                        val base = l?.label ?: id
                        if (!l?.description.isNullOrBlank()) "$base — ${l?.description}" else base
                    },
                    enabled = !state.saving,
                    onSelect = { onUpdate(ConfigPatch(defaultVoiceTranscriptionLanguage = it)) },
                )
            }
        }

        // Voice recording
        Spacer(Modifier.height(16.dp))
        Divider()
        Spacer(Modifier.height(12.dp))
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Column(modifier = Modifier.weight(1f)) {
                Text("Voice recording", style = MaterialTheme.typography.titleSmall)
                Text(
                    "Save raw audio from voice sessions to context/recordings/.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Switch(
                checked = cfg.voiceRecordingEnabled,
                onCheckedChange = { onUpdate(ConfigPatch(voiceRecordingEnabled = it)) },
                enabled = !state.saving,
            )
        }

        Divider(modifier = Modifier.padding(vertical = 12.dp))

        // Voice tuning — Increment B (voice subsystem refactor).
        // Three knobs that were previously hardcoded; defaults equal
        // the documented Silero constants exactly.
        Text("Voice tuning", style = MaterialTheme.typography.titleSmall)
        Text(
            "Adjust the on-device VAD if the assistant misses your speech (raise threshold) " +
                "or cuts you off mid-sentence (raise silence ms). Restart voice to apply.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        VoiceTuningSlider(
            title = "VAD threshold",
            valueText = "%.2f".format(cfg.voiceVadThreshold),
            value = cfg.voiceVadThreshold.toFloat(),
            valueRange = 0.15f..0.5f,
            steps = 34,  // 0.01 increments across 0.35 range
            enabled = !state.saving,
            onCommit = { onUpdate(ConfigPatch(voiceVadThreshold = it.toDouble())) },
            description = "Silero P(speech) needed to enter listening. Lower = more sensitive.",
        )

        VoiceTuningSlider(
            title = "Min silence",
            valueText = "${cfg.voiceVadMinSilenceMs} ms",
            value = cfg.voiceVadMinSilenceMs.toFloat(),
            valueRange = 800f..5000f,
            steps = 41,  // 100ms increments across 4200ms range
            enabled = !state.saving,
            onCommit = {
                onUpdate(ConfigPatch(voiceVadMinSilenceMs = it.toInt()))
            },
            description = "How long below threshold before end-of-turn. Raise if it cuts you off.",
        )

        VoiceTuningSlider(
            title = "Mic gain",
            valueText = "%.2f×".format(cfg.voiceMicGain),
            value = cfg.voiceMicGain.toFloat(),
            valueRange = 0.5f..2.0f,
            steps = 29,  // 0.05 increments across 1.5 range
            enabled = !state.saving,
            onCommit = { onUpdate(ConfigPatch(voiceMicGain = it.toDouble())) },
            description = "Server-side mic-input scale (reserved — wiring lands in a later increment).",
        )
    }
}

/**
 * Increment B (voice subsystem refactor): minimal slider used by the
 * Voice Tuning section. Commits the value on slider release rather
 * than on every drag tick to avoid spamming PUT /api/config (each
 * one validates server-side and writes assistant_config.json).
 */
@Composable
private fun VoiceTuningSlider(
    title: String,
    valueText: String,
    value: Float,
    valueRange: ClosedFloatingPointRange<Float>,
    steps: Int,
    enabled: Boolean,
    description: String,
    onCommit: (Float) -> Unit,
) {
    var localValue by remember(value) { mutableStateOf(value) }

    Column(modifier = Modifier.padding(vertical = 8.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(title, style = MaterialTheme.typography.bodyMedium, modifier = Modifier.weight(1f))
            Text(valueText, style = MaterialTheme.typography.labelMedium)
        }
        Slider(
            value = localValue,
            onValueChange = { localValue = it },
            onValueChangeFinished = { onCommit(localValue) },
            valueRange = valueRange,
            steps = steps,
            enabled = enabled,
        )
        Text(
            description,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

// ─────────────────────────────────────────────────────────────────────────
// Session provider card
// ─────────────────────────────────────────────────────────────────────────

@Composable
private fun SessionProviderCard(state: SystemConfigState, onUpdate: (ConfigPatch) -> Unit) {
    val cfg = state.config ?: return
    SectionCard(icon = Icons.Default.Hub, title = "Session provider") {
        Text(
            "Which agent backs new chat sessions. Existing sessions keep their original provider.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(8.dp))
        val provIds = state.sessionProviders.map { it.id }
        DropdownField(
            label = "Provider",
            options = provIds,
            selected = cfg.provider,
            optionLabel = { id -> state.sessionProviders.firstOrNull { it.id == id }?.label ?: id },
            enabled = !state.saving,
            onSelect = { onUpdate(ConfigPatch(provider = it)) },
        )

        if (cfg.provider == "qwen") {
            Spacer(Modifier.height(8.dp))
            val currentQwen = cfg.harnessModel["qwen"] ?: ""
            DropdownField(
                label = "Model",
                // Prepend the "" option that means "CLI default".
                options = listOf("") + state.qwenHarnessModels.map { it.id },
                selected = currentQwen,
                optionLabel = { id ->
                    if (id.isEmpty()) "CLI default"
                    else state.qwenHarnessModels.firstOrNull { it.id == id }?.let { m ->
                        val badges = listOfNotNull(
                            m.contextWindow?.let { "${(it / 1000)}K ctx" },
                            if (m.supportsThinking) "thinking" else null,
                            if (m.supportsVision) "vision" else null,
                            if (m.supportsVideo) "video" else null,
                        ).joinToString(" · ")
                        if (badges.isNotEmpty()) "${m.displayName} — $badges" else m.displayName
                    } ?: id
                },
                enabled = !state.saving && state.qwenHarnessModels.isNotEmpty(),
                onSelect = { onUpdate(ConfigPatch(harnessModel = mapOf("qwen" to it))) },
            )
        }

        val desc = state.sessionProviders.firstOrNull { it.id == cfg.provider }?.description.orEmpty()
        if (desc.isNotBlank()) {
            Spacer(Modifier.height(8.dp))
            Text(desc, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        if (cfg.provider == "qwen" && state.qwenHarnessModels.isEmpty()) {
            Spacer(Modifier.height(4.dp))
            Text(
                "No models found in ~/.qwen/settings.json — run qwen once to initialize.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────
// Working directory selector (selector only — full CRUD lives on the web UI)
// ─────────────────────────────────────────────────────────────────────────

@Composable
private fun WorkingDirectoryCard(state: SystemConfigState, onUpdate: (ConfigPatch) -> Unit) {
    val cfg = state.config ?: return
    SectionCard(icon = Icons.Default.Folder, title = "Working directory") {
        Text(
            "Where new sessions start. Manage the full list (add/edit/remove) from the web frontend.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(8.dp))
        if (cfg.workingDirectoryHistory.isEmpty()) {
            EmptyHint("No working directories configured")
        } else {
            DropdownField(
                label = "Active directory",
                options = cfg.workingDirectoryHistory.map { it.id },
                selected = cfg.workingDirectory,
                optionLabel = { id ->
                    val e = cfg.workingDirectoryHistory.firstOrNull { it.id == id }
                    if (e == null) id
                    else {
                        val name = e.label.takeUnless { it.isNullOrBlank() } ?: e.path
                        if (!e.sshHost.isNullOrBlank()) "$name (${e.sshHost})" else name
                    }
                },
                enabled = !state.saving,
                onSelect = { onUpdate(ConfigPatch(workingDirectory = it)) },
            )
            Spacer(Modifier.height(6.dp))
            val active = cfg.workingDirectoryHistory.firstOrNull { it.id == cfg.workingDirectory }
            if (active != null) {
                Text(
                    active.path,
                    style = MaterialTheme.typography.bodySmall.copy(fontFamily = FontFamily.Monospace),
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                if (!active.sshHost.isNullOrBlank()) {
                    Text(
                        "via SSH: ${active.sshUser ?: ""}@${active.sshHost}",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────
// Session flags card — currently just chrome_extension
// ─────────────────────────────────────────────────────────────────────────

@Composable
private fun SessionFlagsCard(state: SystemConfigState, onUpdate: (ConfigPatch) -> Unit) {
    val cfg = state.config ?: return
    SectionCard(icon = Icons.Default.Flag, title = "Session flags") {
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(modifier = Modifier.weight(1f)) {
                Text("Chrome extension", style = MaterialTheme.typography.bodyMedium)
                Text(
                    "Launch sessions with --chrome so Claude can drive the bundled browser.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Switch(
                checked = cfg.chromeExtension,
                onCheckedChange = { onUpdate(ConfigPatch(chromeExtension = it)) },
                enabled = !state.saving,
            )
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────
// MCP servers card — toggle which configured MCP servers are enabled.
// ─────────────────────────────────────────────────────────────────────────

@Composable
private fun McpServersCard(state: SystemConfigState, onToggle: (String) -> Unit) {
    val cfg = state.config ?: return
    SectionCard(icon = Icons.Default.Extension, title = "MCP servers") {
        if (state.mcpServers.isEmpty()) {
            EmptyHint("No MCP servers configured in .claude.json")
            return@SectionCard
        }
        Text(
            "Enable or disable individual Model Context Protocol servers. Empty selection means all are enabled (legacy behavior).",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(8.dp))
        // An empty enabled_mcps list means "all enabled" (legacy behavior),
        // so when the list is empty we render every server as on.
        val emptyMeansAll = cfg.enabledMcps.isEmpty()
        state.mcpServers.keys.sorted().forEach { name ->
            val server = state.mcpServers[name]
            val enabled = emptyMeansAll || cfg.enabledMcps.contains(name)
            Row(
                modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column(modifier = Modifier.weight(1f)) {
                    Text(name, style = MaterialTheme.typography.bodyMedium)
                    if (server != null) {
                        Text(
                            server.command + (if (server.args.isNotEmpty()) " " + server.args.joinToString(" ") else ""),
                            style = MaterialTheme.typography.bodySmall.copy(fontFamily = FontFamily.Monospace),
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            maxLines = 1,
                        )
                    }
                }
                Switch(
                    checked = enabled,
                    onCheckedChange = { onToggle(name) },
                    enabled = !state.saving,
                )
            }
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────
// Shared building blocks
// ─────────────────────────────────────────────────────────────────────────

@Composable
private fun SectionCard(
    icon: androidx.compose.ui.graphics.vector.ImageVector,
    title: String,
    content: @Composable ColumnScope.() -> Unit,
) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(16.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(icon, contentDescription = null, tint = MaterialTheme.colorScheme.primary)
                Spacer(Modifier.width(12.dp))
                Text(title, style = MaterialTheme.typography.titleMedium)
            }
            Spacer(Modifier.height(12.dp))
            content()
        }
    }
}

@Composable
private fun EmptyHint(text: String) {
    Text(
        text = text,
        style = MaterialTheme.typography.bodySmall,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
    )
}

/**
 * A compact dropdown built on top of ExposedDropdownMenuBox so it works on
 * older Android versions and renders consistently against Material 3 cards.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun DropdownField(
    label: String,
    options: List<String>,
    selected: String,
    optionLabel: (String) -> String,
    enabled: Boolean,
    onSelect: (String) -> Unit,
) {
    var expanded by remember { mutableStateOf(false) }
    ExposedDropdownMenuBox(
        expanded = expanded && enabled,
        onExpandedChange = { if (enabled) expanded = !expanded },
    ) {
        OutlinedTextField(
            modifier = Modifier.fillMaxWidth().menuAnchor(),
            readOnly = true,
            enabled = enabled,
            value = optionLabel(selected),
            onValueChange = {},
            label = { Text(label) },
            trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded = expanded) },
        )
        ExposedDropdownMenu(
            expanded = expanded && enabled,
            onDismissRequest = { expanded = false },
        ) {
            options.forEach { option ->
                DropdownMenuItem(
                    text = { Text(optionLabel(option)) },
                    onClick = {
                        expanded = false
                        if (option != selected) onSelect(option)
                    },
                )
            }
        }
    }
}
