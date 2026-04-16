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
import com.assistant.peripheral.data.ConnectionState
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
    onUpdateEarpieceMode: (Boolean) -> Unit,
    onUpdateEnableWakeWord: (Boolean) -> Unit,
    onUpdateWakeWord: (String) -> Unit,
    onUpdateVoiceWord: (String) -> Unit,
    onUpdateEnableButtonTrigger: (Boolean) -> Unit,
    onConnect: () -> Unit,
    onDisconnect: () -> Unit,
    onScanForServers: () -> Unit,
    onConnectToServer: (DiscoveredServer) -> Unit,
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

                    // Mic gain slider with Fibonacci-like steps for finer control at low values
                    // Steps: 0, 1, 2, 3, 5, 8, 13, 21, 34, 55, 100, 150, 200%
                    val gainSteps = listOf(0f, 1f, 2f, 3f, 5f, 8f, 13f, 21f, 34f, 55f, 100f, 150f, 200f)

                    // Find closest step index for current value
                    val currentGainPercent = (settings.micGainLevel * 100).roundToInt().toFloat()
                    var sliderIndex by remember(settings.micGainLevel) {
                        mutableFloatStateOf(gainSteps.indexOfFirst { it >= currentGainPercent }.coerceAtLeast(0).toFloat())
                    }
                    val displayPercent = gainSteps.getOrElse(sliderIndex.roundToInt()) { 100f }.roundToInt()

                    Text(
                        text = "Microphone Gain: $displayPercent%",
                        style = MaterialTheme.typography.bodyMedium
                    )

                    Spacer(modifier = Modifier.height(8.dp))

                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Icon(
                            imageVector = Icons.Default.VolumeDown,
                            contentDescription = "Low",
                            modifier = Modifier.size(20.dp),
                            tint = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                        Slider(
                            value = sliderIndex,
                            onValueChange = { sliderIndex = it },
                            onValueChangeFinished = {
                                val gainPercent = gainSteps.getOrElse(sliderIndex.roundToInt()) { 100f }
                                onUpdateMicGainLevel(gainPercent / 100f)
                            },
                            valueRange = 0f..(gainSteps.size - 1).toFloat(),
                            steps = gainSteps.size - 2,  // -2 because endpoints don't count as steps
                            modifier = Modifier
                                .weight(1f)
                                .padding(horizontal = 8.dp)
                        )
                        Icon(
                            imageVector = Icons.Default.VolumeUp,
                            contentDescription = "High",
                            modifier = Modifier.size(20.dp),
                            tint = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }

                    // Reset button (default is 100% = index 10)
                    if (displayPercent != 100) {
                        TextButton(
                            onClick = {
                                sliderIndex = 10f  // Index of 100 in gainSteps
                                onUpdateMicGainLevel(1.0f)
                            }
                        ) {
                            Icon(Icons.Default.Refresh, contentDescription = null, modifier = Modifier.size(16.dp))
                            Spacer(modifier = Modifier.width(4.dp))
                            Text("Reset to 100%")
                        }
                    }

                    Spacer(modifier = Modifier.height(16.dp))

                    // Speaker volume slider with uniformly distributed steps: 0..100..150%
                    val speakerSteps = listOf(0f, 10f, 20f, 30f, 40f, 50f, 60f, 70f, 80f, 90f, 100f, 120f, 150f)

                    // Find closest step index for current value
                    val currentSpeakerPercent = (settings.speakerVolumeLevel * 100).roundToInt().toFloat()
                    var speakerSliderIndex by remember(settings.speakerVolumeLevel) {
                        val closest = speakerSteps.minByOrNull { kotlin.math.abs(it - currentSpeakerPercent) }
                        mutableFloatStateOf(speakerSteps.indexOf(closest).coerceAtLeast(0).toFloat())
                    }
                    val speakerDisplayPercent = speakerSteps.getOrElse(speakerSliderIndex.roundToInt()) { 100f }.roundToInt()

                    Text(
                        text = "Speaker Volume: $speakerDisplayPercent%",
                        style = MaterialTheme.typography.bodyMedium
                    )

                    Spacer(modifier = Modifier.height(8.dp))

                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Icon(
                            imageVector = Icons.Default.VolumeDown,
                            contentDescription = "Low",
                            modifier = Modifier.size(20.dp),
                            tint = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                        Slider(
                            value = speakerSliderIndex,
                            onValueChange = { speakerSliderIndex = it },
                            onValueChangeFinished = {
                                val volPercent = speakerSteps.getOrElse(speakerSliderIndex.roundToInt()) { 100f }
                                onUpdateSpeakerVolumeLevel(volPercent / 100f)
                            },
                            valueRange = 0f..(speakerSteps.size - 1).toFloat(),
                            steps = speakerSteps.size - 2,  // -2 because endpoints don't count as steps
                            modifier = Modifier
                                .weight(1f)
                                .padding(horizontal = 8.dp)
                        )
                        Icon(
                            imageVector = Icons.Default.VolumeUp,
                            contentDescription = "High",
                            modifier = Modifier.size(20.dp),
                            tint = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }

                    // Reset button for speaker (default is 100% = index 10)
                    if (speakerDisplayPercent != 100) {
                        TextButton(
                            onClick = {
                                speakerSliderIndex = 10f  // Index of 100 in speakerSteps
                                onUpdateSpeakerVolumeLevel(1.0f)
                            }
                        ) {
                            Icon(Icons.Default.Refresh, contentDescription = null, modifier = Modifier.size(16.dp))
                            Spacer(modifier = Modifier.width(4.dp))
                            Text("Reset to 100%")
                        }
                    }

                    Spacer(modifier = Modifier.height(16.dp))

                    // Earpiece mode toggle
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Column(modifier = Modifier.weight(1f)) {
                            Text(
                                text = "Earpiece Mode",
                                style = MaterialTheme.typography.bodyMedium
                            )
                            Text(
                                text = if (settings.useEarpiece) "Audio routed to earpiece" else "Audio routed to loudspeaker",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant
                            )
                        }
                        Switch(
                            checked = settings.useEarpiece,
                            onCheckedChange = { onUpdateEarpieceMode(it) }
                        )
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

                        // Wake word mic sensitivity slider — independent of voice session gain.
                        // Scales the RMS threshold: higher gain → more sensitive detection.
                        val wakeGainSteps = listOf(0f, 1f, 2f, 3f, 5f, 8f, 13f, 21f, 34f, 55f, 100f, 150f, 200f)
                        val currentWakeGainPercent = (settings.wakeWordMicGainLevel * 100).roundToInt().toFloat()
                        var wakeSliderIndex by remember(settings.wakeWordMicGainLevel) {
                            mutableFloatStateOf(wakeGainSteps.indexOfFirst { it >= currentWakeGainPercent }.coerceAtLeast(0).toFloat())
                        }
                        val wakeDisplayPercent = wakeGainSteps.getOrElse(wakeSliderIndex.roundToInt()) { 100f }.roundToInt()

                        Text(
                            text = "Wake Word Sensitivity: $wakeDisplayPercent%",
                            style = MaterialTheme.typography.bodyMedium
                        )
                        Text(
                            text = "Higher = easier to trigger (independent of voice session gain)",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )

                        Spacer(modifier = Modifier.height(8.dp))

                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Icon(
                                imageVector = Icons.Default.VolumeDown,
                                contentDescription = "Low",
                                modifier = Modifier.size(20.dp),
                                tint = MaterialTheme.colorScheme.onSurfaceVariant
                            )
                            Slider(
                                value = wakeSliderIndex,
                                onValueChange = { wakeSliderIndex = it },
                                onValueChangeFinished = {
                                    val gainPercent = wakeGainSteps.getOrElse(wakeSliderIndex.roundToInt()) { 100f }
                                    onUpdateWakeWordMicGainLevel(gainPercent / 100f)
                                },
                                valueRange = 0f..(wakeGainSteps.size - 1).toFloat(),
                                steps = wakeGainSteps.size - 2,
                                modifier = Modifier
                                    .weight(1f)
                                    .padding(horizontal = 8.dp)
                            )
                            Icon(
                                imageVector = Icons.Default.VolumeUp,
                                contentDescription = "High",
                                modifier = Modifier.size(20.dp),
                                tint = MaterialTheme.colorScheme.onSurfaceVariant
                            )
                        }

                        if (wakeDisplayPercent != 100) {
                            TextButton(
                                onClick = {
                                    wakeSliderIndex = 10f
                                    onUpdateWakeWordMicGainLevel(1.0f)
                                }
                            ) {
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
