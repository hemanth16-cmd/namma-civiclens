"""PostgreSQL backend and SQLite → PostgreSQL migration, against a real PostgreSQL server.

Needs CIVICLENS_TEST_POSTGRES_URL (e.g. a throwaway Docker container); skipped without it. Each test
gets its own empty schema. No Supabase credentials are involved.
"""

import hashlib
import json
import sqlite3
import threading
from contextlib import closing
from datetime import timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import auth, wards
from app.db import DatabaseConfigError, PostgresDatabase, SQLiteDatabase, database_from_env, selected_backend
from app.db_migrate import main as migrate_cli
from app.db_migrate import _norm_row, migrate
from app.main import create_app
from app.store import POSTGRES_BASE_VERSION, POSTGRES_SCHEMA, SCHEMA, SCHEMA_VERSION, TABLES, ComplaintStore, StoreError
from app.ai.service import InlineExecutor
from tests.aiutil import FakeProvider, ai_settings, photo
from tests.test_complaints import payload
from tests.test_evidence import JPEG
from tests.test_notifications import subscription

pytestmark = pytest.mark.postgres
PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def explicit_backends(monkeypatch):
    """These tests pick each backend explicitly (SQLite sources, PostgreSQL targets), so they start
    from the default environment even when the rest of the suite runs on PostgreSQL."""
    monkeypatch.delenv("DATABASE_BACKEND", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)


@pytest.fixture
def pg_url(postgres_schema):
    return postgres_schema


@pytest.fixture
def pg_store(pg_url):
    store = ComplaintStore(database=PostgresDatabase(pg_url))
    store.initialize()
    yield store
    store.close()


def pg_query(url, sql, params=()):
    with psycopg.connect(url) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def source_state(path):
    """The SQLite file's content hash, plus the size of any WAL file. Opening a WAL-mode database
    read-only may create empty -wal/-shm helper files; that is SQLite reader bookkeeping, not data."""
    wal = path.with_name(path.name + "-wal")
    return hashlib.sha256(path.read_bytes()).hexdigest(), (wal.stat().st_size if wal.exists() else 0)


# ---------- Configuration ----------

def test_backend_selection_and_validation(monkeypatch):
    assert selected_backend() == "sqlite"
    assert isinstance(database_from_env(), SQLiteDatabase)
    monkeypatch.setenv("DATABASE_BACKEND", "mysql")
    with pytest.raises(DatabaseConfigError, match='"sqlite" or "postgres"'):
        selected_backend()
    monkeypatch.setenv("DATABASE_BACKEND", "postgres")
    with pytest.raises(DatabaseConfigError, match="DATABASE_URL"):
        database_from_env()
    monkeypatch.setenv("DATABASE_URL", "mysql://user:secret@host/db")
    with pytest.raises(DatabaseConfigError, match="postgresql://") as err:
        database_from_env()
    assert "secret" not in str(err.value)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:secret@db.example.supabase.co:5432/postgres")
    monkeypatch.setenv("CIVICLENS_DB_POOL_MAX", "zero")
    with pytest.raises(DatabaseConfigError, match="CIVICLENS_DB_POOL_MAX"):
        database_from_env()
    monkeypatch.setenv("CIVICLENS_DB_POOL_MAX", "4")
    db = database_from_env()  # constructing does not connect
    assert isinstance(db, PostgresDatabase) and db.max_size == 4 and db.pool is None
    assert db.describe() == "PostgreSQL db.example.supabase.co:5432/postgres" and "secret" not in db.describe()


def test_app_uses_postgres_when_configured(pg_url, monkeypatch, evidence_dir):
    monkeypatch.setenv("DATABASE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", pg_url)
    with TestClient(create_app(evidence_dir=evidence_dir)) as client:
        assert client.app.state.store.backend == "postgres"
        receipt = client.post("/api/v1/complaints", json=payload()).json()
        assert client.get(f"/api/v1/complaints/{receipt['id']}").status_code == 200
    assert pg_query(pg_url, "SELECT public_id FROM complaints") == [(receipt["id"],)]


# ---------- Schema ----------

def test_schema_initialisation(pg_store, pg_url):
    tables = {r[0] for r in pg_query(pg_url, "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")}
    assert set(TABLES) | {"civiclens_schema", "civiclens_features"} == tables
    assert pg_query(pg_url, "SELECT version FROM civiclens_schema") == [(SCHEMA_VERSION,)]
    types = dict(pg_query(pg_url, "SELECT column_name, data_type FROM information_schema.columns "
                                  "WHERE table_schema = current_schema() AND table_name = 'complaints'"))
    assert types["gps_lat"] == types["gps_lng"] == types["gps_accuracy"] == "double precision", "no float4 precision loss"
    assert types["pk"] == "bigint" and types["created_at"] == "text"
    identity = pg_query(pg_url, "SELECT is_identity, identity_generation FROM information_schema.columns "
                                "WHERE table_schema = current_schema() AND table_name = 'evidence' AND column_name = 'pk'")
    assert identity == [("YES", "BY DEFAULT")]
    indexes = {r[0] for r in pg_query(pg_url, "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")}
    assert {"complaint_timeline_by_complaint", "verification_log_by_complaint", "evidence_access_by_evidence"} <= indexes

    identity = pg_query(pg_url, "SELECT table_name, data_type FROM information_schema.columns WHERE table_schema = current_schema() "
                                "AND column_name IN ('pk', 'recipient_pk', 'complaint_pk') AND table_name IN "
                                "('notification_recipients', 'notification_follows', 'push_subscriptions', 'notifications')")
    assert identity and all(t == "bigint" for _, t in identity)


def test_version_5_postgres_database_is_upgraded_in_place(pg_url):
    """A database created before notifications (schema 5) gains the notification tables and keeps its data."""
    db = PostgresDatabase(pg_url)
    try:
        db.apply_schema(POSTGRES_SCHEMA, {}, POSTGRES_BASE_VERSION)
        old = ComplaintStore(database=db)
        record, _ = old.create(payload(capturedAt=None))
        assert pg_query(pg_url, "SELECT version FROM civiclens_schema") == [(5,)]
        ComplaintStore(database=db).initialize()
        assert pg_query(pg_url, "SELECT version FROM civiclens_schema") == [(SCHEMA_VERSION,)] == [(7,)]
        assert db.features() == {"duplicate_detection": "applied", "resident_accounts": "applied",
                                    "ward_zone_operations": "applied", "ai_analysis": "applied",
                                    "repair_evidence_geotagging": "applied"}
        assert old.get(record["id"])["id"] == record["id"]
        for table in ("notification_recipients", "notification_follows", "push_subscriptions", "notifications"):
            assert pg_query(pg_url, f"SELECT COUNT(*) FROM {table}") == [(0,)]
        with pytest.raises(psycopg.errors.CheckViolation):
            pg_query(pg_url, "INSERT INTO notification_recipients (kind, created_at) VALUES ('citizen', 'z')")
    finally:
        db.close()


def test_initialisation_is_idempotent_and_safe_concurrently(pg_url):
    errors = []
    barrier = threading.Barrier(4)

    def start():
        store = ComplaintStore(database=PostgresDatabase(pg_url))
        try:
            barrier.wait()
            store.initialize()
            store.initialize()
        except Exception as exc:  # noqa: BLE001 - collected for the assertion
            errors.append(exc)
        finally:
            store.close()

    threads = [threading.Thread(target=start) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert pg_query(pg_url, "SELECT COUNT(*), MAX(version) FROM civiclens_schema") == [(1, SCHEMA_VERSION)]


def test_constraints_are_enforced(pg_store, pg_url):
    record, _ = pg_store.create(payload(capturedAt=None))
    with pytest.raises(psycopg.errors.CheckViolation):
        pg_query(pg_url, "UPDATE complaints SET gps_lat = 91 WHERE public_id = %s", (record["id"],))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        pg_query(pg_url, "INSERT INTO complaint_timeline (complaint_pk, status, text, date) VALUES (999999, 'x', 'x', 'x')")
    with pytest.raises(psycopg.errors.UniqueViolation):
        pg_query(pg_url, "INSERT INTO complaints (public_id, claim_hash, title, category, area, media_type, share_evidence, gps_lat, gps_lng, "
                         "gps_source_type, created_at, uploaded_at, updated_at, status, verification, department) "
                         "SELECT public_id, claim_hash, title, category, area, media_type, share_evidence, gps_lat, gps_lng, gps_source_type, "
                         "created_at, uploaded_at, updated_at, status, verification, department FROM complaints")


# ---------- Store operations ----------

def test_complaint_lifecycle_and_timeline(pg_store):
    record, claim = pg_store.create(payload(capturedAt=None))
    loaded = pg_store.get(record["id"])
    assert loaded["category"] == "Potholes" and loaded["status"] == "Submitted" and loaded["verification"] == "Pending Verification"
    assert loaded["gps"]["lat"] == 13.100712 and loaded["gps"]["lng"] == 77.596334, "full double precision round-trip"
    assert [t["status"] for t in loaded["timeline"]] == ["Submitted"] and loaded["verificationLog"] == []
    assert pg_store.check_upload_token(record["id"], record["uploadToken"])["status"] == "ok"
    token, _ = pg_store.issue_upload_token(record["id"])
    assert pg_store.check_upload_token(record["id"], record["uploadToken"])["status"] == "invalid"
    assert pg_store.check_upload_token(record["id"], token)["status"] == "ok"
    items, total = pg_store.list_complaints(10, 0)
    assert total == 1 and items[0]["id"] == record["id"]


def test_evidence_metadata_and_race(pg_store):
    record, _ = pg_store.create(payload(capturedAt=None))
    pk = pg_store.check_upload_token(record["id"], record["uploadToken"])["pk"]
    meta = {"evidenceId": "ev_a", "storageKey": "a" * 32, "mediaType": "photo", "contentType": "image/jpeg",
            "byteSize": 5_000_000_000, "sha256": "f" * 64, "capturedAt": None, "uploadedAt": "2026-10-01T00:00:00.000Z"}
    assert pg_store.add_evidence(pk, meta)[0] == "created"
    outcome, row = pg_store.add_evidence(pk, {**meta, "evidenceId": "ev_b", "storageKey": "b" * 32})
    assert outcome == "exists" and row["evidenceId"] == "ev_a"
    assert pg_store.get_evidence(pk)["byteSize"] == 5_000_000_000, "BIGINT sizes"
    assert pg_store.total_evidence_bytes() == 5_000_000_000 and isinstance(pg_store.total_evidence_bytes(), int)


def test_failed_transaction_rolls_back_everything(pg_store, pg_url):
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("CREATE FUNCTION fail_insert() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'simulated'; END $$ LANGUAGE plpgsql")
        conn.execute("CREATE TRIGGER fail_timeline BEFORE INSERT ON complaint_timeline FOR EACH ROW EXECUTE FUNCTION fail_insert()")
    with pytest.raises(StoreError):
        pg_store.create(payload(capturedAt=None), {"keyHash": "c" * 64, "fingerprint": "d" * 64})
    assert pg_query(pg_url, "SELECT (SELECT COUNT(*) FROM complaints), (SELECT COUNT(*) FROM idempotency_keys)") == [(0, 0)]
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("DROP TRIGGER fail_timeline ON complaint_timeline")
    record, _ = pg_store.create(payload(capturedAt=None))
    assert pg_store.get(record["id"]) is not None, "the connection pool recovered after the failure"


def test_concurrent_creates_and_replays(pg_store, pg_url):
    results, errors = [], []
    barrier = threading.Barrier(12)
    body = payload(capturedAt=None)

    def create():
        barrier.wait()
        try:
            results.append(pg_store.create(body)[0]["id"])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=create) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(set(results)) == 12
    assert pg_query(pg_url, "SELECT COUNT(*) FROM complaint_timeline") == [(12,)]


def test_id_collision_is_retried_on_postgres(pg_url):
    ids = iter(["NCL-2026-AAAAAA", "NCL-2026-AAAAAA", "NCL-2026-BBBBBB"])
    store = ComplaintStore(id_factory=lambda: next(ids), database=PostgresDatabase(pg_url))
    store.initialize()
    try:
        assert store.create(payload(capturedAt=None))[0]["id"] == "NCL-2026-AAAAAA"
        assert store.create(payload(capturedAt=None))[0]["id"] == "NCL-2026-BBBBBB"
    finally:
        store.close()


def test_pool_can_restart_after_close(pg_url):
    store = ComplaintStore(database=PostgresDatabase(pg_url))
    store.initialize()
    store.close()
    store.close()
    record, _ = store.create(payload(capturedAt=None))
    assert store.get(record["id"]) is not None
    store.close()


# ---------- SQLite → PostgreSQL migration ----------

@pytest.fixture
def populated_sqlite(isolated_db_path, evidence_dir):
    """A SQLite database with every table populated through the real API (local evidence storage). AI analysis
    (schema 8) runs with a fake provider on real photos, so its tables are populated too."""
    original, repair = photo(1, "damage"), photo(1, "clear", moved=True)
    app = create_app(db_path=isolated_db_path, evidence_dir=evidence_dir, ai_settings=ai_settings(), ai_provider=FakeProvider(),
                     ai_executor=InlineExecutor())
    with TestClient(app, client=("10.0.0.5", 1)) as client:
        store = client.app.state.store
        store.create_staff("asha", "Asha", auth.hash_password(PASSWORD))
        receipts = [client.post("/api/v1/complaints", json=payload(), headers={"Idempotency-Key": "k" * 40}).json(),
                    client.post("/api/v1/complaints", json=payload(category="Water leakage")).json()]
        inbox = client.post("/api/v1/notifications/inboxes").json()["inboxToken"]
        citizen = {"Authorization": f"Bearer {inbox}"}
        assert client.post("/api/v1/notifications/follows", json={"complaintId": receipts[0]["id"], "claim": receipts[0]["claim"]},
                           headers=citizen).status_code == 201
        assert client.post("/api/v1/notifications/subscriptions", json=subscription(), headers=citizen).status_code == 201
        upload = client.post(f"/api/v1/complaints/{receipts[0]['id']}/evidence", files={"file": ("e.jpg", original, "image/jpeg")},
                             headers={"Authorization": f"Bearer {receipts[0]['uploadToken']}"})
        assert upload.status_code == 201
        token = client.post("/api/v1/staff/sessions", json={"username": "asha", "password": PASSWORD}).json()["token"]
        staff = {"Authorization": f"Bearer {token}"}
        assert client.get(f"/api/v1/staff/complaints/{receipts[0]['id']}/evidence", headers=staff).content == original
        # Complaint management (schema 7): department, assignment, note, work order with before/after evidence, audit.
        store.create_department("roads", "Road maintenance")
        store.set_staff_role("asha", "supervisor")
        store.set_staff_department("asha", "roads", True)
        cid = receipts[0]["id"]
        assert client.post(f"/api/v1/staff/complaints/{cid}/assignment", json={"department": "roads", "expectedStatus": "Submitted"},
                           headers=staff).status_code == 200
        assert client.post(f"/api/v1/staff/complaints/{cid}/notes", json={"body": "internal"}, headers=staff).status_code == 201
        order = client.post(f"/api/v1/staff/complaints/{cid}/work-orders", json={"team": "crew"}, headers=staff).json()["workOrders"][0]["id"]
        assert client.post(f"/api/v1/staff/work-orders/{order}/evidence", files={"file": ("a.jpg", repair, "image/jpeg")},
                           headers=staff).status_code == 201
        # Resident accounts: an account, its session and preferences, and a claimed complaint.
        resident = client.post("/api/v1/residents", json={"email": "bina@example.org", "password": "resident passphrase 1",
                                                          "displayName": "Bina"}).json()["token"]
        res = {"Authorization": f"Bearer {resident}"}
        assert client.put("/api/v1/residents/me/preferences", json={"updates": False}, headers=res).status_code == 200
        assert client.post("/api/v1/residents/me/claims", json={"id": receipts[1]["id"], "claim": receipts[1]["claim"]},
                           headers=res).status_code == 200
        # Duplicate detection (schema 8): one merge and one dismissed pair.
        third = client.post("/api/v1/complaints", json=payload()).json()["id"]
        assert client.post(f"/api/v1/staff/complaints/{receipts[1]['id']}/merge", json={"canonical": cid, "reason": "same issue"},
                           headers=staff).status_code == 200
        assert client.post(f"/api/v1/staff/complaints/{cid}/duplicates/{third}/dismiss", json={"reason": "different"},
                           headers=staff).status_code == 200
        # AI analysis (named migration ai_analysis): both photos analysed and compared on upload.
        assert client.get(f"/api/v1/staff/complaints/{cid}/repair-assessment", headers=staff).json()["assessments"][0]["state"] == "completed"
        # Ward/zone operations: seeded wards, a city-wide scope, a manual ward and an allocation.
        store.seed_wards(wards.load_seed())
        store.set_staff_scope("asha", "city", None, True)
        assert client.post(f"/api/v1/staff/complaints/{cid}/ward", json={"ward": 5, "reason": "checked"}, headers=staff).status_code == 200
        assert client.post("/api/v1/staff/supervisor/allocations", json={"complaints": [cid], "assignee": "asha", "priority": "high",
                                                                          "reason": "drive"}, headers=staff).status_code == 200
    with closing(sqlite3.connect(isolated_db_path)) as conn:
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    assert all(counts[t] > 0 for t in TABLES if t != "verification_log"), counts
    return isolated_db_path, receipts, counts


def test_dry_run_writes_nothing(populated_sqlite, pg_url):
    path, _, counts = populated_sqlite
    db = PostgresDatabase(pg_url)
    try:
        report = migrate(path, db, execute=False)
    finally:
        db.close()
    assert report["mode"] == "dry-run" and report["targetSchemaExists"] is False and report["ok"] is False
    assert {t: v["toInsert"] for t, v in report["tables"].items()} == counts
    assert pg_query(pg_url, "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = current_schema()") == [(0,)]


def test_migration_preserves_everything_and_the_app_runs_on_it(populated_sqlite, pg_url, monkeypatch, evidence_dir):
    path, receipts, counts = populated_sqlite
    before_hash, before_wal = source_state(path)
    assert before_wal == 0
    db = PostgresDatabase(pg_url)
    try:
        report = migrate(path, db, execute=True)
    finally:
        db.close()
    assert report["ok"] is True, report
    assert {t: v["inserted"] for t, v in report["tables"].items()} == counts
    assert report["validation"]["rowsMatch"] and report["validation"]["foreignKeysValid"]
    assert source_state(path) == (before_hash, 0), "the source SQLite data is not modified"
    with closing(sqlite3.connect(path)) as conn:
        source = {t: conn.execute(f"SELECT * FROM {t} ORDER BY pk").fetchall() for t in TABLES}
    for table in TABLES:
        cols = [r[1] for r in sqlite3.connect(path).execute(f"PRAGMA table_info({table})")]
        target = pg_query(pg_url, f"SELECT {', '.join(cols)} FROM {table} ORDER BY pk")
        # JSONB columns come back parsed; compare them as JSON values (db_migrate does the same).
        assert [_norm_row(table, cols, r) for r in target] == [_norm_row(table, cols, r) for r in source[table]], table

    monkeypatch.setenv("DATABASE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", pg_url)
    with TestClient(create_app(evidence_dir=evidence_dir)) as client:
        public = client.get(f"/api/v1/complaints/{receipts[0]['id']}")
        assert public.status_code == 200 and public.json()["timeline"][0]["status"] == "Submitted"
        token = client.post("/api/v1/staff/sessions", json={"username": "asha", "password": PASSWORD}).json()["token"]
        view = client.get(f"/api/v1/staff/complaints/{receipts[0]['id']}/evidence", headers={"Authorization": f"Bearer {token}"})
        assert view.status_code == 200 and view.content == photo(1, "damage"), "migrated metadata still points at the local file"
        assert len(client.get(f"/api/v1/staff/complaints/{receipts[0]['id']}/evidence/access", headers={"Authorization": f"Bearer {token}"}).json()) == 2
        replay = client.post("/api/v1/complaints", json=payload(), headers={"Idempotency-Key": "k" * 40})
        assert replay.status_code == 409 and replay.json()["error"]["code"] in ("idempotency_key_used", "idempotency_key_conflict"), "idempotency state migrated"
        new = client.post("/api/v1/complaints", json=payload()).json()
        assert new["id"] not in {r["id"] for r in receipts}
    max_source_pk = max(r[0] for r in source["complaints"])
    assert pg_query(pg_url, "SELECT pk FROM complaints WHERE public_id = %s", (new["id"],))[0][0] > max_source_pk, "sequences advanced"


def test_rerun_is_safe_and_conflicts_stop_the_import(populated_sqlite, pg_url):
    path, _, counts = populated_sqlite
    db = PostgresDatabase(pg_url)
    try:
        assert migrate(path, db, execute=True)["ok"]
        again = migrate(path, db, execute=True)
        assert again["ok"] and {t: v["inserted"] for t, v in again["tables"].items()} == {t: 0 for t in TABLES}
        assert {t: v["alreadyPresent"] for t, v in again["tables"].items()} == counts
        pg_query(pg_url, "UPDATE complaints SET title = 'changed in PostgreSQL' WHERE pk = (SELECT MIN(pk) FROM complaints)")
        conflicted = migrate(path, db, execute=True)
        assert conflicted["ok"] is False and "stopped" in conflicted
        assert conflicted["conflicts"] == [{"table": "complaints", "pk": pg_query(pg_url, "SELECT MIN(pk) FROM complaints")[0][0]}]
    finally:
        db.close()
    assert pg_query(pg_url, "SELECT COUNT(*) FROM complaints") == [(counts["complaints"],)], "nothing written on conflict"


def test_older_sqlite_schema_is_upgraded_on_a_copy_only(tmp_path, pg_url):
    old = tmp_path / "v1.sqlite3"
    with closing(sqlite3.connect(old)) as conn:
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA user_version = 1")
        conn.execute("INSERT INTO complaints (public_id, claim_hash, title, category, area, media_type, share_evidence, gps_lat, gps_lng, "
                     "gps_source_type, created_at, uploaded_at, updated_at, status, verification, department) VALUES "
                     "('NCL-2026-OLD001', 'scrypt$x', 't', 'Potholes', 'a', 'photo', 0, 13.123456789, 77.5, 'gps', 'z', 'z', 'z', "
                     "'Submitted', 'Pending Verification', 'd')")
        conn.commit()
    db = PostgresDatabase(pg_url)
    try:
        assert migrate(old, db, execute=True)["ok"]
    finally:
        db.close()
    with closing(sqlite3.connect(old)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1, "source left at its old version"
    assert pg_query(pg_url, "SELECT public_id, gps_lat FROM complaints") == [("NCL-2026-OLD001", 13.123456789)]


def test_cli_reports_without_secrets(populated_sqlite, pg_url, monkeypatch, capsys):
    path, _, _ = populated_sqlite
    monkeypatch.setenv("DATABASE_URL", pg_url)
    assert migrate_cli(["--sqlite", str(path), "--json"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["mode"] == "dry-run"
    assert migrate_cli(["--sqlite", str(path), "--execute"]) == 0
    out = capsys.readouterr().out
    assert "RESULT: OK" in out
    hashes = [h for row in pg_query(pg_url, "SELECT claim_hash, upload_token_hash FROM complaints") for h in row if h]
    hashes += [r[0] for r in pg_query(pg_url, "SELECT password_hash FROM staff_accounts")]
    assert not any(h in out for h in hashes) and "civiclens_test" not in out.replace("civiclens_test/", "").split("target:")[0]
    monkeypatch.delenv("DATABASE_URL")
    assert migrate_cli(["--sqlite", str(path)]) == 2
    assert migrate_cli(["--sqlite", str(path / "missing.sqlite3"), "--postgres-url", pg_url]) == 1
