"""SQLite to PostgreSQL migration stress tests under realistic synthetic data volume.

Covers Section 4: Database and migration stress tests.
"""

import os
import sqlite3
import uuid
import pytest
import psycopg

from app.auth import hash_password
from app.claims import hash_claim_code, hash_upload_token
from app.db import PostgresDatabase, SQLiteDatabase
from app.db_migrate import migrate
from app.store import ComplaintStore, SCHEMA_VERSION, TABLES
from tests.test_complaints import payload
from tests.test_evidence import JPEG, WEBM
from tests.test_notifications import subscription

pytestmark = pytest.mark.postgres


@pytest.fixture
def synthetic_sqlite_db(tmp_path):
    """Generates a rich, realistic synthetic SQLite database across all 12 tables."""
    db_file = tmp_path / "synthetic_source.sqlite3"
    store = ComplaintStore(database=SQLiteDatabase(db_file))
    store.initialize()

    # 1. Staff accounts
    store.create_staff("admin_deep", "Chief Inspector", hash_password("AdminSecurePassword1!"))
    store.create_staff("field_officer", "Field Agent", hash_password("FieldAgentPassword2!"))
    staff1 = store.get_staff("admin_deep")
    staff_pk1 = staff1["pk"]

    # Staff sessions
    session_token_1 = hash_upload_token("staff_session_token_1")
    store.create_staff_session(staff_pk1, session_token_1, "2030-01-01T00:00:00.000Z")

    # 2. Complaints, timeline, evidence
    categories = [
        "Potholes", "Road cracks and damage", "Garbage accumulation",
        "Illegal dumping", "Drainage or waterlogging", "Broken streetlights",
    ]

    complaint_pks = []
    for i in range(30):
        cat = categories[i % len(categories)]
        media = "video" if i % 5 == 0 else "photo"
        p = payload(category=cat, mediaType=media, description=f"Synthetic migration case #{i}", capturedAt=None)
        rec, claim = store.create(p)
        c_ref = store.complaint_ref(rec["id"])
        c_pk = c_ref["pk"]
        complaint_pks.append(c_pk)

        # Evidence
        if i % 2 == 0:
            content = WEBM if media == "video" else JPEG
            ctype = "video/webm" if media == "video" else "image/jpeg"
            meta = {
                "evidenceId": f"ev_synth_{i:04d}",
                "storageKey": f"{i:032x}",
                "mediaType": media,
                "contentType": ctype,
                "byteSize": len(content),
                "sha256": "a" * 64,
                "capturedAt": "2026-10-30T10:00:00.000Z",
                "uploadedAt": "2026-10-30T10:05:00.000Z",
                "storageBackend": "local" if i % 4 != 0 else "supabase",
            }
            store.add_evidence(c_pk, meta)
            with sqlite3.connect(db_file) as conn:
                ev_pk = conn.execute("SELECT pk FROM evidence WHERE evidence_id = ?", (meta["evidenceId"],)).fetchone()[0]
            store.log_evidence_access(ev_pk, staff_pk1, "127.0.0.1")

    # 3. Notification recipients, follows, push subscriptions, notifications
    citizen_token_hash = hash_upload_token("citizen_inbox_synth_token")
    citizen_rec = store.create_citizen_recipient(citizen_token_hash)
    citizen_rec_pk = citizen_rec["pk"]
    staff_rec_pk = store.staff_recipient(staff_pk1)["pk"]

    # Follows
    for c_pk in complaint_pks[:5]:
        store.follow_complaint(citizen_rec_pk, c_pk, 50)

    # Push subscriptions
    sub_data = subscription()
    store.save_push_subscription(citizen_rec_pk, {
        "endpoint": sub_data["endpoint"],
        "p256dh": sub_data["keys"]["p256dh"],
        "auth": sub_data["keys"]["auth"],
        "label": "Synthetic Pixel 8",
        "expiresAt": None,
    }, 10)

    # Notifications
    store.add_notifications("recipient", {
        "kind": "test",
        "title": "Migration test notification",
        "body": "Synthetic notification body",
        "url": "#/notifications",
        "dedupeKey": "synth:test:1",
        "recipientPk": citizen_rec_pk,
        "audience": "citizen",
    })
    store.add_notifications("staff", {
        "kind": "new_complaint",
        "title": "New complaint",
        "body": "NCL-2026-SYNTH0 · Potholes",
        "url": "#/staff/NCL-2026-SYNTH0",
        "dedupeKey": "new_complaint:0",
        "complaintPk": complaint_pks[0],
        "audience": "staff",
    })

    return db_file


def test_sqlite_to_postgres_migration_lifecycle(synthetic_sqlite_db, postgres_schema):
    """Comprehensive test of migration planning, dry-run, execution, verification, and rerun idempotency."""
    pg_db = PostgresDatabase(postgres_schema)

    # 1. Dry run on fresh PostgreSQL: reports plan, creates nothing
    dry_report = migrate(synthetic_sqlite_db, pg_db, execute=False)
    assert dry_report["mode"] == "dry-run"
    assert dry_report["ok"] is False
    assert dry_report["conflicts"] == []
    assert dry_report["targetSchemaExists"] is False

    # Verify no tables exist in PostgreSQL yet
    with psycopg.connect(pg_db.url) as conn:
        tables = conn.execute("SELECT table_name FROM information_schema.columns WHERE table_schema = current_schema()").fetchall()
        assert len(tables) == 0

    # 2. Execute migration
    exec_report = migrate(synthetic_sqlite_db, pg_db, execute=True)
    assert exec_report["mode"] == "execute"
    assert exec_report["ok"] is True
    assert exec_report["conflicts"] == []
    assert exec_report["validation"]["rowsMatch"] is True
    assert exec_report["validation"]["foreignKeysValid"] is True

    # 3. Verify sequence advancement: insert new row in PostgreSQL without collision
    pg_store = ComplaintStore(database=pg_db)
    rec, claim = pg_store.create(payload(description="Post-migration complaint", capturedAt=None))
    assert rec["id"].startswith("NCL-")
    pg_ref = pg_store.complaint_ref(rec["id"])
    assert pg_ref is not None

    # 4. Rerun migration: safe, identical rows skipped, 0 conflicts
    rerun_report = migrate(synthetic_sqlite_db, pg_db, execute=True)
    assert rerun_report["ok"] is True
    assert rerun_report["conflicts"] == []
    for t_name, stats in rerun_report["tables"].items():
        assert stats["toInsert"] == 0
        assert stats["conflicts"] == 0
        assert stats["alreadyPresent"] == stats["source"]

    # 5. Conflict handling: simulate conflicting row in SQLite source
    with sqlite3.connect(synthetic_sqlite_db) as src:
        src.execute("UPDATE complaints SET description = 'Conflicting description' WHERE pk = 1")
        src.commit()

    conflict_report = migrate(synthetic_sqlite_db, pg_db, execute=True)
    assert len(conflict_report["conflicts"]) >= 1
    assert any(c["table"] == "complaints" and c["pk"] == 1 for c in conflict_report["conflicts"])
    assert conflict_report["ok"] is False

    pg_db.close()
