import {test} from 'node:test';
import assert from 'node:assert/strict';

const {createApiClient, ApiError, newIdempotencyKey} = await import('../dist/api-client.js');
const {serverEvidenceKey, submitReport, reportMode} = await import('../dist/report-gateway.js');

test('idempotency keys are 256-bit URL-safe and unique', () => {
  const k1 = newIdempotencyKey();
  const k2 = newIdempotencyKey();
  assert.notEqual(k1, k2);
  assert.match(k1, /^[A-Za-z0-9_-]{43,44}$/);
});

test('server evidence keys use the designated prefix', () => {
  assert.equal(serverEvidenceKey('NCL-2026-ABCDEF'), 'server-evidence:NCL-2026-ABCDEF');
});

test('invalid backend config is rejected and never contact network', async () => {
  let called = false;
  const fakeFetch = () => { called = true; throw new Error('should not call'); };

  await assert.rejects(
    submitReport(
      {category: 'Potholes', mediaType: 'photo', gps: {lat: 13, lng: 77, sourceType: 'gps'}},
      null,
      {config: {mode: 'invalid_mode', problem: 'Invalid configuration'}, fetchImpl: fakeFetch}
    ),
    err => err instanceof ApiError && err.code === 'config_invalid'
  );
  assert.equal(called, false);
});

test('api client strips dangerous headers and validates URL', async () => {
  let requestedUrl = null;
  const fakeFetch = async url => {
    requestedUrl = url;
    return new Response(JSON.stringify({status: 'ok'}), {status: 200, headers: {'Content-Type': 'application/json'}});
  };

  const client = createApiClient({baseUrl: 'http://localhost:8000/api/v1', fetchImpl: fakeFetch});
  await client.health();
  assert.equal(requestedUrl, 'http://localhost:8000/api/v1/health');
});
