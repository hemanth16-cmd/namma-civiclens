// Shared, session-scoped application state. Kept free of view logic to avoid circular imports.

function safeJSON(raw, fallback) {
  try { return raw ? JSON.parse(raw) : fallback; } catch { return fallback; }
}
function storageGet(store, key, fallback) {
  try { return safeJSON(store.getItem(key), fallback); } catch { return fallback; }
}
function storageSet(store, key, value) {
  try { store.setItem(key, JSON.stringify(value)); return true; } catch { return false; }
}

export const state = {
  session: storageGet(globalThis.sessionStorage ?? {getItem: () => null}, 'ncl-session', null),
  records: [],
  cleanups: [],
  capture: null,
  camera: {stream: null, facing: 'environment', recorder: null, timer: null, seconds: 0, mode: 'photo', gps: null, locating: false, error: null}
};

export const MAX_RECORDING_SECONDS = 30;

export function setSession(session) {
  state.session = session;
  try {
    if (session) sessionStorage.setItem('ncl-session', JSON.stringify(session));
    else sessionStorage.removeItem('ncl-session');
  } catch { /* session persistence is best-effort */ }
}

/** Viewer identity used for every projection. Anonymous visitors are role 'public'. */
export function viewer() {
  return state.session
    ? {role: state.session.role, email: state.session.email, name: state.session.name}
    : {role: 'public', email: null, name: null};
}

export function addCleanup(fn) { state.cleanups.push(fn); }
export function runCleanups() {
  while (state.cleanups.length) {
    try { state.cleanups.pop()(); } catch { /* ignore disposal errors */ }
  }
}

const ls = () => {
  try { return localStorage; } catch { return null; }
};

export function readPrefs() {
  const store = ls();
  const key = 'ncl-prefs-' + (state.session?.email || 'anonymous');
  return {updates: true, verification: true, hotspots: false, ...(store ? storageGet(store, key, {}) : {})};
}
export function writePrefs(prefs) {
  const store = ls();
  return store ? storageSet(store, 'ncl-prefs-' + (state.session?.email || 'anonymous'), prefs) : false;
}
export function readLastRead() {
  const store = ls();
  return store ? storageGet(store, 'ncl-read-' + (state.session?.email || 'anonymous'), null) : null;
}
export function writeLastRead(iso) {
  const store = ls();
  return store ? storageSet(store, 'ncl-read-' + (state.session?.email || 'anonymous'), iso) : false;
}
