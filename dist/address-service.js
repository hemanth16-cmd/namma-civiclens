// Automatic reverse geocoding for the current location fix.
//
// The camera screen hands every new fix to resolve(). The resolver waits briefly for the fix to
// settle (debounce), reuses an address already found within REUSE_RADIUS_M instead of asking
// again, cancels a request that a newer, distant fix has made stale, and backs off after a
// failure so a poor connection is not hammered. The address is kept by position, not on the fix
// object, so refined fixes a few metres apart keep showing it.
//
// The address is an approximate label for the coordinates. GPS coordinates stay the evidence.
// Dependencies are injected so the behaviour can be tested in Node.

export const ADDR = {
  DEBOUNCE_MS: 700,        // let a refining fix settle before asking
  REUSE_RADIUS_M: 30,      // an address found within this distance is reused without a request
  FAIL_BACKOFF_MS: 30000,  // automatic retries for the same spot wait this long after a failure
  MAX_CACHE: 60
};

/** Great-circle distance in metres. */
export function distanceM(a, b) {
  if (!a || !b) return Infinity;
  const R = 6371000, rad = Math.PI / 180;
  const dLat = (b.lat - a.lat) * rad, dLng = (b.lng - a.lng) * rad;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
}

const validPoint = p => !!p && Number.isFinite(p.lat) && Number.isFinite(p.lng);
const near = (a, b) => distanceM(a, b) <= ADDR.REUSE_RADIUS_M;

export function createAddressResolver({
  lookup,
  now = () => Date.now(),
  isOnline = () => globalThis.navigator?.onLine !== false,
  timers = {setTimeout: (fn, ms) => setTimeout(fn, ms), clearTimeout: id => clearTimeout(id)},
  makeController = () => (typeof AbortController === 'function' ? new AbortController() : null)
} = {}) {
  const s = {status: 'idle', address: null, neighbourhood: null, short: null, point: null, message: '', requests: 0};
  const cache = [];
  const listeners = new Set();
  let token = 0;
  let timer = null;
  let controller = null;
  let inflightPoint = null;
  let failure = null; // {point, at}

  const snapshot = () => ({status: s.status, address: s.address, neighbourhood: s.neighbourhood, short: s.short, point: s.point, message: s.message, requests: s.requests});
  const emit = () => listeners.forEach(fn => { try { fn(snapshot()); } catch { /* a view error must not stop lookups */ } });
  const set = patch => { Object.assign(s, patch); emit(); };

  function cached(point) {
    return validPoint(point) ? cache.find(c => near(c, point)) || null : null;
  }

  function remember(point, result) {
    const hit = cache.findIndex(c => near(c, point));
    if (hit >= 0) cache.splice(hit, 1);
    cache.unshift({lat: point.lat, lng: point.lng, address: result.address, neighbourhood: result.neighbourhood || null, short: result.short || null});
    cache.length = Math.min(cache.length, ADDR.MAX_CACHE);
  }

  function cancelPending() {
    timers.clearTimeout(timer);
    timer = null;
  }

  async function run(point) {
    timer = null;
    const my = ++token;
    try { controller?.abort(); } catch { /* ignore */ }
    controller = makeController();
    inflightPoint = point;
    if (!isOnline()) {
      inflightPoint = null;
      return set({status: 'offline', point, message: 'You are offline. The address will be looked up automatically when you reconnect. Your GPS coordinates are saved.'});
    }
    s.requests += 1;
    set({status: 'resolving', point, message: ''});
    let result;
    try {
      result = await lookup(point, {signal: controller?.signal});
    } catch {
      result = {ok: false, message: 'Address lookup is unavailable right now. Your GPS coordinates are still saved.'};
    }
    if (my !== token) return undefined; // a newer position took over
    inflightPoint = null;
    if (result?.ok) {
      failure = null;
      remember(point, result);
      return set({status: 'resolved', address: result.address, neighbourhood: result.neighbourhood || null, short: result.short || null, point, message: ''});
    }
    if (result?.aborted) return undefined;
    failure = {point, at: now()};
    return set({status: isOnline() && !result?.offline ? 'failed' : 'offline', address: null, neighbourhood: null, short: null, point, message: result?.message || 'The address could not be found. Your GPS coordinates are still saved.'});
  }

  /**
   * Find the address for a fix. Normal calls are debounced and reuse nearby results;
   * `immediate` skips the wait and `force` also bypasses the cache and failure back-off.
   */
  function resolve(fix, {immediate = false, force = false} = {}) {
    if (!validPoint(fix)) return;
    const point = {lat: fix.lat, lng: fix.lng};
    if (!force) {
      const hit = cached(point);
      if (hit) {
        cancelPending();
        if (inflightPoint && !near(inflightPoint, point)) { token += 1; inflightPoint = null; try { controller?.abort(); } catch { /* ignore */ } }
        if (s.status !== 'resolved' || s.address !== hit.address) set({status: 'resolved', address: hit.address, neighbourhood: hit.neighbourhood, short: hit.short, point, message: ''});
        return;
      }
      if (inflightPoint && near(inflightPoint, point)) return;           // already asking for this spot
      if (failure && near(failure.point, point) && now() - failure.at < ADDR.FAIL_BACKOFF_MS && s.status !== 'idle') return;
    }
    cancelPending();
    // Show progress straight away; the request itself waits for the fix to settle.
    if (s.status !== 'resolving' || !near(s.point, point)) set({status: 'resolving', address: null, neighbourhood: null, short: null, point, message: ''});
    if (immediate || force) run(point);
    else timer = timers.setTimeout(() => run(point), ADDR.DEBOUNCE_MS);
  }

  /** Wait (bounded) for an address near this fix. Resolves to {address, neighbourhood} or null. */
  function whenSettled(fix, ms = 4000) {
    if (!validPoint(fix)) return Promise.resolve(null);
    const hit = cached(fix);
    if (hit) return Promise.resolve(hit);
    const busy = (s.status === 'resolving' && near(s.point, fix)) || (inflightPoint && near(inflightPoint, fix));
    if (!busy) return Promise.resolve(null);
    return new Promise(done => {
      let t = null;
      const finish = value => { timers.clearTimeout(t); listeners.delete(onChange); done(value); };
      const onChange = st => {
        if (!near(st.point, fix) || st.status === 'resolving') return;
        finish(st.status === 'resolved' ? {address: st.address, neighbourhood: st.neighbourhood, short: st.short} : null);
      };
      listeners.add(onChange);
      t = timers.setTimeout(() => finish(cached(fix)), ms);
    });
  }

  /** Retry the last position now (Retry button, or coming back online). */
  function retry() {
    if (s.point) resolve(s.point, {force: true});
  }

  /** Called when the browser reports it is back online. */
  function online() {
    if ((s.status === 'offline' || s.status === 'failed') && s.point) resolve(s.point, {force: true});
  }

  /** Forget any pending debounce (screen left). An in-flight request may finish and fill the cache. */
  function pause() {
    cancelPending();
    if (s.status === 'resolving' && !inflightPoint) set({status: 'idle'});
  }

  return {
    resolve, retry, online, pause, whenSettled,
    addressFor: fix => cached(fix),
    subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); },
    get state() { return snapshot(); }
  };
}
