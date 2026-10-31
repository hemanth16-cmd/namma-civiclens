import {test} from 'node:test';
import assert from 'node:assert/strict';

// Resident data scopes (server mode), kept separate:
//   My Complaints               -> GET /residents/me/complaints (only my complaints)
//   Community page + resident map -> GET /complaints (every public complaint across Bengaluru)
//   Staff map                    -> GET /staff/complaints (unchanged)
const storage = new Map();
const shim = {getItem: k => (storage.has(k) ? storage.get(k) : null), setItem: (k, v) => storage.set(k, String(v)), removeItem: k => storage.delete(k)};
globalThis.localStorage = shim;
globalThis.sessionStorage = shim;

const BASE = 'http://localhost:8000/api/v1';
const json = body => new Response(JSON.stringify(body), {status: 200, headers: {'Content-Type': 'application/json'}});
const publicItem = (id, mapPoint, extra = {}) => ({id, title: 'Potholes reported by a citizen', category: 'Potholes', area: 'Hebbal', status: 'Submitted',
  verification: 'Pending Verification', department: 'Awaiting civic review', created: '2026-10-31T05:00:00.000Z', updated: '2026-10-31T05:00:00.000Z', mapPoint, ...extra});
// Reports by resident A, resident B and an anonymous passer-by; one has no usable map point.
const PUBLIC = [
  publicItem('NCL-2026-AAAAA1', {lat: 12.972, lng: 77.595, precision: 'approx-110m'}),
  publicItem('NCL-2026-BBBBB1', {lat: 12.925, lng: 77.594, precision: 'approx-110m'}),
  publicItem('NCL-2026-ANON01', {lat: 13.036, lng: 77.597, precision: 'approx-110m'}),
  publicItem('NCL-2026-NOLOC1', null),
];

const calls = [];
globalThis.fetch = async (url, init = {}) => {
  const u = String(url);
  calls.push(u);
  if (u === 'api-config.json') return json({mode: 'backend', baseUrl: BASE});
  const {pathname, searchParams} = new URL(u);
  if (pathname.endsWith('/api/v1/complaints')) {
    const offset = Number(searchParams.get('offset')), limit = Number(searchParams.get('limit'));
    return json({items: PUBLIC.slice(offset, offset + limit), total: PUBLIC.length, limit, offset, facets: {categories: [], areas: []}});
  }
  if (pathname.endsWith('/residents/me/complaints')) return json({items: [], total: 0, limit: 100, offset: 0});
  if (pathname.endsWith('/staff/complaints')) return json({items: [], total: 0, limit: 100, offset: 0});
  return new Response('{}', {status: 404});
};

const {resetApiConfig} = await import('../dist/api-config.js');
const {residentSession} = await import('../dist/resident-session.js');
const {staffSession} = await import('../dist/staff-session.js');
const {mapRecords, workspaceRecords, loadPublicCommunityComplaints, loadMyComplaints, serverClient} = await import('../dist/server-records.js');
const {pointsFor} = await import('../dist/views/map.js');
const {createApiClient, toMapPoint} = await import('../dist/api-client.js');

const resident = {role: 'resident', name: 'Asha', email: 'asha@example.org', server: true};
const signInResident = () => residentSession.set({token: 'res_' + 'R'.repeat(40), expiresAt: '2999-01-01T00:00:00.000Z', resident: {residentId: 'res_x'}});
const paths = () => calls.filter(u => u !== 'api-config.json').map(u => new URL(u).pathname.replace('/api/v1', ''));

test('the resident map loads the public Bengaluru dataset, not the resident\'s own complaints', async () => {
  resetApiConfig();
  signInResident();
  calls.length = 0;
  const records = await mapRecords(resident);
  assert.deepEqual(paths(), ['/complaints'], 'only the public list; never /residents/me/complaints');
  assert.deepEqual(records.map(r => r.id), PUBLIC.map(p => p.id), 'A, B and anonymous reports alike');
  assert.ok(records.every(r => r.public && !r.mine));
  const plotted = records.find(r => r.id === 'NCL-2026-ANON01').gps;
  assert.deepEqual(plotted, {lat: 13.036, lng: 77.597, accuracy: null, sourceType: null, approximate: true});
});

test('My Complaints stays on the account-scoped endpoint', async () => {
  resetApiConfig();
  signInResident();
  calls.length = 0;
  await workspaceRecords(resident);
  assert.deepEqual(paths(), ['/residents/me/complaints']);
  calls.length = 0;
  await loadMyComplaints(await serverClient(), residentSession.get().token);
  assert.ok(calls.some(u => u.includes('/residents/me/complaints')) && !calls.some(u => /\/api\/v1\/complaints\?/.test(u)));
});

test('the staff map keeps using the authorised staff list', async () => {
  resetApiConfig();
  residentSession.clear();
  staffSession.set({token: 'stf_' + 'S'.repeat(40), expiresAt: '2999-01-01T00:00:00.000Z', staff: {username: 'ravi', displayName: 'Ravi', role: 'supervisor'}});
  calls.length = 0;
  await mapRecords({role: 'official', name: 'Ravi', email: 'ravi', server: true});
  assert.deepEqual(paths(), ['/staff/complaints']);
  staffSession.clear();
});

test('markers only for records with usable coordinates; public points are labelled approximate', async () => {
  resetApiConfig();
  signInResident();
  const records = await mapRecords(resident);
  const broken = [{...records[0], id: 'NCL-2026-BAD001', gps: {lat: NaN, lng: 77.5}}, {...records[0], id: 'NCL-2026-BAD002', gps: {lat: 95, lng: 77.5}},
    {...records[0], id: 'NCL-2026-BAD003', gps: null}];
  const {points, hidden} = pointsFor([...records, ...broken]);
  assert.deepEqual(points.map(p => p.r.id), ['NCL-2026-AAAAA1', 'NCL-2026-BBBBB1', 'NCL-2026-ANON01']);
  assert.ok(points.every(p => p.approx === 'public'));
  assert.deepEqual(hidden.map(r => r.id), ['NCL-2026-NOLOC1', 'NCL-2026-BAD001', 'NCL-2026-BAD002', 'NCL-2026-BAD003']);
});

test('the public loader pages through every result and passes filters to the server', async () => {
  const seen = [];
  const total = 150;
  const fake = {async listPublicComplaints(opts) {
    seen.push(opts);
    const items = Array.from({length: Math.max(0, Math.min(opts.limit, total - opts.offset))},
      (_, i) => publicItem(`NCL-2026-P${String(opts.offset + i).padStart(5, '0')}`, null));
    return {items, total, facets: {categories: [], areas: []}};
  }};
  const rows = await loadPublicCommunityComplaints(fake, {category: 'Potholes', area: 'Hebbal'});
  assert.equal(rows.length, 150);
  assert.equal(new Set(rows.map(r => r.id)).size, 150, 'no page repeated or skipped');
  assert.deepEqual(seen.map(o => [o.offset, o.limit, o.category, o.area]), [[0, 100, 'Potholes', 'Hebbal'], [100, 100, 'Potholes', 'Hebbal']]);
});

test('map points from the API are validated', () => {
  assert.deepEqual(toMapPoint({lat: 12.97, lng: 77.59, precision: 'approx-110m'}), {lat: 12.97, lng: 77.59, precision: 'approx-110m'});
  for (const bad of [null, {}, {lat: '12.9', lng: 77.5}, {lat: 12.9, lng: NaN}, {lat: 91, lng: 77.5}, {lat: 12.9, lng: -181}]) assert.equal(toMapPoint(bad), null);
});
