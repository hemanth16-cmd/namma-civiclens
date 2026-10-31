# Namma CivicLens — Backend

**Status (Phase 3.6 + Supabase migration + Web Push):** a FastAPI service backed by SQLite (default) or **PostgreSQL / Supabase Postgres**, and private evidence storage — local files (default) or a private **Supabase Storage** bucket — with development-grade rate limits, an evidence storage quota, idempotent complaint creation and a staff console API. It creates complaints from metadata, serves public tracking, accepts the complaint's original photo or video through an authorised, streamed upload, and lets **signed-in staff** list complaints and view evidence (every view is logged), and sends **Web Push notifications** about complaints to citizens who follow them and to staff, with a persistent notification history. The frontend in `dist/` can use it in **opt-in backend mode** (see "Using it from the frontend" below); by default the frontend still runs in local mode with `localStorage`/IndexedDB. A complaint's owner can view its evidence with the private claim code or from the resident account that owns it; the public cannot. Everything else in `../docs/api-contract.md` (claiming, staff case updates, repairs, maps) is still **proposed, not implemented**.

> **No user authentication; development-grade abuse protection only.** Anyone who can reach the server can create complaints and look them up by ID. Phase 3.5 added per-IP/per-complaint rate limits, an evidence storage quota and idempotent creation, but the limits live in one process's memory (reset on restart, not shared between workers). Before any public deployment it still needs shared rate limiting, HTTPS, abuse/spam controls and server-side authentication for anything beyond anonymous submit and public tracking.

## Layout

```
backend/
  app/
    __init__.py        API_VERSION ("v1"), APP_VERSION, SERVICE_NAME
    main.py            create_app() factory, lifespan (creates the schema), /api/v1 router, health
    complaints.py      POST /complaints, GET /complaints/{publicId}
    models.py          Pydantic request/response schemas (camelCase, matching dist/services.js)
    store.py           Data-access layer: the only module with SQL (SQLite and PostgreSQL schemas)
    db.py              Database backends: SQLiteDatabase, PostgresDatabase (pool), selection by DATABASE_BACKEND
    db_migrate.py      SQLite → PostgreSQL copy tool (python -m app.db_migrate; dry run by default)
    claims.py          Public ID and claim-code generation, scrypt hashing/verification
    cors.py            Explicit browser-origin allowlist (default http://localhost:4173)
    evidence.py        POST /complaints/{id}/evidence: streamed multipart parsing, signature checks, limits
    evidence_storage.py EvidenceStorage interface + LocalEvidenceStorage (private disk storage)
    supabase_storage.py SupabaseEvidenceStorage (private Supabase Storage bucket, server-side only) + configuration
    ratelimit.py       RateLimiter interface, in-memory sliding window, client-IP / trusted-proxy rules
    quota.py           Total evidence storage quota with reservations
    evidence_audit.py  Read-only-by-default audit: orphan files, missing files (python -m app.evidence_audit)
    __main__.py        Safe launcher (python -m app): uvicorn with proxy headers OFF
    auth.py            Staff passwords (scrypt), bearer sessions, current_staff dependency
    staff.py           Staff account CLI (python -m app.staff create|list|disable|enable|set-password)
    staff_api.py       /staff routes: sign-in/out, complaint list/detail, evidence streaming, access log
    push.py            Web Push: VAPID settings, WebPushSender (pywebpush), key generator (python -m app.push)
    notifications.py   Notification events, history storage and push delivery/failure handling (Notifier)
    notifications_api.py /notifications routes: config, inboxes, follows, devices, history, read state, test
    errors.py          Error envelope {"error": {"code", "message"[, "details"]}}
    ai/                AI analysis with Roboflow (off by default; see "AI analysis" below)
      model_registry.py  Configuration from environment: detectors, category routing, class mapping
      roboflow_service.py HTTPS client for Roboflow models (infer) and workflows (run_workflow); header auth
      result_normalizer.py Provider responses -> {className, category, confidence, bbox[, polygon]}
      images.py          Private evidence reads, JPEG validation, metadata stripping, quality metrics
      image_alignment.py OpenCV ORB + RANSAC: do two photos show the same place?
      repair_verifier.py Before/after decision rules (likely_repaired / damage_visible / inconclusive)
      ai_store.py        SQL for ai_inferences, repair_assessments, repair_assessment_inferences (schema 8)
      service.py         AiService: scheduling, bounded worker pool, startup recovery
    ai_api.py          /staff/complaints/{id}/ai, /ai/retry, /verify-repair, /repair-assessment[/{id}/review]; public /ai
  tests/
    conftest.py        Gives every test its own temporary database
    test_health.py     Health, routing and error-shape tests
    test_complaints.py Complaint API, storage, hashing, validation, isolation
    test_cors.py       Allowed/blocked origins, env override, wildcard refusal
    test_evidence.py   Uploads, tokens, limits, signatures, storage failures, privacy, migration
    test_ratelimit.py  Limits, Retry-After, reset, IP isolation, forwarded-header spoofing, ordering
    test_quota.py      Reservations, concurrency, release on failure, restart, inconsistent metadata
    test_idempotency.py Replay, conflict, concurrency, failed transactions, credential re-issue rules
    test_evidence_audit.py Dry run, deletion safety, recheck, temp files, CLI
    test_staff.py      CLI, sign-in, sessions, revocation, projections, evidence streaming, audit log, login limits
    test_postgres.py   PostgreSQL schema, constraints, rollback, concurrency, pool, migration (real PostgreSQL)
    dbutil.py          test helper: direct SQL on whichever backend the run uses
    test_notifications.py Web Push: ownership, follows, devices, events, history, delivery, real encryption/VAPID
    test_supabase_storage.py Supabase config, startup checks, adapter, failures, full API flow (fake HTTP; no real Supabase)
    test_deep_stress.py 50 concurrent complaints, 100 tracking, 20 streaming uploads, Kannada/multilingual Unicode, 50 mixed DB workers, rate-limiter memory bounds, quota exhaustion
    test_security_review.py IDOR isolation, staff session revocation, tracking secrecy, sanitization, media exploits (SVG, HTML, PHP, traversal), SQL injection, SSRF prevention
    test_migration_stress.py Full 12-table SQLite-to-PostgreSQL synthetic migration, verification, sequence resets, idempotency, conflict halt
    test_ai_unit.py    AI configuration, Roboflow client (mocked HTTP), normalisation, images, alignment, decision rules
    test_ai_api.py     AI end to end over HTTP with a fake provider: detection, failures, retries, scope, repair checks, review
    test_migration_v8.py Schema 8 upgrade on SQLite and PostgreSQL (JSONB), and SQLite -> PostgreSQL copy of AI rows
    aiutil.py          Synthetic test photos and the deterministic fake provider
    test_push_stress.py Push failure pruning (410 Gone, 5-failure threshold), unread race conditions, zero-leakage payload verification
  data/                Default database and evidence location (created on first start; git-ignored)
    civiclens.sqlite3  complaints, timeline, verification log, evidence metadata
    evidence/objects/  stored files, named by random key (objects/<first 2 hex>/<32 hex>)
    evidence/tmp/      in-progress uploads (*.part), removed on failure; stale ones removed at startup
  requirements.txt     Runtime: fastapi, uvicorn, python-multipart, storage3, psycopg, psycopg-pool, pywebpush (pinned)
  .env.example         Environment template (no real values; .env files are git-ignored)
  requirements-dev.txt Adds pytest, httpx2 (FastAPI TestClient)
  pyproject.toml       pytest settings only
```

The backend is separate from the frontend: `package.json`, `scripts/` and `dist/` are untouched, and `npm run check` does not scan `backend/`.

## Setup (Windows, Git Bash or PowerShell)

Needs Python 3.11 or later (developed and tested on 3.14.3). Dependencies go into a virtual environment inside `backend/`, never globally. SQLite comes with Python; there is nothing else to install.

```sh
cd backend
py -3.14 -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt
```

On macOS/Linux use `python3 -m venv .venv` and `.venv/bin/python`.

## Database

Two engines, chosen with `DATABASE_BACKEND`. SQLite is the default and needs nothing; PostgreSQL (for example Supabase) is for deployments. The API, schema, constraints and behaviour are the same on both.

| | `sqlite` (default) | `postgres` |
| --- | --- | --- |
| Configuration | `CIVICLENS_DB_PATH` (optional) | `DATABASE_URL` (required), `CIVICLENS_DB_POOL_MAX` (optional, default 10) |
| Driver | standard-library `sqlite3` | `psycopg` 3 with `psycopg-pool` (thread-safe pool) |
| Schema version | `PRAGMA user_version` | `civiclens_schema` table |
| Concurrency | one writer at a time (`BEGIN IMMEDIATE` where a read-modify-write must be serialised) | row locks (`SELECT … FOR UPDATE`) and unique constraints |

How it is built (`app/db.py`, `app/store.py`): all SQL lives once in `store.py` with `?` placeholders. `db.py` supplies what differs: connections, placeholder translation for psycopg, inserts that return the new key (`lastrowid` vs `RETURNING pk`), transactions, schema bootstrap, and one set of exception classes. PostgreSQL types: `BIGINT` identity keys (BY DEFAULT, so imports keep their keys), `DOUBLE PRECISION` for GPS (PostgreSQL `REAL` is 4-byte and would lose precision), `BIGINT` foreign keys. Timestamps stay ISO 8601 UTC text (`…Z`) on both engines, so comparisons and API output are identical. PostgreSQL schema creation runs in one transaction under an advisory lock, so several app instances starting together cannot race.

### Supabase PostgreSQL setup

1. In the Supabase dashboard open your project → **Connect** (or Project Settings → Database) and copy a **connection string** (URI form): `postgresql://postgres.<project-ref>:<password>@<host>:<port>/postgres`.
   - **Session pooler** (port 5432) or **direct connection**: recommended for this long-running API.
   - **Transaction pooler** (port 6543) also works: CivicLens disables server-side prepared statements (`prepare_threshold=None`), which that pooler does not support.
2. Add `?sslmode=require` to the URL so the connection is always encrypted.
3. Set, in the server environment only (never in frontend files or a committed `.env`):

```sh
DATABASE_BACKEND=postgres
DATABASE_URL=postgresql://postgres.<project-ref>:<password>@<host>:5432/postgres?sslmode=require
```

4. Start the API (`.venv/Scripts/python -m app`). The schema is created on first start. The staff CLI (`python -m app.staff`) and the audit tool follow the same variables.

The database password is never printed: error messages and reports name the host and database only. Use a dedicated database password for the API, and keep Supabase's Row Level Security in mind if you also expose these tables through Supabase's own APIs (CivicLens does not; it connects as a database role and enforces access itself).

### Moving existing SQLite data to PostgreSQL

`app/db_migrate.py` copies a SQLite database into PostgreSQL. It is a **dry run unless you pass `--execute`**:

```sh
cd backend
DATABASE_URL=postgresql://… .venv/Scripts/python -m app.db_migrate --sqlite data/civiclens.sqlite3            # plan: counts per table
DATABASE_URL=postgresql://… .venv/Scripts/python -m app.db_migrate --sqlite data/civiclens.sqlite3 --execute  # copy
```

- The source file is **never modified**: it is read through a read-only connection with SQLite's backup API into a temporary copy (a consistent snapshot including any WAL data); an older schema is upgraded on the copy only. SQLite may create empty `-wal`/`-shm` helper files next to a WAL-mode database while reading; the data file is unchanged. Stop the API first so the snapshot is current.
- `--execute` creates the PostgreSQL schema if needed and inserts every row **with its original primary key** in one transaction, so complaint IDs, timelines, evidence, sessions, idempotency keys and audit entries keep their relationships; identity sequences are then advanced past the imported keys. Any failure rolls everything back.
- **Re-running is safe:** rows already present with identical values are skipped. A key that exists with different values is reported as a conflict (table and key only) and nothing is written.
- Afterwards every source row is compared column by column with PostgreSQL, counts are reported, and every foreign key is verified. Exit code 0 means the copy is complete and valid.
- Evidence **files** are not moved: rows keep pointing at the backend that holds each file (for local files, keep the same `CIVICLENS_EVIDENCE_DIR`).

### SQLite details

- **Engine:** SQLite through Python's standard-library `sqlite3`. No ORM.
- **Default file:** `backend/data/civiclens.sqlite3`. The folder and schema are created automatically when the server starts. Restarting keeps all data.
- **Choose another file:** set `CIVICLENS_DB_PATH`, for example `CIVICLENS_DB_PATH=/tmp/dev.sqlite3`. Code can also pass `create_app(db_path=...)`.
- **Reset local data:** stop the server and delete the database file and its `-wal`/`-shm` companions. (This erases every stored complaint.)
- **Git:** `data/`, `*.sqlite3`, `*.sqlite3-*`, `*.db` and journal files are ignored.
- Journal mode is WAL; foreign keys are on; schema version is stored in `PRAGMA user_version` (currently 6). Older databases are migrated in place at startup (1 → 2 adds the upload-token columns and the `evidence` table; 2 → 3 adds `complaints.credentials_used_at` and the `idempotency_keys` table; 3 → 4 adds `staff_accounts`, `staff_sessions` and `evidence_access_log`; 4 → 5 adds `evidence.storage_backend` (`local`/`supabase`, existing rows become `local`); 5 → 6 adds the notification tables (see "Web Push notifications"); existing complaints are kept).

Tables:

| Table | Purpose |
| --- | --- |
| `complaints` | One row per complaint. `public_id` is `UNIQUE`. Holds `claim_hash` (never the code), category, area, description, media type, share flag, GPS fields, capture/creation/upload/update times, lifecycle `status`, `verification`, `department`. CHECK constraints guard coordinate ranges, media type, source type. |
| `complaint_timeline` | Append-only public timeline entries (`status`, `text`, `date`). |
| `verification_log` | Append-only public verification history. Nothing writes to it yet; it is read so the public response matches the prototype's shape. |
| `evidence` | Metadata for uploaded files: opaque `evidence_id`, `complaint_pk`, `role` (`original`), server-generated `storage_key` (UNIQUE), `media_type`, `content_type`, `byte_size`, `sha256`, `captured_at`, `uploaded_at`. `UNIQUE (complaint_pk, role)`: one original per complaint. No file contents. |

`complaints` also holds `upload_token_hash` (SHA-256 of the current upload token) and `upload_token_expires_at`.

## Evidence storage

- **Interface:** `EvidenceStorage` (`begin` → write chunks → `commit` returns a key, or `abort`; `delete`; `exists`; `open_read`; `stored_bytes`). Two implementations: `LocalEvidenceStorage` (default, described in the rest of this list) and `SupabaseEvidenceStorage` (see "Supabase Storage evidence storage" below). Routes depend only on the interface.
- **Backend per file:** every `evidence` row records `storage_backend` (`local` or `supabase`). New uploads go to the backend chosen by `EVIDENCE_STORAGE_BACKEND`; each file is always read and deleted from the backend recorded on its row, so local evidence stays readable after Supabase Storage is enabled. Existing files are never moved automatically.
- **Location:** `backend/data/evidence/` by default; set `CIVICLENS_EVIDENCE_DIR` to change it. The server refuses to start if the folder is inside the publicly served `dist/` directory. Nothing serves this folder over HTTP.
- **Names:** files are named by a random 128-bit hex key generated on the server, never by the uploaded filename. Keys are validated against `^[0-9a-f]{32}$` before use, so they cannot point outside the folder.
- **Writes:** each upload goes to an exclusively created `tmp/*.part` file, is fsynced, then renamed into `objects/` atomically. If anything fails (bad type, too large, interrupted, disk or database error) the partial file is deleted and no database row is written; if the database write fails after the rename, the stored file is deleted.
- **Cleanup:** `tmp/*.part` files older than one hour are removed at startup. To reset local evidence, stop the server and delete `data/evidence/` together with the database (deleting only one of them leaves rows pointing at missing files, or files nothing refers to).
- **Backups:** there are none. The database and the evidence folder must be backed up together. A crash between the rename and the database insert can leave an unreferenced file in `objects/` (it is never served); a cleanup job for such files is not built yet.

All times are ISO 8601 UTC strings with milliseconds and `Z`, the same as JavaScript's `Date.toISOString()`.

Only `store.py` contains SQL, and it returns plain dicts. A PostgreSQL/PostGIS store can replace `ComplaintStore` later without changing routes or models.

## Run

```sh
cd backend
.venv/Scripts/python -m app --port 8000 --reload
```

`python -m app` starts uvicorn with its proxy-header handling **off**. If you start uvicorn directly, add `--no-proxy-headers`:

```sh
.venv/Scripts/python -m uvicorn app.main:app --port 8000 --no-proxy-headers
```

Why: by default uvicorn trusts `X-Forwarded-For` from `127.0.0.1` and rewrites the client address before the app sees it, so any local process could pick its own address and bypass the per-IP rate limits. This was found in the Phase 3.5 live check (a spoofed header got `201` after a `429`) and fixed by the launcher.

- Health: http://localhost:8000/api/v1/health
- Interactive docs: http://localhost:8000/api/v1/docs (the Swagger UI page loads its scripts from a CDN, so it needs network access; the API itself does not)
- OpenAPI schema: http://localhost:8000/api/v1/openapi.json

It listens on `127.0.0.1` only. The frontend keeps running separately with `npm run dev` on port 4173.

## Abuse protection (Phase 3.5)

### Rate limits

| Endpoint | Default limit | Counted | Environment variable (`count/seconds`) |
| --- | --- | --- | --- |
| `POST /complaints` | 5 per 10 minutes per client IP | before the handler runs (replays and refusals count too) | `CIVICLENS_RATE_CREATE=5/600` |
| `GET /complaints/{id}` | 60 per minute per client IP | before the lookup | `CIVICLENS_RATE_TRACK=60/60` |
| `POST /complaints/{id}/upload-token` | 5 per 15 minutes per client IP **and** 10 per hour per complaint | before the database lookup and the scrypt check | `CIVICLENS_RATE_TOKEN_IP=5/900`, `CIVICLENS_RATE_TOKEN_COMPLAINT=10/3600` |
| Notification endpoints | see "Web Push notifications" | | `CIVICLENS_RATE_INBOX_IP`, `CIVICLENS_RATE_FOLLOW_IP`, `CIVICLENS_RATE_FOLLOW_COMPLAINT`, `CIVICLENS_RATE_PUSH_REGISTER_IP`, `CIVICLENS_RATE_PUSH_TEST` |
| `POST /complaints/{id}/evidence` | 30 per hour per client IP (before authorisation) and 10 authorised attempts per hour per complaint | per-IP before any database or body work; per-complaint after the token is accepted, so strangers cannot use up a complaint's allowance | `CIVICLENS_RATE_EVIDENCE_IP=30/3600`, `CIVICLENS_RATE_EVIDENCE_COMPLAINT=10/3600` |

Exceeding a limit returns `429` with `{"error": {"code": "rate_limited", ...}}` and a `Retry-After` header (whole seconds until the oldest counted request leaves the window). Refused requests do not extend the window. `CIVICLENS_RATE_LIMITS=off` disables all limits (used by the test suites; never in a deployment).

Implementation: `RateLimiter` interface with an in-memory sliding-window implementation. It is thread-safe, keeps at most 10,000 keys (least recently used evicted) and uses an injectable clock in tests. **Limits reset when the server restarts and are not shared between worker processes or machines**; a multi-process deployment needs a shared store (for example Redis) behind the same interface. This is development-grade, not distributed production rate limiting.

The per-complaint token limit is a trade-off: anyone who knows a public ID can use up that complaint's 10 renewal attempts for an hour (a temporary lock-out). Claim codes carry 122 random bits, so the limit exists to bound scrypt CPU and memory, not to stop guessing.

### Client IP and proxies

The client address is the TCP peer as seen by the app. Forwarding headers (`X-Forwarded-For`, `X-Real-IP`, `Forwarded`) are **ignored** unless the peer is listed in `CIVICLENS_TRUSTED_PROXIES` (comma-separated IPs or CIDR ranges, empty by default). When the peer is trusted, the right-most `X-Forwarded-For` address that is not itself a trusted proxy is used, so a client cannot dodge its limit by prepending a fake address. Keep uvicorn's own proxy handling off (`python -m app`, or `--no-proxy-headers`).

For a future deployment behind a reverse proxy: set `CIVICLENS_TRUSTED_PROXIES` to exactly the proxy's address(es), make sure the proxy overwrites (not appends to) client-supplied forwarding headers, and make the API reachable only through the proxy.

### Evidence storage quota

Total stored evidence is capped at 2 GB by default (`CIVICLENS_EVIDENCE_QUOTA_BYTES`). Before an upload's body is read, it reserves its `Content-Length` (or the largest allowed request when the length is unknown); stored evidence plus all reservations must stay within the quota, so concurrent uploads cannot jointly exceed it. On success the reservation becomes the file's real size; on any failure, rejection or cancellation it is released. Usage is loaded at startup as the larger of the database total and the bytes actually present in storage, so it survives restarts and errs on the safe side if metadata is missing.

Policy: an upload that would exceed the quota gets **`507 storage_quota_exceeded`** (the complaint itself stays saved). Per-file limits (25 MB photo, 200 MB video) still return `413`. Nothing is ever deleted automatically to make room. The frontend does not offer a retry for `507`.

### Idempotent complaint creation

Send `Idempotency-Key: <22–128 URL-safe characters>` with `POST /complaints` (the frontend sends 256 random bits per report and reuses it for every retry of that report). Only a SHA-256 hash of the key is stored, in `idempotency_keys` (`UNIQUE(scope, key_hash)`), together with a fingerprint: SHA-256 of the normalised request. The key row is written in the same transaction as the complaint, so a failed creation leaves no key behind.

| Situation | Response |
| --- | --- |
| New key | `201`, normal receipt, `"replayed": false` |
| Same key, same request, credentials never used, within 24 h | `200`, same complaint, `"replayed": true`, header `Idempotent-Replayed: true`, **new claim code and upload token** |
| Same key, same request, but evidence was uploaded or the token was renewed (or more than 24 h passed) | `409 idempotency_key_used` with `"complaintId"` only, no credentials |
| Same key, different request | `409 idempotency_key_conflict`, no complaint data |
| Malformed key | `400 invalid_idempotency_key` |
| No key | previous behaviour (every request creates a complaint) |

Concurrent requests with one key create exactly one complaint: the database's unique constraint decides, and the others take the replay path.

**Receipt recovery design.** The server keeps only hashes of claim codes and upload tokens, so it cannot repeat the original receipt. Instead of storing secrets (encrypted or not), a replay **re-issues** credentials: it replaces the claim-code hash and the upload-token hash in one transaction and returns the new values. Consequences, all tested: the earlier claim code and upload token stop working; the most recent response's credentials are the valid ones; re-issue is refused once the credentials have demonstrably been used (`credentials_used_at`, set on evidence upload or token renewal) or after the replay window (`CIVICLENS_IDEMPOTENCY_REPLAY_SECONDS`, default 86400). Re-issue therefore requires the idempotency key (a client-held random secret) **and** the byte-identical request, and only applies to credentials nobody has used. Idempotency rows are retained (no expiry job yet); after the window the key still prevents a duplicate but returns no credentials.

## Staff console API (Phase 3.6)

Development-grade authentication for one role, `official`. There is **no default account**: create accounts with the CLI (passwords are prompted twice without echo, or read from one line of stdin with `--password-stdin`; never accepted as arguments, never printed; minimum 12 characters; stored as scrypt hashes):

```sh
cd backend
.venv/Scripts/python -m app.staff create --username asha --display-name "Asha (ward office)"
.venv/Scripts/python -m app.staff list
.venv/Scripts/python -m app.staff set-password --username asha   # signs the account out everywhere
.venv/Scripts/python -m app.staff disable --username asha        # signs the account out everywhere
.venv/Scripts/python -m app.staff enable --username asha
```

Add `--db <path>` to target a database other than `CIVICLENS_DB_PATH` / `backend/data/civiclens.sqlite3`.

| Method and path | Auth | Result |
| --- | --- | --- |
| `POST /api/v1/staff/sessions` `{"username", "password"}` | none | `200 {"token", "expiresAt", "staff": {"username", "displayName", "role"}}`. Wrong password and unknown user get the same `401 unauthorized`; unknown users still run one scrypt check. Rate-limited 5 per 15 min per IP and 10 per hour per username **before** the password check (`CIVICLENS_RATE_STAFF_LOGIN_IP`, `CIVICLENS_RATE_STAFF_LOGIN_ACCOUNT`). |
| `DELETE /api/v1/staff/sessions/current` | staff | `204`; the session is revoked on the server |
| `GET /api/v1/staff/me` | staff | the signed-in identity |
| `GET /api/v1/staff/complaints?limit=50&offset=0` | staff | newest first, `limit` ≤ 100: `{"items": [{id, category, area, status, verification, created, updated, mediaType, hasEvidence}], "total", "limit", "offset"}` |
| `GET /api/v1/staff/complaints/{id}` | staff | operational detail: description, precise GPS, times, timeline, evidence metadata (`evidenceId`, type, size, checksum, times). Never the claim code or its hash, upload-token data or storage keys. |
| `GET /api/v1/staff/complaints/{id}/evidence` | staff | the original file, streamed in 64 KB chunks with its content type, `Content-Length`, `Content-Disposition: inline`, `Cache-Control: no-store, private`, `X-Content-Type-Options: nosniff` and a sandboxing CSP. The view is written to `evidence_access_log` (staff, time, client IP) **before** any byte is sent; if logging fails, nothing is sent (`503`). `404 evidence_not_found` when nothing was uploaded; `503` if the file is missing on disk (and nothing is logged). |
| `GET /api/v1/staff/complaints/{id}/evidence/access` | staff | latest 200 views: `[{username, displayName, action, accessedAt}]` |

Sessions: `Authorization: Bearer <token>`. The token is 256-bit random; only its SHA-256 hash is stored, with an expiry (8 hours, `CIVICLENS_STAFF_SESSION_SECONDS`). A session stops working when it expires, on sign-out, when the account is disabled, or when its password changes; re-enabling an account does not revive old sessions. Upload tokens and staff tokens are not interchangeable. Missing or invalid sessions get `401 unauthorized` with `WWW-Authenticate: Bearer`.

Limits of this design: one role only; no MFA, password-reset flow or lockout beyond rate limits; the access log stores the client IP (personal data, kept indefinitely for now); evidence is not available to the public (owners: see "Owner evidence viewing"); `http://` only in development, so tokens travel unencrypted on anything but localhost.

The frontend's staff console is `#/staff` in backend mode (see `docs/IMPLEMENTATION_PROGRESS.md`); it keeps the token in page memory only, so reloading signs the tab out.

## Resident accounts API

Three separate identities use the API. A **passerby** reports and tracks with no account. A
**resident** has a server account. **Staff** use the staff console API above. Resident accounts are
their own authentication domain (`app/resident_auth.py`, tables `resident_accounts`,
`resident_sessions`, `resident_preferences`), built like staff sign-in: scrypt passwords (12+
characters), 256-bit bearer tokens stored only as SHA-256 hashes, an 8-hour expiry
(`CIVICLENS_RESIDENT_SESSION_SECONDS`), and revocation on sign-out, password change or disable.
**A resident token is refused by every `/staff` endpoint, and a staff token by every `/residents`
endpoint.**

| Method and path | Auth | Result |
| --- | --- | --- |
| `POST /api/v1/residents` `{"email", "password", "displayName"}` | none | `201` + a session (`{"token", "expiresAt", "resident"}`). `409 email_taken`; `422` for a bad email or short password. 5 per hour per IP. |
| `POST /api/v1/residents/sessions` `{"email", "password"}` | none | `200` session. Wrong password and unknown email give the same `401`. Rate-limited per IP and per account before the password check. |
| `DELETE /api/v1/residents/sessions/current` | resident | `204`; revoked on the server |
| `GET /api/v1/residents/me`, `PATCH` `{"displayName"}` | resident | `{"residentId", "email", "displayName", "demo", "createdAt"}` |
| `GET /api/v1/residents/me/complaints?limit&offset` | resident | Only this resident's complaints, newest first: public fields plus their own description and location, `ownedVia` (`submitted` / `claimed`) and `ownedAt`. Never the claim code or hash, tokens, staff notes or storage keys. |
| `GET /api/v1/residents/me/complaints/{id}` | resident | One owned complaint. Any other ID is `404`, whether or not it exists. |
| `POST /api/v1/residents/me/claims` `{"id", "claim"}` | resident | Links an **unowned** complaint after checking the private claim code: `200 {"outcome": "claimed" \| "already_yours", "complaint"}`. A wrong ID or code gives one generic `403 invalid_claim`, always after one scrypt check. If another resident owns it: `409 already_claimed` (names nobody). Only the ownership columns change. The claim hash, public timeline, evidence and dates stay as they were, and the claim is written to `audit_log`. 10 per 15 min per IP, 10 per hour per complaint. |
| `GET` / `PUT /api/v1/residents/me/preferences` `{"updates", "verification", "hotspots"}` | resident | Per-account preferences (booleans only) |

**Ownership.** `POST /api/v1/complaints` stays open to everyone. With no `Authorization` header the
complaint is anonymous: `owner_resident_pk` stays `NULL` until a claim. With a valid resident token
the owner is taken **from the session** (`owned_via = 'submitted'`). An `owner` or `role` field in
the body is rejected (`422`). Any other bearer token (expired, revoked, or a staff token) is refused
with `401`, not quietly treated as anonymous. The owner is part of the idempotency fingerprint, so
nobody else can replay a resident's key. Public tracking (`GET /complaints/{id}`) never shows an
owner.

### Owner evidence viewing

The owner of a complaint sees its original photo or video through the **same retrieval service as staff**
(`app/evidence_access.py`: same evidence metadata, storage backend selection, file opening and streaming;
moved unchanged out of `staff_api.py`). Only the authorisation and the access record differ:

| Method and path | Auth | Result |
| --- | --- | --- |
| `POST /api/v1/complaints/{id}/evidence/view` `{"claim"}` | the private claim code, in the JSON body only | the file, streamed with the same headers as the staff view (`no-store, private`, `nosniff`, sandbox CSP). A wrong code and an unknown ID give the same `403 invalid_claim` after one scrypt check. |
| `GET /api/v1/residents/me/complaints/{id}/evidence` | resident session that **owns** the complaint | the same file; any other ID is `404` |

Both are rate-limited before the check (20 per 15 min per IP, 30 per hour per complaint:
`CIVICLENS_RATE_OWNER_EVIDENCE_IP`, `CIVICLENS_RATE_OWNER_EVIDENCE_COMPLAINT`). Every owner view is written to
`audit_log` (`owner_evidence_view`, actor `citizen`, how: `claim_code` or `resident`); `evidence_access_log`
stays staff-only, and an owner's view does not send the "Report under review" notification a staff view sends.
The browser fetches the bytes and shows them from a `blob:` URL, as the staff console does, so no credential,
storage key or storage URL is ever placed in a link. Staff tokens, resident tokens and upload tokens remain
separate: none of them opens another's route.

### Demonstration accounts (local or isolated test databases only)

```sh
cd backend
python -m app.demo seed --confirm-demo                    # SQLite (--db, else CIVICLENS_DB_PATH)
python -m app.demo seed --confirm-demo --allow-postgres   # an isolated PostgreSQL test database only
python -m app.demo remove --confirm-demo                  # disables both and signs them out
```

| Role | Sign in with | Password |
| --- | --- | --- |
| Resident | `resident@civiclens.demo` | `CivicDemo2026!` |
| Administrator (staff role `admin`) | `demo_admin` | `CivicDemo2026!` |

**These credentials are public and not production-safe.** The command does nothing without
`--confirm-demo`. It needs `--allow-postgres` for PostgreSQL and refuses when `CIVICLENS_ENV` is
`production`. It never prints the password and is idempotent: a rerun leaves working accounts and
their sessions alone, and repairs a changed password, role or disabled flag. The API never creates
these accounts, and the frontend never contains the credentials. The admin is made with the same
store methods as `python -m app.staff`, so the role lives only on the server.

## Web Push notifications

System notifications (Windows notification centre, Android notification shade, macOS) about complaints, delivered with the standard **Web Push API**, a **service worker** (`dist/sw.js`) and **VAPID** keys, also when CivicLens is closed (where the browser keeps running in the background; see "Platform notes"). Every notification is stored first, so there is a persistent **history** with read/unread state; push is only the delivery channel. There is no separate in-app pop-up or toast channel and no Telegram integration.

| Who | Receives | Link in the notification |
| --- | --- | --- |
| Citizens, for reports they follow | "Updates turned on" (follow confirmed), "Evidence received", "Report under review" (first time staff open the evidence) | `#/track/<id>` |
| Every active staff account | "New complaint", "New evidence" | `#/staff/<id>` |
| The caller | "Test notification" (button on the inbox page) | the inbox |

Each event has a dedupe key, so a recipient gets it at most once (for example only the first staff view triggers "Report under review"). An idempotent replay of a complaint creation does not notify again. Notifications are created in FastAPI background tasks after the response: a notification or push failure never fails or delays the request that caused it. Bodies contain the public complaint ID and category only: never descriptions, locations, claim codes, tokens or names.

### Recipients, ownership and devices

- **Staff** use their normal staff session token. Disabled accounts receive nothing.
- **Citizens** have no accounts. `POST /notifications/inboxes` creates a **notification inbox**: a random 256-bit bearer token kept in the browser's `localStorage` (only its SHA-256 hash is stored on the server). It can read and manage that inbox's notifications only; it opens no complaint or staff data. Following a complaint requires the complaint's **private claim code** (scrypt check, rate-limited like upload-token renewal), so nobody can follow someone else's report.
- **Multiple devices:** each recipient can register up to 10 push subscriptions (`push_subscriptions`, one row per device). Registering the same browser again updates its keys instead of creating a duplicate. A staff member's phone and computer both receive staff notifications. A citizen follows a report on each device with the claim code (each device has its own inbox).
- **Ownership:** every query is scoped to the caller's recipient; another recipient's notification or device IDs answer `404`, exactly like unknown IDs. Endpoints and keys of subscriptions are never returned by the API.
- **Signing out** of the staff console removes that browser's staff device, so a shared computer stops receiving staff notifications.

### Delivery and failure handling

| Push service answer | Effect |
| --- | --- |
| `201` | `push_status = sent`; the device's failure count resets |
| `404` / `410` (subscription expired or revoked) | the device is deleted immediately |
| `429`, `5xx`, network error | counted as a failure; the device is deleted after 5 failures in a row |
| other `4xx`, malformed keys | counted as a failure (same rule) |

Per notification, `push_status` is `sent`, `partial` (some devices failed), `failed`, `no_devices` or `disabled` (no VAPID keys configured). Devices whose `expirationTime` has passed, or that reached the failure limit, are also removed at every startup. There is no retry queue: a failed delivery is not re-sent, but the notification stays in the history. Logs contain HTTP status codes only, never endpoints, keys or the push service's response body.

**Endpoint allowlist (SSRF protection).** The server sends HTTP requests to subscription endpoints, so it accepts only `https` endpoints on port 443 at known push services: `fcm.googleapis.com` (Chrome, Edge on Android, Samsung Internet), `updates.push.services.mozilla.com` / `push.services.mozilla.com` (Firefox), `*.notify.windows.com` (Edge on Windows) and `*.push.apple.com` (Safari). Override with `CIVICLENS_PUSH_ENDPOINT_HOSTS` (comma-separated) if a browser uses another service.

### Setup

1. Generate a key pair once per deployment (keep the private key secret; changing it invalidates every existing subscription):

   ```sh
   cd backend
   .venv/Scripts/python -m app.push generate-vapid-keys
   ```

2. Put the three values into the server environment (see `backend/.env.example`; never in Git or frontend files):

   ```sh
   VAPID_PUBLIC_KEY=<base64url, 65-byte public key>
   VAPID_PRIVATE_KEY=<base64url, 32-byte private key>   # SECRET
   VAPID_SUBJECT=mailto:civic-ops@example.org            # or an https:// URL; push services use it to contact you
   # optional: CIVICLENS_PUSH_TTL_SECONDS=86400  (how long a push service keeps an undelivered message)
   ```

3. Start the API. A half-configured or mismatched key pair stops startup with a message naming the variable (the private key is never printed). Without keys, push is disabled: `GET /notifications/config` reports `pushEnabled: false`, the inbox page says so, and notifications are still stored in history.
4. Serve the frontend over **HTTPS** (or `http://localhost` on the same computer): browsers allow service workers and push only in secure contexts. Phones on the LAN need an HTTPS address.

The database tables (schema version 6: `notification_recipients`, `notification_follows`, `push_subscriptions`, `notifications`) are created automatically on SQLite and PostgreSQL / Supabase, and `python -m app.db_migrate` copies them too.

### Endpoints

All under `/api/v1/notifications`. Authorization: `Bearer <staff session token or citizen inbox token>` except where noted.

| Method and path | Result |
| --- | --- |
| `GET /config` (public) | `{"pushEnabled", "vapidPublicKey"}` |
| `POST /inboxes` (public, 10 per hour per IP) | `201 {"inboxToken"}` (shown once, `Cache-Control: no-store`) |
| `GET ?limit=30&before=<id>&unread=true` | `{"items": [{"id", "kind", "title", "body", "url", "complaintId", "createdAt", "readAt", "read", "pushStatus"}], "unreadCount", "nextBefore"}`, newest first |
| `GET /unread-count` | `{"unreadCount"}` |
| `POST /{id}/read`, `POST /{id}/unread` | the updated notification (the first read time is kept); `404` if not the caller's |
| `POST /read-all` | `{"updated", "unreadCount": 0}` |
| `POST /subscriptions` `{"endpoint", "keys": {"p256dh", "auth"}, "expirationTime", "label"}` | `201` new device, `200` same browser registered again; `422 invalid_subscription` / `unsupported_push_service`; `409 too_many_devices` |
| `GET /subscriptions` | `[{"subscriptionId", "label", "createdAt", "updatedAt", "lastSuccessAt", "lastFailureAt", "failureCount"}]` |
| `DELETE /subscriptions/{subscriptionId}` | `204`; `404` if not the caller's |
| `POST /follows` `{"complaintId", "claim"}` (citizens) | `201` and a confirmation notification; `200` if already followed; `401` wrong claim code; `403` for staff; `409 too_many_follows` (50) |
| `GET /follows`, `DELETE /follows/{publicId}` (citizens) | follow list / `204` |
| `POST /test` (5 per hour per recipient) | `202`; stores a test notification and pushes it to the caller's devices |

New rate limits: `CIVICLENS_RATE_INBOX_IP=10/3600`, `CIVICLENS_RATE_FOLLOW_IP=10/900`, `CIVICLENS_RATE_FOLLOW_COMPLAINT=10/3600`, `CIVICLENS_RATE_PUSH_REGISTER_IP=30/3600`, `CIVICLENS_RATE_PUSH_TEST=5/3600`.

### Frontend

- `#/notifications` (server mode): turn push on or off for this device, send a test, follow reports with ID and claim code, history with unread count, mark read/unread and "mark all as read", registered devices. The server receipt page offers **Notify me about this report**, and the tracking page links to the follow form. A device that already has notifications on follows its new reports automatically.
- `#/staff/notifications`: the same inbox for the signed-in staff member; the staff header shows the unread count.
- Permission is requested only after a click, never on page load. Blocked permission, unsupported browsers and an unconfigured server each get an explanation.
- Clicking a notification focuses an open CivicLens window and routes it (without reloading, so a staff session survives), or opens a new window; the notification is then marked read. Links outside the app are refused by the service worker and the API client.
- The service worker handles push only: it does not cache pages or intercept requests.

### Platform notes

- **Windows:** Edge and Chrome show notifications in the Windows notification centre. Background delivery while all windows are closed needs the browser to keep running in the background (Edge: "Continue running background extensions and apps when Microsoft Edge is closed"; Chrome: "Continue running background apps"); otherwise notifications arrive the next time the browser starts (within the TTL).
- **Android:** Chrome, Edge and Samsung Internet deliver push when the browser is closed; Android may delay delivery in battery-saving modes.
- **iPhone/iPad:** Web Push works only after "Add to Home Screen" (iOS 16.4+); there is no web app manifest yet, so this is untested.
- Push requires HTTPS for phones (a plain `http://` LAN address will not work).

### Testing

- `tests/test_notifications.py` (51): configuration, ownership and authorisation, follows, devices, validation and the endpoint allowlist, events and dedupe, history paging and read state, delivery outcomes, cleanup and failure isolation, against a fake push sender. `WebPushSender` itself is tested against a fake push service: the captured request is **decrypted with the subscriber's private key** and its **VAPID signature verified**, so real encryption and signing run. The whole file also runs on PostgreSQL (`CIVICLENS_TEST_DATABASE=postgres`).
- `tests/notifications.test.js`, `tests/notification-gateway.test.js` (frontend, Node): the API client, `push-client.js` (permission, subscribe, VAPID key change, disable, click handling) and `sw.js` loaded in a sandbox (push display, hostile payloads, click focus/open).
- `scripts/notifications-e2e.mjs`: a real browser (Edge by default) with temporary profiles, a real push subscription and **real delivery through the browser vendor's push service** to the service worker, history UI, read/unread, click navigation, two staff devices and sign-out cleanup. Needs the dev server, the API with VAPID keys and a staff account; see the file header.

## Supabase Storage evidence storage

**Why Supabase Storage.** Local files live on one machine: they are lost with the disk, cannot be shared between servers and are awkward to back up. A private Supabase Storage bucket keeps evidence durable and off the API machine, next to the Supabase Postgres database. The API, validation, authorisation, quota, idempotency and audit logging are identical in both modes.

| | `local` (default) | `supabase` |
| --- | --- | --- |
| Where files live | `backend/data/evidence/objects/` on the API machine | private Supabase bucket, objects `evidence/<2 hex>/<32 hex>` |
| How an upload is stored | temp file → fsync → atomic rename | staged to a private temp file → uploaded with `upsert: false`, content type, `cache-control: no-store` and the SHA-256 in metadata → size confirmed with an `info` call |
| Needs | nothing | a Supabase project, a private bucket and the service-role key |
| Startup check | folder exists, outside `dist/` | key, URL and bucket checked; startup stops if the bucket is **public**, its file size limit is below the upload limit, or its allowed MIME types exclude JPEG/WebM/MP4 |

The client is the official `storage3` package (the storage part of the Supabase Python SDK, without the realtime and auth extras). Downloads are streamed with the SDK's HTTP client because `storage3`'s `download()` reads whole files into memory.

### Create the project, bucket and key (Supabase dashboard)

1. **Project:** create one (or use the project whose Postgres you use for `DATABASE_URL`).
2. **Bucket:** Storage → New bucket. Name it, for example, `civiclens-evidence`. Leave **Public bucket off**. Optionally set allowed MIME types (`image/jpeg, video/webm, video/mp4`) and a file size limit; the limit must be at least the upload limits (`CIVICLENS_MAX_PHOTO_BYTES` / `CIVICLENS_MAX_VIDEO_BYTES`). The free plan caps single files at 50 MB, so keep the video limit at or below that there. Add **no storage policies**: the backend uses the service-role key, which bypasses RLS, and nothing else should be able to read the bucket.
3. **Project URL:** Project Settings → Data API (or the Connect dialog): `https://<project-ref>.supabase.co`.
4. **Service-role key:** Project Settings → API keys → the `service_role` key (legacy JWT) or a **secret** key (`sb_secret_…`). Never the anon/publishable key (the app refuses it). Treat it like a database password: server environment only, never in `dist/`, the browser, logs or Git.

### Enable Supabase mode

Set these in the server's environment (see `backend/.env.example`; never commit real values, never put them in frontend files):

```sh
EVIDENCE_STORAGE_BACKEND=supabase
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service-role or secret key>
SUPABASE_STORAGE_BUCKET=civiclens-evidence
```

Then start the API as usual (`.venv/Scripts/python -m app`). A missing or malformed setting stops the app before it starts, naming the variable (the key is never printed: it is excluded from the settings' `repr` and from error messages). `https://` is required except for `localhost` (a local Supabase CLI stack).

**Back to local mode:** unset `EVIDENCE_STORAGE_BACKEND` (or set it to `local`). Evidence already stored in Supabase then cannot be viewed (staff get `503` and nothing is logged) until Supabase is configured again; local evidence is always readable.

### Private retrieval: streaming, not signed URLs

Staff evidence is streamed through `GET /api/v1/staff/complaints/{id}/evidence` in both modes: the API checks the staff session, writes the access-log row, then copies the object from Supabase to the browser in 64 KB chunks with `Cache-Control: no-store`. No public URL and no signed URL is ever created.

Signed URLs were considered and deliberately not used: a signed URL is a bearer link that anyone who obtains it can use until it expires, the audit log could only record that a link was issued (not that the file was viewed), and the browser would talk to Supabase directly. The cost of streaming is bandwidth on the API server, which is acceptable for photos and short videos at civic-pilot scale. If that becomes a bottleneck, short-lived (≤ 60 s) single-object signed URLs issued only after the same authorisation and audit step would be the next option.

### Failure handling

| Situation | Behaviour |
| --- | --- |
| Invalid configuration (missing variable, anon key, bad URL or bucket name) | app refuses to build: `StorageConfigError` naming the variable |
| Wrong key, missing bucket, unreachable project, public bucket, too-small size limit, MIME list excluding our types | startup stops with a specific message |
| Supabase outage or network error during an upload | `503 storage_unavailable`; no evidence row; quota reservation released; failed connections are retried twice |
| Upload stored but size confirmation fails | object deleted, `503` |
| Object stored but the database insert fails (SQLite or PostgreSQL) | object deleted (compensation), `503` (if that delete also fails, the object is left unreferenced and logged; there is no Supabase orphan audit yet) |
| Object missing or Supabase unreachable when staff view | `503`, nothing logged, no bytes |
| Identical retry / different file | `200 already_uploaded` / `409 evidence_exists`, unchanged |

Error messages and logs carry HTTP status or error codes only: never the key, auth headers or object names. The `httpx` request logger is set to WARNING so request URLs are not logged.

### Testing without Supabase

`tests/test_supabase_storage.py` runs configuration, startup checks, the adapter and the full API flow (upload, staff view, audit log, outages, compensation after a database failure, local-to-Supabase switch, local mode never contacting Supabase) against an in-process fake of the Supabase Storage HTTP API (`httpx.MockTransport`). No real project, credentials, network calls or charges:

```sh
cd backend
.venv/Scripts/python -m pytest tests/test_supabase_storage.py -v
```

**A real Supabase project has not been tested yet.** To check one manually after setting the variables: start the API, create a staff account, enable backend mode in the frontend, submit a report with a photo, confirm the object appears in the bucket (Storage → bucket → `evidence/`), and view it in the staff console.

### Not included

No automatic migration of existing local evidence to Supabase (a future, separately reviewed task: copy each file, verify the checksum, then update `storage_backend` row by row). The `evidence_audit` command covers local files only. Startup lists the bucket's `evidence/` folders once to measure quota usage, which is slow for very large buckets.

## Evidence storage audit

Dry run (read-only, the default):

```sh
cd backend
.venv/Scripts/python -m app.evidence_audit
.venv/Scripts/python -m app.evidence_audit --json
.venv/Scripts/python -m app.evidence_audit --db <path.sqlite3> --evidence-dir <evidence folder>
```

It covers **local** evidence only (rows with `storage_backend = 'local'`); Supabase Storage objects are not audited by this command. With `DATABASE_BACKEND=postgres` it reads the PostgreSQL database (in a read-only transaction) instead of the SQLite file. It reports recorded and on-disk files and bytes, **orphans** (files with no `evidence` row), **missing files** (rows whose file is gone), size mismatches, unexpected files (names that are not storage keys) and temporary uploads. Exit code 0 when consistent, 1 when files are missing or mismatched, 2 when the database cannot be read.

Deliberate cleanup (destructive, explicit flags required):

```sh
.venv/Scripts/python -m app.evidence_audit --delete-orphans --min-age-minutes 60
.venv/Scripts/python -m app.evidence_audit --delete-stale-temp --min-age-minutes 60
```

Safety rules: the database is opened read-only and never modified; only regular files under `<evidence>/objects/` with valid storage-key names are deletable (unexpected files, symlinks and anything outside the folder are left alone); files younger than `--min-age-minutes` (default 60) are kept; the database is queried again immediately before each deletion; missing files are reported, never "fixed". Stop the server before deleting if you want to rule out an upload finishing at the same moment.

### CORS (browser access)

Only listed origins may call the API from a browser. The default is `http://localhost:4173` (the `npm run dev` frontend). Credentials are not allowed (there are no cookies or sessions), only `GET`/`POST`/`DELETE` (sign-out) with the `Content-Type`, `Authorization` (upload token or staff session) and `Idempotency-Key` headers are permitted, and a wildcard `*` is refused at startup.

To allow other origins, set a comma-separated list before starting the server:

```sh
CIVICLENS_CORS_ORIGINS="http://localhost:4173,http://127.0.0.1:4173" .venv/Scripts/python -m uvicorn app.main:app --port 8000
```

An empty value allows no browser origins. Note that `http://127.0.0.1:4173` is a different origin from `http://localhost:4173`.

### Using it from the frontend

The frontend reads `dist/api-config.json`:

```json
{"mode": "local", "baseUrl": "http://localhost:8000/api/v1"}
```

- `"mode": "local"` (the default, also used when the file is missing): nothing is sent to this API.
- `"mode": "backend"`: report submission and public tracking go to `baseUrl`. The complaint details are created first, then the photo or video is uploaded as a separate step (with a retry button if it fails). A copy of the evidence also stays in the browser's IndexedDB.
- Any other value, or a backend `baseUrl` that is not an absolute http(s) URL, blocks reporting and tracking with an error instead of guessing.

Reload the page after changing the file. Full details are in `../docs/IMPLEMENTATION_PROGRESS.md` (Phase 3.3).

## AI analysis (Roboflow)

Photos uploaded by citizens, and after-repair photos uploaded to work orders, can be analysed by Roboflow in
the background. Staff see the detections (category, model confidence, boxes over the evidence) and a
before/after repair suggestion that a supervisor approves or rejects. **Nothing is decided automatically:**
AI never changes a complaint's status, its citizen verification or a work order. Design and rules:
`docs/ARCHITECTURE.md` section 7; API: `docs/api-contract.md` section 8.1.

### Enable it

```
export CIVICLENS_AI_PROVIDER=roboflow
export ROBOFLOW_API_KEY=...          # Roboflow -> Settings -> API Keys. SECRET: backend environment only.
python -m app
```

Without both variables AI is off (`GET /api/v1/ai` says so, the staff console shows why) and complaints work
exactly as before. The key is sent only in the `Authorization: Bearer` header (inference-sdk's
`api_key_transport="header"`), never in URLs, logs, the database or responses. The server needs outbound HTTPS
to `serverless.roboflow.com` (or `ROBOFLOW_API_URL`).

### Detectors, models and workflows

| Detector | Default | Variables |
| --- | --- | --- |
| `pothole` | workflow `mohammed-arshad-nb1p3/general-segmentation-api-18`, classes `potholes` | `ROBOFLOW_POTHOLE_MODE`, `_WORKFLOW_ID`, `_CLASSES` (or `_MODEL_ID` in model mode) |
| `pavement` | model `pavement-gvib5/1` | `ROBOFLOW_PAVEMENT_MODE`, `_MODEL_ID` |
| `garbage` | workflow `general-segmentation-api-20`, classes `box, garbage, z` | `ROBOFLOW_GARBAGE_*` |
| `infrastructure` | workflow `general-segmentation-api-19`, classes `pothole, graffiti, damaged-sign, electricity-pole-damage, fallen-trees` | `ROBOFLOW_INFRASTRUCTURE_*` |

`ROBOFLOW_WORKSPACE` (default `mohammed-arshad-nb1p3`) applies to workflows. To switch a detector to a trained
Universe model, e.g. garbage: `ROBOFLOW_GARBAGE_MODE=model` and `ROBOFLOW_GARBAGE_MODEL_ID=garbage-dumping-6avsy/<version>`,
using the exact ID from the project's Deploy tab; no version is assumed by the code. `CIVICLENS_AI_DETECTORS`
limits which detectors run. Which detectors run per complaint category, and how class names map to categories,
can be overridden with `CIVICLENS_AI_CATEGORY_ROUTES` and `CIVICLENS_AI_CLASS_MAP` (JSON; examples in
`.env.example`). A malformed value stops startup with a message naming the variable.

**What has been tested:** the request formats were taken from inference-sdk 1.8.3's source, and every adapter is
tested against mocked HTTP responses. **None of the four detectors has been called for real from this code yet**
(the development sandbox could not reach Roboflow), so the real response shape of each workflow, and the class
names `pavement-gvib5/1` returns, still need a live check: run the smoke test below and adjust
`CIVICLENS_AI_CLASS_MAP` if a class shows up with category `null`.

### Live smoke test (needs the key and network access)

```
CIVICLENS_AI_PROVIDER=roboflow ROBOFLOW_API_KEY=... python -c "
from app.ai.model_registry import AiSettings; from app.ai.roboflow_service import RoboflowClient
from app.ai.images import prepare; from app.ai.result_normalizer import normalise
s = AiSettings.from_env(); c = RoboflowClient(s); p = prepare(open('pothole.jpg','rb').read(), s.max_side_px)
for name, spec in s.detectors.items():
    try: r = normalise(c.detect(spec, p.upload_jpeg), s, p.upload_size, (p.width, p.height)); print(name, r['topClass'], r['topCategory'], r['topConfidence'], r['confidenceMeta']['returned'])
    except Exception as e: print(name, 'FAILED', getattr(e, 'code', type(e).__name__))
"
```

### How it runs

After an upload is committed and answered, a background task creates one `ai_inferences` row per routed
detector and hands the photo to a small in-process thread pool (`CIVICLENS_AI_WORKERS`, default 2; at most
`CIVICLENS_AI_MAX_QUEUE` queued). The worker reads the file through the private storage backend, re-encodes it
without metadata (no GPS leaves the server), records `ai_evidence_sent` in the audit log, calls Roboflow
(30 s timeout, at most one retry) and stores the normalised result or an error code. Videos are never sent
(`skipped`, `unsupported_media`). A repair photo also creates a `repair_assessments` row that is computed once
both photos have results. Jobs are rows, so a restart resumes pending work; a stuck `running` job is retried
after 10 minutes; `POST /staff/complaints/{id}/ai/retry` re-runs failed jobs (up to 5 attempts).

### Schema 8

`ai_inferences` (one row per evidence file and detector: provider, mechanism, model/workflow ID, version, class
prompt, state, top class and category, confidence, normalised predictions and confidence metadata as JSON —
JSONB on PostgreSQL —, image size, error code, attempts, timestamps), `repair_assessments` (original and repair
evidence, work order, suggested outcome, verification confidence, location check, GPS distance, details JSON,
requester, review status, reviewer, note, timestamps) and `repair_assessment_inferences` (which inference rows
an assessment compared). Applied automatically at startup on SQLite and PostgreSQL; `python -m app.db_migrate`
copies the new tables too.

## Test

```sh
cd backend
.venv/Scripts/python -m pytest -v
```

Every test runs against its own temporary database (see `tests/conftest.py`); the development database is never touched.

PostgreSQL tests need a real, disposable PostgreSQL. With Docker:

```sh
docker run -d --rm --name civiclens-pgtest -e POSTGRES_PASSWORD=civiclens_test -e POSTGRES_DB=civiclens_test -p 127.0.0.1:55432:5432 postgres:15-alpine
export CIVICLENS_TEST_POSTGRES_URL=postgresql://postgres:civiclens_test@127.0.0.1:55432/civiclens_test
.venv/Scripts/python -m pytest -v                                   # SQLite suite + dedicated PostgreSQL tests
CIVICLENS_TEST_DATABASE=postgres .venv/Scripts/python -m pytest -v  # the whole suite on PostgreSQL
docker stop civiclens-pgtest
```

Without `CIVICLENS_TEST_POSTGRES_URL` the PostgreSQL tests are skipped. In PostgreSQL mode each test gets its own schema, and tests marked `sqlite_only` (SQLite files, PRAGMA, SQLite triggers) are skipped; their PostgreSQL equivalents are in `tests/test_postgres.py`. Never point these variables at a real Supabase project: tests create and drop schemas.

## Load and Stress Testing

A standalone load and stress testing harness is available in scripts/load-stress-harness.py. It tests the live API across all critical paths and concurrency boundaries.

### Executing Stress Workloads

`sh
# Run against local API (default http://127.0.0.1:8000 or --base http://127.0.0.1:8010)
python scripts/load-stress-harness.py --all

# Run individual workload (e.g. concurrent complaints, uploads, rate limits)
python scripts/load-stress-harness.py --workload 1
python scripts/load-stress-harness.py --workload 3

# Run 100-request burst simulation within 500ms
python scripts/load-stress-harness.py --burst

# Run 5-minute sustained load test
python scripts/load-stress-harness.py --sustained --duration 300
`

### Sustained Performance Benchmarks (2026-10-30)

| Metric | Result |
| --- | --- |
| **Duration** | 300 seconds (5 minutes) |
| **Total Requests** | 29,527 |
| **Successful Requests** | 29,527 (100.0%) |
| **Failures / Errors** | 0 (0.0%) |
| **Throughput** | 98.42 requests/second |
| **p50 Latency** | 24.56 ms |
| **p90 Latency** | 74.07 ms |
| **p95 Latency** | 145.47 ms |
| **p99 Latency** | 546.82 ms |
| **Peak Memory (RSS)** | 47.5 MB |
| **Database Locks / Corruption** | 0 detected |

## Endpoints

### `GET /api/v1/health` → `200`

```json
{"status": "ok", "service": "namma-civiclens-api", "api_version": "v1", "version": "0.1.0"}
```

A liveness check only. It does not test the database.

### `POST /api/v1/complaints` → `201 Created`

Creates a complaint from **metadata only**. No photo or video is uploaded, and nothing here verifies that evidence exists or is genuine. Media upload is a later, separate endpoint.

Request (unknown fields are rejected with `422`):

```json
{
  "category": "Potholes",
  "mediaType": "photo",
  "description": "Deep pothole near the bus stop",
  "shareEvidence": false,
  "neighbourhood": "Yelahanka",
  "gps": {
    "lat": 13.1007, "lng": 77.5963, "accuracy": 12.5,
    "sourceType": "gps", "source": "Device GPS",
    "timestamp": 1793311200000, "fixAgeSeconds": 5,
    "address": "optional address from the lookup"
  },
  "capturedAt": "2026-10-30T09:12:00.000Z",
  "captureSource": "Live camera"
}
```

| Field | Rule |
| --- | --- |
| `category` | Required. One of the 9 categories in `dist/services.js` |
| `mediaType` | `photo` (default) or `video` |
| `description` | Optional, at most 1,000 characters |
| `shareEvidence` | Boolean, default `false` |
| `neighbourhood` | Optional, at most 120 characters. Becomes the public `area`; default `Bengaluru · location on file` |
| `gps` | **Required** (the API contract requires coordinates). `lat` −90..90, `lng` −180..180, `accuracy` ≥ 0 or null, `sourceType` `gps`/`exif`/`manual`. `timestamp` is the browser's fix time in epoch milliseconds (as the Geolocation API gives it), or null; not before 2000 and not in the future. NaN/Infinity rejected. Also accepts `source`, `fixAgeSeconds`, `neighbourhood`, `address` as the capture screen sends them |
| `capturedAt` | Optional ISO 8601 time **with a timezone**, not in the future. The frontend calls this `created`; both names are accepted |
| `captureSource` | Optional, at most 80 characters |
| `owner`, `uploadedAt`, `claim`, `status`, … | **Rejected.** There is no authentication, so the server does not trust an owner sent by the client; upload time, claim code and status are set by the server |

A future time allows 10 minutes of clock difference.

Response — the **creation receipt** (`ComplaintReceipt`), sent with `Cache-Control: no-store` and a `Location` header:

```json
{
  "id": "NCL-2026-K7Q2MX",
  "claim": "1b4e28ba-2fa1-41d2-883f-0016d3cca427",
  "category": "Potholes", "area": "Yelahanka",
  "created": "...", "capturedAt": "...", "uploadedAt": "...", "captureSource": "Live camera",
  "gps": {"lat": 13.1007, "lng": 77.5963, "accuracy": 12.5, "source": "Device GPS", "sourceType": "gps", "timestamp": "2026-10-30T09:11:55.000Z", "address": null},
  "mediaType": "photo", "shareEvidence": false,
  "status": "Submitted", "verification": "Pending Verification",
  "uploadToken": "Q2h…(43 characters)", "uploadTokenExpiresAt": "...",
  "replayed": false
}
```

Optional header `Idempotency-Key` makes retries safe; see "Idempotent complaint creation" above.

The receipt is the **only** response that ever contains the claim code. Show it to the submitter once; the server cannot show it again. Note that `gps.timestamp` comes back as ISO 8601, not epoch milliseconds. `uploadToken` authorises the evidence upload below; keep it in memory only.

### `POST /api/v1/complaints/{publicId}/evidence` → `201 Created`

Uploads the complaint's original photo or video. Requires `Authorization: Bearer <uploadToken>`. Body: `multipart/form-data` with exactly one `file` part and an optional `capturedAt` text part (ISO 8601 with timezone, not in the future). Any other field is rejected.

| Rule | Detail |
| --- | --- |
| Accepted types | Decided from the first bytes: JPEG (`image/jpeg`, photo), WebM (`video/webm`, EBML DocType `webm`) and MP4 (`video/mp4`, `ftyp` brands isom/iso2/iso4/iso5/iso6/mp41/mp42/avc1/M4V/dash). PNG, GIF, MOV (`qt`), Matroska and anything else: `415`. These are the formats the capture flow produces (canvas JPEG, MediaRecorder WebM in Chromium/Firefox, MP4 in Safari). |
| Consistency | The part's declared type (parameters ignored) must be the detected type or `application/octet-stream`; a filename extension, if any, must match (`.jpg/.jpeg`, `.webm`, `.mp4/.m4v`): else `422 type_mismatch`. The file must match the complaint's `mediaType`: else `422 media_type_mismatch`. |
| Size | Photo ≤ 25 MB, video ≤ 200 MB (`CIVICLENS_MAX_PHOTO_BYTES`, `CIVICLENS_MAX_VIDEO_BYTES`). A larger `Content-Length` is refused before reading; the stream is also counted while reading, so chunked bodies are limited too: `413`. Empty file: `422 empty_file`. |
| Streaming | Read in chunks, written straight to a temporary file while the SHA-256 is computed; nothing is held in memory beyond one chunk. A pause longer than 30 s (`CIVICLENS_UPLOAD_READ_TIMEOUT`) stops the upload: `408`. A body that ends before the closing boundary: `400 incomplete_upload`. |
| Retries | One original per complaint. The same file again → `200` with `"status": "already_uploaded"` and the same `evidenceId` (no duplicate file or row, also under concurrent retries). A different file → `409 evidence_exists`. |

Response:

```json
{"evidenceId": "ev_…", "status": "uploaded", "mediaType": "photo", "contentType": "image/jpeg",
 "byteSize": 482113, "sha256": "…64 hex…", "capturedAt": "…" , "uploadedAt": "…"}
```

Never returned: storage key, filesystem path, URL. File contents, filenames, tokens and claim codes are never logged.

### `POST /api/v1/complaints/{publicId}/upload-token` → `200`

Body `{"claim": "<claim code>"}`. If the claim code matches (scrypt, constant-time), returns `{"uploadToken", "uploadTokenExpiresAt"}` and **replaces** the previous token. Wrong code: `401 unauthorized` (the current token stays valid). Unknown complaint: `404`. Use it when the first token has expired (default lifetime 1 hour, `CIVICLENS_UPLOAD_TOKEN_TTL_SECONDS`).

**Security assumptions:** the upload token proves possession of the creation receipt, and the claim code proves possession of the private code. Neither proves identity. Tokens are 256-bit random, stored as SHA-256 hashes, sent only in the `Authorization` header and expire. The claim exchange runs scrypt (~16 MB, tens of ms) per request; since Phase 3.5 it is rate-limited per IP and per complaint before scrypt runs.

### `GET /api/v1/complaints/{publicId}` → `200` (public tracking)

Returns the `PublicComplaint` projection, the same allowlist as `api.publicRecord()` in `dist/services.js`:

```json
{
  "id": "NCL-2026-K7Q2MX", "category": "Potholes", "area": "Yelahanka",
  "status": "Submitted", "verification": "Pending Verification",
  "department": "Awaiting civic review", "updated": "...",
  "timeline": [{"status": "Submitted", "text": "Report received by the Namma CivicLens API.", "date": "..."}],
  "verificationLog": []
}
```

Never included: claim code or hash, owner, precise GPS, address, description, evidence (no ID, checksum, key or link), internal fields.

IDs must look like `NCL-<4 digits>-<6 uppercase letters or digits>`, which covers generated IDs and the demo format `NCL-2026-DEMO01`. Unknown or malformed IDs both return `404 not_found` with the same message.

### Receipt vs. public tracking

| | Creation receipt | Public tracking |
| --- | --- | --- |
| Who gets it | The submitter, once, in the `POST` response | Anyone with the public ID |
| Claim code | Yes, plaintext, only here | Never |
| Precise GPS | Yes (the submitter's own evidence) | Never |
| Lifecycle and timeline | Initial status only | Status, verification, timeline, verification history |

## Claim codes and public IDs

- **Public ID:** `NCL-<year in IST>-` plus 6 characters from `ABCDEFGHJKLMNPQRSTUVWXYZ23456789` (the alphabet of `referenceId()` in `dist/services.js`), chosen with Python's `secrets` module. Uniqueness is enforced by the database. On a collision the server retries with a new ID up to 5 times, then fails with `503` without storing anything.
- **Claim code:** a random UUID v4 (122 random bits from the OS's secure random generator), the same format the browser prototype issues.
- **Storage:** only `scrypt$N$r$p$salt$hash` is stored (scrypt with N=2^14, r=8, p=1, a 16-byte random salt per code, a 32-byte hash). `claims.verify_claim_code()` checks a code in constant time; the future claim endpoint will use it.
- The plaintext code is never logged or written to the database.

## Errors

All errors use the shape from `docs/api-contract.md`:

| Situation | Status | `error.code` |
| --- | --- | --- |
| Unknown path, unknown or malformed complaint ID | 404 | `not_found` |
| Wrong method | 405 | `method_not_allowed` |
| Invalid body or unknown field | 422 | `validation_error` (with `details: [{field, message}]`) |
| Malformed `Idempotency-Key` | 400 | `invalid_idempotency_key` |
| Idempotency key reused with a different request / already completed | 409 | `idempotency_key_conflict` / `idempotency_key_used` (with `complaintId`) |
| Rate limit exceeded | 429 | `rate_limited` (with `Retry-After`) |
| Evidence storage quota exceeded | 507 | `storage_quota_exceeded` |
| Missing, wrong or expired upload token; wrong claim code | 401 | `unauthorized` / `token_expired` |
| Upload stalled | 408 | `request_timeout` |
| Different evidence already uploaded | 409 | `evidence_exists` |
| Staff evidence request for a complaint with no upload | 404 | `evidence_not_found` |
| Upload too large | 413 | `payload_too_large` |
| Unsupported file type, or not multipart | 415 | `unsupported_media_type` |
| File type, extension or complaint media type disagree; empty file | 422 | `type_mismatch` / `media_type_mismatch` / `empty_file` |
| Malformed or truncated multipart body | 400 | `bad_request` / `incomplete_upload` |
| Database or evidence storage unavailable, failed write, ID allocation exhausted | 503 | `storage_unavailable` (generic message; the cause is logged server-side) |
| Any other server error | 500 | `internal_error` (generic message; no exception text or stack trace returned) |

A failed create rolls back completely: no half-written complaint is left behind.

## Complaint management (Workstream 1, schema 7)

Staff manage complaints through the API in `docs/api-contract.md` §3.2. Design notes:

- **Two state machines.** `status` (Submitted → Under Review → Assigned → In Progress → Marked Resolved, plus a few controlled backward moves) is changed only by staff. `verification` (Pending Verification, Citizen Confirmed, Disputed, Reinspection Required) is changed by citizens with the claim code and by a supervisor's reinspection request. The rules, roles and department scoping are pure functions in `app/lifecycle.py`, so another channel (for example a Telegram bot) must call the same store methods rather than write SQL.
- **One transaction per action.** `store.py` locks the complaint row (`FOR UPDATE` on PostgreSQL, an immediate transaction on SQLite), re-reads the actor's role and departments, checks the rule, and writes the change, its public timeline or verification entry, and an `audit_log` row together. The client sends `expectedStatus`; if the case moved on, the result is `409 conflict` rather than a lost update.
- **Visibility by table.** Internal notes live in `staff_notes`, which the public projection never queries. Public timeline text names departments and teams, never individual staff.
- **Append-only history.** `audit_log`, `complaint_assignments`, `staff_notes` and `work_order_events` have no update or delete path in the code, and database triggers refuse `UPDATE` and `DELETE` (and `TRUNCATE` on `audit_log` in PostgreSQL).
- **After-repair evidence** reuses the evidence pipeline and the `EvidenceStorage` interface unchanged: an `evidence` row with role `repair` (still exactly one `original` per complaint, enforced by a partial unique index), linked to the work order as "after". The orphan audit and the access log cover it automatically.
- **Notifications** are not sent for management events yet. The notification kinds are a `CHECK` list in the notifications table, which belongs to the notification work; status-change events need an agreed interface first.

Set up roles and departments with the CLI (the database follows `DATABASE_BACKEND`, like the API):

```bash
.venv/Scripts/python -m app.staff department add --code roads --name "Road maintenance"
.venv/Scripts/python -m app.staff create --username ravi --display-name "Ravi" --role supervisor
.venv/Scripts/python -m app.staff join --username asha --department roads
.venv/Scripts/python -m app.staff set-role --username asha --role supervisor
.venv/Scripts/python -m app.staff list
```

Migration 7 upgrades an existing database in place and keeps every row. Existing staff stay `official`, and existing complaints are unassigned (the triage queue). On SQLite, two tables (`staff_accounts`, `evidence`) are rebuilt to change their constraints; `apply_schema` turns foreign keys off for that and checks them before committing. On PostgreSQL the constraints are altered in place. Take a backup before deploying it to Supabase (`pg_dump`, or the dashboard's backup) and let the API apply it at startup, as with earlier migrations.

## Duplicate detection (Workstream 2, schema 8)

`GET /staff/complaints/{id}/duplicates` returns ranked, explained suggestions (see `docs/api-contract.md` §3.3). Supervisors merge, revert or dismiss; nothing is merged automatically.

- **Candidates** come from a bounding-box query on the new `(gps_lat, gps_lng)` index (plain comparisons, identical on SQLite and PostgreSQL), filtered by category, time window, merge state and dismissed pairs, then measured exactly with the haversine distance in Python. No PostGIS or `pg_trgm`; both engines behave the same.
- **Scoring** (`app/duplicates.py`) weighs location relative to the two reports' combined GPS uncertainty, so precise fixes 40 m apart are separate issues while coarse fixes at the same distance may be one. It also weighs category, time and description overlap. Each factor's contribution is returned, so a supervisor can see why.
- **Merges** (`complaint_merges`) keep both complaints intact. `complaints.merged_into_pk` points at the canonical complaint, a partial unique index allows one active merge per duplicate, and rows cannot be deleted. Chains and double merges are refused under row locks. Every merge, revert and dismissal is in the audit log of both complaints.
- **Not done:** perceptual image similarity (needs Pillow/imagehash and decoding private files; proposed separately); followers of a merged report receiving the canonical complaint's notifications (an interface request to the notification work).

Migration 8 only adds a column, two tables and indexes; no existing row changes.

## Not included yet

Shared (multi-process) rate limiting, **public** evidence viewing (staff and owners can view; never permanent public links), MFA, migration of existing local evidence to Supabase Storage, a Supabase Storage orphan audit, server-side EXIF re-parsing, malware scanning, transcoding, PostGIS, AI models, notifications for case status changes (needs an agreed interface with the notification work), a push retry queue, Telegram. Each is a later phase; see `../docs/IMPLEMENTATION_PROGRESS.md`.
