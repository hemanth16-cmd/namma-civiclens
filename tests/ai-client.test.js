import {test} from 'node:test';
import assert from 'node:assert/strict';

// AI analysis client: request shapes, token placement, and strict mapping. The browser never sees an
// API key or storage key, and keeps only the fields it displays.
const {createApiClient, toAiAnalysis, toRepairAssessment, toAiDetection, ApiError} = await import('../dist/api-client.js');

const BASE = 'http://localhost:8000/api/v1';
const TOKEN = 'staff_' + 'S'.repeat(40);
const ID = 'NCL-2026-AI0001';
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

const INFERENCE = {
  id: 'ai_x', evidenceId: 'ev_' + 'a'.repeat(22), evidenceRole: 'original', detector: 'pavement', provider: 'roboflow', mechanism: 'model',
  modelId: 'pavement-gvib5/1', modelVersion: '1', classPrompt: null, state: 'completed', originalClass: 'pothole', detectedCategory: 'Potholes',
  topConfidence: 0.91, detectionCount: 1, imageWidth: 1000, imageHeight: 750, errorCode: null, attempts: 1, createdAt: 'z', startedAt: 'z', completedAt: 'z',
  confidence: {max: 0.91}, storageKey: 'SECRET-KEY', apiKey: 'SECRET',
  detections: [{className: 'pothole', category: 'Potholes', confidence: 0.91, bbox: {x: 425, y: 367.5, width: 150, height: 90}, polygon: [[1, 2]]},
    {className: 'bad', confidence: 7, bbox: {}}]
};
const ANALYSIS = {complaintId: ID, category: 'Potholes', ai: {enabled: true, provider: 'roboflow', reason: null, disclosure: 'd', thresholds: {present: 0.5, low: 0.25}},
  routing: {detectors: ['pothole', 'pavement'], relevantCategories: ['Potholes']}, inferences: [INFERENCE, {id: 'ai_y', state: 'exploded'}]};
const ASSESSMENT = {
  id: 'ra_' + 'b'.repeat(22), workOrderId: 'wo_' + 'c'.repeat(16), originalEvidenceId: 'ev_' + 'a'.repeat(22), repairEvidenceId: 'ev_' + 'd'.repeat(22),
  state: 'completed', suggestedOutcome: 'likely_repaired', verificationConfidence: 0.72, locationCheck: 'match', gpsDistanceM: 4.2,
  details: {reasons: ['Same place'], visualMatch: 'strong', alignment: {inliers: 900, homography: [[1]]}, gps: {lat: 13}}, errorCode: null, attempts: 1,
  requestedBy: 'asha', review: {status: 'pending_review', reviewer: null, reviewerName: null, note: '', reviewedAt: null},
  verifiedResolution: 'unverified', createdAt: 'z', completedAt: 'z', updatedAt: 'z'
};

test('AI results are read with the staff token in the header and mapped strictly', async () => {
  const fetchImpl = recorder(json(200, ANALYSIS));
  const data = await createApiClient({baseUrl: BASE, fetchImpl}).getAiAnalysis(TOKEN, ID);
  const [call] = fetchImpl.calls;
  assert.equal(call.url, `${BASE}/staff/complaints/${ID}/ai`);
  assert.equal(call.init.headers.Authorization, 'Bearer ' + TOKEN);
  assert.equal(data.inferences.length, 1, 'unknown states are dropped');
  const [i] = data.inferences;
  assert.equal(i.modelId, 'pavement-gvib5/1');
  assert.equal(i.detections.length, 1, 'malformed detections are dropped');
  assert.deepEqual(i.detections[0], {className: 'pothole', category: 'Potholes', confidence: 0.91, bbox: {x: 425, y: 367.5, width: 150, height: 90}});
  assert.ok(!JSON.stringify(data).includes('SECRET'), 'no storage or API keys are kept');
});

test('retry posts to the retry route', async () => {
  const fetchImpl = recorder(json(202, {...ANALYSIS, reset: 1}));
  const data = await createApiClient({baseUrl: BASE, fetchImpl}).retryAiAnalysis(TOKEN, ID);
  assert.equal(fetchImpl.calls[0].url, `${BASE}/staff/complaints/${ID}/ai/retry`);
  assert.equal(fetchImpl.calls[0].init.method, 'POST');
  assert.equal(data.reset, 1);
});

test('verify-repair sends only the optional evidence ID; review sends decision and note', async () => {
  const assessments = {complaintId: ID, ai: ANALYSIS.ai, assessments: [ASSESSMENT]};
  let fetchImpl = recorder(json(202, assessments));
  let http = createApiClient({baseUrl: BASE, fetchImpl});
  await http.verifyRepair(TOKEN, ID);
  assert.deepEqual(JSON.parse(fetchImpl.calls[0].init.body), {});
  await http.verifyRepair(TOKEN, ID, ASSESSMENT.repairEvidenceId);
  assert.deepEqual(JSON.parse(fetchImpl.calls[1].init.body), {repairEvidenceId: ASSESSMENT.repairEvidenceId});
  assert.equal(fetchImpl.calls[1].url, `${BASE}/staff/complaints/${ID}/verify-repair`);

  fetchImpl = recorder(json(200, {...ASSESSMENT, review: {...ASSESSMENT.review, status: 'approved', reviewer: 'ravi'}, verifiedResolution: 'verified_repaired'}));
  http = createApiClient({baseUrl: BASE, fetchImpl});
  const a = await http.reviewRepairAssessment(TOKEN, ID, ASSESSMENT.id, {decision: 'approved', note: 'ok'});
  assert.equal(fetchImpl.calls[0].url, `${BASE}/staff/complaints/${ID}/repair-assessment/${ASSESSMENT.id}/review`);
  assert.deepEqual(JSON.parse(fetchImpl.calls[0].init.body), {decision: 'approved', note: 'ok'});
  assert.equal(a.verifiedResolution, 'verified_repaired');
});

test('repair assessments keep the suggestion, its confidence and the human review apart', () => {
  const a = toRepairAssessment(ASSESSMENT);
  assert.equal(a.suggestedOutcome, 'likely_repaired');
  assert.equal(a.verificationConfidence, 0.72);
  assert.equal(a.review.status, 'pending_review');
  assert.equal(a.verifiedResolution, 'unverified');
  assert.equal(a.matchedFeatures, 900);
  assert.ok(!('homography' in a) && !('gps' in a), 'raw alignment and GPS details are not kept');
  assert.equal(toRepairAssessment({...ASSESSMENT, suggestedOutcome: 'fixed', verifiedResolution: 'yes'}).suggestedOutcome, null);
  assert.equal(toRepairAssessment({...ASSESSMENT, verifiedResolution: 'yes'}).verifiedResolution, 'unverified');
  assert.equal(toRepairAssessment({id: 'nope'}), null);
});

test('malformed AI responses are refused', async () => {
  assert.throws(() => toAiAnalysis({inferences: 'x'}), ApiError);
  assert.equal(toAiDetection({className: 'x', confidence: 0.5}), null);
  const http = createApiClient({baseUrl: BASE, fetchImpl: recorder(json(200, {assessments: null}))});
  await assert.rejects(http.getRepairAssessments(TOKEN, ID), e => e.code === 'malformed_response');
});

test('the public disclosure is read without credentials', async () => {
  const fetchImpl = recorder(json(200, {enabled: true, disclosure: 'Photos may be analysed by Roboflow.'}));
  const d = await createApiClient({baseUrl: BASE, fetchImpl}).getAiDisclosure();
  assert.equal(fetchImpl.calls[0].url, `${BASE}/ai`);
  assert.equal(fetchImpl.calls[0].init.headers.Authorization, undefined);
  assert.deepEqual(d, {enabled: true, disclosure: 'Photos may be analysed by Roboflow.'});
});
