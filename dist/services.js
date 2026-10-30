// Namma CivicLens data layer.
// This is a browser-local demonstration of the planned backend contract (see docs/api-contract.md).
// Every projection and role check here mirrors a rule the FastAPI service must enforce server-side.
// Local role checks are demonstration guards only: they are not production authorisation.

import {inspectJpegFile, readGPS, inServiceArea} from './exif.js';
export {readGPS};

export const categories = [
  'Potholes', 'Road cracks and damage', 'Garbage accumulation', 'Illegal dumping',
  'Drainage or waterlogging', 'Broken streetlights', 'Damaged footpaths', 'Water leakage', 'Other civic issues'
];
export const statuses = ['Submitted', 'Under Review', 'Assigned', 'In Progress', 'Marked Resolved'];
export const verificationStates = ['Pending Verification', 'Citizen Confirmed', 'Disputed', 'Reinspection Required'];
export const citizenResponses = ['Appears Resolved', 'Still Present', 'Cannot Confirm'];
export const DEMO_PASSWORD = 'CivicDemo2026';
// Citizen evidence limits. They match the backend upload limits (CIVICLENS_MAX_PHOTO_BYTES /
// CIVICLENS_MAX_VIDEO_BYTES in backend/.env.example), so server mode never accepts a file the API refuses.
export const MAX_VIDEO_BYTES = 50 * 1024 * 1024;
export const MAX_PHOTO_BYTES = 25 * 1024 * 1024;
// Admin video review keeps footage in this browser only and never uploads it, so it allows larger clips.
export const MAX_REVIEW_VIDEO_BYTES = 200 * 1024 * 1024;

// Initial, expert-set severity by category (0-1). Documented assumptions, not calibrated values.
export const CATEGORY_SEVERITY = {
  'Potholes': 0.8, 'Road cracks and damage': 0.6, 'Garbage accumulation': 0.6, 'Illegal dumping': 0.7,
  'Drainage or waterlogging': 0.8, 'Broken streetlights': 0.5, 'Damaged footpaths': 0.5,
  'Water leakage': 0.7, 'Other civic issues': 0.3
};
// Normalised weights for the explainable baseline. Initial expert weights; to be validated on history.
export const PRIORITY_WEIGHTS = {severity: 0.35, exposure: 0.30, unresolved: 0.20, recurrence: 0.15};

export const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

export function secureUUID() {
  if (globalThis.crypto?.randomUUID) return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 15) | 64;
  b[8] = (b[8] & 63) | 128;
  const h = Array.from(b, x => x.toString(16).padStart(2, '0')).join('');
  return [h.slice(0, 8), h.slice(8, 12), h.slice(12, 16), h.slice(16, 20), h.slice(20)].join('-');
}

function referenceId() {
  const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  const bytes = crypto.getRandomValues(new Uint8Array(6));
  return 'NCL-' + new Date().getFullYear() + '-' + Array.from(bytes, b => chars[b % chars.length]).join('');
}

export class StorageError extends Error {
  constructor(message, options) { super(message, options); this.name = 'StorageError'; }
}
export class AccessError extends Error {
  constructor(message) { super(message); this.name = 'AccessError'; }
}

// ---------- Seed data (clearly illustrative) ----------
const SAMPLE_ROWS = [
  ['Pothole near the bus stop', 'Potholes', 'Yelahanka', 'In Progress', 'Pending Verification', 0.85, 'Road maintenance · North', [13.1007, 77.5963]],
  ['Waste piling up beside the market', 'Garbage accumulation', 'Malleswaram', 'Assigned', 'Pending Verification', 0.7, 'Solid waste · West', [13.0035, 77.5692]],
  ['Footpath restored, awaiting your view', 'Damaged footpaths', 'Indiranagar', 'Marked Resolved', 'Pending Verification', 0.5, 'Ward response · East', [12.9784, 77.6408]],
  ['Waterlogging at the junction', 'Drainage or waterlogging', 'Hebbal', 'Under Review', 'Pending Verification', 0.9, 'Drainage response · North', [13.0358, 77.597]],
  ['Streetlight repair needs reinspection', 'Broken streetlights', 'Jayanagar', 'Marked Resolved', 'Disputed', 0.6, 'Lighting team · South', [12.925, 77.5938]],
  ['Cracked road beside school entrance', 'Road cracks and damage', 'Whitefield', 'Submitted', 'Pending Verification', 0.9, null, [12.9698, 77.75]],
  ['Leaking supply line', 'Water leakage', 'Rajajinagar', 'In Progress', 'Pending Verification', 0.6, 'Water supply · West', [12.99, 77.552]],
  ['Dumped debris on the corner', 'Illegal dumping', 'Koramangala', 'Assigned', 'Pending Verification', 0.7, 'Solid waste · South', [12.9352, 77.6245]]
];
export const SAMPLE_POSITIONS = Object.fromEntries(SAMPLE_ROWS.map((row, i) => ['NCL-2026-DEMO0' + (i + 1), row[7]]));

const seedRecords = () => SAMPLE_ROWS.map((s, i) => {
  const id = 'NCL-2026-DEMO0' + (i + 1);
  const created = new Date(Date.UTC(2026, 9, 15 + i)).toISOString();
  const updated = new Date(Date.UTC(2026, 9, 25)).toISOString();
  const resolved = s[3] === 'Marked Resolved';
  return {
    id, claim: 'DEMO-CLAIM-0' + (i + 1), title: s[0], category: s[1], area: s[2], status: s[3], verification: s[4],
    exposure: s[5], recurrence: s[4] === 'Disputed' ? 1 : 0, team: s[6],
    department: s[1].includes('Garbage') || s[1].includes('dumping') ? 'Solid Waste Management' : 'Civic Infrastructure',
    description: 'Sample incident created for the Namma CivicLens prototype. This is not a verified real-world complaint.',
    owner: 'resident@civiclens.demo', created, updated, gps: null, mediaType: 'photo', sample: true,
    shareEvidence: true, repairs: [], workOrders: [], followers: [], verificationLog: [], internal: '',
    reviews: [{
      id: 'sample-' + id, author: 'Sample neighbour',
      text: resolved ? 'The update is useful. A fresh ground-level check will help confirm the repair.' : 'This issue affects everyday access. Clear repair updates would help the neighbourhood.',
      rating: resolved ? 4 : 2, date: updated, sample: true
    }],
    timeline: [
      {status: 'Submitted', text: 'Sample report received in the demonstration workspace.', date: new Date(Date.UTC(2026, 9, 15 + i, 8, 30)).toISOString()},
      ...(s[3] !== 'Submitted' ? [{status: s[3], text: resolved ? 'Sample completion recorded. Independent verification is tracked separately.' : 'Demonstration civic team recorded a progress update.', date: '2026-10-25T10:00:00.000Z'}] : [])
    ]
  };
});

// ---------- Local persistence ----------
const KEY = 'ncl-records-v2';
const LEGACY_KEY = 'ncl-records-v1';
const wait = () => new Promise(r => setTimeout(r, 60));

function normalise(r) {
  r.reviews ??= [];
  r.followers ??= [];
  r.verificationLog ??= [];
  r.repairs ??= [];
  r.workOrders ??= [];
  r.timeline ??= [];
  r.recurrence ??= 0;
  r.gps ??= null;
  r.mediaType ??= 'photo';
  r.sample ??= r.id.startsWith('NCL-2026-DEMO');
  r.shareEvidence ??= r.sample;
  r.internal ??= '';
  if (r.repairShared !== undefined || r.repairMeta) {
    // Version 4 stored a single repair photo in place. Keep it as the first entry of the history.
    if (!r.repairs.length && (r.repairMeta || r.repairShared)) {
      r.repairs.push({
        id: 'legacy-' + r.id, mediaKey: r.id + '-repair', shared: r.repairShared === true,
        gps: r.repairMeta?.gps ?? null, capturedAt: null, uploadedAt: r.repairMeta?.date ?? r.repairDate ?? null, uploadedBy: 'Civic team'
      });
    }
    delete r.repairShared; delete r.repairMeta; delete r.repairDate;
  }
  delete r.followed;
  delete r.score;
  delete r.priority;
  return r;
}

function readAll() {
  let raw = null;
  try { raw = localStorage.getItem(KEY) ?? localStorage.getItem(LEGACY_KEY); } catch { raw = null; }
  let data = null;
  try { data = raw ? JSON.parse(raw) : null; } catch { data = null; }
  if (!Array.isArray(data)) data = seedRecords();
  return data.map(normalise);
}

function writeAll(data) {
  try {
    localStorage.setItem(KEY, JSON.stringify(data));
  } catch (cause) {
    throw new StorageError('This browser cannot store the report: storage is full or blocked (for example, in private browsing). Free up space and try again. Nothing was saved.', {cause});
  }
}

// ---------- Media (IndexedDB) ----------
let dbPromise;
function db() {
  return dbPromise ??= new Promise((resolve, reject) => {
    if (!globalThis.indexedDB) return reject(new StorageError('This browser does not provide IndexedDB, so evidence files cannot be stored here.'));
    const request = indexedDB.open('ncl-evidence', 1);
    request.onupgradeneeded = () => request.result.createObjectStore('media');
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => { dbPromise = null; reject(new StorageError('Evidence storage could not be opened. Check that private browsing or site-data blocking is off.', {cause: request.error})); };
  });
}
const mediaFail = (cause, verb) => new StorageError(`The evidence file could not be ${verb} this browser. Storage may be full, blocked, or the file may be damaged. Try a smaller file.`, {cause});

/** Storage key for the stamped copy of a complaint's photo (kept beside the untouched original). */
export const stampedKey = id => `${id}:stamped`;

export const mediaDB = {
  async put(id, blob) {
    const d = await db();
    return new Promise((resolve, reject) => {
      const tx = d.transaction('media', 'readwrite');
      tx.objectStore('media').put(blob, id);
      tx.oncomplete = resolve;
      tx.onerror = () => reject(mediaFail(tx.error, 'saved to'));
      tx.onabort = () => reject(mediaFail(tx.error, 'saved to'));
    });
  },
  async get(id) {
    const d = await db();
    return new Promise((resolve, reject) => {
      const req = d.transaction('media').objectStore('media').get(id);
      req.onsuccess = () => resolve(req.result ?? null);
      req.onerror = () => reject(mediaFail(req.error, 'read from'));
    });
  },
  async delete(id) {
    const d = await db();
    return new Promise((resolve, reject) => {
      const tx = d.transaction('media', 'readwrite');
      tx.objectStore('media').delete(id);
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
    });
  },
  async clear() {
    const d = await db();
    return new Promise((resolve, reject) => {
      const tx = d.transaction('media', 'readwrite');
      tx.objectStore('media').clear();
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
    });
  }
};

// ---------- Projections and derived state ----------
export function needsReinspection(r) {
  return r.verification === 'Disputed' || r.verification === 'Reinspection Required';
}

export function evidenceConfidence(r) {
  const hasMedia = !r.sample;
  const corroborated = ['Citizen Confirmed', 'Disputed', 'Reinspection Required'].includes(r.verification);
  const signals = [!!r.gps, hasMedia, corroborated].filter(Boolean).length;
  return signals === 3 ? 'High' : signals >= 1 ? 'Medium' : 'Low';
}

/**
 * Explainable baseline priority. Urgency is kept separate from evidence confidence.
 * Score = 100 × Σ(weight × normalised input). Every contribution is returned for display.
 */
export function assessPriority(r, now = Date.now()) {
  const closed = r.status === 'Marked Resolved' && !needsReinspection(r);
  const end = closed ? Date.parse(r.updated || r.created) : now;
  const days = Math.max(0, (end - Date.parse(r.created)) / 864e5);
  const inputs = {
    severity: CATEGORY_SEVERITY[r.category] ?? 0.3,
    exposure: Number.isFinite(r.exposure) ? r.exposure : 0.5,
    unresolved: Math.min(days / 30, 1),
    recurrence: Math.min((r.recurrence || 0) / 3, 1)
  };
  const contributions = Object.fromEntries(Object.entries(PRIORITY_WEIGHTS).map(([k, w]) => [k, Math.round(w * inputs[k] * 1000) / 10]));
  const score = Math.round(Object.values(contributions).reduce((a, b) => a + b, 0));
  const confidence = evidenceConfidence(r);
  return {
    score,
    band: score >= 70 ? 'High' : score >= 45 ? 'Medium' : 'Low',
    inputs,
    contributions,
    weights: PRIORITY_WEIGHTS,
    confidence,
    exposureSource: Number.isFinite(r.exposure) ? 'Sample exposure assessment' : 'Default assumption: exposure not yet assessed',
    flag: inputs.severity >= 0.7 && confidence === 'Low' ? 'Verify evidence first: severe category with limited evidence.' : null,
    daysOpen: Math.round(days)
  };
}

const publicTimeline = r => r.timeline.map(({status, text, date}) => ({status, text, date}));
const publicLog = r => r.verificationLog.map(e => ({id: e.id, date: e.date, type: e.type, from: e.from ?? null, to: e.to ?? null, response: e.response ?? null, by: e.by, actor: e.actor, note: e.note || ''}));
const publicReviews = r => r.reviews.map(({id, author, text, rating, date, sample}) => ({id, author, text, rating, date, sample: !!sample}));
const sharedRepairs = r => r.repairs.filter(x => x.shared).map(x => ({id: x.id, capturedAt: x.capturedAt, uploadedAt: x.uploadedAt, gps: x.gps, uploadedBy: x.uploadedBy, shared: true}));

function summaryFor(r, viewer) {
  const isAdmin = viewer.role === 'official';
  const mine = !!viewer.email && r.owner === viewer.email;
  const row = {
    id: r.id, title: r.title, category: r.category, area: r.area, status: r.status, verification: r.verification,
    department: r.department, team: r.team, created: r.created, updated: r.updated, mediaType: r.mediaType,
    sample: r.sample, recurrence: r.recurrence, reviewCount: r.reviews.length,
    shareEvidence: r.shareEvidence === true, mine, following: !!viewer.email && r.followers.includes(viewer.email)
  };
  row.gps = isAdmin || mine || r.shareEvidence ? r.gps : null;
  if (isAdmin) row.priority = assessPriority(r);
  return row;
}

// ---------- Access rules ----------
function requireRole(viewer, role) {
  if (role === 'official' && viewer?.role !== 'official') throw new AccessError('Admin access is required for this action.');
  if (role === 'resident' && viewer?.role !== 'resident') throw new AccessError('Sign in as a resident to continue.');
}
function find(all, id) {
  const r = all.find(x => x.id === id);
  if (!r) throw new Error('Report not found.');
  return r;
}
function mediaAllowed(r, viewer, kind) {
  const isAdmin = viewer?.role === 'official';
  const mine = !!viewer?.email && r.owner === viewer.email;
  if (kind === 'before') return isAdmin || mine || r.shareEvidence === true;
  return isAdmin || mine || r.repairs.some(x => x.shared);
}

// ---------- Public API ----------
export const api = {
  /** Summary rows for lists and maps. Admin-only fields are added only for officials. */
  async list(viewer = {}) {
    await wait();
    return readAll().map(r => summaryFor(r, viewer));
  },

  /**
   * Full case projection for a viewer:
   * admin  → operational fields (internal notes, priority, all repair history)
   * owner  → own evidence and full history (no internal notes, no claim codes)
   * others → community projection (shared evidence and shared repairs only)
   */
  async detail(id, viewer = {}) {
    await wait();
    const r = readAll().find(x => x.id === id);
    if (!r) return null;
    const isAdmin = viewer.role === 'official';
    const mine = !!viewer.email && r.owner === viewer.email;
    const base = {
      id: r.id, title: r.title, category: r.category, area: r.area, status: r.status, verification: r.verification,
      department: r.department, team: r.team, created: r.created, capturedAt: r.capturedAt ?? null, uploadedAt: r.uploadedAt ?? r.created,
      updated: r.updated, mediaType: r.mediaType, sample: r.sample, recurrence: r.recurrence, timeline: publicTimeline(r), verificationLog: publicLog(r),
      reviews: publicReviews(r), following: !!viewer.email && r.followers.includes(viewer.email),
      followerCount: r.followers.length, mine
    };
    if (isAdmin || mine) {
      return {
        ...base,
        description: r.description, gps: r.gps, shareEvidence: r.shareEvidence === true, owner: mine,
        repairs: r.repairs.map(x => ({...x})),
        workOrders: isAdmin ? r.workOrders.map(x => ({...x})) : [],
        internal: isAdmin ? r.internal : undefined,
        priority: isAdmin ? assessPriority(r) : undefined
      };
    }
    return {
      ...base,
      area: r.area,
      shareEvidence: r.shareEvidence === true,
      gps: r.shareEvidence ? r.gps : null,
      description: r.shareEvidence ? r.description : 'The reporter has not shared private evidence or additional details.',
      repairs: sharedRepairs(r),
      workOrders: []
    };
  },

  /** Private receipt data for the submitter. Only meaningful on the device that created the report. */
  async receipt(id) {
    await wait();
    const r = readAll().find(x => x.id === id);
    if (!r) return null;
    return {id: r.id, claim: r.claim, category: r.category, area: r.area, created: r.created, capturedAt: r.capturedAt ?? null,
      uploadedAt: r.uploadedAt ?? r.created, captureSource: r.captureSource ?? null, gps: r.gps, mediaType: r.mediaType, shareEvidence: r.shareEvidence === true,
      evidenceRef: r.evidenceRef ?? null};
  },

  /** Public tracking: allowlisted, no claim codes, no reporter data, no media, no precise evidence. */
  async publicRecord(id) {
    await wait();
    const r = readAll().find(x => x.id === id);
    if (!r) return null;
    return {
      id: r.id, category: r.category, area: r.area, status: r.status, verification: r.verification,
      department: r.department, updated: r.updated, timeline: publicTimeline(r), verificationLog: publicLog(r)
    };
  },

  /** Community projection for signed-in or anonymous viewers (no owner, claim or internal fields). */
  async communityRecord(id) {
    return this.detail(id, {});
  },

  /** Media access. Before: original evidence when shared (owner/admin always). After: shared repairs only. */
  async mediaFor(viewer, id, {kind = 'before', repairId = null} = {}) {
    await wait();
    const r = readAll().find(x => x.id === id);
    if (!r) return null;
    if (kind === 'before') {
      if (!mediaAllowed(r, viewer, 'before')) return null;
      return mediaDB.get(id);
    }
    const repair = repairId ? r.repairs.find(x => x.id === repairId) : [...r.repairs].reverse().find(x => x.shared);
    if (!repair || !mediaAllowed(r, viewer, 'after') || (!repair.shared && viewer?.role !== 'official' && r.owner !== viewer?.email)) return null;
    return mediaDB.get(repair.mediaKey);
  },

  async communityMedia(id, kind = 'before', repairId = null) {
    return this.mediaFor({}, id, {kind, repairId});
  },

  /** Submit a new report. Success is returned only after both media and record are stored. */
  async create(data, blob) {
    await wait();
    if (!categories.includes(data.category)) throw new Error('Choose a valid issue category.');
    if (!(blob instanceof Blob) || blob.size === 0) throw new Error('The evidence file is empty. Capture it again.');
    const limit = data.mediaType === 'video' ? MAX_VIDEO_BYTES : MAX_PHOTO_BYTES;
    if (blob.size > limit) throw new Error('This evidence file is too large for this demonstration. Capture a shorter clip or a smaller photo.');
    if (data.gps && (!Number.isFinite(data.gps.lat) || !Number.isFinite(data.gps.lng))) throw new Error('The location evidence is incomplete. Capture it again.');
    const all = readAll();
    let id;
    do { id = referenceId(); } while (all.some(r => r.id === id));
    const now = new Date().toISOString();
    const r = {
      id, claim: secureUUID(), owner: data.owner ?? null,
      title: data.category + ' reported by a citizen',
      category: data.category,
      area: data.neighbourhood || 'Bengaluru · location on file',
      description: String(data.description || '').trim().slice(0, 1000),
      gps: data.gps ? {
        lat: data.gps.lat, lng: data.gps.lng, accuracy: data.gps.accuracy ?? null,
        source: data.gps.source, sourceType: ['gps', 'exif', 'manual'].includes(data.gps.sourceType) ? data.gps.sourceType : null,
        timestamp: data.gps.timestamp ?? null, address: data.gps.address ?? null
      } : null,
      // Fingerprint of the original file (not a secret). Printed on the stamped photo instead of the claim code.
      evidenceRef: typeof data.evidenceRef === 'string' && /^EV-[0-9A-F]{4}-[0-9A-F]{4}$/.test(data.evidenceRef) ? data.evidenceRef : null,
      created: data.created || now,
      capturedAt: data.created || null,
      captureSource: data.captureSource || 'Live camera',
      uploadedAt: now,
      updated: now,
      status: 'Submitted', verification: 'Pending Verification',
      department: 'Awaiting civic review', team: null, mediaType: data.mediaType === 'video' ? 'video' : 'photo',
      shareEvidence: data.shareEvidence === true, sample: false, exposure: undefined, recurrence: 0,
      reviews: [], followers: [], verificationLog: [], repairs: [], workOrders: [], internal: '',
      timeline: [{status: 'Submitted', text: 'Report saved in the local demonstration workspace.', date: now}]
    };
    await mediaDB.put(id, blob);
    try {
      writeAll([r, ...all]);
    } catch (err) {
      await mediaDB.delete(id).catch(() => {});
      throw err;
    }
    return r;
  },

  /** Admin case update: lifecycle, team, public response, internal note and verification state. */
  async updateCase(viewer, id, {status, team, internal, publicText, verification, note, due} = {}) {
    requireRole(viewer, 'official');
    await wait();
    const all = readAll(), r = find(all, id);
    if (status !== undefined && !statuses.includes(status)) throw new Error('Choose a valid lifecycle status.');
    if (verification !== undefined && !verificationStates.includes(verification)) throw new Error('Choose a valid verification state.');
    const now = new Date().toISOString();
    if (team !== undefined) r.team = String(team).trim().slice(0, 120) || null;
    if (due !== undefined) r.due = due || null;
    if (internal !== undefined) r.internal = String(internal).slice(0, 2000);
    const statusChanged = status !== undefined && status !== r.status;
    if (statusChanged) r.status = status;
    if (verification !== undefined && verification !== r.verification) {
      r.verificationLog.push({id: secureUUID(), date: now, type: 'state', from: r.verification, to: verification, by: 'admin', actor: 'Civic team', note: String(note || '').slice(0, 500)});
      r.verification = verification;
    }
    if (statusChanged || publicText) {
      r.timeline.push({status: r.status, text: String(publicText || (statusChanged ? 'Lifecycle status updated by the civic team.' : 'Civic team added an update.')).slice(0, 500), date: now});
    }
    r.updated = now;
    writeAll(all);
    return r;
  },

  async addWorkOrder(viewer, id, {team, due, instructions = ''} = {}) {
    requireRole(viewer, 'official');
    await wait();
    if (!String(team || '').trim()) throw new Error('Name the responsible team.');
    const all = readAll(), r = find(all, id);
    const now = new Date().toISOString();
    const order = {id: secureUUID(), team: String(team).trim().slice(0, 120), due: due || null, instructions: String(instructions).slice(0, 1000), createdAt: now, status: 'Open'};
    r.workOrders.push(order);
    r.team = order.team;
    if (['Submitted', 'Under Review'].includes(r.status)) r.status = 'Assigned';
    r.timeline.push({status: r.status, text: `Assigned to ${order.team}${order.due ? `; target date ${order.due}` : ''}.`, date: now});
    r.updated = now;
    writeAll(all);
    return order;
  },

  async requestReinspection(viewer, id, note = '') {
    requireRole(viewer, 'official');
    await wait();
    const all = readAll(), r = find(all, id);
    const now = new Date().toISOString();
    r.verificationLog.push({id: secureUUID(), date: now, type: 'state', from: r.verification, to: 'Reinspection Required', by: 'admin', actor: 'Civic team', note: String(note).slice(0, 500)});
    r.verification = 'Reinspection Required';
    r.timeline.push({status: r.status, text: 'Reinspection requested. Earlier repair and dispute records are retained.', date: now});
    r.updated = now;
    writeAll(all);
    return r;
  },

  /** Attach repair evidence to the case history. Earlier repairs are never removed. */
  async addRepair(viewer, id, {blob, shared = false, gps = null, capturedAt = null, uploadedBy = 'Civic team'} = {}) {
    requireRole(viewer, 'official');
    if (!(blob instanceof Blob) || !String(blob.type).startsWith('image/')) throw new Error('Choose a photograph of the repaired location.');
    if (blob.size > 20 * 1024 * 1024) throw new Error('Use a repair photo smaller than 20 MB.');
    await wait();
    const all = readAll(), r = find(all, id);
    const repairId = secureUUID();
    const mediaKey = `${id}-repair-${repairId}`;
    const now = new Date().toISOString();
    await mediaDB.put(mediaKey, blob);
    r.repairs.push({id: repairId, mediaKey, shared: shared === true, gps: gps ?? null, capturedAt, uploadedAt: now, uploadedBy});
    r.timeline.push({status: r.status, text: 'After-repair evidence was attached for review.', date: now});
    r.updated = now;
    try {
      writeAll(all);
    } catch (err) {
      await mediaDB.delete(mediaKey).catch(() => {});
      throw err;
    }
    return r.repairs.at(-1);
  },

  /** Owner decision to share original evidence and its geo-tag with the community. */
  async shareOriginal(viewer, id, shared) {
    await wait();
    const all = readAll(), r = find(all, id);
    if (!viewer?.email || r.owner !== viewer.email) throw new AccessError('Only the reporter can share original evidence.');
    r.shareEvidence = shared === true;
    r.updated = new Date().toISOString();
    writeAll(all);
    return r;
  },

  /** Citizen response. Never changes the administrative lifecycle status. */
  async citizenResponse(viewer, id, response) {
    if (!viewer?.email) throw new AccessError('Sign in to add your response.');
    if (!citizenResponses.includes(response)) throw new Error('Choose a valid response.');
    await wait();
    const all = readAll(), r = find(all, id);
    const now = new Date().toISOString();
    const entry = {id: secureUUID(), date: now, type: 'response', response, by: 'citizen', actor: 'Neighbour', note: ''};
    if (response === 'Appears Resolved') {
      if (r.status !== 'Marked Resolved') throw new Error('Citizen confirmation opens after the civic team marks this issue resolved.');
      entry.from = r.verification; entry.to = 'Citizen Confirmed';
      r.verification = 'Citizen Confirmed';
    } else if (response === 'Still Present') {
      if (r.status === 'Marked Resolved') {
        r.recurrence = (r.recurrence || 0) + 1;
        entry.from = r.verification; entry.to = 'Disputed';
        r.verification = 'Disputed';
        r.timeline.push({status: r.status, text: 'Community report: the issue still appears present after the marked repair.', date: now});
      }
    }
    r.verificationLog.push(entry);
    r.updated = now;
    writeAll(all);
    return r;
  },

  async toggleFollow(viewer, id) {
    if (!viewer?.email) throw new AccessError('Sign in to follow an issue.');
    await wait();
    const all = readAll(), r = find(all, id);
    const i = r.followers.indexOf(viewer.email);
    if (i >= 0) r.followers.splice(i, 1); else r.followers.push(viewer.email);
    writeAll(all);
    return i < 0;
  },

  async addReview(id, {author, authorEmail, text, rating} = {}) {
    await wait();
    text = String(text || '').trim();
    if (text.length < 3 || text.length > 1000) throw new Error('Write a review between 3 and 1,000 characters.');
    if (!Number.isInteger(+rating) || +rating < 1 || +rating > 5) throw new Error('Choose a rating from 1 to 5.');
    const all = readAll(), r = find(all, id);
    const review = {id: secureUUID(), author: String(author || 'Neighbour').slice(0, 60), authorEmail: authorEmail ?? null, text, rating: +rating, date: new Date().toISOString()};
    r.reviews.push(review);
    writeAll(all);
    const {authorEmail: _omit, ...publicReview} = review;
    return publicReview;
  },

  /** Attach a complaint to a resident profile using both the public ID and the private code. */
  async claim(id, code, email) {
    await wait();
    const all = readAll();
    const r = all.find(x => x.id === id && x.claim === code);
    if (!r || !code) throw new Error('The complaint ID and private code do not match.');
    if (r.owner && r.owner !== email) throw new Error('This report is already claimed by another demo profile.');
    r.owner = email;
    r.updated = new Date().toISOString();
    writeAll(all);
    return {id: r.id, title: r.title};
  },

  /** Per-user notification feed. Preferences and read state are applied here. */
  async notificationsFor(viewer, prefs = {}, lastRead = null) {
    await wait();
    const isAdmin = viewer.role === 'official';
    const events = [];
    for (const r of readAll()) {
      const relevant = isAdmin || (viewer.email && (r.owner === viewer.email || r.followers.includes(viewer.email)));
      if (!relevant) continue;
      for (const t of r.timeline) {
        if (t.text.startsWith('Sample') || t.text.includes('saved in the local')) continue;
        events.push({key: r.id + t.date, caseId: r.id, kind: 'update', title: r.title, text: t.text, status: t.status, date: t.date});
      }
      for (const e of r.verificationLog) {
        events.push({key: r.id + e.id, caseId: r.id, kind: e.type === 'response' ? 'response' : 'verification', title: r.title,
          text: e.type === 'response' ? `Community response: ${e.response}` : `Verification changed to ${e.to}`, status: r.status, date: e.date});
      }
    }
    const allowed = events.filter(e => (e.kind === 'update' && prefs.updates !== false) || (e.kind !== 'update' && prefs.verification !== false));
    allowed.sort((a, b) => (b.date > a.date ? 1 : -1));
    return allowed.slice(0, 40).map(e => ({...e, unread: !lastRead || e.date > lastRead}));
  },

  async login(role, email, password, register, name) {
    await wait();
    const profiles = JSON.parse(localStorage.getItem('ncl-profiles') || '{}');
    if (register) {
      if (profiles[email] || email === 'resident@civiclens.demo') throw new Error('That demo profile already exists. Please sign in.');
      profiles[email] = {name: name || 'Neighbour', pass: password};
      localStorage.setItem('ncl-profiles', JSON.stringify(profiles));
      return {role: 'resident', email, name: name || 'Neighbour'};
    }
    if (email === role + '@civiclens.demo' && password === DEMO_PASSWORD) {
      return {role, email, name: role === 'resident' ? 'Demo resident' : 'Civic official'};
    }
    if (role === 'resident' && profiles[email]?.pass === password) return {role, email, name: profiles[email].name};
    throw new Error('Use the displayed demo credentials or your locally registered profile.');
  },

  async reset() {
    await mediaDB.clear();
    try {
      localStorage.removeItem(LEGACY_KEY);
      localStorage.setItem('ncl-video-reviews-v1', '[]');
    } catch { /* storage blocked: nothing to clear */ }
    writeAll(seedRecords());
  }
};

// ---------- Submission validation helpers (shared with the capture flow) ----------
export async function inspectGalleryFile(file) {
  const isJpeg = /jpe?g$/i.test(file.name || '') || file.type === 'image/jpeg';
  if (!isJpeg) return {ok: false, message: 'Gallery uploads must be JPEG photos with GPS metadata. Export the photo as JPEG, or take a new one with the camera.'};
  if (file.size > MAX_PHOTO_BYTES) return {ok: false, message: 'This photo is larger than 25 MB. Export a smaller JPEG and try again.'};
  const result = await inspectJpegFile(file);
  if (!result.isJpeg) return {ok: false, message: 'This file does not look like a valid JPEG photo. Export it again as JPEG.'};
  if (result.reason === 'no-exif' || result.reason === 'no-gps') {
    return {ok: false, message: 'This photo does not contain GPS location data, so it cannot be geo-tagged. Turn on location in your camera app and try again, or capture a new photo here.'};
  }
  if (result.reason === 'malformed-gps' || result.reason === 'malformed-exif') {
    return {ok: false, message: 'The location metadata in this photo is damaged or unreadable. Re-export the photo with location enabled.'};
  }
  return {ok: true, gps: result.gps, captureTime: result.captureTime, serviceArea: result.serviceArea};
}

export {inServiceArea};

// Future adapters are deliberately unavailable rather than invented inference or delivery.
export const classificationService = {
  async classify() { return {mode: 'manual', available: false, reason: 'No trained classifier is connected.'}; }
};
export const notificationAdapter = {
  async push() { return {delivered: false, reason: 'Firebase Cloud Messaging is not configured.'}; }
};
