package com.assistant.archie.graph

import android.app.Application
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner
import com.assistant.core.data.AgentSocketPool
import com.assistant.core.data.ConversationKey
import com.assistant.core.data.ConnectionRepository
import com.assistant.core.data.ContentChangesRepository
import com.assistant.core.data.ConversationRepository
import com.assistant.core.data.HistoryRepository
import com.assistant.core.data.LanScanner
import com.assistant.core.data.MemoryRepository
import com.assistant.core.data.OpenSessionsRepository
import com.assistant.core.data.ServerConfigRepository
import com.assistant.core.data.ServerScanner
import com.assistant.core.data.ShareRepository
import com.assistant.core.data.UploadRepository
import com.assistant.core.data.VisualsRepository
import com.assistant.core.data.VoicePresence
import com.assistant.core.model.DeviceSettings
import com.assistant.core.network.ArchieApi
import com.assistant.core.network.HttpStack
import com.assistant.core.network.NetLog
import com.assistant.core.network.NetworkMonitor
import com.assistant.core.network.RestCaller
import com.assistant.core.network.SocketState
import com.assistant.core.network.ServerDiscovery
import com.assistant.core.network.SocketClient
import com.assistant.core.network.TrustStore
import com.assistant.core.network.UploadClient
import com.assistant.core.protocol.ServerFrame
import com.assistant.core.session.ArchiePoolApi
import com.assistant.core.session.OrchestratorChannel
import com.assistant.core.session.SettingsOrchestratorIdStore
import com.assistant.core.session.SettingsPinStore
import com.assistant.core.settings.SettingsStore
import com.assistant.core.voicehost.runtime.VoiceHostRuntime
import com.assistant.archie.feature.chat.ChatVoice
import com.assistant.archie.feature.chat.PresenceChatVoice
import com.assistant.archie.feature.chat.VoiceDockModel
import com.assistant.archie.feature.settings.VoiceStatusSource
import com.assistant.archie.system.AgentWork
import com.assistant.archie.system.ApprovalCenter
import com.assistant.archie.system.ApprovalNotifier
import com.assistant.archie.system.HostChatVoice
import com.assistant.archie.system.HostVoicePresence
import com.assistant.archie.system.HostVoiceStatus
import com.assistant.archie.system.MicPermissionGate
import com.assistant.archie.system.ShareController
import com.assistant.archie.system.ShellCommands
import com.assistant.archie.system.SystemApprovalSink
import com.assistant.archie.system.SystemTurnSink
import com.assistant.archie.system.TurnNotifier
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.dropWhile
import kotlinx.coroutines.flow.filterNotNull
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.launchIn
import kotlinx.coroutines.flow.onEach
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import java.io.File

/** Reached as `(application as GraphOwner).graph` (spec 14 §2.2). */
interface GraphOwner {
    val graph: MainAppGraph
}

/**
 * The main app's hand-written dependency graph (spec 14 §2.2: manual DI, no Hilt). Everything here
 * is **process-scoped**: closing the Activity does not end chat or voice state (inv03 §0).
 *
 * Owners: B-03 (this shape), B-09 (voice host, notifications, share sheet wiring; the graph is handed
 * over after wave 3: see the "system integration" block). Parameters with defaults exist so tests can build the graph against a local
 * server and a fake scanner.
 */
class MainAppGraph(
    private val app: Application,
    val settings: SettingsStore = SettingsStore.create(app),
    scanner: ServerScanner? = null,
    val scope: CoroutineScope = CoroutineScope(SupervisorJob() + Dispatchers.Default),
    log: NetLog = AndroidNetLog,
    /**
     * B-09: builds the process-scoped voice host ([com.assistant.archie.system.MainVoiceHost] in
     * production). `null` (JVM tests) leaves voice idle: no runtime, no wake word, no service.
     */
    voiceHostFactory: ((Application, MainAppGraph) -> VoiceHostRuntime)? = null,
) {
    val application: Application get() = app

    private val serverUrl: () -> String = { settings.settings.value?.serverUrl ?: DeviceSettings.DEFAULT_SERVER_URL }

    val http = HttpStack(TrustStore(SettingsPinStore(settings, scope)), File(app.cacheDir, "http"))
    private val rest = RestCaller(http, serverUrl)
    val api = ArchieApi(rest)

    /** The app's single orchestrator socket (T-6); the conversation repository sends `start` (no autoStart). */
    val orchestrator = OrchestratorChannel(
        SocketClient(http, scope, log = log, tag = "orch"),
        ArchiePoolApi(api),
        SettingsOrchestratorIdStore(settings),
        scope,
    )

    val network: NetworkMonitor by lazy { NetworkMonitor(app) }
    val history = HistoryRepository(api, scope)
    val agentSockets = AgentSocketPool({ SocketClient(http, scope, log = log, tag = "agent") })
    val conversations = ConversationRepository(
        api, orchestrator, agentSockets, history, scope, serverUrl = { settings.settings.value?.serverUrl },
    )
    val openSessions = OpenSessionsRepository(conversations, history, orchestrator, scope)
    val connection = ConnectionRepository(
        settings, orchestrator, conversations, openSessions, history,
        scanner ?: LanScanner(ServerDiscovery(http), network),
        scope,
    )
    /**
     * OI-2 + OI-6: agent approvals from anywhere — heads-up notifications for pending agent
     * permissions the user is not looking at, answered over REST (spec 12 §6.9, AN-1..AN-4).
     */
    val approvals = ApprovalCenter(
        ApprovalNotifier(SystemApprovalSink(app), scope, ApprovalNotifier.titleFrom(history, conversations)) { localId, requestId, allow ->
            conversations.answerAgentApproval(localId, requestId, allow, preferSocket = false)
        },
        settingsLoaded = { withTimeoutOrNull(5_000) { settings.settings.filterNotNull().first() } },
    )

    /**
     * "Agent session finished" notifications (spec 12 §3.7, Settings → Notifications) from the
     * watcher frames, and [agentWork], the in-flight turns behind their background hold (the voice
     * host's FGS keeps the socket while a watched turn runs; TurnNotifier.kt has the limits).
     */
    val agentWork = AgentWork()
    val turns = TurnNotifier(SystemTurnSink(app), { localId, sdkId ->
        val sdk = sdkId ?: conversations.current(ConversationKey.agent(localId))?.ref?.sdkId
        history.titleFor(sdk, localId, "").takeIf { it.isNotEmpty() }
    })
    private val lookingAtAgent = ApprovalNotifier.lookingAtFrom(openSessions, conversations, approvals.foreground, approvals.workspaceOnTop)
        .stateIn(scope, SharingStarted.Eagerly, null)

    val memory = MemoryRepository(api, scope)
    val visuals = VisualsRepository(api, scope)

    /** Spec 12 §9.3: `visualization_changed` / `memory_changed` → per-file counters the viewers reload on. */
    val content = ContentChangesRepository(
        orchestrator.frames, orchestrator.state.map { it.socket }.distinctUntilChanged(), visuals, memory, scope,
    )
    val serverConfig = ServerConfigRepository(api, scope)
    val uploads = UploadRepository(UploadClient(rest))
    val share = ShareRepository()

    // ── B-09: system integration (spec 14 §2.5-§2.9) ──────────────────────────────────────────

    /**
     * The process-scoped voice host (A-08), built on first access: the UI's ON_START, the service
     * after a sticky restart, the tile, the trampoline or the assist session (spec 14 §2.5).
     */
    val voiceHost: VoiceHostRuntime? by lazy { voiceHostFactory?.invoke(app, this) }

    /** RECORD_AUDIO asked in context (spec 14 §2.9). */
    val mic = MicPermissionGate(app)

    /** Launcher-shortcut commands for the shell. */
    val commands = ShellCommands()

    /** Shell chrome voice state. */
    val voice: VoicePresence by lazy { voiceHost?.let { HostVoicePresence(it, scope) } ?: VoicePresence.Idle }

    /** The Archie conversation's voice dock / composer (replaces B-04's state-only stand-in). */
    val chatVoice: ChatVoice by lazy { voiceHost?.let { HostChatVoice(it, mic, scope) } ?: PresenceChatVoice() }

    /**
     * The voice controls' one state + reconnect timeline, shared by the Archie conversation's dock
     * and the floating controls (ending from one clears the outcome on both; survives rotation).
     */
    val voiceDock: VoiceDockModel by lazy { VoiceDockModel(chatVoice, scope, System::currentTimeMillis).also { it.start() } }

    /** Settings → Wake word health line. */
    val voiceStatus: VoiceStatusSource by lazy {
        voiceHost?.let { h -> HostVoiceStatus(h, scope) { settings.settings.value?.wakeWord } } ?: VoiceStatusSource.None
    }

    /** The share target (text + streamed file uploads, spec 14 §2.8). */
    val shareFlow: ShareController by lazy { ShareController(app, share, conversations, uploads, openSessions, scope) }

    init {
        // §9.1: visuals refresh after any turn (debounced).
        conversations.events.onEach { if (it is com.assistant.core.data.ConversationEvent.TurnEnded) visuals.refreshSoon() }.launchIn(scope)
        approvals.notifier.attach(
            ApprovalNotifier.pendingFrom(conversations),
            ApprovalNotifier.lookingAtFrom(openSessions, conversations, approvals.foreground, approvals.workspaceOnTop),
        )
        // Agent turns: track what is in flight; a finished one may become a notification.
        orchestrator.frames.onEach { f ->
            agentWork.onFrame(f)
            if (f is ServerFrame.AgentTurnFinished) {
                turns.onFinished(f, settings.settings.value?.notifyAgentTurns == true, lookingAtAgent.value)
            }
        }.launchIn(scope)
        history.pool.onEach { agentWork.reconcile(it) }.launchIn(scope)
        lookingAtAgent.onEach(turns::onLooking).launchIn(scope)
        // Re-read the pool on every orchestrator (re)connect: spec 12 OPEN-4 (open views and "Open now"
        // follow the server; the read feeds the conversation repository's reconcile) and hold hygiene
        // (finishes missed while the socket was down must not keep the service up). Every few minutes
        // while turns are in flight too (an unreachable pool still ages them out).
        orchestrator.state.map { it.socket }.distinctUntilChanged()
            .onEach { if (it == SocketState.Open) resyncAgentWork() }
            .launchIn(scope)
        scope.launch {
            agentWork.busy.map { it > 0 }.distinctUntilChanged().collectLatest { busy ->
                while (busy) {
                    delay(AgentWork.RESYNC_MS)
                    resyncAgentWork()
                }
            }
        }
        // The background hold: switch on and a turn in flight. Nothing happens before the first hold
        // (the voice host is built lazily; building it here would start it at process start).
        combine(agentWork.busy, settings.settings.map { it?.notifyAgentTurns == true }) { busy, on -> if (on) busy else 0 }
            .distinctUntilChanged()
            .dropWhile { it == 0 }
            .onEach { n ->
                android.util.Log.i(TurnNotifier.TAG, "background hold ${if (n > 0) "on" else "off"} (turns in flight: $n)")
                voiceHost?.setAgentWorkHold(n)
            }
            .launchIn(scope)
    }

    private suspend fun resyncAgentWork() {
        val requestedAt = System.currentTimeMillis()
        val rows = history.syncPool()
        if (rows != null) agentWork.reconcile(rows, requestedAt) else agentWork.expire()
        android.util.Log.i(TurnNotifier.TAG, "pool re-sync: ${if (rows == null) "unreachable" else "ok"} (turns in flight: ${agentWork.busy.value})")
    }

    /** T-14: reconnect immediately when a network becomes available. Called once by the Application. */
    fun attachNetwork() {
        orchestrator.attachNetwork(network.available)
    }

    /**
     * Process foreground/background (`ProcessLifecycleOwner`, spec 14 §2.5). ON_STOP never sends
     * anything and never closes a session (decision P-1); it only stops retrying drops.
     */
    val processLifecycle = object : DefaultLifecycleObserver {
        override fun onStart(owner: LifecycleOwner) {
            conversations.onForeground()
            history.refreshAll()
            // A foreground context: the host may start / promote its FGS now (spec 14 §2.6).
            voiceHost?.onUiStarted()
        }

        override fun onStop(owner: LifecycleOwner) {
            // The orchestrator socket stays while the voice host's service needs it (wake word,
            // live voice, "Stay connected"): a headless wake must find it open (spec 14 §2.5).
            val keepAlive = settings.settings.value?.stayConnectedInBackground == true || voiceHost?.serviceWanted() == true
            conversations.onBackground(keepOrchestratorAlive = keepAlive)
            voiceHost?.onUiStopped()
        }
    }
}

/** Socket logs to logcat (audio frames are already suppressed by SocketClient). */
private object AndroidNetLog : NetLog {
    override fun log(level: Char, tag: String, message: String) {
        when (level) {
            'W', 'E' -> android.util.Log.w("Archie/$tag", message)
            else -> android.util.Log.d("Archie/$tag", message)
        }
    }
}
