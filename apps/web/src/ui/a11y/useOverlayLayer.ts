/**
 * The overlay contract in one hook (spec 13 §3.5). Every W-04 overlay uses it:
 *
 * | Option        | Effect                                                                    |
 * |---------------|---------------------------------------------------------------------------|
 * | (always)      | registered in `overlayStack`: Escape closes the topmost only              |
 * | `history`     | Back / Android back gesture closes it (one history entry)                 |
 * | `modal`       | focus trap, background `aria-hidden`, iOS-safe scroll lock                |
 * | `trapFocus`   | trap without the rest of `modal` (non-modal side sheets: false)           |
 * | `outsidePress`| a press outside `containerRef` (and `outsideIgnore`) closes it            |
 * | `returnFocus` | focus goes back to the trigger on close (default true)                    |
 * | `keepExposed` | `modal`: a selector left exposed and focusable (compact screens: voice)   |
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, type RefObject } from 'react';
import { hideOthers } from './hideOthers';
import { overlayStack, type OverlayCloseReason, type OverlayHandle } from './overlayStack';
import { useOutsidePress } from './useDismiss';
import { useFocusTrap } from './useFocusTrap';
import { useLatest } from './useLatest';
import { useReturnFocus } from './useReturnFocus';
import { useScrollLock } from './useScrollLock';

export interface OverlayLayerOptions {
  open: boolean;
  onClose: (reason: OverlayCloseReason) => void;
  containerRef: RefObject<HTMLElement | null>;
  modal?: boolean;
  trapFocus?: boolean;
  history?: boolean;
  escape?: boolean;
  outsidePress?: boolean;
  outsideIgnore?: readonly RefObject<Element | null>[];
  initialFocus?: RefObject<HTMLElement | null>;
  returnFocus?: boolean;
  returnFocusTo?: RefObject<HTMLElement | null>;
  /** With `modal`: elements matching this selector stay exposed and may keep focus. */
  keepExposed?: string;
}

const NO_REFS: readonly RefObject<Element | null>[] = [];

export function useOverlayLayer(o: OverlayLayerOptions): { isTop: () => boolean } {
  const { open, containerRef, modal = false, history = false, escape = true, outsidePress = false } = o;
  const handle = useRef<OverlayHandle | null>(null);
  const onCloseRef = useLatest(o.onClose);

  useLayoutEffect(() => {
    if (!open) return undefined;
    const h = overlayStack.push({ onClose: (r) => onCloseRef.current(r), escape, history });
    handle.current = h;
    return () => {
      h.remove();
      if (handle.current === h) handle.current = null;
    };
    // `escape` changes are applied by the effect below without re-registering.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, history]);

  useEffect(() => {
    handle.current?.update({ escape });
  }, [escape]);

  const isTop = useCallback(() => handle.current?.isTop() ?? false, []);

  const keepExposed = o.keepExposed;
  useLayoutEffect(() => {
    if (!open || !modal) return undefined;
    const el = containerRef.current;
    return el ? hideOthers(el, keepExposed) : undefined;
  }, [open, modal, containerRef, keepExposed]);

  useScrollLock(open && modal);
  useReturnFocus(open, {
    container: containerRef,
    enabled: o.returnFocus !== false,
    ...(o.returnFocusTo ? { returnTo: o.returnFocusTo } : null),
  });
  useFocusTrap(containerRef, {
    active: open && (o.trapFocus ?? modal),
    isTop,
    ...(o.initialFocus ? { initialFocus: o.initialFocus } : null),
    ...(keepExposed ? { allowFocusIn: keepExposed } : null),
  });

  const ignore = o.outsideIgnore ?? NO_REFS;
  const refs = useMemo(() => [containerRef, ...ignore], [containerRef, ignore]);
  useOutsidePress(
    refs,
    () => {
      onCloseRef.current('outside');
    },
    { enabled: open && outsidePress, isTop },
  );

  return { isTop };
}
