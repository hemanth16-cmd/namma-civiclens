import {test} from 'node:test';
import assert from 'node:assert/strict';

// Browser-storage shim for Node, as in services.test.js.
const storage = new Map();
const shim = {getItem: k => (storage.has(k) ? storage.get(k) : null), setItem: (k, v) => storage.set(k, String(v)), removeItem: k => storage.delete(k)};
globalThis.localStorage = shim;
globalThis.sessionStorage = shim;

const {api, mediaDB} = await import('../dist/services.js');
const {createApiClient, newIdempotencyKey, ApiError} = await import('../dist/api-client.js');
const {submitReport, takeServerReceipt, uploadEvidence, forgetUpload} = await import('../dist/report-gateway.js');

const media = new Map();
mediaDB.put = async (k, v) => { media.set(k, v); };
mediaDB.get = async k => media.get(k) ?? null;
mediaDB.delete = async k => { media.delete(k); };
mediaDB.clear = async () => media.clear();

const BASE = 'http://localhost:8000/api/v1';
const BACKEND = {mode: 'backend', baseUrl: BASE, problem: null};
const LOCAL = {mode: 'local', baseUrl: null, problem: null};
const SERVER_KEY_PATTERN = /^[A-Za-z0-9_-]{22,128}$/;  // backend/app/complaints.py IDEMPOTENCY_KEY_PATTERN
const now = Date.now();

const report = () => ({
  category: 'Potholes', description: 'Demo pothole', mediaType: 'photo', shareEvidence: false,
  gps: {lat: 13.0358, lng: 77.597, accuracy: 12, timestamp: now - 4000, source: 'Device GPS', sourceType: 'gps'},
  created: new Date(now - 3000).toISOString()
});
const photo = () => new Blob([new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 1, 2])], {type: 'image/jpeg'});
const receipt = (id, claim, replayed = false) => ({
  id, claim, category: 'Potholes', area: 'Bengaluru', created: null, capturedAt: null, uploadedAt: '2026-10-01T04:00:00.000Z', captureSource: null,
  gps: {lat: 13.0358, lng: 77.597, accuracy: 12, source: 'Device GPS', sourceType: 'gps', timestamp: null, address: null},
  mediaType: 'photo', shareEvidence: false, status: 'Submitted', verification: 'Pending Verification',
  uploadToken: 'tok_' + claim.replace(/-/g, '').padEnd(40, 'x'), uploadTokenExpiresAt: '2099-01-01T00:00:00.000Z', replayed
});
const json = (status, body, headers = {}) => new Response(JSON.stringify(body), {status, headers: {'Content-Type': 'application/json', ...headers}});
const networkDown = () => { throw new TypeError('Failed to fetch'); };

function recorder(...handlers) {
  const calls = [];
  const fn = async (url, init = {}) => {
    calls.push({url, init});
    const h = handlers[Math.min(calls.length - 1, handlers.length - 1)];
    return typeof h === 'function' ? h(url, init) : h.clone();
  };
  fn.calls = calls;
  return fn;
}

test('idempotency keys are 256-bit, URL-safe, accepted by the server pattern and unique', () => {
  const keys = new Set(Array.from({length: 200}, newIdempotencyKey));
  assert.equal(keys.size, 200);
  for (const key of keys) {
    assert.equal(key.length, 43);
    assert.match(key, SERVER_KEY_PATTERN);
  }
});

test('the key travels only in the Idempotency-Key header', async () => {
  const fetchImpl = recorder(json(201, receipt('NCL-2026-IDM001', '11111111-1111-4111-8111-111111111111')));
  const key = newIdempotencyKey();
  await createApiClient({baseUrl: BASE, fetchImpl}).createComplaint({category: 'Potholes'}, {idempotencyKey: key});
  const [call] = fetchImpl.calls;
  assert.equal(call.init.headers['Idempotency-Key'], key);
  assert.ok(!call.url.includes(key) && !call.init.body.includes(key));
  const noKey = recorder(json(201, receipt('NCL-2026-IDM002', '22222222-2222-4222-8222-222222222222')));
  await createApiClient({baseUrl: BASE, fetchImpl: noKey}).createComplaint({category: 'Potholes'});
  assert.equal(noKey.calls[0].init.headers['Idempotency-Key'], undefined);
});

test('a retry after an ambiguous failure reuses the key and shows the recovered receipt', async () => {
  const key = newIdempotencyKey();
  const firstClaim = '33333333-3333-4333-8333-333333333333';
  const reissued = '44444444-4444-4444-8444-444444444444';
  const fetchImpl = recorder(networkDown, json(200, receipt('NCL-2026-IDM003', reissued, true), {'Idempotent-Replayed': 'true'}));

  await assert.rejects(submitReport(report(), photo(), {config: BACKEND, fetchImpl, idempotencyKey: key}), err => err.ambiguous === true);
  assert.equal(takeServerReceipt('NCL-2026-IDM003'), null, 'no receipt for an unconfirmed attempt');

  const saved = await submitReport(report(), photo(), {config: BACKEND, fetchImpl, idempotencyKey: key});
  assert.equal(saved.id, 'NCL-2026-IDM003');
  assert.deepEqual(fetchImpl.calls.map(c => c.init.headers['Idempotency-Key']), [key, key], 'the same key on both attempts');
  assert.deepEqual(JSON.parse(fetchImpl.calls[0].init.body), JSON.parse(fetchImpl.calls[1].init.body), 'the same request, so the server can match it');
  const held = takeServerReceipt('NCL-2026-IDM003');
  assert.equal(held.receipt.replayed, true);
  assert.equal(held.receipt.claim, reissued, 'the re-issued claim code from the server is shown');
  assert.ok(!JSON.stringify(held).includes(firstClaim));
  forgetUpload('NCL-2026-IDM003');
});

test('"already completed" is reported with the public ID but no credentials', async () => {
  const fetchImpl = recorder(json(409, {error: {code: 'idempotency_key_used', message: 'This submission was already completed.', complaintId: 'NCL-2026-IDM004'}}));
  await assert.rejects(submitReport(report(), photo(), {config: BACKEND, fetchImpl, idempotencyKey: newIdempotencyKey()}), err => {
    assert.ok(err instanceof ApiError);
    assert.equal(err.code, 'idempotency_key_used');
    assert.equal(err.complaintId, 'NCL-2026-IDM004');
    assert.equal(err.ambiguous, false);
    return true;
  });
  assert.equal(takeServerReceipt('NCL-2026-IDM004'), null);
  const bogus = recorder(json(409, {error: {code: 'idempotency_key_used', message: 'x', complaintId: '<script>'}}));
  await assert.rejects(createApiClient({baseUrl: BASE, fetchImpl: bogus}).createComplaint({}), err => err.complaintId === null, 'only well-formed public IDs are kept');
});

test('a key conflict is a clear, non-ambiguous error', async () => {
  const fetchImpl = recorder(json(409, {error: {code: 'idempotency_key_conflict', message: 'This Idempotency-Key was already used for a different request.'}}));
  await assert.rejects(submitReport(report(), photo(), {config: BACKEND, fetchImpl, idempotencyKey: newIdempotencyKey()}),
    err => err.code === 'idempotency_key_conflict' && err.status === 409 && !err.ambiguous);
});

test('rate limiting is reported with Retry-After and is retryable for uploads', async () => {
  const limited = recorder(json(429, {error: {code: 'rate_limited', message: 'Too many requests. Try again in 30 seconds.'}}, {'Retry-After': '30'}));
  await assert.rejects(createApiClient({baseUrl: BASE, fetchImpl: limited}).createComplaint({}), err => err.code === 'rate_limited' && err.retryAfter === 30 && err.status === 429);

  const fetchImpl = recorder(json(201, receipt('NCL-2026-IDM005', '55555555-5555-4555-8555-555555555555')),
    json(429, {error: {code: 'rate_limited', message: 'Too many requests. Try again in 60 seconds.'}}, {'Retry-After': '60'}));
  await submitReport(report(), photo(), {config: BACKEND, fetchImpl, idempotencyKey: newIdempotencyKey()});
  const result = await uploadEvidence('NCL-2026-IDM005', {fetchImpl});
  assert.equal(result.state, 'failed');
  assert.equal(result.code, 'rate_limited');
  assert.equal(result.retryable, true);
  forgetUpload('NCL-2026-IDM005');
});

test('a full evidence store is reported and not offered for immediate retry', async () => {
  const fetchImpl = recorder(json(201, receipt('NCL-2026-IDM006', '66666666-6666-4666-8666-666666666666')),
    json(507, {error: {code: 'storage_quota_exceeded', message: 'Evidence storage is full, so this upload cannot be accepted right now. The complaint itself is saved.'}}));
  await submitReport(report(), photo(), {config: BACKEND, fetchImpl, idempotencyKey: newIdempotencyKey()});
  const result = await uploadEvidence('NCL-2026-IDM006', {fetchImpl});
  assert.equal(result.state, 'failed');
  assert.equal(result.code, 'storage_quota_exceeded');
  assert.equal(result.retryable, false);
  assert.match(result.message, /complaint itself is saved/);
  assert.ok(media.has('server-evidence:NCL-2026-IDM006'), 'the local copy is kept');
  forgetUpload('NCL-2026-IDM006');
});

test('keys are never written to browser storage, and local mode ignores them', async () => {
  const key = newIdempotencyKey();
  const fetchImpl = recorder(json(201, receipt('NCL-2026-IDM007', '77777777-7777-4777-8777-777777777777')));
  await submitReport(report(), photo(), {config: BACKEND, fetchImpl, idempotencyKey: key});
  assert.ok(![...storage.values()].join('\n').includes(key));
  forgetUpload('NCL-2026-IDM007');

  await api.reset();
  const network = recorder(() => { throw new Error('must not be called'); });
  const saved = await submitReport(report(), photo(), {config: LOCAL, fetchImpl: network, idempotencyKey: key});
  assert.equal(saved.mode, 'local');
  assert.equal(network.calls.length, 0);
});
