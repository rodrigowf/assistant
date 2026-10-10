package com.assistant.core.voice.session

import com.assistant.core.audio.AudioTuning
import com.assistant.core.audio.ports.FallbackReason
import com.assistant.core.audio.ports.OutputChoice
import com.assistant.core.audio.ports.ProviderKind
import com.assistant.core.audio.ports.Route
import com.assistant.core.voice.VoiceTuning
import com.assistant.core.voice.delivery.LockedCommandRelay
import com.assistant.core.voice.json.string
import com.assistant.core.voice.json.typeOf
import com.assistant.core.voice.ports.CommandRelay
import com.assistant.core.voice.ports.ConnectionSignal
import com.assistant.core.voice.ports.ProviderCommandSink
import com.assistant.core.voice.ports.ProviderPhase
import com.assistant.core.voice.ports.ProviderPhaseState
import com.assistant.core.voice.ports.ProviderSignal
import com.assistant.core.voice.ports.SessionPhase
import com.assistant.core.voice.ports.VoiceConnection
import com.assistant.core.voice.ports.VoiceInbound
import com.assistant.core.voice.ports.VoiceLevels
import com.assistant.core.voice.ports.VoiceSessionController
import com.assistant.core.voice.ports.VoiceSessionDeps
import com.assistant.core.voice.ports.VoiceSessionEvent
import com.assistant.core.voice.ports.VoiceSessionState
import com.assistant.core.voice.ports.VoiceStartConfig
import com.assistant.core.voice.ports.VoiceStartRequest
import com.assistant.core.voice.ports.VoiceTransport
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.flatMapLatest
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

/**
 * The voice-session controller plus the P-2 link-loss surface. Obtain it from
 * `VoiceCore.sessionController(deps)` (and cast) or construct [DefaultVoiceSessionController].
 */
interface ReconnectableVoiceSession : VoiceSessionController {
    /** Link health while voice is live (P-2). [VoiceLinkState.Up] when no voice or no outage. */
    val link: StateFlow<VoiceLinkState>

    /** Cue triggers for the host: lost → reconnecting tone, restored → "reconnected", failed → failure cue. */
    val linkEvents: SharedFlow<VoiceLinkEvent>

    /** The manual Reconnect of the failure state: clears it and starts voice again. */
    fun reconnectVoice()
}

/**
 * The realtime voice session (old `VoiceController` + `VoiceManager`, inv04 §3.2 table, the
 * ownership and `session.update` delivery sub-machines; RS-01, RS-09…RS-17, RS-19, RS-27, RS-29,
 * RS-30), written against the ports.
 *
 * Threading: every entry point may be called from any thread (UI, socket, transport collectors).
 * Mutable fields sit behind [lock]; collaborators are never called while holding it. Transport
 * phase and signal collectors run unconfined so no phase transition is conflated away (a
 * CONNECTING → ACTIVE pair must still clear the reconnect banner); the transports guarantee those
 * never run on a WebRTC native thread.
 *
 * Behaviour fixes vs the old code (accepted by the coordinator, 2026-10-03):
 *  - A second start while one is starting or live is ignored (old: a second `voice_start` was sent).
 *  - A new start cancels the previous session's pending wake re-arm (old: the stale resume
 *    could re-arm the wake word mid-call).
 *  - B10: a voice-config failure after the wake pause ends the session and re-arms the wake word.
 *
 * Minor, also deliberate:
 *  - `session_started{voice:false}` does not clear CONNECTING while a start is in flight.
 *  - Routing is only applied while this session holds audio focus.
 */
class DefaultVoiceSessionController(
    private val deps: VoiceSessionDeps,
    private val relay: CommandRelay = LockedCommandRelay(deps.log),
) : ReconnectableVoiceSession {

    private val log = deps.log
    private val scope = CoroutineScope(deps.scope.coroutineContext + SupervisorJob(deps.scope.coroutineContext[Job]))

    private val _state = MutableStateFlow(VoiceSessionState())
    private val _events = MutableSharedFlow<VoiceSessionEvent>(extraBufferCapacity = 64)
    private val _link = MutableStateFlow<VoiceLinkState>(VoiceLinkState.Up)
    private val _linkEvents = MutableSharedFlow<VoiceLinkEvent>(extraBufferCapacity = 16)
    override val state: StateFlow<VoiceSessionState> = _state.asStateFlow()
    override val events: SharedFlow<VoiceSessionEvent> = _events.asSharedFlow()
    override val link: StateFlow<VoiceLinkState> = _link.asStateFlow()
    override val linkEvents: SharedFlow<VoiceLinkEvent> = _linkEvents.asSharedFlow()

    /** Mirrors [transport] for [levels] only (observation; never read by the session logic). */
    private val levelSource = MutableStateFlow<VoiceTransport?>(null)

    @OptIn(ExperimentalCoroutinesApi::class)
    override val levels: StateFlow<VoiceLevels?> = levelSource
        .flatMapLatest { t -> t?.levels ?: flowOf(null) }
        .stateIn(scope, SharingStarted.WhileSubscribed(0, 0), null)

    // ── guarded by [lock] ──────────────────────────────────────────────────────────────────────
    private val lock = Any()
    private var owner = false
    private var finalized = false
    /**
     * `voice_start`s sent whose `voice_owner_active{active:true}` broadcast has not come back yet.
     * The server broadcasts that frame to every subscriber, the sender included, after each
     * successful `voice_start`: while this is > 0 an incoming one is our own echo; at 0 it means
     * another device took over (V-13). A start answered by an error never echoes and leaves this
     * high, which only costs one missed takeover (the old behaviour), never our own call.
     */
    private var ownerEchoesPending = 0
    private var activeConfig: VoiceStartConfig? = null
    private var transport: VoiceTransport? = null
    private var collectors: List<Job> = emptyList()
    private var startJob: Job? = null
    private var endingJob: Job? = null
    private var reapplyJob: Job? = null
    private var resumeJob: Job? = null
    private var linkJob: Job? = null
    private var focusToken: Any? = null
    private var micGain = AudioTuning.DEFAULT_MIC_GAIN
    private var duckGain = AudioTuning.DEFAULT_ECHO_DUCKING_GAIN
    private var audioOutput = OutputChoice.AUTO
    private val linkMachine = VoiceLinkMachine()

    // ── commands ───────────────────────────────────────────────────────────────────────────────

    override fun markConnecting() {
        _state.update { it.copy(phase = SessionPhase.CONNECTING, errorMessage = null) }
    }

    override fun startVoice() {
        if (!deps.orchestrator.isOrchestratorSession) {
            _state.update { it.copy(phase = SessionPhase.ERROR, errorMessage = "Voice only available for orchestrator sessions") }
            return
        }
        synchronized(lock) {
            if (startJob?.isActive == true || transport != null) {
                log.w(TAG, "startVoice ignored — a voice session is already starting or live")
                return
            }
            resumeJob?.cancel()
            resumeJob = null
            finalized = false
            owner = true
            resetLinkLocked()
        }
        publishLink(null)
        _state.update { it.copy(isOwner = true, remoteVoiceActive = false) }
        // Pause the wake word first; the start proceeds after the ack or WAKE_WORD_ACK_TIMEOUT_MS.
        val pauseAck = deps.wake.pauseWake()
        val job = scope.launch(start = CoroutineStart.LAZY) { runStart(pauseAck) }
        synchronized(lock) { startJob = job }
        job.start()
    }

    override fun reconnectVoice() {
        synchronized(lock) { resetLinkLocked() }
        publishLink(null)
        startVoice()
    }

    /**
     * User stop: ask the backend to end voice, show ENDING until `voice_ended` or the 5 s timeout (RS-16).
     *
     * With no live session (never started, or already finalized) this is a no-op: no `voice_stop`,
     * no ENDING (OI-4). The ENDING exit is the timeout's [finalize], which is idempotent per session,
     * so an ENDING entered after finalize would never leave. The old app only reached the stop from
     * an active voice button, so it never hit this path.
     */
    override fun stopVoice() {
        val (live, staleOwner) = synchronized(lock) {
            val local = owner || startJob?.isActive == true || transport != null
            (!finalized && local) to (finalized && owner)
        }
        if (!live) {
            // The backend re-reported this device as owner (`session_started`) after the local teardown:
            // tell it to stop, but there is nothing local to end, so no ENDING.
            if (staleOwner) deps.wire.sendVoiceStop() else log.d(TAG, "stopVoice ignored — no live voice session")
            // A bare markConnecting() (wake flip before startVoice) has nothing to stop: drop the flip.
            _state.update { if (it.phase == SessionPhase.CONNECTING) it.copy(phase = SessionPhase.OFF) else it }
            return
        }
        deps.wire.sendVoiceStop()
        synchronized(lock) { resetLinkLocked() }
        publishLink(null)
        _state.update { it.copy(phase = SessionPhase.ENDING, errorMessage = null) }
        armEndingTimeout("voice_ended ack timeout — forcing local stop")
    }

    override fun toggleMute() {
        val t = synchronized(lock) { transport }
        val muted = t?.toggleMute() ?: !_state.value.isMuted
        _state.update { it.copy(isMuted = muted) }
    }

    override fun setMicGain(level: Float) {
        val (gain, t) = synchronized(lock) {
            micGain = level.coerceIn(AudioTuning.MIC_GAIN_MIN, AudioTuning.MIC_GAIN_MAX)
            micGain to transport
        }
        t?.setMicGain(gain)
    }

    override fun setEchoDuckingGain(gain: Float) {
        val (g, t) = synchronized(lock) {
            duckGain = gain.coerceIn(AudioTuning.ECHO_DUCKING_GAIN_MIN, AudioTuning.ECHO_DUCKING_GAIN_MAX)
            duckGain to transport
        }
        t?.setEchoDuckingGain(g)
    }

    /** Stored for the next session; applied now when a session holds audio focus. */
    override fun setAudioOutput(output: OutputChoice) {
        synchronized(lock) { audioOutput = output }
        log.d(TAG, "Audio output set to: $output")
        applyRoute()
    }

    override fun release() {
        val (t, token) = synchronized(lock) {
            finalized = true
            owner = false
            listOfNotNull(startJob, endingJob, reapplyJob, resumeJob, linkJob).forEach { it.cancel() }
            collectors.forEach { it.cancel() }
            collectors = emptyList()
            (transport to focusToken).also { transport = null; levelSource.value = null }
        }
        relay.detach()
        scope.cancel()
        if (t != null) deps.scope.launch { safeDisconnect(t) }
        if (token != null) releaseAudio(token)
    }

    // ── backend frames ─────────────────────────────────────────────────────────────────────────

    override fun onInbound(frame: VoiceInbound) {
        when (frame) {
            is VoiceInbound.SessionStarted -> onSessionStarted(frame)
            is VoiceInbound.Command -> {
                // WebRTC control frames belong to the owner only (RS-12, V-5).
                if (!isOwner()) return log.d(TAG, "Ignoring voice_command — not the voice owner")
                relay.onBackendCommand(frame.command)
            }
            is VoiceInbound.OwnerActive -> onOwnerActive(frame.active)
            is VoiceInbound.ProviderEvent -> onProviderEvent(frame.event)
            is VoiceInbound.AudioOut -> synchronized(lock) { transport }?.pushSpeakerChunk(frame.audioB64)
            is VoiceInbound.Ending -> {
                // A non-owner flipping to "Ending…" was the multi-device wedge (RS-12).
                if (!isOwner()) return log.d(TAG, "Ignoring voice_ending — not the voice owner")
                if (_state.value.phase != SessionPhase.ENDING) {
                    _state.update { it.copy(phase = SessionPhase.ENDING) }
                    armEndingTimeout("voice_ended ack timeout after voice_ending")
                }
            }
            is VoiceInbound.Ended, VoiceInbound.Stopped -> {
                if (!isOwner()) {
                    // The server's voice is over, so a transport still held here is a zombie call that
                    // nothing else would ever close (2026-10-08: the call outlived end_voice_session).
                    if (holdsLocalCall()) {
                        log.w(TAG, "voice_ended while not the owner but holding a transport — tearing it down")
                        finalize()
                    }
                    return log.d(TAG, "Ignoring voice_ended/stopped — not the voice owner")
                }
                // TurnComplete never arrives in voice mode: finalize the streaming text, then tear down (RS-17).
                deps.transcripts.voiceEnded()
                // §6.11a SW-3: a switch is a quiet end — the call goes on in the resumed conversation,
                // whose start cancels this re-arm; the wake word is not cycled in between.
                val switching = (frame as? VoiceInbound.Ended)?.reason == END_REASON_SWITCH
                if (switching) log.i(TAG, "voice ended for a conversation switch — quiet end, wake re-arm deferred")
                finalize(wakeRearmDelayMs = if (switching) VoiceTuning.SWITCH_WAKE_REARM_DELAY_MS else VoiceTuning.MIC_RELEASE_DELAY_MS)
            }
            is VoiceInbound.VadState -> _state.update { it.copy(vadState = frame.state, vadDurationMs = frame.durationMs) }
        }
    }

    private fun onSessionStarted(frame: VoiceInbound.SessionStarted) {
        var restored: VoiceLinkEvent? = null
        var lost = false
        var held = false
        val (busy, hasTransport) = synchronized(lock) {
            if (frame.voice) {
                if (!frame.voiceInitiator && owner && holdsLocalCallLocked()) {
                    // Answer to a plain `start` while this device runs the call. With a `voice_start`
                    // in flight or still to be sent (a start in progress, or a reconnect re-arm racing
                    // the conversation's `start`) its answer decides; otherwise another socket owns
                    // voice now: end the local call (V-13). Merely flipping `owner` kept a zombie
                    // WebRTC call that dropped every voice_command and ignored voice_ended
                    // (2026-10-08, POCO X7 Pro).
                    if (ownerEchoesPending > 0 || transport == null) held = true else lost = true
                } else {
                    // We own voice iff the backend says we initiated it; otherwise we are a passive peer (RS-11).
                    owner = frame.voiceInitiator
                }
                if (frame.voiceInitiator && linkMachine.isReconnecting) restored = linkMachine.onVoiceConfirmed(deps.clock.nowMs())
            }
            (startJob?.isActive == true) to (transport != null)
        }
        if (held) return log.i(TAG, "session_started voice_initiator=false during a voice_start re-arm — keeping the call")
        if (lost) return ownershipLost("session_started voice_initiator=false while holding the call")
        if (restored != null) {
            log.i(TAG, "voice re-confirmed after the link loss — ${restored}")
            cancelLinkTicks()
            publishLink(restored)
        }
        if (frame.voice) _state.update { it.copy(isOwner = frame.voiceInitiator, remoteVoiceActive = !frame.voiceInitiator) }
        // Only the initiator forwards the provider config (RS-13, V-5).
        if (frame.voiceInitiator) frame.voiceSessionUpdate?.let { relay.onBackendCommand(it) }
        // Ghost voice (`f5b339a`, RS-14): the server says voice is off but we sit in a pre-start state.
        if (!frame.voice && !hasTransport && !busy) {
            _state.update {
                if (it.phase == SessionPhase.SUMMARIZING || it.phase == SessionPhase.CONNECTING) {
                    log.i(TAG, "session_started voice=false while UI was ${it.phase} — clearing pre-start state")
                    it.copy(phase = SessionPhase.OFF, errorMessage = null)
                } else {
                    it
                }
            }
        }
    }

    /** `voice_owner_active` (spec 12 §7.5 and V-13). */
    private fun onOwnerActive(active: Boolean) {
        val takenOver = synchronized(lock) {
            when {
                !owner -> false
                !active -> false // the end of voice: the owner's own voice_ended drives its state
                ownerEchoesPending > 0 -> { ownerEchoesPending--; false } // the echo of our voice_start
                else -> holdsLocalCallLocked()
            }
        }
        if (takenOver) return ownershipLost("voice_owner_active from another device")
        // Another device's voice: read-only "active elsewhere". The owner's own lifecycle drives its state.
        if (!isOwner()) _state.update { it.copy(remoteVoiceActive = active) }
    }

    /**
     * V-13: another device owns voice now. Tear the local call down **without** `voice_stop` (that
     * would end the other device's call) and show the read-only "active elsewhere" state.
     */
    private fun ownershipLost(why: String) {
        log.w(TAG, "voice ownership lost ($why) — ending the local call, no voice_stop")
        finalize()
        _state.update { it.copy(isOwner = false, remoteVoiceActive = true) }
    }

    /** Wire `voice_start` for this session; counts the `voice_owner_active` echo it will produce. */
    private fun sendVoiceStart(req: VoiceStartRequest) {
        synchronized(lock) { ownerEchoesPending++ }
        deps.wire.sendVoiceStart(req)
    }

    private fun holdsLocalCall() = synchronized(lock) { holdsLocalCallLocked() }

    private fun holdsLocalCallLocked() = !finalized && (transport != null || startJob?.isActive == true)

    private fun onProviderEvent(event: kotlinx.serialization.json.JsonObject) {
        val t = synchronized(lock) { transport }
        if (t != null) {
            t.handleProviderEvent(event)
            return
        }
        // Backend-synthesised statuses that land before the provider exists (the summary LLM runs first).
        if (typeOf(event) != "voice_status") return
        when (event.string("status")) {
            "summarizing" -> _state.update { it.copy(phase = SessionPhase.SUMMARIZING, errorMessage = null) }
            "preparing" -> _state.update { it.copy(phase = SessionPhase.CONNECTING, errorMessage = null) }
        }
    }

    // ── orchestrator link ──────────────────────────────────────────────────────────────────────

    override fun onConnection(signal: ConnectionSignal) {
        when (signal) {
            // A first connect / adoption never auto-starts voice (RS-11).
            ConnectionSignal.Connected -> log.d(TAG, "orchestrator connected")
            is ConnectionSignal.Reconnected -> {
                val cfg = synchronized(lock) {
                    activeConfig?.also { if (linkMachine.isReconnecting) linkMachine.onSocketBack(deps.clock.nowMs()) }
                }
                if (cfg != null) {
                    log.i(TAG, "WS reconnect during live voice — re-arming via voice_start")
                    sendVoiceStart(request(signal.localId, signal.sdkSessionId, cfg).copy(reattach = true))
                    publishLink(null)
                } else {
                    // Resume protocol: the start carries the persisted checkpoint.
                    deps.wire.sendResumeStart(signal.localId, signal.sdkSessionId)
                }
            }
            is ConnectionSignal.Disconnected -> if (signal.willReconnect) onLinkLost() else onTerminalDisconnect()
        }
    }

    /** Transient drop: keep everything (RS-10); P-2: tell the user from the first moment. */
    private fun onLinkLost() {
        val event = synchronized(lock) {
            if (!owner || activeConfig == null) return
            linkMachine.onLinkLost(deps.clock.nowMs())
        }
        if (event != null) log.w(TAG, "orchestrator link lost during live voice — reconnecting (budget ${VoiceTuning.LINK_RETRY_BUDGET_MS}ms)")
        publishLink(event)
        scheduleLinkTicks()
    }

    /**
     * Terminal drop (`9db8f37`, RS-10): without a clean `voice_ended`, finalize here or the wake
     * word never re-arms; forgetting the config stops a later Reconnected from restarting a dead session.
     */
    private fun onTerminalDisconnect() {
        val live = synchronized(lock) {
            (activeConfig != null || owner).also { if (it) activeConfig = null }
        }
        if (live) {
            log.i(TAG, "WS terminal disconnect mid-voice — finalizing voice stop (re-arms wake word)")
            finalize()
        }
    }

    private fun scheduleLinkTicks() {
        synchronized(lock) {
            linkJob?.cancel()
            linkJob = scope.launch {
                while (true) {
                    val d = synchronized(lock) { linkMachine.nextTickDelayMs(deps.clock.nowMs()) } ?: break
                    delay(d)
                    val event = synchronized(lock) { linkMachine.tick(deps.clock.nowMs()) }
                    publishLink(event)
                    if (event is VoiceLinkEvent.Failed) {
                        log.w(TAG, "voice link retry budget exhausted after ${event.downtimeMs}ms — ending voice locally")
                        finalize(SessionPhase.ERROR, LINK_FAILED_MESSAGE, keepLink = true)
                        break
                    }
                }
            }
        }
    }

    private fun cancelLinkTicks() = synchronized(lock) {
        linkJob?.cancel()
        linkJob = null
    }

    private fun resetLinkLocked() {
        linkMachine.reset()
        linkJob?.cancel()
        linkJob = null
    }

    private fun publishLink(event: VoiceLinkEvent?) {
        _link.value = synchronized(lock) { linkMachine.state }
        event?.let { _linkEvents.tryEmit(it) }
    }

    // ── start ──────────────────────────────────────────────────────────────────────────────────

    private suspend fun runStart(pauseAck: Deferred<Unit>) {
        withTimeoutOrNull(VoiceTuning.WAKE_WORD_ACK_TIMEOUT_MS) { pauseAck.await() }
            ?: log.w(TAG, "pauseWakeWord ack timeout — proceeding without confirmed release")
        val cfg = deps.api.getVoiceConfig()
        if (cfg == null) {
            // B10: the wake word was paused; never strand it.
            onVoiceError("Could not load voice config")
            return
        }
        log.i(TAG, "start: voice config provider=${cfg.provider} model=${cfg.model} voice=${cfg.voice} lang=${cfg.transcriptionLanguage}")
        synchronized(lock) { activeConfig = cfg }
        val ctx = deps.orchestrator
        sendVoiceStart(request(ctx.localId, ctx.jsonlSessionId ?: ctx.currentSessionId, cfg))

        val info = deps.api.startVoiceSession(cfg)
        if (info == null) {
            val msg = "Failed to start voice session (no connection info)"
            log.e(TAG, msg)
            _state.update { it.copy(phase = SessionPhase.ERROR, errorMessage = msg) }
            onVoiceError(msg)
            return
        }
        val t = deps.transports.create(cfg.provider, info.connectionType)
        log.i(TAG, "start: using provider=${t.providerId} (${t.kind})")
        val (gain, duck) = synchronized(lock) { micGain to duckGain }
        t.setMicGain(gain)
        t.setEchoDuckingGain(duck)
        // The DC-open / echo self-heal re-asserts the cached session.update (RS-03).
        t.setSessionUpdateFallback { relay.cachedSessionUpdate }
        val adopted = synchronized(lock) {
            if (finalized) false else { transport = t; levelSource.value = t; true }
        }
        if (!adopted) return log.i(TAG, "start: session ended while starting — dropping the transport")
        observe(t)
        // Commands that raced the HTTP round trip (the session.update) go to the transport BEFORE connect (RS-01).
        relay.attach(ProviderCommandSink { t.handleBackendCommand(it) })
        acquireAudio()
        try {
            connect(t, info)
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            log.e(TAG, "transport connect failed: ${e.message}", e)
            onVoiceError(e.message ?: "Voice connection failed")
        }
    }

    private suspend fun connect(t: VoiceTransport, info: VoiceConnection) {
        t.connect(
            info,
            mirrorEvent = { deps.wire.sendVoiceEvent(it) },
            sendMicChunk = { deps.wire.sendVoiceAudioIn(it) },
        )
    }

    private fun request(localId: String, resumeSdkId: String?, cfg: VoiceStartConfig) = VoiceStartRequest(
        localId = localId,
        resumeSdkId = resumeSdkId,
        provider = cfg.provider,
        model = cfg.model,
        voiceName = cfg.voice,
        transcriptionLanguage = cfg.transcriptionLanguage,
        endpoint = cfg.endpoint.takeIf { it.isNotBlank() },
    )

    private fun observe(t: VoiceTransport) {
        val jobs = listOf(
            scope.launch(Dispatchers.Unconfined, CoroutineStart.UNDISPATCHED) { t.phase.collect { onProviderPhase(t, it) } },
            scope.launch(Dispatchers.Unconfined, CoroutineStart.UNDISPATCHED) { t.signals.collect { onSignal(t, it) } },
        )
        synchronized(lock) {
            if (transport === t) collectors = jobs else jobs.forEach { it.cancel() }
        }
    }

    private fun isCurrent(t: VoiceTransport) = synchronized(lock) { transport === t }

    private fun isOwner() = synchronized(lock) { owner }

    private fun onProviderPhase(t: VoiceTransport, ps: ProviderPhaseState) {
        if (!isCurrent(t)) return
        _state.update { cur ->
            // The provider's initial OFF must not clobber a backend-pushed SUMMARIZING / CONNECTING.
            if (ps.phase == ProviderPhase.OFF && (cur.phase == SessionPhase.SUMMARIZING || cur.phase == SessionPhase.CONNECTING)) {
                cur
            } else {
                cur.copy(
                    phase = ps.phase.toSessionPhase(),
                    errorMessage = if (ps.phase == ProviderPhase.ERROR) ps.message else null,
                    // Back to ACTIVE (e.g. voice_status: ready after an upstream cycle) clears the banner.
                    reconnectBanner = if (ps.phase == ProviderPhase.ACTIVE) null else cur.reconnectBanner,
                )
            }
        }
    }

    private fun onSignal(t: VoiceTransport, signal: ProviderSignal) {
        if (!isCurrent(t)) return
        when (signal) {
            is ProviderSignal.UserTranscript -> deps.transcripts.userTranscript(signal.text, true)
            is ProviderSignal.TextComplete -> if (signal.text.isNotEmpty()) deps.transcripts.assistantTranscript(signal.text, true)
            is ProviderSignal.TextDelta -> Unit // waiting for TextComplete
            ProviderSignal.TurnComplete -> deps.transcripts.turnComplete()
            is ProviderSignal.ToolUse -> log.d(TAG, "Voice tool use: ${signal.name}")
            // A voice error is terminal: the provider won't recover a rejected session, and a
            // half-open session wedges the paused wake word (`c0cad2c`, RS-09).
            is ProviderSignal.Error -> onVoiceError(signal.message)
            is ProviderSignal.RoutingFallback -> toast(signal.message)
            is ProviderSignal.ReconnectWarning -> {
                val secs = signal.timeLeftSeconds
                _state.update { it.copy(reconnectBanner = if (secs != null) "Pausing in ~${secs}s to reconnect…" else "Reconnecting shortly…") }
                deps.cues.reconnect() // STREAM_MUSIC beep (`b586e4b`, RS-19)
            }
            ProviderSignal.Reconnecting -> _state.update { it.copy(reconnectBanner = "Pausing for a second to reconnect…") }
            ProviderSignal.SessionEnded -> _state.update { it.copy(phase = SessionPhase.OFF, errorMessage = null, isMuted = false, reconnectBanner = null) }
            ProviderSignal.SessionCreated -> log.d(TAG, "Voice session created")
            ProviderSignal.SpeechStarted -> log.d(TAG, "User speech started")
            ProviderSignal.SpeechStopped -> log.d(TAG, "User speech stopped")
            is ProviderSignal.Phase, ProviderSignal.FlushSpeaker, ProviderSignal.Teardown -> Unit // transport-internal
        }
    }

    private fun onVoiceError(message: String) {
        log.e(TAG, "Voice error: $message")
        deps.transcripts.system("Voice error: $message")
        toast("Voice error: $message") // before finalize: the OFF state hides the error banner
        finalize()
    }

    private fun toast(message: String) {
        _events.tryEmit(VoiceSessionEvent.Toast(message))
    }

    // ── ending / finalize ──────────────────────────────────────────────────────────────────────

    private fun armEndingTimeout(reason: String) {
        synchronized(lock) {
            endingJob?.cancel()
            endingJob = scope.launch {
                delay(VoiceTuning.ENDING_ACK_TIMEOUT_MS)
                log.w(TAG, reason)
                finalize()
            }
        }
    }

    /**
     * Local teardown; idempotent per session (`voiceStopFinalized`). Tears the transport down,
     * releases audio, and re-arms the wake word [wakeRearmDelayMs] later (RS-30; default
     * [VoiceTuning.MIC_RELEASE_DELAY_MS], longer for a conversation switch, §6.11a SW-3).
     */
    private fun finalize(
        endPhase: SessionPhase = SessionPhase.OFF,
        message: String? = null,
        keepLink: Boolean = false,
        wakeRearmDelayMs: Long = VoiceTuning.MIC_RELEASE_DELAY_MS,
    ) {
        val t: VoiceTransport?
        val token: Any?
        synchronized(lock) {
            if (finalized) {
                log.d(TAG, "finalizeVoiceStop ignored — already finalized for this session")
                return
            }
            finalized = true
            owner = false
            activeConfig = null
            listOfNotNull(endingJob, reapplyJob, startJob).forEach { it.cancel() }
            endingJob = null
            reapplyJob = null
            startJob = null
            collectors.forEach { it.cancel() }
            collectors = emptyList()
            t = transport
            transport = null
            levelSource.value = null
            token = focusToken
            if (!keepLink) resetLinkLocked()
        }
        // Commands queued for this session belong to it; the relay stays usable (and keeps its cache).
        relay.detach()
        if (!keepLink) publishLink(null)
        _state.update {
            it.copy(
                phase = endPhase,
                errorMessage = message,
                isOwner = false,
                remoteVoiceActive = false,
                isMuted = false,
                vadState = "idle",
                vadDurationMs = 0L,
                reconnectBanner = null,
            )
        }
        scope.launch {
            if (t != null) safeDisconnect(t)
            if (token != null) releaseAudio(token)
        }
        val resume = scope.launch {
            // WebRTC holds the AudioRecord after stop(): re-arming earlier fails 20+ times (`0bc612f`).
            delay(wakeRearmDelayMs)
            val ack = deps.wake.resumeWake()
            withTimeoutOrNull(VoiceTuning.WAKE_WORD_ACK_TIMEOUT_MS) { ack.await() }
                ?: log.w(TAG, "resumeWakeWord ack timeout — service may be slow or short-circuited")
        }
        synchronized(lock) { resumeJob = resume }
    }

    private suspend fun safeDisconnect(t: VoiceTransport) {
        try {
            t.disconnect()
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            log.w(TAG, "transport disconnect failed: ${e.message}")
        }
    }

    // ── audio focus + routing ──────────────────────────────────────────────────────────────────

    private fun acquireAudio() {
        synchronized(lock) { focusToken = Any() }
        deps.audio.acquireFocus()
        applyRoute()
        // The WebRTC ADM / Samsung HAL re-pin the earpiece after init: re-apply at +1/+4/+9 s (`d36d31b`, RS-27).
        val job = scope.launch {
            for (d in VoiceTuning.ROUTE_REAPPLY_DELAYS_MS) {
                delay(d)
                log.d(TAG, "[ROUTE] post-connect re-apply at +${d}ms")
                applyRoute()
            }
        }
        synchronized(lock) {
            if (focusToken != null && !finalized) reapplyJob = job else job.cancel()
        }
    }

    /** Picks + applies the route for the current output and provider; forwards the speaker plane. */
    private fun applyRoute() {
        val (t, output) = synchronized(lock) {
            if (focusToken == null) return
            transport to audioOutput
        }
        val applied = deps.audio.applyRoute(t?.kind ?: ProviderKind.WEBRTC, output)
        t?.setSpeakerMode(applied.speakerMode, applied.preferredDevice)
        val route = applied.route
        if (route is Route.BluetoothUnsupported) {
            val msg = when (route.reason) {
                FallbackReason.BT_NOT_AVAILABLE -> "Bluetooth not available — using loudspeaker"
                FallbackReason.BT_A2DP_REQUIRES_WS_PROVIDER ->
                    "OpenAI Realtime can't route to Bluetooth speakers (no mic on this device). " +
                        "Switch to Qwen or Gemini, or use a BT headset. Using loudspeaker for now."
            }
            log.w(TAG, "Routing fallback: $msg")
            toast(msg)
        }
    }

    private fun releaseAudio(token: Any) {
        val mine = synchronized(lock) { (focusToken === token).also { if (it) focusToken = null } }
        if (mine) deps.audio.release()
    }

    private companion object {
        const val TAG = "VoiceController"
        const val LINK_FAILED_MESSAGE = "Voice connection lost"

        /** `voice_ended.reason` of an agent-initiated conversation switch (spec 12 §6.11a, §7.6). */
        const val END_REASON_SWITCH = "switch"

        fun ProviderPhase.toSessionPhase(): SessionPhase = when (this) {
            ProviderPhase.OFF -> SessionPhase.OFF
            ProviderPhase.CONNECTING -> SessionPhase.CONNECTING
            ProviderPhase.SUMMARIZING -> SessionPhase.SUMMARIZING
            ProviderPhase.ACTIVE -> SessionPhase.ACTIVE
            ProviderPhase.SPEAKING -> SessionPhase.SPEAKING
            ProviderPhase.THINKING -> SessionPhase.THINKING
            ProviderPhase.TOOL_USE -> SessionPhase.TOOL_USE
            ProviderPhase.ERROR -> SessionPhase.ERROR
        }
    }
}
