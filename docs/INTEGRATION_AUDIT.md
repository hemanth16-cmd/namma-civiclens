# Namma CivicLens — End-to-end integration, role and data-flow audit

Branch `integration/v52-fullstack` at `dca458e` (local branch tracking the remote, created in worktree
`elastic-kepler-6574a1`; working tree clean; nothing committed or pushed). Audit date 2026-10-30.
**Status update (same day): remediation steps 1–4 were approved and are implemented, uncommitted, on
this branch. See "H. Remediation status" at the end.**

Evidence for every claim below comes from reading the source, read-only probes of the live API
(`https://namma-civiclens.onrender.com/api/v1`) and the live Supabase database and bucket, and test
runs on local throwaway infrastructure. No production data was created, changed or deleted.

---

## A. Architecture map

```
Browser (Vercel, static dist/)                     Render (FastAPI, backend/app)                 Supabase
───────────────────────────────                    ──────────────────────────────               ─────────
api-config.json  mode=backend → Render URL
                                                                                                 
SERVER MODE (real data)                                                                          
  capture.js / report-gateway.js ──► api-client.js ──► POST /complaints ............ complaints.py ─► store.py ─► PostgreSQL
                                                    ──► POST /complaints/{id}/evidence  evidence.py ─► EvidenceStorage ─► private bucket
  public.js renderServerTrack ─────► api-client.js ──► GET /complaints/{id} ......... complaints.py ─► store.py
  staff.js (#/staff) ──────────────► api-client.js ──► /staff/* ..................... staff_api.py ─► store.py, storage
  notifications.js / push-client ──► api-client.js ──► /notifications/* ............. notifications_api.py
                                                                                     auth.py: staff bearer session (one role)
                                                                                     claims.py: claim code / upload token
LOCAL DEMO MODE (browser storage, seeded sample data) — still used in backend mode by:
  #/official/* admin workspace, #/resident/* resident workspace, #/issue/*, #/login, map, intelligence
  → services.js `api` (localStorage + IndexedDB, SAMPLE_ROWS)
```

Roles actually implemented:

| Layer | Roles |
|---|---|
| Backend | **anonymous**; **claim-code holder** (citizen proves ownership with the one-time claim code, which yields an upload token or follows); **citizen inbox token** (notifications only); **staff `official`** (`staff_accounts.role CHECK (role IN ('official'))`), the only staff role |
| Frontend local demo | `public`, `resident`, `official` (fake accounts with a shared demo password; no server meaning) |

There is **no backend "admin" or "staff vs admin" distinction, and no citizen account**. Supervisor/admin
roles, departments and scoping exist only in the uncommitted Workstream 1 work on
`feature/core-platform-development`.

---

## B. Frontend → backend mapping

| Frontend page / action | API function | Backend endpoint | Database / storage | Status |
|---|---|---|---|---|
| Capture → submit report | `createComplaint` (+ Idempotency-Key) | `POST /complaints` | `complaints`, `complaint_timeline`, `idempotency_keys` | ✅ Works (e2e) |
| Upload photo / video | `uploadEvidence` (Bearer upload token, multipart) | `POST /complaints/{id}/evidence` | `evidence` row + private object (Supabase bucket or local disk) | ✅ Works (e2e; 8/8 live rows match bucket objects) |
| Retry upload after token expiry | `requestUploadToken` (claim in body) | `POST /complaints/{id}/upload-token` | `complaints.upload_token_hash` | ✅ Works (e2e) |
| Receipt | (from create response) | — | IndexedDB copy `server-evidence:<id>` | ✅ Works |
| Public tracking | `getPublicComplaint` | `GET /complaints/{id}` | `complaints`, `complaint_timeline`, `verification_log` | ✅ Works (live probe: no claim, GPS, description or evidence leaked) |
| Citizen confirm / dispute resolution | — | **none** | — | ❌ Not implemented server-side (local demo only, `services.js citizenResponse`) |
| Resident "My reports" dashboard | `services.js api.list` (local) | **none** | localStorage only | ❌ Server reports never appear (e2e: "No local complaint record was created for the server report") |
| Resident claim complaint | `services.js api.claim` (local) | **none** (`POST /complaints/claim` proposed only) | localStorage | ❌ Local only |
| Admin workspace overview / complaints / work orders / map | `services.js api.*` (local) | **none** | localStorage + seeded `SAMPLE_ROWS` | ❌ Shows demo sample data, not server complaints |
| Admin status change, assignment, work order, reinspection, repair upload, notes | `services.js updateCase/addWorkOrder/requestReinspection/addRepair` (local) | **none** on this branch | localStorage | ❌ Not implemented server-side (Workstream 1, other branch) |
| Staff console sign-in / sign-out | `staffSignIn` / `staffSignOut` | `POST /staff/sessions`, `DELETE /staff/sessions/current` | `staff_sessions` | ✅ Works (e2e) |
| Staff complaint list (paged) | `listStaffComplaints` | `GET /staff/complaints?limit&offset` | `complaints` + evidence | ✅ Works; ⚠ no filters/search/sort |
| Staff complaint detail | `getStaffComplaint` | `GET /staff/complaints/{id}` | complaint + timeline + original evidence | ✅ Works |
| Staff view photo / video | `fetchStaffEvidence` | `GET /staff/complaints/{id}/evidence` | `evidence.storage_backend` → matching storage → streamed | ✅ Works (photo e2e; video played in Chromium); ⚠ see D |
| Evidence access log | `getEvidenceAccessLog` | `GET /staff/complaints/{id}/evidence/access` | `evidence_access_log` | ✅ Works |
| Staff "who am I" | — (unused) | `GET /staff/me` | `staff_sessions` | ⚠ Unused endpoint |
| Notifications (inbox, read, devices, follows, test) | 12 functions in `api-client.js` | `/notifications/*` | notification tables | ✅ Works (e2e earlier today); ⚠ push off on Render (`pushEnabled:false`) |

Every function in `dist/api-client.js` maps to an existing route with the correct method, auth header and
field names (checked earlier today and unchanged on this branch). The integration failures are not
wrong URLs. They are **screens that never call the API at all** in backend mode.

---

## C. Role-permission matrix (as implemented on this branch)

| Operation | Anonymous | Claim-code holder | Inbox token | Staff `official` | Frontend demo `official` |
|---|---|---|---|---|---|
| Create complaint | ✅ (rate-limited) | — | — | — | (local only) |
| Track complaint (public projection) | ✅ | ✅ | ✅ | ✅ | (local) |
| Upload evidence | ❌ | ✅ via upload token | ❌ | ❌ (no repair upload) | (local) |
| List complaints | ❌ 401 | ❌ | ❌ | ✅ all complaints | sample data |
| Complaint details (operational) | ❌ 401 | ❌ | ❌ | ✅ all complaints | sample data |
| View evidence | ❌ 401 | ❌ (own receipt copy is in the browser) | ❌ | ✅ any complaint, logged | sample data |
| Assign department / staff | — | — | — | ❌ not implemented | local demo |
| Status change | — | — | — | ❌ not implemented | local demo |
| Work orders | — | — | — | ❌ not implemented | local demo |
| Internal notes / public updates | — | — | — | ❌ not implemented | local demo |
| Citizen verification | — | ❌ not implemented | — | — | local demo |
| Audit history | — | — | — | Evidence access log only | — |
| User management | — | — | — | CLI only (`python -m app.staff`) | — |
| Follow / notifications | — | ✅ follow with claim | ✅ | ✅ staff inbox | — |

Backend enforcement verified live: `GET /staff/complaints`, `/staff/complaints/{id}`,
`/staff/complaints/{id}/evidence`, `/staff/me` and `/notifications` all return **401** without a
session, and an invalid bearer token returns 401 locally. **There is no scoping**: every official can
read every complaint and its evidence. That is acceptable only while there is a single role.

---

## D. Evidence retrieval audit (administrator / staff)

1. **Storage reference.** `evidence.storage_key` (32 hex) + `evidence.storage_backend`
   (`local`/`supabase`). The Supabase object name is `evidence/<first 2 hex>/<key>` in the private
   bucket. Keys are never sent to the browser (`StaffEvidenceSummary` allow-list).
2. **Authorisation.** `Depends(current_staff)`: bearer token → SHA-256 → `staff_sessions` (not
   expired, not revoked, account not disabled).
3. **Endpoint.** `GET /api/v1/staff/complaints/{id}/evidence`. It looks up the original evidence and
   picks the storage that wrote it (`app.state.evidence_storages[storage_backend]`). It **writes the
   access-log row before sending any byte**, then streams.
4. **Streaming.** `StreamingResponse` in 64 KB chunks, from the local file or from Supabase through the
   service-role client (`_SupabaseReader`, server side; the bucket is never exposed). Headers:
   `Content-Type`, `Content-Length`, `Cache-Control: no-store, private`, `nosniff`, sandbox CSP.
5. **Frontend.** `fetchStaffEvidence` checks the MIME type is in the allow-list, reads the **whole body
   with `arrayBuffer()`**, builds a Blob and object URL, and renders `<img>` or `<video controls>`. The
   URL is revoked when leaving the page.

Verified: photo e2e passed (1920 px rendered); a WebM video loaded (`readyState` 4) and played in
Chromium; anonymous and invalid-token requests got 401; live bucket 8/8 objects present with matching
sizes, 0 orphans.

Incomplete or broken:
- **No HTTP Range support** (`Range: bytes=0-99` → `200`, full body, no `Accept-Ranges`). It works today
  only because the client downloads the whole file into memory first (up to the video limit) before
  playback starts. Adding Range naively would also write **one access-log row per range request**:
  two requests already produced two rows in the test.
- **Only one attachment per complaint** (`evidence.role = 'original'`, unique). No multiple attachments,
  no before/after repair evidence on this branch.
- **2 live evidence rows are `storage_backend = 'local'`** (`NCL-2026-LWSSVP`, `NCL-2026-J66Y2Q`, 825-byte
  test JPEGs uploaded from a developer machine). Render cannot serve them. If local storage is not
  configured there, the endpoint raises `StoreError` → **503 "could not read"**, which looks like an
  outage rather than "this file is on another machine".
- **Every view triggers a "staff opened the evidence" notification path** (deduplicated per complaint by
  the notifier). That is fine, but note it for Ruchika's notification work.
- **Browser-recorded WebM has no duration metadata** (`duration` reported as unknown), so the seek bar
  is limited.

---

## E. Integration issue register

| # | Sev. | Area / file | Current behaviour | Expected | Root cause | Recommended fix | Owner / dependency |
|---|---|---|---|---|---|---|---|
| 1 | **Critical** | `dist/views/workspace.js`, `issue.js`, `map.js` via `services.js api` | In backend mode, the admin workspace (`#/official/*`) shows **seeded demo complaints**; real server complaints are invisible there | The admin dashboard lists server complaints | Workspace screens were never wired to `api-client.js`; only `#/staff` is | Short term: in backend mode, route `#/official/*` to the staff console, or show a clear "server data lives in Staff console" state, and never render `SAMPLE_ROWS`. Long term: back the workspace with the staff API | Us (this patch, frontend only) |
| 2 | **Critical** | Server API | No server-side status change, assignment, work orders, notes, public updates, reinspection, repair evidence or citizen confirm/dispute. Steps 14–22 of the target scenario cannot run | Full management flow | Not built on this branch | **Do not build here.** Delivered by Workstream 1 (`feature/core-platform-development`, in progress); then wire the staff console to it | Us, Workstream 1 branch |
| 3 | **High** | `dist/views/workspace.js` (resident) | Citizen's server reports never appear in "My reports"; tracking needs the ID typed in | Citizen sees their own server complaints | No citizen accounts; the list is local-only | Keep a local index of submitted server IDs (already in IndexedDB via `server-evidence:` keys) and show them from `GET /complaints/{id}`; no claim code stored | Us (frontend only) |
| 4 | **High** | `dist/api-config.json` + `tests/api-integration.test.js:98` + `scripts/backend-e2e.mjs` local-mode checks | Shipped config points at production Render. Unit test fails; 4 e2e checks fail; `npm run dev` on localhost talks to production and is CORS-blocked | Dev/test default stays local; production URL set at deploy time | Production URL committed to source | Deploy-time config (a Vercel build step writing `api-config.json`, or a separate deployed file) and restore the local default in the repo; or update the tests to accept the deployed file. **Team decision** | Team / deploy owner |
| 5 | **High** | Render `CIVICLENS_CORS_ORIGINS` | Only the integration-branch preview origin is allowed; `namma-civiclens.vercel.app` and `localhost:4173` are refused | Production frontend origin allowed | Env set for one preview URL; Vercel previews change per branch | Add the production domain (and any stable alias) to Render's env | Deploy owner (config only) |
| 6 | **High** | `dist/services.js:17` vs backend limit | Frontend allows 200 MB citizen videos; backend deployments use 50 MB (Supabase free plan). Large clips upload for minutes, then fail with 413 | Browser refuses > 50 MB up front | Fix exists only on `integration/fullstack-reconciliation` (`2a86d9f`), not on this branch | Cherry-pick `2a86d9f` (plus the `.env.example` limits commit) | Us |
| 7 | Medium | `backend/app/staff_api.py` `view_evidence`; `dist/api-client.js fetchStaffEvidence` | No Range; whole video buffered in browser memory before playback | Range-capable streaming for video, with one access-log entry per viewing | Endpoint streams the full body only | Add a short-lived, staff-bound evidence view grant: log once, then serve Range requests. Needs Range reads from `EvidenceStorage` (the Supabase reader supports HTTP Range) | Us, plus **coordination with the S3 owner** (storage interface change) |
| 8 | Medium | Live data | 2 evidence rows `local` (developer disk); 2 complaints with no evidence | Evidence readable by the deployed API | Test uploads from a laptop backend against production DB | Show "stored on another server" instead of 503 (`staff_api.py`); decide whether to keep these test rows (no deletion done) | Us (message); data cleanup needs owner approval |
| 9 | Medium | `staff.js renderList`; `GET /staff/complaints` | Paging only; no filter/search by status, category, area or date | Filters and search | Not built | Add query params server-side and in the console (no schema change) | Us (WS1/WS3 overlap: do after WS1) |
| 10 | Medium | Discoverability | `#/staff` is linked only from a notice on the demo admin login | Clear entry point to the real console | UI | Add a "Staff console" link in backend mode (topbar or entry page) | Us (frontend) |
| 11 | Medium | Production | Swagger UI/OpenAPI public (`/api/v1/docs` → 200); junk complaint `area='string'` exists | Docs off in production | Default on | Env flag to disable docs in production (WS4 quick win) | Us |
| 12 | Low | Render | `pushEnabled:false` (no VAPID keys) | Push on if intended | Env not set | Set `VAPID_*` on Render if push is wanted | Ruchika (notifications) |
| 13 | Low | `GET /staff/me` | Unused by frontend | — | — | Use it to re-validate the session on console load, or leave | Us |
| 14 | Low | `docs/api-contract.md` §2–§4 | Lists many endpoints as if planned in this shape (`PATCH /complaints/{id}`, `scope=mine`, map queries) | Docs match reality | Proposal document | Mark implemented vs proposed; update with WS1's actual endpoints | Us |
| 15 | Low | Live data | One 825-byte test JPEG hash appears in 3 evidence rows | — | Test uploads | Informational; WS2 duplicate detection will surface such cases | — |

Not found (good): no wrong endpoint URLs or methods, no secrets in `dist/`, no public bucket or public
evidence URLs, no orphaned bucket objects, no evidence row without a matching object for the Supabase
backend, no complaint without a timeline, and no silent fallback from backend mode to local data on
failure (e2e "Server unreachable: tracking shows an error and no local record").

---

## F. End-to-end test report

Executed on this branch (`dca458e`), Windows 11, Node 25.8.1, Python 3.14.3:

| Test | Result |
|---|---|
| `npm test` | **144 passed, 1 failed** (`api-integration.test.js:98`, shipped config is backend mode; issue 4) |
| `npm run check` | passed (52 files) |
| Backend pytest (SQLite) | **312 passed, 18 skipped** (PostgreSQL-only tests) |
| `scripts/backend-e2e.mjs`: Chromium + local API on **PostgreSQL 15** (throwaway container) + local evidence storage + test staff account | **62 / 66 passed**. All server-mode checks passed: create (idempotent), photo upload, **video upload**, private storage with checksum match, tracking without leaks, upload-failure and retry paths, lost-response recovery, staff sign-in, list, detail, photo view through the authenticated endpoint, access log, session revocation. **4 failed**, all in the "local mode" context that reads the shipped config (issue 4). The browser was CORS-blocked from production, so nothing was written there |
| Staff video playback (Playwright, same stack) | **passed**: blob loaded (`readyState` 4) and playback advanced |
| HTTP Range on evidence | **not supported** (200, full body) |
| Unauthenticated staff/evidence/notification endpoints (live Render) | **401** for all five probed |
| Invalid bearer token on evidence (local) | 401 |
| CORS (live) | integration preview allowed; production alias, localhost and a foreign origin refused |
| Public projection (live, existing complaint) | No claim, GPS, description or evidence fields |
| Database ↔ bucket consistency (live, read-only) | 8/8 Supabase evidence rows have objects with matching size; 0 orphans; 2 `local` rows; 2 complaints without evidence |

Not executed, with reasons:
- Target scenario steps 14–22 (assignment, staff work, repair upload, resolution, citizen
  confirm/dispute, management audit): the features do not exist on this branch (issue 2).
- A full journey against the **live** Render + Supabase stack: it would create records in production;
  no approved test project or test credentials exist.
- Real-device camera, GPS and codec checks (Chromium uses a synthetic camera).
- PostgreSQL-only backend unit tests in this run (they passed earlier today on the identical backend
  code: 324 passed, 6 skipped).

---

## G. Remediation plan (ordered)

**This branch (`integration/v52-fullstack`), small frontend/config patches. Each needs your approval
before I commit:**
1. Cherry-pick the 50 MB limit fix (`24caa4b`, `2a86d9f` from `integration/fullstack-reconciliation`), issue 6.
2. Backend mode never shows demo data: route `#/official/*` and `#/issue/*` to the staff console (or a
   clear notice), add a visible "Staff console" link, and make the resident dashboard explain where
   server reports are. Issues 1, 10.
3. Resident "My reports" from the locally remembered server IDs via public tracking (no claim code
   kept). Issue 3.
4. Controlled message for evidence stored on another backend instead of 503. Issue 8 (small backend
   change, no contract change).
5. Resolve the config/test conflict (issue 4) the way the team chooses.

**Workstream 1 branch (`feature/core-platform-development`), then wired into the staff console:**
6. Status transitions, assignment, work orders, notes, public updates, reinspection, repair evidence,
   citizen confirm/dispute, audit API; RBAC (official/supervisor/admin, department scoping). Issue 2.
7. Staff list filters and search (issue 9) and an evidence Range/view-grant design (issue 7).

**Coordination needed:**
- **S3 developer:** Range reads in the `EvidenceStorage` interface (issue 7); widening
  `evidence.role` / `storage_backend` together (WS1 migration 7).
- **Deploy owner:** Render CORS origins (5), deploy-time `api-config.json` (4), docs off (11).
- **Ruchika:** push keys on Render (12); notification events from WS1 status changes.
- **Telegram developer:** must call WS1's lifecycle service rather than write SQL.

Out of scope and untouched: Priority Engine, Knowledge Graph, FCM, Monte Carlo, predictive analytics,
notification infrastructure, S3 adapter, Telegram.

---

## H. Remediation status (steps 1–4, uncommitted on `integration/v52-fullstack`)

| Step | Change | Files | Verified by |
|---|---|---|---|
| 1 | Citizen video limit 50 MB (matches backend); admin video review keeps 200 MB (local only); `.env.example` limits. Applied from `24caa4b` and `2a86d9f` with `cherry-pick -n` | `dist/services.js`, `dist/video-service.js`, `dist/views/capture.js`, `dist/api-client.js`, `backend/.env.example`, `docs/api-contract.md`, `tests/services.test.js` | unit test (limit + rejection) |
| 2 | Backend mode never shows demo data: `#/official/*` → `#/staff` (except local-only video review); `#/resident/*` → `#/my-reports` (notifications → `#/notifications`); `#/issue/<id>` → `#/track/<id>`. The entry page offers "My reports" and "Staff console" instead of the demo logins, and no demo sample ID; the login pages point to the real screens | `dist/router.js` (`serverModeRoute`), `dist/app.js`, `dist/views/public.js` | 6 unit tests; 21 browser checks (local API on PostgreSQL) |
| 3 | "My reports" (`#/my-reports`): this browser remembers the IDs it submitted in backend mode (ID, category, time only; never the claim code, token, location or description) and shows each one's public status from the server; unknown IDs and lookup errors are explained; "Remove from this list" affects this device only; link from the server receipt | `dist/report-gateway.js`, `dist/views/my-reports.js`, `dist/views/capture.js` | unit tests (stored fields, failed submission not remembered, bounds, blocked storage); browser checks incl. phone width |
| 4 | Evidence the server cannot serve now returns `404 evidence_unavailable` with an explanation instead of `503 try again`: when storage confirms the file is absent, or the row names a backend this server is not configured for. A real outage (storage cannot answer) stays `503`. Nothing is logged in either case | `backend/app/staff_api.py`, `backend/tests/test_staff.py`, `backend/tests/test_supabase_storage.py` | 5 backend tests (2 updated from the old 503 expectation, 3 new incl. Supabase outage); browser check of the console message |

Test results after the changes: `npm test` 151/152 (the one failure is the existing `api-config` test,
issue 4, not in scope), `npm run check` passed (54 files), backend pytest SQLite **315 passed, 18
skipped**, PostgreSQL **327 passed, 6 skipped**, `backend-e2e` 62/66 (the same 4 config-related
failures as before), targeted browser checks **21/21**.

Still open: issue 4 (config/test, team decision), 5 (Render CORS), 7 (Range), 9–14, and everything in
issue 2 (Workstream 1).

## I. Follow-up: staff list timeout and the admin workspace (uncommitted)

Reported after deployment: the staff console showed "The CivicLens server did not answer in time", and
the admin map and dashboards had disappeared.

| Problem | Root cause | Fix | Verified by |
|---|---|---|---|
| Staff list timed out on Render | `store.list_complaints` loaded each complaint separately (~4 queries per complaint). Measured on the live API: each query from Render to the Supabase database costs ~0.2 s, so 12+ complaints took ~10 s, the browser's timeout | One query for the page plus one count, whatever the page size. The list now also carries `title`, `department` and `gps` for the map | backend test asserting exactly 2 queries for 6 complaints; SQLite 316 passed, PostgreSQL 328 passed |
| Map and dashboards gone | Remediation step 2 redirected `#/official/*` to the plain staff console. Before that, in backend mode, those screens showed **demo sample data**, so neither version was right | The admin workspace is back with real data. In server mode "Login as admin" is the server staff sign-in; overview, incident management, city map and intelligence read `GET /staff/complaints` (`dist/server-records.js`); opening a case leads to the staff case page with the evidence viewer; the activity chart is measured from server dates; work orders show an honest "not on the server yet" state with no local form; sign-out revokes the server session | 24 browser checks (sign-in, overview counts, mini-map, city map with every complaint plotted, map filters, incident search, work orders, case with photo, sign-out, phone width); screenshots compared with the original design |
| Map showed "Report symbols could not be drawn: Source "reports" already exists" | **Pre-existing**, also in local mode on the original branch: the layer setup runs on both `load` and `style.load` and tried to re-add its source | Update the existing source and add only missing layers | reproduced 3/3 in local mode before the fix, 0 after; admin map check |

Test results after these changes: `npm test` 155/156 (only the known `api-config` test), `npm run
check` passed (55 files), backend SQLite 316 passed / 18 skipped, PostgreSQL 328 passed / 6 skipped,
`backend-e2e` 62/66 (the same 4 config-related failures; its admin sign-in check was updated to the new
behaviour), server-mode checks 21/21, admin workspace checks 24/24.

The live Render service must be redeployed with this backend change for the timeout to go away.
Moving the Render service to the Singapore region (closest to the Seoul database) would cut each query
further.
