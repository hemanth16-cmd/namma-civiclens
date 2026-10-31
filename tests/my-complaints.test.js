import {test} from 'node:test';
import assert from 'node:assert/strict';

// My Complaints (resident account, server mode): the account-scoped endpoint, the owner's evidence list,
// owner-only evidence files, and routing that keeps own complaints out of public tracking.
const storage = new Map();
const shim = {getItem: k => (storage.has(k) ? storage.get(k) : null), setItem: (k, v) => storage.set(k, String(v)), removeItem: k => storage.delete(k)};
globalThis.localStorage = shim;
globalThis.sessionStorage = shim;

const {createApiClient, toResidentComplaint, toOwnerEvidence, ApiError} = await import('../dist/api-client.js');
const {serverModeRoute, rememberResidentDestination, takeResidentDestination} = await import('../dist/router.js');
const {toResidentRow} = await import('../dist/server-records.js');

const BASE = 'http://localhost:8000/api/v1';
const TOKEN = 'res_' + 'R'.repeat(40);
const EV1 = 'ev_' + 'a'.repeat(22), EV2 = 'ev_' + 'b'.repeat(22);
const json = body => new Response(JSON.stringify(body), {status: 200, headers: {'Content-Type': 'application/json'}});
function recorder(response) {
  const calls = [];
  const fn = async (url, init = {}) => { calls.push({url, init}); return response.clone(); };
  fn.calls = calls;
  return fn;
}
const complaint = (extra = {}) => ({id: 'NCL-2026-MINE01', title: 'Potholes reported by a citizen', category: 'Potholes', area: 'Hebbal', description: 'Deep pothole',
  mediaType: 'photo', shareEvidence: false, hasEvidence: true, gps: {lat: 13.1, lng: 77.6, accuracy: 8, sourceType: 'gps', address: null},
  created: '2026-10-31T05:00:00.000Z', updated: '2026-10-31T05:00:00.000Z', status: 'Submitted', verification: 'Pending Verification',
  department: 'Awaiting civic review', ownedVia: 'submitted', ownedAt: '2026-10-31T05:00:00.000Z', timeline: [], verificationLog: [],
  evidence: [
    {evidenceId: EV1, role: 'original', mediaType: 'photo', contentType: 'image/jpeg', byteSize: 4000, capturedAt: null, uploadedAt: 'z', storageKey: 'SECRET'},
    {evidenceId: EV2, role: 'repair', mediaType: 'photo', contentType: 'image/jpeg', byteSize: 3000, capturedAt: null, uploadedAt: 'z'},
    {evidenceId: 'bad', role: 'original'}],
  ...extra});

test('My Complaints reads the account-scoped endpoint with the resident token, never the public list', async () => {
  const fetchImpl = recorder(json({items: [complaint()], total: 1, limit: 100, offset: 0}));
  const {items} = await createApiClient({baseUrl: BASE, fetchImpl}).listResidentComplaints(TOKEN, {limit: 100});
  const [call] = fetchImpl.calls;
  assert.equal(call.url, BASE + '/residents/me/complaints?limit=100&offset=0');
  assert.equal(call.init.headers.Authorization, 'Bearer ' + TOKEN);
  assert.ok(!fetchImpl.calls.some(c => /\/api\/v1\/complaints(\?|$)/.test(c.url)), 'not GET /complaints');
  assert.equal(items[0].evidence.length, 2, 'both files, the malformed one dropped');
});

test('the owner\'s evidence list keeps known fields only, never a storage key', () => {
  const ev = toOwnerEvidence(complaint().evidence);
  assert.deepEqual(ev.map(e => [e.evidenceId, e.role, e.mediaType]), [[EV1, 'original', 'photo'], [EV2, 'repair', 'photo']]);
  assert.ok(!JSON.stringify(ev).includes('SECRET'));
  assert.deepEqual(toOwnerEvidence(undefined), []);
  assert.deepEqual(toResidentComplaint(complaint({evidence: undefined})).evidence, [], 'an older server without the list still renders');
  assert.equal(toResidentRow(toResidentComplaint(complaint())).evidence.length, 2, 'workspace rows keep the evidence');
});

test('one complaint and its files come from owner-only endpoints with the resident token', async () => {
  const detail = recorder(json(complaint()));
  const http = createApiClient({baseUrl: BASE, fetchImpl: detail});
  await http.getResidentComplaint(TOKEN, 'NCL-2026-MINE01');
  assert.equal(detail.calls[0].url, BASE + '/residents/me/complaints/NCL-2026-MINE01');
  const missing = recorder(new Response(JSON.stringify({error: {code: 'not_found', message: 'None of your complaints has that ID.'}}), {status: 404, headers: {'Content-Type': 'application/json'}}));
  assert.equal(await createApiClient({baseUrl: BASE, fetchImpl: missing}).getResidentComplaint(TOKEN, 'NCL-2026-OTHER1'), null, 'not in the account: null, as the tracking page expects');

  const file = recorder(new Response(new Uint8Array([0xff, 0xd8, 0xff]), {status: 200, headers: {'Content-Type': 'image/jpeg'}}));
  const blob = await createApiClient({baseUrl: BASE, fetchImpl: file}).fetchResidentEvidenceFile(TOKEN, 'NCL-2026-MINE01', EV1);
  assert.equal(file.calls[0].url, `${BASE}/residents/me/complaints/NCL-2026-MINE01/evidence/${EV1}`);
  assert.equal(file.calls[0].init.headers.Authorization, 'Bearer ' + TOKEN);
  assert.equal(blob.type, 'image/jpeg');
  assert.equal(blob.size, 3);
});

test('unexpected file types, missing files and other residents\' files are errors, not broken images', async () => {
  const html = recorder(new Response('<script>', {status: 200, headers: {'Content-Type': 'text/html'}}));
  await assert.rejects(createApiClient({baseUrl: BASE, fetchImpl: html}).fetchResidentEvidenceFile(TOKEN, 'NCL-2026-MINE01', EV1), err => err.code === 'malformed_response');
  const notMine = recorder(new Response(JSON.stringify({error: {code: 'evidence_not_found', message: 'None of your complaints has that evidence file.'}}), {status: 404, headers: {'Content-Type': 'application/json'}}));
  await assert.rejects(createApiClient({baseUrl: BASE, fetchImpl: notMine}).fetchResidentEvidenceFile(TOKEN, 'NCL-2026-OTHER1', EV1),
    err => err instanceof ApiError && err.status === 404 && err.code === 'evidence_not_found');
  const gone = recorder(new Response(JSON.stringify({error: {code: 'evidence_unavailable', message: 'The evidence file is not in this server\'s storage.'}}), {status: 404, headers: {'Content-Type': 'application/json'}}));
  await assert.rejects(createApiClient({baseUrl: BASE, fetchImpl: gone}).fetchResidentEvidenceFile(TOKEN, 'NCL-2026-MINE01', EV1), err => err.code === 'evidence_unavailable');
});

test('a resident\'s own complaint route stays in the account; only #/issue goes to public tracking', () => {
  assert.equal(serverModeRoute(['resident', 'complaints']), null);
  assert.equal(serverModeRoute(['resident', 'complaints', 'NCL-2026-MINE01']), null, 'My Complaints detail is not redirected');
  assert.equal(serverModeRoute(['issue', 'NCL-2026-MINE01']), 'track/NCL-2026-MINE01', 'public tracking stays reachable by its own route');
});

test('after a refresh asks for sign-in, the resident returns to the page they were on', () => {
  rememberResidentDestination('resident/complaints/NCL-2026-MINE01');
  assert.equal(takeResidentDestination(), 'resident/complaints/NCL-2026-MINE01');
  assert.equal(takeResidentDestination(), 'resident', 'used once');
  rememberResidentDestination('official/complaints');
  assert.equal(takeResidentDestination(), 'resident', 'only resident routes are remembered');
  rememberResidentDestination('resident/complaints/<script>');
  assert.equal(takeResidentDestination(), 'resident');
});
