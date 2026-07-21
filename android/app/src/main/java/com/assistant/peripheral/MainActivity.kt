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
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.ui.draw.clip
import androidx.compose.ui.unit.dp
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.platform.LocalContext
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

    // Callbacks set from AssistantApp composable.
    // Per Detour 3 naming (plan §0.5):
    //   onTalkWordDetected → turn-based single voice message
    //   onWakeWordDetected → realtime WebRTC voice conversation
    var onTalkWordDetected: (() -> Unit)? = null
    var onWakeWordDetected: (() -> Unit)? = null

    // Whisper-confirmation lifecycle (fires between a raw Vosk match and the
    // confirmed/rejected outcome). `isRealtime` tells the UI which trigger
    // is being confirmed so it can show the right transient indicator.
    var onWakeConfirming: ((isRealtime: Boolean) -> Unit)? = null
    var onWakeConfirmFailed: ((isRealtime: Boolean) -> Unit)? = null

    // Talk-word same-mic capture: the full turn message (wake phrase + command)
    // was captured on the shared mic and auto-sent on silence. Carries the
    // base64 WAV — the app just ships it, no recording UI dance needed.
    var onTalkMessageCaptured: ((audioB64: String) -> Unit)? = null

    /**
     * A payload shared into the app via ACTION_SEND (share sheet). Observed by
     * the AssistantApp composable, which dispatches it to the ViewModel
     * (upload+inject for files, direct inject for text) and clears it. A
     * StateFlow so a share arriving before the composable is ready (cold launch)
     * isn't lost — the collector picks up whatever value is current.
     */
    val sharedPayload = kotlinx.coroutines.flow.MutableStateFlow<SharedPayload?>(null)

    sealed class SharedPayload {
        data class Text(val text: String, val subject: String?) : SharedPayload()
        data class File(val uri: android.net.Uri, val subject: String?) : SharedPayload()
    }

    /** Parse an ACTION_SEND intent into a [SharedPayload], or null if it isn't one. */
    private fun parseShareIntent(intent: Intent?): SharedPayload? {
        if (intent?.action != Intent.ACTION_SEND) return null
        val subject = intent.getStringExtra(Intent.EXTRA_SUBJECT)
        val streamUri: android.net.Uri? =
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                intent.getParcelableExtra(Intent.EXTRA_STREAM, android.net.Uri::class.java)
            } else {
                @Suppress("DEPRECATION")
                intent.getParcelableExtra(Intent.EXTRA_STREAM)
            }
        if (streamUri != null) return SharedPayload.File(streamUri, subject)
        val text = intent.getStringExtra(Intent.EXTRA_TEXT)
        if (!text.isNullOrBlank()) return SharedPayload.Text(text, subject)
        return null
    }

    private val wakeWordReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            when (intent?.action) {
                WakeWordDetector.ACTION_TALK_WORD_DETECTED -> onTalkWordDetected?.invoke()
                WakeWordDetector.ACTION_WAKE_WORD_DETECTED -> onWakeWordDetected?.invoke()
                WakeWordDetector.ACTION_WAKE_CONFIRMING ->
                    onWakeConfirming?.invoke(
                        intent.getBooleanExtra(WakeWordDetector.EXTRA_IS_REALTIME, false),
                    )
                WakeWordDetector.ACTION_WAKE_CONFIRM_FAILED ->
                    onWakeConfirmFailed?.invoke(
                        intent.getBooleanExtra(WakeWordDetector.EXTRA_IS_REALTIME, false),
                    )
                WakeWordDetector.ACTION_TALK_MESSAGE_CAPTURED ->
                    intent.getStringExtra(WakeWordDetector.EXTRA_TALK_AUDIO_B64)?.let {
                        onTalkMessageCaptured?.invoke(it)
                    }
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        requestRequiredPermissions()

        // Register both wake-word-detector broadcast actions on the same receiver.
        val filter = IntentFilter().apply {
            addAction(WakeWordDetector.ACTION_TALK_WORD_DETECTED)
            addAction(WakeWordDetector.ACTION_WAKE_WORD_DETECTED)
            addAction(WakeWordDetector.ACTION_WAKE_CONFIRMING)
            addAction(WakeWordDetector.ACTION_WAKE_CONFIRM_FAILED)
            addAction(WakeWordDetector.ACTION_TALK_MESSAGE_CAPTURED)
        }
        LocalBroadcastManager.getInstance(this).registerReceiver(wakeWordReceiver, filter)

        // A share (ACTION_SEND) that cold-launched the app — stash it for the
        // composable's collector to dispatch once the ViewModel is up.
        parseShareIntent(intent)?.let { sharedPayload.value = it }

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
        // A share arriving while the app is already running.
        parseShareIntent(intent)?.let { sharedPayload.value = it }
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

        // Bluetooth connect permission (Android 12+). The manifest declares
        // BLUETOOTH_CONNECT for API 31+, but it's a runtime grant. Without
        // it BluetoothAdapter.getProfileConnectionState() throws
        // SecurityException — AudioRouter catches that and reports "no BT"
        // as a safety net, so denying this permission just means the
        // BLUETOOTH routing option stays unavailable. The OS-managed AUTO
        // routing default still works fine without it.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_CONNECT)
                != PackageManager.PERMISSION_GRANTED
            ) {
                permissionsToRequest.add(Manifest.permission.BLUETOOTH_CONNECT)
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
    val termination by viewModel.termination.collectAsState()
    val isRecording by viewModel.isRecording.collectAsState()
    val wakeConfirming by viewModel.wakeConfirming.collectAsState()
    val remoteVoiceActive by viewModel.remoteVoiceActive.collectAsState()
    val settings by viewModel.settings.collectAsState()
    val voiceState by viewModel.voiceState.collectAsState()
    val vadState by viewModel.vadState.collectAsState()
    val vadDurationMs by viewModel.vadDurationMs.collectAsState()
    val isMuted by viewModel.isMuted.collectAsState()
    val liveSessionIds by viewModel.liveSessionIds.collectAsState()
    val isOrchestratorSession by viewModel.isOrchestratorSession.collectAsState()
    val hasMoreMessages by viewModel.hasMoreMessages.collectAsState()
    val isLoadingMoreMessages by viewModel.isLoadingMoreMessages.collectAsState()
    val discoveredServers by viewModel.discoveredServers.collectAsState()
    val isScanning by viewModel.isScanning.collectAsState()
    val noActiveOrchestrator by viewModel.noActiveOrchestrator.collectAsState()
    val systemConfig by viewModel.systemConfig.collectAsState()
    // Surface one-shot router messages (e.g. "BT speaker unsupported on
    // OpenAI") as a system Toast. Each emission on the SharedFlow is a
    // discrete event — Compose collects directly, no "clear" needed.
    val toastContext = LocalContext.current
    LaunchedEffect(Unit) {
        viewModel.toastMessage.collect { msg ->
            android.widget.Toast.makeText(toastContext, msg, android.widget.Toast.LENGTH_LONG).show()
        }
    }
    // Share/upload feedback toasts.
    LaunchedEffect(Unit) {
        viewModel.shareToast.collect { msg ->
            android.widget.Toast.makeText(toastContext, msg, android.widget.Toast.LENGTH_LONG).show()
        }
    }
    // File picker for the in-conversation upload button. GetContent returns a
    // content:// URI the ViewModel reads + uploads. "*/*" so any file type can
    // be shared into the orchestrator.
    val uploadPicker = androidx.activity.compose.rememberLauncherForActivityResult(
        androidx.activity.result.contract.ActivityResultContracts.GetContent()
    ) { uri: android.net.Uri? ->
        uri?.let { viewModel.shareFile(it) }
    }
    // Dispatch a payload shared into the app via the system share sheet. Files
    // upload then inject their link; text injects directly. Cleared after
    // handling so a config change doesn't re-fire it.
    val sharedPayload by activity.sharedPayload.collectAsState()
    LaunchedEffect(sharedPayload) {
        when (val payload = sharedPayload) {
            is MainActivity.SharedPayload.Text ->
                viewModel.shareText(payload.text, payload.subject)
            is MainActivity.SharedPayload.File ->
                viewModel.shareFile(payload.uri, payload.subject)
            null -> {}
        }
        if (sharedPayload != null) activity.sharedPayload.value = null
    }

    // Wire talk-word detection. Same-mic capture: the WakeWordDetector keeps
    // its mic open and records the spoken command directly, auto-sending on
    // ~1.5s of silence — no mic switch, no 5s timer, no button press. This
    // callback is now UI-ack only: beep + show the recording indicator the
    // instant the phrase is confirmed. The audio arrives via
    // onTalkMessageCaptured below.
    DisposableEffect(Unit) {
        activity.onTalkWordDetected = {
            viewModel.playTalkWordAckBeep()
            viewModel.markRecordingStarting()
            navController.navigate(Screen.Chat.route) {
                popUpTo(navController.graph.findStartDestination().id) { saveState = true }
                launchSingleTop = true
                restoreState = true
            }
        }
        // The command was captured on the shared mic and auto-sent on silence —
        // ship the base64 WAV as a voice message and clear the recording UI.
        activity.onTalkMessageCaptured = { audioB64 ->
            viewModel.sendCapturedVoiceMessage(audioB64)
        }
        onDispose {
            activity.onTalkWordDetected = null
            activity.onTalkMessageCaptured = null
        }
    }

    // Wire wake-word detection: start a realtime WebRTC voice conversation.
    DisposableEffect(Unit) {
        activity.onWakeWordDetected = {
            // Immediate user feedback BEFORE the async work — beep + state
            // flip happen synchronously so the user sees "Connecting..." the
            // instant the phrase is detected, instead of after the
            // orchestrator WS round-trip + WebRTC handshake completes.
            viewModel.playWakeWordAckBeep()
            viewModel.markVoiceConnecting()
            navController.navigate(Screen.Chat.route) {
                popUpTo(navController.graph.findStartDestination().id) { saveState = true }
                launchSingleTop = true
                restoreState = true
            }
            viewModel.startVoiceSession()
        }
        onDispose { activity.onWakeWordDetected = null }
    }

    // Wire the Whisper-confirmation lifecycle. A raw Vosk match first fires
    // "confirming" (transient "Listening…" indicator + a foreground nav so the
    // user sees it), then resolves to a real detection callback (above) or a
    // rejection that just clears the indicator.
    DisposableEffect(Unit) {
        activity.onWakeConfirming = { _ ->
            viewModel.markWakeConfirming()
            // Surface the chat screen so the transient indicator is visible even
            // if the phrase turns out to be a false positive that never starts.
            navController.navigate(Screen.Chat.route) {
                popUpTo(navController.graph.findStartDestination().id) { saveState = true }
                launchSingleTop = true
                restoreState = true
            }
        }
        activity.onWakeConfirmFailed = { _ ->
            viewModel.clearWakeConfirming()
        }
        onDispose {
            activity.onWakeConfirming = null
            activity.onWakeConfirmFailed = null
        }
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

    // Apply wake-word-detector config whenever it changes (also fires when DataStore
    // finishes loading on first launch — LaunchedEffect(Unit) runs before DataStore
    // is ready). Gain MUST be included in the key list and the call — without it,
    // this effect silently overwrites the user's `wakeWordMicGainLevel` slider
    // value with 1.0f on every recomposition.
    //
    // Per Detour 3 naming (plan §0.5):
    //   settings.talkWord = turn-based single voice message trigger
    //   settings.wakeWord = realtime WebRTC voice conversation trigger
    LaunchedEffect(
        settings.enableWakeWord,
        settings.talkWord,
        settings.wakeWord,
        settings.wakeWordMicGainLevel,
        settings.serverUrl,
    ) {
        AssistantService.updateWakeWord(
            activity,
            settings.enableWakeWord,
            settings.talkWord,
            settings.wakeWord,
            settings.wakeWordMicGainLevel,
            settings.serverUrl,
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

    // Inc 3.5: navigate to Chat only when an orchestrator session is actually
    // opened (the user's intent went through — either directly because there
    // was no conflict, or after they resolved the dialog with OpenExisting /
    // DiscardAndProceed). Cancel does NOT emit, so the user stays on History.
    LaunchedEffect(Unit) {
        viewModel.orchestratorOpenedToChat.collect {
            navController.navigate(Screen.Chat.route) {
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

    // Pending rewind / fork confirmation. We capture the UI index at click time
    // and resolve it to a JSONL-absolute index inside the ViewModel.
    var pendingMessageAction by remember {
        mutableStateOf<Pair<String, Int>?>(null) // (kind, uiIndex)
    }

    // Pending delete confirmation, mirroring the web frontend's ConfirmModal.
    // We hold (sessionId, title) so the dialog can display the human-readable
    // title even after the user navigates away from the sessions list.
    var pendingDeleteSession by remember {
        mutableStateOf<Pair<String, String>?>(null)
    }

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
                        onLoadMoreMessages = viewModel::loadMoreMessages,
                        onRewindMessage = { idx -> pendingMessageAction = "rewind" to idx },
                        onForkMessage = { idx -> pendingMessageAction = "fork" to idx }
                    )
                }

                composable(Screen.Sessions.route) {
                    // Safety refresh on entering History: pool-watcher pushes
                    // keep the list live while the orchestrator WS is connected,
                    // but a forced refetch here guarantees the true pool state
                    // whenever the user opens the list (covers a missed-push
                    // window, e.g. WS was down). Fires once per navigation.
                    LaunchedEffect(Unit) { viewModel.forceRefreshSessions() }
                    SessionsScreen(
                        sessions = sessions,
                        currentSessionId = currentSessionId,
                        liveSessionIds = liveSessionIds,
                        isLoading = sessionsLoading,
                        onSessionClick = { sessionId, isOrchestrator, liveLocalId ->
                            if (isOrchestrator) {
                                // Inc 3.5: orchestrator switches go through the
                                // conflict mediator. We do NOT navigate to Chat
                                // up-front because the conflict dialog must
                                // overlay History (where the user tapped), not
                                // a stale Chat view. Navigation happens via
                                // the `orchestratorOpenedToChat` LaunchedEffect
                                // below — fired only when the request actually
                                // opens an orchestrator (directly or after
                                // dialog resolution).
                                viewModel.requestLoadOrchestratorSession(sessionId, liveLocalId)
                            } else {
                                // Agent sessions: parallel path stays as today.
                                viewModel.loadSession(sessionId, false, liveLocalId)
                                navController.navigate(Screen.Chat.route)
                            }
                        },
                        onNewSession = {
                            // Inc 3.5: same deferred-navigation pattern as
                            // session click — the conflict dialog must overlay
                            // History when a live orch exists.
                            viewModel.requestNewOrchestratorSession()
                        },
                        onRenameSession = viewModel::renameSession,
                        onDeleteSession = { sid ->
                            val title = sessions.find { it.sessionId == sid }?.title ?: "this conversation"
                            pendingDeleteSession = sid to title
                        },
                        onDuplicateSession = viewModel::duplicateSession,
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
                        systemConfig = systemConfig,
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
                        isWiredHeadphoneAvailable = viewModel.isWiredHeadphoneAvailable(),
                        onUpdateEnableWakeWord = viewModel::updateEnableWakeWord,
                        onUpdateTalkWord = viewModel::updateTalkWord,
                        onUpdateWakeWord = viewModel::updateWakeWord,
                        onUpdateEnableButtonTrigger = viewModel::updateEnableButtonTrigger,
                        onConnect = viewModel::connect,
                        onDisconnect = viewModel::disconnect,
                        onScanForServers = viewModel::scanForServers,
                        onConnectToServer = viewModel::connectToDiscoveredServer,
                        onAddSavedServer = viewModel::addSavedServer,
                        onRemoveSavedServer = viewModel::removeSavedServer,
                        onSelectSavedServer = viewModel::selectSavedServer,
                        onLoadSystemConfig = viewModel::loadSystemConfig,
                        onUpdateSystemConfig = viewModel::updateSystemConfig,
                        onToggleMcp = viewModel::toggleMcp,
                        onDismissVoiceModelAutoCorrected = viewModel::dismissVoiceModelAutoCorrected,
                    )
                }
            }
        }

        // Bottom stack: status bar + chat input (chat tab only, not in voice mode)
        // -> voice controls (when active, global) -> nav tabs.
        // StatusBar is chat-only since it reflects the current chat session;
        // VoiceControls already surfaces its own state during voice mode.
        if (currentRoute == Screen.Chat.route && !isVoiceActive) {
            // Termination banner — shown when the backend signalled this
            // session is permanently gone (subprocess crashed, SSH
            // closed).  Distinct from a transient error: offers an
            // actionable "continue from disk" affordance via the
            // SDK session id (the JSONL on disk is intact).
            termination?.let { term ->
                com.assistant.peripheral.ui.screens.TerminationBanner(
                    reason = term.reason,
                    detail = term.detail,
                    canRecover = !term.sdkSessionId.isNullOrBlank(),
                    onRecover = {
                        val sdkId = term.sdkSessionId
                        if (!sdkId.isNullOrBlank()) {
                            // Reopens the session in the same surface;
                            // the new socket gets a fresh local_id and
                            // resumes from the on-disk JSONL.
                            viewModel.loadSession(sdkId, false, null)
                        }
                    },
                )
            }
            StatusBar(
                connectionState = connectionState,
                sessionStatus = sessionStatus,
            )
            // Transient "heard you, confirming…" banner while the Whisper gate
            // runs. Flashes briefly on false positives, then vanishes; on a
            // real detection the recording/connecting UI takes over.
            if (wakeConfirming) {
                Surface(
                    modifier = Modifier.fillMaxWidth(),
                    color = MaterialTheme.colorScheme.secondaryContainer,
                ) {
                    androidx.compose.foundation.layout.Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(horizontal = 16.dp, vertical = 8.dp),
                        horizontalArrangement = androidx.compose.foundation.layout.Arrangement.Center,
                        verticalAlignment = androidx.compose.ui.Alignment.CenterVertically,
                    ) {
                        androidx.compose.material3.CircularProgressIndicator(
                            modifier = Modifier.size(14.dp),
                            strokeWidth = 2.dp,
                            color = MaterialTheme.colorScheme.onSecondaryContainer,
                        )
                        androidx.compose.foundation.layout.Spacer(Modifier.width(8.dp))
                        Text(
                            text = "Listening…",
                            style = MaterialTheme.typography.bodyMedium,
                            color = MaterialTheme.colorScheme.onSecondaryContainer,
                        )
                    }
                }
            }
            com.assistant.peripheral.ui.screens.ChatInputBar(
                inputText = chatInputText,
                onInputChange = { chatInputText = it },
                onSend = {
                    if (chatInputText.isNotBlank()) {
                        viewModel.sendMessage(chatInputText)
                        chatInputText = ""
                    }
                },
                onInterrupt = viewModel::interrupt,
                isRecording = isRecording,
                onStartRecording = viewModel::startRecording,
                onStopRecording = viewModel::stopRecording,
                isConnected = connectionState is com.assistant.peripheral.data.ConnectionState.Connected,
                isStreaming = sessionStatus in listOf("streaming", "tool_use", "thinking", "processing"),
                voiceState = voiceState,
                onStartVoice = viewModel::startVoiceSession,
                onStopVoice = viewModel::stopVoiceSession,
                isOrchestratorSession = isOrchestratorSession,
                onUploadFile = { uploadPicker.launch("*/*") },
                remoteVoiceActive = remoteVoiceActive,
            )
        }

        if (isVoiceActive) {
            val reconnectBanner by viewModel.voiceReconnectBanner.collectAsState()
            reconnectBanner?.let { msg ->
                Surface(
                    modifier = Modifier.fillMaxWidth(),
                    color = MaterialTheme.colorScheme.tertiaryContainer,
                ) {
                    androidx.compose.foundation.layout.Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(horizontal = 16.dp, vertical = 8.dp),
                        contentAlignment = androidx.compose.ui.Alignment.Center,
                    ) {
                        Text(
                            text = msg,
                            style = MaterialTheme.typography.bodyMedium,
                            color = MaterialTheme.colorScheme.onTertiaryContainer,
                        )
                    }
                }
            }
            VoiceControls(
                voiceState = voiceState,
                isMuted = isMuted,
                onToggleMute = viewModel::toggleMute,
                onStop = viewModel::stopVoiceSession,
                modifier = Modifier.fillMaxWidth(),
                vadState = vadState,
                vadDurationMs = vadDurationMs,
            )
        }

        Surface(
            color = NavigationBarDefaults.containerColor,
            tonalElevation = NavigationBarDefaults.Elevation,
            modifier = Modifier.fillMaxWidth()
        ) {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .windowInsetsPadding(NavigationBarDefaults.windowInsets)
                    .height(80.dp),
                horizontalArrangement = Arrangement.SpaceEvenly,
                verticalAlignment = androidx.compose.ui.Alignment.CenterVertically
            ) {
                val currentDestination = navBackStackEntry?.destination
                val selectedBg = MaterialTheme.colorScheme.secondaryContainer
                val selectedFg = MaterialTheme.colorScheme.onSecondaryContainer
                val unselectedFg = MaterialTheme.colorScheme.onSurfaceVariant

                screens.forEach { screen ->
                    val selected = currentDestination?.hierarchy?.any { it.route == screen.route } == true
                    Box(
                        modifier = Modifier
                            .size(width = 64.dp, height = 48.dp)
                            .clip(androidx.compose.foundation.shape.RoundedCornerShape(24.dp))
                            .background(if (selected) selectedBg else androidx.compose.ui.graphics.Color.Transparent)
                            .clickable {
                                navController.navigate(screen.route) {
                                    popUpTo(navController.graph.findStartDestination().id) {
                                        saveState = true
                                    }
                                    launchSingleTop = true
                                    restoreState = true
                                }
                            },
                        contentAlignment = androidx.compose.ui.Alignment.Center
                    ) {
                        Icon(
                            screen.icon,
                            contentDescription = screen.title,
                            tint = if (selected) selectedFg else unselectedFg
                        )
                    }
                }
            }
        }
    }
    }

    // Delete-conversation confirmation, matching the web ConfirmModal. The
    // file is moved to context/trash/ on the backend and can be recovered
    // manually, but the user shouldn't lose a conversation to a stray tap.
    pendingDeleteSession?.let { (sessionId, title) ->
        AlertDialog(
            onDismissRequest = { pendingDeleteSession = null },
            title = { Text("Delete conversation?") },
            text = {
                Text(
                    "\"$title\" will be moved to trash and hidden from the assistant. " +
                        "The file is kept on disk and can be recovered manually from context/trash/."
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    viewModel.deleteSession(sessionId)
                    pendingDeleteSession = null
                }) {
                    Text("Delete")
                }
            },
            dismissButton = {
                TextButton(onClick = { pendingDeleteSession = null }) {
                    Text("Cancel")
                }
            }
        )
    }

    // Rewind / fork confirmation dialog. Rendered outside the Scaffold's
    // content so it overlays the whole screen.
    pendingMessageAction?.let { (kind, uiIndex) ->
        AlertDialog(
            onDismissRequest = { pendingMessageAction = null },
            title = {
                Text(
                    if (kind == "rewind") "Rewind conversation?" else "Fork conversation?"
                )
            },
            text = {
                Text(
                    if (kind == "rewind")
                        "All messages after the selected one will be removed from this conversation. The session will be closed; reopen it from History to continue from the rewound point."
                    else
                        "A copy of this conversation will be created, truncated to the selected message. The original is unchanged."
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    if (kind == "rewind") viewModel.rewindCurrentSessionAt(uiIndex)
                    else viewModel.forkCurrentSessionAt(uiIndex)
                    pendingMessageAction = null
                }) {
                    Text(if (kind == "rewind") "Rewind" else "Fork")
                }
            },
            dismissButton = {
                TextButton(onClick = { pendingMessageAction = null }) {
                    Text("Cancel")
                }
            }
        )
    }

    // Inc 3.5 — orchestrator session conflict dialog. Surfaces when the user
    // taps a non-live orchestrator in History (or the New Session FAB) while
    // another orchestrator is already live in the pool. Three actions:
    //   - Open the running one: load the live orch into the chat view.
    //   - Close it and switch / start fresh: closePoolSession(live) then
    //     proceed with the original intent.
    //   - Cancel: clear the conflict, no further action.
    val orchestratorConflict by viewModel.orchestratorConflict.collectAsState()
    orchestratorConflict?.let { conflict ->
        val (title, body, discardLabel) = when (conflict) {
            is com.assistant.peripheral.chat.OrchestratorConflict.OnLoad -> Triple(
                "Another orchestrator is running",
                "There's already an active orchestrator session. " +
                    "You can open the running one, or close it and switch to the one you tapped.",
                "Close and switch"
            )
            is com.assistant.peripheral.chat.OrchestratorConflict.OnNew -> Triple(
                "Another orchestrator is running",
                "There's already an active orchestrator session. " +
                    "You can open the running one, or close it and start a fresh session.",
                "Close and start fresh"
            )
        }
        AlertDialog(
            onDismissRequest = {
                viewModel.resolveOrchestratorConflict(
                    com.assistant.peripheral.chat.OrchestratorConflictResolution.Cancel
                )
            },
            title = { Text(title) },
            text = { Text(body) },
            confirmButton = {
                TextButton(onClick = {
                    viewModel.resolveOrchestratorConflict(
                        com.assistant.peripheral.chat.OrchestratorConflictResolution.OpenExisting
                    )
                }) {
                    Text("Open the running one")
                }
            },
            dismissButton = {
                Row {
                    TextButton(onClick = {
                        viewModel.resolveOrchestratorConflict(
                            com.assistant.peripheral.chat.OrchestratorConflictResolution.DiscardAndProceed
                        )
                    }) {
                        Text(discardLabel)
                    }
                    TextButton(onClick = {
                        viewModel.resolveOrchestratorConflict(
                            com.assistant.peripheral.chat.OrchestratorConflictResolution.Cancel
                        )
                    }) {
                        Text("Cancel")
                    }
                }
            }
        )
    }
}
