package com.assistant.archie.shell

import androidx.compose.runtime.Composable
import androidx.compose.runtime.Stable
import androidx.compose.runtime.State
import androidx.compose.runtime.getValue
import androidx.compose.ui.geometry.Rect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.navigation3.runtime.NavKey
import com.assistant.archie.feature.chat.VoiceOverlayModel
import com.assistant.archie.feature.chat.VoiceUi
import com.assistant.archie.feature.chat.ui.ComposerBounds
import com.assistant.archie.feature.chat.ui.OverlayAnchor
import com.assistant.archie.feature.chat.ui.VoiceOverlay
import com.assistant.archie.feature.chat.ui.VoiceOverlayActivity
import com.assistant.core.data.ItemKey

/**
 * The floating voice controls, shell side (the web app's `VoiceOverlayHost`). While this device
 * has a voice call they float over every view except the Archie conversation itself (where the
 * dock already is): another tab, or a screen on the back stack (History, Memory, Visuals, Settings).
 * The shell draws them above the workspace and rail and under the modal drawer / list overlay;
 * sheets, dialogs and menus are windows of their own, above them. "Active elsewhere" never floats:
 * it is not this device's microphone.
 *
 * [model] and [activity] live as long as the Activity's composition; [anchor] is the device
 * setting, [onAnchorChange] persists a drag's snap.
 */
@Stable
class ShellVoiceOverlay(
    val model: VoiceOverlayModel,
    val activity: VoiceOverlayActivity,
    val composer: ComposerBounds,
    val anchor: OverlayAnchor,
    val onAnchorChange: (OverlayAnchor) -> Unit,
) {
    companion object {
        /** On the Archie conversation (its session-settings sheet included): the dock is the control there. */
        fun onArchieView(active: ItemKey?, top: NavKey?): Boolean =
            active == ItemKey.Archie && (top == Workspace || top is SessionSettings)

        fun visible(voice: VoiceUi, active: ItemKey?, top: NavKey?): Boolean =
            VoiceOverlayModel.floats(voice) && !onArchieView(active, top)
    }
}

/**
 * The overlay in the workspace [region] (root coordinates). [onOpen] returns to the Archie
 * conversation (its state text). Above a visible composer when the workspace is on top.
 */
@Composable
internal fun ShellVoiceOverlay.Overlay(region: Rect?, compact: Boolean, active: ItemKey?, top: NavKey?, onOpen: () -> Unit) {
    val voice by model.ui.collectAsStateWithLifecycle()
    if (region == null || !ShellVoiceOverlay.visible(voice, active, top)) return
    val level: State<Float?>? = if (voice is VoiceUi.Active) model.level.collectAsStateWithLifecycle() else null
    VoiceOverlay(
        voice = voice,
        onAction = model::onAction,
        activity = activity,
        region = region,
        anchor = anchor,
        onAnchorChange = onAnchorChange,
        onOpenConversation = onOpen,
        compact = compact,
        avoidTop = if (top == Workspace) composer.rect?.top else null,
        voiceLevel = level?.let { s -> { s.value } },
    )
}
