import hashlib
import logging
import os
import sqlite3
import time
from contextlib import closing
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from tests import dbutil

from app.evidence import UploadSettings, detect_kind
from app.evidence_storage import LocalEvidenceStorage
from app.main import DEFAULT_EVIDENCE_DIR, PUBLIC_DIR, create_app
from app.store import SCHEMA, SCHEMA_VERSION, ComplaintStore, StoreError
from tests.test_complaints import payload

# Generated demonstration media: valid leading signatures followed by filler bytes.
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + os.urandom(4000) + b"\xff\xd9"
WEBM = (b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\xf7\x81\x01\x42\xf2\x81\x04\x42\xf3\x81\x08"
        b"\x42\x82\x84webm\x42\x87\x81\x04\x42\x85\x81\x02" + os.urandom(6000))
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + os.urandom(6000)
PNG = b"\x89PNG\r\n\x1a\n" + os.urandom(500)
MOV = b"\x00\x00\x00\x14ftypqt  \x00\x00\x02\x00qt  " + os.urandom(500)
MKV = b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\x82\x88matroska" + os.urandom(500)


@pytest.fixture
def make_client(isolated_db_path, evidence_dir):
    clients = []

    def make(**kwargs):
        kwargs.setdefault("db_path", isolated_db_path)
        kwargs.setdefault("evidence_dir", evidence_dir)
        client = TestClient(create_app(**kwargs))
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def api(make_client):
    return make_client()


def create(api, media_type="photo"):
    response = api.post("/api/v1/complaints", json=payload(mediaType=media_type))
    assert response.status_code == 201, response.text
    return response.json()


def upload(api, receipt_or_id, content, filename="evidence.jpg", ctype="image/jpeg", token=None, data=None):
    public_id = receipt_or_id["id"] if isinstance(receipt_or_id, dict) else receipt_or_id
    token = token if token is not None else receipt_or_id["uploadToken"]
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return api.post(f"/api/v1/complaints/{public_id}/evidence", files={"file": (filename, content, ctype)}, data=data or {}, headers=headers)


def objects(evidence_dir):
    root = evidence_dir / "objects"
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def temp_files(evidence_dir):
    root = evidence_dir / "tmp"
    return sorted(root.iterdir()) if root.exists() else []


def evidence_rows(db_path):
    with closing(dbutil.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM evidence")]


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code, response.text
    return response.json()["error"]


def assert_nothing_stored(db_path, evidence_dir):
    assert evidence_rows(db_path) == []
    assert objects(evidence_dir) == []
    assert temp_files(evidence_dir) == []


# ---------- Successful uploads ----------

def test_photo_upload_stores_file_metadata_and_checksum(api, isolated_db_path, evidence_dir):
    receipt = create(api)
    response = upload(api, receipt, JPEG, data={"capturedAt": "2026-10-01T09:30:00+05:30"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "uploaded"
    assert body["evidenceId"].startswith("ev_")
    assert (body["mediaType"], body["contentType"], body["byteSize"]) == ("photo", "image/jpeg", len(JPEG))
    assert body["sha256"] == hashlib.sha256(JPEG).hexdigest()
    assert body["capturedAt"] == "2026-10-01T04:00:00.000Z"
    assert response.headers["cache-control"] == "no-store"
    assert set(body) == {"evidenceId", "status", "mediaType", "contentType", "byteSize", "sha256", "capturedAt", "uploadedAt"}

    [row] = evidence_rows(isolated_db_path)
    files = objects(evidence_dir)
    assert len(files) == 1 and files[0].read_bytes() == JPEG, "stored file is byte-identical to the upload"
    assert files[0].name == row["storage_key"] and len(row["storage_key"]) == 32
    assert files[0].parent.name == row["storage_key"][:2]
    for leaked in (row["storage_key"], str(evidence_dir), "objects", "evidence.jpg"):
        assert leaked not in response.text
    assert temp_files(evidence_dir) == []


@pytest.mark.parametrize("content, filename, ctype", [
    (WEBM, "evidence.webm", "video/webm;codecs=vp9"),
    (MP4, "evidence.mp4", "video/mp4"),
])
def test_supported_video_uploads(api, content, filename, ctype):
    receipt = create(api, "video")
    response = upload(api, receipt, content, filename, ctype)
    assert response.status_code == 201, response.text
    assert response.json()["mediaType"] == "video"
    assert response.json()["contentType"] == ctype.split(";")[0]
    assert response.json()["sha256"] == hashlib.sha256(content).hexdigest()


def test_octet_stream_or_missing_filename_is_accepted_when_the_signature_is_known(api):
    receipt = create(api)
    assert upload(api, receipt, JPEG, filename="blob", ctype="application/octet-stream").status_code == 201


# ---------- Rejected files ----------

@pytest.mark.parametrize("content, filename, ctype", [
    (PNG, "evidence.png", "image/png"),
    (MOV, "evidence.mov", "video/quicktime"),
    (MKV, "evidence.mkv", "video/x-matroska"),
    (b"GIF89a" + os.urandom(100), "evidence.gif", "image/gif"),
    (b"%PDF-1.7\n" + os.urandom(100), "evidence.jpg", "image/jpeg"),
])
def test_unsupported_types_are_rejected_by_signature(api, isolated_db_path, evidence_dir, content, filename, ctype):
    assert_error(upload(api, create(api), content, filename, ctype), 415, "unsupported_media_type")
    assert_nothing_stored(isolated_db_path, evidence_dir)


@pytest.mark.parametrize("filename, ctype", [
    ("evidence.webm", "image/jpeg"),
    ("evidence.png", "image/jpeg"),
    ("evidence.jpg", "video/webm"),
    ("evidence.jpg", "image/png"),
])
def test_extension_or_declared_type_must_match_the_signature(api, isolated_db_path, evidence_dir, filename, ctype):
    assert_error(upload(api, create(api), JPEG, filename, ctype), 422, "type_mismatch")
    assert_nothing_stored(isolated_db_path, evidence_dir)


def test_file_must_match_the_complaint_media_type(api, isolated_db_path, evidence_dir):
    assert_error(upload(api, create(api, "photo"), WEBM, "evidence.webm", "video/webm"), 422, "media_type_mismatch")
    assert_error(upload(api, create(api, "video"), JPEG), 422, "media_type_mismatch")
    assert_nothing_stored(isolated_db_path, evidence_dir)


def test_empty_file_is_rejected(api, isolated_db_path, evidence_dir):
    assert_error(upload(api, create(api), b""), 422, "empty_file")
    assert_nothing_stored(isolated_db_path, evidence_dir)


def test_tiny_file_with_a_valid_signature_is_accepted(api):
    assert upload(api, create(api), b"\xff\xd8\xff\xd9").status_code == 201


def test_oversized_file_is_rejected_while_streaming(make_client, isolated_db_path, evidence_dir):
    client = make_client(upload_settings=UploadSettings(max_photo_bytes=2048, max_video_bytes=4096))
    assert_error(upload(client, create(client), JPEG), 413, "payload_too_large")
    assert_nothing_stored(isolated_db_path, evidence_dir)


def test_declared_content_length_over_the_limit_is_rejected_before_reading(make_client, isolated_db_path, evidence_dir):
    client = make_client(upload_settings=UploadSettings(max_photo_bytes=1024, max_video_bytes=1024))
    receipt = create(client)
    big = b"\xff\xd8\xff" + b"\x00" * 200_000
    assert_error(upload(client, receipt, big), 413, "payload_too_large")
    assert_nothing_stored(isolated_db_path, evidence_dir)


def test_chunked_body_without_length_is_still_limited(make_client, isolated_db_path, evidence_dir):
    client = make_client(upload_settings=UploadSettings(max_photo_bytes=1024, max_video_bytes=1024))
    receipt = create(client)

    def body():
        yield b"--B\r\nContent-Disposition: form-data; name=\"file\"; filename=\"e.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8\xff"
        for _ in range(200):
            yield b"\x00" * 1024
        yield b"\r\n--B--\r\n"

    response = client.post(f"/api/v1/complaints/{receipt['id']}/evidence", content=body(),
                           headers={"Authorization": f"Bearer {receipt['uploadToken']}", "Content-Type": "multipart/form-data; boundary=B"})
    assert_error(response, 413, "payload_too_large")
    assert_nothing_stored(isolated_db_path, evidence_dir)


def raw_upload(api, receipt, body: bytes, ctype="multipart/form-data; boundary=B"):
    return api.post(f"/api/v1/complaints/{receipt['id']}/evidence", content=body,
                    headers={"Authorization": f"Bearer {receipt['uploadToken']}", "Content-Type": ctype})


def test_interrupted_upload_is_rejected_and_cleaned_up(api, isolated_db_path, evidence_dir):
    truncated = b"--B\r\nContent-Disposition: form-data; name=\"file\"; filename=\"e.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n" + JPEG[:3000]
    assert_error(raw_upload(api, create(api), truncated), 400, "incomplete_upload")
    assert_nothing_stored(isolated_db_path, evidence_dir)


def test_malformed_and_non_multipart_bodies_are_rejected(api, isolated_db_path, evidence_dir):
    receipt = create(api)
    assert_error(raw_upload(api, receipt, JPEG, ctype="image/jpeg"), 415, "unsupported_media_type")
    assert_error(raw_upload(api, receipt, b"not multipart at all"), 400, "bad_request")
    assert_nothing_stored(isolated_db_path, evidence_dir)


def test_form_must_contain_exactly_one_file_and_known_fields(api, isolated_db_path, evidence_dir):
    receipt = create(api)
    headers = {"Authorization": f"Bearer {receipt['uploadToken']}"}
    url = f"/api/v1/complaints/{receipt['id']}/evidence"
    assert_error(api.post(url, files=[("file", ("a.jpg", JPEG, "image/jpeg")), ("file", ("b.jpg", JPEG, "image/jpeg"))], headers=headers), 422, "validation_error")
    assert_error(api.post(url, files={"file": ("a.jpg", JPEG, "image/jpeg")}, data={"owner": "x"}, headers=headers), 422, "validation_error")
    assert_error(api.post(url, data={"capturedAt": "2026-10-01T04:00:00Z"}, files={"other": ("a.txt", b"x", "text/plain")}, headers=headers), 422, "validation_error")
    assert_nothing_stored(isolated_db_path, evidence_dir)


@pytest.mark.parametrize("captured", ["yesterday", "2026-10-30T10:00:00", "2999-01-01T00:00:00Z", "x" * 100])
def test_invalid_capture_time_is_rejected(api, isolated_db_path, evidence_dir, captured):
    assert_error(upload(api, create(api), JPEG, data={"capturedAt": captured}), 422, "validation_error")
    assert_nothing_stored(isolated_db_path, evidence_dir)


# ---------- Complaint and authorization checks ----------

@pytest.mark.parametrize("public_id", ["NCL-2026-ZZZZZZ", "not-an-id"])
def test_unknown_complaints_return_404(api, public_id):
    assert_error(upload(api, public_id, JPEG, token="A" * 43), 404, "not_found")


def test_upload_without_a_token_is_refused(api, isolated_db_path, evidence_dir):
    receipt = create(api)
    response = api.post(f"/api/v1/complaints/{receipt['id']}/evidence", files={"file": ("e.jpg", JPEG, "image/jpeg")})
    assert_error(response, 401, "unauthorized")
    assert response.headers["www-authenticate"] == "Bearer"
    assert_nothing_stored(isolated_db_path, evidence_dir)


def test_wrong_tokens_are_refused(api, isolated_db_path, evidence_dir):
    first, second = create(api), create(api)
    assert_error(upload(api, first, JPEG, token="B" * 43), 401, "unauthorized")
    assert_error(upload(api, first, JPEG, token=second["uploadToken"]), 401, "unauthorized")
    assert_error(upload(api, first, JPEG, token=first["claim"]), 401, "unauthorized")  # the claim code is not a token
    assert_nothing_stored(isolated_db_path, evidence_dir)


def test_expired_tokens_are_refused(make_client, isolated_db_path, evidence_dir):
    store = ComplaintStore(isolated_db_path, upload_token_ttl=timedelta(seconds=-1))
    client = make_client(db_path=None, store=store)
    assert_error(upload(client, create(client), JPEG), 401, "token_expired")
    assert_nothing_stored(isolated_db_path, evidence_dir)


def test_claim_code_exchanges_for_a_new_token_and_revokes_the_old_one(api):
    receipt = create(api)
    response = api.post(f"/api/v1/complaints/{receipt['id']}/upload-token", json={"claim": receipt["claim"]})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    fresh = response.json()["uploadToken"]
    assert fresh != receipt["uploadToken"] and response.json()["uploadTokenExpiresAt"].endswith("Z")
    assert_error(upload(api, receipt, JPEG), 401, "unauthorized")
    assert upload(api, receipt, JPEG, token=fresh).status_code == 201


def test_token_exchange_requires_the_right_claim_code(api):
    first, second = create(api), create(api)
    url = f"/api/v1/complaints/{first['id']}/upload-token"
    assert_error(api.post(url, json={"claim": second["claim"]}), 401, "unauthorized")
    assert_error(api.post(url, json={"claim": "guess"}), 401, "unauthorized")
    assert_error(api.post(url, json={}), 422, "validation_error")
    assert_error(api.post("/api/v1/complaints/NCL-2026-ZZZZZZ/upload-token", json={"claim": first["claim"]}), 404, "not_found")
    assert upload(api, first, JPEG).status_code == 201, "failed exchanges do not revoke the current token"


def test_expired_token_can_be_replaced_with_the_claim_code(make_client, isolated_db_path):
    client = make_client(store=ComplaintStore(isolated_db_path, upload_token_ttl=timedelta(seconds=-1)), db_path=None)
    receipt = create(client)
    assert_error(upload(client, receipt, JPEG), 401, "token_expired")
    client.app.state.store.upload_token_ttl = timedelta(hours=1)
    fresh = client.post(f"/api/v1/complaints/{receipt['id']}/upload-token", json={"claim": receipt["claim"]}).json()["uploadToken"]
    assert upload(client, receipt, JPEG, token=fresh).status_code == 201


def test_only_token_hashes_are_stored(api, isolated_db_path):
    receipt = create(api)
    with closing(dbutil.connect(isolated_db_path)) as conn:
        (stored,) = conn.execute("SELECT upload_token_hash FROM complaints").fetchone()
    assert stored == hashlib.sha256(receipt["uploadToken"].encode()).hexdigest()
    for f in isolated_db_path.parent.glob(isolated_db_path.name + "*"):
        assert receipt["uploadToken"].encode() not in f.read_bytes()


# ---------- Persistence, retries and failures ----------

def test_evidence_metadata_survives_restart(make_client, isolated_db_path, evidence_dir):
    client = make_client()
    receipt = create(client)
    body = upload(client, receipt, JPEG).json()
    client.__exit__(None, None, None)
    again = make_client()
    second = upload(again, receipt, JPEG)
    assert second.status_code == 200 and second.json()["evidenceId"] == body["evidenceId"]
    [row] = evidence_rows(isolated_db_path)
    assert row["sha256"] == body["sha256"] and row["byte_size"] == len(JPEG)
    assert (evidence_dir / "objects" / row["storage_key"][:2] / row["storage_key"]).read_bytes() == JPEG


def test_retrying_the_same_file_is_idempotent(api, isolated_db_path, evidence_dir):
    receipt = create(api)
    first = upload(api, receipt, JPEG)
    second = upload(api, receipt, JPEG)
    assert first.status_code == 201 and second.status_code == 200
    assert second.json()["status"] == "already_uploaded"
    assert second.json()["evidenceId"] == first.json()["evidenceId"]
    assert len(evidence_rows(isolated_db_path)) == 1 and len(objects(evidence_dir)) == 1
    assert temp_files(evidence_dir) == []


def test_a_different_file_cannot_replace_existing_evidence(api, isolated_db_path, evidence_dir):
    receipt = create(api)
    upload(api, receipt, JPEG)
    other = b"\xff\xd8\xff\xe1" + os.urandom(3000)
    assert_error(upload(api, receipt, other), 409, "evidence_exists")
    [row] = evidence_rows(isolated_db_path)
    assert row["sha256"] == hashlib.sha256(JPEG).hexdigest()
    assert len(objects(evidence_dir)) == 1 and temp_files(evidence_dir) == []


def test_concurrent_retry_race_keeps_one_record(api, isolated_db_path, evidence_dir, monkeypatch):
    receipt = create(api)
    store = api.app.state.store
    monkeypatch.setattr(store, "get_evidence", lambda pk, role="original": None)  # both requests pass the pre-check
    assert upload(api, receipt, JPEG).status_code == 201
    retry = upload(api, receipt, JPEG)
    assert retry.status_code == 200 and retry.json()["status"] == "already_uploaded"
    assert len(evidence_rows(isolated_db_path)) == 1 and len(objects(evidence_dir)) == 1


def test_storage_failure_while_starting_returns_503(api, isolated_db_path, evidence_dir, monkeypatch):
    receipt = create(api)
    storage = api.app.state.evidence_storage
    monkeypatch.setattr(storage, "begin", lambda: (_ for _ in ()).throw(StoreError("disk full")))
    response = upload(api, receipt, JPEG)
    assert_error(response, 503, "storage_unavailable")
    assert "disk full" not in response.text
    assert_nothing_stored(isolated_db_path, evidence_dir)


def test_failed_rename_cleans_up_the_partial_file(api, isolated_db_path, evidence_dir, monkeypatch):
    receipt = create(api)
    monkeypatch.setattr("app.evidence_storage.os.replace", lambda *a: (_ for _ in ()).throw(OSError("rename failed")))
    assert_error(upload(api, receipt, JPEG), 503, "storage_unavailable")
    assert_nothing_stored(isolated_db_path, evidence_dir)


def test_database_failure_after_storing_deletes_the_file(api, isolated_db_path, evidence_dir, monkeypatch):
    receipt = create(api)
    monkeypatch.setattr(api.app.state.store, "add_evidence", lambda *a: (_ for _ in ()).throw(StoreError("db locked")))
    response = upload(api, receipt, JPEG)
    assert_error(response, 503, "storage_unavailable")
    assert "locked" not in response.text
    assert_nothing_stored(isolated_db_path, evidence_dir)


def test_stale_temporary_files_are_removed_at_startup(tmp_path):
    storage = LocalEvidenceStorage(tmp_path / "ev")
    storage.initialize()
    old, new = tmp_path / "ev" / "tmp" / "old.part", tmp_path / "ev" / "tmp" / "new.part"
    old.write_bytes(b"x")
    new.write_bytes(b"y")
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    storage.initialize()
    assert not old.exists() and new.exists()


# ---------- Storage location and privacy ----------

def test_storage_keys_cannot_escape_the_root(tmp_path):
    storage = LocalEvidenceStorage(tmp_path / "ev")
    for bad in ("../../etc/passwd", "..", "ab/../../x", "A" * 32, "", "0" * 31, None):
        with pytest.raises(StoreError):
            storage.path_for(bad)


def test_storage_inside_the_public_frontend_folder_is_refused():
    app = create_app(evidence_dir=PUBLIC_DIR / "uploads")
    with pytest.raises(StoreError):
        with TestClient(app):
            pass
    assert not (PUBLIC_DIR / "uploads").exists()


def test_default_storage_is_outside_the_public_folder():
    assert PUBLIC_DIR.resolve() not in DEFAULT_EVIDENCE_DIR.resolve().parents
    assert DEFAULT_EVIDENCE_DIR.resolve() != PUBLIC_DIR.resolve()


def test_tests_use_their_own_storage_directory(api, evidence_dir):
    assert api.app.state.evidence_storage.root == evidence_dir.resolve()
    assert DEFAULT_EVIDENCE_DIR.resolve() != evidence_dir.resolve()


def test_public_tracking_exposes_no_evidence_details(api, isolated_db_path):
    receipt = create(api)
    body = upload(api, receipt, JPEG, data={"capturedAt": "2026-10-01T04:00:00Z"}).json()
    [row] = evidence_rows(isolated_db_path)
    public = api.get(f"/api/v1/complaints/{receipt['id']}").text
    for leaked in (row["storage_key"], body["evidenceId"], body["sha256"], "objects", "evidence", "storage",
                   receipt["uploadToken"], receipt["claim"], "url", "13.100712"):
        assert leaked not in public, leaked


def test_no_static_route_serves_stored_files(api, isolated_db_path):
    receipt = create(api)
    upload(api, receipt, JPEG)
    [row] = evidence_rows(isolated_db_path)
    for path in (f"/objects/{row['storage_key'][:2]}/{row['storage_key']}", f"/api/v1/evidence/{row['storage_key']}",
                 f"/api/v1/complaints/{receipt['id']}/evidence"):
        assert api.get(path).status_code in (404, 405)


def test_uploads_do_not_log_tokens_claims_or_filenames(api, caplog):
    receipt = create(api)
    with caplog.at_level(logging.DEBUG):
        upload(api, receipt, JPEG, filename="my-street-photo.jpg")
        upload(api, receipt, PNG, filename="private.png", ctype="image/png")
    logged = caplog.text
    for secret in (receipt["uploadToken"], receipt["claim"], "my-street-photo", "private.png"):
        assert secret not in logged


def test_signature_detection():
    assert detect_kind(JPEG[:64]).content_type == "image/jpeg"
    assert detect_kind(WEBM[:64]).content_type == "video/webm"
    assert detect_kind(MP4[:64]).content_type == "video/mp4"
    for other in (PNG, MOV, MKV, b"", b"\xff\xd8"):
        assert detect_kind(other[:64]) is None


# ---------- Schema migration ----------

@pytest.mark.sqlite_only
def test_version_1_database_is_migrated_in_place(tmp_path):
    db = tmp_path / "old.sqlite3"
    with closing(dbutil.connect(db)) as conn:
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA user_version = 1")
        conn.execute("INSERT INTO complaints (public_id, claim_hash, title, category, area, media_type, share_evidence, gps_lat, gps_lng, "
                     "gps_source_type, created_at, uploaded_at, updated_at, status, verification, department) "
                     "VALUES ('NCL-2026-OLD001', 'scrypt$x', 't', 'Potholes', 'a', 'photo', 0, 13, 77, 'gps', 'z', 'z', 'z', 'Submitted', 'Pending Verification', 'd')")
        conn.commit()
    store = ComplaintStore(db)
    store.initialize()
    store.initialize()  # idempotent
    with closing(dbutil.connect(db)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 7
    assert store.db.features() == {"duplicate_detection": "applied", "resident_accounts": "applied"}
    with closing(dbutil.connect(db)) as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(complaints)")}
        assert {"upload_token_hash", "upload_token_expires_at", "credentials_used_at"} <= columns
        assert conn.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0] == 0
        evidence_columns = {r[1]: r for r in conn.execute("PRAGMA table_info(evidence)")}
        assert evidence_columns["storage_backend"][4] == "'local'", "existing evidence defaults to local storage"
        for table in ("staff_accounts", "staff_sessions", "evidence_access_log", "notification_recipients", "notification_follows",
                      "push_subscriptions", "notifications"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
    assert store.get("NCL-2026-OLD001")["category"] == "Potholes"
    assert store.check_upload_token("NCL-2026-OLD001", "anything")["status"] == "invalid", "old complaints have no token until one is issued"
