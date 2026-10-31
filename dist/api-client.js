// HTTP adapter for the CivicLens API (backend/; see backend/README.md).
// It never logs request or response bodies, so claim codes, coordinates and descriptions
// cannot leak through the console. Complaint creation is never retried automatically: after a
// network failure or timeout the server may already have saved the report.

const DEFAULT_TIMEOUT_MS = 10000;
const DEFAULT_UPLOAD_TIMEOUT_MS = 180000;  // a 50 MB video on a slow connection needs minutes
const EVIDENCE_STATUSES = ['uploaded', 'already_uploaded'];
const PUBLIC_ID = /^NCL-\d{4}-[A-Z0-9]{6}$/;
const SOURCE_TYPES = ['gps', 'exif', 'manual'];

export class ApiError extends Error {
  /**
   * @param {string} message  safe to show to the user
   * @param {{status?: number, code?: string, details?: Array, ambiguous?: boolean}} info
   *   ambiguous: true when the request may have reached the server (a POST that failed in transit)
   *   retryAfter: seconds from a 429 Retry-After header; complaintId: public ID named by the server
   */
  constructor(message, {status = 0, code = 'error', details = [], ambiguous = false, retryAfter = null, complaintId = null} = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.details = details;
    this.ambiguous = ambiguous;
    this.retryAfter = retryAfter;
    this.complaintId = complaintId;
  }
}

/**
 * A new Idempotency-Key for one submission attempt: 256 random bits, URL-safe. Generate it once per
 * report and reuse it for every retry of that report, so the server never creates a duplicate.
 */
export function newIdempotencyKey() {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

const STATUS_MESSAGES = {
  404: 'The CivicLens server has no record at that address.',
  422: 'The CivicLens server did not accept the report details.',
  503: 'The CivicLens server could not save or read the complaint right now. Try again shortly.'
};

export function createApiClient({baseUrl, fetchImpl = globalThis.fetch, timeoutMs = DEFAULT_TIMEOUT_MS, uploadTimeoutMs = DEFAULT_UPLOAD_TIMEOUT_MS}) {
  /** `body` is a plain object (sent as JSON) or FormData (sent as multipart; the browser sets the boundary). */
  async function request(method, path, body, {headers = {}, timeout = timeoutMs, raw = false} = {}) {
    const controller = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeout);
    const ambiguous = method !== 'GET';
    const isForm = typeof FormData !== 'undefined' && body instanceof FormData;
    let response;
    try {
      response = await fetchImpl(baseUrl + path, {
        method,
        headers: {Accept: 'application/json', ...(body === undefined || isForm ? {} : {'Content-Type': 'application/json'}), ...headers},
        body: body === undefined ? undefined : (isForm ? body : JSON.stringify(body)),
        signal: controller.signal,
        credentials: 'omit',
        cache: 'no-store'
      });
    } catch {
      throw timedOut
        ? new ApiError('The CivicLens server did not answer in time.', {code: 'timeout', ambiguous})
        : new ApiError('The CivicLens server could not be reached. Check that it is running and that this page’s address is allowed.', {code: 'network_error', ambiguous});
    } finally {
      clearTimeout(timer);
    }
    if (response.ok && raw) return response;           // caller reads the body (evidence files)
    if (response.status === 204) return null;          // e.g. sign-out
    let data = null;
    try {
      const text = await response.text();
      data = text ? JSON.parse(text) : null;
    } catch {
      data = undefined;
    }
    if (!response.ok) {
      const envelope = data && typeof data === 'object' && data.error && typeof data.error === 'object' ? data.error : null;
      const message = typeof envelope?.message === 'string' && envelope.message ? envelope.message.slice(0, 300) : (STATUS_MESSAGES[response.status] || `The CivicLens server returned an error (HTTP ${response.status}).`);
      const details = Array.isArray(envelope?.details) ? envelope.details.map(d => ({field: String(d?.field ?? ''), message: String(d?.message ?? '')})) : [];
      const retryHeader = Number.parseInt(response.headers?.get?.('Retry-After') ?? '', 10);
      const complaintId = typeof envelope?.complaintId === 'string' && PUBLIC_ID.test(envelope.complaintId) ? envelope.complaintId : null;
      throw new ApiError(message, {status: response.status, code: typeof envelope?.code === 'string' ? envelope.code : 'http_error', details,
        retryAfter: Number.isFinite(retryHeader) && retryHeader > 0 ? retryHeader : null, complaintId});
    }
    if (data === undefined || data === null || typeof data !== 'object') {
      throw new ApiError('The CivicLens server sent a response this app could not read.', {status: response.status, code: 'malformed_response'});
    }
    return data;
  }

  return {
    async health() {
      const data = await request('GET', '/health');
      if (data.status !== 'ok') throw new ApiError('The CivicLens server reported that it is not healthy.', {code: 'malformed_response'});
      return {status: data.status, apiVersion: data.api_version, version: data.version};
    },

    /**
     * POST /complaints. `payload` must come from toCreatePayload(). Resolves to the server receipt.
     * With an idempotency key, a retry of the same report returns the same complaint (replayed: true)
     * with newly issued credentials instead of creating a duplicate.
     */
    async createComplaint(payload, {idempotencyKey, residentToken} = {}) {
      // A signed-in resident's token makes the complaint theirs (the server takes the owner from the
      // session). Without it the report is anonymous: no account is needed to report.
      const headers = {...(idempotencyKey ? {'Idempotency-Key': idempotencyKey} : {}), ...(residentToken ? bearer(residentToken) : {})};
      return toReceipt(await request('POST', '/complaints', payload, {headers}));
    },

    /** GET /complaints/{id}. Resolves to the public projection, or null when the server has no such ID. */
    async getPublicComplaint(id) {
      try {
        return toPublicRecord(await request('GET', '/complaints/' + encodeURIComponent(id)));
      } catch (err) {
        if (err instanceof ApiError && err.status === 404 && err.code === 'not_found') return null;
        throw err;
      }
    },

    /**
     * POST /complaints/{id}/evidence as multipart/form-data. The upload token goes in the
     * Authorization header, never the URL. Safe to repeat: the server returns the existing record
     * for an identical file.
     */
    async uploadEvidence(id, blob, {token, capturedAt} = {}) {
      const form = new FormData();
      form.append('file', blob, evidenceFilename(blob));
      if (capturedAt) form.append('capturedAt', capturedAt);
      return toEvidenceReceipt(await request('POST', `/complaints/${encodeURIComponent(id)}/evidence`, form, {headers: {Authorization: 'Bearer ' + token}, timeout: uploadTimeoutMs}));
    },

    /** POST /complaints/{id}/upload-token. The claim code travels in the JSON body only. */
    async requestUploadToken(id, claim) {
      const data = await request('POST', `/complaints/${encodeURIComponent(id)}/upload-token`, {claim});
      if (typeof data.uploadToken !== 'string' || !data.uploadToken) throw new ApiError('The CivicLens server sent an upload token this app could not read.', {code: 'malformed_response'});
      return {uploadToken: data.uploadToken, uploadTokenExpiresAt: data.uploadTokenExpiresAt ?? null};
    },

    // ---------- Resident accounts (server mode). The token is passed in; it is never stored here. ----------

    /** POST /residents (register) or POST /residents/sessions (sign in). Resolves to {token, expiresAt, resident}. */
    async residentRegister(email, password, displayName) {
      return toResidentSession(await request('POST', '/residents', {email, password, displayName}));
    },
    async residentSignIn(email, password) {
      return toResidentSession(await request('POST', '/residents/sessions', {email, password}));
    },
    async residentSignOut(token) {
      await request('DELETE', '/residents/sessions/current', undefined, {headers: bearer(token)});
    },
    async residentMe(token) {
      return toResident(await request('GET', '/residents/me', undefined, {headers: bearer(token)}));
    },
    async listResidentComplaints(token, {limit = 100, offset = 0} = {}) {
      const d = await request('GET', `/residents/me/complaints?limit=${encodeURIComponent(limit)}&offset=${encodeURIComponent(offset)}`, undefined, {headers: bearer(token)});
      if (!Array.isArray(d.items)) throw new ApiError('The CivicLens server sent a complaint list this app could not read.', {code: 'malformed_response'});
      return {items: d.items.map(toResidentComplaint), total: Number(d.total) || 0};
    },
    /** GET /residents/me/complaints/{id}: one complaint, only if the signed-in resident owns it (else null/404). */
    async getResidentComplaint(token, id) {
      try {
        return toResidentComplaint(await request('GET', `/residents/me/complaints/${encodeURIComponent(id)}`, undefined, {headers: bearer(token)}));
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) return null;
        throw err;
      }
    },

    /** An evidence file of the resident's own complaint, as a Blob of an allowed media type (owner-only endpoint). */
    async fetchResidentEvidence(token, id, evidenceId = '') {
      const suffix = evidenceId ? `/${encodeURIComponent(evidenceId)}` : '';
      return evidenceBlob(await request('GET', `/residents/me/complaints/${encodeURIComponent(id)}/evidence${suffix}`, undefined,
        {headers: bearer(token), timeout: uploadTimeoutMs, raw: true}));
    },

    /** One evidence file (original or after-repair) of the resident's own complaint, as a Blob of an allowed media
     * type: GET /residents/me/complaints/{id}/evidence/{evidenceId}. Used by My Complaints. */
    async fetchResidentEvidenceFile(token, id, evidenceId) {
      return this.fetchResidentEvidence(token, id, evidenceId);
    },

    /** POST /residents/me/claims. The claim code travels in the JSON body only. */
    async claimComplaint(token, id, claim) {
      const d = await request('POST', '/residents/me/claims', {id, claim}, {headers: bearer(token)});
      return {outcome: d.outcome, complaint: toResidentComplaint(d.complaint)};
    },
    async getResidentPreferences(token) {
      return toResidentPrefs(await request('GET', '/residents/me/preferences', undefined, {headers: bearer(token)}));
    },
    async putResidentPreferences(token, prefs) {
      return toResidentPrefs(await request('PUT', '/residents/me/preferences', toResidentPrefs(prefs), {headers: bearer(token)}));
    },

    // ---------- Staff console (server mode). The session token is passed in; it is never stored here. ----------

    /** POST /staff/sessions. Resolves to {token, expiresAt, staff}. */
    async staffSignIn(username, password) {
      const d = await request('POST', '/staff/sessions', {username, password});
      if (typeof d.token !== 'string' || !d.token || !d.staff || typeof d.staff.username !== 'string') {
        throw new ApiError('The CivicLens server sent a sign-in response this app could not read.', {code: 'malformed_response'});
      }
      return {token: d.token, expiresAt: d.expiresAt ?? null,
        staff: {username: d.staff.username, displayName: String(d.staff.displayName ?? d.staff.username), role: String(d.staff.role ?? '')}};
    },

    async staffSignOut(token) {
      await request('DELETE', '/staff/sessions/current', undefined, {headers: staffAuth(token)});
    },

    async listStaffComplaints(token, {limit = 50, offset = 0} = {}) {
      const d = await request('GET', `/staff/complaints?limit=${encodeURIComponent(limit)}&offset=${encodeURIComponent(offset)}`, undefined, {headers: staffAuth(token)});
      if (!Array.isArray(d.items)) throw new ApiError('The CivicLens server sent a complaint list this app could not read.', {code: 'malformed_response'});
      return {
        total: Number(d.total) || 0, limit: Number(d.limit) || limit, offset: Number(d.offset) || 0,
        items: d.items.filter(i => typeof i?.id === 'string' && PUBLIC_ID.test(i.id)).map(i => ({
          id: i.id, category: String(i.category ?? ''), area: String(i.area ?? ''), status: String(i.status ?? ''),
          verification: String(i.verification ?? ''), created: i.created ?? null, updated: i.updated ?? null,
          mediaType: i.mediaType === 'video' ? 'video' : 'photo', hasEvidence: i.hasEvidence === true,
          title: String(i.title ?? ''), department: String(i.department ?? ''), gps: toListGps(i.gps), mergedInto: publicIdOrNull(i.mergedInto)
        }))
      };
    },

    async getStaffComplaint(token, id) {
      return toStaffComplaint(await request('GET', `/staff/complaints/${encodeURIComponent(id)}`, undefined, {headers: staffAuth(token)}));
    },

    /** GET /staff/complaints/{id}/evidence. Resolves to a Blob of an allowed media type. Each call is logged by the server. */
    async fetchStaffEvidence(token, id) {
      return evidenceBlob(await request('GET', `/staff/complaints/${encodeURIComponent(id)}/evidence`, undefined, {headers: staffAuth(token), timeout: uploadTimeoutMs, raw: true}));
    },

    // ---------- Owner evidence: the same server retrieval as staff, authorised by ownership instead. ----------

    /** POST /complaints/{id}/evidence/view. The claim code travels in the JSON body only; the file comes back as a Blob. */
    async fetchOwnEvidenceWithClaim(id, claim) {
      return evidenceBlob(await request('POST', `/complaints/${encodeURIComponent(id)}/evidence/view`, {claim}, {timeout: uploadTimeoutMs, raw: true}));
    },
    /** GET /residents/me/complaints/{id}/evidence, for a complaint in the signed-in resident's account. */
    async fetchResidentEvidence(token, id, evidenceId = '') {
      const suffix = evidenceId ? `/${encodeURIComponent(evidenceId)}` : '';
      return evidenceBlob(await request('GET', `/residents/me/complaints/${encodeURIComponent(id)}/evidence${suffix}`, undefined, {headers: bearer(token), timeout: uploadTimeoutMs, raw: true}));
    },
    async getResidentComplaint(token, id) {
      try {
        return toResidentComplaint(await request('GET', `/residents/me/complaints/${encodeURIComponent(id)}`, undefined, {headers: bearer(token)}));
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) return null;
        throw err;
      }
    },

    // ---------- Complaint management (staff). Every call returns the updated staff case from the server. ----------

    /** POST /staff/complaints/{id}/transitions. `expectedStatus` is the status shown on screen (409 if it changed). */
    async transitionComplaint(token, id, {to, expectedStatus, explanation = ''}) {
      return toStaffComplaint(await request('POST', `/staff/complaints/${encodeURIComponent(id)}/transitions`, {to, expectedStatus, explanation}, {headers: staffAuth(token)}));
    },

    async assignComplaint(token, id, {department, assignee = null, reason = '', expectedStatus}) {
      return toStaffComplaint(await request('POST', `/staff/complaints/${encodeURIComponent(id)}/assignment`, {department, assignee: assignee || null, reason, expectedStatus}, {headers: staffAuth(token)}));
    },

    /** Internal note: visible to staff only, never in public tracking. */
    async addStaffNote(token, id, body) {
      return toStaffComplaint(await request('POST', `/staff/complaints/${encodeURIComponent(id)}/notes`, {body}, {headers: staffAuth(token)}));
    },

    /** Public update: appears in the public timeline. kind is "progress" or "department_response". */
    async addPublicUpdate(token, id, {text, kind = 'progress'}) {
      return toStaffComplaint(await request('POST', `/staff/complaints/${encodeURIComponent(id)}/updates`, {text, kind}, {headers: staffAuth(token)}));
    },

    async requestReinspection(token, id, {note, reopen = false, expectedStatus}) {
      return toStaffComplaint(await request('POST', `/staff/complaints/${encodeURIComponent(id)}/reinspections`, {note, reopen: reopen === true, expectedStatus}, {headers: staffAuth(token)}));
    },

    async createWorkOrder(token, id, {team, description = '', dueAt = null, assignee = null}) {
      return toStaffComplaint(await request('POST', `/staff/complaints/${encodeURIComponent(id)}/work-orders`, {team, description, dueAt: dueAt || null, assignee: assignee || null}, {headers: staffAuth(token)}));
    },

    /** Start ("In Progress"), complete ("Completed", needs a note) or cancel ("Cancelled", supervisors) a work order. */
    async changeWorkOrder(token, workOrderId, {to, expectedStatus, note = ''}) {
      return toStaffComplaint(await request('POST', `/staff/work-orders/${encodeURIComponent(workOrderId)}/transitions`, {to, expectedStatus, note}, {headers: staffAuth(token)}));
    },

    /** After-repair JPEG for a work order (multipart). Resolves to the evidence receipt. */
    async uploadRepairEvidence(token, workOrderId, blob) {
      const form = new FormData();
      form.append('file', blob, 'repair.jpg');
      const d = await request('POST', `/staff/work-orders/${encodeURIComponent(workOrderId)}/evidence`, form, {headers: staffAuth(token), timeout: uploadTimeoutMs});
      if (typeof d.evidenceId !== 'string') throw new ApiError('The CivicLens server sent an upload result this app could not read.', {code: 'malformed_response'});
      return {evidenceId: d.evidenceId, byteSize: Number(d.byteSize) || 0, sha256: String(d.sha256 ?? ''), uploadedAt: d.uploadedAt ?? null};
    },

    /** GET a before/after file of a work order as a Blob. Each view is logged by the server. */
    async fetchWorkOrderEvidence(token, workOrderId, evidenceId) {
      const response = await request('GET', `/staff/work-orders/${encodeURIComponent(workOrderId)}/evidence/${encodeURIComponent(evidenceId)}`, undefined, {headers: staffAuth(token), timeout: uploadTimeoutMs, raw: true});
      const type = String(response.headers.get('Content-Type') || '').split(';')[0].trim().toLowerCase();
      if (!EVIDENCE_TYPES.includes(type)) throw new ApiError('The CivicLens server sent evidence of an unexpected type, so it is not shown.', {code: 'malformed_response'});
      return new Blob([await response.arrayBuffer()], {type});
    },

    async listWorkOrders(token, {status = '', overdue = false, department = '', limit = 50, offset = 0} = {}) {
      const q = new URLSearchParams({limit: String(limit), offset: String(offset)});
      if (status) q.set('status', status);
      if (overdue) q.set('overdue', 'true');
      if (department) q.set('department', department);
      const d = await request('GET', '/staff/work-orders?' + q, undefined, {headers: staffAuth(token)});
      if (!Array.isArray(d.items)) throw new ApiError('The CivicLens server sent a work-order list this app could not read.', {code: 'malformed_response'});
      return {items: d.items.map(toWorkOrder).filter(Boolean), total: Number(d.total) || 0};
    },

    async listDepartments(token) {
      const d = await request('GET', '/staff/departments', undefined, {headers: staffAuth(token)});
      if (!Array.isArray(d)) throw new ApiError('The CivicLens server sent a department list this app could not read.', {code: 'malformed_response'});
      return d.filter(x => typeof x?.code === 'string').map(x => ({code: x.code, name: String(x.name ?? x.code)}));
    },

    /** Supervisors and admins only (403 otherwise). Newest first. */
    async getAuditTrail(token, id) {
      const d = await request('GET', `/staff/complaints/${encodeURIComponent(id)}/audit`, undefined, {headers: staffAuth(token)});
      if (!Array.isArray(d)) throw new ApiError('The CivicLens server sent an audit trail this app could not read.', {code: 'malformed_response'});
      return d.map(e => ({id: String(e?.id ?? ''), actorType: String(e?.actorType ?? ''), actor: e?.actor ?? null, actorName: e?.actorName ?? null,
        action: String(e?.action ?? ''), details: e?.details && typeof e.details === 'object' ? e.details : {}, at: e?.at ?? null}));
    },

    // ---------- Supervisor operations (supervisors and admins; 403 for officials) ----------

    /** The wards this supervisor may act on, grouped by zone. Admins are city-wide. */
    async supervisorScope(token) {
      const d = await request('GET', '/staff/supervisor/scope', undefined, {headers: staffAuth(token)});
      if (!Array.isArray(d?.zones)) throw new ApiError('The CivicLens server sent a ward list this app could not read.', {code: 'malformed_response'});
      return {cityWide: d.cityWide === true, role: String(d.role ?? ''), wardCount: Number(d.wardCount) || 0,
        zones: d.zones.filter(z => Number.isInteger(z?.number)).map(z => ({number: z.number, name: String(z.name ?? ''),
          wards: (Array.isArray(z.wards) ? z.wards : []).filter(w => Number.isInteger(w?.number)).map(w => ({number: w.number, name: String(w.name ?? ''), hasBoundary: w.hasBoundary === true}))}))};
    },

    /** City, zone or ward counts from the server's complaint rows. */
    async supervisorDashboard(token, {zone = null, ward = null} = {}) {
      const q = new URLSearchParams();
      if (ward) q.set('ward', String(ward)); else if (zone) q.set('zone', String(zone));
      const query = q.toString();
      const d = await request('GET', '/staff/supervisor/dashboard' + (query ? '?' + query : ''), undefined, {headers: staffAuth(token)});
      if (!d?.totals || !Array.isArray(d.byWard)) throw new ApiError('The CivicLens server sent a dashboard this app could not read.', {code: 'malformed_response'});
      return {level: ['city', 'zone', 'ward'].includes(d.level) ? d.level : 'city', cityWide: d.cityWide === true, zone: d.zone ?? null, ward: d.ward ?? null,
        totals: toCounts(d.totals), unverifiedWard: d.unverifiedWard ? toCounts(d.unverifiedWard) : null,
        byStatus: (Array.isArray(d.byStatus) ? d.byStatus : []).map(s => ({status: String(s?.status ?? ''), count: Number(s?.count) || 0})),
        byCategory: (Array.isArray(d.byCategory) ? d.byCategory : []).map(c => ({category: String(c?.category ?? ''), ...toCounts(c)})),
        byZone: (Array.isArray(d.byZone) ? d.byZone : []).filter(z => Number.isInteger(z?.number)).map(z => ({number: z.number, name: String(z.name ?? ''), wards: Number(z.wards) || 0, ...toCounts(z)})),
        byWard: d.byWard.filter(w => Number.isInteger(w?.number)).map(w => ({...toWardRef(w), ...toCounts(w)}))};
    },

    /** Unresolved complaints in the selected wards (or with no verified ward), oldest first. */
    async supervisorComplaints(token, {wards = [], category = '', status = '', minAgeDays = null, maxAgeDays = null, unverifiedWard = false, limit = 100, offset = 0} = {}) {
      const q = new URLSearchParams({limit: String(limit), offset: String(offset)});
      if (unverifiedWard) q.set('unverifiedWard', 'true'); else q.set('wards', wards.join(','));
      if (category) q.set('category', category);
      if (status) q.set('status', status);
      if (minAgeDays !== null && minAgeDays !== '') q.set('minAgeDays', String(minAgeDays));
      if (maxAgeDays !== null && maxAgeDays !== '') q.set('maxAgeDays', String(maxAgeDays));
      const d = await request('GET', '/staff/supervisor/complaints?' + q, undefined, {headers: staffAuth(token)});
      if (!Array.isArray(d?.items)) throw new ApiError('The CivicLens server sent a complaint list this app could not read.', {code: 'malformed_response'});
      return {total: Number(d.total) || 0, items: d.items.filter(i => typeof i?.id === 'string' && PUBLIC_ID.test(i.id)).map(i => ({
        id: i.id, title: String(i.title ?? ''), category: String(i.category ?? ''), area: String(i.area ?? ''), status: String(i.status ?? ''),
        verification: String(i.verification ?? ''), department: String(i.department ?? ''), received: i.received ?? null, ageDays: Number(i.ageDays) || 0,
        priority: typeof i.priority === 'string' ? i.priority : null, dueAt: typeof i.dueAt === 'string' ? i.dueAt : null, overdue: i.overdue === true,
        ward: toWardRef(i.ward), wardAssignmentStatus: WARD_STATUSES.includes(i.wardAssignmentStatus) ? i.wardAssignmentStatus : 'unverified', assignee: staffRef(i.assignee)}))};
    },

    async supervisorStaff(token) {
      const d = await request('GET', '/staff/supervisor/staff', undefined, {headers: staffAuth(token)});
      if (!Array.isArray(d)) throw new ApiError('The CivicLens server sent a staff list this app could not read.', {code: 'malformed_response'});
      return d.filter(x => typeof x?.username === 'string').map(x => ({username: x.username, displayName: String(x.displayName ?? x.username), role: String(x.role ?? ''),
        departments: Array.isArray(x.departments) ? x.departments.map(String) : []}));
    },

    /** Bulk allocation. All or nothing on the server; returns the per-complaint outcome. */
    async allocateComplaints(token, {complaints, department = null, assignee, priority = 'normal', dueAt = null, reason, note = ''}) {
      const d = await request('POST', '/staff/supervisor/allocations', {complaints, department, assignee, priority, dueAt, reason, note}, {headers: staffAuth(token)});
      if (!Array.isArray(d?.results)) throw new ApiError('The CivicLens server sent an allocation result this app could not read.', {code: 'malformed_response', ambiguous: true});
      return {batch: String(d.batch ?? ''), changed: Number(d.changed) || 0, unchanged: Number(d.unchanged) || 0,
        results: d.results.map(r => ({id: String(r?.id ?? ''), outcome: String(r?.outcome ?? '')}))};
    },

    /** Manual ward by a supervisor or admin (audited; the original GPS is kept). */
    async setComplaintWard(token, id, {ward, reason}) {
      return toStaffComplaint(await request('POST', `/staff/complaints/${encodeURIComponent(id)}/ward`, {ward, reason}, {headers: staffAuth(token)}));
    },

    /** Citizen response with the private claim code (in the JSON body only). No staff session. */
    async submitCitizenResponse(id, claim, response) {
      const d = await request('POST', `/complaints/${encodeURIComponent(id)}/citizen-responses`, {claim, response});
      return {verification: String(d.verification ?? ''), changed: d.changed === true};
    },

    // ---------- Duplicate detection (staff). Suggestions only; merging needs a supervisor. ----------

    async getDuplicateCandidates(token, id, {radius = null, days = null} = {}) {
      const q = new URLSearchParams();
      if (radius) q.set('radius', String(radius));
      if (days) q.set('days', String(days));
      const d = await request('GET', `/staff/complaints/${encodeURIComponent(id)}/duplicates${q.size ? '?' + q : ''}`, undefined, {headers: staffAuth(token)});
      if (!Array.isArray(d.candidates)) throw new ApiError('The CivicLens server sent duplicate suggestions this app could not read.', {code: 'malformed_response'});
      return {radiusM: Number(d.radiusM) || 0, days: Number(d.days) || 0, note: typeof d.note === 'string' ? d.note : null,
        candidates: d.candidates.filter(c => publicIdOrNull(c?.id) && DUPLICATE_LABELS.includes(c.label)).map(c => ({
          id: c.id, category: String(c.category ?? ''), area: String(c.area ?? ''), created: c.created ?? null, status: String(c.status ?? ''),
          verification: String(c.verification ?? ''), score: Number(c.score) || 0, label: c.label, distanceM: Number(c.distanceM) || 0,
          factors: (Array.isArray(c.factors) ? c.factors : []).map(f => ({name: String(f?.name ?? ''), value: Number.isFinite(f?.value) ? f.value : null,
            weight: Number(f?.weight) || 0, contribution: Number(f?.contribution) || 0, detail: String(f?.detail ?? '')})),
          reasons: (Array.isArray(c.reasons) ? c.reasons : []).map(String), caution: typeof c.caution === 'string' ? c.caution : null}))};
    },

    /** Merge complaint `id` (the duplicate) into `canonical`. Resolves to the duplicate's updated staff case. */
    async mergeComplaint(token, id, {canonical, reason}) {
      return toStaffComplaint(await request('POST', `/staff/complaints/${encodeURIComponent(id)}/merge`, {canonical, reason}, {headers: staffAuth(token)}));
    },

    async revertMerge(token, mergeId, reason) {
      return toStaffComplaint(await request('POST', `/staff/merges/${encodeURIComponent(mergeId)}/revert`, {reason}, {headers: staffAuth(token)}));
    },

    async dismissDuplicate(token, id, otherId, reason) {
      return toStaffComplaint(await request('POST', `/staff/complaints/${encodeURIComponent(id)}/duplicates/${encodeURIComponent(otherId)}/dismiss`, {reason}, {headers: staffAuth(token)}));
    },

    /**
     * GET /complaints: community issues across Bengaluru (public fields only), newest first, filtered by
     * issue type (category), neighbourhood (area) and status. Facets give the counts for the filter menus.
     */
    async listPublicComplaints({category = '', area = '', status = '', limit = 50, offset = 0} = {}) {
      const q = new URLSearchParams({limit: String(limit), offset: String(offset)});
      if (category) q.set('category', category);
      if (area) q.set('area', area);
      if (status) q.set('status', status);
      const d = await request('GET', '/complaints?' + q);
      if (!Array.isArray(d.items)) throw new ApiError('The CivicLens server sent a community list this app could not read.', {code: 'malformed_response'});
      const facet = list => (Array.isArray(list) ? list : []).filter(f => typeof f?.name === 'string').map(f => ({name: f.name, count: Number(f.count) || 0}));
      return {
        total: Number(d.total) || 0,
        items: d.items.filter(i => typeof i?.id === 'string' && PUBLIC_ID.test(i.id)).map(i => ({
          id: i.id, title: String(i.title ?? ''), category: String(i.category ?? ''), area: String(i.area ?? ''), status: String(i.status ?? ''),
          verification: String(i.verification ?? ''), department: String(i.department ?? ''), created: i.created ?? null, updated: i.updated ?? null,
          mapPoint: toMapPoint(i.mapPoint)})),
        facets: {categories: facet(d.facets?.categories), areas: facet(d.facets?.areas)}
      };
    },

    async getEvidenceAccessLog(token, id) {
      const d = await request('GET', `/staff/complaints/${encodeURIComponent(id)}/evidence/access`, undefined, {headers: staffAuth(token)});
      if (!Array.isArray(d)) throw new ApiError('The CivicLens server sent an access log this app could not read.', {code: 'malformed_response'});
      return d.map(e => ({username: String(e?.username ?? ''), displayName: String(e?.displayName ?? ''), action: String(e?.action ?? ''), accessedAt: e?.accessedAt ?? null}));
    },

    // ---------- AI analysis (staff). Results are suggestions; the server never changes a case because of them. ----------

    /** Public: whether photos may be analysed by an external AI service, and the notice to show before upload. */
    async getAiDisclosure() {
      const d = await request('GET', '/ai');
      return {enabled: d.enabled === true, disclosure: typeof d.disclosure === 'string' ? d.disclosure.slice(0, 500) : null};
    },

    async getAiAnalysis(token, id) {
      return toAiAnalysis(await request('GET', `/staff/complaints/${encodeURIComponent(id)}/ai`, undefined, {headers: staffAuth(token)}));
    },

    async retryAiAnalysis(token, id) {
      return toAiAnalysis(await request('POST', `/staff/complaints/${encodeURIComponent(id)}/ai/retry`, {}, {headers: staffAuth(token)}));
    },

    async getRepairAssessments(token, id) {
      return toRepairAssessments(await request('GET', `/staff/complaints/${encodeURIComponent(id)}/repair-assessment`, undefined, {headers: staffAuth(token)}));
    },

    /** Compare the original photo with an after-repair photo (default: the latest). */
    async verifyRepair(token, id, repairEvidenceId = null) {
      return toRepairAssessments(await request('POST', `/staff/complaints/${encodeURIComponent(id)}/verify-repair`,
        repairEvidenceId ? {repairEvidenceId} : {}, {headers: staffAuth(token)}));
    },

    /** Supervisor review of an AI suggestion: decision 'approved' or 'rejected' (a note is required to reject). */
    async reviewRepairAssessment(token, id, assessmentId, {decision, note = ''}) {
      return toRepairAssessment(await request('POST', `/staff/complaints/${encodeURIComponent(id)}/repair-assessment/${encodeURIComponent(assessmentId)}/review`,
        {decision, note}, {headers: staffAuth(token)}));
    },

    // ---------- Notifications. `token` is a staff session token or this device's citizen inbox token. ----------

    /** GET /notifications/config: whether Web Push is configured, and the VAPID public key. */
    async getPushConfig() {
      const d = await request('GET', '/notifications/config');
      return {pushEnabled: d.pushEnabled === true, vapidPublicKey: typeof d.vapidPublicKey === 'string' && d.vapidPublicKey ? d.vapidPublicKey : null};
    },

    /** POST /notifications/inboxes: a new citizen inbox token for this device (shown once). */
    async createInbox() {
      const d = await request('POST', '/notifications/inboxes', {});
      if (typeof d.inboxToken !== 'string' || !/^[A-Za-z0-9_-]{20,200}$/.test(d.inboxToken)) throw new ApiError('The CivicLens server sent an inbox this app could not read.', {code: 'malformed_response'});
      return d.inboxToken;
    },

    async listNotifications(token, {limit = 30, before = null, unread = false} = {}) {
      const query = `?limit=${encodeURIComponent(limit)}${before ? '&before=' + encodeURIComponent(before) : ''}${unread ? '&unread=true' : ''}`;
      const d = await request('GET', '/notifications' + query, undefined, {headers: bearer(token)});
      if (!Array.isArray(d.items)) throw new ApiError('The CivicLens server sent notifications this app could not read.', {code: 'malformed_response'});
      return {items: d.items.map(toNotification).filter(Boolean), unreadCount: Number(d.unreadCount) || 0,
        nextBefore: typeof d.nextBefore === 'string' && NOTIFICATION_ID.test(d.nextBefore) ? d.nextBefore : null};
    },

    async unreadCount(token) {
      return Number((await request('GET', '/notifications/unread-count', undefined, {headers: bearer(token)})).unreadCount) || 0;
    },

    /** Mark one notification read (read = true) or unread. Resolves to the updated notification. */
    async markNotification(token, id, read = true) {
      const n = toNotification(await request('POST', `/notifications/${encodeURIComponent(id)}/${read ? 'read' : 'unread'}`, {}, {headers: bearer(token)}));
      if (!n) throw new ApiError('The CivicLens server sent a notification this app could not read.', {code: 'malformed_response'});
      return n;
    },

    async markAllNotificationsRead(token) {
      return Number((await request('POST', '/notifications/read-all', {}, {headers: bearer(token)})).updated) || 0;
    },

    /** POST /notifications/subscriptions with PushSubscription.toJSON(). Registering again updates the same device. */
    async registerPushSubscription(token, subscription, label = '') {
      const body = {endpoint: subscription?.endpoint, keys: {p256dh: subscription?.keys?.p256dh, auth: subscription?.keys?.auth},
        expirationTime: Number.isFinite(subscription?.expirationTime) ? subscription.expirationTime : null, label: String(label).slice(0, 80)};
      return toDevice(await request('POST', '/notifications/subscriptions', body, {headers: bearer(token)}));
    },

    async listPushDevices(token) {
      const d = await request('GET', '/notifications/subscriptions', undefined, {headers: bearer(token)});
      if (!Array.isArray(d)) throw new ApiError('The CivicLens server sent a device list this app could not read.', {code: 'malformed_response'});
      return d.map(toDevice);
    },

    async removePushDevice(token, subscriptionId) {
      await request('DELETE', '/notifications/subscriptions/' + encodeURIComponent(subscriptionId), undefined, {headers: bearer(token)});
    },

    /** POST /notifications/follows. The claim code travels in the JSON body only. */
    async followComplaint(token, id, claim) {
      const d = await request('POST', '/notifications/follows', {complaintId: id, claim}, {headers: bearer(token)});
      return {complaintId: String(d.complaintId ?? id), followedAt: d.followedAt ?? null};
    },

    async listFollows(token) {
      const d = await request('GET', '/notifications/follows', undefined, {headers: bearer(token)});
      if (!Array.isArray(d)) throw new ApiError('The CivicLens server sent a follow list this app could not read.', {code: 'malformed_response'});
      return d.filter(f => typeof f?.complaintId === 'string' && PUBLIC_ID.test(f.complaintId)).map(f => ({complaintId: f.complaintId, followedAt: f.followedAt ?? null}));
    },

    async unfollowComplaint(token, id) {
      await request('DELETE', '/notifications/follows/' + encodeURIComponent(id), undefined, {headers: bearer(token)});
    },

    async sendTestNotification(token) {
      const d = await request('POST', '/notifications/test', {}, {headers: bearer(token)});
      return {pushEnabled: d.pushEnabled === true};
    }
  };
}

const bearer = token => ({Authorization: 'Bearer ' + token});

/** The public list's approximate position (about 110 m), or null when missing or not a valid coordinate. */
export function toMapPoint(p) {
  const ok = v => typeof v === 'number' && Number.isFinite(v);
  if (!p || !ok(p.lat) || !ok(p.lng) || Math.abs(p.lat) > 90 || Math.abs(p.lng) > 180) return null;
  return {lat: p.lat, lng: p.lng, precision: String(p.precision ?? 'approximate')};
}

function toResident(r) {
  if (!r || typeof r.email !== 'string') throw new ApiError('The CivicLens server sent an account this app could not read.', {code: 'malformed_response'});
  return {residentId: String(r.residentId ?? ''), email: r.email, displayName: String(r.displayName ?? r.email), demo: r.demo === true};
}

function toResidentSession(d) {
  if (typeof d?.token !== 'string' || !d.token) throw new ApiError('The CivicLens server sent a sign-in response this app could not read.', {code: 'malformed_response'});
  return {token: d.token, expiresAt: d.expiresAt ?? null, resident: toResident(d.resident)};
}

function toResidentPrefs(p) {
  return {updates: p?.updates !== false, verification: p?.verification !== false, hotspots: p?.hotspots === true};
}

/** A complaint as its owner sees it (allowlisted; never a claim code, token or storage key). */
export function toResidentComplaint(d) {
  const gps = d?.gps && Number.isFinite(d.gps.lat) && Number.isFinite(d.gps.lng)
    ? {lat: d.gps.lat, lng: d.gps.lng, accuracy: d.gps.accuracy ?? null, sourceType: d.gps.sourceType ?? null, address: d.gps.address ?? null} : null;
  return {
    id: String(d.id), title: String(d.title || `${d.category} reported by a citizen`), category: String(d.category), area: String(d.area ?? ''),
    description: String(d.description ?? ''), mediaType: d.mediaType === 'video' ? 'video' : 'photo', shareEvidence: d.shareEvidence === true,
    hasEvidence: d.hasEvidence === true, gps, created: d.created ?? null, updated: d.updated ?? null, status: String(d.status),
    verification: String(d.verification), department: String(d.department ?? ''), ownedVia: d.ownedVia === 'claimed' ? 'claimed' : 'submitted',
    ownedAt: d.ownedAt ?? null,
    timeline: Array.isArray(d.timeline) ? d.timeline.map(t => ({status: String(t.status), text: String(t.text), date: t.date})) : [],
    verificationLog: toVerificationLog(d.verificationLog),
    evidence: toOwnerEvidence(d.evidence)
  };
}

/** The owner's evidence list: known fields only, valid IDs only. Never a storage key or URL. */
export function toOwnerEvidence(list) {
  return (Array.isArray(list) ? list : []).filter(e => typeof e?.evidenceId === 'string' && /^ev_[A-Za-z0-9_-]{22}$/.test(e.evidenceId)).map(e => ({
    evidenceId: e.evidenceId, role: e.role === 'repair' ? 'repair' : 'original', mediaType: e.mediaType === 'video' ? 'video' : 'photo',
    contentType: String(e.contentType ?? ''), byteSize: Number(e.byteSize) || 0, capturedAt: e.capturedAt ?? null, uploadedAt: e.uploadedAt ?? null}));
}
const NOTIFICATION_ID = /^nt_[A-Za-z0-9_-]{16,40}$/;
const SUBSCRIPTION_ID = /^ps_[A-Za-z0-9_-]{16,40}$/;
// In-app destinations only: a notification can never send the browser to another site.
export const SAFE_NOTIFICATION_URL = /^#\/[A-Za-z0-9/_-]{0,120}$/;

/** Only known fields are copied; the link is kept only if it is an in-app route. */
export function toNotification(n) {
  if (typeof n?.id !== 'string' || !NOTIFICATION_ID.test(n.id)) return null;
  return {
    id: n.id, kind: String(n.kind ?? ''), title: String(n.title ?? '').slice(0, 120), body: String(n.body ?? '').slice(0, 300),
    url: typeof n.url === 'string' && SAFE_NOTIFICATION_URL.test(n.url) ? n.url : '#/notifications',
    complaintId: typeof n.complaintId === 'string' && PUBLIC_ID.test(n.complaintId) ? n.complaintId : null,
    createdAt: n.createdAt ?? null, readAt: n.readAt ?? null, read: n.read === true || Boolean(n.readAt), pushStatus: String(n.pushStatus ?? '')
  };
}

function toDevice(d) {
  if (typeof d?.subscriptionId !== 'string' || !SUBSCRIPTION_ID.test(d.subscriptionId)) throw new ApiError('The CivicLens server sent a device this app could not read.', {code: 'malformed_response'});
  return {subscriptionId: d.subscriptionId, label: String(d.label ?? ''), createdAt: d.createdAt ?? null, lastSuccessAt: d.lastSuccessAt ?? null,
    lastFailureAt: d.lastFailureAt ?? null, failureCount: Number(d.failureCount) || 0};
}

const EVIDENCE_TYPES = ['image/jpeg', 'video/webm', 'video/mp4'];

/** An evidence response as a Blob, accepted only with an evidence content type (staff and owner views alike). */
async function evidenceBlob(response) {
  const type = String(response.headers.get('Content-Type') || '').split(';')[0].trim().toLowerCase();
  if (!EVIDENCE_TYPES.includes(type)) throw new ApiError('The CivicLens server sent evidence of an unexpected type, so it is not shown.', {code: 'malformed_response'});
  return new Blob([await response.arrayBuffer()], {type});
}

/** Staff-list location for the admin map; null unless both coordinates are valid. */
function toListGps(g) {
  if (!g || !Number.isFinite(g.lat) || !Number.isFinite(g.lng) || Math.abs(g.lat) > 90 || Math.abs(g.lng) > 180) return null;
  return {lat: g.lat, lng: g.lng, accuracy: Number.isFinite(g.accuracy) && g.accuracy >= 0 ? g.accuracy : null,
    sourceType: SOURCE_TYPES.includes(g.sourceType) ? g.sourceType : null};
}
const staffAuth = token => ({Authorization: 'Bearer ' + token});

const STAFF_ACTIONS = ['transition', 'assign', 'create_work_order', 'request_reinspection', 'add_note', 'add_public_update', 'review_duplicates', 'merge', 'revert_merge', 'set_ward'];
const WARD_STATUSES = ['auto', 'manual', 'unverified'];

function toWardRef(w) {
  if (!w || !Number.isInteger(w.number)) return null;
  return {number: w.number, name: String(w.name ?? ''), zone: {number: Number.isInteger(w.zone?.number) ? w.zone.number : null, name: String(w.zone?.name ?? '')}};
}

function toCounts(c) {
  const n = k => Number.isInteger(c?.[k]) ? c[k] : 0;
  return {total: n('total'), open: n('open'), resolved: n('resolved'), pendingVerification: n('pendingVerification'), overdue: n('overdue')};
}
const DUPLICATE_LABELS = ['likely_duplicate', 'possible_duplicate', 'recurrence', 'nearby_separate'];
const MERGE_ID = /^mg_[A-Za-z0-9_-]{16}$/;
const publicIdOrNull = v => (typeof v === 'string' && PUBLIC_ID.test(v) ? v : null);
const staffRef = s => (s && typeof s.username === 'string' ? {username: s.username, displayName: String(s.displayName ?? s.username)} : null);

/** Work order for staff. Only known fields are copied. */
export function toWorkOrder(w) {
  if (typeof w?.id !== 'string' || !/^wo_[A-Za-z0-9_-]{16}$/.test(w.id)) return null;
  return {
    id: w.id, complaintId: String(w.complaintId ?? ''), department: w.department ?? null, team: String(w.team ?? ''), assignee: w.assignee ?? null,
    description: String(w.description ?? ''), dueAt: w.dueAt ?? null, status: String(w.status ?? ''), startedAt: w.startedAt ?? null,
    completedAt: w.completedAt ?? null, completionNote: String(w.completionNote ?? ''), createdBy: String(w.createdBy ?? ''),
    createdAt: w.createdAt ?? null, overdue: w.overdue === true,
    evidence: (Array.isArray(w.evidence) ? w.evidence : []).filter(e => typeof e?.evidenceId === 'string').map(e => ({
      phase: e.phase === 'after' ? 'after' : 'before', evidenceId: e.evidenceId, contentType: String(e.contentType ?? ''),
      byteSize: Number(e.byteSize) || 0, uploadedAt: e.uploadedAt ?? null})),
    history: (Array.isArray(w.history) ? w.history : []).map(h => ({action: String(h?.action ?? ''), to: h?.to ?? null, note: String(h?.note ?? ''),
      by: String(h?.by ?? ''), at: h?.at ?? null}))
  };
}

/** Management data in the staff case: assignment, work orders, internal notes and the server's allowed actions. */
function toManagement(d) {
  const a = d.assignment && typeof d.assignment === 'object' ? d.assignment : {};
  return {
    version: Number(d.version) || 0,
    assignment: {department: a.department && typeof a.department.code === 'string' ? {code: a.department.code, name: String(a.department.name ?? '')} : null,
      assignee: staffRef(a.assignee)},
    assignmentHistory: (Array.isArray(d.assignmentHistory) ? d.assignmentHistory : []).map(h => ({department: String(h?.department?.name ?? ''),
      assignee: h?.assignee ?? null, reason: String(h?.reason ?? ''), by: String(h?.by ?? ''), at: h?.at ?? null})),
    workOrders: (Array.isArray(d.workOrders) ? d.workOrders : []).map(toWorkOrder).filter(Boolean),
    notes: (Array.isArray(d.notes) ? d.notes : []).map(n => ({id: String(n?.id ?? ''), body: String(n?.body ?? ''), createdAt: n?.createdAt ?? null,
      author: staffRef(n?.author)})),
    mergedInto: publicIdOrNull(d.mergedInto),
    merges: (Array.isArray(d.merges) ? d.merges : []).filter(m => typeof m?.id === 'string' && MERGE_ID.test(m.id)).map(m => ({
      id: m.id, duplicate: publicIdOrNull(m.duplicate), canonical: publicIdOrNull(m.canonical), reason: String(m.reason ?? ''),
      score: Number.isFinite(m.score) ? m.score : null, label: DUPLICATE_LABELS.includes(m.label) ? m.label : null, by: String(m.by ?? ''),
      at: m.at ?? null, revertedAt: m.revertedAt ?? null, revertedBy: m.revertedBy ?? null, revertReason: m.revertReason ?? null})),
    ward: toWardRef(d.ward),
    wardAssignmentStatus: WARD_STATUSES.includes(d.wardAssignmentStatus) ? d.wardAssignmentStatus : 'unverified',
    priority: typeof d.priority === 'string' ? d.priority : null,
    dueAt: typeof d.dueAt === 'string' ? d.dueAt : null,
    wardHistory: (Array.isArray(d.wardHistory) ? d.wardHistory : []).map(h => ({from: Number.isInteger(h?.from) ? h.from : null, to: Number.isInteger(h?.to) ? h.to : null,
      method: WARD_STATUSES.includes(h?.method) ? h.method : 'unverified', reason: String(h?.reason ?? ''), by: h?.by ?? null, at: h?.at ?? null})),
    allocations: (Array.isArray(d.allocations) ? d.allocations : []).map(a => ({kind: String(a?.kind ?? ''), ward: a?.ward ?? null,
      from: {assignee: a?.from?.assignee ?? null, priority: a?.from?.priority ?? null, dueAt: a?.from?.dueAt ?? null},
      to: {assignee: a?.to?.assignee ?? null, department: a?.to?.department ?? null, priority: a?.to?.priority ?? null, dueAt: a?.to?.dueAt ?? null},
      reason: String(a?.reason ?? ''), note: String(a?.note ?? ''), by: String(a?.by ?? ''), at: a?.at ?? null})),
    allowedActions: (Array.isArray(d.allowedActions) ? d.allowedActions : []).filter(x => STAFF_ACTIONS.includes(x?.action)).map(x => ({
      action: x.action, label: String(x.label ?? ''), to: typeof x.to === 'string' ? x.to : null, needsExplanation: x.needsExplanation === true}))
  };
}

/** Operational projection for staff. Only known fields are copied; no claim, token or storage data exists to copy. */
export function toStaffComplaint(d) {
  if (typeof d?.id !== 'string' || !PUBLIC_ID.test(d.id) || !d.gps || !Array.isArray(d.timeline)) {
    throw new ApiError('The CivicLens server sent a complaint this app could not read.', {code: 'malformed_response'});
  }
  const g = d.gps;
  const e = d.evidence;
  return {
    id: d.id, category: String(d.category ?? ''), area: String(d.area ?? ''), description: String(d.description ?? ''),
    status: String(d.status ?? ''), verification: String(d.verification ?? ''), department: String(d.department ?? ''),
    created: d.created ?? null, capturedAt: d.capturedAt ?? null, uploadedAt: d.uploadedAt ?? null, updated: d.updated ?? null,
    captureSource: d.captureSource ?? null, mediaType: d.mediaType === 'video' ? 'video' : 'photo', shareEvidence: d.shareEvidence === true,
    gps: {lat: g.lat, lng: g.lng, accuracy: g.accuracy ?? null, source: g.source ?? null, sourceType: g.sourceType ?? null, timestamp: g.timestamp ?? null, address: g.address ?? null},
    timeline: d.timeline.map(t => ({status: String(t?.status ?? ''), text: String(t?.text ?? ''), date: t?.date ?? null})),
    verificationLog: toVerificationLog(d.verificationLog),
    evidence: e && typeof e === 'object' ? {evidenceId: String(e.evidenceId ?? ''), mediaType: e.mediaType === 'video' ? 'video' : 'photo',
      contentType: String(e.contentType ?? ''), byteSize: Number(e.byteSize) || 0, sha256: String(e.sha256 ?? ''), capturedAt: e.capturedAt ?? null, uploadedAt: e.uploadedAt ?? null} : null,
    ...toManagement(d)
  };
}

/** Filename sent with the file part; the server ignores it for storage and checks only its extension. */
export function evidenceFilename(blob) {
  const type = String(blob?.type || '').toLowerCase();
  if (type.startsWith('video/mp4')) return 'evidence.mp4';
  if (type.startsWith('video/')) return 'evidence.webm';
  return 'evidence.jpg';
}

function toEvidenceReceipt(d) {
  if (typeof d.evidenceId !== 'string' || !EVIDENCE_STATUSES.includes(d.status) || !/^[0-9a-f]{64}$/.test(String(d.sha256)) || !Number.isInteger(d.byteSize)) {
    throw new ApiError('The CivicLens server sent an upload result this app could not read. Retry the upload to check.', {code: 'malformed_response', ambiguous: true});
  }
  return {evidenceId: d.evidenceId, status: d.status, mediaType: d.mediaType === 'video' ? 'video' : 'photo', contentType: String(d.contentType ?? ''),
    byteSize: d.byteSize, sha256: d.sha256, capturedAt: d.capturedAt ?? null, uploadedAt: d.uploadedAt ?? null};
}

// ---------- Request mapping ----------

const str = (v, max) => (typeof v === 'string' && v.trim() ? v.trim().slice(0, max) : undefined);
const finite = v => typeof v === 'number' && Number.isFinite(v);

function toIso(value) {
  if (value == null || value === '') return undefined;
  const ms = typeof value === 'number' ? value : Date.parse(value);
  if (!Number.isFinite(ms)) return undefined;
  if (typeof value === 'string' && !/(Z|[+-]\d{2}:?\d{2})$/i.test(value.trim())) return undefined; // no zone: ambiguous
  return new Date(ms).toISOString();
}

function toEpochMs(value) {
  if (finite(value) && value > 0) return Math.round(value);
  if (typeof value === 'string' && /(Z|[+-]\d{2}:?\d{2})$/i.test(value.trim())) {
    const ms = Date.parse(value);
    if (Number.isFinite(ms)) return ms;
  }
  return null;
}

/**
 * Builds the POST /complaints body from the capture screen's report object. Only fields the API
 * accepts are copied. `owner`, `uploadedAt` and the media Blob are never sent. Throws ApiError
 * (code "missing_location") when there is no usable GPS fix: the API requires one, and no
 * coordinate is ever invented.
 */
export function toCreatePayload(data) {
  const gps = data?.gps;
  if (!gps || !finite(gps.lat) || !finite(gps.lng) || Math.abs(gps.lat) > 90 || Math.abs(gps.lng) > 180) {
    throw new ApiError('This report has no usable GPS location. The CivicLens server requires one, so it cannot be sent in server mode.', {code: 'missing_location'});
  }
  if (!SOURCE_TYPES.includes(gps.sourceType)) {
    throw new ApiError('This report’s location has no recognised source (device GPS or photo EXIF), so it cannot be sent in server mode.', {code: 'missing_location'});
  }
  const outGps = {
    lat: gps.lat,
    lng: gps.lng,
    accuracy: finite(gps.accuracy) && gps.accuracy >= 0 ? gps.accuracy : null,
    sourceType: gps.sourceType,
    timestamp: toEpochMs(gps.timestamp)
  };
  const source = str(gps.source, 80);
  if (source) outGps.source = source;
  if (Number.isInteger(gps.fixAgeSeconds) && gps.fixAgeSeconds >= 0 && gps.fixAgeSeconds <= 86400) outGps.fixAgeSeconds = gps.fixAgeSeconds;
  const gpsArea = str(gps.neighbourhood, 120);
  if (gpsArea) outGps.neighbourhood = gpsArea;
  const address = str(gps.address, 300);
  if (address) outGps.address = address;

  const payload = {
    category: data.category,
    mediaType: data.mediaType === 'video' ? 'video' : 'photo',
    description: typeof data.description === 'string' ? data.description.trim().slice(0, 1000) : '',
    shareEvidence: data.shareEvidence === true,
    gps: outGps
  };
  const neighbourhood = str(data.neighbourhood, 120);
  if (neighbourhood) payload.neighbourhood = neighbourhood;
  const capturedAt = toIso(data.capturedAt ?? data.created);
  if (capturedAt) payload.capturedAt = capturedAt;
  const captureSource = str(data.captureSource, 80);
  if (captureSource) payload.captureSource = captureSource;
  return payload;
}

// ---------- Response mapping ----------
// Only known fields are copied, so an unexpected server field can never reach the UI.

function toReceipt(d) {
  if (typeof d.id !== 'string' || !PUBLIC_ID.test(d.id) || typeof d.claim !== 'string' || !d.claim || !d.gps || typeof d.gps !== 'object') {
    throw new ApiError('The CivicLens server sent a receipt this app could not read. The report may have been saved; do not resubmit straight away.', {code: 'malformed_response', ambiguous: true});
  }
  const g = d.gps;
  return {
    id: d.id, claim: d.claim, category: String(d.category ?? ''), area: String(d.area ?? ''),
    created: d.created ?? null, capturedAt: d.capturedAt ?? null, uploadedAt: d.uploadedAt ?? null, captureSource: d.captureSource ?? null,
    gps: {lat: g.lat, lng: g.lng, accuracy: g.accuracy ?? null, source: g.source ?? null, sourceType: g.sourceType ?? null, timestamp: g.timestamp ?? null, address: g.address ?? null},
    mediaType: d.mediaType === 'video' ? 'video' : 'photo', shareEvidence: d.shareEvidence === true,
    status: String(d.status ?? ''), verification: String(d.verification ?? ''),
    uploadToken: typeof d.uploadToken === 'string' ? d.uploadToken : null, uploadTokenExpiresAt: d.uploadTokenExpiresAt ?? null,
    replayed: d.replayed === true
  };
}

export function toPublicRecord(d) {
  if (typeof d.id !== 'string' || !Array.isArray(d.timeline)) {
    throw new ApiError('The CivicLens server sent a tracking record this app could not read.', {code: 'malformed_response'});
  }
  return {
    id: d.id, category: String(d.category ?? ''), area: String(d.area ?? ''), status: String(d.status ?? ''),
    verification: String(d.verification ?? ''), department: String(d.department ?? ''), updated: d.updated ?? null,
    mergedInto: publicIdOrNull(d.mergedInto),
    timeline: d.timeline.map(t => ({status: String(t?.status ?? ''), text: String(t?.text ?? ''), date: t?.date ?? null})),
    verificationLog: toVerificationLog(d.verificationLog)
  };
}

/** Verification history (citizen responses and verification changes): known fields only. */
function toVerificationLog(list) {
  return (Array.isArray(list) ? list : []).map(e => ({
    id: String(e?.id ?? ''), date: e?.date ?? null, type: String(e?.type ?? ''), from: e?.from ?? null, to: e?.to ?? null,
    response: e?.response ?? null, by: String(e?.by ?? ''), actor: String(e?.actor ?? ''), note: String(e?.note ?? '')
  }));
}

// ---------- AI analysis ----------

const AI_STATES = ['pending', 'running', 'completed', 'failed', 'skipped'];
const OUTCOMES = ['likely_repaired', 'damage_visible', 'inconclusive'];
const unit = v => (typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= 1 ? v : null);
const px = v => (typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : 0);

function toAiStatus(a) {
  return {enabled: a?.enabled === true, provider: typeof a?.provider === 'string' ? a.provider : null,
    reason: typeof a?.reason === 'string' ? a.reason : null, disclosure: typeof a?.disclosure === 'string' ? a.disclosure : null,
    thresholds: {present: unit(a?.thresholds?.present) ?? 0.5, low: unit(a?.thresholds?.low) ?? 0.25}};
}

/** One detection: class, mapped category (or null), model confidence and a top-left pixel box in the original photo. */
export function toAiDetection(d) {
  if (typeof d?.className !== 'string' || unit(d.confidence) === null || !d.bbox || typeof d.bbox !== 'object') return null;
  return {className: d.className.slice(0, 100), category: typeof d.category === 'string' ? d.category : null, confidence: d.confidence,
    bbox: {x: px(d.bbox.x), y: px(d.bbox.y), width: px(d.bbox.width), height: px(d.bbox.height)}};
}

function toAiInference(i) {
  if (typeof i?.id !== 'string' || !AI_STATES.includes(i.state)) return null;
  return {id: i.id, evidenceId: String(i.evidenceId ?? ''), evidenceRole: i.evidenceRole === 'repair' ? 'repair' : 'original',
    detector: String(i.detector ?? ''), provider: String(i.provider ?? ''), mechanism: i.mechanism === 'model' ? 'model' : 'workflow',
    modelId: String(i.modelId ?? ''), modelVersion: i.modelVersion ?? null, classPrompt: i.classPrompt ?? null, state: i.state,
    detectedCategory: i.detectedCategory ?? null, originalClass: i.originalClass ?? null, topConfidence: unit(i.topConfidence),
    detections: (Array.isArray(i.detections) ? i.detections : []).map(toAiDetection).filter(Boolean),
    imageWidth: Number(i.imageWidth) || null, imageHeight: Number(i.imageHeight) || null, errorCode: i.errorCode ?? null,
    attempts: Number(i.attempts) || 0, completedAt: i.completedAt ?? null};
}

export function toAiAnalysis(d) {
  if (!d || typeof d !== 'object' || !Array.isArray(d.inferences)) throw new ApiError('The CivicLens server sent AI results this app could not read.', {code: 'malformed_response'});
  return {complaintId: String(d.complaintId ?? ''), category: String(d.category ?? ''), ai: toAiStatus(d.ai),
    routing: {detectors: (Array.isArray(d.routing?.detectors) ? d.routing.detectors : []).map(String)},
    inferences: d.inferences.map(toAiInference).filter(Boolean), reset: Number.isFinite(d.reset) ? d.reset : null};
}

export function toRepairAssessment(a) {
  if (typeof a?.id !== 'string' || !/^ra_[A-Za-z0-9_-]{22}$/.test(a.id)) return null;
  const details = a.details && typeof a.details === 'object' ? a.details : {};
  const review = a.review && typeof a.review === 'object' ? a.review : {};
  return {id: a.id, workOrderId: a.workOrderId ?? null, originalEvidenceId: String(a.originalEvidenceId ?? ''), repairEvidenceId: String(a.repairEvidenceId ?? ''),
    state: String(a.state ?? ''), suggestedOutcome: OUTCOMES.includes(a.suggestedOutcome) ? a.suggestedOutcome : null,
    verificationConfidence: unit(a.verificationConfidence),
    locationCheck: ['match', 'mismatch', 'unverified'].includes(a.locationCheck) ? a.locationCheck : null,
    gpsDistanceM: typeof a.gpsDistanceM === 'number' && Number.isFinite(a.gpsDistanceM) ? a.gpsDistanceM : null,
    reasons: (Array.isArray(details.reasons) ? details.reasons : []).map(r => String(r).slice(0, 300)),
    visualMatch: typeof details.visualMatch === 'string' ? details.visualMatch : null,
    matchedFeatures: Number(details.alignment?.inliers) || 0,
    errorCode: a.errorCode ?? null,
    review: {status: ['pending_review', 'approved', 'rejected'].includes(review.status) ? review.status : 'pending_review',
      reviewer: review.reviewer ?? null, reviewerName: review.reviewerName ?? null, note: String(review.note ?? ''), reviewedAt: review.reviewedAt ?? null},
    verifiedResolution: ['verified_repaired', 'verified_not_repaired'].includes(a.verifiedResolution) ? a.verifiedResolution : 'unverified',
    requestedBy: a.requestedBy ?? null, createdAt: a.createdAt ?? null, completedAt: a.completedAt ?? null};
}

function toRepairAssessments(d) {
  if (!d || typeof d !== 'object' || !Array.isArray(d.assessments)) throw new ApiError('The CivicLens server sent repair assessments this app could not read.', {code: 'malformed_response'});
  return {ai: toAiStatus(d.ai), assessments: d.assessments.map(toRepairAssessment).filter(Boolean)};
}
