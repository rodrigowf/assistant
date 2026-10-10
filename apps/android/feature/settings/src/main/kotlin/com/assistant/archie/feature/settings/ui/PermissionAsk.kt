package com.assistant.archie.feature.settings.ui

import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.Context
import android.content.ContextWrapper
import android.content.Intent
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.Stable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.window.Dialog
import com.assistant.archie.feature.settings.AppPermission
import com.assistant.archie.feature.settings.PermissionStatus
import com.assistant.archie.feature.settings.SettingsFeature
import com.assistant.core.design.components.ArchieButton
import com.assistant.core.design.components.ArchieDialogSurface
import com.assistant.core.design.components.ButtonStyle

/** One-screen rationale copy (spec 14 §2.9). */
internal fun rationale(p: AppPermission): Pair<String, String> = when (p) {
    AppPermission.MICROPHONE -> "Allow the microphone" to
        "Archie listens for your wake phrases and hears you in voice conversations. Audio goes only to your Archie server, and only while you talk."
    AppPermission.NOTIFICATIONS -> "Allow notifications" to
        "While Archie listens in the background, Android shows a notification. It carries Pause, Resume and Talk, approval requests from agents, and a note when an agent session finishes."
    AppPermission.NEARBY_DEVICES -> "Allow Nearby devices" to
        "Android needs this to send Archie's voice to a Bluetooth headset."
}

/**
 * Asks for a permission in context: rationale first, then the system dialog; a blocked
 * permission ("Don't ask again") offers app settings instead. Denials are recorded in
 * PermissionCenter, so the rows show them (fixes "denials are silent", inv03 §1.1).
 */
@Stable
internal class PermissionAsk(private val start: (AppPermission, () -> Unit) -> Unit) {
    fun ask(p: AppPermission, onGranted: () -> Unit = {}) = start(p, onGranted)
}

@Composable
internal fun rememberPermissionAsk(feature: SettingsFeature): PermissionAsk {
    val context = LocalContext.current
    var pending by remember { mutableStateOf<AppPermission?>(null) }
    var then by remember { mutableStateOf<(() -> Unit)?>(null) }
    var inFlight by remember { mutableStateOf<AppPermission?>(null) }
    val launcher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        val p = inFlight ?: return@rememberLauncherForActivityResult
        inFlight = null
        val activity = context.findActivity()
        val canAskAgain = activity?.shouldShowRequestPermissionRationale(p.manifest) ?: true
        feature.permissions.onResult(p, granted, canAskAgain)
        if (granted) then?.invoke()
        then = null
    }
    val ask = remember(feature) {
        PermissionAsk { p, onGranted ->
            when (feature.permissions.status(p)) {
                PermissionStatus.GRANTED, PermissionStatus.NOT_REQUIRED -> onGranted()
                else -> { pending = p; then = onGranted }
            }
        }
    }
    pending?.let { p ->
        val blocked = feature.permissions.status(p) == PermissionStatus.BLOCKED
        val (title, body) = rationale(p)
        Dialog(onDismissRequest = { pending = null; then = null }) {
            ArchieDialogSurface(
                title,
                modifier = Modifier.testTag("rationale:${p.name}"),
                body = { Text(if (blocked) "$body\n\nIt was turned off for Archie. Allow it in app settings → Permissions." else body) },
            ) {
                ArchieButton("Not now", { pending = null; then = null }, style = ButtonStyle.Text)
                if (blocked) {
                    ArchieButton("Open app settings", {
                        pending = null
                        context.launch(feature.platform.appDetailsIntent())
                    }, style = ButtonStyle.Text)
                } else {
                    ArchieButton("Continue", {
                        pending = null
                        inFlight = p
                        launcher.launch(p.manifest)
                    }, style = ButtonStyle.Text, modifier = Modifier.testTag("rationale-continue"))
                }
            }
        }
    }
    return ask
}

internal tailrec fun Context.findActivity(): Activity? = when (this) {
    is Activity -> this
    is ContextWrapper -> baseContext.findActivity()
    else -> null
}

/** Starts the first intent that resolves (fallback chains such as Xiaomi Autostart → app details). */
internal fun Context.launch(vararg intents: Intent): Boolean {
    for (i in intents) {
        val intent = Intent(i)
        if (findActivity() == null) intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        try {
            startActivity(intent)
            return true
        } catch (_: ActivityNotFoundException) {
            // try the next
        } catch (_: SecurityException) {
            // try the next
        }
    }
    return false
}
