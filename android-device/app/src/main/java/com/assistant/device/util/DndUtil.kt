package com.assistant.device.util

import android.annotation.TargetApi
import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.os.Build
import android.provider.Settings
import android.util.Log

/**
 * Utilities for enabling Do Not Disturb (total silence) mode.
 *
 * ## API version differences
 *
 * - **API 21-22 (this device):** `NotificationManager.setInterruptionFilter()` exists
 *   but on Samsung 5.0.2 it may not be reliable. The fallback is writing
 *   `Settings.Global.zen_mode = 2` (INTERRUPTION_FILTER_NONE equivalent),
 *   which requires WRITE_SECURE_SETTINGS.
 *
 * - **API 23+:** `ACCESS_NOTIFICATION_POLICY` permission is enforced. The app
 *   must be granted notification policy access by the user in Settings, or
 *   the call throws a SecurityException. A direct-to-settings Intent is
 *   provided for the user to grant this.
 *
 * Zen mode values (Settings.Global.zen_mode):
 *   0 = off (INTERRUPTION_FILTER_ALL)
 *   1 = priority only (INTERRUPTION_FILTER_PRIORITY)
 *   2 = total silence (INTERRUPTION_FILTER_NONE)
 *   3 = alarms only (INTERRUPTION_FILTER_ALARMS, API 23+)
 */
object DndUtil {

    private const val TAG = "DndUtil"

    // Settings.Global.ZEN_MODE constant value (API 17+, undocumented in API 21 javadoc)
    private const val ZEN_MODE_KEY = "zen_mode"
    private const val ZEN_MODE_TOTAL_SILENCE = 2

    /**
     * Enables total silence (no interruptions, no alarms, no media).
     *
     * On this device (API 21), writes zen_mode via Settings.Global.
     * On API 23+, uses NotificationManager.setInterruptionFilter().
     *
     * @return true if DND was successfully enabled.
     */
    fun enableDnd(context: Context): Boolean {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            enableDndApi23(context)
        } else {
            enableDndLegacy(context)
        }
    }

    /**
     * Legacy path for API 21-22: write zen_mode via Settings.Global.
     * Requires WRITE_SECURE_SETTINGS.
     */
    private fun enableDndLegacy(context: Context): Boolean {
        return try {
            Settings.Global.putInt(context.contentResolver, ZEN_MODE_KEY, ZEN_MODE_TOTAL_SILENCE)
            Log.i(TAG, "DND enabled via Settings.Global.zen_mode (API 21 path)")
            true
        } catch (e: SecurityException) {
            Log.e(TAG, "WRITE_SECURE_SETTINGS not granted — cannot set zen_mode")
            false
        } catch (e: Exception) {
            Log.e(TAG, "Failed to set zen_mode: ${e.message}")
            false
        }
    }

    /**
     * API 23+ path: use NotificationManager with ACCESS_NOTIFICATION_POLICY.
     */
    @TargetApi(Build.VERSION_CODES.M)
    private fun enableDndApi23(context: Context): Boolean {
        val nm = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        return if (nm.isNotificationPolicyAccessGranted) {
            nm.setInterruptionFilter(NotificationManager.INTERRUPTION_FILTER_NONE)
            Log.i(TAG, "DND enabled via NotificationManager (API 23+ path)")
            true
        } else {
            Log.w(TAG, "Notification policy access not granted — user must allow in Settings")
            false
        }
    }

    /**
     * Disables DND (restores all interruptions).
     */
    fun disableDnd(context: Context): Boolean {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            val nm = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            if (nm.isNotificationPolicyAccessGranted) {
                nm.setInterruptionFilter(NotificationManager.INTERRUPTION_FILTER_ALL)
                true
            } else false
        } else {
            try {
                Settings.Global.putInt(context.contentResolver, ZEN_MODE_KEY, 0)
                true
            } catch (e: Exception) { false }
        }
    }

    /**
     * Returns whether DND (total silence) is currently active.
     */
    fun isDndActive(context: Context): Boolean {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            val nm = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            nm.currentInterruptionFilter == NotificationManager.INTERRUPTION_FILTER_NONE
        } else {
            try {
                Settings.Global.getInt(context.contentResolver, ZEN_MODE_KEY, 0) ==
                    ZEN_MODE_TOTAL_SILENCE
            } catch (e: Exception) { false }
        }
    }

    /**
     * Returns an Intent that opens the notification policy access settings screen.
     * Use this on API 23+ when `isNotificationPolicyAccessGranted` is false.
     */
    @TargetApi(Build.VERSION_CODES.M)
    fun notificationPolicySettingsIntent(): Intent =
        Intent(Settings.ACTION_NOTIFICATION_POLICY_ACCESS_SETTINGS)
}
