package com.assistant.peripheral.viewmodel

import android.app.Application
import android.content.Context
import android.media.AudioManager
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.*
import androidx.datastore.preferences.preferencesDataStore
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import android.util.Log
import com.assistant.peripheral.audio.AudioRecorder
import com.assistant.peripheral.data.*
import com.assistant.peripheral.network.ApiClient
import com.assistant.peripheral.network.DiscoveredServer
import com.assistant.peripheral.network.NetworkScanner
import com.assistant.peripheral.network.WebSocketEndpoint
import com.assistant.peripheral.network.WebSocketManager
import com.assistant.peripheral.service.AssistantService
import com.assistant.peripheral.voice.VoiceEvent
import com.assistant.peripheral.voice.VoiceManager
import kotlinx.coroutines.flow.*
import kotlinx.coroutines.launch
import java.util.UUID

// DataStore for app settings
private val Context.dataStore: DataStore<Preferences> by preferencesDataStore(name = "settings")

class AssistantViewModel(application: Application) : AndroidViewModel(application) {

    companion object {
        private const val TAG = "AssistantViewModel"
        private const val MAX_CACHED_SESSIONS = 5  // Keep at most 5 sessions in cache
        private const val MAX_CACHED_MESSAGES_PER_SESSION = 100  // Limit messages per cached session
    }

    private val dataStore = application.dataStore
    private val webSocketManager = WebSocketManager()
    private val audioRecorder = AudioRecorder(application.applicationContext)

    // API client (created when server URL is known)
    private var apiClient: ApiClient? = null

    // Voice manager for WebRTC (created lazily when apiClient is available)
    private var voiceManager: VoiceManager? = null

    // Connection state
    val connectionState: StateFlow<ConnectionState> = webSocketManager.connectionState

    // Current session
    private val _currentSessionId = MutableStateFlow<String?>(null)
    val currentSessionId: StateFlow<String?> = _currentSessionId.asStateFlow()

    private val _currentLocalId = MutableStateFlow(UUID.randomUUID().toString())
    val currentLocalId: StateFlow<String> = _currentLocalId.asStateFlow()

    // All sessions (from REST API)
    private val _sessions = MutableStateFlow<List<SessionInfo>>(emptyList())
    val sessions: StateFlow<List<SessionInfo>> = _sessions.asStateFlow()

    private val _sessionsLoading = MutableStateFlow(false)
    val sessionsLoading: StateFlow<Boolean> = _sessionsLoading.asStateFlow()

    // Live session pool (truly open sessions)
    private val _liveSessionIds = MutableStateFlow<Set<String>>(emptySet())
    val liveSessionIds: StateFlow<Set<String>> = _liveSessionIds.asStateFlow()

    // Whether current session is orchestrator
    private val _isOrchestratorSession = MutableStateFlow(false)
    val isOrchestratorSession: StateFlow<Boolean> = _isOrchestratorSession.asStateFlow()

    // Messages for current session
    private val _messages = MutableStateFlow<List<ChatMessage>>(emptyList())
    val messages: StateFlow<List<ChatMessage>> = _messages.asStateFlow()

    // Session cache - keeps messages and state for multiple sessions in memory

    private data class CachedSession(
        val messages: List<ChatMessage>,
        val isOrchestrator: Boolean,
        val paginationStartIndex: Int,
        val hasMoreMessages: Boolean
    )
    private val sessionCache = LinkedHashMap<String, CachedSession>(MAX_CACHED_SESSIONS, 0.75f, true)

    // Pagination state
    private var currentSessionIdForPagination: String? = null
    private var paginationStartIndex: Int = 0
    private val _hasMoreMessages = MutableStateFlow(false)
    val hasMoreMessages: StateFlow<Boolean> = _hasMoreMessages.asStateFlow()
    private val _isLoadingMoreMessages = MutableStateFlow(false)
    val isLoadingMoreMessages: StateFlow<Boolean> = _isLoadingMoreMessages.asStateFlow()

    // Tracks the sdk session id we're reconnecting to, so SessionStarted can load history
    private val _pendingResumeSessionId = MutableStateFlow<String?>(null)

    // The true JSONL/SDK session ID (distinct from local_id on reconnect).
    // Used as resume_sdk_id when starting voice so history is always found.
    private var _jsonlSessionId: String? = null

    // Streaming message being built
    private var streamingMessageId: String? = null
    private val _streamingContent = MutableStateFlow("")
    private var currentThinkingContent = ""
    private var currentToolBlocks = mutableMapOf<String, MessageBlock.ToolUse>()

    /**
     * Ensure a streaming message exists. Creates one if needed.
     * Call this before any streaming content updates.
     */
    private fun ensureStreamingMessage() {
        if (streamingMessageId == null) {
            streamingMessageId = UUID.randomUUID().toString()
            val newMessage = ChatMessage(
                id = streamingMessageId!!,
                role = MessageRole.ASSISTANT,
                content = "",
                blocks = emptyList(),
                isStreaming = true
            )
            _messages.update { it + newMessage }
        }
    }

    // Session status (matches web frontend)
    private val _sessionStatus = MutableStateFlow("idle")
    val sessionStatus: StateFlow<String> = _sessionStatus.asStateFlow()

    // Recording state
    private val _isRecording = MutableStateFlow(false)
    val isRecording: StateFlow<Boolean> = _isRecording.asStateFlow()

    // Voice state
    private val _voiceState = MutableStateFlow<VoiceState>(VoiceState.Off)
    val voiceState: StateFlow<VoiceState> = _voiceState.asStateFlow()

    // Muted state for voice
    private val _isMuted = MutableStateFlow(false)
    val isMuted: StateFlow<Boolean> = _isMuted.asStateFlow()

    // Settings
    private val _settings = MutableStateFlow(AppSettings())
    val settings: StateFlow<AppSettings> = _settings.asStateFlow()

    // Network scan
    private val _discoveredServers = MutableStateFlow<List<DiscoveredServer>>(emptyList())
    val discoveredServers: StateFlow<List<DiscoveredServer>> = _discoveredServers.asStateFlow()

    private val _isScanning = MutableStateFlow(false)
    val isScanning: StateFlow<Boolean> = _isScanning.asStateFlow()

    // Preference keys
    private object PreferenceKeys {
        val SERVER_URL = stringPreferencesKey("server_url")
        val AUTO_CONNECT = booleanPreferencesKey("auto_connect")
        val ENABLE_WAKE_WORD = booleanPreferencesKey("enable_wake_word")
        val WAKE_WORD = stringPreferencesKey("wake_word")
        val VOICE_WORD = stringPreferencesKey("voice_word")
        val THEME_MODE = stringPreferencesKey("theme_mode")
        val MIC_GAIN_LEVEL = floatPreferencesKey("mic_gain_level")
        val SPEAKER_VOLUME_LEVEL = floatPreferencesKey("speaker_volume_level")
    }

    init {
        // Load settings from DataStore
        viewModelScope.launch {
            dataStore.data.collect { preferences ->
                _settings.value = AppSettings(
                    serverUrl = preferences[PreferenceKeys.SERVER_URL] ?: "ws://192.168.0.28:8765",
                    autoConnect = preferences[PreferenceKeys.AUTO_CONNECT] ?: true,
                    enableWakeWord = preferences[PreferenceKeys.ENABLE_WAKE_WORD] ?: false,
                    wakeWord = preferences[PreferenceKeys.WAKE_WORD] ?: "hey assistant",
                    voiceWord = preferences[PreferenceKeys.VOICE_WORD] ?: "hey realtime",
                    themeMode = try {
                        ThemeMode.valueOf(preferences[PreferenceKeys.THEME_MODE] ?: ThemeMode.SYSTEM.name)
                    } catch (e: Exception) {
                        ThemeMode.SYSTEM
                    },
                    micGainLevel = preferences[PreferenceKeys.MIC_GAIN_LEVEL] ?: 1.0f,
                    speakerVolumeLevel = preferences[PreferenceKeys.SPEAKER_VOLUME_LEVEL] ?: 1.0f
                )
                // Update API client when server URL changes
                apiClient = ApiClient(_settings.value.serverUrl)
                // Update VoiceManager with new API client
                voiceManager?.release()
                voiceManager = VoiceManager(getApplication(), apiClient!!).also {
                    // Apply saved mic gain level
                    it.setMicGain(_settings.value.micGainLevel)
                }
                setupVoiceManagerCallbacks()
            }
        }

        // Collect WebSocket events
        viewModelScope.launch {
            webSocketManager.events.collect { event ->
                handleWebSocketEvent(event)
            }
        }
    }

    private fun setupVoiceManagerCallbacks() {
        voiceManager?.let { vm ->
            // Collect voice state changes
            viewModelScope.launch {
                vm.state.collect { state ->
                    _voiceState.value = state
                }
            }

            // Collect voice events for transcription and messages
            viewModelScope.launch {
                vm.events.collect { event ->
                    handleVoiceEvent(event)
                }
            }

            // Set callback for mirroring OpenAI events to backend via WebSocket
            // Web frontend: wsRef.current?.send({ type: "voice_event", event })
            vm.setVoiceEventCallback { eventMap ->
                webSocketManager.send(WebSocketMessage.VoiceEvent(eventMap))
            }
        }
    }

    private fun handleVoiceEvent(event: VoiceEvent) {
        when (event) {
            is VoiceEvent.UserTranscript -> {
                // Add user voice transcript as a message
                // Web frontend: optsRef.current.onUserTranscript?.(transcript)
                val userMessage = ChatMessage(
                    role = MessageRole.USER,
                    content = "[voice] ${event.text}",
                    blocks = listOf(MessageBlock.Text("[voice] ${event.text}"))
                )
                _messages.update { it + userMessage }
            }
            is VoiceEvent.TextComplete -> {
                // Add assistant response as a message
                // Web frontend: optsRef.current.onAssistantComplete?.(transcript)
                if (event.text.isNotEmpty()) {
                    val assistantMessage = ChatMessage(
                        role = MessageRole.ASSISTANT,
                        content = event.text,
                        blocks = listOf(MessageBlock.Text(event.text))
                    )
                    _messages.update { it + assistantMessage }
                }
            }
            is VoiceEvent.ToolUse -> {
                // Tool call from assistant
                // Web frontend: optsRef.current.onToolUse?.(callId, name, args)
                Log.d(TAG, "Voice tool use: ${event.name}")
                // Tool results are handled by backend and sent back via voice_command
            }
            is VoiceEvent.TurnComplete -> {
                // Turn completed
                // Web frontend: optsRef.current.onTurnComplete?.()
                _sessionStatus.value = "idle"
            }
            is VoiceEvent.Error -> {
                Log.e(TAG, "Voice error: ${event.message}")
                val errorMessage = ChatMessage(
                    role = MessageRole.SYSTEM,
                    content = "Voice error: ${event.message}"
                )
                _messages.update { it + errorMessage }
            }
            is VoiceEvent.SessionEnded -> {
                _voiceState.value = VoiceState.Off
                _isMuted.value = false
            }
            is VoiceEvent.SessionCreated -> {
                Log.d(TAG, "Voice session created")
            }
            is VoiceEvent.SpeechStarted -> {
                Log.d(TAG, "User speech started")
            }
            is VoiceEvent.SpeechStopped -> {
                Log.d(TAG, "User speech stopped")
            }
            is VoiceEvent.TextDelta -> {
                // Streaming assistant text - could update UI incrementally
                // For now, we wait for TextComplete
            }
        }
    }

    private fun handleWebSocketEvent(event: WebSocketEvent) {
        when (event) {
            is WebSocketEvent.Connected -> {
                // Check for existing orchestrator and reconnect to it, or start a new one
                viewModelScope.launch {
                    val livePool = apiClient?.getLivePool() ?: emptyList()
                    val existingOrchestrator = livePool.find { it.isOrchestrator }

                    if (existingOrchestrator != null) {
                        // Reuse the existing orchestrator's local_id so the backend
                        // recognises this as a reconnect (not a new/conflicting session)
                        _currentLocalId.value = existingOrchestrator.localId
                        // Also track the sdk session id so we can load history
                        _pendingResumeSessionId.value = existingOrchestrator.sdkSessionId
                        webSocketManager.send(WebSocketMessage.Start(
                            localId = existingOrchestrator.localId,
                            resumeSdkId = existingOrchestrator.sdkSessionId
                        ))
                    } else {
                        _pendingResumeSessionId.value = null
                        webSocketManager.send(WebSocketMessage.Start(
                            localId = _currentLocalId.value
                        ))
                    }
                }
            }

            is WebSocketEvent.SessionStarted -> {
                _currentSessionId.value = event.sessionId
                _sessionStatus.value = "idle"
                _isOrchestratorSession.value = true // Orchestrator sessions only via this endpoint

                // Track the true JSONL session ID for voice resume.
                // On reconnect the backend returns local_id as session_id — use
                // pendingResumeSessionId (the actual SDK/JSONL id) instead.
                _jsonlSessionId = _pendingResumeSessionId.value ?: event.sessionId

                // If this is a voice session, forward the session.update payload to OpenAI
                // This sends the system prompt + tool definitions so the voice session
                // has full context (matches web frontend's useVoiceOrchestrator)
                event.voiceSessionUpdate?.let { update ->
                    voiceManager?.handleBackendCommand(update)
                }

                // Load existing messages if reconnecting to an existing session
                val resumeId = _pendingResumeSessionId.value
                if (resumeId != null && _messages.value.isEmpty()) {
                    viewModelScope.launch {
                        val paginated = apiClient?.getMessagesPaginated(resumeId, limit = 50)
                            ?: com.assistant.peripheral.network.PaginatedMessages(emptyList(), 0, false, 0)
                        if (paginated.messages.isNotEmpty()) {
                            currentSessionIdForPagination = resumeId
                            paginationStartIndex = paginated.startIndex
                            _hasMoreMessages.value = paginated.hasMore
                            _messages.value = paginated.messages
                        }
                    }
                }
                _pendingResumeSessionId.value = null
                refreshSessions()
            }

            is WebSocketEvent.SessionStopped -> {
                _sessionStatus.value = "disconnected"
            }

            is WebSocketEvent.Status -> {
                _sessionStatus.value = event.status
            }

            is WebSocketEvent.Disconnected -> {
                streamingMessageId = null
                _streamingContent.value = ""
                _sessionStatus.value = "disconnected"
            }

            is WebSocketEvent.MessageStart -> {
                streamingMessageId = event.messageId
                _streamingContent.value = ""
                currentThinkingContent = ""
                currentToolBlocks.clear()

                val newMessage = ChatMessage(
                    id = event.messageId,
                    role = MessageRole.ASSISTANT,
                    content = "",
                    blocks = emptyList(),
                    isStreaming = true
                )
                _messages.update { it + newMessage }
                _sessionStatus.value = "streaming"
            }

            is WebSocketEvent.TextDelta -> {
                // Ensure streaming message exists (orchestrator doesn't send message_start)
                ensureStreamingMessage()
                _streamingContent.update { it + event.text }
                updateStreamingMessage()
            }

            is WebSocketEvent.TextComplete -> {
                ensureStreamingMessage()
                _streamingContent.value = event.text
                updateStreamingMessage()
            }

            is WebSocketEvent.ThinkingDelta -> {
                ensureStreamingMessage()
                currentThinkingContent += event.text
                updateStreamingMessage()
            }

            is WebSocketEvent.ThinkingComplete -> {
                ensureStreamingMessage()
                currentThinkingContent = event.text
                updateStreamingMessage()
            }

            is WebSocketEvent.ToolUse -> {
                ensureStreamingMessage()
                currentToolBlocks[event.toolUseId] = MessageBlock.ToolUse(
                    toolUseId = event.toolUseId,
                    toolName = event.toolName,
                    toolInput = event.toolInput,
                    isExecuting = false,
                    isComplete = false
                )
                updateStreamingMessage()
                _sessionStatus.value = "tool_use"
            }

            is WebSocketEvent.ToolExecuting -> {
                currentToolBlocks[event.toolUseId]?.let { block ->
                    currentToolBlocks[event.toolUseId] = block.copy(isExecuting = true)
                    updateStreamingMessage()
                }
            }

            is WebSocketEvent.ToolResult -> {
                currentToolBlocks[event.toolUseId]?.let { block ->
                    currentToolBlocks[event.toolUseId] = block.copy(
                        result = event.output,
                        isError = event.isError,
                        isExecuting = false,
                        isComplete = true
                    )
                    updateStreamingMessage()
                }
            }

            is WebSocketEvent.MessageEnd, is WebSocketEvent.TurnComplete -> {
                streamingMessageId?.let { messageId ->
                    _messages.update { messages ->
                        messages.map { msg ->
                            if (msg.id == messageId) {
                                msg.copy(isStreaming = false)
                            } else msg
                        }
                    }
                }
                streamingMessageId = null
                _streamingContent.value = ""
                currentThinkingContent = ""
                currentToolBlocks.clear()
                _sessionStatus.value = "idle"

                // Update cache with new messages
                saveCurrentSessionToCache()
            }

            is WebSocketEvent.CompactComplete -> {
                // Add compact divider
                val compactMessage = ChatMessage(
                    role = MessageRole.SYSTEM,
                    content = "",
                    blocks = listOf(MessageBlock.Compact(event.summary))
                )
                _messages.update { it + compactMessage }
            }

            is WebSocketEvent.Error -> {
                val errorMessage = ChatMessage(
                    role = MessageRole.SYSTEM,
                    content = "Error: ${event.message}${event.detail?.let { "\n$it" } ?: ""}"
                )
                _messages.update { it + errorMessage }
                _sessionStatus.value = "error"
            }

            is WebSocketEvent.VoiceCommand -> {
                // Forward voice_command from backend to OpenAI via VoiceManager
                // Web frontend: case "voice_command": sendToOpenAI(event.command)
                @Suppress("UNCHECKED_CAST")
                val command = event.command as? Map<String, Any?> ?: return
                voiceManager?.handleBackendCommand(command)
            }

            is WebSocketEvent.VoiceTranscript -> {
                // Handle voice transcripts (not used with realtime API)
            }

            // These are handled by ViewModel directly, not from WebSocket
            is WebSocketEvent.SessionList,
            is WebSocketEvent.HistoryLoaded,
            is WebSocketEvent.ToolProgress -> {}
        }
    }

    private fun updateStreamingMessage() {
        val messageId = streamingMessageId ?: return

        // Build blocks list
        val blocks = mutableListOf<MessageBlock>()

        // Add thinking block if present
        if (currentThinkingContent.isNotEmpty()) {
            blocks.add(MessageBlock.Thinking(currentThinkingContent, isStreaming = true))
        }

        // Add text block if present
        if (_streamingContent.value.isNotEmpty()) {
            blocks.add(MessageBlock.Text(_streamingContent.value, isStreaming = true))
        }

        // Add tool blocks
        blocks.addAll(currentToolBlocks.values)

        _messages.update { messages ->
            messages.map { msg ->
                if (msg.id == messageId) {
                    msg.copy(
                        content = _streamingContent.value,
                        blocks = blocks
                    )
                } else msg
            }
        }
    }

    fun connect() {
        viewModelScope.launch {
            webSocketManager.connect(_settings.value.serverUrl, _currentLocalId.value)
        }
    }

    fun disconnect() {
        webSocketManager.disconnect()
    }

    fun sendMessage(text: String) {
        if (text.isBlank()) return

        // Add user message to UI immediately
        val userMessage = ChatMessage(
            role = MessageRole.USER,
            content = text,
            blocks = listOf(MessageBlock.Text(text))
        )
        _messages.update { it + userMessage }

        // Send to server
        webSocketManager.send(WebSocketMessage.Send(text))
    }

    fun interrupt() {
        webSocketManager.send(WebSocketMessage.Interrupt)
        _sessionStatus.value = "interrupted"
    }

    fun compact() {
        webSocketManager.send(WebSocketMessage.Compact)
    }

    // Session management - debounced to prevent rapid duplicate refreshes
    private var lastRefreshTime = 0L
    private val refreshDebounceMs = 500L

    fun refreshSessions() {
        val now = System.currentTimeMillis()
        if (now - lastRefreshTime < refreshDebounceMs) {
            // Skip if we just refreshed
            return
        }
        lastRefreshTime = now

        viewModelScope.launch {
            _sessionsLoading.value = true

            // Fetch both sessions and live pool in parallel
            val sessions = apiClient?.listSessions() ?: emptyList()
            val livePool = apiClient?.getLivePool() ?: emptyList()

            // Extract SDK session IDs that are truly live
            _liveSessionIds.value = livePool.map { it.sdkSessionId }.toSet()

            _sessions.value = sessions.sortedByDescending { it.lastActivity }
            _sessionsLoading.value = false
        }
    }

    fun loadSession(sessionId: String, isOrchestrator: Boolean = false) {
        viewModelScope.launch {
            // Save current session to cache before switching
            saveCurrentSessionToCache()

            // Check if session is already cached
            val cached = sessionCache[sessionId]
            if (cached != null) {
                // Restore from cache - instant switch!
                currentSessionIdForPagination = sessionId
                paginationStartIndex = cached.paginationStartIndex
                _hasMoreMessages.value = cached.hasMoreMessages
                _messages.value = cached.messages
                _isOrchestratorSession.value = cached.isOrchestrator

                // Still need to reconnect WebSocket for the new session
                _currentLocalId.value = UUID.randomUUID().toString()
                disconnect()
                val endpoint = if (isOrchestrator) WebSocketEndpoint.ORCHESTRATOR else WebSocketEndpoint.AGENT
                webSocketManager.connect(_settings.value.serverUrl, _currentLocalId.value, endpoint)
                webSocketManager.send(WebSocketMessage.Start(
                    localId = _currentLocalId.value,
                    resumeSdkId = sessionId
                ))
                return@launch
            }

            // Not cached - fetch from server with pagination
            val paginated = apiClient?.getMessagesPaginated(sessionId, limit = 50)
                ?: com.assistant.peripheral.network.PaginatedMessages(emptyList(), 0, false, 0)

            if (paginated.totalCount > 0 || paginated.messages.isNotEmpty()) {
                // Store pagination state for loading more
                currentSessionIdForPagination = sessionId
                paginationStartIndex = paginated.startIndex
                _hasMoreMessages.value = paginated.hasMore
                _messages.value = paginated.messages

                // Generate new local ID for this session
                _currentLocalId.value = UUID.randomUUID().toString()

                // Set orchestrator flag
                _isOrchestratorSession.value = isOrchestrator

                // Reconnect with resume - use appropriate endpoint
                disconnect()
                val endpoint = if (isOrchestrator) WebSocketEndpoint.ORCHESTRATOR else WebSocketEndpoint.AGENT
                webSocketManager.connect(_settings.value.serverUrl, _currentLocalId.value, endpoint)

                // Send start with resume
                webSocketManager.send(WebSocketMessage.Start(
                    localId = _currentLocalId.value,
                    resumeSdkId = sessionId
                ))
            }
        }
    }

    /**
     * Save current session state to cache for quick restoration later.
     * Limits messages to prevent TransactionTooLargeException.
     */
    private fun saveCurrentSessionToCache() {
        val sessionId = currentSessionIdForPagination ?: return
        if (_messages.value.isEmpty()) return

        // Limit messages to prevent memory issues
        val messagesToCache = if (_messages.value.size > MAX_CACHED_MESSAGES_PER_SESSION) {
            // Keep only the most recent messages
            _messages.value.takeLast(MAX_CACHED_MESSAGES_PER_SESSION)
        } else {
            _messages.value
        }

        sessionCache[sessionId] = CachedSession(
            messages = messagesToCache,
            isOrchestrator = _isOrchestratorSession.value,
            paginationStartIndex = paginationStartIndex,
            // If we trimmed messages, mark as having more
            hasMoreMessages = _hasMoreMessages.value || _messages.value.size > MAX_CACHED_MESSAGES_PER_SESSION
        )

        // Evict oldest sessions if cache is too large
        while (sessionCache.size > MAX_CACHED_SESSIONS) {
            val oldestKey = sessionCache.keys.firstOrNull() ?: break
            sessionCache.remove(oldestKey)
        }
    }

    /**
     * Load older messages when user scrolls up (reverse infinite scroll).
     * Messages are prepended to the existing list.
     */
    fun loadMoreMessages() {
        if (_isLoadingMoreMessages.value || !_hasMoreMessages.value) return
        val sessionId = currentSessionIdForPagination ?: return

        viewModelScope.launch {
            _isLoadingMoreMessages.value = true
            try {
                val paginated = apiClient?.getMessagesPaginated(
                    sessionId,
                    limit = 50,
                    beforeIndex = paginationStartIndex
                ) ?: return@launch

                if (paginated.messages.isNotEmpty()) {
                    // Prepend older messages to the list
                    _messages.update { paginated.messages + it }
                    paginationStartIndex = paginated.startIndex
                    _hasMoreMessages.value = paginated.hasMore
                }
            } finally {
                _isLoadingMoreMessages.value = false
            }
        }
    }

    fun newSession() {
        // Save current session to cache before starting new one
        saveCurrentSessionToCache()

        // Generate new local ID
        _currentLocalId.value = UUID.randomUUID().toString()
        _messages.value = emptyList()

        // Reset pagination state
        currentSessionIdForPagination = null
        paginationStartIndex = 0
        _hasMoreMessages.value = false

        // Reconnect
        if (connectionState.value is ConnectionState.Connected) {
            webSocketManager.send(WebSocketMessage.Stop)
        }
        connect()
    }

    fun deleteSession(sessionId: String) {
        viewModelScope.launch {
            val success = apiClient?.deleteSession(sessionId) ?: false
            if (success) {
                _sessions.update { it.filter { s -> s.sessionId != sessionId } }
                // Also remove from cache
                sessionCache.remove(sessionId)
            }
        }
    }

    fun renameSession(sessionId: String, title: String) {
        viewModelScope.launch {
            val success = apiClient?.renameSession(sessionId, title) ?: false
            if (success) {
                _sessions.update { sessions ->
                    sessions.map { s ->
                        if (s.sessionId == sessionId) s.copy(title = title) else s
                    }
                }
            }
        }
    }

    // Recording
    fun startRecording() {
        viewModelScope.launch {
            val success = audioRecorder.startRecording()
            if (success) {
                _isRecording.value = true
            } else {
                val errorMessage = ChatMessage(
                    role = MessageRole.SYSTEM,
                    content = "Failed to start recording. Check microphone permission."
                )
                _messages.update { it + errorMessage }
            }
        }
    }

    fun stopRecording() {
        viewModelScope.launch {
            val base64Audio = audioRecorder.stopRecording()
            _isRecording.value = false

            if (base64Audio != null) {
                // Add user message placeholder
                val userMessage = ChatMessage(
                    role = MessageRole.USER,
                    content = "[Voice message]",
                    blocks = listOf(MessageBlock.Text("[Voice message]"))
                )
                _messages.update { it + userMessage }

                // Send audio to server
                webSocketManager.send(WebSocketMessage.SendAudio(base64Audio, "wav"))
            }
        }
    }

    // Voice session controls (WebRTC)
    fun startVoiceSession() {
        // Voice only works with orchestrator sessions
        if (!_isOrchestratorSession.value) {
            _voiceState.value = VoiceState.Error("Voice only available for orchestrator sessions")
            return
        }

        val vm = voiceManager
        if (vm == null) {
            _voiceState.value = VoiceState.Error("Voice manager not initialized")
            return
        }

        viewModelScope.launch {
            // Send voice_start to backend — passes local_id AND the true JSONL session id
            // so the orchestrator resumes from the correct history file.
            // NOTE: _currentSessionId may be local_id on reconnect; _jsonlSessionId is always
            // the real SDK/JSONL id (matches web frontend's resumeSdkId behaviour).
            webSocketManager.send(WebSocketMessage.VoiceStart(
                localId = _currentLocalId.value,
                resumeSdkId = _jsonlSessionId ?: _currentSessionId.value
            ))
            // Then start the actual WebRTC connection
            vm.start()
        }
    }

    fun stopVoiceSession() {
        viewModelScope.launch {
            voiceManager?.stop()
            _voiceState.value = VoiceState.Off
            _isMuted.value = false
        }
    }

    fun toggleMute() {
        val newMuteState = voiceManager?.toggleMute() ?: !_isMuted.value
        _isMuted.value = newMuteState
    }

    // Network discovery
    fun scanForServers() {
        if (_isScanning.value) return
        viewModelScope.launch {
            _isScanning.value = true
            _discoveredServers.value = emptyList()
            try {
                val servers = NetworkScanner.scan(getApplication())
                _discoveredServers.value = servers
                // Auto-connect to first discovered server if not already connected
                if (servers.isNotEmpty() && connectionState.value !is ConnectionState.Connected) {
                    connectToDiscoveredServer(servers.first())
                }
            } finally {
                _isScanning.value = false
            }
        }
    }

    fun connectToDiscoveredServer(server: DiscoveredServer) {
        viewModelScope.launch {
            dataStore.edit { preferences ->
                preferences[PreferenceKeys.SERVER_URL] = server.wsUrl
            }
            // connect() will be triggered by settings update via DataStore flow
        }
    }

    // Settings
    fun updateServerUrl(url: String) {
        viewModelScope.launch {
            dataStore.edit { preferences ->
                preferences[PreferenceKeys.SERVER_URL] = url
            }
        }
    }

    fun updateThemeMode(mode: ThemeMode) {
        viewModelScope.launch {
            dataStore.edit { preferences ->
                preferences[PreferenceKeys.THEME_MODE] = mode.name
            }
        }
    }

    fun updateAutoConnect(enabled: Boolean) {
        viewModelScope.launch {
            dataStore.edit { preferences ->
                preferences[PreferenceKeys.AUTO_CONNECT] = enabled
            }
        }
    }

    fun updateMicGainLevel(level: Float) {
        viewModelScope.launch {
            dataStore.edit { preferences ->
                preferences[PreferenceKeys.MIC_GAIN_LEVEL] = level.coerceIn(0.0f, 2.0f)
            }
            // Apply gain to active voice session
            voiceManager?.setMicGain(level)
        }
    }

    fun updateSpeakerVolumeLevel(level: Float) {
        viewModelScope.launch {
            val clamped = level.coerceIn(0.0f, 1.5f)
            dataStore.edit { preferences ->
                preferences[PreferenceKeys.SPEAKER_VOLUME_LEVEL] = clamped
            }
            // Apply to system audio
            val audioManager = getApplication<Application>().getSystemService(Context.AUDIO_SERVICE) as AudioManager
            val maxVolume = audioManager.getStreamMaxVolume(AudioManager.STREAM_MUSIC)
            val newVolume = (clamped * maxVolume).toInt().coerceIn(0, maxVolume)
            audioManager.setStreamVolume(AudioManager.STREAM_MUSIC, newVolume, 0)
        }
    }

    fun updateEnableWakeWord(enabled: Boolean) {
        viewModelScope.launch {
            dataStore.edit { preferences ->
                preferences[PreferenceKeys.ENABLE_WAKE_WORD] = enabled
            }
            val s = _settings.value
            AssistantService.updateWakeWord(getApplication(), enabled, s.wakeWord, s.voiceWord)
        }
    }

    fun updateWakeWord(word: String) {
        viewModelScope.launch {
            dataStore.edit { preferences ->
                preferences[PreferenceKeys.WAKE_WORD] = word
            }
            val s = _settings.value
            if (s.enableWakeWord) {
                AssistantService.updateWakeWord(getApplication(), true, word, s.voiceWord)
            }
        }
    }

    fun updateVoiceWord(word: String) {
        viewModelScope.launch {
            dataStore.edit { preferences ->
                preferences[PreferenceKeys.VOICE_WORD] = word
            }
            val s = _settings.value
            if (s.enableWakeWord) {
                AssistantService.updateWakeWord(getApplication(), true, s.wakeWord, word)
            }
        }
    }

    override fun onCleared() {
        super.onCleared()
        voiceManager?.release()
        webSocketManager.release()
        audioRecorder.release()
    }
}
