// Service worker for PWA installability (spec 13 §1.7, inv02 §1.10).
// Registered only by the production main build at '/' (scripts/vite-plugin-html-target.ts).
// Navigations: network-first with a cached index.html fallback. Everything else: network only.
// Notifications: the app posts its "agent session finished" notices through this registration
// (src/platform/notifications.ts; Android Chrome has no page Notification); a click focuses an
// open Archie page and tells it which session to open, or opens a new one with ?open_session=.
// Cache 'assistant-v2' replaces 'assistant-v1' (the old app); activate deletes every other cache.

const CACHE_NAME = 'assistant-v2';
const SCOPE = new URL(self.registration.scope).pathname; // '/' in production
const INDEX = SCOPE + 'index.html';

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll([SCOPE, INDEX])));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  if (event.request.mode === 'navigate') {
    event.respondWith(fetch(event.request).catch(() => caches.match(INDEX)));
  }
  // Other requests are not intercepted (network only).
});

// Only the app shell (hash routing: '/' or '/index.html'), never another same-origin page
// (/legacy/, /compat/, a visualization, a memory file) that happens to share the origin.
const isShell = (client) => {
  const path = new URL(client.url).pathname;
  return path === SCOPE || path === INDEX;
};

self.addEventListener('notificationclick', (event) => {
  const data = (event.notification && event.notification.data) || {};
  event.notification.close();
  if (typeof data.kind !== 'string') return; // not one of ours
  // 'agent-turn' opens its session; any other kind (the settings test notice) just brings Archie forward.
  const target = data.kind === 'agent-turn' && data.localId ? data : null;
  const message = target && { type: 'archie:notification-click', kind: target.kind, localId: target.localId, sdkId: target.sdkId || null };
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
      const pages = list.filter(isShell);
      const page = pages.find((c) => c.focused) || pages.find((c) => c.visibilityState === 'visible') || pages[0];
      if (page) {
        return page.focus().then(
          (focused) => message && (focused || page).postMessage(message),
          () => message && page.postMessage(message),
        );
      }
      const url = new URL(SCOPE, self.location.origin);
      if (target) {
        url.searchParams.set('open_session', target.localId);
        if (target.sdkId) url.searchParams.set('open_sdk', target.sdkId);
      }
      return self.clients.openWindow(url.href);
    }),
  );
});
