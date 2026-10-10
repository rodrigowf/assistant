/**
 * Floating voice controls (spec 13 §3.9 `VoiceOverlay`): the pure parts — where the overlay sits
 * for a snap anchor, which anchor a drop lands on, and which voice states keep it fully shown.
 * Pure, so the overlay, its tests and the gallery share it.
 */
import type { VoiceOverlayAnchor } from '@/stores';
import type { VoiceSnapshot } from '@/voice';

/**
 * The six snap positions inside the workspace (bottom-center, the default, is where the dock sits
 * on the Archie page). A device pref (`voiceOverlayAnchor`), so each device keeps its own.
 */
export type OverlayAnchor = VoiceOverlayAnchor;

export { VOICE_OVERLAY_SELECTOR } from './overlaySelector';

/** No pointer, touch, wheel or key activity for this long → the controls shrink to the faded pill. */
export const IDLE_AFTER_MS = 4_000;
/**
 * A press elsewhere wakes the pill into the full controls under the finger; clicks on the controls
 * are swallowed this long after such a wake, so that press can never mute or end the call.
 */
export const WAKE_GUARD_MS = 600;
/** Clear of the workspace top bar / compact app bar (64) for the top anchors. */
export const TOP_INSET_PX = 72;
/** Above the composer it must not cover. */
export const AVOID_GAP_PX = 8;

export interface OverlayRect {
  readonly left: number;
  readonly top: number;
  readonly width: number;
  readonly height: number;
}

/** Fixed-position box of the overlay's row; the controls align inside it. */
export interface OverlayBox {
  readonly left: number;
  readonly width: number;
  /** Exactly one of `top` / `bottom` is set. */
  readonly top: number | null;
  readonly bottom: number | null;
  readonly align: 'left' | 'center' | 'right';
  /** Lifted above a composer (which already clears the home indicator). */
  readonly avoided: boolean;
}

function split(a: OverlayAnchor): { v: 'top' | 'bottom'; h: 'left' | 'center' | 'right' } {
  const [v, h] = a.split('-') as ['top' | 'bottom', 'left' | 'center' | 'right'];
  return { v, h };
}

/**
 * Where the overlay goes for `anchor` in `region` (the workspace, viewport coordinates): the dock's
 * side gutters (12 compact / 28 wider) and bottom gap (12 / 20), and above `avoidTop` (the top of
 * the visible composer) when one is given.
 */
export function overlayBox(
  anchor: OverlayAnchor,
  region: OverlayRect,
  viewportHeight: number,
  opts: { compact: boolean; avoidTop: number | null },
): OverlayBox {
  const { v, h } = split(anchor);
  const side = opts.compact ? 12 : 28;
  const left = region.left + side;
  const width = Math.max(0, region.width - 2 * side);
  if (v === 'top') return { left, width, top: region.top + TOP_INSET_PX, bottom: null, align: h, avoided: false };
  let bottom = viewportHeight - (region.top + region.height) + (opts.compact ? 12 : 20);
  if (opts.avoidTop !== null) bottom = Math.max(bottom, viewportHeight - opts.avoidTop + AVOID_GAP_PX);
  return { left, width, top: null, bottom: Math.max(0, bottom), align: h, avoided: opts.avoidTop !== null };
}

export function sameBox(a: OverlayBox | null, b: OverlayBox): boolean {
  return !!a && a.left === b.left && a.width === b.width && a.top === b.top && a.bottom === b.bottom && a.align === b.align && a.avoided === b.avoided;
}

/** The anchor a dragged overlay snaps to: thirds across, halves down, by the dropped controls' centre. */
export function snapAnchor(center: { x: number; y: number }, region: OverlayRect): OverlayAnchor {
  const fx = region.width > 0 ? (center.x - region.left) / region.width : 0.5;
  const fy = region.height > 0 ? (center.y - region.top) / region.height : 1;
  const h = fx < 1 / 3 ? 'left' : fx > 2 / 3 ? 'right' : 'center';
  const v = fy < 0.5 ? 'top' : 'bottom';
  return `${v}-${h}`;
}

/**
 * States that need the user, so the controls stay fully shown and never shrink: connecting,
 * ending, errors, a lost or just-restored link, a banner (e.g. "Pausing in ~5s to reconnect").
 */
export function needsAttention(s: VoiceSnapshot): boolean {
  return s.status === 'connecting' || s.status === 'ending' || s.status === 'error' || s.link !== 'ok' || s.banner !== null;
}
