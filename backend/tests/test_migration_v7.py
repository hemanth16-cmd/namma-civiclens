"""Schema 7 (complaint management): an existing version-6 database is upgraded in place on SQLite
and PostgreSQL, every row survives (including rows in tables that were rebuilt), the new constraints
hold, and the history tables refuse updates and deletes."""

import os
import sqlite3
from contextlib import closing

import pytest

from app.db import PostgresDatabase, SQLiteDatabase
from app.store import MIGRATIONS, POSTGRES_BASE_VERSION, POSTGRES_MIGRATIONS, POSTGRES_SCHEMA, SCHEMA, SCHEMA_VERSION, ComplaintStore, utc_now
from tests.test_complaints import payload

PG_URL = os.environ.get("CIVICLENS_TEST_POSTGRES_URL", "")


def _populate_v6(store: ComplaintStore) -> dict:
    record, _ = store.create(payload())
    store.create_staff("asha", "Asha", "scrypt$hash")
    staff = store.get_staff("asha")
    store.create_staff_session(staff["pk"], "a" * 64, "2999-01-01T00:00:00.000Z")
    ref = store.complaint_ref(record["id"])
    meta = {"evidenceId": "ev_old", "storageKey": "k" * 32, "mediaType": "photo", "contentType": "image/jpeg",
            "byteSize": 10, "sha256": "0" * 64, "capturedAt": None, "uploadedAt": utc_now(), "storageBackend": "local"}
    _, evidence = store.add_evidence(ref["pk"], meta)
    with closing(store._connect()) as conn:
        ev_pk = conn.execute("SELECT pk FROM evidence WHERE evidence_id = 'ev_old'").fetchone()[0]
    store.log_evidence_access(ev_pk, staff["pk"], "10.0.0.1")
    return {"id": record["id"], "complaint_pk": ref["pk"], "staff_pk": staff["pk"], "evidence_pk": ev_pk}


def _check_upgraded(store: ComplaintStore, raw_execute, refs: dict, integrity_errors: tuple, append_only_errors: tuple):
    assert store.get(refs["id"])["status"] == "Submitted"
    assert store.get_staff("asha")["role"] == "official", "existing accounts keep their role"
    assert store.get_staff_session("a" * 64) is not None, "rebuilding staff_accounts must not cascade to sessions"
    assert len(store.list_evidence_access(refs["evidence_pk"])) == 1, "access log survives the evidence rebuild"
    assert store.get_evidence(refs["complaint_pk"])["evidenceId"] == "ev_old"
    row = raw_execute("SELECT department_pk, assigned_staff_pk, version FROM complaints WHERE pk = ?", (refs["complaint_pk"],))[0]
    assert tuple(row) == (None, None, 1)
    assert raw_execute("SELECT kind FROM complaint_timeline WHERE complaint_pk = ?", (refs["complaint_pk"],))[0][0] == "progress"

    # New role values and a repair photo are accepted; a second original is not.
    raw_execute("UPDATE staff_accounts SET role = 'supervisor' WHERE pk = ?", (refs["staff_pk"],), write=True)
    raw_execute("INSERT INTO evidence (evidence_id, complaint_pk, role, storage_key, media_type, content_type, byte_size, sha256, "
                "uploaded_at, storage_backend) VALUES ('ev_rep', ?, 'repair', ?, 'photo', 'image/jpeg', 5, ?, 'z', 'local')",
                (refs["complaint_pk"], "r" * 32, "1" * 64), write=True)
    with pytest.raises(integrity_errors):
        raw_execute("INSERT INTO evidence (evidence_id, complaint_pk, role, storage_key, media_type, content_type, byte_size, sha256, "
                    "uploaded_at, storage_backend) VALUES ('ev_2nd', ?, 'original', ?, 'photo', 'image/jpeg', 5, ?, 'z', 'local')",
                    (refs["complaint_pk"], "s" * 32, "2" * 64), write=True)
    with pytest.raises(integrity_errors):
        raw_execute("UPDATE staff_accounts SET role = 'mayor' WHERE pk = ?", (refs["staff_pk"],), write=True)

    raw_execute("INSERT INTO audit_log (audit_id, complaint_pk, actor_type, actor_staff_pk, action, created_at) "
                "VALUES ('au_1', ?, 'staff', ?, 'test', 'z')", (refs["complaint_pk"], refs["staff_pk"]), write=True)
    for sql in ("UPDATE audit_log SET action = 'changed'", "DELETE FROM audit_log"):
        with pytest.raises(append_only_errors):
            raw_execute(sql, (), write=True)
    assert raw_execute("SELECT action FROM audit_log", ())[0][0] == "test"


@pytest.mark.sqlite_only
def test_sqlite_version_6_is_upgraded_in_place(tmp_path):
    path = tmp_path / "v6.sqlite3"
    SQLiteDatabase(path).apply_schema(SCHEMA, {v: sql for v, sql in MIGRATIONS.items() if v <= 6})
    assert SQLiteDatabase(path).schema_version() == 6
    refs = _populate_v6(ComplaintStore(path))

    store = ComplaintStore(path)
    store.initialize()
    store.initialize()  # idempotent
    assert SQLiteDatabase(path).schema_version() == SCHEMA_VERSION

    def raw(sql, params, write=False):
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            rows = conn.execute(sql, params).fetchall()
            conn.commit()
            return rows

    _check_upgraded(store, raw, refs, (sqlite3.IntegrityError,), (sqlite3.IntegrityError,))
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # Foreign keys still point at the rebuilt tables by name.
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO staff_sessions (token_hash, staff_pk, created_at, expires_at) VALUES (?, 999, 'z', 'z')", ("b" * 64,))


@pytest.mark.sqlite_only
def test_failed_sqlite_migration_rolls_back(tmp_path):
    path = tmp_path / "v6.sqlite3"
    SQLiteDatabase(path).apply_schema(SCHEMA, {v: sql for v, sql in MIGRATIONS.items() if v <= 6})
    broken = {**MIGRATIONS, 7: MIGRATIONS[7] + "\nTHIS IS NOT SQL;"}
    with pytest.raises(sqlite3.Error):
        SQLiteDatabase(path).apply_schema(SCHEMA, broken)
    assert SQLiteDatabase(path).schema_version() == 6
    with closing(sqlite3.connect(path)) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "audit_log" not in names and "staff_accounts_v7" not in names and "staff_accounts" in names


@pytest.mark.postgres
def test_postgres_version_6_is_upgraded_in_place(postgres_schema):
    import psycopg

    db = PostgresDatabase(postgres_schema)
    try:
        db.apply_schema(POSTGRES_SCHEMA, {6: POSTGRES_MIGRATIONS[6]}, POSTGRES_BASE_VERSION)
        assert db.schema_version() == 6
        refs = _populate_v6(ComplaintStore(database=db))
        store = ComplaintStore(database=db)
        store.initialize()
        store.initialize()
        assert db.schema_version() == SCHEMA_VERSION

        def raw(sql, params, write=False):
            with psycopg.connect(postgres_schema, autocommit=True) as conn:
                cur = conn.execute(sql.replace("?", "%s"), params)
                return cur.fetchall() if cur.description else []

        _check_upgraded(store, raw, refs, (psycopg.errors.IntegrityError,), (psycopg.errors.RaiseException,))
        with pytest.raises(psycopg.errors.RaiseException):
            raw("TRUNCATE audit_log", (), write=True)
    finally:
        db.close()
