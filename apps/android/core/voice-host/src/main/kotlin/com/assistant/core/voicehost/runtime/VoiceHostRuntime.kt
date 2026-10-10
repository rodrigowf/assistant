package com.assistant.core.voicehost.runtime

import com.assistant.core.audio.ports.MonotonicClock
import com.assistant.core.audio.ports.PcmMath
import com.assistant.core.audio.ports.VoiceLog
import com.assistant.core.model.VoiceConfig
import com.assistant.core.network.SendResult
import com.assistant.core.network.SocketState
import com.assistant.core.protocol.ClientFrame
import com.assistant.core.protocol.ServerFrame
import com.assistant.core.session.ChannelEvent
import com.assistant.core.session.ChannelState
import com.assistant.core.session.OrchestratorChannel
import com.assistant.core.voice.VoiceTuning
import com.assistant.core.voice.platform.VoiceFrames
import com.assistant.core.voice.ports.AudioSessionPort
import com.assistant.core.voice.ports.ConnectionSignal
import com.assistant.core.voice.ports.OrchestratorContext
import com.assistant.core.voice.ports.SessionPhase
import com.assistant.core.voice.ports.TranscriptSink
import com.assistant.core.voice.ports.VoiceBackendApi
import com.assistant.core.voice.ports.VoiceInbound
import com.assistant.core.voice.ports.VoiceLevels
import com.assistant.core.voice.ports.VoiceSessionDeps
import com.assistant.core.voice.ports.VoiceSessionEvent
import com.assistant.core.voice.ports.VoiceStartRequest
import com.assistant.core.voice.ports.VoiceTransportFactory
import com.assistant.core.voice.ports.VoiceWire
import com.assistant.core.voice.ports.WakeHandoff
import com.assistant.core.voice.session.DefaultVoiceSessionController
import com.assistant.core.voice.session.ReconnectableVoiceSession
import com.assistant.core.voice.session.VoiceLinkState
import com.assistant.core.voicehost.HostConfig
import com.assistant.core.voicehost.HostConnection
import com.assistant.core.voicehost.HostTuning
import com.assistant.core.voicehost.LastExchange
import com.assistant.core.voicehost.VoiceHost
import com.assistant.core.voicehost.VoiceHostSettings
import com.assistant.core.voicehost.VoiceUiEvent
import com.assistant.core.voicehost.VoiceUiState
import com.assistant.core.voicehost.WakeHealth
import com.assistant.core.voicehost.cue.CuePlayer
import com.assistant.core.voicehost.cue.HostCues
import com.assistant.core.voicehost.cue.LinkCueScheduler
import com.assistant.core.voicehost.fgs.FgsDecision
import com.assistant.core.voicehost.ports.TalkMessageSender
import com.assistant.core.voicehost.ports.TalkUiState
import com.assistant.core.voicehost.ports.Trigger
import com.assistant.core.voicehost.ports.TriggerDeps
import com.assistant.core.voicehost.ports.WakeConfigStore
import com.assistant.core.voicehost.ports.WakeEngineProvider
import com.assistant.core.voicehost.ports.WakeNotice
import com.assistant.core.voicehost.ports.WakeServiceConfig
import com.assistant.core.voicehost.ports.WakeServiceDeps
import com.assistant.core.voicehost.toChoice
import com.assistant.core.voicehost.trigger.DefaultTriggerRouter
import com.assistant.core.voicehost.trigger.ForegroundBringer
import com.assistant.core.voicehost.trigger.TriggerIngress
import com.assistant.core.voicehost.wake.DefaultWakeServiceController
import com.assistant.core.wakeword.ports.WakeLoopEvent
import com.assistant.core.wakeword.ports.WakePhase
import com.assistant.core.wakeword.ports.WakeWordEngine
import java.util.UUID
import java.util.concurrent.atomic.AtomicReference
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.filterNotNull
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.flatMapLatest
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Everything the runtime needs, as ports (Android adapters in `AndroidVoiceHost`; fakes in tests).
 *
 * @property scope process-lifetime scope (SupervisorJob). Never cancelled in production.
 * @property settings device settings, `null` until loaded: nothing is applied before the real
 *   values arrive (fixes inv04 B4, the startup default-config race).
 * @property wakeEngines builds a wake engine (A-07 `WakeLoop`) for one configuration.
 */
class RuntimeDeps(
    val scope: CoroutineScope,
    val clock: MonotonicClock,
    val log: VoiceLog,
    val config: HostConfig,
    val channel: OrchestratorChannel,
    val settings: Flow<VoiceHostSettings?>,
    val wakeStore: WakeConfigStore,
    val wakeEngines: (WakeServiceConfig) -> WakeWordEngine,
    val voiceApi: VoiceBackendApi,
    val transports: VoiceTransportFactory,
    val audio: AudioSessionPort,
    val transcripts: TranscriptSink,
    val cuePlayer: CuePlayer,
    val bringer: ForegroundBringer,
    val pcm: PcmMath,
    val service: ServiceControl,
    val buttonTriggerPref: ButtonTriggerPref = ButtonTriggerPref { },
    val pushToTalk: PushToTalkRecorder? = null,
    val speakerMuter: SpeakerMuter = SpeakerMuter { },
    /**
     * Lite: the runtime connects / switches servers / handles foreground itself. Main: the app's
     * `ConnectionRepository` + `ConversationRepository` own the channel lifecycle and the runtime
     * only rides on the shared socket.
     */
    val manageConnection: Boolean = true,
    val sessionFactory: (VoiceSessionDeps) -> ReconnectableVoiceSession = { DefaultVoiceSessionController(it) },
    /**
     * Main app (B-09, spec 12 §4.7 / VT-2): every inbound OpenAI data-channel event of the session
     * this device owns (exactly what the WebRTC transport mirrors as `voice_event`). The server never
     * echoes these back, so the app feeds them to its own timeline. Default: ignored (lite).
     */
    val dataChannelTap: (kotlinx.serialization.json.JsonObject) -> Unit = { },
)

/**
 * The process-scoped voice host (spec 14 §2.5): owns the orchestrator channel wiring, the voice
 * session, the wake-word service logic, cues, the single [TriggerIngress], and the aggregated
 * [VoiceUiState]. The foreground service only keeps the process alive and renders the notification.
 *
 * P-1: no path in here ever sends `stop`/`close`. `voice_stop` is sent only by an explicit user
 * [stopVoice] (or the session's own user-stop path).
 */
@OptIn(ExperimentalCoroutinesApi::class)
class VoiceHostRuntime(private val deps: RuntimeDeps) : VoiceHost {
    private val log = deps.log
    private val scope = deps.scope
    private val channel = deps.channel

    val config: HostConfig get() = deps.config

    /** The app's single orchestrator channel (main: shared with the conversation repository). */
    val orchestrator: OrchestratorChannel get() = channel

    private val _events = MutableSharedFlow<VoiceUiEvent>(extraBufferCapacity = 64)
    override val events: Flow<VoiceUiEvent> = _events.asSharedFlow()

    private val _state = MutableStateFlow(VoiceUiState())
    override val state: StateFlow<VoiceUiState> = _state.asStateFlow()

    /** The session's own levels (already shared and subscription-gated). */
    override val levels: StateFlow<VoiceLevels?> get() = session.levels

    // ── wake ────────────────────────────────────────────────────────────────────────────────────
    val micGate = MicGate()
    private val wakePausedForVoice = MutableStateFlow(false)
    private val backgroundRestricted = MutableStateFlow(false)
    private val currentEngine = MutableStateFlow<GatedWakeEngineHandle?>(null)
    private val settingsFlow = MutableStateFlow<VoiceHostSettings?>(null)
    private val lastExchange = MutableStateFlow(LastExchange())
    private val speakerMuted = MutableStateFlow(false)
    private val pushToTalkActive = MutableStateFlow(false)
    private val linkDismissed = MutableStateFlow(false)

    private val engineProvider = WakeEngineProvider { cfg ->
        GatedWakeEngineHandle(deps.wakeEngines(cfg), micGate, scope, log) { handle, event -> onEngineEvent(handle, event) }
            .also { currentEngine.value = it }
    }

    val wakeService = DefaultWakeServiceController(WakeServiceDeps(scope, deps.clock, log, deps.wakeStore, engineProvider))

    // ── cues ────────────────────────────────────────────────────────────────────────────────────
    val cues = HostCues(deps.cuePlayer, log) { _events.tryEmit(VoiceUiEvent.Cue(it)) }

    // ── voice session ───────────────────────────────────────────────────────────────────────────
    @Volatile private var pendingNewLocalId: String? = null
    private var pttStart: Job? = null

    /** §6.11a: the resumed conversation's `localId` whose `session_started` starts voice (taken once). */
    private val switchVoiceLocalId = AtomicReference<String?>(null)

    /** §6.11a: voice ended for a switch and the call continues in the resumed conversation (keeps the FGS). */
    @Volatile private var switchHold = false
    private var switchHoldJob: Job? = null

    /** Main app: agent turns this device will notify about that are in flight ([setAgentWorkHold]). */
    private val agentTurns = MutableStateFlow(0)

    private val orchestratorContext = object : OrchestratorContext {
        override val isOrchestratorSession: Boolean get() = true
        override val localId: String get() = channel.state.value.orchestrator?.localId ?: pendingNewLocalId.orEmpty()
        override val jsonlSessionId: String? get() = channel.state.value.orchestrator?.sdkId
        override val currentSessionId: String? get() = channel.state.value.orchestrator?.sdkId
    }

    private val wire = object : VoiceWire {
        override fun sendVoiceStart(request: VoiceStartRequest) {
            val frame = ClientFrame.VoiceStart(
                localId = request.localId,
                resumeSdkId = request.resumeSdkId,
                voice = VoiceConfig(
                    provider = request.provider,
                    model = request.model,
                    voice = request.voiceName,
                    transcriptionLanguage = request.transcriptionLanguage,
                    endpoint = request.endpoint,
                ),
                reattach = request.reattach.takeIf { it },
            )
            sendOrLog(frame)
        }

        override fun sendVoiceStop() { sendOrLog(ClientFrame.VoiceStop) }
        override fun sendVoiceEvent(event: kotlinx.serialization.json.JsonObject) {
            channel.send(ClientFrame.VoiceEvent(event))
            try {
                deps.dataChannelTap(event)
            } catch (e: Exception) {
                log.w(TAG, "data-channel tap failed: ${e.message}")
            }
        }
        override fun sendVoiceAudioIn(audioB64: String) { channel.send(ClientFrame.VoiceAudioIn(audioB64)) }

        /**
         * The plain `start` after a reconnect is owned by the channel (`autoStart`, lite) or by the
         * conversation repository (main): sending it here too would double it.
         */
        override fun sendResumeStart(localId: String, sdkSessionId: String?) {
            log.d(TAG, "reconnect without live voice — start is owned by the channel / conversation repository")
        }
    }

    private val handoff = object : WakeHandoff {
        override fun pauseWake(): Deferred<Unit> {
            val ack = CompletableDeferred<Unit>()
            wakePausedForVoice.value = true
            wakeService.onPauseForVoice(ack)
            return ack
        }

        override fun resumeWake(): Deferred<Unit> {
            val ack = CompletableDeferred<Unit>()
            wakeService.onResumeAfterVoice(ack)
            wakePausedForVoice.value = wakeService.voiceSessionActive
            return ack
        }
    }

    private val transcriptTee = object : TranscriptSink {
        override fun userTranscript(text: String, final: Boolean) {
            if (final) lastExchange.value = LastExchange(user = text.take(LastExchange.MAX_CHARS), assistant = null)
            _events.tryEmit(VoiceUiEvent.Transcript(VoiceUiEvent.Role.USER, text, final))
            deps.transcripts.userTranscript(text, final)
        }

        override fun assistantTranscript(text: String, final: Boolean) {
            if (final) lastExchange.update { it.copy(assistant = text.take(LastExchange.MAX_CHARS)) }
            _events.tryEmit(VoiceUiEvent.Transcript(VoiceUiEvent.Role.ASSISTANT, text, final))
            deps.transcripts.assistantTranscript(text, final)
        }

        override fun system(text: String) {
            _events.tryEmit(VoiceUiEvent.Transcript(VoiceUiEvent.Role.SYSTEM, text, true))
            deps.transcripts.system(text)
        }

        override fun voiceMessageSent() = deps.transcripts.voiceMessageSent()
        override fun turnComplete() = deps.transcripts.turnComplete()
        override fun voiceEnded() = deps.transcripts.voiceEnded()
    }

    val session: ReconnectableVoiceSession = deps.sessionFactory(
        VoiceSessionDeps(
            scope = scope,
            clock = deps.clock,
            log = log,
            api = deps.voiceApi,
            wire = wire,
            orchestrator = orchestratorContext,
            wake = handoff,
            transports = deps.transports,
            audio = deps.audio,
            transcripts = transcriptTee,
            cues = cues,
        ),
    )

    val linkCues = LinkCueScheduler(scope, cues, { session.link.value }, deps.clock)

    // ── triggers ────────────────────────────────────────────────────────────────────────────────
    private val talkSender = TalkMessageSender { wav ->
        val b64 = deps.pcm.base64(wav)
        val sent = channel.send(ClientFrame.SendAudio(b64, "wav"))
        if (sent == SendResult.SENT) {
            transcriptTee.voiceMessageSent()
        } else {
            log.w(TAG, "voice message not sent ($sent)")
            toast("Voice message not sent — not connected")
        }
    }

    private val router = DefaultTriggerRouter(
        TriggerDeps(session, cues, talkSender, { settingsFlow.value?.buttonTriggerEnabled == true }, log),
    )

    /** The one entry point for every trigger source (spec 14 §2.5). */
    val ingress = TriggerIngress(
        router,
        if (deps.config.bringToFrontOnTrigger) deps.bringer else ForegroundBringer.NONE,
    ) { settingsFlow.value?.buttonTriggerEnabled == true }

    // ── lifecycle ───────────────────────────────────────────────────────────────────────────────
    private var lastApplied: VoiceHostSettings? = null
    private var lastWake: WakeServiceConfig? = null
    private var connectRequested = false
    @Volatile private var uiStarted = false
    private var idleDisconnectJob: Job? = null
    private val collectors = mutableListOf<Job>()

    init {
        collectors += scope.launch { channel.frames.collect { f -> onFrame(f) } }
        collectors += scope.launch { channel.audioFrames.collect { session.onInbound(VoiceInbound.AudioOut(it.audio)) } }
        collectors += scope.launch { channel.events.collect { onChannelEvent(it) } }
        collectors += scope.launch { session.linkEvents.collect { linkCues.onLinkEvent(it) } }
        collectors += scope.launch {
            session.link.collect {
                linkCues.onLinkState(it)
                if (it !is VoiceLinkState.Failed) linkDismissed.value = false
            }
        }
        collectors += scope.launch {
            session.events.collect { if (it is VoiceSessionEvent.Toast) toast(it.message) }
        }
        collectors += scope.launch { deps.settings.filterNotNull().collect { applySettings(it) } }
        collectors += scope.launch { assembleState() }
        collectors += scope.launch { session.state.collect { onSessionState(it.phase, it.isOwner) } }
    }

    // ── VoiceHost ───────────────────────────────────────────────────────────────────────────────

    override fun connect() {
        if (!deps.manageConnection) return channel.onNetworkAvailable() // main: ConnectionRepository owns connect/switch
        connectRequested = true
        val url = settingsFlow.value?.serverUrl
        if (url == null) {
            log.d(TAG, "connect() before settings loaded — deferred")
            return
        }
        channel.connect(url)
    }

    override fun disconnect() {
        if (!deps.manageConnection) return log.d(TAG, "disconnect() ignored — the app's ConnectionRepository owns the socket")
        connectRequested = false
        channel.disconnect()
    }

    override fun startVoice(trigger: Trigger) {
        if (channel.state.value.orchestrator == null && channel.state.value.noOrchestrator) {
            // No conversation on the server: arm a new orchestrator with a client-minted id (spec 14 §5.4).
            val id = UUID.randomUUID().toString()
            pendingNewLocalId = id
            channel.armNewSession(id)
        }
        ingress.trigger(trigger)
        ensureServiceIfForeground()
    }

    /**
     * User stop. With no live session (OFF / ERROR, e.g. the P-2 failure state) nothing is sent: the
     * call only dismisses the failure banner. (`session.stopVoice()` on a finalized session would sit
     * in ENDING forever, because its 5 s timeout finalize is a no-op once finalized.)
     * Root cause fixed in `:core:voice` (OI-4): the session's own `stopVoice()` is now a no-op without a
     * live session; this guard stays as defence in depth.
     */
    override fun stopVoice() {
        val phase = session.state.value.phase
        if (phase == SessionPhase.OFF || phase == SessionPhase.ERROR) {
            if (session.link.value is VoiceLinkState.Failed) linkDismissed.value = true
            return
        }
        session.stopVoice()
    }

    override fun reconnectVoice() = session.reconnectVoice()

    override fun toggleMute() = session.toggleMute()

    override fun toggleSpeakerMute() {
        val muted = !speakerMuted.value
        speakerMuted.value = muted
        deps.speakerMuter.setMuted(muted)
    }

    override fun startPushToTalk() {
        val rec = deps.pushToTalk ?: return log.w(TAG, "push-to-talk not available in this app")
        if (pushToTalkActive.value) return
        val phase = session.state.value.phase
        if (phase != SessionPhase.OFF && phase != SessionPhase.ERROR) return log.d(TAG, "push-to-talk ignored — voice is $phase")
        pushToTalkActive.value = true
        // B8: pause the wake word first and let its loop release the AudioRecord, so two
        // AudioRecords never compete for the mic (the loop releases on its own thread, R4).
        handoff.pauseWake()
        pttStart = scope.launch {
            withTimeoutOrNull(VoiceTuning.WAKE_WORD_ACK_TIMEOUT_MS) {
                currentEngine.value?.phase?.first { it == WakePhase.PAUSED || it == WakePhase.STOPPED }
            }
            if (!rec.start()) {
                pushToTalkActive.value = false
                toast("Microphone unavailable")
                resumeWakeAfterMicRelease()
            }
        }
    }

    override fun stopPushToTalk(send: Boolean) {
        val rec = deps.pushToTalk ?: return
        if (!pushToTalkActive.value) return
        pushToTalkActive.value = false
        val starting = pttStart
        scope.launch {
            starting?.join()
            val wav = rec.stop()
            if (send && wav != null) talkSender.send(wav)
            resumeWakeAfterMicRelease()
        }
    }

    private suspend fun resumeWakeAfterMicRelease() {
        delay(VoiceTuning.MIC_RELEASE_DELAY_MS)
        handoff.resumeWake()
    }

    override fun pauseListening() = micGate.close(GateReason.USER_PAUSED)

    override fun resumeListening() {
        micGate.open(GateReason.USER_PAUSED)
        backgroundRestricted.value = false
    }

    override fun updateSettings(s: VoiceHostSettings) = applySettings(s)

    // ── app / service hooks ─────────────────────────────────────────────────────────────────────

    /** Main app: Activity ON_START (ProcessLifecycleOwner). A foreground context. */
    fun onUiStarted() {
        uiStarted = true
        idleDisconnectJob?.cancel()
        if (deps.manageConnection) channel.onForeground()
        ensureServiceIfForeground()
    }

    /** Main app: ON_STOP. Keeps the socket alive while the service needs it (T-14); otherwise disconnects 60 s later. */
    fun onUiStopped() {
        uiStarted = false
        val keep = serviceWanted()
        idleDisconnectJob?.cancel()
        if (!deps.manageConnection) return
        channel.onBackground(keepAlive = keep)
        if (!keep && !deps.config.serviceRunsAlways) {
            idleDisconnectJob = scope.launch {
                delay(HostTuning.UI_STOPPED_DISCONNECT_DELAY_MS)
                if (!uiStarted && !serviceWanted()) {
                    log.d(TAG, "UI stopped for ${HostTuning.UI_STOPPED_DISCONNECT_DELAY_MS} ms and nothing needs the socket — disconnecting (sends nothing, P-1)")
                    channel.disconnect()
                }
            }
        }
    }

    /**
     * Whether the FGS should run (spec 14 §2.5): always (lite) or wake / voice / "stay connected" /
     * agent work being watched for a notification (main).
     */
    fun serviceWanted(): Boolean = agentTurns.value > 0 || serviceWantedBesidesAgentWork()

    private fun serviceWantedBesidesAgentWork(): Boolean {
        if (deps.config.serviceRunsAlways) return true
        val s = settingsFlow.value ?: return false
        val p = session.state.value.phase
        val voiceLive = p != SessionPhase.OFF && p != SessionPhase.ERROR
        return s.enableWakeWord || voiceLive || switchHold || s.stayConnectedInBackground
    }

    /**
     * Whether a (re)start may take the microphone FGS type. False while the agent-work hold is the
     * only reason the service runs: it never opens the mic, so it starts as special-use only; wake
     * word or voice later promote it from a foreground start (FgsPolicy).
     */
    fun micTypeWanted(): Boolean = serviceWantedBesidesAgentWork()

    /**
     * Main app (Settings → Notifications → "Agent session finished" on, an agent turn running): keep
     * the service, and with it the process and the orchestrator socket, until the turn ends, so the
     * "finished" notification still arrives with the phone in a pocket. Like every other reason the
     * service only *starts* from a foreground context (Android 12+), so this works for turns that are
     * running while the app is in front or brought to front; the hold drops with the last turn.
     */
    fun setAgentWorkHold(turns: Int) {
        val n = turns.coerceAtLeast(0)
        val was = agentTurns.value
        if (was == n) return
        agentTurns.value = n
        log.d(TAG, "agent work hold: $n turn(s)")
        when {
            was == 0 -> ensureServiceIfForeground()
            n == 0 -> if (!serviceWanted()) deps.service.stop()
        }
    }

    /** The last applied settings (null until loaded). */
    fun lastSettings(): VoiceHostSettings? = settingsFlow.value

    fun buttonTriggerEnabled(): Boolean = settingsFlow.value?.buttonTriggerEnabled == true

    /** The service applied an FGS decision: degrade the wake mic when it was started without the microphone type. */
    fun onServiceStarted(decision: FgsDecision) {
        if (decision.micAllowed) micGate.open(GateReason.NEEDS_FOREGROUND) else micGate.close(GateReason.NEEDS_FOREGROUND)
        backgroundRestricted.value = false
    }

    /** `startForegroundService` was refused (Android 12+ background start). */
    fun onServiceStartRefused() {
        backgroundRestricted.value = true
    }

    /** START_STICKY restart: restore the wake word from `assistant_service_prefs` (RS-32). */
    fun onStickyRestart() = wakeService.onStickyRestart()

    fun onScreenOnOrUserPresent() = wakeService.onScreenOnOrUserPresent()

    /** Tests / process teardown. Sends nothing (P-1). */
    fun release() {
        collectors.forEach { it.cancel() }
        linkCues.stopPattern()
        wakeService.destroy()
        session.release()
    }

    // ── internals ───────────────────────────────────────────────────────────────────────────────

    private fun ensureServiceIfForeground() {
        if (!uiStarted || !serviceWanted()) return
        if (!deps.service.startFromForeground()) onServiceStartRefused()
    }

    private fun onSessionState(phase: SessionPhase, owner: Boolean) {
        if (phase == SessionPhase.OFF || phase == SessionPhase.ERROR) {
            if (speakerMuted.value) {
                speakerMuted.value = false
                deps.speakerMuter.setMuted(false)
            }
            if (!serviceWanted()) deps.service.stop()
        } else if (owner) {
            ensureServiceIfForeground()
        }
    }

    private fun onEngineEvent(handle: GatedWakeEngineHandle, event: WakeLoopEvent) {
        // Late events of a replaced engine are dropped.
        if (wakeService.currentEngine !== handle) return
        when (event) {
            WakeLoopEvent.MicUnavailable -> wakeService.onMicUnavailable()
            WakeLoopEvent.MicAvailable -> wakeService.onMicAvailable()
            WakeLoopEvent.RecognizerUnhealthy -> wakeService.onRecognizerUnhealthy()
            else -> ingress.onWakeEvent(event)
        }
    }

    private fun onFrame(f: ServerFrame) {
        // §6.11a SW-3: hold the service BEFORE the session goes OFF, so the call can go on in the
        // resumed conversation (the state collector sees OFF only after this).
        val reason = (f as? ServerFrame.VoiceEnding)?.reason ?: (f as? ServerFrame.VoiceEnded)?.reason
        if (reason == END_REASON_SWITCH && session.state.value.isOwner) holdForSwitch()
        VoiceFrames.toInbound(f)?.let { session.onInbound(it) }
        // After the session saw it: the resume `start`'s session_started{voice:false} has cleared any pre-start state.
        if (f is ServerFrame.SessionStarted) f.sessionId?.let { startSwitchedVoice(it) }
    }

    private fun onChannelEvent(e: ChannelEvent) {
        when (e) {
            is ChannelEvent.Adopted -> if (!e.reconnect) session.onConnection(ConnectionSignal.Connected)
            is ChannelEvent.NewSessionArmed -> session.onConnection(ConnectionSignal.Connected)
            is ChannelEvent.Reconnected -> session.onConnection(ConnectionSignal.Reconnected(e.ref.localId, e.ref.sdkId))
            is ChannelEvent.Disconnected -> session.onConnection(ConnectionSignal.Disconnected(e.willReconnect))
            is ChannelEvent.SwitchRequested -> onSwitchRequested(e)
            is ChannelEvent.Conflict, ChannelEvent.GaveUp -> if (switchVoiceLocalId.getAndSet(null) != null) {
                log.w(TAG, "switch: the resumed conversation did not start — voice stays off")
                releaseSwitchHold()
            }
            else -> Unit
        }
    }

    /**
     * Spec 12 §6.11a / SW-4. The channel already armed the resumed conversation (new `localId`, the
     * past jsonl id) and its `start` goes out from the channel (lite, autoStart) or the conversation
     * repository (main). With [ChannelEvent.SwitchRequested.voice] voice starts on it once that start's
     * `session_started` arrives, so the server takes `voice_start` as the re-arm of the same
     * session; a `voice_start` racing ahead of the `start` would make this device a passive viewer.
     */
    private fun onSwitchRequested(e: ChannelEvent.SwitchRequested) {
        log.i(TAG, "switch: ${e.fromLocalId} → ${e.ref.sdkId} as ${e.ref.localId} (voice=${e.voice})")
        if (!e.voice) {
            switchVoiceLocalId.set(null)
            releaseSwitchHold()
            return
        }
        holdForSwitch()
        switchVoiceLocalId.set(e.ref.localId)
        // Its session_started may already have been seen (frames and events run on separate collectors).
        val st = channel.state.value
        if (st.subscribed && st.orchestrator?.localId == e.ref.localId) startSwitchedVoice(e.ref.localId)
    }

    /** SW-2: no gesture and no wake cue — the call simply continues in the resumed conversation. */
    private fun startSwitchedVoice(localId: String) {
        if (!switchVoiceLocalId.compareAndSet(localId, null)) return
        val phase = session.state.value.phase
        if (phase != SessionPhase.OFF && phase != SessionPhase.ERROR) {
            log.w(TAG, "switch: voice already $phase — not starting it again")
        } else {
            log.i(TAG, "switch: resumed conversation $localId is up — starting voice on it")
            session.markConnecting()
            session.startVoice()
        }
        releaseSwitchHold()
        ensureServiceIfForeground()
    }

    private fun holdForSwitch() {
        synchronized(this) {
            switchHold = true
            switchHoldJob?.cancel()
            switchHoldJob = scope.launch {
                delay(HostTuning.SWITCH_HOLD_MS)
                if (switchVoiceLocalId.getAndSet(null) != null) log.w(TAG, "switch: no session_started for the resumed conversation — giving up on voice")
                releaseSwitchHold()
            }
        }
    }

    private fun releaseSwitchHold() {
        val was = synchronized(this) {
            val held = switchHold
            switchHold = false
            switchHoldJob?.cancel()
            switchHoldJob = null
            held
        }
        if (was && !serviceWanted()) deps.service.stop()
    }

    @Synchronized
    private fun applySettings(s: VoiceHostSettings) {
        val prev = lastApplied
        if (prev == s) return
        lastApplied = s
        settingsFlow.value = s
        // Voice gains / output: always explicit (RS-33).
        session.setMicGain(s.micGain)
        session.setEchoDuckingGain(s.echoDuckingGain)
        if (prev?.audioOutput != s.audioOutput) session.setAudioOutput(s.audioOutput.toChoice())
        if (prev?.buttonTriggerEnabled != s.buttonTriggerEnabled) deps.buttonTriggerPref.set(s.buttonTriggerEnabled)
        // The wake service gets ONE update per change of its six fields (single ingress, `9200d50`).
        val wake = s.wakeConfig()
        if (wake != lastWake) {
            lastWake = wake
            wakeService.onConfigUpdate(wake)
        }
        // Server: connect on first load (auto-connect / pending connect), switch on change (T-15).
        if (deps.manageConnection) {
            when {
                prev == null -> if (s.autoConnect || connectRequested) channel.connect(s.serverUrl)
                prev.serverUrl != s.serverUrl -> channel.changeServer(s.serverUrl)
            }
        }
        if (prev != null && !serviceWanted()) deps.service.stop() else ensureServiceIfForeground()
    }

    private suspend fun assembleState() {
        val wakeInputs = combine(
            micGate.reasons,
            wakePausedForVoice,
            backgroundRestricted,
            wakeService.notice,
            settingsFlow,
        ) { gate, pausedForVoice, restricted, notice, s ->
            wakeHealth(s?.enableWakeWord == true, gate, pausedForVoice, restricted, notice)
        }
        val wakePhase: Flow<WakePhase?> = currentEngine.flatMapLatest { it?.phase ?: flowOf(null) }
        val visibleLink = combine(session.link, linkDismissed) { l, dismissed -> if (dismissed && l is VoiceLinkState.Failed) VoiceLinkState.Up else l }
        val talk = combine(router.talkUi, pushToTalkActive) { t, ptt -> if (ptt) t.copy(isRecording = true) else t }
        combine(
            listOf(channel.state, session.state, visibleLink, talk, wakeInputs, wakePhase, speakerMuted, pushToTalkActive, lastExchange, settingsFlow, agentTurns),
        ) { a ->
            val ch = a[0] as ChannelState
            val turns = a[10] as Int
            @Suppress("UNCHECKED_CAST")
            VoiceUiState(
                connection = connectionOf(ch.socket),
                serverUrl = (a[9] as VoiceHostSettings?)?.serverUrl,
                noOrchestrator = ch.noOrchestrator,
                session = a[1] as com.assistant.core.voice.ports.VoiceSessionState,
                link = a[2] as VoiceLinkState,
                talk = a[3] as TalkUiState,
                wake = a[4] as WakeHealth,
                wakePhase = a[5] as WakePhase?,
                speakerMuted = a[6] as Boolean,
                pushToTalk = a[7] as Boolean,
                lastExchange = a[8] as LastExchange,
                agentTurnsWaiting = if (turns > 0 && !serviceWantedBesidesAgentWork()) turns else 0,
            )
        }.collect { _state.value = it }
    }

    private fun sendOrLog(frame: ClientFrame) {
        val r = channel.send(frame)
        if (r != SendResult.SENT) log.w(TAG, "${frame.type} not sent ($r)")
    }

    private fun toast(message: String) {
        _events.tryEmit(VoiceUiEvent.Toast(message))
    }

    /** Cancels the runtime's own coroutines only (tests). */
    internal fun cancelForTest() = scope.cancel()

    companion object {
        private const val TAG = "VoiceHost"

        /** `voice_ending` / `voice_ended` reason of an agent-initiated conversation switch (spec 12 §6.11a). */
        private const val END_REASON_SWITCH = "switch"

        fun connectionOf(s: SocketState): HostConnection = when (s) {
            SocketState.Open -> HostConnection.CONNECTED
            is SocketState.Connecting -> HostConnection.CONNECTING
            is SocketState.Disconnected -> if (s.willReconnect) HostConnection.CONNECTING else HostConnection.OFFLINE
            SocketState.Idle -> HostConnection.OFFLINE
        }

        /** Pure: wake health from its inputs (precedence: disabled → foreground / restricted → user pause → voice → stalled → armed). */
        fun wakeHealth(
            enabled: Boolean,
            gate: Set<GateReason>,
            pausedForVoice: Boolean,
            backgroundRestricted: Boolean,
            notice: WakeNotice,
        ): WakeHealth = when {
            !enabled -> WakeHealth.DISABLED
            backgroundRestricted -> WakeHealth.BACKGROUND_RESTRICTED
            GateReason.NEEDS_FOREGROUND in gate -> WakeHealth.PAUSED_NEEDS_FOREGROUND
            GateReason.USER_PAUSED in gate -> WakeHealth.PAUSED_BY_USER
            pausedForVoice -> WakeHealth.PAUSED_FOR_VOICE
            notice == WakeNotice.MIC_STALLED -> WakeHealth.MIC_STALLED
            else -> WakeHealth.ARMED
        }
    }
}
