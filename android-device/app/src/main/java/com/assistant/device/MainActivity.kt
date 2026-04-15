package com.assistant.device

import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.app.Activity
import android.provider.Settings
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.Switch
import android.widget.TextView
import com.assistant.device.service.WatchdogService
import com.assistant.device.util.AdbUtil
import com.assistant.device.util.CpuUtil
import com.assistant.device.util.DndUtil
import com.assistant.device.util.Prefs
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Single-screen status and configuration UI for AssistantDevice.
 *
 * Shows the current state of each feature (WiFi ADB, auto-launch, CPU governor,
 * DND, watchdog) with toggles that immediately apply and persist changes.
 *
 * Also displays:
 *   - ADB connection address (IP:5555)
 *   - Last boot timestamp
 *   - Setup warnings (e.g. WRITE_SECURE_SETTINGS not granted)
 *
 * ## Design philosophy
 *
 * This is a utility app. The UI uses the native Android Material theme (API 21)
 * with no external UI libraries. Plain XML layouts + Activity = minimal overhead
 * on the low-RAM Snapdragon 410.
 */
class MainActivity : Activity() {

    private lateinit var prefs: android.content.SharedPreferences

    // Status views
    private lateinit var tvAdbAddress: TextView
    private lateinit var tvLastBoot: TextView
    private lateinit var vSetupWarning: LinearLayout

    // Feature rows
    private lateinit var switchWifiAdb: Switch
    private lateinit var tvWifiAdbStatus: TextView

    private lateinit var switchAutoLaunch: Switch
    private lateinit var tvAutoLaunchStatus: TextView

    private lateinit var switchCpuGovernor: Switch
    private lateinit var tvCpuGovernorStatus: TextView

    private lateinit var switchDnd: Switch
    private lateinit var tvDndStatus: TextView

    private lateinit var switchWatchdog: Switch
    private lateinit var tvWatchdogStatus: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        prefs = Prefs.get(this)
        bindViews()
        setupListeners()
        // Apply all enabled features immediately on first run (don't wait for a toggle or reboot)
        applyAll()
    }

    override fun onResume() {
        super.onResume()
        updateUi()
    }

    private fun bindViews() {
        tvAdbAddress     = findViewById(R.id.tv_adb_address)
        tvLastBoot       = findViewById(R.id.tv_last_boot)
        vSetupWarning    = findViewById(R.id.tv_setup_warning)

        switchWifiAdb      = findViewById(R.id.switch_wifi_adb)
        tvWifiAdbStatus    = findViewById(R.id.tv_wifi_adb_status)

        switchAutoLaunch   = findViewById(R.id.switch_auto_launch)
        tvAutoLaunchStatus = findViewById(R.id.tv_auto_launch_status)

        switchCpuGovernor    = findViewById(R.id.switch_cpu_governor)
        tvCpuGovernorStatus  = findViewById(R.id.tv_cpu_governor_status)

        switchDnd    = findViewById(R.id.switch_dnd)
        tvDndStatus  = findViewById(R.id.tv_dnd_status)

        switchWatchdog    = findViewById(R.id.switch_watchdog)
        tvWatchdogStatus  = findViewById(R.id.tv_watchdog_status)

        findViewById<Button>(R.id.btn_apply_all).setOnClickListener { applyAll() }
        findViewById<Button>(R.id.btn_set_home).setOnClickListener { promptSetHomeApp() }
    }

    private fun setupListeners() {
        switchWifiAdb.setOnCheckedChangeListener { _, checked ->
            prefs.edit().putBoolean(Prefs.KEY_WIFI_ADB_ENABLED, checked).apply()
            if (checked) AdbUtil.enableWifiAdb(this)
            updateWifiAdbStatus()
        }
        switchAutoLaunch.setOnCheckedChangeListener { _, checked ->
            prefs.edit().putBoolean(Prefs.KEY_AUTO_LAUNCH_ENABLED, checked).apply()
            tvAutoLaunchStatus.text = if (checked) "Will launch on next boot" else "Disabled"
        }
        switchCpuGovernor.setOnCheckedChangeListener { _, checked ->
            prefs.edit().putBoolean(Prefs.KEY_CPU_GOVERNOR_ENABLED, checked).apply()
            if (checked) CpuUtil.setPerformanceGovernor()
            updateCpuStatus()
        }
        switchDnd.setOnCheckedChangeListener { _, checked ->
            prefs.edit().putBoolean(Prefs.KEY_DND_ENABLED, checked).apply()
            if (checked) DndUtil.enableDnd(this) else DndUtil.disableDnd(this)
            updateDndStatus()
        }
        switchWatchdog.setOnCheckedChangeListener { _, checked ->
            prefs.edit().putBoolean(Prefs.KEY_WATCHDOG_ENABLED, checked).apply()
            if (checked) {
                startService(Intent(this, WatchdogService::class.java))
            } else {
                stopService(Intent(this, WatchdogService::class.java))
            }
            tvWatchdogStatus.text = if (checked) "Running" else "Stopped"
        }
    }

    /** Refreshes all UI state from current system state. Called in onResume. */
    private fun updateUi() {
        // Setup warning
        val hasPermission = AdbUtil.hasSecureSettingsPermission(this)
        vSetupWarning.visibility = if (hasPermission) View.GONE else View.VISIBLE

        // Connection info
        tvAdbAddress.text = "ADB: ${AdbUtil.getAdbAddress(this)}"

        // Last boot
        val bootMs = prefs.getLong(Prefs.KEY_LAST_BOOT_MS, 0L)
        tvLastBoot.text = if (bootMs == 0L) {
            "Last boot: unknown (app not yet rebooted)"
        } else {
            val fmt = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.getDefault())
            "Last boot: ${fmt.format(Date(bootMs))}"
        }

        // Sync toggles without triggering listeners by temporarily removing them.
        // We set isChecked directly — on API 21, Switch does not call its listener
        // when programmatically changed, but to be safe we do the full refresh
        // pattern: remove listener → set → re-add listener.
        switchWifiAdb.setOnCheckedChangeListener(null)
        switchAutoLaunch.setOnCheckedChangeListener(null)
        switchCpuGovernor.setOnCheckedChangeListener(null)
        switchDnd.setOnCheckedChangeListener(null)
        switchWatchdog.setOnCheckedChangeListener(null)

        switchWifiAdb.isChecked     = prefs.getBoolean(Prefs.KEY_WIFI_ADB_ENABLED, true)
        switchAutoLaunch.isChecked  = prefs.getBoolean(Prefs.KEY_AUTO_LAUNCH_ENABLED, true)
        switchCpuGovernor.isChecked = prefs.getBoolean(Prefs.KEY_CPU_GOVERNOR_ENABLED, true)
        switchDnd.isChecked         = prefs.getBoolean(Prefs.KEY_DND_ENABLED, true)
        switchWatchdog.isChecked    = prefs.getBoolean(Prefs.KEY_WATCHDOG_ENABLED, true)

        // Re-attach listeners after setting values
        setupListeners()

        updateWifiAdbStatus()
        updateCpuStatus()
        updateDndStatus()
        tvAutoLaunchStatus.text =
            if (switchAutoLaunch.isChecked) "Will launch on next boot" else "Disabled"
        tvWatchdogStatus.text =
            if (switchWatchdog.isChecked) "Running" else "Stopped"
    }

    private fun updateWifiAdbStatus() {
        val addr = AdbUtil.getAdbAddress(this)
        tvWifiAdbStatus.text = if (switchWifiAdb.isChecked) addr else "Disabled"
    }

    private fun updateCpuStatus() {
        val summary = CpuUtil.getGovernorSummary()
        tvCpuGovernorStatus.text = if (switchCpuGovernor.isChecked) {
            "Current: $summary"
        } else {
            "Disabled"
        }
    }

    private fun updateDndStatus() {
        val active = DndUtil.isDndActive(this)
        tvDndStatus.text = when {
            !switchDnd.isChecked -> "Disabled"
            active               -> "Active (total silence)"
            else                 -> "Inactive (will apply on next boot)"
        }
    }

    /** Applies all enabled features immediately (not just on next boot). */
    private fun applyAll() {
        if (prefs.getBoolean(Prefs.KEY_WIFI_ADB_ENABLED, true)) AdbUtil.enableWifiAdb(this)
        if (prefs.getBoolean(Prefs.KEY_CPU_GOVERNOR_ENABLED, true)) CpuUtil.setPerformanceGovernor()
        if (prefs.getBoolean(Prefs.KEY_DND_ENABLED, true)) DndUtil.enableDnd(this)
        if (prefs.getBoolean(Prefs.KEY_WATCHDOG_ENABLED, true)) {
            startService(Intent(this, WatchdogService::class.java))
        }
        updateUi()
    }

    /**
     * Shows the Android "Choose default home" dialog, allowing the user to
     * set com.assistant.peripheral as the default launcher.
     *
     * This is the only way to set a default home app without root or Device Owner.
     * The user must select the assistant app from the dialog.
     */
    private fun promptSetHomeApp() {
        // Clearing our own preferred activities forces the system chooser to appear
        // even if a home app is already set. The user picks the assistant app from
        // the "Which app would you like to use?" dialog.
        packageManager.clearPackagePreferredActivities(packageName)

        val intent = Intent(Intent.ACTION_MAIN).apply {
            addCategory(Intent.CATEGORY_HOME)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        startActivity(intent)
    }
}
