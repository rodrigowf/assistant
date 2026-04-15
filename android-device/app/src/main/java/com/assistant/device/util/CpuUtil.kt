package com.assistant.device.util

import android.util.Log
import java.io.File
import java.io.IOException

/**
 * Utilities for reading and (attempting to) write CPU frequency governor settings.
 *
 * ## Background
 *
 * Android exposes CPU frequency scaling via the Linux cpufreq sysfs interface:
 *   /sys/devices/system/cpu/cpuN/cpufreq/scaling_governor
 *
 * Available governors on the Snapdragon 410 (MSM8916):
 *   - interactive (default): scales up quickly on load, scales down slowly
 *   - performance: always runs at max frequency — best latency, highest power
 *   - powersave: always runs at min frequency — lowest power, worst latency
 *   - ondemand: scales based on utilisation (not available on all kernels)
 *
 * ## Root requirement
 *
 * The sysfs cpufreq nodes are owned by root and are not writable by app processes
 * on non-rooted devices. Writes will fail with IOException. This is expected and
 * handled gracefully — the UI shows "root required" rather than crashing.
 *
 * The feature is still included because:
 *   1. If root is obtained later, it works immediately without code changes.
 *   2. The status UI provides useful info regardless (shows current governor).
 *   3. It documents intent clearly for future device configuration.
 */
object CpuUtil {

    private const val TAG = "CpuUtil"

    /** sysfs path template for the scaling governor of each core. */
    private const val GOVERNOR_PATH = "/sys/devices/system/cpu/cpu%d/cpufreq/scaling_governor"

    /** sysfs path template for available governors on each core. */
    private const val AVAILABLE_GOVERNORS_PATH =
        "/sys/devices/system/cpu/cpu%d/cpufreq/scaling_available_governors"

    /**
     * Attempts to set the scaling governor to 'performance' on all CPU cores.
     *
     * This will fail silently on non-rooted devices — each failed core is logged
     * individually so partial success (e.g. if root is granted for some cores)
     * is visible in logcat.
     *
     * @return Number of cores successfully updated (0 on a non-rooted device).
     */
    fun setPerformanceGovernor(): Int {
        val coreCount = Runtime.getRuntime().availableProcessors()
        var successCount = 0
        for (core in 0 until coreCount) {
            try {
                File(GOVERNOR_PATH.format(core)).writeText("performance")
                Log.i(TAG, "Core $core: governor set to performance")
                successCount++
            } catch (e: IOException) {
                Log.w(TAG, "Core $core: write failed (root required) — ${e.message}")
            }
        }
        return successCount
    }

    /**
     * Reads the current scaling governor for each CPU core.
     *
     * @return List of governor names (one per core), or "n/a" if the sysfs
     *         node cannot be read.
     */
    fun getCurrentGovernors(): List<String> {
        val coreCount = Runtime.getRuntime().availableProcessors()
        return (0 until coreCount).map { core ->
            try {
                File(GOVERNOR_PATH.format(core)).readText().trim()
            } catch (e: IOException) {
                "n/a"
            }
        }
    }

    /**
     * Returns a summary string of the current governors, e.g. "interactive (×4)".
     * Used by the status UI for compact display.
     */
    fun getGovernorSummary(): String {
        val governors = getCurrentGovernors()
        if (governors.isEmpty()) return "n/a"
        val distinct = governors.distinct()
        return if (distinct.size == 1) {
            "${distinct[0]} (×${governors.size})"
        } else {
            governors.joinToString(", ")
        }
    }

    /**
     * Returns available governors for core 0, or empty list if unreadable.
     */
    fun getAvailableGovernors(): List<String> {
        return try {
            File(AVAILABLE_GOVERNORS_PATH.format(0))
                .readText().trim().split(" ").filter { it.isNotBlank() }
        } catch (e: IOException) {
            emptyList()
        }
    }
}
