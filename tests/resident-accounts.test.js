import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync, readdirSync, statSync} from 'node:fs';
import path from 'node:path';

// Browser-storage shim for Node, as in services.test.js.
const storage = new Map();
const shim = {getItem: k => (storage.has(k) ? storage.get(k) : null), setItem: (k, v) => storage.set(k, String(v)), removeItem: k => storage.delete(k)};
globalThis.localStorage = shim;
globalThis.sessionStorage = shim;

const {mediaDB} = await import('../dist/services.js');
const {submitReport} = await import('../dist/report-gateway.js');
const {createApiClient, toResidentComplaint} = await import('../dist/api-client.js');
const {residentSession} = await import('../dist/resident-session.js');
const {toResidentRow} = await import('../dist/server-records.js');
mediaDB.put = async () => {};

const BACKEND = {mode: 'backend', baseUrl: 'http://api.test/api/v1', problem: null};
const json = (status, body) => new Response(JSON.stringify(body), {status, headers: {'Content-Type': 'application/json'}});
const receipt = {
  id: 'NCL-2026-RESID1', claim: '6f1c2a7e-0d4b-4f6e-9a3b-2c8d1e5f7a90', category: 'Potholes', area: 'Hebbal', created: '2026-10-30T04:00:00.000Z',
  capturedAt: null, uploadedAt: '2026-10-30T04:00:01.000Z', captureSource: 'Live camera',
  gps: {lat: 13.03, lng: 77.59, accuracy: 12, source: 'Device GPS', sourceType: 'gps', timestamp: null, address: null},
  mediaType: 'photo', shareEvidence: false, status: 'Submitted', verification: 'Pending Verification', uploadToken: 'u'.repeat(43), uploadTokenExpiresAt: '2026-10-30T05:00:00.000Z'
};
const report = {category: 'Potholes', mediaType: 'photo', gps: {lat: 13.03, lng: 77.59, accuracy: 12, timestamp: Date.now() - 3000, sourceType: 'gps'}};
const SESSION = {token: 'r'.repeat(43), expiresAt: new Date(Date.now() + 3600e3).toISOString(), resident: {email: 'resident@civiclens.demo', displayName: 'Demo resident'}};

function recorder(response) {
  const calls = [];
  return {calls, fetchImpl: async (url, init) => { calls.push({url, init}); return response(); }};
}

test('a passerby report carries no Authorization header and no owner', async () => {
  residentSession.clear();
  const r = recorder(() => json(201, receipt));
  await submitReport(report, null, {config: BACKEND, fetchImpl: r.fetchImpl});
  const {init} = r.calls[0];
  assert.equal(init.headers.Authorization, undefined);
  assert.ok(!('owner' in JSON.parse(init.body)) && !('role' in JSON.parse(init.body)));
});

test('a signed-in resident report sends the resident session token, not an owner field', async () => {
  residentSession.set(SESSION);
  const r = recorder(() => json(201, receipt));
  await submitReport(report, null, {config: BACKEND, fetchImpl: r.fetchImpl});
  const {init} = r.calls[0];
  assert.equal(init.headers.Authorization, 'Bearer ' + SESSION.token);
  assert.ok(!('owner' in JSON.parse(init.body)), 'ownership comes from the server session, never the body');
  residentSession.clear();
});

test('an ended resident session is cleared and the report is not silently sent anonymously', async () => {
  residentSession.set(SESSION);
  const r = recorder(() => json(401, {error: {code: 'unauthorized', message: 'Sign in'}}));
  await assert.rejects(submitReport(report, null, {config: BACKEND, fetchImpl: r.fetchImpl}), err => err.code === 'resident_session_ended');
  assert.equal(r.calls.length, 1, 'no anonymous retry');
  assert.equal(residentSession.get(), null);
});

test('the resident session is memory-only and expires', () => {
  residentSession.set({...SESSION, expiresAt: new Date(Date.now() - 1000).toISOString()});
  assert.equal(residentSession.get(), null);
  residentSession.set(SESSION);
  assert.ok(![...storage.values()].some(v => v.includes(SESSION.token)), 'the token is never written to browser storage');
  residentSession.clear();
});

test('resident API calls use the resident endpoints and bearer token', async () => {
  const r = recorder(() => json(200, {token: 't'.repeat(43), expiresAt: null, resident: {residentId: 'res_x', email: 'a@b.org', displayName: 'A', demo: false}}));
  const http = createApiClient({baseUrl: BACKEND.baseUrl, fetchImpl: r.fetchImpl});
  const session = await http.residentSignIn('a@b.org', 'pw');
  assert.equal(r.calls[0].url, BACKEND.baseUrl + '/residents/sessions');
  assert.equal(session.resident.email, 'a@b.org');
  const c = recorder(() => json(200, {outcome: 'claimed', complaint: {id: 'NCL-2026-AAAAAA', category: 'Potholes', status: 'Submitted', verification: 'Pending Verification', ownedVia: 'claimed', claimHash: 'x', claim: 'y'}}));
  const http2 = createApiClient({baseUrl: BACKEND.baseUrl, fetchImpl: c.fetchImpl});
  const out = await http2.claimComplaint('k'.repeat(43), 'NCL-2026-AAAAAA', 'code');
  assert.equal(c.calls[0].url, BACKEND.baseUrl + '/residents/me/claims');
  assert.equal(c.calls[0].init.headers.Authorization, 'Bearer ' + 'k'.repeat(43));
  assert.deepEqual(JSON.parse(c.calls[0].init.body), {id: 'NCL-2026-AAAAAA', claim: 'code'}, 'the claim code goes in the body only');
  assert.ok(!c.calls[0].url.includes('code'));
  assert.ok(!('claimHash' in out.complaint) && !('claim' in out.complaint), 'unknown fields are dropped');
});

test('owned complaints become workspace rows marked as the resident\'s own', () => {
  const row = toResidentRow(toResidentComplaint({id: 'NCL-2026-AAAAAA', category: 'Potholes', area: 'Hebbal', status: 'Submitted', verification: 'Pending Verification',
    gps: {lat: 13, lng: 77}, ownedVia: 'submitted', timeline: []}));
  assert.equal(row.mine, true);
  assert.equal(row.server, true);
  assert.deepEqual(row.gps, {lat: 13, lng: 77, accuracy: null, sourceType: null});
});

test('no server credentials or demo admin details are shipped in the frontend', () => {
  const files = [];
  const walk = dir => readdirSync(dir).forEach(f => { const p = path.join(dir, f); statSync(p).isDirectory() ? walk(p) : files.push(p); });
  walk('dist');
  for (const file of files.filter(f => /\.(js|html|json|css|webmanifest)$/.test(f))) {
    const text = readFileSync(file, 'utf8');
    for (const secret of ['CivicDemo2026!', 'demo_admin', 'service_role', 'sb_secret_', 'DATABASE_URL']) {
      assert.ok(!text.includes(secret), `${file} contains ${secret}`);
    }
  }
});
