package com.assistant.device.service

import android.app.ActivityManager
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import androidx.core.app.NotificationCompat
import com.assistant.device.util.Prefs

/**
 * Foreground service that periodically checks whether the assistant app is alive
 * and restarts it if not.
 *
 * ## Why a foreground service?
 *
 * On Android 5.0, background services are subject to aggressive killing under
 * memory pressure. A foreground service with an active notification is protected
 * from this — Android will not kill it unless in extreme low-memory situations.
 * `START_STICKY` ensures the OS restarts the service if it is killed.
 *
 * ## Polling mechanism
 *
 * Uses `Handler.postDelayed` with a repeating `Runnable` rather than AlarmManager
 * or WorkManager because:
 *   - WorkManager minimum periodic interval is 15 minutes (too slow for a watchdog)
 *   - AlarmManager.setExactAndAllowWhileIdle() is API 23+ (target is API 21)
 *   - A Handler loop inside a foreground service is simple, reliable, and correct
 *
 * ## Process detection
 *
 * Uses `ActivityManager.getRunningAppProcesses()` to detect whether
 * `com.assistant.peripheral` is alive. On API 21 this returns all visible
 * processes (the GET_TASKS permission in the manifest improves completeness).
 * If the process list is null (can happen under low memory), no action is taken.
 *
 * ## Notification
 *
 * Shows a minimal persistent notification (required for foreground services).
 * On API 26+ a notification channel is created; on API 21-25 it is not needed.
 */
class WatchdogService : Service() {

    companion object {
        private const val TAG = "WatchdogService"
        private const val ASSISTANT_PACKAGE = "com.assistant.peripheral"
        private const val ASSISTANT_ACTIVITY = "com.assistant.peripheral.MainActivity"

        /** How often to check if the assistant app is running. */
        private const val POLL_INTERVAL_MS = 30_000L

        private const val NOTIFICATION_ID = 2001
        private const val CHANNEL_ID = "watchdog_channel"
        private const val CHANNEL_NAME = "AssistantDevice Watchdog"
    }

    private val handler = Handler(Looper.getMainLooper())
    private var isRunning = false

    /** Repeating check: if assistant app is not running, launch it. */
    private val checkRunnable = object : Runnable {
        override fun run() {
            if (!isRunning) return
            val prefs = Prefs.get(this@WatchdogService)
            if (prefs.getBoolean(Prefs.KEY_WATCHDOG_ENABLED, true)) {
                checkAndRevive()
            }
            handler.postDelayed(this, POLL_INTERVAL_MS)
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (isRunning) {
            Log.d(TAG, "Already running — ignoring duplicate start")
            return START_STICKY
        }
        isRunning = true

        createNotificationChannel()
        startForeground(NOTIFICATION_ID, buildNotification())

        handler.post(checkRunnable)
        Log.i(TAG, "Watchdog started — checking every ${POLL_INTERVAL_MS / 1000}s")

        // START_STICKY: if this service is killed, Android will restart it
        // (without re-delivering the original intent, which is fine).
        return START_STICKY
    }

    override fun onDestroy() {
        isRunning = false
        handler.removeCallbacksAndMessages(null)
        Log.i(TAG, "Watchdog stopped")
        super.onDestroy()
    }

    /** Required by Service but not used — this service is not bound. */
    override fun onBind(intent: Intent?): IBinder? = null

    // ── Private helpers ───────────────────────────────────────────────────

    /**
     * Checks whether com.assistant.peripheral is alive by looking for its
     * AssistantService in the running services list.
     *
     * Why not getRunningAppProcesses()?
     * On Android 5.0 (API 21), getRunningAppProcesses() only returns processes
     * visible to the calling app's uid. Since com.assistant.device and
     * com.assistant.peripheral run under different uids, the peripheral process
     * is invisible to us — causing false "not found" results even when it's running.
     *
     * getRunningServices() is not subject to this restriction on API 21 and
     * correctly returns services from any package regardless of uid.
     */
    private fun checkAndRevive() {
        val am = getSystemService(ACTIVITY_SERVICE) as ActivityManager

        @Suppress("DEPRECATION")
        val services = am.getRunningServices(100)

        val alive = services?.any { it.service.packageName == ASSISTANT_PACKAGE } == true

        if (!alive) {
            Log.w(TAG, "Assistant service not found — relaunching")
            launchAssistant()
        } else {
            Log.d(TAG, "Assistant app alive — OK")
        }
    }

    private fun launchAssistant() {
        val launch = packageManager.getLaunchIntentForPackage(ASSISTANT_PACKAGE)
            ?: Intent().setClassName(ASSISTANT_PACKAGE, ASSISTANT_ACTIVITY)
        launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_REORDER_TO_FRONT)
        try {
            startActivity(launch)
        } catch (e: Exception) {
            Log.e(TAG, "Failed to relaunch assistant: ${e.message}")
        }
    }

    private fun buildNotification() = NotificationCompat.Builder(this, CHANNEL_ID)
        .setContentTitle("AssistantDevice")
        .setContentText("Monitoring assistant app")
        .setSmallIcon(android.R.drawable.ic_menu_manage)
        .setPriority(NotificationCompat.PRIORITY_MIN)
        .setOngoing(true)   // Cannot be dismissed by the user
        .build()

    /**
     * Creates the notification channel required on API 26+.
     * No-op on API 21-25 (channels did not exist).
     */
    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                CHANNEL_NAME,
                NotificationManager.IMPORTANCE_MIN  // Silent, no badge, no sound
            ).apply {
                description = "Keeps the assistant app running"
                setShowBadge(false)
            }
            val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            nm.createNotificationChannel(channel)
        }
    }
}
