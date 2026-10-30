import {test} from 'node:test';
import assert from 'node:assert/strict';

// Browser-storage shim for Node, as in services.test.js.
const storage = new Map();
const shim = {getItem: k => (storage.has(k) ? storage.get(k) : null), setItem: (k, v) => storage.set(k, String(v)), removeItem: k => storage.delete(k)};
globalThis.localStorage = shim;
globalThis.sessionStorage = shim;

const {api, mediaDB} = await import('../dist/services.js');
const {createApiClient, evidenceFilename, ApiError} = await import('../dist/api-client.js');
const {submitReport, takeServerReceipt, uploadEvidence, uploadState, forgetUpload, trackPublic, serverEvidenceKey} = await import('../dist/report-gateway.js');

const media = new Map();
const deleted = [];
mediaDB.put = async (k, v) => { media.set(k, v); };
mediaDB.get = async k => media.get(k) ?? null;
mediaDB.delete = async k => { deleted.push(k); media.delete(k); };
mediaDB.clear = async () => media.clear();

const BASE = 'http://localhost:8000/api/v1';
const BACKEND = {mode: 'backend', baseUrl: BASE, problem: null};
const LOCAL = {mode: 'local', baseUrl: null, problem: null};
const CLAIM = '0b8f7d2a-3c4e-4a1b-9f6d-5e7c8a9b0d1e';
const TOKEN = 'tok_' + 'A'.repeat(40);
const FRESH = 'tok_' + 'B'.repeat(40);
const SHA = 'a'.repeat(64);
const now = Date.now();

const report = (overrides = {}) => ({
  category: 'Potholes', description: 'Demo pothole', mediaType: 'photo', shareEvidence: false, neighbourhood: 'Hebbal',
  gps: {lat: 13.0358, lng: 77.597, accuracy: 12, timestamp: now - 4000, source: 'Device GPS', sourceType: 'gps'},
  created: new Date(now - 3000).toISOString(), uploadedAt: new Date(now).toISOString(), owner: 'resident@civiclens.demo', ...overrides
});
const photo = () => new Blob([new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 1, 2, 3, 0xff, 0xd9])], {type: 'image/jpeg'});

const receiptBody = id => ({
  id, claim: CLAIM, category: 'Potholes', area: 'Hebbal', created: '2026-10-01T04:00:00.000Z', capturedAt: '2026-10-01T04:00:00.000Z',
  uploadedAt: '2026-10-01T04:00:01.000Z', captureSource: 'Live camera',
  gps: {lat: 13.0358, lng: 77.597, accuracy: 12, source: 'Device GPS', sourceType: 'gps', timestamp: '2026-10-01T03:59:56.000Z', address: null},
  mediaType: 'photo', shareEvidence: false, status: 'Submitted', verification: 'Pending Verification',
  uploadToken: TOKEN, uploadTokenExpiresAt: '2099-01-01T00:00:00.000Z'
});
const evidenceBody = (status = 'uploaded') => ({evidenceId: 'ev_demo123', status, mediaType: 'photo', contentType: 'image/jpeg', byteSize: 9, sha256: SHA, capturedAt: null, uploadedAt: '2026-10-01T04:00:02.000Z'});
const json = (status, body) => new Response(JSON.stringify(body), {status, headers: {'Content-Type': 'application/json'}});
const errorBody = (code, message) => ({error: {code, message}});
const networkDown = () => { throw new TypeError('Failed to fetch'); };
const hangUntilAborted = (url, init) => new Promise((_, reject) => init.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError'))));

/**
 * Routes calls by endpoint. Each route is a queue of handlers; the last one repeats.
 * routes: {create, upload, token}
 */
function server(routes) {
  const calls = [];
  const queues = Object.fromEntries(Object.entries(routes).map(([k, v]) => [k, Array.isArray(v) ? [...v] : [v]]));
  const fn = async (url, init = {}) => {
    const kind = url.endsWith('/evidence') ? 'upload' : url.endsWith('/upload-token') ? 'token' : url.endsWith('/complaints') ? 'create' : 'get';
    calls.push({kind, url, init});
    const queue = queues[kind];
    if (!queue) throw new Error('unexpected request to ' + url);
    const handler = queue.length > 1 ? queue.shift() : queue[0];
    return typeof handler === 'function' ? handler(url, init) : handler.clone();
  };
  fn.calls = calls;
  fn.count = kind => calls.filter(c => c.kind === kind).length;
  return fn;
}

async function createServerReport(id, fetchImpl, blob = photo()) {
  const saved = await submitReport(report(), blob, {config: BACKEND, fetchImpl});
  assert.equal(saved.id, id);
  return saved;
}

const storageDump = () => [...storage.values()].join('\n');

// ---------- API adapter ----------

test('uploadEvidence sends one multipart file with the token in the Authorization header', async () => {
  const fetchImpl = server({upload: json(201, evidenceBody())});
  const result = await createApiClient({baseUrl: BASE, fetchImpl}).uploadEvidence('NCL-2026-UPL001', photo(), {token: TOKEN, capturedAt: '2026-10-01T04:00:00.000Z'});
  const [call] = fetchImpl.calls;
  assert.equal(call.url, BASE + '/complaints/NCL-2026-UPL001/evidence');
  assert.equal(call.init.method, 'POST');
  assert.equal(call.init.headers.Authorization, 'Bearer ' + TOKEN);
  assert.equal(call.init.headers['Content-Type'], undefined, 'the browser sets the multipart boundary');
  assert.ok(call.init.body instanceof FormData);
  const file = call.init.body.get('file');
  assert.equal(file.name, 'evidence.jpg');
  assert.equal(file.size, 9);
  assert.equal(call.init.body.get('capturedAt'), '2026-10-01T04:00:00.000Z');
  assert.deepEqual([...call.init.body.keys()].sort(), ['capturedAt', 'file']);
  assert.ok(!call.url.includes(TOKEN) && !call.url.includes(CLAIM), 'no secrets in the URL');
  assert.equal(result.evidenceId, 'ev_demo123');
  assert.equal(result.status, 'uploaded');
});

test('evidence filenames follow the captured media type', () => {
  assert.equal(evidenceFilename(new Blob([], {type: 'image/jpeg'})), 'evidence.jpg');
  assert.equal(evidenceFilename(new Blob([], {type: 'video/webm;codecs=vp9'})), 'evidence.webm');
  assert.equal(evidenceFilename(new Blob([], {type: 'video/mp4'})), 'evidence.mp4');
});

test('requestUploadToken sends the claim code in the JSON body only', async () => {
  const fetchImpl = server({token: json(200, {uploadToken: FRESH, uploadTokenExpiresAt: '2099-01-01T00:00:00.000Z'})});
  const result = await createApiClient({baseUrl: BASE, fetchImpl}).requestUploadToken('NCL-2026-UPL001', CLAIM);
  const [call] = fetchImpl.calls;
  assert.equal(call.url, BASE + '/complaints/NCL-2026-UPL001/upload-token');
  assert.deepEqual(JSON.parse(call.init.body), {claim: CLAIM});
  assert.ok(!call.url.includes(CLAIM));
  assert.equal(result.uploadToken, FRESH);
});

test('upload timeouts and malformed upload results are reported', async () => {
  const slow = createApiClient({baseUrl: BASE, fetchImpl: server({upload: hangUntilAborted}), uploadTimeoutMs: 30});
  await assert.rejects(slow.uploadEvidence('NCL-2026-UPL001', photo(), {token: TOKEN}), err => err.code === 'timeout');
  const junk = createApiClient({baseUrl: BASE, fetchImpl: server({upload: json(201, {evidenceId: 'ev_x', status: 'done'})})});
  await assert.rejects(junk.uploadEvidence('NCL-2026-UPL001', photo(), {token: TOKEN}), err => err.code === 'malformed_response');
  const html = createApiClient({baseUrl: BASE, fetchImpl: server({upload: new Response('<html>', {status: 200})})});
  await assert.rejects(html.uploadEvidence('NCL-2026-UPL001', photo(), {token: TOKEN}), err => err.code === 'malformed_response');
});

// ---------- Gateway: upload after creation ----------

test('backend mode creates the complaint, then the upload succeeds as a separate step', async () => {
  const fetchImpl = server({create: json(201, receiptBody('NCL-2026-UPL010')), upload: json(201, evidenceBody())});
  const blob = photo();
  await createServerReport('NCL-2026-UPL010', fetchImpl, blob);
  assert.equal(fetchImpl.count('upload'), 0, 'creation does not upload by itself');
  assert.equal(uploadState('NCL-2026-UPL010'), 'pending');

  const held = takeServerReceipt('NCL-2026-UPL010');
  assert.equal(held.receipt.uploadToken, undefined, 'the upload token is not handed to the receipt view');

  const result = await uploadEvidence('NCL-2026-UPL010', {fetchImpl});
  assert.equal(result.state, 'uploaded');
  assert.equal(result.evidence.evidenceId, 'ev_demo123');
  assert.equal(uploadState('NCL-2026-UPL010'), 'uploaded');
  assert.equal(fetchImpl.calls.find(c => c.kind === 'upload').init.body.get('file').size, blob.size);
  assert.equal(media.get(serverEvidenceKey('NCL-2026-UPL010')), blob, 'the local copy is kept after a confirmed upload');
  assert.ok(!deleted.includes(serverEvidenceKey('NCL-2026-UPL010')));
  forgetUpload('NCL-2026-UPL010');
});

test('a failed upload keeps the complaint and the local evidence, and retry does not create a second complaint', async () => {
  const fetchImpl = server({
    create: json(201, receiptBody('NCL-2026-UPL020')),
    upload: [json(503, errorBody('storage_unavailable', 'The complaint could not be saved or read right now. Try again shortly.')), networkDown, json(201, evidenceBody())]
  });
  const blob = photo();
  await createServerReport('NCL-2026-UPL020', fetchImpl, blob);

  const first = await uploadEvidence('NCL-2026-UPL020', {fetchImpl});
  assert.equal(first.state, 'failed');
  assert.equal(first.retryable, true);
  assert.match(first.message, /Try again shortly/);
  assert.equal(uploadState('NCL-2026-UPL020'), 'failed');
  assert.equal(media.get(serverEvidenceKey('NCL-2026-UPL020')), blob, 'local evidence preserved after failure');

  const second = await uploadEvidence('NCL-2026-UPL020', {fetchImpl});
  assert.equal(second.state, 'failed');
  assert.equal(second.code, 'network_error');
  assert.equal(second.retryable, true);

  const third = await uploadEvidence('NCL-2026-UPL020', {fetchImpl});
  assert.equal(third.state, 'uploaded');
  assert.equal(fetchImpl.count('create'), 1, 'retries never re-create the complaint');
  assert.equal(fetchImpl.count('upload'), 3);
  assert.ok(fetchImpl.calls.filter(c => c.kind === 'upload').every(c => c.url === BASE + '/complaints/NCL-2026-UPL020/evidence'), 'every retry targets the same complaint');
  forgetUpload('NCL-2026-UPL020');
});

test('an expired upload token is renewed once with the in-memory claim code', async () => {
  const fetchImpl = server({
    create: json(201, receiptBody('NCL-2026-UPL030')),
    upload: [json(401, errorBody('token_expired', 'The upload token has expired.')), json(201, evidenceBody())],
    token: json(200, {uploadToken: FRESH, uploadTokenExpiresAt: '2099-01-01T00:00:00.000Z'})
  });
  await createServerReport('NCL-2026-UPL030', fetchImpl);
  const result = await uploadEvidence('NCL-2026-UPL030', {fetchImpl});
  assert.equal(result.state, 'uploaded');
  const uploadsSent = fetchImpl.calls.filter(c => c.kind === 'upload');
  assert.deepEqual(uploadsSent.map(c => c.init.headers.Authorization), ['Bearer ' + TOKEN, 'Bearer ' + FRESH]);
  assert.deepEqual(JSON.parse(fetchImpl.calls.find(c => c.kind === 'token').init.body), {claim: CLAIM});
  assert.equal(fetchImpl.count('create'), 1);
  forgetUpload('NCL-2026-UPL030');
});

test('a rejected claim code ends with a non-retryable failure', async () => {
  const fetchImpl = server({
    create: json(201, receiptBody('NCL-2026-UPL040')),
    upload: json(401, errorBody('token_expired', 'The upload token has expired.')),
    token: json(401, errorBody('unauthorized', 'The claim code does not match this complaint.'))
  });
  await createServerReport('NCL-2026-UPL040', fetchImpl);
  const result = await uploadEvidence('NCL-2026-UPL040', {fetchImpl});
  assert.equal(result.state, 'failed');
  assert.equal(result.retryable, false);
  assert.equal(fetchImpl.count('token'), 1, 'the token is renewed at most once per attempt');
  forgetUpload('NCL-2026-UPL040');
});

test('files the server refuses are not offered for retry', async () => {
  for (const [status, code] of [[415, 'unsupported_media_type'], [413, 'payload_too_large'], [422, 'type_mismatch'], [409, 'evidence_exists']]) {
    const id = `NCL-2026-REF${status}`;
    const fetchImpl = server({create: json(201, receiptBody(id)), upload: json(status, errorBody(code, 'Refused.'))});
    await createServerReport(id, fetchImpl);
    const result = await uploadEvidence(id, {fetchImpl});
    assert.equal(result.state, 'failed', code);
    assert.equal(result.retryable, false, code);
    forgetUpload(id);
  }
});

test('an upload timeout is reported as retryable', async () => {
  const fetchImpl = server({create: json(201, receiptBody('NCL-2026-UPL050')), upload: hangUntilAborted});
  await createServerReport('NCL-2026-UPL050', fetchImpl);
  const result = await uploadEvidence('NCL-2026-UPL050', {fetchImpl, uploadTimeoutMs: 30});
  assert.equal(result.state, 'failed');
  assert.equal(result.code, 'timeout');
  assert.equal(result.retryable, true);
  forgetUpload('NCL-2026-UPL050');
});

test('a double click shares one upload request', async () => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const fetchImpl = server({create: json(201, receiptBody('NCL-2026-UPL060')), upload: async () => { await gate; return json(201, evidenceBody()); }});
  await createServerReport('NCL-2026-UPL060', fetchImpl);
  const a = uploadEvidence('NCL-2026-UPL060', {fetchImpl});
  const b = uploadEvidence('NCL-2026-UPL060', {fetchImpl});
  assert.equal(uploadState('NCL-2026-UPL060'), 'uploading');
  release();
  assert.equal((await a).state, 'uploaded');
  assert.equal((await b).state, 'uploaded');
  assert.equal(fetchImpl.count('upload'), 1);
  assert.equal((await uploadEvidence('NCL-2026-UPL060', {fetchImpl})).state, 'uploaded', 'an uploaded file is not sent again');
  assert.equal(fetchImpl.count('upload'), 1);
  forgetUpload('NCL-2026-UPL060');
});

test('upload tokens and claim codes are never written to browser storage', async () => {
  const fetchImpl = server({create: json(201, receiptBody('NCL-2026-UPL070')), upload: json(201, evidenceBody())});
  await createServerReport('NCL-2026-UPL070', fetchImpl);
  await uploadEvidence('NCL-2026-UPL070', {fetchImpl});
  const dump = storageDump();
  assert.ok(!dump.includes(TOKEN) && !dump.includes(CLAIM));
  for (const value of media.values()) assert.ok(value instanceof Blob, 'IndexedDB holds only evidence files');
  forgetUpload('NCL-2026-UPL070');
});

test('after forgetUpload the context is gone and nothing is retried silently', async () => {
  const fetchImpl = server({create: json(201, receiptBody('NCL-2026-UPL080')), upload: json(201, evidenceBody())});
  await createServerReport('NCL-2026-UPL080', fetchImpl);
  forgetUpload('NCL-2026-UPL080');
  const result = await uploadEvidence('NCL-2026-UPL080', {fetchImpl});
  assert.equal(result.state, 'unavailable');
  assert.equal(result.retryable, false);
  assert.equal(fetchImpl.count('upload'), 0);
  assert.ok(media.has(serverEvidenceKey('NCL-2026-UPL080')), 'the local copy is still kept');
});

test('a receipt without an upload token or file reports the upload as unavailable', async () => {
  const noToken = {...receiptBody('NCL-2026-UPL090'), uploadToken: undefined};
  const fetchImpl = server({create: json(201, noToken)});
  await createServerReport('NCL-2026-UPL090', fetchImpl);
  assert.equal(uploadState('NCL-2026-UPL090'), 'unavailable');
  const noFile = server({create: json(201, receiptBody('NCL-2026-UPL091'))});
  await submitReport(report(), null, {config: BACKEND, fetchImpl: noFile});
  assert.equal((await uploadEvidence('NCL-2026-UPL091', {fetchImpl: noFile})).state, 'unavailable');
  forgetUpload('NCL-2026-UPL090');
  forgetUpload('NCL-2026-UPL091');
});

// ---------- Local mode and public tracking ----------

test('local mode is unchanged and never uploads', async () => {
  await api.reset();
  const fetchImpl = server({});
  const blob = photo();
  const saved = await submitReport(report(), blob, {config: LOCAL, fetchImpl});
  assert.equal(saved.mode, 'local');
  assert.equal(uploadState(saved.id), 'unavailable');
  assert.equal((await uploadEvidence(saved.id, {fetchImpl})).state, 'unavailable');
  assert.equal(fetchImpl.calls.length, 0);
  assert.equal(media.get(saved.id), blob, 'local evidence still keyed by the bare ID');
});

test('public tracking never shows media URLs, evidence details or claim codes', async () => {
  const leaky = {
    id: 'NCL-2026-UPL100', category: 'Potholes', area: 'Hebbal', status: 'Submitted', verification: 'Pending Verification',
    department: 'Awaiting civic review', updated: '2026-10-01T04:00:01.000Z', timeline: [], verificationLog: [],
    claim: CLAIM, evidence: {evidenceId: 'ev_demo123', url: 'http://localhost:8000/media/abc', storageKey: 'f'.repeat(32)}, mediaUrl: 'http://x/y.jpg'
  };
  const {record} = await trackPublic('NCL-2026-UPL100', {config: BACKEND, fetchImpl: server({get: json(200, leaky)})});
  const shown = JSON.stringify(record);
  for (const secret of [CLAIM, 'ev_demo123', 'http://', 'storageKey', 'f'.repeat(32), 'mediaUrl', 'evidence']) assert.ok(!shown.includes(secret), secret);
});
