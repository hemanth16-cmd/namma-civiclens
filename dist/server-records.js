// Server complaints for the admin workspace (overview, incident management, city map, intelligence)
// in server mode. The workspace screens were built on browser-local records; this module gives them
// the same row shape from GET /staff/complaints, so the existing design keeps working on real data.
// Signed-in residents get their own complaints from /residents/me/complaints. In local mode the local
// demonstration store is used exactly as before.
import {api, assessPriority} from './services.js';
import {loadApiConfig} from './api-config.js';
import {createApiClient} from './api-client.js';
import {staffSession} from './staff-session.js';
import {residentSession} from './resident-session.js';

const PAGE = 100;        // the API's maximum page size
const MAX_PAGES = 20;    // 2,000 complaints; more needs server-side filtering (see docs/INTEGRATION_AUDIT.md)

/** An API client in server mode, else null. */
export async function serverClient() {
  const config = await loadApiConfig();
  return config.mode === 'backend' ? createApiClient({baseUrl: config.baseUrl}) : null;
}

/** True when the workspace should show server complaints: server mode with a signed-in staff session. */
export async function usesServerRecords() {
  return !!staffSession.get() && (await loadApiConfig()).mode === 'backend';
}

/** One staff-list item as a workspace row. Server complaints carry no sample flags, reviews or owner. */
export function toWorkspaceRecord(item) {
  const row = {
    id: item.id, title: item.title || `${item.category} reported by a citizen`, category: item.category, area: item.area,
    status: item.status, verification: item.verification, department: item.department || '', team: null,
    created: item.created, updated: item.updated, mediaType: item.mediaType, hasEvidence: item.hasEvidence === true,
    gps: item.gps ? {lat: item.gps.lat, lng: item.gps.lng, accuracy: item.gps.accuracy ?? null, sourceType: item.gps.sourceType ?? null} : null,
    sample: false, server: true, recurrence: 0, reviewCount: 0, shareEvidence: false, mine: false, following: false
  };
  // The existing, explainable baseline priority shown by the workspace, computed from the same inputs as for
  // local records. It is a display of the prototype's formula, not a server-side priority engine.
  row.priority = assessPriority(row);
  return row;
}

/** Every server complaint (newest first), page by page. Throws ApiError on failure: no local fallback. */
export async function loadServerRecords(http, token) {
  const rows = [];
  for (let page = 0; page < MAX_PAGES; page++) {
    const {items, total} = await http.listStaffComplaints(token, {limit: PAGE, offset: page * PAGE});
    rows.push(...items.map(toWorkspaceRecord));
    if (!items.length || rows.length >= total) break;
  }
  return rows;
}

/** A resident's own server complaint as a workspace row (the owner may see their own location). */
export function toResidentRow(c) {
  const row = {
    id: c.id, title: c.title, category: c.category, area: c.area, status: c.status, verification: c.verification,
    department: c.department, team: null, created: c.created, updated: c.updated, mediaType: c.mediaType, hasEvidence: c.hasEvidence,
    gps: c.gps ? {lat: c.gps.lat, lng: c.gps.lng, accuracy: c.gps.accuracy, sourceType: c.gps.sourceType} : null,
    description: c.description, ownedVia: c.ownedVia, ownedAt: c.ownedAt, timeline: c.timeline,
    sample: false, server: true, recurrence: 0, reviewCount: 0, shareEvidence: c.shareEvidence, mine: true, following: false
  };
  row.priority = assessPriority(row);
  return row;
}

/** My Complaints: only the signed-in resident's complaints, from GET /residents/me/complaints (ownership is
 * decided by the server from the session). Throws ApiError (401 when the session ended). */
export async function loadMyComplaints(http, token) {
  const {items} = await http.listResidentComplaints(token, {limit: 100});
  return items.map(toResidentRow);
}
export const residentRecords = loadMyComplaints;  // earlier name, kept for existing callers

/** One public community-list item as a workspace/map row. `gps` is the approximate public point (about
 * 110 m), flagged `approximate`; precise coordinates are never in the public API. */
export function toPublicRow(item) {
  const row = {
    id: item.id, title: item.title || `${item.category} reported by a citizen`, category: item.category, area: item.area,
    status: item.status, verification: item.verification, department: item.department || '', team: null,
    created: item.created, updated: item.updated,
    gps: item.mapPoint ? {lat: item.mapPoint.lat, lng: item.mapPoint.lng, accuracy: null, sourceType: null, approximate: true} : null,
    sample: false, server: true, public: true, recurrence: 0, reviewCount: 0, shareEvidence: false, mine: false, following: false
  };
  row.priority = assessPriority(row);
  return row;
}

/**
 * Community page and neighbourhood map: every public complaint across Bengaluru from GET /complaints,
 * whoever reported it (residents A and B, anonymous passers-by). Independent of the viewer's own
 * complaints. Throws ApiError on failure: no local fallback.
 */
export async function loadPublicCommunityComplaints(http, filters = {}) {
  const rows = [];
  for (let page = 0; page < MAX_PAGES; page++) {
    const {items, total} = await http.listPublicComplaints({...filters, limit: PAGE, offset: page * PAGE});
    rows.push(...items.map(toPublicRow));
    if (!items.length || rows.length >= total) break;
  }
  return rows;
}

/** Records for a map: the public Bengaluru-wide dataset for a signed-in resident in server mode, else the
 * same records as the workspace (staff list for staff, the local store in local mode). */
export async function mapRecords(viewer) {
  if (await isServerResident(viewer)) return loadPublicCommunityComplaints(await serverClient());
  return workspaceRecords(viewer);
}

/** Records for the workspace screens: server complaints for signed-in staff, the resident's own complaints
 * for a signed-in resident in server mode, else the local demonstration store. */
export async function workspaceRecords(viewer) {
  if (viewer?.role === 'official' && await usesServerRecords()) return loadServerRecords(await serverClient(), staffSession.get().token);
  if (await isServerResident(viewer)) return loadMyComplaints(await serverClient(), residentSession.get().token);
  return api.list(viewer);
}

/** True for a resident signed in with a server resident account (server mode). */
export async function isServerResident(viewer) {
  return viewer?.role === 'resident' && !!residentSession.get() && (await loadApiConfig()).mode === 'backend';
}

/** Reporting activity: complaints received on each of the last `days` days (UTC dates), oldest first. */
export function dailyActivity(records, days = 12, now = Date.now()) {
  const out = [];
  for (let i = days - 1; i >= 0; i--) {
    const day = new Date(now - i * 864e5).toISOString().slice(0, 10);
    out.push({day, count: records.filter(r => String(r.created || '').slice(0, 10) === day).length});
  }
  return out;
}
