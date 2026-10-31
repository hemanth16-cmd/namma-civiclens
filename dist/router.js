// Hash router with an in-app history stack. Back returns to the previous in-app screen;
// direct links (no in-app history) fall back to a safe screen chosen by the caller.

const HISTORY_KEY = 'ncl-route-history';
const MAX_HISTORY = 40;

function loadHistory() {
  try {
    const parsed = JSON.parse(sessionStorage.getItem(HISTORY_KEY) || '[]');
    return Array.isArray(parsed) ? parsed.filter(x => typeof x === 'string') : [];
  } catch { return []; }
}
let history = loadHistory();
function saveHistory() {
  try { sessionStorage.setItem(HISTORY_KEY, JSON.stringify(history)); } catch { /* best-effort */ }
}

export function currentPath() {
  if (typeof location === 'undefined') return 'camera';
  return location.hash.replace(/^#\/?/, '') || 'camera';
}

export function navigate(path) {
  const target = '#/' + path.replace(/^\/+/, '');
  if (location.hash === target) return false;
  location.hash = target;
  return true;
}

// Where a resident was going when sign-in was required (the session lives in memory, so a refresh asks
// again). Only a resident route path is kept, in sessionStorage; never a token or anything private.
const NEXT_KEY = 'ncl-resident-next';
const RESIDENT_ROUTE = /^resident(\/[A-Za-z0-9-]+){0,2}$/;

export function rememberResidentDestination(path) {
  try { if (RESIDENT_ROUTE.test(path)) sessionStorage.setItem(NEXT_KEY, path); } catch { /* storage blocked: sign-in lands on the overview */ }
}

/** The remembered resident route (cleared once read), or 'resident'. */
export function takeResidentDestination() {
  let path = null;
  try { path = sessionStorage.getItem(NEXT_KEY); sessionStorage.removeItem(NEXT_KEY); } catch { /* storage blocked */ }
  return path && RESIDENT_ROUTE.test(path) ? path : 'resident';
}

/** Record a rendered route. Stepping back to the previous entry pops it instead of pushing. */
export function recordRoute(path) {
  if (history.at(-1) === path) return;
  if (history.at(-2) === path) history.pop();
  else history.push(path);
  history = history.slice(-MAX_HISTORY);
  saveHistory();
}

export function previousRoute() {
  const current = currentPath();
  return [...history].reverse().find(p => p !== current) ?? null;
}

export function goBack(fallback) {
  const previous = previousRoute();
  if (previous) navigate(previous);
  else navigate(fallback);
}

// The application registers its render function here so views can refresh after a change.
let renderer = null;
export function setRenderer(fn) { renderer = fn; }
export function rerender() { return renderer?.(); }

/**
 * Where a workspace route leads in server mode, or null to keep it. The admin workspace stays (its screens
 * read server complaints for signed-in staff) and a case opens in the original case page built from the
 * server. Everyone else opening a case gets public tracking.
 */
export function serverModeRoute(parts, {staff = false} = {}) {
  const [head, second] = parts;
  if (head === 'official') return null;
  if (head === 'resident') return second === 'notifications' ? 'notifications' : null;  // app.js admits only a signed-in server resident
  if (head === 'issue') return staff ? (second ? null : 'official/complaints') : (second ? 'track/' + second : 'resident');
  return null;
}

/**
 * Who may open a server-mode workspace route. Pure, so it can be tested: `staff` and `resident` are the
 * in-memory server sessions (or null) and `session` the display identity from state.js. Returns
 * {redirect, clear, identity}: `clear` drops the display identity, `identity` replaces it. A URL alone
 * never grants a role: only a session the server issued does.
 */
export function serverWorkspaceAccess(head, {staff = null, resident = null, session = null} = {}) {
  const role = session?.role ?? 'public';
  if (head === 'issue' && !staff && session?.server && role === 'official') return {redirect: 'login/official', clear: true};
  if (head === 'official' || (head === 'issue' && staff)) {
    if (!staff) return {redirect: 'login/official', clear: role === 'official'};
    if (role !== 'official' || !session?.server) return {identity: {role: 'official', name: staff.staff.displayName, email: staff.staff.username, server: true}};
  }
  if (head === 'resident') {
    if (!resident) return {redirect: 'login/resident', clear: role === 'resident'};
    if (role !== 'resident' || !session?.server) return {identity: {role: 'resident', name: resident.resident.displayName, email: resident.resident.email, server: true}};
  }
  return {};
}
