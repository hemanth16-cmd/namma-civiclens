// End-to-end Web Push check in a real browser: service worker registration, the permission
// request, real push subscriptions, delivery through the browser vendor's push service, the
// notification history UI, read/unread, click navigation, multiple staff devices and sign-out cleanup.
//
// Needs: the frontend (npm run dev) and the API started with VAPID keys (backend/README.md →
// "Web Push notifications") and CIVICLENS_RATE_LIMITS=off, plus a staff account created with
// python -m app.staff create. Push registration needs a browser with a push service and a
// persistent profile: Microsoft Edge (default, Windows Push Notification Services) or Google Chrome.
// Open-source Chromium cannot register (no push service credentials). Profiles are temporary and
// deleted at the end; api-config.json is served to the browser in backend mode, so no repository
// file changes. The browser contacts the vendor's push service (network required).
//
// Usage: BASE=http://localhost:4173 API=http://localhost:8010/api/v1 STAFF_USERNAME=... STAFF_PASSWORD=... \
//        PLAYWRIGHT_PATH=/path/to/node_modules/playwright/index.js [CHANNEL=msedge|chrome] [HEADED=1] node scripts/notifications-e2e.mjs

import {createRequire} from 'node:module';
import {mkdtempSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import path from 'node:path';

const BASE = (process.env.BASE || 'http://localhost:4173').replace(/\/+$/, '');
const API = (process.env.API || 'http://localhost:8000/api/v1').replace(/\/+$/, '');
const CHANNEL = process.env.CHANNEL || 'msedge';
const STAFF = {username: process.env.STAFF_USERNAME, password: process.env.STAFF_PASSWORD};
const PUSH_WAIT_MS = 45000;

let pw;
try { pw = (await import('playwright')).chromium; } catch {
  pw = createRequire(import.meta.url)(process.env.PLAYWRIGHT_PATH || 'playwright').chromium;
}

let passed = 0, failed = 0;
function check(name, ok, detail = '') {
  ok ? passed++ : failed++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail && !ok ? `  (${detail})` : ''}`);
}

const config = await fetch(API + '/notifications/config').then(r => r.json()).catch(() => null);
check('API reports Web Push as configured', config?.pushEnabled === true && typeof config.vapidPublicKey === 'string', JSON.stringify(config));
if (!config?.pushEnabled || !STAFF.username || !STAFF.password) {
  console.log('\nStart the API with VAPID keys and pass STAFF_USERNAME / STAFF_PASSWORD (see the header of this file).');
  process.exit(1);
}

// ---------- API helpers (Node side, like a second device or another person) ----------
const JPEG = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 0, 16, 0x4a, 0x46, 0x49, 0x46, 0, 1, 1, 0, 0, 1, 0, 1, 0, 0, ...Array(2000).fill(7), 0xff, 0xd9]);
async function createComplaint() {
  const response = await fetch(API + '/complaints', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
    category: 'Potholes', mediaType: 'photo', description: 'Notification e2e', shareEvidence: false,
    gps: {lat: 13.0358, lng: 77.597, accuracy: 9, sourceType: 'gps', timestamp: Date.now() - 2000}})});
  return response.json();
}
async function uploadEvidence(receipt) {
  const form = new FormData();
  form.append('file', new Blob([JPEG], {type: 'image/jpeg'}), 'e.jpg');
  return (await fetch(`${API}/complaints/${receipt.id}/evidence`, {method: 'POST', body: form, headers: {Authorization: 'Bearer ' + receipt.uploadToken}})).status;
}
const apiGet = (p, token) => fetch(API + p, {headers: {Authorization: 'Bearer ' + token}}).then(r => r.json());

// ---------- Browser helpers ----------
const profiles = [];
async function openDevice({grant = true} = {}) {
  const dir = mkdtempSync(path.join(tmpdir(), 'ncl-push-'));
  profiles.push(dir);
  const ctx = await pw.launchPersistentContext(dir, {channel: CHANNEL, headless: !process.env.HEADED, viewport: {width: 1100, height: 900}});
  if (grant) await ctx.grantPermissions(['notifications'], {origin: BASE});
  await ctx.route('**/api-config.json', route => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({mode: 'backend', baseUrl: API})}));
  const page = ctx.pages()[0] || await ctx.newPage();
  page.on('pageerror', e => console.log('  page error:', e.message));
  return {ctx, page};
}
const shown = page => page.evaluate(async () => (await (await navigator.serviceWorker.ready).getNotifications())
  .map(n => ({title: n.title, body: n.body, tag: n.tag, data: n.data})));
async function waitForNotification(page, predicate) {
  const deadline = Date.now() + PUSH_WAIT_MS;
  while (Date.now() < deadline) {
    const match = (await shown(page)).find(predicate);
    if (match) return match;
    await page.waitForTimeout(500);
  }
  return null;
}
const text = (page, sel) => page.locator(sel).innerText().catch(() => '');

const browser = [];
try {
  // ===== Citizen device =====
  const citizen = await openDevice({grant: false});
  browser.push(citizen.ctx);
  const page = citizen.page;
  await page.goto(BASE + '/#/notifications');
  await page.waitForSelector('#push-panel');
  check('Citizen inbox renders in server mode with push off', /Off\./.test(await text(page, '#push-panel')) && await page.locator('#push-enable').count() === 1);

  // Permission not granted: the browser refuses (automation answers the prompt with "denied" or "default").
  await page.click('#push-enable');
  await page.waitForFunction(() => document.querySelector('#push-feedback .notice') || /blocked/i.test(document.querySelector('#push-panel')?.innerText || ''), null, {timeout: 15000}).catch(() => {});
  const refused = await text(page, '#push-panel');
  check('Without permission nothing is subscribed and the page explains why', /blocked|not turned on/i.test(refused), refused.slice(0, 160));
  const inbox = await page.evaluate(() => localStorage.getItem('ncl-notification-inbox'));
  check('A citizen inbox token is kept on the device', /^[A-Za-z0-9_-]{20,}$/.test(inbox || ''));
  check('No device was registered without permission', (await apiGet('/notifications/subscriptions', inbox)).length === 0);

  await citizen.ctx.grantPermissions(['notifications'], {origin: BASE});
  await page.reload();
  await page.waitForSelector('#push-enable');
  await page.click('#push-enable');
  await page.waitForSelector('#push-disable', {timeout: 30000}).catch(() => {});
  check('Service worker is registered and active', await page.evaluate(async () => Boolean((await navigator.serviceWorker.getRegistration())?.active?.scriptURL.endsWith('/sw.js'))));
  const endpoint = await page.evaluate(async () => (await (await navigator.serviceWorker.ready).pushManager.getSubscription())?.endpoint || '');
  check('Browser holds a real push subscription', /^https:\/\//.test(endpoint), endpoint.slice(0, 60));
  console.log(`  push service: ${endpoint ? new URL(endpoint).host : 'none'}`);
  const devices = await apiGet('/notifications/subscriptions', inbox);
  check('Server registered exactly one device with a browser label', devices.length === 1 && / on Windows| on /.test(devices[0].label), JSON.stringify(devices));
  check('Device list in the page shows the device', /added/.test(await text(page, '#device-list')));

  // Follow a report with its claim code; the confirmation arrives as a real push.
  const receipt = await createComplaint();
  await page.fill('#follow-id', receipt.id);
  await page.fill('#follow-claim', receipt.claim);
  await page.click('#follow-form button[type=submit]');
  await page.waitForSelector('#follow-feedback .notice');
  check('Following with the claim code succeeds', (await text(page, '#follow-feedback')).includes(receipt.id));
  const confirmation = await waitForNotification(page, n => n.title === 'Updates turned on');
  check('Real push delivered: "Updates turned on" shown as a system notification', Boolean(confirmation));
  check('The pushed notification links to the report', confirmation?.data?.url === `#/track/${receipt.id}`, JSON.stringify(confirmation?.data));

  check('Evidence upload accepted', await uploadEvidence(receipt) === 201);
  const evidencePush = await waitForNotification(page, n => n.title === 'Evidence received');
  check('Real push delivered: "Evidence received"', Boolean(evidencePush));
  await page.waitForFunction(() => /Evidence received/.test(document.querySelector('#notification-list')?.innerText || ''), null, {timeout: 10000}).catch(() => {});
  check('History refreshed itself when the push arrived (no reload)', /Evidence received/.test(await text(page, '#notification-list')));
  check('Unread count shows 2', (await text(page, '#unread-count')).startsWith('2'), await text(page, '#unread-count'));

  // Read / unread
  const firstToggle = page.locator('[data-toggle]').first();
  await firstToggle.click();
  await page.waitForFunction(() => document.querySelector('#unread-count')?.textContent.startsWith('1'), null, {timeout: 5000}).catch(() => {});
  check('Mark as read lowers the unread count', (await text(page, '#unread-count')).startsWith('1'));
  await page.locator('[data-toggle]').first().click();
  await page.waitForFunction(() => document.querySelector('#unread-count')?.textContent.startsWith('2'), null, {timeout: 5000}).catch(() => {});
  check('Mark as unread restores it', (await text(page, '#unread-count')).startsWith('2'));

  // Click navigation through the service worker's notificationclick handler.
  const [worker] = citizen.ctx.serviceWorkers();
  const clickResult = await worker.evaluate(async () => {
    const [n] = (await self.registration.getNotifications()).filter(x => x.title === 'Evidence received');
    const event = new NotificationEvent('notificationclick', {notification: n});
    try { self.dispatchEvent(event); } catch (e) { return 'dispatch error ' + e.message; }
    return n.data.id;
  });
  await page.waitForFunction(id => location.hash === id, `#/track/${receipt.id}`, {timeout: 10000}).catch(() => {});
  check('Clicking the notification opens the report page in the open window', page.url().endsWith(`#/track/${receipt.id}`), page.url());
  await page.waitForTimeout(800);
  const afterClick = await apiGet('/notifications?limit=10', inbox);
  check('The clicked notification is marked read on the server', afterClick.items.find(i => i.id === clickResult)?.read === true, clickResult);
  check('The tracking page offers "Get notified"', await page.locator('#track-notify').count() === 1);

  // A window opened by a click (no CivicLens window open) arrives with ?n=&a=.
  const unread = (await apiGet('/notifications?unread=true', inbox)).items[0];
  const second = await citizen.ctx.newPage();
  await second.goto(`${BASE}/?n=${unread.id}&a=citizen#/track/${receipt.id}`);
  await second.waitForTimeout(1500);
  check('A launch URL from a notification is cleaned from the address bar', !second.url().includes('?n='), second.url());
  check('…and marks that notification read', (await apiGet('/notifications?limit=10', inbox)).items.find(i => i.id === unread.id)?.read === true);
  await second.close();

  // Test notification, then turn off.
  await page.goto(BASE + '/#/notifications');
  await page.waitForSelector('#push-test');
  await page.click('#push-test');
  check('Real push delivered: test notification', Boolean(await waitForNotification(page, n => n.title === 'Test notification')));
  await page.click('#push-disable');
  await page.waitForSelector('#push-enable', {timeout: 15000}).catch(() => {});
  check('Turning off removes the device on the server', (await apiGet('/notifications/subscriptions', inbox)).length === 0);
  check('…and the browser subscription', await page.evaluate(async () => !(await (await navigator.serviceWorker.ready).pushManager.getSubscription())));
  check('History is kept after turning off', (await apiGet('/notifications', inbox)).items.length >= 3);

  // ===== Staff on two devices =====
  const staffPages = [];
  for (let i = 0; i < 2; i++) {
    const device = await openDevice();
    browser.push(device.ctx);
    const p = device.page;
    await p.goto(BASE + '/#/staff/notifications');
    await p.fill('#staff-username', STAFF.username);
    await p.fill('#staff-password', STAFF.password);
    await p.click('#staff-sign-in button[type=submit]');
    await p.waitForSelector('#push-enable', {timeout: 15000});
    check(`Staff device ${i + 1}: sign-in returns to the notifications page`, p.url().endsWith('#/staff/notifications'));
    await p.click('#push-enable');
    await p.waitForSelector('#push-disable', {timeout: 30000}).catch(() => {});
    staffPages.push(p);
  }
  const staffToken = await staffPages[0].evaluate(async () => (await import('./staff-session.js')).staffSession.get()?.token);
  check('Staff account has two registered devices', (await apiGet('/notifications/subscriptions', staffToken)).length === 2);
  const staffReceipt = await createComplaint();
  for (const [i, p] of staffPages.entries()) {
    const n = await waitForNotification(p, x => x.title === 'New complaint' && x.body.includes(staffReceipt.id));
    check(`Real push delivered to staff device ${i + 1}: "New complaint"`, Boolean(n));
    check(`…linking to the staff case page`, n?.data?.url === `#/staff/${staffReceipt.id}` && n?.data?.audience === 'staff');
  }
  await staffPages[0].goto(BASE + '/#/staff');
  await staffPages[0].waitForSelector('#staff-unread:not([hidden])', {timeout: 10000}).catch(() => {});
  check('Staff header shows the unread badge', Number(await text(staffPages[0], '#staff-unread')) >= 1);
  await staffPages[1].click('#staff-sign-out');
  await staffPages[1].waitForSelector('#staff-sign-in', {timeout: 10000});
  check('Signing out removes that browser’s staff device', (await apiGet('/notifications/subscriptions', staffToken)).length === 1);
} catch (err) {
  check('Suite ran without an exception', false, err?.stack || String(err));
} finally {
  for (const ctx of browser) await ctx.close().catch(() => {});
  for (const dir of profiles) rmSync(dir, {recursive: true, force: true});
}

console.log(`\n${passed}/${passed + failed} checks passed.`);
process.exit(failed ? 1 : 0);
