package com.assistant.device.receiver

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.util.Log
import com.assistant.device.service.WatchdogService
import com.assistant.device.util.AdbUtil
import com.assistant.device.util.CpuUtil
import com.assistant.device.util.DndUtil
import com.assistant.device.util.Prefs

/**
 * Handles BOOT_COMPLETED to configure the device as an assistant terminal.
 *
 * ## Execution context
 *
 * BroadcastReceiver.onReceive() runs on the main thread. Android will kill the
 * receiver process roughly 10 seconds after onReceive() returns if no components
 * are running. We:
 *   1. Acquire a timed WakeLock immediately to prevent CPU sleep.
 *   2. Do all fast, synchronous work directly in onReceive().
 *   3. Use Handler.postDelayed for the assistant app launch (3-second delay to
 *      let the system settle after boot). The wake lock covers this delay.
 *   4. Start the WatchdogService, which keeps the process alive long-term.
 *
 * ## Boot sequence order
 *
 * 1. Record boot timestamp
 * 2. Re-enable WiFi ADB (Settings.Secure write — instant)
 * 3. Set CPU governor to 'performance' (sysfs write — instant, may fail without root)
 * 4. Enable Do Not Disturb (Settings.Global write — instant)
 * 5. Start WatchdogService (foreground service — keeps process alive)
 * 6. Launch assistant app after 3-second delay
 */
class BootReceiver : BroadcastReceiver() {

    companion object {
        private const val TAG = "BootReceiver"
        private const val ASSISTANT_PACKAGE = "com.assistant.peripheral"
        private const val ASSISTANT_ACTIVITY = "com.assistant.peripheral.MainActivity"

        /** Delay before launching the assistant app — lets the launcher and
         *  system services finish initialising after boot. */
        private const val LAUNCH_DELAY_MS = 3_000L

        /** Maximum time we hold the wake lock. Must be longer than LAUNCH_DELAY_MS. */
        private const val WAKE_LOCK_TIMEOUT_MS = 15_000L
    }

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return

        Log.i(TAG, "BOOT_COMPLETED received — starting device configuration")

        // Acquire a partial wake lock so the CPU doesn't sleep before we're done.
        val pm = context.getSystemService(Context.POWER_SERVICE) as PowerManager
        val wakeLock = pm.newWakeLock(
            PowerManager.PARTIAL_WAKE_LOCK,
            "assistantdevice:boot"
        ).apply { acquire(WAKE_LOCK_TIMEOUT_MS) }

        val prefs = Prefs.get(context)

        // 1. Record boot time for status display in MainActivity
        prefs.edit().putLong(Prefs.KEY_LAST_BOOT_MS, System.currentTimeMillis()).apply()

        // 2. Re-enable WiFi ADB
        if (prefs.getBoolean(Prefs.KEY_WIFI_ADB_ENABLED, true)) {
            val ok = AdbUtil.enableWifiAdb(context)
            Log.i(TAG, "WiFi ADB: ${if (ok) "enabled" else "FAILED (WRITE_SECURE_SETTINGS missing?)"}")
        }

        // 3. Set CPU governor to 'performance'
        if (prefs.getBoolean(Prefs.KEY_CPU_GOVERNOR_ENABLED, true)) {
            val cores = CpuUtil.setPerformanceGovernor()
            Log.i(TAG, "CPU governor: updated $cores core(s) " +
                "(0 = root required, this is expected on non-rooted devices)")
        }

        // 4. Enable Do Not Disturb
        if (prefs.getBoolean(Prefs.KEY_DND_ENABLED, true)) {
            val ok = DndUtil.enableDnd(context)
            Log.i(TAG, "DND: ${if (ok) "enabled" else "FAILED"}")
        }

        // 5. Start WatchdogService (foreground service — survives as long as device runs)
        if (prefs.getBoolean(Prefs.KEY_WATCHDOG_ENABLED, true)) {
            context.startService(Intent(context, WatchdogService::class.java))
            Log.i(TAG, "WatchdogService started")
        }

        // 6. Launch the assistant app after a short delay
        if (prefs.getBoolean(Prefs.KEY_AUTO_LAUNCH_ENABLED, true)) {
            Handler(Looper.getMainLooper()).postDelayed({
                launchAssistantApp(context)
                wakeLock.release()
                Log.i(TAG, "Boot configuration complete")
            }, LAUNCH_DELAY_MS)
        } else {
            wakeLock.release()
            Log.i(TAG, "Boot configuration complete (auto-launch disabled)")
        }
    }

    /**
     * Starts the assistant app. Falls back to an explicit component Intent if
     * the package manager cannot resolve the launch intent (e.g. app not installed).
     */
    private fun launchAssistantApp(context: Context) {
        val launch = context.packageManager
            .getLaunchIntentForPackage(ASSISTANT_PACKAGE)
            ?: Intent().setClassName(ASSISTANT_PACKAGE, ASSISTANT_ACTIVITY)

        launch.addFlags(
            Intent.FLAG_ACTIVITY_NEW_TASK or
            Intent.FLAG_ACTIVITY_REORDER_TO_FRONT
        )

        try {
            context.startActivity(launch)
            Log.i(TAG, "Assistant app launched")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to launch assistant app: ${e.message} " +
                "(is com.assistant.peripheral installed?)")
        }
    }
}
