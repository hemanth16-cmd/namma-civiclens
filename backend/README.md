# Namma CivicLens — Backend

**Status (Phase 3.6):** a FastAPI service backed by SQLite and private local file storage, with development-grade rate limits, an evidence storage quota, idempotent complaint creation and a staff console API. It creates complaints from metadata, serves public tracking, accepts the complaint's original photo or video through an authorised, streamed upload, and lets **signed-in staff** list complaints and view evidence (every view is logged). The frontend in `dist/` can use it in **opt-in backend mode** (see "Using it from the frontend" below); by default the frontend still runs in local mode with `localStorage`/IndexedDB. Owners and the public cannot view uploaded evidence. Everything else in `../docs/api-contract.md` (claiming, staff case updates, repairs, maps) is still **proposed, not implemented**.

> **No user authentication; development-grade abuse protection only.** Anyone who can reach the server can create complaints and look them up by ID. Phase 3.5 added per-IP/per-complaint rate limits, an evidence storage quota and idempotent creation, but the limits live in one process's memory (reset on restart, not shared between workers). Before any public deployment it still needs shared rate limiting, HTTPS, abuse/spam controls and server-side authentication for anything beyond anonymous submit and public tracking.

## Layout

```
backend/
  app/
    __init__.py        API_VERSION ("v1"), APP_VERSION, SERVICE_NAME
    main.py            create_app() factory, lifespan (creates the schema), /api/v1 router, health
    complaints.py      POST /complaints, GET /complaints/{publicId}
    models.py          Pydantic request/response schemas (camelCase, matching dist/services.js)
    store.py           SQLite data-access layer: the only module that runs SQL
    claims.py          Public ID and claim-code generation, scrypt hashing/verification
    cors.py            Explicit browser-origin allowlist (default http://localhost:4173)
    evidence.py        POST /complaints/{id}/evidence: streamed multipart parsing, signature checks, limits
    evidence_storage.py EvidenceStorage interface + LocalEvidenceStorage (private disk storage)
    ratelimit.py       RateLimiter interface, in-memory sliding window, client-IP / trusted-proxy rules
    quota.py           Total evidence storage quota with reservations
    evidence_audit.py  Read-only-by-default audit: orphan files, missing files (python -m app.evidence_audit)
    __main__.py        Safe launcher (python -m app): uvicorn with proxy headers OFF
    auth.py            Staff passwords (scrypt), bearer sessions, current_staff dependency
    staff.py           Staff account CLI (python -m app.staff create|list|disable|enable|set-password)
    staff_api.py       /staff routes: sign-in/out, complaint list/detail, evidence streaming, access log
    errors.py          Error envelope {"error": {"code", "message"[, "details"]}}
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
  data/                Default database and evidence location (created on first start; git-ignored)
    civiclens.sqlite3  complaints, timeline, verification log, evidence metadata
    evidence/objects/  stored files, named by random key (objects/<first 2 hex>/<32 hex>)
    evidence/tmp/      in-progress uploads (*.part), removed on failure; stale ones removed at startup
  requirements.txt     Runtime: fastapi, uvicorn, python-multipart (pinned)
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

- **Engine:** SQLite through Python's standard-library `sqlite3`. No ORM.
- **Default file:** `backend/data/civiclens.sqlite3`. The folder and schema are created automatically when the server starts. Restarting keeps all data.
- **Choose another file:** set `CIVICLENS_DB_PATH`, for example `CIVICLENS_DB_PATH=/tmp/dev.sqlite3`. Code can also pass `create_app(db_path=...)`.
- **Reset local data:** stop the server and delete the database file and its `-wal`/`-shm` companions. (This erases every stored complaint.)
- **Git:** `data/`, `*.sqlite3`, `*.sqlite3-*`, `*.db` and journal files are ignored.
- Journal mode is WAL; foreign keys are on; schema version is stored in `PRAGMA user_version` (currently 4). Older databases are migrated in place at startup (1 → 2 adds the upload-token columns and the `evidence` table; 2 → 3 adds `complaints.credentials_used_at` and the `idempotency_keys` table; 3 → 4 adds `staff_accounts`, `staff_sessions` and `evidence_access_log`; existing complaints are kept).

Tables:

| Table | Purpose |
| --- | --- |
| `complaints` | One row per complaint. `public_id` is `UNIQUE`. Holds `claim_hash` (never the code), category, area, description, media type, share flag, GPS fields, capture/creation/upload/update times, lifecycle `status`, `verification`, `department`. CHECK constraints guard coordinate ranges, media type, source type. |
| `complaint_timeline` | Append-only public timeline entries (`status`, `text`, `date`). |
| `verification_log` | Append-only public verification history. Nothing writes to it yet; it is read so the public response matches the prototype's shape. |
| `evidence` | Metadata for uploaded files: opaque `evidence_id`, `complaint_pk`, `role` (`original`), server-generated `storage_key` (UNIQUE), `media_type`, `content_type`, `byte_size`, `sha256`, `captured_at`, `uploaded_at`. `UNIQUE (complaint_pk, role)`: one original per complaint. No file contents. |

`complaints` also holds `upload_token_hash` (SHA-256 of the current upload token) and `upload_token_expires_at`.

## Evidence storage

- **Interface:** `EvidenceStorage` (`begin` → write chunks → `commit` returns a key, or `abort`; `delete`; `exists`). `LocalEvidenceStorage` is the development implementation; an S3-compatible one can replace it without changing routes.
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

Limits of this design: one role only; no MFA, password-reset flow or lockout beyond rate limits; the access log stores the client IP (personal data, kept indefinitely for now); evidence is still not available to owners or the public; `http://` only in development, so tokens travel unencrypted on anything but localhost.

The frontend's staff console is `#/staff` in backend mode (see `docs/IMPLEMENTATION_PROGRESS.md`); it keeps the token in page memory only, so reloading signs the tab out.

## Evidence storage audit

Dry run (read-only, the default):

```sh
cd backend
.venv/Scripts/python -m app.evidence_audit
.venv/Scripts/python -m app.evidence_audit --json
.venv/Scripts/python -m app.evidence_audit --db <path.sqlite3> --evidence-dir <evidence folder>
```

It reports recorded and on-disk files and bytes, **orphans** (files with no `evidence` row), **missing files** (rows whose file is gone), size mismatches, unexpected files (names that are not storage keys) and temporary uploads. Exit code 0 when consistent, 1 when files are missing or mismatched, 2 when the database cannot be read.

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

## Test

```sh
cd backend
.venv/Scripts/python -m pytest -v
```

Every test runs against its own temporary database (see `tests/conftest.py`); the development database is never touched.

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

## Not included yet

Authentication and sessions, shared (multi-process) rate limiting, claiming complaints, staff operations, repairs, **evidence viewing for owners or the public** (staff viewing exists since Phase 3.6; future object storage would use short-lived signed URLs, never permanent public links), staff case updates and work orders on the server, more staff roles, MFA, object storage, server-side EXIF re-parsing, malware scanning, transcoding, PostgreSQL/PostGIS, AI models, notifications. Each is a later phase; see `../docs/IMPLEMENTATION_PROGRESS.md`.
