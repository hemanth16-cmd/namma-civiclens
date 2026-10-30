// Chooses between local mode (browser storage, the default) and backend mode (the CivicLens API).
// Read from api-config.json next to index.html. Backend mode is used only when the file says so
// explicitly with a valid http(s) baseUrl. A missing file means local mode. A file that is present
// but invalid gives mode "invalid", which blocks reporting and tracking with an error rather than
// silently picking either store.

export const DEFAULT_BASE_URL = 'http://localhost:8000/api/v1';
const CONFIG_URL = 'api-config.json';

export function parseApiConfig(raw) {
  if (raw == null) return {mode: 'local', baseUrl: null, problem: null};
  if (typeof raw !== 'object' || Array.isArray(raw)) return invalid('api-config.json must contain a JSON object.');
  const mode = raw.mode ?? 'local';
  if (mode === 'local') return {mode: 'local', baseUrl: null, problem: null};
  if (mode !== 'backend') return invalid('"mode" must be "local" or "backend".');
  const baseUrl = normaliseBaseUrl(raw.baseUrl ?? DEFAULT_BASE_URL);
  if (!baseUrl) return invalid('"baseUrl" must be an absolute http(s) URL without credentials, query or fragment.');
  return {mode: 'backend', baseUrl, problem: null};
}

function invalid(problem) {
  return {mode: 'invalid', baseUrl: null, problem: 'The CivicLens API configuration is invalid: ' + problem};
}

function normaliseBaseUrl(value) {
  if (typeof value !== 'string') return null;
  let url;
  try { url = new URL(value); } catch { return null; }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) return null;
  return url.href.replace(/\/+$/, '');
}

let pending = null;

/** Loads and caches the configuration. `fetchImpl` is injectable for tests. */
export function loadApiConfig(fetchImpl = globalThis.fetch) {
  if (!pending) pending = readConfig(fetchImpl);
  return pending;
}

async function readConfig(fetchImpl) {
  let response;
  try {
    response = await fetchImpl(CONFIG_URL, {cache: 'no-cache'});
  } catch {
    return parseApiConfig(null);
  }
  if (response.status === 404) return parseApiConfig(null);
  if (!response.ok) return invalid(`api-config.json could not be read (HTTP ${response.status}).`);
  try {
    return parseApiConfig(JSON.parse(await response.text()));
  } catch {
    return invalid('api-config.json is not valid JSON.');
  }
}

/** Tests only: forget the cached configuration. */
export function resetApiConfig() {
  pending = null;
}
