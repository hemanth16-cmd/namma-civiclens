import {test} from 'node:test';
import assert from 'node:assert/strict';
const {readGPS, inspectJpegBuffer, inServiceArea} = await import('../dist/exif.js');

// Builds a minimal JPEG with an APP1 EXIF segment. Options control which GPS fields are present.
function jpegWith({gps = true, latRef = 'N', lngRef = 'E', lat = [13, 6, 0], lng = [77, 36, 0], dateTime = null, malformedLat = false} = {}) {
  const parts = [];
  const u8 = (...b) => parts.push(Uint8Array.from(b));
  const tiff = [];
  const w16 = (arr, v) => { arr.push(v & 0xff, (v >> 8) & 0xff); };
  const w32 = (arr, v) => { arr.push(v & 0xff, (v >>> 8) & 0xff, (v >>> 16) & 0xff, (v >>> 24) & 0xff); };
  // TIFF header (little endian) and IFD0 at offset 8.
  tiff.push(0x49, 0x49); w16(tiff, 42); w32(tiff, 8);
  const ifd0Count = 1 + (gps ? 1 : 0);
  // Layout: IFD0 entries, next-IFD pointer, then extras.
  const ifd0Start = tiff.length; // 8
  const ifd0Size = 2 + ifd0Count * 12 + 4;
  let cursor = ifd0Start + ifd0Size;
  const exifIFDOffset = cursor;
  const exifSize = 2 + (dateTime ? 1 : 0) * 12 + 4;
  const exifDateOffset = cursor + exifSize;
  const gpsIFDOffset = exifDateOffset + (dateTime ? 20 : 0);
  const gpsSize = 2 + 4 * 12 + 4;
  const gpsValuesOffset = gpsIFDOffset + gpsSize;
  const latOffset = gpsValuesOffset;
  const lngOffset = latOffset + 24;
  const entries = [];
  const entry = (tag, type, count, valueOrOffset) => { const e = []; w16(e, tag); w16(e, type); w32(e, count); if (typeof valueOrOffset === 'number') w32(e, valueOrOffset); else e.push(...valueOrOffset); return e; };
  const inlineAscii = s => [...(s + '\0\0\0\0').slice(0, 4)].map(c => c.charCodeAt(0));
  // IFD0
  tiff.push(...(() => { const b = []; w16(b, ifd0Count); return b; })());
  if (dateTime) {}
  let ifd0 = [];
  if (gps) ifd0.push(entry(0x8825, 4, 1, gpsIFDOffset));
  ifd0.push(entry(0x8769, 4, 1, exifIFDOffset));
  ifd0.forEach(e => tiff.push(...e));
  w32(tiff, 0);
  // Exif IFD
  const exifCount = dateTime ? 1 : 0;
  const exifStart = tiff.length;
  w16(tiff, exifCount);
  if (dateTime) tiff.push(...entry(0x9003, 2, 20, exifDateOffset));
  w32(tiff, 0);
  if (dateTime) tiff.push(...[...(dateTime + '\0')].map(c => c.charCodeAt(0)));
  // GPS IFD
  if (gps) {
    w16(tiff, 4);
    tiff.push(...entry(1, 2, 2, inlineAscii(latRef)));
    tiff.push(...entry(2, 5, 3, latOffset));
    tiff.push(...entry(3, 2, 2, inlineAscii(lngRef)));
    tiff.push(...entry(4, 5, 3, lngOffset));
    w32(tiff, 0);
    const rat = (vals) => vals.flatMap(v => { const b = []; w32(b, v); w32(b, 1); return b; });
    tiff.push(...rat(malformedLat ? [13, 61, 0] : lat));
    tiff.push(...rat(lng));
  }
  const exifBody = [0x45, 0x78, 0x69, 0x66, 0, 0, ...tiff];
  const app1 = [0xff, 0xe1];
  const len = exifBody.length + 2;
  app1.push((len >> 8) & 0xff, len & 0xff, ...exifBody);
  const bytes = [0xff, 0xd8, ...app1, 0xff, 0xd9];
  return new Blob([Uint8Array.from(bytes)], {type: 'image/jpeg'});
}

test('valid JPEG EXIF GPS is extracted with source and capture time', async () => {
  const blob = jpegWith({dateTime: '2026:10:20 09:15:30'});
  const result = inspectJpegBuffer(await blob.arrayBuffer());
  assert.equal(result.reason, 'ok');
  assert.ok(Math.abs(result.gps.lat - 13.1) < 1e-9);
  assert.ok(Math.abs(result.gps.lng - 77.6) < 1e-9);
  assert.equal(result.gps.source, 'Submitted EXIF metadata');
  assert.equal(result.captureTime, '2026-10-20T03:45:30.000Z', 'EXIF local time is read as IST');
  assert.equal(result.serviceArea, true);
});

test('southern and western references are honoured', async () => {
  const result = inspectJpegBuffer(await jpegWith({latRef: 'S', lngRef: 'W'}).arrayBuffer());
  assert.equal(result.reason, 'ok');
  assert.ok(result.gps.lat < 0 && result.gps.lng < 0);
});

test('images without GPS or EXIF are rejected with a specific reason and never substituted', async () => {
  assert.equal(inspectJpegBuffer(await jpegWith({gps: false}).arrayBuffer()).reason, 'no-gps');
  assert.equal(await readGPS(new Blob([new Uint8Array([0xff, 0xd8, 0xff, 0xd9])])), null);
  assert.equal(inspectJpegBuffer(await new Blob([new Uint8Array([0xff, 0xd8, 0xff, 0xd9])]).arrayBuffer()).reason, 'no-exif');
});

test('non-JPEG bytes are not treated as JPEG evidence', async () => {
  const png = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0, 0, 0, 0]);
  assert.equal(inspectJpegBuffer(png.buffer).isJpeg, false);
  assert.equal(await readGPS(new Blob([png])), null);
});

test('malformed GPS rational values are rejected', async () => {
  const result = inspectJpegBuffer(await jpegWith({malformedLat: true}).arrayBuffer());
  assert.equal(result.reason, 'malformed-gps');
  assert.equal(result.gps, null);
});

test('service-area check flags coordinates outside Bengaluru without rejecting them', () => {
  assert.equal(inServiceArea(13.0, 77.6), true);
  assert.equal(inServiceArea(19.07, 72.87), false);
});
