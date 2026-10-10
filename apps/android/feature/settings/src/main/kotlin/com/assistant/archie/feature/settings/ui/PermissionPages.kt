package com.assistant.archie.feature.settings.ui

import android.content.Intent
import android.provider.Settings
import androidx.compose.foundation.layout.padding
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.assistant.archie.feature.settings.AppPermission
import com.assistant.archie.feature.settings.PermissionStatus
import com.assistant.archie.feature.settings.ReliabilityItem
import com.assistant.archie.feature.settings.SettingsFeature
import com.assistant.core.design.components.ArchieButton
import com.assistant.core.design.components.ButtonSize
import com.assistant.core.design.components.ButtonStyle
import com.assistant.core.design.components.SettingsRow
import com.assistant.core.design.icons.ArchieIcon
import com.assistant.core.design.icons.ArchieIcons
import com.assistant.core.design.theme.ArchieTheme

internal fun AppPermission.icon() = when (this) {
    AppPermission.MICROPHONE -> ArchieIcons.Mic
    AppPermission.NOTIFICATIONS -> SettingsIcons.Notifications
    AppPermission.NEARBY_DEVICES -> SettingsIcons.Bluetooth
}

internal fun AppPermission.usedFor() = when (this) {
    AppPermission.MICROPHONE -> "Voice, voice messages and the wake word"
    AppPermission.NOTIFICATIONS -> "Finished agent sessions, approval requests and background listening"
    AppPermission.NEARBY_DEVICES -> "Bluetooth headset output"
}

internal fun PermissionStatus.label(p: AppPermission) = when (this) {
    PermissionStatus.GRANTED -> "Allowed"
    PermissionStatus.DENIED -> "Not allowed · tap to allow"
    PermissionStatus.BLOCKED -> "Blocked · open app settings"
    PermissionStatus.NOT_REQUIRED -> "Not needed on this Android version"
}

/**
 * PermissionCenter (spec 14 §2.9): Settings → This device → Permissions, with the live status of
 * every runtime permission. Each is still asked in context; this page is where a denial is visible
 * and fixable (fixes "denials are silent", inv03 §1.1).
 */
@Composable
internal fun PermissionsPage(feature: SettingsFeature, onBack: (() -> Unit)?, open: (SettingsPageKey) -> Unit) {
    val st by feature.permissions.state.collectAsStateWithLifecycle()
    val ask = rememberPermissionAsk(feature)
    SettingsPageFrame("Permissions", feature.messages, onBack, scope = ScopeLabel.device(feature.platform.deviceName)) {
        Section(null) {
            for (p in AppPermission.entries) {
                val s = st.status(p)
                SettingsRow(
                    title = p.title,
                    icon = p.icon(),
                    value = "${s.label(p)} · ${p.usedFor()}",
                    onClick = if (s == PermissionStatus.GRANTED || s == PermissionStatus.NOT_REQUIRED) null else ({ ask.ask(p) }),
                    trailing = { StatusIcon(s == PermissionStatus.GRANTED || s == PermissionStatus.NOT_REQUIRED) },
                    modifier = Modifier.testTag("perm:${p.name}"),
                )
            }
            SettingsRow(
                title = "Battery",
                icon = SettingsIcons.BatteryFull,
                value = if (st.ignoringBatteryOptimizations) "Unrestricted" else "Optimised · may stop the wake word",
                onClick = { open(SettingsPageKey.BACKGROUND) },
            )
        }
        HelpLine("Archie asks for each permission when a feature needs it.", modifier = Modifier.padding(start = 4.dp))
    }
}

@Composable
private fun StatusIcon(ok: Boolean?) {
    val c = ArchieTheme.colors
    when (ok) {
        true -> ArchieIcon(ArchieIcons.CheckCircleFilled, "Done", tint = c.primary)
        false -> ArchieIcon(ArchieIcons.Warning, "Needed", tint = ArchieTheme.extended.warning.color)
        null -> ArchieIcon(ArchieIcons.Help, "Check it yourself", tint = c.onSurfaceVariant)
    }
}

/**
 * Background reliability (spec 14 §2.6, risk X10): HyperOS and other skins kill background apps,
 * which stops the wake word. A checklist with live status and a fix for each item.
 */
@Composable
internal fun BackgroundReliabilityPage(feature: SettingsFeature, onBack: (() -> Unit)?) {
    val st by feature.permissions.state.collectAsStateWithLifecycle()
    val ask = rememberPermissionAsk(feature)
    val context = LocalContext.current
    val platform = feature.platform
    SettingsPageFrame("Background reliability", feature.messages, onBack, scope = ScopeLabel.device(platform.deviceName)) {
        HelpLine(
            "Android may stop Archie in the background, which stops the wake word. ${st.checklistSummary}.",
            info = "Phones with HyperOS / MIUI (Xiaomi, Redmi, POCO) stop background apps aggressively. These steps keep Archie's listening service alive.",
            modifier = Modifier.padding(start = 4.dp, bottom = 12.dp),
        )
        Section(null) {
            for ((item, done) in st.checklist) {
                when (item) {
                    ReliabilityItem.MICROPHONE -> CheckRow(
                        "Microphone", ArchieIcons.Mic, done,
                        if (done == true) "Allowed" else "Needed for the wake word",
                        "Allow",
                    ) { ask.ask(AppPermission.MICROPHONE) }
                    ReliabilityItem.BATTERY -> CheckRow(
                        "Battery: Unrestricted", SettingsIcons.BatteryFull, done,
                        if (done == true) "Archie is not battery-optimised" else "Battery optimisation can stop Archie",
                        "Allow",
                    ) { context.launch(platform.batteryOptimizationIntent(), Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS), platform.appDetailsIntent()) }
                    ReliabilityItem.AUTOSTART -> CheckRow(
                        "Autostart", ArchieIcons.RestartAlt, null,
                        "Turn on Autostart for Archie (can't be checked from here)",
                        "Open",
                    ) { context.launch(*platform.autostartIntents().toTypedArray()) }
                    ReliabilityItem.NOTIFICATIONS -> CheckRow(
                        "Notifications", SettingsIcons.Notifications, done,
                        if (done == true) "The listening notification can show" else "Without them the listening notification is hidden",
                        "Allow",
                    ) {
                        if (!st.granted(AppPermission.NOTIFICATIONS)) ask.ask(AppPermission.NOTIFICATIONS)
                        else context.launch(platform.notificationSettingsIntent())
                    }
                    ReliabilityItem.RECENTS_LOCK -> CheckRow(
                        "Lock Archie in Recents", SettingsIcons.Lock, null,
                        "Open Recents, long-press Archie and tap the lock",
                        null,
                    ) { }
                }
            }
        }
    }
}

@Composable
private fun CheckRow(title: String, icon: androidx.compose.ui.graphics.vector.ImageVector, done: Boolean?, detail: String, action: String?, onAction: () -> Unit) {
    SettingsRow(
        title = title,
        icon = icon,
        value = detail,
        modifier = Modifier.testTag("check:$title"),
        trailing = {
            if (done != true && action != null) {
                ArchieButton(action, onAction, style = ButtonStyle.Tonal, size = ButtonSize.Small)
            } else {
                StatusIcon(done)
            }
        },
    )
}
