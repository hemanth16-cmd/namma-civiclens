import {test} from 'node:test';
import assert from 'node:assert/strict';

// Supervisor operations client: request shapes, token placement and strict mapping. The server checks
// role and geographic scope on every request; the client never invents wards or counts.
const {createApiClient, toStaffComplaint} = await import('../dist/api-client.js');
const {isSupervisor} = await import('../dist/views/supervisor.js');

const BASE = 'http://localhost:8000/api/v1';
const TOKEN = 'staff_' + 'S'.repeat(40);
const json = (status, body) => new Response(JSON.stringify(body), {status, headers: {'Content-Type': 'application/json'}});

function recorder(response) {
  const calls = [];
  const fn = async (url, init = {}) => {
    calls.push({url, init});
    return response.clone();
  };
  fn.calls = calls;
  return fn;
}

const COUNTS = {total: 3, open: 2, resolved: 1, pendingVerification: 1, overdue: 1};

test('the dashboard asks for a zone or a ward and keeps only the count fields', async () => {
  const body = {level: 'zone', cityWide: false, zone: 1, ward: null, today: '2026-10-31', totals: {...COUNTS, secret: 'x'}, unverifiedWard: null,
    byStatus: [{status: 'Submitted', count: 2}], byCategory: [{category: 'Potholes', ...COUNTS}],
    byZone: [{number: 1, name: 'Byatarayanapura', wards: 22, ...COUNTS}],
    byWard: [{number: 5, name: 'Jakkur', active: true, testing: false, zone: {number: 1, name: 'Byatarayanapura'}, ...COUNTS}, {name: 'no number'}]};
  const fetchImpl = recorder(json(200, body));
  const http = createApiClient({baseUrl: BASE, fetchImpl});
  const d = await http.supervisorDashboard(TOKEN, {zone: 1});
  assert.equal(fetchImpl.calls[0].url, BASE + '/staff/supervisor/dashboard?zone=1');
  assert.equal(fetchImpl.calls[0].init.headers.Authorization, 'Bearer ' + TOKEN);
  assert.deepEqual(d.totals, COUNTS, 'unknown fields dropped');
  assert.equal(d.byWard.length, 1, 'a ward without a number is dropped');
  assert.deepEqual(d.byWard[0].zone, {number: 1, name: 'Byatarayanapura'});
  await http.supervisorDashboard(TOKEN, {zone: 1, ward: 5});
  assert.equal(fetchImpl.calls[1].url, BASE + '/staff/supervisor/dashboard?ward=5', 'a ward wins over its zone');
  await http.supervisorDashboard(TOKEN);
  assert.equal(fetchImpl.calls[2].url, BASE + '/staff/supervisor/dashboard');
});

test('the multi-ward list sends ward numbers and filters; the unverified list sends no wards', async () => {
  const fetchImpl = recorder(json(200, {items: [{id: 'NCL-2026-ABC123', title: 't', category: 'Potholes', status: 'Submitted', ageDays: 3,
    ward: null, wardAssignmentStatus: 'unverified', assignee: null, gps: {lat: 13, lng: 77}}, {id: 'bad'}], total: 1, limit: 100, offset: 0}));
  const http = createApiClient({baseUrl: BASE, fetchImpl});
  const list = await http.supervisorComplaints(TOKEN, {wards: [5, 12], category: 'Potholes', status: 'Assigned', minAgeDays: 7, maxAgeDays: ''});
  const url = new URL(fetchImpl.calls[0].url);
  assert.equal(url.pathname, '/api/v1/staff/supervisor/complaints');
  assert.deepEqual(Object.fromEntries(url.searchParams), {limit: '100', offset: '0', wards: '5,12', category: 'Potholes', status: 'Assigned', minAgeDays: '7'});
  assert.equal(list.items.length, 1);
  assert.equal(list.items[0].ward, null);
  assert.ok(!('gps' in list.items[0]), 'the list keeps no precise location');
  await http.supervisorComplaints(TOKEN, {unverifiedWard: true});
  const second = new URL(fetchImpl.calls[1].url);
  assert.equal(second.searchParams.get('unverifiedWard'), 'true');
  assert.equal(second.searchParams.get('wards'), null);
});

test('allocation and manual ward post JSON bodies with the token in the header only', async () => {
  const fetchImpl = recorder(json(200, {batch: 'ab_1', results: [{id: 'NCL-2026-ABC123', outcome: 'allocated'}], changed: 1, unchanged: 0}));
  const http = createApiClient({baseUrl: BASE, fetchImpl});
  const r = await http.allocateComplaints(TOKEN, {complaints: ['NCL-2026-ABC123'], assignee: 'asha', priority: 'high', dueAt: '2026-12-01', reason: 'drive', note: 'n'});
  const [call] = fetchImpl.calls;
  assert.equal(call.url, BASE + '/staff/supervisor/allocations');
  assert.equal(call.init.method, 'POST');
  assert.deepEqual(JSON.parse(call.init.body), {complaints: ['NCL-2026-ABC123'], department: null, assignee: 'asha', priority: 'high', dueAt: '2026-12-01', reason: 'drive', note: 'n'});
  assert.ok(!call.url.includes(TOKEN) && !call.init.body.includes(TOKEN));
  assert.equal(r.changed, 1);
});

test('the staff case keeps ward fields and the set_ward action', () => {
  const r = toStaffComplaint({id: 'NCL-2026-ABC123', gps: {lat: 13, lng: 77}, timeline: [],
    ward: {number: 5, name: 'Jakkur', zone: {number: 1, name: 'Byatarayanapura'}, boundary: 'x'}, wardAssignmentStatus: 'manual',
    wardHistory: [{id: 'wh_1', from: null, to: 5, method: 'manual', reason: 'checked', by: 'ravi', at: 'z', gps: {lat: 13, lng: 77}}],
    allowedActions: [{action: 'set_ward', label: 'Set ward'}]});
  assert.deepEqual(r.ward, {number: 5, name: 'Jakkur', zone: {number: 1, name: 'Byatarayanapura'}});
  assert.equal(r.wardAssignmentStatus, 'manual');
  assert.deepEqual(r.wardHistory, [{from: null, to: 5, method: 'manual', reason: 'checked', by: 'ravi', at: 'z'}]);
  assert.deepEqual(r.allowedActions.map(a => a.action), ['set_ward']);
  const old = toStaffComplaint({id: 'NCL-2026-ABC123', gps: {lat: 13, lng: 77}, timeline: []});
  assert.deepEqual([old.ward, old.wardAssignmentStatus, old.wardHistory, old.allocations], [null, 'unverified', [], []], 'an older server still renders');
});

test('only supervisors and admins see the supervisor workspace', () => {
  assert.equal(isSupervisor({role: 'supervisor'}), true);
  assert.equal(isSupervisor({role: 'admin'}), true);
  assert.equal(isSupervisor({role: 'official'}), false);
  assert.equal(isSupervisor(null), false);
});
