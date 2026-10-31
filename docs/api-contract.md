# Namma CivicLens — Proposed API contract

**Status: mostly proposed.** This document describes the HTTP API a FastAPI backend would expose so that the browser prototype's rules move to the server without changing the screens. In local mode (the default) the same operations run in `dist/services.js` against `localStorage` and IndexedDB, and the functions are named to match these endpoints.

**Implemented in `backend/` so far** (see `backend/README.md` for exact request and response shapes): `GET /api/v1/health`, `POST /api/v1/complaints`, `GET /api/v1/complaints/{publicId}`, `POST /api/v1/complaints/{publicId}/upload-token` `POST /api/v1/complaints/{publicId}/evidence` (section 5) the staff console endpoints under `/api/v1/staff` (section 3.1), complaint management (section 3.2: lifecycle, assignment, work orders, notes, citizen verification, audit), duplicate detection (section 3.3), the community list (section 2.3) and Web Push notifications under `/api/v1/notifications` (section 9.1), with per-IP/per-complaint rate limits (`429 rate_limited` + `Retry-After`), an evidence storage quota (`507 storage_quota_exceeded`) and `Idempotency-Key` support on complaint creation (section 2.1). Everything else below is still proposed. Evidence files can be stored on local disk (default) or in a private Supabase Storage bucket (`EVIDENCE_STORAGE_BACKEND=supabase`), and complaint data in SQLite (default) or PostgreSQL / Supabase Postgres (`DATABASE_BACKEND=postgres`); the API contract is identical in every combination. Resident accounts are implemented under `/api/v1/residents` (section 2.2). Shared (multi-process) rate limiting is not built.

Conventions: JSON over HTTPS under `/api/v1`. Times are ISO 8601 in UTC; the UI displays Asia/Kolkata. Errors use `{ "error": { "code": "...", "message": "..." } }`. Every mutation is authorised on the server, never by the client's role guard.

## 1. Roles and projections

Five callers exist. The server chooses the projection from the authenticated principal, never from a request field.

| Caller | Projection | Never returned |
| --- | --- | --- |
| Anonymous public | Public ID, category, neighbourhood, lifecycle, verification, department, public timeline and verification history | Claim code, owner, precise GPS, media, internal notes, reviews |
| Signed-in community user | Community projection: shared original evidence and location only where the owner shared them; shared repairs; reviews | Claim code, owner identity, internal notes, unshared media |
| Owner | Own case, own evidence, full repair history | Internal notes, other owners' claim data |
| Official | Operational fields, internal notes, priority inputs, all repairs, work orders | Claim code (not needed to act on a case) |
| Video reviewer (official, admin-only scope) | Footage metadata, observations, frames, export | Any footage link outside a signed, short-lived URL |

Claim codes are hashed server-side and shown once, to the submitting client, at creation. They are never placed on shared evidence.

## 2. Complaints

| Method and path | Caller | Purpose | Prototype equivalent |
| --- | --- | --- | --- |
| `POST /api/v1/complaints` | Anonymous or signed-in | Create a case from capture or validated gallery upload. Returns public ID and a one-time claim code. Requires coordinates; gallery uploads are validated for JPEG EXIF GPS server-side. | `api.create()` |
| `GET /api/v1/complaints/{publicId}` | Anonymous | Public tracking projection | `api.publicRecord()` |
| `GET /api/v1/residents/me/complaints` | Signed-in resident | Owner's cases (implemented, section 2.2) | `api.list(viewer)` |
| `GET /api/v1/complaints?scope=community` | Signed-in user | Community projection with filters | `api.list(viewer)` with community scope |
| `GET /api/v1/complaints/{id}` | Role-dependent | Detail, projected by principal | `api.detail(id, viewer)` |
| `GET /api/v1/complaints/{id}/receipt` | Submitter only | Receipt, including claim-code presence and evidence copy metadata | `api.receipt(id)` |
| `POST /api/v1/residents/me/claims` | Signed-in resident | Claim by public ID and private code. Rate-limited; generic error on mismatch (implemented, section 2.2) | `api.claim()` |
| `POST /api/v1/complaints/{id}/share` | Owner | Set `shareEvidence` for original evidence and location | `api.shareOriginal()` |
| `PUT /api/v1/complaints/{id}/follow` | Signed-in user | Per-account follow state | `api.toggleFollow()` |

### 2.1 Implemented (Phase 3.5): idempotency and rate limits

`POST /api/v1/complaints` accepts an optional `Idempotency-Key` header (22–128 URL-safe characters; clients should use at least 128 random bits). The server stores only a SHA-256 hash of the key and a fingerprint of the normalised request, in the same transaction as the complaint.

- Same key, same request, original credentials never used, within 24 h → `200`, same complaint, `"replayed": true`, `Idempotent-Replayed: true`, and a **newly issued** claim code and upload token (the earlier ones stop working). The server never stores claim codes or tokens in recoverable form, so the original receipt cannot be repeated; re-issuing is the documented recovery.
- Same key, same request, but the credentials were used (evidence uploaded or token renewed) or the window passed → `409 idempotency_key_used` with `complaintId` only.
- Same key, different request → `409 idempotency_key_conflict`. Malformed key → `400 invalid_idempotency_key`.
- Concurrent requests with one key create one complaint.

Rate limits (per client IP unless stated; configurable, in-process, reset on restart): create 5 per 10 min; tracking 60 per min; upload-token 5 per 15 min per IP and 10 per hour per complaint; evidence 30 per hour per IP before authorisation and 10 authorised attempts per hour per complaint. Exceeded → `429 rate_limited` with `Retry-After`. Forwarded headers are honoured only from configured trusted proxies.

### 2.2 Implemented: resident accounts and ownership

Implemented in `backend/app/residents_api.py` (exact shapes: `backend/README.md`, "Resident accounts API").
Passerby, resident and staff are separate principals:

| Principal | How it is established | Complaint ownership |
| --- | --- | --- |
| Passerby | No credentials | None. The complaint stays unowned until claimed. |
| Resident | `POST /residents/sessions` (or registration), then a resident bearer token | Taken from the session on `POST /complaints`, or set by `POST /residents/me/claims` with the private claim code |
| Staff | `POST /staff/sessions`, then a staff bearer token; role and departments come from the server | Staff act on complaints but never own them |

Tokens are not interchangeable: a resident token is `401` on `/staff/*`, a staff token is `401` on
`/residents/*` and on `POST /complaints`. The server never reads `owner` or `role` from a request body.

## 3. Staff operations

| Method and path | Caller | Purpose | Prototype equivalent |
| --- | --- | --- | --- |
| `PATCH /api/v1/complaints/{id}` | Official | Lifecycle status, team, public update, internal note, verification and note. Every change writes an audit entry. | `api.updateCase()` |
| `POST /api/v1/complaints/{id}/work-orders` | Official | Create a work order | `api.addWorkOrder()` |
| `POST /api/v1/complaints/{id}/reinspections` | Official | Request reinspection; sets verification to Reinspection Required | `api.requestReinspection()` |
| `POST /api/v1/complaints/{id}/repairs` | Official | Append a repair record with optional EXIF validation and sharing flag. Append-only; no overwrite | `api.addRepair()` |
| `POST /api/v1/complaints/{id}/citizen-responses` | Signed-in user | Appears Resolved, Still Present or Cannot Confirm. Changes verification only under the rules in section 6 | `api.citizenResponse()` |
| `POST /api/v1/complaints/{id}/reviews` | Signed-in user | Add a community review. Never changes lifecycle or verification | `api.addReview()` |
| `GET /api/v1/work-orders` | Official | Work-order table with overdue flags | workspace work-orders view |

### 3.1 Implemented (Phase 3.6): staff console

Staff authenticate with server accounts created by an administrator (`python -m app.staff create`; no default account). Roles since schema 7: `official`, `supervisor`, `admin` (section 3.2).

| Method and path | Caller | Purpose | Status |
| --- | --- | --- | --- |
| `POST /api/v1/staff/sessions` | Anyone with credentials | Sign in; returns a short-lived bearer token (8 h; only its hash is stored). Same `401` for wrong password and unknown user; rate-limited per IP and per username before the password check. | Implemented and tested |
| `DELETE /api/v1/staff/sessions/current` | Official | Sign out (revokes the session server-side) | Implemented and tested |
| `GET /api/v1/staff/me` | Official | Signed-in identity | Implemented and tested |
| `GET /api/v1/staff/complaints` | Official | Newest-first list with paging (`limit` ≤ 100, `offset`). Each item: `id`, `title`, `category`, `area`, `status`, `verification`, `department`, `created`, `updated`, `mediaType`, `hasEvidence`, and `gps` (`lat`, `lng`, `accuracy`, `sourceType`) for the admin map. Two database queries per page, however long. | Implemented and tested |
| `GET /api/v1/staff/complaints/{id}` | Official | Operational projection: description, precise location, timeline, evidence metadata. Never claim code, hashes, upload tokens or storage keys. | Implemented and tested |
| `GET /api/v1/staff/complaints/{id}/evidence` | Official | Streams the original file (`no-store`, `nosniff`). Each view is written to an append-only access log before any byte is sent; no log, no bytes. Errors: `404 evidence_not_found` (nothing uploaded), `404 evidence_unavailable` (the record exists but this server's storage confirms the file is absent, or it is in a storage backend this server is not configured for; nothing is logged), `503 storage_unavailable` (storage unreachable; retry). | Implemented and tested |
| `GET /api/v1/staff/complaints/{id}/evidence/access` | Official | Who viewed the evidence and when | Implemented and tested |

Sessions end on expiry, sign-out, account disable or password change. Staff tokens and upload tokens are not interchangeable.

### 3.2 Implemented (Workstream 1): complaint management

Explicit action endpoints replace the proposed `PATCH /complaints/{id}`: one permission check and one audit action per endpoint, and no partial-update ambiguity. All writes are `POST` (the CORS allowlist is `GET, POST, DELETE`). Every staff mutation returns the updated staff view of the complaint (`StaffCase`: the staff projection plus `version`, `assignment`, `assignmentHistory`, `workOrders`, `notes` and `allowedActions`). Rules live in `backend/app/lifecycle.py`; `store.py` applies them inside one transaction that locks the complaint row and writes the state change, its public timeline or verification entry, and an `audit_log` record together.

Roles: `official` (complaints in their departments, their own assignments, and the unassigned triage queue), `supervisor` (all complaints), `admin`. Roles and department membership are set only with `python -m app.staff` and take effect on the next request without signing in again. An official outside a complaint's scope gets `403` on its detail, evidence, access log and every action, and it is left out of their list.

| Method and path | Caller | Purpose |
| --- | --- | --- |
| `POST /api/v1/staff/complaints/{id}/transitions` | per transition | `{to, expectedStatus, explanation}`. Allowed moves: Submitted → Under Review → Assigned (needs a department) → In Progress → Marked Resolved (needs an explanation and no open work orders); Assigned → Under Review and Marked Resolved → In Progress (supervisor, with explanation); In Progress → Assigned (with explanation). Anything else is `409 invalid_transition`; a stale `expectedStatus` is `409 conflict`. Marking a case resolved after a dispute or reinspection resets verification to Pending Verification. |
| `POST /api/v1/staff/complaints/{id}/assignment` | supervisor+ | `{department, assignee?, reason, expectedStatus}`. Assigns or reassigns (a reason is required when reassigning). Officials must be members of the department. Submitted or Under Review moves to Assigned. History is append-only. The public timeline names the department only. |
| `POST /api/v1/staff/complaints/{id}/notes` | official+ in scope | Internal note, kept in `staff_notes`, never in any public response. |
| `POST /api/v1/staff/complaints/{id}/updates` | official+ in scope | Public update (`kind`: `progress` or `department_response`) added to the public timeline. |
| `POST /api/v1/staff/complaints/{id}/reinspections` | supervisor+ | `{note, reopen, expectedStatus}`. Marked Resolved complaints only; verification becomes Reinspection Required, and `reopen` also moves the case back to In Progress. |
| `POST /api/v1/staff/complaints/{id}/work-orders` | official+ in scope | `{team, description, dueAt (YYYY-MM-DD, IST), assignee?}`. Complaint must be Assigned or In Progress. The complaint's original evidence is linked as the "before" reference. |
| `POST /api/v1/staff/work-orders/{woId}/transitions` | official+ in scope | `{to, expectedStatus, note}`: Open → In Progress (start; an Assigned complaint moves to In Progress), In Progress → Completed (note required), Open or In Progress → Cancelled (supervisor+, reason required). Completed and Cancelled are final. |
| `POST /api/v1/staff/work-orders/{woId}/evidence` | official+ in scope | After-repair JPEG (multipart), through the same streaming checks, size limit, SHA-256 and quota as citizen evidence. Stored as an `evidence` row with role `repair` and linked as "after". |
| `GET /api/v1/staff/work-orders/{woId}/evidence/{evidenceId}` | official+ in scope | Streams a before/after file; each view is written to the evidence access log first. |
| `GET /api/v1/staff/work-orders?status=&overdue=&department=` | official+ | Work orders the caller may see, newest first, with an `overdue` flag (active and past `dueAt`). |
| `GET /api/v1/staff/complaints/{id}/audit` | supervisor+ | Audit trail, newest first: actor, action, details, time. |
| `GET /api/v1/staff/departments` | official+ | Active departments. |
| `POST /api/v1/complaints/{id}/citizen-responses` | claim code holder | `{claim, response}` with `Appears Resolved`, `Still Present` or `Cannot Confirm`, under the rules in section 6. Rate-limited per IP and per complaint; the same `401` for an unknown ID or a wrong code. |

`GET /api/v1/complaints/{id}` (public) keeps its shape: the timeline now includes public updates and lifecycle explanations, and `verificationLog` is filled, always with generic actors ("Citizen", "Civic team"). It never contains internal notes, staff names, work-order details, completion notes, evidence IDs or coordinates (tested). Not built yet: repairs as a separate resource (after-repair photos belong to work orders), reviews, owner accounts and claiming.

### 2.3 Implemented: community list across Bengaluru

`GET /api/v1/complaints?category=&area=&status=&limit=&offset=` (anonymous, rate-limited like tracking) lists every complaint inside the Bengaluru service area (12.75–13.3° N, 77.35–77.9° E, the same box as `dist/exif.js`), newest first, `limit` ≤ 100. It returns the public-tracking allowlist without the timeline: `id, title, category, area, status, verification, department, created, updated`. No coordinates, description, evidence or claim data. Reports merged into another as duplicates are left out. `facets.categories` and `facets.areas` count every listed complaint by issue type and neighbourhood, ignoring the filters, and feed the filter menus on the resident Community issues page (server mode). Complaints outside the service area (for example test data) are not listed.

### 3.3 Implemented (Workstream 2): duplicate detection

Suggestions are explainable and never acted on automatically. Scoring lives in `backend/app/duplicates.py`, distances in `backend/app/geo.py` (WGS84, haversine, metres).

| Method and path | Caller | Purpose |
| --- | --- | --- |
| `GET /api/v1/staff/complaints/{id}/duplicates?radius=&days=` | official+ in scope | Up to 10 candidates within `radius` metres (default 250, max 1000) and ±`days` (default 90, max 365), same or related category, not merged away, not dismissed as a pair, and within the caller's scope. Each has `score` (0–1), `label` (`likely_duplicate` ≥ 0.75, `possible_duplicate` ≥ 0.5, `recurrence`, `nearby_separate`), `distanceM`, `factors` (location against combined GPS accuracy 0.45, category 0.20, time 0.20, description 0.15, each with its contribution), `reasons` and a `caution` for locations worse than ±100 m (score capped at 0.6). An identical evidence file lifts the score to at least 0.97. Reports too far apart for their GPS accuracy are always `nearby_separate`; a nearby report that was already marked resolved is a `recurrence`. No description, coordinates or evidence are returned. |
| `POST /api/v1/staff/complaints/{id}/merge` | supervisor+ | `{canonical, reason}`: merges `{id}` (the duplicate) into `canonical`. Refused: self-merge (422), an already merged duplicate, a canonical that is itself merged (no chains), and a duplicate that others were merged into (409). Both rows are locked in a fixed order. The score at merge time is stored. Both complaints keep their records, evidence and history; the duplicate gets `mergedInto` and refuses lifecycle actions, assignment, work orders and reinspection (409 `merged`); notes are still allowed. |
| `POST /api/v1/staff/merges/{mergeId}/revert` | supervisor+ | `{reason}`. The merge record stays, marked reverted; the duplicate is independent again. |
| `POST /api/v1/staff/complaints/{id}/duplicates/{otherId}/dismiss` | supervisor+ | `{reason}`: the pair is no longer suggested, in either direction. |

`GET /api/v1/complaints/{id}` (public) gains `mergedInto` (a public ID or null), and both complaints' public timelines mention the link. Merge reasons, scores and staff names stay internal. Image similarity beyond identical files is not implemented (it would need a new imaging dependency and server-side decoding of private evidence; video has no equivalent).

## 4. Map queries

| Method and path | Caller | Purpose |
| --- | --- | --- |
| `GET /api/v1/map/points?bbox=&category=&status=&resolution=&verification=&area=` | Role-dependent | Points inside the viewport. Precise coordinates are returned only where the projection allows; otherwise the record is counted but not plotted, and the UI shows "Location private". |
| `GET /api/v1/map/clusters?zoom=&bbox=` | Role-dependent | Server-side clustering for large volumes, with neutral cluster counts. |

The list and the map must use the same filter parameters. The prototype enforces this with one `setVisible(ids)` path.

## 5. Evidence media

### 5.1 Implemented (Phase 3.4): original evidence upload

| Method and path | Caller | Purpose | Status |
| --- | --- | --- | --- |
| `POST /api/v1/complaints/{publicId}/evidence` | Holder of a valid upload token for that complaint | Upload the complaint's original photo or video as `multipart/form-data` (`file`, optional `capturedAt`). Stored privately; metadata in the database. | Implemented and tested |
| `POST /api/v1/complaints/{publicId}/upload-token` | Holder of the claim code | Exchange the claim code (JSON body) for a fresh short-lived upload token; replaces the previous token. | Implemented and tested |

Authorisation, without user accounts: the creation receipt includes a random upload token (256 bits, valid 1 hour by default; only its SHA-256 hash is stored). The token goes in `Authorization: Bearer …`, never in the URL. When it expires, the claim code obtains a new one. Knowing the public ID alone is never enough. This proves possession of the creation receipt or claim code, not identity, and it is not rate-limited yet.

Rules enforced by the server:

- Types are decided from the file's leading bytes: JPEG (photo), WebM or MP4 (video). The declared MIME type and filename extension must agree with the signature or the upload is rejected. The file must match the complaint's `mediaType`.
- Limits (configurable): photo 25 MB, video 200 MB by default; deployments set `CIVICLENS_MAX_VIDEO_BYTES=52428800` (50 MB, see `backend/.env.example`), which the frontend's citizen capture limit matches. Checked against `Content-Length` before reading and again while streaming. A stalled upload (30 s without data) is stopped. A total storage quota (default 2 GB) is enforced with reservations taken before the body is read; exceeding it returns `507 storage_quota_exceeded` and nothing is deleted to make room.
- The file is streamed in chunks to a temporary file, hashed (SHA-256) on the way, then moved into place atomically. Failed or interrupted uploads leave no file and no database row.
- One original per complaint. Re-sending the same file returns the existing record (`200`, `already_uploaded`); a different file returns `409 evidence_exists`.
- The response carries an opaque `evidenceId`, type, size, checksum and times. Never a storage key, path or URL.
- Public tracking never includes evidence. Since Phase 3.6, stored files are served only to signed-in staff through `GET /api/v1/staff/complaints/{id}/evidence` (section 3.1), with every view logged.
- Stamped photos (version 5.2) are a client-side convenience copy and are never uploaded: the evidence sent to this endpoint is always the untouched original. The stamp's evidence reference (`EV-XXXX-XXXX`) is the first 8 hex digits of the original file's SHA-256, so it matches the start of the `sha256` this endpoint returns. It is not a secret and is not sent in `POST /api/v1/complaints`; the claim code is never printed on a photo or used in a file name.

### 5.2 Still proposed

| Method and path | Caller | Purpose |
| --- | --- | --- |
| `POST /api/v1/complaints/{id}/repairs` media | Official (repair) | Repair photos as a separate evidence role, after staff authentication exists. |
| `GET /api/v1/media/{mediaId}/url` | Owner, community (when shared) | Authenticated streaming for owners after resident accounts exist (as for staff, including with Supabase Storage). Supabase signed URLs are deliberately not used: they are bearer links that bypass the staff session and the view-level audit log. Never a permanent public link. Staff streaming is implemented (section 3.1). |
| Server-side EXIF re-parsing | — | Re-read GPS and capture time from uploaded JPEGs and compare with the submitted metadata. |

Video uploads: no location inference from the container. The API returns `geoTagged: false` unless a validated metadata source is added later.

## 6. Verification and citizen responses (server rules)

Lifecycle and verification are independent fields with separate append-only logs.

- `Appears Resolved` moves verification to Citizen Confirmed only when the lifecycle is Marked Resolved.
- `Still Present` moves verification to Disputed and increments `recurrence` only when the lifecycle is Marked Resolved. In other states it is logged without changing verification.
- `Cannot Confirm` is logged and changes nothing.
- No review, response or citizen action ever changes the lifecycle status.

## 7. Video review (admin-only)

| Method and path | Purpose |
| --- | --- |
| `POST /api/v1/video-reviews` | Create a private review record (metadata only) |
| `POST /api/v1/video-reviews/{id}/observations` | Add a timestamped manual observation (Suspected dumping, Waste present, No dumping observed, Inconclusive) with optional frame |
| `PATCH /api/v1/video-reviews/{id}` | Link to a complaint, set a manual finding and summary. Linking never publishes footage or changes case status |
| `GET /api/v1/video-reviews/{id}/export` | JSON export labelled "Manual administrator review. Not AI detection." |

## 8. Intelligence (proposed)

| Method and path | Job | Status |
| --- | --- | --- |
| `GET /api/v1/priority/{complaintId}` | Normalised priority score with visible contributions (severity, exposure, unresolved time, recurrence) and separate evidence confidence | Implemented in the prototype; inputs illustrative |
| `GET /api/v1/hotspots/outlook` | 30-day ward/category activity versus historical baseline | Proposed; not connected or trained |
| `POST /api/v1/vision/road-damage` | Road-damage detection on images (YOLO11n proposed) | Proposed; not connected or trained |
| `POST /api/v1/incidents/link-suggestions` | Suggested duplicates for human review; never auto-merges | Proposed; not connected |
| `POST /api/v1/scenarios/run` | Seeded Monte Carlo over labelled assumptions | Illustrative demo; exploratory |

Techniques are not causal claims. Every response must carry its method, inputs, version and an uncertainty note, and no response may state a detection or confidence without a model behind it.

## 9. Notifications

| Method and path | Purpose | Status |
| --- | --- | --- |
| `GET /api/v1/notifications` | Notification history for the principal (staff session or citizen inbox token), newest first, with unread count | **Implemented** (9.1) |
| `PUT /api/v1/notification-preferences` | Per-user preferences (which events to receive) | Proposed |
| Push delivery | Web Push with VAPID to every registered device (FCM, Mozilla, Windows and Apple push services) | **Implemented** (9.1) |

### 9.1 Implemented: Web Push notifications

Request and response shapes are in `backend/README.md` → "Web Push notifications". Summary:

- Principals: a staff session, or a citizen **notification inbox** (`POST /notifications/inboxes`, a device-held bearer token stored hashed). Citizens follow a complaint with its claim code (`POST /notifications/follows`); staff receive staff events automatically.
- Events: citizens (followers) — follow confirmed, evidence received, evidence first opened by staff; staff — new complaint, new evidence. Each event at most once per recipient. Case-status notifications wait for server-side staff case updates (section 3).
- History is stored before any push (`GET /notifications`, read/unread per notification, `POST /notifications/read-all`, `GET /notifications/unread-count`).
- Devices: `POST/GET/DELETE /notifications/subscriptions`, up to 10 per principal, idempotent per browser; endpoints restricted to known push services; expired or repeatedly failing devices are deleted.
- `GET /notifications/config` gives the VAPID public key; `POST /notifications/test` sends a test to the caller's devices.
- Payloads carry the notification ID, title, short body (public ID and category only) and an in-app link (`#/track/<id>` or `#/staff/<id>`); never descriptions, locations, claim codes or tokens.

## 10. Open questions before implementation

- Authentication provider and session model (cookie or token) for citizens and officials.
- Municipal identity source for staff roles.
- Whether shared evidence needs moderation before it appears in the community projection.
- Retention periods for original media, video footage and audit logs.
- Photon or self-hosted geocoder terms for coordinate disclosure.
