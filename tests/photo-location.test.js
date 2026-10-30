import test from 'node:test';
import assert from 'node:assert/strict';
import {createAddressResolver, distanceM, ADDR} from '../dist/address-service.js';
import {stampRows, stampFileName, stampLayout, stampPanelHeight, formatLat, formatLng, formatIST, evidenceRef} from '../dist/photo-stamp.js';
import {preferredMethod, savePhoto, autoSavePhoto} from '../dist/photo-save.js';

// ---------- Fakes ----------
function fakeTimers() {
  let now = 0, id = 0;
  const queue = new Map();
  return {
    now: () => now,
    timers: {
      setTimeout(fn, ms) { queue.set(++id, {fn, at: now + ms}); return id; },
      clearTimeout(t) { queue.delete(t); }
    },
    advance(ms) {
      now += ms;
      for (const [k, t] of [...queue]) if (t.at <= now) { queue.delete(k); t.fn(); }
    }
  };
}
const tick = () => new Promise(r => setImmediate(r));
const YELAHANKA = {lat: 13.1007, lng: 77.5963};
const offset = (p, metresNorth) => ({lat: p.lat + metresNorth / 111320, lng: p.lng});

function fakeLookup({results = [], hold = false} = {}) {
  const calls = [];
  const pending = [];
  const lookup = (point, {signal} = {}) => {
    calls.push({point, signal});
    const next = results.shift() ?? {ok: true, address: `Address for ${point.lat.toFixed(4)}`, neighbourhood: 'Yelahanka', short: 'Yelahanka, Bengaluru'};
    if (!hold) return Promise.resolve(next);
    return new Promise(resolve => pending.push(() => resolve(next)));
  };
  return {lookup, calls, release: () => pending.splice(0).forEach(fn => fn())};
}

// ---------- Address resolver ----------
test('distance helper is accurate to a metre over short distances', () => {
  assert.ok(Math.abs(distanceM(YELAHANKA, offset(YELAHANKA, 100)) - 100) < 1);
});

test('address resolves automatically after the debounce, without any button', async () => {
  const t = fakeTimers();
  const f = fakeLookup();
  const r = createAddressResolver({lookup: f.lookup, timers: t.timers, now: t.now});
  r.resolve(YELAHANKA);
  assert.equal(r.state.status, 'resolving', 'progress shows immediately');
  assert.equal(f.calls.length, 0, 'no request before the fix settles');
  t.advance(ADDR.DEBOUNCE_MS);
  await tick();
  assert.equal(f.calls.length, 1);
  assert.equal(r.state.status, 'resolved');
  assert.match(r.state.address, /Address for 13\.1007/);
});

test('a burst of refined fixes produces one request (debounce)', async () => {
  const t = fakeTimers();
  const f = fakeLookup();
  const r = createAddressResolver({lookup: f.lookup, timers: t.timers, now: t.now});
  for (let m = 0; m < 5; m++) { r.resolve(offset(YELAHANKA, m * 3)); t.advance(200); }
  t.advance(ADDR.DEBOUNCE_MS);
  await tick();
  assert.equal(f.calls.length, 1);
});

test('nearby fixes reuse the cached address; a real move triggers a new lookup', async () => {
  const t = fakeTimers();
  const f = fakeLookup();
  const r = createAddressResolver({lookup: f.lookup, timers: t.timers, now: t.now});
  r.resolve(YELAHANKA, {immediate: true});
  await tick();
  r.resolve(offset(YELAHANKA, 12));
  r.resolve(offset(YELAHANKA, 20), {immediate: true});
  await tick();
  assert.equal(f.calls.length, 1, 'within the reuse radius');
  assert.equal(r.state.status, 'resolved');
  assert.ok(r.addressFor(offset(YELAHANKA, 25)));
  r.resolve(offset(YELAHANKA, 400), {immediate: true});
  await tick();
  assert.equal(f.calls.length, 2, 'moved 400 m');
  assert.match(r.state.address, /13\.1043/);
});

test('repeated calls for the same spot while a request is in flight do not duplicate it', async () => {
  const t = fakeTimers();
  const f = fakeLookup({hold: true});
  const r = createAddressResolver({lookup: f.lookup, timers: t.timers, now: t.now});
  r.resolve(YELAHANKA, {immediate: true});
  r.resolve(YELAHANKA, {immediate: true});
  r.resolve(offset(YELAHANKA, 5));
  t.advance(5000);
  assert.equal(f.calls.length, 1);
  f.release();
  await tick();
  assert.equal(r.state.status, 'resolved');
});

test('a stale in-flight lookup is cancelled and cannot overwrite a newer position', async () => {
  const t = fakeTimers();
  const f = fakeLookup({hold: true, results: [{ok: true, address: 'OLD place'}, {ok: true, address: 'NEW place'}]});
  const r = createAddressResolver({lookup: f.lookup, timers: t.timers, now: t.now});
  r.resolve(YELAHANKA, {immediate: true});
  r.resolve(offset(YELAHANKA, 800), {immediate: true});
  assert.equal(f.calls[0].signal.aborted, true, 'first request aborted');
  f.release();
  await tick();
  assert.equal(r.state.address, 'NEW place');
  assert.equal(r.addressFor(YELAHANKA), null, 'the stale answer is not cached');
});

test('failure is reported with the message, backs off, and Retry forces a new request', async () => {
  const t = fakeTimers();
  const f = fakeLookup({results: [{ok: false, message: 'Address lookup is unavailable right now. Your GPS coordinates are still saved.'}]});
  const r = createAddressResolver({lookup: f.lookup, timers: t.timers, now: t.now});
  r.resolve(YELAHANKA, {immediate: true});
  await tick();
  assert.equal(r.state.status, 'failed');
  assert.match(r.state.message, /coordinates are still saved/);
  r.resolve(YELAHANKA); t.advance(ADDR.DEBOUNCE_MS); await tick();
  assert.equal(f.calls.length, 1, 'automatic repeats wait for the back-off');
  r.retry();
  await tick();
  assert.equal(f.calls.length, 2);
  assert.equal(r.state.status, 'resolved');
});

test('offline: no request is sent, and coming back online resolves automatically', async () => {
  const t = fakeTimers();
  const f = fakeLookup();
  let online = false;
  const r = createAddressResolver({lookup: f.lookup, timers: t.timers, now: t.now, isOnline: () => online});
  r.resolve(YELAHANKA, {immediate: true});
  await tick();
  assert.equal(r.state.status, 'offline');
  assert.equal(f.calls.length, 0);
  online = true;
  r.online();
  await tick();
  assert.equal(r.state.status, 'resolved');
});

test('whenSettled waits for an in-flight lookup (photo taken before the address arrived), bounded', async () => {
  const t = fakeTimers();
  const f = fakeLookup({hold: true});
  const r = createAddressResolver({lookup: f.lookup, timers: t.timers, now: t.now});
  r.resolve(YELAHANKA, {immediate: true});
  const waiting = r.whenSettled(offset(YELAHANKA, 4), 3000);
  f.release();
  await tick();
  assert.match((await waiting).address, /13\.1007/);
  // Nothing in flight elsewhere: resolves to null at once.
  assert.equal(await r.whenSettled(offset(YELAHANKA, 5000)), null);
  // Bounded wait.
  r.resolve(offset(YELAHANKA, 9000), {immediate: true});
  const slow = r.whenSettled(offset(YELAHANKA, 9000), 3000);
  t.advance(3000);
  assert.equal(await slow, null);
});

// ---------- Stamp content and privacy ----------
const GPS = {lat: 13.1007, lng: 77.5963, accuracy: 12, sourceType: 'gps'};

test('stamp rows: real complaint ID, evidence ref, address, coordinates and IST time when location is allowed', () => {
  const rows = stampRows({complaintId: 'NCL-2026-ABC123', evidenceRef: 'EV-3F9A-21C0', includeLocation: true, gps: GPS, address: 'Yelahanka, Bengaluru, Karnataka', capturedAt: '2026-10-30T07:15:00Z'});
  const text = rows.map(r => `${r.label}: ${r.value}`).join('\n');
  assert.match(text, /Complaint ID: NCL-2026-ABC123/);
  assert.match(text, /Evidence ref: EV-3F9A-21C0/);
  assert.match(text, /Location: Yelahanka, Bengaluru, Karnataka/);
  assert.match(text, /Latitude: 13\.100700° N/);
  assert.match(text, /Longitude: 77\.596300° E .*±12 m · device GPS/);
  assert.match(text, /Captured: 30 Oct 2026 \| 12:45 PM IST/);
});

test('stamp rows: no address or coordinates without the reporter’s consent', () => {
  const text = JSON.stringify(stampRows({complaintId: 'NCL-2026-ABC123', includeLocation: false, gps: GPS, address: 'Yelahanka', capturedAt: '2026-10-30T07:15:00Z'}));
  assert.doesNotMatch(text, /13\.10|77\.59|Yelahanka/);
  assert.match(text, /Not shown on this copy/);
});

test('stamp rows: before submission the ID row says it is pending, never a made-up ID', () => {
  const [first] = stampRows({complaintId: null, gps: GPS});
  assert.equal(first.value, 'Assigned when you submit');
  assert.doesNotMatch(JSON.stringify(stampRows({gps: GPS})), /NCL-/);
});

test('stamp rows never accept a claim code', () => {
  const text = JSON.stringify(stampRows({complaintId: 'NCL-2026-ABC123', claim: 'secret-claim-uuid', includeLocation: true, gps: GPS}));
  assert.doesNotMatch(text, /secret-claim-uuid/);
});

test('file name uses the public ID and IST capture time only', () => {
  assert.equal(stampFileName('NCL-2026-ABC123', '2026-10-30T07:15:00Z'), 'NCL_2026_ABC123_20261030_124500.jpg');
  assert.equal(stampFileName('NCL-2026-ABC123', '2026-10-30T18:45:09Z'), 'NCL_2026_ABC123_20261031_001509.jpg', 'IST date roll-over');
  assert.doesNotMatch(stampFileName('NCL-2026-ABC123 ../x', '2026-10-30T07:15:00Z'), /[ ./]{2}|\s/);
});

test('coordinate and time formatting', () => {
  assert.equal(formatLat(-12.5), '12.500000° S');
  assert.equal(formatLng(-0.1), '0.100000° W');
  assert.equal(formatIST(null), 'Not recorded in the file');
});

test('stamp layout scales with the photo and stays compact on portrait and landscape', () => {
  const cases = [[3024, 4032], [1080, 1920], [4032, 3024], [1920, 1080], [640, 480]];
  const sizes = cases.map(([w, h]) => stampLayout(w, h));
  assert.ok(sizes[0].fs > sizes[3].fs && sizes[3].fs > sizes[4].fs, 'font follows resolution');
  cases.forEach(([w, h], i) => {
    const L = sizes[i];
    const height = stampPanelHeight(L, 8); // seven rows, the address on two lines
    const area = (height * L.panelWidth) / (w * h);
    if (Math.min(w, h) >= 1000) {
      assert.ok(height < h * 0.32, `${w}×${h}: panel height ${height}`);
      assert.ok(area < (L.landscape ? 0.16 : 0.25), `${w}×${h}: covers ${(area * 100).toFixed(0)}% of the photo`);
    } else {
      assert.ok(area < 0.25, `${w}×${h}: covers ${(area * 100).toFixed(0)}% of a low-resolution photo`);
    }
    assert.ok(L.panelWidth <= w - L.margin * 2);
  });
  assert.ok(sizes[3].panelWidth < 1920 * 0.6, 'landscape panel leaves most of the scene clear');
});

test('evidence reference is a deterministic, non-secret file fingerprint', async () => {
  const a = await evidenceRef(new Blob(['photo-bytes']));
  assert.match(a, /^EV-[0-9A-F]{4}-[0-9A-F]{4}$/);
  assert.equal(a, await evidenceRef(new Blob(['photo-bytes'])));
  assert.notEqual(a, await evidenceRef(new Blob(['other-bytes'])));
});

// ---------- Saving ----------
test('save method: share sheet on phones with file sharing, download on desktop', () => {
  const doc = {createElement: () => ({download: ''})};
  const phone = {File, document: doc, matchMedia: () => ({matches: true}), navigator: {userAgent: 'iPhone', share() {}, canShare: () => true}};
  const desktop = {File, document: doc, matchMedia: () => ({matches: false}), navigator: {userAgent: 'Windows', share() {}, canShare: () => true}};
  const oldPhone = {File, document: doc, matchMedia: () => ({matches: true}), navigator: {userAgent: 'Android'}};
  assert.equal(preferredMethod(phone), 'share');
  assert.equal(preferredMethod(desktop), 'download');
  assert.equal(preferredMethod(oldPhone), 'download');
});

test('share outcomes are reported honestly and concurrent saves collapse into one', async () => {
  let calls = 0;
  let mode = 'ok';
  Object.defineProperty(globalThis, 'navigator', {value: {share: async () => { calls += 1; await tick(); if (mode !== 'ok') throw Object.assign(new Error(mode), {name: mode}); }}, configurable: true});
  const blob = new Blob(['x'], {type: 'image/jpeg'});
  const [a, b] = await Promise.all([savePhoto(blob, 'a.jpg', {key: 'k', method: 'share'}), savePhoto(blob, 'a.jpg', {key: 'k', method: 'share'})]);
  assert.equal(calls, 1);
  assert.equal(a, b);
  assert.equal(a.outcome, 'shared');
  assert.doesNotMatch(a.message, /saved to Photos/i, 'never claims the Photos app received it');
  mode = 'AbortError';
  assert.equal((await savePhoto(blob, 'a.jpg', {key: 'k', method: 'share'})).outcome, 'cancelled');
  mode = 'NotAllowedError';
  assert.equal((await savePhoto(blob, 'a.jpg', {key: 'k', method: 'share'})).outcome, 'blocked');
});

test('automatic save runs at most once per complaint', async () => {
  const store = new Map();
  globalThis.sessionStorage = {getItem: k => store.get(k) ?? null, setItem: (k, v) => store.set(k, v)};
  let calls = 0;
  Object.defineProperty(globalThis, 'navigator', {value: {share: async () => { calls += 1; }}, configurable: true});
  const blob = new Blob(['x'], {type: 'image/jpeg'});
  const first = await autoSavePhoto(blob, 'n.jpg', 'NCL-2026-AAAAAA', {method: 'share'});
  const again = await autoSavePhoto(blob, 'n.jpg', 'NCL-2026-AAAAAA', {method: 'share'});
  assert.equal(first.outcome, 'shared');
  assert.equal(again, null);
  assert.equal(calls, 1);
});
