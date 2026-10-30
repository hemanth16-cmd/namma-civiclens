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

test('in server mode, the original workspaces stay: admin on server data, residents on this device', () => {
  for (const page of [undefined, 'complaints', 'map', 'workorders', 'intelligence', 'video-review', 'notifications']) {
    assert.equal(serverModeRoute(page ? ['official', page] : ['official'], {staff: true}), null);
  }
  for (const page of [undefined, 'complaints', 'community', 'map', 'claim', 'settings']) {
    assert.equal(serverModeRoute(page ? ['resident', page] : ['resident']), null, 'the resident shell shows this device');
  }
  assert.equal(serverModeRoute(['resident', 'notifications']), 'notifications', 'the citizen inbox page');
  assert.equal(serverModeRoute(['issue', 'NCL-2026-ABC123'], {staff: true}), null, 'staff open the original case page');
  assert.equal(serverModeRoute(['issue', 'NCL-2026-ABC123']), 'track/NCL-2026-ABC123', 'everyone else gets public tracking');
  assert.equal(serverModeRoute(['issue'], {staff: true}), 'official/complaints');
  assert.equal(serverModeRoute(['issue']), 'resident');
  for (const head of ['camera', 'track', 'staff', 'receipt', 'home', 'my-reports']) assert.equal(serverModeRoute([head, 'x']), null);
});

const {toWorkspaceRecord, loadServerRecords, dailyActivity} = await import('../dist/server-records.js');
const {createApiClient} = await import('../dist/api-client.js');

const listItem = (n, extra = {}) => ({
  id: `NCL-2026-${String(n).padStart(6, '0')}`, title: 'Potholes reported by a citizen', category: 'Potholes', area: 'Hebbal',
  status: 'Submitted', verification: 'Pending Verification', department: 'Awaiting civic review', created: '2026-10-30T04:00:00.000Z',
  updated: '2026-10-30T04:00:00.000Z', mediaType: 'photo', hasEvidence: true, gps: {lat: 13.03, lng: 77.59, accuracy: 12, sourceType: 'gps'}, ...extra
});

test('server complaints become workspace rows the dashboard and map can use', () => {
  const row = toWorkspaceRecord(listItem(1));
  assert.equal(row.server, true);
  assert.equal(row.sample, false);
  assert.deepEqual(row.gps, {lat: 13.03, lng: 77.59, accuracy: 12, sourceType: 'gps'});
  assert.ok(row.priority && Number.isFinite(row.priority.score), 'the existing baseline priority is shown');
  assert.equal(toWorkspaceRecord(listItem(2, {title: '', gps: null})).title, 'Potholes reported by a citizen');
  assert.equal(toWorkspaceRecord(listItem(3, {gps: null})).gps, null);
});

test('the staff list client keeps valid locations only', async () => {
  const items = [listItem(1), listItem(2, {gps: {lat: 200, lng: 77}}), listItem(3, {gps: {lat: 'x', lng: 77}}), listItem(4, {gps: null})];
  const http = createApiClient({baseUrl: 'http://api.test/api/v1', fetchImpl: async () => json(200, {items, total: 4, limit: 100, offset: 0})});
  const {items: mapped} = await http.listStaffComplaints('t'.repeat(30));
  assert.deepEqual(mapped.map(i => i.gps && i.gps.lat), [13.03, null, null, null]);
  assert.equal(mapped[0].title, 'Potholes reported by a citizen');
  assert.equal(mapped[0].department, 'Awaiting civic review');
});

test('every server page is loaded, and failures are not hidden', async () => {
  const all = Array.from({length: 230}, (_, i) => listItem(i));
  const seen = [];
  const http = {listStaffComplaints: async (token, {limit, offset}) => { seen.push(offset); return {items: all.slice(offset, offset + limit), total: all.length}; }};
  const rows = await loadServerRecords(http, 'token');
  assert.equal(rows.length, 230);
  assert.deepEqual(seen, [0, 100, 200]);
  await assert.rejects(loadServerRecords({listStaffComplaints: async () => { throw new Error('timeout'); }}, 'token'), /timeout/);
});

test('reporting activity counts complaints per day', () => {
  const now = Date.parse('2026-10-30T12:00:00Z');
  const rows = [{created: '2026-10-30T01:00:00Z'}, {created: '2026-10-30T09:00:00Z'}, {created: '2026-10-28T09:00:00Z'}, {created: '2026-09-01T00:00:00Z'}];
  const days = dailyActivity(rows, 12, now);
  assert.equal(days.length, 12);
  assert.deepEqual(days.at(-1), {day: '2026-10-30', count: 2});
  assert.equal(days.find(d => d.day === '2026-10-28').count, 1);
  assert.equal(days.reduce((a, d) => a + d.count, 0), 3, 'older complaints are outside the window');
});

test('the staff case keeps its verification history (used by the restored case page)', async () => {
  const {toStaffComplaint} = await import('../dist/api-client.js');
  const r = toStaffComplaint({
    id: 'NCL-2026-ABC123', category: 'Potholes', gps: {lat: 13, lng: 77}, timeline: [],
    verificationLog: [{id: 'v1', date: '2026-10-31T00:00:00Z', type: 'response', response: 'Still Present', by: 'citizen', actor: 'Citizen', note: '', secret: 'x'}]
  });
  assert.deepEqual(r.verificationLog, [{id: 'v1', date: '2026-10-31T00:00:00Z', type: 'response', from: null, to: null, response: 'Still Present', by: 'citizen', actor: 'Citizen', note: ''}]);
  assert.deepEqual(toStaffComplaint({id: 'NCL-2026-ABC124', gps: {lat: 13, lng: 77}, timeline: []}).verificationLog, []);
});
