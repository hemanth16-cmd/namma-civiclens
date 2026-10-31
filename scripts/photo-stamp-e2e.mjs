// Automatic address, photo stamping and saving, in headless Chromium with a synthetic camera.
// Photon is mocked with page.route (so request counts are exact and no network is needed), and
// the share sheet is stubbed for the phone journey. This does NOT replace testing on a real
// phone: real share sheets, Photos/Gallery targets and GPS hardware are not exercised here.
//
// Usage: BASE=http://localhost:4173 SHOTS=./screenshots node scripts/photo-stamp-e2e.mjs
import {createRequire} from 'node:module';
import {mkdirSync, readFileSync} from 'node:fs';
import path from 'node:path';

const BASE = process.env.BASE || 'http://localhost:4173';
const SHOTS = process.env.SHOTS || './screenshots';
mkdirSync(SHOTS, {recursive: true});
let pw;
try { pw = (await import('playwright')).chromium; } catch {
  pw = createRequire(import.meta.url)(process.env.PLAYWRIGHT_PATH || 'playwright').chromium;
}

const results = [];
const check = (name, ok, detail = '') => { results.push({name, ok: !!ok}); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ` — ${detail}` : ''}`); };
const errors = [];

const PLACES = [
  {lat: 13.1007, lng: 77.5963, name: 'Yelahanka New Town', locality: 'Yelahanka'},
  {lat: 13.0358, lng: 77.597, name: 'Hebbal Flyover', locality: 'Hebbal'},
  {lat: 13.0035, lng: 77.5692, name: 'Malleswaram 8th Cross', locality: 'Malleswaram'}
];
const nearest = (lat, lng) => PLACES.reduce((a, b) => (Math.hypot(b.lat - lat, b.lng - lng) < Math.hypot(a.lat - lat, a.lng - lng) ? b : a));

/** Photon stand-in with a request counter, adjustable delay and failure mode. */
function photonMock() {
  const mock = {requests: 0, delay: 0, fail: false};
  mock.handler = async route => {
    mock.requests += 1;
    const u = new URL(route.request().url());
    if (mock.delay) await new Promise(r => setTimeout(r, mock.delay));
    if (mock.fail) return route.fulfill({status: 503, body: 'unavailable'});
    const p = nearest(Number(u.searchParams.get('lat')), Number(u.searchParams.get('lon')));
    return route.fulfill({contentType: 'application/json', body: JSON.stringify({features: [{properties: {name: p.name, locality: p.locality, city: 'Bengaluru', state: 'Karnataka', postcode: '560064'}}]})});
  };
  return mock;
}

async function newPage({permissions = ['camera', 'geolocation'], geolocation = {latitude: 13.1007, longitude: 77.5963, accuracy: 9}, mobile = false, init = null} = {}) {
  const ctx = await browser.newContext({
    permissions, geolocation, acceptDownloads: true,
    viewport: mobile ? {width: 390, height: 844} : {width: 1280, height: 860},
    ...(mobile ? {isMobile: true, hasTouch: true, deviceScaleFactor: 2, userAgent: 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36'} : {})
  });
  if (init) await ctx.addInitScript(init);
  const photon = photonMock();
  await ctx.route('**/photon.komoot.io/**', photon.handler);
  const page = await ctx.newPage();
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|net::ERR/.test(m.text())) errors.push(m.text()); });
  return {ctx, page, photon};
}

const waitLive = page => page.waitForFunction(() => document.querySelector('#cam-chip-text')?.textContent === 'Live', null, {timeout: 15000}).then(() => true).catch(() => false);
const text = (page, sel) => page.locator(sel).innerText().catch(() => '');
const waitText = (page, sel, re, timeout = 8000) => page.waitForFunction(([s, src]) => new RegExp(src).test(document.querySelector(s)?.textContent || ''), [sel, re.source], {timeout}).then(() => true).catch(() => false);

async function fillAndSubmit(page, {stampLocation = true} = {}) {
  await page.waitForSelector('#review-form');
  await page.click('[data-category="Potholes"]');
  await page.check('#confirm-location');
  if (stampLocation) await page.check('#stamp-location');
  await page.click('#submit-btn');
}

/** Pixel probe of a JPEG file: dimensions and average colour of two regions. */
async function probe(page, file) {
  const b64 = readFileSync(file).toString('base64');
  return page.evaluate(async src => {
    const img = new Image();
    img.src = src;
    await img.decode();
    const c = document.createElement('canvas');
    c.width = img.naturalWidth; c.height = img.naturalHeight;
    const ctx = c.getContext('2d');
    ctx.drawImage(img, 0, 0);
    const avg = (x, y, w, h) => {
      const d = ctx.getImageData(x, y, w, h).data;
      let r = 0, g = 0, b = 0;
      for (let i = 0; i < d.length; i += 4) { r += d[i]; g += d[i + 1]; b += d[i + 2]; }
      const n = d.length / 4;
      return [r / n, g / n, b / n].map(Math.round);
    };
    const W = c.width, H = c.height, m = Math.round(Math.min(W, H) * 0.03);
    // Inside the panel, right of the text column, near its bottom edge; and the top of the scene.
    return {w: W, h: H, panel: avg(Math.round(W * 0.3), H - m - Math.round(H * 0.04), Math.round(W * 0.05), Math.round(H * 0.02)), scene: avg(Math.round(W * 0.45), Math.round(H * 0.25), Math.round(W * 0.1), Math.round(H * 0.05))};
  }, `data:image/jpeg;base64,${b64}`);
}

const browser = await pw.launch({args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream']});

// 1. Desktop: automatic address, caching, movement, capture before the address arrives, stamping and saving.
{
  const {ctx, page, photon} = await newPage();
  await page.goto(BASE + '/#/camera');
  check('Camera opens', await waitLive(page));
  const auto = await waitText(page, '#address-text', /Yelahanka/);
  check('Address resolves automatically, with no Search press', auto, await text(page, '#address-text'));
  check('Viewfinder card shows the short address under the coordinates', /Yelahanka, Bengaluru/.test(await text(page, '#vf-address')) && /13\.10070/.test(await text(page, '#vf-location-text')));
  check('Location phase reads Located', /^located$/i.test(await text(page, '#loc-phase-text')));
  check('Address card labels the address as approximate, separate from GPS coordinates', /approximate/i.test(await text(page, '#address-card')) && /13\.100700/.test(await text(page, '#loc-dl')));
  check('Navbar and viewfinder logos load from the central asset', await page.evaluate(() => [...document.querySelectorAll('.logo-mark img, .vf-logo, .chip-logo')].every(i => i.complete && i.naturalWidth > 0 && i.getAttribute('src') === 'assets/logo.svg')));
  await page.screenshot({path: path.join(SHOTS, 'photo-camera-address.png')});
  await page.waitForTimeout(6000); // two 5 s UI ticks
  check('No repeated lookups while standing still (one request)', photon.requests === 1, `requests=${photon.requests}`);

  await ctx.setGeolocation({latitude: 13.1007 + 10 / 111320, longitude: 77.5963, accuracy: 8});
  await page.click('#loc-retry');
  await page.waitForFunction(() => /13\.10079/.test(document.querySelector('#loc-dl')?.textContent || ''), null, {timeout: 8000}).catch(() => {});
  await page.waitForTimeout(1200);
  check('A 10 m move reuses the cached address (no new request)', photon.requests === 1 && /Yelahanka/.test(await text(page, '#address-text')), `requests=${photon.requests}`);

  await ctx.setGeolocation({latitude: 13.0358, longitude: 77.597, accuracy: 8});
  await page.click('#loc-retry');
  check('Address updates automatically when the coordinates change', await waitText(page, '#address-text', /Hebbal/), await text(page, '#address-text'));
  check('Exactly one request for the new place', photon.requests === 2, `requests=${photon.requests}`);

  // Capture before reverse geocoding finishes.
  photon.delay = 2500;
  await ctx.setGeolocation({latitude: 13.0035, longitude: 77.5692, accuracy: 8});
  await page.click('#loc-retry');
  await page.waitForFunction(() => /13\.00350/.test(document.querySelector('#vf-location-text')?.textContent || '') && document.querySelector('#capture-ready')?.dataset.ready === 'true', null, {timeout: 8000});
  const before = await text(page, '#address-text');
  const videoDims = await page.evaluate(() => [document.querySelector('#live').videoWidth, document.querySelector('#live').videoHeight]);
  await page.click('#shutter');
  await page.waitForFunction(() => location.hash === '#/review', null, {timeout: 8000}).catch(() => {});
  check('Photo captured while the address was still loading', /Finding/.test(before) && (await page.evaluate(() => location.hash)) === '#/review', before);
  check('Review shows the address once the in-flight lookup finishes', await waitText(page, '#review-address', /Malleswaram/, 8000), await text(page, '#review-address'));
  check('The review screen did not send a duplicate lookup', photon.requests === 3, `requests=${photon.requests}`);
  await page.waitForFunction(() => document.querySelector('#stamp-progress')?.hidden && document.querySelector('#review-photo')?.complete, null, {timeout: 8000}).catch(() => {});
  check('Location stamping is off until the reporter opts in', !(await page.isChecked('#stamp-location')));
  check('Stamp preview replaces the raw preview', await page.evaluate(() => document.querySelector('#review-photo').naturalWidth > 0));
  photon.delay = 0;
  await page.check('#stamp-location');
  await page.waitForTimeout(900);
  await page.screenshot({path: path.join(SHOTS, 'photo-review-stamp.png'), fullPage: false});

  const downloads = [];
  page.on('download', d => downloads.push(d));
  await fillAndSubmit(page, {stampLocation: false});
  await page.waitForFunction(() => location.hash.startsWith('#/receipt/NCL-'), null, {timeout: 15000});
  await page.waitForFunction(() => /Download started/.test(document.querySelector('#save-status')?.textContent || ''), null, {timeout: 8000}).catch(() => {});
  const id = (await text(page, '.reference-card .code-value')).trim();
  const claim = (await text(page, '.private-card .code-private')).trim();
  check('Stamped photo saved automatically after submit (desktop download)', downloads.length === 1, `downloads=${downloads.length}`);
  const name = downloads[0]?.suggestedFilename() || '';
  const expectedPrefix = id.replace(/-/g, '_') + '_';
  check('File name is NCL_YYYY_ID_YYYYMMDD_HHMMSS.jpg for this complaint', name.startsWith(expectedPrefix) && /^NCL_\d{4}_[A-Z0-9]{6}_\d{8}_\d{6}\.jpg$/.test(name), name);
  check('File name contains no claim code or address', !name.includes(claim) && !/Malleswaram/i.test(name));
  check('Save status says the download started (not that it was saved to Photos)', /Download started/.test(await text(page, '#save-status')) && !/saved to photos/i.test(await text(page, '#save-status')));
  const file = await downloads[0]?.path();
  const px = file ? await probe(page, file) : null;
  const navyish = px && px.panel[2] > px.panel[1] - 40 && px.panel[0] < 90 && px.panel[1] < 110;
  const greenScene = px && px.scene[1] > px.scene[0] + 40;
  check('Saved JPEG keeps the captured resolution', px && px.w >= videoDims[0] && px.h >= videoDims[1], px ? `${px.w}×${px.h} (preview ${videoDims.join('×')})` : 'no file');
  check('Saved JPEG pixels contain the stamp panel near the bottom', navyish, JSON.stringify(px?.panel));
  check('Scene above the panel is left uncovered', greenScene, JSON.stringify(px?.scene));
  check('Receipt shows the stamped photo and the evidence reference', (await page.locator('.receipt-media').count()) === 1 && /EV-[0-9A-F]{4}-[0-9A-F]{4}/.test(await text(page, 'main')));
  await page.screenshot({path: path.join(SHOTS, 'photo-receipt.png'), fullPage: true});

  // Re-rendering the receipt must not save again; the button saves on request.
  await page.goto(BASE + '/#/home');
  await page.goto(BASE + `/#/receipt/${id}`);
  await page.waitForSelector('[data-evidence]');
  await page.waitForTimeout(1500);
  check('Returning to the receipt does not trigger another automatic save', downloads.length === 1, `downloads=${downloads.length}`);
  const [manual] = await Promise.all([page.waitForEvent('download', {timeout: 8000}).catch(() => null), page.click('[data-evidence]')]);
  check('Save image button downloads the same stamped file name', !!manual && manual.suggestedFilename() === name, manual?.suggestedFilename());
  check('Track page still works for the new complaint', await (async () => { await page.goto(BASE + `/#/track/${id}`); await page.waitForTimeout(500); return /Submitted/.test(await text(page, 'main')); })());
  await ctx.close();
}

// 2. Address lookup failure, then Retry.
{
  const {ctx, page, photon} = await newPage();
  photon.fail = true;
  await page.goto(BASE + '/#/camera');
  await waitLive(page);
  const failed = await page.waitForFunction(() => document.querySelector('#address-card')?.dataset.state === 'failed', null, {timeout: 8000}).then(() => true).catch(() => false);
  check('Lookup failure is shown with a helpful message; coordinates are kept', failed && /coordinates are still saved/i.test(await text(page, '#address-status')) && /13\.1007/.test(await text(page, '#loc-dl')));
  photon.fail = false;
  await page.click('[data-action="lookup-live"]');
  check('Retry address resolves it', await waitText(page, '#address-text', /Yelahanka/));
  await ctx.close();
}

// 3. Offline, then back online.
{
  const {ctx, page, photon} = await newPage({permissions: ['camera']});
  await page.goto(BASE + '/#/camera');
  await waitLive(page);
  await ctx.setOffline(true);
  await ctx.grantPermissions(['camera', 'geolocation']);
  await page.click('#loc-retry');
  const offline = await page.waitForFunction(() => document.querySelector('#address-card')?.dataset.state === 'offline', null, {timeout: 8000}).then(() => true).catch(() => false);
  check('Offline: address card explains it will look up on reconnect', offline && /offline/i.test(await text(page, '#address-status')), await text(page, '#address-status'));
  check('Offline: no geocoding request was attempted', photon.requests === 0);
  await page.screenshot({path: path.join(SHOTS, 'photo-offline.png')});
  await ctx.setOffline(false);
  check('Back online: the address resolves without any tap', await waitText(page, '#address-text', /Yelahanka/, 8000));
  await ctx.close();
}

// 4. Location blocked.
{
  const {ctx, page} = await newPage({permissions: ['camera'], geolocation: undefined});
  await page.goto(BASE + '/#/camera');
  await waitLive(page);
  await page.waitForFunction(() => document.querySelector('#loc-badge')?.textContent === 'Blocked', null, {timeout: 10000}).catch(() => {});
  check('Location blocked: phase reads Needs attention', /^needs attention$/i.test(await text(page, '#loc-phase-text')), await text(page, '#loc-badge'));
  check('Location blocked: address card waits instead of guessing', /Appears automatically/.test(await text(page, '#address-text')));
  await ctx.close();
}

// 5. Phone: share sheet path (Android Chrome / iOS Safari behaviour stubbed).
const SHARE_STUB = () => {
  window.__shares = [];
  window.__shareMode = 'ok';
  navigator.canShare = data => !!data?.files?.length;
  navigator.share = async data => {
    window.__shares.push(data.files.map(f => ({name: f.name, type: f.type, size: f.size})));
    if (window.__shareMode === 'blocked') { window.__shareMode = 'ok'; throw new DOMException('needs gesture', 'NotAllowedError'); }
  };
};
{
  const {ctx, page} = await newPage({mobile: true, init: SHARE_STUB});
  await page.goto(BASE + '/#/camera');
  check('Phone: camera opens', await waitLive(page));
  await waitText(page, '#address-text', /Yelahanka/);
  check('Phone: no horizontal scroll on the camera', await page.evaluate(() => document.documentElement.scrollWidth - innerWidth) <= 0);
  await page.screenshot({path: path.join(SHOTS, 'photo-mobile-camera.png')});
  await page.click('#shutter');
  await page.waitForFunction(() => location.hash === '#/review', null, {timeout: 8000});
  await page.waitForFunction(() => document.querySelector('#stamp-progress')?.hidden, null, {timeout: 8000}).catch(() => {});
  await page.screenshot({path: path.join(SHOTS, 'photo-mobile-review.png')});
  await fillAndSubmit(page);
  await page.waitForFunction(() => location.hash.startsWith('#/receipt/NCL-'), null, {timeout: 15000});
  await page.waitForFunction(() => /Share sheet completed/.test(document.querySelector('#save-status')?.textContent || ''), null, {timeout: 8000}).catch(() => {});
  const shares = await page.evaluate(() => window.__shares);
  check('Phone: share sheet opened once, automatically, with one JPEG file', shares.length === 1 && shares[0].length === 1 && shares[0][0].type === 'image/jpeg' && /^NCL_\d{4}_[A-Z0-9]{6}_\d{8}_\d{6}\.jpg$/.test(shares[0][0].name), JSON.stringify(shares));
  check('Phone: button reads Save to Photos and the status does not over-claim', /Save to Photos/.test(await text(page, '[data-evidence]')) && /Share sheet completed/.test(await text(page, '#save-status')));
  check('Phone: no horizontal scroll on the receipt', await page.evaluate(() => document.documentElement.scrollWidth - innerWidth) <= 0);
  await page.screenshot({path: path.join(SHOTS, 'photo-mobile-receipt.png'), fullPage: true});

  // When the browser refuses an automatic share (no recent tap), the user gets a clear button.
  await page.goto(BASE + '/#/camera');
  await waitLive(page);
  await page.evaluate(() => { window.__shareMode = 'blocked'; });
  await page.waitForFunction(() => document.querySelector('#capture-ready')?.dataset.ready === 'true', null, {timeout: 8000});
  await page.click('#shutter');
  await page.waitForFunction(() => location.hash === '#/review', null, {timeout: 8000});
  await fillAndSubmit(page);
  await page.waitForFunction(() => location.hash.startsWith('#/receipt/NCL-'), null, {timeout: 15000});
  const blocked = await waitText(page, '#save-status', /needs a tap/);
  check('Phone: a refused automatic share asks for a tap instead of claiming success', blocked, await text(page, '#save-status'));
  await page.click('[data-evidence]');
  check('Phone: tapping Save to Photos opens the share sheet', await waitText(page, '#save-status', /Share sheet completed/));
  await ctx.close();
}

// 6. Logo, favicon and install manifest.
{
  const {ctx, page} = await newPage({permissions: []});
  const res = await page.goto(BASE + '/');
  check('Entry document loads', res?.ok());
  const manifestHref = await page.getAttribute('link[rel="manifest"]', 'href');
  const manifest = await page.evaluate(async href => (await fetch(href)).json(), manifestHref);
  const icons = await page.evaluate(async list => Promise.all(list.map(async i => { const r = await fetch(i.src); const b = await r.blob(); const bmp = await createImageBitmap(b); return [r.ok, `${bmp.width}x${bmp.height}`, i.sizes]; })), manifest.icons);
  check('Manifest lists 192, 512 and maskable icons that load at their stated sizes', icons.length === 3 && icons.every(([ok, real, stated]) => ok && real === stated), JSON.stringify(icons));
  const favicon = await page.evaluate(async () => Promise.all([...document.querySelectorAll('link[rel~="icon"], link[rel="apple-touch-icon"]')].map(async l => (await fetch(l.href)).ok)));
  check('Favicon and Apple touch icon load', favicon.length === 3 && favicon.every(Boolean));
  await page.goto(BASE + '/#/home');
  await page.waitForTimeout(400);
  check('Entry screen shows the logo', await page.evaluate(() => { const i = document.querySelector('.logo-mark img'); return !!i && i.naturalWidth > 0; }));
  await ctx.close();
}

check('No page errors or console errors', errors.length === 0, errors.slice(0, 3).join(' | '));
await browser.close();
const passed = results.filter(r => r.ok).length;
console.log(`\n${passed}/${results.length} checks passed.`);
process.exitCode = passed === results.length ? 0 : 1;
