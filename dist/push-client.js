// Browser side of Web Push: service worker registration, the explicit permission request, push
// subscriptions per audience, the citizen inbox token, and handling clicks on notifications.
//
// Audiences: "citizen" uses this device's inbox token (localStorage, so history and push keep
// working after the browser is closed; it grants access to notifications only); "staff" uses the
// in-memory staff session. One browser has one push subscription; it can be registered for both
// audiences, and the server records one device row per audience. Permission is only ever requested
// from a click handler, never on page load. Everything takes an `env` object so tests can inject
// fakes; in the browser it defaults to the real globals.

const INBOX_KEY = 'ncl-notification-inbox';
const deviceKey = audience => `ncl-push-device:${audience}`;
const SAFE_URL = /^#\/[A-Za-z0-9/_-]{0,120}$/;
const SAFE_ID = /^nt_[A-Za-z0-9_-]{16,40}$/;

const browser = () => globalThis;

function storageOf(env) {
  try { return env.localStorage ?? null; } catch { return null; }
}

/** {supported, reason}: reason explains, in user terms, why push cannot work here. */
export function pushSupport(env = browser()) {
  if (!env.isSecureContext) return {supported: false, reason: 'Notifications need a secure page (https://, or http://localhost on this computer).'};
  if (!env.navigator?.serviceWorker) return {supported: false, reason: 'This browser does not support background notifications (no service workers).'};
  if (!('PushManager' in env)) return {supported: false, reason: 'This browser does not support Web Push. On iPhone and iPad, add CivicLens to the Home Screen first.'};
  if (!('Notification' in env)) return {supported: false, reason: 'This browser cannot show system notifications.'};
  return {supported: true, reason: ''};
}

/** "granted" | "denied" | "default" | "unsupported" */
export function permissionState(env = browser()) {
  return pushSupport(env).supported ? env.Notification.permission : 'unsupported';
}

/**
 * Ask for notification permission. Call it first in a click handler, before any network request:
 * browsers (Firefox in particular) accept the request only while the click is still being handled.
 */
export async function requestPermissionNow(env = browser()) {
  if (!pushSupport(env).supported) return 'unsupported';
  return env.Notification.permission === 'default' ? env.Notification.requestPermission() : env.Notification.permission;
}

export const inbox = {
  get(env = browser()) {
    try {
      const v = storageOf(env)?.getItem(INBOX_KEY);
      return typeof v === 'string' && /^[A-Za-z0-9_-]{20,200}$/.test(v) ? v : null;
    } catch { return null; }
  },
  set(token, env = browser()) { try { storageOf(env)?.setItem(INBOX_KEY, token); } catch { /* storage blocked: push still works this visit */ } },
  clear(env = browser()) { try { storageOf(env)?.removeItem(INBOX_KEY); } catch { /* ignore */ } }
};

/** This device's citizen inbox token, created on the server the first time. */
export async function ensureInbox(http, env = browser()) {
  const existing = inbox.get(env);
  if (existing) return existing;
  const token = await http.createInbox();
  inbox.set(token, env);
  return token;
}

export function registeredDevice(audience, env = browser()) {
  try { return storageOf(env)?.getItem(deviceKey(audience)) || null; } catch { return null; }
}

function rememberDevice(audience, id, env) {
  try { id ? storageOf(env)?.setItem(deviceKey(audience), id) : storageOf(env)?.removeItem(deviceKey(audience)); } catch { /* ignore */ }
}

export function urlBase64ToUint8Array(value) {
  const base64 = (value + '='.repeat((4 - value.length % 4) % 4)).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(base64);
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}

function sameKey(a, b) {
  if (!a || !b) return false;
  const x = new Uint8Array(a), y = new Uint8Array(b);
  return x.length === y.length && x.every((v, i) => v === y[i]);
}

/** A short device label such as "Chrome on Windows" (no version numbers or identifiers). */
export function deviceLabel(nav = browser().navigator) {
  const ua = String(nav?.userAgent || '');
  const os = /Android/.test(ua) ? 'Android' : /Windows/.test(ua) ? 'Windows' : /iPhone|iPad/.test(ua) ? 'iOS' : /Mac OS X/.test(ua) ? 'macOS' : /Linux/.test(ua) ? 'Linux' : 'this device';
  const name = /Edg\//.test(ua) ? 'Edge' : /SamsungBrowser/.test(ua) ? 'Samsung Internet' : /Firefox\//.test(ua) ? 'Firefox' : /Chrome\//.test(ua) ? 'Chrome' : /Safari\//.test(ua) ? 'Safari' : 'Browser';
  return `${name} on ${os}`;
}

export async function registerServiceWorker(env = browser()) {
  const registration = await env.navigator.serviceWorker.register('sw.js');
  await env.navigator.serviceWorker.ready;
  return registration;
}

/**
 * Turn notifications on for `audience` on this device. Must be called from a click (the browser
 * asks the user for permission here). Resolves to {state: 'enabled', device} or
 * {state: 'denied' | 'dismissed' | 'unsupported' | 'unavailable', message}.
 */
export async function enablePush({http, token, audience, config, env = browser()}) {
  const support = pushSupport(env);
  if (!support.supported) return {state: 'unsupported', message: support.reason};
  if (!config?.pushEnabled || !config.vapidPublicKey) return {state: 'unavailable', message: 'The CivicLens server has not been set up to send push notifications yet. Notifications are still kept in your history below.'};
  let permission = env.Notification.permission;
  if (permission === 'default') permission = await env.Notification.requestPermission();
  if (permission === 'denied') return {state: 'denied', message: 'Notifications are blocked for this site. Allow them in the browser’s site settings (the icon beside the address), then try again.'};
  if (permission !== 'granted') return {state: 'dismissed', message: 'Notifications were not turned on. You can try again at any time.'};
  const registration = await registerServiceWorker(env);
  const key = urlBase64ToUint8Array(config.vapidPublicKey);
  let subscription = await registration.pushManager.getSubscription();
  if (subscription && !sameKey(subscription.options?.applicationServerKey, key)) {
    await subscription.unsubscribe();  // made for another server key: it would never receive our messages
    subscription = null;
  }
  subscription ??= await registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: key});
  const device = await http.registerPushSubscription(token, subscription.toJSON(), deviceLabel(env.navigator));
  rememberDevice(audience, device.subscriptionId, env);
  return {state: 'enabled', device};
}

/** Re-register an existing subscription (after the browser rotated it). No permission prompt. */
export async function refreshPush({http, token, audience, env = browser()}) {
  if (!registeredDevice(audience, env) || permissionState(env) !== 'granted') return null;
  const registration = await env.navigator.serviceWorker.getRegistration();
  const subscription = await registration?.pushManager.getSubscription();
  if (!subscription) return null;
  const device = await http.registerPushSubscription(token, subscription.toJSON(), deviceLabel(env.navigator));
  rememberDevice(audience, device.subscriptionId, env);
  return device;
}

/**
 * Turn notifications off for `audience` on this device: the server forgets the device, and the
 * browser subscription is removed once no audience uses it. History is kept on the server.
 */
export async function disablePush({http, token, audience, env = browser()}) {
  const id = registeredDevice(audience, env);
  if (id) {
    try {
      await http.removePushDevice(token, id);
    } catch (err) {
      if (err?.status !== 404) throw err;  // already gone on the server
    }
  }
  rememberDevice(audience, null, env);
  const other = audience === 'staff' ? 'citizen' : 'staff';
  if (!registeredDevice(other, env) && pushSupport(env).supported) {
    const registration = await env.navigator.serviceWorker.getRegistration();
    const subscription = await registration?.pushManager.getSubscription();
    await subscription?.unsubscribe();
  }
}

/** "unsupported" | "denied" | "enabled" (this device is registered for the audience) | "off" */
export function pushState(audience, env = browser()) {
  const permission = permissionState(env);
  if (permission === 'unsupported') return 'unsupported';
  if (permission === 'denied') return 'denied';
  return permission === 'granted' && registeredDevice(audience, env) ? 'enabled' : 'off';
}

// ---------- Opening notifications ----------

// Staff clicks that arrive before the staff member has signed in in this tab (staff sessions are
// memory-only): marked read after sign-in by takePendingStaffReads().
const pendingStaffReads = new Set();

export function takePendingStaffReads() {
  const ids = [...pendingStaffReads];
  pendingStaffReads.clear();
  return ids;
}

/** Reads ?n=<id>&a=<audience> left by the service worker when it opened this window, and removes it from the address. */
export function takeLaunchNotification(env = browser()) {
  const url = new URL(env.location.href);
  if (!url.searchParams.has('n') && !url.searchParams.has('a')) return null;
  const id = url.searchParams.get('n');
  const audience = url.searchParams.get('a') === 'staff' ? 'staff' : 'citizen';
  url.searchParams.delete('n');
  url.searchParams.delete('a');
  env.history?.replaceState?.(env.history.state, '', url.pathname + url.search + url.hash);
  if (!id || !SAFE_ID.test(id)) return null;
  return {id, audience};
}

/**
 * A notification was opened: mark it read with this audience's credential (citizen: the inbox
 * token; staff: the in-memory session, or later if not signed in yet), and go to its page.
 * `go(hash)` navigates; `staffToken()` returns the current staff token or null.
 */
export async function openNotification({id, audience, url}, {http, staffToken, go, env = browser()}) {
  if (url && SAFE_URL.test(url)) go(url.slice(2));
  if (!id || !SAFE_ID.test(id) || !http) return;
  const token = audience === 'staff' ? staffToken() : inbox.get(env);
  if (!token) {
    if (audience === 'staff') pendingStaffReads.add(id);
    return;
  }
  try { await http.markNotification(token, id, true); } catch { /* best-effort; it stays unread in history */ }
}
