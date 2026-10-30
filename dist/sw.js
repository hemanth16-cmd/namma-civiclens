// Namma CivicLens service worker: Web Push notifications only. It does not cache pages or
// intercept network requests, so the app behaves exactly as before when it is installed.
//
// push               shows a system notification (Windows Action Center, Android shade) even when
//                    no CivicLens page is open. The payload is decrypted by the browser; it holds a
//                    notification ID, title, short body and an in-app link (#/...), nothing private.
// notificationclick  focuses an open CivicLens window and tells it which notification was opened,
//                    or opens a new window at ?n=<id>&a=<audience>#/<route>; the page then marks the
//                    notification read with its own credentials. Links outside the app are refused.

const FALLBACK = {title: 'Namma CivicLens', body: 'You have a new update.', url: '#/notifications'};
const SAFE_URL = /^#\/[A-Za-z0-9/_-]{0,120}$/;
const SAFE_ID = /^nt_[A-Za-z0-9_-]{16,40}$/;

function readPayload(data) {
  let p = {};
  try { p = data ? data.json() : {}; } catch { p = {}; }
  const audience = p.audience === 'staff' ? 'staff' : 'citizen';
  return {
    id: typeof p.id === 'string' && SAFE_ID.test(p.id) ? p.id : null,
    title: typeof p.title === 'string' && p.title ? p.title.slice(0, 120) : FALLBACK.title,
    body: typeof p.body === 'string' && p.body ? p.body.slice(0, 300) : FALLBACK.body,
    url: typeof p.url === 'string' && SAFE_URL.test(p.url) ? p.url : (audience === 'staff' ? '#/staff/notifications' : FALLBACK.url),
    audience,
    createdAt: typeof p.createdAt === 'string' ? p.createdAt : null
  };
}

/** Where a click goes: the app's own origin and path, with the notification in the query. */
function launchUrl(data) {
  const url = new URL(self.registration.scope);
  if (data.id) {
    url.searchParams.set('n', data.id);
    url.searchParams.set('a', data.audience);
  }
  url.hash = (SAFE_URL.test(data.url) ? data.url : FALLBACK.url).slice(1);
  return url.href;
}

async function tellClients(message) {
  const windows = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
  for (const client of windows) client.postMessage(message);
}

async function showPush(event) {
  const data = readPayload(event.data);
  const timestamp = Date.parse(data.createdAt || '');
  await self.registration.showNotification(data.title, {
    body: data.body,
    tag: data.id || undefined,
    icon: 'icons/notification-192.png',
    badge: 'icons/notification-badge-96.png',
    timestamp: Number.isFinite(timestamp) ? timestamp : Date.now(),
    data: {id: data.id, url: data.url, audience: data.audience}
  });
  await tellClients({type: 'civiclens:notification-received', id: data.id, audience: data.audience});
}

async function openFromNotification(raw) {
  const data = {id: SAFE_ID.test(raw?.id || '') ? raw.id : null, url: SAFE_URL.test(raw?.url || '') ? raw.url : FALLBACK.url,
    audience: raw?.audience === 'staff' ? 'staff' : 'citizen'};
  const origin = new URL(self.registration.scope).origin;
  const windows = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
  const existing = windows.find(c => new URL(c.url).origin === origin);
  if (existing) {
    // Navigating would reload the page and end an in-memory staff session; the page routes itself.
    // The message goes first, so the page still routes if the browser refuses to focus the window.
    existing.postMessage({type: 'civiclens:notification-click', ...data});
    try {
      return (await existing.focus()) || existing;
    } catch {
      return existing;
    }
  }
  return self.clients.openWindow(launchUrl(data));
}

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('push', event => event.waitUntil(showPush(event)));
self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil(openFromNotification(event.notification.data));
});
// The browser replaced this device's subscription. Open pages re-register it with their credentials;
// the old endpoint answers 410 Gone at the next delivery and the server deletes it.
self.addEventListener('pushsubscriptionchange', event => {
  event.waitUntil(tellClients({type: 'civiclens:subscription-changed'}));
});
