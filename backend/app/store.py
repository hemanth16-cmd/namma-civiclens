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

import json
import logging
import os
import secrets
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timedelta, timezone

from .db import DB_ERRORS, DB_PATH_ENV, DEFAULT_DB_PATH, INTEGRITY_ERRORS, Feature, SQLiteDatabase, database_from_env, resolve_db_path  # noqa: F401
from .claims import hash_claim_code, hash_upload_token, new_claim_code, new_public_id, new_upload_token, upload_token_matches
from . import duplicates as dup
from . import geo
from . import lifecycle as lc
from .lifecycle import RuleViolation

logger = logging.getLogger("civiclens.store")

MAX_ID_ATTEMPTS = 5
SCHEMA_VERSION = 7
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
    6: """
-- Web Push notifications. A recipient is a staff account or a citizen inbox (a device-held bearer
-- token; only its SHA-256 hash is stored). Citizens receive notifications for complaints they follow,
-- proven with the claim code. Each recipient may register several devices (push subscriptions).
CREATE TABLE notification_recipients (
    pk          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('citizen', 'staff')),
    staff_pk    INTEGER UNIQUE REFERENCES staff_accounts(pk) ON DELETE CASCADE,
    token_hash  TEXT UNIQUE CHECK (token_hash IS NULL OR length(token_hash) = 64),
    created_at  TEXT NOT NULL,
    CHECK ((kind = 'staff' AND staff_pk IS NOT NULL AND token_hash IS NULL)
        OR (kind = 'citizen' AND staff_pk IS NULL AND token_hash IS NOT NULL))
);

CREATE TABLE notification_follows (
    pk            INTEGER PRIMARY KEY,
    recipient_pk  INTEGER NOT NULL REFERENCES notification_recipients(pk) ON DELETE CASCADE,
    complaint_pk  INTEGER NOT NULL REFERENCES complaints(pk) ON DELETE CASCADE,
    created_at    TEXT NOT NULL,
    UNIQUE (recipient_pk, complaint_pk)
);
CREATE INDEX notification_follows_by_complaint ON notification_follows (complaint_pk);

-- One row per device and recipient. The endpoint is a capability URL at the browser's push service;
-- it and the keys are never returned by the API.
CREATE TABLE push_subscriptions (
    pk               INTEGER PRIMARY KEY,
    subscription_id  TEXT NOT NULL UNIQUE,
    recipient_pk     INTEGER NOT NULL REFERENCES notification_recipients(pk) ON DELETE CASCADE,
    endpoint         TEXT NOT NULL,
    p256dh           TEXT NOT NULL,
    auth             TEXT NOT NULL,
    label            TEXT NOT NULL DEFAULT '',
    expires_at       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    last_success_at  TEXT,
    last_failure_at  TEXT,
    failure_count    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (recipient_pk, endpoint)
);

-- Persistent history. dedupe_key makes each event notify a recipient at most once.
CREATE TABLE notifications (
    pk               INTEGER PRIMARY KEY,
    notification_id  TEXT NOT NULL UNIQUE,
    recipient_pk     INTEGER NOT NULL REFERENCES notification_recipients(pk) ON DELETE CASCADE,
    complaint_pk     INTEGER REFERENCES complaints(pk) ON DELETE CASCADE,
    kind             TEXT NOT NULL CHECK (kind IN ('follow_confirmed', 'evidence_received', 'evidence_reviewed', 'new_complaint', 'new_evidence', 'test')),
    title            TEXT NOT NULL,
    body             TEXT NOT NULL,
    url              TEXT NOT NULL,
    dedupe_key       TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    read_at          TEXT,
    push_status      TEXT NOT NULL DEFAULT 'pending' CHECK (push_status IN ('pending', 'sent', 'partial', 'failed', 'no_devices', 'disabled')),
    UNIQUE (recipient_pk, dedupe_key)
);
CREATE INDEX notifications_by_recipient ON notifications (recipient_pk, pk);
""",
    7: """
-- Complaint management (Workstream 1). See docs/CORE_PLATFORM_PLAN.md §5.
-- Staff roles: official (acts within their departments), supervisor, admin. SQLite cannot change a
-- CHECK constraint, so the table is rebuilt with the same rows (apply_schema turns foreign keys off).
CREATE TABLE staff_accounts_v7 (
    pk            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'official' CHECK (role IN ('official', 'supervisor', 'admin')),
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    disabled_at   TEXT
);
INSERT INTO staff_accounts_v7 (pk, username, display_name, role, password_hash, created_at, disabled_at)
    SELECT pk, username, display_name, role, password_hash, created_at, disabled_at FROM staff_accounts;
DROP TABLE staff_accounts;
ALTER TABLE staff_accounts_v7 RENAME TO staff_accounts;

-- Evidence gains the 'repair' role (after-repair photos linked to work orders). Still exactly one
-- original per complaint, now enforced by a partial unique index instead of UNIQUE (complaint_pk, role).
CREATE TABLE evidence_v7 (
    pk                   INTEGER PRIMARY KEY,
    evidence_id          TEXT NOT NULL UNIQUE,
    complaint_pk         INTEGER NOT NULL REFERENCES complaints(pk) ON DELETE CASCADE,
    role                 TEXT NOT NULL DEFAULT 'original' CHECK (role IN ('original', 'repair')),
    storage_key          TEXT NOT NULL UNIQUE,
    media_type           TEXT NOT NULL CHECK (media_type IN ('photo', 'video')),
    content_type         TEXT NOT NULL CHECK (content_type IN ('image/jpeg', 'video/webm', 'video/mp4')),
    byte_size            INTEGER NOT NULL CHECK (byte_size > 0),
    sha256               TEXT NOT NULL CHECK (length(sha256) = 64),
    captured_at          TEXT,
    uploaded_at          TEXT NOT NULL,
    storage_backend      TEXT NOT NULL DEFAULT 'local' CHECK (storage_backend IN ('local', 'supabase')),
    uploaded_by_staff_pk INTEGER REFERENCES staff_accounts(pk)
);
INSERT INTO evidence_v7 (pk, evidence_id, complaint_pk, role, storage_key, media_type, content_type, byte_size, sha256,
                         captured_at, uploaded_at, storage_backend)
    SELECT pk, evidence_id, complaint_pk, role, storage_key, media_type, content_type, byte_size, sha256,
           captured_at, uploaded_at, storage_backend FROM evidence;
DROP TABLE evidence;
ALTER TABLE evidence_v7 RENAME TO evidence;
CREATE UNIQUE INDEX evidence_one_original ON evidence (complaint_pk) WHERE role = 'original';
CREATE INDEX evidence_by_complaint ON evidence (complaint_pk, role);

CREATE TABLE departments (
    pk          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at  TEXT NOT NULL
);

CREATE TABLE staff_departments (
    pk             INTEGER PRIMARY KEY,
    staff_pk       INTEGER NOT NULL REFERENCES staff_accounts(pk) ON DELETE CASCADE,
    department_pk  INTEGER NOT NULL REFERENCES departments(pk),
    created_at     TEXT NOT NULL,
    UNIQUE (staff_pk, department_pk)
);

-- Current assignment and an optimistic-concurrency counter. `department` (text) stays as the
-- public display name; department_pk is the operational link.
ALTER TABLE complaints ADD COLUMN department_pk INTEGER REFERENCES departments(pk);
ALTER TABLE complaints ADD COLUMN assigned_staff_pk INTEGER REFERENCES staff_accounts(pk);
ALTER TABLE complaints ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
CREATE INDEX complaints_by_status ON complaints (status, pk);
CREATE INDEX complaints_by_department ON complaints (department_pk, status);

-- Public timeline entries now say what kind of update they are and (internally) who wrote them.
ALTER TABLE complaint_timeline ADD COLUMN kind TEXT NOT NULL DEFAULT 'progress'
    CHECK (kind IN ('progress', 'status_change', 'department_response', 'assignment', 'work_order', 'verification'));
ALTER TABLE complaint_timeline ADD COLUMN actor_staff_pk INTEGER REFERENCES staff_accounts(pk);

-- Assignment history: one row per assignment or reassignment, never updated.
CREATE TABLE complaint_assignments (
    pk                 INTEGER PRIMARY KEY,
    assignment_id      TEXT NOT NULL UNIQUE,
    complaint_pk       INTEGER NOT NULL REFERENCES complaints(pk),
    department_pk      INTEGER NOT NULL REFERENCES departments(pk),
    assignee_staff_pk  INTEGER REFERENCES staff_accounts(pk),
    reason             TEXT NOT NULL DEFAULT '',
    actor_staff_pk     INTEGER NOT NULL REFERENCES staff_accounts(pk),
    assigned_at        TEXT NOT NULL
);
CREATE INDEX complaint_assignments_by_complaint ON complaint_assignments (complaint_pk, pk);

-- Internal staff notes. Kept apart from complaint_timeline so no public query can ever read them.
CREATE TABLE staff_notes (
    pk               INTEGER PRIMARY KEY,
    note_id          TEXT NOT NULL UNIQUE,
    complaint_pk     INTEGER NOT NULL REFERENCES complaints(pk),
    author_staff_pk  INTEGER NOT NULL REFERENCES staff_accounts(pk),
    body             TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 2000),
    created_at       TEXT NOT NULL
);
CREATE INDEX staff_notes_by_complaint ON staff_notes (complaint_pk, pk);

CREATE TABLE work_orders (
    pk                   INTEGER PRIMARY KEY,
    work_order_id        TEXT NOT NULL UNIQUE,
    complaint_pk         INTEGER NOT NULL REFERENCES complaints(pk),
    department_pk        INTEGER REFERENCES departments(pk),
    team                 TEXT NOT NULL,
    assignee_staff_pk    INTEGER REFERENCES staff_accounts(pk),
    description          TEXT NOT NULL DEFAULT '',
    due_at               TEXT,
    status               TEXT NOT NULL DEFAULT 'Open' CHECK (status IN ('Open', 'In Progress', 'Completed', 'Cancelled')),
    started_at           TEXT,
    completed_at         TEXT,
    completion_note      TEXT NOT NULL DEFAULT '',
    created_by_staff_pk  INTEGER NOT NULL REFERENCES staff_accounts(pk),
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    version              INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX work_orders_by_complaint ON work_orders (complaint_pk, pk);
CREATE INDEX work_orders_by_status ON work_orders (status, due_at);

-- Work-order history: one row per change, never updated.
CREATE TABLE work_order_events (
    pk              INTEGER PRIMARY KEY,
    work_order_pk   INTEGER NOT NULL REFERENCES work_orders(pk),
    action          TEXT NOT NULL,
    from_status     TEXT,
    to_status       TEXT,
    note            TEXT NOT NULL DEFAULT '',
    actor_staff_pk  INTEGER NOT NULL REFERENCES staff_accounts(pk),
    created_at      TEXT NOT NULL
);
CREATE INDEX work_order_events_by_order ON work_order_events (work_order_pk, pk);

-- Before/after evidence references for a work order ('before' usually points at the complaint's original).
CREATE TABLE work_order_evidence (
    pk                  INTEGER PRIMARY KEY,
    work_order_pk       INTEGER NOT NULL REFERENCES work_orders(pk),
    evidence_pk         INTEGER NOT NULL REFERENCES evidence(pk),
    phase               TEXT NOT NULL CHECK (phase IN ('before', 'after')),
    linked_by_staff_pk  INTEGER NOT NULL REFERENCES staff_accounts(pk),
    linked_at           TEXT NOT NULL,
    UNIQUE (work_order_pk, evidence_pk)
);

-- Audit trail for every management action. details is a JSON object.
CREATE TABLE audit_log (
    pk              INTEGER PRIMARY KEY,
    audit_id        TEXT NOT NULL UNIQUE,
    complaint_pk    INTEGER REFERENCES complaints(pk),
    actor_type      TEXT NOT NULL CHECK (actor_type IN ('staff', 'citizen', 'system')),
    actor_staff_pk  INTEGER REFERENCES staff_accounts(pk),
    action          TEXT NOT NULL,
    details         TEXT NOT NULL DEFAULT '{}',
    client_ip       TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX audit_log_by_complaint ON audit_log (complaint_pk, pk);

-- History tables are append-only: the application never updates or deletes them, and the database refuses to.
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER complaint_assignments_no_update BEFORE UPDATE ON complaint_assignments BEGIN SELECT RAISE(ABORT, 'complaint_assignments is append-only'); END;
CREATE TRIGGER complaint_assignments_no_delete BEFORE DELETE ON complaint_assignments BEGIN SELECT RAISE(ABORT, 'complaint_assignments is append-only'); END;
CREATE TRIGGER staff_notes_no_update BEFORE UPDATE ON staff_notes BEGIN SELECT RAISE(ABORT, 'staff_notes is append-only'); END;
CREATE TRIGGER staff_notes_no_delete BEFORE DELETE ON staff_notes BEGIN SELECT RAISE(ABORT, 'staff_notes is append-only'); END;
CREATE TRIGGER work_order_events_no_update BEFORE UPDATE ON work_order_events BEGIN SELECT RAISE(ABORT, 'work_order_events is append-only'); END;
CREATE TRIGGER work_order_events_no_delete BEFORE DELETE ON work_order_events BEGIN SELECT RAISE(ABORT, 'work_order_events is append-only'); END;
""",
}

# PostgreSQL schema at version 5 (later versions: POSTGRES_MIGRATIONS): the same tables, columns, constraints and indexes
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

# Later PostgreSQL migrations, keyed by the same version numbers as MIGRATIONS (same tables and
# constraints in PostgreSQL types). POSTGRES_SCHEMA is version 5; these bring it to SCHEMA_VERSION.
POSTGRES_BASE_VERSION = 5
POSTGRES_MIGRATIONS: dict[int, str] = {
    6: """
-- Web Push notifications. A recipient is a staff account or a citizen inbox (a device-held bearer
-- token; only its SHA-256 hash is stored). Citizens receive notifications for complaints they follow,
-- proven with the claim code. Each recipient may register several devices (push subscriptions).
CREATE TABLE notification_recipients (
    pk          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('citizen', 'staff')),
    staff_pk    BIGINT UNIQUE REFERENCES staff_accounts(pk) ON DELETE CASCADE,
    token_hash  TEXT UNIQUE CHECK (token_hash IS NULL OR length(token_hash) = 64),
    created_at  TEXT NOT NULL,
    CHECK ((kind = 'staff' AND staff_pk IS NOT NULL AND token_hash IS NULL)
        OR (kind = 'citizen' AND staff_pk IS NULL AND token_hash IS NOT NULL))
);

CREATE TABLE notification_follows (
    pk            BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    recipient_pk  BIGINT NOT NULL REFERENCES notification_recipients(pk) ON DELETE CASCADE,
    complaint_pk  BIGINT NOT NULL REFERENCES complaints(pk) ON DELETE CASCADE,
    created_at    TEXT NOT NULL,
    UNIQUE (recipient_pk, complaint_pk)
);
CREATE INDEX notification_follows_by_complaint ON notification_follows (complaint_pk);

-- One row per device and recipient. The endpoint is a capability URL at the browser's push service;
-- it and the keys are never returned by the API.
CREATE TABLE push_subscriptions (
    pk               BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    subscription_id  TEXT NOT NULL UNIQUE,
    recipient_pk     BIGINT NOT NULL REFERENCES notification_recipients(pk) ON DELETE CASCADE,
    endpoint         TEXT NOT NULL,
    p256dh           TEXT NOT NULL,
    auth             TEXT NOT NULL,
    label            TEXT NOT NULL DEFAULT '',
    expires_at       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    last_success_at  TEXT,
    last_failure_at  TEXT,
    failure_count    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (recipient_pk, endpoint)
);

-- Persistent history. dedupe_key makes each event notify a recipient at most once.
CREATE TABLE notifications (
    pk               BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    notification_id  TEXT NOT NULL UNIQUE,
    recipient_pk     BIGINT NOT NULL REFERENCES notification_recipients(pk) ON DELETE CASCADE,
    complaint_pk     BIGINT REFERENCES complaints(pk) ON DELETE CASCADE,
    kind             TEXT NOT NULL CHECK (kind IN ('follow_confirmed', 'evidence_received', 'evidence_reviewed', 'new_complaint', 'new_evidence', 'test')),
    title            TEXT NOT NULL,
    body             TEXT NOT NULL,
    url              TEXT NOT NULL,
    dedupe_key       TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    read_at          TEXT,
    push_status      TEXT NOT NULL DEFAULT 'pending' CHECK (push_status IN ('pending', 'sent', 'partial', 'failed', 'no_devices', 'disabled')),
    UNIQUE (recipient_pk, dedupe_key)
);
CREATE INDEX notifications_by_recipient ON notifications (recipient_pk, pk);
""",
    7: """
-- Complaint management (Workstream 1): the same changes as SQLite MIGRATIONS[7]. Constraint names are
-- PostgreSQL's defaults from POSTGRES_SCHEMA; if one differs, DROP CONSTRAINT fails and the whole
-- migration rolls back rather than leaving an old constraint in place.
ALTER TABLE staff_accounts DROP CONSTRAINT staff_accounts_role_check;
ALTER TABLE staff_accounts ADD CONSTRAINT staff_accounts_role_check CHECK (role IN ('official', 'supervisor', 'admin'));

ALTER TABLE evidence DROP CONSTRAINT evidence_role_check;
ALTER TABLE evidence ADD CONSTRAINT evidence_role_check CHECK (role IN ('original', 'repair'));
ALTER TABLE evidence DROP CONSTRAINT evidence_complaint_pk_role_key;
CREATE UNIQUE INDEX evidence_one_original ON evidence (complaint_pk) WHERE role = 'original';
CREATE INDEX evidence_by_complaint ON evidence (complaint_pk, role);
ALTER TABLE evidence ADD COLUMN uploaded_by_staff_pk BIGINT REFERENCES staff_accounts(pk);

CREATE TABLE departments (
    pk          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at  TEXT NOT NULL
);

CREATE TABLE staff_departments (
    pk             BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    staff_pk       BIGINT NOT NULL REFERENCES staff_accounts(pk) ON DELETE CASCADE,
    department_pk  BIGINT NOT NULL REFERENCES departments(pk),
    created_at     TEXT NOT NULL,
    UNIQUE (staff_pk, department_pk)
);

ALTER TABLE complaints ADD COLUMN department_pk BIGINT REFERENCES departments(pk);
ALTER TABLE complaints ADD COLUMN assigned_staff_pk BIGINT REFERENCES staff_accounts(pk);
ALTER TABLE complaints ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
CREATE INDEX complaints_by_status ON complaints (status, pk);
CREATE INDEX complaints_by_department ON complaints (department_pk, status);

ALTER TABLE complaint_timeline ADD COLUMN kind TEXT NOT NULL DEFAULT 'progress'
    CHECK (kind IN ('progress', 'status_change', 'department_response', 'assignment', 'work_order', 'verification'));
ALTER TABLE complaint_timeline ADD COLUMN actor_staff_pk BIGINT REFERENCES staff_accounts(pk);

CREATE TABLE complaint_assignments (
    pk                 BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    assignment_id      TEXT NOT NULL UNIQUE,
    complaint_pk       BIGINT NOT NULL REFERENCES complaints(pk),
    department_pk      BIGINT NOT NULL REFERENCES departments(pk),
    assignee_staff_pk  BIGINT REFERENCES staff_accounts(pk),
    reason             TEXT NOT NULL DEFAULT '',
    actor_staff_pk     BIGINT NOT NULL REFERENCES staff_accounts(pk),
    assigned_at        TEXT NOT NULL
);
CREATE INDEX complaint_assignments_by_complaint ON complaint_assignments (complaint_pk, pk);

CREATE TABLE staff_notes (
    pk               BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    note_id          TEXT NOT NULL UNIQUE,
    complaint_pk     BIGINT NOT NULL REFERENCES complaints(pk),
    author_staff_pk  BIGINT NOT NULL REFERENCES staff_accounts(pk),
    body             TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 2000),
    created_at       TEXT NOT NULL
);
CREATE INDEX staff_notes_by_complaint ON staff_notes (complaint_pk, pk);

CREATE TABLE work_orders (
    pk                   BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    work_order_id        TEXT NOT NULL UNIQUE,
    complaint_pk         BIGINT NOT NULL REFERENCES complaints(pk),
    department_pk        BIGINT REFERENCES departments(pk),
    team                 TEXT NOT NULL,
    assignee_staff_pk    BIGINT REFERENCES staff_accounts(pk),
    description          TEXT NOT NULL DEFAULT '',
    due_at               TEXT,
    status               TEXT NOT NULL DEFAULT 'Open' CHECK (status IN ('Open', 'In Progress', 'Completed', 'Cancelled')),
    started_at           TEXT,
    completed_at         TEXT,
    completion_note      TEXT NOT NULL DEFAULT '',
    created_by_staff_pk  BIGINT NOT NULL REFERENCES staff_accounts(pk),
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    version              INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX work_orders_by_complaint ON work_orders (complaint_pk, pk);
CREATE INDEX work_orders_by_status ON work_orders (status, due_at);

CREATE TABLE work_order_events (
    pk              BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    work_order_pk   BIGINT NOT NULL REFERENCES work_orders(pk),
    action          TEXT NOT NULL,
    from_status     TEXT,
    to_status       TEXT,
    note            TEXT NOT NULL DEFAULT '',
    actor_staff_pk  BIGINT NOT NULL REFERENCES staff_accounts(pk),
    created_at      TEXT NOT NULL
);
CREATE INDEX work_order_events_by_order ON work_order_events (work_order_pk, pk);

CREATE TABLE work_order_evidence (
    pk                  BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    work_order_pk       BIGINT NOT NULL REFERENCES work_orders(pk),
    evidence_pk         BIGINT NOT NULL REFERENCES evidence(pk),
    phase               TEXT NOT NULL CHECK (phase IN ('before', 'after')),
    linked_by_staff_pk  BIGINT NOT NULL REFERENCES staff_accounts(pk),
    linked_at           TEXT NOT NULL,
    UNIQUE (work_order_pk, evidence_pk)
);

CREATE TABLE audit_log (
    pk              BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    audit_id        TEXT NOT NULL UNIQUE,
    complaint_pk    BIGINT REFERENCES complaints(pk),
    actor_type      TEXT NOT NULL CHECK (actor_type IN ('staff', 'citizen', 'system')),
    actor_staff_pk  BIGINT REFERENCES staff_accounts(pk),
    action          TEXT NOT NULL,
    details         TEXT NOT NULL DEFAULT '{}',
    client_ip       TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX audit_log_by_complaint ON audit_log (complaint_pk, pk);

CREATE OR REPLACE FUNCTION civiclens_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END
$$;
CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON audit_log FOR EACH ROW EXECUTE FUNCTION civiclens_append_only();
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log FOR EACH STATEMENT EXECUTE FUNCTION civiclens_append_only();
CREATE TRIGGER complaint_assignments_append_only BEFORE UPDATE OR DELETE ON complaint_assignments FOR EACH ROW EXECUTE FUNCTION civiclens_append_only();
CREATE TRIGGER staff_notes_append_only BEFORE UPDATE OR DELETE ON staff_notes FOR EACH ROW EXECUTE FUNCTION civiclens_append_only();
CREATE TRIGGER work_order_events_append_only BEFORE UPDATE OR DELETE ON work_order_events FOR EACH ROW EXECUTE FUNCTION civiclens_append_only();
""",
}

# Tables in foreign-key order (parents first): used by the SQLite → PostgreSQL migration tool.
# Staff accounts and departments come first because complaints (schema 7) reference them.
# Named migrations (db.Feature), applied after the numbered ones and recorded by name in
# civiclens_features. New schema changes go here, not into MIGRATIONS: origin/main and
# origin/feature/core-platform-development each shipped a different MIGRATIONS[8], so a numbered
# version above 7 no longer identifies a schema (docs/MIGRATION_RECONCILIATION.md).
# Duplicate detection (Workstream 2): first shipped as feature/core-platform-development's MIGRATIONS[8];
# the SQL is unchanged. adopt_if: a database upgraded by that branch already has these objects.
DUPLICATE_DETECTION_SQLITE = """
-- Duplicate detection (Workstream 2). A merge links a duplicate report to a canonical complaint; both
-- records, their evidence and history stay intact. Reverting sets reverted_at; rows are never deleted.
ALTER TABLE complaints ADD COLUMN merged_into_pk INTEGER REFERENCES complaints(pk);
CREATE INDEX complaints_by_location ON complaints (gps_lat, gps_lng);
CREATE INDEX complaints_by_merge ON complaints (merged_into_pk);

CREATE TABLE complaint_merges (
    pk                    INTEGER PRIMARY KEY,
    merge_id              TEXT NOT NULL UNIQUE,
    duplicate_pk          INTEGER NOT NULL REFERENCES complaints(pk),
    canonical_pk          INTEGER NOT NULL REFERENCES complaints(pk),
    reason                TEXT NOT NULL,
    score                 REAL,
    label                 TEXT,
    factors               TEXT NOT NULL DEFAULT '[]',
    actor_staff_pk        INTEGER NOT NULL REFERENCES staff_accounts(pk),
    created_at            TEXT NOT NULL,
    reverted_at           TEXT,
    reverted_by_staff_pk  INTEGER REFERENCES staff_accounts(pk),
    revert_reason         TEXT,
    CHECK (duplicate_pk <> canonical_pk)
);
-- At most one active merge per duplicate.
CREATE UNIQUE INDEX complaint_merges_active ON complaint_merges (duplicate_pk) WHERE reverted_at IS NULL;
CREATE INDEX complaint_merges_by_canonical ON complaint_merges (canonical_pk);
CREATE TRIGGER complaint_merges_no_delete BEFORE DELETE ON complaint_merges BEGIN SELECT RAISE(ABORT, 'complaint_merges is append-only'); END;

-- Pairs a supervisor marked "not a duplicate", so they stop being suggested. Stored with the smaller pk first.
CREATE TABLE duplicate_dismissals (
    pk              INTEGER PRIMARY KEY,
    complaint_a_pk  INTEGER NOT NULL REFERENCES complaints(pk),
    complaint_b_pk  INTEGER NOT NULL REFERENCES complaints(pk),
    reason          TEXT NOT NULL DEFAULT '',
    actor_staff_pk  INTEGER NOT NULL REFERENCES staff_accounts(pk),
    created_at      TEXT NOT NULL,
    CHECK (complaint_a_pk < complaint_b_pk),
    UNIQUE (complaint_a_pk, complaint_b_pk)
);
"""

DUPLICATE_DETECTION_POSTGRES = """
-- Duplicate detection (Workstream 2): the same changes as SQLite MIGRATIONS[8].
ALTER TABLE complaints ADD COLUMN merged_into_pk BIGINT REFERENCES complaints(pk);
CREATE INDEX complaints_by_location ON complaints (gps_lat, gps_lng);
CREATE INDEX complaints_by_merge ON complaints (merged_into_pk);

CREATE TABLE complaint_merges (
    pk                    BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    merge_id              TEXT NOT NULL UNIQUE,
    duplicate_pk          BIGINT NOT NULL REFERENCES complaints(pk),
    canonical_pk          BIGINT NOT NULL REFERENCES complaints(pk),
    reason                TEXT NOT NULL,
    score                 DOUBLE PRECISION,
    label                 TEXT,
    factors               TEXT NOT NULL DEFAULT '[]',
    actor_staff_pk        BIGINT NOT NULL REFERENCES staff_accounts(pk),
    created_at            TEXT NOT NULL,
    reverted_at           TEXT,
    reverted_by_staff_pk  BIGINT REFERENCES staff_accounts(pk),
    revert_reason         TEXT,
    CHECK (duplicate_pk <> canonical_pk)
);
CREATE UNIQUE INDEX complaint_merges_active ON complaint_merges (duplicate_pk) WHERE reverted_at IS NULL;
CREATE INDEX complaint_merges_by_canonical ON complaint_merges (canonical_pk);
CREATE TRIGGER complaint_merges_no_delete BEFORE DELETE OR TRUNCATE ON complaint_merges FOR EACH STATEMENT EXECUTE FUNCTION civiclens_append_only();

CREATE TABLE duplicate_dismissals (
    pk              BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    complaint_a_pk  BIGINT NOT NULL REFERENCES complaints(pk),
    complaint_b_pk  BIGINT NOT NULL REFERENCES complaints(pk),
    reason          TEXT NOT NULL DEFAULT '',
    actor_staff_pk  BIGINT NOT NULL REFERENCES staff_accounts(pk),
    created_at      TEXT NOT NULL,
    CHECK (complaint_a_pk < complaint_b_pk),
    UNIQUE (complaint_a_pk, complaint_b_pk)
);
"""

RESIDENT_ACCOUNTS_SQLITE = """
-- Resident accounts (server-backed sign-in), separate from staff accounts and from anonymous reporting.
-- A complaint's owner is set only from a validated resident session: when a signed-in resident submits
-- it (owned_via 'submitted') or claims it with its private claim code ('claimed'). Existing complaints
-- keep owner_resident_pk NULL: a passerby's report is never assigned to anyone automatically.
CREATE TABLE resident_accounts (
    pk             INTEGER PRIMARY KEY,
    resident_id    TEXT NOT NULL UNIQUE,
    email          TEXT NOT NULL UNIQUE CHECK (email = lower(email) AND length(email) BETWEEN 3 AND 254),
    display_name   TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 80),
    password_hash  TEXT NOT NULL,
    is_demo        INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1)),
    created_at     TEXT NOT NULL,
    disabled_at    TEXT
);
CREATE TABLE resident_sessions (
    pk           INTEGER PRIMARY KEY,
    token_hash   TEXT NOT NULL UNIQUE CHECK (length(token_hash) = 64),
    resident_pk  INTEGER NOT NULL REFERENCES resident_accounts(pk) ON DELETE CASCADE,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    revoked_at   TEXT
);
CREATE TABLE resident_preferences (
    pk           INTEGER PRIMARY KEY,
    resident_pk  INTEGER NOT NULL UNIQUE REFERENCES resident_accounts(pk) ON DELETE CASCADE,
    preferences  TEXT NOT NULL DEFAULT '{}',
    updated_at   TEXT NOT NULL
);
ALTER TABLE complaints ADD COLUMN owner_resident_pk INTEGER REFERENCES resident_accounts(pk);
ALTER TABLE complaints ADD COLUMN owned_via TEXT CHECK (owned_via IS NULL OR owned_via IN ('submitted', 'claimed'));
ALTER TABLE complaints ADD COLUMN owned_at TEXT;
CREATE INDEX complaints_by_owner ON complaints (owner_resident_pk, pk);
"""

RESIDENT_ACCOUNTS_POSTGRES = """
-- Resident accounts: the same tables and columns as RESIDENT_ACCOUNTS_SQLITE.
CREATE TABLE resident_accounts (
    pk             BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    resident_id    TEXT NOT NULL UNIQUE,
    email          TEXT NOT NULL UNIQUE CHECK (email = lower(email) AND length(email) BETWEEN 3 AND 254),
    display_name   TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 80),
    password_hash  TEXT NOT NULL,
    is_demo        INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1)),
    created_at     TEXT NOT NULL,
    disabled_at    TEXT
);
CREATE TABLE resident_sessions (
    pk           BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    token_hash   TEXT NOT NULL UNIQUE CHECK (length(token_hash) = 64),
    resident_pk  BIGINT NOT NULL REFERENCES resident_accounts(pk) ON DELETE CASCADE,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    revoked_at   TEXT
);
CREATE TABLE resident_preferences (
    pk           BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    resident_pk  BIGINT NOT NULL UNIQUE REFERENCES resident_accounts(pk) ON DELETE CASCADE,
    preferences  TEXT NOT NULL DEFAULT '{}',
    updated_at   TEXT NOT NULL
);
ALTER TABLE complaints ADD COLUMN owner_resident_pk BIGINT REFERENCES resident_accounts(pk);
ALTER TABLE complaints ADD COLUMN owned_via TEXT CHECK (owned_via IS NULL OR owned_via IN ('submitted', 'claimed'));
ALTER TABLE complaints ADD COLUMN owned_at TEXT;
CREATE INDEX complaints_by_owner ON complaints (owner_resident_pk, pk);
"""

FEATURES: tuple[Feature, ...] = (
    Feature("duplicate_detection", DUPLICATE_DETECTION_SQLITE, DUPLICATE_DETECTION_POSTGRES, adopt_if="complaint_merges"),
    Feature("resident_accounts", RESIDENT_ACCOUNTS_SQLITE, RESIDENT_ACCOUNTS_POSTGRES),
)

TABLES = ("staff_accounts", "departments", "staff_departments", "resident_accounts",
          "complaints", "complaint_timeline", "verification_log", "evidence", "idempotency_keys",
          "staff_sessions", "evidence_access_log",
          "notification_recipients", "notification_follows", "push_subscriptions", "notifications",
          "complaint_assignments", "staff_notes", "work_orders", "work_order_events", "work_order_evidence", "audit_log",
          "resident_sessions", "resident_preferences", "complaint_merges", "duplicate_dismissals")

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


class ResidentExists(Exception):
    """A resident account with that email address already exists."""


STAFF_LIST_MAX = 100
NOTIFICATION_PAGE_MAX = 100

# Device fields the API may show: never the endpoint or keys.
_DEVICE_COLUMNS = "subscription_id, label, created_at, updated_at, last_success_at, last_failure_at, failure_count"
_NOTIFICATION_COLUMNS = ("n.notification_id, n.kind, n.title, n.body, n.url, n.created_at, n.read_at, n.push_status, "
                         "c.public_id AS complaint_public_id")


def _device_dict(row) -> dict:
    return {"subscriptionId": row["subscription_id"], "label": row["label"], "createdAt": row["created_at"],
            "updatedAt": row["updated_at"], "lastSuccessAt": row["last_success_at"], "lastFailureAt": row["last_failure_at"],
            "failureCount": row["failure_count"]}


def _notification_dict(row) -> dict:
    return {"id": row["notification_id"], "kind": row["kind"], "title": row["title"], "body": row["body"], "url": row["url"],
            "complaintId": row["complaint_public_id"], "createdAt": row["created_at"], "readAt": row["read_at"],
            "read": row["read_at"] is not None, "pushStatus": row["push_status"]}


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
                self.db.apply_schema(POSTGRES_SCHEMA, POSTGRES_MIGRATIONS, POSTGRES_BASE_VERSION, FEATURES)
            else:
                self.db.apply_schema(SCHEMA, MIGRATIONS, FEATURES)
        except (OSError, *DB_ERRORS) as exc:
            raise StoreError("Could not initialise the complaint database") from exc

    def close(self) -> None:
        """Release pooled connections (PostgreSQL). Safe to call more than once."""
        self.db.close()

    def _token_expiry(self) -> str:
        return iso(datetime.now(timezone.utc) + self.upload_token_ttl)

    def create(self, data: dict, idempotency: dict | None = None, owner_resident_pk: int | None = None) -> tuple[dict, str]:
        """Insert a complaint. Returns (stored record, plaintext claim code).

        `data` holds validated fields from ComplaintCreate. The claim code is
        generated here, hashed, and returned to the caller exactly once. A first
        upload token is issued in the same transaction; the returned record carries
        it as `uploadToken` / `uploadTokenExpiresAt` (only the hash is stored).
        With `idempotency` ({"keyHash", "fingerprint"}) the key row is inserted in the
        same transaction, so a failed create leaves no key behind.
        `owner_resident_pk` comes only from a validated resident session (never from the request
        body); without it the complaint is anonymous and stays unowned until a claim.
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
        if owner_resident_pk is not None:
            row.update({"owner_resident_pk": owner_resident_pk, "owned_via": "submitted", "owned_at": now})
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
                          replay_window: timedelta = DEFAULT_REPLAY_WINDOW, owner_resident_pk: int | None = None) -> tuple[str, dict, str]:
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
                    record, claim = self.create(data, {"keyHash": key_hash, "fingerprint": fingerprint}, owner_resident_pk)
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

    def create_staff(self, username: str, display_name: str, password_hash: str, role: str = "official") -> None:
        if role not in lc.ROLES:
            raise RuleViolation(422, "invalid_role", f"Role must be one of {', '.join(lc.ROLES)}.")
        try:
            with closing(self._connect()) as conn, conn.transaction():
                conn.execute("INSERT INTO staff_accounts (username, display_name, role, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                             (username, display_name, role, password_hash, utc_now()))
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

    # ---------- Resident accounts, sessions, ownership and preferences ----------
    # Separate from staff accounts: a resident session is looked up only in resident_sessions and a
    # staff session only in staff_sessions, so neither token can stand in for the other.

    def create_resident(self, email: str, display_name: str, password_hash: str, is_demo: bool = False) -> dict:
        try:
            with closing(self._connect()) as conn, conn.transaction():
                conn.execute("INSERT INTO resident_accounts (resident_id, email, display_name, password_hash, is_demo, created_at) "
                             "VALUES (?, ?, ?, ?, ?, ?)",
                             ("res_" + secrets.token_urlsafe(12), email, display_name, password_hash, 1 if is_demo else 0, utc_now()))
                return dict(conn.execute("SELECT * FROM resident_accounts WHERE email = ?", (email,)).fetchone())
        except INTEGRITY_ERRORS as exc:
            raise ResidentExists(email) from exc
        except DB_ERRORS as exc:
            raise StoreError("Resident account could not be created") from exc

    def get_resident(self, email: str) -> dict | None:
        try:
            with closing(self._connect()) as conn:
                row = conn.execute("SELECT * FROM resident_accounts WHERE email = ?", (email,)).fetchone()
        except DB_ERRORS as exc:
            raise StoreError("Resident account could not be read") from exc
        return dict(row) if row else None

    def update_resident(self, email: str, *, display_name: str | None = None, password_hash: str | None = None,
                        disabled: bool | None = None, is_demo: bool | None = None) -> bool:
        """Changing the password or disabling the account revokes its open sessions."""
        sets, params = [], []
        for column, value in (("display_name", display_name), ("password_hash", password_hash),
                              ("is_demo", None if is_demo is None else int(is_demo))):
            if value is not None:
                sets.append(f"{column} = ?")
                params.append(value)
        if disabled is not None:
            sets.append("disabled_at = ?")
            params.append(utc_now() if disabled else None)
        if not sets:
            return self.get_resident(email) is not None
        try:
            with closing(self._connect()) as conn, conn.transaction():
                cur = conn.execute(f"UPDATE resident_accounts SET {', '.join(sets)} WHERE email = ?", (*params, email))
                if password_hash is not None or disabled:
                    conn.execute("UPDATE resident_sessions SET revoked_at = ? WHERE revoked_at IS NULL AND resident_pk = "
                                 "(SELECT pk FROM resident_accounts WHERE email = ?)", (utc_now(), email))
                return cur.rowcount == 1
        except DB_ERRORS as exc:
            raise StoreError("Resident account could not be updated") from exc

    def create_resident_session(self, resident_pk: int, token_hash: str, expires_at: str) -> None:
        self._write("INSERT INTO resident_sessions (token_hash, resident_pk, created_at, expires_at) VALUES (?, ?, ?, ?)",
                    (token_hash, resident_pk, utc_now(), expires_at), "Resident session creation")

    def get_resident_session(self, token_hash: str) -> dict | None:
        """The session's resident account, or None if the token is unknown, expired, revoked or the account is disabled."""
        try:
            with closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT a.pk, a.resident_id, a.email, a.display_name, a.is_demo, a.created_at, s.expires_at FROM resident_sessions s "
                    "JOIN resident_accounts a ON a.pk = s.resident_pk "
                    "WHERE s.token_hash = ? AND s.revoked_at IS NULL AND s.expires_at > ? AND a.disabled_at IS NULL",
                    (token_hash, utc_now()),
                ).fetchone()
        except DB_ERRORS as exc:
            raise StoreError("Resident session could not be read") from exc
        return dict(row) if row else None

    def revoke_resident_session(self, token_hash: str) -> None:
        self._write("UPDATE resident_sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                    (utc_now(), token_hash), "Resident session revocation")

    def owner_of(self, public_id: str) -> int | None:
        """The owning resident's pk, or None (unowned or no such complaint)."""
        try:
            with closing(self._connect()) as conn:
                row = conn.execute("SELECT owner_resident_pk FROM complaints WHERE public_id = ?", (public_id,)).fetchone()
        except DB_ERRORS as exc:
            raise StoreError("Complaint could not be read") from exc
        return row[0] if row else None

    def resident_complaints(self, resident_pk: int, limit: int = 100, offset: int = 0) -> tuple[list[dict], int]:
        """The resident's own complaints, newest first, with ownership details. Returns (records, total)."""
        limit = max(1, min(int(limit), STAFF_LIST_MAX))
        try:
            with closing(self._connect()) as conn:
                total = conn.execute("SELECT COUNT(*) FROM complaints WHERE owner_resident_pk = ?", (resident_pk,)).fetchone()[0]
                rows = conn.execute("SELECT public_id, owned_via, owned_at, pk FROM complaints WHERE owner_resident_pk = ? "
                                    "ORDER BY pk DESC LIMIT ? OFFSET ?", (resident_pk, limit, max(0, int(offset)))).fetchall()
                out = []
                for r in rows:
                    record = self._load(conn, r["public_id"])
                    evidence = self._owner_evidence(conn, r["pk"])
                    out.append({**record, "ownedVia": r["owned_via"], "ownedAt": r["owned_at"],
                                "hasEvidence": any(e["role"] == "original" for e in evidence), "evidence": evidence})
                return out, int(total)
        except DB_ERRORS as exc:
            raise StoreError("Resident complaints could not be read") from exc

    def resident_complaint(self, resident_pk: int, public_id: str) -> dict | None:
        """One complaint, only if this resident owns it."""
        try:
            with closing(self._connect()) as conn:
                row = conn.execute("SELECT pk, owned_via, owned_at FROM complaints WHERE public_id = ? AND owner_resident_pk = ?",
                                   (public_id, resident_pk)).fetchone()
                if row is None:
                    return None
                evidence = self._owner_evidence(conn, row["pk"])
                return {**self._load(conn, public_id), "ownedVia": row["owned_via"], "ownedAt": row["owned_at"],
                        "hasEvidence": any(e["role"] == "original" for e in evidence), "evidence": evidence}
        except DB_ERRORS as exc:
            raise StoreError("Complaint could not be read") from exc

    @staticmethod
    def _owner_evidence(conn, complaint_pk: int) -> list[dict]:
        """Evidence metadata an owner may see for their own complaint: the file they reported (role
        'original') and after-repair photos staff attached (role 'repair'), oldest first. Never the
        storage key, storage backend or uploader; files are fetched through resident_evidence()."""
        rows = conn.execute(
            "SELECT evidence_id, role, media_type, content_type, byte_size, captured_at, uploaded_at FROM evidence "
            "WHERE complaint_pk = ? ORDER BY CASE role WHEN 'original' THEN 0 ELSE 1 END, pk", (complaint_pk,)).fetchall()
        return [{"evidenceId": r["evidence_id"], "role": r["role"], "mediaType": r["media_type"], "contentType": r["content_type"],
                 "byteSize": r["byte_size"], "capturedAt": r["captured_at"], "uploadedAt": r["uploaded_at"]} for r in rows]

    def resident_evidence(self, resident_pk: int, public_id: str, evidence_id: str) -> dict | None:
        """One evidence file's metadata (with pk and storage key, for streaming) only if it belongs to a
        complaint this resident owns; None otherwise, so another resident's file is indistinguishable
        from one that does not exist."""
        try:
            with closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT e.* FROM evidence e JOIN complaints c ON c.pk = e.complaint_pk "
                    "WHERE c.public_id = ? AND c.owner_resident_pk = ? AND e.evidence_id = ?",
                    (public_id, resident_pk, evidence_id)).fetchone()
        except DB_ERRORS as exc:
            raise StoreError("Evidence could not be read") from exc
        return _evidence_dict(row) | {"pk": row["pk"]} if row else None

    def claim_complaint(self, public_id: str, resident: dict, client_ip: str | None = None) -> str:
        """Attach an unowned complaint to `resident`. The caller has ALREADY verified the claim code.
        Returns "claimed", or "already_yours". Raises RuleViolation 409 when another resident owns it.
        Only the ownership columns change: the claim hash, timeline, evidence and dates stay as they were,
        and the claim is recorded in the audit log (never in the public timeline)."""
        now = utc_now()

        def run(conn):
            row = conn.execute("SELECT pk, owner_resident_pk FROM complaints WHERE public_id = ?" + self.db.for_update, (public_id,)).fetchone()
            if row is None:
                raise RuleViolation(404, "not_found", "No complaint was found with that ID.")
            if row["owner_resident_pk"] == resident["pk"]:
                return "already_yours"
            if row["owner_resident_pk"] is not None:
                raise RuleViolation(409, "already_claimed", "This complaint is already linked to another resident account.")
            # Using the claim code counts as using the receipt's credentials (as an upload-token renewal does),
            # so an idempotent replay can no longer re-issue a new claim code for this complaint.
            conn.execute("UPDATE complaints SET owner_resident_pk = ?, owned_via = 'claimed', owned_at = ?, "
                         "credentials_used_at = COALESCE(credentials_used_at, ?) WHERE pk = ? AND owner_resident_pk IS NULL",
                         (resident["pk"], now, now, row["pk"]))
            self._audit(conn, row["pk"], None, "resident_claim", {"residentId": resident["residentId"]}, client_ip, now, actor_type="citizen")
            return "claimed"

        return self._mutate("Complaint claim", run)

    def get_resident_preferences(self, resident_pk: int) -> dict:
        try:
            with closing(self._connect()) as conn:
                row = conn.execute("SELECT preferences FROM resident_preferences WHERE resident_pk = ?", (resident_pk,)).fetchone()
        except DB_ERRORS as exc:
            raise StoreError("Preferences could not be read") from exc
        return json.loads(row[0]) if row else {}

    def set_resident_preferences(self, resident_pk: int, preferences: dict) -> dict:
        body = json.dumps(preferences, sort_keys=True, separators=(",", ":"))
        try:
            with closing(self._connect()) as conn, conn.transaction():
                conn.execute("INSERT INTO resident_preferences (resident_pk, preferences, updated_at) VALUES (?, ?, ?) "
                             "ON CONFLICT (resident_pk) DO UPDATE SET preferences = excluded.preferences, updated_at = excluded.updated_at",
                             (resident_pk, body, utc_now()))
        except DB_ERRORS as exc:
            raise StoreError("Preferences could not be saved") from exc
        return preferences

    # ---------- Staff views of complaints ----------

    def list_complaints(self, limit: int = 50, offset: int = 0, staff_pk: int | None = None) -> tuple[list[dict], int]:
        """Newest first. Returns (summaries, total). With `staff_pk`, an official sees only their scope
        (lifecycle.in_scope): their departments, their own assignments and the unassigned triage queue.
        A fixed number of queries whatever the page size: the list must not cost a round trip per
        complaint (the database can be far from the API)."""
        limit = max(1, min(int(limit), STAFF_LIST_MAX))
        try:
            with closing(self._connect()) as conn:
                where, params = "", ()
                if staff_pk is not None:
                    actor = self._actor(conn, staff_pk)
                    if not lc.has_role(actor, "supervisor"):
                        marks = ", ".join("?" * len(actor["departmentPks"])) or "NULL"
                        where = f" WHERE c.department_pk IS NULL OR c.department_pk IN ({marks}) OR c.assigned_staff_pk = ?"
                        params = (*actor["departmentPks"], actor["pk"])
                total = conn.execute("SELECT COUNT(*) FROM complaints c" + where, params).fetchone()[0]
                rows = conn.execute(
                    "SELECT c.public_id, c.title, c.category, c.area, c.status, c.verification, c.department, c.created_at, "
                    "c.updated_at, c.media_type, c.gps_lat, c.gps_lng, c.gps_accuracy, c.gps_source_type, "
                    "EXISTS (SELECT 1 FROM evidence e WHERE e.complaint_pk = c.pk AND e.role = 'original') AS has_evidence, "
                    "(SELECT m.public_id FROM complaints m WHERE m.pk = c.merged_into_pk) AS merged_into "
                    "FROM complaints c" + where + " ORDER BY c.pk DESC LIMIT ? OFFSET ?",
                    (*params, limit, max(0, int(offset)))).fetchall()
        except RuleViolation:
            raise
        except DB_ERRORS as exc:
            raise StoreError("Complaints could not be listed") from exc
        return [{
            "id": r["public_id"], "title": r["title"], "category": r["category"], "area": r["area"], "status": r["status"],
            "verification": r["verification"], "department": r["department"], "created": r["created_at"],
            "updated": r["updated_at"], "mediaType": r["media_type"], "hasEvidence": bool(r["has_evidence"]),
            "gps": {"lat": r["gps_lat"], "lng": r["gps_lng"], "accuracy": r["gps_accuracy"], "sourceType": r["gps_source_type"]},
            "mergedInto": r["merged_into"],
        } for r in rows], total

    # ---------- Public community list ----------

    def list_public_complaints(self, category: str | None = None, area: str | None = None, status: str | None = None,
                               limit: int = 50, offset: int = 0) -> dict:
        """Community view of every complaint in the Bengaluru service area, newest first. Only public
        fields (the same allowlist as public tracking: no coordinates, description, evidence or claim
        data). Reports merged into another as duplicates are left out; their canonical complaint is listed.
        Facets count every listed complaint by category and by neighbourhood, ignoring the filters, so the
        filter menus always offer every option."""
        box = geo.SERVICE_AREA
        base = ("FROM complaints c WHERE c.merged_into_pk IS NULL AND c.gps_lat BETWEEN ? AND ? AND c.gps_lng BETWEEN ? AND ?")
        base_params = (box["south"], box["north"], box["west"], box["east"])
        where, params = base, base_params
        for column, value in (("category", category), ("area", area), ("status", status)):
            if value:
                where += f" AND c.{column} = ?"
                params += (value,)
        limit = max(1, min(int(limit), STAFF_LIST_MAX))
        try:
            with closing(self._connect()) as conn:
                total = conn.execute(f"SELECT COUNT(*) {where}", params).fetchone()[0]
                rows = conn.execute(
                    f"SELECT c.public_id, c.title, c.category, c.area, c.status, c.verification, c.department, c.created_at, c.updated_at, "
                    f"c.gps_lat, c.gps_lng "
                    f"{where} ORDER BY c.pk DESC LIMIT ? OFFSET ?", (*params, limit, max(0, int(offset)))).fetchall()
                categories = conn.execute(f"SELECT c.category, COUNT(*) {base} GROUP BY c.category ORDER BY COUNT(*) DESC, c.category",
                                          base_params).fetchall()
                areas = conn.execute(f"SELECT c.area, COUNT(*) {base} GROUP BY c.area ORDER BY COUNT(*) DESC, c.area", base_params).fetchall()
        except DB_ERRORS as exc:
            raise StoreError("Community complaints could not be listed") from exc
        return {
            "items": [{"id": r["public_id"], "title": r["title"], "category": r["category"], "area": r["area"], "status": r["status"],
                       "verification": r["verification"], "department": r["department"], "created": r["created_at"],
                       "updated": r["updated_at"], "mapPoint": geo.public_map_point(r["gps_lat"], r["gps_lng"])} for r in rows],
            "total": total, "limit": limit, "offset": max(0, int(offset)),
            "facets": {"categories": [{"name": r[0], "count": r[1]} for r in categories],
                       "areas": [{"name": r[0], "count": r[1]} for r in areas]},
        }

    def require_case_scope(self, public_id: str, staff_pk: int) -> None:
        """Raises RuleViolation 403 when an official may not see this complaint (lifecycle.in_scope)."""
        try:
            with closing(self._connect()) as conn:
                case = self._case(conn, public_id)
                if case is not None:
                    lc.require_scope(case, self._actor(conn, staff_pk))
        except RuleViolation:
            raise
        except DB_ERRORS as exc:
            raise StoreError("Complaint could not be read") from exc

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

    # ---------- Notifications: recipients, follows, push subscriptions, history ----------

    def complaint_ref(self, public_id: str) -> dict | None:
        """{"pk", "claimHash"} for a complaint, or None."""
        try:
            with closing(self._connect()) as conn:
                row = conn.execute("SELECT pk, claim_hash FROM complaints WHERE public_id = ?", (public_id,)).fetchone()
        except DB_ERRORS as exc:
            raise StoreError("Complaint could not be read") from exc
        return {"pk": row["pk"], "claimHash": row["claim_hash"]} if row else None

    def staff_recipient(self, staff_pk: int) -> dict:
        """The staff account's notification recipient, created on first use."""
        try:
            with closing(self._connect()) as conn, conn.transaction():
                conn.execute("INSERT INTO notification_recipients (kind, staff_pk, created_at) VALUES ('staff', ?, ?) "
                             "ON CONFLICT (staff_pk) DO NOTHING", (staff_pk, utc_now()))
                row = conn.execute("SELECT pk, kind FROM notification_recipients WHERE staff_pk = ?", (staff_pk,)).fetchone()
        except DB_ERRORS as exc:
            raise StoreError("Notification recipient could not be read") from exc
        return dict(row)

    def create_citizen_recipient(self, token_hash: str) -> dict:
        try:
            with closing(self._connect()) as conn, conn.transaction():
                pk = conn.insert("INSERT INTO notification_recipients (kind, token_hash, created_at) VALUES ('citizen', ?, ?)",
                                 (token_hash, utc_now()))
        except DB_ERRORS as exc:
            raise StoreError("Notification inbox could not be created") from exc
        return {"pk": pk, "kind": "citizen"}

    def citizen_recipient(self, token_hash: str) -> dict | None:
        try:
            with closing(self._connect()) as conn:
                row = conn.execute("SELECT pk, kind FROM notification_recipients WHERE token_hash = ? AND kind = 'citizen'",
                                   (token_hash,)).fetchone()
        except DB_ERRORS as exc:
            raise StoreError("Notification inbox could not be read") from exc
        return dict(row) if row else None

    def follow_complaint(self, recipient_pk: int, complaint_pk: int, max_follows: int) -> str:
        """Returns "created", "exists" or "limit"."""
        try:
            with closing(self._connect()) as conn, conn.transaction(immediate=True):
                if conn.execute("SELECT 1 FROM notification_follows WHERE recipient_pk = ? AND complaint_pk = ?",
                                (recipient_pk, complaint_pk)).fetchone():
                    return "exists"
                # Lock the recipient so concurrent follows cannot pass the limit together.
                conn.execute("SELECT pk FROM notification_recipients WHERE pk = ?" + self.db.for_update, (recipient_pk,)).fetchone()
                if conn.execute("SELECT COUNT(*) FROM notification_follows WHERE recipient_pk = ?", (recipient_pk,)).fetchone()[0] >= max_follows:
                    return "limit"
                cur = conn.execute("INSERT INTO notification_follows (recipient_pk, complaint_pk, created_at) VALUES (?, ?, ?) "
                                   "ON CONFLICT (recipient_pk, complaint_pk) DO NOTHING", (recipient_pk, complaint_pk, utc_now()))
                return "created" if cur.rowcount == 1 else "exists"
        except DB_ERRORS as exc:
            raise StoreError("Follow could not be saved") from exc

    def unfollow_complaint(self, recipient_pk: int, public_id: str) -> bool:
        return self._write("DELETE FROM notification_follows WHERE recipient_pk = ? AND complaint_pk = "
                           "(SELECT pk FROM complaints WHERE public_id = ?)", (recipient_pk, public_id), "Unfollow") == 1

    def list_follows(self, recipient_pk: int) -> list[dict]:
        try:
            with closing(self._connect()) as conn:
                return [{"complaintId": r["public_id"], "followedAt": r["created_at"]} for r in conn.execute(
                    "SELECT c.public_id, f.created_at FROM notification_follows f JOIN complaints c ON c.pk = f.complaint_pk "
                    "WHERE f.recipient_pk = ? ORDER BY f.pk DESC", (recipient_pk,))]
        except DB_ERRORS as exc:
            raise StoreError("Follows could not be read") from exc

    def save_push_subscription(self, recipient_pk: int, sub: dict, max_devices: int) -> tuple[str, dict | None]:
        """Register a device, or refresh its keys if this recipient already registered the endpoint.
        Returns ("created" | "updated", row) or ("limit", None)."""
        now = utc_now()
        try:
            with closing(self._connect()) as conn, conn.transaction(immediate=True):
                conn.execute("SELECT pk FROM notification_recipients WHERE pk = ?" + self.db.for_update, (recipient_pk,)).fetchone()
                existing = conn.execute("SELECT subscription_id FROM push_subscriptions WHERE recipient_pk = ? AND endpoint = ?",
                                        (recipient_pk, sub["endpoint"])).fetchone()
                if existing:
                    conn.execute("UPDATE push_subscriptions SET p256dh = ?, auth = ?, label = ?, expires_at = ?, updated_at = ?, "
                                 "failure_count = 0 WHERE recipient_pk = ? AND endpoint = ?",
                                 (sub["p256dh"], sub["auth"], sub["label"], sub["expiresAt"], now, recipient_pk, sub["endpoint"]))
                    outcome, subscription_id = "updated", existing["subscription_id"]
                else:
                    if conn.execute("SELECT COUNT(*) FROM push_subscriptions WHERE recipient_pk = ?", (recipient_pk,)).fetchone()[0] >= max_devices:
                        return "limit", None
                    subscription_id = "ps_" + secrets.token_urlsafe(16)
                    conn.execute("INSERT INTO push_subscriptions (subscription_id, recipient_pk, endpoint, p256dh, auth, label, expires_at, "
                                 "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                 (subscription_id, recipient_pk, sub["endpoint"], sub["p256dh"], sub["auth"], sub["label"], sub["expiresAt"], now, now))
                    outcome = "created"
                row = conn.execute(f"SELECT {_DEVICE_COLUMNS} FROM push_subscriptions WHERE subscription_id = ?", (subscription_id,)).fetchone()
                return outcome, _device_dict(row)
        except DB_ERRORS as exc:
            raise StoreError("Push subscription could not be saved") from exc

    def list_push_subscriptions(self, recipient_pk: int) -> list[dict]:
        try:
            with closing(self._connect()) as conn:
                return [_device_dict(r) for r in conn.execute(
                    f"SELECT {_DEVICE_COLUMNS} FROM push_subscriptions WHERE recipient_pk = ? ORDER BY pk", (recipient_pk,))]
        except DB_ERRORS as exc:
            raise StoreError("Push subscriptions could not be read") from exc

    def delete_push_subscription(self, recipient_pk: int, subscription_id: str) -> bool:
        """Only the owning recipient can delete a device."""
        return self._write("DELETE FROM push_subscriptions WHERE recipient_pk = ? AND subscription_id = ?",
                           (recipient_pk, subscription_id), "Push subscription removal") == 1

    def prune_push_subscriptions(self, max_failures: int) -> int:
        """Delete devices whose subscription has expired or that failed `max_failures` times in a row."""
        return self._write("DELETE FROM push_subscriptions WHERE (expires_at IS NOT NULL AND expires_at <= ?) OR failure_count >= ?",
                           (utc_now(), max_failures), "Push subscription cleanup")

    def _staff_recipient_pks(self, conn) -> list[int]:
        """Every active staff account's recipient (created here for accounts that have none yet)."""
        now = utc_now()
        for (staff_pk,) in conn.execute("SELECT a.pk FROM staff_accounts a LEFT JOIN notification_recipients r ON r.staff_pk = a.pk "
                                        "WHERE a.disabled_at IS NULL AND r.pk IS NULL").fetchall():
            conn.execute("INSERT INTO notification_recipients (kind, staff_pk, created_at) VALUES ('staff', ?, ?) "
                         "ON CONFLICT (staff_pk) DO NOTHING", (staff_pk, now))
        return [r[0] for r in conn.execute("SELECT r.pk FROM notification_recipients r JOIN staff_accounts a ON a.pk = r.staff_pk "
                                          "WHERE a.disabled_at IS NULL ORDER BY r.pk")]

    def add_notifications(self, audience: str, event: dict) -> list[dict]:
        """Store one notification per recipient in `audience` ("followers" of event["complaintPk"],
        "staff" = every active staff account, or "recipient" = event["recipientPk"]). A recipient that
        already has event["dedupeKey"] is skipped. Returns the new rows, each with its devices."""
        now = utc_now()
        created = []
        try:
            with closing(self._connect()) as conn, conn.transaction():
                if audience == "followers":
                    targets = [r[0] for r in conn.execute("SELECT recipient_pk FROM notification_follows WHERE complaint_pk = ? ORDER BY pk",
                                                          (event["complaintPk"],))]
                elif audience == "staff":
                    targets = self._staff_recipient_pks(conn)
                else:
                    targets = [event["recipientPk"]]
                for recipient_pk in targets:
                    notification_id = "nt_" + secrets.token_urlsafe(16)
                    cur = conn.execute(
                        "INSERT INTO notifications (notification_id, recipient_pk, complaint_pk, kind, title, body, url, dedupe_key, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (recipient_pk, dedupe_key) DO NOTHING",
                        (notification_id, recipient_pk, event.get("complaintPk"), event["kind"], event["title"], event["body"],
                         event["url"], event["dedupeKey"], now))
                    if cur.rowcount != 1:
                        continue
                    devices = [dict(d) for d in conn.execute(
                        "SELECT pk, endpoint, p256dh, auth FROM push_subscriptions WHERE recipient_pk = ? ORDER BY pk", (recipient_pk,))]
                    created.append({"id": notification_id, "recipientPk": recipient_pk, "kind": event["kind"], "title": event["title"],
                                    "body": event["body"], "url": event["url"], "audience": event["audience"], "createdAt": now, "devices": devices})
        except DB_ERRORS as exc:
            raise StoreError("Notifications could not be saved") from exc
        return created

    def record_push_result(self, subscription_pk: int, outcome: str, max_failures: int) -> None:
        """outcome: "sent" resets the failure count; "gone" deletes the device (expired or unsubscribed);
        anything else counts a failure, and the device is deleted after `max_failures` in a row."""
        now = utc_now()
        if outcome == "sent":
            self._write("UPDATE push_subscriptions SET last_success_at = ?, failure_count = 0 WHERE pk = ?", (now, subscription_pk), "Push result")
        elif outcome == "gone":
            self._write("DELETE FROM push_subscriptions WHERE pk = ?", (subscription_pk,), "Push subscription removal")
        else:
            self._write("UPDATE push_subscriptions SET last_failure_at = ?, failure_count = failure_count + 1 WHERE pk = ?",
                        (now, subscription_pk), "Push result")
            self._write("DELETE FROM push_subscriptions WHERE pk = ? AND failure_count >= ?", (subscription_pk, max_failures),
                        "Push subscription removal")

    def set_push_status(self, notification_id: str, status: str) -> None:
        self._write("UPDATE notifications SET push_status = ? WHERE notification_id = ?", (status, notification_id), "Push status")

    def list_notifications(self, recipient_pk: int, limit: int = 50, before: str | None = None, unread_only: bool = False) -> dict:
        """Newest first. `before` is the id of the last notification already shown (cursor paging)."""
        limit = max(1, min(int(limit), NOTIFICATION_PAGE_MAX))
        try:
            with closing(self._connect()) as conn:
                where, params = ["n.recipient_pk = ?"], [recipient_pk]
                if before:
                    where.append("n.pk < (SELECT pk FROM notifications WHERE notification_id = ? AND recipient_pk = ?)")
                    params += [before, recipient_pk]
                if unread_only:
                    where.append("n.read_at IS NULL")
                rows = conn.execute(
                    f"SELECT {_NOTIFICATION_COLUMNS} FROM notifications n LEFT JOIN complaints c ON c.pk = n.complaint_pk "
                    f"WHERE {' AND '.join(where)} ORDER BY n.pk DESC LIMIT ?", (*params, limit + 1)).fetchall()
                unread = conn.execute("SELECT COUNT(*) FROM notifications WHERE recipient_pk = ? AND read_at IS NULL", (recipient_pk,)).fetchone()[0]
        except DB_ERRORS as exc:
            raise StoreError("Notifications could not be read") from exc
        items = [_notification_dict(r) for r in rows[:limit]]
        return {"items": items, "unreadCount": int(unread), "nextBefore": items[-1]["id"] if len(rows) > limit else None}

    def unread_count(self, recipient_pk: int) -> int:
        try:
            with closing(self._connect()) as conn:
                return int(conn.execute("SELECT COUNT(*) FROM notifications WHERE recipient_pk = ? AND read_at IS NULL",
                                        (recipient_pk,)).fetchone()[0])
        except DB_ERRORS as exc:
            raise StoreError("Notifications could not be read") from exc

    def mark_notification(self, recipient_pk: int, notification_id: str, read: bool) -> dict | None:
        """Mark one of the recipient's notifications read (keeping the first read time) or unread."""
        try:
            with closing(self._connect()) as conn:
                with conn.transaction():
                    sql = ("UPDATE notifications SET read_at = COALESCE(read_at, ?) WHERE recipient_pk = ? AND notification_id = ?" if read
                           else "UPDATE notifications SET read_at = NULL WHERE recipient_pk = ? AND notification_id = ?")
                    cur = conn.execute(sql, ((utc_now(),) if read else ()) + (recipient_pk, notification_id))
                if cur.rowcount != 1:
                    return None
                row = conn.execute(f"SELECT {_NOTIFICATION_COLUMNS} FROM notifications n LEFT JOIN complaints c ON c.pk = n.complaint_pk "
                                   "WHERE n.notification_id = ?", (notification_id,)).fetchone()
        except DB_ERRORS as exc:
            raise StoreError("Notification could not be updated") from exc
        return _notification_dict(row)

    def mark_all_notifications_read(self, recipient_pk: int) -> int:
        return self._write("UPDATE notifications SET read_at = ? WHERE recipient_pk = ? AND read_at IS NULL",
                           (utc_now(), recipient_pk), "Notifications update")

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
        merged_pk = row["merged_into_pk"] if "merged_into_pk" in row.keys() else None  # absent before schema 8
        merged_into = conn.execute("SELECT public_id FROM complaints WHERE pk = ?", (merged_pk,)).fetchone() if merged_pk is not None else None
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
            "mergedInto": merged_into["public_id"] if merged_into else None,
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

    # ---------- Complaint management (schema 7): departments, roles, lifecycle, work orders, audit ----------
    # Every mutation runs in one immediate transaction: the complaint row is locked (FOR UPDATE on
    # PostgreSQL; SQLite locks the database), the rules in lifecycle.py are checked against the locked
    # row and the actor's current role and departments, and the state change, its public timeline or
    # verification entry and its audit record are written together or not at all.

    def _actor(self, conn, staff_pk: int) -> dict:
        row = conn.execute("SELECT pk, username, display_name, role, disabled_at FROM staff_accounts WHERE pk = ?", (staff_pk,)).fetchone()
        if row is None or row["disabled_at"] is not None:
            raise RuleViolation(401, "unauthorized", "Sign in as CivicLens staff to continue.")
        departments = [r[0] for r in conn.execute("SELECT department_pk FROM staff_departments WHERE staff_pk = ?", (staff_pk,))]
        return {"pk": row["pk"], "username": row["username"], "displayName": row["display_name"], "role": row["role"], "departmentPks": departments}

    def _case(self, conn, public_id: str, lock: bool = False) -> dict | None:
        row = conn.execute(
            "SELECT pk, public_id, status, verification, department, department_pk, assigned_staff_pk, version, merged_into_pk FROM complaints "
            "WHERE public_id = ?" + (self.db.for_update if lock else ""), (public_id,)).fetchone()
        if row is None:
            return None
        return {"pk": row["pk"], "id": row["public_id"], "status": row["status"], "verification": row["verification"],
                "department": row["department"], "departmentPk": row["department_pk"], "assignedStaffPk": row["assigned_staff_pk"],
                "version": row["version"], "mergedIntoPk": row["merged_into_pk"]}

    def _locked_case(self, conn, public_id: str) -> dict:
        case = self._case(conn, public_id, lock=True)
        if case is None:
            raise RuleViolation(404, "not_found", "No complaint was found with that ID.")
        return case

    @staticmethod
    def _active_work_orders(conn, complaint_pk: int) -> int:
        return conn.execute("SELECT COUNT(*) FROM work_orders WHERE complaint_pk = ? AND status IN ('Open', 'In Progress')",
                            (complaint_pk,)).fetchone()[0]

    @staticmethod
    def _audit(conn, complaint_pk: int | None, actor: dict | None, action: str, details: dict, client_ip: str | None, now: str,
               actor_type: str | None = None) -> None:
        conn.execute(
            "INSERT INTO audit_log (audit_id, complaint_pk, actor_type, actor_staff_pk, action, details, client_ip, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("au_" + secrets.token_urlsafe(16), complaint_pk, actor_type or ("staff" if actor else "system"),
             actor["pk"] if actor else None, action, json.dumps(details, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
             client_ip, now))

    @staticmethod
    def _timeline(conn, complaint_pk: int, status: str, text: str, kind: str, actor: dict | None, now: str) -> None:
        conn.execute("INSERT INTO complaint_timeline (complaint_pk, status, text, date, kind, actor_staff_pk) VALUES (?, ?, ?, ?, ?, ?)",
                     (complaint_pk, status, text[:500], now, kind, actor["pk"] if actor else None))

    @staticmethod
    def _verification_entry(conn, complaint_pk: int, now: str, *, type_: str, from_: str | None, to: str | None,
                            response: str | None, by: str, actor_label: str, note: str = "") -> None:
        # Public: actor_label is generic ("Civic team", "Citizen"), never a staff or citizen name.
        conn.execute(
            "INSERT INTO verification_log (complaint_pk, entry_id, date, type, from_state, to_state, response, by_role, actor, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (complaint_pk, "vl_" + secrets.token_urlsafe(12), now, type_, from_, to, response, by, actor_label, note[:500]))

    @staticmethod
    def _touch(conn, complaint_pk: int, now: str, **fields) -> None:
        allowed = ("status", "verification", "department", "department_pk", "assigned_staff_pk", "merged_into_pk")
        sets = [f"{k} = ?" for k in fields if k in allowed]
        if len(sets) != len(fields):
            raise ValueError("unknown complaint column")
        conn.execute(f"UPDATE complaints SET {', '.join(sets + ['updated_at = ?', 'version = version + 1'])} WHERE pk = ?",
                     (*fields.values(), now, complaint_pk))

    def _mutate(self, what: str, fn):
        """Run fn(conn) in an immediate transaction. RuleViolation passes through; database errors become StoreError."""
        try:
            with closing(self._connect()) as conn:
                with conn.transaction(immediate=True):
                    return fn(conn)
        except RuleViolation:
            raise
        except DB_ERRORS as exc:
            raise StoreError(f"{what} failed") from exc

    def _change_status(self, conn, case: dict, actor: dict | None, to: str, text: str, now: str, client_ip: str | None,
                       reason: str = "") -> None:
        """Apply an already validated status change: status, public timeline, verification round, audit."""
        new_verification = lc.verification_after_status(case, to)
        fields = {"status": to} | ({"verification": new_verification} if new_verification else {})
        self._touch(conn, case["pk"], now, **fields)
        self._timeline(conn, case["pk"], to, text, "status_change", actor, now)
        details = {"from": case["status"], "to": to, "explanation": reason}
        if new_verification:
            self._verification_entry(conn, case["pk"], now, type_="state", from_=case["verification"], to=new_verification,
                                     response=None, by="staff", actor_label="Civic team",
                                     note="Marked resolved again; awaiting citizen verification.")
            details |= {"verificationFrom": case["verification"], "verificationTo": new_verification}
        self._audit(conn, case["pk"], actor, "status_changed", details, client_ip, now)
        case.update(status=to, verification=new_verification or case["verification"])

    # --- Departments and roles (admin CLI) ---

    def create_department(self, code: str, name: str) -> dict:
        now = utc_now()

        def run(conn):
            if conn.execute("SELECT 1 FROM departments WHERE code = ?", (code,)).fetchone():
                raise RuleViolation(409, "department_exists", f"A department with code {code!r} already exists.")
            conn.execute("INSERT INTO departments (code, name, active, created_at) VALUES (?, ?, 1, ?)", (code, name, now))
            self._audit(conn, None, None, "department_created", {"code": code, "name": name}, None, now)
            return {"code": code, "name": name, "active": True}
        return self._mutate("Department creation", run)

    def set_department_active(self, code: str, active: bool) -> bool:
        now = utc_now()

        def run(conn):
            changed = conn.execute("UPDATE departments SET active = ? WHERE code = ?", (1 if active else 0, code)).rowcount == 1
            if changed:
                self._audit(conn, None, None, "department_activated" if active else "department_deactivated", {"code": code}, None, now)
            return changed
        return self._mutate("Department update", run)

    def list_departments(self, include_inactive: bool = False) -> list[dict]:
        try:
            with closing(self._connect()) as conn:
                rows = conn.execute("SELECT code, name, active FROM departments" + ("" if include_inactive else " WHERE active = 1") +
                                    " ORDER BY name").fetchall()
        except DB_ERRORS as exc:
            raise StoreError("Departments could not be read") from exc
        return [{"code": r["code"], "name": r["name"], "active": bool(r["active"])} for r in rows]

    def set_staff_role(self, username: str, role: str) -> bool:
        if role not in lc.ROLES:
            raise RuleViolation(422, "invalid_role", f"Role must be one of {', '.join(lc.ROLES)}.")
        now = utc_now()

        def run(conn):
            row = conn.execute("SELECT pk, role FROM staff_accounts WHERE username = ?", (username,)).fetchone()
            if row is None:
                return False
            conn.execute("UPDATE staff_accounts SET role = ? WHERE pk = ?", (role, row["pk"]))
            self._audit(conn, None, None, "staff_role_changed", {"username": username, "from": row["role"], "to": role}, None, now)
            return True
        return self._mutate("Staff role update", run)

    def set_staff_department(self, username: str, code: str, member: bool) -> bool:
        """Add (member=True) or remove a staff member's department membership. False if either is unknown."""
        now = utc_now()

        def run(conn):
            staff = conn.execute("SELECT pk FROM staff_accounts WHERE username = ?", (username,)).fetchone()
            dept = conn.execute("SELECT pk FROM departments WHERE code = ?", (code,)).fetchone()
            if staff is None or dept is None:
                return False
            exists = conn.execute("SELECT 1 FROM staff_departments WHERE staff_pk = ? AND department_pk = ?",
                                  (staff["pk"], dept["pk"])).fetchone()
            if member and not exists:
                conn.execute("INSERT INTO staff_departments (staff_pk, department_pk, created_at) VALUES (?, ?, ?)", (staff["pk"], dept["pk"], now))
            elif not member and exists:
                conn.execute("DELETE FROM staff_departments WHERE staff_pk = ? AND department_pk = ?", (staff["pk"], dept["pk"]))
            else:
                return True
            self._audit(conn, None, None, "staff_department_added" if member else "staff_department_removed",
                        {"username": username, "department": code}, None, now)
            return True
        return self._mutate("Staff department update", run)

    def staff_departments(self, username: str) -> list[str]:
        try:
            with closing(self._connect()) as conn:
                return [r[0] for r in conn.execute(
                    "SELECT d.code FROM staff_departments sd JOIN departments d ON d.pk = sd.department_pk "
                    "JOIN staff_accounts a ON a.pk = sd.staff_pk WHERE a.username = ? ORDER BY d.code", (username,))]
        except DB_ERRORS as exc:
            raise StoreError("Staff departments could not be read") from exc

    # --- Staff case view ---

    def case_detail(self, public_id: str, staff_pk: int) -> dict | None:
        """The staff projection plus management data: assignment, work orders, internal notes and the
        actions this staff member may take now. Officials outside the case's scope get 403."""
        try:
            with closing(self._connect()) as conn:
                record = self._staff_record(conn, public_id)
                if record is None:
                    return None
                case = self._case(conn, public_id)
                actor = self._actor(conn, staff_pk)
                if not lc.in_scope(case, actor):
                    raise RuleViolation(403, "forbidden", "This complaint belongs to a department you are not a member of.")
                return record | self._management(conn, case, actor)
        except RuleViolation:
            raise
        except DB_ERRORS as exc:
            raise StoreError("Complaint could not be read") from exc

    def _management(self, conn, case: dict, actor: dict) -> dict:
        dept = conn.execute("SELECT code, name FROM departments WHERE pk = ?", (case["departmentPk"],)).fetchone() if case["departmentPk"] else None
        assignee = conn.execute("SELECT username, display_name FROM staff_accounts WHERE pk = ?",
                                (case["assignedStaffPk"],)).fetchone() if case["assignedStaffPk"] else None
        orders = self._work_orders(conn, "w.complaint_pk = ?", (case["pk"],))
        notes = conn.execute(
            "SELECT n.note_id, n.body, n.created_at, a.username, a.display_name FROM staff_notes n "
            "JOIN staff_accounts a ON a.pk = n.author_staff_pk WHERE n.complaint_pk = ? ORDER BY n.pk", (case["pk"],)).fetchall()
        history = conn.execute(
            "SELECT h.assignment_id, d.code, d.name, s.username AS assignee, h.reason, a.username AS actor, h.assigned_at "
            "FROM complaint_assignments h JOIN departments d ON d.pk = h.department_pk "
            "LEFT JOIN staff_accounts s ON s.pk = h.assignee_staff_pk JOIN staff_accounts a ON a.pk = h.actor_staff_pk "
            "WHERE h.complaint_pk = ? ORDER BY h.pk", (case["pk"],)).fetchall()
        active = sum(1 for o in orders if o["status"] in lc.ACTIVE_WORK_ORDER)
        merges = conn.execute(
            "SELECT m.merge_id, d.public_id AS duplicate, c.public_id AS canonical, m.reason, m.score, m.label, a.username AS actor, m.created_at, "
            "m.reverted_at, r.username AS reverted_by, m.revert_reason FROM complaint_merges m "
            "JOIN complaints d ON d.pk = m.duplicate_pk JOIN complaints c ON c.pk = m.canonical_pk "
            "JOIN staff_accounts a ON a.pk = m.actor_staff_pk LEFT JOIN staff_accounts r ON r.pk = m.reverted_by_staff_pk "
            "WHERE m.duplicate_pk = ? OR m.canonical_pk = ? ORDER BY m.pk", (case["pk"], case["pk"])).fetchall()
        return {
            "version": case["version"],
            "merges": [{"id": m["merge_id"], "duplicate": m["duplicate"], "canonical": m["canonical"], "reason": m["reason"],
                        "score": m["score"], "label": m["label"], "by": m["actor"], "at": m["created_at"], "revertedAt": m["reverted_at"],
                        "revertedBy": m["reverted_by"], "revertReason": m["revert_reason"]} for m in merges],
            "assignment": {"department": {"code": dept["code"], "name": dept["name"]} if dept else None,
                           "assignee": {"username": assignee["username"], "displayName": assignee["display_name"]} if assignee else None},
            "assignmentHistory": [{"id": h["assignment_id"], "department": {"code": h["code"], "name": h["name"]}, "assignee": h["assignee"],
                                   "reason": h["reason"], "by": h["actor"], "at": h["assigned_at"]} for h in history],
            "workOrders": orders,
            "notes": [{"id": n["note_id"], "body": n["body"], "createdAt": n["created_at"],
                       "author": {"username": n["username"], "displayName": n["display_name"]}} for n in notes],
            "allowedActions": lc.allowed_actions(case, actor, active),
        }

    def _work_orders(self, conn, where: str, params: tuple) -> list[dict]:
        rows = conn.execute(
            "SELECT w.*, c.public_id, d.code AS department_code, s.username AS assignee, cb.username AS created_by FROM work_orders w "
            "JOIN complaints c ON c.pk = w.complaint_pk LEFT JOIN departments d ON d.pk = w.department_pk "
            "LEFT JOIN staff_accounts s ON s.pk = w.assignee_staff_pk JOIN staff_accounts cb ON cb.pk = w.created_by_staff_pk "
            f"WHERE {where} ORDER BY w.pk", params).fetchall()
        today = _today_ist()
        result = []
        for r in rows:
            links = conn.execute(
                "SELECT l.phase, e.evidence_id, e.role, e.content_type, e.byte_size, e.sha256, e.uploaded_at FROM work_order_evidence l "
                "JOIN evidence e ON e.pk = l.evidence_pk WHERE l.work_order_pk = ? ORDER BY l.pk", (r["pk"],)).fetchall()
            events = conn.execute(
                "SELECT e.action, e.from_status, e.to_status, e.note, a.username, e.created_at FROM work_order_events e "
                "JOIN staff_accounts a ON a.pk = e.actor_staff_pk WHERE e.work_order_pk = ? ORDER BY e.pk", (r["pk"],)).fetchall()
            result.append({
                "id": r["work_order_id"], "complaintId": r["public_id"], "department": r["department_code"], "team": r["team"],
                "assignee": r["assignee"], "description": r["description"], "dueAt": r["due_at"], "status": r["status"],
                "startedAt": r["started_at"], "completedAt": r["completed_at"], "completionNote": r["completion_note"],
                "createdBy": r["created_by"], "createdAt": r["created_at"], "updatedAt": r["updated_at"],
                "overdue": bool(r["due_at"]) and r["status"] in lc.ACTIVE_WORK_ORDER and r["due_at"] < today,
                "evidence": [{"phase": e["phase"], "evidenceId": e["evidence_id"], "role": e["role"], "contentType": e["content_type"],
                              "byteSize": e["byte_size"], "sha256": e["sha256"], "uploadedAt": e["uploaded_at"]} for e in links],
                "history": [{"action": e["action"], "from": e["from_status"], "to": e["to_status"], "note": e["note"],
                             "by": e["username"], "at": e["created_at"]} for e in events],
            })
        return result

    # --- Lifecycle actions ---

    def transition(self, public_id: str, staff_pk: int, to: str, expected_status: str | None, explanation: str = "",
                   client_ip: str | None = None) -> None:
        def run(conn):
            case = self._locked_case(conn, public_id)
            actor = self._actor(conn, staff_pk)
            lc.require_expected(case["status"], expected_status)
            lc.check_status_transition(case, actor, to, explanation, self._active_work_orders(conn, case["pk"]))
            self._change_status(conn, case, actor, to, explanation.strip() or f"Status changed to {to}.", utc_now(), client_ip,
                                explanation.strip())
        self._mutate("Status change", run)

    def assign(self, public_id: str, staff_pk: int, department_code: str, assignee_username: str | None, reason: str,
               expected_status: str | None, client_ip: str | None = None) -> None:
        def run(conn):
            now = utc_now()
            case = self._locked_case(conn, public_id)
            actor = self._actor(conn, staff_pk)
            lc.require_expected(case["status"], expected_status)
            d = conn.execute("SELECT pk, code, name, active FROM departments WHERE code = ?", (department_code,)).fetchone()
            department = {"pk": d["pk"], "code": d["code"], "name": d["name"], "active": bool(d["active"])} if d else None
            assignee = None
            if assignee_username:
                a = conn.execute("SELECT pk, username, role, disabled_at FROM staff_accounts WHERE username = ?", (assignee_username,)).fetchone()
                if a is None:
                    raise RuleViolation(422, "invalid_assignee", "No staff account has that username.")
                assignee = {"pk": a["pk"], "username": a["username"], "role": a["role"], "disabledAt": a["disabled_at"],
                            "departmentPks": [r[0] for r in conn.execute("SELECT department_pk FROM staff_departments WHERE staff_pk = ?", (a["pk"],))]}
            lc.check_assignment(case, actor, department, assignee)
            reassign = case["departmentPk"] is not None or case["assignedStaffPk"] is not None
            if reassign and not reason.strip():
                raise RuleViolation(422, "reason_required", "Give a reason for reassigning this complaint.")
            if case["departmentPk"] == department["pk"] and case["assignedStaffPk"] == (assignee["pk"] if assignee else None):
                raise RuleViolation(409, "no_change", "The complaint is already assigned that way.")
            before = {"department": case["department"] if case["departmentPk"] else None,
                      "assignee": self._username(conn, case["assignedStaffPk"])}
            conn.execute(
                "INSERT INTO complaint_assignments (assignment_id, complaint_pk, department_pk, assignee_staff_pk, reason, actor_staff_pk, assigned_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("as_" + secrets.token_urlsafe(12), case["pk"], department["pk"], assignee["pk"] if assignee else None,
                 reason.strip(), actor["pk"], now))
            self._touch(conn, case["pk"], now, department=department["name"], department_pk=department["pk"],
                        assigned_staff_pk=assignee["pk"] if assignee else None)
            self._audit(conn, case["pk"], actor, "reassigned" if reassign else "assigned",
                        {"from": before, "to": {"department": department["code"], "assignee": assignee["username"] if assignee else None},
                         "reason": reason.strip()}, client_ip, now)
            case.update(departmentPk=department["pk"], department=department["name"])
            # Public text names the department only; individual staff are never named publicly. When the
            # assignment also moves the status, the status change carries the text (one public entry, not two).
            text = f"{'Reassigned' if reassign else 'Assigned'} to {department['name']}."
            if to := lc.status_after_assignment(case):
                self._change_status(conn, case, actor, to, text, now, client_ip)
            else:
                self._timeline(conn, case["pk"], case["status"], text, "assignment", actor, now)
        self._mutate("Assignment", run)

    @staticmethod
    def _username(conn, staff_pk: int | None) -> str | None:
        if staff_pk is None:
            return None
        row = conn.execute("SELECT username FROM staff_accounts WHERE pk = ?", (staff_pk,)).fetchone()
        return row["username"] if row else None

    def add_note(self, public_id: str, staff_pk: int, body: str, client_ip: str | None = None) -> str:
        def run(conn):
            now = utc_now()
            case = self._locked_case(conn, public_id)
            actor = self._actor(conn, staff_pk)
            lc.check_note(case, actor)
            note_id = "sn_" + secrets.token_urlsafe(12)
            conn.execute("INSERT INTO staff_notes (note_id, complaint_pk, author_staff_pk, body, created_at) VALUES (?, ?, ?, ?, ?)",
                         (note_id, case["pk"], actor["pk"], body, now))
            # The note text stays in staff_notes (append-only); the audit record references it.
            self._audit(conn, case["pk"], actor, "note_added", {"noteId": note_id, "length": len(body)}, client_ip, now)
            return note_id
        return self._mutate("Internal note", run)

    def add_public_update(self, public_id: str, staff_pk: int, text: str, kind: str, client_ip: str | None = None) -> None:
        def run(conn):
            now = utc_now()
            case = self._locked_case(conn, public_id)
            actor = self._actor(conn, staff_pk)
            lc.check_note(case, actor)
            self._timeline(conn, case["pk"], case["status"], text, kind, actor, now)
            self._touch(conn, case["pk"], now)
            self._audit(conn, case["pk"], actor, "public_update", {"kind": kind, "text": text}, client_ip, now)
        self._mutate("Public update", run)

    def request_reinspection(self, public_id: str, staff_pk: int, note: str, reopen: bool, expected_status: str | None,
                             client_ip: str | None = None) -> None:
        def run(conn):
            now = utc_now()
            case = self._locked_case(conn, public_id)
            actor = self._actor(conn, staff_pk)
            lc.require_expected(case["status"], expected_status)
            lc.check_reinspection(case, actor, note)
            if reopen:  # validated before anything is written
                lc.check_status_transition(case, actor, "In Progress", note, self._active_work_orders(conn, case["pk"]))
            self._touch(conn, case["pk"], now, verification="Reinspection Required")
            self._verification_entry(conn, case["pk"], now, type_="state", from_=case["verification"], to="Reinspection Required",
                                     response=None, by="staff", actor_label="Civic team", note=note.strip())
            self._timeline(conn, case["pk"], case["status"], "Reinspection requested. Earlier repair and dispute records are retained.",
                           "verification", actor, now)
            self._audit(conn, case["pk"], actor, "reinspection_requested",
                        {"from": case["verification"], "to": "Reinspection Required", "note": note.strip(), "reopen": reopen}, client_ip, now)
            case["verification"] = "Reinspection Required"
            if reopen:
                self._change_status(conn, case, actor, "In Progress", note.strip(), now, client_ip, note.strip())
        self._mutate("Reinspection request", run)

    def citizen_response(self, public_id: str, response: str, client_ip: str | None = None) -> dict:
        """Record a citizen response (the caller has already verified the claim code). Returns
        {"verification", "changed"}. Never changes the operational status."""
        def run(conn):
            now = utc_now()
            case = self._locked_case(conn, public_id)
            new = lc.citizen_verification(case, response)
            self._verification_entry(conn, case["pk"], now, type_="response", from_=case["verification"] if new else None,
                                     to=new, response=response, by="citizen", actor_label="Citizen")
            if new:
                self._touch(conn, case["pk"], now, verification=new)
                if new == "Disputed":
                    self._timeline(conn, case["pk"], case["status"],
                                   "A citizen reports the issue still appears present after the marked repair.", "verification", None, now)
            self._audit(conn, case["pk"], None, "citizen_response", {"response": response, "from": case["verification"], "to": new},
                        client_ip, now, actor_type="citizen")
            return {"verification": new or case["verification"], "changed": new is not None}
        return self._mutate("Citizen response", run)

    # --- Work orders ---

    def create_work_order(self, public_id: str, staff_pk: int, team: str, description: str, due_at: str | None,
                          assignee_username: str | None, client_ip: str | None = None) -> str:
        def run(conn):
            now = utc_now()
            case = self._locked_case(conn, public_id)
            actor = self._actor(conn, staff_pk)
            lc.check_work_order_create(case, actor)
            assignee_pk = None
            if assignee_username:
                a = conn.execute("SELECT pk, disabled_at FROM staff_accounts WHERE username = ?", (assignee_username,)).fetchone()
                if a is None or a["disabled_at"] is not None:
                    raise RuleViolation(422, "invalid_assignee", "Choose an active staff account.")
                assignee_pk = a["pk"]
            order_id = "wo_" + secrets.token_urlsafe(12)
            order_pk = conn.insert(
                "INSERT INTO work_orders (work_order_id, complaint_pk, department_pk, team, assignee_staff_pk, description, due_at, status, "
                "created_by_staff_pk, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'Open', ?, ?, ?)",
                (order_id, case["pk"], case["departmentPk"], team, assignee_pk, description, due_at, actor["pk"], now, now))
            conn.execute("INSERT INTO work_order_events (work_order_pk, action, from_status, to_status, note, actor_staff_pk, created_at) "
                         "VALUES (?, 'created', NULL, 'Open', ?, ?, ?)", (order_pk, description[:500], actor["pk"], now))
            original = conn.execute("SELECT pk FROM evidence WHERE complaint_pk = ? AND role = 'original'", (case["pk"],)).fetchone()
            if original:  # the reported evidence is the work order's "before" reference
                conn.execute("INSERT INTO work_order_evidence (work_order_pk, evidence_pk, phase, linked_by_staff_pk, linked_at) "
                             "VALUES (?, ?, 'before', ?, ?)", (order_pk, original["pk"], actor["pk"], now))
            self._timeline(conn, case["pk"], case["status"], f"Work order created for {team}" + (f"; target date {due_at}." if due_at else "."),
                           "work_order", actor, now)
            self._touch(conn, case["pk"], now)
            self._audit(conn, case["pk"], actor, "work_order_created",
                        {"workOrderId": order_id, "team": team, "dueAt": due_at, "assignee": assignee_username}, client_ip, now)
            return order_id
        return self._mutate("Work order creation", run)

    def _locked_order(self, conn, work_order_id: str) -> tuple[dict, dict]:
        ref = conn.execute("SELECT c.public_id FROM work_orders w JOIN complaints c ON c.pk = w.complaint_pk WHERE w.work_order_id = ?",
                           (work_order_id,)).fetchone()
        if ref is None:
            raise RuleViolation(404, "not_found", "No work order was found with that ID.")
        case = self._locked_case(conn, ref["public_id"])  # always lock the complaint first, then its work order
        row = conn.execute("SELECT pk, status, team FROM work_orders WHERE work_order_id = ?" + self.db.for_update, (work_order_id,)).fetchone()
        return case, {"pk": row["pk"], "id": work_order_id, "status": row["status"], "team": row["team"]}

    def update_work_order(self, work_order_id: str, staff_pk: int, to: str, expected_status: str | None, note: str = "",
                          client_ip: str | None = None) -> str:
        """Start, complete or cancel a work order. Returns the complaint's public ID."""
        def run(conn):
            now = utc_now()
            case, order = self._locked_order(conn, work_order_id)
            actor = self._actor(conn, staff_pk)
            lc.require_expected(order["status"], expected_status, "work-order status")
            lc.check_work_order_change(case, order, actor, to, note)
            extra = {"In Progress": ", started_at = ?", "Completed": ", completed_at = ?, completion_note = ?",
                     "Cancelled": ", completion_note = ?"}[to]
            params = {"In Progress": (now,), "Completed": (now, note.strip()), "Cancelled": (note.strip(),)}[to]
            conn.execute(f"UPDATE work_orders SET status = ?, updated_at = ?, version = version + 1{extra} WHERE pk = ?",
                         (to, now, *params, order["pk"]))
            action = {"In Progress": "started", "Completed": "completed", "Cancelled": "cancelled"}[to]
            conn.execute("INSERT INTO work_order_events (work_order_pk, action, from_status, to_status, note, actor_staff_pk, created_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", (order["pk"], action, order["status"], to, note.strip()[:1000], actor["pk"], now))
            public = {"started": f"Work started by {order['team']}.", "completed": f"Work completed by {order['team']}.",
                      "cancelled": "A work order was cancelled."}[action]
            self._audit(conn, case["pk"], actor, "work_order_" + action,
                        {"workOrderId": work_order_id, "from": order["status"], "to": to, "note": note.strip()}, client_ip, now)
            if to == "In Progress" and (status := lc.status_after_work_started(case)):
                self._change_status(conn, case, actor, status, public, now, client_ip)  # one public entry for both
            else:
                self._timeline(conn, case["pk"], case["status"], public, "work_order", actor, now)
                self._touch(conn, case["pk"], now)
            return case["id"]
        return self._mutate("Work order update", run)

    def work_order_context(self, work_order_id: str, staff_pk: int) -> dict:
        """Authorise an upload before its body is read: {"complaintPk", "complaintId", "workOrderPk"} or RuleViolation."""
        try:
            with closing(self._connect()) as conn:
                row = conn.execute("SELECT w.pk, w.status, c.public_id FROM work_orders w JOIN complaints c ON c.pk = w.complaint_pk "
                                   "WHERE w.work_order_id = ?", (work_order_id,)).fetchone()
                if row is None:
                    raise RuleViolation(404, "not_found", "No work order was found with that ID.")
                case = self._case(conn, row["public_id"])
                lc.require_scope(case, self._actor(conn, staff_pk))
                if row["status"] == "Cancelled":
                    raise RuleViolation(409, "invalid_transition", "Evidence cannot be added to a cancelled work order.")
                return {"complaintPk": case["pk"], "complaintId": case["id"], "workOrderPk": row["pk"]}
        except RuleViolation:
            raise
        except DB_ERRORS as exc:
            raise StoreError("Work order could not be read") from exc

    def add_repair_evidence(self, work_order_id: str, staff_pk: int, meta: dict, client_ip: str | None = None) -> dict:
        """Store after-repair evidence metadata (the file is already committed to storage) and link it
        to the work order as 'after'. Re-checks authorisation inside the transaction."""
        def run(conn):
            now = utc_now()
            case, order = self._locked_order(conn, work_order_id)
            actor = self._actor(conn, staff_pk)
            lc.require_scope(case, actor)
            if order["status"] == "Cancelled":
                raise RuleViolation(409, "invalid_transition", "Evidence cannot be added to a cancelled work order.")
            evidence_pk = conn.insert(
                "INSERT INTO evidence (evidence_id, complaint_pk, role, storage_key, media_type, content_type, byte_size, sha256, captured_at, "
                "uploaded_at, storage_backend, uploaded_by_staff_pk) VALUES (?, ?, 'repair', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (meta["evidenceId"], case["pk"], meta["storageKey"], meta["mediaType"], meta["contentType"], meta["byteSize"],
                 meta["sha256"], meta.get("capturedAt"), meta["uploadedAt"], meta.get("storageBackend", "local"), actor["pk"]))
            conn.execute("INSERT INTO work_order_evidence (work_order_pk, evidence_pk, phase, linked_by_staff_pk, linked_at) "
                         "VALUES (?, ?, 'after', ?, ?)", (order["pk"], evidence_pk, actor["pk"], now))
            self._timeline(conn, case["pk"], case["status"], "After-repair evidence was attached for review.", "work_order", actor, now)
            self._touch(conn, case["pk"], now)
            self._audit(conn, case["pk"], actor, "repair_evidence_added",
                        {"workOrderId": work_order_id, "evidenceId": meta["evidenceId"], "sha256": meta["sha256"]}, client_ip, now)
            return _evidence_dict(conn.execute("SELECT * FROM evidence WHERE pk = ?", (evidence_pk,)).fetchone())
        return self._mutate("Repair evidence", run)

    def work_order_evidence(self, work_order_id: str, evidence_id: str, staff_pk: int) -> dict | None:
        """Metadata (with pk and storage key, for streaming) of one file linked to a work order the staff member may see."""
        try:
            with closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT e.*, c.public_id FROM work_order_evidence l JOIN work_orders w ON w.pk = l.work_order_pk "
                    "JOIN evidence e ON e.pk = l.evidence_pk JOIN complaints c ON c.pk = w.complaint_pk "
                    "WHERE w.work_order_id = ? AND e.evidence_id = ?", (work_order_id, evidence_id)).fetchone()
                if row is None:
                    return None
                lc.require_scope(self._case(conn, row["public_id"]), self._actor(conn, staff_pk))
                return _evidence_dict(row) | {"pk": row["pk"]}
        except RuleViolation:
            raise
        except DB_ERRORS as exc:
            raise StoreError("Evidence could not be read") from exc

    def list_work_orders(self, staff_pk: int, status: str | None = None, overdue: bool = False, department: str | None = None,
                         limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
        """Work orders the staff member may see (officials: their departments and their own), newest first."""
        try:
            with closing(self._connect()) as conn:
                actor = self._actor(conn, staff_pk)
                where, params = ["1 = 1"], []
                if not lc.has_role(actor, "supervisor"):
                    marks = ", ".join("?" * len(actor["departmentPks"])) or "NULL"
                    where.append(f"(w.department_pk IN ({marks}) OR w.assignee_staff_pk = ? OR c.assigned_staff_pk = ?)")
                    params += [*actor["departmentPks"], actor["pk"], actor["pk"]]
                if status:
                    where.append("w.status = ?")
                    params.append(status)
                if overdue:
                    where.append("w.status IN ('Open', 'In Progress') AND w.due_at IS NOT NULL AND w.due_at < ?")
                    params.append(_today_ist())
                if department:
                    where.append("w.department_pk = (SELECT pk FROM departments WHERE code = ?)")
                    params.append(department)
                clause = " AND ".join(where)
                base = f"FROM work_orders w JOIN complaints c ON c.pk = w.complaint_pk WHERE {clause}"
                total = conn.execute(f"SELECT COUNT(*) {base}", tuple(params)).fetchone()[0]
                ids = [r[0] for r in conn.execute(f"SELECT w.pk {base} ORDER BY w.pk DESC LIMIT ? OFFSET ?",
                                                  (*params, max(1, min(int(limit), STAFF_LIST_MAX)), max(0, int(offset))))]
                if not ids:
                    return [], total
                orders = self._work_orders(conn, f"w.pk IN ({', '.join('?' * len(ids))})", tuple(ids))
                return list(reversed(orders)), total
        except RuleViolation:
            raise
        except DB_ERRORS as exc:
            raise StoreError("Work orders could not be listed") from exc

    # --- Audit trail ---

    def audit_trail(self, public_id: str, staff_pk: int, limit: int = 200) -> list[dict] | None:
        """Newest first. Supervisor+ only (checked here as well as in the route)."""
        try:
            with closing(self._connect()) as conn:
                actor = self._actor(conn, staff_pk)
                lc.require_role(actor, "supervisor", "read the audit trail")
                case = self._case(conn, public_id)
                if case is None:
                    return None
                rows = conn.execute(
                    "SELECT l.audit_id, l.actor_type, a.username, a.display_name, l.action, l.details, l.created_at FROM audit_log l "
                    "LEFT JOIN staff_accounts a ON a.pk = l.actor_staff_pk WHERE l.complaint_pk = ? ORDER BY l.pk DESC LIMIT ?",
                    (case["pk"], max(1, min(int(limit), 500)))).fetchall()
        except RuleViolation:
            raise
        except DB_ERRORS as exc:
            raise StoreError("Audit trail could not be read") from exc
        return [{"id": r["audit_id"], "actorType": r["actor_type"], "actor": r["username"], "actorName": r["display_name"],
                 "action": r["action"], "details": json.loads(r["details"]), "at": r["created_at"]} for r in rows]

    # --- Duplicate detection (schema 8) ---

    @staticmethod
    def _report(row) -> dict:
        return {"pk": row["pk"], "id": row["public_id"], "category": row["category"], "area": row["area"], "created": row["created_at"],
                "status": row["status"], "verification": row["verification"], "description": row["description"],
                "lat": row["gps_lat"], "lng": row["gps_lng"], "accuracy": row["gps_accuracy"], "sourceType": row["gps_source_type"],
                "sha256": row["sha256"], "departmentPk": row["department_pk"], "assignedStaffPk": row["assigned_staff_pk"],
                "mergedIntoPk": row["merged_into_pk"]}

    _REPORT_COLUMNS = ("c.pk, c.public_id, c.category, c.area, c.created_at, c.status, c.verification, c.description, c.gps_lat, c.gps_lng, "
                       "c.gps_accuracy, c.gps_source_type, c.department_pk, c.assigned_staff_pk, c.merged_into_pk, "
                       "(SELECT e.sha256 FROM evidence e WHERE e.complaint_pk = c.pk AND e.role = 'original') AS sha256")

    def _report_by_pk(self, conn, pk: int) -> dict:
        return self._report(conn.execute(f"SELECT {self._REPORT_COLUMNS} FROM complaints c WHERE c.pk = ?", (pk,)).fetchone())

    def duplicate_candidates(self, public_id: str, staff_pk: int, radius_m: float = dup.DEFAULT_RADIUS_M,
                             days: int = dup.DEFAULT_WINDOW_DAYS) -> dict | None:
        """Ranked, explained duplicate suggestions for one complaint, or None if it does not exist.
        Candidates: same or related category, within radius_m and ±days, not merged away, not dismissed
        as a pair, and (for officials) within the caller's scope."""
        radius_m = max(10.0, min(float(radius_m), dup.MAX_RADIUS_M))
        days = max(1, min(int(days), dup.MAX_WINDOW_DAYS))
        try:
            with closing(self._connect()) as conn:
                case = self._case(conn, public_id)
                if case is None:
                    return None
                actor = self._actor(conn, staff_pk)
                lc.require_scope(case, actor)
                target = self._report_by_pk(conn, case["pk"])
                result = {"complaintId": public_id, "radiusM": radius_m, "days": days, "candidates": [], "note": None}
                if target["mergedIntoPk"] is not None:
                    result["note"] = "This complaint is already merged into another, so no duplicates are suggested for it."
                    return result
                created = datetime.fromisoformat(target["created"].replace("Z", "+00:00"))
                min_lat, max_lat, min_lng, max_lng = geo.bounding_box(target["lat"], target["lng"], radius_m)
                categories = sorted(dup.comparable_categories(target["category"]))
                rows = conn.execute(
                    f"SELECT {self._REPORT_COLUMNS} FROM complaints c WHERE c.pk <> ? AND c.merged_into_pk IS NULL "
                    "AND c.gps_lat BETWEEN ? AND ? AND c.gps_lng BETWEEN ? AND ? "
                    f"AND c.category IN ({', '.join('?' * len(categories))}) AND c.created_at BETWEEN ? AND ? "
                    "AND NOT EXISTS (SELECT 1 FROM duplicate_dismissals d WHERE (d.complaint_a_pk = c.pk AND d.complaint_b_pk = ?) "
                    "OR (d.complaint_a_pk = ? AND d.complaint_b_pk = c.pk)) ORDER BY c.pk DESC LIMIT 500",
                    (case["pk"], min_lat, max_lat, min_lng, max_lng, *categories, iso(created - timedelta(days=days)),
                     iso(created + timedelta(days=days)), case["pk"], case["pk"])).fetchall()
        except RuleViolation:
            raise
        except DB_ERRORS as exc:
            raise StoreError("Duplicate candidates could not be read") from exc
        candidates = [self._report(r) for r in rows]
        candidates = [c for c in candidates if geo.haversine_m(target["lat"], target["lng"], c["lat"], c["lng"]) <= radius_m
                      and lc.in_scope(c, actor)]
        public = ("id", "category", "area", "created", "status", "verification", "score", "label", "distanceM", "factors", "reasons", "caution")
        result["candidates"] = [{k: c[k] for k in public} for c in dup.rank(target, candidates)]
        return result

    def merge_complaints(self, duplicate_id: str, canonical_id: str, staff_pk: int, reason: str, client_ip: str | None = None) -> str:
        """Merge `duplicate_id` into `canonical_id` (supervisor+). Both complaints keep their records,
        evidence and history; the duplicate points at the canonical one. Returns the merge ID."""
        def run(conn):
            now = utc_now()
            refs = {r["public_id"]: r["pk"] for r in conn.execute(
                "SELECT pk, public_id FROM complaints WHERE public_id IN (?, ?)", (duplicate_id, canonical_id))}
            if duplicate_id not in refs or canonical_id not in refs:
                raise RuleViolation(404, "not_found", "No complaint was found with that ID.")
            locked = {i: self._locked_case(conn, i) for i in sorted({duplicate_id, canonical_id}, key=refs.get)}  # lock in pk order
            duplicate, canonical = locked[duplicate_id], locked[canonical_id]
            actor = self._actor(conn, staff_pk)
            has_duplicates = conn.execute("SELECT 1 FROM complaint_merges WHERE canonical_pk = ? AND reverted_at IS NULL",
                                          (duplicate["pk"],)).fetchone() is not None
            lc.check_merge(duplicate, canonical, actor, has_duplicates, reason)
            snapshot = dup.score_pair(self._report_by_pk(conn, canonical["pk"]), self._report_by_pk(conn, duplicate["pk"]))
            merge_id = "mg_" + secrets.token_urlsafe(12)
            conn.execute(
                "INSERT INTO complaint_merges (merge_id, duplicate_pk, canonical_pk, reason, score, label, factors, actor_staff_pk, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (merge_id, duplicate["pk"], canonical["pk"], reason.strip(), snapshot["score"], snapshot["label"],
                 json.dumps(snapshot["factors"], separators=(",", ":")), actor["pk"], now))
            self._touch(conn, duplicate["pk"], now, merged_into_pk=canonical["pk"])
            self._touch(conn, canonical["pk"], now)
            self._timeline(conn, duplicate["pk"], duplicate["status"],
                           f"Merged into {canonical_id} as a report of the same issue. Follow {canonical_id} for updates.", "progress", actor, now)
            self._timeline(conn, canonical["pk"], canonical["status"], f"Another report of this issue ({duplicate_id}) was linked to this complaint.",
                           "progress", actor, now)
            details = {"mergeId": merge_id, "duplicate": duplicate_id, "canonical": canonical_id, "reason": reason.strip(),
                       "score": snapshot["score"], "label": snapshot["label"]}
            self._audit(conn, duplicate["pk"], actor, "merged_as_duplicate", details, client_ip, now)
            self._audit(conn, canonical["pk"], actor, "duplicate_merged_in", details, client_ip, now)
            return merge_id
        return self._mutate("Merge", run)

    def revert_merge(self, merge_id: str, staff_pk: int, reason: str, client_ip: str | None = None) -> str:
        """Undo a merge (supervisor+). The merge record stays, marked reverted. Returns the duplicate's public ID."""
        def run(conn):
            now = utc_now()
            m = conn.execute(
                "SELECT m.pk, m.reverted_at, d.public_id AS duplicate, c.public_id AS canonical, d.pk AS dpk, c.pk AS cpk "
                "FROM complaint_merges m JOIN complaints d ON d.pk = m.duplicate_pk JOIN complaints c ON c.pk = m.canonical_pk "
                "WHERE m.merge_id = ?", (merge_id,)).fetchone()
            if m is None:
                raise RuleViolation(404, "not_found", "No merge was found with that ID.")
            ids = sorted([(m["dpk"], m["duplicate"]), (m["cpk"], m["canonical"])])
            locked = {i: self._locked_case(conn, i) for _, i in ids}
            actor = self._actor(conn, staff_pk)
            lc.check_merge_revert(actor, reason)
            if conn.execute("SELECT reverted_at FROM complaint_merges WHERE pk = ?", (m["pk"],)).fetchone()[0] is not None:
                raise RuleViolation(409, "already_reverted", "This merge was already reverted.")
            conn.execute("UPDATE complaint_merges SET reverted_at = ?, reverted_by_staff_pk = ?, revert_reason = ? WHERE pk = ?",
                         (now, actor["pk"], reason.strip(), m["pk"]))
            duplicate, canonical = locked[m["duplicate"]], locked[m["canonical"]]
            self._touch(conn, duplicate["pk"], now, merged_into_pk=None)
            self._touch(conn, canonical["pk"], now)
            self._timeline(conn, duplicate["pk"], duplicate["status"], f"No longer merged into {m['canonical']}; this report is handled on its own again.",
                           "progress", actor, now)
            self._timeline(conn, canonical["pk"], canonical["status"], f"Report {m['duplicate']} is no longer linked to this complaint.",
                           "progress", actor, now)
            details = {"mergeId": merge_id, "duplicate": m["duplicate"], "canonical": m["canonical"], "reason": reason.strip()}
            self._audit(conn, duplicate["pk"], actor, "merge_reverted", details, client_ip, now)
            self._audit(conn, canonical["pk"], actor, "merge_reverted", details, client_ip, now)
            return m["duplicate"]
        return self._mutate("Merge revert", run)

    def dismiss_duplicate(self, public_id: str, other_id: str, staff_pk: int, reason: str, client_ip: str | None = None) -> None:
        """Record that two complaints are not duplicates (supervisor+), so the pair is no longer suggested."""
        def run(conn):
            now = utc_now()
            a, b = self._case(conn, public_id), self._case(conn, other_id)
            if a is None or b is None:
                raise RuleViolation(404, "not_found", "No complaint was found with that ID.")
            actor = self._actor(conn, staff_pk)
            lc.require_role(actor, "supervisor", "dismiss duplicate suggestions")
            if a["pk"] == b["pk"]:
                raise RuleViolation(422, "invalid_dismissal", "A complaint cannot be compared with itself.")
            low, high = sorted((a["pk"], b["pk"]))
            if conn.execute("SELECT 1 FROM duplicate_dismissals WHERE complaint_a_pk = ? AND complaint_b_pk = ?", (low, high)).fetchone():
                raise RuleViolation(409, "already_dismissed", "These complaints were already marked as not duplicates.")
            conn.execute("INSERT INTO duplicate_dismissals (complaint_a_pk, complaint_b_pk, reason, actor_staff_pk, created_at) VALUES (?, ?, ?, ?, ?)",
                         (low, high, reason.strip(), actor["pk"], now))
            for case, other in ((a, other_id), (b, public_id)):
                self._audit(conn, case["pk"], actor, "duplicate_dismissed", {"other": other, "reason": reason.strip()}, client_ip, now)
        self._mutate("Duplicate dismissal", run)


def _today_ist() -> str:
    """Today's date in India (UTC+05:30, no daylight saving) as YYYY-MM-DD: work-order due dates are local dates."""
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date().isoformat()
