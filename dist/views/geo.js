// Address lookup (Photon reverse geocoding over OpenStreetMap data). The camera screen resolves
// the address automatically once a fix arrives (see address-service.js); this module performs
// the single request. Results are cached per session at ~11 m precision, and a failed lookup
// never replaces or erases the coordinate evidence.
import {createAddressResolver} from '../address-service.js';

let config = null;

export async function loadGeoConfig() {
  if (!config) {
    try {
      config = await fetch('map-config.json', {cache: 'no-cache'}).then(r => {
        if (!r.ok) throw new Error('config');
        return r.json();
      });
    } catch {
      config = {geocoder: null};
    }
  }
  return config;
}

function cacheKey(lat, lng) {
  return `ncl-geo-${lat.toFixed(4)},${lng.toFixed(4)}`;
}

/** Short place label for compact displays: locality, city, state. */
function shortLabel(p) {
  const parts = [p.locality || p.district || p.name, p.city || p.county, p.state].filter(Boolean);
  return [...new Set(parts)].join(', ');
}

export async function lookupAddress(target, {signal} = {}) {
  if (!target || !Number.isFinite(target.lat) || !Number.isFinite(target.lng)) {
    return {ok: false, message: 'Share your location first. Address lookup needs coordinates.'};
  }
  const key = cacheKey(target.lat, target.lng);
  try {
    const cached = JSON.parse(sessionStorage.getItem(key) || 'null');
    if (cached?.address) return {ok: true, ...cached, cached: true};
  } catch { /* cache is optional */ }

  if (globalThis.navigator?.onLine === false) {
    return {ok: false, offline: true, message: 'You are offline. The address will be looked up when you reconnect. Your GPS coordinates are saved.'};
  }
  const cfg = await loadGeoConfig();
  if (!cfg.geocoder) return {ok: false, message: 'Address lookup is not configured here. Your GPS coordinates are still saved.'};
  // One controller for both the caller's cancel and the 8 s timeout (AbortSignal.any is not universal).
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, 8000);
  const onCancel = () => controller.abort();
  if (signal?.aborted) controller.abort();
  signal?.addEventListener?.('abort', onCancel, {once: true});
  try {
    const url = new URL('reverse', cfg.geocoder);
    url.search = new URLSearchParams({lat: target.lat, lon: target.lng, limit: 1, lang: 'en'});
    const response = await fetch(url, {signal: controller.signal});
    if (!response.ok) throw new Error(`Lookup failed (${response.status})`);
    const data = await response.json();
    const p = data.features?.[0]?.properties;
    if (!p) return {ok: false, message: 'No mapped address was found at this point. Your GPS coordinates are still saved.'};
    const street = [p.housenumber, p.street].filter(Boolean).join(' ');
    const parts = [p.name, street, p.locality || p.district, p.city || p.county, p.state, p.postcode].filter(Boolean);
    const address = [...new Set(parts)].join(', ');
    const neighbourhood = p.locality || p.district || p.city || p.county || 'Bengaluru';
    if (!address) return {ok: false, message: 'The lookup returned no readable address. Your GPS coordinates are still saved.'};
    const result = {address, neighbourhood, short: shortLabel(p) || neighbourhood};
    try { sessionStorage.setItem(key, JSON.stringify(result)); } catch { /* ignore */ }
    return {ok: true, ...result};
  } catch (err) {
    if (signal?.aborted && !timedOut) return {ok: false, aborted: true, message: ''};
    if (globalThis.navigator?.onLine === false) return {ok: false, offline: true, message: 'You are offline. The address will be looked up when you reconnect. Your GPS coordinates are saved.'};
    return {
      ok: false,
      message: timedOut
        ? 'Address lookup timed out. Your GPS coordinates are still saved.'
        : 'Address lookup is unavailable right now (network or service issue). Your GPS coordinates are still saved.'
    };
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener?.('abort', onCancel);
  }
}

/** Shared resolver: one cache and one request queue for the camera, review and submit steps. */
export const addressResolver = createAddressResolver({lookup: lookupAddress});
globalThis.addEventListener?.('online', () => addressResolver.online());
