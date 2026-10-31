"""Migration reconciliation (docs/MIGRATION_RECONCILIATION.md).

Schema versions 1-7 are identical on every branch, but two branches each shipped their own version 8:
AI analysis on origin/main and duplicate detection on origin/feature/core-platform-development. A
database at "version 8" can therefore hold either. This branch adds named migrations (db.Feature,
recorded in civiclens_features) so a change is applied exactly once whatever numbered history the
database has. These tests build each real starting state from the other branches' DDL (legacy_v8.py)
and check, on SQLite and PostgreSQL, that:

- this branch starts on every state without touching rows or lowering the version number;
- the reconciliation recipe for the eventual merge (both v8 changes as named, adoptable features)
  turns every state into the same schema, adopting what exists and creating only what is missing;
- a failing named migration is rolled back completely and a repeat run is a no-op.
"""

import os
from contextlib import closing

import pytest

import app.store as store_mod
from app.db import Feature, PostgresDatabase, SQLiteDatabase
from app.store import MIGRATIONS, POSTGRES_BASE_VERSION, POSTGRES_MIGRATIONS, POSTGRES_SCHEMA, SCHEMA, ComplaintStore, utc_now
from tests.legacy_v8 import AI_V8_POSTGRES, AI_V8_SQLITE, DUPLICATES_V8_POSTGRES, DUPLICATES_V8_SQLITE
from tests.test_complaints import payload

PG_URL = os.environ.get("CIVICLENS_TEST_POSTGRES_URL", "")

AI_TABLES = {"ai_inferences", "repair_assessments", "repair_assessment_inferences"}
DUP_TABLES = {"complaint_merges", "duplicate_dismissals"}

# The merge recipe: each branch's version 8 becomes a named feature that is adopted where it exists,
# followed by this branch's own named migrations (resident_accounts).
RECIPE = (
    Feature("ai_analysis", AI_V8_SQLITE, AI_V8_POSTGRES, adopt_if="ai_inferences"),
    Feature("duplicate_detection", DUPLICATES_V8_SQLITE, DUPLICATES_V8_POSTGRES, adopt_if="complaint_merges"),
    *store_mod.FEATURES,
)
RESIDENT_TABLES = {"resident_accounts", "resident_sessions", "resident_preferences"}

STATES = {
    "v7 (integration/v52-fullstack, develop)": ({}, 7),
    "v8 AI (main)": ({8: "ai"}, 8),
    "v8 duplicates (feature/core-platform-development)": ({8: "dup"}, 8),
}


def _migrations(engine: str, extra: dict) -> dict:
    base = dict(MIGRATIONS if engine == "sqlite" else POSTGRES_MIGRATIONS)
    sql = {("sqlite", "ai"): AI_V8_SQLITE, ("sqlite", "dup"): DUPLICATES_V8_SQLITE,
           ("postgres", "ai"): AI_V8_POSTGRES, ("postgres", "dup"): DUPLICATES_V8_POSTGRES}
    return {**base, **{v: sql[(engine, kind)] for v, kind in extra.items()}}


def _build(engine: str, target, extra: dict):
    """A database exactly as another branch's code would have left it (no civiclens_features)."""
    if engine == "sqlite":
        db = SQLiteDatabase(target)
        db.apply_schema(SCHEMA, _migrations(engine, extra))
    else:
        db = PostgresDatabase(target)
        db.apply_schema(POSTGRES_SCHEMA, _migrations(engine, extra), POSTGRES_BASE_VERSION)
    return db


def _populate(db) -> dict:
    store = ComplaintStore(database=db)
    record, claim = store.create(payload())
    store.create_staff("asha", "Asha", "scrypt$hash", role="supervisor")
    ref = store.complaint_ref(record["id"])
    meta = {"evidenceId": "ev_legacy", "storageKey": "k" * 32, "mediaType": "photo", "contentType": "image/jpeg",
            "byteSize": 10, "sha256": "0" * 64, "capturedAt": None, "uploadedAt": utc_now(), "storageBackend": "local"}
    store.add_evidence(ref["pk"], meta)
    with closing(db.connect()) as conn:
        claim_hash = conn.execute("SELECT claim_hash FROM complaints WHERE pk = ?", (ref["pk"],)).fetchone()[0]
    return {"id": record["id"], "pk": ref["pk"], "claim": claim, "claim_hash": claim_hash}


def _tables(db) -> set[str]:
    with closing(db.connect()) as conn:
        if db.backend == "sqlite":
            return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        return {r[0] for r in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")}


def _check_preserved(db, refs: dict) -> None:
    store = ComplaintStore(database=db)
    assert store.get(refs["id"])["status"] == "Submitted"
    assert store.get_evidence(refs["pk"])["evidenceId"] == "ev_legacy", "evidence references survive"
    assert store.get_staff("asha")["role"] == "supervisor"
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT claim_hash FROM complaints WHERE pk = ?", (refs["pk"],)).fetchone()[0] == refs["claim_hash"]
    from app.claims import verify_claim_code
    assert verify_claim_code(refs["claim"], refs["claim_hash"]), "the existing claim code still verifies"


def _check_unowned(db, refs: dict) -> None:
    """Existing (passerby) complaints are never assigned to a resident by a migration."""
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT owner_resident_pk, owned_via, owned_at FROM complaints WHERE pk = ?", (refs["pk"],)).fetchone()
        assert tuple(row) == (None, None, None)
        assert conn.execute("SELECT COUNT(*) FROM complaints WHERE owner_resident_pk IS NOT NULL").fetchone()[0] == 0


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def engine_target(request, tmp_path):
    if request.param == "sqlite":
        yield "sqlite", tmp_path / "legacy.sqlite3"
    else:
        yield "postgres", request.getfixturevalue("postgres_schema")


@pytest.mark.parametrize("state", list(STATES))
def test_this_branch_starts_on_every_known_state_without_changing_it(engine_target, state, monkeypatch):
    engine, target = engine_target
    extra, version = STATES[state]
    db = _build(engine, target, extra)
    try:
        refs = _populate(db)
        before = _tables(db)
        ComplaintStore(database=db).initialize()
        ComplaintStore(database=db).initialize()
        assert db.schema_version() == version, "the numbered version is never lowered or bumped"
        assert before <= _tables(db), "no table is dropped"
        assert RESIDENT_TABLES <= _tables(db)
        assert db.features() == {"resident_accounts": "applied"}
        _check_preserved(db, refs)
        _check_unowned(db, refs)
    finally:
        if engine == "postgres":
            db.close()


@pytest.mark.parametrize("state", list(STATES))
def test_reconciliation_recipe_converges_every_state(engine_target, state, monkeypatch):
    engine, target = engine_target
    extra, version = STATES[state]
    db = _build(engine, target, extra)
    try:
        refs = _populate(db)
        monkeypatch.setattr(store_mod, "FEATURES", RECIPE)
        ComplaintStore(database=db).initialize()
        tables = _tables(db)
        assert AI_TABLES <= tables and DUP_TABLES <= tables, "both branches' tables exist afterwards"
        expected = {"ai_analysis": "adopted" if extra.get(8) == "ai" else "applied",
                    "duplicate_detection": "adopted" if extra.get(8) == "dup" else "applied",
                    "resident_accounts": "applied"}
        assert db.features() == expected
        assert db.schema_version() == version
        with closing(db.connect()) as conn:
            assert conn.execute("SELECT merged_into_pk FROM complaints WHERE pk = ?", (refs["pk"],)).fetchone()[0] is None
            assert conn.execute("SELECT COUNT(*) FROM ai_inferences").fetchone()[0] == 0
        _check_preserved(db, refs)
        _check_unowned(db, refs)
        ComplaintStore(database=db).initialize()  # a repeat run is a no-op
        assert db.features() == expected
    finally:
        if engine == "postgres":
            db.close()


def test_a_fresh_database_gets_every_named_feature(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "FEATURES", RECIPE)
    db = SQLiteDatabase(tmp_path / "fresh.sqlite3")
    ComplaintStore(database=db).initialize()
    assert db.features() == {"ai_analysis": "applied", "duplicate_detection": "applied", "resident_accounts": "applied"}
    assert db.schema_version() == 7


def test_a_failing_named_migration_is_rolled_back(tmp_path, monkeypatch):
    db = _build("sqlite", tmp_path / "legacy.sqlite3", {})
    refs = _populate(db)
    broken = Feature("broken", "CREATE TABLE half_done (pk INTEGER PRIMARY KEY);\nALTER TABLE no_such_table ADD COLUMN x TEXT;", "")
    monkeypatch.setattr(store_mod, "FEATURES", (broken,))
    with pytest.raises(store_mod.StoreError):
        ComplaintStore(database=db).initialize()
    assert "half_done" not in _tables(db), "nothing of the failed feature is left behind"
    assert db.features() == {}
    _check_preserved(db, refs)
