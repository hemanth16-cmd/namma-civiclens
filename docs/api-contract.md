# Namma CivicLens — Proposed API contract

**Status: Proposed; not implemented.** This document describes the HTTP API a FastAPI backend would expose so that the browser prototype's rules move to the server without changing the screens. Today the same operations run in `dist/services.js` against `localStorage` and IndexedDB, and the functions are named to match these endpoints. Nothing below is running. Authentication, rate limits and storage are not built.

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
| `GET /api/v1/complaints?scope=mine` | Signed-in owner | Owner's cases with filters | `api.list(viewer)` |
| `GET /api/v1/complaints?scope=community` | Signed-in user | Community projection with filters | `api.list(viewer)` with community scope |
| `GET /api/v1/complaints/{id}` | Role-dependent | Detail, projected by principal | `api.detail(id, viewer)` |
| `GET /api/v1/complaints/{id}/receipt` | Submitter only | Receipt, including claim-code presence and evidence copy metadata | `api.receipt(id)` |
| `POST /api/v1/complaints/claim` | Signed-in user | Claim by public ID and private code. Rate-limited; generic error on mismatch | `api.claim()` |
| `POST /api/v1/complaints/{id}/share` | Owner | Set `shareEvidence` for original evidence and location | `api.shareOriginal()` |
| `PUT /api/v1/complaints/{id}/follow` | Signed-in user | Per-account follow state | `api.toggleFollow()` |

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

## 4. Map queries

| Method and path | Caller | Purpose |
| --- | --- | --- |
| `GET /api/v1/map/points?bbox=&category=&status=&resolution=&verification=&area=` | Role-dependent | Points inside the viewport. Precise coordinates are returned only where the projection allows; otherwise the record is counted but not plotted, and the UI shows "Location private". |
| `GET /api/v1/map/clusters?zoom=&bbox=` | Role-dependent | Server-side clustering for large volumes, with neutral cluster counts. |

The list and the map must use the same filter parameters. The prototype enforces this with one `setVisible(ids)` path.

## 5. Evidence media

| Method and path | Caller | Purpose |
| --- | --- | --- |
| `POST /api/v1/complaints/{id}/media` | Owner or official (repair) | Upload original or repair JPEG/MP4/WebM. Server re-parses EXIF, scans content type and size, and stores the object privately. |
| `GET /api/v1/media/{mediaId}/url` | Principal allowed by projection | Returns a short-lived signed URL. Never a permanent public link. |

Video uploads (admin-only): maximum 200 MB, no location inference from the container. The API returns `geoTagged: false` unless a validated metadata source is added later.

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
| `GET /api/v1/notifications` | Notifications for the principal, filtered by per-user preferences | Proposed |
| `PUT /api/v1/notification-preferences` | Per-user preferences | Proposed |
| Push delivery (FCM) | Device alerts | Proposed; not configured |

## 10. Open questions before implementation

- Authentication provider and session model (cookie or token) for citizens and officials.
- Municipal identity source for staff roles.
- Whether shared evidence needs moderation before it appears in the community projection.
- Retention periods for original media, video footage and audit logs.
- Photon or self-hosted geocoder terms for coordinate disclosure.
