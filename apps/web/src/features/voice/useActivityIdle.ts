/**
 * Idle detection for the floating voice controls (`VoiceOverlay`): after `idleAfterMs` with no
 * pointer, touch, wheel or key activity anywhere in the app, `idle` turns true (the controls
 * shrink to the faded pill); the next activity wakes them.
 *
 * - Listens on the window (capture, passive) for mousemove, mousedown, touchstart, touchmove,
 *   wheel and keydown, and on every same-origin iframe document too (visualizations run in
 *   iframes, whose events never reach the parent). Iframes are re-scanned when one is added
 *   (MutationObserver), when `scanKey` changes, on going idle, when the window blurs (focus moved
 *   into a frame) and on each frame's `load` (a navigation replaces its document). Cross-origin
 *   frames are skipped.
 * - Only user intent counts: no `scroll` (a streaming conversation scrolls itself), and a
 *   mousemove whose coordinates did not change (the browser's synthetic hover update) is ignored.
 * - Presses inside the controls do not wake them through here: the pill's own click does, so a
 *   tap on the pill only expands it. A press elsewhere that wakes them arms `guardUntil`, and the
 *   overlay swallows clicks on the controls until then (no accidental mute / end).
 * - `hold` (keyboard focus inside, hover, a state that needs the user) wakes them and stops the
 *   countdown; it restarts in full when the hold ends. `pause` (a drag) only stops it.
 * - `modality` is the last input kind, so the overlay holds for keyboard focus only (a tapped
 *   button keeps focus on Android Chrome, and must not pin the controls open).
 */
import { useCallback, useEffect, useRef, useState, type MutableRefObject, type RefObject } from 'react';
import { useLatest } from '@/ui/a11y';
import { IDLE_AFTER_MS, WAKE_GUARD_MS } from './overlay';

export type InputModality = 'pointer' | 'keyboard';

export interface ActivityIdleOptions {
  readonly enabled: boolean;
  readonly hold: boolean;
  /** Stops the countdown without waking (a drag of the pill). */
  readonly pause?: boolean;
  /** The floating controls (presses inside them are theirs). */
  readonly ref: RefObject<HTMLElement | null>;
  readonly idleAfterMs?: number;
  /** Changes when the view under the overlay changes: re-scan iframes. */
  readonly scanKey?: string;
}

export interface ActivityIdle {
  readonly idle: boolean;
  readonly wake: () => void;
  /** `Date.now()` until which clicks on the controls are swallowed. */
  readonly guardUntil: MutableRefObject<number>;
  readonly modality: MutableRefObject<InputModality>;
  /** Last touch (`Date.now()`): mouse events emulated after a tap are not hover. */
  readonly lastTouchAt: MutableRefObject<number>;
}

const EVENTS = ['mousemove', 'mousedown', 'touchstart', 'touchmove', 'wheel', 'keydown'] as const;
const OPTS: AddEventListenerOptions = { capture: true, passive: true };

function isPress(type: string): boolean {
  return type === 'mousedown' || type === 'touchstart';
}

export function useActivityIdle({ enabled, hold, pause = false, ref, idleAfterMs = IDLE_AFTER_MS, scanKey }: ActivityIdleOptions): ActivityIdle {
  const [idle, setIdle] = useState(false);
  const idleRef = useLatest(idle);
  const last = useRef(0);
  const guardUntil = useRef(0);
  const modality = useRef<InputModality>('pointer');
  const lastTouchAt = useRef(0);
  const scanRef = useRef<() => void>(() => undefined);

  const wake = useCallback(() => {
    last.current = Date.now();
    setIdle(false);
  }, []);

  // Activity listeners: the window plus every same-origin iframe document.
  useEffect(() => {
    if (!enabled) return undefined;
    let lastMove: { doc: Document | null; x: number; y: number } = { doc: null, x: NaN, y: NaN };

    const onActivity = (e: Event): void => {
      const now = Date.now();
      if (e.type === 'mousemove') {
        const m = e as MouseEvent;
        const doc = (m.target as Node | null)?.ownerDocument ?? null;
        if (lastMove.doc === doc && lastMove.x === m.clientX && lastMove.y === m.clientY) return;
        lastMove = { doc, x: m.clientX, y: m.clientY };
      }
      if (e.type === 'keydown') modality.current = 'keyboard';
      else if (isPress(e.type)) modality.current = 'pointer';
      if (e.type === 'touchstart' || e.type === 'touchmove') lastTouchAt.current = now;
      const target = e.target as Node | null;
      const inside = !!(target && ref.current && ref.current.contains(target));
      if (inside && e.type !== 'mousemove') return;
      last.current = now;
      if (!idleRef.current) return;
      if (!inside && isPress(e.type)) guardUntil.current = now + WAKE_GUARD_MS;
      setIdle(false);
    };

    const frames = new Map<HTMLIFrameElement, { doc: Document | null; off: () => void }>();
    const scan = (): void => {
      const list = Array.from(document.getElementsByTagName('iframe'));
      frames.forEach((f, el) => {
        if (list.indexOf(el) < 0) {
          f.off();
          frames.delete(el);
        }
      });
      for (const el of list) {
        let doc: Document | null = null;
        try {
          doc = el.contentDocument;
        } catch {
          doc = null; // cross-origin
        }
        const known = frames.get(el);
        if (known && known.doc === doc) continue;
        known?.off();
        const offs: (() => void)[] = [];
        el.addEventListener('load', scan);
        offs.push(() => el.removeEventListener('load', scan));
        if (doc) {
          try {
            for (const t of EVENTS) doc.addEventListener(t, onActivity, OPTS);
            const d = doc;
            offs.push(() => {
              for (const t of EVENTS) d.removeEventListener(t, onActivity, OPTS);
            });
          } catch {
            /* a frame mid-navigation: its load re-scans */
          }
        }
        frames.set(el, { doc, off: () => offs.forEach((o) => o()) });
      }
    };
    scanRef.current = scan;

    const onBlur = (): void => {
      // Focus moved into an iframe (a click on a visualization): that is activity, and the frame
      // may be new.
      scan();
      last.current = Date.now();
      if (idleRef.current) setIdle(false);
    };

    // A new view's iframe (a visual opened on a compact screen, a lazy viewer) is watched at once.
    let pendingScan: ReturnType<typeof setTimeout> | null = null;
    const hasFrame = (n: Node): boolean =>
      n.nodeType === 1 && ((n as Element).tagName === 'IFRAME' || (n as Element).getElementsByTagName('iframe').length > 0);
    const mo =
      typeof MutationObserver === 'function'
        ? new MutationObserver((records) => {
            if (pendingScan !== null) return;
            for (const r of records)
              for (let i = 0; i < r.addedNodes.length; i += 1) {
                const n = r.addedNodes[i];
                if (n && hasFrame(n)) {
                  pendingScan = setTimeout(() => {
                    pendingScan = null;
                    scan();
                  }, 0);
                  return;
                }
              }
          })
        : null;
    mo?.observe(document.body, { childList: true, subtree: true });

    for (const t of EVENTS) window.addEventListener(t, onActivity, OPTS);
    window.addEventListener('blur', onBlur);
    scan();
    return () => {
      mo?.disconnect();
      if (pendingScan !== null) clearTimeout(pendingScan);
      for (const t of EVENTS) window.removeEventListener(t, onActivity, OPTS);
      window.removeEventListener('blur', onBlur);
      frames.forEach((f) => f.off());
      frames.clear();
      scanRef.current = () => undefined;
    };
  }, [enabled, ref, idleRef]);

  useEffect(() => {
    scanRef.current();
  }, [scanKey, idle]);

  // Hidden overlay: start awake next time. A hold wakes (state adjusted while rendering).
  const held = !enabled || hold;
  const [wasHeld, setWasHeld] = useState(held);
  if (held !== wasHeld) {
    setWasHeld(held);
    if (held) setIdle(false);
  }

  // The countdown: one timer that re-arms for the time left (mousemove never resets a timer).
  useEffect(() => {
    if (!enabled || hold || pause || idle) return undefined;
    last.current = Date.now();
    let t: ReturnType<typeof setTimeout>;
    const check = (): void => {
      const left = idleAfterMs - (Date.now() - last.current);
      if (left <= 0) setIdle(true);
      else t = setTimeout(check, left);
    };
    t = setTimeout(check, idleAfterMs);
    return () => clearTimeout(t);
  }, [enabled, hold, pause, idle, idleAfterMs]);

  return { idle, wake, guardUntil, modality, lastTouchAt };
}
