// Optional, user-triggered address lookup (Photon reverse geocoding). It is disclosed in the UI,
// cached per session at ~11 m precision, and never replaces or erases coordinate evidence.

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

export async function lookupAddress(target) {
  if (!target || !Number.isFinite(target.lat) || !Number.isFinite(target.lng)) {
    return {ok: false, message: 'Share your location first. Address lookup needs coordinates.'};
  }
  const key = cacheKey(target.lat, target.lng);
  try {
    const cached = JSON.parse(sessionStorage.getItem(key) || 'null');
    if (cached?.address) return {ok: true, ...cached, cached: true};
  } catch { /* cache is optional */ }

  const cfg = await loadGeoConfig();
  if (!cfg.geocoder) return {ok: false, message: 'Address lookup is not configured here. Your GPS coordinates are still saved.'};
  try {
    const url = new URL('reverse', cfg.geocoder);
    url.search = new URLSearchParams({lat: target.lat, lon: target.lng, limit: 1, lang: 'en'});
    const response = await fetch(url, {signal: AbortSignal.timeout(8000)});
    if (!response.ok) throw new Error(`Lookup failed (${response.status})`);
    const data = await response.json();
    const p = data.features?.[0]?.properties;
    if (!p) return {ok: false, message: 'No mapped address was found at this point. Your GPS coordinates are still saved.'};
    const parts = [p.name, p.street, p.locality || p.district, p.city || p.county, p.state, p.postcode].filter(Boolean);
    const address = [...new Set(parts)].join(', ');
    const neighbourhood = p.locality || p.district || p.city || p.county || 'Bengaluru';
    if (!address) return {ok: false, message: 'The lookup returned no readable address. Your GPS coordinates are still saved.'};
    const result = {address, neighbourhood};
    try { sessionStorage.setItem(key, JSON.stringify(result)); } catch { /* ignore */ }
    return {ok: true, ...result};
  } catch (err) {
    return {
      ok: false,
      message: err?.name === 'TimeoutError'
        ? 'Address lookup timed out. Your GPS coordinates are still saved.'
        : 'Address lookup is unavailable right now (network or service issue). Your GPS coordinates are still saved.'
    };
  }
}
