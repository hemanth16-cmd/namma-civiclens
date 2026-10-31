// Role isolation in a real browser: passerby, resident and administrator journeys against a running API.
//
// Needs: the frontend (npm run dev) and the API on an ISOLATED database (never production), with the
// demonstration accounts and one department:
//   cd backend
//   CIVICLENS_DB_PATH=/tmp/civiclens-e2e.sqlite3 CIVICLENS_RATE_LIMITS=off python -m app --port 8000
//   CIVICLENS_DB_PATH=/tmp/civiclens-e2e.sqlite3 python -m app.demo seed --confirm-demo
//   CIVICLENS_DB_PATH=/tmp/civiclens-e2e.sqlite3 python -m app.staff department add --code roads --name "Road maintenance"
// Then: BASE=http://localhost:4173 API=http://localhost:8000/api/v1 SHOTS=./screenshots node scripts/roles-e2e.mjs
// Use a fresh database for each run: the checks count the demo resident's complaints.
// Backend mode is switched on by intercepting api-config.json in the browser; no repository file changes.
import {createRequire} from 'node:module';
import fs from 'node:fs';
import {createHash} from 'node:crypto';

let chromium;
try { ({chromium} = await import('playwright')); } catch {
  chromium = createRequire(import.meta.url)(process.env.PLAYWRIGHT_PATH || 'playwright').chromium;
}
const API = (process.env.API || 'http://localhost:8000/api/v1').replace(/\/+$/, ''), BASE = process.env.BASE || 'http://localhost:4173';
const SHOTS = process.env.SHOTS || './screenshots'; fs.mkdirSync(SHOTS, {recursive: true});

const DEMO = {email: 'resident@civiclens.demo', password: 'CivicDemo2026!', admin: 'demo_admin'};
let fails = 0; const results = [];
const check = (n, ok, d = '') => { results.push([n, !!ok]); if (!ok) fails++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${n}${d ? ' — ' + d : ''}`); };
const consoleLines = [];

async function context(browser) {
  const ctx = await browser.newContext({viewport: {width: 1280, height: 860}});
  await ctx.route('**/api-config.json', r => r.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({mode: 'backend', baseUrl: API})}));
  const page = await ctx.newPage();
  page.errors = [];
  page.on('pageerror', e => page.errors.push(e.message));
  page.on('console', m => consoleLines.push(m.text()));
  return page;
}
const go = async (p, hash, wait = 900) => { await p.goto(BASE + '/#/' + hash); await p.waitForTimeout(wait); };
const hash = p => p.url().split('#')[1] || '';
const title = p => p.textContent('#page-title, #login-title').then(t => t.trim()).catch(() => '');
// The resident's My complaints page (server mode) lists cards in #mc-results once loaded.
const myComplaints = async p => {
  await p.waitForFunction(() => !/Loading/.test(document.querySelector('#mc-count')?.textContent || 'Loading'), null, {timeout: 8000}).catch(() => {});
  return p.textContent('#mc-results').catch(() => '');
};
const session = p => p.evaluate(() => sessionStorage.getItem('ncl-session'));
const residentToken = p => p.evaluate(async () => (await import('/resident-session.js')).residentSession.get()?.token ?? null);

// SHA-256 of the bytes an <img>/<video> on the page is actually showing (read back from its blob: URL).
const shownSha = (p, sel) => p.evaluate(async sel => {
  const el = document.querySelector(sel); if (!el) return null;
  const buf = await (await fetch(el.currentSrc || el.src)).arrayBuffer();
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256', buf))].map(b => b.toString(16).padStart(2, '0')).join('');
}, sel);
const sha = b64 => createHash('sha256').update(Buffer.from(b64, 'base64')).digest('hex');
const seen = {};

// Media made in the page: a real JPEG from a canvas and a real WebM from MediaRecorder.
async function makeMedia(p) {
  return p.evaluate(async () => {
    const c = document.createElement('canvas'); c.width = 320; c.height = 240;
    const g = c.getContext('2d'); g.fillStyle = '#c33'; g.fillRect(0, 0, 320, 240); g.fillStyle = '#fff'; g.fillText('CivicLens e2e', 20, 120);
    const jpeg = await new Promise(r => c.toBlob(r, 'image/jpeg', 0.9));
    const rec = new MediaRecorder(c.captureStream(15), {mimeType: 'video/webm'}); const parts = [];
    rec.ondataavailable = e => parts.push(e.data); rec.start(200);
    for (let i = 0; i < 20; i++) { g.fillStyle = `hsl(${i * 18},70%,50%)`; g.fillRect(0, 0, 320, 240); await new Promise(r => setTimeout(r, 60)); }
    await new Promise(r => { rec.onstop = r; rec.stop(); });
    const webm = new Blob(parts, {type: 'video/webm'});
    const b64 = async b => btoa(String.fromCharCode(...new Uint8Array(await b.arrayBuffer())));
    return {jpeg: await b64(jpeg), webm: await b64(webm)};
  });
}

async function anonymousComplaint(media, kind) {
  const gps = {lat: 13.0358, lng: 77.597, accuracy: 10, sourceType: 'gps', timestamp: Date.now() - 2000};
  const r = await fetch(API + '/complaints', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({category: 'Potholes', mediaType: kind, gps})});
  const rec = await r.json();
  const form = new FormData();
  form.append('file', new Blob([Buffer.from(kind === 'video' ? media.webm : media.jpeg, 'base64')], {type: kind === 'video' ? 'video/webm' : 'image/jpeg'}), kind === 'video' ? 'e.webm' : 'e.jpg');
  const up = await fetch(`${API}/complaints/${rec.id}/evidence`, {method: 'POST', headers: {Authorization: 'Bearer ' + rec.uploadToken}, body: form});
  return {...rec, status: r.status, uploadStatus: up.status};
}

{
  const browser = await chromium.launch();

  // ---------------- Passerby ----------------
  const guest = await context(browser);
  await go(guest, 'home');
  const media = await makeMedia(guest);
  const photo = await anonymousComplaint(media, 'photo');
  const video = await anonymousComplaint(media, 'video');
  check('passerby: anonymous photo complaint + private evidence upload', photo.status === 201 && photo.uploadStatus === 201);
  check('passerby: anonymous video complaint + private evidence upload', video.status === 201 && video.uploadStatus === 201);
  await go(guest, 'resident');
  check('passerby: #/resident → resident sign-in, no session', hash(guest) === '/login/resident' && (await session(guest)) === null);
  check('passerby: sign-in form has no prefilled or shown credentials', (await guest.inputValue('#login-email')) === '' && !(await guest.content()).includes('CivicDemo'));
  for (const r of ['resident/complaints', 'resident/claim', 'resident/settings', 'resident/community', 'resident/map', 'resident/notifications']) {
    await go(guest, r, 600);
    check(`passerby: #/${r} → resident sign-in, no session`, hash(guest) === '/login/resident' && (await session(guest)) === null, hash(guest));
  }
  for (const r of ['official', 'official/complaints', 'issue/' + photo.id]) {
    await go(guest, r, 900);
    check(`passerby: #/${r} shows no staff data`, ['/login/official', '/track/' + photo.id].includes(hash(guest)) && (await session(guest)) === null, hash(guest));
  }
  for (const stale of [{role: 'resident', name: 'This device', email: '', server: true, device: true}, {role: 'resident', name: 'Demo resident', email: 'resident@civiclens.demo'}]) {
    await guest.evaluate(v => sessionStorage.setItem('ncl-session', JSON.stringify(v)), stale);
    await guest.reload(); await go(guest, 'resident', 700);
    check(`passerby: a leftover ${stale.device ? '"this device"' : 'local demo'} identity does not open the resident workspace`, hash(guest) === '/login/resident' && (await session(guest)) === null);
  }
  await guest.evaluate(id => localStorage.setItem('ncl-server-reports-v1', JSON.stringify([{id, category: 'Potholes', submittedAt: new Date().toISOString()}])), photo.id);
  await go(guest, 'my-reports', 1500);
  check('passerby: Reports on This Device lists the report without any role', (await title(guest)) === 'Reports on This Device' && (await guest.textContent('main')).includes(photo.id) && (await session(guest)) === null);
  await go(guest, 'track/' + photo.id, 1500);
  const track = await guest.textContent('#track-outcome');
  check('passerby: public tracking works', track.includes(photo.id));
  check('passerby: tracking shows no evidence until ownership is proven', !(await guest.$('#owner-evidence img, #owner-evidence video')));
  await go(guest, 'track/' + video.id, 1500);
  await guest.fill('#owner-evidence-claim', '00000000-0000-4000-8000-000000000000');
  await guest.click('#owner-evidence-form button[type=submit]'); await guest.waitForTimeout(1200);
  check('passerby: a wrong claim code shows no evidence', /not correct/i.test(await guest.textContent('#owner-evidence-status')) && !(await guest.$('#owner-evidence video')));
  await guest.fill('#owner-evidence-claim', video.claim);
  await guest.click('#owner-evidence-form button[type=submit]'); await guest.waitForTimeout(2500);
  const gv = await guest.evaluate(async () => { const v = document.querySelector('#owner-evidence video'); if (!v) return null; v.muted = true; try { await v.play(); } catch (e) { return {error: String(e)}; } await new Promise(r => setTimeout(r, 400)); return {ready: v.readyState, time: v.currentTime, src: v.src.slice(0, 5)}; });
  check('passerby: the claim code shows their own video, playing from a blob: URL', gv && !gv.error && gv.ready >= 2 && gv.time > 0 && gv.src === 'blob:', JSON.stringify(gv));
  seen.passerbyVideo = await shownSha(guest, '#owner-evidence video');
  check('passerby: the claim code never reaches the URL or browser storage', !guest.url().includes(video.claim) && !(await guest.evaluate(() => JSON.stringify({...localStorage}) + JSON.stringify({...sessionStorage}))).includes(video.claim));
  await guest.screenshot({path: SHOTS + '/1b-passerby-own-video.png'});
  await guest.screenshot({path: SHOTS + '/1-passerby-tracking.png'});

  // ---------------- Resident ----------------
  const res = await context(browser);
  await go(res, 'login/resident');
  await res.fill('#login-email', DEMO.email); await res.fill('#login-password', 'wrong password!!');
  await res.click('#login-form button[type=submit]'); await res.waitForTimeout(900);
  check('resident: invalid password is refused', hash(res) === '/login/resident' && /incorrect/i.test(await res.textContent('#login-error')));
  await res.fill('#login-email', DEMO.email); await res.fill('#login-password', DEMO.password);
  await res.click('#login-form button[type=submit]'); await res.waitForTimeout(1500);
  check('resident: demo login opens the resident dashboard', hash(res) === '/resident' && (await title(res)) === 'Hello, Demo resident', `${hash(res)} "${await title(res)}"`);
  const sess = JSON.parse(await session(res) || '{}');
  check('resident: display identity is a server resident', sess.role === 'resident' && sess.server === true && !sess.device);
  const tok = await residentToken(res);
  const stored = await res.evaluate(() => JSON.stringify({...localStorage}) + JSON.stringify({...sessionStorage}));
  check('resident: token is kept in memory only', !!tok && !stored.includes(tok));
  await res.screenshot({path: SHOTS + '/2-resident-dashboard.png'});

  await go(res, 'resident/claim');
  await res.fill('#claim-id', photo.id); await res.fill('#claim-code', '00000000-0000-4000-8000-000000000000');
  await res.click('#claim-form button[type=submit]'); await res.waitForTimeout(900);
  check('resident: a wrong claim code is rejected', /not correct/i.test(await res.textContent('#claim-error')));
  await res.fill('#claim-id', photo.id.toLowerCase()); await res.fill('#claim-code', photo.claim);
  await res.click('#claim-form button[type=submit]'); await res.waitForTimeout(1500);
  check('resident: the right claim code claims the guest complaint', hash(res) === '/resident/complaints' && (await myComplaints(res)).includes(photo.id));
  await go(res, 'track/' + photo.id, 1500);
  await res.click('#owner-evidence-account'); await res.waitForTimeout(2500);
  const ri = await res.evaluate(() => { const i = document.querySelector('#owner-evidence img'); return i ? {w: i.naturalWidth, src: i.src.slice(0, 5)} : null; });
  check('resident: sees the photo of a claimed complaint from their account', ri && ri.w === 320 && ri.src === 'blob:', JSON.stringify(ri));
  seen.residentPhoto = await shownSha(res, '#owner-evidence img');
  await res.screenshot({path: SHOTS + '/3b-resident-own-photo.png'});
  await res.screenshot({path: SHOTS + '/3-resident-my-complaints.png'});

  const submitted = await res.evaluate(async () => {
    const {submitReport} = await import('/report-gateway.js');
    const s = await submitReport({category: 'Garbage accumulation', mediaType: 'photo', gps: {lat: 13.04, lng: 77.6, accuracy: 9, timestamp: Date.now() - 2000, sourceType: 'gps'}}, null, {});
    return s.id;
  });
  await go(res, 'resident', 800);
  await go(res, 'resident/complaints', 1500);
  check('resident: a complaint submitted while signed in is in My complaints', (await myComplaints(res)).includes(submitted));
  const own = await (await fetch(API + '/residents/me/complaints', {headers: {Authorization: 'Bearer ' + tok}})).json();
  check('resident: server lists exactly the claimed and the submitted complaint', own.total === 2 && own.items.some(i => i.id === photo.id && i.ownedVia === 'claimed') && own.items.some(i => i.id === submitted && i.ownedVia === 'submitted'));

  await go(res, 'resident/complaints/' + photo.id, 2500);
  await res.waitForFunction(() => document.querySelector('#mc-detail img'), null, {timeout: 8000}).catch(() => {});
  const mc = await res.evaluate(() => { const i = document.querySelector('#mc-detail img'); return i ? {w: i.naturalWidth, src: i.src.slice(0, 5)} : null; });
  check('resident: My Complaints detail shows the claimed complaint photo', mc && mc.w === 320 && mc.src === 'blob:', JSON.stringify(mc));
  seen.myComplaintsPhoto = await shownSha(res, '#mc-detail img');
  await go(res, 'resident/settings', 1200);
  await res.click('input[name="updates"]'); await res.waitForTimeout(900);
  const prefs = await (await fetch(API + '/residents/me/preferences', {headers: {Authorization: 'Bearer ' + tok}})).json();
  check('resident: preferences are saved to the account on the server', prefs.updates === false && /saved to your account/i.test(await res.textContent('#prefs-status')));
  check('resident: account panel shows demo warning', /demonstration/i.test(await res.textContent('[data-panel="account"]')));
  await res.screenshot({path: SHOTS + '/4-resident-preferences.png'});
  await go(res, 'resident/map', 2500);
  check('resident: city map page loads', (await title(res)).length > 0 && res.errors.length === 0, res.errors.join('; '));
  await go(res, 'resident/community');
  await res.waitForFunction(() => !/Loading/.test(document.querySelector('#community-count')?.textContent || 'Loading'), null, {timeout: 8000}).catch(() => {});
  check('resident: community issues list public Bengaluru complaints from the server', (await res.textContent('#community-results')).includes(photo.id),
    (await res.textContent('#community-count')).trim());
  await go(res, 'resident/notifications', 1200);
  check('resident: notifications open the notification inbox', hash(res).startsWith('/notifications'));
  await go(res, 'official');
  check('resident: cannot open the admin workspace', hash(res) === '/login/official');
  const staffWithResident = await fetch(API + '/staff/complaints', {headers: {Authorization: 'Bearer ' + tok}});
  check('resident: resident token is refused by staff APIs', staffWithResident.status === 401);

  await go(res, 'resident', 1200);
  await res.click('[data-action="logout"]'); await res.waitForTimeout(1200);
  const after = await fetch(API + '/residents/me', {headers: {Authorization: 'Bearer ' + tok}});
  check('resident: logout ends the session on the server and in the tab', after.status === 401 && (await session(res)) === null && hash(res) === '/home');
  await go(res, 'resident');
  check('resident: after logout #/resident needs sign-in again', hash(res) === '/login/resident');
  await res.fill('#login-email', DEMO.email); await res.fill('#login-password', DEMO.password);
  await res.click('#login-form button[type=submit]'); await res.waitForTimeout(1200);
  await res.reload(); await res.waitForTimeout(1200);
  await go(res, 'resident');
  check('resident: a reload signs out (memory-only session)', hash(res) === '/login/resident' && (await session(res)) === null);

  // ---------------- A second resident ----------------
  const other = await context(browser);
  await go(other, 'register');
  await other.fill('#login-name', 'Bina'); await other.fill('#login-email', `bina+${Date.now()}@example.org`); await other.fill('#login-password', 'bina passphrase 1');
  await other.click('#login-form button[type=submit]'); await other.waitForTimeout(1500);
  check('second resident: registration signs in', hash(other) === '/resident' && (await title(other)) === 'Hello, Bina');
  await go(other, 'resident/complaints', 1200);
  const otherList = await myComplaints(other);
  check("second resident: does not see the first resident's complaints", !otherList.includes(photo.id) && !otherList.includes(submitted));
  await go(other, 'resident/claim');
  await other.fill('#claim-id', photo.id); await other.fill('#claim-code', photo.claim);
  await other.click('#claim-form button[type=submit]'); await other.waitForTimeout(900);
  check('second resident: cannot take a claimed complaint even with its code', /already linked/i.test(await other.textContent('#claim-error')));

  // ---------------- Administrator ----------------
  const adm = await context(browser);
  await go(adm, 'official');
  check('admin: #/official without sign-in → staff sign-in', hash(adm) === '/login/official');
  await adm.fill('#login-username', DEMO.email); await adm.fill('#login-password', DEMO.password);
  await adm.click('#login-form button[type=submit]'); await adm.waitForTimeout(900);
  check('admin: resident credentials do not sign in as staff', hash(adm) === '/login/official');
  await adm.fill('#login-username', DEMO.admin); await adm.fill('#login-password', DEMO.password);
  await adm.click('#login-form button[type=submit]'); await adm.waitForTimeout(2000);
  check('admin: demo_admin opens the admin dashboard', hash(adm) === '/official' && /overview/i.test(await title(adm)), `${hash(adm)} "${await title(adm)}"`);
  await adm.screenshot({path: SHOTS + '/5-admin-dashboard.png'});
  await go(adm, 'official/complaints', 2000);
  const list = await adm.textContent('#complaint-results');
  check('admin: lists submitted complaints (guest and resident)', [photo.id, video.id, submitted].every(id => list.includes(id)));

  await go(adm, 'issue/' + photo.id, 2500);
  check('admin: opens complaint details', (await adm.textContent('main')).includes(photo.id));
  await adm.waitForFunction(() => !/Loading AI results/.test(document.querySelector('#staff-ai')?.textContent || 'Loading AI results'), null, {timeout: 8000}).catch(() => {});
  const aiText = await adm.textContent('#case-ai').catch(() => '');
  check('admin: the AI analysis panel loads on the case page (AI off without a Roboflow key)', /AI analysis/.test(aiText) && /Repair verification/.test(aiText)
    && !/request failed|could not read/i.test(aiText), aiText.replace(/\s+/g, ' ').slice(0, 120));
  await adm.click('#staff-view-evidence'); await adm.waitForTimeout(2000);
  const img = await adm.evaluate(() => { const i = document.querySelector('.evidence-figure img.evidence-media'); return i ? {w: i.naturalWidth, src: i.src.slice(0, 5)} : null; });
  check('admin: views the authorised photo evidence', img && img.w === 320 && img.src === 'blob:', JSON.stringify(img));
  seen.adminPhoto = await shownSha(adm, '.evidence-figure img.evidence-media');
  await adm.screenshot({path: SHOTS + '/6-admin-photo.png', fullPage: false});
  const sel = await adm.$('#case-status');
  if (sel) {
    await adm.selectOption('#case-status', {index: 0});
    const to = await adm.$eval('#case-status', s => s.value);
    await adm.fill('#case-explanation', 'Inspection scheduled (e2e).');
    await adm.click('#case-form button[type=submit]'); await adm.waitForTimeout(1800);
    check('admin: changes the status and it appears in history', (await adm.textContent('main')).includes('Inspection scheduled (e2e).') || (await adm.textContent('main')).includes(to), to);
  } else check('admin: status form is available', false);
  await adm.waitForSelector('#case-department option[value="roads"]', {timeout: 5000}).catch(() => {});
  if (await adm.$('#assign-form')) {
    await adm.selectOption('#case-department', 'roads');
    const reason = await adm.$('#case-reason[required]'); if (reason) await adm.fill('#case-reason', 'Roads team');
    await adm.click('#assign-form button[type=submit]'); await adm.waitForTimeout(1800);
    check('admin: assigns the complaint to a department', /Road maintenance/.test(await adm.textContent('main')));
  } else check('admin: assignment form is available', false);
  if (await adm.$('#work-form')) {
    await adm.fill('#wo-team', 'Ward crew (e2e)');
    await adm.click('#work-form button[type=submit]'); await adm.waitForTimeout(1800);
    check('admin: creates a work order', (await adm.textContent('#work-orders')).includes('Ward crew (e2e)'));
  } else check('admin: work-order form is available', false);
  await go(adm, 'official/workorders', 2000);
  check('admin: work-order page lists it', (await adm.textContent('main')).includes('Ward crew (e2e)'));

  await go(adm, 'issue/' + video.id, 2500);
  await adm.click('#staff-view-evidence'); await adm.waitForTimeout(2500);
  const vid = await adm.evaluate(async () => {
    const v = document.querySelector('.evidence-figure video.evidence-media'); if (!v) return null;
    v.muted = true; try { await v.play(); } catch (e) { return {error: String(e)}; }
    await new Promise(r => setTimeout(r, 400));
    return {ready: v.readyState, time: v.currentTime, src: v.src.slice(0, 5)};
  });
  check('admin: plays the authorised video evidence', vid && !vid.error && vid.ready >= 2 && vid.time > 0 && vid.src === 'blob:', JSON.stringify(vid));
  seen.adminVideo = await shownSha(adm, '.evidence-figure video.evidence-media');
  check('same photo file: admin, resident owner and My Complaints see byte-identical evidence', seen.adminPhoto && seen.adminPhoto === seen.residentPhoto && seen.adminPhoto === seen.myComplaintsPhoto && seen.adminPhoto === sha(media.jpeg), JSON.stringify({a: seen.adminPhoto?.slice(0, 12), r: seen.residentPhoto?.slice(0, 12)}));
  check('same video file: admin and claim-code passerby see byte-identical evidence', seen.adminVideo && seen.adminVideo === seen.passerbyVideo && seen.adminVideo === sha(media.webm), JSON.stringify({a: seen.adminVideo?.slice(0, 12), p: seen.passerbyVideo?.slice(0, 12)}));
  await adm.screenshot({path: SHOTS + '/7-admin-video.png'});
  const unauth = await fetch(`${API}/staff/complaints/${video.id}/evidence`);
  check('admin APIs refuse unauthenticated requests (evidence)', unauth.status === 401);
  const stok = await adm.evaluate(async () => (await import('/staff-session.js')).staffSession.get()?.token);
  check('staff token cannot act as a resident', (await fetch(API + '/residents/me', {headers: {Authorization: 'Bearer ' + stok}})).status === 401);
  await go(adm, 'resident');
  check('admin: cannot open the resident workspace', hash(adm) === '/login/resident');

  // ---------------- Public view after all of this ----------------
  await go(guest, 'track/' + photo.id, 1500);
  const pub = (await guest.textContent('#track-outcome')).toLowerCase();
  check('public tracking reveals no owner, email or claim code', !pub.includes('civiclens.demo') && !pub.includes('demo resident') && !pub.includes(photo.claim.toLowerCase()));
  const pubApi = await (await fetch(API + '/complaints/' + photo.id)).text();
  check('public API reveals no owner fields', !/owner|resident|civiclens\.demo/i.test(pubApi));

  const leaked = consoleLines.filter(l => l.includes(DEMO.password) || l.includes(photo.claim) || (tok && l.includes(tok)) || (stok && l.includes(stok)));
  check('no password, claim code or token in the browser console', leaked.length === 0, leaked.join(' | '));
  const errs = [guest, res, other, adm].flatMap(p => p.errors);
  check('no page errors in any journey', errs.length === 0, errs.join('; '));
  await browser.close();
  console.log(`\n${results.filter(r => r[1]).length}/${results.length} checks passed.`);
  process.exit(fails ? 1 : 0);
}
