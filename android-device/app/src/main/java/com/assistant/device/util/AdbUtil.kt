package com.assistant.device.util

import android.content.Context
import android.net.wifi.WifiManager
import android.os.Build
import android.provider.Settings
import android.util.Log

/**
 * Utilities for enabling and querying WiFi ADB (TCP ADB on port 5555).
 *
 * ## How WiFi ADB works on a non-rooted Android 5.0 device
 *
 * Android exposes ADB-over-TCP via two mechanisms:
 *   1. `Settings.Global.ADB_ENABLED` — master ADB toggle.
 *   2. `service.adb.tcp.port` system property — tells adbd which port to listen on.
 *      Setting this to 5555 enables TCP mode; -1 disables it.
 *
 * From an app process (uid ~10xxx), `Runtime.exec("setprop ...")` will be
 * silently rejected by Android's property service for `service.*` namespaced
 * properties. Therefore WiFi ADB is re-enabled entirely via Settings writes,
 * which DO work when WRITE_SECURE_SETTINGS has been granted via:
 *
 *     adb shell pm grant com.assistant.device android.permission.WRITE_SECURE_SETTINGS
 *
 * adbd picks up the port change the next time it restarts (which happens on
 * each boot, making the BootReceiver the right place to call this).
 *
 * ## Setup (one-time, via USB)
 * ```
 * adb shell pm grant com.assistant.device android.permission.WRITE_SECURE_SETTINGS
 * ```
 * This survives app updates but not reinstalls.
 */
object AdbUtil {

    private const val TAG = "AdbUtil"
    private const val ADB_TCP_PORT = 5555

    // Undocumented Settings.Secure key used by AOSP and Samsung to store the ADB TCP port.
    private const val SECURE_KEY_ADB_PORT = "adb_port"

    /**
     * Re-enables WiFi ADB on port 5555.
     *
     * Writes to Settings.Global (ADB enabled) and Settings.Secure (TCP port).
     * Requires WRITE_SECURE_SETTINGS, granted via `adb shell pm grant`.
     *
     * @return true if writes succeeded; false if the permission was not granted.
     */
    fun enableWifiAdb(context: Context): Boolean {
        return try {
            val resolver = context.contentResolver

            // Ensure the master ADB toggle is on
            Settings.Global.putInt(resolver, Settings.Global.ADB_ENABLED, 1)

            // Set the TCP port. adbd reads this at startup.
            Settings.Secure.putString(resolver, SECURE_KEY_ADB_PORT, ADB_TCP_PORT.toString())

            // On some Samsung 5.0 builds, adb_wifi_enabled is a separate key
            Settings.Secure.putString(resolver, "adb_wifi_enabled", "1")

            Log.i(TAG, "WiFi ADB settings written (port $ADB_TCP_PORT)")
            true
        } catch (e: SecurityException) {
            Log.e(TAG, "WRITE_SECURE_SETTINGS not granted — run: " +
                "adb shell pm grant com.assistant.device android.permission.WRITE_SECURE_SETTINGS")
            false
        } catch (e: Exception) {
            Log.e(TAG, "Failed to enable WiFi ADB: ${e.message}")
            false
        }
    }

    /**
     * Returns the device's current WiFi IP address, or null if not connected.
     *
     * Uses the deprecated WifiManager.getConnectionInfo() which is available
     * on API 21. The replacement (LinkProperties) requires API 29+.
     */
    @Suppress("DEPRECATION")
    fun getWifiIpAddress(context: Context): String? {
        val wm = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
        val ip = wm.connectionInfo?.ipAddress ?: return null
        if (ip == 0) return null
        // IP is stored little-endian in an int
        return "%d.%d.%d.%d".format(
            ip and 0xff,
            (ip shr 8) and 0xff,
            (ip shr 16) and 0xff,
            (ip shr 24) and 0xff
        )
    }

    /**
     * Returns the full ADB connection string (IP:port) or a human-readable
     * status message if WiFi is not available.
     */
    fun getAdbAddress(context: Context): String {
        val ip = getWifiIpAddress(context) ?: return "WiFi not connected"
        return "$ip:$ADB_TCP_PORT"
    }

    /**
     * Checks whether WRITE_SECURE_SETTINGS has been granted to this app.
     * Used by MainActivity to display a warning banner if setup is incomplete.
     */
    fun hasSecureSettingsPermission(context: Context): Boolean {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            context.checkSelfPermission(android.Manifest.permission.WRITE_SECURE_SETTINGS) ==
                android.content.pm.PackageManager.PERMISSION_GRANTED
        } else {
            // On API 21-22, checkSelfPermission always returns GRANTED for declared permissions.
            // The real test is whether the Settings write succeeds, which we find out at runtime.
            true
        }
    }
}
