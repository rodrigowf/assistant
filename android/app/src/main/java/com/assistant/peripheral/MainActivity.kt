package com.assistant.peripheral

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
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
import androidx.navigation.NavDestination.Companion.hierarchy
import androidx.navigation.NavGraph.Companion.findStartDestination
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import com.assistant.peripheral.data.VoiceState
import com.assistant.peripheral.ui.screens.ChatScreen
import com.assistant.peripheral.ui.screens.SessionsScreen
import com.assistant.peripheral.ui.screens.SettingsScreen
import com.assistant.peripheral.ui.theme.AssistantTheme
import com.assistant.peripheral.viewmodel.AssistantViewModel

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

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        requestRequiredPermissions()

        setContent {
            val viewModel: AssistantViewModel = viewModel()
            val settings by viewModel.settings.collectAsState()

            AssistantTheme(themeMode = settings.themeMode) {
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = MaterialTheme.colorScheme.background
                ) {
                    AssistantApp(viewModel = viewModel)
                }
            }
        }
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
fun AssistantApp(viewModel: AssistantViewModel) {
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

    // Auto-connect on launch
    LaunchedEffect(Unit) {
        if (settings.autoConnect) {
            viewModel.connect()
        }
    }

    // Load sessions when connected
    LaunchedEffect(connectionState) {
        if (connectionState is com.assistant.peripheral.data.ConnectionState.Connected) {
            viewModel.refreshSessions()
        }
    }

    Scaffold(
        bottomBar = {
            NavigationBar {
                val navBackStackEntry by navController.currentBackStackEntryAsState()
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
    ) { innerPadding ->
        NavHost(
            navController = navController,
            startDestination = Screen.Chat.route,
            modifier = Modifier.padding(innerPadding)
        ) {
            composable(Screen.Chat.route) {
                ChatScreen(
                    messages = messages,
                    connectionState = connectionState,
                    sessionStatus = sessionStatus,
                    isRecording = isRecording,
                    voiceState = voiceState,
                    isOrchestratorSession = isOrchestratorSession,
                    hasMoreMessages = hasMoreMessages,
                    isLoadingMoreMessages = isLoadingMoreMessages,
                    onSendMessage = viewModel::sendMessage,
                    onStartRecording = viewModel::startRecording,
                    onStopRecording = viewModel::stopRecording,
                    onInterrupt = viewModel::interrupt,
                    onStartVoice = viewModel::startVoiceSession,
                    onStopVoice = viewModel::stopVoiceSession,
                    onToggleMute = viewModel::toggleMute,
                    onLoadMoreMessages = viewModel::loadMoreMessages,
                    isMuted = isMuted
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
                    onRefresh = viewModel::refreshSessions
                )
            }

            composable(Screen.Settings.route) {
                SettingsScreen(
                    settings = settings,
                    connectionState = connectionState,
                    onUpdateServerUrl = viewModel::updateServerUrl,
                    onUpdateThemeMode = viewModel::updateThemeMode,
                    onUpdateAutoConnect = viewModel::updateAutoConnect,
                    onConnect = viewModel::connect,
                    onDisconnect = viewModel::disconnect
                )
            }
        }
    }
}
