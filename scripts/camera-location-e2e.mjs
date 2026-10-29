// Camera and location journeys in headless Chromium with a synthetic camera device.
// This exercises the real browser APIs (getUserMedia, MediaStream, permissions, <video>), but the
// camera is Chromium's fake device and location comes from Playwright or an injected fake. It does
// NOT replace testing on a real phone camera and GPS receiver.
//
// Usage: BASE=http://localhost:4173 [INSECURE_BASE=http://<lan-ip>:4173] SHOTS=./screenshots node scripts/camera-location-e2e.mjs
import {createRequire} from 'node:module';
import {mkdirSync} from 'node:fs';
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
const GEO = {latitude: 13.0358, longitude: 77.597, accuracy: 12};

// Counts getUserMedia calls and live tracks so duplicate or orphaned streams are visible.
const INSTRUMENT = () => {
  const md = navigator.mediaDevices;
  if (!md?.getUserMedia) return;
  window.__gum = {calls: 0, streams: [], requested: []};
  const original = md.getUserMedia.bind(md);
  md.getUserMedia = async constraints => {
    window.__gum.calls += 1;
    const requested = constraints?.video?.deviceId?.exact || null;
    window.__gum.requested.push(requested);
    const useFakePair = window.__twoCameras && requested;
    const stream = await original(useFakePair ? {video: true, audio: false} : constraints);
    if (useFakePair) stream.getVideoTracks().forEach(t => { t.__deviceId = requested; });
    window.__gum.streams.push(stream);
    return stream;
  };
  window.__liveTracks = () => window.__gum.streams.flatMap(s => s.getTracks()).filter(t => t.readyState === 'live').length;
  if (window.__twoCameras) {
    md.enumerateDevices = async () => [
      {kind: 'videoinput', deviceId: 'cam-back', label: 'Back camera', groupId: 'g1'},
      {kind: 'videoinput', deviceId: 'cam-front', label: 'Front camera', groupId: 'g2'}
    ];
    const getSettings = MediaStreamTrack.prototype.getSettings;
    MediaStreamTrack.prototype.getSettings = function () {
      const s = getSettings.call(this);
      return this.__deviceId ? {...s, deviceId: this.__deviceId, facingMode: this.__deviceId === 'cam-front' ? 'user' : 'environment'} : {...s, deviceId: 'cam-back'};
    };
  }
};

// Scriptable geolocation: 'silent' never answers, 'poor' answers ±350 m then ±11 m, 'ok' answers ±9 m.
const FAKE_GEO = () => {
  window.__geoMode = window.__geoMode || 'silent';
  const watches = new Map();
  let id = 0;
  const emit = (fn, accuracy, lat = 13.0412, lng = 77.6025) => fn({coords: {latitude: lat, longitude: lng, accuracy}, timestamp: Date.now()});
  const fake = {
    watchPosition(ok) {
      const i = ++id;
      watches.set(i, true);
      const mode = window.__geoMode;
      if (mode === 'ok') setTimeout(() => watches.has(i) && emit(ok, 9), 300);
      if (mode === 'poor') {
        setTimeout(() => watches.has(i) && emit(ok, 350), 300);
        setTimeout(() => watches.has(i) && emit(ok, 11, 13.0415, 77.6029), 2500);
      }
      return i;
    },
    clearWatch(i) { watches.delete(i); window.__geoCleared = (window.__geoCleared || 0) + 1; },
    getCurrentPosition() {}
  };
  window.__activeWatches = () => watches.size;
  Object.defineProperty(navigator, 'geolocation', {value: fake, configurable: true});
};

// Headless Chromium cannot show a real permission prompt (getUserMedia reports NotSupportedError
// without the fake-UI flag), so the camera prompt is auto-accepted and a camera denial is simulated
// by rejecting with the browser's NotAllowedError. Location denial uses the real permission system.
const DENY_CAMERA = () => {
  const md = navigator.mediaDevices;
  const original = md.getUserMedia.bind(md);
  md.getUserMedia = c => (window.__allowCamera ? original(c) : Promise.reject(new DOMException('Permission denied', 'NotAllowedError')));
};
const launchArgs = ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream', '--enable-unsafe-swiftshader'];
const browser = await pw.launch({args: launchArgs});
const status = page => page.locator('#cam-chip-text').innerText();
const waitLive = page => page.waitForFunction(() => document.querySelector('#cam-chip-text')?.textContent === 'Live', null, {timeout: 10000}).then(() => true, () => false);
const previewVisible = page => page.evaluate(() => {
  const r = document.querySelector('#viewfinder')?.getBoundingClientRect();
  if (!r) return false;
  const v = document.querySelector('#live');
  return document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2) === v && v.readyState >= 2 && v.videoWidth > 0 && !v.paused;
});
async function newPage(opts = {}, scripts = [INSTRUMENT]) {
  const ctx = await browser.newContext({viewport: {width: 1280, height: 860}, ...opts});
  for (const s of scripts) await ctx.addInitScript(s);
  const page = await ctx.newPage();
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|net::ERR/.test(m.text())) errors.push(m.text()); });
  return {ctx, page};
}

// 1. First visit with permissions already granted.
{
  const {ctx, page} = await newPage({permissions: ['camera', 'geolocation'], geolocation: GEO});
  await page.goto(BASE + '/#/camera');
  const live = await waitLive(page);
  check('Granted: camera starts automatically on opening the screen', live, await status(page));
  check('Granted: the live preview is the visible top layer with decoded frames', await previewVisible(page));
  check('Granted: exactly one camera request and one live track', (await page.evaluate(() => [window.__gum.calls, window.__liveTracks()])).join() === '1,1');
  await page.waitForFunction(() => /±\d+ m/.test(document.querySelector('#vf-location-text')?.textContent || ''), null, {timeout: 8000}).catch(() => {});
  const locText = await page.locator('#loc-dl').innerText();
  check('Granted: location shows coordinates, accuracy in metres and fix time', /13\.0358/.test(locText) && /±12 m/.test(locText) && /Fix obtained/i.test(locText), (await page.locator('#loc-badge').innerText()));
  check('Granted: shutter reports ready with the fix it will use', /^Ready\. Evidence will be tagged/.test(await page.locator('#capture-ready').innerText()));
  await page.screenshot({path: path.join(SHOTS, 'cam-desktop-live.png')});

  // 3. Leave and reopen.
  await page.goto(BASE + '/#/home');
  await page.waitForTimeout(400);
  check('Leaving the camera screen stops every camera track', (await page.evaluate(() => window.__liveTracks())) === 0);
  await page.goto(BASE + '/#/camera');
  check('Reopening restarts the preview without a new permission prompt', await waitLive(page) && await previewVisible(page));
  check('Reopening leaves exactly one live track (no duplicate streams)', (await page.evaluate(() => window.__liveTracks())) === 1);
  // Rapid leave while a request is in flight.
  await page.goto(BASE + '/#/home');
  await page.goto(BASE + '/#/camera');
  await page.evaluate(() => { location.hash = '#/home'; });
  await page.waitForTimeout(1500);
  check('Leaving mid-start does not leave an orphaned camera running', (await page.evaluate(() => window.__liveTracks())) === 0);
  await page.goto(BASE + '/#/camera');
  await waitLive(page);

  // 5. Actual photo capture.
  await page.click('#shutter');
  await page.waitForFunction(() => location.hash === '#/review', null, {timeout: 5000}).catch(() => {});
  check('Shutter captures a real frame and opens review', (await page.evaluate(() => location.hash)) === '#/review');
  const img = await page.evaluate(async () => { const i = document.querySelector('.review-media img'); await i?.decode?.(); return i ? [i.naturalWidth, i.naturalHeight] : [0, 0]; });
  check('Captured photo decodes with real dimensions', img[0] > 0 && img[1] > 0, img.join('×'));
  const reviewLoc = await page.locator('#location-block').innerText();
  check('Review shows source, accuracy and fix time for the captured photo', /Device GPS at capture/.test(reviewLoc) && /±12 m/.test(reviewLoc) && /before capture/.test(reviewLoc));
  check('Preview caption shows the evidence location', /13\.03580.*device GPS/.test(await page.locator('.media-geo').innerText()));
  check('Camera is released while reviewing', (await page.evaluate(() => window.__liveTracks())) === 0);
  await page.screenshot({path: path.join(SHOTS, 'cam-review.png')});
  await ctx.close();
}

// Back navigation from the camera when the user arrived from another screen.
{
  const {ctx, page} = await newPage({permissions: ['camera', 'geolocation'], geolocation: GEO});
  await page.goto(BASE + '/#/home');
  await page.waitForSelector('.topbar a[href="#/camera"]');
  await page.click('.topbar a[href="#/camera"]');
  await waitLive(page);
  check('Back control appears on the camera when there is somewhere to go back to', (await page.locator('.topbar [data-action="back"]').count()) === 1);
  await page.click('.topbar [data-action="back"]');
  await page.waitForTimeout(500);
  check('Back returns to the previous screen and releases the camera', (await page.evaluate(() => location.hash)) === '#/home' && (await page.evaluate(() => window.__liveTracks())) === 0);
  await ctx.close();
}

// 2. Permission denied, then allowed and retried.
{
  const {ctx, page} = await newPage({permissions: [], geolocation: GEO}, [DENY_CAMERA, INSTRUMENT]);
  await page.goto(BASE + '/#/camera');
  await page.waitForTimeout(1500);
  const denied = await page.locator('#cam-state').innerText();
  check('Denied (simulated NotAllowedError): clear blocked message with Retry and gallery fallback', /blocked/i.test(denied) && /Retry camera/.test(denied) && /geo-tagged JPEG/.test(denied), denied.split('\n')[0]);
  check('Denied (real browser permission): location reports blocked separately', /Blocked/.test(await page.locator('#loc-badge').innerText()));
  const callsBefore = await page.evaluate(() => window.__gum.calls);
  await page.evaluate(() => { location.hash = '#/home'; });
  await page.waitForTimeout(200);
  await page.evaluate(() => { location.hash = '#/camera'; });
  await page.waitForTimeout(800);
  check('Denied: reopening does not request permission again automatically', (await page.evaluate(() => window.__gum.calls)) === callsBefore);
  await page.screenshot({path: path.join(SHOTS, 'cam-denied.png')});
  await ctx.grantPermissions(['camera', 'geolocation']);
  await page.evaluate(() => { window.__allowCamera = true; });
  await page.click('#cam-state [data-action="enable-camera"]');
  check('Denied → allowed: Retry camera brings the preview up', await waitLive(page) && await previewVisible(page));
  await page.click('#loc-retry');
  await page.waitForFunction(() => /Location acquired/.test(document.querySelector('#loc-badge')?.textContent || ''), null, {timeout: 8000}).catch(() => {});
  check('Denied → allowed: Retry location acquires a fix', /Location acquired/.test(await page.locator('#loc-badge').innerText()));
  await ctx.close();
}

// 4. Camera switching with two (simulated) cameras.
{
  const {ctx, page} = await newPage({permissions: ['camera', 'geolocation'], geolocation: GEO}, [() => { window.__twoCameras = true; }, INSTRUMENT]);
  await page.goto(BASE + '/#/camera');
  await waitLive(page);
  const sw = page.locator('#switch-btn');
  check('Switch is enabled when two cameras are reported', !(await sw.isDisabled()), await sw.getAttribute('aria-label'));
  await sw.click();
  await waitLive(page);
  const after = await page.evaluate(() => ({req: window.__gum.requested.at(-1), live: window.__liveTracks(), mirrored: document.querySelector('#live').classList.contains('is-mirrored')}));
  check('Switching requests the other camera and keeps one live track', after.req === 'cam-front' && after.live === 1, JSON.stringify(after));
  check('Front camera preview is mirrored for natural framing', after.mirrored);
  check('Preview is visible after switching', await previewVisible(page));
  await sw.click();
  await waitLive(page);
  check('Switching again returns to the back camera', (await page.evaluate(() => window.__gum.requested.at(-1))) === 'cam-back');
  await ctx.close();
}
{
  const {ctx, page} = await newPage({permissions: ['camera', 'geolocation'], geolocation: GEO});
  await page.goto(BASE + '/#/camera');
  await waitLive(page);
  check('Switch is disabled and explained when only one camera exists', await page.locator('#switch-btn').isDisabled() && /Only one camera/.test(await page.locator('#switch-btn').getAttribute('aria-label')));
  await ctx.close();
}

// Video mode: tap to start, tap to stop.
{
  const {ctx, page} = await newPage({permissions: ['camera', 'geolocation'], geolocation: GEO});
  await page.goto(BASE + '/#/camera');
  await waitLive(page);
  await page.waitForFunction(() => document.querySelector('#capture-ready')?.dataset.ready === 'true', null, {timeout: 8000}).catch(() => {});
  await page.click('[data-mode="video"]');
  await page.click('#shutter');
  await page.waitForTimeout(2300);
  check('Video mode shows the recording timer', /Recording [12]s of 30s/.test(await page.locator('#cam-chip-text').innerText()));
  await page.click('#shutter');
  await page.waitForFunction(() => location.hash === '#/review', null, {timeout: 6000}).catch(() => {});
  check('Stopping a recording opens review with a playable clip and its location', (await page.locator('.review-media video').count()) === 1 && /device GPS/.test(await page.locator('.media-geo').innerText()));
  await ctx.close();
}

// 6. Location timeout, then retry.
{
  const {ctx, page} = await newPage({permissions: ['camera', 'geolocation']}, [INSTRUMENT, FAKE_GEO]);
  await page.goto(BASE + '/#/camera');
  await waitLive(page);
  check('Locating state is shown while waiting for a fix', /Locating/.test(await page.locator('#loc-badge').innerText()));
  await page.waitForFunction(() => /Timed out/.test(document.querySelector('#loc-badge')?.textContent || ''), null, {timeout: 25000}).catch(() => {});
  check('Timeout: reported as timed out after 20 s, with no coordinates invented', /Timed out/.test(await page.locator('#loc-badge').innerText()) && /—/.test(await page.locator('#loc-dl').innerText()));
  check('Timeout: watcher is cleared', (await page.evaluate(() => window.__activeWatches())) === 0);
  check('Timeout: shutter explains that a location fix is needed', /location fix is needed/i.test(await page.locator('#capture-ready').innerText()));
  await page.click('#shutter', {force: true});
  await page.waitForTimeout(300);
  check('Timeout: pressing the shutter does not capture without location', (await page.evaluate(() => location.hash)) === '#/camera');
  await page.screenshot({path: path.join(SHOTS, 'cam-location-timeout.png')});
  await page.evaluate(() => { window.__geoMode = 'ok'; });
  await page.click('#loc-retry');
  await page.waitForFunction(() => /Location acquired/.test(document.querySelector('#loc-badge')?.textContent || ''), null, {timeout: 5000}).catch(() => {});
  check('Timeout → Retry location acquires a fresh fix', /Location acquired/.test(await page.locator('#loc-badge').innerText()));
  await ctx.close();
}

// 7. Poor first fix, refined; address lookup failure keeps coordinates.
{
  const {ctx, page} = await newPage({permissions: ['camera', 'geolocation']}, [() => { window.__geoMode = 'poor'; }, INSTRUMENT, FAKE_GEO]);
  await page.route('**/photon.komoot.io/**', r => r.abort());
  await page.goto(BASE + '/#/camera');
  await page.waitForFunction(() => /Improving accuracy/.test(document.querySelector('#loc-badge')?.textContent || ''), null, {timeout: 5000}).catch(() => {});
  check('Poor fix: shown immediately with ±350 m while accuracy improves', /Improving accuracy/.test(await page.locator('#loc-badge').innerText()) && /±350 m/.test(await page.locator('#loc-dl').innerText()));
  await page.screenshot({path: path.join(SHOTS, 'cam-location-refining.png')});
  await page.waitForFunction(() => /Location acquired/.test(document.querySelector('#loc-badge')?.textContent || ''), null, {timeout: 6000}).catch(() => {});
  check('Poor fix: refined to the better ±11 m reading and the watcher stopped', /±11 m/.test(await page.locator('#loc-dl').innerText()) && (await page.evaluate(() => window.__activeWatches())) === 0);
  await page.click('[data-action="lookup-live"]');
  await page.waitForFunction(() => { const t = document.querySelector('#address-status')?.textContent || ''; return t && !t.startsWith('Looking up'); }, null, {timeout: 12000}).catch(() => {});
  const addr = await page.locator('#address-status').innerText();
  check('Address lookup failure is explained and coordinates are kept', /coordinates are still saved/i.test(addr) && /13\.0415/.test(await page.locator('#loc-dl').innerText()), addr);
  await ctx.close();
}

// 8. Insecure origin (plain HTTP on a network address).
if (process.env.INSECURE_BASE) {
  const {ctx, page} = await newPage({permissions: ['camera', 'geolocation'], geolocation: GEO});
  await page.goto(process.env.INSECURE_BASE + '/#/camera');
  await page.waitForTimeout(1200);
  const cam = await page.locator('#cam-state').innerText();
  check('Insecure HTTP: camera explains HTTPS/localhost instead of failing silently', /secure page/i.test(cam) && /localhost/.test(cam));
  check('Insecure HTTP: location reports Needs HTTPS', /Needs HTTPS/.test(await page.locator('#loc-badge').innerText()));
  check('Insecure HTTP: no camera request is attempted', (await page.evaluate(() => window.__gum?.calls ?? 0)) === 0);
  await page.screenshot({path: path.join(SHOTS, 'cam-insecure.png')});
  await ctx.close();
}

// 9. Mobile layouts with the camera live.
for (const [w, h] of [[375, 812], [320, 640]]) {
  const {ctx, page} = await newPage({viewport: {width: w, height: h}, isMobile: true, hasTouch: true, permissions: ['camera', 'geolocation'], geolocation: GEO});
  await page.goto(BASE + '/#/camera');
  await waitLive(page);
  const m = await page.evaluate(() => {
    const box = id => document.getElementById(id)?.getBoundingClientRect();
    const sh = box('shutter'); const sw = box('switch-btn');
    return {overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth, shutterBottom: Math.round(sh.bottom), shutterSize: Math.round(sh.width), switchSize: Math.round(sw.width), h: innerHeight};
  });
  check(`${w}px: preview live and visible`, await previewVisible(page));
  check(`${w}px: no horizontal scroll`, m.overflow === 0, `${m.overflow}px`);
  check(`${w}px: shutter on screen and touch targets ≥44 px`, m.shutterBottom <= m.h && m.shutterSize >= 44 && m.switchSize >= 44, JSON.stringify(m));
  await page.screenshot({path: path.join(SHOTS, `cam-mobile-${w}.png`)});
  await ctx.close();
}

check('No page errors during camera and location checks', errors.length === 0, errors.slice(0, 3).join(' | '));
await browser.close();
const failed = results.filter(r => !r.ok).length;
console.log(`\n${results.length - failed}/${results.length} checks passed.`);
process.exit(failed ? 1 : 0);
