import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

// Browser-storage shim for Node, as in services.test.js.
const storage = new Map();
const shim = {getItem: k => (storage.has(k) ? storage.get(k) : null), setItem: (k, v) => storage.set(k, String(v)), removeItem: k => storage.delete(k)};
globalThis.localStorage = shim;
globalThis.sessionStorage = shim;

const {api, mediaDB} = await import('../dist/services.js');
const {parseApiConfig, loadApiConfig, resetApiConfig, DEFAULT_BASE_URL} = await import('../dist/api-config.js');
const {createApiClient, toCreatePayload, ApiError} = await import('../dist/api-client.js');
const {submitReport, trackPublic, takeServerReceipt, reportMode, serverEvidenceKey} = await import('../dist/report-gateway.js');

const media = new Map();
mediaDB.put = async (k, v) => { media.set(k, v); };
mediaDB.get = async k => media.get(k) ?? null;
mediaDB.delete = async k => { media.delete(k); };
mediaDB.clear = async () => media.clear();

const BASE = 'http://localhost:8000/api/v1';
const BACKEND = {mode: 'backend', baseUrl: BASE, problem: null};
const LOCAL = {mode: 'local', baseUrl: null, problem: null};
const CLAIM = '6f1c2a7e-0d4b-4f6e-9a3b-2c8d1e5f7a90';
const now = Date.now();

const report = (overrides = {}) => ({
  category: 'Potholes',
  description: '  Deep pothole near the bus stop  ',
  gps: {lat: 13.0358, lng: 77.597, accuracy: 12, timestamp: now - 4000, source: 'Device GPS', sourceType: 'gps', fixAgeSeconds: 4, neighbourhood: 'Hebbal', address: '1 Example Road'},
  neighbourhood: 'Hebbal',
  created: new Date(now - 3000).toISOString(),
  uploadedAt: new Date(now).toISOString(),
  owner: 'resident@civiclens.demo',
  mediaType: 'photo',
  shareEvidence: true,
  captureSource: 'Live camera',
  ...overrides
});

const serverReceipt = (id = 'NCL-2026-K7Q2MX', extra = {}) => ({
  id, claim: CLAIM, category: 'Potholes', area: 'Hebbal', created: '2026-10-30T04:00:00.000Z', capturedAt: '2026-10-30T04:00:00.000Z',
  uploadedAt: '2026-10-30T04:00:01.000Z', captureSource: 'Live camera',
  gps: {lat: 13.0358, lng: 77.597, accuracy: 12, source: 'Device GPS', sourceType: 'gps', timestamp: '2026-10-30T03:59:56.000Z', address: null},
  mediaType: 'photo', shareEvidence: true, status: 'Submitted', verification: 'Pending Verification', ...extra
});

const publicBody = (id = 'NCL-2026-K7Q2MX', extra = {}) => ({
  id, category: 'Potholes', area: 'Hebbal', status: 'Submitted', verification: 'Pending Verification', department: 'Awaiting civic review',
  updated: '2026-10-30T04:00:01.000Z', timeline: [{status: 'Submitted', text: 'Report received by the Namma CivicLens API.', date: '2026-10-30T04:00:01.000Z'}],
  verificationLog: [], ...extra
});

const json = (status, body) => new Response(body === undefined ? '' : JSON.stringify(body), {status, headers: {'Content-Type': 'application/json'}});

/** A fetch mock that records calls and answers from a queue of handlers. */
function mockFetch(...handlers) {
  const calls = [];
  const fn = async (url, init = {}) => {
    calls.push({url, init, body: init.body === undefined ? undefined : JSON.parse(init.body)});
    const h = handlers[Math.min(calls.length - 1, handlers.length - 1)];
    return typeof h === 'function' ? h(url, init) : h;
  };
  fn.calls = calls;
  return fn;
}

const networkDown = () => { throw new TypeError('Failed to fetch'); };
const hangUntilAborted = (url, init) => new Promise((_, reject) => init.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError'))));

/** Captures every console call made while `fn` runs. */
async function captureConsole(fn) {
  const seen = [];
  const methods = ['log', 'info', 'warn', 'error', 'debug'];
  const saved = methods.map(m => console[m]);
  methods.forEach(m => { console[m] = (...args) => seen.push(args.map(String).join(' ')); });
  try { await fn(); } finally { methods.forEach((m, i) => { console[m] = saved[i]; }); }
  return seen;
}

const storageDump = () => [...storage.values()].join('\n');
const localRecordCount = () => JSON.parse(localStorage.getItem('ncl-records-v2') || '[]').length;

// ---------- Configuration ----------

test('missing configuration means local mode', async () => {
  assert.deepEqual(parseApiConfig(null), LOCAL);
  assert.deepEqual(parseApiConfig(undefined), LOCAL);
  assert.equal(parseApiConfig({}).mode, 'local', 'an object without "mode" is local');
  resetApiConfig();
  assert.equal((await loadApiConfig(async () => new Response('', {status: 404}))).mode, 'local', 'no api-config.json file');
  resetApiConfig();
  assert.equal((await loadApiConfig(networkDown)).mode, 'local', 'config unreachable');
  resetApiConfig();
});

test('the shipped api-config.json has a valid configuration', () => {
  const shipped = JSON.parse(readFileSync(new URL('../dist/api-config.json', import.meta.url), 'utf8'));
  const parsed = parseApiConfig(shipped);
  assert.ok(['local', 'backend'].includes(parsed.mode));
  assert.equal(parsed.problem, null);
});

test('backend mode is used only when explicitly configured with a valid URL', async () => {
  assert.deepEqual(parseApiConfig({mode: 'backend'}), {mode: 'backend', baseUrl: DEFAULT_BASE_URL, problem: null});
  assert.equal(parseApiConfig({mode: 'backend', baseUrl: 'https://api.example.org/api/v1/'}).baseUrl, 'https://api.example.org/api/v1', 'trailing slash trimmed');
  resetApiConfig();
  const loaded = await loadApiConfig(async () => json(200, {mode: 'backend', baseUrl: BASE}));
  assert.equal(loaded.mode, 'backend');
  resetApiConfig();
});

test('invalid configuration never switches to backend mode', async () => {
  const bad = [
    'backend', [], 42, {mode: 'Backend'}, {mode: 'remote'}, {mode: true},
    {mode: 'backend', baseUrl: 'ftp://localhost/api'}, {mode: 'backend', baseUrl: 'javascript:alert(1)'},
    {mode: 'backend', baseUrl: '/api/v1'}, {mode: 'backend', baseUrl: 'http://user:pass@localhost:8000/api/v1'},
    {mode: 'backend', baseUrl: 'http://localhost:8000/api/v1?x=1'}, {mode: 'backend', baseUrl: 7}
  ];
  for (const raw of bad) {
    const parsed = parseApiConfig(raw);
    assert.equal(parsed.mode, 'invalid', JSON.stringify(raw));
    assert.equal(parsed.baseUrl, null);
    assert.match(parsed.problem, /configuration is invalid/);
  }
  resetApiConfig();
  assert.equal((await loadApiConfig(async () => new Response('{mode: backend', {status: 200}))).mode, 'invalid', 'malformed JSON');
  resetApiConfig();
  assert.equal((await loadApiConfig(async () => new Response('', {status: 500}))).mode, 'invalid', 'unreadable file');
  resetApiConfig();
});

// ---------- API adapter ----------

test('health check reads the server status', async () => {
  const fetchImpl = mockFetch(json(200, {status: 'ok', service: 'namma-civiclens-api', api_version: 'v1', version: '0.1.0'}));
  assert.deepEqual(await createApiClient({baseUrl: BASE, fetchImpl}).health(), {status: 'ok', apiVersion: 'v1', version: '0.1.0'});
  assert.equal(fetchImpl.calls[0].url, BASE + '/health');
  assert.equal(fetchImpl.calls[0].init.method, 'GET');
});

test('complaint creation posts JSON without credentials and returns the receipt', async () => {
  const fetchImpl = mockFetch(json(201, serverReceipt()));
  const payload = toCreatePayload(report());
  const receipt = await createApiClient({baseUrl: BASE, fetchImpl}).createComplaint(payload);
  const [call] = fetchImpl.calls;
  assert.equal(call.url, BASE + '/complaints');
  assert.equal(call.init.method, 'POST');
  assert.equal(call.init.headers['Content-Type'], 'application/json');
  assert.equal(call.init.credentials, 'omit');
  assert.deepEqual(call.body, payload);
  assert.equal(receipt.id, 'NCL-2026-K7Q2MX');
  assert.equal(receipt.claim, CLAIM);
});

test('public tracking returns only allowlisted fields, even if the server sends more', async () => {
  const leaky = publicBody('NCL-2026-K7Q2MX', {claim: CLAIM, claimHash: 'scrypt$x', gps: {lat: 13}, owner: 'x@example.com', internal: 'note'});
  const record = await createApiClient({baseUrl: BASE, fetchImpl: mockFetch(json(200, leaky))}).getPublicComplaint('NCL-2026-K7Q2MX');
  assert.deepEqual(Object.keys(record).sort(), ['area', 'category', 'department', 'id', 'mergedInto', 'status', 'timeline', 'updated', 'verification', 'verificationLog']);
  assert.equal(record.mergedInto, null);
  for (const secret of [CLAIM, 'scrypt', 'owner', 'internal', '"lat"']) assert.ok(!JSON.stringify(record).includes(secret), secret);
});

test('404 not_found means no record; 422 keeps the error envelope and field details', async () => {
  const notFound = mockFetch(json(404, {error: {code: 'not_found', message: 'No complaint was found with that ID.'}}));
  assert.equal(await createApiClient({baseUrl: BASE, fetchImpl: notFound}).getPublicComplaint('NCL-2026-ZZZZZZ'), null);
  assert.equal(notFound.calls[0].url, BASE + '/complaints/NCL-2026-ZZZZZZ');

  const invalid = mockFetch(json(422, {error: {code: 'validation_error', message: 'The request is not valid.', details: [{field: 'body.gps.lat', message: 'Input should be less than or equal to 90'}]}}));
  await assert.rejects(createApiClient({baseUrl: BASE, fetchImpl: invalid}).createComplaint({}), err => {
    assert.ok(err instanceof ApiError);
    assert.equal(err.status, 422);
    assert.equal(err.code, 'validation_error');
    assert.equal(err.message, 'The request is not valid.');
    assert.deepEqual(err.details, [{field: 'body.gps.lat', message: 'Input should be less than or equal to 90'}]);
    assert.equal(err.ambiguous, false, 'the server answered, so the outcome is known');
    return true;
  });
});

test('an unreachable backend is reported, and a failed POST is marked ambiguous and not retried', async () => {
  const down = mockFetch(networkDown);
  const client = createApiClient({baseUrl: BASE, fetchImpl: down});
  await assert.rejects(client.createComplaint(toCreatePayload(report())), err => err.code === 'network_error' && err.ambiguous === true);
  assert.equal(down.calls.length, 1, 'creation is never retried automatically');
  await assert.rejects(client.getPublicComplaint('NCL-2026-K7Q2MX'), err => err.code === 'network_error' && err.ambiguous === false);
});

test('requests time out', async () => {
  const client = createApiClient({baseUrl: BASE, fetchImpl: mockFetch(hangUntilAborted), timeoutMs: 30});
  await assert.rejects(client.health(), err => err.code === 'timeout' && /did not answer in time/.test(err.message));
  await assert.rejects(client.createComplaint(toCreatePayload(report())), err => err.code === 'timeout' && err.ambiguous === true);
});

test('malformed responses are rejected', async () => {
  const notJson = mockFetch(new Response('<html>oops</html>', {status: 200}));
  await assert.rejects(createApiClient({baseUrl: BASE, fetchImpl: notJson}).health(), err => err.code === 'malformed_response');
  const noClaim = mockFetch(json(201, {...serverReceipt(), claim: undefined}));
  await assert.rejects(createApiClient({baseUrl: BASE, fetchImpl: noClaim}).createComplaint({}), err => err.code === 'malformed_response' && err.ambiguous);
  const badId = mockFetch(json(201, serverReceipt('not-an-id')));
  await assert.rejects(createApiClient({baseUrl: BASE, fetchImpl: badId}).createComplaint({}), err => err.code === 'malformed_response');
  const htmlError = mockFetch(new Response('<h1>Bad gateway</h1>', {status: 502}));
  await assert.rejects(createApiClient({baseUrl: BASE, fetchImpl: htmlError}).health(), err => err.status === 502 && err.code === 'http_error' && !/<h1>/.test(err.message));
  const noTimeline = mockFetch(json(200, {id: 'NCL-2026-K7Q2MX'}));
  await assert.rejects(createApiClient({baseUrl: BASE, fetchImpl: noTimeline}).getPublicComplaint('NCL-2026-K7Q2MX'), err => err.code === 'malformed_response');
});

test('the adapter writes nothing to the console, so sensitive values cannot leak into logs', async () => {
  const seen = await captureConsole(async () => {
    const payload = toCreatePayload(report());
    await createApiClient({baseUrl: BASE, fetchImpl: mockFetch(json(201, serverReceipt()))}).createComplaint(payload);
    await createApiClient({baseUrl: BASE, fetchImpl: mockFetch(networkDown)}).createComplaint(payload).catch(() => {});
    await createApiClient({baseUrl: BASE, fetchImpl: mockFetch(json(422, {error: {code: 'validation_error', message: 'x', details: []}}))}).createComplaint(payload).catch(() => {});
    await createApiClient({baseUrl: BASE, fetchImpl: mockFetch(new Response('junk', {status: 200}))}).getPublicComplaint('NCL-2026-K7Q2MX').catch(() => {});
    await submitReport(report(), new Blob(['jpeg']), {config: BACKEND, fetchImpl: mockFetch(json(201, serverReceipt('NCL-2026-LOGCHK')))});
    takeServerReceipt('NCL-2026-LOGCHK');
  });
  assert.deepEqual(seen, []);
});

test('error messages never contain the claim code, coordinates or description', async () => {
  const echoing = mockFetch(json(422, {error: {code: 'validation_error', message: 'The request is not valid.', details: [{field: 'body.description', message: 'String should have at most 1000 characters'}]}}));
  const err = await createApiClient({baseUrl: BASE, fetchImpl: echoing}).createComplaint(toCreatePayload(report())).catch(e => e);
  const text = JSON.stringify({message: err.message, details: err.details});
  for (const secret of ['Deep pothole', '13.0358', '77.597', CLAIM]) assert.ok(!text.includes(secret), secret);
});

// ---------- Request mapping ----------

test('the request mapper sends only API fields and drops owner, uploadedAt and media', () => {
  const payload = toCreatePayload(report({blob: new Blob(['x']), file: new Blob(['y']), id: 'NCL-2026-FAKE01', claim: 'client-made', gps: {...report().gps, quality: 'good', extra: 1}}));
  assert.deepEqual(Object.keys(payload).sort(), ['captureSource', 'capturedAt', 'category', 'description', 'gps', 'mediaType', 'neighbourhood', 'shareEvidence']);
  assert.deepEqual(Object.keys(payload.gps).sort(), ['accuracy', 'address', 'fixAgeSeconds', 'lat', 'lng', 'neighbourhood', 'source', 'sourceType', 'timestamp']);
  assert.equal(payload.owner, undefined);
  assert.equal(payload.uploadedAt, undefined);
  assert.equal(payload.description, 'Deep pothole near the bus stop');
  const wire = JSON.stringify(payload);
  assert.deepEqual(JSON.parse(wire), payload, 'payload is plain JSON (no Blob or other objects)');
  for (const leaked of ['resident@civiclens.demo', 'client-made', 'NCL-2026-FAKE01', 'quality']) assert.ok(!wire.includes(leaked), leaked);
});

test('timestamps are mapped to the formats the API expects', () => {
  const base = report();
  assert.equal(toCreatePayload(base).capturedAt, new Date(now - 3000).toISOString(), '`created` becomes `capturedAt`');
  assert.match(toCreatePayload(base).capturedAt, /Z$/);
  assert.equal(toCreatePayload(report({created: undefined, capturedAt: '2026-10-30T09:30:00+05:30'})).capturedAt, '2026-10-30T04:00:00.000Z');
  assert.equal(toCreatePayload(report({created: '2026-10-30T09:30:00'})).capturedAt, undefined, 'a time without a zone is not guessed');
  assert.equal(toCreatePayload(report({created: 'not a date'})).capturedAt, undefined);
  assert.equal(toCreatePayload(report({created: null})).capturedAt, undefined);
  assert.equal(toCreatePayload(base).gps.timestamp, now - 4000, 'fix time stays epoch milliseconds');
  assert.equal(toCreatePayload(report({gps: {...base.gps, timestamp: 1793311200000.6}})).gps.timestamp, 1793311200001);
  assert.equal(toCreatePayload(report({gps: {...base.gps, timestamp: '2026-10-30T04:00:00.000Z'}})).gps.timestamp, Date.parse('2026-10-30T04:00:00.000Z'));
  assert.equal(toCreatePayload(report({gps: {...base.gps, timestamp: null, sourceType: 'exif', accuracy: null}})).gps.timestamp, null);
});

test('EXIF gallery locations keep their source and a null accuracy', () => {
  const gps = {lat: 12.97, lng: 77.59, accuracy: null, source: 'Submitted EXIF metadata', sourceType: 'exif', timestamp: null};
  assert.deepEqual(toCreatePayload(report({gps})).gps, {lat: 12.97, lng: 77.59, accuracy: null, sourceType: 'exif', timestamp: null, source: 'Submitted EXIF metadata'});
});

test('reports without a usable GPS fix are refused, never given invented coordinates', () => {
  for (const gps of [null, undefined, {}, {lat: NaN, lng: 77, sourceType: 'gps'}, {lat: '13', lng: '77', sourceType: 'gps'}, {lat: 95, lng: 77, sourceType: 'gps'}, {lat: 13, lng: 77}, {lat: 13, lng: 77, sourceType: 'wifi'}]) {
    assert.throws(() => toCreatePayload(report({gps})), err => err instanceof ApiError && err.code === 'missing_location', JSON.stringify(gps));
  }
});

// ---------- Integration through the gateway ----------

test('local mode keeps the existing creation flow and never calls the network', async () => {
  await api.reset();
  const fetchImpl = mockFetch(() => { throw new Error('network must not be used'); });
  const before = localRecordCount();
  const blob = new Blob(['photo'], {type: 'image/jpeg'});
  const saved = await submitReport(report(), blob, {config: LOCAL, fetchImpl});
  assert.equal(saved.mode, 'local');
  assert.equal(fetchImpl.calls.length, 0);
  assert.equal(localRecordCount(), before + 1);
  const receipt = await api.receipt(saved.id);
  assert.ok(receipt.claim, 'local receipts still carry the locally generated claim code');
  assert.equal(media.get(saved.id), blob, 'local evidence keyed by the bare ID, as before');
  assert.equal(takeServerReceipt(saved.id), null, 'no server receipt in local mode');
});

test('existing demo records still work in local mode', async () => {
  await api.reset();
  const {mode, record, error} = await trackPublic('NCL-2026-DEMO01', {config: LOCAL});
  assert.equal(mode, 'local');
  assert.equal(error, null);
  assert.equal(record.id, 'NCL-2026-DEMO01');
  assert.equal(record.claim, undefined);
  assert.equal(await reportMode({config: LOCAL}), 'local');
});

test('backend mode sends mapped metadata, keeps evidence locally and holds the receipt in memory once', async () => {
  await api.reset();
  const before = localRecordCount();
  const fetchImpl = mockFetch(json(201, serverReceipt('NCL-2026-SRV001')));
  const blob = new Blob(['jpeg-bytes'], {type: 'image/jpeg'});
  const saved = await submitReport(report(), blob, {config: BACKEND, fetchImpl});
  assert.deepEqual(saved, {mode: 'backend', id: 'NCL-2026-SRV001'});

  assert.equal(fetchImpl.calls.length, 1);
  const sent = fetchImpl.calls[0].body;
  assert.equal(sent.owner, undefined);
  assert.equal(sent.uploadedAt, undefined);
  assert.equal(sent.capturedAt, new Date(now - 3000).toISOString());
  assert.ok(!fetchImpl.calls[0].init.body.includes('jpeg-bytes'), 'media bytes are not sent');

  assert.equal(media.get(serverEvidenceKey('NCL-2026-SRV001')), blob, 'evidence kept in IndexedDB under a server-evidence key');
  assert.equal(media.get('NCL-2026-SRV001'), undefined, 'never under the bare ID, so local evidence cannot be overwritten');
  assert.equal(localRecordCount(), before, 'no local complaint record is created');
  assert.ok(!storageDump().includes(CLAIM), 'claim code is not written to localStorage or sessionStorage');

  const held = takeServerReceipt('NCL-2026-SRV001');
  assert.equal(held.receipt.claim, CLAIM, 'the server-generated claim code is used');
  assert.equal(held.evidenceKept, true);
  assert.equal(takeServerReceipt('NCL-2026-SRV001'), null, 'the receipt can be taken only once');
});

test('a failed server creation shows no success receipt and saves nothing anywhere', async () => {
  await api.reset();
  const before = localRecordCount();
  const failures = [
    json(422, {error: {code: 'validation_error', message: 'The request is not valid.', details: []}}),
    json(503, {error: {code: 'storage_unavailable', message: 'The complaint could not be saved or read right now. Try again shortly.'}}),
    networkDown
  ];
  for (const failure of failures) {
    await assert.rejects(submitReport(report(), new Blob(['x']), {config: BACKEND, fetchImpl: mockFetch(failure)}), ApiError);
  }
  assert.equal(localRecordCount(), before, 'no silent fallback to local storage');
  assert.equal([...media.keys()].filter(k => k.startsWith('server-evidence:')).length, 0);
  assert.equal(takeServerReceipt('NCL-2026-K7Q2MX'), null);
});

test('backend mode refuses a report without GPS before contacting the server', async () => {
  const fetchImpl = mockFetch(json(201, serverReceipt()));
  await assert.rejects(submitReport(report({gps: null}), new Blob(['x']), {config: BACKEND, fetchImpl}), err => err.code === 'missing_location');
  assert.equal(fetchImpl.calls.length, 0);
});

test('invalid configuration blocks reporting and tracking instead of choosing a store', async () => {
  await api.reset();
  const before = localRecordCount();
  const config = parseApiConfig({mode: 'remote'});
  const fetchImpl = mockFetch(json(201, serverReceipt()));
  await assert.rejects(submitReport(report(), new Blob(['x']), {config, fetchImpl}), err => err.code === 'config_invalid');
  assert.equal(fetchImpl.calls.length, 0);
  assert.equal(localRecordCount(), before);
  const tracked = await trackPublic('NCL-2026-DEMO01', {config, fetchImpl});
  assert.equal(tracked.record, null, 'the local demo record is not shown');
  assert.match(tracked.error, /configuration is invalid/);
});

test('if the evidence file cannot be kept locally, the server receipt still says so honestly', async () => {
  const realPut = mediaDB.put;
  mediaDB.put = async () => { throw new Error('quota'); };
  try {
    const saved = await submitReport(report(), new Blob(['x']), {config: BACKEND, fetchImpl: mockFetch(json(201, serverReceipt('NCL-2026-NOFILE')))});
    assert.equal(takeServerReceipt(saved.id).evidenceKept, false);
  } finally {
    mediaDB.put = realPut;
  }
});

test('backend tracking uses the server and never falls back to local data', async () => {
  await api.reset();
  assert.ok(await api.publicRecord('NCL-2026-DEMO01'), 'precondition: the demo record exists locally');

  const notFound = mockFetch(json(404, {error: {code: 'not_found', message: 'No complaint was found with that ID.'}}));
  const missing = await trackPublic('NCL-2026-DEMO01', {config: BACKEND, fetchImpl: notFound});
  assert.deepEqual(missing, {mode: 'backend', record: null, error: null});
  assert.equal(notFound.calls[0].url, BASE + '/complaints/NCL-2026-DEMO01');

  const down = await trackPublic('NCL-2026-DEMO01', {config: BACKEND, fetchImpl: mockFetch(networkDown)});
  assert.equal(down.record, null);
  assert.match(down.error, /could not be reached/);

  const serverError = await trackPublic('NCL-2026-DEMO01', {config: BACKEND, fetchImpl: mockFetch(json(503, {error: {code: 'storage_unavailable', message: 'The complaint could not be saved or read right now. Try again shortly.'}}))});
  assert.equal(serverError.record, null);
  assert.match(serverError.error, /Try again shortly/);
});

test('backend tracking shows the server record and never a claim code', async () => {
  const leaky = publicBody('NCL-2026-DEMO01', {claim: CLAIM, claimHash: 'scrypt$16384$8$1$abc$def'});
  const {record, error} = await trackPublic('NCL-2026-DEMO01', {config: BACKEND, fetchImpl: mockFetch(json(200, leaky))});
  assert.equal(error, null);
  assert.equal(record.id, 'NCL-2026-DEMO01', 'demo-style IDs are looked up on the server');
  assert.equal(record.status, 'Submitted');
  const shown = JSON.stringify(record);
  assert.ok(!shown.includes(CLAIM) && !shown.includes('scrypt'));
});
