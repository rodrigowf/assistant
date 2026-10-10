package com.assistant.archie.feature.settings.ui

import android.os.Build
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.background
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.assistant.archie.feature.settings.AppPermission
import com.assistant.archie.feature.settings.ConnectionModel
import com.assistant.archie.feature.settings.Format
import com.assistant.archie.feature.settings.NotifyLogic
import com.assistant.archie.feature.settings.Ranges
import com.assistant.archie.feature.settings.ServerRow
import com.assistant.archie.feature.settings.SettingsFeature
import com.assistant.archie.feature.settings.SliderSpec
import com.assistant.archie.feature.settings.TextSize
import com.assistant.archie.feature.settings.TrustRequest
import com.assistant.archie.feature.settings.phrases
import com.assistant.core.data.ConnectionStatus
import com.assistant.core.design.components.ArchieButton
import com.assistant.core.design.components.ArchieDialogSurface
import com.assistant.core.design.components.ArchieIconButton
import com.assistant.core.design.components.ArchieTextField
import com.assistant.core.design.components.ButtonSize
import com.assistant.core.design.components.ButtonStyle
import com.assistant.core.design.components.DotTone
import com.assistant.core.design.components.LevelSlider
import com.assistant.core.design.components.SegmentOption
import com.assistant.core.design.components.SegmentedChoice
import com.assistant.core.design.components.SettingsFieldSet
import com.assistant.core.design.components.SettingsRow
import com.assistant.core.design.components.Spinner
import com.assistant.core.design.components.StatusDot
import com.assistant.core.design.icons.ArchieIcon
import com.assistant.core.design.icons.ArchieIcons
import com.assistant.core.design.theme.ArchieTheme
import com.assistant.core.model.AudioOutput
import com.assistant.core.model.ThemeMode
import java.text.DateFormat
import java.util.Date
import kotlin.math.abs

// ───────────────────────────── Connection ─────────────────────────────

@Composable
internal fun ConnectionPage(feature: SettingsFeature, onBack: (() -> Unit)?) {
    val st by feature.connection.state.collectAsStateWithLifecycle()
    val ask = rememberPermissionAsk(feature)
    var editing by rememberSaveable { mutableStateOf<String?>(null) } // url, or "" for a new server
    var removing by remember { mutableStateOf<ServerRow?>(null) }
    val status = st.status
    SettingsPageFrame("Connection", feature.messages, onBack, scope = ScopeLabel.device(feature.platform.deviceName)) {
        FieldBlock(Modifier.testTag("connection-status")) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                StatusDot(tone = status.tone(), size = 10.dp)
                Column(Modifier.weight(1f)) {
                    val label = status.serverLabel.ifEmpty { "No server" }
                    Text(
                        when (status.phase) {
                            ConnectionStatus.Phase.CONNECTED -> "Connected to $label"
                            ConnectionStatus.Phase.CONNECTING -> "Connecting to $label…"
                            ConnectionStatus.Phase.RECONNECTING -> "Reconnecting to $label…"
                            ConnectionStatus.Phase.OFFLINE -> "Not connected"
                        },
                        style = ArchieTheme.typography.titleMedium.copy(letterSpacing = 0.sp),
                        color = ArchieTheme.colors.onSurface,
                    )
                    Text(status.serverUrl.orEmpty(), style = ArchieTheme.typography.bodySmall, color = ArchieTheme.colors.onSurfaceVariant)
                }
                if (status.phase == ConnectionStatus.Phase.OFFLINE) {
                    ArchieButton("Connect", feature.connection::connect, style = ButtonStyle.Tonal, size = ButtonSize.Small, icon = ArchieIcons.Sync)
                } else {
                    ArchieButton("Disconnect", feature.connection::disconnect, style = ButtonStyle.Text, size = ButtonSize.Small)
                }
            }
        }
        Spacer(Modifier.height(16.dp))
        Section("Servers") {
            for (row in st.rows) {
                ServerRowItem(
                    row,
                    switching = st.switching == row.url,
                    onSelect = { feature.connection.select(row) },
                    onEdit = { editing = row.url },
                    onRemove = { removing = row },
                    onSave = {
                        status.discovered.firstOrNull { it.serverUrl == row.url }?.let(feature.connection::saveDiscovered)
                    },
                )
            }
        }
        Row(Modifier.fillMaxWidth().padding(bottom = 4.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            if (status.scanning) {
                ArchieButton("Scanning…", {}, style = ButtonStyle.Tonal, enabled = false, icon = ArchieIcons.NetworkCheck)
            } else {
                ArchieButton("Scan network", feature.connection::scan, style = ButtonStyle.Tonal, icon = ArchieIcons.NetworkCheck, modifier = Modifier.testTag("scan"))
            }
            ArchieButton("Add server", { editing = "" }, style = ButtonStyle.Outlined, icon = ArchieIcons.Add, modifier = Modifier.testTag("add-server"))
        }
        HelpLine("Tap a server to switch to it. Scanning finds Archie servers on this Wi-Fi network.", modifier = Modifier.padding(start = 4.dp, bottom = 16.dp))
        val s = st.settings
        if (s != null) {
            Section(null) {
                ToggleField("Auto-connect", s.autoConnect, feature.device::setAutoConnect, help = "Connect when Archie opens.")
                ToggleField(
                    "Stay connected in background",
                    s.stayConnectedInBackground,
                    { on ->
                        if (on) ask.ask(AppPermission.NOTIFICATIONS) { feature.device.setStayConnected(true) }
                        else feature.device.setStayConnected(false)
                    },
                    help = "Keeps the link for approval and agent notifications.",
                    info = "Archie keeps its server connection while it is in the background, so agent approval requests and “agent session finished” notifications reach this phone. Uses a little more battery. Off by default.",
                    testTag = "stay-connected",
                )
            }
        }
    }
    editing?.let { url ->
        val existing = st.rows.firstOrNull { it.url == url }
        ServerEditorDialog(
            existing,
            onDismiss = { editing = null },
            onSave = { label, address -> feature.connection.save(label, address, replacing = existing?.takeIf { it.saved }?.url).also { if (it == null) editing = null } },
        )
    }
    removing?.let { row ->
        com.assistant.core.design.components.ArchieConfirmDialog(
            title = "Remove ${row.label}?",
            text = "It leaves the saved list on this device. ${if (row.current) "Archie stays connected until you pick another server." else ""}".trim(),
            confirmLabel = "Remove",
            destructive = true,
            onConfirm = { feature.connection.remove(row); removing = null },
            onDismissRequest = { removing = null },
        )
    }
    st.trust?.let { ServerTrustDialog(it, onTrust = feature.connection::trustAndSwitch, onCancel = feature.connection::cancelTrust) }
}

internal fun ConnectionStatus.tone(): DotTone = when (phase) {
    ConnectionStatus.Phase.CONNECTED -> DotTone.Success
    ConnectionStatus.Phase.OFFLINE -> DotTone.Error
    else -> DotTone.Warning
}

@Composable
private fun ServerRowItem(row: ServerRow, switching: Boolean, onSelect: () -> Unit, onEdit: () -> Unit, onRemove: () -> Unit, onSave: () -> Unit) {
    val c = ArchieTheme.colors
    Row(
        Modifier
            .fillMaxWidth()
            .heightIn(min = 64.dp)
            .clip(RoundedCornerShape(4.dp))
            .background(if (row.current) c.secondaryContainer else c.surfaceContainer)
            .clickable(role = Role.RadioButton, onClickLabel = if (row.current) "Reconnect" else "Switch to ${row.label}", onClick = onSelect)
            .testTag("server:${row.url}")
            .padding(start = 16.dp, end = 4.dp, top = 8.dp, bottom = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        val fg = if (row.current) c.onSecondaryContainer else c.onSurface
        ArchieIcon(if (row.current) ArchieIcons.CheckCircleFilled else if (row.discovered && !row.saved) ArchieIcons.Wifi else ArchieIcons.Dns, null, tint = if (row.current) c.primary else c.onSurfaceVariant)
        Column(Modifier.weight(1f)) {
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.CenterVertically) {
                Text(row.label, style = ArchieTheme.typography.bodyLarge.copy(letterSpacing = 0.sp), color = fg, maxLines = 1)
                if (row.current) Badge("Current", active = true)
                if (row.needsTrust) Badge("TLS")
            }
            Text(row.detail, style = ArchieTheme.typography.bodySmall.copy(letterSpacing = 0.sp), color = c.onSurfaceVariant, maxLines = 1)
        }
        if (switching) Spinner(Modifier.padding(end = 12.dp), size = 18.dp)
        if (row.saved) {
            ArchieIconButton(ArchieIcons.Edit, "Edit ${row.label}", onEdit, size = 40.dp, iconSize = 20.dp)
            ArchieIconButton(ArchieIcons.Delete, "Remove ${row.label}", onRemove, size = 40.dp, iconSize = 20.dp)
        } else if (row.discovered) {
            ArchieIconButton(ArchieIcons.Add, "Save ${row.label}", onSave, size = 40.dp, iconSize = 20.dp)
        } else {
            ArchieIconButton(ArchieIcons.Add, "Save ${row.label}", onEdit, size = 40.dp, iconSize = 20.dp)
        }
    }
}

/** Add / edit a saved server (old `ServerEditorDialog`): label + address. */
@Composable
internal fun ServerEditorDialog(existing: ServerRow?, onDismiss: () -> Unit, onSave: (label: String, url: String) -> String?) {
    var label by rememberSaveable { mutableStateOf(existing?.label.orEmpty()) }
    var url by rememberSaveable { mutableStateOf(existing?.url.orEmpty()) }
    var error by rememberSaveable { mutableStateOf<String?>(null) }
    val editingSaved = existing?.saved == true
    Dialog(onDismissRequest = onDismiss) {
        ArchieDialogSurface(if (editingSaved) "Edit server" else "Add server", modifier = Modifier.testTag("server-editor"), body = {
            Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                ArchieTextField(label, { label = it }, "Name", Modifier.fillMaxWidth().testTag("server-label"), placeholder = "jetson", supportingText = "Optional")
                ArchieTextField(
                    url, { url = it; error = null }, "Address", Modifier.fillMaxWidth().testTag("server-url"),
                    placeholder = "ws://192.168.0.200:80",
                    errorText = error,
                    supportingText = "ws://, wss://, http:// or https://",
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri, imeAction = ImeAction.Done),
                )
            }
        }) {
            ArchieButton("Cancel", onDismiss, style = ButtonStyle.Text)
            ArchieButton(if (editingSaved) "Save" else "Add", { error = onSave(label, url) }, style = ButtonStyle.Text, enabled = url.isNotBlank(), modifier = Modifier.testTag("server-save"))
        }
    }
}

/** TOFU (spec 14 §4.3): the server's certificate is unknown, or changed since it was trusted. */
@Composable
internal fun ServerTrustDialog(t: TrustRequest, onTrust: () -> Unit, onCancel: () -> Unit) {
    val c = ArchieTheme.colors
    val until = DateFormat.getDateInstance(DateFormat.MEDIUM).format(Date(t.certificate.notAfterMillis))
    Dialog(onDismissRequest = onCancel) {
        ArchieDialogSurface(if (t.changed) "The certificate changed" else "Trust this server?", modifier = Modifier.testTag("trust-dialog"), body = {
            Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text(
                    if (t.changed) "${t.hostPort} now presents a different certificate than the one you trusted. Only continue if you replaced it yourself."
                    else "${t.hostPort} uses a certificate this phone doesn't know. Only trust it if this is your own Archie server.",
                    color = if (t.changed) c.error else c.onSurfaceVariant,
                )
                KeyValues(
                    listOf(
                        "Subject" to t.certificate.subject,
                        "Issuer" to t.certificate.issuer,
                        "Valid until" to until,
                    ),
                )
                Text("SHA-256 key fingerprint", style = ArchieTheme.typography.labelMedium, color = c.onSurfaceVariant)
                Text(t.certificate.spkiSha256Hex, style = ArchieTheme.text.codeSmall, color = c.onSurface)
            }
        }) {
            ArchieButton("Cancel", onCancel, style = ButtonStyle.Text)
            ArchieButton(if (t.changed) "Trust new certificate" else "Trust", onTrust, style = ButtonStyle.Text, modifier = Modifier.testTag("trust-confirm"))
        }
    }
}

// ───────────────────────────── Audio ─────────────────────────────

/** A [SettingsFieldSet] + slider holding a local value while dragging; commits once on release. */
@Composable
internal fun CommitSlider(
    title: String,
    saved: Float,
    spec: SliderSpec,
    format: (Float) -> String,
    help: String?,
    onCommit: (Float) -> Unit,
    info: String? = null,
    enabled: Boolean = true,
    testTag: String? = null,
) {
    var live by remember(saved) { mutableFloatStateOf(saved.coerceIn(spec.min, spec.max)) }
    SettingsFieldSet(title, if (testTag != null) Modifier.testTag(testTag) else Modifier, format(live), null) {
        LevelSlider(
            value = live,
            onValueChange = { live = spec.snap(it) },
            valueRange = spec.range,
            steps = spec.steps,
            onValueChangeFinished = { if (abs(live - saved) > spec.step / 1000) onCommit(live) },
            valueLabel = format,
            enabled = enabled,
        )
        if (help != null) HelpLine(help, info)
    }
}

@Composable
internal fun AudioPage(feature: SettingsFeature, onBack: (() -> Unit)?) {
    val s by feature.device.settings.collectAsStateWithLifecycle()
    val speaker by feature.device.speakerLevel.collectAsStateWithLifecycle()
    val outputs by remember(feature) { feature.platform.availableOutputs() }.collectAsStateWithLifecycle(setOf(AudioOutput.AUTO, AudioOutput.LOUDSPEAKER, AudioOutput.EARPIECE))
    val perms by feature.permissions.state.collectAsStateWithLifecycle()
    val ask = rememberPermissionAsk(feature)
    SettingsPageFrame("Audio", feature.messages, onBack, scope = ScopeLabel.device(feature.platform.deviceName)) {
        val d = s ?: return@SettingsPageFrame LoadingBody("Loading…")
        com.assistant.core.design.components.SettingsGroup {
            CommitSlider(
                "Microphone level", d.micGainLevel, Ranges.LEVEL, Format::percent,
                help = "Raise it if Archie misses quiet speech. 0 to 150%.",
                onCommit = feature.device::setMicGain, testTag = "mic-level",
            )
            if (speaker != null) {
                CommitSlider(
                    "Speaker level", speaker!!, Ranges.SPEAKER, Format::percent,
                    help = "Archie's voice volume on this device.",
                    info = "This is Android's call volume, which Archie's voice plays on. The volume keys change it too during a conversation.",
                    onCommit = feature.device::setSpeakerLevel, testTag = "speaker-level",
                )
            }
            CommitSlider(
                "Echo ducking", d.echoDuckingGain, Ranges.DUCK, Format::duckPercent,
                help = "Mic level while Archie speaks. Lower cuts echo.",
                info = "While Archie talks, the microphone drops to this level instead of muting, so you can still interrupt. 0 to 10%, default 5%.",
                onCommit = feature.device::setEchoDucking, testTag = "echo-ducking",
            )
            OutputRouteField(d.audioOutput, outputs, perms.granted(AppPermission.NEARBY_DEVICES)) { o ->
                if (o == AudioOutput.BLUETOOTH) ask.ask(AppPermission.NEARBY_DEVICES) { feature.device.setAudioOutput(o) }
                else feature.device.setAudioOutput(o)
            }
        }
    }
}

/** The output route as a labeled segmented button (mockup (f); replaces the old unlabeled icon row). */
@Composable
private fun OutputRouteField(selected: AudioOutput, available: Set<AudioOutput>, btAllowed: Boolean, onSelect: (AudioOutput) -> Unit) {
    val order = listOf(AudioOutput.AUTO, AudioOutput.LOUDSPEAKER, AudioOutput.EARPIECE, AudioOutput.BLUETOOTH, AudioOutput.WIRED)
    val shown = order.filter { it in available || it == selected }
    SettingsFieldSet("Output route", Modifier.testTag("output-route")) {
        Spacer(Modifier.height(6.dp))
        SegmentedChoice(
            options = shown.map { o ->
                when (o) {
                    AudioOutput.AUTO -> SegmentOption("Auto", ArchieIcons.BrightnessAuto)
                    AudioOutput.LOUDSPEAKER -> SegmentOption("Speaker", ArchieIcons.VolumeUp)
                    AudioOutput.EARPIECE -> SegmentOption("Earpiece", ArchieIcons.PhoneInTalk)
                    AudioOutput.BLUETOOTH -> SegmentOption("Bluetooth", SettingsIcons.Bluetooth)
                    AudioOutput.WIRED -> SegmentOption("Wired", SettingsIcons.Headphones)
                }
            },
            selectedIndex = shown.indexOf(selected),
            onSelect = { i -> if (shown[i] != selected) onSelect(shown[i]) },
        )
        Spacer(Modifier.height(4.dp))
        HelpLine(
            when {
                AudioOutput.BLUETOOTH in available && !btAllowed && Build.VERSION.SDK_INT >= 31 -> "Bluetooth needs “Nearby devices”; you'll be asked."
                AudioOutput.BLUETOOTH !in available -> "Bluetooth appears when a headset is connected."
                else -> "Auto lets Android pick, as in a phone call."
            },
            info = "Auto follows Android's call routing. Speaker and Earpiece pin the route; Bluetooth uses the headset's call audio.",
        )
    }
}

// ───────────────────────────── Wake word & triggers ─────────────────────────────

@Composable
internal fun WakeWordPage(feature: SettingsFeature, onBack: (() -> Unit)?, open: (SettingsPageKey) -> Unit) {
    val s by feature.device.settings.collectAsStateWithLifecycle()
    val perms by feature.permissions.state.collectAsStateWithLifecycle()
    val wakeStatus by feature.voice.wakeStatus.collectAsStateWithLifecycle()
    val ask = rememberPermissionAsk(feature)
    val context = LocalContext.current
    SettingsPageFrame("Wake word & triggers", feature.messages, onBack, scope = ScopeLabel.device(feature.platform.deviceName)) {
        val d = s ?: return@SettingsPageFrame LoadingBody("Loading…")
        val mic = perms.granted(AppPermission.MICROPHONE)
        if (d.enableWakeWord && !mic) {
            Notice(
                NoticeTone.WARNING, "Microphone permission needed", body = "Wake word is on, but Archie can't use the microphone.",
                icon = ArchieIcons.MicOff,
                actions = { com.assistant.core.design.components.InlineCardAction("Allow", { ask.ask(AppPermission.MICROPHONE) }, primary = true) },
            )
        }
        Section(null) {
            ToggleField(
                "Wake word",
                d.enableWakeWord && mic,
                { on ->
                    if (on) {
                        ask.ask(AppPermission.MICROPHONE) {
                            feature.device.setWakeWordEnabled(true)
                            if (!feature.permissions.state.value.granted(AppPermission.NOTIFICATIONS)) ask.ask(AppPermission.NOTIFICATIONS)
                        }
                    } else {
                        feature.device.setWakeWordEnabled(false)
                    }
                },
                help = when {
                    !mic -> "Needs the microphone permission."
                    wakeStatus != null && d.enableWakeWord -> wakeStatus
                    else -> "Listens on this device for the phrases below."
                },
                info = "A detection is confirmed by a quick cloud transcription check before it fires.",
                testTag = "wake-toggle",
            )
            val enabled = d.enableWakeWord && mic
            PhraseField(
                "Talk phrases", d.talkWord, "my friend, hey assistant",
                "Comma-separated. Any of them sends one voice message.",
                enabled, feature.device::setTalkPhrases, testTag = "talk-phrases",
            )
            PhraseField(
                "Wake phrases", d.wakeWord, "wake up, hey realtime",
                "Comma-separated. Any of them starts a voice conversation.",
                enabled, feature.device::setWakePhrases, testTag = "wake-phrases",
            )
            CommitSlider(
                "Wake word sensitivity", d.wakeWordMicGainLevel, Ranges.LEVEL, Format::percent,
                help = "Higher triggers more easily.",
                info = "Scales the wake word's loudness gate. Independent of the microphone level used in conversations.",
                onCommit = feature.device::setWakeSensitivity, enabled = enabled, testTag = "wake-sensitivity",
            )
            CommitSlider(
                "Talk auto-stop", d.talkSilenceSensitivity, Ranges.TALK_SILENCE, Format::multiplier,
                help = "How clear a pause ends a voice message.",
                info = "A voice message stops after you pause. Lower stops sooner (may clip a soft last word); higher waits for a clearer pause (better in a lively room).",
                onCommit = feature.device::setTalkAutoStop, enabled = enabled, testTag = "talk-auto-stop",
            )
        }
        Section("Triggers") {
            SettingsRow(
                "Assist gesture",
                icon = ArchieIcons.TouchApp,
                value = when (perms.defaultAssistant) {
                    true -> "Archie is the default assistant"
                    false -> "Set Archie as the default digital assistant"
                    null -> "Choose Archie as the default digital assistant"
                },
                onClick = { context.launch(feature.platform.assistantSettingsIntent()) },
            )
            SettingsRow(
                "Notification actions",
                icon = SettingsIcons.Notifications,
                value = if (perms.granted(AppPermission.NOTIFICATIONS) && perms.notificationsEnabled) "Pause, Resume and Talk from the notification"
                else "Notifications are off for Archie",
                onClick = {
                    if (!perms.granted(AppPermission.NOTIFICATIONS)) ask.ask(AppPermission.NOTIFICATIONS)
                    else context.launch(feature.platform.notificationSettingsIntent())
                },
            )
            SettingsRow(
                "Quick Settings tile",
                icon = ArchieIcons.Bolt,
                value = "Add “Talk to Archie” from the Quick Settings editor",
            )
            SettingsRow(
                "Background",
                icon = SettingsIcons.BatteryFull,
                value = "Keep listening when the screen is off · ${perms.checklistSummary}",
                onClick = { open(SettingsPageKey.BACKGROUND) },
                modifier = Modifier.testTag("open-background"),
            )
        }
    }
}

/**
 * A comma-separated phrase field. Saves on Done or when focus leaves (no separate Save button,
 * which the old app needed, inv03 §8); a blank value is refused and the saved one comes back.
 */
@Composable
private fun PhraseField(label: String, saved: String, placeholder: String, help: String, enabled: Boolean, onSave: (String) -> Boolean, testTag: String) {
    var text by rememberSaveable(saved) { mutableStateOf(saved) }
    var error by remember { mutableStateOf<String?>(null) }
    var focused by remember { mutableStateOf(false) }
    val commit = {
        if (text.trim() != saved.trim()) {
            if (phrases(text).isEmpty()) error = "Enter at least one phrase" else { error = null; onSave(text) }
        }
    }
    // Leaving the page (Back, or the keyboard hidden then Back) must not drop an edit: Done and
    // focus loss are not the only ways out (found on the POCO X7 Pro, 2026-10-05).
    val latestCommit by rememberUpdatedState(commit)
    DisposableEffect(Unit) { onDispose { latestCommit() } }
    FieldBlock {
        ArchieTextField(
            text, { text = it; error = null }, label,
            Modifier.fillMaxWidth().testTag(testTag).onFocusChanged { f ->
                if (focused && !f.isFocused) commit()
                focused = f.isFocused
            },
            placeholder = placeholder,
            supportingText = help,
            errorText = error,
            enabled = enabled,
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done),
            keyboardActions = KeyboardActions(onDone = { commit() }),
        )
    }
}

// ───────────────────────────── Notifications ─────────────────────────────

/**
 * "Agent session finished" (spec 12 §3.7 `agent_turn_finished`, §8.2). Turning it on asks for
 * POST_NOTIFICATIONS (API 33+) through the usual rationale; the switch reads on only when Android
 * would actually show the notification.
 */
@Composable
internal fun NotificationsPage(feature: SettingsFeature, onBack: (() -> Unit)?, open: (SettingsPageKey) -> Unit) {
    val s by feature.device.settings.collectAsStateWithLifecycle()
    val perms by feature.permissions.state.collectAsStateWithLifecycle()
    val ask = rememberPermissionAsk(feature)
    val context = LocalContext.current
    SettingsPageFrame("Notifications", feature.messages, onBack, scope = ScopeLabel.device(feature.platform.deviceName)) {
        val d = s ?: return@SettingsPageFrame LoadingBody("Loading…")
        val granted = perms.granted(AppPermission.NOTIFICATIONS)
        if (d.notifyAgentTurns && !granted) {
            Notice(
                NoticeTone.WARNING, "Notifications are not allowed", body = "Archie can't show the agent notifications until Android allows it.",
                icon = SettingsIcons.Notifications,
                actions = { com.assistant.core.design.components.InlineCardAction("Allow", { ask.ask(AppPermission.NOTIFICATIONS) }, primary = true) },
            )
        } else if (d.notifyAgentTurns && !perms.notificationsEnabled) {
            Notice(
                NoticeTone.WARNING, "Notifications are off for Archie", body = "Turn them on in Android’s notification settings.",
                icon = SettingsIcons.Notifications,
                actions = {
                    com.assistant.core.design.components.InlineCardAction("Open settings", { context.launch(feature.platform.notificationSettingsIntent()) }, primary = true)
                },
            )
        }
        Section(null) {
            ToggleField(
                "Agent session finished",
                NotifyLogic.effective(d.notifyAgentTurns, granted, perms.notificationsEnabled),
                { on ->
                    if (on) ask.ask(AppPermission.NOTIFICATIONS) { feature.device.setNotifyAgentTurns(true) }
                    else feature.device.setNotifyAgentTurns(false)
                },
                help = "A notification when an agent session finishes, unless you’re looking at it.",
                info = "For any agent session (Claude Code, Codex, Gemini, Qwen…), whether you started it here, on another device or " +
                    "Archie did. A stopped turn doesn’t notify; a failed one says so. Tapping it opens the session.",
                testTag = "notify-agent-turns",
            )
        }
        Section("In the background") {
            SettingsRow(
                "Stay connected in background",
                icon = ArchieIcons.Lan,
                value = if (d.stayConnectedInBackground) "On: every finished turn reaches this phone" else "Off",
                onClick = { open(SettingsPageKey.CONNECTION) },
                modifier = Modifier.testTag("open-connection"),
            )
        }
        HelpLine(
            "While a turn is running, Archie keeps its connection (the “Waiting for N agent sessions” notification) until it ends, " +
                "so the phone can be in your pocket.",
            info = "Android only lets Archie hold the connection if the turn was already running while Archie was open. " +
                "Turns started elsewhere while Archie is closed or long in the background reach this phone only with " +
                "“Stay connected in background” on, or while the wake word or a voice conversation keeps Archie running.",
            modifier = Modifier.padding(start = 4.dp, bottom = 16.dp),
        )
    }
}

// ───────────────────────────── Appearance ─────────────────────────────

@Composable
internal fun AppearancePage(feature: SettingsFeature, onBack: (() -> Unit)?) {
    val s by feature.device.settings.collectAsStateWithLifecycle()
    val a by feature.device.appearance.collectAsStateWithLifecycle()
    SettingsPageFrame("Appearance", feature.messages, onBack, scope = ScopeLabel.device(feature.platform.deviceName)) {
        val d = s ?: return@SettingsPageFrame LoadingBody("Loading…")
        com.assistant.core.design.components.SettingsGroup {
            val themes = listOf(ThemeMode.SYSTEM, ThemeMode.DARK, ThemeMode.LIGHT)
            SettingsFieldSet("Theme", Modifier.testTag("theme"), help = "Dark is the default. System follows this device.") {
                Spacer(Modifier.height(6.dp))
                SegmentedChoice(
                    listOf(SegmentOption("System", ArchieIcons.BrightnessAuto), SegmentOption("Dark", ArchieIcons.DarkMode), SegmentOption("Light", ArchieIcons.LightMode)),
                    themes.indexOf(d.themeMode),
                    { i -> if (themes[i] != d.themeMode) feature.device.setTheme(themes[i]) },
                )
                Spacer(Modifier.height(4.dp))
            }
            val sizes = TextSize.entries
            SettingsFieldSet("Text size", Modifier.testTag("text-size"), help = "Scales all text in the app.") {
                Spacer(Modifier.height(6.dp))
                SegmentedChoice(sizes.map { SegmentOption(it.label) }, sizes.indexOf(a.textSize), { i -> if (sizes[i] != a.textSize) feature.device.setTextSize(sizes[i]) })
                Spacer(Modifier.height(4.dp))
            }
            ToggleField("Reduce motion", a.reduceMotion, feature.device::setReduceMotion, help = "Turns off animations.", info = "Spinners, the voice orb and transitions draw still frames.")
        }
    }
}
