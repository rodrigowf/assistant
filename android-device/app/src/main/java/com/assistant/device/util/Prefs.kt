package com.assistant.device.util

import android.content.Context
import android.content.SharedPreferences

/**
 * Centralised SharedPreferences wrapper for all AssistantDevice settings.
 *
 * All feature toggles and state are stored here so every component reads
 * from the same source of truth. No Room, no DataStore — just primitives.
 *
 * Usage:
 *   val prefs = Prefs.get(context)
 *   prefs.edit().putBoolean(Prefs.KEY_WIFI_ADB_ENABLED, true).apply()
 */
object Prefs {

    private const val FILE_NAME = "device_config"

    // ── Feature toggle keys ────────────────────────────────────────────────

    /** Re-enable WiFi ADB (port 5555) on every boot. */
    const val KEY_WIFI_ADB_ENABLED = "wifi_adb_enabled"

    /** Auto-launch com.assistant.peripheral 3 seconds after boot. */
    const val KEY_AUTO_LAUNCH_ENABLED = "auto_launch_enabled"

    /** Attempt to set CPU governor to 'performance' on boot. */
    const val KEY_CPU_GOVERNOR_ENABLED = "cpu_governor_enabled"

    /** Enable Do Not Disturb (total silence) on boot. */
    const val KEY_DND_ENABLED = "dnd_enabled"

    /** Start WatchdogService to keep the assistant app alive. */
    const val KEY_WATCHDOG_ENABLED = "watchdog_enabled"

    // ── State keys ─────────────────────────────────────────────────────────

    /** Epoch millis of the last BOOT_COMPLETED event handled. */
    const val KEY_LAST_BOOT_MS = "last_boot_ms"

    // ── Defaults ───────────────────────────────────────────────────────────

    /** All features enabled by default. */
    val DEFAULTS = mapOf(
        KEY_WIFI_ADB_ENABLED     to true,
        KEY_AUTO_LAUNCH_ENABLED  to true,
        KEY_CPU_GOVERNOR_ENABLED to true,
        KEY_DND_ENABLED          to true,
        KEY_WATCHDOG_ENABLED     to true,
    )

    fun get(context: Context): SharedPreferences =
        context.getSharedPreferences(FILE_NAME, Context.MODE_PRIVATE)
}
