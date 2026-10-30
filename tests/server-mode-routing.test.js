import {test} from 'node:test';
import assert from 'node:assert/strict';

// Browser-storage shim for Node, as in services.test.js.
const storage = new Map();
const shim = {getItem: k => (storage.has(k) ? storage.get(k) : null), setItem: (k, v) => storage.set(k, String(v)), removeItem: k => storage.delete(k)};
globalThis.localStorage = shim;
globalThis.sessionStorage = shim;

const {mediaDB} = await import('../dist/services.js');
const {submitReport, rememberedServerReports, rememberServerReport, forgetServerReport} = await import('../dist/report-gateway.js');
const {serverModeRoute} = await import('../dist/router.js');

mediaDB.put = async () => {};

const BACKEND = {mode: 'backend', baseUrl: 'http://localhost:8000/api/v1', problem: null};
const CLAIM = '6f1c2a7e-0d4b-4f6e-9a3b-2c8d1e5f7a90';
const TOKEN = 'upload-token-abcdefghijklmnopqrstuvwxyz';
const KEY = 'ncl-server-reports-v1';
const json = (status, body) => new Response(JSON.stringify(body), {status, headers: {'Content-Type': 'application/json'}});
const receipt = id => ({
  id, claim: CLAIM, category: 'Potholes', area: 'Hebbal', created: '2026-10-30T04:00:00.000Z', capturedAt: '2026-10-30T04:00:00.000Z',
  uploadedAt: '2026-10-30T04:00:01.000Z', captureSource: 'Live camera',
  gps: {lat: 13.0358, lng: 77.597, accuracy: 12, source: 'Device GPS', sourceType: 'gps', timestamp: '2026-10-30T03:59:56.000Z', address: null},
  mediaType: 'photo', shareEvidence: false, status: 'Submitted', verification: 'Pending Verification', uploadToken: TOKEN, uploadTokenExpiresAt: '2026-10-30T05:00:00.000Z'
});
const report = {
  category: 'Potholes', description: 'Deep pothole', neighbourhood: 'Hebbal', mediaType: 'photo', created: new Date(Date.now() - 3000).toISOString(),
  gps: {lat: 13.0358, lng: 77.597, accuracy: 12, timestamp: Date.now() - 4000, source: 'Device GPS', sourceType: 'gps'}
};

test('a confirmed server report is remembered on this device without its claim code or token', async () => {
  storage.clear();
  const saved = await submitReport(report, new Blob(['jpeg'], {type: 'image/jpeg'}), {config: BACKEND, fetchImpl: async () => json(201, receipt('NCL-2026-MINE01'))});
  assert.equal(saved.id, 'NCL-2026-MINE01');
  assert.deepEqual(rememberedServerReports(), [{id: 'NCL-2026-MINE01', category: 'Potholes', submittedAt: '2026-10-30T04:00:01.000Z'}]);
  const raw = storage.get(KEY);
  for (const secret of [CLAIM, TOKEN, '13.0358', 'Deep pothole']) assert.ok(!raw.includes(secret), `stored list must not contain ${secret}`);
});

test('a failed submission is not remembered', async () => {
  storage.clear();
  await assert.rejects(submitReport(report, new Blob(['x']), {config: BACKEND, fetchImpl: async () => json(503, {error: {code: 'storage_unavailable', message: 'down'}})}));
  assert.deepEqual(rememberedServerReports(), []);
});

test('the list is newest first, de-duplicated, bounded, and ignores malformed entries', () => {
  storage.clear();
  rememberServerReport({id: 'NCL-2026-AAAAA1', category: 'Potholes', submittedAt: '2026-10-30T01:00:00.000Z'});
  rememberServerReport({id: 'NCL-2026-BBBBB2', category: 'Garbage accumulation', submittedAt: '2026-10-30T02:00:00.000Z'});
  rememberServerReport({id: 'NCL-2026-AAAAA1', category: 'Potholes', submittedAt: '2026-10-30T03:00:00.000Z'});
  assert.deepEqual(rememberedServerReports().map(r => r.id), ['NCL-2026-AAAAA1', 'NCL-2026-BBBBB2']);
  assert.equal(rememberServerReport({id: 'not-an-id'}), false);
  storage.set(KEY, JSON.stringify([{id: 'NCL-2026-CCCCC3'}, {id: '<script>'}, null, 'x']));
  assert.deepEqual(rememberedServerReports().map(r => r.id), ['NCL-2026-CCCCC3']);
  storage.set(KEY, 'not json');
  assert.deepEqual(rememberedServerReports(), []);
  storage.clear();
  for (let i = 0; i < 205; i++) rememberServerReport({id: `NCL-2026-${String(i).padStart(6, '0')}`});
  assert.equal(rememberedServerReports().length, 200);
});

test('forgetting removes the report from this device only', () => {
  storage.clear();
  rememberServerReport({id: 'NCL-2026-KEEP01'});
  rememberServerReport({id: 'NCL-2026-GONE01'});
  forgetServerReport('NCL-2026-GONE01');
  assert.deepEqual(rememberedServerReports().map(r => r.id), ['NCL-2026-KEEP01']);
});

test('unavailable browser storage never breaks a submission', async () => {
  const broken = {getItem: () => { throw new Error('blocked'); }, setItem: () => { throw new Error('blocked'); }};
  assert.equal(rememberServerReport({id: 'NCL-2026-NOSTOR'}, {storage: broken}), false);
  assert.deepEqual(rememberedServerReports({storage: broken}), []);
  const saved = await submitReport(report, new Blob(['x']), {config: BACKEND, storage: broken, fetchImpl: async () => json(201, receipt('NCL-2026-NOSTOR'))});
  assert.equal(saved.id, 'NCL-2026-NOSTOR');
});

test('in server mode, demonstration workspaces never stand in for server data', () => {
  assert.equal(serverModeRoute(['official']), 'staff');
  for (const page of ['complaints', 'map', 'workorders', 'intelligence', 'settings']) assert.equal(serverModeRoute(['official', page]), 'staff');
  assert.equal(serverModeRoute(['official', 'video-review']), null, 'video review keeps footage in this browser only');
  assert.equal(serverModeRoute(['resident']), 'my-reports');
  for (const page of ['complaints', 'community', 'map', 'claim', 'settings']) assert.equal(serverModeRoute(['resident', page]), 'my-reports');
  assert.equal(serverModeRoute(['resident', 'notifications']), 'notifications');
  assert.equal(serverModeRoute(['issue', 'NCL-2026-ABC123']), 'track/NCL-2026-ABC123');
  assert.equal(serverModeRoute(['issue']), 'my-reports');
  for (const head of ['camera', 'track', 'staff', 'receipt', 'home', 'my-reports']) assert.equal(serverModeRoute([head, 'x']), null);
});
