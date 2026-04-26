package com.assistant.peripheral

import android.Manifest
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.core.content.ContextCompat
import androidx.lifecycle.viewmodel.compose.viewModel
import kotlinx.coroutines.launch
import androidx.lifecycle.ViewModelProvider
import androidx.localbroadcastmanager.content.LocalBroadcastManager
import androidx.navigation.NavDestination.Companion.hierarchy
import androidx.navigation.NavGraph.Companion.findStartDestination
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import com.assistant.peripheral.data.VoiceState
import com.assistant.peripheral.service.AssistantService
import com.assistant.peripheral.ui.components.StatusBar
import com.assistant.peripheral.ui.components.VoiceControls
import com.assistant.peripheral.ui.screens.ChatScreen
import com.assistant.peripheral.ui.screens.SessionsScreen
import com.assistant.peripheral.ui.screens.SettingsScreen
import com.assistant.peripheral.ui.theme.AssistantTheme
import com.assistant.peripheral.viewmodel.AssistantViewModel
import com.assistant.peripheral.voice.WakeWordDetector

class MainActivity : ComponentActivity() {

    private val requestPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { permissions ->
        // Handle permission results
        val audioGranted = permissions[Manifest.permission.RECORD_AUDIO] == true
        if (audioGranted) {
            // Audio permission granted
        }
    }

    // Callbacks set from AssistantApp composable
    var onWakeWordDetected: (() -> Unit)? = null   // turn-based recording
    var onVoiceWordDetected: (() -> Unit)? = null  // realtime WebRTC session

    private val wakeWordReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            when (intent?.action) {
                WakeWordDetector.ACTION_WAKE_WORD_DETECTED -> onWakeWordDetected?.invoke()
                WakeWordDetector.ACTION_VOICE_WORD_DETECTED -> onVoiceWordDetected?.invoke()
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        requestRequiredPermissions()

        // Register both wake word broadcast actions on the same receiver
        val filter = IntentFilter().apply {
            addAction(WakeWordDetector.ACTION_WAKE_WORD_DETECTED)
            addAction(WakeWordDetector.ACTION_VOICE_WORD_DETECTED)
        }
        LocalBroadcastManager.getInstance(this).registerReceiver(wakeWordReceiver, filter)

        setContent {
            val viewModel: AssistantViewModel = viewModel()
            val settings by viewModel.settings.collectAsState()

            AssistantTheme(themeMode = settings.themeMode) {
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = MaterialTheme.colorScheme.background
                ) {
                    AssistantApp(viewModel = viewModel, activity = this@MainActivity)
                }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        // Wake word fired while activity was already running (e.g. screen locked).
        // The activity is brought to front via FLAG_ACTIVITY_REORDER_TO_FRONT; we also
        // need to explicitly turn the screen on for pre-O devices (attribute alone isn't enough
        // when the activity is already running).
        if (intent.getBooleanExtra(AssistantService.EXTRA_WAKE_WORD_TRIGGERED, false)) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
                setTurnScreenOn(true)
                setShowWhenLocked(true)
            } else {
                @Suppress("DEPRECATION")
                window.addFlags(
                    WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON or
                    WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
                    WindowManager.LayoutParams.FLAG_DISMISS_KEYGUARD
                )
            }
        }
    }

    override fun onResume() {
        super.onResume()
        // Re-connect WebSocket if the app was in the background (screen lock, app switch, etc.)
        // The ViewModel is retained across activity recreation, so this is the right place.
        val viewModel = androidx.lifecycle.ViewModelProvider(this)[AssistantViewModel::class.java]
        viewModel.reconnectIfNeeded()
    }

    override fun onDestroy() {
        super.onDestroy()
        LocalBroadcastManager.getInstance(this).unregisterReceiver(wakeWordReceiver)
    }

    private fun requestRequiredPermissions() {
        val permissionsToRequest = mutableListOf<String>()

        // Audio recording permission
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED
        ) {
            permissionsToRequest.add(Manifest.permission.RECORD_AUDIO)
        }

        // Notification permission (Android 13+)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED
            ) {
                permissionsToRequest.add(Manifest.permission.POST_NOTIFICATIONS)
            }
        }

        if (permissionsToRequest.isNotEmpty()) {
            requestPermissionLauncher.launch(permissionsToRequest.toTypedArray())
        }
    }
}

sealed class Screen(val route: String, val title: String, val icon: ImageVector) {
    object Chat : Screen("chat", "Chat", Icons.Default.Chat)
    object Sessions : Screen("sessions", "History", Icons.Default.History)
    object Settings : Screen("settings", "Settings", Icons.Default.Settings)
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AssistantApp(viewModel: AssistantViewModel, activity: MainActivity) {
    val navController = rememberNavController()
    val screens = listOf(Screen.Chat, Screen.Sessions, Screen.Settings)

    // Collect state
    val connectionState by viewModel.connectionState.collectAsState()
    val messages by viewModel.messages.collectAsState()
    val sessions by viewModel.sessions.collectAsState()
    val sessionsLoading by viewModel.sessionsLoading.collectAsState()
    val currentSessionId by viewModel.currentSessionId.collectAsState()
    val sessionStatus by viewModel.sessionStatus.collectAsState()
    val isRecording by viewModel.isRecording.collectAsState()
    val settings by viewModel.settings.collectAsState()
    val voiceState by viewModel.voiceState.collectAsState()
    val isMuted by viewModel.isMuted.collectAsState()
    val liveSessionIds by viewModel.liveSessionIds.collectAsState()
    val isOrchestratorSession by viewModel.isOrchestratorSession.collectAsState()
    val hasMoreMessages by viewModel.hasMoreMessages.collectAsState()
    val isLoadingMoreMessages by viewModel.isLoadingMoreMessages.collectAsState()
    val discoveredServers by viewModel.discoveredServers.collectAsState()
    val isScanning by viewModel.isScanning.collectAsState()
    val noActiveOrchestrator by viewModel.noActiveOrchestrator.collectAsState()

    // Wire wake word detection: start turn-based recording and navigate to chat
    val coroutineScope = rememberCoroutineScope()
    DisposableEffect(Unit) {
        activity.onWakeWordDetected = {
            // Navigate to chat so the user sees the recording UI
            navController.navigate(Screen.Chat.route) {
                popUpTo(navController.graph.findStartDestination().id) { saveState = true }
                launchSingleTop = true
                restoreState = true
            }
            // Start recording — same as pressing the mic button
            viewModel.startRecording()
            // Auto-stop after 5 seconds (user speaks their request after the wake word)
            coroutineScope.launch {
                kotlinx.coroutines.delay(5000L)
                if (viewModel.isRecording.value) {
                    viewModel.stopRecording()
                }
            }
        }
        onDispose { activity.onWakeWordDetected = null }
    }

    // Wire realtime voice word detection: start WebRTC voice session
    DisposableEffect(Unit) {
        activity.onVoiceWordDetected = {
            navController.navigate(Screen.Chat.route) {
                popUpTo(navController.graph.findStartDestination().id) { saveState = true }
                launchSingleTop = true
                restoreState = true
            }
            viewModel.startVoiceSession()
        }
        onDispose { activity.onVoiceWordDetected = null }
    }

    // Auto-connect or auto-scan on launch
    LaunchedEffect(Unit) {
        val defaultUrl = com.assistant.peripheral.data.AppSettings().serverUrl
        val hasCustomUrl = settings.serverUrl != defaultUrl
        if (settings.autoConnect) {
            viewModel.connect()
        } else if (!hasCustomUrl) {
            // Only scan when using the default URL — if the user has chosen a server
            // (saved or manually entered), skip the subnet sweep.
            viewModel.scanForServers()
        }
        // Start foreground service (wake word config applied separately below)
        AssistantService.start(activity)
    }

    // Apply wake word setting whenever it changes (also fires when DataStore finishes
    // loading on first launch — LaunchedEffect(Unit) runs before DataStore is ready).
    LaunchedEffect(settings.enableWakeWord, settings.wakeWord, settings.voiceWord) {
        AssistantService.updateWakeWord(
            activity,
            settings.enableWakeWord,
            settings.wakeWord,
            settings.voiceWord
        )
    }

    // Also scan when auto-connect is on but we fail to connect after a moment.
    // Skip when the user has a custom (non-default) server URL — no need to sweep the subnet.
    LaunchedEffect(settings.autoConnect) {
        val defaultUrl = com.assistant.peripheral.data.AppSettings().serverUrl
        val hasCustomUrl = settings.serverUrl != defaultUrl
        if (settings.autoConnect && !hasCustomUrl) {
            viewModel.scanForServers()
        }
    }

    // Load sessions when connected
    LaunchedEffect(connectionState) {
        if (connectionState is com.assistant.peripheral.data.ConnectionState.Connected) {
            viewModel.refreshSessions()
        }
    }

    // When we connect and there's no live orchestrator on the server, route the user
    // to History so they can pick or create a session — instead of staring at an empty
    // chat for a session that doesn't exist.
    LaunchedEffect(noActiveOrchestrator) {
        if (noActiveOrchestrator) {
            navController.navigate(Screen.Sessions.route) {
                popUpTo(navController.graph.findStartDestination().id) { saveState = true }
                launchSingleTop = true
                restoreState = true
            }
        }
    }

    val navBackStackEntry by navController.currentBackStackEntryAsState()
    val currentRoute = navBackStackEntry?.destination?.route
    val isVoiceActive = voiceState != VoiceState.Off && voiceState !is VoiceState.Error

    // Chat input state lives at app scope so it persists across tab switches.
    var chatInputText by remember { mutableStateOf("") }

    Scaffold { innerPadding ->
    Column(modifier = Modifier.fillMaxSize().padding(innerPadding)) {
        // Page content
        Box(modifier = Modifier.weight(1f)) {
            NavHost(
                navController = navController,
                startDestination = Screen.Chat.route
            ) {
                composable(Screen.Chat.route) {
                    ChatScreen(
                        messages = messages,
                        hasMoreMessages = hasMoreMessages,
                        isLoadingMoreMessages = isLoadingMoreMessages,
                        onLoadMoreMessages = viewModel::loadMoreMessages
                    )
                }

                composable(Screen.Sessions.route) {
                    SessionsScreen(
                        sessions = sessions,
                        currentSessionId = currentSessionId,
                        liveSessionIds = liveSessionIds,
                        isLoading = sessionsLoading,
                        onSessionClick = { sessionId, isOrchestrator ->
                            viewModel.loadSession(sessionId, isOrchestrator)
                            navController.navigate(Screen.Chat.route)
                        },
                        onNewSession = {
                            viewModel.newSession()
                            navController.navigate(Screen.Chat.route)
                        },
                        onRenameSession = viewModel::renameSession,
                        onDeleteSession = viewModel::deleteSession,
                        onCloseSession = viewModel::closeSession,
                        onRefresh = viewModel::refreshSessions
                    )
                }

                composable(Screen.Settings.route) {
                    SettingsScreen(
                        settings = settings,
                        connectionState = connectionState,
                        discoveredServers = discoveredServers,
                        isScanning = isScanning,
                        onUpdateServerUrl = viewModel::updateServerUrl,
                        onUpdateThemeMode = viewModel::updateThemeMode,
                        onUpdateAutoConnect = viewModel::updateAutoConnect,
                        onUpdateMicGainLevel = viewModel::updateMicGainLevel,
                        onUpdateWakeWordMicGainLevel = viewModel::updateWakeWordMicGainLevel,
                        onUpdateSpeakerVolumeLevel = viewModel::updateSpeakerVolumeLevel,
                        onUpdateEchoDuckingGain = viewModel::updateEchoDuckingGain,
                        onUpdateAudioOutput = viewModel::updateAudioOutput,
                        // Recomputed on each recomposition so plugging/unplugging a BT device
                        // and re-entering the Settings screen refreshes the enablement state.
                        // TODO: surface this via a StateFlow if we want live updates without
                        // leaving Settings.
                        isBluetoothAvailable = viewModel.isBluetoothAudioAvailable(),
                        onUpdateEnableWakeWord = viewModel::updateEnableWakeWord,
                        onUpdateWakeWord = viewModel::updateWakeWord,
                        onUpdateVoiceWord = viewModel::updateVoiceWord,
                        onUpdateEnableButtonTrigger = viewModel::updateEnableButtonTrigger,
                        onConnect = viewModel::connect,
                        onDisconnect = viewModel::disconnect,
                        onScanForServers = viewModel::scanForServers,
                        onConnectToServer = viewModel::connectToDiscoveredServer,
                        onAddSavedServer = viewModel::addSavedServer,
                        onRemoveSavedServer = viewModel::removeSavedServer,
                        onSelectSavedServer = viewModel::selectSavedServer
                    )
                }
            }
        }

        // Bottom stack: status bar + chat input (chat tab only, not in voice mode)
        // -> voice controls (when active, global) -> nav tabs.
        // StatusBar is chat-only since it reflects the current chat session;
        // VoiceControls already surfaces its own state during voice mode.
        if (currentRoute == Screen.Chat.route && !isVoiceActive) {
            StatusBar(
                connectionState = connectionState,
                sessionStatus = sessionStatus,
                onInterrupt = viewModel::interrupt
            )
            com.assistant.peripheral.ui.screens.ChatInputBar(
                inputText = chatInputText,
                onInputChange = { chatInputText = it },
                onSend = {
                    if (chatInputText.isNotBlank()) {
                        viewModel.sendMessage(chatInputText)
                        chatInputText = ""
                    }
                },
                isRecording = isRecording,
                onStartRecording = viewModel::startRecording,
                onStopRecording = viewModel::stopRecording,
                isConnected = connectionState is com.assistant.peripheral.data.ConnectionState.Connected,
                isStreaming = sessionStatus == "streaming" || sessionStatus == "tool_use",
                voiceState = voiceState,
                onStartVoice = viewModel::startVoiceSession,
                onStopVoice = viewModel::stopVoiceSession,
                isOrchestratorSession = isOrchestratorSession
            )
        }

        if (isVoiceActive) {
            VoiceControls(
                voiceState = voiceState,
                isMuted = isMuted,
                onToggleMute = viewModel::toggleMute,
                onStop = viewModel::stopVoiceSession,
                modifier = Modifier.fillMaxWidth()
            )
        }

        NavigationBar {
            val currentDestination = navBackStackEntry?.destination

            screens.forEach { screen ->
                NavigationBarItem(
                    icon = { Icon(screen.icon, contentDescription = screen.title) },
                    label = { Text(screen.title) },
                    selected = currentDestination?.hierarchy?.any { it.route == screen.route } == true,
                    onClick = {
                        navController.navigate(screen.route) {
                            popUpTo(navController.graph.findStartDestination().id) {
                                saveState = true
                            }
                            launchSingleTop = true
                            restoreState = true
                        }
                    }
                )
            }
        }
    }
    }
}
