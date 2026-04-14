package com.assistant.peripheral.ui.screens

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

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(
    settings: AppSettings,
    connectionState: ConnectionState,
    onUpdateServerUrl: (String) -> Unit,
    onUpdateThemeMode: (ThemeMode) -> Unit,
    onUpdateAutoConnect: (Boolean) -> Unit,
    onConnect: () -> Unit,
    onDisconnect: () -> Unit,
    modifier: Modifier = Modifier
) {
    var serverUrl by remember(settings.serverUrl) { mutableStateOf(settings.serverUrl) }

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
