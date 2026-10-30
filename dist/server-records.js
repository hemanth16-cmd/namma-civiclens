// Server complaints for the admin workspace (overview, incident management, city map, intelligence)
// in server mode. The workspace screens were built on browser-local records; this module gives them
// the same row shape from GET /staff/complaints, so the existing design keeps working on real data.
// In local mode, and for residents, the local demonstration store is used exactly as before.
import {api, assessPriority} from './services.js';
import {loadApiConfig} from './api-config.js';
import {createApiClient} from './api-client.js';
import {staffSession} from './staff-session.js';

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

/** Records for the workspace screens: server complaints for signed-in staff in server mode, else the local store. */
export async function workspaceRecords(viewer) {
  if (await usesServerRecords()) return loadServerRecords(await serverClient(), staffSession.get().token);
  return api.list(viewer);
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
