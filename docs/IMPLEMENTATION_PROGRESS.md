# Namma CivicLens — Implementation Progress

Persistent handoff document across Claude Code sessions. Update it at the end of every session and whenever meaningful progress (or a failure) occurs.

Labels: **[verified]** = observed directly in this workspace with the evidence noted; **[unverified]** = claimed by docs or assumed, not yet checked; **[proposed]** = designed or documented only, not built.

Last updated: 2026-10-30 (Phase 4: Remote `develop` integration, deep stress testing, AppSec review, sustained 5-minute endurance validation, and SQLite-to-PostgreSQL migration verification on branch `develop` at `0239786`).

## 1. Project objective and current architecture

Camera-first civic reporting and resolution platform for Bengaluru — Alliance One / SHASTRA 2026, CodeSangram 3.0, problem CS/26-0012 (Digital Citizen Services).

Current architecture **[verified by inspection]**:

- Frontend-only browser prototype in `dist/`: native JavaScript ES modules, no build step, no framework.
- `package.json` declares no dependencies; `package-lock.json` contains only the root package.
- Data layer: `localStorage` (`ncl-records-v2`) for records, IndexedDB (`ncl-evidence`) for media. Everything stays in one browser.
- `scripts/serve.mjs`: dependency-free Node static server for `dist/` on port 4173.
- Vendored: MapLibre GL JS, Leaflet, Leaflet.markercluster, Inter font.
- **Backend (Phases 3.1–3.2) [verified]**: separate `backend/` folder with a FastAPI app: `GET /api/v1/health`, `POST /api/v1/complaints` (metadata only) and `GET /api/v1/complaints/{publicId}` (public tracking), persisted in SQLite via the standard-library `sqlite3`. No authentication. CORS limited to `http://localhost:4173`.
- **Frontend ↔ backend (Phase 3.3) [verified]**: opt-in **backend mode** via `dist/api-config.json`. **Local mode is the default** and behaves as before. In backend mode, report submission and public tracking use the API; the complaint is created first, then its photo/video is uploaded as a separate, retryable step (Phase 3.4); a copy of the evidence also stays in IndexedDB.
- **Evidence storage (Phase 3.4) [verified]**: private local disk storage behind an `EvidenceStorage` interface (`backend/data/evidence/` by default, outside `dist/`), metadata in SQLite (schema version 2). Upload is authorised by a short-lived upload token from the creation receipt (renewable with the claim code). **No endpoint serves stored evidence yet.**
- **Abuse protection (Phase 3.5) [verified]**: in-process sliding-window rate limits on all anonymous endpoints (429 + Retry-After), forwarded headers trusted only from configured proxies, 2 GB evidence quota with reservations (507), `Idempotency-Key` on complaint creation (schema version 3), and a read-only-by-default evidence audit command. The API is started with `python -m app` (uvicorn proxy headers off).
- **Staff console (Phase 3.6) [verified]**: server staff accounts (CLI, scrypt, no default account), bearer sessions held in page memory (8 h, hash stored), `/api/v1/staff` list/detail, evidence streamed only to signed-in staff with every view logged (schema version 4), and a `#/staff` console in backend mode. Local-mode workspaces are unchanged; owners and the public still cannot view uploaded evidence.
- **PostgreSQL database (branch `s3-integration`) [verified against a real local PostgreSQL 15; Supabase Postgres unverified]**: `DATABASE_BACKEND=sqlite|postgres` with `DATABASE_URL`. `app/db.py` hides the driver differences (psycopg 3 connection pool, `?` placeholders translated, `RETURNING` ids, row locks instead of `BEGIN IMMEDIATE`, schema version table plus advisory lock). Same tables, constraints and foreign keys as SQLite schema 5. `python -m app.db_migrate` copies an existing SQLite database into PostgreSQL (dry run by default, source never modified, validated, rerun-safe). SQLite stays the default.
- **Web Push notifications (branch `web-notifications`) [verified: automated suites, plus real push delivery through Windows Push Notification Services to Microsoft Edge on this Windows PC; Android and on-screen Windows toasts not observed]**: VAPID Web Push with a service worker (`dist/sw.js`), persistent notification history with read/unread and unread counts, multiple devices per recipient, citizen inboxes (device token) that follow complaints with the claim code, staff notifications for new complaints and evidence, delivery-failure handling and expired-subscription cleanup, click navigation. Schema version 6. See `backend/README.md` → "Web Push notifications".
- **Supabase Storage evidence storage (branch `s3-integration`) [verified with a fake Supabase HTTP API only]**: `EVIDENCE_STORAGE_BACKEND=local|supabase`. `SupabaseEvidenceStorage` (official `storage3` client, service-role key in the backend only) implements the existing `EvidenceStorage` interface against a private bucket; startup refuses a public bucket. Each evidence row records its backend (schema version 5), so local evidence stays readable after switching. Evidence is still streamed through the staff-only API (no public or signed URLs). The earlier Cloudflare R2 adapter was removed. **Not yet tested against a real Supabase project.**

Future architecture **[proposed]**: the rest of the FastAPI complaint API, PostGIS, background workers (`docs/ARCHITECTURE.md` §5, `docs/api-contract.md`). None of that exists yet beyond the three endpoints above.

## 2. Git state

| Item | Value | Status |
| --- | --- | --- |
| Remote | `https://github.com/hemanth16-cmd/namma-civiclens.git` | [verified] |
| Target branch | `develop` | [verified] |
| Base commit | `02397867a1ddd5580de56caf4424630c70297c3d` (PR #2 Supabase/S3 + PR #3 Web Notifications merged into `origin/develop`) | [verified] |
| Related branches | `main` (`29e5bbc`), `s3-integration` (`f49d30f`), `web-notifications` (`8fc3cc9`) | [verified] |
| Working tree | On `develop`, verified clean before test execution, now adding verified stress suites and docs | [verified] |
| Author identity | `shawntaran <shawntarancr7@gmail.com>` (configured Git repo identity) | [verified] |

## 3. Repository structure and important modules

```
dist/                  The app (served as-is)
  index.html, style.css, app.js, router.js, state.js
  services.js          Data layer + role projections (mirrors proposed API)
  camera-service.js    Camera lifecycle
  location-service.js  GPS fixes, refinement, freshness
  exif.js              JPEG EXIF GPS parser
  filters.js, presentation.js, ui.js
  video-service.js, video-review.js   Admin manual video review
  views/               capture, geo, intelligence, issue, map, map-page, public, workspace
  api-config.json      {"mode": "local"|"backend", "baseUrl"} — local by default (Phase 3.3)
  api-config.js        loads/validates api-config.json; missing → local, invalid → "invalid" (blocks)
  api-client.js        fetch adapter, ApiError, request mapper, response allowlists (no logging)
  report-gateway.js    picks local vs backend for submit/track; in-memory server receipt
  staff-session.js     staff console session, in memory only (Phase 3.6)
  views/staff.js       #/staff console: server sign-in, list, detail, evidence viewer, access log
  map-config.json      Photon geocoder endpoint
scripts/               serve.mjs, check.mjs, browser-smoke.mjs, camera-location-e2e.mjs, backend-e2e.mjs (Phase 3.3; needs both servers)
tests/                 camera-location, exif, services, video, api-integration, evidence-upload, idempotency, staff (*.test.js)
docs/                  ARCHITECTURE.md, api-contract.md, IMPLEMENTATION_PROGRESS.md
backend/               FastAPI service; see backend/README.md
  app/__init__.py      API_VERSION "v1", APP_VERSION "0.1.0", SERVICE_NAME
  app/main.py          create_app(db_path=None, store=None) factory, lifespan creates schema, /api/v1 router, health
  app/complaints.py    POST /complaints, GET /complaints/{publicId}
  app/models.py        ComplaintCreate, GpsIn, PublicComplaint, ComplaintReceipt (Pydantic, camelCase)
  app/store.py         ComplaintStore: the only module with SQL (sqlite3, parameterised)
  app/claims.py        public ID + claim-code generation, scrypt hash/verify
  app/cors.py          explicit origin allowlist (CIVICLENS_CORS_ORIGINS; default http://localhost:4173)
  app/evidence.py      POST /complaints/{id}/evidence: streaming multipart, signatures, limits, idempotent retries
  app/evidence_storage.py EvidenceStorage interface + LocalEvidenceStorage (tmp → fsync → atomic rename)
  app/ratelimit.py     RateLimiter interface + InMemoryRateLimiter (thread-safe, bounded), client_ip(), enforce()
  app/quota.py         EvidenceQuota: reservations before reading, release on failure, 507 policy
  app/evidence_audit.py python -m app.evidence_audit: orphan/missing report; deletion only with explicit flags
  app/__main__.py      python -m app: uvicorn with proxy_headers=False
  app/auth.py          staff password hashing, bearer sessions, current_staff dependency
  app/staff.py         python -m app.staff create|list|disable|enable|set-password
  app/staff_api.py     /staff: sessions, me, complaints list/detail, evidence streaming, access log
  app/supabase_storage.py SupabaseSettings (env validation), SupabaseEvidenceStorage (private bucket via storage3), selected_backend()
  app/db.py            SQLiteDatabase / PostgresDatabase (psycopg pool), database_from_env(), DATABASE_BACKEND selection
  app/db_migrate.py    python -m app.db_migrate: SQLite → PostgreSQL copy (dry run default, validation, rerun-safe)
  app/errors.py        error envelope; 404/405/422/503 storage_unavailable/500
  tests/conftest.py    autouse temp database per test (CIVICLENS_DB_PATH)
  tests/test_health.py 7 tests
  tests/test_complaints.py 44 tests
  tests/test_cors.py   15 tests
  tests/test_evidence.py 52 tests
  tests/test_ratelimit.py 16, test_quota.py 10, test_idempotency.py 18, test_evidence_audit.py 8, test_staff.py 30, test_supabase_storage.py 40, test_postgres.py 16 (real PostgreSQL; skipped without one)
  tests/dbutil.py      direct SQL helper for SQLite or the PostgreSQL test schema
  app/push.py          PushSettings (VAPID env), WebPushSender (pywebpush), python -m app.push generate-vapid-keys
  app/notifications.py Notifier: events, dedupe, history storage, push delivery and failure handling
  app/notifications_api.py /notifications: config, inboxes, follows, subscriptions, history, read state, test
  tests/test_notifications.py 51 tests (fake sender; real encryption + VAPID signature checked against a fake push service)
dist/ (Web Push)
  sw.js                service worker: push → system notification, notificationclick → focus/route or open
  push-client.js       permission, subscribe/unsubscribe, citizen inbox token, launch/click handling
  views/notifications.js inbox UI (#/notifications, #/staff/notifications)
  icons/               notification icon and Android badge
tests/notifications.test.js, tests/notification-gateway.test.js (17 + 4); scripts/notifications-e2e.mjs (real browser, real push service)
  requirements.txt, requirements-dev.txt, pyproject.toml, .gitignore, README.md
  data/                default SQLite location (git-ignored; created on first server start)
  .env.example         environment template (tracked; real .env files are git-ignored)
  .venv/               local virtual environment (git-ignored, not committed)
```

## 4. Features verified as implemented

Verified in this workspace means "unit tests pass" or "observed in the running app", as noted. Real devices were not used.

| Feature | Evidence | Status |
| --- | --- | --- |
| Camera lifecycle logic (rear preference, frame gating, stale-start handling, switching, error states) | 10 passing unit tests with injected fakes | [verified — unit level only] |
| Location logic (fresh fix, bounded refinement, timeout, denial, freshness) | 8 passing unit tests | [verified — unit level only] |
| EXIF GPS parsing and rejection reasons | 6 passing unit tests with synthetic JPEGs | [verified — unit level only] |
| Report lifecycle, role projections, claim code excluded from public view, append-only repairs, independent verification, reviews, notifications, priority formula, filters, storage-failure handling | 14 passing service tests | [verified — unit level only] |
| Admin video review (manual observations, no status change on linking) | 3 passing unit tests | [verified — unit level only] |
| App loads and serves from `npm run dev` | HTTP 200 for `/`, `/app.js`, `/style.css`, `/map-config.json`, `/views/capture.js`; 404 for missing file | [verified] |
| Camera screen shows explicit "Camera access is blocked" / "Location blocked" recovery states when permissions are denied | Observed in the in-app browser pane (which blocks camera) | [verified] |
| Home three-choice screen (`#/home`) | Rendered in browser pane | [verified] |
| Public tracking (`#/track/NCL-2026-DEMO01`) shows status, verification, timeline; claim code not present in page HTML | Rendered in browser pane; HTML searched for claim code: absent | [verified] |
| No console errors on loaded routes | Browser console read: empty | [verified] |
| Browser smoke suite: main journeys, role guards, workspaces, map list/markers agree (8 shown · 8 plotted), legend, attribution, 375 px layout, gallery rejection | `npm run e2e` 56/56 in headless Chromium 153 | [verified — headless Chromium only] |
| Backend `GET /api/v1/health` returns `200 {"status":"ok","service":"namma-civiclens-api","api_version":"v1","version":"0.1.0"}` | pytest + live uvicorn on port 8000 probed with curl | [verified] |
| Backend error envelope: 404 `not_found`, 405 `method_not_allowed`, 422 `validation_error` with field details, 500 `internal_error` without leaking exception text | pytest (7/7); 404 and 405 also confirmed against the live server | [verified] |
| `POST /api/v1/complaints`: validates metadata, generates public ID + claim code, stores only a scrypt hash, one transaction, returns 201 receipt with `Cache-Control: no-store` and `Location` | 44 pytest tests + live curl | [verified] |
| `GET /api/v1/complaints/{publicId}`: public allowlist only (no claim/hash, GPS, address, description, owner); 404 envelope for unknown/malformed IDs; demo-style IDs such as `NCL-2026-DEMO01` accepted | pytest + live curl | [verified] |
| SQLite persistence across application restarts | pytest (reopen + new app) and live server stop/start returning the same complaint | [verified] |
| Claim code absent from DB rows, DB/WAL file bytes, public responses and server log | pytest scans rows and raw files; live check grepped public response and uvicorn log: 0 matches | [verified] |
| Tests detect a leak in the public model | Mutation check: adding `claimHash` to `PublicComplaint` made 2 tests fail; reverted | [verified] |
| Backend CORS: only `http://localhost:4173` by default, no credentials, GET/POST only, wildcard refused, env-configurable | 15 pytest tests; live preflight succeeded from the frontend | [verified] |
| Frontend mode switch: missing `api-config.json` → local; explicit `"backend"` + valid URL → backend; anything else → `invalid` (blocks, never guesses) | unit tests; shipped file parses as local | [verified] |
| Frontend API adapter: health, create, public tracking; 404/422/503/network/timeout/malformed handling; POST failures marked ambiguous and never retried; no console output | 26 unit tests with mocked `fetch` (incl. console capture) | [verified] |
| Backend-mode submission through the real UI → server receipt with server-generated ID and claim code; evidence kept in IndexedDB under `server-evidence:<id>`; no local record; claim code not in local/session storage, console or server log | `scripts/backend-e2e.mjs` 30/30 against both servers (scratch DB), synthetic camera | [verified — synthetic camera/GPS] |
| Backend-mode tracking shows the server's public record; 404 and sample IDs explained; server down → error; never a local fallback | unit tests + `backend-e2e.mjs` | [verified] |
| Local mode unchanged: demo tracking, local submission and receipt, no API calls | `npm test` 67/67, `npm run e2e` 56/56, `npm run e2e:camera` 48/48, `backend-e2e.mjs` local section | [verified] |
| Evidence upload `POST /complaints/{id}/evidence`: JPEG/WebM/MP4 by signature; declared type/extension/complaint media type must agree; 25 MB photo / 200 MB video enforced from `Content-Length` and while streaming; SHA-256 computed while streaming; atomic store; one original per complaint, identical retry → 200 `already_uploaded`, different file → 409 | 52 pytest tests (generated media) | [verified] |
| Upload authorisation: bearer upload token from the receipt (256-bit, SHA-256 hash stored, 1 h TTL); expired → `token_expired`; claim-code exchange `POST /complaints/{id}/upload-token` replaces the token | pytest (missing/wrong/foreign/expired tokens, claim exchange, revocation) | [verified] |
| Failure cleanup: unsupported/oversized/truncated/malformed uploads, disk errors, failed rename and DB failure after rename leave no file, no temp file and no row | pytest with temp DB + temp storage dirs | [verified] |
| Privacy: public tracking has no evidence fields; no route serves stored files; storage keys never returned; logs contain no tokens, claim codes or filenames; storage refused inside `dist/` | pytest + live server log grep | [verified] |
| Schema migration v1 → v2 keeps existing complaints | pytest | [verified] |
| Frontend: receipt shows "Complaint details: Saved" and the evidence upload as separate states; failed upload keeps the complaint ID and local copy and offers "Retry … upload" (never re-creates the complaint); expired token renewed once with the in-memory claim code | 16 new unit tests + `backend-e2e.mjs` | [verified] |
| Real browser uploads against the live API: synthetic-camera JPEG and a real MediaRecorder WebM clip (≈167 KB) accepted; upload failure → retry → success with one complaint; stored file checksum matches | `scripts/backend-e2e.mjs` 45/45 with `EVIDENCE_DIR` | [verified — synthetic camera] |
| Rate limits: create 5/10 min, track 60/min, upload-token 5/15 min per IP + 10/h per complaint, evidence 30/h per IP (pre-auth) + 10/h per complaint (post-auth); 429 `rate_limited` + `Retry-After`; refused requests don't extend the window; configurable via env | 16 pytest tests (fake clock) + live check with defaults | [verified] |
| Limits run before expensive work: token renewal before DB lookup and scrypt; evidence per-IP before token lookup and body read | pytest (call counters) | [verified] |
| Client IP: forwarded headers ignored unless the peer is in `CIVICLENS_TRUSTED_PROXIES`; right-most untrusted XFF entry used behind a trusted proxy; uvicorn proxy headers disabled by `python -m app` | pytest + live check (spoofed XFF after 429 → still 429) | [verified] |
| Evidence quota (default 2 GB): reservation before reading, concurrent reservations cannot over-allocate, released on any failure, usage = max(DB total, bytes on disk) at startup; 507 `storage_quota_exceeded`; nothing auto-deleted | 10 pytest tests (threads) + live check (30 KB quota → second upload 507) | [verified] |
| Idempotent creation: same key + request → one complaint, replay returns re-issued credentials (old ones invalid); different request → 409 conflict; used credentials or > 24 h → 409 used with public ID only; concurrent same-key requests → one complaint; failed transaction leaves no key | 18 pytest tests + 8 frontend tests + `backend-e2e.mjs` lost-response journey + live check | [verified] |
| Frontend: one idempotency key per captured report (in memory), reused on retry, new key for a new report; "Not confirmed" now says retrying is safe; recovered receipt notice; "Already received" with tracking link; 429 shown with wait time; 507 not offered for retry | unit tests + `backend-e2e.mjs` | [verified] |
| Evidence audit command: dry-run default; orphans, missing files, size mismatches, unexpected files, temp files; deletion only with `--delete-orphans`/`--delete-stale-temp`, min-age, DB recheck, inside `objects/` only, DB read-only | 8 pytest tests + run on live e2e data (5/5 consistent) | [verified] |
| Staff accounts via CLI: scrypt hashes, no default account, username/password validation, disable/enable/set-password (disable and password change revoke sessions) | `tests/test_staff.py` | [verified] |
| Staff sign-in: generic 401 for wrong password and unknown user (one scrypt check either way); rate-limited per IP and per username before the password check; token hash stored; sessions expire, revoke on sign-out, refuse disabled accounts even if sessions were not revoked | pytest (incl. a mutation-found gap) + e2e | [verified] |
| Staff complaint list/detail: newest first, paging, operational fields, no claim/hash/token/storage key | pytest + e2e | [verified] |
| Evidence streaming to staff: bytes identical, correct type, `no-store, private`, `nosniff`, sandbox CSP; access log written before any byte; no log → no bytes; missing file → 503 and no log; anonymous / upload-token requests get 401 and nothing is logged | pytest + e2e (real synthetic-camera JPEG, 1920 px, one log entry) | [verified] |
| `#/staff` console (server mode): sign-in, list, detail, view evidence, access log, sign-out; token in memory only (reload signs out); local mode shows a "server mode only" notice; demo admin sign-in points to it in server mode | `tests/staff.test.js` (9) + `backend-e2e.mjs` (15 staff/console checks) | [verified — synthetic camera] |
| PostgreSQL backend: `DATABASE_BACKEND` sqlite (default) or postgres (anything else refused); `DATABASE_URL` validated, password never printed; full schema 5 with the same CHECK/UNIQUE/FK constraints (GPS as DOUBLE PRECISION, ids as identity BIGINT); create, track, upload token, evidence, idempotency (incl. concurrent replays), staff sessions and access log | `tests/test_postgres.py` + the whole suite run on PostgreSQL (`CIVICLENS_TEST_DATABASE=postgres`) | [verified — local PostgreSQL 15] |
| PostgreSQL transactions: a failure mid-create (trigger-forced) leaves no partial complaint; concurrent creates and evidence uploads race safely; pool survives app restart | pytest on real PostgreSQL | [verified — local PostgreSQL 15] |
| SQLite → PostgreSQL migration tool: dry run writes nothing; execute copies every table in one transaction keeping ids, resets sequences; source file hash unchanged; validation (row-by-row match, FK orphan check); rerun is a no-op; conflicting rows stop the run with nothing written; old-schema sources upgraded in a temporary copy | pytest + live CLI against Docker PostgreSQL (`civiclens_migrated`, 23 rows) | [verified — local PostgreSQL 15] |
| Live API on PostgreSQL: `backend-e2e.mjs` 66/66 with `DATABASE_BACKEND=postgres` (local evidence) | live run | [verified — local PostgreSQL 15] |
| Supabase configuration: `EVIDENCE_STORAGE_BACKEND` local (default) or supabase (`r2`, `s3` and others refused); URL https (http only for localhost); anon/publishable keys refused; bucket name validated; key excluded from repr and messages | `tests/test_supabase_storage.py` | [verified] |
| Supabase startup checks: missing bucket, bad key, unreachable project → specific messages; public bucket, too-small file size limit, MIME list excluding JPEG/WebM/MP4 → startup refused | pytest (fake Supabase HTTP API) | [verified — simulated] |
| Supabase adapter: upload with `upsert: false`, content type, `no-store`, SHA-256 metadata; size confirmation (mismatch → object deleted); streamed reads; exists/delete; usage by paged listing; staged temp files always removed | pytest (fake Supabase HTTP API) | [verified — simulated] |
| Supabase through the API: upload 201 with `storage_backend = 'supabase'` and nothing on local disk; staff view streams identical bytes and writes the audit log; anonymous 401, no log; responses expose no key, bucket or object name; idempotent retry 200 / different file 409; outage → 503, no row, quota released; **database failure after upload → object deleted** (compensation); missing object → 503, no log; local evidence readable after switching; local mode never contacts Supabase | pytest (full app, fake Supabase HTTP API) | [verified — simulated] |
| Real Supabase project (Postgres connection, Storage uploads and retrievals) | — | **[unverified]** — no real project or credentials were used |
| Web Push API: config, citizen inboxes (hashed tokens), follows with claim code, devices (validated keys, endpoint allowlist, no duplicates, limit 10), history with cursor paging, read/unread/read-all, unread count, test notification, per-recipient ownership (404 for others) | `tests/test_notifications.py` on SQLite and PostgreSQL | [verified] |
| Web Push events: staff get new complaint / new evidence (disabled staff excluded); followers get follow confirmed / evidence received / first staff evidence view (deduplicated); idempotent replay does not notify again; payloads carry no private data | pytest | [verified] |
| Web Push delivery: 201 → sent; 404/410 → device deleted; 429/5xx/network/other 4xx → failure counted, device deleted after 5 in a row; partial/failed/no_devices/disabled status; expired subscriptions pruned at startup; notification or sender failures never break the triggering request | pytest | [verified] |
| WebPushSender: aes128gcm payload decrypts with the subscriber's private key; VAPID JWT signature verifies with the public key (aud, sub, exp checked); status mapping | pytest against a fake push service | [verified] |
| Frontend Web Push: permission only on click (denied/dismissed/unsupported/unconfigured explained), subscribe with VAPID key, key change re-subscribes, disable keeps the browser subscription while another audience uses it, click handling and launch URL, unsafe links refused (API client and service worker) | `tests/notifications.test.js`, `tests/notification-gateway.test.js` | [verified] |
| Real browser + real push service (Windows): Edge 'new headless' with temporary profiles: SW registered, real WNS subscription (`*.notify.windows.com`), pushes delivered via WNS and shown by the service worker (follow confirmation, evidence received, test, new complaint to two staff devices), history refreshes on arrival, read/unread, notificationclick routes the open window and marks read, launch URL marks read, turn-off removes server device and browser subscription, staff sign-out removes that device | `scripts/notifications-e2e.mjs` 36/36 | [verified — Windows, Edge; notifications read back with `getNotifications()`, toasts on screen not observed] |
| Backend OpenAPI schema and Swagger UI at `/api/v1/openapi.json`, `/api/v1/docs` | pytest + browser pane listed `/api/v1/health` | [verified] |
| Camera/location journeys with Chromium's synthetic camera: auto-start, visible preview, track release, capture at 1920×1080, video record, denial + retry, switching, location timeout/refinement, insecure-HTTP messaging (via LAN address), 375 px and 320 px layouts | `npm run e2e:camera` 48/48 | [verified — synthetic camera and injected positions only] |

## 5. Demos, unverified, or not integrated

| Item | Status |
| --- | --- |
| Real camera capture, real GPS, real phone EXIF, video codecs, phone layouts | [unverified] — no physical device used |
| Vector map tile rendering (OpenFreeMap) and which renderer (MapLibre vs Leaflet fallback) was used during e2e | [unverified] — suite checks markers, not tiles |
| Resident / official workspaces, sign-in flows, work orders, intelligence studio UI | Covered by headless smoke checks; [unverified] by a human walkthrough |
| Photon address lookup | [unverified] — needs network; not exercised |
| Priority queue, knowledge graph, Fuzzy Cognitive Map, Monte Carlo scenario lab | Illustrative demo (per docs; not measurement) |
| Hotspot prediction, road-damage / waste detection AI | [proposed] — adapters report unavailable |
| Duplicate/recurrence suggestions | [proposed] — only recurrence counters exist |
| Web Push on Android phones; Windows toasts actually appearing in the notification centre; background delivery with every browser window closed; Chrome and Firefox desktop | **[unverified]** — no physical phone used; Windows delivery verified to the service worker in headless Edge only (see §4) |
| Telegram | [proposed] — not built (excluded from the Web Push task by the user) |
| Notifications for case status changes | [proposed] — staff case updates do not exist on the server yet |
| Claim/ownership, staff operations, repairs, citizen responses, reviews, map queries, media upload endpoints | [proposed] — not built |
| Rate limiting, abuse protection, request-size limits for anonymous create/tracking | [proposed] — **not built; required before any public deployment** |
| Evidence viewing for owners or the public | [proposed] — only signed-in staff can view (Phase 3.6) |
| Migration of existing local evidence to Supabase Storage; a Supabase orphan audit; signed URLs | [proposed] — deliberately not built (see §11) |
| Operation against a real Supabase project (Postgres and Storage) | [unverified] — PostgreSQL tested on local PostgreSQL 15; Storage only against a fake HTTP API |
| A production SQLite → Supabase Postgres data migration | [proposed] — tool built and tested locally; not run against Supabase (needs the user's approval) |
| Staff case updates, work orders and repairs on the server; more staff roles; MFA | [proposed] |
| Shared / multi-process rate limiting (Redis or similar) | [proposed] — limits are per process, reset on restart |
| Expiry/purge of old idempotency rows | [proposed] — rows are retained |
| MP4 uploads from a real Safari recorder; MOV/HEIC | MP4 [verified only with a generated header in pytest]; MOV/HEIC rejected by design |
| Backend mode for workspaces, claiming, maps, staff tools | [proposed] — these still use local data in both modes |
| Database, object storage, server-side auth, rate limits | [proposed] — not built |
| Role guards / demo login | Demonstration only; not real authorisation |

## 6. Environment and test results

| Item | Value | Status |
| --- | --- | --- |
| OS | Windows 11 Home 10.0.26300 | [verified] |
| Node.js | v25.8.1 (meets documented minimum of 20; README says tested on 22) | [verified] |
| npm | 11.11.0 | [verified] |
| git | 2.53.0.windows.2 | [verified] |
| npm script shell | default (cmd.exe on Windows) | [verified] |
| Playwright package | Not in the project. Installed `playwright@1.63.0` **outside the repo** in the session scratchpad (`…\scratchpad\pw`) and passed via `PLAYWRIGHT_PATH`. Scratchpad is temporary: a future session must reinstall it | [verified] |
| Python | 3.14.3 (default `py`); 3.11 and 3.10 also installed. Backend venv uses 3.14.3 | [verified] |
| Backend direct deps (pinned) | fastapi 0.162.0, uvicorn 0.54.0, **python-multipart 0.0.32** (added in 3.4: streaming multipart parser, the one FastAPI uses for forms); **storage3 2.33.0** (official Supabase Storage client), **psycopg[binary] 3.3.6**, **psycopg-pool 3.3.3** (PostgreSQL); dev: pytest 9.2.0, httpx2 2.16.0. boto3 and moto (R2 period) removed | [verified] |
| SQLite (bundled with Python) | 3.50.4, via standard-library `sqlite3`; no new dependency added in Phase 3.2 | [verified] |
| Backend resolved deps (in `.venv`) | starlette 1.10.0, pydantic 2.14.2, pydantic_core 2.50.2, anyio 4.15.1, h11 0.16.0, click 8.5.0, httpcore2 2.16.0; `pip check`: no broken requirements | [verified] |
| Playwright browser cache | `%LOCALAPPDATA%\ms-playwright` contains chromium-1243 (Chromium 153.0.8010.12), headless shell, ffmpeg | [verified] |

Test results (2026-10-30):

- `npm test` → **41 tests, 41 pass, 0 fail**, 0 skipped, ~4.0 s, exit 0. Matches README claim. [verified]
- `npm run check` → **"Syntax check passed for 28 files."**, exit 0. Matches README claim. [verified]
- `npm run e2e` → **56/56 checks passed**, exit 0. Matches README claim. [verified]
- `npm run e2e:camera` → **48/48 checks passed**, exit 0. Matches README claim. [verified]
- **Phase 3.1 re-run (after adding `backend/`)**: `npm test` 41/41, `npm run check` 28 files, `npm run e2e` 56/56, `npm run e2e:camera` 48/48 — all exit 0. `git diff` on tracked files is empty. [verified]
- **Backend (Phase 3.1)** `.venv/Scripts/python -m pytest -v` → 7 passed. [verified]
- **Backend (Phase 3.2)** `.venv/Scripts/python -m pytest -q` → 51 passed. [verified]
- **Phase 3.3 (final run)**: backend pytest **66 passed** (7 health + 44 complaints + 15 CORS); `npm test` **67/67** (41 existing + 26 new in `tests/api-integration.test.js`); `npm run check` **33 files** (28 + 5 new); `npm run e2e` **56/56**; `npm run e2e:camera` **48/48**; `node scripts/backend-e2e.mjs` **30/30** with both servers running. All exit 0. [verified]
- **Phase 3.4 baseline (before changes)**: backend 66, `npm test` 67/67, `npm run check` 33 files — matched. [verified]
- **Phase 3.4 (final run)**: backend pytest **118 passed** (66 + 52 evidence); `npm test` **83/83** (67 + 16 in `tests/evidence-upload.test.js`); `npm run check` **34 files**; `npm run e2e` **56/56**; `npm run e2e:camera` **48/48**; `node scripts/backend-e2e.mjs` **45/45** (scratch DB + scratch evidence dir). All exit 0; `pip check` clean. [verified]
- **Phase 3.5 baseline (before changes)**: backend 118, `npm test` 83/83, `npm run check` 34 files — matched. [verified]
- **Phase 3.5 (final run)**: backend pytest **170 passed** (118 + 16 rate limit + 10 quota + 18 idempotency + 8 audit); `npm test` **91/91** (83 + 8 in `tests/idempotency.test.js`); `npm run check` **35 files**; `npm run e2e` **56/56**; `npm run e2e:camera` **48/48**; `node scripts/backend-e2e.mjs` **52/52** (API via `python -m app`, limits off, scratch DB + evidence dir); live default-limits check **11/11**. `pip check` clean. [verified]
- **Phase 3.6 baseline**: the Phase 3.5 final run (170 / 91 / 35 files) minutes before; only the progress doc had changed. [verified]
- **Phase 3.6 (final run)**: backend pytest **200 passed** (170 + 30 staff); `npm test` **100/100** (91 + 9 in `tests/staff.test.js`); `npm run check` **38 files**; `npm run e2e` **56/56**; `npm run e2e:camera` **48/48**; `node scripts/backend-e2e.mjs` **66/66** with a CLI-created staff account (scratch DB + evidence dir, API via `python -m app`). The staff password did not appear in the e2e or server logs. [verified]
- **R2 task baseline (start, branch `s3-integration`)**: backend 200, `npm test` 100/100, `npm run check` 38 files. [verified]
- **R2 task (final run)**: backend pytest **242 passed** (200 + 42 in `tests/test_r2_storage.py`); `npm test` **100/100**; `npm run check` **38 files**; `npm run e2e` **56/56**; `npm run e2e:camera` **48/48**; `node scripts/backend-e2e.mjs` **66/66** (API in local mode, scratch DB migrated to schema 5, all rows `local`). `pip check` clean. No real Cloudflare calls. [verified]
- **Web Push task (final run, branch `web-notifications`)**: backend pytest with a real local PostgreSQL **308 passed**; whole suite on PostgreSQL **302 passed, 6 skipped**; without PostgreSQL **291 passed, 17 skipped**; `npm test` **121/121** (100 + 17 + 4); `npm run check` **44 files**; `npm run e2e` **56/56**; `npm run e2e:camera` **48/48**; `node scripts/backend-e2e.mjs` **66/66**; `node scripts/notifications-e2e.mjs` **36/36** (Edge, real WNS delivery). Mutations caught: endpoint allowlist off, read state ignoring the owner, dedupe removed, 410 not deleting, review event removed, pushing to every recipient's devices, unsafe links in the client, service worker using another site's window, permission never requested. [verified]
- **Supabase Storage + PostgreSQL tasks (final run, branch `s3-integration`)**: backend pytest with a real local PostgreSQL **256 passed** (SQLite suite + 16 PostgreSQL tests); whole suite on PostgreSQL (`CIVICLENS_TEST_DATABASE=postgres`) **250 passed, 6 skipped** (SQLite-only mechanics); without PostgreSQL **240 passed, 16 skipped**; `npm test` **100/100**; `npm run check` **38 files**; `npm run e2e` **56/56**; `npm run e2e:camera` **48/48**; `node scripts/backend-e2e.mjs` **66/66** against the API on PostgreSQL. No real Supabase calls. [verified]
- **Phase 3.2 frontend re-run**: `npm test` 41/41, `npm run check` 28 files, `npm run e2e` 56/56, `npm run e2e:camera` 48/48 — all exit 0. The e2e run printed one browser console error (MapLibre: OpenFreeMap sprite request aborted); see §9. [verified]
- No failures in either suite. One log line ("This photo does not contain GPS location data, ") is the multi-line detail text of a passing gallery-rejection check, not an error.

## 7. Current implementation phase

Step 3 — backend implementation. **Phase 3.1 (FastAPI foundation): complete. Phase 3.2 (persistent complaint API on SQLite): complete. Phase 3.3 (opt-in frontend integration): complete. Phase 3.4 (secure evidence upload): complete. Phase 3.5 (rate limiting, quota, idempotency, orphan audit): complete. Phase 3.6 (staff accounts, staff console, private evidence viewing): complete.** On branch `s3-integration`: **SQLite → PostgreSQL (Supabase Postgres) implemented and verified against a real local PostgreSQL; Cloudflare R2 replaced by private Supabase Storage, verified against a fake Supabase HTTP API. Real Supabase project verification pending (needs the user's credentials).** On branch `web-notifications`: **Web Push notifications implemented and tested (automated, plus real push delivery on Windows/Edge); Android and on-screen Windows verification pending.** Next: §10.

Step 2 — environment verification and baseline testing: complete.

## 8. Completed steps and evidence

**Step 1 — initial inspection and setup**

- Workspace `P:\NammaCivicLens` was empty and not a git repo; repository cloned into it (no duplicate). [verified]
- README, ARCHITECTURE, api-contract, package.json, .gitignore, scripts read. [verified]
- Step 1 is now complete for repository setup and inspection. UI walkthrough of the signed-in workspaces is still outstanding.

**Step 2 — environment verification and baseline**

- A. Environment: node/npm/git versions recorded; branch and commit recorded; working tree clean. [verified]
- B. Scripts: `dev`, `test`, `check`, `e2e`, `e2e:camera` exist exactly as documented. No dependencies installed. [verified]
- C. Baseline: 41/41 unit tests pass; syntax check passes for 28 files. [verified]
- D. Startup: server started, printed `http://localhost:4173`; HTTP probes returned 200; app inspected in browser pane (camera, home, public tracking). [verified]
- E. This document written. [verified]
- Browser e2e (approved by user): Playwright 1.63.0 installed outside the repo, chosen because it matches the already-cached Chromium 1243 (latest 1.64.1 needs Chromium 1248, a ~180 MB download). `npm run e2e` 56/56, `npm run e2e:camera` 48/48. [verified]

**Step 3, Phase 3.1 — FastAPI foundation**

1. Inspected working tree: only `.claude/` and `docs/IMPLEMENTATION_PROGRESS.md` untracked; HEAD `a40afe6`. Both preserved. [verified]
2. Created `backend/` with venv (`py -3.14 -m venv .venv`); nothing installed globally. [verified]
3. Minimal FastAPI app via `create_app()` factory; routes under `/api/v1`. [verified]
4. `GET /api/v1/health` returns status, service name, API version and app version. It is a liveness check only (no dependencies exist to check). [verified]
5. 7 pytest tests: health body, versioned path only, 404, 405, 422 field details, 500 without leaking internals, OpenAPI served. 7/7 pass. [verified]
6. `backend/requirements.txt` + `requirements-dev.txt`; root `package.json` untouched. [verified]
7. `backend/README.md` documents layout, setup, run, test, error format and what is not included. [verified]
8. Live check: uvicorn on 127.0.0.1:8000; curl `/api/v1/health` → 200 JSON; `/api/v1/nope` → 404 envelope; `POST /api/v1/health` → 405 envelope; Swagger UI loads in browser pane. [verified]
9. Frontend regression after the change: all four suites pass (see §6). [verified]

Files created in Phase 3.1: `backend/app/__init__.py`, `backend/app/main.py`, `backend/app/errors.py`, `backend/tests/__init__.py`, `backend/tests/test_health.py`, `backend/requirements.txt`, `backend/requirements-dev.txt`, `backend/pyproject.toml`, `backend/.gitignore`, `backend/README.md`. Modified: `.claude/launch.json` (added a `civiclens-api` entry), this file. No tracked file was modified.

**Step 3, Phase 3.2 — persistent complaint API (SQLite)**

Decisions given by the user: SQLite via standard-library `sqlite3`, no SQLAlchemy, repository layer, configurable path, no demo seeding, demo-ID compatibility, frontend unchanged, no auth, no commit.

1. Inspected `git status` (only `.claude/`, `backend/`, this file untracked), `backend/`, `dist/services.js` (`create`, `receipt`, `publicRecord`, `referenceId`), `dist/views/capture.js` (actual `api.create()` payload), `dist/location-service.js` (`toFix()`), `dist/exif.js`. [verified]
2. `app/store.py`: schema created at startup (lifespan), idempotent; parameterised SQL; complaint + first timeline row in one transaction; `public_id UNIQUE`; bounded retries (5) on ID collision, then `IdAllocationError` → 503; ISO 8601 UTC timestamps; WAL mode; `PRAGMA user_version = 1`; `sqlite3.Error` wrapped in `StoreError` → generic 503. [verified by tests]
3. `app/claims.py`: public ID via `secrets.choice` over the `referenceId()` alphabet, year in IST; claim code = UUID v4; stored as `scrypt$16384$8$1$<salt>$<hash>` with a 16-byte random salt; constant-time `verify_claim_code()`. [verified by tests]
4. `app/models.py`: `ComplaintCreate` with `extra="forbid"`, the 9 real categories, coordinate/accuracy/time validation; explicit `PublicComplaint` and `ComplaintReceipt` response models. [verified by tests]
5. `app/complaints.py`: the two endpoints; receipt is the only response containing the claim code. [verified]
6. `app/errors.py`: added `StoreError` → `503 storage_unavailable` (generic message, cause logged server-side). Existing handlers unchanged. `app/main.py`: `create_app()` gained optional `db_path`/`store` arguments and a lifespan; health route unchanged. [verified]
7. 44 new tests in `tests/test_complaints.py` covering all 18 required cases (plus `created` alias, default area, NaN, missing GPS, unreadable DB file). `tests/conftest.py` gives each test its own temporary DB. [verified]
8. Live check on a scratch database (`CIVICLENS_DB_PATH` in the session scratchpad, so `backend/data/` was not created): POST → 201 receipt; GET → 200 public projection; unknown ID → 404; invalid body → 422 with field details; claim code found 0 times in the public response and the server log; after stopping and restarting the server the same complaint was returned. [verified]
9. Frontend regression: all four suites pass; `git diff` on tracked files is empty. [verified]

Files created in Phase 3.2: `backend/app/claims.py`, `backend/app/store.py`, `backend/app/models.py`, `backend/app/complaints.py`, `backend/tests/conftest.py`, `backend/tests/test_complaints.py`. Modified (all untracked, created in 3.1): `backend/app/main.py`, `backend/app/errors.py`, `backend/.gitignore` (database files, `.env`), `backend/README.md` (rewritten for 3.2), this file. Not modified: any frontend file, `package.json`, `package-lock.json`, `.claude/launch.json`.

Database schema (version 1): `complaints` (pk, `public_id` UNIQUE, `claim_hash`, title, category, area, description, `media_type`, `share_evidence`, `gps_lat`/`gps_lng`/`gps_accuracy`/`gps_source`/`gps_source_type`/`gps_timestamp`/`gps_fix_age_seconds`/`gps_address`, `captured_at`, `capture_source`, `created_at`, `uploaded_at`, `updated_at`, status, verification, department; CHECK constraints on ranges and enums), `complaint_timeline` (append-only), `verification_log` (append-only; nothing writes to it yet). Full description in `backend/README.md`.

**Step 3, Phase 3.3 — opt-in frontend-to-backend integration**

Decisions given by the user: local mode stays default; backend mode opt-in via documented config; metadata only, evidence stays in IndexedDB; show the server receipt with the claim code once; backend tracking in backend mode; no migration of local complaints; no commit; Playwright outside the repo.

1. Inspected `git status`, the progress doc, backend modules, `dist/services.js`, `app.js`, `router.js`, `state.js`, `views/capture.js` (submit + receipt), `views/public.js` (tracking), `views/geo.js` (existing `map-config.json` loading), `exif.js` (capture time already UTC ISO), existing tests and e2e scripts. [verified]
2. **Configuration** — new `dist/api-config.json` (`{"mode":"local","baseUrl":"http://localhost:8000/api/v1"}`) and `dist/api-config.js`. A separate file rather than `map-config.json`, because that file is the geocoder's config and is read by `geo.js`; mixing them would couple unrelated settings. Missing file or missing `mode` → local; `"backend"` needs an absolute http(s) `baseUrl` without credentials/query/fragment (trailing slash trimmed); anything else → mode `invalid`, which blocks reporting and tracking with an error. [verified by tests]
3. **API adapter** — `dist/api-client.js`: `fetch` with `credentials: 'omit'`, `cache: 'no-store'`, 10 s timeout via `AbortController`; keeps the server's error envelope (`code`, `message`, `details`) in `ApiError`; network/timeout errors on POST are flagged `ambiguous`; no automatic retry; responses copied through allowlists (`toReceipt`, `toPublicRecord`) so unexpected server fields never reach the UI; no console output at all. [verified by tests]
4. **Request mapper** — `toCreatePayload()` sends only `category, mediaType, description, shareEvidence, neighbourhood, gps{lat,lng,accuracy,sourceType,timestamp,source,fixAgeSeconds,neighbourhood,address}, capturedAt, captureSource`. `created` → `capturedAt` as UTC ISO (`…Z`); a time string without a timezone is dropped rather than guessed; GPS fix time stays epoch ms (ISO strings with a zone are converted). `owner`, `uploadedAt`, Blobs, client IDs/claims and unknown GPS keys are never sent. No usable GPS fix, or no recognised `sourceType` → `missing_location` error before any request (no coordinates invented). [verified by tests]
5. **Gateway** — `dist/report-gateway.js`: local mode calls the unchanged `api.create()` / `api.publicRecord()`; backend mode posts metadata, then stores the evidence Blob in IndexedDB under `server-evidence:<id>` (never the bare ID, so local evidence cannot be overwritten). The server receipt is held in a module variable and handed to the receipt page once (`takeServerReceipt`), never written to storage, URL or a global. No fallback between stores in either direction. [verified by tests]
6. **Capture screen** (`views/capture.js`, edited): submit goes through the gateway; button text "Sending to the CivicLens server…" in backend mode; failure notices distinguish "Not sent" (server answered with an error; field details listed) from "Not confirmed" (connection failed, outcome unknown, retry may duplicate); local error text unchanged. New `renderServerReceipt()` shows the server ID and claim code with "Save this code now. It will not be shown again", says the photo/video was **not uploaded** and is kept only in this browser, previews the local copy labelled "on this device only", and offers the private receipt download (text notes the media was not uploaded). The camera-screen footnote (`#storage-note`) changes text only in backend/invalid mode. Local receipt markup unchanged. [verified]
7. **Tracking** (`views/public.js`, edited): local mode runs the unchanged code path. Backend mode uses `renderServerTrack()`: loading message, server record via the existing `trackResult()` (workspace link hidden), not-found message, a specific message for sample IDs `NCL-2026-DEMO01…08` ("samples exist only in local demo mode"), and an error state when the server is unreachable or fails. [verified]
8. **Backend CORS** — `backend/app/cors.py`, wired in `create_app(cors_origins=None)`; `CIVICLENS_CORS_ORIGINS` env override; wildcard and malformed origins refused at startup. [verified by tests]
9. **Live two-server check** — frontend `http://localhost:4173`, API `http://localhost:8000` on a scratch DB in the session scratchpad. `scripts/backend-e2e.mjs` drove the real capture UI with Chromium's synthetic camera and a fixed geolocation: POST 201 → server receipt `NCL-2026-6MCGT2` (demo data, description "Automated integration test report…"); GET via API and via the tracking page returned it without claim/GPS/description; reopening the receipt did not show the claim again; 503 and dropped-connection submissions showed "Not sent" / "Not confirmed" and saved nothing; tracking with the API unreachable showed an error and no local record; local mode tracked `NCL-2026-DEMO01` and saved a local report with no API calls. Server log: only method/path/status lines, no claim codes. [verified]

Files created in Phase 3.3: `dist/api-config.json`, `dist/api-config.js`, `dist/api-client.js`, `dist/report-gateway.js`, `tests/api-integration.test.js`, `scripts/backend-e2e.mjs`, `backend/app/cors.py`, `backend/tests/test_cors.py`. Modified: `dist/views/capture.js` (+112/−11), `dist/views/public.js` (+39), `backend/app/main.py`, `backend/README.md`, this file. Not modified: `package.json`, `package-lock.json`, `dist/services.js`, `dist/app.js`, `dist/router.js`, `dist/state.js`, `dist/map-config.json`, `.claude/launch.json`, existing tests and e2e scripts.

**How to switch modes** (reload the page after editing):

- Local (default): `dist/api-config.json` → `{"mode": "local", ...}` (or delete the file).
- Backend: start the API (`cd backend && .venv/Scripts/python -m uvicorn app.main:app --port 8000`), then set `{"mode": "backend", "baseUrl": "http://localhost:8000/api/v1"}`. Open the frontend at `http://localhost:4173` (not `127.0.0.1`, unless that origin is added to `CIVICLENS_CORS_ORIGINS`).

**Step 3, Phase 3.4 — secure evidence upload and private storage**

Scope given by the user: private local filesystem storage behind an interface; metadata in SQLite; streamed multipart upload with signature checks and limits; safe authorisation without inventing accounts; evidence private by default; frontend upload as a separate, retryable step; local mode unchanged; no commit.

1. Baseline re-run before changes: backend 66, `npm test` 67/67, `npm run check` 33 files. [verified]
2. **Dependency:** `python-multipart==0.0.32`. Reason: FastAPI's `UploadFile` parses and spools the whole body before the handler runs, so limits and authorisation could not be enforced while reading; python-multipart's low-level `MultipartParser` is driven chunk by chunk from `request.stream()`. It is the parser FastAPI itself uses for forms. `pip check` clean. [verified]
3. **Storage** (`app/evidence_storage.py`): `EvidenceStorage` protocol; `LocalEvidenceStorage(root, forbidden_roots=[dist/])`. Keys = `secrets.token_hex(16)`, validated `^[0-9a-f]{32}$` before becoming `objects/<2>/<32>`. Writes: `open(tmp/<random>.part, 'xb')` → chunks → fsync → `os.replace`. `abort()` deletes the temp file. Stale `*.part` (> 1 h) removed at startup. [verified by tests]
4. **Schema v2** (`app/store.py`): numbered migrations via `PRAGMA user_version`; v2 adds `complaints.upload_token_hash`, `complaints.upload_token_expires_at` and the `evidence` table (`evidence_id` UNIQUE, `storage_key` UNIQUE, `UNIQUE(complaint_pk, role)`, CHECKs on type/size/hash). New store methods: `issue_upload_token`, `check_upload_token`, `get_evidence`, `add_evidence` (returns `exists` on a unique-constraint race). `create()` issues the first token in the same transaction. All SQL stays in `store.py`. [verified by tests]
5. **Authorisation**: upload token (256-bit `token_urlsafe`) in the creation receipt; SHA-256 hash + expiry stored; `Authorization: Bearer` only. `POST /complaints/{id}/upload-token` with `{"claim"}` (scrypt-verified) issues a replacement. Documented in `backend/README.md` and `docs/api-contract.md` §5.1 before/with implementation. [verified by tests]
6. **Endpoint** (`app/evidence.py`): order of checks — ID format → bearer token present → token valid/expired (no body read yet) → multipart content type → `Content-Length` ≤ limit → stream: first 64 bytes decide JPEG/WebM/MP4, declared type + extension consistency, per-type size limit while streaming, total body limit, 30 s per-chunk read timeout, closing boundary required → media type must match complaint → duplicate check → commit → DB insert (file deleted if insert fails). Errors via new `ApiProblem` (specific `error.code`) in `errors.py`. [verified by tests]
7. **CORS**: `Authorization` added to allowed headers. [verified by e2e]
8. **Frontend**: `api-client.js` gains `uploadEvidence()` (FormData, bearer header, 180 s timeout, result allowlist) and `requestUploadToken()`; receipt mapper keeps the token. `report-gateway.js`: `submitReport()` strips the token from the receipt view and stores an in-memory upload context (token, claim, Blob, state); `uploadEvidence(id)` never creates complaints, shares in-flight requests, renews an expired/invalid token once with the claim code, classifies failures as retryable (network, timeout, 5xx, incomplete) or not (413/415/422/409/rejected claim); `forgetUpload()` on leaving the receipt. `views/capture.js`: "Submission status" card (Complaint details: Saved / Photo|Video: Uploading → Uploaded privately (size, checksum) | Upload failed + "Retry … upload" + "does not create a new complaint"); local copy text; private-receipt download text reflects the upload state; camera-screen note updated for backend mode. Local mode untouched. [verified]
9. **Live check**: API on scratch DB + scratch evidence dir (`CIVICLENS_DB_PATH`, `CIVICLENS_EVIDENCE_DIR` in the session scratchpad). `backend-e2e.mjs` (45 checks): photo created + uploaded (201, checksum of stored file matches, one new file, outside `dist/`), public API and tracking page show no evidence data or media, upload 503 → "Upload failed" with complaint ID kept and local copy present → Retry → 201 to the same complaint with exactly one complaint POST, real MediaRecorder WebM video uploaded (201, `video/webm`), local mode unchanged. Server log: no claim codes, `Bearer` or filenames; `tmp/` empty afterwards. The dev database and `backend/data/` were never created. [verified — synthetic camera, no physical device]

Files created in Phase 3.4: `backend/app/evidence.py`, `backend/app/evidence_storage.py`, `backend/tests/test_evidence.py`, `tests/evidence-upload.test.js`. Modified: `backend/app/store.py`, `backend/app/claims.py`, `backend/app/models.py`, `backend/app/complaints.py`, `backend/app/errors.py`, `backend/app/cors.py`, `backend/app/main.py`, `backend/requirements.txt`, `backend/tests/conftest.py`, `backend/README.md`, `dist/api-client.js`, `dist/report-gateway.js`, `dist/views/capture.js`, `scripts/backend-e2e.mjs`, `docs/api-contract.md` (status line and §5), this file. Not modified: `package.json`, `package-lock.json`, `dist/services.js`, `dist/views/public.js` (since 3.3), `.claude/launch.json`.

**Step 3, Phase 3.5 — rate limiting, storage quota, idempotency, orphan audit**

1. Baseline re-run before changes: backend 118, `npm test` 83/83, `npm run check` 34 files. [verified]
2. **Rate limiting** (`app/ratelimit.py`): `RateLimiter` protocol; `InMemoryRateLimiter` (sliding window per key, `threading.Lock`, max 10,000 keys with LRU eviction, injectable clock). `RateLimitSettings` with documented defaults and `CIVICLENS_RATE_*` overrides, `CIVICLENS_RATE_LIMITS=off`, `CIVICLENS_TRUSTED_PROXIES`. `enforce()` raises `ApiProblem(429, "rate_limited")` with `Retry-After`. Applied: create (dependency), track (dependency), token renewal (per IP, then per complaint, before lookup/scrypt), evidence (per IP before token lookup; per complaint after authorisation). [verified]
3. **Quota** (`app/quota.py`): `EvidenceQuota` with reservations (context manager), commit to real size, release on failure; loaded at startup from max(DB `SUM(byte_size)`, bytes on disk via new `LocalEvidenceStorage.stored_bytes()`). Evidence route reserves `Content-Length` (or max request size) before reading. Policy `507 storage_quota_exceeded`. [verified]
4. **Idempotency** (`app/store.py`, `app/complaints.py`): migration 3 adds `idempotency_keys` (`UNIQUE(scope, key_hash)`, fingerprint, replay count) and `complaints.credentials_used_at` (set on evidence upload and token renewal). `create()` inserts the key row in the complaint's transaction; `create_idempotent()` → created / replayed (re-issue under `BEGIN IMMEDIATE`) / `IdempotencyConflict` / `IdempotencyCompleted`. Key format validated; only SHA-256 stored; fingerprint = SHA-256 of canonical JSON of the normalised request. `ComplaintReceipt.replayed`; header `Idempotent-Replayed`. `ApiProblem` gained `extra` for `complaintId`. CORS allows `Idempotency-Key`, exposes `Retry-After`, `Idempotent-Replayed`. [verified]
5. **Receipt-recovery decision**: chose explicit credential re-issue over storing encrypted secrets (no new dependency, no server key to manage, nothing secret stored). Consequences defined and tested (old credentials invalid; refused after first use or window). **User confirmed on 2026-10-30: keep credential re-issue;** encrypted replay (option A) is not planned. [decision recorded; see §11]
6. **Orphan audit** (`app/evidence_audit.py`): see `backend/README.md` for commands and safety rules. [verified]
7. **Bugs found and fixed during this phase**: (a) `create_app()` used `rate_limiter or InMemoryRateLimiter()`, and an empty limiter is falsy (`__len__` = 0), so injected limiters were silently replaced — caught by the fake-clock tests (Retry-After stayed at the full window); fixed with explicit `is None` checks. (b) The frontend marked `507` as retryable (all ≥ 500 were) — caught by a new unit test; fixed. (c) **Live check found a real rate-limit bypass**: uvicorn trusts `X-Forwarded-For` from 127.0.0.1 by default and rewrote the client address, so a spoofed header got `201` after a `429`. Fixed with `python -m app` (proxy headers off), `--no-proxy-headers` in the documented uvicorn command and `.claude/launch.json`; re-verified live (spoof → 429). [verified]
8. **Live checks** (scratch DB + evidence dirs; never `backend/data/`): `backend-e2e.mjs` 52/52 including a real lost-response journey (Playwright forwards the first POST to the server, then drops the response; retry with the same key → 200 replayed, same complaint, new claim code; scratch DB shows 5 complaints for 5 reports and 1 replay); default-limits check 11/11 (duplicate → one complaint, conflict 409, quota 507, used 409 with public ID, 6th create 429 with Retry-After 586, spoofed XFF still 429, claim guessing cut off after 5); audit on e2e data 5 recorded / 5 on disk / 0 orphans / 0 missing. Server logs: no claim codes, tokens or guesses. [verified]

Files created in Phase 3.5: `backend/app/ratelimit.py`, `backend/app/quota.py`, `backend/app/evidence_audit.py`, `backend/app/__main__.py`, `backend/tests/test_ratelimit.py`, `backend/tests/test_quota.py`, `backend/tests/test_idempotency.py`, `backend/tests/test_evidence_audit.py`, `tests/idempotency.test.js`. Modified: `backend/app/store.py`, `backend/app/complaints.py`, `backend/app/evidence.py`, `backend/app/evidence_storage.py`, `backend/app/errors.py`, `backend/app/cors.py`, `backend/app/models.py`, `backend/app/main.py`, `backend/tests/conftest.py` (limits off by default for tests), `backend/tests/test_evidence.py` (migration test now expects schema 3 and checks the new table/column), `backend/README.md`, `dist/api-client.js`, `dist/report-gateway.js`, `dist/views/capture.js`, `scripts/backend-e2e.mjs`, `docs/api-contract.md`, `.claude/launch.json` (`--no-proxy-headers`), this file. Not modified: `package.json`, `package-lock.json`, `backend/requirements*.txt` (no new dependency), `dist/services.js`.

**Step 3, Phase 3.6 — staff accounts, staff console, private evidence viewing**

User decisions (2026-10-30): staff accounts via a CLI; bearer token in page memory; evidence viewable by staff only, with an audit log; frontend staff list and viewer in server mode with local workspaces unchanged. Also confirmed: keep credential re-issue for idempotent replays.

1. **Schema v4** (`store.py`): `staff_accounts` (username UNIQUE, scrypt `password_hash`, role `official`, `disabled_at`), `staff_sessions` (`token_hash` UNIQUE, `expires_at`, `revoked_at`), `evidence_access_log` (append-only: evidence, staff, action `view`, client IP, time). Store methods for accounts, sessions (lookup refuses expired, revoked or disabled), paged complaint list, staff record with evidence summary, access logging. [verified]
2. **Auth** (`app/auth.py`): `hash_password`/`verify_password` reuse the claim-code scrypt; `sign_in()` always runs one scrypt check (dummy hash for unknown users); 256-bit tokens, SHA-256 stored, TTL `CIVICLENS_STAFF_SESSION_SECONDS` (8 h); `current_staff` dependency → 401 with `WWW-Authenticate: Bearer`. [verified]
3. **CLI** (`app/staff.py`): create/list/disable/enable/set-password; passwords via no-echo prompt or `--password-stdin`, never arguments or output; ≥ 12 characters. [verified]
4. **Routes** (`app/staff_api.py`): see `backend/README.md` "Staff console API". Evidence: storage `open_read()` (key only) → access-log write → stream; logging failure closes the file and returns 503. Login limits `staff_login_per_ip` 5/900 and `staff_login_per_account` 10/3600 added to `RateLimitSettings`. CORS allows `DELETE`, exposes `Content-Disposition`. [verified]
5. **Frontend**: `api-client.js` staff methods (`staffSignIn`, `staffSignOut`, `listStaffComplaints`, `getStaffComplaint`, `fetchStaffEvidence` → Blob of an allowed type only, `getEvidenceAccessLog`) with allowlisted projections; `request()` gained `raw` and `204` handling; `staff-session.js` (memory only, expiry-aware); `views/staff.js` (`#/staff`, `#/staff/<id>`); `app.js` route; `public.js` notice on the official login in server mode. Any 401 clears the session and returns to sign-in. [verified]
6. **Tests and checks**: 30 backend tests; mutations — skipping the audit log (2 failures) and leaking `claimHash` (1 failure) were caught; ignoring disabled accounts in the session lookup was **not** caught at first (CLI disable also revokes sessions), so a test now disables an account directly in the database and confirms its unrevoked session is refused. Two existing tests updated for intended changes (CORS disallowed-method example now PUT/PATCH; migration test expects schema 4 and the new tables). 9 frontend tests; 15 new e2e checks. [verified]
7. **Interruption**: the session hit a usage limit after the e2e run; on resuming, the frontend dev server had stopped (both browser suites first failed to connect, HTTP 000). After restarting it, smoke 56/56 and camera 48/48. [verified]

Files created in Phase 3.6: `backend/app/auth.py`, `backend/app/staff.py`, `backend/app/staff_api.py`, `backend/tests/test_staff.py`, `dist/staff-session.js`, `dist/views/staff.js`, `tests/staff.test.js`. Modified: `backend/app/store.py`, `backend/app/models.py`, `backend/app/evidence_storage.py`, `backend/app/ratelimit.py`, `backend/app/cors.py`, `backend/app/main.py`, `backend/tests/test_cors.py`, `backend/tests/test_evidence.py`, `backend/README.md`, `dist/api-client.js`, `dist/app.js`, `dist/views/public.js`, `scripts/backend-e2e.mjs`, `docs/api-contract.md`, this file. No new dependency; `package.json` unchanged.

How to try it: start the API (`cd backend && .venv/Scripts/python -m app`), create an account (`.venv/Scripts/python -m app.staff create --username <name> --display-name "<name>"`), set `dist/api-config.json` to backend mode, open `http://localhost:4173/#/staff`.

**Storage task — Cloudflare R2 evidence storage (branch `s3-integration`) — superseded by Supabase Storage below; R2 code removed**

Request history: the user first asked for AWS S3 support, then (during implementation, before any S3 tests or docs were written) switched the target to Cloudflare R2 for its free tier and S3-compatible API. The S3 adapter existed only as uncommitted work on this branch; it was converted in place and removed. No AWS-specific code, configuration or variables remain; `boto3` stays as the S3-compatible client.

1. **Inspection**: branch `s3-integration` confirmed, tree clean, baseline 200 / 100 / 38. Storage call sites: `evidence.py` (begin/commit/delete), `staff_api.py` (`open_read`), `main.py` (construction, quota `stored_bytes`), `evidence_audit.py` (local-only). Gap found: rows did not record their backend. [verified]
2. **Schema v5**: `evidence.storage_backend` (`local`/`r2`, default `local`). Upload route records `storage.name`; staff route reads from `app.state.evidence_storages[row.storageBackend]`; an unconfigured backend → `StoreError` (503) before logging. Local storage is always constructed. Quota usage = max(DB total, sum of `stored_bytes()` over configured backends). Audit tool restricted to `local` rows. [verified]
3. **Interface additions**: `name` on each storage; `content_type` on pending uploads (set from the detected signature). [verified]
4. **`app/r2_storage.py`**: `selected_backend()`, `R2Settings` (validation, secrets excluded from repr), `R2EvidenceStorage` (spooled upload → `upload_fileobj` → `head_object` confirmation; `_R2Reader` for streaming; error codes only in messages/logs; `HeadBucket` at startup). [verified — moto]
5. **Dependencies**: `boto3==1.43.122` (runtime), `moto[s3]==5.2.5` (dev). [verified]
6. **Retrieval decision**: streaming kept; presigned URLs rejected (bearer links bypass the staff session and view-level audit; need bucket CORS). Documented in `backend/README.md`. [decision]
7. **Tests**: 42 R2 tests; mutations caught — reading from the active backend instead of the row's backend (local-after-switch test failed), skipping the size confirmation, and using the default AWS credential chain. Migration test updated to schema 5 with a check that `storage_backend` defaults to `local`. [verified]
8. **Docs**: `backend/README.md` (Cloudflare R2 section: why, comparison, bucket and token setup, variables, local mode, retrieval trade-off, failure table, moto testing, limits), `backend/.env.example` (new; `backend/.gitignore` now allows only this file), `docs/api-contract.md` storage wording. [verified]

Files created: `backend/app/r2_storage.py`, `backend/tests/test_r2_storage.py`, `backend/.env.example`. Modified: `backend/app/evidence.py`, `backend/app/evidence_audit.py`, `backend/app/evidence_storage.py`, `backend/app/main.py`, `backend/app/staff_api.py`, `backend/app/store.py`, `backend/requirements.txt`, `backend/requirements-dev.txt`, `backend/tests/conftest.py`, `backend/tests/test_evidence.py`, `backend/.gitignore`, `backend/README.md`, `docs/api-contract.md`, this file. Removed: `backend/app/s3_storage.py` (created earlier in the same task, never committed). Frontend unchanged.

**Storage task — Cloudflare R2 → Supabase Storage (branch `s3-integration`)**

The user replaced the R2 target with Supabase Storage. Nothing from R2 was committed; it was removed from the working tree.

1. **`app/supabase_storage.py`**: `selected_backend()` (`local`/`supabase`), `SupabaseSettings` (URL, service-role key, bucket; key hidden from repr; anon/publishable keys refused), `SupabaseEvidenceStorage` on the official `storage3` client (the full `supabase` package would add realtime/auth dependencies). Uploads are staged to the local store's private `tmp/`, uploaded with `upsert: false`, then confirmed by size; downloads stream through the SDK's HTTP client because `storage3.download()` buffers whole files. Startup refuses public buckets, too-small size limits and MIME lists that exclude our types. [verified — simulated]
2. **Schema**: `evidence.storage_backend` CHECK is now `('local', 'supabase')` (SQLite migration 5 and the PostgreSQL schema). R2 was never committed, so no stored row uses `r2`. [verified]
3. **Removed**: `app/r2_storage.py`, `tests/test_r2_storage.py`, `boto3`, `moto`, every `R2_*` variable and the R2 documentation. [verified by grep]
4. **Tests**: 40 tests in `tests/test_supabase_storage.py` against an `httpx.MockTransport` fake, including compensation (database failure after upload deletes the object) on SQLite and on PostgreSQL. Mutations caught: allowing a public bucket, skipping upload confirmation, reading from the active backend instead of the row's backend. [verified]
5. **Docs**: `backend/README.md` (Supabase Storage section: setup, variables, streaming vs signed URLs, failure table, testing), `backend/.env.example`, `docs/api-contract.md`. [verified]

**Database task — SQLite → PostgreSQL / Supabase Postgres (branch `s3-integration`)**

1. **`app/db.py`**: `SQLiteDatabase` and `PostgresDatabase` behind one small interface (`connect`, `execute`, `insert`, `transaction(immediate=)`, `apply_schema`, `close`). PostgreSQL uses a psycopg 3 pool (`CIVICLENS_DB_POOL_MAX`, default 10), `prepare_threshold=None` so it also works through the Supabase transaction pooler, and a `civiclens_schema` version table guarded by an advisory lock. [verified]
2. **`app/store.py`**: all SQL goes through the database object; `POSTGRES_SCHEMA` mirrors SQLite schema 5 (identity BIGINT ids, DOUBLE PRECISION GPS, timestamps kept as ISO text so API output is byte-identical). Row locks (`FOR UPDATE`) replace `BEGIN IMMEDIATE` for idempotency replays. [verified]
3. **`app/db_migrate.py`**: reads the SQLite file through a read-only connection into a temporary backup copy (upgraded there if older), plans per table (insert / already present / conflict), copies in one transaction with original ids, resets identity sequences, then validates row by row plus FK orphan checks. Dry run by default; `--execute` to write; `--json` for a report. [verified]
4. **Callers**: `main.py` (closes the pool on shutdown), `staff.py` CLI, `evidence_audit.py` (read-only transaction on PostgreSQL). [verified]
5. **Tests**: real PostgreSQL 15 in Docker (`127.0.0.1:55432`, throwaway). `tests/test_postgres.py` (16) plus the whole suite in PostgreSQL mode with a private schema per test. Mutations caught: GPS as REAL, skipping the sequence reset. Fixed during the task: pool reuse after app restart; row iteration order in validation; non-ASCII console output in the migration CLI on Windows (cp1252). [verified]

Files created (both tasks): `backend/app/db.py`, `backend/app/db_migrate.py`, `backend/app/supabase_storage.py`, `backend/tests/dbutil.py`, `backend/tests/test_postgres.py`, `backend/tests/test_supabase_storage.py`. Modified: `backend/app/evidence.py`, `backend/app/evidence_audit.py`, `backend/app/evidence_storage.py`, `backend/app/main.py`, `backend/app/staff.py`, `backend/app/staff_api.py`, `backend/app/store.py`, `backend/requirements.txt`, `backend/requirements-dev.txt`, `backend/tests/conftest.py`, `backend/tests/test_complaints.py`, `backend/tests/test_evidence.py`, `backend/tests/test_evidence_audit.py`, `backend/tests/test_idempotency.py`, `backend/tests/test_staff.py`, `backend/.env.example`, `backend/.gitignore`, `backend/README.md`, `docs/api-contract.md`, this file. Removed: `backend/app/r2_storage.py`, `backend/tests/test_r2_storage.py`. Frontend, `package.json` and the lockfile unchanged.

**Web Push task (branch `web-notifications`)**

Prerequisites: the user committed and pushed `s3-integration` (`f49d30f`); `develop` was not updated, and the user said to skip that merge. Claude created `web-notifications` from `s3-integration` (no upstream; nothing pushed).

1. **Schema 6** (SQLite migration 6, `POSTGRES_MIGRATIONS[6]`; PostgreSQL base schema stays 5): `notification_recipients` (staff account or hashed citizen inbox token, CHECK enforces exactly one), `notification_follows`, `push_subscriptions` (unique per recipient and endpoint), `notifications` (unique dedupe key per recipient). Added to the migration tool's table and foreign-key lists (nullable references are not orphans). [verified]
2. **Backend**: `push.py` (VAPID settings validated at startup, key generator, `WebPushSender` with status mapping, logs without endpoints or response bodies), `notifications.py` (events, dedupe, delivery, failure counting), `notifications_api.py` (routes; bearer = staff session or citizen inbox). Events from `complaints.py`, `evidence.py`, `staff_api.py` run as background tasks. Five new rate limits. [verified]
3. **Frontend**: `sw.js`, `push-client.js`, `views/notifications.js`, receipt "Notify me about this report", auto-follow on devices with notifications on, track-page link, staff inbox and unread badge, staff sign-out removes the device, click handling in `app.js`, styles, icons. [verified]
4. **Found while testing**: Playwright's ephemeral contexts and open-source Chromium cannot register for push (no push service credentials / incognito), so the e2e uses Edge with persistent temporary profiles; the service worker now posts the click message before focusing (focus can be refused). Earlier PostgreSQL migration fixture needed the new tables populated. [verified]

Files created: `backend/app/push.py`, `backend/app/notifications.py`, `backend/app/notifications_api.py`, `backend/tests/test_notifications.py`, `dist/sw.js`, `dist/push-client.js`, `dist/views/notifications.js`, `dist/icons/notification-192.png`, `dist/icons/notification-badge-96.png`, `tests/notifications.test.js`, `tests/notification-gateway.test.js`, `scripts/notifications-e2e.mjs`. Modified: `backend/app/store.py`, `backend/app/main.py`, `backend/app/models.py`, `backend/app/ratelimit.py`, `backend/app/complaints.py`, `backend/app/evidence.py`, `backend/app/staff_api.py`, `backend/app/db_migrate.py`, `backend/requirements.txt` (pywebpush 2.5.0), `backend/tests/conftest.py`, `backend/tests/test_evidence.py`, `backend/tests/test_postgres.py`, `backend/.env.example`, `backend/README.md`, `dist/api-client.js`, `dist/app.js`, `dist/report-gateway.js`, `dist/style.css`, `dist/views/capture.js`, `dist/views/public.js`, `dist/views/staff.js`, `docs/api-contract.md`, this file. `package.json` and the lockfile unchanged.

## 9. Known issues and blockers

1. **Playwright is not part of the project (by decision, see §11).** Each session that runs e2e must install it outside the repo first. Steps (Git Bash; any folder outside the repo works):

   ```sh
   PW="$TEMP/ncl-playwright"            # outside the repository
   mkdir -p "$PW" && (cd "$PW" && npm init -y >/dev/null && npm install playwright@1.63.0)
   npm run dev                           # in a separate terminal; serves http://localhost:4173
   BASE=http://localhost:4173 SHOTS="$TEMP/ncl-shots" PLAYWRIGHT_PATH="$PW/node_modules/playwright/index.js" npm run e2e
   BASE=http://localhost:4173 INSECURE_BASE=http://<lan-ip>:4173 SHOTS="$TEMP/ncl-shots" PLAYWRIGHT_PATH="$PW/node_modules/playwright/index.js" npm run e2e:camera
   ```

   `<lan-ip>` is one of the network addresses `npm run dev` prints. Version 1.63.0 matches the Chromium already cached in `%LOCALAPPDATA%\ms-playwright` (revision 1243). Using a newer Playwright requires `npx playwright install chromium` (a large download). Keep `SHOTS` outside the repo so screenshots are not committed.
2. **Possible Node 20 + Windows issue with `npm test`** [unverified]: the script uses `node --test tests/*.test.js`. On Windows, npm runs scripts through cmd.exe, which does not expand `*`. It works here because Node 21+ expands glob patterns in `--test` itself. On Node 20 this may fail despite the README stating Node 20 is supported. Not tested.
3. **Camera and location cannot be verified in the in-app browser pane** — it blocks device capture by design. Real verification needs a real browser on localhost or a phone over HTTPS.
4. ~~Backend has no CORS yet.~~ **Resolved in 3.3**: explicit allowlist, default `http://localhost:4173`.
5. **Swagger UI needs network**: FastAPI's `/api/v1/docs` page loads its JS/CSS from a CDN. The API and `/api/v1/openapi.json` work offline.
6. ~~Demo public IDs don't match the generated ID format.~~ **Resolved in 3.2**: lookup accepts `NCL-\d{4}-[A-Z0-9]{6}`, covering both; generation still uses the `referenceId()` alphabet. Demo IDs are not seeded into SQLite.
7. README's "Working copy and delivery" section says the prototype "does not synchronise with any Git repository" — now outdated since the project lives on GitHub. Documentation-only; not changed.
8. **No authentication, rate limiting or abuse protection** on `POST /api/v1/complaints` and public tracking. Public IDs have about 30 bits of randomness, so they can be guessed by brute force without rate limits (public projection only). Must be addressed before any public deployment.
9. ~~Frontend payload differences~~ **Handled in 3.3** by `toCreatePayload()`: `owner`/`uploadedAt`/Blob dropped; reports without GPS are refused in backend mode with an explanation (local mode unchanged); the server receipt's ISO `gps.timestamp` displays correctly. Remaining compatibility limit: `services.js` accepts `gps: null` locally but the API does not, so a GPS-less report can only be saved in local mode (the capture flow always supplies GPS in practice).
10. **Intermittent MapLibre console error in `npm run e2e`** (Phase 3.2 run only): `AJAXError: signal is aborted without reason` fetching `https://tiles.openfreemap.org/sprites/.../ofm.json`. The suite still passed 56/56, no frontend file changed, and earlier runs did not print it, so it is a network/timing effect with the external tile server, not a regression. [observed once in 3.2; did not recur in the 3.3 runs; cause unverified]
11. **`verification_log` table is never written yet**; the public `verificationLog` is always `[]` until staff/citizen-response endpoints exist.
12. **Evidence viewing is staff-only.** Signed-in staff can view evidence (every view logged); owners and the public cannot. Future: owner viewing after resident accounts; short-lived signed URLs with object storage; never permanent public links. A copy also stays in the submitting browser.
13. **Backend-mode reports are invisible to the local workspaces.** Resident/admin workspaces, maps and case pages still read local data, so a server report does not appear there; existing local complaints are not migrated (by decision).
14. ~~Ambiguous submissions can duplicate.~~ **Resolved in 3.5** by `Idempotency-Key` (one key per captured report, reused on retry). Remaining edge: if the user edits the report after an ambiguous failure, the server answers `409 idempotency_key_conflict`; the UI then explains that the next attempt is a separate report that may duplicate the earlier one, and issues a new key.
15. **A wrong `baseUrl` path looks like "not found" when tracking.** The API's generic 404 uses the same `not_found` code as a missing complaint, so a misconfigured path (e.g. `/api/v2`) shows the not-found message rather than a configuration error. Submissions to a wrong path fail clearly.
16. **Security limits:** no user authentication; rate limits are in-process (reset on restart, not shared across workers or hosts) and keyed by IP (shared NAT users share a limit; IPv6 clients can rotate addresses within a /64); public IDs remain guessable at 60 lookups/min/IP; `http://` only in development. Backend mode is for development and demos, not public deployment.
17. **Upload authorisation is possession-based, not identity-based.** Anyone holding the receipt's upload token or the claim code can upload the (single) original evidence. Since 3.5 the claim-code exchange and uploads are rate-limited and total storage is capped, but limits are per process and a distributed attacker can still spread requests across many IPs.
23. **Credential re-issue on replay** (by design, see §11): a replay with the same key and identical request within 24 h, before the credentials are used, invalidates the previous claim code and upload token. A client that sends two identical requests concurrently must use the receipt it received last.
24. **Per-complaint token-renewal limit enables a temporary lock-out**: anyone who knows a public ID can exhaust its 10 renewal attempts per hour.
25. **Idempotency rows are never purged** yet; after the 24 h window a key still blocks duplicates but returns no credentials.
26. **Quota reservations for unknown-length uploads** reserve the maximum request size (~200 MB), which can briefly block other uploads when the quota is nearly full.
27. **Staff authentication is development-grade**: one role, no MFA, no password-reset flow, no lockout beyond rate limits; tokens travel over `http://` in development. The per-username login limit lets anyone temporarily block a known staff username (10 attempts per hour).
28. **Access log holds personal data** (staff identity and client IP) and is kept indefinitely; no retention policy yet.
29. **Staff console sessions end on reload** (by design: memory only). Server complaints still do not appear in the local demonstration workspaces, and staff cannot yet change status, assign teams or add repairs on the server.
30. **Supabase not verified against a real project.** PostgreSQL behaviour is verified on a local PostgreSQL 15; Supabase-specific behaviour (pooler modes, SSL, network latency, Storage API errors and limits) is untested until a real project is connected. Storage is tested only against a fake HTTP API.
31. **No Supabase orphan audit and no local → Supabase evidence migration.** If deleting an object fails after a database error, the object stays unreferenced in the bucket (logged with a status code). Moving existing local evidence is a separate future task.
32. **Startup lists the bucket's `evidence/` folders** to measure quota usage; slow for very large buckets. Switching back to local mode makes Supabase evidence unviewable (503) until Supabase is configured again.
33. **Free-plan file size cap (50 MB)** is below the default 200 MB video limit; set `CIVICLENS_MAX_VIDEO_BYTES` accordingly or startup refuses a bucket whose limit is lower.
34. **Timestamps stay ISO text in PostgreSQL** (not `timestamptz`) to keep API output identical to SQLite; date queries on the server would need a later migration.
35. **The migration tool copies data only**: it does not move evidence files, and it expects the PostgreSQL target to be empty or contain only matching rows (conflicts stop it).
36. **Web Push real-device status**: delivery through WNS to Edge on this Windows PC was verified (headless, read back from the service worker); toasts in the Windows notification centre, background delivery with Edge fully closed, Chrome/Firefox desktop and Android phones were not tested. Phones need the frontend over HTTPS.
37. **Citizen inbox token in localStorage**: needed so history and push survive closing the browser; it grants access to that device's notifications only (complaint IDs and short texts), but any script running on the page could read it. Clearing site data loses the inbox (follow again with the claim code).
38. **No push retry queue**: a failed delivery is not re-sent (history keeps it). Subscriptions the browser rotates (`pushsubscriptionchange`) are re-registered only when a CivicLens page is open.
39. **No notification preferences and no case-status events yet**; staff receive every new complaint and evidence notification (fine for a pilot, noisy at scale). Disabled staff are excluded, but a staff browser keeps its device until sign-out, a failed delivery or removal.
40. **Open-source Chromium (and Playwright's ephemeral contexts) cannot subscribe**; use Edge, Chrome, Firefox or Samsung Internet. A browser on an unlisted push service is refused until its host is added to `CIVICLENS_PUSH_ENDPOINT_HOSTS`.
18. **Evidence lifetime in the browser:** the upload context (token, claim code, Blob) lives only in memory while the receipt is open. After a reload or leaving the page, a failed upload cannot be retried from the UI (the complaint and local copy remain; a "retry with claim code" form is not built).
19. **No malware scanning, transcoding or server-side EXIF re-parsing.** Only the leading signature is checked; a file with a valid header and arbitrary content is stored as-is (never executed or served).
20. **Orphan files after a crash:** a crash between the atomic rename and the database insert can leave an unreferenced file in `objects/`; no reconciliation job exists. No backups of the database or evidence folder.
21. **Format coverage:** a real Chromium MediaRecorder WebM was accepted end-to-end. MP4 is verified only with a generated `ftyp` header (no real Safari recording tested). MOV (`qt` brand), HEIC, PNG and Matroska are rejected.
22. **Event loop:** uploads run in an async route; file writes happen on the event loop in 64 KB-class chunks. Acceptable for a development server, not tuned for concurrency.

## 10. Next actions (priority order)

**Next exact step: review and commit `web-notifications` (the user handles Git), then verify Web Push on real devices** (see `backend/README.md` → "Web Push notifications"):

1. Generate VAPID keys (`python -m app.push generate-vapid-keys`) and set them in the API environment; serve the frontend over HTTPS for phones.
2. Windows: in Edge or Chrome, open `#/notifications`, turn notifications on, send a test; confirm the toast in the notification centre; close every browser window and trigger an event (e.g. staff open a followed report's evidence); confirm delivery and that clicking opens the report.
3. Android: the same in Chrome over HTTPS, with the browser closed and the screen off.
4. Staff: sign in on a phone and a computer, turn on notifications on both, submit a report, confirm both receive "New complaint"; sign out on one and confirm it stops.
5. Record results here, separately from the automated results.

Still pending from the infrastructure work: merge `s3-integration` into `develop` (the user skipped it for now) and connect a real Supabase project (database URL, private bucket, service-role key), then run the migration tool only with the user's approval.

After that: Phase 3.7 server-side case management (which also enables case-status notifications), notification preferences, resident accounts, retention policies, shared rate limiting, PostGIS, HTTPS deployment.

Still outstanding (not blocking): physical-device camera/GPS/upload testing over HTTPS (including Safari MP4); Node 20 / Windows glob check.

## 11. Decisions and reasons

- **Cloned into the empty workspace root** rather than a subfolder, so the working directory is the repository. Reason: no existing copy; avoids nested duplicates.
- **Playwright installed outside the repository** (user approved installing it). Reason: keeps `package.json` and the lockfile unchanged and the project dependency-free; the scripts already support `PLAYWRIGHT_PATH`. Version 1.63.0 chosen to reuse the cached Chromium and avoid a large download. **User decision (2026-10-30): keep Playwright outside the repo; do not add it as a devDependency.** Reinstall steps are in §9.
- **Added `.claude/launch.json`** (untracked, local tooling) so the Claude desktop app could start `npm run dev` as a managed preview server. It does not affect the app. Can be deleted or added to `.gitignore` — user's choice.
- **No application code changed in Step 2.** Reason: baseline only.
- **Backend in its own `backend/` folder with its own venv and requirements.** Reason: keeps the frontend dependency-free and untouched; `npm run check` and the Node server never see Python files.
- **Python 3.14.3** for the venv. Reason: default interpreter; FastAPI 0.162 / pydantic 2.14 install as wheels with no build step, and `pip check` is clean. The code uses nothing 3.14-specific; 3.11+ should work (not tested).
- **`httpx2` instead of `httpx` for tests.** Reason: Starlette 1.10 warns that plain `httpx` is deprecated for `TestClient`; the swap removed the warning and all tests still pass.
- **App factory (`create_app()`) with shared error handlers.** Reason: tests build isolated apps, and every future endpoint gets the contract's error envelope automatically.
- **Unhandled errors return a generic 500 message.** Reason: avoids leaking internals (including any future secret) to clients; details go to the server log.
- **No CORS, persistence or auth in 3.1.** Reason: user scoped Phase 3.1 to the foundation only.
- **Phase 3.2 decisions (user, 2026-10-30):** SQLite via standard-library `sqlite3` (no SQLAlchemy), repository layer, no demo seeding, demo-ID compatibility, frontend unchanged.
- **`ComplaintStore` is the only module with SQL and returns plain dicts.** Reason: a PostgreSQL/PostGIS store can replace it without touching routes or models.
- **Schema created in the FastAPI lifespan, not at import.** Reason: importing `app.main` (as tests do) never creates the development database.
- **scrypt (stdlib `hashlib.scrypt`, N=2^14, r=8, p=1, 16-byte salt) for claim codes.** Reason: a standard password-hashing KDF with no new dependency; the codes already carry 122 random bits.
- **Claim code stays a UUID v4.** Reason: same format the browser prototype issues, so the receipt and claim UI need no change.
- **camelCase fields and the `services.js` category list.** Reason: no data-contract change for the frontend. `capturedAt` also accepts `created`, the name the capture screen uses.
- **`owner` and `uploadedAt` rejected; `gps` required.** Reason: no authentication, so a client-supplied owner can't be trusted; upload time belongs to the server; the API contract requires coordinates.
- **Timestamps must carry a timezone and not be in the future (10-minute skew allowed); GPS fix time in epoch ms, stored as ISO.** Reason: rejects malformed or ambiguous times while matching what the Geolocation API produces.
- **Unknown and malformed IDs both return the same 404.** Reason: doesn't reveal which IDs are well-formed, and never queries the DB with junk.
- **Storage failures return `503 storage_unavailable` with a generic message.** Reason: tells clients to retry without exposing SQL or file paths; full details go to the server log.
- **Live verification used a scratch database** (via `CIVICLENS_DB_PATH`). Reason: avoid leaving test complaints in `backend/data/`.
- **Phase 3.3 decisions (user, 2026-10-30):** local default, opt-in backend mode, metadata only, evidence in IndexedDB, server receipt shown once, backend tracking in backend mode, no migration.
- **Separate `api-config.json` instead of extending `map-config.json`.** Reason: `map-config.json` is the geocoder's config read by `geo.js`; a dedicated file keeps the switch obvious and independent.
- **Invalid config blocks instead of falling back.** Reason: falling back to local would make a user who asked for backend mode think reports reached the server; switching to backend would contradict "local by default". Missing file is the only silent case, and it means local.
- **Gateway module rather than branching inside `services.js`.** Reason: `services.js` and its 41 tests stay untouched; local mode literally runs the same calls as before.
- **Claim code held in a module variable and consumed once.** Reason: shows it exactly once without persisting it; reloading cannot reveal it again.
- **Server evidence keyed `server-evidence:<id>`.** Reason: server and local IDs share an alphabet; a separate prefix makes overwriting a local report's evidence impossible.
- **POST never retried and connection failures reported as "Not confirmed".** Reason: the first request may have been saved; an automatic retry could duplicate the complaint.
- **Response allowlists in the client.** Reason: defence in depth — even if the server ever leaked a field, the public UI would not render it.
- **`scripts/backend-e2e.mjs` intercepts `api-config.json` in the browser.** Reason: tests backend mode without editing a committed file; run with `node` because `package.json` must not change.
- **Phase 3.4 decisions:** local filesystem storage behind an interface; upload authorised by a receipt-issued, short-lived token renewable with the claim code; evidence private by default; upload and retrieval separated (retrieval deferred rather than weakening access control).
- **Upload token instead of sending the claim code with every upload.** Reason: the claim code is the long-lived secret; a short-lived, revocable token limits exposure, and only its hash is stored. The claim code is still the recovery path when the token expires.
- **SHA-256 (unsalted) for upload tokens, scrypt for claim codes.** Reason: tokens are 256-bit random values, so a slow salted hash adds nothing; claim codes are the user-held secret and keep scrypt.
- **Streaming parser (`python-multipart` low-level API) rather than `UploadFile`.** Reason: limits, signature checks and authorisation must happen while reading, not after the whole body is spooled.
- **Signature first, then declared type/extension must agree.** Reason: browser MIME types and filenames are untrusted; mismatches are rejected rather than silently trusted or corrected.
- **One original per complaint; identical retry returns the existing record, different file → 409.** Reason: makes retries safe after ambiguous failures without letting a token holder replace evidence.
- **File committed before the DB row; row failure deletes the file.** Reason: a row must never point at a missing file; the only remaining gap (crash between the two) leaves an unreferenced private file, documented as issue 20.
- **Storage refused inside `dist/`.** Reason: makes accidental public hosting of evidence impossible through configuration.
- **Frontend keeps the local copy after upload and holds token/claim in memory only.** Reason: nothing is lost if the upload or server fails; no secret is persisted in browser storage.
- **Phase 3.5 decisions:** in-process limiter behind an interface (no Redis); 507 for quota exhaustion; dry-run-first audit tool.
- **Receipt recovery by explicit credential re-issue, not encrypted replay.** Reason: the server keeps only hashes of claim codes and tokens, so it cannot repeat the original receipt. Storing them encrypted would need a new dependency (`cryptography`, since the standard library has no authenticated encryption) plus server key management and rotation — the substantial secret-management design the brief said to avoid without discussion. Re-issue stores nothing secret; it requires the client-held random key and the identical request, applies only to credentials never used (`credentials_used_at`) and within 24 h, and invalidates the previous ones. **User decision (2026-10-30): keep re-issue; do not switch to encrypted replay.**
- **Per-complaint evidence limit counted after authorisation; per-IP limit before.** Reason: counting unauthenticated attempts per complaint would let strangers block a complaint's uploads.
- **Token-renewal per-complaint limit counted before verification.** Reason: its purpose is to bound scrypt cost; the lock-out risk is documented (issue 24).
- **Usage = max(DB total, bytes on disk) at startup.** Reason: missing or inconsistent metadata must not make the quota too generous.
- **`python -m app` launcher with uvicorn proxy headers off.** Reason: uvicorn's default trust of 127.0.0.1 forwarding headers bypassed per-IP limits (found live); the app's own trusted-proxy logic is the single place that reads forwarded headers.
- **Test suites run with `CIVICLENS_RATE_LIMITS=off`** (pytest conftest; documented for `backend-e2e.mjs`). Reason: they create many complaints from one address; limits have their own deterministic tests and a live check with defaults.
- **Phase 3.6 decisions (user, 2026-10-30):** CLI-created staff accounts; bearer token in memory; staff-only evidence viewing with audit log; staff list + viewer in the frontend. Keep idempotent re-issue.
- **Separate `#/staff` console instead of reusing the demo admin sign-in.** Reason: the demo session lives in `sessionStorage` and drives the local workspaces; mixing a server token into it would either persist the token or break local mode. A separate in-memory session keeps both intact.
- **Bearer tokens, not cookies.** Reason: CORS stays credential-free, so no CSRF handling is needed; the cost is signing in again after a reload.
- **Audit log written before streaming; no log, no bytes.** Reason: every disclosed view must be accountable; a view that cannot be recorded is not served.
- **Dummy scrypt check for unknown usernames and one generic 401.** Reason: avoids revealing which usernames exist through messages or timing.
- **Login limits counted before the password check, per IP and per username.** Reason: bounds scrypt cost and slows guessing across addresses; the lock-out risk is documented (issue 27).
- **Supabase Storage instead of Cloudflare R2 (user decision, 2026-10-30; R2 had replaced AWS S3 earlier the same day).** Reason: one provider for database and files. Only `local` and `supabase` exist; no multi-provider layer. The R2 decisions below are kept as history; their code is removed.
- **`storage3` instead of the full `supabase` package; streamed downloads via its HTTP client.** Reason: official client without realtime/auth dependencies; `download()` would buffer whole videos in memory.
- **Service-role key, no storage policies, private bucket enforced at startup.** Reason: only the backend touches the bucket; staff authorisation and audit stay in FastAPI.
- **psycopg 3 with a thin `db.py` layer, no ORM.** Reason: keeps the existing hand-written SQL and behaviour; one place handles placeholder and transaction differences.
- **Timestamps kept as ISO text in PostgreSQL.** Reason: byte-identical API output and a trivial data migration; native timestamps can come later.
- **Web Push with VAPID via pywebpush, no Firebase SDK.** Reason: standards-based, works with every browser push service (FCM, Mozilla, WNS, Apple) and needs no third-party account; payloads are end-to-end encrypted.
- **History first, push second; events in background tasks.** Reason: a notification is never lost because a device is offline, and a push or database problem never fails the citizen's or staff member's request.
- **Citizen inbox tokens + claim-code follows instead of accounts.** Reason: there are no resident accounts on the server yet; the claim code already proves ownership of a report, and the inbox token grants nothing beyond notifications.
- **Endpoint allowlist.** Reason: the server makes HTTP requests to client-supplied URLs; limiting them to known push services prevents server-side request forgery.
- **Service worker does push only (no caching).** Reason: avoids changing the app's loading behaviour and stale-cache risks; offline support is a separate decision.
- **Migration tool: dry run default, read-only source, one transaction, validation, conflicts stop.** Reason: the user must approve any real migration, and a partial or silent overwrite must be impossible.
- **Backend recorded per evidence row (schema 5) rather than one global setting.** Reason: switching backends must not strand existing evidence; no automatic migration.
- **Streaming retrieval kept; no presigned URLs.** Reason: preserves staff-only access and view-level audit logging, and avoids shareable bearer links and bucket CORS; cost is API bandwidth.
- **Explicit `R2_*` credentials passed to boto3, region `auto`.** Reason: avoids silently using AWS credentials or profiles on the machine; R2 requires region `auto`.
- **Spool then upload, then `head_object` confirmation.** Reason: the upload route's streaming checks stay unchanged; only a confirmed object counts as stored, and mismatches are deleted.
- **moto (plus Stubber) for tests.** Reason: exercises real boto3 request paths against a fake S3 API on an R2-style endpoint, with no account, network or cost.

## 12. Commands executed and results

| Command | Result |
| --- | --- |
| `git status` (before clone) | `fatal: not a git repository` — workspace empty |
| `git clone https://github.com/hemanth16-cmd/namma-civiclens.git .` | Success |
| `git branch --show-current` / `git rev-parse HEAD` | `main` / `a40afe65f204856896df9f0ece7d7e36832a036e` |
| `git status --short` | Empty (clean) |
| `node -v` / `npm -v` | `v25.8.1` / `11.11.0` |
| `require.resolve('playwright')` | Not resolvable |
| `npm test` | 41 pass, 0 fail, exit 0 |
| `npm run check` | Syntax check passed for 28 files, exit 0 |
| `npm run dev` (via desktop preview server) | Running at http://localhost:4173 (LAN addresses also printed) |
| `curl` probes on `/`, `/app.js`, `/style.css`, `/map-config.json`, `/views/capture.js`, `/nope.js` | 200, 200, 200, 200, 200, 404 |
| Browser pane: `/`, `#/track/NCL-2026-DEMO01`, `#/home` | Rendered; no console errors; claim code absent from public page |
| `npm install playwright` (in scratchpad) | 1.64.1 installed; needs Chromium 1248, not cached |
| `npm install playwright@1.63` (in scratchpad) | 1.63.0; headless Chromium launch OK (153.0.8010.12) |
| `BASE=http://localhost:4173 PLAYWRIGHT_PATH=… npm run e2e` | 56/56 passed, exit 0 |
| `BASE=http://localhost:4173 INSECURE_BASE=http://10.1.33.69:4173 PLAYWRIGHT_PATH=… npm run e2e:camera` | 48/48 passed, exit 0 |
| **Phase 3.1** | |
| `python --version`; `py -0` | 3.14.3 default; 3.11, 3.10 also present |
| `py -3.14 -m venv backend/.venv` | Created |
| `pip install fastapi uvicorn pytest httpx` (in venv) | fastapi 0.162.0, uvicorn 0.54.0, pytest 9.2.0, httpx 0.28.1 |
| `pytest -v` (first run) | 7 passed, 1 warning (Starlette: use `httpx2` instead of `httpx`) |
| `pip install httpx2`; `pip uninstall httpx httpcore`; `pip install -r requirements-dev.txt`; `pip check` | httpx2 2.16.0; no broken requirements |
| `pytest -v` | 7 passed, 0 warnings |
| `python -m uvicorn app.main:app --app-dir backend --port 8000` (desktop preview server) | Running on 127.0.0.1:8000 |
| `curl /api/v1/health`, `/api/v1/nope`, `POST /api/v1/health`, `/api/v1/openapi.json` | 200 JSON, 404 envelope, 405 envelope, 200 |
| Browser pane `http://localhost:8000/api/v1/docs` | Swagger UI loaded; lists `/api/v1/health` |
| `npm test` / `npm run check` | 41/41 pass / 28 files pass |
| `npm run e2e` / `npm run e2e:camera` (external Playwright) | 56/56 / 48/48 pass |
| `git status --short` / `git diff --stat` | `?? .claude/`, `?? backend/`, `?? docs/IMPLEMENTATION_PROGRESS.md` / empty |
| **Phase 3.2** | |
| `python -c "import sqlite3, hashlib; ..."` (venv) | Python 3.14.3, SQLite 3.50.4, `hashlib.scrypt` available |
| `pytest -q` | 51 passed in ~2.2 s |
| Mutation: add `claimHash` to `PublicComplaint`, run `pytest tests/test_complaints.py`, revert | 2 failed, 42 passed (leak caught); after revert 51 passed |
| `CIVICLENS_DB_PATH=<scratch>/live.sqlite3 python -m uvicorn app.main:app --port 8000` | Started |
| `curl -X POST /api/v1/complaints` (valid body) | 201, `cache-control: no-store`, `location: /api/v1/complaints/NCL-2026-UD6SFL` |
| `curl /api/v1/complaints/NCL-2026-UD6SFL` | 200 public projection (no claim, GPS, description) |
| `curl /api/v1/complaints/NCL-2026-DEMO01` (not in DB) | 404 `not_found` |
| `curl -X POST` invalid (bad category, lat 95, `owner`) | 422 with 3 field details |
| grep claim code in public response / server log | 0 / 0 |
| Stop server, restart on same DB, GET same ID | 200, same record |
| `npm test` / `npm run check` | 41/41 / 28 files |
| `npm run e2e` / `npm run e2e:camera` (external Playwright) | 56/56 (one MapLibre console error printed, see §9) / 48/48 |
| `git status --short`; `git diff --stat`; `git check-ignore` | only untracked `.claude/`, `backend/`, this file; diff empty; `backend/data/*.sqlite3`, `-wal`, `.venv`, `.env` ignored |
| **Phase 3.3** | |
| `pytest -q` (after CORS) | 66 passed |
| `node --test tests/api-integration.test.js` | 26/26 |
| Mutation: forward `owner` in mapper + local fallback in backend tracking | 3 tests failed (caught); restored → 26/26 |
| `CIVICLENS_DB_PATH=<scratch>/integration.sqlite3 python -m uvicorn app.main:app --port 8000` | Started |
| `BASE=… API=… PLAYWRIGHT_PATH=… node scripts/backend-e2e.mjs` (1st run) | 28/30: two test-script issues (storage check matched the router's route history; local receipt read before render). Fixed the checks, not the app |
| `node scripts/backend-e2e.mjs` (final) | 30/30; created and tracked `NCL-2026-6MCGT2` |
| Server log grep for UUIDs | none |
| `npm test` / `npm run check` | 67/67 / 33 files |
| `npm run e2e` / `npm run e2e:camera` | 56/56 / 48/48 |
| `git diff --stat`; `git diff --quiet -- package.json package-lock.json` | only `dist/views/capture.js`, `dist/views/public.js` modified among tracked files; package files unchanged |
| **Phase 3.4** | |
| `pytest -q`; `node --test tests/*.test.js`; `node scripts/check.mjs` (baseline) | 66 / 67 / 33 files |
| `pip install python-multipart` (venv) | 0.0.32; pinned in `requirements.txt` |
| `pytest -q tests/test_evidence.py` (first run) | 50 passed, 2 failed: test data used capture times on 2026-10-30, which is in the future for the machine's UTC clock (2026-10-29). Fixed the test dates, not the code |
| Mutation: disable token status check | 2 tests failed (caught); restored → 118 passed |
| `node --test tests/evidence-upload.test.js` | 16/16; mutation (always retryable + delete local copy after upload) → 3 failed; restored → 16/16 |
| `CIVICLENS_DB_PATH=<scratch> CIVICLENS_EVIDENCE_DIR=<scratch> python -m uvicorn app.main:app --port 8000` | Started |
| `EVIDENCE_DIR=<scratch> node scripts/backend-e2e.mjs` (1st run) | 44/45: a check flagged `contentType` ("image/jpeg") as a path because it contains "/". Fixed the check |
| `node scripts/backend-e2e.mjs` (final) | 45/45; WebM video 167,331 bytes accepted |
| grep server log for UUIDs / `Bearer` / filenames; `ls tmp/`; count `objects/` | 0 / 0 files / 6 files for 6 uploads over two runs |
| Final: `pytest`, `npm test`, `npm run check`, `npm run e2e`, `npm run e2e:camera` | 118 / 83 / 34 files / 56 / 48 |
| **Phase 3.5** | |
| `pytest -q`; `node --test tests/*.test.js`; `node scripts/check.mjs` (baseline) | 118 / 83 / 34 files |
| `pytest -q` after wiring | 117 passed, 1 failed: migration test expected schema 2 (intended change to 3); test updated to expect 3 and check the new table/column |
| `pytest tests/test_ratelimit.py` (first run) | 13 passed, 3 failed: injected limiter replaced because an empty limiter is falsy (real bug in `create_app`); fixed → 16 passed |
| `pytest tests/test_quota.py tests/test_idempotency.py` | 27 passed, 1 failed: test client cannot send a non-ASCII header; case replaced by a non-URL-safe base64 key → 28 passed |
| `pytest tests/test_evidence_audit.py` (first run) | 6 passed, 2 failed: snapshot included SQLite `-wal/-shm` files created by read-only opens; snapshot now excludes them, DB rows and main DB file still compared → 8 passed |
| Mutations: trust XFF always / skip credentials-used check / never release reservations | 2 / 2 / 5 tests failed (all caught); restored → 170 passed |
| `node --test tests/idempotency.test.js` (first run) | 7/8: 507 treated as retryable (real bug); fixed → 8/8; all frontend 91/91 |
| `CIVICLENS_RATE_LIMITS=off … python -m uvicorn …`; `node scripts/backend-e2e.mjs` | 52/52; DB: 5 complaints, 5 keys, 1 replay |
| Live default-limits check (scratch script, `uvicorn` without `--no-proxy-headers`) | 8/11: 2 header-case bugs in the script, and **a real bypass**: spoofed `X-Forwarded-For` got 201 after 429 |
| Added `python -m app` (proxy headers off); live check again | 11/11; Retry-After 586; spoof → 429 |
| `python -m app.evidence_audit --db <e2e> --evidence-dir <e2e>` | dry-run: 5 recorded, 5 on disk, 0 orphans, 0 missing; exit 0 |
| Final: `pytest`, `npm test`, `npm run check`, `npm run e2e`, `npm run e2e:camera`, `backend-e2e.mjs` (via `python -m app`) | 170 / 91 / 35 files / 56 / 48 / 52 |
| **Phase 3.6** | |
| `pytest -q` after wiring | 168 passed, 2 failed: CORS test used DELETE as a disallowed method (now allowed for sign-out) and migration test expected schema 3; both updated for the intended change → 170 passed |
| `pytest -q tests/test_staff.py` | 29 passed |
| Mutations: ignore disabled accounts / skip audit log / leak claimHash | 0 / 2 / 1 failures — first not caught; added a direct-disable test → caught; restored → 200 passed |
| `node --test tests/staff.test.js`; all frontend; `node scripts/check.mjs` | 9/9; 100/100; 38 files |
| `python -m app.staff --db <scratch> create --username e2e.staff --password-stdin` (random password from a file, never printed) | Created; `list` shows active |
| `STAFF_USERNAME=… STAFF_PASSWORD=… node scripts/backend-e2e.mjs` (API: `python -m app`, limits off, scratch DB) | 66/66; password found 0 times in the e2e and server logs |
| `npm run e2e` / `npm run e2e:camera` (first attempt after resuming) | failed to connect: frontend dev server had stopped (HTTP 000) |
| Restarted the frontend dev server; `npm run e2e` / `npm run e2e:camera` | 56/56 / 48/48 |
| Final: `pytest`, `npm test`, `npm run check`, `npm run e2e`, `npm run e2e:camera`, `backend-e2e.mjs` | 200 / 100 / 38 files / 56 / 48 / 66 |
| **Storage task (R2)** | |
| `git branch --show-current`; `git status --short` | `s3-integration`; clean (checked again when the target changed to R2) |
| `pytest -q`; `node --test tests/*.test.js`; `node scripts/check.mjs` (baseline) | 200 / 100 / 38 files |
| `pip install boto3 "moto[s3]"` (venv) | boto3 1.43.122, moto 5.2.5; `pip check` clean |
| `pytest -q` after the schema/storage wiring | 199 passed, 1 failed: migration test expected schema 4 (intended change); updated to 5 with a `storage_backend` default check → 200 |
| moto probe with `MOTO_S3_CUSTOM_ENDPOINTS` on an R2-style endpoint | bucket creation fails under region `auto` (test-setup only; R2 buckets are created in the dashboard) → tests create the bucket with a setup client; 10 MB multipart upload, metadata and 404 for a missing bucket work under `auto` |
| `pytest -q tests/test_r2_storage.py` | 42 passed |
| Mutations: read from active backend / skip size confirmation / default AWS credential chain | 1 / 1 / 1 failures (all caught); restored → 242 passed |
| `npm run e2e`, `npm run e2e:camera` (first attempt) | frontend dev server was down (HTTP 000) → restarted → 56/56, 48/48 |
| `node scripts/backend-e2e.mjs` (API local mode, CLI-created staff account) | 66/66; scratch DB schema 5, 4 evidence rows all `local` |
| `EVIDENCE_STORAGE_BACKEND=r2 … python -m app` with missing keys; `EVIDENCE_STORAGE_BACKEND=s3` | `StorageConfigError` naming `R2_ACCESS_KEY_ID`/`R2_SECRET_ACCESS_KEY` (secret not printed); `must be "local" or "r2"` |
| **Supabase Storage + PostgreSQL tasks** | |
| `pip install storage3==2.33.0 "psycopg[binary]==3.3.6" psycopg-pool==3.3.3`; `pip uninstall boto3 moto` (venv) | installed / removed; `pip check` clean |
| `docker run --rm -d --name civiclens-pgtest -p 127.0.0.1:55432:5432 postgres:15-alpine` (throwaway, stopped at the end) | PostgreSQL 15 on localhost only |
| `CIVICLENS_TEST_POSTGRES_URL=… pytest -q` | 256 passed |
| `CIVICLENS_TEST_DATABASE=postgres CIVICLENS_TEST_POSTGRES_URL=… pytest -q` | 243 passed, 5 skipped, but run with `--ignore=tests/test_evidence_audit.py`; without it, 7 audit tests errored (they read SQLite directly). Audit tests made backend-neutral (`dbutil.connect`, a `db` fixture; the missing-file test marked SQLite-only); a mutation of the PostgreSQL audit query is caught (3 failures) → **250 passed, 6 skipped** |
| `pytest -q` (no PostgreSQL) | 240 passed, 16 skipped |
| Mutations: GPS as REAL / skip setval / allow public bucket / skip upload confirmation / read from active backend | all caught; restored |
| `python -m app.db_migrate --sqlite <e2e copy>` dry run / `--execute` / rerun against Docker DB `civiclens_migrated` | first attempt crashed printing `→` on the cp1252 console → replaced with `->`; then 23 rows planned / inserted / 0 inserted (all present), validation OK, exit 0; source hash unchanged |
| `DATABASE_BACKEND=postgres … python -m app --port 8010` + `backend-e2e.mjs` | 66/66; port 8000 was taken by another local project, left alone |
| `npm test`; `npm run check`; `npm run e2e`; `npm run e2e:camera` | 100/100; 38 files; 56/56; 48/48 |

| **Web Push task (branch `web-notifications`)** | |
| `git fetch --all --prune`; `git branch -a -vv`; `git merge-base --is-ancestor f49d30f origin/develop` | `s3-integration` = `origin/s3-integration` = `f49d30f`; `develop` = `29e5bbc` (does NOT contain it); tree clean |
| `git switch -c web-notifications s3-integration` (user authorised; told to skip the develop merge) | branch created at `f49d30f`; no upstream |
| `pip install pywebpush` (venv) | pywebpush 2.5.0 (py-vapid 1.9.6, http-ece 1.2.1, aiohttp 3.14.5 …); `pip check` clean |
| `pytest tests/test_notifications.py` | 51 passed (after fixing 3 test/mapping issues: CLI flag, idempotent body reuse, key errors → rejected) |
| `test_postgres.py` after schema 6 | 4 errors: the migration fixture requires every table populated → fixture now follows and registers a device → 17 passed (incl. new 5 → 6 upgrade test) |
| Push probes: headless shell / Chromium / Edge, ephemeral vs persistent profile | ephemeral: "Registration failed - permission denied" everywhere; persistent: Chromium `jmt17.google.com`, Edge `wns2-pn1p.notify.windows.com` |
| `node scripts/notifications-e2e.mjs` (API on 8010 with throwaway VAPID keys, SQLite scratch DB, Edge headless) | 36/36; API log contains no VAPID private key, staff password, endpoints, keys or bearer tokens |
| `npm run e2e`; `npm run e2e:camera`; `backend-e2e.mjs` (same API) | 56/56; 48/48; 66/66 |
| Full backend (3 modes); `npm test`; `npm run check` | 308; 302 + 6 skipped; 291 + 17 skipped; 121/121; 44 files |


| **Phase 4: Deep Stress Testing, AppSec Review & Sustained Endurance** | |
| `git fetch origin`; fast-forward local `develop` to `0239786` | remote PR #2 and PR #3 successfully merged into develop; local develop up to date | [verified] |
| Concurrency stress (`tests/test_deep_stress.py`): 50 concurrent complaints, 100 concurrent tracking, 20 concurrent streaming uploads, 50 mixed DB workers | 7 tests pass; 0 lock timeouts, 0 data races, atomic metadata and quota reservations | [verified] |
| Multilingual & Unicode stress: Kannada, Tamil, Telugu, Hindi, Malayalam, Japanese, Arabic RTL, emojis, 1000-char boundary | All persisted and round-tripped without truncation, mojibake, or encoding corruption | [verified] |
| Rate-limiter memory bounds: 50,000 distinct client keys | In-memory limiter bounds memory (LRU / size-bounded), zero runaway heap growth | [verified] |
| Quota concurrency race: 10 threads racing for 50 KB remaining quota | Exactly 1 succeeds and commits; 9 fail cleanly with 507; 0 orphaned files, quota usage exact | [verified] |
| AppSec review (`tests/test_security_review.py`): IDOR recipient isolation, staff auth & session revocation, tracking secrecy, error envelope sanitization, media exploit rejection, streaming security headers, SQL injection immunity, SSRF prevention | 10 tests pass; zero token or GPS leaks in public tracking, malicious SVG/HTML/PHP rejected, nosniff/sandbox headers active | [verified] |
| PostgreSQL migration stress (`tests/test_migration_stress.py`): Full 12-table SQLite-to-PostgreSQL migration under load | Dry run writes 0 rows; execute achieves 100% column match and valid FKs; sequences safely advanced; rerun idempotent; conflict stops migration | [verified] |
| Web Push stress (`tests/test_push_stress.py`): 410 Gone immediate deletion, 5-failure pruning, unread concurrency, zero payload leakage | 4 tests pass; devices pruned correctly, unread counts exact, no private data leaked in push payloads | [verified] |
| Frontend security gateway (`tests/security-gateway.test.js`): Safe API URLs, idempotency key entropy, server evidence prefixing, config network rejection | 4 tests pass (125 total frontend unit tests) | [verified] |
| 5-minute sustained endurance test (`scripts/load-stress-harness.py --sustained --duration 300`) | 29,527 requests executed, 98.4 req/s throughput, 0 errors (100% success rate), p50 24.56ms, p95 145.47ms, peak RSS 47.5 MB | [verified] |
| Final test suite results: Pytest (SQLite) | 330/330 passed | [verified] |
| Final test suite results: Pytest (PostgreSQL) | 324/330 passed (6 SQLite-only PRAGMA tests safely skipped) | [verified] |
| Final test suite results: Frontend unit tests (`npm test`) | 125/125 passed | [verified] |
| Final test suite results: Syntax check (`npm run check`) | 45/45 files passed | [verified] |
| Final test suite results: Browser smoke (`npm run e2e`) | 56/56 checks passed | [verified] |
| Final test suite results: Camera E2E (`npm run e2e:camera`) | 45/45 checks passed | [verified] |
| Final test suite results: Backend integration (`scripts/backend-e2e.mjs`) | 66/66 checks passed against live API | [verified] |
| Final test suite results: Web Push E2E (`scripts/notifications-e2e.mjs`) | 36/36 checks passed with live WNS delivery | [verified] |

Working tree now (branch develop): Clean after committing Phase 4 stress suites, AppSec review, and updated documentation.
