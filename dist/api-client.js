// HTTP adapter for the CivicLens API (backend/; see backend/README.md).
// It never logs request or response bodies, so claim codes, coordinates and descriptions
// cannot leak through the console. Complaint creation is never retried automatically: after a
// network failure or timeout the server may already have saved the report.

const DEFAULT_TIMEOUT_MS = 10000;
const DEFAULT_UPLOAD_TIMEOUT_MS = 180000;  // a 200 MB video on a slow connection needs minutes
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
    async createComplaint(payload, {idempotencyKey} = {}) {
      return toReceipt(await request('POST', '/complaints', payload, {headers: idempotencyKey ? {'Idempotency-Key': idempotencyKey} : {}}));
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
          mediaType: i.mediaType === 'video' ? 'video' : 'photo', hasEvidence: i.hasEvidence === true
        }))
      };
    },

    async getStaffComplaint(token, id) {
      return toStaffComplaint(await request('GET', `/staff/complaints/${encodeURIComponent(id)}`, undefined, {headers: staffAuth(token)}));
    },

    /** GET /staff/complaints/{id}/evidence. Resolves to a Blob of an allowed media type. Each call is logged by the server. */
    async fetchStaffEvidence(token, id) {
      const response = await request('GET', `/staff/complaints/${encodeURIComponent(id)}/evidence`, undefined, {headers: staffAuth(token), timeout: uploadTimeoutMs, raw: true});
      const type = String(response.headers.get('Content-Type') || '').split(';')[0].trim().toLowerCase();
      if (!EVIDENCE_TYPES.includes(type)) throw new ApiError('The CivicLens server sent evidence of an unexpected type, so it is not shown.', {code: 'malformed_response'});
      const bytes = await response.arrayBuffer();
      return new Blob([bytes], {type});
    },

    async getEvidenceAccessLog(token, id) {
      const d = await request('GET', `/staff/complaints/${encodeURIComponent(id)}/evidence/access`, undefined, {headers: staffAuth(token)});
      if (!Array.isArray(d)) throw new ApiError('The CivicLens server sent an access log this app could not read.', {code: 'malformed_response'});
      return d.map(e => ({username: String(e?.username ?? ''), displayName: String(e?.displayName ?? ''), action: String(e?.action ?? ''), accessedAt: e?.accessedAt ?? null}));
    }
  };
}

const EVIDENCE_TYPES = ['image/jpeg', 'video/webm', 'video/mp4'];
const staffAuth = token => ({Authorization: 'Bearer ' + token});

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
    evidence: e && typeof e === 'object' ? {evidenceId: String(e.evidenceId ?? ''), mediaType: e.mediaType === 'video' ? 'video' : 'photo',
      contentType: String(e.contentType ?? ''), byteSize: Number(e.byteSize) || 0, sha256: String(e.sha256 ?? ''), capturedAt: e.capturedAt ?? null, uploadedAt: e.uploadedAt ?? null} : null
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
    timeline: d.timeline.map(t => ({status: String(t?.status ?? ''), text: String(t?.text ?? ''), date: t?.date ?? null})),
    verificationLog: (Array.isArray(d.verificationLog) ? d.verificationLog : []).map(e => ({
      id: String(e?.id ?? ''), date: e?.date ?? null, type: String(e?.type ?? ''), from: e?.from ?? null, to: e?.to ?? null,
      response: e?.response ?? null, by: String(e?.by ?? ''), actor: String(e?.actor ?? ''), note: String(e?.note ?? '')
    }))
  };
}
