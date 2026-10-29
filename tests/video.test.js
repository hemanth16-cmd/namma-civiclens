import {test} from 'node:test';
import assert from 'node:assert/strict';
const storage = new Map();
globalThis.localStorage = {getItem: k => (storage.has(k) ? storage.get(k) : null), setItem: (k, v) => storage.set(k, String(v)), removeItem: k => storage.delete(k)};
const {mediaDB} = await import('../dist/services.js');
const media = new Map();
mediaDB.put = async (k, v) => media.set(k, v);
mediaDB.get = async k => media.get(k) ?? null;
mediaDB.delete = async k => media.delete(k);
const {videoReviewService: videos, videoAnalysisAdapter, findings} = await import('../dist/video-service.js');

const admin = {role: 'official', name: 'Test admin', email: 'official@civiclens.demo'};
const resident = {role: 'resident', name: 'Test resident', email: 'resident@civiclens.demo'};
const publicViewer = {role: 'public'};

test('video review is admin-only and preserves observations, frames and manual findings', async () => {
  await assert.rejects(videos.list(resident), /Admin access/);
  await assert.rejects(videos.list(publicViewer), /Admin access/);
  await assert.rejects(videos.import(admin, new Blob(['image'], {type: 'image/jpeg'})), /supported video/);
  await assert.rejects(videos.import(admin, new Blob([], {type: 'video/mp4'})), /empty/);
  const big = {name: 'big.mp4', type: 'video/mp4', size: 201 * 1024 * 1024, arrayBuffer: async () => new ArrayBuffer(0)};
  await assert.rejects(videos.import(admin, big), /under 200 MB/);
  const clip = await videos.import(admin, new Blob(['video-fixture'], {type: 'video/mp4'}), {caseId: 'NCL-2026-DEMO01'});
  assert.equal(clip.finding, 'Not reviewed');
  await videos.update(admin, clip.id, {duration: 25});
  const frame = new Blob(['frame-fixture'], {type: 'image/jpeg'});
  const o = await videos.observe(admin, clip.id, {time: 8.25, kind: 'Suspected dumping', notes: 'Object appears to be dropped.', frame});
  assert.equal(o.time, 8.25);
  assert.ok(await videos.frame(admin, clip.id, o.id));
  await assert.rejects(videos.observe(admin, clip.id, {time: 30, kind: 'Suspected dumping'}), /valid time/);
  await assert.rejects(videos.observe(admin, clip.id, {time: 3, kind: 'Looks like a crime'}), /observation type/);
  await assert.rejects(videos.media(resident, clip.id), /Admin access/);
  await videos.update(admin, clip.id, {finding: 'Inconclusive', notes: 'Needs further review.'});
  const exported = await videos.export(admin, clip.id);
  assert.equal(exported.overallFinding, 'Inconclusive');
  assert.equal(exported.observations.length, 1);
  assert.equal(exported.observations[0].hasSavedFrame, true);
  assert.equal(exported.observations[0].frameKey, undefined);
  assert.match(exported.reviewType, /Not AI detection/);
  assert.match(exported.observations[0].timestampLabel, /00:08/);
  assert.equal((await videoAnalysisAdapter.analyse()).available, false);
  assert.ok(await videos.media(admin, clip.id));
});

test('linking footage does not change case status or publish footage', async () => {
  const clip = await videos.import(admin, new Blob(['v2'], {type: 'video/webm'}), {caseId: 'NCL-2026-DEMO02'});
  const exported = await videos.export(admin, clip.id);
  assert.match(exported.publication, /private/i);
  assert.match(exported.locationEvidence, /does not establish GPS/);
});

test('observation types are the four manual categories', () => {
  assert.deepEqual(findings, ['Suspected dumping', 'Waste present', 'No dumping observed', 'Inconclusive']);
});
