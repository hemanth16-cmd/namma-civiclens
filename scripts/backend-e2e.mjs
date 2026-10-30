// Frontend + backend integration journeys in headless Chromium with a synthetic camera.
// Needs both servers running: the frontend (npm run dev) and the API (backend/README.md), ideally
// on a scratch database (CIVICLENS_DB_PATH). Backend mode is switched on by intercepting
// api-config.json inside the browser, so no file in the repository is changed. A local-mode
// context with the real api-config.json is checked too. Device camera and GPS are simulated, so the
// uploaded photo and video are Chromium's synthetic camera frames, not real-world media.
//
// The suite creates several complaints from one address, so start the API with CIVICLENS_RATE_LIMITS=off
// (rate limits are tested separately in backend/tests/test_ratelimit.py and against a live server).
//
// Usage: BASE=http://localhost:4173 API=http://localhost:8000/api/v1 SHOTS=./screenshots node scripts/backend-e2e.mjs
// Optional: PLAYWRIGHT_PATH=/path/to/node_modules/playwright/index.js if Playwright is not resolvable.
// Optional: EVIDENCE_DIR=<the API's CIVICLENS_EVIDENCE_DIR> to also check the stored files on disk.
// Optional: STAFF_USERNAME / STAFF_PASSWORD for an account made with `python -m app.staff create`
// (staff console checks are skipped, and reported as skipped, without them).
import {createRequire} from 'node:module';
import {createHash} from 'node:crypto';
import {mkdirSync, readFileSync, readdirSync, statSync} from 'node:fs';
import path from 'node:path';

const BASE = process.env.BASE || 'http://localhost:4173';
const API = (process.env.API || 'http://localhost:8000/api/v1').replace(/\/+$/, '');
const EVIDENCE_DIR = process.env.EVIDENCE_DIR || null;
const storedObjects = () => {
  if (!EVIDENCE_DIR) return [];
  const root = path.join(EVIDENCE_DIR, 'objects');
  try { return readdirSync(root).flatMap(d => readdirSync(path.join(root, d)).map(f => path.join(root, d, f))); } catch { return []; }
};
const isEvidenceUpload = r => r.request().method() === 'POST' && /\/complaints\/NCL-\d{4}-[A-Z0-9]{6}\/evidence$/.test(r.url());
const SHOTS = process.env.SHOTS || './screenshots';
mkdirSync(SHOTS, {recursive: true});
let pw;
try { pw = (await import('playwright')).chromium; } catch {
  pw = createRequire(import.meta.url)(process.env.PLAYWRIGHT_PATH || 'playwright').chromium;
}

const results = [];
const check = (name, ok, detail = '') => { results.push({name, ok: !!ok}); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ` — ${detail}` : ''}`); };
const errors = [];
const consoleText = [];
const GEO = {latitude: 13.0358, longitude: 77.597, accuracy: 12};
const BACKEND_CONFIG = JSON.stringify({mode: 'backend', baseUrl: API});
const DEMO_DESCRIPTION = 'Automated integration test report. Demonstration data, not a real complaint.';

const health = await fetch(API + '/health').then(r => r.json()).catch(() => null);
check('API health endpoint is reachable', health?.status === 'ok', API);
if (health?.status !== 'ok') {
  console.log(`\nStart the API first (see backend/README.md).\n0/1 checks passed.`);
  process.exit(1);
}

const browser = await pw.launch({args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream', '--enable-unsafe-swiftshader']});

async function newPage({backend}) {
  const ctx = await browser.newContext({viewport: {width: 1280, height: 860}, permissions: ['camera', 'geolocation'], geolocation: GEO});
  if (backend) await ctx.route('**/api-config.json', route => route.fulfill({status: 200, contentType: 'application/json', body: BACKEND_CONFIG}));
  const page = await ctx.newPage();
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => {
    consoleText.push(m.text());
    if (m.type() === 'error' && !/Failed to load resource|net::ERR|tiles\.openfreemap|AJAXError/.test(m.text())) errors.push(m.text());
  });
  return {ctx, page};
}

const waitHash = (page, pattern, timeout = 8000) => page.waitForFunction(p => new RegExp(p).test(location.hash), pattern, {timeout}).then(() => true, () => false);

/** Camera → shutter (or a short recording) → review → category, confirmation and description. Leaves the page on #/review. */
async function captureAndFill(page, {video = false} = {}) {
  await page.goto(BASE + '/#/camera');
  await page.waitForFunction(() => document.querySelector('#capture-ready')?.dataset.ready === 'true', null, {timeout: 12000}).catch(() => {});
  if (video) {
    await page.click('[data-mode="video"]');
    await page.click('#shutter');
    await page.waitForTimeout(2300);
  }
  await page.click('#shutter');
  const onReview = await waitHash(page, '^#/review$');
  await page.click('[data-category="Potholes"]');
  await page.check('#confirm-location');
  await page.fill('#description', DEMO_DESCRIPTION);
  return onReview;
}

const storageDump = page => page.evaluate(async () => {
  const dump = [JSON.stringify({...localStorage}), JSON.stringify({...sessionStorage})];
  const keys = await new Promise(resolve => {
    const open = indexedDB.open('ncl-evidence');
    open.onerror = () => resolve([]);
    open.onsuccess = () => {
      const db = open.result;
      const names = [...db.objectStoreNames];
      if (!names.length) return resolve([]);
      const req = db.transaction(names[0]).objectStore(names[0]).getAllKeys();
      req.onsuccess = () => resolve(req.result.map(String));
      req.onerror = () => resolve([]);
    };
  });
  let recordIds = [];
  try { recordIds = JSON.parse(localStorage.getItem('ncl-records-v2') || '[]').map(r => r.id); } catch { recordIds = []; }
  return {text: dump.join('\n'), evidenceKeys: keys, recordIds};
});

// 1. Backend mode: a real submission against the API, then public tracking.
let publicId = null;
let claim = null;
{
  const {ctx, page} = await newPage({backend: true});
  const posted = [];
  page.on('request', req => { if (req.method() === 'POST' && req.url() === API + '/complaints') posted.push(req.postDataJSON()); });

  check('Backend mode: camera note says report details go to the server and media stays on the device', await page.goto(BASE + '/#/camera').then(() => page.waitForFunction(() => /Server mode/.test(document.querySelector('#storage-note')?.textContent || ''), null, {timeout: 5000}).then(() => true, () => false)));
  check('Backend mode: capture reaches review', await captureAndFill(page));
  const objectsBefore = storedObjects().length;
  const responsePromise = page.waitForResponse(r => r.url() === API + '/complaints' && r.request().method() === 'POST', {timeout: 10000}).catch(() => null);
  const evidencePromise = page.waitForResponse(isEvidenceUpload, {timeout: 20000}).catch(() => null);
  await page.click('#submit-btn');
  const response = await responsePromise;
  check('Backend mode: POST /complaints answered 201', response?.status() === 201, String(response?.status()));
  check('Backend mode: navigates to the receipt for the server ID', await waitHash(page, '^#/receipt/NCL-\\d{4}-[A-Z0-9]{6}$'));
  publicId = decodeURIComponent((await page.evaluate(() => location.hash)).split('/')[2] || '');
  const body = posted[0] || {};
  check('Request carries mapped metadata only (no owner, uploadedAt or media)', posted.length === 1 && body.owner === undefined && body.uploadedAt === undefined
    && body.category === 'Potholes' && body.gps?.sourceType === 'gps' && /Z$/.test(body.capturedAt || '') && !('blob' in body), JSON.stringify(Object.keys(body)));
  check('Request does not include a client-generated claim code or ID', body.claim === undefined && body.id === undefined);

  const main = page.locator('main[data-receipt-source="server"]');
  check('Receipt is the server receipt', await main.count() === 1);
  const receiptText = await page.locator('main').innerText();
  claim = (await page.locator('.code-private').innerText()).trim();
  const serverReceipt = await response?.json().catch(() => null);
  check('Receipt shows the server-generated public ID and claim code', serverReceipt?.id === publicId && serverReceipt?.claim === claim && /^[0-9a-f-]{36}$/.test(claim));
  check('Receipt tells the user to save the claim code because it will not be shown again', /will not be shown again/i.test(receiptText));
  check('Receipt does not use the local-mode wording', !/Uploaded to CivicLens|Report saved on this device/.test(receiptText));

  const evidenceResponse = await evidencePromise;
  const evidence = await evidenceResponse?.json().catch(() => null);
  const evidenceText = JSON.stringify(evidence || {});
  check('Evidence upload answered 201 with an evidence ID and checksum', evidenceResponse?.status() === 201 && /^ev_/.test(evidence?.evidenceId || '') && /^[0-9a-f]{64}$/.test(evidence?.sha256 || '') && evidence?.contentType === 'image/jpeg', `${evidenceResponse?.status()} ${evidence?.contentType}`);
  check('Upload result contains no storage key, path or URL', evidence && !Object.keys(evidence).some(k => /storage|key|path|url|location/i.test(k)) && !Object.entries(evidence).some(([k, v]) => k !== 'contentType' && /[\\/]/.test(String(v))), Object.keys(evidence || {}).join(','));
  const evidenceRequest = evidenceResponse?.request();
  const auth = evidenceRequest ? await evidenceRequest.headerValue('authorization') : null;
  check('Upload uses a bearer token header and multipart body; no token or claim in the URL', /^Bearer [A-Za-z0-9_-]{20,}$/.test(auth || '') && /^multipart\/form-data; boundary=/.test(await evidenceRequest?.headerValue('content-type') || '')
    && !evidenceRequest.url().includes(auth?.slice(7) || '~') && !evidenceRequest.url().includes(claim));
  await page.waitForFunction(() => document.querySelector('#evidence-upload-status')?.dataset.state === 'uploaded', null, {timeout: 10000}).catch(() => {});
  const statusText = await page.locator('#submission-status').innerText();
  check('Receipt shows metadata and evidence as separate, confirmed steps', /Saved on the CivicLens server/.test(statusText) && /Uploaded privately/.test(statusText) && /not shown in public tracking/.test(statusText));
  check('Receipt says a copy stays in this browser', /copy in this browser/i.test(receiptText));
  if (EVIDENCE_DIR) {
    const after = storedObjects();
    const newest = after.sort((a, b) => statSync(b).mtimeMs - statSync(a).mtimeMs)[0];
    check('Exactly one new file is stored privately and its checksum matches', after.length === objectsBefore + 1 && newest && createHash('sha256').update(readFileSync(newest)).digest('hex') === evidence?.sha256 && statSync(newest).size === evidence?.byteSize);
    check('Stored file is outside the public frontend folder', !path.resolve(newest || '').startsWith(path.resolve('dist')));
  }
  await page.screenshot({path: path.join(SHOTS, 'backend-receipt.png'), fullPage: true});

  const stored = await storageDump(page);
  check('Claim code is not stored in localStorage or sessionStorage', claim && !stored.text.includes(claim));
  check('Evidence is kept in IndexedDB under the server-evidence key', stored.evidenceKeys.includes('server-evidence:' + publicId), stored.evidenceKeys.join(','));
  check('No local complaint record was created for the server report', !stored.recordIds.includes(publicId));

  const direct = await fetch(`${API}/complaints/${publicId}`);
  const directText = await direct.text();
  check('API returns the complaint publicly without the claim code or GPS', direct.status === 200 && directText.includes(publicId) && !directText.includes(claim) && !directText.includes('13.0358'));
  check('Public API response exposes no evidence ID, checksum or media link', !directText.includes(evidence?.evidenceId || '~') && !directText.includes(evidence?.sha256 || '~') && !/evidence|media|url/i.test(directText));

  await page.goto(`${BASE}/#/track/${publicId}`);
  await page.waitForFunction(id => document.querySelector('#track-outcome')?.textContent.includes(id) && !/Checking/.test(document.querySelector('#track-outcome').textContent), publicId, {timeout: 8000}).catch(() => {});
  const trackText = await page.locator('main').innerText();
  check('Tracking page uses the server in backend mode', await page.locator('main[data-track-source="server"]').count() === 1);
  check('Tracking shows the server record (ID, category, status, department)', trackText.includes(publicId) && /Potholes/.test(trackText) && /Submitted/.test(trackText) && /Awaiting civic review/.test(trackText));
  check('Tracking never shows the claim code, coordinates or description', !trackText.includes(claim) && !trackText.includes('13.0358') && !trackText.includes(DEMO_DESCRIPTION));
  check('Tracking page shows no evidence image, video or evidence ID', await page.locator('main img, main video').count() === 0 && !trackText.includes(evidence?.evidenceId || '~'));
  await page.screenshot({path: path.join(SHOTS, 'backend-track.png')});

  await page.goto(`${BASE}/#/receipt/${publicId}`);
  await page.waitForTimeout(800);
  check('Reopening the receipt does not show the claim code again', !(await page.content()).includes(claim), await page.evaluate(() => location.hash));

  await page.goto(`${BASE}/#/track/NCL-2026-DEMO01`);
  await page.waitForFunction(() => /sample report/i.test(document.querySelector('#track-outcome')?.textContent || ''), null, {timeout: 8000}).catch(() => {});
  const demoText = await page.locator('#track-outcome').innerText();
  check('Sample IDs in backend mode explain that samples are local-only (no local fallback)', /sample report/i.test(demoText) && !/Yelahanka/.test(demoText));
  await ctx.close();
}

// 2. Backend mode with the server failing: no false success, no local fallback.
{
  const {ctx, page} = await newPage({backend: true});
  await captureAndFill(page);
  await ctx.route(API + '/complaints', route => route.fulfill({status: 503, contentType: 'application/json', body: JSON.stringify({error: {code: 'storage_unavailable', message: 'The complaint could not be saved or read right now. Try again shortly.'}})}));
  await page.click('#submit-btn');
  await page.waitForFunction(() => /Not sent/.test(document.querySelector('#submit-error')?.textContent || ''), null, {timeout: 8000}).catch(() => {});
  check('Server error: stays on review with a clear "Not sent" message', (await page.evaluate(() => location.hash)) === '#/review' && /Try again shortly/.test(await page.locator('#submit-error').innerText()));

  await ctx.unroute(API + '/complaints');
  await ctx.route(API + '/complaints', route => route.abort('connectionreset'));
  await page.click('#submit-btn');
  await page.waitForFunction(() => /Not confirmed/.test(document.querySelector('#submit-error')?.textContent || ''), null, {timeout: 8000}).catch(() => {});
  const ambiguous = await page.locator('#submit-error').innerText();
  check('Connection failure: warns the outcome is unknown and a retry could duplicate', (await page.evaluate(() => location.hash)) === '#/review' && /duplicate/i.test(ambiguous));
  const stored = await storageDump(page);
  check('Failed submissions created no local record and kept no server evidence', !stored.evidenceKeys.some(k => k.startsWith('server-evidence:')) && stored.recordIds.every(id => /DEMO0\d$/.test(id)), stored.recordIds.join(','));

  await ctx.route(API + '/complaints/**', route => route.abort('connectionrefused'));
  await page.goto(`${BASE}/#/track/NCL-2026-DEMO01`);
  await page.waitForFunction(() => /unavailable/i.test(document.querySelector('#track-outcome')?.textContent || ''), null, {timeout: 8000}).catch(() => {});
  const down = await page.locator('#track-outcome').innerText();
  check('Server unreachable: tracking shows an error and no local record', /Tracking is unavailable/.test(down) && !/Yelahanka/.test(down));
  await ctx.close();
}

// 2b. Complaint saved, evidence upload fails, then a retry succeeds against the same complaint.
{
  const {ctx, page} = await newPage({backend: true});
  let complaintPosts = 0;
  page.on('request', req => { if (req.method() === 'POST' && req.url() === API + '/complaints') complaintPosts += 1; });
  await ctx.route(/\/complaints\/NCL-\d{4}-[A-Z0-9]{6}\/evidence$/, route => route.fulfill({status: 503, contentType: 'application/json', body: JSON.stringify({error: {code: 'storage_unavailable', message: 'The complaint could not be saved or read right now. Try again shortly.'}})}));
  await captureAndFill(page);
  await page.click('#submit-btn');
  await waitHash(page, '^#/receipt/NCL-');
  const failedId = decodeURIComponent((await page.evaluate(() => location.hash)).split('/')[2] || '');
  await page.waitForFunction(() => document.querySelector('#evidence-upload-status')?.dataset.state === 'failed', null, {timeout: 10000}).catch(() => {});
  const failedText = await page.locator('#submission-status').innerText();
  check('Upload failure: receipt keeps the complaint and reports only the photo upload as failed', /Saved on the CivicLens server/.test(failedText) && /Upload failed/.test(failedText) && failedText.includes(failedId) && /is saved/.test(failedText));
  check('Upload failure: a retry button explains it will not create a new complaint', await page.locator('#retry-upload').count() === 1 && /does not create a new complaint/.test(failedText));
  const kept = await storageDump(page);
  check('Upload failure: the local evidence copy is preserved', kept.evidenceKeys.includes('server-evidence:' + failedId));
  await ctx.unroute(/\/complaints\/NCL-\d{4}-[A-Z0-9]{6}\/evidence$/);
  const retryResponse = page.waitForResponse(isEvidenceUpload, {timeout: 15000}).catch(() => null);
  await page.click('#retry-upload');
  const retried = await retryResponse;
  await page.waitForFunction(() => document.querySelector('#evidence-upload-status')?.dataset.state === 'uploaded', null, {timeout: 10000}).catch(() => {});
  check('Retry uploads to the same complaint and succeeds', retried?.status() === 201 && retried.url().endsWith(`/complaints/${failedId}/evidence`) && /Uploaded privately/.test(await page.locator('#evidence-upload-status').innerText()));
  check('Retry did not create a second complaint', complaintPosts === 1, String(complaintPosts));
  await page.screenshot({path: path.join(SHOTS, 'backend-upload-retry.png'), fullPage: true});
  await ctx.close();
}

// 2d. Lost response: the server creates the complaint but the page never sees the answer. The retry
// reuses the same Idempotency-Key and recovers the same complaint instead of creating a duplicate.
{
  const {ctx, page} = await newPage({backend: true});
  const keys = [];
  page.on('request', req => { if (req.method() === 'POST' && req.url() === API + '/complaints') keys.push(req.headers()['idempotency-key'] || null); });
  let firstServerReceipt = null;
  await ctx.route(API + '/complaints', async route => {
    const real = await route.fetch();            // the request really reaches the server…
    firstServerReceipt = await real.json().catch(() => null);
    await route.abort('connectionreset');        // …but the response is lost on the way back
  });
  await captureAndFill(page);
  await page.click('#submit-btn');
  await page.waitForFunction(() => /Not confirmed/.test(document.querySelector('#submit-error')?.textContent || ''), null, {timeout: 10000}).catch(() => {});
  const lostText = await page.locator('#submit-error').innerText();
  check('Lost response: the server created a complaint but the page reports "Not confirmed" and says retrying is safe', /^NCL-/.test(firstServerReceipt?.id || '') && /Not confirmed/.test(lostText) && /will not create a duplicate/.test(lostText));
  await ctx.unroute(API + '/complaints');
  const replayResponse = page.waitForResponse(r => r.url() === API + '/complaints' && r.request().method() === 'POST', {timeout: 10000}).catch(() => null);
  const uploadAfterReplay = page.waitForResponse(isEvidenceUpload, {timeout: 20000}).catch(() => null);
  await page.click('#submit-btn');
  const replay = await replayResponse;
  await waitHash(page, '^#/receipt/NCL-');
  const recoveredId = decodeURIComponent((await page.evaluate(() => location.hash)).split('/')[2] || '');
  const replayBody = await replay?.json().catch(() => null);
  check('Retry reuses the same Idempotency-Key', keys.length === 2 && keys[0] && keys[0] === keys[1] && /^[A-Za-z0-9_-]{43}$/.test(keys[0]), `${keys.length} requests`);
  check('Retry recovers the same complaint (200, replayed) instead of creating a duplicate', replay?.status() === 200 && replayBody?.replayed === true && recoveredId === firstServerReceipt?.id);
  check('Recovered receipt shows a new claim code and explains the earlier one no longer works', /Recovered after an interrupted connection/.test(await page.locator('main').innerText())
    && (await page.locator('.code-private').innerText()).trim() === replayBody?.claim && replayBody?.claim !== firstServerReceipt?.claim);
  const uploaded = await uploadAfterReplay;
  check('Evidence uploads with the re-issued token after recovery', uploaded?.status() === 201 && uploaded.url().endsWith(`/complaints/${recoveredId}/evidence`));
  const keyInStorage = (await storageDump(page)).text.includes(keys[0] || '~');
  check('The idempotency key is not stored in browser storage or put in the URL', !keyInStorage && !(await page.evaluate(() => location.href)).includes(keys[0] || '~'));

  const nextKey = page.waitForRequest(req => req.method() === 'POST' && req.url() === API + '/complaints', {timeout: 15000}).catch(() => null);
  await captureAndFill(page);
  await page.click('#submit-btn');
  const second = await nextKey;
  await waitHash(page, '^#/receipt/NCL-');
  const secondId = decodeURIComponent((await page.evaluate(() => location.hash)).split('/')[2] || '');
  check('A genuinely new report uses a new key and becomes a new complaint', second && second.headers()['idempotency-key'] !== keys[0] && secondId !== recoveredId);
  await ctx.close();
}

// 2c. A real MediaRecorder video from the synthetic camera is accepted by the server's signature check.
{
  const {ctx, page} = await newPage({backend: true});
  const reached = await captureAndFill(page, {video: true});
  check('Video: recording reaches review', reached && await page.locator('.review-media video').count() === 1);
  const videoResponse = page.waitForResponse(isEvidenceUpload, {timeout: 20000}).catch(() => null);
  await page.click('#submit-btn');
  await waitHash(page, '^#/receipt/NCL-');
  const uploaded = await videoResponse;
  const result = await uploaded?.json().catch(() => null);
  await page.waitForFunction(() => document.querySelector('#evidence-upload-status')?.dataset.state === 'uploaded', null, {timeout: 10000}).catch(() => {});
  check('Video: the recorded clip uploads and is stored as video', uploaded?.status() === 201 && result?.mediaType === 'video' && ['video/webm', 'video/mp4'].includes(result?.contentType), `${uploaded?.status()} ${result?.contentType} ${result?.byteSize} bytes`);
  await ctx.close();
}

// 2e. Staff console: server sign-in, complaint list, evidence viewing with an access log.
const STAFF_USERNAME = process.env.STAFF_USERNAME;
const STAFF_PASSWORD = process.env.STAFF_PASSWORD;
if (!STAFF_USERNAME || !STAFF_PASSWORD) {
  console.log('SKIP  Staff console checks: set STAFF_USERNAME and STAFF_PASSWORD (account from `python -m app.staff create`).');
} else {
  const {ctx, page} = await newPage({backend: true});
  check('Staff: anonymous API requests for evidence are refused', (await fetch(`${API}/staff/complaints/${publicId}/evidence`)).status === 401);
  await page.goto(BASE + '/#/login/official');
  check('Staff: in server mode the admin sign-in is the server staff sign-in, not the demo login', await page.waitForSelector('[data-server-login]', {timeout: 5000}).then(() => true, () => false)
    && !(await page.content()).includes('CivicDemo2026'));
  await page.goto(BASE + '/#/staff');
  await page.waitForSelector('#staff-sign-in', {timeout: 8000}).catch(() => {});
  check('Staff: the console asks for a server sign-in', await page.locator('#staff-sign-in').count() === 1);
  await page.fill('#staff-username', STAFF_USERNAME);
  await page.fill('#staff-password', 'definitely not the password');
  await page.click('#staff-sign-in button[type=submit]');
  await page.waitForFunction(() => /incorrect/.test(document.querySelector('#staff-sign-in-error')?.textContent || ''), null, {timeout: 8000}).catch(() => {});
  check('Staff: a wrong password is refused with a generic message', /username or password is incorrect/.test(await page.locator('#staff-sign-in-error').innerText()));

  const signInResponse = page.waitForResponse(r => r.url() === API + '/staff/sessions' && r.request().method() === 'POST' && r.status() === 200, {timeout: 10000}).catch(() => null);
  await page.fill('#staff-password', STAFF_PASSWORD);
  await page.click('#staff-sign-in button[type=submit]');
  const session = await (await signInResponse)?.json().catch(() => null);
  const staffToken = session?.token;
  await page.waitForSelector('#complaint-results table', {timeout: 8000}).catch(() => {});
  const listText = await page.locator('main').innerText();
  check('Staff: signing in opens Incident management with the reported complaint', Boolean(staffToken) && await waitHash(page, '^#/official/complaints$').then(() => true, () => false)
    && listText.includes(publicId));

  await page.click(`#complaint-results a[href="#/issue/${publicId}"]`);
  await page.waitForSelector('.detail-layout', {timeout: 8000}).catch(() => {});
  const detailText = await page.locator('main').innerText();
  check('Staff: the case opens in the original case page with operational details (description and precise location)',
    await page.locator('#workspace .sidebar').count() === 1 && detailText.includes(DEMO_DESCRIPTION) && /13\.03580/.test(detailText));
  check('Staff: the case shows no claim code', !detailText.includes(claim));
  const evidenceResponse = page.waitForResponse(r => r.url() === `${API}/staff/complaints/${publicId}/evidence`, {timeout: 15000}).catch(() => null);
  await page.click('#staff-view-evidence');
  const viewed = await evidenceResponse;
  await page.waitForFunction(() => { const i = document.querySelector('#staff-evidence-view img'); return i && i.complete && i.naturalWidth > 0; }, null, {timeout: 10000}).catch(() => {});
  const shown = await page.evaluate(() => { const i = document.querySelector('#staff-evidence-view img'); return i ? {w: i.naturalWidth, blob: i.src.startsWith('blob:')} : null; });
  check('Staff: viewing streams the private photo through the authenticated endpoint', viewed?.status() === 200 && viewed.headers()['content-type'] === 'image/jpeg'
    && viewed.headers()['cache-control'] === 'no-store, private' && shown?.w > 0 && shown.blob, `${viewed?.status()} ${shown?.w}px`);
  await page.waitForFunction(() => /Viewed the evidence/.test(document.querySelector('#staff-access-log')?.textContent || ''), null, {timeout: 8000}).catch(() => {});
  check('Staff: the view appears in the evidence access log with the staff name', (await page.locator('#staff-access-log').innerText()).includes(STAFF_USERNAME));
  const accessLog = await fetch(`${API}/staff/complaints/${publicId}/evidence/access`, {headers: {Authorization: `Bearer ${staffToken}`}}).then(r => r.json()).catch(() => []);
  check('Staff: the server recorded exactly one view for this session', Array.isArray(accessLog) && accessLog.filter(e => e.username === STAFF_USERNAME).length === 1, String(accessLog.length));
  const stored = await storageDump(page);
  check('Staff: the session token is not stored in the browser or put in the URL', !stored.text.includes(staffToken || '~') && !(await page.evaluate(() => location.href)).includes(staffToken || '~'));

  await page.reload();
  await page.waitForSelector('[data-server-login]', {timeout: 8000}).catch(() => {});
  check('Staff: reloading signs the tab out (session held in memory only)', await page.locator('[data-server-login]').count() === 1);
  await page.fill('#login-username', STAFF_USERNAME);
  await page.fill('#login-password', STAFF_PASSWORD);
  const second = page.waitForResponse(r => r.url() === API + '/staff/sessions' && r.status() === 200, {timeout: 10000}).catch(() => null);
  await page.click('#login-form button[type=submit]');
  const secondToken = (await (await second)?.json().catch(() => null))?.token;
  await page.waitForSelector('[data-action="logout"]', {timeout: 8000}).catch(() => {});
  await page.click('[data-action="logout"]');
  await page.waitForTimeout(1000);
  const afterSignOut = await fetch(`${API}/staff/me`, {headers: {Authorization: `Bearer ${secondToken}`}});
  check('Staff: signing out revokes the session on the server', Boolean(secondToken) && afterSignOut.status === 401);
  await page.screenshot({path: path.join(SHOTS, 'staff-console.png'), fullPage: true});
  await ctx.close();
}

// 3. Local mode (the real api-config.json): unchanged behaviour, no API calls.
{
  const {ctx, page} = await newPage({backend: false});
  const apiCalls = [];
  page.on('request', req => { if (req.url().startsWith(API)) apiCalls.push(req.url()); });
  await page.goto(`${BASE}/#/track/NCL-2026-DEMO01`);
  await page.waitForSelector('.track-result', {timeout: 8000}).catch(() => {});
  const text = await page.locator('main').innerText();
  check('Local mode: sample record tracks from browser storage as before', /NCL-2026-DEMO01/.test(text) && /Yelahanka/.test(text) && await page.locator('main[data-track-source="server"]').count() === 0);
  await captureAndFill(page);
  await page.click('#submit-btn');
  await waitHash(page, '^#/receipt/NCL-');
  const localTitle = await page.waitForFunction(() => document.querySelector('#page-title')?.textContent.includes('Report saved on this device'), null, {timeout: 8000}).then(() => true, () => false);
  check('Local mode: submission shows the existing on-device receipt', localTitle && await page.locator('main[data-receipt-source="server"]').count() === 0, await page.evaluate(() => location.hash));
  await page.goto(BASE + '/#/staff');
  check('Local mode: the staff console explains it needs server mode', await page.waitForFunction(() => /works only in/.test(document.querySelector('main')?.textContent || ''), null, {timeout: 5000}).then(() => true, () => false));
  check('Local mode: the API is never contacted', apiCalls.length === 0, apiCalls.join(', '));
  await ctx.close();
}

check('Claim code never appears in the browser console', claim && !consoleText.some(t => t.includes(claim)));
check('No page errors during integration checks', errors.length === 0, errors.slice(0, 3).join(' | '));
await browser.close();
const failed = results.filter(r => !r.ok).length;
console.log(`\nCreated and tracked ${publicId || 'no complaint'} through the API.`);
console.log(`${results.length - failed}/${results.length} checks passed.`);
process.exit(failed ? 1 : 0);
