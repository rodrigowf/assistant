package com.assistant.archie.feature.chat.ui

import androidx.compose.animation.core.VectorConverter
import androidx.compose.animation.core.animate
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.navigationBars
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBars
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.Stable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshots.Snapshot
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Rect
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.input.pointer.PointerEventPass
import androidx.compose.ui.input.pointer.PointerEventType
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.Layout
import androidx.compose.ui.layout.LayoutCoordinates
import androidx.compose.ui.layout.boundsInRoot
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.layout.positionInRoot
import androidx.compose.ui.platform.LocalAccessibilityManager
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.paneTitle
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.Constraints
import androidx.compose.ui.unit.IntSize
import androidx.compose.ui.unit.dp
import com.assistant.archie.feature.chat.ChatAction
import com.assistant.archie.feature.chat.VoiceUi
import com.assistant.core.design.Motion
import com.assistant.core.design.components.OrbTone
import com.assistant.core.design.components.VoiceDockState
import com.assistant.core.design.components.VoiceOrb
import com.assistant.core.design.icons.ArchieIcon
import com.assistant.core.design.icons.ArchieIcons
import com.assistant.core.design.theme.ArchieTheme
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt

/*
 * Floating voice controls (spec 14 §2.4; the web app's `VoiceOverlay`). While this device has a
 * voice call, the same controls as the Archie view's dock ([VoiceControls]) float above every other
 * view — agent sessions, memory, visuals, settings — so the call can be muted or ended while
 * reading or watching something else. The shell decides when (not on the Archie view itself) and
 * hosts it above the workspace and rail, under the modal drawer / list overlay, sheets and dialogs.
 *
 * - Position: where the dock sits on the Archie view (bottom centre of the workspace, its gutters),
 *   above a visible composer ([LocalComposerBounds]). A drag snaps it to one of six anchors
 *   ([OverlayAnchor]), kept per device (DeviceSettings.voiceOverlayAnchor).
 * - Idle: after 4 s with no touch, scroll or key anywhere it shrinks to a faded pill (orb + mic
 *   state, never invisible: a live microphone always shows). [observeVoiceOverlayActivity] on the
 *   shell root watches every pointer event at [PointerEventPass.Initial] without consuming it, so
 *   touches over WebViews (visuals) count too. A press on the pill only expands it; a press
 *   elsewhere cannot reach its buttons (Compose hit-tests on the down, before it expands). Never
 *   while dragged or while the call needs the user (connecting, reconnecting, outcomes); a mic mute
 *   change wakes it. With TalkBack the delay is the system's "time to take action" setting.
 */

/** The six snap positions (keys as the web app's `voiceOverlayAnchor` pref). */
enum class OverlayAnchor(val key: String) {
    TopLeft("top-left"), TopCenter("top-center"), TopRight("top-right"),
    BottomLeft("bottom-left"), BottomCenter("bottom-center"), BottomRight("bottom-right");

    val isTop: Boolean get() = this == TopLeft || this == TopCenter || this == TopRight

    companion object {
        fun parse(key: String?): OverlayAnchor = entries.firstOrNull { it.key == key } ?: BottomCenter
    }
}

/** Pure placement math (px), unit-tested. */
object VoiceOverlayGeometry {
    const val IDLE_AFTER_MS = 4_000L
    const val IDLE_ALPHA = 0.55f
    val MaxWidth = 560.dp
    val TopInset = 72.dp
    val AvoidGap = 8.dp

    /** The anchor a dropped overlay snaps to: thirds across, halves down, by its centre. */
    fun snap(center: Offset, region: Rect): OverlayAnchor {
        val fx = if (region.width > 0) (center.x - region.left) / region.width else 0.5f
        val fy = if (region.height > 0) (center.y - region.top) / region.height else 1f
        val col = when {
            fx < 1f / 3 -> 0
            fx > 2f / 3 -> 2
            else -> 1
        }
        return OverlayAnchor.entries[(if (fy < 0.5f) 0 else 3) + col]
    }

    /**
     * Top-left of an overlay of [size] at [anchor] in [region]: [side] gutters, [top] below the
     * region's top, [bottom] above its bottom.
     */
    fun topLeft(anchor: OverlayAnchor, region: Rect, size: IntSize, side: Float, top: Float, bottom: Float): Offset {
        val x = when (anchor) {
            OverlayAnchor.TopLeft, OverlayAnchor.BottomLeft -> region.left + side
            OverlayAnchor.TopRight, OverlayAnchor.BottomRight -> region.right - side - size.width
            else -> region.left + (region.width - size.width) / 2f
        }
        val y = if (anchor.isTop) region.top + top else region.bottom - bottom - size.height
        return Offset(x, y)
    }

    /** Distance kept from the region's bottom: the gap, or above the composer's top ([avoidTop]). */
    fun bottomPadding(region: Rect, gap: Float, avoidTop: Float?, avoidGap: Float): Float =
        if (avoidTop == null) gap else max(gap, region.bottom - avoidTop + avoidGap)
}

/** Where the visible composer is (root coordinates), so the floating controls stay above it. */
@Stable
class ComposerBounds {
    var rect: Rect? by mutableStateOf(null)
        private set
    private var owner: Any? = null

    fun report(by: Any, r: Rect) {
        owner = by
        if (rect != r) rect = r
    }

    fun clear(by: Any) {
        if (owner === by) {
            owner = null
            rect = null
        }
    }
}

/** Provided by the app shell; [ConversationScreen] reports its composer area into it. */
val LocalComposerBounds = staticCompositionLocalOf<ComposerBounds?> { null }

/** Activity anywhere in the app, for the floating controls' idle fade. */
@Stable
class VoiceOverlayActivity {
    var idle: Boolean by mutableStateOf(false)
        internal set

    /** Bumped on every activity; the idle loop watches it (no wall clock: test clocks drive it). */
    internal var tick = 0L
        private set

    /** The floating controls (root coordinates): their own presses are theirs. */
    internal var bounds: Rect? = null
    internal var observer: LayoutCoordinates? = null

    /** A pointer event at [root]; [press]: a down (only presses elsewhere matter for waking). */
    fun onPointer(root: Offset, press: Boolean) {
        if (bounds?.contains(root) == true) {
            // Using the controls restarts the countdown; on the pill, its own tap expands it.
            if (!idle) tick++
            return
        }
        tick++
        if (idle) idle = false
    }

    fun wake() {
        tick++
        idle = false
    }

    internal fun goIdle() {
        idle = true
    }
}

/**
 * Watches every pointer event under this node (the shell root) at [PointerEventPass.Initial]
 * without consuming it, and hardware keys while focus is inside, for [VoiceOverlayActivity].
 */
fun Modifier.observeVoiceOverlayActivity(activity: VoiceOverlayActivity): Modifier = this
    .onGloballyPositioned { activity.observer = it }
    .onPreviewKeyEvent { activity.wake(); false }
    .pointerInput(activity) {
        awaitPointerEventScope {
            while (true) {
                val e = awaitPointerEvent(PointerEventPass.Initial)
                val p = e.changes.firstOrNull()?.position ?: continue
                val root = activity.observer?.takeIf { it.isAttached }?.localToRoot(p) ?: p
                activity.onPointer(root, press = e.type == PointerEventType.Press)
            }
        }
    }

/** Placement of the last layout pass (read by the drag's snap), and the running snap animation. */
private class Placed {
    var topLeft = Offset.Zero
    var size = IntSize.Zero
    var settling: Job? = null
}

/**
 * The floating controls in [region] (root coordinates, the workspace). Fills its parent without
 * taking any touches outside the controls themselves.
 */
@Composable
fun VoiceOverlay(
    voice: VoiceUi,
    onAction: (ChatAction) -> Unit,
    activity: VoiceOverlayActivity,
    region: Rect,
    anchor: OverlayAnchor,
    onAnchorChange: (OverlayAnchor) -> Unit,
    onOpenConversation: () -> Unit,
    compact: Boolean,
    modifier: Modifier = Modifier,
    avoidTop: Float? = null,
    voiceLevel: (() -> Float?)? = null,
    clock: () -> Long = System::currentTimeMillis,
) {
    val density = LocalDensity.current
    val reduceMotion = ArchieTheme.reduceMotion
    val scope = rememberCoroutineScope()
    var dragging by remember { mutableStateOf(false) }
    var drag by remember { mutableStateOf(Offset.Zero) }
    var current by remember { mutableStateOf(anchor) }
    var origin by remember { mutableStateOf(Offset.Zero) }
    val placed = remember { Placed() }

    LaunchedEffect(anchor) { if (!dragging) current = anchor }

    /* ---------------------------------------------------------------- idle */

    val active = voice as? VoiceUi.Active
    val hold = active == null || dragging
    val timeout = LocalAccessibilityManager.current
        ?.calculateRecommendedTimeoutMillis(VoiceOverlayGeometry.IDLE_AFTER_MS, containsIcons = true, containsText = true, containsControls = true)
        ?: VoiceOverlayGeometry.IDLE_AFTER_MS
    LaunchedEffect(hold) { if (hold && !dragging) activity.wake() }
    LaunchedEffect(activity, hold, activity.idle, timeout) {
        if (hold || activity.idle || timeout >= Long.MAX_VALUE / 8) return@LaunchedEffect
        // Quiet quarters: idle after 4 in a row without activity (between 4 and 5 s after the last).
        val quarter = timeout / 4
        var seen = activity.tick
        var quiet = 0
        while (quiet < 4) {
            delay(quarter)
            if (activity.tick == seen) quiet++ else { seen = activity.tick; quiet = 0 }
        }
        activity.goIdle()
    }
    // A mute / unmute shows the full controls (privacy: the new state is visible).
    val muted = active?.micMuted
    val seenMuted = remember { mutableStateOf(muted) }
    LaunchedEffect(muted) {
        if (seenMuted.value != muted) {
            seenMuted.value = muted
            activity.wake()
        }
    }
    DisposableEffect(activity) { onDispose { activity.bounds = null } }
    // A drag of the pill keeps it a pill; any other hold has already woken it.
    val idle = activity.idle && active != null

    /* ---------------------------------------------------------------- layout */

    val side = with(density) { (if (compact) 12.dp else 28.dp).toPx() }
    val topInset = with(density) { VoiceOverlayGeometry.TopInset.toPx() } + if (compact) WindowInsets.statusBars.getTop(density) else 0
    val gap = with(density) { (if (compact) 12.dp else 20.dp).toPx() } + if (compact) WindowInsets.navigationBars.getBottom(density) else 0
    val bottom = VoiceOverlayGeometry.bottomPadding(region, gap, avoidTop, with(density) { VoiceOverlayGeometry.AvoidGap.toPx() })
    val maxWidth = with(density) { VoiceOverlayGeometry.MaxWidth.toPx() }

    // Snap a drop to its anchor. The anchor is committed (and saved) first, with the offset that
    // keeps the controls where they were dropped; only that offset eases home, so an interrupted
    // animation (a new drag) never loses the choice. Always reads this composition's values.
    val settle by rememberUpdatedState {
        val dropped = placed.topLeft + drag
        val center = dropped + Offset(placed.size.width / 2f, placed.size.height / 2f)
        val next = VoiceOverlayGeometry.snap(center, region)
        val base = VoiceOverlayGeometry.topLeft(next, region, placed.size, side, topInset, bottom)
        Snapshot.withMutableSnapshot {
            current = next
            drag = if (reduceMotion) Offset.Zero else dropped - base
            dragging = false
        }
        onAnchorChange(next)
        if (!reduceMotion) {
            placed.settling = scope.launch {
                animate(Offset.VectorConverter, drag, Offset.Zero, animationSpec = tween(Motion.DurationMedium1, easing = Motion.EasingEmphasizedDecelerate)) { v, _ -> drag = v }
            }
        }
    }

    Layout(
        content = {
            Box(
                Modifier
                    .testTag("voice-overlay")
                    .semantics { paneTitle = "Voice call" }
                    .onGloballyPositioned { activity.bounds = it.boundsInRoot() }
                    .pointerInput(Unit) {
                        detectDragGestures(
                            onDragStart = {
                                placed.settling?.cancel() // a new drag takes over from a running snap
                                dragging = true
                            },
                            onDragEnd = { settle() },
                            onDragCancel = { settle() },
                            onDrag = { change, amount ->
                                change.consume()
                                drag += amount
                            },
                        )
                    },
            ) {
                if (idle && active != null) {
                    VoicePill(active, onExpand = { activity.wake() }, level = voiceLevel)
                } else {
                    val shape = RoundedCornerShape(if (voice is VoiceUi.Reconnected || voice is VoiceUi.ReconnectFailed) 24.dp else 32.dp)
                    VoiceControls(
                        voice, onAction,
                        modifier = Modifier.shadow(6.dp, shape),
                        clock = clock,
                        voiceLevel = voiceLevel,
                        onOpenConversation = onOpenConversation,
                    )
                }
            }
        },
        modifier = modifier.fillMaxSize().onGloballyPositioned { origin = it.positionInRoot() },
    ) { measurables, constraints ->
        val w = min(region.width - 2 * side, maxWidth).coerceAtLeast(0f).roundToInt()
        val p = measurables.first().measure(Constraints(maxWidth = w))
        layout(constraints.maxWidth, constraints.maxHeight) {
            val size = IntSize(p.width, p.height)
            val tl = VoiceOverlayGeometry.topLeft(current, region, size, side, topInset, bottom)
            placed.topLeft = tl
            placed.size = size
            p.place((tl.x - origin.x + drag.x).roundToInt(), (tl.y - origin.y + drag.y).roundToInt())
        }
    }
}

/** The idle form: orb and mic state, faded; a tap brings the full controls back. */
@Composable
fun VoicePill(voice: VoiceUi.Active, onExpand: () -> Unit, modifier: Modifier = Modifier, level: (() -> Float?)? = null) {
    val c = ArchieTheme.colors
    val state = dockState(voice.phase)
    val word = if (voice.micMuted) "Muted" else state.label
    val shape = RoundedCornerShape(24.dp)
    Row(
        modifier
            .testTag("voice-pill")
            .alpha(VoiceOverlayGeometry.IDLE_ALPHA)
            .shadow(4.dp, shape)
            .clip(shape)
            .background(c.surfaceContainerHighest)
            .clickable(onClickLabel = "Show voice controls", role = Role.Button, onClick = onExpand)
            .semantics(mergeDescendants = true) {
                contentDescription = "Voice call: $word. Show voice controls"
                liveRegion = LiveRegionMode.Polite
            }
            .heightIn(min = 48.dp)
            .padding(start = 6.dp, end = 14.dp),
        horizontalArrangement = Arrangement.spacedBy(4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        VoiceOrb(if (state == VoiceDockState.Speaking) OrbTone.Speaking else OrbTone.Listening, size = 36.dp, level = level)
        ArchieIcon(
            if (voice.micMuted) ArchieIcons.MicOffFilled else ArchieIcons.Mic,
            null,
            size = 20.dp,
            tint = if (voice.micMuted) c.error else c.onSurface,
        )
    }
}
