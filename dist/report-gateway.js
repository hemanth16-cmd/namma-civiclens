// Routes report submission and public tracking to local storage (default) or the CivicLens API,
// according to api-config.json. There is no fallback between the two: a failed server request is
// reported as a failure, never quietly saved locally, and a missing server record is never
// replaced by a local one.
//
// Backend mode works in two separate steps: (1) the complaint details are created, then (2) the
// evidence file is uploaded to that complaint. A failed upload never re-creates the complaint;
// retrying sends the same file to the same complaint, which the server treats idempotently.

import {api, mediaDB} from './services.js';
import {loadApiConfig} from './api-config.js';
import {ApiError, createApiClient, toCreatePayload} from './api-client.js';
import {enablePush, ensureInbox, inbox, pushState, requestPermissionNow} from './push-client.js';

// A copy of server-mode evidence always stays in this browser until the user removes it; it is
// never deleted automatically, even after a confirmed upload. A separate key prefix keeps it apart
// from local-mode evidence, which is keyed by the bare ID.
export const serverEvidenceKey = id => 'server-evidence:' + id;

// The latest server receipt, held only in memory so the receipt page can show the claim code
// once. It is never written to localStorage, sessionStorage, IndexedDB, the URL or a global.
let lastServerReceipt = null;

// Per-report upload context, in memory only: upload token, claim code (to renew an expired token),
// the evidence Blob and the current upload state. Cleared by forgetUpload() when the receipt page
// is left, and lost on reload by design.
const uploads = new Map();

const RETRYABLE_CODES = new Set(['network_error', 'timeout', 'storage_unavailable', 'request_timeout', 'incomplete_upload', 'http_error', 'malformed_response', 'rate_limited']);

async function settings(options) {
  return options.config ?? loadApiConfig();
}

function client(config, options) {
  return createApiClient({baseUrl: config.baseUrl, fetchImpl: options.fetchImpl, timeoutMs: options.timeoutMs, uploadTimeoutMs: options.uploadTimeoutMs});
}

export async function reportMode(options = {}) {
  return (await settings(options)).mode;
}

// Server reports submitted from this browser, so "My reports" can find them again (there are no
// citizen accounts). Only the public ID, category and submission time are kept: never the claim
// code, upload token, location or description.
const REPORTS_KEY = 'ncl-server-reports-v1';
const REPORT_ID = /^NCL-\d{4}-[A-Z0-9]{6}$/;
const MAX_REMEMBERED = 200;

function storageOf(options) {
  try { return options.storage ?? globalThis.localStorage ?? null; } catch { return null; }
}

/** Newest first. Entries that are not well-formed are dropped. */
export function rememberedServerReports(options = {}) {
  let list = [];
  try { list = JSON.parse(storageOf(options)?.getItem(REPORTS_KEY) || '[]'); } catch { list = []; }
  if (!Array.isArray(list)) return [];
  return list.filter(r => typeof r?.id === 'string' && REPORT_ID.test(r.id))
    .map(r => ({id: r.id, category: String(r.category ?? '').slice(0, 80), submittedAt: typeof r.submittedAt === 'string' ? r.submittedAt : null}));
}

function writeRemembered(list, options) {
  try { storageOf(options)?.setItem(REPORTS_KEY, JSON.stringify(list.slice(0, MAX_REMEMBERED))); return true; } catch { return false; }
}

/** Adds or refreshes one report. Returns false when browser storage is unavailable (the report itself is unaffected). */
export function rememberServerReport({id, category = '', submittedAt = null}, options = {}) {
  if (typeof id !== 'string' || !REPORT_ID.test(id)) return false;
  const rest = rememberedServerReports(options).filter(r => r.id !== id);
  return writeRemembered([{id, category: String(category).slice(0, 80), submittedAt: submittedAt ?? new Date().toISOString()}, ...rest], options);
}

/** Removes a report from this browser's list only. The complaint on the server is unchanged. */
export function forgetServerReport(id, options = {}) {
  return writeRemembered(rememberedServerReports(options).filter(r => r.id !== id), options);
}

/**
 * Saves a report. Local mode: unchanged api.create(). Backend mode: creates the complaint from
 * metadata, keeps a copy of the evidence in IndexedDB and prepares (but does not start) the upload,
 * which the receipt page runs with uploadEvidence(). Resolves to {mode, id}; rejects if the
 * complaint itself was not confirmed.
 */
export async function submitReport(data, blob, options = {}) {
  const config = await settings(options);
  if (config.mode === 'local') {
    const record = await api.create(data, blob);
    return {mode: 'local', id: record.id};
  }
  if (config.mode !== 'backend') throw new ApiError(config.problem, {code: 'config_invalid'});
  const payload = toCreatePayload(data);
  // options.idempotencyKey: one key per report, reused for every retry of that report (see capture.js).
  const {uploadToken, uploadTokenExpiresAt, ...receipt} = await client(config, options).createComplaint(payload, {idempotencyKey: options.idempotencyKey});
  rememberServerReport({id: receipt.id, category: receipt.category, submittedAt: receipt.uploadedAt ?? receipt.created}, options);
  const hasFile = blob instanceof Blob && blob.size > 0;
  let evidenceKept = false;
  if (hasFile) {
    try {
      await mediaDB.put(serverEvidenceKey(receipt.id), blob);
      evidenceKept = true;
    } catch {
      evidenceKept = false; // the server already has the report; the receipt says the file was not kept
    }
  }
  uploads.set(receipt.id, {
    config, token: uploadToken, tokenExpiresAt: uploadTokenExpiresAt, claim: receipt.claim,
    blob: hasFile ? blob : null, capturedAt: payload.capturedAt ?? null,
    state: hasFile && uploadToken ? 'pending' : 'unavailable', evidence: null, inFlight: null
  });
  // A device that already turned notifications on follows its new reports automatically (before the
  // evidence upload, so "evidence received" is notified too). Never blocks or fails the submission.
  let following = false;
  const inboxToken = inbox.get(options.env);
  if (inboxToken && pushState('citizen', options.env) === 'enabled') {
    try {
      await client(config, options).followComplaint(inboxToken, receipt.id, receipt.claim);
      following = true;
    } catch {
      following = false;
    }
  }
  lastServerReceipt = {receipt, evidenceKept, following};
  return {mode: 'backend', id: receipt.id};
}

/**
 * Follow a just-submitted report on this device and turn on push (called from a click on the
 * receipt page; it asks for notification permission first). Uses the claim code held in memory.
 * Resolves to {following, push: 'enabled'|'denied'|'dismissed'|'unsupported'|'unavailable', message}.
 */
export async function notifyAboutReport(id, options = {}) {
  const ctx = uploads.get(id);
  if (!ctx?.claim) return {following: false, message: 'The claim code is no longer available on this page. Follow the report from Notifications with its ID and claim code.'};
  await requestPermissionNow(options.env);
  const http = client(options.config ?? ctx.config, options);
  let token;
  try {
    token = await ensureInbox(http, options.env);
    await http.followComplaint(token, id, ctx.claim);
  } catch (err) {
    return {following: false, message: err instanceof ApiError ? err.message : 'Following this report failed.'};
  }
  try {
    const result = await enablePush({http, token, audience: 'citizen', config: await http.getPushConfig(), env: options.env});
    return {following: true, push: result.state, message: result.message ?? ''};
  } catch (err) {
    // e.g. the browser's push service could not be reached: history still records every update.
    return {following: true, push: 'failed', message: err instanceof ApiError ? err.message : 'This browser could not register for push notifications.'};
  }
}

/** Returns the in-memory server receipt for `id` once, then forgets it. */
export function takeServerReceipt(id) {
  if (!lastServerReceipt || lastServerReceipt.receipt.id !== id) return null;
  const held = lastServerReceipt;
  lastServerReceipt = null;
  return held;
}

/** Current upload state for a server-mode report: pending | uploading | uploaded | failed | unavailable. */
export function uploadState(id) {
  return uploads.get(id)?.state ?? 'unavailable';
}

/** Drops the in-memory upload context (token, claim code, Blob). The IndexedDB copy is kept. */
export function forgetUpload(id) {
  uploads.delete(id);
}

/**
 * Uploads (or re-uploads) the evidence for an existing server complaint. Never creates a complaint.
 * Resolves to {state: 'uploaded', evidence} or {state: 'failed'|'unavailable', message, retryable}.
 * An expired or rejected token is renewed once with the claim code held in memory.
 */
export async function uploadEvidence(id, options = {}) {
  const ctx = uploads.get(id);
  if (!ctx) return {state: 'unavailable', retryable: false, message: 'The upload details are no longer available on this page. The complaint is saved; the evidence copy remains in this browser.'};
  if (ctx.state === 'uploaded') return {state: 'uploaded', evidence: ctx.evidence};
  if (!ctx.blob) return {state: 'unavailable', retryable: false, message: 'There is no evidence file to upload for this report.'};
  if (ctx.inFlight) return ctx.inFlight; // a double click shares the request in progress
  ctx.state = 'uploading';
  ctx.inFlight = (async () => {
    const http = client(options.config ?? ctx.config, options);
    const send = () => http.uploadEvidence(id, ctx.blob, {token: ctx.token, capturedAt: ctx.capturedAt});
    try {
      let evidence;
      try {
        evidence = await send();
      } catch (err) {
        if (!(err instanceof ApiError && err.status === 401 && ctx.claim)) throw err;
        const renewed = await http.requestUploadToken(id, ctx.claim);
        ctx.token = renewed.uploadToken;
        ctx.tokenExpiresAt = renewed.uploadTokenExpiresAt;
        evidence = await send();
      }
      ctx.state = 'uploaded';
      ctx.evidence = evidence;
      return {state: 'uploaded', evidence};
    } catch (err) {
      ctx.state = 'failed';
      const known = err instanceof ApiError;
      return {
        state: 'failed',
        code: known ? err.code : 'error',
        // A full evidence store (507) does not clear by retrying, so it is not offered for retry.
        retryable: !known || RETRYABLE_CODES.has(err.code) || (err.status >= 500 && err.code !== 'storage_quota_exceeded'),
        message: known ? err.message : 'The evidence upload failed.'
      };
    } finally {
      ctx.inFlight = null;
    }
  })();
  return ctx.inFlight;
}

/**
 * Public tracking. Resolves to {mode, record, error}: record is null when nothing matches, and
 * error is a user-facing message when the lookup itself failed (backend or configuration).
 */
export async function trackPublic(id, options = {}) {
  const config = await settings(options);
  if (config.mode === 'local') return {mode: 'local', record: await api.publicRecord(id), error: null};
  if (config.mode !== 'backend') return {mode: config.mode, record: null, error: config.problem};
  try {
    return {mode: 'backend', record: await client(config, options).getPublicComplaint(id), error: null};
  } catch (err) {
    return {mode: 'backend', record: null, error: err instanceof ApiError ? err.message : 'The CivicLens server lookup failed.'};
  }
}
