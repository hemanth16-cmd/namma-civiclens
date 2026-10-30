# Namma CivicLens — Core platform development: audit and plan

Branch `feature/core-platform-development`, created from `origin/integration/v52-fullstack` at `dca458e`
(2026-10-30). Status: **plan approved; Workstream 1 implemented and awaiting review** (see §12). Workstreams 2–4 not started.

Scope: our four workstreams only — (1) server-side complaint management, (2) duplicate detection,
(3) geospatial intelligence, (4) security and product hardening. Out of scope and not touched:
Ruchika's Priority Engine, Knowledge Graph, FCM, Monte Carlo, predictive analytics and notification
infrastructure; the other developer's S3-compatible storage and Telegram bots.

---

## 1. Git setup (verified)

| Check | Result |
|---|---|
| Location | `P:\NammaCivicLens\.claude\worktrees\namma-civiclens-integration-9be298` (a git worktree) |
| Branch before | `integration/fullstack-reconciliation` |
| Working tree | Clean apart from one untracked file, `PR_BODY.md`, which Claude wrote for the earlier reconciliation PR. Branch switches do not affect it. |
| Fetch | `origin/integration/v52-fullstack` moved `349498f → dca458e` ("Change API config to backend mode with new URL") |
| New branch | `feature/core-platform-development` = `dca458ef5abd658417ca954ae0b32cebbdea71a8` = `origin/integration/v52-fullstack` (verified equal) |
| Original branch | Untouched. No local copy was modified and nothing was pushed. |

The prescribed `git switch integration/v52-fullstack && git pull` was replaced by
`git switch -c feature/core-platform-development --no-track origin/integration/v52-fullstack`, which
gives the same starting commit without needing a local copy of the branch.

**Urgent finding outside our scope: `main` has lost its frontend.** Commits `5e59f8d` and `015fee1`
("ignore build output dist/ folder") delete all 57 files in `dist/` (8,939 lines) and add `dist/` to
`.gitignore`. `dist/` is not build output: it is the hand-written frontend, and there is no build
step. `main` no longer has a working app. The fix is to revert those two commits on `main` (needs the
repository owner's decision). Our branch starts from `integration/v52-fullstack`, which still has `dist/`.

---

## 2. Repository architecture (verified against source)

### Frontend: `dist/` (build-free ES modules, about 50 files)
- Hash router (`router.js`), views in `dist/views/`, service worker, PWA manifest.
- Two modes, chosen by `dist/api-config.json`. **This branch now ships `backend` mode** pointing at
  `https://namma-civiclens.onrender.com/api/v1` (live; `/health` returns ok).
- **Server-mode screens:** capture and submit (`capture.js`), receipt, public tracking
  (`public.js → renderServerTrack`), staff console (`staff.js`: sign-in, list, detail, evidence viewer,
  access log; **read-only**), notifications (`notifications.js`).
- **Local-mode-only screens** (browser storage): workspace overview, complaints and work orders
  (`workspace.js`), issue detail (`issue.js`), map (`map.js`, client-side MapLibre/Leaflet clustering),
  intelligence page (`intelligence.js`, which is Ruchika's area).
- `dist/services.js` (local `api`) already implements the *behaviour* we need on the server:
  `updateCase`, `addWorkOrder`, `requestReinspection`, `addRepair`, `citizenResponse`, `claim`. It works
  as an executable spec. `assessPriority` / `PRIORITY_WEIGHTS` there is Ruchika's and will not be touched.

### Backend: `backend/app/` (FastAPI, 4,720 lines)
- `main.py` app factory; routes under `/api/v1`. Routers: `complaints.py`, `evidence.py`,
  `staff_api.py`, `notifications_api.py`.
- **`store.py` is the only module with SQL** (one `ComplaintStore` class, about 1,170 lines). `db.py`
  hides the SQLite/PostgreSQL differences (`?` placeholders, `conn.transaction(immediate=True)`,
  `for_update` row locks on PostgreSQL).
- Migrations: numbered. SQLite uses `MIGRATIONS` + `PRAGMA user_version`; PostgreSQL uses
  `POSTGRES_SCHEMA` + `POSTGRES_MIGRATIONS` + the `civiclens_schema` table, guarded by an advisory lock.
  **Current version 6 on both**, and the live Supabase database is at 6.
- Auth (`auth.py`): staff username + scrypt password, opaque bearer session token (hash stored), 8 h
  TTL, revocation on sign-out, password change and account disable. **One role only: `official`**
  (`CHECK (role IN ('official'))`). No citizen accounts. Citizens prove ownership with the one-time
  **claim code** (hash stored); upload tokens and notification follows already reuse it.
- Evidence (`evidence.py`, `evidence_storage.py`, `supabase_storage.py`): streamed upload with
  content-sniffing, size limits and SHA-256; an `EvidenceStorage` interface with `local` and
  `supabase` adapters; per-row `storage_backend`; staff-only streaming reads with an access log.
  `evidence.role` is `CHECK (role IN ('original'))` with `UNIQUE (complaint_pk, role)`.
- Abuse controls: in-memory sliding-window rate limits (`ratelimit.py`), storage quota, idempotency
  keys, a trusted-proxy client-IP rule.
- Notifications (`notifications.py`, `push.py`): `Notifier` with event hooks (`complaint_created`,
  `evidence_uploaded`, `evidence_viewed`, …), inbox tokens, Web Push.

### Database (Supabase, checked read-only)
- PostgreSQL **17.11**. 13 tables in `public`, all owned by the backend's role.
- **12 complaints, all `Submitted / Pending Verification`**: nothing has ever changed a status,
  because the server has no endpoint that can. 10 evidence rows (8 in Supabase Storage, 2 local).
- Extensions *available but not installed*: `postgis 3.3.7`, `pg_trgm`, `earthdistance`, `cube`,
  `btree_gist`, `fuzzystrmatch`, `unaccent`.
- **RLS is off on every table, but `anon` and `authenticated` hold no privileges on any of them**, so
  the public Data API cannot read or write them today. Hardening item in §8.

### Deployment
- Frontend on Vercel (static `dist/`). API on Render (`namma-civiclens.onrender.com`). `render.yaml`
  exists only on `integration/fullstack-reconciliation`, not on this branch.

### Documentation vs code
| Claim | Verdict |
|---|---|
| `docs/api-contract.md` §3 staff operations (`PATCH /complaints/{id}`, work orders, reinspections, repairs, citizen responses) | **Proposed only**; none exist in the backend |
| `docs/api-contract.md` §6 verification rules | Implemented in the **local frontend only** (`services.js`) |
| `docs/api-contract.md` §4 map queries | Not implemented; map clustering is client-side |
| `docs/api-contract.md` §8 `incidents/link-suggestions` (duplicates) | Proposed; not implemented |
| `backend/README.md` "Not included yet" (staff case updates, work orders, PostGIS, shared rate limiting) | Accurate |
| `IMPLEMENTATION_PROGRESS.md` "Supabase not yet tested against a real project" | **Out of date**: the live Supabase database and bucket hold real data |
| `verification_log` table | Exists and is returned publicly, but **no code writes to it** |

---

## 3. Baseline tests (executed on `dca458e`)

| Suite | Result |
|---|---|
| `npm test` | **144 passed, 1 failed** (145). Failure: `tests/api-integration.test.js:98` "the shipped api-config.json selects local mode". **Existing failure** caused by `dca458e` switching the shipped config to backend mode. Not modified. It needs a team decision: either the test changes to accept the deployed config, or the deployed URL moves to deployment configuration. |
| `npm run check` | pass (52 files) |
| Backend pytest, SQLite | **312 passed, 18 skipped** (the skipped tests need PostgreSQL) |
| Backend pytest, PostgreSQL 15 (temporary Docker container, removed afterwards) | **324 passed, 6 skipped** (the skipped tests are SQLite-only, and they pass in the SQLite run) |

The browser end-to-end suites (`backend-e2e` 66/66, notifications 36/36, smoke 56/56, photo-stamp 45/45,
camera 45/45) passed earlier today on the identical frontend and backend code
(`integration/fullstack-reconciliation`). They were not re-run for this audit because only
`api-config.json` differs, and `backend-e2e` overrides that file anyway.

---

## 4. Workstream assessment

| | Exists | Missing | Reuse |
|---|---|---|---|
| **1 Complaint management** | `status`, `verification`, `department` columns with initial values; public `complaint_timeline`; `verification_log` table (unused); staff auth; read-only staff console; claim-code verification; local-mode rules in `services.js` | Any write path for status, verification or department; transition rules; RBAC beyond one role; departments; assignments; work orders; internal notes; citizen confirm/dispute API; audit log; frontend staff actions in server mode | `ComplaintStore` transactions and row locks; `claims.py`; `auth.current_staff`; `ratelimit` rules; `Notifier` hooks; evidence pipeline for after-repair photos |
| **2 Duplicate detection** | Required GPS on every complaint (`gps_lat/lng NOT NULL`, accuracy, source type); category; timestamps; evidence SHA-256 | Candidate search, scoring, review queue, merge records, canonical reference | Store queries; staff auth; audit log from WS1 |
| **3 Geospatial** | lat/lng on every row; `area` (reverse-geocoded neighbourhood, Photon); client-side map clustering | Server-side nearby/radius queries, clustering, recurring-location summaries; **no ward boundaries anywhere** | Same filters as the staff list; WS2 distance function |
| **4 Hardening** | scrypt passwords; session TTL/revocation; strict CORS (a foreign origin gets 400 in production); allow-listed response models; sanitised 500s; upload sniffing and limits; idempotency; rate limits; private evidence + audit | RBAC; durable/shared rate limiting; RLS defence in depth; docs exposure in production; backup/retention policy; monitoring | Existing test patterns (`test_security_review.py`, `test_deep_stress.py`) |

---

## 5. Workstream 1 — Server-side complaint management (first priority)

### 5.1 Design decisions (for approval)

1. **Two independent state machines, as today's columns already imply.**
   - `status` (operational): `Submitted → Under Review → Assigned → In Progress → Marked Resolved`, plus
     controlled backward moves: `Assigned → Under Review` (unassign), `Marked Resolved → In Progress`
     (reopen after dispute or reinspection).
   - `verification` (citizen): `Pending Verification → Citizen Confirmed | Disputed`,
     `Disputed → Reinspection Required` (staff), `Reinspection Required → Pending Verification` (when
     the case is marked resolved again).
   - The stored value stays `Disputed` (the frontend and existing data use it) and is labelled
     "Citizen Disputed" in the UI. No combined status field.
2. **Rules live in one pure module** (`app/lifecycle.py`): a transition table, preconditions and the
   role permitted for each action. No SQL and no HTTP, so the web API, tests and the future Telegram
   webhook all call the same rules.
3. **Roles (RBAC):** `official` (existing; acts on cases assigned to them or their department),
   `supervisor` (assign/reassign, resolve, reopen, approve merges in WS2), `admin` (manage staff and
   departments; everything a supervisor can do). Existing accounts keep `official`. Set by the
   `python -m app.staff` CLI only; there is no self-service role change.
4. **Visibility by table, not by flag.** Public updates keep using `complaint_timeline` (already the
   public feed). Internal notes go in a **separate `staff_notes` table** that the public `_load()` never
   queries. Public responses keep their explicit Pydantic models (allow-list).
5. **Append-only history and audit.** `audit_log` has no update or delete path in `store.py`, plus
   database triggers that reject `UPDATE`/`DELETE` on both engines. Its foreign key to complaints is
   `RESTRICT`, not `CASCADE` (complaints are never deleted).
6. **Concurrency.** Every mutation runs in `conn.transaction(immediate=True)` with the complaint row
   locked `FOR UPDATE` on PostgreSQL. The client sends `expectedStatus` (and the work-order
   `expectedStatus`); a mismatch returns `409 conflict` rather than overwriting someone else's change.
7. **Citizen verification uses the claim code** (no new identity system): `POST` with `{claim,
   response}` in the body, never in the URL. Rate-limited per IP and per complaint like the
   upload-token rule. Generic error on mismatch.
8. **After-repair evidence reuses the existing upload pipeline.** Option A (recommended): extend
   `evidence.role` to `('original', 'repair')`, replacing `UNIQUE (complaint_pk, role)` with a partial
   unique index on the original only, so storage, the orphan audit and the access log keep working
   unchanged. Option B: a separate `work_order_evidence` table (no change to `evidence`, but the orphan
   audit and access log must learn a second table). **Either way the `EvidenceStorage` interface is
   untouched** (keys are opaque), so the S3 developer's adapter is unaffected. *Needs approval: shared
   table.*

### 5.2 Database: migration 7 (SQLite `MIGRATIONS[7]`, PostgreSQL `POSTGRES_MIGRATIONS[7]` + `POSTGRES_SCHEMA`)

| Change | Existing records |
|---|---|
| `departments (pk, code UNIQUE, name, active, created_at)` | New; seeded from an admin CLI, not hard-coded demo data |
| `staff_accounts.role` CHECK widened to `official, supervisor, admin` | SQLite cannot alter a CHECK, so the table is rebuilt (copy → swap) inside the migration with foreign keys preserved; existing rows keep `official`. PostgreSQL: drop/add the constraint |
| `staff_departments (staff_pk, department_pk)` | New |
| `complaints` + `department_pk`, `assigned_staff_pk` (nullable FKs), `version INTEGER NOT NULL DEFAULT 1` | Existing rows get NULL/1; the free-text `department` column is kept for display and backward compatibility |
| `complaint_assignments` (append-only history: department, assignee, reason, actor, assigned_at, ended_at) | New |
| `work_orders (work_order_id UNIQUE, complaint_pk, department_pk, team, assignee_staff_pk, description, due_at, status Open/In Progress/Completed/Cancelled, started_at, completed_at, completion_note, created_by, created_at, updated_at)` | New |
| `work_order_events` (history of each work-order change) | New |
| `staff_notes (note_id, complaint_pk, author_staff_pk, kind internal_note/operational, body, created_at)` | New; never public |
| `complaint_timeline` + `kind` (progress/department_response/status_explanation), `actor_staff_pk` nullable | Existing rows get `kind='progress'` |
| `verification_log`: starts being written; schema unchanged | Existing (empty) |
| `audit_log (audit_id, complaint_pk RESTRICT, actor_type staff/citizen/system, actor_staff_pk, action, details JSON, client_ip, created_at)` + no-update/no-delete triggers | New |
| `evidence.role` widened (Option A) | Rebuilt on SQLite with the same rows; PostgreSQL alters the constraint |
| Indexes: `complaints(status)`, `complaints(department_pk, status)`, `work_orders(complaint_pk)`, `audit_log(complaint_pk, pk)` | — |

Migration tests: v6 → v7 upgrade on a populated SQLite file (the existing `test_migration_stress.py`
pattern), schema parity between SQLite and PostgreSQL (`test_postgres.py`), and a dry run against a
copy of the Supabase schema before production. **No data is deleted or rewritten.**

### 5.3 API (all under `/api/v1`; staff routes need a bearer session)

| Method and path | Role | Purpose |
|---|---|---|
| `GET /staff/complaints/{id}` (extended) | official+ | Adds `allowedActions`, assignment, work orders, staff notes, audit summary |
| `POST /staff/complaints/{id}/transitions` | per action | `{to, expectedStatus, publicText?, explanation?}`, validated by `lifecycle.py` |
| `POST /staff/complaints/{id}/assignment` | supervisor+ | `{departmentCode, assignee?, reason}`; also reassigns |
| `POST /staff/complaints/{id}/notes` | official+ | Internal note |
| `POST /staff/complaints/{id}/updates` | official+ | Public update (progress or department response) |
| `POST /staff/complaints/{id}/work-orders`, `PATCH /staff/work-orders/{woId}` | official+ (own department) | Create; start/complete/cancel with `expectedStatus` and a completion note |
| `POST /staff/work-orders/{woId}/evidence` | official+ | After-repair photo (existing streaming pipeline) |
| `GET /staff/work-orders?status=&overdue=&department=` | official+ | Queue with overdue flags |
| `POST /staff/complaints/{id}/reinspections` | supervisor+ | Disputed → Reinspection Required (+ reopen) |
| `GET /staff/complaints/{id}/audit` | supervisor+ | Audit trail |
| `GET /staff/departments`, CLI `python -m app.staff department add` | official+ / CLI | Departments |
| `POST /complaints/{id}/citizen-responses` | claim code | `Appears Resolved`, `Still Present`, `Cannot Confirm` (rules in `api-contract.md` §6) |
| `GET /complaints/{id}` (public, unchanged shape) | anyone | Timeline now shows public updates and verification entries with generic actors ("Civic team", "Citizen"), never staff names or notes |

`docs/api-contract.md` §3 currently proposes `PATCH /complaints/{id}`. We recommend the explicit
action endpoints above instead: one permission check and one audit action per endpoint, and no
partial-update ambiguity. The document will be updated to match. *Needs approval: shared contract.*

### 5.4 File-by-file

| File | Current | Change | Reasoning / risk |
|---|---|---|---|
| `backend/app/lifecycle.py` (new) | — | Transition tables, preconditions, role matrix, `allowed_actions(record, staff)` | Pure and testable; the single rule source for web and Telegram |
| `backend/app/store.py` | Read-only for status | Migration 7; methods `transition`, `assign`, `add_note`, `add_public_update`, `create_work_order`, `update_work_order`, `citizen_response`, `audit_for`; each is one transaction writing state + history + audit | Keeps "the only module with SQL"; largest change, so split into small commits |
| `backend/app/models.py` | Read models | Strict input models (`extra="forbid"`, length limits, enums) and staff/public output models | Allow-listing |
| `backend/app/auth.py` | `current_staff` | `require_role(*roles)` dependency; department scoping helper | Server-side authorisation |
| `backend/app/cases_api.py` (new) | — | Staff action routes above | Keeps `staff_api.py` small |
| `backend/app/complaints.py` | create/track/token | `citizen-responses` route | Next to the claim-code logic |
| `backend/app/ratelimit.py` | Rules | `citizen_response_per_ip`, `per_complaint` | Same pattern as the existing rules |
| `backend/app/staff.py` (CLI) | create/disable/password | `--role`, `set-role`, `department add/list`, `assign-department` | Admin bootstrap without a UI |
| `backend/app/notifications.py` | Hooks for create/evidence | **Hook calls only** (`status_changed`, `resolved`, …) through the existing `Notifier` interface | Delivery belongs to Ruchika; we only emit events. Interface to be agreed |
| `backend/app/main.py` | Router wiring | Include `cases_api` | — |
| `backend/tests/test_lifecycle.py`, `test_cases_api.py`, `test_citizen_verification.py`, `test_audit.py`, `test_migration_v7.py` (new) | — | Every allowed and forbidden transition; 401/403/404/409/422; role escalation (official cannot assign, cannot act outside their department); internal-note text never appears in public JSON; audit row for every action; concurrent transition → one 409; v6 → v7 with data | Also run on PostgreSQL |
| `dist/api-client.js` | Staff read calls | Methods for the new endpoints + strict response mappers | — |
| `dist/views/staff.js` | Read-only detail | Action panel driven by server `allowedActions` (never decided client-side), notes, updates, work orders, audit | Server mode only; local mode unchanged |
| `dist/views/public.js` | Server tracking | Citizen confirm/dispute form (claim code typed, never stored) | — |
| `tests/*.test.js` | — | Client mapper tests; `scripts/backend-e2e.mjs` extended with a staff transition and a citizen confirmation journey | — |
| `docs/api-contract.md`, `backend/README.md`, `docs/IMPLEMENTATION_PROGRESS.md` | — | Updated after verification | Rules 9–11 |

Estimated size: about 1,500–2,000 lines of backend code + tests, plus about 400 lines of frontend.
Suggested commits: (1) migration 7 + tests; (2) `lifecycle.py` + tests; (3) RBAC + departments + CLI;
(4) transitions/assignment/notes/updates API; (5) work orders; (6) citizen verification; (7) audit
endpoint; (8) frontend staff actions; (9) frontend citizen verification; (10) docs.

---

## 6. Workstream 2 — Duplicate detection

**Signals** (computed in Python on a bounded candidate set, so SQLite and PostgreSQL behave identically):

| Signal | How | Why |
|---|---|---|
| Distance vs location uncertainty | Haversine (WGS84, R = 6,371,008.8 m); `z = d / sqrt(acc₁² + acc₂² + 10²)` | 30 m apart with ±40 m accuracy is "same place"; with ±5 m it is not. Live data averages ±32 m for GPS and ±10 m for manual |
| Category | Same category is required for "duplicate"; a configurable related-category map (e.g. Potholes ~ Road cracks) gives a weak link | Cross-category matches are rarely duplicates |
| Time | Gap in days with decay; an older **resolved** case nearby is labelled *recurrence*, not duplicate | Distinguishes repeat problems from repeat reports |
| Description | Character-trigram Jaccard + token overlap, in Python (no new dependency; `pg_trgm` is not used so SQLite stays at parity) | Short civic texts |
| Evidence | Identical SHA-256 = strong signal (same file submitted twice) | Exact and cheap |
| Image similarity | **Deferred**: needs a new dependency (Pillow/imagehash), decoding private files from Supabase, and no answer for video. Propose separately with justification | Rule 4 (no unnecessary dependencies) |

**Output:** ranked candidates with `score` (0–1), `label` (`likely_duplicate` / `possible_duplicate` /
`nearby_separate` / `recurrence`), and per-factor contributions with plain-language reasons. Low
accuracy (manual pins, > 100 m) caps the score and says so. No automatic merging.

**Merging (supervisor+):** `complaint_merges (merge_id, duplicate_pk UNIQUE WHERE active, canonical_pk,
reason, actor, created_at, reverted_at, reverted_by)` and `duplicate_dismissals` (so rejected pairs stop
reappearing). Rules: no self-merge; the canonical case must not itself be merged (no chains); a case
that is the canonical for others cannot be merged away; the duplicate locked `FOR UPDATE`. The original
complaint, its evidence and its history stay intact. Public tracking of a merged ID shows "Merged into
NCL-…". Reverting is a new record, never a deletion. All of this is written to the WS1 audit log.

Files: `app/duplicates.py` (pure scoring), `app/geo.py` (shared distance and bbox), store methods,
`cases_api.py` routes (`GET /staff/complaints/{id}/duplicates`, `POST …/merge`, `POST …/duplicates/{other}/dismiss`,
`POST /staff/merges/{id}/revert`), migration 8, staff console panel. Tests: synthetic pairs that
cover same pothole vs two potholes 25 m apart, a GPS-accuracy edge, a recurrence after resolution,
identical files, chain/conflict prevention, concurrent merges, and missing or low-accuracy location.

**Coordination:** followers of a merged complaint should receive the canonical case's updates. That
is an interface request to Ruchika's notification work, not something we build.

---

## 7. Workstream 3 — Geospatial intelligence

**Architecture decision: no PostGIS for now.** PostGIS 3.3.7 is available on Supabase but not
installed. At the current scale (12 rows; realistic near-term < 50k) a **bounding-box prefilter on a
`(gps_lat, gps_lng)` B-tree index + exact haversine in Python** is fast, works identically on SQLite
(the test engine and dev default) and adds no extension. Revisit PostGIS when ward polygons or volume
make it pay off (polygon joins, > ~100k points).

| Capability | Plan |
|---|---|
| Nearby search | `GET /staff/geo/nearby?lat&lng&radiusM(≤5000)&category&status&from&to&limit&offset`, sorted by distance, then created; distances in metres |
| Clustering | DBSCAN-style on a filtered set (eps in metres, min points, eps widened by GPS accuracy), with grid bucketing for O(n·k). Returns centroid, radius, count, category/status mix, member IDs. Staff only |
| Recurring locations | Clusters spanning ≥ N distinct weeks; each member labelled using WS2: merged duplicate / separate issue / recurrence after resolution / unresolved |
| Area summaries | By the reverse-geocoded `area` (neighbourhood), **explicitly labelled "reported neighbourhood, not a ward"** |
| Wards | **Blocked on data: there are no ward boundaries in the repository or database.** Plan: a `wards` table + `python -m app.geo import-wards <official GeoJSON>` + a point-in-polygon check in Python + `complaints.ward_id` set at creation and backfilled. Needs an authoritative, licensed boundary file for the current Bengaluru ward delimitation, which the team must supply. No ward is ever guessed |
| Public map | Not in this workstream. Public responses keep excluding precise GPS (`api-contract.md` §4 privacy rule) |

Files: `app/geo.py`, `app/geo_api.py`, store queries + index migration, tests with known distances
(e.g. 1 km north ≈ 0.008993° latitude), antimeridian/pole guards, and low-accuracy handling.
Frontend: a staff "nearby/clusters" panel (the map view stays client-side until the team decides to
move it to server data).

---

## 8. Workstream 4 — Security and product hardening (findings so far)

| # | Finding | Severity | Proposed action |
|---|---|---|---|
| 1 | `main` deleted the whole frontend (`dist/`) as "build output" | **Critical (availability)** | Revert `5e59f8d`, `015fee1` on `main`; owner decision |
| 2 | **Swagger UI and `openapi.json` are public in production** (`/api/v1/docs` → 200). The `area = 'string'` complaint in Supabase was created from it | Medium | `CIVICLENS_API_DOCS=off` setting, default off when `DATABASE_BACKEND=postgres` |
| 3 | Only one staff role; every official could do everything once write endpoints exist | High (once WS1 lands) | RBAC in WS1 |
| 4 | Rate limits and quota are in memory: reset on restart, not shared | Medium | Postgres-backed limiter behind the existing `RateLimiter` interface (no Redis needed at this scale); keep Render at one instance until then |
| 5 | RLS off on all 13 tables (currently safe only because `anon`/`authenticated` have no grants) | Medium (defence in depth) | Migration enabling RLS on every table, plus revoking default privileges for future tables; the backend's owner role is unaffected |
| 6 | Staff sign-in: rate limit only; no lockout, MFA or session list | Medium | Account-level backoff; "revoke all sessions" CLI; MFA is a later decision |
| 7 | No backup/restore procedure; Supabase free plan has no point-in-time recovery | Medium | Documented `pg_dump` schedule + bucket export + a restore drill |
| 8 | No retention policy for evidence, audit logs and access logs (`api-contract.md` §10 open question) | Policy | Team/legal decision; we implement whatever is decided |
| 9 | No error monitoring; logs go to stdout only | Low | Structured request logs with request IDs; optional error reporting via an environment setting |
| 10 | `CIVICLENS_TRUSTED_PROXIES` must be set on Render or every visitor shares one rate-limit bucket | Medium | Verify on the live service |
| 11 | Push notifications use in-process `BackgroundTasks` (lost on restart) | Low | Ruchika's area; flag only |
| 12 | Baseline test failure: shipped config is backend mode | Low | Team decision (§3) |

Confirmed good: scrypt hashing; session revocation on password change and disable; CORS rejects
foreign origins in production (400); public projection excludes claim, GPS, description and evidence;
500 errors are sanitised; evidence private + audited; no secrets in `dist/`.

Security tests to add (WS4, plus each workstream's own): unauthorised and role-escalated actions,
cross-department actions, public-response leakage of every new field, evidence access, invalid
transitions, rate-limit enforcement on new endpoints, input validation limits, and audit
immutability (the update/delete triggers fire).

---

## 9. Coordination and dependencies

| With | What | Our side | Theirs |
|---|---|---|---|
| S3 developer | `evidence.role` widening (Option A) and work-order evidence; `storage_backend` CHECK (`'local','supabase'`) must also allow their backend | We link evidence to work orders; no storage code changes | Their adapter implements the existing `EvidenceStorage` interface; one migration should widen `storage_backend` and `role` together to avoid conflicting version numbers |
| Telegram developer | Status changes, assignments and citizen responses from Telegram | `lifecycle.py` + store methods are the only rule source; we can expose a small service facade | Their webhook calls those, never SQL directly; staff identity mapping (Telegram user → staff account) needs a design |
| Ruchika | Priority score in staff queues; notification events; FCM; predictions using our clusters | We emit domain events through `Notifier` hooks; we expose clusters/recurrence read APIs | Priority Engine provides a read API (e.g. `GET /priority/{id}`); we display it, never compute it |
| All | **Migration numbering**: version 7 is next; parallel branches must not both claim 7 | We reserve 7 (WS1), 8 (WS2), 9 (WS3 indexes/wards), 10 (WS4 RLS) | Agree before merging |

## 10. Recommended sequence

1. **WS1 complaint management.** Everything else depends on its RBAC, audit log and state model.
2. **WS4 quick wins in parallel with WS1, only where trivial and independent:** disable public API
   docs in production, verify trusted proxies. (Optional; otherwise wait.)
3. **WS2 duplicate detection.** Needs WS1's supervisor role, audit and merge-safe transactions; it
   introduces the shared `geo.py` distance code.
4. **WS3 geospatial.** Reuses WS2's distance code; recurring-location labels depend on WS2's
   duplicate/recurrence classification.
5. **WS4 full hardening pass** across everything, including RLS, the shared rate limiter, backups and
   security tests.

The default order stands. The one adjustment is pulling the two trivial WS4 items forward, because
public Swagger is already producing junk data in production.

## 11. Decisions needed before implementation

1. Approve the two-state-machine design and the transition table (§5.1).
2. Approve the roles `official` / `supervisor` / `admin` and department scoping.
3. Choose Option A or B for after-repair evidence (shared `evidence` table).
4. Approve explicit action endpoints instead of `PATCH /complaints/{id}` (contract change).
5. Approve migration 7 (staff role table rebuild on SQLite) and the reserved version numbers.
6. Decide how the failing `api-config` test should be resolved.
7. Supply official ward boundaries (or accept neighbourhood-only summaries for now).
8. Fix `main`'s deleted `dist/` (repository owner).

---

## 12. Workstream 1 — implementation record (2026-10-30)

Approved decisions as implemented: two state machines (§5.1); roles official/supervisor/admin with department scoping; **Option A** for after-repair evidence (`evidence.role` widened to `repair`, partial unique index on the original); explicit action endpoints (`api-contract.md` §3.2); migration 7 on both engines.

Differences from the plan, and why:
- The work-order status change is `POST /staff/work-orders/{id}/transitions`, not `PATCH`, because the CORS allowlist has no `PATCH`.
- History is append-only by design: assignments are rows with no `ended_at`, and the current assignment lives on `complaints`.
- No notification events are emitted. The notifications table restricts `kind` with a CHECK list owned by the notification work, so new kinds need that owner's agreement (§9).
- `GET /staff/complaints/{id}` now returns `StaffCase` (a superset of the old response). Officials are scoped to their departments on the list, detail, evidence and access-log routes. Existing accounts are unaffected until departments are created, because unassigned complaints remain visible to every official.

Files: `backend/app/lifecycle.py`, `cases_api.py` (new); `store.py`, `db.py`, `models.py`, `staff_api.py`, `complaints.py`, `errors.py`, `ratelimit.py`, `staff.py`, `main.py`; tests `test_lifecycle.py`, `test_migration_v7.py`, `test_cases_api.py` (new) plus version and fixture updates in `test_evidence.py` and `test_postgres.py`; `dist/api-client.js`, `report-gateway.js`, `views/staff.js`, `views/public.js`; `tests/case-management.test.js`.

Before production: back up Supabase, then deploy. The API applies migration 7 at startup, and a failure rolls the whole migration back. After that, create departments and roles with the CLI.
