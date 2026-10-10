/**
 * Floating voice controls (VoiceOverlay): the same dock controls, the idle pill after 4 s of no
 * activity, wake on activity (window and same-origin iframes), the first press elsewhere cannot
 * click the controls, holds (hover, keyboard focus, states that need the user), the mic-state
 * wake, and the pure placement / snap helpers.
 */
import { act, fireEvent, render, screen } from '@testing-library/react';
import { createRef } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { expectNoAxeViolations } from '@/test/axe';
import { OFF_SNAPSHOT, type VoiceSnapshot } from '@/voice';
import { IDLE_AFTER_MS, needsAttention, overlayBox, snapAnchor, WAKE_GUARD_MS } from '../overlay';
import type { VoiceDockViewProps } from '../VoiceDock';
import { FloatingVoiceDock, VoicePill } from '../VoiceOverlay';

const snap = (over: Partial<VoiceSnapshot> = {}): VoiceSnapshot => ({ ...OFF_SNAPSHOT, status: 'active', ...over });

function dock(over: Partial<VoiceSnapshot> = {}): VoiceDockViewProps & { calls: string[] } {
  const calls: string[] = [];
  const f = (n: string) => () => {
    calls.push(n);
  };
  return {
    calls,
    snapshot: snap(over),
    now: 0,
    onEnd: f('end'),
    onToggleMic: f('mic'),
    onToggleSpeaker: f('speaker'),
    onInterrupt: f('interrupt'),
    onRetry: f('retry'),
    onReconnect: f('reconnect'),
    onDismiss: f('dismiss'),
  };
}

const region = { left: 400, top: 0, width: 1000, height: 800 };

describe('placement helpers', () => {
  it('bottom-centre in the workspace gutters, lifted above a composer; top anchors clear the top bar', () => {
    expect(overlayBox('bottom-center', region, 800, { compact: false, avoidTop: null })).toEqual({
      left: 428,
      width: 944,
      top: null,
      bottom: 20,
      align: 'center',
      avoided: false,
    });
    expect(overlayBox('bottom-right', region, 800, { compact: false, avoidTop: 690 })).toMatchObject({ bottom: 118, align: 'right', avoided: true });
    expect(overlayBox('top-left', { left: 0, top: 0, width: 412, height: 900 }, 900, { compact: true, avoidTop: 800 })).toMatchObject({
      left: 12,
      width: 388,
      top: 72,
      bottom: null,
      align: 'left',
    });
  });

  it('a drop snaps by thirds across and halves down', () => {
    expect(snapAnchor({ x: 450, y: 100 }, region)).toBe('top-left');
    expect(snapAnchor({ x: 900, y: 700 }, region)).toBe('bottom-center');
    expect(snapAnchor({ x: 1350, y: 500 }, region)).toBe('bottom-right');
  });

  it('states that need the user keep the controls up', () => {
    expect(needsAttention(snap())).toBe(false);
    expect(needsAttention(snap({ status: 'speaking' }))).toBe(false);
    expect(needsAttention(snap({ status: 'connecting' }))).toBe(true);
    expect(needsAttention(snap({ status: 'error' }))).toBe(true);
    expect(needsAttention(snap({ link: 'lost' }))).toBe(true);
    expect(needsAttention(snap({ banner: { kind: 'reconnect_warning', message: 'x', timeLeftS: 5 } as VoiceSnapshot['banner'] }))).toBe(true);
  });
});

describe('VoicePill', () => {
  it('names the call state and shows the mic state; a tap expands', async () => {
    const onExpand = vi.fn();
    const { container, rerender } = render(<VoicePill snapshot={snap()} onExpand={onExpand} />);
    const pill = screen.getByRole('button', { name: 'Voice call: Listening. Show voice controls' });
    fireEvent.click(pill);
    expect(onExpand).toHaveBeenCalled();
    await expectNoAxeViolations(container);
    rerender(<VoicePill snapshot={snap({ micMuted: true })} onExpand={onExpand} />);
    expect(screen.getByRole('button', { name: 'Voice call: Muted. Show voice controls' })).toBeTruthy();
  });
});

describe('FloatingVoiceDock', () => {
  let main: HTMLElement;
  beforeEach(() => {
    vi.useFakeTimers();
    main = document.createElement('main');
    document.body.appendChild(main);
  });
  afterEach(() => {
    vi.useRealTimers();
    main.remove();
    document.querySelectorAll('iframe').forEach((f) => f.remove());
  });

  function mount(d = dock(), onOpen = vi.fn()) {
    const ref = createRef<HTMLElement>();
    (ref as { current: HTMLElement | null }).current = main;
    const onAnchor = vi.fn();
    const utils = render(<FloatingVoiceDock dock={d} regionRef={ref} compact={false} anchor="bottom-center" onAnchorChange={onAnchor} onOpenConversation={onOpen} />);
    const rerender = (next: VoiceDockViewProps): void =>
      utils.rerender(<FloatingVoiceDock dock={next} regionRef={ref} compact={false} anchor="bottom-center" onAnchorChange={onAnchor} onOpenConversation={onOpen} />);
    return { ...utils, rerender, onOpen, onAnchor };
  }

  const pill = () => screen.queryByRole('button', { name: /Show voice controls/ });
  const controls = () => screen.queryByRole('group', { name: 'Voice controls' });
  const idleNow = (): void => {
    act(() => {
      vi.advanceTimersByTime(IDLE_AFTER_MS + 10);
    });
  };

  it('the same controls as the dock, in a "Voice call" region; the state text opens Archie', () => {
    const d = dock();
    const { container, onOpen } = mount(d);
    expect(screen.getByRole('region', { name: 'Voice call' })).toBeTruthy();
    expect(container.querySelector('[data-voice-overlay]')).toBeTruthy();
    expect(controls()).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Mute microphone' }));
    fireEvent.click(screen.getByRole('button', { name: 'End voice' }));
    expect(d.calls).toEqual(['mic', 'end']);
    fireEvent.click(screen.getByRole('button', { name: 'Open the Archie conversation' }));
    expect(onOpen).toHaveBeenCalled();
  });

  it('shrinks to the faded pill after 4 s still; a mouse move, a key or a wheel wakes it', () => {
    mount();
    act(() => {
      vi.advanceTimersByTime(IDLE_AFTER_MS - 500);
    });
    expect(controls()).toBeTruthy();
    idleNow();
    expect(controls()).toBeNull();
    expect(pill()).toBeTruthy();
    expect(screen.getByRole('region', { name: 'Voice call' }).hasAttribute('data-idle')).toBe(true);

    fireEvent.mouseMove(document.body, { clientX: 10, clientY: 10 });
    expect(controls()).toBeTruthy();
    idleNow();
    // the browser's synthetic hover update (same coordinates) is not activity
    fireEvent.mouseMove(document.body, { clientX: 10, clientY: 10 });
    expect(controls()).toBeNull();
    fireEvent.keyDown(document.body, { key: 'ArrowDown' });
    expect(controls()).toBeTruthy();
    idleNow();
    fireEvent.wheel(document.body, { deltaY: 40 });
    expect(controls()).toBeTruthy();
  });

  it('activity keeps it up: the countdown restarts on every move', () => {
    mount();
    for (let i = 0; i < 5; i += 1) {
      act(() => {
        vi.advanceTimersByTime(IDLE_AFTER_MS - 1000);
      });
      fireEvent.mouseMove(document.body, { clientX: i * 5 + 1, clientY: 3 });
    }
    expect(controls()).toBeTruthy();
  });

  it('a tap on the pill only expands it (no control fires)', () => {
    const d = dock();
    mount(d);
    idleNow();
    const p = pill();
    if (!p) throw new Error('no pill');
    fireEvent.mouseDown(p);
    expect(controls()).toBeNull(); // presses inside are the pill's own
    fireEvent.click(p);
    expect(controls()).toBeTruthy();
    expect(d.calls).toEqual([]);
  });

  it('a press elsewhere wakes it, and cannot click the controls for the guard window', () => {
    const d = dock();
    mount(d);
    idleNow();
    fireEvent.touchStart(document.body, { touches: [{ clientX: 5, clientY: 5 }] });
    expect(controls()).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Mute microphone' }));
    expect(d.calls).toEqual([]);
    act(() => {
      vi.advanceTimersByTime(WAKE_GUARD_MS + 10);
    });
    fireEvent.click(screen.getByRole('button', { name: 'Mute microphone' }));
    expect(d.calls).toEqual(['mic']);
  });

  it('a mouse drag snaps it to the anchor nearest the drop (no click on the controls)', () => {
    main.getBoundingClientRect = () => ({ left: 0, top: 0, width: 1000, height: 800, right: 1000, bottom: 800, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect;
    const d = dock();
    const { onAnchor } = mount(d);
    const frame = screen.getByRole('region', { name: 'Voice call' });
    frame.getBoundingClientRect = () => ({ left: 220, top: 700, width: 560, height: 84, right: 780, bottom: 784, x: 220, y: 700, toJSON: () => ({}) }) as DOMRect;
    fireEvent.mouseDown(screen.getByRole('button', { name: 'Mute microphone' }), { button: 0, clientX: 500, clientY: 760 });
    fireEvent.mouseMove(document, { clientX: 520, clientY: 740 });
    expect(frame.hasAttribute('data-dragging')).toBe(true);
    // dropped with its centre at (900, 142): the right third, the top half
    fireEvent.mouseMove(document, { clientX: 900, clientY: 160 });
    fireEvent.mouseUp(document, { clientX: 900, clientY: 160 });
    fireEvent.click(screen.getByRole('button', { name: 'Mute microphone' }));
    expect(onAnchor).toHaveBeenCalledWith('top-right');
    expect(d.calls).toEqual([]); // the release after a drag is not a click
  });

  it('an iframe added while idle (a visual screen opening) is watched at once', async () => {
    mount();
    idleNow();
    const frame = document.createElement('iframe');
    main.appendChild(frame);
    await act(async () => {
      await Promise.resolve();
      vi.advanceTimersByTime(1);
    });
    const doc = frame.contentDocument;
    if (!doc) throw new Error('no frame document');
    fireEvent.mouseMove(doc.body, { clientX: 31, clientY: 41 });
    expect(controls()).toBeTruthy();
  });

  it('activity inside a same-origin iframe (a visualization) wakes it', () => {
    const frame = document.createElement('iframe');
    main.appendChild(frame);
    mount();
    idleNow();
    const doc = frame.contentDocument;
    if (!doc) throw new Error('no frame document');
    fireEvent.mouseMove(doc.body, { clientX: 30, clientY: 40 });
    expect(controls()).toBeTruthy();
  });

  it('never shrinks while hovered, or while the call needs the user', () => {
    const { rerender } = mount();
    fireEvent.mouseEnter(screen.getByRole('region', { name: 'Voice call' }));
    idleNow();
    idleNow();
    expect(controls()).toBeTruthy();
    fireEvent.mouseLeave(screen.getByRole('region', { name: 'Voice call' }));
    idleNow();
    expect(pill()).toBeTruthy();

    rerender(dock({ link: 'lost', linkLostAt: 0 }));
    expect(screen.getByText('Reconnecting…')).toBeTruthy();
    idleNow();
    idleNow();
    expect(screen.getByText('Reconnecting…')).toBeTruthy();
  });

  it('keeps up with keyboard focus inside (not with a tapped button); a key wakes it so Tab reaches the controls', () => {
    mount();
    idleNow();
    fireEvent.keyDown(document.body, { key: 'Tab' });
    expect(controls()).toBeTruthy();
    act(() => {
      screen.getByRole('button', { name: 'Mute microphone' }).focus();
    });
    idleNow();
    idleNow();
    expect(controls()).toBeTruthy();

    act(() => {
      (document.activeElement as HTMLElement).blur();
    });
    // a pointer press, then focus (Android Chrome focuses a tapped button): no hold
    fireEvent.mouseDown(document.body, { clientX: 1, clientY: 1 });
    act(() => {
      screen.getByRole('button', { name: 'Mute speaker' }).focus();
    });
    idleNow();
    expect(pill()).toBeTruthy();
  });

  it('a pill focused from the keyboard expands into the controls and keeps focus inside', () => {
    mount();
    idleNow();
    const p = pill();
    if (!p) throw new Error('no pill');
    act(() => {
      p.focus(); // programmatic: no key event woke it first
    });
    expect(controls()).toBeNull(); // pointer modality: focusing the pill alone does not expand it
    fireEvent.keyDown(p, { key: 'Enter' });
    fireEvent.click(p);
    expect(controls()).toBeTruthy();
    expect(screen.getByRole('region', { name: 'Voice call' }).contains(document.activeElement)).toBe(true);
  });

  it('a mic mute change wakes it (privacy: the new state is shown in full)', () => {
    const { rerender } = mount();
    idleNow();
    expect(pill()).toBeTruthy();
    rerender(dock({ micMuted: true }));
    expect(controls()).toBeTruthy();
    expect(screen.getByText('Muted')).toBeTruthy();
  });
});
