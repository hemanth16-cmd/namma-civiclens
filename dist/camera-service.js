// Live camera lifecycle for evidence capture.
//
// One controller owns at most one MediaStream. Every start is tagged with a token, so a
// getUserMedia call that resolves after the user has left the screen, switched cameras or
// pressed Retry stops its own tracks instead of overwriting the current stream. The preview is
// only reported as live once the mounted <video> is actually playing frames.
//
// Dependencies are injected so the lifecycle can be tested in Node without a browser.

export const CAMERA_LIMITS = {
  METADATA_TIMEOUT_MS: 8000,   // stream attached, waiting for video dimensions
  FIRST_FRAME_TIMEOUT_MS: 6000 // playing, waiting for a decoded frame
};

/** Map a getUserMedia / play() error to a user-facing state. */
export function classifyCameraError(err) {
  switch (err?.name) {
    case 'NotAllowedError':
    case 'PermissionDeniedError':
    case 'SecurityError':
      return 'denied';
    case 'NotFoundError':
    case 'DevicesNotFoundError':
      return 'missing';
    case 'NotReadableError':
    case 'TrackStartError':
    case 'AbortError':
      return 'busy';
    case 'OverconstrainedError':
    case 'ConstraintNotSatisfiedError':
      return 'constraints';
    default:
      return 'error';
  }
}

/** Best guess at which way a camera faces from its label (labels exist only after permission). */
export function facingFromLabel(label = '') {
  const l = label.toLowerCase();
  if (/back|rear|environment|world/.test(l)) return 'environment';
  if (/front|user|face|selfie|integrated|webcam|facetime/.test(l)) return 'user';
  return null;
}

const liveTracks = stream => !!stream && stream.getTracks().some(t => t.readyState === 'live');
const stopTracks = stream => stream?.getTracks().forEach(t => { try { t.stop(); } catch { /* already stopped */ } });

function waitFor(target, events, test, ms, timers) {
  return new Promise(resolve => {
    if (test()) return resolve(true);
    let done = false;
    const finish = ok => {
      if (done) return;
      done = true;
      timers.clearTimeout(timer);
      timers.clearInterval(poll);
      events.forEach(ev => target.removeEventListener?.(ev, onEvent));
      resolve(ok);
    };
    const onEvent = () => { if (test()) finish(true); };
    events.forEach(ev => target.addEventListener?.(ev, onEvent));
    // Some mobile browsers fire events before dimensions are set; poll as a backstop.
    const poll = timers.setInterval(onEvent, 100);
    const timer = timers.setTimeout(() => finish(test()), ms);
  });
}

export function createCamera({
  mediaDevices = globalThis.navigator?.mediaDevices,
  isSecureContext = globalThis.isSecureContext !== false,
  // Wrapped so browser timer functions are never called with the wrong `this`.
  timers = {setTimeout: (fn, ms) => setTimeout(fn, ms), clearTimeout: id => clearTimeout(id), setInterval: (fn, ms) => setInterval(fn, ms), clearInterval: id => clearInterval(id)}
} = {}) {
  const s = {
    status: 'idle',          // idle | starting | attaching | live | needs-tap | no-frames | denied | missing | busy | ended | insecure | unsupported | error
    detail: '',
    stream: null,
    facing: 'environment',   // rear camera first on phones; desktops ignore the hint
    deviceId: null,
    devices: [],
    deniedThisSession: false,
    requests: 0              // getUserMedia calls made, exposed for tests and diagnostics
  };
  const listeners = new Set();
  let token = 0;
  let pending = null;
  let video = null;

  const snapshot = () => ({
    status: s.status, detail: s.detail, facing: s.facing, deviceId: s.deviceId,
    cameraCount: s.devices.length, canSwitch: canSwitch(), live: s.status === 'live'
  });
  const emit = () => listeners.forEach(fn => { try { fn(snapshot()); } catch { /* a view error must not break the camera */ } });
  const set = (status, detail = '') => { s.status = status; s.detail = detail; emit(); };

  function canSwitch() {
    if (s.devices.length > 1) return true;
    // Without device enumeration we can still ask for the other facing mode.
    return s.devices.length === 0 && !!mediaDevices?.getUserMedia;
  }

  async function listCameras() {
    try {
      const all = await mediaDevices.enumerateDevices?.();
      return (all || []).filter(d => d.kind === 'videoinput');
    } catch {
      return [];
    }
  }

  function attempts() {
    const list = [];
    if (s.deviceId) list.push({video: {deviceId: {exact: s.deviceId}, width: {ideal: 1920}, height: {ideal: 1080}}, audio: false});
    list.push({video: {facingMode: {ideal: s.facing}, width: {ideal: 1920}, height: {ideal: 1080}}, audio: false});
    list.push({video: {facingMode: {ideal: s.facing}}, audio: false});
    list.push({video: true, audio: false}); // any available camera
    return list;
  }

  async function acquire() {
    let lastErr = null;
    for (const constraints of attempts()) {
      try {
        s.requests += 1;
        return await mediaDevices.getUserMedia(constraints);
      } catch (err) {
        lastErr = err;
        const kind = classifyCameraError(err);
        // Permission and missing hardware do not improve with looser constraints.
        if (kind === 'denied' || kind === 'missing') throw err;
      }
    }
    throw lastErr;
  }

  /** Request a stream. Concurrent calls share one request; a live stream is reused. */
  function start({auto = false} = {}) {
    if (!isSecureContext) { set('insecure'); return Promise.resolve(null); }
    if (!mediaDevices?.getUserMedia) { set('unsupported'); return Promise.resolve(null); }
    if (auto && s.deniedThisSession) { set('denied'); return Promise.resolve(null); }
    if (liveTracks(s.stream)) return Promise.resolve(s.stream);
    if (pending) return pending;
    const my = ++token;
    set('starting');
    pending = (async () => {
      try {
        const stream = await acquire();
        if (my !== token) { stopTracks(stream); return null; }
        s.stream = stream;
        s.deniedThisSession = false;
        const track = stream.getVideoTracks?.()[0];
        const settings = track?.getSettings?.() || {};
        s.deviceId = settings.deviceId || s.deviceId;
        if (settings.facingMode === 'user' || settings.facingMode === 'environment') s.facing = settings.facingMode;
        if (track) track.onended = () => {
          if (my !== token) return;
          s.stream = null;
          set('ended');
        };
        const cams = await listCameras();
        if (my !== token) { stopTracks(stream); return null; }
        s.devices = cams;
        if (!settings.facingMode) {
          const current = cams.find(d => d.deviceId === s.deviceId);
          const guess = facingFromLabel(current?.label);
          if (guess) s.facing = guess;
        }
        set('attaching');
        return stream;
      } catch (err) {
        if (my !== token) return null;
        let kind = classifyCameraError(err);
        if (kind === 'constraints') kind = 'error';
        if (kind === 'denied') s.deniedThisSession = true;
        set(kind, err?.message || '');
        return null;
      } finally {
        if (my === token) pending = null;
      }
    })();
    return pending;
  }

  /** Attach the current stream to the mounted video element and wait for real frames. */
  async function attach(el) {
    const stream = s.stream;
    if (!el || !liveTracks(stream)) return false;
    const my = token;
    video = el;
    el.muted = true;
    el.defaultMuted = true;
    el.playsInline = true;
    el.autoplay = true;
    el.setAttribute?.('playsinline', '');
    el.setAttribute?.('muted', '');
    if (el.srcObject !== stream) el.srcObject = stream;
    set('attaching');
    const hasMeta = await waitFor(el, ['loadedmetadata', 'loadeddata'], () => el.readyState >= 1 && el.videoWidth > 0, CAMERA_LIMITS.METADATA_TIMEOUT_MS, timers);
    if (my !== token || el !== video) return false;
    if (!hasMeta && el.readyState < 1) { set('no-frames'); return false; }
    try {
      await el.play?.();
    } catch (err) {
      if (my !== token || el !== video) return false;
      if (err?.name === 'NotAllowedError') { set('needs-tap'); return false; }
      if (err?.name !== 'AbortError') { set('error', err?.message || ''); return false; }
    }
    const frames = await waitFor(el, ['playing', 'timeupdate', 'resize'], () => el.readyState >= 2 && el.videoWidth > 0 && !el.paused, CAMERA_LIMITS.FIRST_FRAME_TIMEOUT_MS, timers);
    if (my !== token || el !== video) return false;
    if (!frames) { set('no-frames'); return false; }
    set('live');
    return true;
  }

  /** Called from a user tap when autoplay was blocked. */
  async function resume() {
    if (!video) return false;
    return attach(video);
  }

  function release() {
    token += 1;
    pending = null;
    stopTracks(s.stream);
    s.stream = null;
    if (video) {
      try { video.pause?.(); } catch { /* ignore */ }
      video.srcObject = null;
    }
    video = null;
  }

  /** Stop every track and detach. Safe to call repeatedly. */
  function stop() {
    release();
    set(s.deniedThisSession ? 'denied' : 'idle');
  }

  /** Move to the next camera. Restores the previous camera if the new one cannot start. */
  async function switchCamera(el) {
    const previous = {deviceId: s.deviceId, facing: s.facing};
    if (s.devices.length > 1) {
      const index = s.devices.findIndex(d => d.deviceId === s.deviceId);
      const next = s.devices[(index + 1) % s.devices.length];
      s.deviceId = next.deviceId;
      s.facing = facingFromLabel(next.label) || (s.facing === 'environment' ? 'user' : 'environment');
    } else {
      s.deviceId = null;
      s.facing = s.facing === 'environment' ? 'user' : 'environment';
    }
    release(); // phones cannot open two cameras at once
    const stream = await start();
    if (stream) return attach(el);
    if (s.status === 'denied') return false;
    Object.assign(s, previous);
    const restored = await start();
    if (restored) {
      await attach(el);
      set(s.status, 'That camera could not start, so the previous camera is back on.');
    }
    return false;
  }

  return {
    start, attach, resume, stop, switchCamera,
    subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); },
    get state() { return snapshot(); },
    get stream() { return liveTracks(s.stream) ? s.stream : null; },
    get requests() { return s.requests; },
    forgetDenial() { s.deniedThisSession = false; }
  };
}

export const camera = createCamera();
