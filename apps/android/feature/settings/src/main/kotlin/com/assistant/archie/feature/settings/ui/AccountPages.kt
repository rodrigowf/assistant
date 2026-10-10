package com.assistant.archie.feature.settings.ui

import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.assistant.archie.feature.settings.AuthModel
import com.assistant.archie.feature.settings.AuthPhase
import com.assistant.archie.feature.settings.SettingsFeature
import com.assistant.core.data.ConnectionRepository
import com.assistant.core.design.components.ArchieButton
import com.assistant.core.design.components.ArchieTextField
import com.assistant.core.design.components.ButtonStyle
import com.assistant.core.design.components.Spinner
import com.assistant.core.design.icons.ArchieIcon
import com.assistant.core.design.icons.ArchieIcons
import com.assistant.core.design.theme.ArchieTheme
import kotlinx.coroutines.launch
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.remember
import com.assistant.archie.feature.settings.AccountsModel
import kotlinx.coroutines.delay

/**
 * The Claude sign-in flows of the AuthGate (inv02 §1.11, web `AuthPanel`); Settings → Accounts has every
 * service's methods (`AccountsPage`):
 * - **Sign in with a link** (any server, also headless): `POST /api/accounts/claude/login` runs
 *   `claude setup-token` on the server; the URL opens in the browser, the user pastes back the
 *   code; the 1-year token is saved on the server. Older servers fall back to the blocking
 *   `POST /api/auth/login` when they have a screen.
 * - **Paste credentials**: `~/.claude/.credentials.json` from a signed-in machine
 *   (`POST /api/auth/credentials`), with an optional link to the Claude Console.
 */
@Composable
internal fun AuthPanel(auth: AuthModel, host: String, startWithPaste: Boolean = false, onSignedIn: () -> Unit = {}) {
    val st by auth.state.collectAsStateWithLifecycle()
    val scope = rememberCoroutineScope()
    val context = LocalContext.current
    val c = ArchieTheme.colors
    var paste by rememberSaveable { mutableStateOf(startWithPaste) }
    // Secrets stay out of saved state (`remember`).
    var text by remember { mutableStateOf("") }
    var code by remember { mutableStateOf("") }
    val flow = st.flow
    DisposableEffect(auth) { onDispose { auth.clearError() } }
    LaunchedEffect(flow?.id, flow?.active) {
        while (flow?.active == true) {
            delay(AccountsModel.FLOW_POLL_MS)
            auth.pollLink()
        }
    }

    if (!paste) {
        Column(verticalArrangement = Arrangement.spacedBy(12.dp), modifier = Modifier.testTag("auth-panel-login")) {
            val live = flow?.takeIf { it.active && it.url != null }
            if (live == null) {
                Text("Sign in with your Claude subscription: open a link on any device, sign in, and paste back the code it shows. The token is saved on $host.", style = ArchieTheme.typography.bodyMedium, color = c.onSurface)
            } else {
                val url = live.url.orEmpty()
                Text("1. Open the link and sign in with your Claude account.\n2. Copy the code the page shows and paste it below.", style = ArchieTheme.typography.bodyMedium, color = c.onSurface)
                Text(url, Modifier.fillMaxWidth().testTag("auth-link-url"), style = ArchieTheme.typography.bodySmall, color = c.onSurfaceVariant, maxLines = 3)
                FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    ArchieButton("Open link", { context.launch(Intent(Intent.ACTION_VIEW, Uri.parse(url))) }, Modifier.testTag("auth-link-open"), icon = ArchieIcons.OpenInNew)
                }
                ArchieTextField(
                    code, { code = it }, "Code", Modifier.fillMaxWidth().testTag("auth-link-code"),
                    enabled = live.status == "waiting", keyboardOptions = KeyboardOptions(autoCorrectEnabled = false),
                )
                FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    ArchieButton(
                        if (st.phase == AuthPhase.SIGNING_IN || live.status == "verifying") "Checking…" else "Finish sign-in",
                        { scope.launch { auth.submitLinkCode(code); code = "" } },
                        Modifier.testTag("auth-link-submit"), icon = ArchieIcons.Check,
                        enabled = code.isNotBlank() && st.phase != AuthPhase.SIGNING_IN && live.status == "waiting",
                    )
                    ArchieButton("Cancel", { scope.launch { auth.cancelLink() } }, style = ButtonStyle.Text)
                }
            }
            if (st.phase == AuthPhase.SIGNING_IN && flow == null) {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                    Spinner(size = 16.dp)
                    Text("Waiting for $host…", style = ArchieTheme.typography.bodySmall, color = c.onSurfaceVariant)
                }
            }
            st.actionError?.let { Text(it, style = ArchieTheme.typography.bodySmall, color = c.error, modifier = Modifier.testTag("auth-error")) }
            if (flow?.active != true) {
                FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    ArchieButton("Sign in with Claude", { scope.launch { auth.startLink(); if (auth.state.value.status?.authenticated == true) onSignedIn() } }, icon = ArchieIcons.AccountCircle, enabled = st.phase != AuthPhase.SIGNING_IN, modifier = Modifier.testTag("auth-sign-in"))
                    ArchieButton("Paste credentials instead", { auth.clearError(); paste = true }, style = ButtonStyle.Text, icon = ArchieIcons.ContentPaste, enabled = st.phase != AuthPhase.SIGNING_IN)
                }
            }
        }
        return
    }
    Column(verticalArrangement = Arrangement.spacedBy(12.dp), modifier = Modifier.testTag("auth-panel-paste")) {
        Text("1. On a computer where Claude Code is signed in, open ~/.claude/.credentials.json.\n2. Copy the whole file and paste it below.", style = ArchieTheme.typography.bodyMedium, color = c.onSurface)
        Row(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.CenterVertically) {
            ArchieIcon(ArchieIcons.Info, null, size = 16.dp, tint = c.onSurfaceVariant)
            Text("Don't paste a login another machine keeps using: refresh tokens rotate and one of the two stops working.", style = ArchieTheme.typography.bodySmall, color = c.onSurfaceVariant)
        }
        ArchieTextField(
            text, { text = it; if (st.actionError != null) auth.clearError() }, "Credentials JSON",
            Modifier.fillMaxWidth().testTag("auth-credentials"),
            singleLine = false,
            errorText = st.actionError,
            supportingText = "Saved on the server as .credentials.json (only readable by the server user).",
            keyboardOptions = KeyboardOptions(autoCorrectEnabled = false),
        )
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            ArchieButton(
                if (st.phase == AuthPhase.SAVING) "Setting…" else "Set credentials",
                { scope.launch { if (auth.submitCredentials(text)) { text = ""; onSignedIn() } } },
                icon = ArchieIcons.Check, enabled = text.isNotBlank() && st.phase != AuthPhase.SAVING,
                modifier = Modifier.testTag("auth-set-credentials"),
            )
            st.status?.authUrl?.let { url ->
                ArchieButton("Claude Console", { context.launch(Intent(Intent.ACTION_VIEW, Uri.parse(url))) }, style = ButtonStyle.Text, icon = ArchieIcons.OpenInNew)
            }
            if (!startWithPaste) {
                ArchieButton("Back", { auth.clearError(); paste = false }, style = ButtonStyle.Text, icon = ArchieIcons.ArrowBack)
            }
        }
    }
}

/**
 * `AuthGate{content}` (web `AuthGate`, spec 13 §3.9): when the backend's Claude CLI is known to be
 * signed out, a sign-in screen covers the app (the app stays composed underneath, so nothing
 * restarts after sign-in). A failed check never shows it; "Not now" dismisses it for this run,
 * because Archie and the Qwen / Gemini harnesses work without Claude credentials.
 */
@Composable
fun AuthGate(feature: SettingsFeature, content: @Composable () -> Unit) {
    val st by feature.auth.state.collectAsStateWithLifecycle()
    Box(Modifier.fillMaxSize()) {
        content()
        if (st.gateBlocked) SignInScreen(feature)
    }
}

@Composable
internal fun SignInScreen(feature: SettingsFeature) {
    val st by feature.auth.state.collectAsStateWithLifecycle()
    val conn by feature.connection.state.collectAsStateWithLifecycle()
    val host = conn.status.serverUrl?.let(ConnectionRepository::hostOf).orEmpty().ifEmpty { "the server" }
    val c = ArchieTheme.colors
    Box(
        Modifier.fillMaxSize().background(c.surface).windowInsetsPadding(WindowInsets.safeDrawing).imePadding().testTag("auth-gate"),
        contentAlignment = Alignment.TopCenter,
    ) {
        Column(
            Modifier.widthIn(max = 520.dp).fillMaxWidth().verticalScroll(rememberScrollState()).padding(24.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            Spacer(Modifier.height(32.dp))
            ArchieIcon(ArchieIcons.AccountCircle, null, size = 48.dp, tint = c.primary)
            Text("Sign in to Claude", style = ArchieTheme.typography.headlineSmall.copy(fontWeight = FontWeight.W500), color = c.onSurface)
            Text("Agent sessions run Claude Code on $host, and it isn't signed in yet.", style = ArchieTheme.typography.bodyLarge, color = c.onSurfaceVariant, textAlign = TextAlign.Center)
            Box(Modifier.fillMaxWidth().background(c.surfaceContainer, androidx.compose.foundation.shape.RoundedCornerShape(20.dp)).padding(18.dp)) {
                AuthPanel(feature.auth, host)
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                ArchieButton("Check again", feature.auth::check, style = ButtonStyle.Text, icon = ArchieIcons.Refresh, enabled = st.phase == AuthPhase.IDLE)
                ArchieButton("Not now", feature.auth::dismissGate, style = ButtonStyle.Text, enabled = st.phase != AuthPhase.SIGNING_IN && st.phase != AuthPhase.SAVING, modifier = Modifier.testTag("auth-not-now"))
            }
        }
    }
}

// ───────────────────────────── About ─────────────────────────────

/** Open-source licenses of what the Android app ships (spec 14 §1.4 catalog). */
internal val LICENSES = listOf(
    "AndroidX (Compose, Material 3, Activity, Lifecycle, Navigation 3, DataStore, Core)" to "Apache-2.0",
    "Kotlin, kotlinx.coroutines, kotlinx.serialization, kotlinx.collections.immutable" to "Apache-2.0",
    "OkHttp, Okio" to "Apache-2.0",
    "commonmark-java" to "BSD-2-Clause",
    "Highlights (dev.snipme)" to "Apache-2.0",
    "java-diff-utils" to "Apache-2.0",
    "stream-webrtc-android (WebRTC)" to "Apache-2.0 / BSD-3-Clause",
    "Vosk, Kaldi" to "Apache-2.0",
    "JNA" to "Apache-2.0 / LGPL-2.1",
    "Roboto Flex, JetBrains Mono" to "OFL-1.1",
    "Material Symbols" to "Apache-2.0",
)

@Composable
internal fun AboutPage(feature: SettingsFeature, onBack: (() -> Unit)?) {
    val conn by feature.connection.state.collectAsStateWithLifecycle()
    val v = feature.platform.appVersion
    val host = conn.status.serverUrl?.let(ConnectionRepository::hostOf).orEmpty()
    var licenses by rememberSaveable { mutableStateOf(false) }
    SettingsPageFrame("About Archie", feature.messages, onBack) {
        Section("Archie") {
            FieldBlock(Modifier.testTag("about-version")) {
                KeyValues(
                    listOf(
                        "App version" to "${v.name} (${v.code})",
                        "Build" to "Android main · ${if (v.debuggable) "debug" else "release"}",
                        "Package" to feature.platform.packageName,
                        "Backend" to "${host.ifEmpty { "—" }} · ${phaseWord(conn.status.phase)}",
                        "Backend version" to "Not reported by the server",
                    ),
                )
                ArchieButton("Check the server again", { feature.server.refresh(); feature.auth.check() }, style = ButtonStyle.Text, icon = ArchieIcons.Refresh)
            }
        }
        Section(null) {
            com.assistant.core.design.components.SettingsRow(
                "Open-source licenses", icon = ArchieIcons.Description, onClick = { licenses = !licenses },
                trailing = { ArchieIcon(if (licenses) ArchieIcons.KeyboardArrowUp else ArchieIcons.KeyboardArrowDown, null, tint = ArchieTheme.colors.onSurfaceVariant) },
            )
            if (licenses) {
                FieldBlock(Modifier.testTag("licenses")) {
                    for ((name, license) in LICENSES) {
                        Text("$name · $license", style = ArchieTheme.typography.bodySmall, color = ArchieTheme.colors.onSurfaceVariant)
                    }
                }
            }
        }
    }
}
