import {test} from 'node:test';
import assert from 'node:assert/strict';
import {wards, zones, wardForRecord} from '../dist/ward-catalog.js';
import {filterRecords} from '../dist/filters.js';

test('ward directory has 198 distinct real wards, without the testing ward', () => {
  assert.equal(wards.length, 198);
  assert.equal(new Set(wards.map(w => w.wardNumber)).size, 198);
  assert.ok(zones.length > 1);
  assert.ok(!wards.some(w => /testing/i.test(w.wardName)));
});

test('ward matching is exact and never guesses a location outside the directory', () => {
  assert.equal(wardForRecord({area: ' JAKKUR '}).wardNumber, 5);
  assert.equal(wardForRecord({area: 'Chikkahagade', gps: {lat: 12.731671, lng: 77.707881}}), null);
  assert.equal(wardForRecord({area: 'Near Jakkur'}), null);
});

test('zone and ward combine with existing category, lifecycle and verification filters', () => {
  const rows = [
    {id: 'a', area: 'Jakkur', category: 'Potholes', status: 'Under Construction', verification: 'Pending Verification'},
    {id: 'b', area: 'Thanisandra', category: 'Potholes', status: 'Under Construction', verification: 'Pending Verification'},
    {id: 'c', area: 'Chikkahagade', category: 'Potholes', status: 'Submitted'}
  ];
  assert.deepEqual(filterRecords(rows, {zone: '1', ward: '5', status: 'Under Construction', category: 'Potholes', verification: 'Pending Verification'}).map(r => r.id), ['a']);
  assert.deepEqual(filterRecords(rows, {zone: 'unassigned'}).map(r => r.id), ['c']);
  assert.equal(filterRecords(rows, {ward: '5', category: 'Garbage accumulation'}).length, 0);
});
