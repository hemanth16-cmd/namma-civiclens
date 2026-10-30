// Dependency-free JPEG EXIF reader for civic evidence.
// It reads only what the file actually contains. It never substitutes device location
// for missing image metadata, and it never claims more precision than the tags provide.

const EXIF_HEADER = 0x45786966; // "Exif"
const TAG = {gpsPointer: 0x8825, exifPointer: 0x8769, dateTimeOriginal: 0x9003, gpsLatRef: 1, gpsLat: 2, gpsLngRef: 3, gpsLng: 4};
const TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8};
// Bengaluru service area (approximate bounding box). Outside it we warn; we do not reject.
export const SERVICE_AREA = {south: 12.75, north: 13.3, west: 77.35, east: 77.9};

export function inServiceArea(lat, lng) {
  return lat >= SERVICE_AREA.south && lat <= SERVICE_AREA.north && lng >= SERVICE_AREA.west && lng <= SERVICE_AREA.east;
}

/**
 * Inspect a JPEG buffer. Returns a result describing exactly what was found.
 * reason: 'ok' | 'not-jpeg' | 'no-exif' | 'no-gps' | 'malformed-gps' | 'malformed-exif'
 */
export function inspectJpegBuffer(buffer) {
  const v = new DataView(buffer);
  const base = {isJpeg: false, gps: null, captureTime: null, serviceArea: null, reason: 'not-jpeg'};
  if (v.byteLength < 4 || v.getUint16(0) !== 0xffd8) return base;
  base.isJpeg = true;
  let pos = 2;
  while (pos + 4 <= v.byteLength) {
    if (v.getUint8(pos) !== 0xff) break;
    const marker = v.getUint16(pos);
    const length = v.getUint16(pos + 2);
    if (marker === 0xffda) break; // start of image data; EXIF must precede it
    if (marker === 0xffe1 && pos + 10 <= v.byteLength && v.getUint32(pos + 4) === EXIF_HEADER && v.getUint16(pos + 8) === 0) {
      return {...base, ...parseExif(v, pos + 10, Math.min(v.byteLength, pos + 2 + length))};
    }
    if (length < 2) break;
    pos += length + 2;
  }
  return {...base, reason: 'no-exif'};
}

export function inspectJpegFile(file) {
  return file.arrayBuffer().then(inspectJpegBuffer);
}

/** Backwards-compatible helper: returns {lat,lng} for a valid JPEG GPS tag, otherwise null. */
export async function readGPS(file) {
  const result = await inspectJpegFile(file);
  return result.gps ?? null;
}

function parseExif(v, t, end) {
  const fail = reason => ({reason, gps: null, captureTime: null, serviceArea: null});
  try {
    const order = v.getUint16(t);
    const le = order === 0x4949;
    if (!le && order !== 0x4d4d) return fail('malformed-exif');
    const g16 = o => v.getUint16(t + o, le);
    const g32 = o => v.getUint32(t + o, le);
    const readIFD = off => {
      if (off <= 0 || off + 2 > end - t) return null;
      const count = g16(off);
      const entries = {};
      for (let i = 0; i < count; i++) {
        const e = off + 2 + i * 12;
        if (e + 12 > end - t) break;
        entries[g16(e)] = {type: g16(e + 2), count: g32(e + 4), e};
      }
      return entries;
    };
    const valueOffset = ent => {
      const total = (TYPE_SIZE[ent.type] || 1) * ent.count;
      return total <= 4 ? ent.e + 8 : g32(ent.e + 8);
    };
    const ascii = ent => {
      const o = valueOffset(ent);
      let s = '';
      for (let i = 0; i < ent.count && i < 32; i++) {
        const c = v.getUint8(t + o + i);
        if (!c) break;
        s += String.fromCharCode(c);
      }
      return s.trim();
    };
    const rationals = ent => {
      const o = valueOffset(ent);
      return [0, 1, 2].map(j => {
        const den = g32(o + j * 8 + 4);
        return den ? g32(o + j * 8) / den : NaN;
      });
    };

    const ifd0 = readIFD(g32(4));
    if (!ifd0) return fail('malformed-exif');

    let captureTime = null;
    const exifIFD = ifd0[TAG.exifPointer] && readIFD(g32(ifd0[TAG.exifPointer].e + 8));
    const dateEntry = exifIFD?.[TAG.dateTimeOriginal];
    if (dateEntry && dateEntry.type === 2) {
      // EXIF stores local time without a zone. Bengaluru cameras are assumed to use IST (+05:30).
      const m = ascii(dateEntry).match(/^(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})$/);
      if (m) {
        const iso = `${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}+05:30`;
        if (!Number.isNaN(Date.parse(iso))) captureTime = new Date(iso).toISOString();
      }
    }

    const gpsPtr = ifd0[TAG.gpsPointer];
    if (!gpsPtr) return {reason: 'no-gps', gps: null, captureTime, serviceArea: null};
    const gps = readIFD(g32(gpsPtr.e + 8));
    if (!gps || !gps[TAG.gpsLat] || !gps[TAG.gpsLng]) return {reason: 'no-gps', gps: null, captureTime, serviceArea: null};

    const latRef = gps[TAG.gpsLatRef] ? ascii(gps[TAG.gpsLatRef]) : '';
    const lngRef = gps[TAG.gpsLngRef] ? ascii(gps[TAG.gpsLngRef]) : '';
    if (!['N', 'S'].includes(latRef) || !['E', 'W'].includes(lngRef)) return {reason: 'malformed-gps', gps: null, captureTime, serviceArea: null};
    const lat0 = rationals(gps[TAG.gpsLat]);
    const lng0 = rationals(gps[TAG.gpsLng]);
    const valid = a => a.every(Number.isFinite) && a[0] >= 0 && a[1] >= 0 && a[1] < 60 && a[2] >= 0 && a[2] < 60;
    if (!valid(lat0) || !valid(lng0)) return {reason: 'malformed-gps', gps: null, captureTime, serviceArea: null};
    const dec = a => a[0] + a[1] / 60 + a[2] / 3600;
    const lat = dec(lat0) * (latRef === 'S' ? -1 : 1);
    const lng = dec(lng0) * (lngRef === 'W' ? -1 : 1);
    if (!(Math.abs(lat) <= 90 && Math.abs(lng) <= 180) || (lat === 0 && lng === 0)) {
      return {reason: 'malformed-gps', gps: null, captureTime, serviceArea: null};
    }
    return {
      reason: 'ok',
      gps: {lat, lng, source: 'Submitted EXIF metadata', sourceType: 'exif'},
      captureTime,
      serviceArea: inServiceArea(lat, lng)
    };
  } catch {
    return fail('malformed-exif');
  }
}
