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
import android.util.Log
import androidx.core.app.NotificationCompat
import com.assistant.peripheral.MainActivity
import com.assistant.peripheral.R
import com.assistant.peripheral.voice.WakeWordDetector

/**
 * Foreground service that keeps the assistant running in the background.
 * Maintains WebSocket connection and listens for wake word.
 */
class AssistantService : Service() {

    companion object {
        private const val TAG = "AssistantService"
        private const val NOTIFICATION_ID = 1001
        private const val CHANNEL_ID = "assistant_service_channel"
        private const val EXTRA_ENABLE_WAKE_WORD = "enable_wake_word"
        private const val EXTRA_WAKE_WORD = "wake_word"
        private const val EXTRA_VOICE_WORD = "voice_word"
        private const val EXTRA_PAUSE_WAKE_WORD = "pause_wake_word"
        private const val EXTRA_RESUME_WAKE_WORD = "resume_wake_word"
        const val EXTRA_WAKE_WORD_TRIGGERED = "wake_word_triggered"

        // SharedPreferences keys — survive process death
        private const val PREFS_NAME = "assistant_service_prefs"
        private const val PREF_ENABLED = "wake_word_enabled"
        private const val PREF_WAKE_WORD = "wake_word"
        private const val PREF_VOICE_WORD = "voice_word"

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

        fun pauseWakeWord(context: Context) {
            val intent = Intent(context, AssistantService::class.java).apply {
                putExtra(EXTRA_PAUSE_WAKE_WORD, true)
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) context.startForegroundService(intent)
            else context.startService(intent)
        }

        fun resumeWakeWord(context: Context) {
            val intent = Intent(context, AssistantService::class.java).apply {
                putExtra(EXTRA_RESUME_WAKE_WORD, true)
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) context.startForegroundService(intent)
            else context.startService(intent)
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

        fun updateWakeWord(context: Context, enabled: Boolean, wakeWord: String, voiceWord: String = "") {
            val intent = Intent(context, AssistantService::class.java).apply {
                putExtra(EXTRA_ENABLE_WAKE_WORD, enabled)
                putExtra(EXTRA_WAKE_WORD, wakeWord)
                putExtra(EXTRA_VOICE_WORD, voiceWord)
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

    // Set to true while a voice session is active (between pauseWakeWord and resumeWakeWord).
    // Prevents ACTION_SCREEN_ON from restarting the detector and stealing the mic from WebRTC.
    private var voiceSessionActive: Boolean = false

    // Debounce handler: ACTION_SCREEN_ON and ACTION_USER_PRESENT often fire within ms of each
    // other — collapse them into a single rearmWakeWord() call after a short delay.
    private val rearmHandler = Handler(Looper.getMainLooper())
    private val rearmRunnable = Runnable { rearmWakeWord() }

    // In-memory cache of last-known config (authoritative copy is in SharedPreferences)
    private var lastWakeWord: String = "hey assistant"
    private var lastVoiceWord: String = ""
    private var lastEnabled: Boolean = false

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
        val wakeWord = prefs.getString(PREF_WAKE_WORD, "hey assistant") ?: "hey assistant"
        val voiceWord = prefs.getString(PREF_VOICE_WORD, "") ?: ""
        // Sync in-memory cache
        lastEnabled = enabled
        lastWakeWord = wakeWord
        lastVoiceWord = voiceWord

        if (!enabled) return

        val detector = wakeWordDetector
        when {
            detector == null -> startWakeWord(wakeWord, voiceWord)
            detector.isPaused -> {
                // Detector is cleanly paused (e.g. during a voice session) — resume it.
                // If resume fails (mic still busy), startSilenceMonitor() has its own retry.
                detector.resume()
            }
            !detector.isActive -> startWakeWord(wakeWord, voiceWord)
            else -> {
                // Detector appears active — but the silence monitor may have silently failed
                // (e.g. mic was busy when startRecording() was called). Do a clean restart
                // to guarantee a healthy state.
                startWakeWord(wakeWord, voiceWord)
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
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        Log.d(TAG, "Service started")
        startForeground(NOTIFICATION_ID, createNotification())

        if (intent != null) {
            if (intent.getBooleanExtra(EXTRA_PAUSE_WAKE_WORD, false)) {
                Log.d(TAG, "Pausing wake word detection for voice session")
                voiceSessionActive = true
                wakeWordDetector?.pause()
            } else if (intent.getBooleanExtra(EXTRA_RESUME_WAKE_WORD, false)) {
                Log.d(TAG, "Resuming wake word detection after voice session")
                voiceSessionActive = false
                // Always do a full restart here — the silence monitor may be in a broken
                // state if the mic was held by WebRTC when resume() was last called
                // (e.g. screen-unlock fired while the voice session was still active).
                startWakeWord(lastWakeWord, lastVoiceWord)
            } else if (intent.hasExtra(EXTRA_ENABLE_WAKE_WORD)) {
                val enableWakeWord = intent.getBooleanExtra(EXTRA_ENABLE_WAKE_WORD, false)
                val wakeWord = intent.getStringExtra(EXTRA_WAKE_WORD) ?: "hey assistant"
                val voiceWord = intent.getStringExtra(EXTRA_VOICE_WORD) ?: ""
                // Persist config to SharedPreferences so it survives process death
                prefs.edit()
                    .putBoolean(PREF_ENABLED, enableWakeWord)
                    .putString(PREF_WAKE_WORD, wakeWord)
                    .putString(PREF_VOICE_WORD, voiceWord)
                    .apply()
                lastEnabled = enableWakeWord
                lastWakeWord = wakeWord
                lastVoiceWord = voiceWord
                if (enableWakeWord) {
                    startWakeWord(wakeWord, voiceWord)
                } else {
                    stopWakeWord()
                }
            }
        } else {
            // Null intent = sticky restart after process kill.
            // In-memory fields are lost — restore from SharedPreferences.
            val enabled = prefs.getBoolean(PREF_ENABLED, false)
            val wakeWord = prefs.getString(PREF_WAKE_WORD, "hey assistant") ?: "hey assistant"
            val voiceWord = prefs.getString(PREF_VOICE_WORD, "") ?: ""
            lastEnabled = enabled
            lastWakeWord = wakeWord
            lastVoiceWord = voiceWord
            Log.d(TAG, "Sticky restart — restored config from prefs: enabled=$enabled, wake=\"$wakeWord\"")
            if (enabled) {
                startWakeWord(wakeWord, voiceWord)
            }
        }

        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        super.onDestroy()
        rearmHandler.removeCallbacks(rearmRunnable)
        unregisterReceiver(screenReceiver)
        wakeWordDetector?.release()
        Log.d(TAG, "Service destroyed")
    }

    private fun startWakeWord(wakeWord: String, voiceWord: String) {
        wakeWordDetector?.stop()
        wakeWordDetector = WakeWordDetector(this, wakeWord, voiceWord)
        wakeWordDetector?.start()
        Log.d(TAG, "Wake word detection started — wake: \"$wakeWord\", voice: \"$voiceWord\"")
    }

    private fun stopWakeWord() {
        wakeWordDetector?.stop()
        wakeWordDetector = null
        Log.d(TAG, "Wake word detection stopped")
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

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.notification_title))
            .setContentText(getString(R.string.notification_text))
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .build()
    }
}
