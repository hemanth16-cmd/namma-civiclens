import {test} from 'node:test';
import assert from 'node:assert/strict';

// Browser-storage shim for Node, as in services.test.js.
const storage = new Map();
const shim = {getItem: k => (storage.has(k) ? storage.get(k) : null), setItem: (k, v) => storage.set(k, String(v)), removeItem: k => storage.delete(k)};
globalThis.localStorage = shim;
globalThis.sessionStorage = shim;

const {mediaDB} = await import('../dist/services.js');
const {submitReport, takeServerReceipt, notifyAboutReport, forgetUpload} = await import('../dist/report-gateway.js');

mediaDB.put = async () => {};

const BASE = 'http://localhost:8000/api/v1';
const BACKEND = {mode: 'backend', baseUrl: BASE, problem: null};
const CLAIM = '0b8f7d2a-3c4e-4a1b-9f6d-5e7c8a9b0d1e';
const INBOX = 'inbox_' + 'I'.repeat(40);
const VAPID = 'BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U';
const json = (status, body) => new Response(JSON.stringify(body), {status, headers: {'Content-Type': 'application/json'}});

const report = () => ({category: 'Potholes', description: 'Demo', mediaType: 'photo', shareEvidence: false,
  gps: {lat: 13.0358, lng: 77.597, accuracy: 12, timestamp: Date.now() - 4000, source: 'Device GPS', sourceType: 'gps'}, created: new Date().toISOString()});
const receipt = id => ({id, claim: CLAIM, category: 'Potholes', area: 'Hebbal', created: 'z', capturedAt: null, uploadedAt: 'z', captureSource: null,
  gps: {lat: 13.0358, lng: 77.597, accuracy: 12, source: 'Device GPS', sourceType: 'gps', timestamp: null, address: null},
  mediaType: 'photo', shareEvidence: false, status: 'Submitted', verification: 'Pending Verification', uploadToken: 'tok_' + 'A'.repeat(40), uploadTokenExpiresAt: '2099-01-01T00:00:00.000Z'});

function server(handlers) {
  const calls = [];
  const fn = async (url, init = {}) => {
    const path = url.slice(BASE.length).split('?')[0];
    calls.push({path, method: init.method, body: init.body && typeof init.body === 'string' ? JSON.parse(init.body) : undefined, auth: init.headers?.Authorization});
    const handler = handlers[`${init.method} ${path}`];
    if (!handler) throw new Error('unexpected ' + init.method + ' ' + path);
    return handler();
  };
  fn.calls = calls;
  return fn;
}

function env({permission = 'granted', registered = false, inbox = null, subscribeFails = false} = {}) {
  const store = new Map();
  if (inbox) store.set('ncl-notification-inbox', inbox);
  if (registered) store.set('ncl-push-device:citizen', 'ps_' + 'd'.repeat(22));
  const registration = {pushManager: {
    async getSubscription() { return null; },
    async subscribe() {
      if (subscribeFails) throw new DOMException('Registration failed - push service error', 'AbortError');
      return {endpoint: 'https://fcm.googleapis.com/fcm/send/x', options: {}, toJSON: () => ({endpoint: 'https://fcm.googleapis.com/fcm/send/x', expirationTime: null, keys: {p256dh: 'p'.repeat(87), auth: 'a'.repeat(22)}})};
    }
  }};
  return {
    isSecureContext: true, PushManager: function () {},
    Notification: {permission, requests: 0, async requestPermission() { this.requests++; this.permission = 'granted'; return 'granted'; }},
    navigator: {userAgent: 'Chrome/131.0 Windows', serviceWorker: {register: async () => registration, ready: Promise.resolve(registration), getRegistration: async () => registration}},
    localStorage: {getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k)}
  };
}

const followed = id => () => json(201, {complaintId: id, followedAt: 'z'});

test('a device with notifications on follows its new report before the evidence upload', async () => {
  const id = 'NCL-2026-AUTO01';
  const fetchImpl = server({'POST /complaints': () => json(201, receipt(id)), 'POST /notifications/follows': followed(id)});
  await submitReport(report(), null, {config: BACKEND, fetchImpl, env: env({registered: true, inbox: INBOX})});
  const follow = fetchImpl.calls.find(c => c.path === '/notifications/follows');
  assert.deepEqual(follow.body, {complaintId: id, claim: CLAIM});
  assert.equal(follow.auth, 'Bearer ' + INBOX);
  assert.equal(takeServerReceipt(id).following, true);
  forgetUpload(id);
});

test('a device without notifications does not follow automatically, and a follow failure never fails the report', async () => {
  const quiet = server({'POST /complaints': () => json(201, receipt('NCL-2026-AUTO02'))});
  await submitReport(report(), null, {config: BACKEND, fetchImpl: quiet, env: env({inbox: INBOX})});
  assert.deepEqual(quiet.calls.map(c => c.path), ['/complaints']);
  assert.equal(takeServerReceipt('NCL-2026-AUTO02').following, false);

  const failing = server({'POST /complaints': () => json(201, receipt('NCL-2026-AUTO03')), 'POST /notifications/follows': () => json(503, {error: {code: 'storage_unavailable', message: 'down'}})});
  const result = await submitReport(report(), null, {config: BACKEND, fetchImpl: failing, env: env({registered: true, inbox: INBOX})});
  assert.equal(result.id, 'NCL-2026-AUTO03');
  assert.equal(takeServerReceipt('NCL-2026-AUTO03').following, false);
});

test('"Notify me" creates an inbox, follows with the claim code in memory, then turns on push', async () => {
  const id = 'NCL-2026-NOTIF1';
  await submitReport(report(), null, {config: BACKEND, fetchImpl: server({'POST /complaints': () => json(201, receipt(id))}), env: env()});
  takeServerReceipt(id);
  const e = env({permission: 'default'});
  const fetchImpl = server({
    'POST /notifications/inboxes': () => json(201, {inboxToken: INBOX}),
    'POST /notifications/follows': followed(id),
    'GET /notifications/config': () => json(200, {pushEnabled: true, vapidPublicKey: VAPID}),
    'POST /notifications/subscriptions': () => json(201, {subscriptionId: 'ps_' + 'e'.repeat(22), label: 'Chrome on Windows', createdAt: 'z', lastSuccessAt: null, lastFailureAt: null, failureCount: 0})
  });
  const result = await notifyAboutReport(id, {fetchImpl, env: e});
  assert.deepEqual(result, {following: true, push: 'enabled', message: ''});
  assert.equal(e.Notification.requests, 1);
  assert.deepEqual(fetchImpl.calls.map(c => c.path), ['/notifications/inboxes', '/notifications/follows', '/notifications/config', '/notifications/subscriptions']);
  assert.equal(e.localStorage.getItem('ncl-notification-inbox'), INBOX);
  forgetUpload(id);
});

test('"Notify me" still follows when the browser cannot register for push', async () => {
  const id = 'NCL-2026-NOTIF2';
  await submitReport(report(), null, {config: BACKEND, fetchImpl: server({'POST /complaints': () => json(201, receipt(id))}), env: env()});
  takeServerReceipt(id);
  const fetchImpl = server({
    'POST /notifications/inboxes': () => json(201, {inboxToken: INBOX}),
    'POST /notifications/follows': followed(id),
    'GET /notifications/config': () => json(200, {pushEnabled: true, vapidPublicKey: VAPID})
  });
  const result = await notifyAboutReport(id, {fetchImpl, env: env({subscribeFails: true})});
  assert.equal(result.following, true);
  assert.equal(result.push, 'failed');
  forgetUpload(id);
  assert.equal((await notifyAboutReport(id, {fetchImpl})).following, false, 'without the in-memory claim code it cannot follow');
});
