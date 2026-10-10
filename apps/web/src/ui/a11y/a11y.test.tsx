import { act, fireEvent, waitFor } from '@testing-library/react';
import { StrictMode, useRef, useState, type ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderUi } from '@/test/render';
import { announce, resetAnnouncerForTests } from './announce';
import { classifySwipe, createDragTracker, useDrag, useLongPress, useSwipe, type DragSample } from './gestures';
import { hideOthers } from './hideOthers';
import { resetOverlayStackForTests } from './overlayStack';
import { createTypeahead } from './typeahead';
import { touch } from './touch.testutil';
import { useOverlayLayer } from './useOverlayLayer';
import { useRovingFocus } from './useRovingFocus';
import { isScrollLocked, lockScroll } from './useScrollLock';

/* ---------------------------------------------------------------- helpers */


function Layer({ onClose, modal = true, outside = false, children }: { onClose: (r: string) => void; modal?: boolean; outside?: boolean; children?: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useOverlayLayer({ open: true, onClose, containerRef: ref, modal, outsidePress: outside });
  return (
    <div ref={ref} role="dialog" aria-label="Layer" tabIndex={-1}>
      {children ?? (
        <>
          <button type="button">first</button>
          <button type="button">last</button>
        </>
      )}
    </div>
  );
}

function Harness({ modal = true, outside = false }: { modal?: boolean; outside?: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button type="button" onClick={() => setOpen(true)}>
        open
      </button>
      <button type="button">elsewhere</button>
      {open ? <Layer modal={modal} outside={outside} onClose={() => setOpen(false)} /> : null}
    </div>
  );
}

beforeEach(() => {
  resetOverlayStackForTests();
});
afterEach(() => {
  resetAnnouncerForTests();
});

/* ---------------------------------------------------------------- focus trap + return focus */

describe('useOverlayLayer: focus trap, return focus, Escape', () => {
  it('moves focus in, wraps Tab and Shift+Tab, and returns focus to the trigger on Escape', async () => {
    const { getByText, user, queryByRole } = renderUi(<Harness />);
    const trigger = getByText('open');
    await user.click(trigger);
    await waitFor(() => {
      expect(document.activeElement).toBe(getByText('first'));
    });
    await user.tab();
    expect(document.activeElement).toBe(getByText('last'));
    await user.tab();
    expect(document.activeElement).toBe(getByText('first'));
    await user.tab({ shift: true });
    expect(document.activeElement).toBe(getByText('last'));
    await user.keyboard('{Escape}');
    expect(queryByRole('dialog')).toBeNull();
    await waitFor(() => {
      expect(document.activeElement).toBe(trigger);
    });
  });

  it('returns focus to a pressed trigger that never got focus (Safari does not focus buttons on click)', async () => {
    const { getByText, queryByRole, user } = renderUi(<Harness />);
    const trigger = getByText('open');
    fireEvent.mouseDown(trigger);
    fireEvent.click(trigger);
    expect(queryByRole('dialog')).not.toBeNull();
    await user.keyboard('{Escape}');
    await waitFor(() => {
      expect(document.activeElement).toBe(trigger);
    });
  });

  it('survives StrictMode effect replays: focus stays inside, then returns', async () => {
    const { getByText, user } = renderUi(
      <StrictMode>
        <Harness />
      </StrictMode>,
    );
    const trigger = getByText('open');
    await user.click(trigger);
    await new Promise((r) => setTimeout(r, 10));
    expect(document.activeElement).toBe(getByText('first'));
    await user.keyboard('{Escape}');
    await waitFor(() => {
      expect(document.activeElement).toBe(trigger);
    });
  });

  it('pulls escaped focus back inside while modal', async () => {
    const { getByText, user } = renderUi(<Harness />);
    await user.click(getByText('open'));
    await waitFor(() => {
      expect(document.activeElement).toBe(getByText('first'));
    });
    act(() => {
      getByText('elsewhere').focus();
    });
    expect(document.activeElement?.textContent).toBe('first');
  });

  it('modal layers hide the background from assistive tech and lock scrolling', async () => {
    const { getByText, user, container } = renderUi(<Harness />);
    await user.click(getByText('open'));
    expect(container.getAttribute('aria-hidden')).toBe(null); // an ancestor of the dialog
    expect(getByText('elsewhere').getAttribute('aria-hidden')).toBe('true'); // a sibling: hidden
    expect(isScrollLocked()).toBe(true);
    // Never the fixed-body technique: it left overlays with no layout on Safari 12.
    expect(document.body.style.position).toBe('');
    await user.keyboard('{Escape}');
    expect(isScrollLocked()).toBe(false);
    expect(document.body.style.position).toBe('');
    expect(getByText('elsewhere').hasAttribute('aria-hidden')).toBe(false);
  });

  it('outside press closes only when enabled and on top', async () => {
    const { getByText, user, queryByRole } = renderUi(<Harness modal={false} outside />);
    await user.click(getByText('open'));
    expect(queryByRole('dialog')).not.toBeNull();
    await user.click(getByText('first'));
    expect(queryByRole('dialog')).not.toBeNull();
    fireEvent.mouseDown(getByText('elsewhere'));
    expect(queryByRole('dialog')).toBeNull();
  });

  it('Escape closes only the topmost of two nested layers', async () => {
    const outer = vi.fn();
    const inner = vi.fn();
    const { user } = renderUi(
      <>
        <Layer onClose={outer} />
        <Layer onClose={inner}>
          <button type="button">inner</button>
        </Layer>
      </>,
    );
    await user.keyboard('{Escape}');
    expect(inner).toHaveBeenCalledWith('escape');
    expect(outer).not.toHaveBeenCalled();
  });
});

/* ---------------------------------------------------------------- hideOthers */

describe('hideOthers', () => {
  it('hides siblings up to body, keeps live regions, and restores in any order', () => {
    document.body.innerHTML = `
      <div id="app"><main id="main"></main></div>
      <div id="keep" data-a11y-keep></div>
      <div id="pre" aria-hidden="true"></div>
      <div id="root"><div id="d1"></div><div id="d2"></div></div>`;
    const $ = (id: string) => document.getElementById(id) as HTMLElement;
    const undo1 = hideOthers($('d1'));
    expect($('app').getAttribute('aria-hidden')).toBe('true');
    expect($('d2').getAttribute('aria-hidden')).toBe('true');
    expect($('keep').hasAttribute('aria-hidden')).toBe(false);
    expect($('d1').hasAttribute('aria-hidden')).toBe(false);
    const undo2 = hideOthers($('d2')); // nested modal hides d1
    expect($('d1').getAttribute('aria-hidden')).toBe('true');
    undo1();
    expect($('app').getAttribute('aria-hidden')).toBe('true'); // still hidden by d2
    undo2();
    expect($('app').hasAttribute('aria-hidden')).toBe(false);
    expect($('d1').hasAttribute('aria-hidden')).toBe(false);
    expect($('pre').getAttribute('aria-hidden')).toBe('true'); // not ours: untouched
    document.body.innerHTML = '';
  });

  it('`keep` leaves a selector exposed for that layer only (a compact screen, not a dialog over it)', () => {
    document.body.innerHTML = `
      <div id="shell"><div id="screens"><section id="screen"></section></div><div id="voice" data-voice-overlay></div>
      <div id="root"><div id="dialog"></div></div></div>`;
    const $ = (id: string) => document.getElementById(id) as HTMLElement;
    const undoScreen = hideOthers($('screen'), '[data-voice-overlay]');
    expect($('voice').hasAttribute('aria-hidden')).toBe(false);
    expect($('root').getAttribute('aria-hidden')).toBe('true');
    const undoDialog = hideOthers($('dialog'));
    expect($('voice').getAttribute('aria-hidden')).toBe('true');
    undoDialog();
    expect($('voice').hasAttribute('aria-hidden')).toBe(false);
    undoScreen();
    document.body.innerHTML = '';
  });
});

/* ---------------------------------------------------------------- scroll lock */

describe('lockScroll', () => {
  it('is reference-counted and never touches the body position', () => {
    const a = lockScroll();
    const b = lockScroll();
    expect(document.body.style.position).toBe('');
    expect(document.documentElement.hasAttribute('data-scroll-locked')).toBe(true);
    a();
    a(); // idempotent
    expect(isScrollLocked()).toBe(true);
    b();
    expect(isScrollLocked()).toBe(false);
    expect(document.body.style.position).toBe('');
    expect(document.documentElement.hasAttribute('data-scroll-locked')).toBe(false);
  });
});

/* ---------------------------------------------------------------- roving focus + typeahead */

function Roving({ orientation = 'vertical' as const }: { orientation?: 'vertical' | 'horizontal' }) {
  const ref = useRef<HTMLDivElement>(null);
  const onKey = useRovingFocus(ref, { itemSelector: 'button', orientation, typeahead: true });
  return (
    <div ref={ref} role="toolbar" aria-label="Items" onKeyDown={onKey}>
      {['Apple', 'Banana', 'Blueberry', 'Cherry'].map((l) => (
        <button key={l} type="button" tabIndex={-1}>
          {l}
        </button>
      ))}
    </div>
  );
}

describe('useRovingFocus', () => {
  it('moves with arrows (wrapping), Home/End and type-ahead', async () => {
    const { getByText, user } = renderUi(<Roving />);
    act(() => {
      getByText('Apple').focus();
    });
    await user.keyboard('{ArrowDown}');
    expect(document.activeElement).toBe(getByText('Banana'));
    await user.keyboard('{ArrowUp}{ArrowUp}');
    expect(document.activeElement).toBe(getByText('Cherry'));
    await user.keyboard('{Home}');
    expect(document.activeElement).toBe(getByText('Apple'));
    await user.keyboard('{End}');
    expect(document.activeElement).toBe(getByText('Cherry'));
    await user.keyboard('b');
    expect(document.activeElement).toBe(getByText('Banana'));
    await user.keyboard('l');
    expect(document.activeElement).toBe(getByText('Blueberry'));
  });

  it('ignores the cross axis', async () => {
    const { getByText, user } = renderUi(<Roving orientation="horizontal" />);
    act(() => {
      getByText('Apple').focus();
    });
    await user.keyboard('{ArrowDown}');
    expect(document.activeElement).toBe(getByText('Apple'));
    await user.keyboard('{ArrowRight}');
    expect(document.activeElement).toBe(getByText('Banana'));
  });
});

describe('createTypeahead', () => {
  it('cycles a repeated letter and resets after the timeout', () => {
    const t = createTypeahead(500);
    const labels = ['bar', 'baz', 'foo', 'bat'];
    expect(t.next('b', labels, 0, 0)).toBe(1);
    expect(t.next('b', labels, 1, 100)).toBe(3);
    expect(t.next('b', labels, 3, 200)).toBe(0);
    expect(t.next('f', labels, 0, 1000)).toBe(2);
    expect(t.next('x', labels, 2, 2000)).toBe(-1);
  });
});

/* ---------------------------------------------------------------- announce */

describe('announce', () => {
  it('writes to a shared polite region that survives modals, and re-announces repeats', () => {
    vi.useFakeTimers();
    try {
      announce('Saved');
      vi.advanceTimersByTime(60);
      const region = document.querySelector('[data-announcer="polite"]') as HTMLElement;
      expect(region.getAttribute('aria-live')).toBe('polite');
      expect(region.hasAttribute('data-a11y-keep')).toBe(true);
      expect(region.textContent).toBe('Saved');
      announce('Saved');
      expect(region.textContent).toBe('');
      vi.advanceTimersByTime(60);
      expect(region.textContent).toBe('Saved');
      announce('Connection lost', 'assertive');
      vi.advanceTimersByTime(60);
      expect(document.querySelector('[data-announcer="assertive"]')?.textContent).toBe('Connection lost');
    } finally {
      vi.useRealTimers();
    }
  });
});

/* ---------------------------------------------------------------- gestures */

describe('gesture math', () => {
  it('tracks distance and recent velocity', () => {
    const t = createDragTracker({ x: 0, y: 0 }, 0);
    t.move({ x: 0, y: 20 }, 50);
    const s = t.move({ x: 0, y: 100 }, 100);
    expect(s.dy).toBe(100);
    expect(s.vy).toBeCloseTo(1, 5);
  });

  it('classifies horizontal swipes by distance or speed', () => {
    const s = (dx: number, dy: number, vx = 0): DragSample => ({ dx, dy, vx, vy: 0 });
    expect(classifySwipe(s(-80, 5))).toBe('left');
    expect(classifySwipe(s(80, 10))).toBe('right');
    expect(classifySwipe(s(30, 2))).toBe(null);
    expect(classifySwipe(s(30, 2, 0.6))).toBe('right');
    expect(classifySwipe(s(60, 60))).toBe(null); // diagonal
  });
});

function GestureBox({ onLong, onSwipe, onEnd }: { onLong?: () => void; onSwipe?: (d: string) => void; onEnd?: (s: DragSample) => void }) {
  const ref = useRef<HTMLDivElement>(null);
  useLongPress(ref, () => onLong?.());
  useSwipe(ref, (d) => onSwipe?.(d), { enabled: !!onSwipe });
  useDrag(ref, { onEnd: (s) => onEnd?.(s), shouldStart: (s) => s.dy > 0 }, { enabled: !!onEnd, axis: 'y' });
  return <div ref={ref} data-testid="g" />;
}

describe('gesture hooks (touch events)', () => {
  it('long-press fires after the delay and not when the finger moves', () => {
    vi.useFakeTimers();
    try {
      const onLong = vi.fn();
      const { getByTestId } = renderUi(<GestureBox onLong={onLong} />);
      const el = getByTestId('g');
      touch(el, 'touchstart', 10, 10);
      vi.advanceTimersByTime(499);
      expect(onLong).not.toHaveBeenCalled();
      vi.advanceTimersByTime(2);
      expect(onLong).toHaveBeenCalledTimes(1);
      const end = touch(el, 'touchend', 10, 10);
      expect(end.defaultPrevented).toBe(true); // no synthetic click
      touch(el, 'touchstart', 10, 10);
      touch(el, 'touchmove', 40, 10);
      vi.advanceTimersByTime(600);
      expect(onLong).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('swipe reports the direction', () => {
    const onSwipe = vi.fn();
    const { getByTestId } = renderUi(<GestureBox onSwipe={onSwipe} />);
    const el = getByTestId('g');
    touch(el, 'touchstart', 200, 10, 0);
    touch(el, 'touchmove', 150, 12, 50);
    touch(el, 'touchend', 100, 12, 100);
    expect(onSwipe).toHaveBeenCalledWith('left');
  });

  it('drag on the y axis reports the end sample and blocks the page scroll; an upward drag is handed back', () => {
    const onEnd = vi.fn();
    const { getByTestId } = renderUi(<GestureBox onEnd={onEnd} />);
    const el = getByTestId('g');
    touch(el, 'touchstart', 10, 10, 0);
    const move = touch(el, 'touchmove', 10, 60, 50);
    expect(move.defaultPrevented).toBe(true);
    touch(el, 'touchend', 10, 110, 100);
    expect(onEnd).toHaveBeenCalledTimes(1);
    expect((onEnd.mock.calls[0]?.[0] as DragSample).dy).toBe(100);

    touch(el, 'touchstart', 10, 100, 200);
    const up = touch(el, 'touchmove', 10, 50, 250);
    expect(up.defaultPrevented).toBe(false);
    touch(el, 'touchend', 10, 40, 300);
    expect(onEnd).toHaveBeenCalledTimes(1);
  });
});
