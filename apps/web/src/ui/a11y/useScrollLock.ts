/**
 * Scroll lock for modal overlays (spec 13 §2.6). Reference-counted, so nested modals lock once and
 * unlock on the last close; while locked <html> carries `data-scroll-locked`.
 *
 * It deliberately does NOT use the fixed-body technique (`body { position: fixed }`). On Safari 12
 * (iPad mini 2) switching <body> to `position: fixed` in the same commit that mounts an overlay
 * left the overlay's `position: fixed` layer with no layout at all: a 0x0 box, invisible and
 * untappable, so every modal sheet looked like "nothing opens" (2026-10-09, found by measuring the
 * overlay on the device). The page never scrolls anyway: `html, body { height: 100%; overflow: hidden }`
 * and the shell is an absolutely positioned full-window box, so there is nothing to lock in the
 * body; overlay content scrolls in its own ScrollArea.
 */
import { useLayoutEffect } from 'react';

let locks = 0;

export function lockScroll(): () => void {
  if (typeof document === 'undefined') return () => undefined;
  locks += 1;
  if (locks === 1) document.documentElement.setAttribute('data-scroll-locked', '');
  let released = false;
  return () => {
    if (released) return;
    released = true;
    locks -= 1;
    if (locks === 0) document.documentElement.removeAttribute('data-scroll-locked');
  };
}

export function isScrollLocked(): boolean {
  return locks > 0;
}

export function useScrollLock(active: boolean): void {
  useLayoutEffect(() => (active ? lockScroll() : undefined), [active]);
}
