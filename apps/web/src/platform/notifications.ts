/**
 * System notifications (the "agent session finished" notices, spec 12 §3.7 / §8.2). Feature-
 * detected, never assumed: the compat build's iOS 12 Safari has no Notification API at all, an
 * http origin gets none (secure context only), and Chrome on Android refuses `new Notification()`
 * ("Illegal constructor"): there only the service worker (`registration.showNotification`, main
 * build at `/`, public-main/sw.js) can post. The globals are reached through typed lookups so the
 * compat lint (eslint-plugin-compat) sees no unguarded use.
 */

/** `unsupported`: no Notification API. `insecure`: an http origin (no secure context). */
export type NotifyPermission = 'granted' | 'denied' | 'default' | 'unsupported' | 'insecure';

interface NotificationLike {
  onclick: ((ev: unknown) => void) | null;
  close(): void;
}

interface NotificationCtor {
  readonly permission: string;
  requestPermission(callback?: (p: string) => void): Promise<string> | undefined;
  new (title: string, options?: Record<string, unknown>): NotificationLike;
}

interface RegistrationLike {
  /** The activated worker; `showNotification` rejects without one. */
  active?: unknown;
  showNotification?: (title: string, options?: Record<string, unknown>) => Promise<void>;
}

interface ServiceWorkerContainerLike {
  getRegistration?: () => Promise<RegistrationLike | undefined>;
  /** Resolves with the registration once it has an active worker. */
  ready?: Promise<RegistrationLike>;
  addEventListener?: (type: 'message', fn: (ev: { data?: unknown }) => void) => void;
  removeEventListener?: (type: 'message', fn: (ev: { data?: unknown }) => void) => void;
}

export interface NotifyWindow {
  Notification?: NotificationCtor;
  isSecureContext?: boolean;
  navigator?: { serviceWorker?: ServiceWorkerContainerLike };
  focus?: () => void;
}

function currentWindow(): NotifyWindow {
  return (typeof window !== 'undefined' ? window : {}) as unknown as NotifyWindow;
}

function ctorOf(win: NotifyWindow): NotificationCtor | null {
  const N = win.Notification;
  return typeof N === 'function' ? N : null;
}

function mapPermission(p: unknown): NotifyPermission {
  return p === 'granted' || p === 'denied' ? p : 'default';
}

/** What this browser allows right now. */
export function notificationPermission(win: NotifyWindow = currentWindow()): NotifyPermission {
  if (win.isSecureContext === false) return 'insecure';
  const N = ctorOf(win);
  if (!N) return 'unsupported';
  return mapPermission(N.permission);
}

/**
 * Ask for permission. MUST be called from the user's click (the gesture is what lets the browser
 * show its prompt). Handles the old callback form (Safari before 15) and the promise form.
 */
export function requestNotificationPermission(win: NotifyWindow = currentWindow()): Promise<NotifyPermission> {
  const now = notificationPermission(win);
  const N = ctorOf(win);
  if (!N || now === 'insecure' || now === 'unsupported' || now !== 'default') return Promise.resolve(now);
  return new Promise((resolve) => {
    let done = false;
    const finish = (p: unknown): void => {
      if (done) return;
      done = true;
      resolve(mapPermission(p));
    };
    try {
      const r = N.requestPermission(finish);
      if (r && typeof r.then === 'function') r.then(finish, () => finish(N.permission));
    } catch {
      finish(N.permission);
    }
  });
}

export interface SystemNotice {
  title: string;
  body: string;
  /** Same tag = the newer notice replaces the older one. */
  tag: string;
  icon?: string;
  /** Handed back by the service worker on a click (structured-cloneable). */
  data?: Record<string, unknown>;
}

/** How a notice went out: the service worker, the page's own Notification, or not at all. */
export type NoticePath = 'sw' | 'page' | 'failed';

const SW_LOOKUP_TIMEOUT_MS = 1500;

function withTimeout<T>(p: Promise<T>, ms: number): Promise<T | undefined> {
  return new Promise((resolve) => {
    const t = setTimeout(() => resolve(undefined), ms);
    p.then(
      (v) => {
        clearTimeout(t);
        resolve(v);
      },
      () => {
        clearTimeout(t);
        resolve(undefined);
      },
    );
  });
}

/**
 * Post a system notification. The service worker path first (works everywhere the SW is
 * registered, and is the only one on Android Chrome); its clicks come back through
 * `onNotificationClick`. Otherwise `new Notification`, whose click runs `onPageClick` here.
 */
export async function showSystemNotification(
  n: SystemNotice,
  onPageClick: () => void,
  win: NotifyWindow = currentWindow(),
): Promise<NoticePath> {
  if (notificationPermission(win) !== 'granted') return 'failed';
  const options: Record<string, unknown> = { body: n.body, tag: n.tag, renotify: true, data: n.data ?? {} };
  if (n.icon) options.icon = n.icon;
  const sw = win.navigator?.serviceWorker;
  if (sw && typeof sw.getRegistration === 'function') {
    try {
      let reg = await withTimeout(sw.getRegistration(), SW_LOOKUP_TIMEOUT_MS);
      // Registered but still installing (first load): wait briefly for it to activate.
      if (reg && !reg.active && sw.ready) reg = (await withTimeout(sw.ready, SW_LOOKUP_TIMEOUT_MS)) ?? reg;
      if (reg && reg.active && typeof reg.showNotification === 'function') {
        await reg.showNotification(n.title, options);
        return 'sw';
      }
    } catch {
      // fall through to the page notification
    }
  }
  const N = ctorOf(win);
  if (!N) return 'failed';
  try {
    const shown = new N(n.title, options);
    shown.onclick = () => {
      try {
        win.focus?.();
      } catch {
        // some browsers refuse focus without a gesture; the click handler still runs
      }
      onPageClick();
      shown.close();
    };
    return 'page';
  } catch {
    return 'failed'; // Android Chrome without a service worker: "Illegal constructor"
  }
}

/** The message public-main/sw.js posts to the app after a notification click. */
export const NOTIFICATION_CLICK_MESSAGE = 'archie:notification-click';

/** Subscribe to service-worker notification clicks. Returns an unsubscribe. */
export function onNotificationClick(fn: (data: Record<string, unknown>) => void, win: NotifyWindow = currentWindow()): () => void {
  const sw = win.navigator?.serviceWorker;
  if (!sw || typeof sw.addEventListener !== 'function') return () => undefined;
  const h = (ev: { data?: unknown }): void => {
    const d = ev.data;
    if (d && typeof d === 'object' && (d as { type?: unknown }).type === NOTIFICATION_CLICK_MESSAGE) fn(d as Record<string, unknown>);
  };
  sw.addEventListener('message', h);
  return () => sw.removeEventListener?.('message', h);
}
