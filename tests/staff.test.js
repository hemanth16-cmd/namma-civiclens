import {test} from 'node:test';
import assert from 'node:assert/strict';

const storage = new Map();
const shim = {getItem: k => (storage.has(k) ? storage.get(k) : null), setItem: (k, v) => storage.set(k, String(v)), removeItem: k => storage.delete(k)};
globalThis.localStorage = shim;
globalThis.sessionStorage = shim;

const {createApiClient, toStaffComplaint, ApiError} = await import('../dist/api-client.js');
const {staffSession} = await import('../dist/staff-session.js');

const BASE = 'http://localhost:8000/api/v1';
const TOKEN = 'staff_' + 'S'.repeat(40);
const json = (status, body, headers = {}) => new Response(body === undefined ? null : JSON.stringify(body), {status, headers: {'Content-Type': 'application/json', ...headers}});

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

const detail = (extra = {}) => ({
  id: 'NCL-2026-STF001', category: 'Potholes', area: 'Hebbal', description: 'Demo pothole', status: 'Submitted', verification: 'Pending Verification',
  department: 'Awaiting civic review', created: '2026-10-01T04:00:00.000Z', capturedAt: null, uploadedAt: '2026-10-01T04:00:01.000Z',
  updated: '2026-10-01T04:00:01.000Z', captureSource: 'Live camera', mediaType: 'photo', shareEvidence: false,
  gps: {lat: 13.0358, lng: 77.597, accuracy: 9, source: 'Device GPS', sourceType: 'gps', timestamp: null, address: null},
  timeline: [{status: 'Submitted', text: 'Report received.', date: '2026-10-01T04:00:01.000Z'}], verificationLog: [],
  evidence: {evidenceId: 'ev_x', mediaType: 'photo', contentType: 'image/jpeg', byteSize: 4000, sha256: 'a'.repeat(64), capturedAt: null, uploadedAt: '2026-10-01T04:00:02.000Z'},
  ...extra
});

test('staff sign-in posts credentials as JSON and returns only the session fields', async () => {
  const fetchImpl = recorder(json(200, {token: TOKEN, expiresAt: '2026-10-01T12:00:00.000Z', staff: {username: 'asha', displayName: 'Asha', role: 'official'}, extra: 'x'}));
  const session = await createApiClient({baseUrl: BASE, fetchImpl}).staffSignIn('asha', 'correct horse battery');
  const [call] = fetchImpl.calls;
  assert.equal(call.url, BASE + '/staff/sessions');
  assert.equal(call.init.method, 'POST');
  assert.deepEqual(JSON.parse(call.init.body), {username: 'asha', password: 'correct horse battery'});
  assert.ok(!call.url.includes('correct'), 'the password is never in the URL');
  assert.deepEqual(session, {token: TOKEN, expiresAt: '2026-10-01T12:00:00.000Z', staff: {username: 'asha', displayName: 'Asha', role: 'official'}});
});

test('wrong credentials and malformed sign-in responses are errors', async () => {
  const wrong = recorder(json(401, {error: {code: 'unauthorized', message: 'The username or password is incorrect.'}}));
  await assert.rejects(createApiClient({baseUrl: BASE, fetchImpl: wrong}).staffSignIn('asha', 'x'), err => err instanceof ApiError && err.status === 401 && /incorrect/.test(err.message));
  const junk = recorder(json(200, {token: ''}));
  await assert.rejects(createApiClient({baseUrl: BASE, fetchImpl: junk}).staffSignIn('asha', 'x'), err => err.code === 'malformed_response');
  const limited = recorder(json(429, {error: {code: 'rate_limited', message: 'Too many requests.'}}, {'Retry-After': '900'}));
  await assert.rejects(createApiClient({baseUrl: BASE, fetchImpl: limited}).staffSignIn('asha', 'x'), err => err.code === 'rate_limited' && err.retryAfter === 900);
});

test('staff requests carry the bearer token in the header only', async () => {
  const fetchImpl = recorder(json(200, {items: [], total: 0, limit: 25, offset: 0}), new Response(null, {status: 204}));
  const http = createApiClient({baseUrl: BASE, fetchImpl});
  await http.listStaffComplaints(TOKEN, {limit: 25, offset: 50});
  await http.staffSignOut(TOKEN);
  assert.equal(fetchImpl.calls[0].url, BASE + '/staff/complaints?limit=25&offset=50');
  assert.equal(fetchImpl.calls[1].url, BASE + '/staff/sessions/current');
  assert.equal(fetchImpl.calls[1].init.method, 'DELETE');
  for (const call of fetchImpl.calls) {
    assert.equal(call.init.headers.Authorization, 'Bearer ' + TOKEN);
    assert.ok(!call.url.includes(TOKEN));
    assert.equal(call.init.credentials, 'omit');
  }
});

test('the complaint list keeps only well-formed entries and known fields', async () => {
  const fetchImpl = recorder(json(200, {total: 3, limit: 25, offset: 0, items: [
    {id: 'NCL-2026-STF001', category: 'Potholes', area: 'Hebbal', status: 'Submitted', verification: 'Pending Verification', created: 'c', updated: 'u', mediaType: 'video', hasEvidence: true, claimHash: 'scrypt$x'},
    {id: '<img src=x>', category: 'x'},
    {id: 'NCL-2026-STF002', hasEvidence: 'yes'}
  ]}));
  const list = await createApiClient({baseUrl: BASE, fetchImpl}).listStaffComplaints(TOKEN);
  assert.deepEqual(list.items.map(i => i.id), ['NCL-2026-STF001', 'NCL-2026-STF002']);
  assert.equal(list.items[0].mediaType, 'video');
  assert.equal(list.items[1].hasEvidence, false, 'only a real true counts');
  assert.ok(!JSON.stringify(list).includes('scrypt'));
});

test('the staff detail projection drops anything unexpected, including secrets', () => {
  const r = toStaffComplaint(detail({claim: 'c0ffee00-0000-4000-8000-000000000000', claimHash: 'scrypt$1', uploadToken: 'tok', evidence: {...detail().evidence, storageKey: 'f'.repeat(32), url: 'http://x'}}));
  const text = JSON.stringify(r);
  for (const secret of ['c0ffee00', 'scrypt', 'tok', 'f'.repeat(32), 'http://x', 'storageKey', 'claim']) assert.ok(!text.includes(secret), secret);
  assert.equal(r.gps.lat, 13.0358);
  assert.equal(r.description, 'Demo pothole');
  assert.equal(r.evidence.contentType, 'image/jpeg');
  assert.throws(() => toStaffComplaint({id: 'bad'}), err => err.code === 'malformed_response');
});

test('evidence is fetched as a Blob of an allowed type', async () => {
  const bytes = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 1, 2, 3]);
  const fetchImpl = recorder(new Response(bytes, {status: 200, headers: {'Content-Type': 'image/jpeg', 'Content-Disposition': 'inline; filename="x.jpg"'}}));
  const blob = await createApiClient({baseUrl: BASE, fetchImpl}).fetchStaffEvidence(TOKEN, 'NCL-2026-STF001');
  assert.equal(fetchImpl.calls[0].url, BASE + '/staff/complaints/NCL-2026-STF001/evidence');
  assert.equal(fetchImpl.calls[0].init.headers.Authorization, 'Bearer ' + TOKEN);
  assert.equal(blob.type, 'image/jpeg');
  assert.deepEqual(new Uint8Array(await blob.arrayBuffer()), bytes);
});

test('unexpected evidence types and auth failures are refused', async () => {
  const html = recorder(new Response('<script>alert(1)</script>', {status: 200, headers: {'Content-Type': 'text/html'}}));
  await assert.rejects(createApiClient({baseUrl: BASE, fetchImpl: html}).fetchStaffEvidence(TOKEN, 'NCL-2026-STF001'), err => err.code === 'malformed_response');
  const expired = recorder(json(401, {error: {code: 'unauthorized', message: 'Sign in as CivicLens staff to continue.'}}));
  await assert.rejects(createApiClient({baseUrl: BASE, fetchImpl: expired}).fetchStaffEvidence(TOKEN, 'NCL-2026-STF001'), err => err.status === 401);
  const missing = recorder(json(404, {error: {code: 'evidence_not_found', message: 'No evidence has been uploaded for this complaint.'}}));
  await assert.rejects(createApiClient({baseUrl: BASE, fetchImpl: missing}).fetchStaffEvidence(TOKEN, 'NCL-2026-STF001'), err => err.code === 'evidence_not_found');
});

test('the access log is mapped to known fields', async () => {
  const fetchImpl = recorder(json(200, [{username: 'asha', displayName: 'Asha', action: 'view', accessedAt: '2026-10-01T05:00:00.000Z', client_ip: '10.0.0.7'}]));
  const log = await createApiClient({baseUrl: BASE, fetchImpl}).getEvidenceAccessLog(TOKEN, 'NCL-2026-STF001');
  assert.deepEqual(log, [{username: 'asha', displayName: 'Asha', action: 'view', accessedAt: '2026-10-01T05:00:00.000Z'}]);
});

test('the staff session lives in memory only and expires', () => {
  const before = JSON.stringify([...storage]);
  staffSession.set({token: TOKEN, expiresAt: new Date(Date.now() + 60_000).toISOString(), staff: {username: 'asha', displayName: 'Asha', role: 'official'}});
  assert.equal(staffSession.get().token, TOKEN);
  assert.equal(JSON.stringify([...storage]), before, 'nothing written to localStorage or sessionStorage');
  assert.equal(staffSession.get(Date.now() + 120_000), null, 'expired sessions are dropped');
  staffSession.set({token: TOKEN, expiresAt: new Date(Date.now() + 60_000).toISOString(), staff: {}});
  staffSession.clear();
  assert.equal(staffSession.get(), null);
  staffSession.set({token: 42});
  assert.equal(staffSession.get(), null, 'malformed sessions are ignored');
});
