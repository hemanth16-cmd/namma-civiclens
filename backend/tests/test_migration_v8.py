"""AI analysis as the named migration ai_analysis (it shipped on main as numbered version 8; see
docs/MIGRATION_RECONCILIATION.md). A version-7 database gets the AI tables in place on SQLite and PostgreSQL
with every existing row kept and its numbered version left at 7; a database that already ran main's
version 8 adopts the feature without re-running it and keeps its AI rows; the constraints hold (one
inference per evidence and detector, one assessment per evidence pair, valid states and outcomes);
PostgreSQL stores JSON as JSONB; a failed migration leaves nothing behind; and the SQLite -> PostgreSQL
migration tool copies AI rows and validates them."""

import json
import sqlite3
from contextlib import closing

import pytest

from app.ai.ai_store import AiStore
from app.db import PostgresDatabase, SQLiteDatabase
from app.db_migrate import migrate
import app.store as store_mod
from app.db import Feature
from app.store import (AI_ANALYSIS_POSTGRES, AI_ANALYSIS_SQLITE, MIGRATIONS, POSTGRES_BASE_VERSION, POSTGRES_MIGRATIONS, POSTGRES_SCHEMA,
                       SCHEMA, SCHEMA_VERSION, ComplaintStore, StoreError, utc_now)
from tests.legacy_v8 import AI_V8_POSTGRES, AI_V8_SQLITE
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
def test_sqlite_version_7_gets_ai_analysis(tmp_path):
    path = tmp_path / "v7.sqlite3"
    SQLiteDatabase(path).apply_schema(SCHEMA, {v: sql for v, sql in MIGRATIONS.items() if v <= 7})
    assert SQLiteDatabase(path).schema_version() == 7
    refs = _populate_v7(ComplaintStore(path))
    store = ComplaintStore(path)
    store.initialize()
    store.initialize()
    assert SQLiteDatabase(path).schema_version() == SCHEMA_VERSION == 7, "the numbered version stays 7"
    assert SQLiteDatabase(path).features()["ai_analysis"] == "applied"
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


def test_the_ai_sql_is_mains_version_8_unchanged():
    assert AI_ANALYSIS_SQLITE == AI_V8_SQLITE and AI_ANALYSIS_POSTGRES == AI_V8_POSTGRES


@pytest.mark.sqlite_only
def test_a_database_that_ran_mains_version_8_adopts_ai_analysis_and_keeps_its_rows(tmp_path):
    path = tmp_path / "main-v8.sqlite3"
    SQLiteDatabase(path).apply_schema(SCHEMA, {**{v: sql for v, sql in MIGRATIONS.items() if v <= 7}, 8: AI_V8_SQLITE})
    refs = _populate_v7(_legacy_store(path))
    with closing(sqlite3.connect(path)) as conn:
        ev = conn.execute("SELECT pk FROM evidence").fetchone()[0]
        conn.execute("INSERT INTO ai_inferences (inference_id, complaint_pk, evidence_pk, detector, provider, mechanism, model_id, state, "
                     "predictions, created_at, updated_at) VALUES ('ai_legacy', ?, ?, 'legacy-detector', 'roboflow', 'model', 'm/1', 'failed', "
                     "'[{\"className\":\"pothole\"}]', 'z', 'z')", (refs["complaint_pk"], ev))
        conn.commit()
    store = ComplaintStore(path)
    store.initialize()
    store.initialize()
    db = SQLiteDatabase(path)
    assert db.schema_version() == 8, "a legacy version is never lowered"
    assert db.features() == {"duplicate_detection": "applied", "resident_accounts": "applied", "ward_zone_operations": "applied", "ai_analysis": "adopted", "repair_evidence_geotagging": "applied"}
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT predictions FROM ai_inferences WHERE inference_id = 'ai_legacy'").fetchone()[0] == '[{"className":"pothole"}]'
    # The restored AI code writes to the adopted tables next to the legacy row.
    ai = AiStore(store)
    original = ai.original_evidence(refs["complaint_pk"])
    ai.ensure_inferences(refs["complaint_pk"], original["pk"], list(ai_settings().detectors.values())[:1], "roboflow")
    assert {r["id"] for r in ai.results_for_evidence(original["pk"])} >= {"ai_legacy"} and len(ai.results_for_evidence(original["pk"])) == 2


def _legacy_store(path):
    """A store over a database built by another branch's code, without applying this branch's features."""
    store = ComplaintStore(database=SQLiteDatabase(path))
    return store


@pytest.mark.sqlite_only
def test_failed_ai_analysis_migration_leaves_nothing_behind(tmp_path, monkeypatch):
    path = tmp_path / "v7.sqlite3"
    SQLiteDatabase(path).apply_schema(SCHEMA, {v: sql for v, sql in MIGRATIONS.items() if v <= 7})
    broken = tuple(Feature(f.name, f.sqlite + "\nNOT SQL;", f.postgres, f.adopt_if) if f.name == "ai_analysis" else f for f in store_mod.FEATURES)
    monkeypatch.setattr(store_mod, "FEATURES", broken)
    with pytest.raises(StoreError):
        ComplaintStore(path).initialize()
    assert SQLiteDatabase(path).schema_version() == 7
    assert "ai_analysis" not in SQLiteDatabase(path).features()
    with closing(sqlite3.connect(path)) as conn:
        assert "ai_inferences" not in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


@pytest.mark.postgres
def test_postgres_version_7_gets_ai_analysis_with_jsonb(postgres_schema):
    import psycopg

    db = PostgresDatabase(postgres_schema)
    try:
        db.apply_schema(POSTGRES_SCHEMA, {v: POSTGRES_MIGRATIONS[v] for v in (6, 7)}, POSTGRES_BASE_VERSION)
        assert db.schema_version() == 7
        refs = _populate_v7(ComplaintStore(database=db))
        store = ComplaintStore(database=db)
        store.initialize()
        store.initialize()
        assert db.schema_version() == SCHEMA_VERSION == 7
        assert db.features()["ai_analysis"] == "applied"
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
