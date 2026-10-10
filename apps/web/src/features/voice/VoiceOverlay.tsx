/**
 * Floating voice controls (spec 13 §3.9 `VoiceOverlay`). While this device has a voice call, the
 * same controls as the Archie page's dock float above every other view — agent sessions, memory
 * documents, visuals, settings, the compact screens — so the call can be muted or ended while
 * reading or watching something else. The shell decides when (`app/shell/VoiceOverlayHost`): not
 * on the Archie conversation itself, where the dock already is.
 *
 * - **Same controls.** `VoiceDockView` driven by `useVoiceDock(localId)`: mute, speaker, End,
 *   interrupt, Retry and Reconnect behave exactly as in the dock. Its state text opens the
 *   Archie conversation.
 * - **Position.** A fixed row at z-index 19: above the rail, list pane, workspace and screens;
 *   below the modal layers of `#overlay-root` (drawer, sheets, dialogs at 20; menus, snackbars,
 *   tooltips higher). Default where the dock sits on the Archie page (bottom centre of the
 *   workspace, its gutters), lifted above a visible composer. Mouse or touch drag snaps it to one
 *   of six anchors (`overlay.ts`), kept per device (`voiceOverlayAnchor` pref).
 * - **Idle.** After 4 s without activity it shrinks to a faded pill: orb + mic state, never
 *   invisible (a live microphone always shows). Activity anywhere, same-origin iframes included,
 *   wakes it (`useActivityIdle`); a press elsewhere that wakes it cannot click the controls for
 *   600 ms. Never while hovered, with keyboard focus inside, or while the call needs the user
 *   (connecting, reconnecting, errors); a mic mute change wakes it.
 * - **A11y.** Region "Voice call"; the pill is a button naming the state, and a polite live
 *   region speaks state changes while it is one; Tab onto the pill expands it and focuses the
 *   controls. Modal compact screens leave it exposed and focusable (`VOICE_OVERLAY_SELECTOR`);
 *   dialogs, sheets and menus hide it like the rest of the page. Fade and
 *   snap motion are off under reduced motion / low-end (motion.css).
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type MouseEvent as ReactMouseEvent, type RefObject } from 'react';
import { setPref, usePrefs } from '@/stores';
import { useDrag, useLatest } from '@/ui/a11y';
import { Icon, VisuallyHidden, cx } from '@/ui/primitives';
import { statusWord } from './copy';
import { LevelOrb } from './LevelOrb';
import { needsAttention, overlayBox, sameBox, snapAnchor, type OverlayAnchor, type OverlayBox } from './overlay';
import { useActivityIdle } from './useActivityIdle';
import { orbTone, useVoiceDock, VoiceDockView, type VoiceDockViewProps } from './VoiceDock';
import styles from './VoiceOverlay.module.css';

/** Mouse events this soon after a touch are the browser's emulation, not hover. */
const EMULATED_MOUSE_MS = 800;
const SETTLE_MS = 250;

export interface VoicePillProps {
  readonly snapshot: VoiceDockViewProps['snapshot'];
  readonly level?: () => number;
  readonly onExpand: () => void;
  readonly onFocus?: () => void;
}

/** The idle form: orb and mic state; a tap (or Enter) brings the full controls back. */
export function VoicePill({ snapshot: s, level, onExpand, onFocus }: VoicePillProps) {
  const muted = s.micMuted;
  return (
    <button
      type="button"
      className={styles.pill}
      aria-label={`Voice call: ${statusWord(s)}. Show voice controls`}
      aria-expanded={false}
      data-voice-pill=""
      onClick={onExpand}
      onFocus={onFocus}
    >
      <LevelOrb tone={orbTone(s)} size={36} level={level} />
      <Icon name={muted ? 'mic_off' : 'mic'} size={20} className={muted ? styles.micOff : undefined} />
    </button>
  );
}

export interface FloatingVoiceDockProps {
  /** The dock's live props (`useVoiceDock`), or pinned ones (tests, gallery). */
  readonly dock: VoiceDockViewProps;
  /** The area the controls float in: the workspace. */
  readonly regionRef: RefObject<HTMLElement | null>;
  /** A bottom element they must stay above: the visible composer. */
  readonly getAvoid?: () => Element | null;
  /** Changes with the view underneath: re-measure, re-scan iframes. */
  readonly layoutKey?: string;
  readonly compact: boolean;
  readonly anchor: OverlayAnchor;
  readonly onAnchorChange: (a: OverlayAnchor) => void;
  readonly onOpenConversation: () => void;
  readonly idleAfterMs?: number;
}

function viewportHeight(): number {
  return document.documentElement.clientHeight || window.innerHeight;
}

export function FloatingVoiceDock(p: FloatingVoiceDockProps) {
  const s = p.dock.snapshot;
  const frameRef = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState(false);
  const [keyFocus, setKeyFocus] = useState(false);
  const [dragging, setDragging] = useState(false);
  const { idle, wake, guardUntil, modality, lastTouchAt } = useActivityIdle({
    enabled: true,
    hold: hover || keyFocus || needsAttention(s),
    pause: dragging,
    ref: frameRef,
    ...(p.idleAfterMs !== undefined ? { idleAfterMs: p.idleAfterMs } : null),
    ...(p.layoutKey !== undefined ? { scanKey: p.layoutKey } : null),
  });

  // A mute / unmute from anywhere (keyboard shortcut, another view) shows the full controls.
  const micMuted = s.micMuted;
  const seenMic = useRef(micMuted);
  useEffect(() => {
    if (seenMic.current === micMuted) return;
    seenMic.current = micMuted;
    wake();
  }, [micMuted, wake]);

  /* ---------------------------------------------------------------- position */

  const [box, setBox] = useState<OverlayBox | null>(null);
  const avoid = useLatest(p.getAvoid);
  const { anchor, compact, regionRef } = p;
  const measure = useCallback(() => {
    const region = regionRef.current;
    if (!region) return;
    const a = avoid.current?.()?.getBoundingClientRect();
    const next = overlayBox(anchor, region.getBoundingClientRect(), viewportHeight(), { compact, avoidTop: a && a.height > 0 ? a.top : null });
    setBox((prev) => (sameBox(prev, next) ? prev : next));
  }, [anchor, compact, regionRef, avoid]);

  useLayoutEffect(() => {
    measure();
    let raf = 0;
    const schedule = (): void => {
      if (!raf)
        raf = requestAnimationFrame(() => {
          raf = 0;
          measure();
        });
    };
    window.addEventListener('resize', schedule);
    window.addEventListener('orientationchange', schedule);
    let ro: ResizeObserver | null = null;
    if (typeof ResizeObserver === 'function') {
      ro = new ResizeObserver(schedule);
      if (regionRef.current) ro.observe(regionRef.current);
      const el = avoid.current?.();
      if (el) ro.observe(el); // a composer growing with its draft
    }
    return () => {
      if (raf) cancelAnimationFrame(raf);
      window.removeEventListener('resize', schedule);
      window.removeEventListener('orientationchange', schedule);
      ro?.disconnect();
    };
  }, [measure, regionRef, avoid, p.layoutKey]);

  /* ---------------------------------------------------------------- drag → snap */

  const dragStart = useRef<DOMRect | null>(null);
  const flipFrom = useRef<{ left: number; top: number } | null>(null);

  /** FLIP from where the controls were dropped to where their anchor puts them. */
  const settle = useCallback(() => {
    const el = frameRef.current;
    const from = flipFrom.current;
    flipFrom.current = null;
    if (!el || !from) return;
    el.removeAttribute('data-dragging');
    el.style.transform = '';
    const to = el.getBoundingClientRect();
    el.style.transform = `translate(${from.left - to.left}px, ${from.top - to.top}px)`;
    void el.offsetWidth; // commit the start position before the transition
    el.setAttribute('data-settling', '');
    el.style.transform = '';
    setTimeout(() => el.removeAttribute('data-settling'), SETTLE_MS);
  }, []);

  useLayoutEffect(() => {
    if (flipFrom.current) settle();
  }, [box, settle]);

  const onAnchorChange = useLatest(p.onAnchorChange);
  useDrag(
    frameRef,
    {
      onStart: () => {
        const el = frameRef.current;
        if (!el) return false;
        dragStart.current = el.getBoundingClientRect();
        return true;
      },
      onMove: (d) => {
        const el = frameRef.current;
        if (!el || !dragStart.current) return;
        if (!el.hasAttribute('data-dragging')) {
          el.setAttribute('data-dragging', '');
          setDragging(true);
        }
        el.style.transform = `translate(${d.dx}px, ${d.dy}px)`;
      },
      onEnd: (d) => {
        const start = dragStart.current;
        dragStart.current = null;
        setDragging(false);
        if (!start) return;
        const dropped = { left: start.left + d.dx, top: start.top + d.dy };
        flipFrom.current = dropped;
        const region = regionRef.current?.getBoundingClientRect();
        const next = region ? snapAnchor({ x: dropped.left + start.width / 2, y: dropped.top + start.height / 2 }, region) : anchor;
        if (next !== anchor) onAnchorChange.current(next);
        else settle();
      },
      onCancel: () => {
        dragStart.current = null;
        setDragging(false);
        const el = frameRef.current;
        if (el) {
          el.removeAttribute('data-dragging');
          el.style.transform = '';
        }
      },
    },
    // The frame mounts once the first measure lands (`box`): enabling then attaches the listeners.
    { threshold: 6, enabled: box !== null },
  );

  /* ---------------------------------------------------------------- wake, focus */

  const refocus = useRef(false);
  const expand = (): void => {
    if (modality.current === 'keyboard') refocus.current = true;
    wake();
  };

  useLayoutEffect(() => {
    if (idle || !refocus.current) return;
    refocus.current = false;
    frameRef.current?.querySelector<HTMLElement>('button:not([disabled])')?.focus();
  }, [idle]);

  const onClickCapture = (e: ReactMouseEvent): void => {
    if (Date.now() < guardUntil.current) {
      e.preventDefault();
      e.stopPropagation();
    }
  };

  if (!box) return null;
  const position = box.top !== null ? { top: box.top } : { bottom: box.bottom ?? 0 };
  return (
    <div
      className={cx(
        styles.layer,
        box.align === 'left' ? styles.alignLeft : box.align === 'right' ? styles.alignRight : styles.alignCenter,
        box.bottom !== null && !box.avoided ? styles.safeBottom : undefined,
      )}
      style={{ left: box.left, width: box.width, ...position }}
      data-voice-overlay=""
    >
      <div
        ref={frameRef}
        role="region"
        aria-label="Voice call"
        className={cx(styles.frame, idle ? styles.idle : undefined)}
        data-idle={idle ? '' : undefined}
        onClickCapture={onClickCapture}
        onMouseEnter={() => {
          if (Date.now() - lastTouchAt.current > EMULATED_MOUSE_MS) setHover(true);
        }}
        onMouseLeave={() => setHover(false)}
        onFocus={() => {
          if (modality.current === 'keyboard') setKeyFocus(true);
        }}
        onBlur={(e) => {
          if (!frameRef.current?.contains(e.relatedTarget as Node | null)) setKeyFocus(false);
        }}
      >
        {idle ? (
          <>
            <VoicePill
              snapshot={s}
              {...(p.dock.level ? { level: p.dock.level } : null)}
              onExpand={expand}
              onFocus={() => {
                if (modality.current === 'keyboard') expand();
              }}
            />
            <VisuallyHidden aria-live="polite">{statusWord(s)}</VisuallyHidden>
          </>
        ) : (
          <VoiceDockView {...p.dock} onOpenConversation={p.onOpenConversation} />
        )}
      </div>
    </div>
  );
}

export interface VoiceOverlayProps {
  /** The Archie conversation that has voice on this device. */
  readonly localId: string;
  readonly regionRef: RefObject<HTMLElement | null>;
  readonly getAvoid?: () => Element | null;
  readonly layoutKey?: string;
  readonly compact: boolean;
  readonly onOpenConversation: () => void;
}

/** The live floating controls of `localId`'s call. */
export function VoiceOverlay({ localId, ...rest }: VoiceOverlayProps) {
  const dock = useVoiceDock(localId);
  const anchor = usePrefs((pr) => pr.voiceOverlayAnchor);
  const onAnchorChange = useCallback((a: OverlayAnchor) => setPref('voiceOverlayAnchor', a), []);
  return <FloatingVoiceDock dock={dock} anchor={anchor} onAnchorChange={onAnchorChange} {...rest} />;
}
