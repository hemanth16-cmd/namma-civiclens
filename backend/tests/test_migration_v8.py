"""Schema 8 (AI analysis): a version-7 database is upgraded in place on SQLite and PostgreSQL with every
existing row kept, the new constraints hold (one inference per evidence and detector, one assessment per
evidence pair, valid states and outcomes), PostgreSQL stores JSON as JSONB, and the SQLite -> PostgreSQL
migration tool copies AI rows and validates them."""

import json
import sqlite3
from contextlib import closing

import pytest

from app.ai.ai_store import AiStore
from app.db import PostgresDatabase, SQLiteDatabase
from app.db_migrate import migrate
from app.store import MIGRATIONS, POSTGRES_BASE_VERSION, POSTGRES_MIGRATIONS, POSTGRES_SCHEMA, SCHEMA, SCHEMA_VERSION, ComplaintStore, utc_now
from tests.aiutil import ai_settings
from tests.test_complaints import payload


def _populate_v7(store: ComplaintStore) -> dict:
    record, _ = store.create(payload())
    store.create_staff("asha", "Asha", "scrypt$hash", "supervisor")
    ref = store.complaint_ref(record["id"])
    meta = {"evidenceId": "ev_" + "a" * 22, "storageKey": "k" * 32, "mediaType": "photo", "contentType": "image/jpeg",
            "byteSize": 10, "sha256": "0" * 64, "capturedAt": None, "uploadedAt": utc_now(), "storageBackend": "local"}
    store.add_evidence(ref["pk"], meta)
    return {"id": record["id"], "complaint_pk": ref["pk"], "staff_pk": store.get_staff("asha")["pk"]}


def _exercise_v8(store: ComplaintStore, refs: dict, integrity_errors: tuple):
    assert store.get(refs["id"])["status"] == "Submitted", "existing complaints survive"
    ai = AiStore(store)
    original = ai.original_evidence(refs["complaint_pk"])
    specs = list(ai_settings().detectors.values())[:2]
    ai.ensure_inferences(refs["complaint_pk"], original["pk"], specs, "roboflow")
    ai.ensure_inferences(refs["complaint_pk"], original["pk"], specs, "roboflow")
    assert len(ai.results_for_evidence(original["pk"])) == 2, "ON CONFLICT keeps one row per evidence and detector"
    pk = ai.pending_inferences(original["pk"])[0]
    assert ai.claim_inference(pk, specs[0]) is not None and ai.claim_inference(pk, specs[0]) is None
    result = {"topClass": "pothole", "topCategory": "Potholes", "topConfidence": 0.9, "imageWidth": 10, "imageHeight": 10,
              "detections": [{"className": "pothole", "category": "Potholes", "confidence": 0.9,
                              "bbox": {"x": 1, "y": 2, "width": 3, "height": 4}}],
              "confidenceMeta": {"max": 0.9, "presentThreshold": 0.5}}
    ai.complete_inference(pk, result)
    done = [r for r in ai.results_for_evidence(original["pk"]) if r["state"] == "completed"][0]
    assert done["detections"] == result["detections"] and done["confidence"]["max"] == 0.9, "JSON round-trips on both engines"
    with closing(store._connect()) as conn:
        with pytest.raises(integrity_errors):
            with conn.transaction():
                conn.execute("UPDATE ai_inferences SET state = 'guessing' WHERE pk = ?", (pk,))
        with pytest.raises(integrity_errors):
            with conn.transaction():
                conn.execute("UPDATE ai_inferences SET top_confidence = 1.5 WHERE pk = ?", (pk,))
    return ai, original


@pytest.mark.sqlite_only
def test_sqlite_version_7_is_upgraded_to_8(tmp_path):
    path = tmp_path / "v7.sqlite3"
    SQLiteDatabase(path).apply_schema(SCHEMA, {v: sql for v, sql in MIGRATIONS.items() if v <= 7})
    assert SQLiteDatabase(path).schema_version() == 7
    refs = _populate_v7(ComplaintStore(path))
    store = ComplaintStore(path)
    store.initialize()
    store.initialize()
    assert SQLiteDatabase(path).schema_version() == SCHEMA_VERSION == 8
    _exercise_v8(store, refs, (sqlite3.IntegrityError,))
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):  # an assessment must point at real evidence
            conn.execute("INSERT INTO repair_assessments (assessment_id, complaint_pk, original_evidence_pk, repair_evidence_pk, created_at, updated_at) "
                         "VALUES ('ra_x', ?, 999, 998, 'z', 'z')", (refs["complaint_pk"],))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO repair_assessments (assessment_id, complaint_pk, original_evidence_pk, repair_evidence_pk, suggested_outcome, "
                         "created_at, updated_at) VALUES ('ra_y', ?, 1, 1, 'fixed', 'z', 'z')", (refs["complaint_pk"],))


@pytest.mark.sqlite_only
def test_failed_v8_migration_leaves_version_7(tmp_path):
    path = tmp_path / "v7.sqlite3"
    SQLiteDatabase(path).apply_schema(SCHEMA, {v: sql for v, sql in MIGRATIONS.items() if v <= 7})
    with pytest.raises(sqlite3.Error):
        SQLiteDatabase(path).apply_schema(SCHEMA, {**MIGRATIONS, 8: MIGRATIONS[8] + "\nNOT SQL;"})
    assert SQLiteDatabase(path).schema_version() == 7
    with closing(sqlite3.connect(path)) as conn:
        assert "ai_inferences" not in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


@pytest.mark.postgres
def test_postgres_version_7_is_upgraded_to_8_with_jsonb(postgres_schema):
    import psycopg

    db = PostgresDatabase(postgres_schema)
    try:
        db.apply_schema(POSTGRES_SCHEMA, {v: POSTGRES_MIGRATIONS[v] for v in (6, 7)}, POSTGRES_BASE_VERSION)
        assert db.schema_version() == 7
        refs = _populate_v7(ComplaintStore(database=db))
        store = ComplaintStore(database=db)
        store.initialize()
        store.initialize()
        assert db.schema_version() == SCHEMA_VERSION == 8
        _exercise_v8(store, refs, (psycopg.errors.IntegrityError,))
        with psycopg.connect(postgres_schema) as conn:
            types = dict(conn.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = current_schema() "
                                      "AND table_name IN ('ai_inferences', 'repair_assessments') AND column_name IN "
                                      "('predictions', 'confidence_meta', 'details')").fetchall())
            assert types == {"predictions": "jsonb", "confidence_meta": "jsonb", "details": "jsonb"}
            assert conn.execute("SELECT predictions -> 0 ->> 'className' FROM ai_inferences WHERE state = 'completed'").fetchone()[0] == "pothole"
    finally:
        db.close()


@pytest.mark.postgres
def test_sqlite_ai_rows_migrate_to_postgres(tmp_path, postgres_schema):
    path = tmp_path / "source.sqlite3"
    source = ComplaintStore(database=SQLiteDatabase(path))  # explicit: the suite may be running on PostgreSQL
    source.initialize()
    refs = _populate_v7(source)
    ai, original = _exercise_v8(source, refs, (sqlite3.IntegrityError,))
    repair_meta = {"evidenceId": "ev_" + "b" * 22, "storageKey": "r" * 32, "mediaType": "photo", "contentType": "image/jpeg",
                   "byteSize": 10, "sha256": "1" * 64, "uploadedAt": utc_now(), "storageBackend": "local"}
    with closing(source._connect()) as conn, conn.transaction():
        conn.execute("INSERT INTO evidence (evidence_id, complaint_pk, role, storage_key, media_type, content_type, byte_size, sha256, uploaded_at, "
                     "storage_backend) VALUES (?, ?, 'repair', ?, 'photo', 'image/jpeg', 10, ?, ?, 'local')",
                     (repair_meta["evidenceId"], refs["complaint_pk"], repair_meta["storageKey"], repair_meta["sha256"], repair_meta["uploadedAt"]))
    repair = ai.repair_evidence(refs["complaint_pk"])
    pk, created = ai.create_assessment(refs["complaint_pk"], original["pk"], repair, refs["staff_pk"])
    assert created and ai.create_assessment(refs["complaint_pk"], original["pk"], repair, refs["staff_pk"]) == (pk, False)
    ai.claim_assessment(pk)
    ai.complete_assessment(pk, {"outcome": "inconclusive", "verificationConfidence": None, "locationCheck": "unverified",
                                "gpsDistanceM": None, "details": {"reasons": ["test"], "nested": {"b": 1, "a": [1, 2]}}},
                           [(r["pk"], "original") for r in ai.results_for_evidence(original["pk"])])

    pg = PostgresDatabase(postgres_schema)
    try:
        report = migrate(path, pg, execute=True)
        assert report["ok"], json.dumps(report, indent=1)
        for table in ("ai_inferences", "repair_assessments", "repair_assessment_inferences"):
            assert report["tables"][table]["inserted"] == report["tables"][table]["source"] > 0
        again = migrate(path, pg, execute=True)
        assert again["ok"] and again["tables"]["ai_inferences"]["alreadyPresent"] == 2, "JSONB values compare equal on re-run"
        copied = AiStore(ComplaintStore(database=pg)).assessments_for_complaint(refs["complaint_pk"])
        assert copied[0]["details"]["nested"] == {"a": [1, 2], "b": 1}
    finally:
        pg.close()
