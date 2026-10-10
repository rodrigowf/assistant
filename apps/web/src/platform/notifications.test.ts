import { describe, expect, it, vi } from 'vitest';
import {
  NOTIFICATION_CLICK_MESSAGE,
  notificationPermission,
  onNotificationClick,
  requestNotificationPermission,
  showSystemNotification,
  type NotifyWindow,
} from './notifications';

/** A fake Notification constructor with a settable permission and recorded instances. */
function fakeNotification(permission: string, opts: { throws?: boolean; callbackOnly?: boolean; answer?: string } = {}) {
  const made: { title: string; options: Record<string, unknown>; onclick: ((e: unknown) => void) | null; closed: boolean }[] = [];
  class N {
    static permission = permission;
    static requestPermission(cb?: (p: string) => void): Promise<string> | undefined {
      const answer = opts.answer ?? 'granted';
      N.permission = answer;
      if (opts.callbackOnly) {
        cb?.(answer);
        return undefined;
      }
      return Promise.resolve(answer);
    }
    onclick: ((e: unknown) => void) | null = null;
    closed = false;
    constructor(
      public title: string,
      public options: Record<string, unknown> = {},
    ) {
      if (opts.throws) throw new TypeError('Illegal constructor');
      made.push(this);
    }
    close(): void {
      this.closed = true;
    }
  }
  return { N: N as unknown as NotifyWindow['Notification'], made };
}

describe('notificationPermission', () => {
  it('maps the browser state; http origins and missing APIs are explained, not "denied"', () => {
    expect(notificationPermission({ isSecureContext: false, Notification: fakeNotification('granted').N })).toBe('insecure');
    expect(notificationPermission({ isSecureContext: true })).toBe('unsupported');
    expect(notificationPermission({ isSecureContext: true, Notification: fakeNotification('granted').N })).toBe('granted');
    expect(notificationPermission({ isSecureContext: true, Notification: fakeNotification('denied').N })).toBe('denied');
    expect(notificationPermission({ isSecureContext: true, Notification: fakeNotification('default').N })).toBe('default');
  });
});

describe('requestNotificationPermission', () => {
  it('promise form and the old callback form (Safari before 15)', async () => {
    expect(await requestNotificationPermission({ isSecureContext: true, Notification: fakeNotification('default').N })).toBe('granted');
    expect(
      await requestNotificationPermission({ isSecureContext: true, Notification: fakeNotification('default', { callbackOnly: true, answer: 'denied' }).N }),
    ).toBe('denied');
  });

  it('does not prompt when the answer is already known', async () => {
    const { N } = fakeNotification('denied');
    const spy = vi.spyOn(N as unknown as { requestPermission: () => void }, 'requestPermission');
    expect(await requestNotificationPermission({ isSecureContext: true, Notification: N })).toBe('denied');
    expect(spy).not.toHaveBeenCalled();
  });
});

describe('showSystemNotification', () => {
  const notice = { title: 'Energy', body: 'Done', tag: 'archie-turn:A1', data: { kind: 'agent-turn', localId: 'A1' } };

  it('goes through the service worker registration when there is one', async () => {
    const showNotification = vi.fn(() => Promise.resolve());
    const { N, made } = fakeNotification('granted');
    const win: NotifyWindow = {
      isSecureContext: true,
      Notification: N,
      navigator: { serviceWorker: { getRegistration: () => Promise.resolve({ active: {}, showNotification }) } },
    };
    expect(await showSystemNotification(notice, () => undefined, win)).toBe('sw');
    expect(showNotification).toHaveBeenCalledWith('Energy', expect.objectContaining({ body: 'Done', tag: 'archie-turn:A1', renotify: true }));
    expect(made).toHaveLength(0);
  });

  it('a registration still installing: waits for `ready`, else falls back to the page', async () => {
    const showNotification = vi.fn(() => Promise.resolve());
    const { N, made } = fakeNotification('granted');
    const ready: NotifyWindow = {
      isSecureContext: true,
      Notification: N,
      navigator: {
        serviceWorker: {
          getRegistration: () => Promise.resolve({ showNotification }),
          ready: Promise.resolve({ active: {}, showNotification }),
        },
      },
    };
    expect(await showSystemNotification(notice, () => undefined, ready)).toBe('sw');
    vi.useFakeTimers();
    try {
      const never: NotifyWindow = {
        isSecureContext: true,
        Notification: N,
        navigator: { serviceWorker: { getRegistration: () => Promise.resolve({ showNotification }), ready: new Promise(() => undefined) } },
      };
      const p = showSystemNotification(notice, () => undefined, never);
      await vi.advanceTimersByTimeAsync(2000);
      expect(await p).toBe('page');
      expect(made).toHaveLength(1);
      expect(showNotification).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('falls back to the page Notification; its click focuses and runs the handler', async () => {
    const { N, made } = fakeNotification('granted');
    const focus = vi.fn();
    const onClick = vi.fn();
    const win: NotifyWindow = { isSecureContext: true, Notification: N, focus, navigator: { serviceWorker: { getRegistration: () => Promise.resolve(undefined) } } };
    expect(await showSystemNotification(notice, onClick, win)).toBe('page');
    made[0]?.onclick?.({});
    expect(focus).toHaveBeenCalled();
    expect(onClick).toHaveBeenCalled();
    expect(made[0]?.closed).toBe(true);
  });

  it('reports failure without permission or when the constructor is refused (Android Chrome)', async () => {
    expect(await showSystemNotification(notice, () => undefined, { isSecureContext: true, Notification: fakeNotification('default').N })).toBe('failed');
    expect(await showSystemNotification(notice, () => undefined, { isSecureContext: true, Notification: fakeNotification('granted', { throws: true }).N })).toBe(
      'failed',
    );
  });
});

describe('onNotificationClick', () => {
  it('passes only our service-worker message on', () => {
    let handler: ((ev: { data?: unknown }) => void) | null = null;
    const win: NotifyWindow = {
      navigator: {
        serviceWorker: {
          addEventListener: (_t, fn) => {
            handler = fn;
          },
          removeEventListener: () => {
            handler = null;
          },
        },
      },
    };
    const fn = vi.fn();
    const off = onNotificationClick(fn, win);
    const deliver = (data: unknown): void => (handler as ((ev: { data?: unknown }) => void) | null)?.({ data });
    deliver({ type: 'other' });
    deliver({ type: NOTIFICATION_CLICK_MESSAGE, localId: 'A1' });
    expect(fn).toHaveBeenCalledTimes(1);
    expect(fn).toHaveBeenCalledWith({ type: NOTIFICATION_CLICK_MESSAGE, localId: 'A1' });
    off();
    expect(handler).toBeNull();
  });
});
