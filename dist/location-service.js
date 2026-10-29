// Device location for live evidence.
//
// Uses real browser geolocation only. There is no default coordinate, no IP lookup and no
// silent substitution: if the browser cannot provide a fix, the state says so. A poor first fix
// starts a short, bounded refinement window; the watcher always stops when it ends, when a good
// fix arrives, or when the screen is left.
//
// Dependencies are injected so timeouts and refinement can be tested in Node.

export const GEO = {
  GOOD_ACCURACY_M: 25,          // stop refining once a fix is this precise
  LOW_ACCURACY_M: 50,           // flagged as low accuracy above this
  APPROXIMATE_M: 500,           // likely Wi-Fi or cell based, not satellite GPS
  FIRST_FIX_TIMEOUT_MS: 20000,  // give up waiting for any fix
  REFINE_WINDOW_MS: 15000,      // extra time allowed to improve a poor first fix
  MAX_FIX_AGE_MS: 120000        // a capture needs a fix newer than this
};

export function classifyGeoError(err) {
  if (err?.code === 1) return 'denied';
  if (err?.code === 3) return 'timeout';
  return 'unavailable';
}

export function fixQuality(fix) {
  if (!fix || !Number.isFinite(fix.accuracy)) return 'unknown';
  if (fix.accuracy <= GEO.GOOD_ACCURACY_M) return 'good';
  if (fix.accuracy <= GEO.LOW_ACCURACY_M) return 'fair';
  if (fix.accuracy <= GEO.APPROXIMATE_M) return 'poor';
  return 'approximate';
}

export const fixAgeMs = (fix, now = Date.now()) => (Number.isFinite(fix?.timestamp) ? Math.max(0, now - fix.timestamp) : Infinity);
export const isFresh = (fix, now = Date.now()) => fixAgeMs(fix, now) <= GEO.MAX_FIX_AGE_MS;

/** A newer reading replaces the best one when it is at least as precise. */
export function isBetterFix(candidate, best) {
  if (!best) return true;
  if (!Number.isFinite(candidate.accuracy)) return false;
  if (!Number.isFinite(best.accuracy)) return true;
  return candidate.accuracy <= best.accuracy;
}

export function toFix(position, now = Date.now()) {
  const c = position.coords;
  return {
    lat: c.latitude,
    lng: c.longitude,
    accuracy: Number.isFinite(c.accuracy) ? c.accuracy : null,
    timestamp: Number.isFinite(position.timestamp) && position.timestamp > 0 ? position.timestamp : now,
    source: 'Device GPS',
    sourceType: 'gps'
  };
}

export function createLocator({
  geolocation = globalThis.navigator?.geolocation,
  isSecureContext = globalThis.isSecureContext !== false,
  now = () => Date.now(),
  timers = {setTimeout: (fn, ms) => setTimeout(fn, ms), clearTimeout: id => clearTimeout(id)}
} = {}) {
  const s = {status: 'idle', fix: null, refining: false, deniedThisSession: false};
  const listeners = new Set();
  let token = 0;
  let watchId = null;
  let firstTimer = null;
  let refineTimer = null;

  const snapshot = () => ({status: s.status, fix: s.fix, refining: s.refining, active: watchId !== null});
  const emit = () => listeners.forEach(fn => { try { fn(snapshot()); } catch { /* view errors must not stop location */ } });

  function clearWatch() {
    if (watchId !== null) {
      try { geolocation.clearWatch(watchId); } catch { /* ignore */ }
      watchId = null;
    }
    timers.clearTimeout(firstTimer);
    timers.clearTimeout(refineTimer);
    firstTimer = null;
    refineTimer = null;
    s.refining = false;
  }

  function finish(status) {
    clearWatch();
    s.status = status;
    emit();
  }

  /** Begin a fresh location attempt. `auto` attempts respect an earlier denial this session. */
  function start({auto = false} = {}) {
    if (!isSecureContext) return finish('insecure');
    if (!geolocation?.watchPosition) return finish('unsupported');
    if (auto && s.deniedThisSession) return finish('denied');
    clearWatch();
    const my = ++token;
    let gotFix = false;
    s.status = 'locating';
    emit();

    firstTimer = timers.setTimeout(() => {
      if (my === token && !gotFix) finish('timeout');
    }, GEO.FIRST_FIX_TIMEOUT_MS);

    const onPosition = position => {
      if (my !== token) return;
      const fix = toFix(position, now());
      if (!gotFix) {
        gotFix = true;
        timers.clearTimeout(firstTimer);
        s.fix = fix;
        s.deniedThisSession = false;
        if (fixQuality(fix) === 'good') return finish('acquired');
        s.status = 'acquired';
        s.refining = true;
        refineTimer = timers.setTimeout(() => { if (my === token) finish('acquired'); }, GEO.REFINE_WINDOW_MS);
        return emit();
      }
      if (isBetterFix(fix, s.fix)) s.fix = fix;
      if (fixQuality(s.fix) === 'good') return finish('acquired');
      emit();
    };

    const onError = err => {
      if (my !== token) return;
      const kind = classifyGeoError(err);
      if (kind === 'denied') {
        s.deniedThisSession = true;
        s.fix = null; // never keep showing a location the user has just refused to share
        return finish('denied');
      }
      // During refinement a failed extra reading keeps the best fix already obtained.
      if (gotFix) return finish('acquired');
      finish(kind);
    };

    try {
      watchId = geolocation.watchPosition(onPosition, onError, {
        enableHighAccuracy: true,
        maximumAge: 0,                       // a cached fix could be from somewhere else
        timeout: GEO.FIRST_FIX_TIMEOUT_MS
      });
    } catch {
      finish('unavailable');
    }
  }

  /** Stop watching. Keeps the last fix, which carries its own timestamp. */
  function stop() {
    token += 1;
    const wasActive = watchId !== null || s.status === 'locating';
    clearWatch();
    if (wasActive) {
      s.status = s.fix ? 'acquired' : 'idle';
      emit();
    }
  }

  return {
    start, stop,
    subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); },
    get state() { return snapshot(); },
    get fix() { return s.fix; },
    /** A fix is usable for a new capture only if it is recent. */
    usableFix(at = now()) { return s.fix && isFresh(s.fix, at) ? s.fix : null; },
    forgetDenial() { s.deniedThisSession = false; }
  };
}

export const locator = createLocator();
