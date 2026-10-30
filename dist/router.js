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
  return location.hash.replace(/^#\/?/, '') || 'camera';
}

export function navigate(path) {
  const target = '#/' + path.replace(/^\/+/, '');
  if (location.hash === target) return false;
  location.hash = target;
  return true;
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

/** Where a demonstration-workspace route leads in server mode, or null to keep it. */
export function serverModeRoute(parts) {
  const [head, second] = parts;
  if (head === 'official') return second === 'video-review' ? null : 'staff';  // video review never leaves this browser
  if (head === 'resident') return second === 'notifications' ? 'notifications' : 'my-reports';
  if (head === 'issue') return second ? 'track/' + second : 'my-reports';
  return null;
}
