import test from 'node:test';
import assert from 'node:assert/strict';
import {createCamera, classifyCameraError, facingFromLabel} from '../dist/camera-service.js';
import {createLocator, classifyGeoError, fixQuality, isBetterFix, GEO} from '../dist/location-service.js';

// ---------- Fakes ----------
function fakeTrack(deviceId = 'cam-a') {
  return {readyState: 'live', stopped: false, onended: null, stop() { this.readyState = 'ended'; this.stopped = true; }, getSettings() { return {deviceId, facingMode: deviceId === 'cam-b' ? 'user' : 'environment'}; }};
}
function fakeStream(deviceId) {
  const track = fakeTrack(deviceId);
  return {track, getTracks: () => [track], getVideoTracks: () => [track]};
}
function fakeMedia({fail = [], devices = [{kind: 'videoinput', deviceId: 'cam-a', label: 'Back camera'}], delay = 0} = {}) {
  const calls = [];
  const streams = [];
  return {
    calls, streams,
    async getUserMedia(constraints) {
      calls.push(constraints);
      if (delay) await new Promise(r => setTimeout(r, delay));
      const err = fail.shift();
      if (err) throw Object.assign(new Error(err), {name: err});
      const s = fakeStream(constraints.video?.deviceId?.exact);
      streams.push(s);
      return s;
    },
    async enumerateDevices() { return devices; }
  };
}
function fakeVideo({blockAutoplay = false} = {}) {
  return {
    srcObject: null, readyState: 0, videoWidth: 0, paused: true, muted: false, attrs: {},
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener() {}, removeEventListener() {},
    async play() {
      if (blockAutoplay) throw Object.assign(new Error('blocked'), {name: 'NotAllowedError'});
      this.paused = false;
    },
    pause() { this.paused = true; },
    // Simulates the browser decoding the first frame after a stream is attached.
    loadFrames() { this.readyState = 4; this.videoWidth = 1280; }
  };
}

// ---------- Camera ----------
test('camera errors map to actionable states', () => {
  assert.equal(classifyCameraError({name: 'NotAllowedError'}), 'denied');
  assert.equal(classifyCameraError({name: 'NotFoundError'}), 'missing');
  assert.equal(classifyCameraError({name: 'NotReadableError'}), 'busy');
  assert.equal(classifyCameraError({name: 'OverconstrainedError'}), 'constraints');
  assert.equal(classifyCameraError({name: 'Weird'}), 'error');
  assert.equal(facingFromLabel('Back Camera 2'), 'environment');
  assert.equal(facingFromLabel('FaceTime HD Camera'), 'user');
});

test('camera prefers the rear camera and attaches only when frames arrive', async () => {
  const media = fakeMedia();
  const cam = createCamera({mediaDevices: media, isSecureContext: true});
  const stream = await cam.start({auto: true});
  assert.ok(stream);
  assert.deepEqual(media.calls[0].video.facingMode, {ideal: 'environment'});
  const video = fakeVideo();
  setTimeout(() => video.loadFrames(), 30);
  assert.equal(await cam.attach(video), true);
  assert.equal(video.srcObject, stream);
  assert.equal(video.muted, true);
  assert.equal(video.playsInline, true);
  assert.equal(cam.state.status, 'live');
});

test('concurrent starts share one request and a live stream is reused', async () => {
  const media = fakeMedia({delay: 20});
  const cam = createCamera({mediaDevices: media, isSecureContext: true});
  const [a, b] = await Promise.all([cam.start(), cam.start()]);
  assert.equal(a, b);
  assert.equal(media.calls.length, 1);
  await cam.start();
  assert.equal(media.calls.length, 1, 'a live stream is not requested again');
});

test('leaving during a pending request stops the late stream (no orphaned camera)', async () => {
  const media = fakeMedia({delay: 30});
  const cam = createCamera({mediaDevices: media, isSecureContext: true});
  const pending = cam.start();
  cam.stop();
  assert.equal(await pending, null);
  assert.equal(media.streams[0].track.stopped, true);
  assert.equal(cam.stream, null);
});

test('stop releases every track and detaches the video', async () => {
  const media = fakeMedia();
  const cam = createCamera({mediaDevices: media, isSecureContext: true});
  await cam.start();
  const video = fakeVideo();
  video.loadFrames();
  await cam.attach(video);
  cam.stop();
  assert.equal(media.streams[0].track.stopped, true);
  assert.equal(video.srcObject, null);
  assert.equal(cam.state.status, 'idle');
  await cam.start();
  assert.equal(media.calls.length, 2, 'returning to the screen starts a fresh stream');
});

test('constraint and busy failures fall back to any available camera', async () => {
  const media = fakeMedia({fail: ['OverconstrainedError', 'NotReadableError']});
  const cam = createCamera({mediaDevices: media, isSecureContext: true});
  assert.ok(await cam.start());
  assert.equal(media.calls.length, 3);
  assert.equal(media.calls[2].video, true);
});

test('denial is remembered: automatic starts do not prompt again, explicit retry does', async () => {
  const media = fakeMedia({fail: ['NotAllowedError']});
  const cam = createCamera({mediaDevices: media, isSecureContext: true});
  assert.equal(await cam.start({auto: true}), null);
  assert.equal(cam.state.status, 'denied');
  await cam.start({auto: true});
  assert.equal(media.calls.length, 1, 'no repeated permission request on reopen');
  cam.forgetDenial();
  assert.ok(await cam.start({auto: false}));
  assert.equal(media.calls.length, 2);
});

test('insecure and unsupported environments never call getUserMedia', async () => {
  const media = fakeMedia();
  const insecure = createCamera({mediaDevices: media, isSecureContext: false});
  await insecure.start();
  assert.equal(insecure.state.status, 'insecure');
  const unsupported = createCamera({mediaDevices: {}, isSecureContext: true});
  await unsupported.start();
  assert.equal(unsupported.state.status, 'unsupported');
  assert.equal(media.calls.length, 0);
});

test('blocked autoplay asks for a tap instead of showing a blank preview', async () => {
  const cam = createCamera({mediaDevices: fakeMedia(), isSecureContext: true});
  await cam.start();
  const video = fakeVideo({blockAutoplay: true});
  video.loadFrames();
  assert.equal(await cam.attach(video), false);
  assert.equal(cam.state.status, 'needs-tap');
});

test('switching cycles devices and stops the old stream first', async () => {
  const devices = [{kind: 'videoinput', deviceId: 'cam-a', label: 'Back camera'}, {kind: 'videoinput', deviceId: 'cam-b', label: 'Front camera'}];
  const media = fakeMedia({devices});
  const cam = createCamera({mediaDevices: media, isSecureContext: true});
  await cam.start();
  const video = fakeVideo();
  video.loadFrames();
  await cam.attach(video);
  assert.equal(cam.state.canSwitch, true);
  await cam.switchCamera(video);
  assert.equal(media.streams[0].track.stopped, true);
  assert.deepEqual(media.calls[1].video.deviceId, {exact: 'cam-b'});
  assert.equal(cam.state.facing, 'user');
});

// ---------- Location ----------
function fakeTimers() {
  let now = 0;
  let id = 0;
  const queue = new Map();
  return {
    now: () => now,
    setTimeout(fn, ms) { queue.set(++id, {fn, at: now + ms}); return id; },
    clearTimeout(i) { queue.delete(i); },
    advance(ms) {
      now += ms;
      for (const [i, t] of [...queue]) if (t.at <= now) { queue.delete(i); t.fn(); }
    }
  };
}
function fakeGeo() {
  const g = {watches: new Map(), cleared: [], nextId: 0, options: null};
  g.watchPosition = (ok, fail, options) => { g.options = options; const i = ++g.nextId; g.watches.set(i, {ok, fail}); return i; };
  g.clearWatch = i => { g.cleared.push(i); g.watches.delete(i); };
  g.emit = (lat, lng, accuracy, timestamp) => [...g.watches.values()].forEach(w => w.ok({coords: {latitude: lat, longitude: lng, accuracy}, timestamp}));
  g.fail = code => [...g.watches.values()].forEach(w => w.fail({code}));
  return g;
}

test('location helpers classify errors and fix quality', () => {
  assert.equal(classifyGeoError({code: 1}), 'denied');
  assert.equal(classifyGeoError({code: 2}), 'unavailable');
  assert.equal(classifyGeoError({code: 3}), 'timeout');
  assert.equal(fixQuality({accuracy: 10}), 'good');
  assert.equal(fixQuality({accuracy: 40}), 'fair');
  assert.equal(fixQuality({accuracy: 200}), 'poor');
  assert.equal(fixQuality({accuracy: 2000}), 'approximate');
  assert.equal(isBetterFix({accuracy: 20}, {accuracy: 80}), true);
  assert.equal(isBetterFix({accuracy: 90}, {accuracy: 80}), false);
});

test('a good first fix is acquired and the watcher stops', () => {
  const t = fakeTimers();
  const geo = fakeGeo();
  const loc = createLocator({geolocation: geo, isSecureContext: true, now: t.now, timers: t});
  loc.start();
  assert.equal(loc.state.status, 'locating');
  assert.equal(geo.options.enableHighAccuracy, true);
  assert.equal(geo.options.maximumAge, 0, 'never accepts a cached fix from elsewhere');
  geo.emit(13.03, 77.59, 9, 1000);
  assert.equal(loc.state.status, 'acquired');
  assert.equal(loc.fix.accuracy, 9);
  assert.equal(loc.fix.sourceType, 'gps');
  assert.equal(geo.watches.size, 0);
});

test('a poor fix is refined within a bounded window, keeping the best reading', () => {
  const t = fakeTimers();
  const geo = fakeGeo();
  const loc = createLocator({geolocation: geo, isSecureContext: true, now: t.now, timers: t});
  loc.start();
  geo.emit(13.0, 77.5, 400, 1000);
  assert.equal(loc.state.status, 'acquired');
  assert.equal(loc.state.refining, true);
  geo.emit(13.01, 77.51, 900, 2000); // worse: ignored
  assert.equal(loc.fix.accuracy, 400);
  geo.emit(13.02, 77.52, 60, 3000); // better: kept, still refining
  assert.equal(loc.fix.accuracy, 60);
  t.advance(GEO.REFINE_WINDOW_MS + 1);
  assert.equal(loc.state.refining, false);
  assert.equal(geo.watches.size, 0, 'refinement watcher stops at the end of the window');
  assert.equal(loc.fix.accuracy, 60);
});

test('no fix within the limit reports a timeout and never invents a coordinate', () => {
  const t = fakeTimers();
  const geo = fakeGeo();
  const loc = createLocator({geolocation: geo, isSecureContext: true, now: t.now, timers: t});
  loc.start();
  t.advance(GEO.FIRST_FIX_TIMEOUT_MS + 1);
  assert.equal(loc.state.status, 'timeout');
  assert.equal(loc.fix, null);
  assert.equal(geo.watches.size, 0);
});

test('denied location clears the fix, is remembered, and retry can ask again', () => {
  const t = fakeTimers();
  const geo = fakeGeo();
  const loc = createLocator({geolocation: geo, isSecureContext: true, now: t.now, timers: t});
  loc.start();
  geo.fail(1);
  assert.equal(loc.state.status, 'denied');
  assert.equal(loc.fix, null);
  loc.start({auto: true});
  assert.equal(geo.nextId, 1, 'automatic restart does not request again after a denial');
  loc.forgetDenial();
  loc.start();
  assert.equal(geo.nextId, 2);
});

test('unavailable errors and insecure pages are reported separately', () => {
  const t = fakeTimers();
  const geo = fakeGeo();
  const loc = createLocator({geolocation: geo, isSecureContext: true, now: t.now, timers: t});
  loc.start();
  geo.fail(2);
  assert.equal(loc.state.status, 'unavailable');
  const insecure = createLocator({geolocation: geo, isSecureContext: false});
  insecure.start();
  assert.equal(insecure.state.status, 'insecure');
});

test('stopping on leave clears the watcher and ignores late callbacks', () => {
  const t = fakeTimers();
  const geo = fakeGeo();
  const loc = createLocator({geolocation: geo, isSecureContext: true, now: t.now, timers: t});
  loc.start();
  const [{ok}] = [...geo.watches.values()];
  loc.stop();
  assert.equal(geo.watches.size, 0);
  ok({coords: {latitude: 1, longitude: 2, accuracy: 5}, timestamp: 1});
  assert.equal(loc.fix, null, 'a stale callback cannot set the location');
});

test('old fixes are not usable for a new capture', () => {
  const t = fakeTimers();
  const geo = fakeGeo();
  const loc = createLocator({geolocation: geo, isSecureContext: true, now: t.now, timers: t});
  loc.start();
  geo.emit(13.03, 77.59, 8, 1);
  assert.ok(loc.usableFix(1000));
  assert.equal(loc.usableFix(GEO.MAX_FIX_AGE_MS + 2), null);
});
