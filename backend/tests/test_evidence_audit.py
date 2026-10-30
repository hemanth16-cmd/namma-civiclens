import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app import evidence_audit
from app.evidence_audit import audit, main
from app.main import create_app
from tests.test_complaints import payload
from tests.test_evidence import JPEG

OLD = time.time() - 7200


def key_path(evidence_dir, key):
    return evidence_dir / "objects" / key[:2] / key


def place(evidence_dir, key, data=b"orphan", mtime=OLD):
    path = key_path(evidence_dir, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


def snapshot(root):
    """Hashes of everything that matters: all evidence files, the file outside the evidence folder and
    the main database file. SQLite's -wal/-shm sidecars are excluded (opening a WAL database, even
    read-only, creates them); database rows are compared separately with db_dump()."""
    files = [p for p in root.rglob("*") if p.is_file() and not p.name.endswith(("-wal", "-shm"))]
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def db_dump(db_path):
    with closing(sqlite3.connect(db_path)) as conn:
        return {t: conn.execute(f"SELECT * FROM {t} ORDER BY pk").fetchall() for t in ("complaints", "evidence", "idempotency_keys")}


@pytest.fixture
def scene(isolated_db_path, evidence_dir):
    """Two complaints with uploaded evidence, plus every kind of inconsistency."""
    with TestClient(create_app(db_path=isolated_db_path, evidence_dir=evidence_dir)) as api:
        keys = []
        for content in (JPEG, b"\xff\xd8\xff\xe1" + os.urandom(3000)):
            receipt = api.post("/api/v1/complaints", json=payload()).json()
            assert api.post(f"/api/v1/complaints/{receipt['id']}/evidence", files={"file": ("e.jpg", content, "image/jpeg")},
                            headers={"Authorization": f"Bearer {receipt['uploadToken']}"}).status_code == 201
    with closing(sqlite3.connect(isolated_db_path)) as conn:
        keys = [k for (k,) in conn.execute("SELECT storage_key FROM evidence ORDER BY pk")]
    for key in keys:  # referenced files are old enough to be candidates if they were orphans
        os.utime(key_path(evidence_dir, key), (OLD, OLD))
    key_path(evidence_dir, keys[1]).unlink()  # referenced but missing
    old_orphan = place(evidence_dir, "a" * 32)
    recent_orphan = place(evidence_dir, "b" * 32, mtime=time.time())
    unexpected = evidence_dir / "objects" / "notes.txt"
    unexpected.write_bytes(b"not a key")
    os.utime(unexpected, (OLD, OLD))
    stale_tmp, fresh_tmp = evidence_dir / "tmp" / "old.part", evidence_dir / "tmp" / "new.part"
    stale_tmp.write_bytes(b"x")
    fresh_tmp.write_bytes(b"y")
    os.utime(stale_tmp, (OLD, OLD))
    outside = evidence_dir.parent / ("c" * 32)
    outside.write_bytes(b"outside the evidence folder")
    os.utime(outside, (OLD, OLD))
    return {"keys": keys, "old_orphan": old_orphan, "recent_orphan": recent_orphan, "unexpected": unexpected,
            "stale_tmp": stale_tmp, "fresh_tmp": fresh_tmp, "outside": outside}


def test_dry_run_reports_everything_and_changes_nothing(scene, isolated_db_path, evidence_dir):
    files_before, db_before = snapshot(evidence_dir.parent), db_dump(isolated_db_path)
    report = audit(isolated_db_path, evidence_dir)
    assert report["mode"] == "dry-run"
    assert report["recordedFiles"] == 2 and report["filesOnDisk"] == 3
    assert {o["key"] for o in report["orphans"]} == {"a" * 32, "b" * 32}
    assert report["missingFiles"] == [scene["keys"][1]]
    assert [u["path"].replace("\\", "/") for u in report["unexpectedFiles"]] == ["objects/notes.txt"]
    assert {t["name"] for t in report["tempFiles"]} == {"old.part", "new.part"}
    assert report["deletedOrphans"] == [] and report["deletedTempFiles"] == []
    assert snapshot(evidence_dir.parent) == files_before
    assert db_dump(isolated_db_path) == db_before


def test_delete_mode_removes_only_old_unreferenced_key_files(scene, isolated_db_path, evidence_dir):
    db_before = db_dump(isolated_db_path)
    report = audit(isolated_db_path, evidence_dir, delete_orphans=True, min_age_seconds=3600)
    assert report["deletedOrphans"] == ["a" * 32]
    assert report["keptRecentOrphans"] == ["b" * 32]
    assert not scene["old_orphan"].exists()
    assert scene["recent_orphan"].exists(), "recent files are kept while an upload may be finishing"
    assert key_path(evidence_dir, scene["keys"][0]).exists(), "referenced files are never deleted"
    assert scene["unexpected"].exists() and scene["outside"].exists()
    assert scene["stale_tmp"].exists(), "temp files need their own flag"
    assert db_dump(isolated_db_path) == db_before, "database rows are never changed"
    assert audit(isolated_db_path, evidence_dir)["missingFiles"] == [scene["keys"][1]], "missing files are reported, not fixed"


def test_database_is_rechecked_before_each_deletion(scene, isolated_db_path, evidence_dir, monkeypatch):
    monkeypatch.setattr(evidence_audit, "_still_unreferenced", lambda db, key: False)  # an upload just recorded it
    report = audit(isolated_db_path, evidence_dir, delete_orphans=True, min_age_seconds=0)
    assert report["deletedOrphans"] == []
    assert scene["old_orphan"].exists() and scene["recent_orphan"].exists()


def test_stale_temp_files_are_removed_only_when_requested(scene, isolated_db_path, evidence_dir):
    report = audit(isolated_db_path, evidence_dir, delete_stale_temp=True, min_age_seconds=3600)
    assert report["deletedTempFiles"] == ["old.part"]
    assert not scene["stale_tmp"].exists() and scene["fresh_tmp"].exists()
    assert scene["old_orphan"].exists(), "orphans need --delete-orphans"


def test_cli_dry_run_json_and_exit_codes(scene, isolated_db_path, evidence_dir, capsys):
    before = snapshot(evidence_dir.parent)
    code = main(["--db", str(isolated_db_path), "--evidence-dir", str(evidence_dir), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert code == 1, "missing files make the audit exit non-zero"
    assert report["mode"] == "dry-run" and len(report["orphans"]) == 2
    assert snapshot(evidence_dir.parent) == before
    main(["--db", str(isolated_db_path), "--evidence-dir", str(evidence_dir)])
    assert "No changes were made" in capsys.readouterr().out


def test_cli_delete_requires_the_explicit_flag(scene, isolated_db_path, evidence_dir, capsys):
    main(["--db", str(isolated_db_path), "--evidence-dir", str(evidence_dir), "--min-age-minutes", "0"])
    assert scene["old_orphan"].exists() and scene["recent_orphan"].exists()
    main(["--db", str(isolated_db_path), "--evidence-dir", str(evidence_dir), "--delete-orphans", "--min-age-minutes", "0"])
    assert not scene["old_orphan"].exists() and not scene["recent_orphan"].exists()
    assert key_path(evidence_dir, scene["keys"][0]).exists()


def test_report_contains_no_secrets(scene, isolated_db_path, evidence_dir, capsys):
    with closing(sqlite3.connect(isolated_db_path)) as conn:
        hashes = [h for row in conn.execute("SELECT claim_hash, upload_token_hash FROM complaints") for h in row]
    main(["--db", str(isolated_db_path), "--evidence-dir", str(evidence_dir), "--json"])
    out = capsys.readouterr().out
    assert not any(h in out for h in hashes) and "scrypt" not in out and "13.100712" not in out


def test_missing_database_fails_without_changes(tmp_path, evidence_dir, capsys):
    place(evidence_dir, "d" * 32)
    assert main(["--db", str(tmp_path / "nope.sqlite3"), "--evidence-dir", str(evidence_dir), "--delete-orphans", "--min-age-minutes", "0"]) == 2
    assert key_path(evidence_dir, "d" * 32).exists(), "without a readable database nothing is treated as an orphan"
    assert not (tmp_path / "nope.sqlite3").exists(), "the database is opened read-only and never created"
