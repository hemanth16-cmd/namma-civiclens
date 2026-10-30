import {test} from 'node:test';
import assert from 'node:assert/strict';

// Complaint management client (Workstream 1): request shapes, auth placement, and strict mapping of
// the staff case. The server is the authority on every rule; these tests check the client never
// widens what it sends or what it keeps.
const {createApiClient, toStaffComplaint, toWorkOrder, ApiError} = await import('../dist/api-client.js');

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

const ORDER = {id: 'wo_' + 'A'.repeat(16), complaintId: 'NCL-2026-STF001', department: 'roads', team: 'Ward 4 crew', assignee: 'asha',
  description: 'Fill', dueAt: '2026-11-05', status: 'Open', startedAt: null, completedAt: null, completionNote: '', createdBy: 'ravi',
  createdAt: '2026-10-30T10:00:00.000Z', updatedAt: '2026-10-30T10:00:00.000Z', overdue: false, storageKey: 'SECRET-KEY',
  evidence: [{phase: 'before', evidenceId: 'ev_' + 'b'.repeat(22), role: 'original', contentType: 'image/jpeg', byteSize: 10, sha256: 'a'.repeat(64),
    uploadedAt: 'z', storageKey: 'SECRET-KEY'}],
  history: [{action: 'created', from: null, to: 'Open', note: '', by: 'ravi', at: 'z'}]};

const staffCase = (extra = {}) => ({
  id: 'NCL-2026-STF001', category: 'Potholes', area: 'Hebbal', description: 'd', status: 'Assigned', verification: 'Pending Verification',
  department: 'Road maintenance', created: 'z', capturedAt: null, uploadedAt: 'z', updated: 'z', captureSource: null, mediaType: 'photo',
  shareEvidence: false, gps: {lat: 13, lng: 77, accuracy: 9, source: 'GPS', sourceType: 'gps', timestamp: null, address: null},
  timeline: [], verificationLog: [], evidence: null, version: 4,
  assignment: {department: {code: 'roads', name: 'Road maintenance'}, assignee: {username: 'asha', displayName: 'Asha', passwordHash: 'x'}},
  assignmentHistory: [{id: 'as_1', department: {code: 'roads', name: 'Road maintenance'}, assignee: 'asha', reason: '', by: 'ravi', at: 'z'}],
  workOrders: [ORDER, {id: 'not-a-work-order'}],
  notes: [{id: 'sn_1', body: 'internal', createdAt: 'z', author: {username: 'ravi', displayName: 'Ravi'}}],
  allowedActions: [{action: 'transition', to: 'In Progress', label: 'Start work', needsExplanation: false}, {action: 'delete_everything', label: 'x'}],
  ...extra
});

test('a status change posts the target and the status the user saw, with the token in the header only', async () => {
  const fetchImpl = recorder(json(200, staffCase()));
  const r = await createApiClient({baseUrl: BASE, fetchImpl}).transitionComplaint(TOKEN, 'NCL-2026-STF001', {to: 'In Progress', expectedStatus: 'Assigned'});
  const [call] = fetchImpl.calls;
  assert.equal(call.url, BASE + '/staff/complaints/NCL-2026-STF001/transitions');
  assert.equal(call.init.method, 'POST');
  assert.equal(call.init.headers.Authorization, 'Bearer ' + TOKEN);
  assert.deepEqual(JSON.parse(call.init.body), {to: 'In Progress', expectedStatus: 'Assigned', explanation: ''});
  assert.ok(!call.url.includes(TOKEN));
  assert.equal(r.version, 4);
});

test('the staff case keeps only known management fields and actions', () => {
  const r = toStaffComplaint(staffCase());
  assert.deepEqual(r.allowedActions, [{action: 'transition', to: 'In Progress', label: 'Start work', needsExplanation: false}], 'unknown actions dropped');
  assert.deepEqual(r.assignment.assignee, {username: 'asha', displayName: 'Asha'}, 'no extra account fields');
  assert.equal(r.workOrders.length, 1, 'malformed work orders dropped');
  assert.ok(!JSON.stringify(r).includes('SECRET-KEY'), 'no storage keys survive mapping');
  assert.deepEqual(r.notes, [{id: 'sn_1', body: 'internal', createdAt: 'z', author: {username: 'ravi', displayName: 'Ravi'}}]);
  const bare = toStaffComplaint(staffCase({assignment: undefined, workOrders: undefined, notes: undefined, allowedActions: undefined, assignmentHistory: undefined}));
  assert.deepEqual([bare.assignment, bare.workOrders, bare.notes, bare.allowedActions], [{department: null, assignee: null}, [], [], []],
    'an older server without management data still renders');
});

test('work orders map evidence phases and history without extra fields', () => {
  const w = toWorkOrder(ORDER);
  assert.deepEqual(w.evidence, [{phase: 'before', evidenceId: 'ev_' + 'b'.repeat(22), contentType: 'image/jpeg', byteSize: 10, uploadedAt: 'z'}]);
  assert.equal(toWorkOrder({id: 'wo_short'}), null);
  assert.equal(toWorkOrder(null), null);
});

test('assignment, notes, updates, reinspection and work orders hit their own endpoints', async () => {
  const fetchImpl = recorder(json(200, staffCase()));
  const http = createApiClient({baseUrl: BASE, fetchImpl});
  const id = 'NCL-2026-STF001', wo = ORDER.id;
  await http.assignComplaint(TOKEN, id, {department: 'roads', assignee: '', reason: 'r', expectedStatus: 'Submitted'});
  await http.addStaffNote(TOKEN, id, 'note');
  await http.addPublicUpdate(TOKEN, id, {text: 'update', kind: 'department_response'});
  await http.requestReinspection(TOKEN, id, {note: 'n', reopen: 'yes', expectedStatus: 'Marked Resolved'});
  await http.createWorkOrder(TOKEN, id, {team: 'crew', dueAt: ''});
  await http.changeWorkOrder(TOKEN, wo, {to: 'Completed', expectedStatus: 'In Progress', note: 'done'});
  const sent = fetchImpl.calls.map(c => [c.url.replace(BASE, ''), JSON.parse(c.init.body)]);
  assert.deepEqual(sent, [
    [`/staff/complaints/${id}/assignment`, {department: 'roads', assignee: null, reason: 'r', expectedStatus: 'Submitted'}],
    [`/staff/complaints/${id}/notes`, {body: 'note'}],
    [`/staff/complaints/${id}/updates`, {text: 'update', kind: 'department_response'}],
    [`/staff/complaints/${id}/reinspections`, {note: 'n', reopen: false, expectedStatus: 'Marked Resolved'}],
    [`/staff/complaints/${id}/work-orders`, {team: 'crew', description: '', dueAt: null, assignee: null}],
    [`/staff/work-orders/${wo}/transitions`, {to: 'Completed', expectedStatus: 'In Progress', note: 'done'}],
  ]);
  assert.ok(fetchImpl.calls.every(c => c.init.method === 'POST' && c.init.headers.Authorization === 'Bearer ' + TOKEN));
});

test('a citizen response sends the claim code in the body, never the URL, and needs no staff token', async () => {
  const fetchImpl = recorder(json(200, {verification: 'Citizen Confirmed', changed: true, extra: 1}));
  const r = await createApiClient({baseUrl: BASE, fetchImpl}).submitCitizenResponse('NCL-2026-STF001', 'my-private-claim', 'Appears Resolved');
  const [call] = fetchImpl.calls;
  assert.equal(call.url, BASE + '/complaints/NCL-2026-STF001/citizen-responses');
  assert.ok(!call.url.includes('my-private-claim'));
  assert.equal(call.init.headers.Authorization, undefined);
  assert.deepEqual(JSON.parse(call.init.body), {claim: 'my-private-claim', response: 'Appears Resolved'});
  assert.deepEqual(r, {verification: 'Citizen Confirmed', changed: true});
});

test('rule violations surface the server message and status', async () => {
  const fetchImpl = recorder(json(409, {error: {code: 'conflict', message: 'The status changed to “In Progress” since you loaded it. Reload and try again.'}}));
  await assert.rejects(createApiClient({baseUrl: BASE, fetchImpl}).transitionComplaint(TOKEN, 'NCL-2026-STF001', {to: 'In Progress', expectedStatus: 'Assigned'}),
    err => err instanceof ApiError && err.status === 409 && err.code === 'conflict' && /Reload/.test(err.message));
});

test('audit and department lists are mapped defensively', async () => {
  const http = createApiClient({baseUrl: BASE, fetchImpl: recorder(json(200, [{code: 'roads', name: 'Road maintenance', secret: 1}, {name: 'no code'}]))});
  assert.deepEqual(await http.listDepartments(TOKEN), [{code: 'roads', name: 'Road maintenance'}]);
  const audit = createApiClient({baseUrl: BASE, fetchImpl: recorder(json(200, [{id: 'au_1', actorType: 'staff', actor: 'ravi', actorName: 'Ravi', action: 'assigned', details: 'oops', at: 'z'}]))});
  assert.deepEqual((await audit.getAuditTrail(TOKEN, 'NCL-2026-STF001'))[0].details, {});
});

test('duplicate suggestions keep known labels and fields only, and merges post to the right endpoints', async () => {
  const body = {complaintId: 'NCL-2026-STF001', radiusM: 250, days: 90, note: null, candidates: [
    {id: 'NCL-2026-STF002', category: 'Potholes', area: 'Hebbal', created: 'z', status: 'Submitted', verification: 'Pending Verification',
      score: 0.91, label: 'likely_duplicate', distanceM: 5.2, gpsLat: 13.1, description: 'secret text',
      factors: [{name: 'location', value: 0.98, weight: 0.45, contribution: 0.44, detail: '5 m apart'}], reasons: ['Within the GPS uncertainty of each other.'], caution: null},
    {id: 'NCL-2026-STF003', label: 'merge_now', score: 1}]};
  const fetchImpl = recorder(json(200, body));
  const result = await createApiClient({baseUrl: BASE, fetchImpl}).getDuplicateCandidates(TOKEN, 'NCL-2026-STF001', {radius: 100});
  assert.equal(fetchImpl.calls[0].url, BASE + '/staff/complaints/NCL-2026-STF001/duplicates?radius=100');
  assert.equal(result.candidates.length, 1, 'unknown labels are dropped');
  assert.ok(!JSON.stringify(result).includes('secret text') && !JSON.stringify(result).includes('13.1'), 'no extra fields kept');

  const posts = recorder(json(200, staffCase({mergedInto: 'NCL-2026-STF002', merges: [{id: 'mg_' + 'M'.repeat(16), duplicate: 'NCL-2026-STF001',
    canonical: 'NCL-2026-STF002', reason: 'same', score: 0.9, label: 'likely_duplicate', by: 'ravi', at: 'z', revertedAt: null}, {id: 'bad'}]})));
  const http = createApiClient({baseUrl: BASE, fetchImpl: posts});
  const merged = await http.mergeComplaint(TOKEN, 'NCL-2026-STF001', {canonical: 'NCL-2026-STF002', reason: 'same'});
  await http.revertMerge(TOKEN, 'mg_' + 'M'.repeat(16), 'oops');
  await http.dismissDuplicate(TOKEN, 'NCL-2026-STF001', 'NCL-2026-STF003', 'different');
  assert.deepEqual(posts.calls.map(c => [c.url.replace(BASE, ''), JSON.parse(c.init.body)]), [
    ['/staff/complaints/NCL-2026-STF001/merge', {canonical: 'NCL-2026-STF002', reason: 'same'}],
    ['/staff/merges/mg_' + 'M'.repeat(16) + '/revert', {reason: 'oops'}],
    ['/staff/complaints/NCL-2026-STF001/duplicates/NCL-2026-STF003/dismiss', {reason: 'different'}],
  ]);
  assert.equal(merged.mergedInto, 'NCL-2026-STF002');
  assert.equal(merged.merges.length, 1, 'malformed merge records are dropped');
});
