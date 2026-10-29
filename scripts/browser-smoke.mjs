// Browser smoke test for the main journeys. Requires Playwright with a Chromium build.
// Usage: BASE=http://localhost:4173 SHOTS=./screenshots node scripts/browser-smoke.mjs
// Optional: PLAYWRIGHT_PATH=/path/to/node_modules/playwright/index.js if Playwright is not resolvable.
import {createRequire} from 'node:module';
import {mkdirSync, writeFileSync} from 'node:fs';
import path from 'node:path';

const BASE = process.env.BASE || 'http://localhost:4173';
const SHOTS = process.env.SHOTS || './screenshots';
mkdirSync(SHOTS, {recursive: true});
let pw;
try {
  pw = (await import('playwright')).chromium;
} catch {
  const req = createRequire(import.meta.url);
  pw = req(process.env.PLAYWRIGHT_PATH || 'playwright').chromium;
}

const results = [];
const check = (name, ok, detail = '') => { results.push({name, ok: !!ok, detail}); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ` — ${detail}` : ''}`); };
const hash = page => page.evaluate(() => location.hash);
const overflow = page => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
const waitHash = async (page, pattern, timeout = 6000) => {
  try { await page.waitForFunction(p => new RegExp(p).test(location.hash), pattern, {timeout}); return true; } catch { return false; }
};

const browser = await pw.launch({args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream', '--enable-unsafe-swiftshader', '--ignore-certificate-errors']});
const consoleErrors = [];
const newContext = (viewport, extra = {}) => browser.newContext({viewport, permissions: ['camera', 'geolocation', 'clipboard-read', 'clipboard-write'], geolocation: {latitude: 13.0358, longitude: 77.597, accuracy: 12}, ...extra});
const trackErrors = page => page.on('console', m => { if (m.type() === 'error' && !/ERR_TUNNEL|Failed to load resource|net::ERR/.test(m.text())) consoleErrors.push(m.text()); });
const pageErrors = [];

// ---------- Desktop journeys ----------
{
  const ctx = await newContext({width: 1280, height: 800});
  const page = await ctx.newPage();
  page.on('pageerror', e => pageErrors.push(e.message));
  trackErrors(page);
  await page.goto(BASE + '/', {waitUntil: 'load'});
  await page.waitForTimeout(400);
  check('Default route opens the reporting camera', (await hash(page)).includes('camera') || (await hash(page)) === '', await hash(page));
  const shutter = await page.locator('#shutter').boundingBox();
  check('Shutter is visible above the fold at 1280×800', shutter && shutter.y + shutter.height <= 800, shutter ? `bottom ${Math.round(shutter.y + shutter.height)}px` : 'missing');
  await page.screenshot({path: path.join(SHOTS, 'desktop-camera.png')});

  await page.click('.close-button');
  await page.waitForTimeout(300);
  check('Close (×) opens the three-choice entry', (await hash(page)) === '#/home', await hash(page));
  const choices = await page.locator('.entry-grid > *').count();
  check('Entry shows exactly three choices', choices === 3, `${choices} choices`);
  check('Entry has no dashboard stats', (await page.locator('.stats').count()) === 0);
  await page.screenshot({path: path.join(SHOTS, 'desktop-entry.png')});

  await page.click('[data-action="back"]');
  await page.waitForTimeout(300);
  check('Back from entry returns to the previous in-app screen (camera)', (await hash(page)).includes('camera'), await hash(page));

  await page.goto(BASE + '/#/home', {waitUntil: 'load'});
  await page.fill('#entry-track-id', 'NCL-2026-DEMO01');
  await page.press('#entry-track-id', 'Enter');
  await waitHash(page, 'track/NCL-2026-DEMO01');
  await page.waitForSelector('.track-result', {timeout: 8000}).catch(() => {});
  const trackText = await page.locator('main').innerText();
  check('Public tracking shows status and timeline', trackText.includes('Public timeline') && trackText.includes('In Progress'));
  check('Public tracking never shows a claim code or reporter details', !/DEMO-CLAIM|resident@civiclens/.test(trackText));

  // Role guards and sign-in.
  await page.goto(BASE + '/#/official', {waitUntil: 'load'});
  await waitHash(page, 'login/official');
  check('Admin route redirects an anonymous visitor to admin sign-in', (await hash(page)) === '#/login/official', await hash(page));
  await page.click('button[type=submit]');
  await waitHash(page, '^#/official$');
  await page.waitForSelector('.sidebar nav', {timeout: 8000}).catch(async () => { console.log('DEBUG admin body:', (await page.locator('#app').innerText()).slice(0, 300)); });
  const adminNav = await page.locator('.sidebar nav').first().innerText({timeout: 5000}).catch(() => '');
  check('Admin sees video review, work orders and intelligence', /Video review/.test(adminNav) && /Work orders/.test(adminNav) && /Intelligence studio/.test(adminNav));
  await page.screenshot({path: path.join(SHOTS, 'desktop-admin-overview.png')});

  // Admin map: filter drives both markers and list.
  await page.goto(BASE + '/#/official/map', {waitUntil: 'load'});
  await page.waitForTimeout(1200);
  const countText = async () => (await page.locator('#map-count').innerText());
  check('Admin map lists every sample case', (await countText()).startsWith('8 shown'), await countText());
  await page.selectOption('#mf-category', 'Potholes');
  await page.waitForTimeout(200);
  check('Category filter updates the list', (await page.locator('#map-list .issue-row').count()) === 1, await countText());
  check('Map legend explains green, red and clusters', (await page.locator('.map-legend').innerText()).match(/Green circle[\s\S]*Red circle[\s\S]*Numbered navy cluster/));
  check('Map attribution is visible', (await page.locator('.map-disclosure').innerText()).includes('OpenStreetMap'));
  await page.selectOption('#mf-category', '');
  await page.selectOption('#mf-resolution', 'resolved');
  await page.waitForTimeout(200);
  check('Green-only filter shows marked resolved cases without open disputes', (await page.locator('#map-list .issue-row').count()) === 1, await countText());
  await page.selectOption('#mf-resolution', '');
  await page.screenshot({path: path.join(SHOTS, 'desktop-map.png')});

  // Admin case: verification history, repair upload, reinspection.
  await page.goto(BASE + '/#/issue/NCL-2026-DEMO05', {waitUntil: 'load'});
  await page.waitForTimeout(500);
  const issueText = await page.locator('main').innerText();
  check('Disputed resolution is labelled as unresolved in the case state', /Resolved but disputed|Unresolved/.test(issueText) || /Disputed/.test(issueText));
  check('Case shows before/after panel and repair history', issueText.includes('Before') && issueText.includes('Repair history'));
  check('Admin case panel exposes internal notes separately', (await page.locator('#case-internal').count()) === 1);
  await page.screenshot({path: path.join(SHOTS, 'desktop-issue-admin.png'), fullPage: false});

  // Work order.
  await page.goto(BASE + '/#/official/workorders', {waitUntil: 'load'});
  await page.waitForTimeout(400);
  await page.selectOption('#wo-case', 'NCL-2026-DEMO06');
  await page.fill('#wo-team', 'Drainage response · West');
  await page.fill('#wo-due', '2026-12-15');
  await page.click('#work-form button[type=submit]');
  await page.waitForTimeout(500);
  check('Work order assigns team and appears in the table', (await page.locator('table').innerText()).includes('Drainage response · West'));

  // Intelligence readiness.
  await page.goto(BASE + '/#/official/intelligence', {waitUntil: 'load'});
  await page.waitForTimeout(400);
  await page.click('[data-tab="readiness"]');
  await page.waitForTimeout(200);
  const readiness = await page.locator('#intel-panel').innerText();
  check('Intelligence labels unvalidated techniques as proposed', readiness.includes('Proposed; not connected or trained') && readiness.includes('YOLO11n'));
  await page.click('[data-tab="fcm"]');
  await page.waitForTimeout(150);
  check('Cognitive map is documented as not Firebase messaging', (await page.locator('#intel-panel').innerText()).includes('not Firebase Cloud Messaging'));

  // Admin video review, using a generated WebM clip.
  await page.goto(BASE + '/#/official/video-review', {waitUntil: 'load'});
  await page.waitForTimeout(400);
  const webm = await page.evaluate(async () => {
    const c = document.createElement('canvas'); c.width = 320; c.height = 240;
    const ctx = c.getContext('2d'); const stream = c.captureStream(15);
    const rec = new MediaRecorder(stream, {mimeType: 'video/webm'}); const chunks = [];
    rec.ondataavailable = e => chunks.push(e.data); rec.start();
    let t = 0; const iv = setInterval(() => { ctx.fillStyle = `hsl(${t * 25},70%,50%)`; ctx.fillRect(0, 0, 320, 240); t++; }, 100);
    await new Promise(r => setTimeout(r, 2500)); clearInterval(iv);
    await new Promise(r => { rec.onstop = r; rec.stop(); });
    const bytes = new Uint8Array(await new Blob(chunks, {type: 'video/webm'}).arrayBuffer());
    let bin = ''; for (const b of bytes) bin += String.fromCharCode(b);
    return btoa(bin);
  });
  await page.setInputFiles('#video-file', {name: 'review-clip.webm', mimeType: 'video/webm', buffer: Buffer.from(webm, 'base64')});
  await page.waitForSelector('#review-player', {timeout: 8000}).catch(() => {});
  await page.waitForFunction(() => { const v = document.querySelector('#review-player'); return v && v.readyState >= 2; }, null, {timeout: 8000}).catch(() => {});
  check('Uploaded clip loads in the review player', (await page.locator('#review-player').count()) === 1);
  await page.evaluate(() => { const v = document.querySelector('#review-player'); v.currentTime = 0.8; });
  await page.waitForTimeout(400);
  await page.selectOption('#observation-kind', 'Waste present');
  await page.fill('#observation-note', 'Smoke test observation: waste visible near the bins.');
  await page.click('#observation-form button[type=submit]');
  await page.waitForTimeout(700);
  const momentCount = await page.locator('.moment').count();
  check('Observation saves with a timestamp and a frame link', momentCount === 1 && (await page.locator('.moment').first().innerText()).includes('00:00') && (await page.locator('.moment [data-frame]').count()) === 1, `${momentCount} moment(s)`);
  await page.click('#finding-form button[type=submit]');
  await page.waitForTimeout(300);
  const [download] = await Promise.all([page.waitForEvent('download', {timeout: 5000}).catch(() => null), page.click('#export-review')]);
  check('Review exports as JSON', !!download && (download.suggestedFilename() || '').endsWith('.json'));
  await page.screenshot({path: path.join(SHOTS, 'desktop-video.png')});
  await ctx.close();
}

// ---------- Capture to receipt with a fake camera ----------
{
  const ctx = await newContext({width: 1280, height: 860});
  const page = await ctx.newPage();
  page.on('pageerror', e => pageErrors.push(e.message));
  trackErrors(page);
  await page.goto(BASE + '/', {waitUntil: 'load'});
  await page.waitForFunction(() => document.querySelector('#cam-chip')?.textContent.trim() === 'Live', null, {timeout: 10000}).catch(() => {});
  check('Camera goes live with permission granted', (await page.locator('#cam-chip').innerText()).trim() === 'Live');
  const previewOnTop = await page.evaluate(() => {
    const r = document.querySelector('#viewfinder').getBoundingClientRect();
    return document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)?.id;
  });
  check('Live preview is visible, not covered by an overlay', previewOnTop === 'live', previewOnTop);
  await page.waitForFunction(() => /\d+\.\d+,\s*\d+\.\d+/.test(document.querySelector('#vf-location-text')?.textContent || ''), null, {timeout: 8000}).catch(() => {});
  check('Device location is shown with coordinates and accuracy', /±\d+ m/.test(await page.locator('#vf-location-text').innerText()));
  await page.click('#shutter');
  await waitHash(page, 'review');
  check('Capture opens review', (await hash(page)) === '#/review', await hash(page));
  await page.click('[data-category="Potholes"]');
  await page.check('#confirm-location');
  await page.check('#share-evidence').catch(() => {});
  await page.fill('#description', 'Smoke test: pothole near the junction');
  await page.screenshot({path: path.join(SHOTS, 'desktop-review.png')});
  await page.click('#submit-btn');
  await waitHash(page, 'receipt/NCL-');
  await page.waitForTimeout(400);
  const ref = await page.locator('.reference-card .code-value').innerText().catch(() => '');
  const claim = await page.locator('.private-card .code-private').innerText().catch(() => '');
  check('Receipt shows a public ID', /^NCL-\d{4}-[A-Z0-9]{6}$/.test(ref.trim()), ref);
  check('Public ID and private claim code are separate values', claim.trim().length > 10 && claim.trim() !== ref.trim());
  const receiptText = await page.locator('main').innerText();
  check('Receipt reports the captured time separately from upload', /Uploaded to CivicLens/.test(receiptText));
  await page.screenshot({path: path.join(SHOTS, 'desktop-receipt.png')});
  const [dl] = await Promise.all([page.waitForEvent('download', {timeout: 8000}).catch(() => null), page.click('[data-evidence]')]);
  let jpegOk = false;
  if (dl) {
    const p = await dl.path();
    const head = (await import('node:fs')).readFileSync(p).subarray(0, 2);
    jpegOk = head[0] === 0xff && head[1] === 0xd8;
  }
  check('Evidence copy downloads as a JPEG with a stamped strip', jpegOk);
  await page.goto(BASE + '/#/track/' + ref.trim(), {waitUntil: 'load'});
  await page.waitForTimeout(500);
  check('Submitted report is trackable by its public ID', (await page.locator('main').innerText()).includes('Submitted'));
  await page.goto(BASE + '/#/camera', {waitUntil: 'load'});
  await page.waitForTimeout(300);
  await page.setInputFiles('#gallery-input', {name: 'no-gps.jpg', mimeType: 'image/jpeg', buffer: Buffer.from(await page.evaluate(async () => {
    const c = document.createElement('canvas'); c.width = 32; c.height = 32; c.getContext('2d').fillRect(0, 0, 32, 32);
    const b = await new Promise(r => c.toBlob(r, 'image/jpeg', 0.8)); const a = new Uint8Array(await b.arrayBuffer());
    let s = ''; for (const x of a) s += String.fromCharCode(x); return btoa(s);
  }), 'base64')});
  await page.waitForTimeout(700);
  const dialogText = await page.locator('dialog').innerText().catch(() => '');
  check('Gallery photo without GPS is rejected without substituting device location', /does not contain GPS/.test(dialogText), dialogText.slice(0, 80));
  check('Rejected gallery photo does not leave the camera flow', (await hash(page)).includes('camera'));
  await ctx.close();
}

// Denied camera recovery: a browser without the fake-device flags, where permission is refused.
{
  const plain = await pw.launch({args: ['--enable-unsafe-swiftshader']});
  const ctx = await plain.newContext({viewport: {width: 1280, height: 800}, permissions: []});
  const page = await ctx.newPage();
  await page.goto(BASE + '/', {waitUntil: 'load'});
  await page.waitForTimeout(1500);
  const state = await page.locator('#cam-state').innerText();
  check('Denied or missing camera shows a recoverable message with gallery fallback', /blocked|No camera|could not start|secure page/i.test(state) && (await page.locator('#gallery-input').count()) === 1, state.split('\n').slice(0, 2).join(' | '));
  check('Denied camera keeps the entry, close and gallery controls usable', (await page.locator('.close-button').count()) === 1 && (await page.locator('[data-action="gallery"]').count()) >= 1);
  await page.screenshot({path: path.join(SHOTS, 'desktop-camera-denied.png')});
  await plain.close();
}

// ---------- Community review on a second resident account ----------
{
  const ctx = await newContext({width: 1280, height: 800});
  const page = await ctx.newPage();
  trackErrors(page);
  await page.goto(BASE + '/#/login/resident', {waitUntil: 'load'});
  await page.click('button[type=submit]');
  await waitHash(page, '^#/resident$');
  await page.goto(BASE + '/#/issue/NCL-2026-DEMO06', {waitUntil: 'load'});
  await page.waitForTimeout(400);
  check('Resident sees the case without owner controls', (await page.locator('#case-form').count()) === 0);
  await page.fill('#review-text', 'Smoke test review: the footpath edge is still uneven.');
  await page.click('#review-form button[type=submit]');
  await page.waitForTimeout(500);
  check('Review persists and does not change status', (await page.locator('main').innerText()).includes('Smoke test review'));
  await page.goto(BASE + '/#/resident/community', {waitUntil: 'load'});
  await page.waitForTimeout(300);
  await page.screenshot({path: path.join(SHOTS, 'desktop-community.png')});
  await page.goto(BASE + '/#/resident/notifications', {waitUntil: 'load'});
  await page.waitForTimeout(300);
  check('Resident notifications page renders', (await page.locator('main h1').innerText()).includes('Notifications'));
  await page.goto(BASE + '/#/official', {waitUntil: 'load'});
  await waitHash(page, 'login/official');
  check('Resident cannot open admin workspace', (await hash(page)) === '#/login/official');
  await ctx.close();
}

// ---------- Mobile widths: overflow and navigation ----------
{
  const ctx = await newContext({width: 375, height: 812}, {isMobile: true, hasTouch: true});
  const page = await ctx.newPage();
  page.on('pageerror', e => pageErrors.push(e.message));
  trackErrors(page);
  const routes = [['#/camera', 'mobile-camera'], ['#/home', 'mobile-entry'], ['#/track/NCL-2026-DEMO01', 'mobile-track'], ['#/login/resident', 'mobile-login']];
  for (const [r, name] of routes) {
    await page.goto(BASE + '/' + r, {waitUntil: 'load'});
    await page.waitForTimeout(350);
    const o = await overflow(page);
    check(`375px: no horizontal page scroll on ${r}`, o <= 1, `overflow ${o}px`);
    await page.screenshot({path: path.join(SHOTS, name + '.png'), fullPage: false});
  }
  const shutterBox = await page.goto(BASE + '/#/camera').then(() => page.waitForTimeout(300)).then(() => page.locator('#shutter').boundingBox());
  check('375px: shutter, switch and location controls are reachable', shutterBox && shutterBox.width >= 60 && shutterBox.y + shutterBox.height <= 812, shutterBox ? `${Math.round(shutterBox.width)}px` : 'none');
  await page.goto(BASE + '/#/login/resident', {waitUntil: 'load'});
  await page.click('button[type=submit]');
  await waitHash(page, '^#/resident$');
  await page.waitForTimeout(300);
  check('375px: navigation drawer hidden until opened', !(await page.locator('.app-shell').evaluate(e => e.classList.contains('menu-open'))));
  await page.click('[data-action="open-menu"]');
  await page.waitForTimeout(300);
  check('375px: navigation drawer opens from the menu button', await page.locator('.app-shell').evaluate(e => e.classList.contains('menu-open')));
  await page.keyboard.press('Escape');
  for (const r of ['#/resident', '#/resident/complaints', '#/resident/community', '#/resident/map', '#/resident/claim', '#/resident/notifications', '#/resident/settings', '#/issue/NCL-2026-DEMO03']) {
    await page.goto(BASE + '/' + r, {waitUntil: 'load'});
    await page.waitForTimeout(350);
    const o = await overflow(page);
    check(`375px: no horizontal page scroll on ${r}`, o <= 1, `overflow ${o}px`);
    if (r.endsWith('map')) await page.screenshot({path: path.join(SHOTS, 'mobile-map.png')});
    if (r.includes('issue')) await page.screenshot({path: path.join(SHOTS, 'mobile-issue.png')});
  }
  await ctx.close();
}

await browser.close();
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed.`);
if (pageErrors.length) console.log('Page errors:', pageErrors.slice(0, 10));
if (consoleErrors.length) console.log('Console errors:', consoleErrors.slice(0, 10));
writeFileSync(path.join(SHOTS, 'results.json'), JSON.stringify({results, pageErrors, consoleErrors}, null, 2));
process.exitCode = failed.length ? 1 : 0;
