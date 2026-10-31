import {test} from 'node:test';
import assert from 'node:assert/strict';

// Browser-storage shim for Node. Individual tests can make setItem fail to simulate full storage.
const storage = new Map();
const shim = {getItem: k => (storage.has(k) ? storage.get(k) : null), setItem: (k, v) => storage.set(k, String(v)), removeItem: k => storage.delete(k)};
globalThis.localStorage = shim;
globalThis.sessionStorage = shim;

const {api, mediaDB, StorageError, AccessError, assessPriority, evidenceConfidence, categories, citizenResponses, resolutionState} = await import('../dist/services.js');
const {filterRecords} = await import('../dist/filters.js');
const {resolutionState: resolution} = await import('../dist/presentation.js');
const {verificationStates} = await import('../dist/services.js');

const media = new Map();
mediaDB.put = async (k, v) => { media.set(k, v); };
mediaDB.get = async k => media.get(k) ?? null;
mediaDB.delete = async k => { media.delete(k); };
mediaDB.clear = async () => media.clear();

const admin = {role: 'official', email: 'official@civiclens.demo', name: 'Civic official'};
const owner = {role: 'resident', email: 'owner@example.com', name: 'Owner'};
const neighbour = {role: 'resident', email: 'neighbour@example.com', name: 'Neighbour'};
const publicViewer = {role: 'public', email: null};
const gps = {lat: 13.0, lng: 77.6, accuracy: 8, source: 'Device GPS', timestamp: Date.now(), address: 'Yelahanka, Bengaluru'};
const newReport = (overrides = {}) => ({category: 'Potholes', description: 'Test evidence', gps, created: new Date().toISOString(), owner: null, mediaType: 'photo', shareEvidence: false, neighbourhood: 'Yelahanka', ...overrides});

test('report lifecycle persists, public projection strips secrets, verification stays independent', async () => {
  await api.reset();
  const r = await api.create(newReport({owner: owner.email}), new Blob(['photo-bytes'], {type: 'image/jpeg'}));
  assert.match(r.id, /^NCL-\d{4}-[A-Z0-9]+$/);
  assert.ok(r.claim);
  assert.equal(r.area, 'Yelahanka');
  assert.equal(r.capturedAt, r.created);
  const pub = await api.publicRecord(r.id);
  for (const key of ['claim', 'owner', 'gps', 'internal', 'description', 'reviews']) assert.equal(pub[key], undefined, key);
  assert.equal(pub.area, 'Yelahanka', 'public view shows only the neighbourhood');
  await assert.rejects(api.claim(r.id, 'wrong-code', owner.email));
  await api.claim(r.id, r.claim, owner.email);
  await api.updateCase(admin, r.id, {status: 'Marked Resolved', internal: 'private team note', publicText: 'Repair recorded'});
  const after = await api.publicRecord(r.id);
  assert.equal(after.status, 'Marked Resolved');
  assert.equal(after.verification, 'Pending Verification');
  await api.updateCase(admin, r.id, {verification: 'Disputed', note: 'Inspector disagreed'});
  assert.equal((await api.publicRecord(r.id)).status, 'Marked Resolved');
  assert.equal((await api.publicRecord(r.id)).verification, 'Disputed');
  assert.equal((await api.list(admin)).find(x => x.id === r.id).internal, undefined, 'list never exposes internal notes');
});

test('demo login rejects invalid credentials and supports local registration', async () => {
  await assert.rejects(api.login('official', 'official@civiclens.demo', 'wrong'));
  assert.equal((await api.login('official', 'official@civiclens.demo', 'CivicDemo2026')).role, 'official');
  await api.login('resident', 'new@example.com', 'demo123', true, 'Test');
  assert.equal((await api.login('resident', 'new@example.com', 'demo123')).name, 'Test');
});

test('case updates and work orders are admin-only', async () => {
  await assert.rejects(api.updateCase(owner, 'NCL-2026-DEMO01', {status: 'Marked Resolved'}), AccessError);
  await assert.rejects(api.updateCase(publicViewer, 'NCL-2026-DEMO01', {status: 'Marked Resolved'}), AccessError);
  await assert.rejects(api.addWorkOrder(owner, 'NCL-2026-DEMO06', {team: 'Team'}), AccessError);
  await assert.rejects(api.requestReinspection(owner, 'NCL-2026-DEMO01', 'x'), AccessError);
  const order = await api.addWorkOrder(admin, 'NCL-2026-DEMO06', {team: 'Drainage response · North', due: '2026-11-15'});
  assert.equal(order.status, 'Open');
  assert.equal((await api.detail('NCL-2026-DEMO06', admin)).status, 'Assigned');
});

test('community projection shares opted-in evidence, keeps private repairs hidden, and never exposes claim or internal data', async () => {
  const r = await api.create(newReport({category: 'Garbage accumulation', owner: owner.email, description: 'Community fixture'}), new Blob(['before'], {type: 'image/jpeg'}));
  await api.updateCase(admin, r.id, {status: 'Assigned', internal: 'private operations note', team: 'Solid waste · West', publicText: 'Team assigned'});
  const neighbourView = await api.detail(r.id, neighbour);
  assert.equal(neighbourView.gps, null);
  assert.equal(neighbourView.claim, undefined);
  assert.equal(neighbourView.internal, undefined);
  assert.equal(neighbourView.owner, undefined);
  assert.equal(await api.mediaFor(neighbour, r.id, {kind: 'before'}), null, 'original stays private until shared');
  await api.shareOriginal(owner, r.id, true);
  assert.equal((await api.detail(r.id, neighbour)).gps.lat, 13);
  assert.ok(await api.mediaFor(neighbour, r.id, {kind: 'before'}));
  await assert.rejects(api.shareOriginal(neighbour, r.id, true), AccessError);

  await api.addRepair(admin, r.id, {blob: new Blob(['after-private'], {type: 'image/jpeg'}), shared: false, gps: null, capturedAt: null});
  assert.equal((await api.detail(r.id, neighbour)).repairs.length, 0, 'unshared repair is hidden from community');
  assert.equal(await api.communityMedia(r.id, 'after'), null);
  const shared = await api.addRepair(admin, r.id, {blob: new Blob(['after-shared'], {type: 'image/jpeg'}), shared: true, gps: null, capturedAt: null});
  assert.ok(await api.mediaFor(neighbour, r.id, {kind: 'after', repairId: shared.id}));
  await api.addReview(r.id, {author: 'Neighbour', authorEmail: neighbour.email, text: 'The issue is still visible here.', rating: 2});
  const view = await api.detail(r.id, neighbour);
  assert.equal(view.reviews.length, 1);
  assert.equal(view.reviews[0].authorEmail, undefined, 'review authors are not exposed by email');
  assert.equal(view.status, 'Assigned');
  assert.equal(view.verification, 'Pending Verification');
  await assert.rejects(api.addReview(r.id, {author: 'N', text: 'x', rating: 6}));
});

test('repair history is append-only: a later repair never erases an earlier record', async () => {
  const r = await api.create(newReport(), new Blob(['before']));
  const first = await api.addRepair(admin, r.id, {blob: new Blob(['r1'], {type: 'image/jpeg'}), shared: true, gps: {lat: 13.01, lng: 77.61, source: 'Submitted EXIF metadata'}, capturedAt: '2026-10-20T04:00:00.000Z'});
  const second = await api.addRepair(admin, r.id, {blob: new Blob(['r2'], {type: 'image/jpeg'}), shared: true, gps: null, capturedAt: null});
  const detail = await api.detail(r.id, admin);
  assert.deepEqual(detail.repairs.map(x => x.id), [first.id, second.id]);
  assert.equal(detail.repairs[1].gps, null, 'missing EXIF stays missing; original coordinates are not copied');
  assert.ok(await api.mediaFor(admin, r.id, {kind: 'after', repairId: first.id}), 'earlier repair media is retained');
});

test('citizen responses keep lifecycle separate, record disputes and recurrence, and never overwrite a dispute with pending', async () => {
  const r = await api.create(newReport(), new Blob(['before']));
  await assert.rejects(api.citizenResponse(owner, r.id, 'Appears Resolved'), /after the civic team marks/);
  await api.updateCase(admin, r.id, {status: 'Marked Resolved'});
  await api.citizenResponse(neighbour, r.id, 'Still Present');
  let d = await api.detail(r.id, admin);
  assert.equal(d.status, 'Marked Resolved');
  assert.equal(d.verification, 'Disputed');
  assert.equal(d.recurrence, 1);
  await api.citizenResponse(neighbour, r.id, 'Cannot Confirm');
  d = await api.detail(r.id, admin);
  assert.equal(d.verification, 'Disputed', 'Cannot Confirm does not erase a dispute');
  assert.equal(d.verificationLog.length, 2, 'every response remains in history');
  assert.deepEqual(citizenResponses, ['Appears Resolved', 'Still Present', 'Cannot Confirm']);
});

test('reinspection keeps the dispute history and the map shows the case as unresolved', async () => {
  const r = await api.create(newReport(), new Blob(['before']));
  await api.updateCase(admin, r.id, {status: 'Marked Resolved'});
  await api.requestReinspection(admin, r.id, 'Inspection needed after complaint.');
  const d = await api.detail(r.id, admin);
  assert.equal(d.verification, 'Reinspection Required');
  assert.equal(resolution(d).solved, false);
  assert.ok(d.verificationLog.some(e => e.to === 'Reinspection Required'));
});

test('reviews persist without changing lifecycle or verification', async () => {
  const before = await api.detail('NCL-2026-DEMO03', admin);
  await api.addReview('NCL-2026-DEMO03', {author: 'Tester', authorEmail: neighbour.email, text: 'Looks good from the street.', rating: 4});
  const after = await api.detail('NCL-2026-DEMO03', admin);
  assert.equal(after.reviews.length, before.reviews.length + 1);
  assert.equal(after.status, before.status);
  assert.equal(after.verification, before.verification);
});

test('notifications respect per-user preferences and read state', async () => {
  const feed = await api.notificationsFor(admin, {updates: true, verification: false}, null);
  assert.ok(feed.every(e => e.kind === 'update'), 'verification events hidden when the preference is off');
  const none = await api.notificationsFor(admin, {updates: false, verification: false}, null);
  assert.equal(none.length, 0);
  const unread = await api.notificationsFor(admin, {updates: true, verification: true}, new Date().toISOString());
  assert.ok(unread.every(e => !e.unread));
});

test('explainable priority contributions sum to the score and evidence confidence stays separate', () => {
  const r = {id: 'x', category: 'Potholes', status: 'Submitted', verification: 'Pending Verification', created: '2026-10-01T00:00:00.000Z', updated: '2026-10-01T00:00:00.000Z', exposure: 0.8, recurrence: 2, gps: null, sample: true};
  const p = assessPriority(r, Date.parse('2026-10-31T00:00:00.000Z'));
  const sum = Object.values(p.contributions).reduce((a, b) => a + b, 0);
  assert.ok(Math.abs(sum - p.score) <= 2, 'contributions reconcile with the score');
  assert.equal(p.weights.severity, 0.35);
  assert.equal(p.confidence, 'Low');
  assert.match(p.flag, /Verify evidence first/, 'severe case with weak evidence is flagged, not ranked low');
  assert.equal(evidenceConfidence({gps: {lat: 1, lng: 1}, sample: false, verification: 'Citizen Confirmed'}), 'High');
  assert.equal(assessPriority({...r, exposure: undefined}).exposureSource.startsWith('Default assumption'), true);
});

test('filters produce the same result set for lists and map input', () => {
  const rows = [
    {id: 'a', title: 'Pothole at junction', area: 'Hebbal', category: 'Potholes', status: 'Assigned', verification: 'Pending Verification', created: '2026-10-02'},
    {id: 'b', title: 'Garbage beside market', area: 'Malleswaram', category: 'Garbage accumulation', status: 'Marked Resolved', verification: 'Disputed', created: '2026-10-03'},
    {id: 'c', title: 'Footpath restored', area: 'Indiranagar', category: 'Damaged footpaths', status: 'Marked Resolved', verification: 'Pending Verification', created: '2026-10-04'}
  ];
  assert.deepEqual(filterRecords(rows, {q: 'market'}).map(r => r.id), ['b']);
  assert.deepEqual(filterRecords(rows, {resolution: 'unresolved'}).map(r => r.id), ['a', 'b'], 'disputed resolutions are red');
  assert.deepEqual(filterRecords(rows, {resolution: 'resolved'}).map(r => r.id), ['c']);
  assert.deepEqual(filterRecords(rows, {category: 'Potholes', status: 'Assigned'}).map(r => r.id), ['a']);
  assert.deepEqual(filterRecords(rows, {area: 'Nowhere'}), []);
});

test('storage failure does not report success and does not orphan the evidence file', async () => {
  await api.reset();
  const originalSet = shim.setItem;
  shim.setItem = (k, v) => { if (k === 'ncl-records-v2') throw new Error('QuotaExceededError'); originalSet(k, v); };
  try {
    const before = media.size;
    await assert.rejects(api.create(newReport(), new Blob(['x'])), err => err instanceof StorageError && /storage is full|blocked/.test(err.message));
    assert.equal(media.size, before, 'failed submission leaves no orphaned media');
  } finally {
    shim.setItem = originalSet;
  }
});

test('categories have distinct symbols and disputed resolutions remain red', async () => {
  const {categoryIcon, resolutionState: rs, markerSVG} = await import('../dist/presentation.js');
  assert.equal(new Set(categories.map(categoryIcon)).size, categories.length);
  assert.equal(rs({status: 'Marked Resolved', verification: 'Pending Verification'}).solved, true);
  assert.equal(rs({status: 'Marked Resolved', verification: 'Disputed'}).solved, false);
  assert.equal(rs({status: 'Marked Resolved', verification: 'Reinspection Required'}).solved, false);
  assert.equal(rs({status: 'In Progress', verification: 'Pending Verification'}).solved, false);
  assert.match(markerSVG('Potholes', true), /#21835B/);
  assert.match(markerSVG('Garbage accumulation', false), /#D34F48/);
  assert.equal(verificationStates.length, 4);
});

test('public tracking of the sample record never exposes restricted fields', async () => {
  const pub = await api.publicRecord('NCL-2026-DEMO05');
  assert.equal(pub.verification, 'Disputed');
  for (const key of ['claim', 'owner', 'gps', 'internal', 'description', 'followers', 'repairs']) assert.equal(pub[key], undefined, key);
});

test('citizen evidence limits match the backend upload limits (25 MB photo, 50 MB video)', async () => {
  const {MAX_PHOTO_BYTES, MAX_VIDEO_BYTES, MAX_REVIEW_VIDEO_BYTES} = await import('../dist/services.js');
  assert.equal(MAX_PHOTO_BYTES, 26214400);
  assert.equal(MAX_VIDEO_BYTES, 52428800);
  assert.ok(MAX_REVIEW_VIDEO_BYTES > MAX_VIDEO_BYTES, 'local-only admin review may allow larger clips');
  await api.reset();
  const tooBig = new Blob([new Uint8Array(MAX_VIDEO_BYTES + 1)], {type: 'video/webm'});
  await assert.rejects(api.create(newReport({mediaType: 'video'}), tooBig), /too large/);
});
