import {test} from 'node:test';
import assert from 'node:assert/strict';

import {createApiClient, toStaffComplaint, toWorkOrder, toPublicRecord} from '../dist/api-client.js';
import {distanceM} from '../dist/address-service.js';
import {inspectJpegBuffer} from '../dist/exif.js';
import {renderStampedPhoto, evidenceRef, stampRows} from '../dist/photo-stamp.js';
import {createLocator} from '../dist/location-service.js';

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

test('uploadCaseRepairEvidence and uploadRepairEvidence send GPS metadata in multipart fields', async () => {
  const receipt = {
    evidenceId: 'ev_' + 'r'.repeat(22),
    mediaType: 'photo',
    contentType: 'image/jpeg',
    byteSize: 12345,
    sha256: 'a'.repeat(64),
    capturedAt: '2026-10-30T10:00:00.000Z',
    uploadedAt: '2026-10-30T10:00:05.000Z'
  };
  const fetchImpl = recorder(json(201, receipt));
  const http = createApiClient({baseUrl: BASE, fetchImpl});

  const blob = new Blob([new Uint8Array([0xff, 0xd8, 0xff, 0xd9])], {type: 'image/jpeg'});
  const meta = {
    capturedAt: '2026-10-30T10:00:00.000Z',
    gps: {
      lat: 12.9716,
      lng: 77.5946,
      accuracy: 12.5,
      source: 'Browser GPS',
      sourceType: 'gps',
      timestamp: '2026-10-30T10:00:00.000Z',
      address: 'MG Road'
    }
  };

  // Case repair upload
  await http.uploadCaseRepairEvidence(TOKEN, 'NCL-2026-ABC123', blob, meta);
  assert.equal(fetchImpl.calls.length, 1);
  const [call1] = fetchImpl.calls;
  assert.equal(call1.url, `${BASE}/staff/complaints/NCL-2026-ABC123/repair-evidence`);
  assert.ok(call1.init.body instanceof FormData);
  assert.equal(call1.init.body.get('lat'), '12.9716');
  assert.equal(call1.init.body.get('lng'), '77.5946');
  assert.equal(call1.init.body.get('accuracy'), '12.5');
  assert.equal(call1.init.body.get('source'), 'Browser GPS');
  assert.equal(call1.init.body.get('sourceType'), 'gps');
  assert.equal(call1.init.body.get('address'), 'MG Road');
  assert.equal(call1.init.body.get('capturedAt'), '2026-10-30T10:00:00.000Z');

  // Work order repair upload
  await http.uploadRepairEvidence(TOKEN, 'wo_' + 'W'.repeat(16), blob, meta);
  assert.equal(fetchImpl.calls.length, 2);
  const call2 = fetchImpl.calls[1];
  assert.equal(call2.url, `${BASE}/staff/work-orders/wo_${'W'.repeat(16)}/evidence`);
  assert.ok(call2.init.body instanceof FormData);
  assert.equal(call2.init.body.get('lat'), '12.9716');
  assert.equal(call2.init.body.get('lng'), '77.5946');
  assert.equal(call2.init.body.get('capturedAt'), '2026-10-30T10:00:00.000Z');
});

test('toStaffComplaint maps repair evidence GPS and capture time while dropping internal secrets', () => {
  const raw = {
    id: 'NCL-2026-STF001',
    category: 'Potholes',
    area: 'Hebbal',
    description: 'Deep pothole',
    status: 'Work Completed',
    verification: 'Pending Verification',
    department: 'Road maintenance',
    created: '2026-10-30T08:00:00.000Z',
    gps: {lat: 13.035, lng: 77.597, accuracy: 15, source: 'GPS', sourceType: 'gps', timestamp: null, address: null},
    timeline: [],
    repairEvidence: [
      {
        evidenceId: 'ev_' + '1'.repeat(22),
        mediaType: 'photo',
        capturedAt: '2026-10-30T09:30:00.000Z',
        uploadedAt: '2026-10-30T09:30:10.000Z',
        gps: {
          lat: 13.0352,
          lng: 77.5971,
          accuracy: 10,
          source: 'Browser GPS',
          sourceType: 'gps',
          timestamp: '2026-10-30T09:29:58.000Z',
          address: 'Bellary Road'
        },
        storageKey: 'SUPER_SECRET_KEY',
        uploadedByStaffPk: 42
      },
      {
        evidenceId: 'ev_' + '2'.repeat(22),
        mediaType: 'photo',
        capturedAt: null,
        uploadedAt: '2026-10-30T09:45:00.000Z',
        gps: null
      }
    ]
  };

  const c = toStaffComplaint(raw);
  assert.equal(c.repairEvidence.length, 2);

  // First repair has full GPS
  const r1 = c.repairEvidence[0];
  assert.equal(r1.evidenceId, 'ev_' + '1'.repeat(22));
  assert.equal(r1.capturedAt, '2026-10-30T09:30:00.000Z');
  assert.deepEqual(r1.gps, {
    lat: 13.0352,
    lng: 77.5971,
    accuracy: 10,
    source: 'Browser GPS',
    sourceType: 'gps',
    timestamp: '2026-10-30T09:29:58.000Z',
    address: 'Bellary Road'
  });
  assert.equal(r1.storageKey, undefined);
  assert.equal(r1.uploadedByStaffPk, undefined);

  // Second repair has null GPS
  const r2 = c.repairEvidence[1];
  assert.equal(r2.gps, null);
  assert.equal(r2.capturedAt, null);
});

test('toWorkOrder maps evidence GPS when present and omits when absent', () => {
  const wo = {
    id: 'wo_' + 'A'.repeat(16),
    complaintId: 'NCL-2026-STF001',
    evidence: [
      {
        phase: 'before',
        evidenceId: 'ev_' + 'b'.repeat(22),
        role: 'original',
        contentType: 'image/jpeg',
        byteSize: 100,
        uploadedAt: '2026-10-30T08:00:00.000Z'
      },
      {
        phase: 'after',
        evidenceId: 'ev_' + 'a'.repeat(22),
        role: 'repair',
        contentType: 'image/jpeg',
        byteSize: 200,
        uploadedAt: '2026-10-30T10:00:00.000Z',
        capturedAt: '2026-10-30T09:59:50.000Z',
        gps: {
          lat: 13.0351,
          lng: 77.5972,
          accuracy: 8,
          source: 'EXIF',
          sourceType: 'gps',
          timestamp: '2026-10-30T09:59:50.000Z',
          address: null
        }
      }
    ]
  };

  const parsed = toWorkOrder(wo);
  assert.equal(parsed.evidence.length, 2);
  assert.equal(parsed.evidence[0].gps, undefined);
  assert.equal(parsed.evidence[0].capturedAt, undefined);
  assert.deepEqual(parsed.evidence[1].gps, {
    lat: 13.0351,
    lng: 77.5972,
    accuracy: 8,
    source: 'EXIF',
    sourceType: 'gps',
    timestamp: '2026-10-30T09:59:50.000Z',
    address: null
  });
  assert.equal(parsed.evidence[1].capturedAt, '2026-10-30T09:59:50.000Z');
});

test('distanceM correctly measures distance between reported issue and repair', () => {
  const orig = {lat: 12.9716, lng: 77.5946, accuracy: 10};
  const closeRepair = {lat: 12.9718, lng: 77.5948, accuracy: 8};
  const farRepair = {lat: 13.0350, lng: 77.5970, accuracy: 15}; // ~7 km away

  const dClose = Math.round(distanceM(orig, closeRepair));
  assert.ok(dClose < 50, `Expected close distance, got ${dClose}`);

  const dFar = Math.round(distanceM(orig, farRepair));
  assert.ok(dFar > 5000, `Expected far distance, got ${dFar}`);
});

test('public tracking never exposes GPS coordinates or repair storage keys', () => {
  const raw = {
    id: 'NCL-2026-PUB001',
    category: 'Potholes',
    area: 'Indiranagar',
    status: 'Marked Resolved',
    verification: 'Confirmed by citizen',
    department: 'Roads',
    updated: '2026-10-30T12:00:00.000Z',
    timeline: [],
    publicEvidence: [
      {
        evidenceId: 'ev_' + '1'.repeat(22),
        phase: 'before',
        mediaType: 'photo',
        uploadedAt: '2026-10-30T08:00:00.000Z',
        gps_lat: 12.9716,
        gps_lng: 77.5946,
        storageKey: 'SECRET_STORAGE_KEY'
      },
      {
        evidenceId: 'ev_' + '2'.repeat(22),
        phase: 'after',
        mediaType: 'photo',
        uploadedAt: '2026-10-30T11:00:00.000Z',
        gps_lat: 12.9718,
        gps_lng: 77.5948,
        storageKey: 'ANOTHER_SECRET_KEY'
      }
    ]
  };

  const tracked = toPublicRecord(raw);
  assert.equal(tracked.publicEvidence.length, 2);
  for (const item of tracked.publicEvidence) {
    assert.equal(item.gps_lat, undefined);
    assert.equal(item.gps_lng, undefined);
    assert.equal(item.gps, undefined);
    assert.equal(item.storageKey, undefined);
    assert.ok(['before', 'after'].includes(item.phase));
  }
});

test('photo stamp rows include repair location and capture time without credentials or claim codes', () => {
  const rows = stampRows({
    complaintId: 'NCL-2026-REP001',
    evidenceRef: 'EV-1234-5678',
    includeLocation: true,
    gps: {lat: 12.971598, lng: 77.594562, accuracy: 12, sourceType: 'gps'},
    address: '100ft Road, Indiranagar',
    capturedAt: '2026-10-30T10:00:00.000Z'
  });

  const labels = rows.map(r => r.label);
  assert.ok(labels.includes('Complaint ID'));
  assert.ok(labels.includes('Evidence ref'));
  assert.ok(labels.includes('Location'));
  assert.ok(labels.includes('Latitude'));
  assert.ok(labels.includes('Longitude'));
  assert.ok(labels.includes('Captured'));

  // Ensure no credentials or claim codes
  const stringified = JSON.stringify(rows);
  assert.ok(!stringified.toLowerCase().includes('claim'));
  assert.ok(!stringified.toLowerCase().includes('password'));
  assert.ok(!stringified.toLowerCase().includes('token'));
});

test('createLocator watcher stops when requested and provides usable fix only when fresh', () => {
  let cleared = false;
  let watchCallback = null;
  const mockGeo = {
    watchPosition(onPos, onErr) {
      watchCallback = onPos;
      return 123;
    },
    clearWatch(id) {
      if (id === 123) cleared = true;
    }
  };

  const locator = createLocator({
    geolocation: mockGeo,
    isSecureContext: true,
    now: () => 1000000,
    timers: {setTimeout: (fn) => 1, clearTimeout: () => {}}
  });

  locator.start();
  assert.equal(locator.state.status, 'locating');
  assert.equal(locator.usableFix(1000000), null);

  // Position arrives
  watchCallback({
    coords: {latitude: 12.97, longitude: 77.59, accuracy: 10},
    timestamp: 1000000
  });

  assert.equal(locator.state.status, 'acquired');
  const fix = locator.usableFix(1000000);
  assert.ok(fix);
  assert.equal(fix.lat, 12.97);
  assert.equal(fix.lng, 77.59);

  // Stale check (e.g. 5 minutes later)
  assert.equal(locator.usableFix(1000000 + 300000), null, 'stale fix is not usable');

  locator.stop();
  assert.ok(cleared, 'clearWatch should be called on stop');
});
