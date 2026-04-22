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
    var serverUrl by remember(settings.serverUrl) { mutableStateOf(settings.serverUrl) }
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

                    // Connection status
                    ConnectionStatusCard(connectionState)

                    Spacer(modifier = Modifier.height(16.dp))

                    // Saved Servers section
                    SavedServersSection(
                        savedServers = settings.savedServers,
                        currentUrl = settings.serverUrl,
                        onSelect = onSelectSavedServer,
                        onRemove = onRemoveSavedServer,
                        onAdd = onAddSavedServer
                    )

                    Spacer(modifier = Modifier.height(16.dp))

                    // Scan for servers button
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        OutlinedButton(
                            onClick = onScanForServers,
                            enabled = !isScanning,
                            modifier = Modifier.weight(1f)
                        ) {
                            if (isScanning) {
                                CircularProgressIndicator(
                                    modifier = Modifier.size(16.dp),
                                    strokeWidth = 2.dp
                                )
                            } else {
                                Icon(Icons.Default.NetworkWifi, contentDescription = null, modifier = Modifier.size(18.dp))
                            }
                            Spacer(modifier = Modifier.width(6.dp))
                            Text(if (isScanning) "Scanning..." else "Scan Network")
                        }
                    }

                    // Discovered servers list
                    if (discoveredServers.isNotEmpty()) {
                        Spacer(modifier = Modifier.height(8.dp))
                        Text(
                            text = "Found ${discoveredServers.size} server${if (discoveredServers.size > 1) "s" else ""}:",
                            style = MaterialTheme.typography.labelMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                        Spacer(modifier = Modifier.height(4.dp))
                        discoveredServers.forEach { server ->
                            val isSelected = settings.serverUrl == server.wsUrl
                            Surface(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .clickable { onConnectToServer(server) },
                                color = if (isSelected)
                                    MaterialTheme.colorScheme.primaryContainer
                                else
                                    MaterialTheme.colorScheme.surfaceVariant,
                                shape = MaterialTheme.shapes.small
                            ) {
                                Row(
                                    modifier = Modifier.padding(horizontal = 12.dp, vertical = 10.dp),
                                    verticalAlignment = Alignment.CenterVertically
                                ) {
                                    Icon(
                                        imageVector = if (isSelected) Icons.Default.CheckCircle else Icons.Default.Computer,
                                        contentDescription = null,
                                        modifier = Modifier.size(18.dp),
                                        tint = if (isSelected)
                                            MaterialTheme.colorScheme.primary
                                        else
                                            MaterialTheme.colorScheme.onSurfaceVariant
                                    )
                                    Spacer(modifier = Modifier.width(10.dp))
                                    Column {
                                        Text(
                                            text = server.ip,
                                            style = MaterialTheme.typography.bodyMedium
                                        )
                                        Text(
                                            text = server.wsUrl,
                                            style = MaterialTheme.typography.bodySmall,
                                            color = MaterialTheme.colorScheme.onSurfaceVariant
                                        )
                                    }
                                }
                            }
                            Spacer(modifier = Modifier.height(4.dp))
                        }
                    } else if (!isScanning) {
                        Spacer(modifier = Modifier.height(4.dp))
                        Text(
                            text = "No servers found yet. Tap Scan Network to discover.",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }

                    Spacer(modifier = Modifier.height(12.dp))

                    // Server URL input
                    OutlinedTextField(
                        value = serverUrl,
                        onValueChange = { serverUrl = it },
                        label = { Text("Server URL") },
                        placeholder = { Text("ws://192.168.0.28:8765") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true,
                        leadingIcon = {
                            Icon(Icons.Default.Link, contentDescription = null)
                        }
                    )

                    // Save URL button
                    if (serverUrl != settings.serverUrl) {
                        Spacer(modifier = Modifier.height(8.dp))
                        TextButton(
                            onClick = { onUpdateServerUrl(serverUrl) }
                        ) {
                            Icon(Icons.Default.Save, contentDescription = null)
                            Spacer(modifier = Modifier.width(4.dp))
                            Text("Save URL")
                        }
                    }

                    Spacer(modifier = Modifier.height(16.dp))

                    // Auto-connect toggle
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Column {
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

                    Spacer(modifier = Modifier.height(16.dp))

                    // Connect/Disconnect button
                    Button(
                        onClick = {
                            if (connectionState is ConnectionState.Connected) {
                                onDisconnect()
                            } else {
                                onUpdateServerUrl(serverUrl)
                                onConnect()
                            }
                        },
                        modifier = Modifier.fillMaxWidth(),
                        colors = ButtonDefaults.buttonColors(
                            containerColor = if (connectionState is ConnectionState.Connected)
                                MaterialTheme.colorScheme.error
                            else
                                MaterialTheme.colorScheme.primary
                        )
                    ) {
                        Icon(
                            imageVector = if (connectionState is ConnectionState.Connected)
                                Icons.Default.CloudOff
                            else
                                Icons.Default.CloudDone,
                            contentDescription = null
                        )
                        Spacer(modifier = Modifier.width(8.dp))
                        Text(
                            text = when (connectionState) {
                                is ConnectionState.Connected -> "Disconnect"
                                is ConnectionState.Connecting -> "Connecting..."
                                else -> "Connect"
                            }
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

@Composable
private fun SavedServersSection(
    savedServers: List<SavedServer>,
    currentUrl: String,
    onSelect: (SavedServer) -> Unit,
    onRemove: (String) -> Unit,
    onAdd: (String, String) -> Unit
) {
    var showAddForm by remember { mutableStateOf(false) }
    var newLabel by remember { mutableStateOf("") }
    var newUrl by remember { mutableStateOf("") }

    Text(
        text = "Saved Servers",
        style = MaterialTheme.typography.labelMedium,
        color = MaterialTheme.colorScheme.onSurfaceVariant
    )
    Spacer(modifier = Modifier.height(6.dp))

    if (savedServers.isEmpty() && !showAddForm) {
        Text(
            text = "No saved servers. Add one for quick switching (e.g. Tailscale IP).",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
    } else {
        savedServers.forEach { server ->
            val isSelected = currentUrl == server.url
            Surface(
                modifier = Modifier
                    .fillMaxWidth()
                    .clickable { onSelect(server) },
                color = if (isSelected)
                    MaterialTheme.colorScheme.primaryContainer
                else
                    MaterialTheme.colorScheme.surfaceVariant,
                shape = MaterialTheme.shapes.small
            ) {
                Row(
                    modifier = Modifier.padding(horizontal = 12.dp, vertical = 10.dp),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Icon(
                        imageVector = if (isSelected) Icons.Default.CheckCircle else Icons.Default.Dns,
                        contentDescription = null,
                        modifier = Modifier.size(18.dp),
                        tint = if (isSelected)
                            MaterialTheme.colorScheme.primary
                        else
                            MaterialTheme.colorScheme.onSurfaceVariant
                    )
                    Spacer(modifier = Modifier.width(10.dp))
                    Column(modifier = Modifier.weight(1f)) {
                        Text(
                            text = server.label,
                            style = MaterialTheme.typography.bodyMedium
                        )
                        Text(
                            text = server.url,
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                    IconButton(onClick = { onRemove(server.url) }) {
                        Icon(
                            imageVector = Icons.Default.Delete,
                            contentDescription = "Remove ${server.label}",
                            modifier = Modifier.size(18.dp),
                            tint = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                }
            }
            Spacer(modifier = Modifier.height(4.dp))
        }
    }

    Spacer(modifier = Modifier.height(4.dp))

    if (showAddForm) {
        OutlinedTextField(
            value = newLabel,
            onValueChange = { newLabel = it },
            label = { Text("Label") },
            placeholder = { Text("e.g. Laptop (Tailscale)") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true
        )
        Spacer(modifier = Modifier.height(4.dp))
        OutlinedTextField(
            value = newUrl,
            onValueChange = { newUrl = it },
            label = { Text("WebSocket URL") },
            placeholder = { Text("ws://100.111.80.128:8765") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true
        )
        Row {
            TextButton(onClick = {
                showAddForm = false
                newLabel = ""
                newUrl = ""
            }) { Text("Cancel") }
            Spacer(modifier = Modifier.width(4.dp))
            TextButton(
                onClick = {
                    onAdd(newLabel, newUrl)
                    showAddForm = false
                    newLabel = ""
                    newUrl = ""
                },
                enabled = newLabel.isNotBlank() && newUrl.isNotBlank()
            ) {
                Icon(Icons.Default.Save, contentDescription = null, modifier = Modifier.size(16.dp))
                Spacer(modifier = Modifier.width(4.dp))
                Text("Save")
            }
        }
    } else {
        TextButton(onClick = { showAddForm = true }) {
            Icon(Icons.Default.Add, contentDescription = null, modifier = Modifier.size(16.dp))
            Spacer(modifier = Modifier.width(4.dp))
            Text("Add server")
        }
    }
}

@Composable
private fun ConnectionStatusCard(connectionState: ConnectionState) {
    val (icon, text, color) = when (connectionState) {
        is ConnectionState.Connected -> Triple(
            Icons.Default.CheckCircle,
            "Connected",
            MaterialTheme.colorScheme.primary
        )
        is ConnectionState.Connecting -> Triple(
            Icons.Default.Sync,
            "Connecting...",
            MaterialTheme.colorScheme.tertiary
        )
        is ConnectionState.Disconnected -> Triple(
            Icons.Default.Cancel,
            "Disconnected",
            MaterialTheme.colorScheme.error
        )
        is ConnectionState.Error -> Triple(
            Icons.Default.Error,
            "Error: ${connectionState.message}",
            MaterialTheme.colorScheme.error
        )
    }

    Surface(
        color = color.copy(alpha = 0.1f),
        shape = MaterialTheme.shapes.small
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(12.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Icon(
                imageVector = icon,
                contentDescription = null,
                tint = color
            )
            Spacer(modifier = Modifier.width(8.dp))
            Text(
                text = text,
                style = MaterialTheme.typography.bodyMedium,
                color = color
            )
        }
    }
}
