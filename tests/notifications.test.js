import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const {createApiClient, toNotification} = await import('../dist/api-client.js');
const push = await import('../dist/push-client.js');

const BASE = 'http://localhost:8000/api/v1';
const TOKEN = 'inbox_' + 'T'.repeat(40);
const VAPID = 'BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U';
const NID = 'nt_' + 'a'.repeat(22);
const json = (status, body) => new Response(body === undefined ? null : JSON.stringify(body), {status, headers: {'Content-Type': 'application/json'}});

function recorder(...responses) {
  const calls = [];
  const fn = async (url, init = {}) => {
    calls.push({url, init, body: init.body ? JSON.parse(init.body) : undefined});
    const r = responses[Math.min(calls.length - 1, responses.length - 1)];
    return typeof r === 'function' ? r(url, init) : r.clone();
  };
  fn.calls = calls;
  return fn;
}

const notification = (extra = {}) => ({id: NID, kind: 'evidence_received', title: 'Evidence received', body: 'Your photo for NCL-2026-ABCDEF was stored.',
  url: '#/track/NCL-2026-ABCDEF', complaintId: 'NCL-2026-ABCDEF', createdAt: '2026-10-30T05:00:00.000Z', readAt: null, read: false, pushStatus: 'sent', ...extra});

// ---------- API client ----------

test('notification requests send the bearer token in the header and project only known fields', async () => {
  const fetchImpl = recorder(json(200, {items: [notification({extra: 'x'}), {id: 'bad'}], unreadCount: 3, nextBefore: NID}));
  const page = await createApiClient({baseUrl: BASE, fetchImpl}).listNotifications(TOKEN, {limit: 10, before: NID, unread: true});
  const [call] = fetchImpl.calls;
  assert.equal(call.url, `${BASE}/notifications?limit=10&before=${NID}&unread=true`);
  assert.equal(call.init.headers.Authorization, 'Bearer ' + TOKEN);
  assert.equal(call.init.credentials, 'omit');
  assert.equal(page.items.length, 1, 'malformed items are dropped');
  assert.equal(page.items[0].extra, undefined);
  assert.deepEqual([page.unreadCount, page.nextBefore], [3, NID]);
});

test('notification links are kept only when they are in-app routes', () => {
  for (const url of ['https://evil.example/', 'javascript:alert(1)', '//evil.example', '#/track/<x>', '#/../../x?y']) {
    assert.equal(toNotification(notification({url})).url, '#/notifications', url);
  }
  assert.equal(toNotification(notification({url: '#/staff/NCL-2026-ABCDEF'})).url, '#/staff/NCL-2026-ABCDEF');
  assert.equal(toNotification(notification({complaintId: 'not an id'})).complaintId, null);
});

test('marking, following and device calls use the documented routes', async () => {
  const device = {subscriptionId: 'ps_' + 'b'.repeat(22), label: 'Chrome on Android', createdAt: 'z', lastSuccessAt: null, lastFailureAt: null, failureCount: 0};
  const fetchImpl = recorder(
    json(200, notification({read: true, readAt: 'z'})), json(200, {updated: 4, unreadCount: 0}),
    json(201, device), json(201, {complaintId: 'NCL-2026-ABCDEF', followedAt: 'z'}), json(201, {inboxToken: TOKEN}), json(200, {unreadCount: 2}));
  const http = createApiClient({baseUrl: BASE, fetchImpl});
  assert.equal((await http.markNotification(TOKEN, NID, true)).read, true);
  assert.equal(await http.markAllNotificationsRead(TOKEN), 4);
  const sub = {endpoint: 'https://fcm.googleapis.com/fcm/send/x', expirationTime: null, keys: {p256dh: 'p'.repeat(87), auth: 'a'.repeat(22)}};
  assert.equal((await http.registerPushSubscription(TOKEN, sub, 'Chrome on Android')).subscriptionId, device.subscriptionId);
  await http.followComplaint(TOKEN, 'NCL-2026-ABCDEF', 'CLAIM-CODE');
  assert.equal(await http.createInbox(), TOKEN);
  assert.equal(await http.unreadCount(TOKEN), 2);
  const [read, all, reg, fol, inbox] = fetchImpl.calls;
  assert.deepEqual([read.init.method, read.url], ['POST', `${BASE}/notifications/${NID}/read`]);
  assert.equal(all.url, `${BASE}/notifications/read-all`);
  assert.deepEqual(reg.body, {endpoint: sub.endpoint, keys: sub.keys, expirationTime: null, label: 'Chrome on Android'});
  assert.deepEqual(fol.body, {complaintId: 'NCL-2026-ABCDEF', claim: 'CLAIM-CODE'});
  assert.ok(!fol.url.includes('CLAIM'), 'the claim code never goes in the URL');
  assert.equal(inbox.init.headers.Authorization, undefined);
});

// ---------- push-client ----------

function fakeEnv({permission = 'default', answer = 'granted', existing = null, secure = true} = {}) {
  const store = new Map();
  const calls = {requests: 0, subscribe: [], unsubscribed: 0, registered: 0, replaced: []};
  const makeSub = key => ({
    endpoint: 'https://fcm.googleapis.com/fcm/send/' + Math.random().toString(36).slice(2),
    options: {applicationServerKey: key.buffer},
    toJSON() { return {endpoint: this.endpoint, expirationTime: null, keys: {p256dh: 'p'.repeat(87), auth: 'a'.repeat(22)}}; },
    async unsubscribe() { calls.unsubscribed++; registration.current = null; return true; }
  });
  const registration = {
    current: existing ? makeSub(existing) : null,
    pushManager: {
      async getSubscription() { return registration.current; },
      async subscribe(options) { calls.subscribe.push(options); registration.current = makeSub(options.applicationServerKey); return registration.current; }
    }
  };
  const env = {
    isSecureContext: secure,
    PushManager: function PushManager() {},
    Notification: {permission, async requestPermission() { calls.requests++; env.Notification.permission = answer; return answer; }},
    navigator: {userAgent: 'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/131.0 Mobile Safari/537.36',
      serviceWorker: {async register(url) { calls.registered++; assert.equal(url, 'sw.js'); return registration; }, ready: Promise.resolve(registration),
        async getRegistration() { return registration; }}},
    localStorage: {getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k)},
    location: {href: 'https://civiclens.example/'},
    history: {state: null, replaceState(_s, _t, url) { calls.replaced.push(url); }}
  };
  return {env, calls, registration, store};
}

function fakeHttp() {
  const calls = [];
  return {
    calls,
    async registerPushSubscription(token, sub, label) { calls.push(['register', token, sub.endpoint, label]); return {subscriptionId: 'ps_' + 'c'.repeat(22)}; },
    async removePushDevice(token, id) { calls.push(['remove', token, id]); },
    async markNotification(token, id, read) { calls.push(['mark', token, id, read]); return {}; },
    async createInbox() { calls.push(['inbox']); return TOKEN; }
  };
}

const CONFIG = {pushEnabled: true, vapidPublicKey: VAPID};

test('push support needs a secure page, service workers, PushManager and Notification', () => {
  assert.equal(push.pushSupport(fakeEnv().env).supported, true);
  assert.match(push.pushSupport(fakeEnv({secure: false}).env).reason, /secure/);
  const {env} = fakeEnv();
  delete env.PushManager;
  assert.match(push.pushSupport(env).reason, /Home Screen/);
  assert.equal(push.permissionState(env), 'unsupported');
});

test('enabling push asks permission, subscribes with the VAPID key and registers the device', async () => {
  const {env, calls, store} = fakeEnv();
  const http = fakeHttp();
  const result = await push.enablePush({http, token: TOKEN, audience: 'citizen', config: CONFIG, env});
  assert.equal(result.state, 'enabled');
  assert.equal(calls.requests, 1);
  assert.equal(calls.subscribe[0].userVisibleOnly, true);
  assert.deepEqual([...calls.subscribe[0].applicationServerKey], [...push.urlBase64ToUint8Array(VAPID)]);
  assert.deepEqual(http.calls[0].slice(0, 2), ['register', TOKEN]);
  assert.equal(http.calls[0][3], 'Chrome on Android');
  assert.equal(store.get('ncl-push-device:citizen'), 'ps_' + 'c'.repeat(22));
  assert.equal(push.pushState('citizen', env), 'enabled');
  assert.equal(push.pushState('staff', env), 'off');
  // Enabling again reuses the browser subscription: no new prompt, no new subscription.
  await push.enablePush({http, token: TOKEN, audience: 'citizen', config: CONFIG, env});
  assert.deepEqual([calls.requests, calls.subscribe.length], [1, 1]);
});

test('denied, dismissed, unsupported and unconfigured push never subscribe', async () => {
  for (const [opts, config, state] of [[{permission: 'denied'}, CONFIG, 'denied'], [{answer: 'default'}, CONFIG, 'dismissed'],
    [{secure: false}, CONFIG, 'unsupported'], [{}, {pushEnabled: false, vapidPublicKey: null}, 'unavailable']]) {
    const {env, calls} = fakeEnv(opts);
    const http = fakeHttp();
    const result = await push.enablePush({http, token: TOKEN, audience: 'citizen', config, env});
    assert.equal(result.state, state);
    assert.ok(result.message);
    assert.deepEqual([calls.subscribe.length, http.calls.length], [0, 0], state);
  }
});

test('a subscription made for another server key is replaced', async () => {
  const {env, calls} = fakeEnv({permission: 'granted', existing: new Uint8Array([4, 1, 2, 3])});
  await push.enablePush({http: fakeHttp(), token: TOKEN, audience: 'staff', config: CONFIG, env});
  assert.deepEqual([calls.unsubscribed, calls.subscribe.length, calls.requests], [1, 1, 0]);
});

test('disabling push removes this audience; the browser subscription goes when no audience uses it', async () => {
  const {env, calls, registration} = fakeEnv({permission: 'granted'});
  const http = fakeHttp();
  await push.enablePush({http, token: TOKEN, audience: 'citizen', config: CONFIG, env});
  await push.enablePush({http, token: 'staff-token-' + 'S'.repeat(30), audience: 'staff', config: CONFIG, env});
  await push.disablePush({http, token: 'staff-token-' + 'S'.repeat(30), audience: 'staff', env});
  assert.equal(calls.unsubscribed, 0, 'still used by the citizen inbox');
  assert.ok(registration.current);
  http.removePushDevice = async () => { throw Object.assign(new Error('gone'), {status: 404}); };
  await push.disablePush({http, token: TOKEN, audience: 'citizen', env});
  assert.equal(calls.unsubscribed, 1);
  assert.equal(push.pushState('citizen', env), 'off');
});

test('the inbox token is created once and kept on the device', async () => {
  const {env} = fakeEnv();
  const http = fakeHttp();
  assert.equal(await push.ensureInbox(http, env), TOKEN);
  assert.equal(await push.ensureInbox(http, env), TOKEN);
  assert.deepEqual(http.calls, [['inbox']]);
  push.inbox.clear(env);
  assert.equal(push.inbox.get(env), null);
});

test('a window opened from a notification reads and removes ?n= and ?a=', () => {
  const {env, calls} = fakeEnv();
  env.location.href = `https://civiclens.example/?n=${NID}&a=staff#/staff/NCL-2026-ABCDEF`;
  assert.deepEqual(push.takeLaunchNotification(env), {id: NID, audience: 'staff'});
  assert.deepEqual(calls.replaced, ['/#/staff/NCL-2026-ABCDEF']);
  env.location.href = 'https://civiclens.example/?n=../../evil&a=citizen#/camera';
  assert.equal(push.takeLaunchNotification(env), null, 'invalid IDs are ignored (and still removed)');
  env.location.href = 'https://civiclens.example/#/camera';
  assert.equal(push.takeLaunchNotification(env), null);
});

test('opening a notification marks it read with the right credential and navigates only in-app', async () => {
  const {env} = fakeEnv();
  push.inbox.set(TOKEN, env);
  const http = fakeHttp();
  const visited = [];
  await push.openNotification({id: NID, audience: 'citizen', url: '#/track/NCL-2026-ABCDEF'}, {http, staffToken: () => null, go: p => visited.push(p), env});
  assert.deepEqual(http.calls, [['mark', TOKEN, NID, true]]);
  assert.deepEqual(visited, ['track/NCL-2026-ABCDEF']);
  await push.openNotification({id: NID, audience: 'citizen', url: 'https://evil.example'}, {http, staffToken: () => null, go: p => visited.push(p), env});
  assert.equal(visited.length, 1, 'external links are ignored');
  // Staff not signed in yet: remembered and marked after sign-in.
  await push.openNotification({id: NID, audience: 'staff'}, {http, staffToken: () => null, go: () => {}, env});
  assert.deepEqual(push.takePendingStaffReads(), [NID]);
  assert.deepEqual(push.takePendingStaffReads(), []);
});

test('device labels name the browser and system only', () => {
  assert.equal(push.deviceLabel({userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36 Edg/131.0'}), 'Edge on Windows');
  assert.equal(push.deviceLabel({userAgent: 'Mozilla/5.0 (Windows NT 10.0; rv:133.0) Gecko/20100101 Firefox/133.0'}), 'Firefox on Windows');
  assert.equal(push.deviceLabel({}), 'Browser on this device');
});

// ---------- Service worker ----------

function loadWorker({clients = [], scope = 'https://civiclens.example/'} = {}) {
  const listeners = {};
  const shown = [];
  const posted = [];
  const opened = [];
  const windows = clients.map(url => ({url, focused: false, async focus() { this.focused = true; return this; }, postMessage(m) { posted.push({url, m}); }}));
  const self = {
    addEventListener: (type, fn) => { listeners[type] = fn; },
    skipWaiting() {},
    registration: {scope, async showNotification(title, options) { shown.push({title, options}); }},
    clients: {async matchAll() { return windows; }, async openWindow(url) { opened.push(url); return {url}; }, async claim() {}}
  };
  vm.runInNewContext(readFileSync(new URL('../dist/sw.js', import.meta.url), 'utf8'), {self, URL, Date, Number});
  const fire = async (type, event) => {
    let pending;
    listeners[type]({...event, waitUntil: p => { pending = p; }});
    await pending;
  };
  return {fire, shown, posted, opened, windows};
}

const pushEvent = payload => ({data: {json: () => (typeof payload === 'string' ? JSON.parse(payload) : payload)}});

test('the service worker shows a system notification from the push payload', async () => {
  const sw = loadWorker({clients: ['https://civiclens.example/#/camera']});
  await sw.fire('push', pushEvent({id: NID, kind: 'evidence_received', title: 'Evidence received', body: 'Stored.', url: '#/track/NCL-2026-ABCDEF', audience: 'citizen', createdAt: '2026-10-30T05:00:00.000Z'}));
  const [{title, options}] = sw.shown;
  assert.equal(title, 'Evidence received');
  assert.equal(options.body, 'Stored.');
  assert.equal(options.tag, NID);
  assert.equal(options.icon, 'icons/notification-192.png');
  assert.equal(options.timestamp, Date.parse('2026-10-30T05:00:00.000Z'));
  assert.deepEqual({...options.data}, {id: NID, url: '#/track/NCL-2026-ABCDEF', audience: 'citizen'});
  assert.equal(sw.posted[0].m.type, 'civiclens:notification-received', 'open pages refresh their history');
});

test('malformed or hostile push payloads fall back to safe values', async () => {
  const sw = loadWorker();
  await sw.fire('push', pushEvent('not json'));
  await sw.fire('push', pushEvent({id: '<script>', title: '', url: 'https://evil.example', audience: 'staff'}));
  assert.equal(sw.shown[0].title, 'Namma CivicLens');
  assert.equal(sw.shown[0].options.data.url, '#/notifications');
  assert.equal(sw.shown[1].options.data.id, null);
  assert.equal(sw.shown[1].options.data.url, '#/staff/notifications');
});

test('clicking a notification focuses an open CivicLens window and tells it what was opened', async () => {
  const sw = loadWorker({clients: ['https://other.example/', 'https://civiclens.example/#/camera']});
  let closed = false;
  await sw.fire('notificationclick', {notification: {close() { closed = true; }, data: {id: NID, url: '#/staff/NCL-2026-ABCDEF', audience: 'staff'}}});
  assert.ok(closed);
  assert.equal(sw.windows[0].focused, false, 'windows of other sites are never used');
  assert.equal(sw.windows[1].focused, true);
  assert.deepEqual(sw.posted.map(p => ({...p.m})), [{type: 'civiclens:notification-click', id: NID, url: '#/staff/NCL-2026-ABCDEF', audience: 'staff'}]);
  assert.deepEqual(sw.opened, []);
});

test('clicking with no open window opens the app at the notification route', async () => {
  const sw = loadWorker({scope: 'https://civiclens.example/app/'});
  await sw.fire('notificationclick', {notification: {close() {}, data: {id: NID, url: '#/track/NCL-2026-ABCDEF', audience: 'citizen'}}});
  assert.deepEqual(sw.opened, [`https://civiclens.example/app/?n=${NID}&a=citizen#/track/NCL-2026-ABCDEF`]);
  const hostile = loadWorker();
  await hostile.fire('notificationclick', {notification: {close() {}, data: {id: 'x', url: 'javascript:alert(1)', audience: 'citizen'}}});
  assert.deepEqual(hostile.opened, ['https://civiclens.example/#/notifications']);
});

test('a rotated subscription asks open pages to register it again', async () => {
  const sw = loadWorker({clients: ['https://civiclens.example/']});
  await sw.fire('pushsubscriptionchange', {});
  assert.equal(sw.posted[0].m.type, 'civiclens:subscription-changed');
});
