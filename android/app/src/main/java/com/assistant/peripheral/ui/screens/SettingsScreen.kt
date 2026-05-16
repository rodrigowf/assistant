package com.assistant.peripheral.ui.screens

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.selection.selectableGroup
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.unit.dp
import com.assistant.peripheral.data.AppSettings
import com.assistant.peripheral.data.AudioOutput
import com.assistant.peripheral.data.ConnectionState
import com.assistant.peripheral.data.SavedServer
import com.assistant.peripheral.data.ThemeMode
import com.assistant.peripheral.network.DiscoveredServer
import kotlin.math.roundToInt

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(
    settings: AppSettings,
    connectionState: ConnectionState,
    discoveredServers: List<DiscoveredServer>,
    isScanning: Boolean,
    onUpdateServerUrl: (String) -> Unit,
    onUpdateThemeMode: (ThemeMode) -> Unit,
    onUpdateAutoConnect: (Boolean) -> Unit,
    onUpdateMicGainLevel: (Float) -> Unit,
    onUpdateWakeWordMicGainLevel: (Float) -> Unit,
    onUpdateSpeakerVolumeLevel: (Float) -> Unit,
    onUpdateEchoDuckingGain: (Float) -> Unit,
    onUpdateAudioOutput: (AudioOutput) -> Unit,
    isBluetoothAvailable: Boolean,
    onUpdateEnableWakeWord: (Boolean) -> Unit,
    onUpdateWakeWord: (String) -> Unit,
    onUpdateVoiceWord: (String) -> Unit,
    onUpdateEnableButtonTrigger: (Boolean) -> Unit,
    onConnect: () -> Unit,
    onDisconnect: () -> Unit,
    onScanForServers: () -> Unit,
    onConnectToServer: (DiscoveredServer) -> Unit,
    onAddSavedServer: (String, String) -> Unit,
    onRemoveSavedServer: (String) -> Unit,
    onSelectSavedServer: (SavedServer) -> Unit,
    modifier: Modifier = Modifier
) {
    var wakeWordText by remember(settings.wakeWord) { mutableStateOf(settings.wakeWord) }
    var voiceWordText by remember(settings.voiceWord) { mutableStateOf(settings.voiceWord) }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Settings") }
            )
        }
    ) { padding ->
        Column(
            modifier = modifier
                .fillMaxSize()
                .padding(padding)
                .verticalScroll(rememberScrollState())
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp)
        ) {
            // Connection Section
            Card(
                modifier = Modifier.fillMaxWidth()
            ) {
                Column(
                    modifier = Modifier.padding(16.dp)
                ) {
                    Row(
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Icon(
                            imageVector = Icons.Default.Cloud,
                            contentDescription = null,
                            tint = MaterialTheme.colorScheme.primary
                        )
                        Spacer(modifier = Modifier.width(12.dp))
                        Text(
                            text = "Server Connection",
                            style = MaterialTheme.typography.titleMedium
                        )
                    }

                    Spacer(modifier = Modifier.height(16.dp))

                    // Unified servers list (saved + discovered, de-duplicated by URL)
                    ServersSection(
                        savedServers = settings.savedServers,
                        discoveredServers = discoveredServers,
                        currentUrl = settings.serverUrl,
                        isScanning = isScanning,
                        onSelectSaved = onSelectSavedServer,
                        onConnectDiscovered = onConnectToServer,
                        onRemove = onRemoveSavedServer,
                        onAddOrUpdate = onAddSavedServer,
                        onScan = onScanForServers
                    )

                    Spacer(modifier = Modifier.height(16.dp))
                    Divider()
                    Spacer(modifier = Modifier.height(16.dp))

                    // Status + connect button on a single row.
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        ConnectionStatusPill(
                            connectionState = connectionState,
                            modifier = Modifier.weight(1f)
                        )
                        Spacer(modifier = Modifier.width(12.dp))
                        Button(
                            onClick = {
                                if (connectionState is ConnectionState.Connected) onDisconnect()
                                else onConnect()
                            },
                            colors = ButtonDefaults.buttonColors(
                                containerColor = if (connectionState is ConnectionState.Connected)
                                    MaterialTheme.colorScheme.errorContainer
                                else
                                    MaterialTheme.colorScheme.primary,
                                contentColor = if (connectionState is ConnectionState.Connected)
                                    MaterialTheme.colorScheme.onErrorContainer
                                else
                                    MaterialTheme.colorScheme.onPrimary
                            )
                        ) {
                            Text(
                                text = when (connectionState) {
                                    is ConnectionState.Connected -> "Disconnect"
                                    is ConnectionState.Connecting -> "Connecting…"
                                    else -> "Connect"
                                }
                            )
                        }
                    }

                    Spacer(modifier = Modifier.height(12.dp))

                    // Auto-connect toggle
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Column(modifier = Modifier.weight(1f)) {
                            Text(
                                text = "Auto-connect",
                                style = MaterialTheme.typography.bodyMedium
                            )
                            Text(
                                text = "Connect automatically on app start",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant
                            )
                        }
                        Switch(
                            checked = settings.autoConnect,
                            onCheckedChange = onUpdateAutoConnect
                        )
                    }
                }
            }

            // Audio Section
            Card(
                modifier = Modifier.fillMaxWidth()
            ) {
                Column(
                    modifier = Modifier.padding(16.dp)
                ) {
                    Row(
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Icon(
                            imageVector = Icons.Default.Mic,
                            contentDescription = null,
                            tint = MaterialTheme.colorScheme.primary
                        )
                        Spacer(modifier = Modifier.width(12.dp))
                        Text(
                            text = "Audio",
                            style = MaterialTheme.typography.titleMedium
                        )
                    }

                    Spacer(modifier = Modifier.height(16.dp))

                    // Uniform steps 0..150% in steps of 10 (16 steps total)
                    val volumeSteps = (0..150 step 10).map { it.toFloat() }
                    val defaultVolumeIndex = volumeSteps.indexOf(100f).toFloat()

                    // --- Microphone Gain ---
                    val currentGainPercent = (settings.micGainLevel * 100).roundToInt().toFloat()
                    var micSliderIndex by remember(settings.micGainLevel) {
                        val closest = volumeSteps.minByOrNull { kotlin.math.abs(it - currentGainPercent) }
                        mutableFloatStateOf(volumeSteps.indexOf(closest).coerceAtLeast(0).toFloat())
                    }
                    val micDisplayPercent = volumeSteps.getOrElse(micSliderIndex.roundToInt()) { 100f }.roundToInt()

                    Text("Microphone Gain: $micDisplayPercent%", style = MaterialTheme.typography.bodyMedium)
                    Spacer(modifier = Modifier.height(8.dp))
                    Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                        Icon(Icons.Default.VolumeDown, contentDescription = "Low", modifier = Modifier.size(20.dp), tint = MaterialTheme.colorScheme.onSurfaceVariant)
                        Slider(
                            value = micSliderIndex,
                            onValueChange = { micSliderIndex = it },
                            onValueChangeFinished = { onUpdateMicGainLevel(volumeSteps.getOrElse(micSliderIndex.roundToInt()) { 100f } / 100f) },
                            valueRange = 0f..(volumeSteps.size - 1).toFloat(),
                            steps = volumeSteps.size - 2,
                            modifier = Modifier.weight(1f).padding(horizontal = 8.dp)
                        )
                        Icon(Icons.Default.VolumeUp, contentDescription = "High", modifier = Modifier.size(20.dp), tint = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    if (micDisplayPercent != 100) {
                        TextButton(onClick = { micSliderIndex = defaultVolumeIndex; onUpdateMicGainLevel(1.0f) }) {
                            Icon(Icons.Default.Refresh, contentDescription = null, modifier = Modifier.size(16.dp))
                            Spacer(modifier = Modifier.width(4.dp))
                            Text("Reset to 100%")
                        }
                    }

                    Spacer(modifier = Modifier.height(16.dp))

                    // --- Speaker Volume ---
                    val currentSpeakerPercent = (settings.speakerVolumeLevel * 100).roundToInt().toFloat()
                    var speakerSliderIndex by remember(settings.speakerVolumeLevel) {
                        val closest = volumeSteps.minByOrNull { kotlin.math.abs(it - currentSpeakerPercent) }
                        mutableFloatStateOf(volumeSteps.indexOf(closest).coerceAtLeast(0).toFloat())
                    }
                    val speakerDisplayPercent = volumeSteps.getOrElse(speakerSliderIndex.roundToInt()) { 100f }.roundToInt()

                    Text("Speaker Volume: $speakerDisplayPercent%", style = MaterialTheme.typography.bodyMedium)
                    Spacer(modifier = Modifier.height(8.dp))
                    Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                        Icon(Icons.Default.VolumeDown, contentDescription = "Low", modifier = Modifier.size(20.dp), tint = MaterialTheme.colorScheme.onSurfaceVariant)
                        Slider(
                            value = speakerSliderIndex,
                            onValueChange = { speakerSliderIndex = it },
                            onValueChangeFinished = { onUpdateSpeakerVolumeLevel(volumeSteps.getOrElse(speakerSliderIndex.roundToInt()) { 100f } / 100f) },
                            valueRange = 0f..(volumeSteps.size - 1).toFloat(),
                            steps = volumeSteps.size - 2,
                            modifier = Modifier.weight(1f).padding(horizontal = 8.dp)
                        )
                        Icon(Icons.Default.VolumeUp, contentDescription = "High", modifier = Modifier.size(20.dp), tint = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    if (speakerDisplayPercent != 100) {
                        TextButton(onClick = { speakerSliderIndex = defaultVolumeIndex; onUpdateSpeakerVolumeLevel(1.0f) }) {
                            Icon(Icons.Default.Refresh, contentDescription = null, modifier = Modifier.size(16.dp))
                            Spacer(modifier = Modifier.width(4.dp))
                            Text("Reset to 100%")
                        }
                    }

                    Spacer(modifier = Modifier.height(16.dp))

                    // --- Echo Ducking Gain ---
                    // Steps: 0.0%, 0.5%, 1.0%, ..., 10.0% (21 steps)
                    // gain sent to VoiceManager = duckDisplayValue / 100f (e.g. 5.0% → 0.05)
                    val duckSteps = (0..20).map { it * 0.5f }  // 0.0..10.0 representing percent
                    val currentDuckPercent = settings.echoDuckingGain * 100f  // e.g. 0.05 → 5.0%
                    var duckSliderIndex by remember(settings.echoDuckingGain) {
                        val closest = duckSteps.minByOrNull { kotlin.math.abs(it - currentDuckPercent) }
                        mutableFloatStateOf(duckSteps.indexOf(closest).coerceAtLeast(0).toFloat())
                    }
                    val duckDisplayValue = duckSteps.getOrElse(duckSliderIndex.roundToInt()) { 5.0f }

                    Text("Echo Ducking: ${"%.1f".format(duckDisplayValue)}%", style = MaterialTheme.typography.bodyMedium)
                    Text(
                        "Mic gain while agent speaks — lower reduces echo, higher allows interruption",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                        Icon(Icons.Default.VolumeDown, contentDescription = "Low", modifier = Modifier.size(20.dp), tint = MaterialTheme.colorScheme.onSurfaceVariant)
                        Slider(
                            value = duckSliderIndex,
                            onValueChange = { duckSliderIndex = it },
                            onValueChangeFinished = {
                                val gain = duckSteps.getOrElse(duckSliderIndex.roundToInt()) { 5.0f } / 100f
                                onUpdateEchoDuckingGain(gain)
                            },
                            valueRange = 0f..(duckSteps.size - 1).toFloat(),
                            steps = duckSteps.size - 2,
                            modifier = Modifier.weight(1f).padding(horizontal = 8.dp)
                        )
                        Icon(Icons.Default.VolumeUp, contentDescription = "High", modifier = Modifier.size(20.dp), tint = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    if (duckDisplayValue != 5.0f) {
                        TextButton(onClick = {
                            // Reset to 5% default (gain 0.05, step index 10)
                            duckSliderIndex = 10f
                            onUpdateEchoDuckingGain(0.05f)
                        }) {
                            Icon(Icons.Default.Refresh, contentDescription = null, modifier = Modifier.size(16.dp))
                            Spacer(modifier = Modifier.width(4.dp))
                            Text("Reset to 5%")
                        }
                    }

                    Spacer(modifier = Modifier.height(16.dp))

                    // Audio output routing — 3 options: Earpiece, Loudspeaker, Bluetooth.
                    // Bluetooth is grayed out when no BT audio device is connected.
                    Column(modifier = Modifier.fillMaxWidth()) {
                        Text(
                            text = "Audio Output",
                            style = MaterialTheme.typography.bodyMedium
                        )
                        Text(
                            text = when (settings.audioOutput) {
                                AudioOutput.EARPIECE -> "Audio routed to earpiece"
                                AudioOutput.LOUDSPEAKER -> "Audio routed to loudspeaker"
                                AudioOutput.BLUETOOTH ->
                                    if (isBluetoothAvailable) "Audio routed to Bluetooth device"
                                    else "No Bluetooth device connected"
                            },
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                        Spacer(modifier = Modifier.height(12.dp))
                        // Round icon-only toggle buttons. Selection is communicated by the
                        // filled background; the subtitle above spells out the current choice.
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.spacedBy(16.dp),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            val options = listOf(
                                Triple(AudioOutput.EARPIECE, "Earpiece", Icons.Default.Hearing),
                                Triple(AudioOutput.LOUDSPEAKER, "Speaker", Icons.Default.VolumeUp),
                                Triple(AudioOutput.BLUETOOTH, "Bluetooth", Icons.Default.Bluetooth),
                            )
                            options.forEach { (output, label, icon) ->
                                val enabled = output != AudioOutput.BLUETOOTH || isBluetoothAvailable
                                FilledIconToggleButton(
                                    checked = settings.audioOutput == output,
                                    onCheckedChange = { if (it) onUpdateAudioOutput(output) },
                                    enabled = enabled,
                                    modifier = Modifier.size(56.dp)
                                ) {
                                    Icon(
                                        imageVector = icon,
                                        contentDescription = label,
                                        modifier = Modifier.size(24.dp)
                                    )
                                }
                            }
                        }
                    }

                    Spacer(modifier = Modifier.height(16.dp))

                    Divider()

                    Spacer(modifier = Modifier.height(16.dp))

                    // Wake Word toggle
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Column(modifier = Modifier.weight(1f)) {
                            Text(
                                text = "Wake Word Detection",
                                style = MaterialTheme.typography.bodyMedium
                            )
                            Text(
                                text = "Listen for wake word to start voice session",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant
                            )
                        }
                        Switch(
                            checked = settings.enableWakeWord,
                            onCheckedChange = onUpdateEnableWakeWord
                        )
                    }

                    if (settings.enableWakeWord) {
                        Spacer(modifier = Modifier.height(12.dp))

                        // Turn-based voice input wake word
                        OutlinedTextField(
                            value = wakeWordText,
                            onValueChange = { wakeWordText = it },
                            label = { Text("Voice Input Words") },
                            placeholder = { Text("hey assistant, assistant") },
                            modifier = Modifier.fillMaxWidth(),
                            singleLine = true,
                            supportingText = { Text("Comma-separated phrases — any match starts turn-based recording") },
                            leadingIcon = {
                                Icon(Icons.Default.Mic, contentDescription = null)
                            }
                        )

                        if (wakeWordText != settings.wakeWord && wakeWordText.isNotBlank()) {
                            TextButton(onClick = { onUpdateWakeWord(wakeWordText) }) {
                                Icon(Icons.Default.Save, contentDescription = null, modifier = Modifier.size(16.dp))
                                Spacer(modifier = Modifier.width(4.dp))
                                Text("Save")
                            }
                        }

                        Spacer(modifier = Modifier.height(8.dp))

                        // Realtime voice session wake word
                        OutlinedTextField(
                            value = voiceWordText,
                            onValueChange = { voiceWordText = it },
                            label = { Text("Realtime Voice Words") },
                            placeholder = { Text("hey realtime, realtime") },
                            modifier = Modifier.fillMaxWidth(),
                            singleLine = true,
                            supportingText = { Text("Comma-separated phrases — any match starts realtime WebRTC conversation") },
                            leadingIcon = {
                                Icon(Icons.Default.RecordVoiceOver, contentDescription = null)
                            }
                        )

                        if (voiceWordText != settings.voiceWord && voiceWordText.isNotBlank()) {
                            TextButton(onClick = { onUpdateVoiceWord(voiceWordText) }) {
                                Icon(Icons.Default.Save, contentDescription = null, modifier = Modifier.size(16.dp))
                                Spacer(modifier = Modifier.width(4.dp))
                                Text("Save")
                            }
                        }

                        Spacer(modifier = Modifier.height(12.dp))

                        // Wake word mic sensitivity slider — uniform 0..150% in steps of 10
                        val currentWakeGainPercent = (settings.wakeWordMicGainLevel * 100).roundToInt().toFloat()
                        var wakeSliderIndex by remember(settings.wakeWordMicGainLevel) {
                            val closest = volumeSteps.minByOrNull { kotlin.math.abs(it - currentWakeGainPercent) }
                            mutableFloatStateOf(volumeSteps.indexOf(closest).coerceAtLeast(0).toFloat())
                        }
                        val wakeDisplayPercent = volumeSteps.getOrElse(wakeSliderIndex.roundToInt()) { 100f }.roundToInt()

                        Text("Wake Word Sensitivity: $wakeDisplayPercent%", style = MaterialTheme.typography.bodyMedium)
                        Text(
                            "Higher = easier to trigger (independent of voice session gain)",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                        Spacer(modifier = Modifier.height(8.dp))
                        Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                            Icon(Icons.Default.VolumeDown, contentDescription = "Low", modifier = Modifier.size(20.dp), tint = MaterialTheme.colorScheme.onSurfaceVariant)
                            Slider(
                                value = wakeSliderIndex,
                                onValueChange = { wakeSliderIndex = it },
                                onValueChangeFinished = { onUpdateWakeWordMicGainLevel(volumeSteps.getOrElse(wakeSliderIndex.roundToInt()) { 100f } / 100f) },
                                valueRange = 0f..(volumeSteps.size - 1).toFloat(),
                                steps = volumeSteps.size - 2,
                                modifier = Modifier.weight(1f).padding(horizontal = 8.dp)
                            )
                            Icon(Icons.Default.VolumeUp, contentDescription = "High", modifier = Modifier.size(20.dp), tint = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                        if (wakeDisplayPercent != 100) {
                            TextButton(onClick = { wakeSliderIndex = defaultVolumeIndex; onUpdateWakeWordMicGainLevel(1.0f) }) {
                                Icon(Icons.Default.Refresh, contentDescription = null, modifier = Modifier.size(16.dp))
                                Spacer(modifier = Modifier.width(4.dp))
                                Text("Reset to 100%")
                            }
                        }

                        Spacer(modifier = Modifier.height(4.dp))

                        Text(
                            text = "Each phrase also matches common mishearings automatically",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }

                    Spacer(modifier = Modifier.height(16.dp))

                    Divider()

                    Spacer(modifier = Modifier.height(16.dp))

                    // Recents button long-press trigger
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Column(modifier = Modifier.weight(1f)) {
                            Text(
                                text = "Recents Button Trigger",
                                style = MaterialTheme.typography.bodyMedium
                            )
                            Text(
                                text = "Long-press recents button to start voice session (screen must be on)",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant
                            )
                        }
                        Switch(
                            checked = settings.enableButtonTrigger,
                            onCheckedChange = onUpdateEnableButtonTrigger
                        )
                    }
                }
            }

            // Appearance Section
            Card(
                modifier = Modifier.fillMaxWidth()
            ) {
                Column(
                    modifier = Modifier.padding(16.dp)
                ) {
                    Row(
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Icon(
                            imageVector = Icons.Default.Palette,
                            contentDescription = null,
                            tint = MaterialTheme.colorScheme.primary
                        )
                        Spacer(modifier = Modifier.width(12.dp))
                        Text(
                            text = "Appearance",
                            style = MaterialTheme.typography.titleMedium
                        )
                    }

                    Spacer(modifier = Modifier.height(16.dp))

                    Text(
                        text = "Theme",
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )

                    Spacer(modifier = Modifier.height(8.dp))

                    // Theme selection
                    Column(Modifier.selectableGroup()) {
                        ThemeMode.values().forEach { mode ->
                            Row(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .selectable(
                                        selected = settings.themeMode == mode,
                                        onClick = { onUpdateThemeMode(mode) },
                                        role = Role.RadioButton
                                    )
                                    .padding(vertical = 12.dp),
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                RadioButton(
                                    selected = settings.themeMode == mode,
                                    onClick = null
                                )
                                Spacer(modifier = Modifier.width(12.dp))
                                Icon(
                                    imageVector = when (mode) {
                                        ThemeMode.SYSTEM -> Icons.Default.BrightnessAuto
                                        ThemeMode.LIGHT -> Icons.Default.LightMode
                                        ThemeMode.DARK -> Icons.Default.DarkMode
                                    },
                                    contentDescription = null,
                                    modifier = Modifier.size(20.dp),
                                    tint = MaterialTheme.colorScheme.onSurfaceVariant
                                )
                                Spacer(modifier = Modifier.width(8.dp))
                                Text(
                                    text = when (mode) {
                                        ThemeMode.SYSTEM -> "System default"
                                        ThemeMode.LIGHT -> "Light"
                                        ThemeMode.DARK -> "Dark"
                                    },
                                    style = MaterialTheme.typography.bodyMedium
                                )
                            }
                        }
                    }
                }
            }

            // About Section
            Card(
                modifier = Modifier.fillMaxWidth()
            ) {
                Column(
                    modifier = Modifier.padding(16.dp)
                ) {
                    Row(
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Icon(
                            imageVector = Icons.Default.Info,
                            contentDescription = null,
                            tint = MaterialTheme.colorScheme.primary
                        )
                        Spacer(modifier = Modifier.width(12.dp))
                        Text(
                            text = "About",
                            style = MaterialTheme.typography.titleMedium
                        )
                    }

                    Spacer(modifier = Modifier.height(16.dp))

                    Text(
                        text = "Assistant Peripheral",
                        style = MaterialTheme.typography.bodyLarge
                    )
                    Text(
                        text = "Version 1.0.0",
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )

                    Spacer(modifier = Modifier.height(8.dp))

                    Text(
                        text = "A mobile companion app for your personal assistant. " +
                               "Connect to your server to chat via text or voice, " +
                               "manage conversations, and access your assistant on the go.",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
            }
        }
    }
}

private sealed class ServerEntry {
    abstract val url: String
    abstract val label: String

    data class Saved(val server: SavedServer, val alsoDiscovered: Boolean) : ServerEntry() {
        override val url: String get() = server.url
        override val label: String get() = server.label
    }

    data class Discovered(val server: DiscoveredServer) : ServerEntry() {
        override val url: String get() = server.wsUrl
        override val label: String get() = server.ip
    }
}

@Composable
private fun ServersSection(
    savedServers: List<SavedServer>,
    discoveredServers: List<DiscoveredServer>,
    currentUrl: String,
    isScanning: Boolean,
    onSelectSaved: (SavedServer) -> Unit,
    onConnectDiscovered: (DiscoveredServer) -> Unit,
    onRemove: (String) -> Unit,
    onAddOrUpdate: (String, String) -> Unit,
    onScan: () -> Unit
) {
    // Dialog state. `null` = closed; non-null carries prefill values.
    var editor by remember { mutableStateOf<ServerEditorState?>(null) }

    // Merge: saved first (sorted by label), then any discovered URLs we haven't saved.
    val savedUrls = savedServers.map { it.url }.toSet()
    val discoveredUrls = discoveredServers.map { it.wsUrl }.toSet()
    val entries: List<ServerEntry> =
        savedServers.map { ServerEntry.Saved(it, alsoDiscovered = it.url in discoveredUrls) } +
            discoveredServers.filter { it.wsUrl !in savedUrls }.map { ServerEntry.Discovered(it) }

    if (entries.isEmpty()) {
        Text(
            text = "No servers yet. Scan the network or add one manually.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
        Spacer(modifier = Modifier.height(8.dp))
    } else {
        entries.forEachIndexed { index, entry ->
            ServerRow(
                entry = entry,
                isSelected = currentUrl == entry.url,
                onClick = {
                    when (entry) {
                        is ServerEntry.Saved -> onSelectSaved(entry.server)
                        is ServerEntry.Discovered -> onConnectDiscovered(entry.server)
                    }
                },
                onEdit = {
                    val s = (entry as ServerEntry.Saved).server
                    editor = ServerEditorState(originalUrl = s.url, label = s.label, url = s.url)
                },
                onSave = {
                    val d = (entry as ServerEntry.Discovered).server
                    editor = ServerEditorState(originalUrl = null, label = d.ip, url = d.wsUrl)
                },
                onRemove = { onRemove(entry.url) }
            )
            if (index < entries.lastIndex) Spacer(modifier = Modifier.height(6.dp))
        }
    }

    Spacer(modifier = Modifier.height(12.dp))

    // Action row: Scan + Add manually
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        OutlinedButton(
            onClick = onScan,
            enabled = !isScanning,
            modifier = Modifier.weight(1f)
        ) {
            if (isScanning) {
                CircularProgressIndicator(modifier = Modifier.size(16.dp), strokeWidth = 2.dp)
            } else {
                Icon(Icons.Default.NetworkWifi, contentDescription = null, modifier = Modifier.size(18.dp))
            }
            Spacer(modifier = Modifier.width(6.dp))
            Text(if (isScanning) "Scanning…" else "Scan")
        }
        OutlinedButton(
            onClick = { editor = ServerEditorState(originalUrl = null, label = "", url = "") },
            modifier = Modifier.weight(1f)
        ) {
            Icon(Icons.Default.Add, contentDescription = null, modifier = Modifier.size(18.dp))
            Spacer(modifier = Modifier.width(6.dp))
            Text("Add")
        }
    }

    editor?.let { state ->
        ServerEditorDialog(
            state = state,
            onDismiss = { editor = null },
            onSubmit = { label, url ->
                if (state.originalUrl != null && state.originalUrl != url) {
                    onRemove(state.originalUrl)
                }
                onAddOrUpdate(label, url)
                editor = null
            }
        )
    }
}

@Composable
private fun ServerRow(
    entry: ServerEntry,
    isSelected: Boolean,
    onClick: () -> Unit,
    onEdit: () -> Unit,
    onSave: () -> Unit,
    onRemove: () -> Unit
) {
    Surface(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onClick),
        color = if (isSelected)
            MaterialTheme.colorScheme.primaryContainer
        else
            MaterialTheme.colorScheme.surfaceVariant,
        shape = MaterialTheme.shapes.medium
    ) {
        Row(
            modifier = Modifier.padding(horizontal = 12.dp, vertical = 10.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            val (leadingIcon, iconTint) = when {
                isSelected -> Icons.Default.CheckCircle to MaterialTheme.colorScheme.primary
                entry is ServerEntry.Discovered -> Icons.Default.NetworkWifi to MaterialTheme.colorScheme.onSurfaceVariant
                else -> Icons.Default.Dns to MaterialTheme.colorScheme.onSurfaceVariant
            }
            Icon(
                imageVector = leadingIcon,
                contentDescription = null,
                modifier = Modifier.size(20.dp),
                tint = iconTint
            )
            Spacer(modifier = Modifier.width(12.dp))
            Column(modifier = Modifier.weight(1f)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        text = entry.label,
                        style = MaterialTheme.typography.bodyMedium
                    )
                    if (entry is ServerEntry.Saved && entry.alsoDiscovered) {
                        Spacer(modifier = Modifier.width(6.dp))
                        Icon(
                            imageVector = Icons.Default.NetworkWifi,
                            contentDescription = "On network",
                            modifier = Modifier.size(12.dp),
                            tint = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                }
                Text(
                    text = entry.url,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
            when (entry) {
                is ServerEntry.Saved -> {
                    IconButton(onClick = onEdit) {
                        Icon(
                            imageVector = Icons.Default.Edit,
                            contentDescription = "Edit ${entry.label}",
                            modifier = Modifier.size(18.dp),
                            tint = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                    IconButton(onClick = onRemove) {
                        Icon(
                            imageVector = Icons.Default.Delete,
                            contentDescription = "Remove ${entry.label}",
                            modifier = Modifier.size(18.dp),
                            tint = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                }
                is ServerEntry.Discovered -> {
                    TextButton(onClick = onSave) {
                        Icon(Icons.Default.BookmarkBorder, contentDescription = null, modifier = Modifier.size(16.dp))
                        Spacer(modifier = Modifier.width(4.dp))
                        Text("Save")
                    }
                }
            }
        }
    }
}

private data class ServerEditorState(
    val originalUrl: String?,
    val label: String,
    val url: String
)

@Composable
private fun ServerEditorDialog(
    state: ServerEditorState,
    onDismiss: () -> Unit,
    onSubmit: (String, String) -> Unit
) {
    var label by remember(state) { mutableStateOf(state.label) }
    var url by remember(state) { mutableStateOf(state.url) }
    val isEditing = state.originalUrl != null
    val canSubmit = label.isNotBlank() && url.isNotBlank()

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(if (isEditing) "Edit server" else "Add server") },
        text = {
            Column {
                OutlinedTextField(
                    value = label,
                    onValueChange = { label = it },
                    label = { Text("Label") },
                    placeholder = { Text("e.g. Laptop (Tailscale)") },
                    modifier = Modifier.fillMaxWidth(),
                    singleLine = true
                )
                Spacer(modifier = Modifier.height(8.dp))
                OutlinedTextField(
                    value = url,
                    onValueChange = { url = it },
                    label = { Text("WebSocket URL") },
                    placeholder = { Text("ws://192.168.0.200:80") },
                    modifier = Modifier.fillMaxWidth(),
                    singleLine = true
                )
            }
        },
        confirmButton = {
            TextButton(
                onClick = { onSubmit(label.trim(), url.trim()) },
                enabled = canSubmit
            ) { Text(if (isEditing) "Save" else "Add") }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) { Text("Cancel") }
        }
    )
}

@Composable
private fun ConnectionStatusPill(
    connectionState: ConnectionState,
    modifier: Modifier = Modifier
) {
    val (icon, text, color) = when (connectionState) {
        is ConnectionState.Connected -> Triple(
            Icons.Default.CheckCircle,
            "Connected",
            MaterialTheme.colorScheme.primary
        )
        is ConnectionState.Connecting -> Triple(
            Icons.Default.Sync,
            "Connecting…",
            MaterialTheme.colorScheme.tertiary
        )
        is ConnectionState.Disconnected -> Triple(
            Icons.Default.Cancel,
            "Disconnected",
            MaterialTheme.colorScheme.onSurfaceVariant
        )
        is ConnectionState.Error -> Triple(
            Icons.Default.Error,
            connectionState.message.ifBlank { "Error" },
            MaterialTheme.colorScheme.error
        )
    }

    Row(
        modifier = modifier,
        verticalAlignment = Alignment.CenterVertically
    ) {
        Icon(
            imageVector = icon,
            contentDescription = null,
            tint = color,
            modifier = Modifier.size(18.dp)
        )
        Spacer(modifier = Modifier.width(8.dp))
        Text(
            text = text,
            style = MaterialTheme.typography.bodyMedium,
            color = color
        )
    }
}
