package com.assistant.peripheral.service

import android.app.*
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.SharedPreferences
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.localbroadcastmanager.content.LocalBroadcastManager
import com.assistant.peripheral.MainActivity
import com.assistant.peripheral.R
import com.assistant.peripheral.voice.WakeWordDetector
import java.io.DataInputStream
import java.io.FileInputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicLong
import kotlinx.coroutines.CompletableDeferred

/**
 * Foreground service that keeps the assistant running in the background.
 * Maintains WebSocket connection and listens for wake word.
 */
class AssistantService : Service() {

    companion object {
        private const val TAG = "AssistantService"
        private const val NOTIFICATION_ID = 1001
        private const val CHANNEL_ID = "assistant_service_channel"
        // Intent extras. Detour 3 / plan §0.5: TALK_WORD and WAKE_WORD now
        // refer respectively to the turn-based single-message trigger and the
        // realtime conversation trigger (semantically swapped from pre-Detour-3).
        // Umbrella concepts (enable / pause / resume / triggered) keep their
        // historical names since they describe the whole detector, not a
        // specific trigger.
        private const val EXTRA_ENABLE_WAKE_WORD = "enable_wake_word"
        private const val EXTRA_TALK_WORD = "turn_talk_word"
        private const val EXTRA_WAKE_WORD = "realtime_wake_word"
        private const val EXTRA_PAUSE_WAKE_WORD = "pause_wake_word"
        private const val EXTRA_RESUME_WAKE_WORD = "resume_wake_word"
        const val EXTRA_WAKE_WORD_TRIGGERED = "wake_word_triggered"

        // Inc 7: ack-token for the deferred hand-off contract. The caller
        // stashes a CompletableDeferred<Unit> in `pendingAcks` keyed by a
        // monotonic Long token, then passes the token through the Intent.
        // onStartCommand completes the deferred after the pause/resume
        // body has drained. Default value of -1L means "no caller is
        // awaiting" (e.g. system-redelivered intents on sticky restart).
        private const val EXTRA_ACK_TOKEN = "ack_token"
        private const val ACK_TOKEN_NONE = -1L

        /**
         * Process-static registry for Inc 7 deferred-ack contracts. Lives
         * on the companion (not on a Service instance) because the caller
         * stashes BEFORE the service exists in cold-start cases. Cleared
         * automatically by takeAck (one-shot) so completed deferreds
         * don't accumulate.
         */
        private val pendingAcks: ConcurrentHashMap<Long, CompletableDeferred<Unit>> =
            ConcurrentHashMap()
        private val ackTokenGenerator = AtomicLong(0L)

        private fun nextAckToken(): Long = ackTokenGenerator.incrementAndGet()
        private fun stashAck(token: Long, ack: CompletableDeferred<Unit>) {
            pendingAcks[token] = ack
        }
        private fun takeAck(token: Long): CompletableDeferred<Unit>? =
            if (token == ACK_TOKEN_NONE) null else pendingAcks.remove(token)

        // Test-only accessors so PauseResumeAckParityTest can exercise the
        // registry contract without a Service runtime. Visibility is
        // `internal` to keep them out of the public API surface.
        internal fun nextAckTokenForTest(): Long = nextAckToken()
        internal fun stashAckForTest(token: Long, ack: CompletableDeferred<Unit>) =
            stashAck(token, ack)
        internal fun takeAckForTest(token: Long): CompletableDeferred<Unit>? = takeAck(token)
        internal fun clearPendingAcksForTest() {
            pendingAcks.clear()
            ackTokenGenerator.set(0L)
        }

        // SharedPreferences keys — survive process death.
        // Same naming convention: TALK_WORD = turn-based, WAKE_WORD = realtime.
        private const val PREFS_NAME = "assistant_service_prefs"
        private const val PREF_ENABLED = "wake_word_enabled"
        private const val PREF_TALK_WORD = "turn_talk_word"
        private const val PREF_WAKE_WORD = "realtime_wake_word"
        private const val PREF_WAKE_MIC_GAIN = "wake_word_mic_gain"

        // Inc 8 removed `WATCHDOG_INTERVAL_MS` (was `2 * 60 * 60 * 1000L`).
        // The 2-hour periodic rebuild has been replaced by a NO_SPEECH-error-
        // driven health check inside WakeWordDetector. When the count of
        // consecutive ERROR_NO_SPEECH errors crosses
        // `WakeWordDetector.NO_SPEECH_HEALTH_THRESHOLD` (8, plan §9
        // decision 6), the detector broadcasts ACTION_RECOGNIZER_UNHEALTHY
        // and this service rebuilds via `startWakeWord(...)`. Rebuild rate
        // is capped at one per 3 s by Inc 3's dedupe.

        // Window inside which a second `startWakeWord` call with the same
        // (talkWord, wakeWord, micGain) tuple is treated as a duplicate
        // (Android intent redelivery / sticky-restart races). 3 s covers
        // both 20 ms and 1.3 s redelivery gaps observed in the field, and
        // is short enough not to mask legitimate user toggles. See
        // wakeword_subsystem_refactor_plan_2026_06_09.md §3 Inc 3.
        private const val WAKE_START_DEDUPE_WINDOW_MS = 3000L

        /**
         * Pure predicate for the wake-word start dedupe (Increment 3).
         * Returns true when a `startWakeWord(talkWord, wakeWord, micGain)`
         * call should short-circuit because it matches the previous call's
         * key AND it lands within `WAKE_START_DEDUPE_WINDOW_MS` of the
         * previous call.
         *
         * Strict `<` on the time window (not `<=`): a call exactly at the
         * window boundary is allowed through, so a legitimate user toggle
         * exactly 3 s after a redelivered intent isn't masked.
         */
        internal fun shouldDedupeWakeStart(
            key: Triple<String, String, Float>,
            nowMs: Long,
            lastKey: Triple<String, String, Float>?,
            lastAtMs: Long,
        ): Boolean =
            lastKey != null &&
                key == lastKey &&
                (nowMs - lastAtMs) < WAKE_START_DEDUPE_WINDOW_MS

        fun start(context: Context) {
            val intent = Intent(context, AssistantService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        fun stop(context: Context) {
            val intent = Intent(context, AssistantService::class.java)
            context.stopService(intent)
        }

        /**
         * Inc 7: pauseWakeWord returns a CompletableDeferred<Unit> that
         * completes when the service has drained the detector's pause
         * path (silence-monitor cancelled, AudioRecord released, recognizer
         * destroyed if running). Callers await the deferred with a 2 s
         * timeout (plan §9 decision 5) before assuming the mic is free.
         */
        fun pauseWakeWord(context: Context): CompletableDeferred<Unit> {
            val ack = CompletableDeferred<Unit>()
            val token = nextAckToken()
            stashAck(token, ack)
            val intent = Intent(context, AssistantService::class.java).apply {
                putExtra(EXTRA_PAUSE_WAKE_WORD, true)
                putExtra(EXTRA_ACK_TOKEN, token)
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) context.startForegroundService(intent)
            else context.startService(intent)
            return ack
        }

        /**
         * Inc 7: resumeWakeWord returns a CompletableDeferred<Unit> that
         * completes when the service has fired the wake-word startup
         * sequence (startWakeWord returned; the silence-monitor coroutine
         * is launched on its IO dispatcher). The caller can await this to
         * know the resume intent reached the service — and to prevent
         * the duplicate-resume-intent race surfaced by Detour 3 (multiple
         * finalizeVoiceStop call sites firing resumeWakeWord concurrently).
         */
        fun resumeWakeWord(context: Context): CompletableDeferred<Unit> {
            val ack = CompletableDeferred<Unit>()
            val token = nextAckToken()
            stashAck(token, ack)
            val intent = Intent(context, AssistantService::class.java).apply {
                putExtra(EXTRA_RESUME_WAKE_WORD, true)
                putExtra(EXTRA_ACK_TOKEN, token)
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) context.startForegroundService(intent)
            else context.startService(intent)
            return ack
        }

        fun bringToForeground(context: Context) {
            // Acquire a wake lock to turn the screen on before starting the activity.
            // ACQUIRE_CAUSES_WAKEUP forces the screen on even when it's off — this is the
            // reliable path on Android 5 (Lollipop) where window flags alone don't work
            // when the activity is already running in the background.
            val pm = context.getSystemService(Context.POWER_SERVICE) as PowerManager
            @Suppress("DEPRECATION")
            val wl = pm.newWakeLock(
                PowerManager.SCREEN_BRIGHT_WAKE_LOCK or PowerManager.ACQUIRE_CAUSES_WAKEUP,
                "assistant:wakeword"
            )
            wl.acquire(3000L) // hold for 3 s — enough for the activity to apply its own flags

            val intent = Intent(context, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or
                        Intent.FLAG_ACTIVITY_REORDER_TO_FRONT or
                        Intent.FLAG_ACTIVITY_SINGLE_TOP
                putExtra(EXTRA_WAKE_WORD_TRIGGERED, true)
            }
            context.startActivity(intent)
        }

        // wakeWordMicGain is REQUIRED — a silent default would let a forgetful caller
        // clobber the user's slider value with 1.0f every time the service is told to
        // re-apply config. Every caller already has the gain readily available from
        // DataStore; pass it explicitly.
        //
        // Naming (Detour 3 / plan §0.5):
        //   talkWord = turn-based single voice message phrase (push-to-talk style)
        //   wakeWord = realtime WebRTC voice conversation phrase
        fun updateWakeWord(context: Context, enabled: Boolean, talkWord: String, wakeWord: String, wakeWordMicGain: Float) {
            val intent = Intent(context, AssistantService::class.java).apply {
                putExtra(EXTRA_ENABLE_WAKE_WORD, enabled)
                putExtra(EXTRA_TALK_WORD, talkWord)
                putExtra(EXTRA_WAKE_WORD, wakeWord)
                putExtra("wake_word_mic_gain", wakeWordMicGain)
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }
    }

    private var wakeWordDetector: WakeWordDetector? = null
    private lateinit var prefs: SharedPreferences

    // Recents long-press monitor (reads /dev/input/event2 directly)
    private var recentsMonitorThread: Thread? = null
    @Volatile private var recentsMonitorRunning = false

    // Set to true while a voice session is active (between pauseWakeWord and resumeWakeWord).
    // Prevents ACTION_SCREEN_ON from restarting the detector and stealing the mic from WebRTC.
    private var voiceSessionActive: Boolean = false

    // Debounce handler: ACTION_SCREEN_ON and ACTION_USER_PRESENT often fire within ms of each
    // other — collapse them into a single rearmWakeWord() call after a short delay.
    private val rearmHandler = Handler(Looper.getMainLooper())
    private val rearmRunnable = Runnable { rearmWakeWord() }

    // Inc 8: NO_SPEECH-driven health receiver. WakeWordDetector broadcasts
    // ACTION_RECOGNIZER_UNHEALTHY when consecutive NO_SPEECH errors cross
    // the threshold; we rebuild via `startWakeWord(...)`. Funnels through
    // Inc 3's dedupe so a flapping recognizer can't trigger a rebuild storm.
    // Replaces the deleted 2-hour `watchdogRunnable`.
    private val recognizerUnhealthyReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            if (voiceSessionActive || !lastEnabled) return
            Log.d(TAG, "Recognizer unhealthy broadcast received — rebuilding wake-word detector")
            startWakeWord(lastTalkWord, lastWakeWord)
        }
    }

    /**
     * Inc 9: notification manager handle for re-issuing the foreground
     * notification when the mic-unavailable state changes. Lazily fetched
     * via `getSystemService`.
     */
    private val notificationManager: NotificationManager
        get() = getSystemService(NOTIFICATION_SERVICE) as NotificationManager

    /**
     * Inc 9: tracks whether the foreground notification is currently
     * showing the "mic stalled" warning text. Toggled by the
     * ACTION_MIC_UNAVAILABLE / ACTION_MIC_AVAILABLE receiver. The
     * notification builder reads this to pick the contentText.
     */
    @Volatile private var micUnavailable: Boolean = false

    private val micAvailabilityReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            when (intent?.action) {
                WakeWordDetector.ACTION_MIC_UNAVAILABLE -> {
                    if (!micUnavailable) {
                        micUnavailable = true
                        Log.w(TAG, "Mic unavailable — updating notification")
                        notificationManager.notify(NOTIFICATION_ID, createNotification())
                    }
                }
                WakeWordDetector.ACTION_MIC_AVAILABLE -> {
                    if (micUnavailable) {
                        micUnavailable = false
                        Log.d(TAG, "Mic available again — clearing notification warning")
                        notificationManager.notify(NOTIFICATION_ID, createNotification())
                    }
                }
            }
        }
    }

    // In-memory cache of last-known config (authoritative copy is in SharedPreferences).
    // Naming (Detour 3 / plan §0.5):
    //   lastTalkWord = last turn-based phrase
    //   lastWakeWord = last realtime phrase
    // Defaults match the AppSettings defaults — they apply only when
    // SharedPreferences hasn't been populated yet (e.g. fresh install).
    private var lastTalkWord: String = "my friend"
    private var lastWakeWord: String = "wake up"
    private var lastEnabled: Boolean = false
    private var lastWakeMicGain: Float = 1.0f

    // Inc 3 dedupe: last `startWakeWord` (key, monotonic-clock timestamp)
    // — used by `shouldDedupeWakeStart` to suppress duplicate intents
    // arriving inside `WAKE_START_DEDUPE_WINDOW_MS`. SystemClock.elapsedRealtime
    // (monotonic, includes sleep) is preferred over System.currentTimeMillis
    // (wall-clock, jumps with NTP / user changes).
    private var lastStartKey: Triple<String, String, Float>? = null
    private var lastStartAtMs: Long = 0L

    // Receiver for screen-on (ACTION_SCREEN_ON) and keyguard dismiss (ACTION_USER_PRESENT).
    // ACTION_SCREEN_ON fires immediately when the display turns on (even with lock screen).
    // ACTION_USER_PRESENT fires only after the user dismisses the keyguard (PIN / none).
    // We handle both so detection resumes even on devices with no lock screen.
    private val screenReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            when (intent?.action) {
                Intent.ACTION_SCREEN_ON,
                Intent.ACTION_USER_PRESENT -> {
                    Log.d(TAG, "Screen on / unlocked (${intent.action}) — re-arming wake word")
                    // Debounce: SCREEN_ON and USER_PRESENT often fire within ms of each other.
                    // Cancel any pending rearm and schedule one 300ms from now.
                    rearmHandler.removeCallbacks(rearmRunnable)
                    rearmHandler.postDelayed(rearmRunnable, 300)
                }
            }
        }
    }

    private fun rearmWakeWord() {
        // Don't touch the mic if a voice session is active — WebRTC owns it.
        // The session will call resumeWakeWord() when it ends.
        if (voiceSessionActive) {
            Log.d(TAG, "Screen on during voice session — skipping wake word rearm")
            return
        }

        // Reload from SharedPreferences in case in-memory fields are stale (fresh process)
        val enabled = prefs.getBoolean(PREF_ENABLED, false)
        val talkWord = prefs.getString(PREF_TALK_WORD, "my friend") ?: "my friend"
        val wakeWord = prefs.getString(PREF_WAKE_WORD, "wake up") ?: "wake up"
        val wakeMicGain = prefs.getFloat(PREF_WAKE_MIC_GAIN, 1.0f)
        // Sync in-memory cache
        lastEnabled = enabled
        lastTalkWord = talkWord
        lastWakeWord = wakeWord
        lastWakeMicGain = wakeMicGain

        if (!enabled) return

        val detector = wakeWordDetector
        when {
            detector == null -> startWakeWord(talkWord, wakeWord)
            detector.isPaused -> {
                // Detector is cleanly paused (e.g. during a voice session) — resume it.
                // If resume fails (mic still busy), startSilenceMonitor() has its own retry.
                detector.resume()
            }
            !detector.isActive -> startWakeWord(talkWord, wakeWord)
            else -> {
                // Detector appears active — but the silence monitor may have silently failed
                // (e.g. mic was busy when startRecording() was called). Do a clean restart
                // to guarantee a healthy state.
                startWakeWord(talkWord, wakeWord)
            }
        }
    }

    override fun onCreate() {
        super.onCreate()
        Log.d(TAG, "Service created")
        prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        createNotificationChannel()

        // Register for screen-on and keyguard-dismiss events.
        // Both must be registered dynamically (not in manifest) — system-only intents.
        val filter = IntentFilter().apply {
            addAction(Intent.ACTION_SCREEN_ON)
            addAction(Intent.ACTION_USER_PRESENT)
        }
        registerReceiver(screenReceiver, filter)

        startRecentsMonitor()

        // Inc 8: register the NO_SPEECH-driven recognizer-unhealthy receiver.
        // Replaces the deleted 2-hour periodic rebuild watchdog.
        LocalBroadcastManager.getInstance(this).registerReceiver(
            recognizerUnhealthyReceiver,
            IntentFilter(WakeWordDetector.ACTION_RECOGNIZER_UNHEALTHY),
        )
        // Inc 9: register the mic-availability receiver. Toggles the foreground
        // notification text between the steady-state and the "Wake word stalled"
        // warning. Independent of the Inc 8 receiver — different signal, different
        // remediation (notification vs detector rebuild).
        LocalBroadcastManager.getInstance(this).registerReceiver(
            micAvailabilityReceiver,
            IntentFilter().apply {
                addAction(WakeWordDetector.ACTION_MIC_UNAVAILABLE)
                addAction(WakeWordDetector.ACTION_MIC_AVAILABLE)
            },
        )
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        Log.d(TAG, "Service started")
        startForeground(NOTIFICATION_ID, createNotification())

        if (intent != null) {
            // Inc 7: extract the deferred-ack token before the body. Completed
            // unconditionally at the end of the pause/resume branch so the
            // caller's withTimeoutOrNull(2000L) { ack.await() } unblocks even
            // when the body short-circuited.
            val ackToken = intent.getLongExtra(EXTRA_ACK_TOKEN, ACK_TOKEN_NONE)
            if (intent.getBooleanExtra(EXTRA_PAUSE_WAKE_WORD, false)) {
                Log.d(TAG, "Pausing wake word detection for voice session")
                voiceSessionActive = true
                wakeWordDetector?.pause()
                takeAck(ackToken)?.complete(Unit)
            } else if (intent.getBooleanExtra(EXTRA_RESUME_WAKE_WORD, false)) {
                // Inc 7 + Detour-3 follow-up: idempotent at the service level.
                // finalizeVoiceStop() in AssistantViewModel has three call sites
                // (legacy bug from before voice_initiator landed) and a duplicate
                // resume intent firing ~600-750 ms after the first one tore down
                // the in-flight recognizer cycle ("AudioRecord.startRecording()
                // failed — mic busy" log). voiceSessionActive==false means the
                // service has already processed a resume for this session-end;
                // short-circuit so the duplicate intent is harmless. The
                // deferred is still completed so the caller's await() unblocks.
                if (!voiceSessionActive) {
                    Log.d(TAG, "Resume wake word intent ignored — already resumed for this session")
                    takeAck(ackToken)?.complete(Unit)
                    return START_STICKY
                }
                Log.d(TAG, "Resuming wake word detection after voice session")
                voiceSessionActive = false
                // Re-read enabled state from SharedPreferences — the user may have toggled
                // wake word OFF while the voice session was active. Without this check,
                // resumeWakeWord() would unconditionally restart detection regardless of setting.
                val enabledNow = prefs.getBoolean(PREF_ENABLED, false)
                lastEnabled = enabledNow
                if (enabledNow) {
                    // Always do a full restart here — the silence monitor may be in a broken
                    // state if the mic was held by WebRTC when resume() was last called.
                    startWakeWord(lastTalkWord, lastWakeWord)
                } else {
                    Log.d(TAG, "Wake word disabled — skipping restart after voice session")
                }
                takeAck(ackToken)?.complete(Unit)
            } else if (intent.hasExtra(EXTRA_ENABLE_WAKE_WORD)) {
                val enableWakeWord = intent.getBooleanExtra(EXTRA_ENABLE_WAKE_WORD, false)
                val talkWord = intent.getStringExtra(EXTRA_TALK_WORD) ?: "my friend"
                val wakeWord = intent.getStringExtra(EXTRA_WAKE_WORD) ?: "wake up"
                val wakeMicGain = intent.getFloatExtra("wake_word_mic_gain", lastWakeMicGain)
                // Persist config to SharedPreferences so it survives process death.
                prefs.edit()
                    .putBoolean(PREF_ENABLED, enableWakeWord)
                    .putString(PREF_TALK_WORD, talkWord)
                    .putString(PREF_WAKE_WORD, wakeWord)
                    .putFloat(PREF_WAKE_MIC_GAIN, wakeMicGain)
                    .apply()
                lastEnabled = enableWakeWord
                lastTalkWord = talkWord
                lastWakeWord = wakeWord
                lastWakeMicGain = wakeMicGain
                if (enableWakeWord) {
                    startWakeWord(talkWord, wakeWord, wakeMicGain)
                } else {
                    stopWakeWord()
                }
            }
        } else {
            // Null intent = sticky restart after process kill.
            // In-memory fields are lost — restore from SharedPreferences.
            val enabled = prefs.getBoolean(PREF_ENABLED, false)
            val talkWord = prefs.getString(PREF_TALK_WORD, "my friend") ?: "my friend"
            val wakeWord = prefs.getString(PREF_WAKE_WORD, "wake up") ?: "wake up"
            val wakeMicGain = prefs.getFloat(PREF_WAKE_MIC_GAIN, 1.0f)
            lastEnabled = enabled
            lastTalkWord = talkWord
            lastWakeWord = wakeWord
            lastWakeMicGain = wakeMicGain
            Log.d(TAG, "Sticky restart — restored config from prefs: enabled=$enabled, talk=\"$talkWord\", wake=\"$wakeWord\", gain=$wakeMicGain")
            if (enabled) {
                startWakeWord(talkWord, wakeWord, wakeMicGain)
            }
        }

        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        super.onDestroy()
        rearmHandler.removeCallbacks(rearmRunnable)
        unregisterReceiver(screenReceiver)
        // Inc 8: unregister the NO_SPEECH health receiver.
        LocalBroadcastManager.getInstance(this).unregisterReceiver(recognizerUnhealthyReceiver)
        // Inc 9: unregister the mic-availability receiver.
        LocalBroadcastManager.getInstance(this).unregisterReceiver(micAvailabilityReceiver)
        wakeWordDetector?.release()
        stopRecentsMonitor()
        Log.d(TAG, "Service destroyed")
    }

    private fun startWakeWord(talkWord: String, wakeWord: String, micGain: Float = lastWakeMicGain) {
        val key = Triple(talkWord, wakeWord, micGain)
        val nowMs = SystemClock.elapsedRealtime()
        if (shouldDedupeWakeStart(key, nowMs, lastStartKey, lastStartAtMs)) {
            Log.d(TAG, "startWakeWord() dedupe — same key within ${WAKE_START_DEDUPE_WINDOW_MS}ms")
            return
        }
        lastStartKey = key
        lastStartAtMs = nowMs
        wakeWordDetector?.stop()
        wakeWordDetector = WakeWordDetector(this, talkWord, wakeWord, micGain)
        wakeWordDetector?.start()
        Log.d(TAG, "Wake word detection started — talk: \"$talkWord\", wake: \"$wakeWord\", gain=$micGain")
    }

    private fun stopWakeWord() {
        wakeWordDetector?.stop()
        wakeWordDetector = null
        // Clear Inc 3 dedupe state so a subsequent enable-toggle with the
        // same (talk, wake, gain) within 3 s is NOT mistaken for a
        // duplicate intent. The user genuinely wants a restart here.
        lastStartKey = null
        lastStartAtMs = 0L
        Log.d(TAG, "Wake word detection stopped")
    }

    // -------------------------------------------------------------------------
    // Recents button long-press monitor
    // Reads /dev/input/event2 (sec_touchkey) directly. KEY_APPSWITCH (0x00fe)
    // held for >= LONG_PRESS_MS triggers the realtime voice session.
    // -------------------------------------------------------------------------

    private fun startRecentsMonitor() {
        stopRecentsMonitor()
        recentsMonitorRunning = true
        recentsMonitorThread = Thread({
            // input_event struct: timeval (8 bytes) + type (2) + code (2) + value (4) = 16 bytes
            val STRUCT_SIZE = 16
            val KEY_APPSWITCH = 0x00fe.toShort()
            val EV_KEY = 0x01.toShort()
            val LONG_PRESS_MS = 600L

            Log.d(TAG, "Recents monitor started")
            try {
                DataInputStream(FileInputStream("/dev/input/event2")).use { dis ->
                    val buf = ByteArray(STRUCT_SIZE)
                    var pressedAt = 0L
                    while (recentsMonitorRunning) {
                        var offset = 0
                        while (offset < STRUCT_SIZE) {
                            val n = dis.read(buf, offset, STRUCT_SIZE - offset)
                            if (n < 0) { recentsMonitorRunning = false; break }
                            offset += n
                        }
                        if (!recentsMonitorRunning) break

                        val bb = ByteBuffer.wrap(buf).order(ByteOrder.LITTLE_ENDIAN)
                        bb.getLong() // skip timeval (8 bytes)
                        val type = bb.short
                        val code = bb.short
                        val value = bb.int  // 1=down, 0=up, 2=repeat

                        if (type == EV_KEY && code == KEY_APPSWITCH) {
                            when (value) {
                                1 -> pressedAt = System.currentTimeMillis()
                                0 -> {
                                    val held = System.currentTimeMillis() - pressedAt
                                    Log.d(TAG, "KEY_APPSWITCH released after ${held}ms")
                                    if (pressedAt > 0 && held >= LONG_PRESS_MS) {
                                        Log.d(TAG, "Recents long-press → starting realtime voice session")
                                        bringToForeground(this)
                                        // Recents long-press triggers the same UX path as the realtime
                                        // wake word — fire ACTION_WAKE_WORD_DETECTED (post-Detour-3 naming:
                                        // wakeWord = realtime conversation).
                                        LocalBroadcastManager.getInstance(this)
                                            .sendBroadcast(Intent(WakeWordDetector.ACTION_WAKE_WORD_DETECTED))
                                    }
                                    pressedAt = 0L
                                }
                            }
                        }
                    }
                }
            } catch (e: Exception) {
                Log.w(TAG, "Recents monitor error: ${e.message}")
            }
            Log.d(TAG, "Recents monitor stopped")
        }, "recents-monitor")
        recentsMonitorThread?.isDaemon = true
        recentsMonitorThread?.start()
    }

    private fun stopRecentsMonitor() {
        recentsMonitorRunning = false
        recentsMonitorThread?.interrupt()
        recentsMonitorThread = null
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                getString(R.string.notification_channel_name),
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = getString(R.string.notification_channel_description)
                setShowBadge(false)
            }

            val notificationManager = getSystemService(NotificationManager::class.java)
            notificationManager.createNotificationChannel(channel)
        }
    }

    private fun createNotification(): Notification {
        val pendingIntent = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        // Inc 9: swap content text on mic-unavailable. The warning text is
        // inlined here (rather than added to res/values/strings.xml) because
        // the Inc 9 scope is observability-only — adding to strings.xml
        // would invite localization work that's out of scope. If the
        // notification ships in non-en locales the inline literal becomes
        // a future cleanup.
        val contentText = if (micUnavailable)
            "Wake word stalled — mic held by another app"
        else
            getString(R.string.notification_text)

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.notification_title))
            .setContentText(contentText)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .build()
    }
}
