"""Data-access layer for complaints, for SQLite (default) and PostgreSQL (Supabase).

This is the only module that contains SQL. Routes call ComplaintStore methods and receive plain
dicts. The SQL is written once, with `?` placeholders; db.py supplies the engine-specific parts
(connections, placeholder translation, inserts returning keys, transactions, schema bootstrap).

Rules kept here:
- Every query is parameterised.
- A complaint and its first timeline entry are written in one transaction.
- Public IDs are UNIQUE in the schema; collisions are retried a bounded number of times.
- Only the claim-code hash is stored, never the plaintext code.
- Timestamps are ISO 8601 strings in UTC with millisecond precision and a "Z" suffix,
  the same shape as JavaScript's Date.toISOString().
- Upload tokens are stored as SHA-256 hashes with an expiry; evidence rows hold metadata and a
  storage key only, never file contents.

Schema changes are numbered migrations (SQLite: PRAGMA user_version; PostgreSQL: the
civiclens_schema table), so an existing database is upgraded in place. PostgreSQL starts directly
at the current version (POSTGRES_SCHEMA); later changes must be added to both MIGRATIONS and
POSTGRES_MIGRATIONS.
"""

import logging
import os
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timedelta, timezone

from .db import DB_ERRORS, DB_PATH_ENV, DEFAULT_DB_PATH, INTEGRITY_ERRORS, SQLiteDatabase, database_from_env, resolve_db_path  # noqa: F401
from .claims import hash_claim_code, hash_upload_token, new_claim_code, new_public_id, new_upload_token, upload_token_matches

logger = logging.getLogger("civiclens.store")

MAX_ID_ATTEMPTS = 5
SCHEMA_VERSION = 5
DEFAULT_UPLOAD_TOKEN_TTL = timedelta(hours=1)

INITIAL_STATUS = "Submitted"
INITIAL_VERIFICATION = "Pending Verification"
INITIAL_DEPARTMENT = "Awaiting civic review"
DEFAULT_AREA = "Bengaluru · location on file"

SCHEMA = """
CREATE TABLE IF NOT EXISTS complaints (
    pk                  INTEGER PRIMARY KEY,
    public_id           TEXT NOT NULL UNIQUE,
    claim_hash          TEXT NOT NULL,
    title               TEXT NOT NULL,
    category            TEXT NOT NULL,
    area                TEXT NOT NULL,
    description         TEXT NOT NULL DEFAULT '',
    media_type          TEXT NOT NULL CHECK (media_type IN ('photo', 'video')),
    share_evidence      INTEGER NOT NULL CHECK (share_evidence IN (0, 1)),
    gps_lat             REAL NOT NULL CHECK (gps_lat BETWEEN -90 AND 90),
    gps_lng             REAL NOT NULL CHECK (gps_lng BETWEEN -180 AND 180),
    gps_accuracy        REAL CHECK (gps_accuracy IS NULL OR gps_accuracy >= 0),
    gps_source          TEXT,
    gps_source_type     TEXT NOT NULL CHECK (gps_source_type IN ('gps', 'exif', 'manual')),
    gps_timestamp       TEXT,
    gps_fix_age_seconds INTEGER,
    gps_address         TEXT,
    captured_at         TEXT,
    capture_source      TEXT,
    created_at          TEXT NOT NULL,
    uploaded_at         TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    status              TEXT NOT NULL,
    verification        TEXT NOT NULL,
    department          TEXT NOT NULL
);

-- Public timeline (append-only).
CREATE TABLE IF NOT EXISTS complaint_timeline (
    pk           INTEGER PRIMARY KEY,
    complaint_pk INTEGER NOT NULL REFERENCES complaints(pk) ON DELETE CASCADE,
    status       TEXT NOT NULL,
    text         TEXT NOT NULL,
    date         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS complaint_timeline_by_complaint ON complaint_timeline (complaint_pk, pk);

-- Public verification history (append-only). Nothing writes to it yet; it is read
-- by the public projection so the response shape matches dist/services.js.
CREATE TABLE IF NOT EXISTS verification_log (
    pk           INTEGER PRIMARY KEY,
    complaint_pk INTEGER NOT NULL REFERENCES complaints(pk) ON DELETE CASCADE,
    entry_id     TEXT NOT NULL,
    date         TEXT NOT NULL,
    type         TEXT NOT NULL,
    from_state   TEXT,
    to_state     TEXT,
    response     TEXT,
    by_role      TEXT NOT NULL,
    actor        TEXT NOT NULL,
    note         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS verification_log_by_complaint ON verification_log (complaint_pk, pk);
"""

# Version 1 is SCHEMA above. Each later version is applied once, in order, in its own transaction.
MIGRATIONS = {
    2: """
ALTER TABLE complaints ADD COLUMN upload_token_hash TEXT;
ALTER TABLE complaints ADD COLUMN upload_token_expires_at TEXT;

-- Evidence metadata. The file itself lives in EvidenceStorage under storage_key.
-- One original file per complaint (role 'original'); later roles (e.g. repair photos) can be added.
CREATE TABLE evidence (
    pk            INTEGER PRIMARY KEY,
    evidence_id   TEXT NOT NULL UNIQUE,
    complaint_pk  INTEGER NOT NULL REFERENCES complaints(pk) ON DELETE CASCADE,
    role          TEXT NOT NULL DEFAULT 'original' CHECK (role IN ('original')),
    storage_key   TEXT NOT NULL UNIQUE,
    media_type    TEXT NOT NULL CHECK (media_type IN ('photo', 'video')),
    content_type  TEXT NOT NULL CHECK (content_type IN ('image/jpeg', 'video/webm', 'video/mp4')),
    byte_size     INTEGER NOT NULL CHECK (byte_size > 0),
    sha256        TEXT NOT NULL CHECK (length(sha256) = 64),
    captured_at   TEXT,
    uploaded_at   TEXT NOT NULL,
    UNIQUE (complaint_pk, role)
);
""",
    3: """
-- Set the first time a complaint's credentials are demonstrably used (evidence uploaded or upload
-- token renewed with the claim code). After that, an idempotent replay never re-issues credentials.
ALTER TABLE complaints ADD COLUMN credentials_used_at TEXT;

-- Idempotency keys for POST /complaints. Only a SHA-256 hash of the client's key is stored, with a
-- fingerprint (SHA-256 of the canonical request) so a reused key with a different request is refused.
CREATE TABLE idempotency_keys (
    pk               INTEGER PRIMARY KEY,
    scope            TEXT NOT NULL,
    key_hash         TEXT NOT NULL CHECK (length(key_hash) = 64),
    fingerprint      TEXT NOT NULL CHECK (length(fingerprint) = 64),
    complaint_pk     INTEGER NOT NULL REFERENCES complaints(pk) ON DELETE CASCADE,
    created_at       TEXT NOT NULL,
    replay_count     INTEGER NOT NULL DEFAULT 0,
    last_replayed_at TEXT,
    UNIQUE (scope, key_hash)
);
""",
    4: """
-- Staff accounts for the server console. Created only with `python -m app.staff create`;
-- there is no default account or password. Passwords are stored as scrypt hashes.
CREATE TABLE staff_accounts (
    pk            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'official' CHECK (role IN ('official')),
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    disabled_at   TEXT
);

-- Bearer sessions: only a SHA-256 hash of the token is stored, with an expiry.
CREATE TABLE staff_sessions (
    pk          INTEGER PRIMARY KEY,
    token_hash  TEXT NOT NULL UNIQUE CHECK (length(token_hash) = 64),
    staff_pk    INTEGER NOT NULL REFERENCES staff_accounts(pk) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    revoked_at  TEXT
);

-- Append-only record of every evidence view by staff.
CREATE TABLE evidence_access_log (
    pk           INTEGER PRIMARY KEY,
    evidence_pk  INTEGER NOT NULL REFERENCES evidence(pk) ON DELETE CASCADE,
    staff_pk     INTEGER NOT NULL REFERENCES staff_accounts(pk),
    action       TEXT NOT NULL CHECK (action IN ('view')),
    client_ip    TEXT,
    accessed_at  TEXT NOT NULL
);
CREATE INDEX evidence_access_by_evidence ON evidence_access_log (evidence_pk, pk);
""",
    5: """
-- Which storage backend holds each evidence file. Every existing row is local-disk evidence, so the
-- default keeps it readable after Supabase Storage is enabled; new uploads record the backend that stored them.
ALTER TABLE evidence ADD COLUMN storage_backend TEXT NOT NULL DEFAULT 'local' CHECK (storage_backend IN ('local', 'supabase'));
""",
}

# PostgreSQL schema at SCHEMA_VERSION (version 5): the same tables, columns, constraints and indexes
# as SQLite's SCHEMA plus MIGRATIONS 2–5, in PostgreSQL types:
#   INTEGER PRIMARY KEY → BIGINT identity (BY DEFAULT, so the SQLite import can keep its keys)
#   REAL                → DOUBLE PRECISION (PostgreSQL REAL is 4-byte and would lose GPS precision)
#   foreign keys        → BIGINT, matching the identity keys
# Timestamps stay ISO 8601 UTC text ("…Z"), exactly as in SQLite, so comparisons and API output are
# identical on both engines. The storage_backend list must stay in step with MIGRATIONS[5].
POSTGRES_SCHEMA = """
CREATE TABLE complaints (
    pk                      BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    public_id               TEXT NOT NULL UNIQUE,
    claim_hash              TEXT NOT NULL,
    title                   TEXT NOT NULL,
    category                TEXT NOT NULL,
    area                    TEXT NOT NULL,
    description             TEXT NOT NULL DEFAULT '',
    media_type              TEXT NOT NULL CHECK (media_type IN ('photo', 'video')),
    share_evidence          INTEGER NOT NULL CHECK (share_evidence IN (0, 1)),
    gps_lat                 DOUBLE PRECISION NOT NULL CHECK (gps_lat BETWEEN -90 AND 90),
    gps_lng                 DOUBLE PRECISION NOT NULL CHECK (gps_lng BETWEEN -180 AND 180),
    gps_accuracy            DOUBLE PRECISION CHECK (gps_accuracy IS NULL OR gps_accuracy >= 0),
    gps_source              TEXT,
    gps_source_type         TEXT NOT NULL CHECK (gps_source_type IN ('gps', 'exif', 'manual')),
    gps_timestamp           TEXT,
    gps_fix_age_seconds     INTEGER,
    gps_address             TEXT,
    captured_at             TEXT,
    capture_source          TEXT,
    created_at              TEXT NOT NULL,
    uploaded_at             TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    status                  TEXT NOT NULL,
    verification            TEXT NOT NULL,
    department              TEXT NOT NULL,
    upload_token_hash       TEXT,
    upload_token_expires_at TEXT,
    credentials_used_at     TEXT
);

CREATE TABLE complaint_timeline (
    pk           BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    complaint_pk BIGINT NOT NULL REFERENCES complaints(pk) ON DELETE CASCADE,
    status       TEXT NOT NULL,
    text         TEXT NOT NULL,
    date         TEXT NOT NULL
);
CREATE INDEX complaint_timeline_by_complaint ON complaint_timeline (complaint_pk, pk);

CREATE TABLE verification_log (
    pk           BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    complaint_pk BIGINT NOT NULL REFERENCES complaints(pk) ON DELETE CASCADE,
    entry_id     TEXT NOT NULL,
    date         TEXT NOT NULL,
    type         TEXT NOT NULL,
    from_state   TEXT,
    to_state     TEXT,
    response     TEXT,
    by_role      TEXT NOT NULL,
    actor        TEXT NOT NULL,
    note         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX verification_log_by_complaint ON verification_log (complaint_pk, pk);

CREATE TABLE evidence (
    pk              BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    evidence_id     TEXT NOT NULL UNIQUE,
    complaint_pk    BIGINT NOT NULL REFERENCES complaints(pk) ON DELETE CASCADE,
    role            TEXT NOT NULL DEFAULT 'original' CHECK (role IN ('original')),
    storage_key     TEXT NOT NULL UNIQUE,
    media_type      TEXT NOT NULL CHECK (media_type IN ('photo', 'video')),
    content_type    TEXT NOT NULL CHECK (content_type IN ('image/jpeg', 'video/webm', 'video/mp4')),
    byte_size       BIGINT NOT NULL CHECK (byte_size > 0),
    sha256          TEXT NOT NULL CHECK (length(sha256) = 64),
    captured_at     TEXT,
    uploaded_at     TEXT NOT NULL,
    storage_backend TEXT NOT NULL DEFAULT 'local' CHECK (storage_backend IN ('local', 'supabase')),
    UNIQUE (complaint_pk, role)
);

CREATE TABLE idempotency_keys (
    pk               BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    scope            TEXT NOT NULL,
    key_hash         TEXT NOT NULL CHECK (length(key_hash) = 64),
    fingerprint      TEXT NOT NULL CHECK (length(fingerprint) = 64),
    complaint_pk     BIGINT NOT NULL REFERENCES complaints(pk) ON DELETE CASCADE,
    created_at       TEXT NOT NULL,
    replay_count     INTEGER NOT NULL DEFAULT 0,
    last_replayed_at TEXT,
    UNIQUE (scope, key_hash)
);

CREATE TABLE staff_accounts (
    pk            BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'official' CHECK (role IN ('official')),
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    disabled_at   TEXT
);

CREATE TABLE staff_sessions (
    pk          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    token_hash  TEXT NOT NULL UNIQUE CHECK (length(token_hash) = 64),
    staff_pk    BIGINT NOT NULL REFERENCES staff_accounts(pk) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    revoked_at  TEXT
);

CREATE TABLE evidence_access_log (
    pk           BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    evidence_pk  BIGINT NOT NULL REFERENCES evidence(pk) ON DELETE CASCADE,
    staff_pk     BIGINT NOT NULL REFERENCES staff_accounts(pk),
    action       TEXT NOT NULL CHECK (action IN ('view')),
    client_ip    TEXT,
    accessed_at  TEXT NOT NULL
);
CREATE INDEX evidence_access_by_evidence ON evidence_access_log (evidence_pk, pk);
"""

# Later PostgreSQL migrations, keyed by the same version numbers as MIGRATIONS.
POSTGRES_MIGRATIONS: dict[int, str] = {}

# Tables in foreign-key order (parents first): used by the SQLite → PostgreSQL migration tool.
TABLES = ("complaints", "complaint_timeline", "verification_log", "evidence", "idempotency_keys",
          "staff_accounts", "staff_sessions", "evidence_access_log")

CREATE_SCOPE = "create_complaint"
DEFAULT_REPLAY_WINDOW = timedelta(hours=24)


class IdempotencyConflict(Exception):
    """The idempotency key was already used with a different request."""


class IdempotencyCompleted(Exception):
    """The key's request already completed and its credentials can no longer be re-issued."""

    def __init__(self, public_id: str):
        super().__init__("Idempotent request already completed")
        self.public_id = public_id


class _IdempotencyExists(Exception):
    pass


class StaffExists(Exception):
    """A staff account with that username already exists."""


STAFF_LIST_MAX = 100


def _evidence_dict(row) -> dict:
    return {
        "evidenceId": row["evidence_id"], "complaintPk": row["complaint_pk"], "role": row["role"],
        "storageKey": row["storage_key"], "mediaType": row["media_type"], "contentType": row["content_type"],
        "byteSize": row["byte_size"], "sha256": row["sha256"], "capturedAt": row["captured_at"],
        "uploadedAt": row["uploaded_at"], "storageBackend": row["storage_backend"],
    }


class StoreError(Exception):
    """The database could not complete the operation. The message is for logs only."""


class IdAllocationError(StoreError):
    """No unused public ID was found within MAX_ID_ATTEMPTS."""


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utc_now() -> str:
    return iso(datetime.now(timezone.utc))


class ComplaintStore:
    def __init__(self, db_path: str | os.PathLike | None = None, id_factory: Callable[[], str] = new_public_id,
                 upload_token_ttl: timedelta = DEFAULT_UPLOAD_TOKEN_TTL, database=None):
        """`database` is a db.SQLiteDatabase or db.PostgresDatabase. Without it, the backend comes from
        DATABASE_BACKEND (default sqlite); `db_path` is the SQLite file and is ignored for PostgreSQL."""
        self.db = database if database is not None else database_from_env(db_path)
        self.db_path = self.db.path if isinstance(self.db, SQLiteDatabase) else None
        self._new_id = id_factory
        self.upload_token_ttl = upload_token_ttl

    @property
    def backend(self) -> str:
        return self.db.backend

    def _connect(self):
        return self.db.connect()

    def initialize(self) -> None:
        """Create the database (and schema) if missing and apply pending migrations. Safe to repeat."""
        try:
            if self.db.backend == "postgres":
                self.db.apply_schema(POSTGRES_SCHEMA, POSTGRES_MIGRATIONS, SCHEMA_VERSION)
            else:
                self.db.apply_schema(SCHEMA, MIGRATIONS)
        except (OSError, *DB_ERRORS) as exc:
            raise StoreError("Could not initialise the complaint database") from exc

    def close(self) -> None:
        """Release pooled connections (PostgreSQL). Safe to call more than once."""
        self.db.close()

    def _token_expiry(self) -> str:
        return iso(datetime.now(timezone.utc) + self.upload_token_ttl)

    def create(self, data: dict, idempotency: dict | None = None) -> tuple[dict, str]:
        """Insert a complaint. Returns (stored record, plaintext claim code).

        `data` holds validated fields from ComplaintCreate. The claim code is
        generated here, hashed, and returned to the caller exactly once. A first
        upload token is issued in the same transaction; the returned record carries
        it as `uploadToken` / `uploadTokenExpiresAt` (only the hash is stored).
        With `idempotency` ({"keyHash", "fingerprint"}) the key row is inserted in the
        same transaction, so a failed create leaves no key behind.
        """
        claim = new_claim_code()
        claim_hash = hash_claim_code(claim)
        upload_token = new_upload_token()
        token_expires = self._token_expiry()
        now = utc_now()
        gps = data["gps"]
        row = {
            "claim_hash": claim_hash,
            "upload_token_hash": hash_upload_token(upload_token),
            "upload_token_expires_at": token_expires,
            "title": f"{data['category']} reported by a citizen",
            "category": data["category"],
            "area": data.get("neighbourhood") or DEFAULT_AREA,
            "description": data.get("description") or "",
            "media_type": data["mediaType"],
            "share_evidence": 1 if data.get("shareEvidence") else 0,
            "gps_lat": gps["lat"],
            "gps_lng": gps["lng"],
            "gps_accuracy": gps.get("accuracy"),
            "gps_source": gps.get("source"),
            "gps_source_type": gps["sourceType"],
            "gps_timestamp": gps.get("timestamp"),
            "gps_fix_age_seconds": gps.get("fixAgeSeconds"),
            "gps_address": gps.get("address"),
            "captured_at": data.get("capturedAt"),
            "capture_source": data.get("captureSource"),
            "created_at": data.get("capturedAt") or now,
            "uploaded_at": now,
            "updated_at": now,
            "status": INITIAL_STATUS,
            "verification": INITIAL_VERIFICATION,
            "department": INITIAL_DEPARTMENT,
        }
        columns = ", ".join(["public_id", *row])
        placeholders = ", ".join(["?"] * (len(row) + 1))
        insert_sql = f"INSERT INTO complaints ({columns}) VALUES ({placeholders})"

        try:
            with closing(self._connect()) as conn:
                for _ in range(MAX_ID_ATTEMPTS):
                    public_id = self._new_id()
                    try:
                        with conn.transaction():  # one transaction: commit on success, roll back on any error
                            complaint_pk = conn.insert(insert_sql, (public_id, *row.values()))
                            conn.execute(
                                "INSERT INTO complaint_timeline (complaint_pk, status, text, date) VALUES (?, ?, ?, ?)",
                                (complaint_pk, INITIAL_STATUS, "Report received by the Namma CivicLens API.", now),
                            )
                            if idempotency:
                                conn.execute(
                                    "INSERT INTO idempotency_keys (scope, key_hash, fingerprint, complaint_pk, created_at) VALUES (?, ?, ?, ?, ?)",
                                    (CREATE_SCOPE, idempotency["keyHash"], idempotency["fingerprint"], complaint_pk, now),
                                )
                    except INTEGRITY_ERRORS as exc:
                        if idempotency and self._find_key(conn, idempotency["keyHash"]) is not None:
                            raise _IdempotencyExists() from exc
                        if self._is_public_id_collision(conn, public_id):
                            logger.warning("Public ID collision; retrying with a new ID")
                            continue
                        raise StoreError("Complaint insert violated a constraint") from exc
                    record = self._load(conn, public_id)
                    return {**record, "uploadToken": upload_token, "uploadTokenExpiresAt": token_expires}, claim
        except (StoreError, _IdempotencyExists):
            raise
        except DB_ERRORS as exc:
            raise StoreError("Complaint could not be saved") from exc
        raise IdAllocationError(f"No unused public ID after {MAX_ID_ATTEMPTS} attempts")

    @staticmethod
    def _is_public_id_collision(conn, public_id: str) -> bool:
        return conn.execute("SELECT 1 FROM complaints WHERE public_id = ?", (public_id,)).fetchone() is not None

    # ---------- Idempotent creation ----------

    @staticmethod
    def _find_key(conn, key_hash: str, lock: str = ""):
        return conn.execute(
            "SELECT k.fingerprint, k.created_at, k.complaint_pk, c.public_id, c.credentials_used_at "
            "FROM idempotency_keys k JOIN complaints c ON c.pk = k.complaint_pk WHERE k.scope = ? AND k.key_hash = ?" + lock,
            (CREATE_SCOPE, key_hash),
        ).fetchone()

    def create_idempotent(self, data: dict, key_hash: str, fingerprint: str,
                          replay_window: timedelta = DEFAULT_REPLAY_WINDOW) -> tuple[str, dict, str]:
        """Create a complaint once per idempotency key. Returns ("created" | "replayed", record, claim).

        A replay (same key, same fingerprint) returns the same complaint with a NEWLY ISSUED claim code
        and upload token; the previous ones stop working. Re-issue is allowed only while the original
        credentials have never been used and within `replay_window`; otherwise IdempotencyCompleted.
        A different request with the same key raises IdempotencyConflict. Concurrent requests with one
        key create one complaint: the loser of the race takes the replay path.
        """
        try:
            with closing(self._connect()) as conn:
                existing = self._find_key(conn, key_hash)
            if existing is None:
                try:
                    record, claim = self.create(data, {"keyHash": key_hash, "fingerprint": fingerprint})
                    return "created", record, claim
                except _IdempotencyExists:
                    pass
            record, claim = self._replay(key_hash, fingerprint, replay_window)
            return "replayed", record, claim
        except DB_ERRORS as exc:
            raise StoreError("Idempotent complaint creation failed") from exc

    def _replay(self, key_hash: str, fingerprint: str, replay_window: timedelta) -> tuple[dict, str]:
        claim, token, expires, now = new_claim_code(), new_upload_token(), self._token_expiry(), utc_now()
        claim_hash = hash_claim_code(claim)  # slow; computed before taking the write lock
        with closing(self._connect()) as conn:
            # Serialises concurrent replays of the same key: SQLite takes the write lock up front
            # (BEGIN IMMEDIATE); PostgreSQL locks the key and complaint rows (FOR UPDATE).
            with conn.transaction(immediate=True):
                row = self._find_key(conn, key_hash, self.db.for_update)
                if row is None:
                    raise StoreError("Idempotency key disappeared during replay")
                if row["fingerprint"] != fingerprint:
                    raise IdempotencyConflict()
                cutoff = iso(datetime.now(timezone.utc) - replay_window)
                if row["credentials_used_at"] is not None or row["created_at"] < cutoff:
                    raise IdempotencyCompleted(row["public_id"])
                conn.execute(
                    "UPDATE complaints SET claim_hash = ?, upload_token_hash = ?, upload_token_expires_at = ? WHERE pk = ?",
                    (claim_hash, hash_upload_token(token), expires, row["complaint_pk"]),
                )
                conn.execute(
                    "UPDATE idempotency_keys SET replay_count = replay_count + 1, last_replayed_at = ? WHERE scope = ? AND key_hash = ?",
                    (now, CREATE_SCOPE, key_hash),
                )
            record = self._load(conn, row["public_id"])
        return {**record, "uploadToken": token, "uploadTokenExpiresAt": expires}, claim

    def total_evidence_bytes(self) -> int:
        try:
            with closing(self._connect()) as conn:
                return int(conn.execute("SELECT CAST(COALESCE(SUM(byte_size), 0) AS BIGINT) FROM evidence").fetchone()[0])
        except DB_ERRORS as exc:
            raise StoreError("Evidence usage could not be read") from exc

    # ---------- Upload tokens ----------

    def issue_upload_token(self, public_id: str) -> tuple[str, str] | None:
        """Replace the complaint's upload token. Returns (token, expiresAt), or None if no such
        complaint. Call only after the caller proved ownership (claim code verified)."""
        token, expires = new_upload_token(), self._token_expiry()
        try:
            with closing(self._connect()) as conn, conn.transaction():
                cur = conn.execute(
                    "UPDATE complaints SET upload_token_hash = ?, upload_token_expires_at = ?, "
                    "credentials_used_at = COALESCE(credentials_used_at, ?) WHERE public_id = ?",
                    (hash_upload_token(token), expires, utc_now(), public_id),
                )
                return (token, expires) if cur.rowcount else None
        except DB_ERRORS as exc:
            raise StoreError("Upload token could not be issued") from exc

    def check_upload_token(self, public_id: str, token: str) -> dict | None:
        """Returns None if the complaint does not exist, else {"status": "ok"|"invalid"|"expired",
        "pk", "mediaType"}. Expiry is checked only for a matching token."""
        try:
            with closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT pk, media_type, upload_token_hash, upload_token_expires_at FROM complaints WHERE public_id = ?",
                    (public_id,),
                ).fetchone()
        except DB_ERRORS as exc:
            raise StoreError("Upload token could not be checked") from exc
        if row is None:
            return None
        result = {"pk": row["pk"], "mediaType": row["media_type"]}
        if not upload_token_matches(token, row["upload_token_hash"]):
            return {**result, "status": "invalid"}
        if (row["upload_token_expires_at"] or "") <= utc_now():
            return {**result, "status": "expired"}
        return {**result, "status": "ok"}

    # ---------- Evidence metadata ----------

    def get_evidence(self, complaint_pk: int, role: str = "original") -> dict | None:
        try:
            with closing(self._connect()) as conn:
                row = conn.execute("SELECT * FROM evidence WHERE complaint_pk = ? AND role = ?", (complaint_pk, role)).fetchone()
        except DB_ERRORS as exc:
            raise StoreError("Evidence metadata could not be read") from exc
        return _evidence_dict(row) if row else None

    def add_evidence(self, complaint_pk: int, meta: dict) -> tuple[str, dict]:
        """Insert evidence metadata. Returns ("created", row) or, when the complaint already has
        evidence in that role (e.g. a concurrent retry), ("exists", existing row)."""
        try:
            with closing(self._connect()) as conn:
                try:
                    with conn.transaction():
                        conn.execute(
                            "INSERT INTO evidence (evidence_id, complaint_pk, role, storage_key, media_type, content_type, "
                            "byte_size, sha256, captured_at, uploaded_at, storage_backend) VALUES (?, ?, 'original', ?, ?, ?, ?, ?, ?, ?, ?)",
                            (meta["evidenceId"], complaint_pk, meta["storageKey"], meta["mediaType"], meta["contentType"],
                             meta["byteSize"], meta["sha256"], meta.get("capturedAt"), meta["uploadedAt"],
                             meta.get("storageBackend", "local")),
                        )
                        conn.execute("UPDATE complaints SET credentials_used_at = COALESCE(credentials_used_at, ?) WHERE pk = ?",
                                     (meta["uploadedAt"], complaint_pk))
                except INTEGRITY_ERRORS:
                    existing = conn.execute("SELECT * FROM evidence WHERE complaint_pk = ? AND role = 'original'", (complaint_pk,)).fetchone()
                    if existing is None:
                        raise
                    return "exists", _evidence_dict(existing)
                row = conn.execute("SELECT * FROM evidence WHERE evidence_id = ?", (meta["evidenceId"],)).fetchone()
                return "created", _evidence_dict(row)
        except DB_ERRORS as exc:
            raise StoreError("Evidence metadata could not be saved") from exc

    # ---------- Staff accounts and sessions ----------

    def _write(self, sql: str, params: tuple, what: str) -> int:
        try:
            with closing(self._connect()) as conn, conn.transaction():
                return conn.execute(sql, params).rowcount
        except DB_ERRORS as exc:
            raise StoreError(f"{what} failed") from exc

    def create_staff(self, username: str, display_name: str, password_hash: str) -> None:
        try:
            with closing(self._connect()) as conn, conn.transaction():
                conn.execute("INSERT INTO staff_accounts (username, display_name, password_hash, created_at) VALUES (?, ?, ?, ?)",
                             (username, display_name, password_hash, utc_now()))
        except INTEGRITY_ERRORS as exc:
            raise StaffExists(username) from exc
        except DB_ERRORS as exc:
            raise StoreError("Staff account could not be created") from exc

    def get_staff(self, username: str) -> dict | None:
        try:
            with closing(self._connect()) as conn:
                row = conn.execute("SELECT * FROM staff_accounts WHERE username = ?", (username,)).fetchone()
        except DB_ERRORS as exc:
            raise StoreError("Staff account could not be read") from exc
        return dict(row) if row else None

    def list_staff(self) -> list[dict]:
        try:
            with closing(self._connect()) as conn:
                return [dict(r) for r in conn.execute(
                    "SELECT username, display_name, role, created_at, disabled_at FROM staff_accounts ORDER BY username")]
        except DB_ERRORS as exc:
            raise StoreError("Staff accounts could not be read") from exc

    def set_staff_disabled(self, username: str, disabled: bool) -> bool:
        """Disabling also revokes every open session of the account."""
        now = utc_now()
        try:
            with closing(self._connect()) as conn, conn.transaction():
                cur = conn.execute("UPDATE staff_accounts SET disabled_at = ? WHERE username = ?", (now if disabled else None, username))
                if disabled:
                    conn.execute("UPDATE staff_sessions SET revoked_at = ? WHERE revoked_at IS NULL AND staff_pk = "
                                 "(SELECT pk FROM staff_accounts WHERE username = ?)", (now, username))
                return cur.rowcount == 1
        except DB_ERRORS as exc:
            raise StoreError("Staff account could not be updated") from exc

    def set_staff_password(self, username: str, password_hash: str) -> bool:
        """Changing a password revokes every open session of the account."""
        now = utc_now()
        try:
            with closing(self._connect()) as conn, conn.transaction():
                cur = conn.execute("UPDATE staff_accounts SET password_hash = ? WHERE username = ?", (password_hash, username))
                conn.execute("UPDATE staff_sessions SET revoked_at = ? WHERE revoked_at IS NULL AND staff_pk = "
                             "(SELECT pk FROM staff_accounts WHERE username = ?)", (now, username))
                return cur.rowcount == 1
        except DB_ERRORS as exc:
            raise StoreError("Staff password could not be updated") from exc

    def create_staff_session(self, staff_pk: int, token_hash: str, expires_at: str) -> None:
        self._write("INSERT INTO staff_sessions (token_hash, staff_pk, created_at, expires_at) VALUES (?, ?, ?, ?)",
                    (token_hash, staff_pk, utc_now(), expires_at), "Staff session creation")

    def get_staff_session(self, token_hash: str) -> dict | None:
        """The session's staff account, or None if the token is unknown, expired, revoked or the account is disabled."""
        try:
            with closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT a.pk, a.username, a.display_name, a.role, s.expires_at FROM staff_sessions s "
                    "JOIN staff_accounts a ON a.pk = s.staff_pk "
                    "WHERE s.token_hash = ? AND s.revoked_at IS NULL AND s.expires_at > ? AND a.disabled_at IS NULL",
                    (token_hash, utc_now()),
                ).fetchone()
        except DB_ERRORS as exc:
            raise StoreError("Staff session could not be read") from exc
        return dict(row) if row else None

    def revoke_staff_session(self, token_hash: str) -> None:
        self._write("UPDATE staff_sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                    (utc_now(), token_hash), "Staff session revocation")

    # ---------- Staff views of complaints ----------

    def list_complaints(self, limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
        """Newest first. Returns (records, total). Records come from _load plus an evidence summary."""
        limit = max(1, min(int(limit), STAFF_LIST_MAX))
        try:
            with closing(self._connect()) as conn:
                total = conn.execute("SELECT COUNT(*) FROM complaints").fetchone()[0]
                ids = [r[0] for r in conn.execute("SELECT public_id FROM complaints ORDER BY pk DESC LIMIT ? OFFSET ?", (limit, max(0, int(offset))))]
                return [self._staff_record(conn, i) for i in ids], total
        except DB_ERRORS as exc:
            raise StoreError("Complaints could not be listed") from exc

    def get_staff_complaint(self, public_id: str) -> dict | None:
        try:
            with closing(self._connect()) as conn:
                return self._staff_record(conn, public_id)
        except DB_ERRORS as exc:
            raise StoreError("Complaint could not be read") from exc

    def _staff_record(self, conn, public_id: str) -> dict | None:
        record = self._load(conn, public_id)
        if record is None:
            return None
        row = conn.execute(
            "SELECT e.* FROM evidence e JOIN complaints c ON c.pk = e.complaint_pk WHERE c.public_id = ? AND e.role = 'original'",
            (public_id,),
        ).fetchone()
        return {**record, "evidence": _evidence_dict(row) | {"pk": row["pk"]} if row else None}

    def log_evidence_access(self, evidence_pk: int, staff_pk: int, client_ip: str | None, action: str = "view") -> str:
        now = utc_now()
        self._write("INSERT INTO evidence_access_log (evidence_pk, staff_pk, action, client_ip, accessed_at) VALUES (?, ?, ?, ?, ?)",
                    (evidence_pk, staff_pk, action, client_ip, now), "Evidence access logging")
        return now

    def list_evidence_access(self, evidence_pk: int) -> list[dict]:
        try:
            with closing(self._connect()) as conn:
                return [dict(r) for r in conn.execute(
                    "SELECT a.username, a.display_name, l.action, l.accessed_at FROM evidence_access_log l "
                    "JOIN staff_accounts a ON a.pk = l.staff_pk WHERE l.evidence_pk = ? ORDER BY l.pk DESC LIMIT 200",
                    (evidence_pk,))]
        except DB_ERRORS as exc:
            raise StoreError("Evidence access log could not be read") from exc

    def get(self, public_id: str) -> dict | None:
        try:
            with closing(self._connect()) as conn:
                return self._load(conn, public_id)
        except DB_ERRORS as exc:
            raise StoreError("Complaint could not be read") from exc

    def _load(self, conn, public_id: str) -> dict | None:
        row = conn.execute("SELECT * FROM complaints WHERE public_id = ?", (public_id,)).fetchone()
        if row is None:
            return None
        timeline = conn.execute(
            "SELECT status, text, date FROM complaint_timeline WHERE complaint_pk = ? ORDER BY pk", (row["pk"],)
        ).fetchall()
        log = conn.execute(
            "SELECT entry_id, date, type, from_state, to_state, response, by_role, actor, note "
            "FROM verification_log WHERE complaint_pk = ? ORDER BY pk",
            (row["pk"],),
        ).fetchall()
        return {
            "id": row["public_id"],
            "claimHash": row["claim_hash"],
            "title": row["title"],
            "category": row["category"],
            "area": row["area"],
            "description": row["description"],
            "mediaType": row["media_type"],
            "shareEvidence": bool(row["share_evidence"]),
            "gps": {
                "lat": row["gps_lat"],
                "lng": row["gps_lng"],
                "accuracy": row["gps_accuracy"],
                "source": row["gps_source"],
                "sourceType": row["gps_source_type"],
                "timestamp": row["gps_timestamp"],
                "fixAgeSeconds": row["gps_fix_age_seconds"],
                "address": row["gps_address"],
            },
            "capturedAt": row["captured_at"],
            "captureSource": row["capture_source"],
            "created": row["created_at"],
            "uploadedAt": row["uploaded_at"],
            "updated": row["updated_at"],
            "status": row["status"],
            "verification": row["verification"],
            "department": row["department"],
            "timeline": [dict(t) for t in timeline],
            "verificationLog": [
                {
                    "id": e["entry_id"], "date": e["date"], "type": e["type"], "from": e["from_state"],
                    "to": e["to_state"], "response": e["response"], "by": e["by_role"], "actor": e["actor"],
                    "note": e["note"],
                }
                for e in log
            ],
        }
