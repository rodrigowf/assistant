package com.assistant.archie.feature.settings.ui

import android.content.ClipData
import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.ClipEntry
import androidx.compose.ui.platform.LocalClipboard
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.assistant.archie.feature.settings.AccountsModel
import com.assistant.archie.feature.settings.Option
import com.assistant.archie.feature.settings.SettingsFeature
import com.assistant.core.data.ConnectionRepository
import com.assistant.core.design.components.ArchieButton
import com.assistant.core.design.components.ArchieButtonDefaults
import com.assistant.core.design.components.ArchieConfirmDialog
import com.assistant.core.design.components.ArchieDialogSurface
import com.assistant.core.design.components.ArchieIconButton
import com.assistant.core.design.components.ArchieTextField
import com.assistant.core.design.components.ButtonSize
import com.assistant.core.design.components.ButtonStyle
import com.assistant.core.design.components.Spinner
import com.assistant.core.design.icons.ArchieIcon
import com.assistant.core.design.icons.ArchieIcons
import com.assistant.core.design.theme.ArchieTheme
import com.assistant.core.protocol.AccountEnvFieldDto
import com.assistant.core.protocol.AccountMethodDto
import com.assistant.core.protocol.AccountServiceDto
import com.assistant.core.protocol.EnvKeyDto
import com.assistant.core.protocol.LoginFlowDto
import kotlinx.coroutines.launch

/**
 * Settings → Archie (server) → Accounts (web `AccountsPage`, spec 12 §8.1): every service's sign-in
 * through every method it offers, then the `context/.env` key manager. Status is refetched on
 * open; an active sign-in is polled while the page is shown (ACC-2). Revealed env values live in
 * this composition only (ACC-1).
 */
@Composable
internal fun AccountsPage(feature: SettingsFeature, onBack: (() -> Unit)?) {
    val model = feature.accounts
    val st by model.state.collectAsStateWithLifecycle()
    val conn by feature.connection.state.collectAsStateWithLifecycle()
    val host = conn.status.serverUrl?.let(ConnectionRepository::hostOf).orEmpty().ifEmpty { "the server" }
    LaunchedEffect(model) { model.load(); model.loadEnv() }
    LaunchedEffect(model) { model.watchFlows() }
    SettingsPageFrame("Accounts", feature.messages, onBack, scope = ScopeLabel.server(conn.status.serverLabel.ifEmpty { host })) {
        st.loadError?.let { err ->
            Notice(NoticeTone.ERROR, "Couldn't load the accounts", body = err, actions = {
                ArchieButton("Retry", model::load, style = ButtonStyle.Tonal, icon = ArchieIcons.Refresh)
            })
        }
        val services = st.services
        if (services == null) {
            if (st.loadError == null) LoadingBody(if (st.loading) "Checking every account on the server…" else "Loading…")
        } else {
            for ((group, title) in AccountsModel.GROUPS) {
                val list = services.filter { it.group == group }
                if (list.isEmpty()) continue
                Section(title) {
                    for (s in list) ServiceCard(model, s, st.busy[s.id], st.errors[s.id])
                }
            }
        }
        Section("Environment keys") { EnvKeysBlock(model) }
    }
}

private fun stateColors(state: String, c: androidx.compose.material3.ColorScheme, success: Pair<Color, Color>, warning: Pair<Color, Color>): Pair<Color, Color> =
    when (state) {
        "signed_in" -> success
        "signed_out" -> c.surfaceContainerHighest to c.onSurfaceVariant
        "expired", "unavailable" -> c.errorContainer to c.onErrorContainer
        else -> warning
    }

private fun methodIcon(kind: String): ImageVector = when (kind) {
    "link" -> ArchieIcons.Link
    "credentials" -> ArchieIcons.ContentPaste
    "env" -> ArchieIcons.Shield
    else -> ArchieIcons.Logout
}

private fun serviceIcon(id: String): ImageVector = when (id) {
    "claude" -> ArchieIcons.SmartToy
    "codex" -> ArchieIcons.Terminal
    "gemini" -> ArchieIcons.StarShine
    "qwen" -> ArchieIcons.Code
    "modelstudio" -> ArchieIcons.Hub
    "openai" -> ArchieIcons.RecordVoiceOver
    "google_ai", "dashscope" -> ArchieIcons.GraphicEq
    "anthropic" -> ArchieIcons.Forum
    "browser" -> ArchieIcons.Language
    else -> ArchieIcons.Shield
}

@Composable
private fun ServiceCard(model: AccountsModel, s: AccountServiceDto, busy: String?, error: String?) {
    val c = ArchieTheme.colors
    val ext = ArchieTheme.extended
    val scope = rememberCoroutineScope()
    var open by rememberSaveable(s.id) { mutableStateOf<String?>(null) }
    var confirmSignOut by rememberSaveable(s.id) { mutableStateOf(false) }
    val methods = s.methods.filter { it.kind != "signout" }
    val signout = s.methods.firstOrNull { it.kind == "signout" }
    val flowActive = s.flow?.active == true
    FieldBlock(Modifier.testTag("account:${s.id}")) {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            ArchieIcon(serviceIcon(s.id), null, tint = c.onSurfaceVariant)
            Column(Modifier.weight(1f)) {
                Text(s.label, style = ArchieTheme.typography.titleMedium, color = c.onSurface)
                Text(s.description, style = ArchieTheme.typography.bodySmall, color = c.onSurfaceVariant)
            }
            val (bg, fg) = stateColors(s.state, c, ext.success.colorContainer to ext.success.onColorContainer, ext.warning.colorContainer to ext.warning.onColorContainer)
            Text(
                AccountsModel.STATE_LABEL[s.state] ?: s.state,
                Modifier.clip(RoundedCornerShape(50)).background(bg).padding(horizontal = 10.dp, vertical = 2.dp).testTag("account-state:${s.id}"),
                style = ArchieTheme.typography.labelMedium.copy(fontWeight = FontWeight.W600),
                color = fg,
            )
            if (busy == "refresh") Spinner(size = 18.dp)
            else ArchieIconButton(ArchieIcons.Refresh, "Check ${s.label} again", { scope.launch { model.refresh(s.id) } }, size = 40.dp, iconSize = 20.dp)
        }
        val facts = listOfNotNull(
            s.method?.let { "Signed in with" to it },
            s.account?.let { "Account" to it },
            s.plan?.let { "Plan" to it },
            AccountsModel.formatExpiry(s.expiresAt)?.let { "Valid" to it },
        )
        if (facts.isNotEmpty()) KeyValues(facts)
        s.detail?.let { Line(ArchieIcons.Info, it, c.onSurfaceVariant) }
        for (w in s.warnings) Line(ArchieIcons.Warning, w, ext.warning.color)
        s.verified?.let { Line(if (it.ok) ArchieIcons.CheckCircle else ArchieIcons.Error, it.message, if (it.ok) ext.success.color else c.error) }
        error?.let { ErrorBox(it, Modifier.testTag("account-error:${s.id}")) }
        s.flow?.let { FlowPanel(model, s, it, busy) }
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            for (m in methods) {
                if (!m.available) continue
                ArchieButton(
                    m.label,
                    {
                        model.clearError(s.id)
                        if (m.kind == "link") {
                            open = m.id
                            scope.launch { model.startLogin(s.id, m.id) }
                        } else {
                            open = if (open == m.id) null else m.id
                        }
                    },
                    Modifier.testTag("method:${s.id}:${m.id}"),
                    style = when { open == m.id -> ButtonStyle.Filled; m.recommended -> ButtonStyle.Tonal; else -> ButtonStyle.Outlined },
                    size = ButtonSize.Small,
                    icon = methodIcon(m.kind),
                    enabled = !(m.kind == "link" && (flowActive || busy == "login")),
                )
            }
            if (s.canVerify) {
                ArchieButton("Test", { scope.launch { model.verify(s.id) } }, Modifier.testTag("verify:${s.id}"), style = ButtonStyle.Text, size = ButtonSize.Small, icon = ArchieIcons.NetworkCheck, enabled = busy != "verify")
            }
            if (signout?.available == true) {
                ArchieButton(
                    signout.label, { confirmSignOut = true }, Modifier.testTag("signout:${s.id}"), style = ButtonStyle.Text, size = ButtonSize.Small,
                    icon = ArchieIcons.Logout, colors = ArchieButtonDefaults.colors(ButtonStyle.Text).copy(content = c.error),
                )
            }
        }
        for (m in methods.filter { !it.available }) Line(ArchieIcons.Close, "${m.label}: ${m.unavailableReason}", c.onSurfaceVariant)
        if (signout != null && !signout.available && s.state == "signed_in" && signout.unavailableReason.isNotEmpty()) {
            Line(ArchieIcons.Info, signout.unavailableReason, c.onSurfaceVariant)
        }
        val openMethod = methods.firstOrNull { it.id == open }
        if (openMethod != null) {
            when (openMethod.kind) {
                "credentials" -> CredentialsPanel(model, s.id, openMethod, busy) { open = null }
                "env" -> Panel(openMethod.label, ArchieIcons.Shield) {
                    if (openMethod.description.isNotEmpty()) PanelText(openMethod.description)
                    for (f in openMethod.fields) EnvFieldRow(model, s.id, f, busy == "env:${f.name}")
                }
                "link" -> if (s.flow == null) PanelText(openMethod.description)
            }
        }
    }
    if (confirmSignOut && signout != null) {
        ArchieConfirmDialog(
            title = "${signout.label}?",
            text = signout.description,
            confirmLabel = "Sign out",
            destructive = true,
            onConfirm = { confirmSignOut = false; scope.launch { model.signOut(s.id) } },
            onDismissRequest = { confirmSignOut = false },
        )
    }
}

@Composable
private fun Line(icon: ImageVector, text: String, tint: Color) {
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        ArchieIcon(icon, null, size = 16.dp, tint = tint)
        Text(text, style = ArchieTheme.typography.bodySmall, color = ArchieTheme.colors.onSurfaceVariant)
    }
}

@Composable
private fun ErrorBox(text: String, modifier: Modifier = Modifier) {
    val c = ArchieTheme.colors
    Text(
        text,
        modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(c.errorContainer).padding(horizontal = 12.dp, vertical = 8.dp),
        style = ArchieTheme.typography.bodyMedium, color = c.onErrorContainer,
    )
}

@Composable
private fun Panel(title: String, icon: ImageVector?, modifier: Modifier = Modifier, content: @Composable ColumnScope.() -> Unit) {
    val c = ArchieTheme.colors
    Column(
        modifier.fillMaxWidth().clip(RoundedCornerShape(16.dp)).background(c.surfaceContainerHigh).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            if (icon != null) ArchieIcon(icon, null, size = 18.dp, tint = c.onSurface) else Spinner(size = 16.dp)
            Text(title, style = ArchieTheme.typography.titleSmall, color = c.onSurface)
        }
        content()
    }
}

@Composable
private fun PanelText(text: String) =
    Text(text, style = ArchieTheme.typography.bodyMedium, color = ArchieTheme.colors.onSurfaceVariant)

@Composable
private fun FlowPanel(model: AccountsModel, s: AccountServiceDto, flow: LoginFlowDto, busy: String?) {
    val c = ArchieTheme.colors
    val context = LocalContext.current
    val clipboard = LocalClipboard.current
    val scope = rememberCoroutineScope()
    // Secrets are never put in saved state (`remember`, not `rememberSaveable`): a pasted code,
    // credentials or key value is gone after process death / rotation instead of being persisted.
    var code by remember(flow.id) { mutableStateOf("") }
    val method = s.methods.firstOrNull { it.id == flow.method }
    val label = method?.label ?: "Sign in"
    val icon = when (flow.status) {
        "succeeded" -> ArchieIcons.CheckCircle
        "failed", "expired" -> ArchieIcons.Error
        "cancelled" -> ArchieIcons.Close
        else -> null
    }
    fun copy(text: String, what: String) = scope.launch {
        clipboard.setClipEntry(ClipEntry(ClipData.newPlainText(what, text)))
        model.post("$what copied")
    }
    Panel("$label · ${AccountsModel.FLOW_LABEL[flow.status] ?: flow.status}", icon, Modifier.testTag("flow:${s.id}")) {
        val warning = method?.warning.orEmpty()
        if (flow.active && warning.isNotEmpty()) Line(ArchieIcons.Warning, warning, ArchieTheme.extended.warning.color)
        val url = flow.url
        if (url != null && flow.active) {
            PanelText(
                buildString {
                    append("1. Open the link on any device and sign in.")
                    if (flow.userCode != null) append("\n2. Enter the code below when asked.")
                    append(if (flow.needsCode) "\n${if (flow.userCode != null) 3 else 2}. ${flow.codeHelp.ifEmpty { "Paste the ${flow.codeLabel.lowercase()} you get here." }}" else "\nApprove; this page updates by itself.")
                },
            )
            SelectionContainer {
                Text(
                    url,
                    Modifier.fillMaxWidth().heightIn(max = 96.dp).clip(RoundedCornerShape(12.dp)).background(c.surfaceContainerHighest).padding(horizontal = 12.dp, vertical = 8.dp).testTag("flow-url"),
                    style = ArchieTheme.typography.bodySmall.copy(fontFamily = FontFamily.Monospace), color = c.onSurface,
                )
            }
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                ArchieButton("Open link", { context.launch(Intent(Intent.ACTION_VIEW, Uri.parse(url))) }, Modifier.testTag("flow-open"), size = ButtonSize.Small, icon = ArchieIcons.OpenInNew)
                ArchieButton("Copy link", { copy(url, "Link") }, style = ButtonStyle.Outlined, size = ButtonSize.Small, icon = ArchieIcons.ContentCopy)
            }
        }
        val userCode = flow.userCode
        if (userCode != null && flow.active) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(
                    userCode,
                    Modifier.clip(RoundedCornerShape(12.dp)).background(c.primaryContainer).padding(horizontal = 16.dp, vertical = 8.dp).testTag("flow-user-code"),
                    style = ArchieTheme.typography.headlineSmall.copy(fontFamily = FontFamily.Monospace), color = c.onPrimaryContainer,
                )
                ArchieIconButton(ArchieIcons.ContentCopy, "Copy the code", { copy(userCode, "Code") })
            }
        }
        if (flow.needsCode && (flow.status == "waiting" || flow.status == "verifying")) {
            ArchieTextField(
                code, { code = it }, flow.codeLabel, Modifier.testTag("flow-code"),
                enabled = flow.status == "waiting",
                keyboardOptions = KeyboardOptions(autoCorrectEnabled = false, capitalization = KeyboardCapitalization.None, keyboardType = KeyboardType.Uri),
            )
            ArchieButton(
                if (busy == "code" || flow.status == "verifying") "Checking…" else "Finish sign-in",
                { scope.launch { model.submitCode(s.id, code) } },
                Modifier.testTag("flow-submit"), icon = ArchieIcons.Check,
                enabled = code.isNotBlank() && busy != "code" && flow.status == "waiting",
            )
        }
        Text(flow.message, Modifier.testTag("flow-message"), style = ArchieTheme.typography.bodyMedium, color = c.onSurfaceVariant)
        if (flow.active) ArchieButton("Cancel sign-in", { scope.launch { model.cancelLogin(s.id) } }, Modifier.testTag("flow-cancel"), style = ButtonStyle.Text, size = ButtonSize.Small, icon = ArchieIcons.Close, enabled = busy != "cancel")
        else ArchieButton(if (flow.status == "succeeded") "Done" else "Dismiss", { model.dismissFlow(s.id) }, Modifier.testTag("flow-dismiss"), style = ButtonStyle.Text, size = ButtonSize.Small)
    }
}

@Composable
private fun CredentialsPanel(model: AccountsModel, serviceId: String, m: AccountMethodDto, busy: String?, onDone: () -> Unit) {
    val scope = rememberCoroutineScope()
    var text by remember(serviceId, m.id) { mutableStateOf("") }
    var localError by remember { mutableStateOf<String?>(null) }
    val secret = m.input == "secret"
    val saving = busy == "credentials:${m.id}"
    Panel(m.label, if (secret) ArchieIcons.Shield else ArchieIcons.ContentPaste, Modifier.testTag("credentials:$serviceId:${m.id}")) {
        if (m.description.isNotEmpty()) PanelText(m.description)
        if (m.sourceHint.isNotEmpty()) PanelText("1. On a machine where it is signed in, open ${m.sourceHint}.\n2. Copy the whole file and paste it below.")
        if (m.warning.isNotEmpty()) Line(ArchieIcons.Warning, m.warning, ArchieTheme.extended.warning.color)
        if (secret) {
            SecretField(m.label, text, { text = it; localError = null }, Modifier.testTag("credentials-input"), placeholder = m.placeholder.ifEmpty { null }, errorText = localError,
                supportingText = m.path.takeIf { it.isNotEmpty() }?.let { "Stored in $it" })
        } else {
            ArchieTextField(
                text, { text = it; localError = null }, "File contents (JSON)", Modifier.testTag("credentials-input"),
                singleLine = false, errorText = localError, placeholder = m.placeholder.ifEmpty { null },
                supportingText = m.path.takeIf { it.isNotEmpty() }?.let { "Saved on the server as $it (mode 600; the previous file is kept as a backup)." },
                keyboardOptions = KeyboardOptions(autoCorrectEnabled = false),
            )
        }
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            ArchieButton(if (saving) "Saving…" else "Save", {
                val err = if (secret) (if (text.isBlank()) "Paste the key" else null) else AccountsModel.jsonError(text)
                localError = err
                if (err == null) scope.launch { if (model.saveCredentials(serviceId, m.id, text)) { text = ""; onDone() } }
            }, Modifier.testTag("credentials-save"), icon = ArchieIcons.Check, enabled = text.isNotBlank() && !saving)
            ArchieButton("Cancel", onDone, style = ButtonStyle.Text)
        }
    }
}

/** A masked text field with a show/hide toggle. */
@Composable
private fun SecretField(
    label: String,
    value: String,
    onValueChange: (String) -> Unit,
    modifier: Modifier = Modifier,
    placeholder: String? = null,
    errorText: String? = null,
    supportingText: String? = null,
    initiallyVisible: Boolean = false,
) {
    var visible by rememberSaveable { mutableStateOf(initiallyVisible) }
    ArchieTextField(
        value, onValueChange, label, modifier,
        placeholder = placeholder, errorText = errorText, supportingText = supportingText,
        visualTransformation = if (visible) VisualTransformation.None else PasswordVisualTransformation(),
        keyboardOptions = KeyboardOptions(autoCorrectEnabled = false, keyboardType = KeyboardType.Password),
        trailing = { ArchieIconButton(if (visible) ArchieIcons.VisibilityOff else ArchieIcons.Visibility, if (visible) "Hide value" else "Show value", { visible = !visible }) },
    )
}

@Composable
private fun EnvFieldRow(model: AccountsModel, serviceId: String, f: AccountEnvFieldDto, busy: Boolean) {
    val c = ArchieTheme.colors
    val scope = rememberCoroutineScope()
    // Editor open-state and draft live and die together (both `remember`): after a rotation the
    // editor is closed rather than open with an empty draft that Save would write.
    var editing by remember(f.name) { mutableStateOf(false) }
    var draft by remember(f.name) { mutableStateOf("") }
    var removing by rememberSaveable(f.name) { mutableStateOf(false) }
    val choices = f.choices
    if (choices != null) {
        SelectRow(
            f.label, choices.map { Option(it.value, it.label) }, f.value.orEmpty(),
            onSelect = { v -> scope.launch { if (v.isEmpty()) model.removeKey(serviceId, f.name) else model.setKey(serviceId, f.name, v) } },
            enabled = !busy, supporting = f.help.ifEmpty { f.name },
        )
        return
    }
    Column(
        Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(c.surfaceContainerHighest).padding(start = 12.dp, end = 4.dp, top = 6.dp, bottom = 6.dp).testTag("env-field:${f.name}"),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("${f.label} · ${f.name}", style = ArchieTheme.typography.bodyMedium, color = c.onSurface)
                Text(
                    if (!f.set) "Not set" else if (f.secret) f.preview.ifEmpty { "••••" } else f.value.orEmpty(),
                    style = ArchieTheme.typography.bodySmall.copy(fontFamily = FontFamily.Monospace), color = c.onSurfaceVariant,
                )
                if (f.help.isNotEmpty()) Text(f.help, style = ArchieTheme.typography.bodySmall, color = c.onSurfaceVariant)
            }
            if (!editing) {
                ArchieIconButton(if (f.set) ArchieIcons.Edit else ArchieIcons.Add, "${if (f.set) "Change" else "Set"} ${f.name}", {
                    draft = if (f.secret) "" else f.value.orEmpty(); editing = true
                }, Modifier.testTag("env-edit:${f.name}"), enabled = !busy)
                if (f.set) ArchieIconButton(ArchieIcons.Delete, "Remove ${f.name}", { removing = true }, Modifier.testTag("env-remove:${f.name}"), enabled = !busy)
            }
        }
        if (editing) {
            Column(Modifier.padding(end = 8.dp, top = 8.dp, bottom = 4.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                if (f.secret) SecretField(f.label, draft, { draft = it }, Modifier.testTag("env-input:${f.name}"), placeholder = f.placeholder.ifEmpty { null }, supportingText = "Saved in context/.env on the server.")
                else ArchieTextField(draft, { draft = it }, f.label, Modifier.testTag("env-input:${f.name}"), placeholder = f.placeholder.ifEmpty { null }, keyboardOptions = KeyboardOptions(autoCorrectEnabled = false))
                FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    ArchieButton("Save", { scope.launch { if (model.setKey(serviceId, f.name, draft.trim())) { editing = false; draft = "" } } },
                        Modifier.testTag("env-save:${f.name}"), icon = ArchieIcons.Check, enabled = draft.isNotBlank() && !busy)
                    ArchieButton("Cancel", { editing = false }, style = ButtonStyle.Text)
                }
            }
        }
    }
    if (removing) {
        ArchieConfirmDialog(
            title = "Remove ${f.name}?",
            text = "It is deleted from context/.env (a backup is kept on the server). What uses it stops working until it is set again.",
            confirmLabel = "Remove", destructive = true,
            onConfirm = { removing = false; scope.launch { model.removeKey(serviceId, f.name) } },
            onDismissRequest = { removing = false },
        )
    }
}

// ───────────────────────────── env keys ─────────────────────────────

@Composable
private fun EnvKeysBlock(model: AccountsModel) {
    val st by model.state.collectAsStateWithLifecycle()
    val revealed = remember { mutableStateMapOf<String, String>() }
    val scope = rememberCoroutineScope()
    var adding by remember { mutableStateOf(false) }
    var deleting by rememberSaveable { mutableStateOf<String?>(null) }
    DisposableEffect(model) { onDispose { revealed.clear(); model.clearEnvError() } }
    FieldBlock(Modifier.testTag("env-keys")) {
        HelpLine(
            "The keys in ${st.envPath.ifEmpty { "context/.env" }} on the server. Values stay hidden until you reveal one.",
            info = "Changes apply to the server right away and to agent sessions started afterwards; running sessions keep the old value until restarted. " +
                "The file syncs to your other machines with context/, but their running servers keep their values until restarted (shown as \"not loaded\").",
        )
        st.envError?.let { ErrorBox(it, Modifier.testTag("env-error")) }
        val keys = st.envKeys
        when {
            keys == null -> PanelText(if (st.envLoading) "Loading keys…" else "—")
            keys.isEmpty() -> PanelText("No keys yet.")
            else -> for (k in keys) EnvKeyRow(model, k, revealed[k.name], st.envBusy == k.name, onReveal = { revealed[k.name] = it }, onHide = { revealed.remove(k.name) }, onDelete = { deleting = k.name })
        }
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            ArchieButton("Add key", { adding = true }, Modifier.testTag("env-add"), style = ButtonStyle.Tonal, icon = ArchieIcons.Add)
            ArchieButton("Reload", model::loadEnv, style = ButtonStyle.Text, icon = ArchieIcons.Refresh, enabled = !st.envLoading)
        }
    }
    if (adding) AddKeyDialog(model, st.envKeys.orEmpty().map { it.name }) { adding = false }
    deleting?.let { name ->
        ArchieConfirmDialog(
            title = "Delete $name?",
            text = "Every line that sets it is removed from context/.env (a backup is kept on the server). The running server forgets it too.",
            confirmLabel = "Delete", destructive = true,
            onConfirm = { deleting = null; scope.launch { if (model.deleteKey(name)) revealed.remove(name) } },
            onDismissRequest = { deleting = null },
        )
    }
}

@Composable
private fun EnvKeyRow(model: AccountsModel, k: EnvKeyDto, value: String?, busy: Boolean, onReveal: (String) -> Unit, onHide: () -> Unit, onDelete: () -> Unit) {
    val c = ArchieTheme.colors
    val scope = rememberCoroutineScope()
    var editing by remember(k.name) { mutableStateOf(false) }
    var draft by remember(k.name) { mutableStateOf("") }
    var fetching by remember(k.name) { mutableStateOf(false) }
    suspend fun fetch(): String? {
        if (value != null) return value
        fetching = true
        val v = model.reveal(k.name)
        fetching = false
        if (v != null) onReveal(v)
        return v
    }
    Column(
        Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(c.surfaceContainerHigh).padding(start = 12.dp, end = 4.dp, top = 6.dp, bottom = 6.dp).testTag("env-key:${k.name}"),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Row(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.CenterVertically) {
                    Text(k.name, style = ArchieTheme.typography.bodyMedium.copy(fontFamily = FontFamily.Monospace), color = c.onSurface)
                    if (k.exported) Badge("export")
                    if (k.duplicates > 0) Badge("×${k.duplicates + 1}")
                    if (!k.inProcess) Badge("not loaded")
                }
                Text(
                    value?.ifEmpty { "(empty)" } ?: if (k.set) "${k.preview} · ${k.length} chars" else "(empty)",
                    Modifier.testTag("env-value:${k.name}"),
                    style = ArchieTheme.typography.bodySmall.copy(fontFamily = FontFamily.Monospace), color = c.onSurfaceVariant,
                )
            }
            if (!editing) {
                if (fetching) Spinner(size = 18.dp)
                else ArchieIconButton(
                    if (value != null) ArchieIcons.VisibilityOff else ArchieIcons.Visibility,
                    if (value != null) "Hide ${k.name}" else "Reveal ${k.name}",
                    { if (value != null) onHide() else scope.launch { fetch() } },
                    Modifier.testTag("env-reveal:${k.name}"), enabled = k.set || value != null,
                )
                ArchieIconButton(ArchieIcons.Edit, "Edit ${k.name}", { scope.launch { fetch()?.let { draft = it; editing = true } } }, Modifier.testTag("env-key-edit:${k.name}"), enabled = !busy)
                ArchieIconButton(ArchieIcons.Delete, "Delete ${k.name}", onDelete, Modifier.testTag("env-key-delete:${k.name}"), enabled = !busy)
            }
        }
        if (editing) {
            Column(Modifier.padding(end = 8.dp, top = 8.dp, bottom = 4.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                SecretField("Value", draft, { draft = it }, Modifier.testTag("env-key-input:${k.name}"), initiallyVisible = value != null)
                FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    ArchieButton("Save", { scope.launch { if (model.updateKey(k.name, draft)) { onHide(); editing = false } } }, Modifier.testTag("env-key-save:${k.name}"), icon = ArchieIcons.Check, enabled = !busy)
                    ArchieButton("Cancel", { editing = false }, style = ButtonStyle.Text)
                }
            }
        }
    }
}

@Composable
private fun AddKeyDialog(model: AccountsModel, existing: List<String>, onClose: () -> Unit) {
    val st by model.state.collectAsStateWithLifecycle()
    val scope = rememberCoroutineScope()
    var name by rememberSaveable { mutableStateOf("") }
    var value by remember { mutableStateOf("") }
    var touched by rememberSaveable { mutableStateOf(false) }
    val nameError = AccountsModel.envNameError(name, existing)
    Dialog(onDismissRequest = onClose) {
        ArchieDialogSurface("Add a key", body = {
            Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                ArchieTextField(
                    name, { name = it.uppercase(); touched = true }, "Name", Modifier.testTag("env-new-name"),
                    placeholder = "MY_API_KEY", errorText = if (touched) nameError else null, supportingText = "Capital letters, digits and underscores.",
                    keyboardOptions = KeyboardOptions(autoCorrectEnabled = false, capitalization = KeyboardCapitalization.Characters),
                )
                SecretField("Value", value, { value = it }, Modifier.testTag("env-new-value"), supportingText = "Saved in context/.env on the server.")
                st.envError?.let { Text(it, style = ArchieTheme.typography.bodySmall, color = ArchieTheme.colors.error) }
            }
        }) {
            ArchieButton("Cancel", onClose, style = ButtonStyle.Text)
            ArchieButton("Add", {
                touched = true
                if (nameError == null) scope.launch { if (model.createKey(name.trim(), value)) onClose() }
            }, Modifier.testTag("env-new-add"), style = ButtonStyle.Text, enabled = name.isNotBlank() && st.envBusy == null)
        }
    }
}
