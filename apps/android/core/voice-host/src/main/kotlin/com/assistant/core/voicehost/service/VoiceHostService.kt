package com.assistant.core.voicehost.service

import android.Manifest
import android.app.Activity
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.os.Binder
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import com.assistant.core.audio.platform.LogcatVoiceLog
import com.assistant.core.voice.ports.SessionPhase
import com.assistant.core.voicehost.HostTuning
import com.assistant.core.voicehost.VoiceHost
import com.assistant.core.voicehost.fgs.FgsPolicy
import com.assistant.core.voicehost.fgs.FgsTypes
import com.assistant.core.voicehost.fgs.StartOrigin
import com.assistant.core.voicehost.notify.NotificationAction
import com.assistant.core.voicehost.notify.NotificationPolicy
import com.assistant.core.voicehost.ports.Trigger
import com.assistant.core.voicehost.runtime.VoiceHostRuntime
import com.assistant.core.voicehost.trigger.DevInputRecentsMonitor
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.launch

/** The Application of an app that hosts voice (`ArchieApplication`, `LiteApplication`). */
interface VoiceHostOwner {
    /** The process-scoped runtime (lazy in the app graph). */
    val voiceHostRuntime: VoiceHostRuntime
}

/**
 * The foreground service (spec 14 §2.5): it owns nothing but the notification, the FGS type and a
 * `PARTIAL_WAKE_LOCK` during live voice; it keeps the process — and so the [VoiceHostRuntime] —
 * alive. It is also the "long-running service in package" the A300M companion watchdog looks for
 * (inv04 §6.4 contract 3), with the old notification id 1001 and channel `assistant_service_channel`.
 *
 * Start it only from a foreground context ([start] with `fromForeground = true`) or, on the lite
 * app (API 21/22), from `Application.onCreate`. A START_STICKY restart degrades to SPECIAL_USE on
 * Android 14+ and pauses the wake mic until "Resume listening" (trampoline) promotes it ([FgsPolicy]).
 */
class VoiceHostService : Service() {
    private val log = LogcatVoiceLog
    private var runtime: VoiceHostRuntime? = null
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private lateinit var notifications: HostNotifications
    private var foreground = false
    private var currentMask = FgsTypes.NONE
    private var voiceWakeLock: PowerManager.WakeLock? = null
    private var recents: DevInputRecentsMonitor? = null

    inner class LocalBinder : Binder() {
        val host: VoiceHost? get() = runtime
    }

    private val binder = LocalBinder()

    private val screenReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            when (intent?.action) {
                Intent.ACTION_SCREEN_ON, Intent.ACTION_USER_PRESENT -> {
                    log.d(TAG, "Screen on / unlocked (${intent.action}) — re-arming wake word")
                    runtime?.onScreenOnOrUserPresent()
                }
            }
        }
    }

    override fun onCreate() {
        super.onCreate()
        val rt = (application as? VoiceHostOwner)?.voiceHostRuntime
        if (rt == null) {
            log.e(TAG, "Application does not implement VoiceHostOwner — the voice host cannot run", null)
            return
        }
        runtime = rt
        notifications = HostNotifications(this, rt.config).also { it.ensureChannels() }
        val screen = IntentFilter().apply {
            addAction(Intent.ACTION_SCREEN_ON)
            addAction(Intent.ACTION_USER_PRESENT)
        }
        if (Build.VERSION.SDK_INT >= 33) {
            // Protected system broadcasts still reach a NOT_EXPORTED receiver; the flag is mandatory at targetSdk 34+.
            registerReceiver(screenReceiver, screen, Context.RECEIVER_NOT_EXPORTED)
        } else {
            // As the old service. (ContextCompat's pre-33 NOT_EXPORTED emulation adds a signature broadcast
            // permission to the registration, which the A300M's SCREEN_ON sender must not depend on.)
            @Suppress("UnspecifiedRegisterReceiverFlag")
            registerReceiver(screenReceiver, screen)
        }
        if (rt.config.rawRecentsMonitor) {
            recents = DevInputRecentsMonitor(log) { rt.ingress.trigger(Trigger.RECENTS) }.also { it.start() }
        }
        scope.launch {
            rt.state.map { NotificationPolicy.model(it, rt.config.notification, rt.wakeService.currentConfig.wakeWord) }
                .distinctUntilChanged()
                .collect { model -> if (foreground) notifications.notify(notifications.build(model), HostTuning.NOTIFICATION_ID) }
        }
        scope.launch {
            rt.state.map { it.session.isOwner && it.session.phase != SessionPhase.OFF && it.session.phase != SessionPhase.ERROR }
                .distinctUntilChanged()
                .collect { live ->
                    holdVoiceWakeLock(live)
                    if (!live && !rt.serviceWanted()) {
                        log.d(TAG, "Nothing needs the service any more — stopping")
                        stopGuarded(this@VoiceHostService)
                    }
                }
        }
        log.d(TAG, "Service created")
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val rt = runtime ?: run {
            Track.delivered(intent, foreground = false, mask = FgsTypes.NONE)
            stopSelf()
            return START_NOT_STICKY
        }
        val origin = when {
            intent == null -> StartOrigin.STICKY_RESTART
            intent.getBooleanExtra(EXTRA_FROM_FOREGROUND, false) -> StartOrigin.FOREGROUND
            else -> StartOrigin.BACKGROUND
        }
        val granted = ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
        // Hold-only (agent notifications) never opens the mic: no microphone type then.
        val decision = FgsPolicy.decide(Build.VERSION.SDK_INT, origin, granted && rt.micTypeWanted(), foreground, currentMask)
        if (decision.callStartForeground) {
            val model = NotificationPolicy.model(rt.state.value, rt.config.notification, rt.wakeService.currentConfig.wakeWord)
            try {
                ServiceCompat.startForeground(this, HostTuning.NOTIFICATION_ID, notifications.build(model), decision.typeMask)
                foreground = true
                currentMask = decision.typeMask
            } catch (e: Exception) {
                // ForegroundServiceStartNotAllowedException / SecurityException (type not allowed now).
                log.w(TAG, "startForeground(${decision.typeMask}) refused: ${e.javaClass.simpleName}: ${e.message}")
                val fallback = FgsPolicy.maskFor(Build.VERSION.SDK_INT, micEligible = false)
                try {
                    ServiceCompat.startForeground(this, HostTuning.NOTIFICATION_ID, notifications.build(model), fallback)
                    foreground = true
                    currentMask = fallback
                } catch (e2: Exception) {
                    log.e(TAG, "startForeground fallback refused — background restricted", e2)
                    rt.onServiceStartRefused()
                    Track.delivered(intent, foreground = false, mask = FgsTypes.NONE)
                    stopSelf()
                    return START_NOT_STICKY
                }
            }
        }
        rt.onServiceStarted(decision.copy(micAllowed = micAllowedFor(granted)))
        log.d(TAG, "Service started (origin=$origin, fgsTypes=0x${currentMask.toString(16)}, mic=${micAllowedFor(granted)})")
        // A startForegroundService() is now satisfied (startForeground ran, now or earlier); a stop
        // that arrived while it was in flight runs only now (B-09: stopping before delivery crashes).
        if (Track.delivered(intent, foreground, currentMask)) {
            log.d(TAG, "Deferred stop after the pending foreground start")
            stopSelf()
            return START_NOT_STICKY
        }

        when (intent?.action?.removePrefix(ACTION_PREFIX)) {
            null -> if (intent == null) rt.onStickyRestart()
            NotificationAction.MUTE.name, NotificationAction.UNMUTE.name -> rt.toggleMute()
            NotificationAction.END.name -> rt.stopVoice()
            NotificationAction.PAUSE_LISTENING.name -> rt.pauseListening()
            NotificationAction.RESUME_LISTENING.name -> rt.resumeListening()
            NotificationAction.TALK.name -> rt.ingress.trigger(Trigger.NOTIFICATION)
            NotificationAction.RECONNECT.name -> rt.reconnectVoice()
        }
        return START_STICKY
    }

    private fun micAllowedFor(granted: Boolean): Boolean =
        if (Build.VERSION.SDK_INT < 30) granted else currentMask and FgsTypes.MICROPHONE != 0

    override fun onBind(intent: Intent?): IBinder = binder

    override fun onDestroy() {
        if (runtime != null) {
            try { unregisterReceiver(screenReceiver) } catch (_: Exception) {}
        }
        recents?.stop()
        holdVoiceWakeLock(false)
        scope.cancel()
        foreground = false
        Track.destroyed()
        // The runtime is process-scoped: voice and the socket survive the service (spec 14 §2.5).
        log.d(TAG, "Service destroyed")
        super.onDestroy()
    }

    private fun holdVoiceWakeLock(hold: Boolean) {
        synchronized(this) {
            if (hold && voiceWakeLock == null) {
                val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
                voiceWakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "assistant:voice").apply {
                    setReferenceCounted(false)
                    acquire(VOICE_WAKE_LOCK_MAX_MS)
                }
            } else if (!hold) {
                voiceWakeLock?.let { if (it.isHeld) it.release() }
                voiceWakeLock = null
            }
        }
    }

    companion object {
        private const val TAG = "AssistantService"
        const val EXTRA_FROM_FOREGROUND = "voicehost_from_foreground"

        /** Trampoline extra: a [NotificationAction] name (TALK / RESUME_LISTENING / RECONNECT). */
        const val EXTRA_HOST_ACTION = "voicehost_action"
        const val ACTION_PREFIX = "com.assistant.core.voicehost.action."

        /** Safety cap for the voice wake lock (re-acquired per session). */
        private const val VOICE_WAKE_LOCK_MAX_MS = 4 * 60 * 60 * 1000L

        /**
         * Start (or promote) the service. Returns false when the platform refused a background FGS
         * start (Android 12+ `ForegroundServiceStartNotAllowedException`).
         */
        fun start(context: Context, fromForeground: Boolean): Boolean {
            val granted = ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
            val wanted = FgsPolicy.maskFor(Build.VERSION.SDK_INT, micEligible = granted && (Build.VERSION.SDK_INT < 30 || fromForeground))
            if (!Track.claimStart(wanted)) return true // already foreground with these types, or a start is in flight
            return try {
                val i = Intent(context, VoiceHostService::class.java)
                    .putExtra(EXTRA_FROM_FOREGROUND, fromForeground)
                    .putExtra(EXTRA_FGS_START, true)
                ContextCompat.startForegroundService(context, i)
                true
            } catch (e: IllegalStateException) {
                // ForegroundServiceStartNotAllowedException (12+) extends IllegalStateException.
                Track.startFailed()
                LogcatVoiceLog.w(TAG, "startForegroundService refused (${e.javaClass.simpleName}): ${e.message} — staying in the current mode")
                false
            } catch (e: SecurityException) {
                Track.startFailed()
                LogcatVoiceLog.w(TAG, "startForegroundService refused (SecurityException): ${e.message} — staying in the current mode")
                false
            }
        }

        /**
         * Stop the service. If a `startForegroundService()` is still in flight (not yet delivered to
         * [onStartCommand]), stopping now would crash the app ("did not then call startForeground");
         * the stop is deferred until that start has been delivered.
         */
        fun stop(context: Context) {
            if (Track.deferStop()) {
                LogcatVoiceLog.d(TAG, "stop deferred — a foreground start is in flight")
                return
            }
            context.stopService(Intent(context, VoiceHostService::class.java))
        }

        /** [stopSelf], with the same in-flight guard as [stop]. */
        private fun stopGuarded(service: Service) {
            if (Track.deferStop()) {
                LogcatVoiceLog.d(TAG, "stopSelf deferred — a foreground start is in flight")
                return
            }
            service.stopSelf()
        }

        /** Tests only: forget the process-wide start/stop bookkeeping. */
        internal fun resetForTest() = Track.reset()

        internal const val EXTRA_FGS_START = "voicehost_fgs_start"

        /**
         * For the trampoline / launch Activity (a foreground context): promote the service and run
         * the notification action carried by [intent]. Returns true when an action was handled.
         */
        fun handleActivityIntent(activity: Activity, intent: Intent?): Boolean {
            val rt = (activity.application as? VoiceHostOwner)?.voiceHostRuntime ?: return false
            val action = intent?.getStringExtra(EXTRA_HOST_ACTION) ?: return false
            intent.removeExtra(EXTRA_HOST_ACTION)
            if (!start(activity, fromForeground = true)) rt.onServiceStartRefused()
            when (action) {
                NotificationAction.TALK.name -> rt.startVoice(Trigger.NOTIFICATION)
                NotificationAction.RESUME_LISTENING.name -> rt.resumeListening()
                NotificationAction.RECONNECT.name -> rt.reconnectVoice()
                else -> return false
            }
            return true
        }
    }
}

/**
 * Process-wide bookkeeping of the foreground service's start/stop (B-09 crash fix):
 *  - [claimStart]: a `startForegroundService()` is sent only when the service is not already in the
 *    foreground with every wanted type and no start is in flight (idempotent start; the runtime calls
 *    it on every owner phase change).
 *  - [deferStop]: a stop while a start is in flight is remembered instead of executed; [delivered]
 *    reports it once the start reached `onStartCommand` (after `startForeground`).
 *  - A new start while a stop is deferred cancels that stop.
 */
internal object Track {
    /** A start the platform never delivers (e.g. refused after the call returned) must not block forever. */
    private const val IN_FLIGHT_EXPIRY_NS = 10_000_000_000L

    private var inFlight = 0
    private var inFlightSinceNs = 0L
    private var stopDeferred = false
    private var foreground = false
    private var mask = FgsTypes.NONE

    @Synchronized
    fun claimStart(wanted: Int): Boolean {
        stopDeferred = false
        if (inFlight > 0 && System.nanoTime() - inFlightSinceNs > IN_FLIGHT_EXPIRY_NS) {
            LogcatVoiceLog.w("AssistantService", "a foreground start was never delivered — starting again")
            inFlight = 0
        }
        if (inFlight > 0) return false
        if (foreground && (wanted and mask.inv()) == 0) return false
        inFlight++
        inFlightSinceNs = System.nanoTime()
        return true
    }

    @Synchronized
    fun startFailed() {
        if (inFlight > 0) inFlight--
    }

    @Synchronized
    fun deferStop(): Boolean {
        if (inFlight == 0) return false
        stopDeferred = true
        return true
    }

    /** Called from onStartCommand; returns true when a deferred stop must run now. */
    @Synchronized
    fun delivered(intent: Intent?, foreground: Boolean, mask: Int): Boolean {
        this.foreground = foreground
        this.mask = mask
        if (intent?.getBooleanExtra(VoiceHostService.EXTRA_FGS_START, false) == true && inFlight > 0) inFlight--
        if (inFlight == 0 && stopDeferred) {
            stopDeferred = false
            return true
        }
        return false
    }

    @Synchronized
    fun destroyed() {
        foreground = false
        mask = FgsTypes.NONE
        // A start sent while the old instance was being destroyed is still in flight: keep counting it.
    }

    @Synchronized
    fun reset() {
        inFlight = 0
        stopDeferred = false
        foreground = false
        mask = FgsTypes.NONE
    }
}
