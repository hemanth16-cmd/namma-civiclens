"""Supabase Storage evidence backend, tested against an in-process fake of the Storage REST API.

The fake (httpx.MockTransport) implements the endpoints that storage3 and the streaming read use,
checks the service-role headers on every request, and can simulate outages and errors. No real
Supabase project, credentials or network calls are involved.
"""

import base64
import email.parser
import email.policy
import hashlib
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from app import auth, supabase_storage
from app.evidence_storage import LocalEvidenceStorage
from app.main import create_app
from app.store import StoreError
from app.supabase_storage import StorageConfigError, SupabaseEvidenceStorage, SupabaseSettings, selected_backend
from tests import dbutil
from tests.test_complaints import payload
from tests.test_evidence import JPEG, WEBM

URL = "https://abcdefghijklmnopqrst.supabase.co"
SERVICE_KEY = "sb_secret_test-service-role-key-never-logged"
BUCKET = "civiclens-evidence"
PASSWORD = "correct horse battery"
MB = 1024 * 1024


def jwt(role):
    part = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{part({'alg': 'HS256'})}.{part({'role': role, 'iss': 'supabase'})}.signature"


class FakeSupabaseStorage:
    """Dictionary-backed stand-in for /storage/v1."""

    def __init__(self):
        self.key = SERVICE_KEY
        self.buckets = {BUCKET: {"public": False, "file_size_limit": None, "allowed_mime_types": None}}
        self.objects = {}            # (bucket, path) -> {"data", "content_type", "cache_control", "metadata"}
        self.requests = []           # (method, path, headers)
        self.outage = False          # every request raises a connection error
        self.fail = {}               # operation -> HTTP status to return once
        self.info_size_override = None

    @staticmethod
    def error(http_status, status_code, error, message):
        return httpx.Response(http_status, json={"statusCode": str(status_code), "error": error, "message": message})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path, dict(request.headers)))
        if self.outage:
            raise httpx.ConnectError("connection refused", request=request)
        if request.headers.get("apikey") != self.key or request.headers.get("authorization") != f"Bearer {self.key}":
            return self.error(400, 403, "Unauthorized", "invalid signature")
        assert path.startswith("/storage/v1/"), path
        route = path[len("/storage/v1/"):]
        op = None
        if request.method == "GET" and route.startswith("bucket/"):
            name = route.split("/", 1)[1]
            if name not in self.buckets:
                return self.error(400, 404, "Bucket not found", "Bucket not found")
            b = self.buckets[name]
            return httpx.Response(200, json={"id": name, "name": name, "owner": "", "public": b["public"],
                                             "file_size_limit": b["file_size_limit"], "allowed_mime_types": b["allowed_mime_types"],
                                             "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-01T00:00:00Z"})
        if request.method == "POST" and route.startswith("object/list/"):
            op = "list"
            bucket = route.split("/", 2)[2]
            body = json.loads(request.content)
            prefix = body["prefix"].rstrip("/") + "/"
            entries = {}
            for (b, p), obj in sorted(self.objects.items()):
                if b != bucket or not p.startswith(prefix):
                    continue
                rest = p[len(prefix):]
                if "/" in rest:
                    entries.setdefault(rest.split("/")[0], {"name": rest.split("/")[0], "id": None, "metadata": None})
                else:
                    entries[rest] = {"name": rest, "id": "obj-" + rest, "metadata": {"size": len(obj["data"]), "mimetype": obj["content_type"]}}
            items = list(entries.values())[body["offset"]: body["offset"] + body["limit"]]
            return httpx.Response(200, json=items)
        if route.startswith("object/info/"):
            bucket, _, key = route[len("object/info/"):].partition("/")
            obj = self.objects.get((bucket, key))
            if obj is None:
                return self.error(400, 404, "not_found", "Object not found")
            size = self.info_size_override if self.info_size_override is not None else len(obj["data"])
            return httpx.Response(200, json={"name": key, "size": size, "content_type": obj["content_type"], "metadata": obj["metadata"]})
        if route.startswith("object/"):
            bucket, _, key = route[len("object/"):].partition("/")
            if bucket not in self.buckets:
                return self.error(400, 404, "Bucket not found", "Bucket not found")
            if request.method == "POST":
                op = "upload"
                if op in self.fail:
                    return self.error(self.fail.pop(op), self.fail_status_code, "InternalError", "storage backend unavailable")
                if (bucket, key) in self.objects:
                    return self.error(400, 409, "Duplicate", "The resource already exists")
                self.objects[(bucket, key)] = self._parse_upload(request)
                return httpx.Response(200, json={"Key": f"{bucket}/{key}", "Id": "uuid-" + key[-6:]})
            if request.method == "DELETE" and not key:
                op = "remove"
                if op in self.fail:
                    return self.error(self.fail.pop(op), 500, "InternalError", "delete failed")
                removed = []
                for p in json.loads(request.content)["prefixes"]:
                    if self.objects.pop((bucket, p), None) is not None:
                        removed.append({"name": p})
                return httpx.Response(200, json=removed)
            obj = self.objects.get((bucket, key))
            if request.method == "HEAD":
                return httpx.Response(200 if obj else 400)
            if request.method == "GET":
                if obj is None:
                    return self.error(400, 404, "not_found", "Object not found")
                return httpx.Response(200, content=obj["data"], headers={"content-type": obj["content_type"]})
        raise AssertionError(f"unexpected request {request.method} {path}")

    fail_status_code = 500

    @staticmethod
    def _parse_upload(request):
        message = email.parser.BytesParser(policy=email.policy.default).parsebytes(
            b"Content-Type: " + request.headers["content-type"].encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + request.read())
        fields, data, ctype = {}, None, None
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if name == "file":
                data, ctype = part.get_payload(decode=True), part.get_content_type()
            else:
                fields[name] = part.get_payload(decode=True).decode()
        return {"data": data, "content_type": ctype, "cache_control": fields.get("cacheControl"),
                "metadata": json.loads(fields["metadata"]) if "metadata" in fields else None,
                "upsert": request.headers.get("x-upsert")}


@pytest.fixture
def fake(monkeypatch):
    server = FakeSupabaseStorage()
    monkeypatch.setattr(supabase_storage, "_transport", lambda: httpx.MockTransport(server))
    return server


@pytest.fixture
def supabase_env(monkeypatch, fake):
    monkeypatch.setenv("SUPABASE_URL", URL)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", SERVICE_KEY)
    monkeypatch.setenv("SUPABASE_STORAGE_BUCKET", BUCKET)
    return fake


@pytest.fixture
def storage(supabase_env, tmp_path):
    s = SupabaseEvidenceStorage(SupabaseSettings.from_env(), temp_dir=tmp_path / "tmp", max_object_bytes=200 * MB)
    s.initialize()
    return s


def upload_via(storage, data, content_type="image/jpeg"):
    pending = storage.begin()
    pending.content_type = content_type
    for i in range(0, len(data), 65536):
        pending.write(data[i:i + 65536])
    return pending, pending.commit()


def read_all(reader):
    data = b"".join(iter(lambda: reader.read(65536), b""))
    reader.close()
    return data


# ---------- Configuration ----------

def test_backend_selection(monkeypatch):
    assert selected_backend() == "local"
    monkeypatch.setenv("EVIDENCE_STORAGE_BACKEND", " Supabase ")
    assert selected_backend() == "supabase"
    for retired in ("r2", "s3", "aws", "gcs"):
        monkeypatch.setenv("EVIDENCE_STORAGE_BACKEND", retired)
        with pytest.raises(StorageConfigError, match='"local" or "supabase"'):
            selected_backend()


def test_settings_from_environment(supabase_env, monkeypatch):
    settings = SupabaseSettings.from_env()
    assert settings.storage_url == f"{URL}/storage/v1" and settings.bucket == BUCKET
    assert SERVICE_KEY not in repr(settings), "the service-role key is never in repr"
    monkeypatch.setenv("SUPABASE_URL", "http://127.0.0.1:54321")
    assert SupabaseSettings.from_env().storage_url == "http://127.0.0.1:54321/storage/v1", "local Supabase CLI over http is allowed"
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", jwt("service_role"))
    assert SupabaseSettings.from_env().service_role_key.count(".") == 2


@pytest.mark.parametrize("changes, message", [
    ({"SUPABASE_URL": None}, "SUPABASE_URL"),
    ({"SUPABASE_URL": "http://abcdefghijklmnopqrst.supabase.co"}, "SUPABASE_URL"),
    ({"SUPABASE_URL": f"{URL}/rest/v1"}, "SUPABASE_URL"),
    ({"SUPABASE_URL": "https://user:pw@abc.supabase.co"}, "SUPABASE_URL"),
    ({"SUPABASE_SERVICE_ROLE_KEY": None}, "SUPABASE_SERVICE_ROLE_KEY"),
    ({"SUPABASE_SERVICE_ROLE_KEY": "has space"}, "whitespace"),
    ({"SUPABASE_SERVICE_ROLE_KEY": "sb_publishable_abc123"}, "anon/publishable"),
    ({"SUPABASE_SERVICE_ROLE_KEY": jwt("anon")}, "anon/publishable"),
    ({"SUPABASE_STORAGE_BUCKET": None}, "SUPABASE_STORAGE_BUCKET"),
    ({"SUPABASE_STORAGE_BUCKET": "bad bucket/name"}, "not a valid bucket name"),
])
def test_invalid_configuration_is_rejected(supabase_env, monkeypatch, changes, message):
    for name, value in changes.items():
        monkeypatch.delenv(name, raising=False) if value is None else monkeypatch.setenv(name, value)
    with pytest.raises(StorageConfigError, match=message) as err:
        SupabaseSettings.from_env()
    assert SERVICE_KEY not in str(err.value)


def test_app_refuses_to_build_with_incomplete_settings(supabase_env, monkeypatch):
    monkeypatch.setenv("EVIDENCE_STORAGE_BACKEND", "supabase")
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY")
    with pytest.raises(StorageConfigError, match="SUPABASE_SERVICE_ROLE_KEY"):
        create_app()


def test_every_request_carries_the_service_role_headers(storage, fake):
    upload_via(storage, JPEG)
    assert fake.requests
    for _, _, headers in fake.requests:
        assert headers["apikey"] == SERVICE_KEY and headers["authorization"] == f"Bearer {SERVICE_KEY}"


# ---------- Startup checks ----------

def test_startup_accepts_a_private_bucket(storage, fake):
    assert ("GET", f"/storage/v1/bucket/{BUCKET}") in [(m, p) for m, p, _ in fake.requests]


def test_startup_rejects_a_missing_bucket(supabase_env, monkeypatch, tmp_path):
    monkeypatch.setenv("SUPABASE_STORAGE_BUCKET", "no-such-bucket")
    with pytest.raises(StoreError, match="does not exist"):
        SupabaseEvidenceStorage(SupabaseSettings.from_env(), tmp_path, 200 * MB).initialize()


def test_startup_rejects_a_wrong_key(supabase_env, tmp_path):
    supabase_env.key = "a-different-key"
    with pytest.raises(StoreError, match="service-role key was rejected") as err:
        SupabaseEvidenceStorage(SupabaseSettings.from_env(), tmp_path, 200 * MB).initialize()
    assert SERVICE_KEY not in str(err.value)


def test_startup_reports_an_unreachable_project(supabase_env, tmp_path):
    supabase_env.outage = True
    with pytest.raises(StoreError, match="could not be reached"):
        SupabaseEvidenceStorage(SupabaseSettings.from_env(), tmp_path, 200 * MB).initialize()


def test_startup_refuses_a_public_bucket(supabase_env, tmp_path):
    supabase_env.buckets[BUCKET]["public"] = True
    with pytest.raises(StoreError, match="is public"):
        SupabaseEvidenceStorage(SupabaseSettings.from_env(), tmp_path, 200 * MB).initialize()


def test_startup_refuses_limits_that_would_reject_evidence(supabase_env, tmp_path):
    supabase_env.buckets[BUCKET]["file_size_limit"] = 50 * MB  # the Supabase free-plan maximum
    with pytest.raises(StoreError, match="CIVICLENS_MAX_VIDEO_BYTES"):
        SupabaseEvidenceStorage(SupabaseSettings.from_env(), tmp_path, 200 * MB).initialize()
    SupabaseEvidenceStorage(SupabaseSettings.from_env(), tmp_path, 50 * MB).initialize()  # lowered API limit: fine
    supabase_env.buckets[BUCKET]["allowed_mime_types"] = ["image/jpeg"]
    with pytest.raises(StoreError, match="video/webm, video/mp4"):
        SupabaseEvidenceStorage(SupabaseSettings.from_env(), tmp_path, 50 * MB).initialize()
    supabase_env.buckets[BUCKET]["allowed_mime_types"] = ["image/*", "video/*"]
    SupabaseEvidenceStorage(SupabaseSettings.from_env(), tmp_path, 50 * MB).initialize()


# ---------- Adapter ----------

def test_photo_upload_stores_a_private_object(storage, fake, tmp_path):
    pending, key = upload_via(storage, JPEG)
    obj = fake.objects[(BUCKET, f"evidence/{key[:2]}/{key}")]
    assert obj["data"] == JPEG and obj["content_type"] == "image/jpeg"
    assert obj["cache_control"] == "no-store" and obj["upsert"] == "false"
    assert obj["metadata"] == {"sha256": hashlib.sha256(JPEG).hexdigest()} and pending.sha256 == obj["metadata"]["sha256"]
    assert list((tmp_path / "tmp").iterdir()) == [], "the staged copy is removed"


def test_video_upload_and_streaming_retrieval(storage, fake):
    data = WEBM + os.urandom(3 * MB)
    _, key = upload_via(storage, data, "video/webm")
    assert fake.objects[(BUCKET, f"evidence/{key[:2]}/{key}")]["content_type"] == "video/webm"
    reader = storage.open_read(key)
    first = reader.read()
    assert 0 < len(first) <= 64 * 1024, "streamed in chunks, not loaded whole"
    assert first + read_all(reader) == data


def test_exists_delete_and_missing_objects(storage, fake):
    _, key = upload_via(storage, JPEG)
    assert storage.exists(key)
    storage.delete(key)
    assert not storage.exists(key) and fake.objects == {}
    with pytest.raises(StoreError, match="missing"):
        storage.open_read(key)
    storage.delete(key)  # deleting an absent object is not an error


def test_failed_upload_leaves_nothing(storage, fake, tmp_path):
    fake.fail["upload"] = 500
    pending = storage.begin()
    pending.write(JPEG)
    with pytest.raises(StoreError, match="500") as err:
        pending.commit()
    assert SERVICE_KEY not in str(err.value) and URL not in str(err.value)
    assert fake.objects == {} and list((tmp_path / "tmp").iterdir()) == []


def test_network_failure_during_upload(storage, fake, tmp_path):
    pending = storage.begin()
    pending.write(JPEG)
    fake.outage = True
    with pytest.raises(StoreError, match="ConnectError"):
        pending.commit()
    assert fake.objects == {} and list((tmp_path / "tmp").iterdir()) == []


def test_unconfirmed_upload_is_removed(storage, fake):
    fake.info_size_override = 1
    pending = storage.begin()
    pending.write(JPEG)
    with pytest.raises(StoreError, match="could not be confirmed"):
        pending.commit()
    assert fake.objects == {}


def test_keys_cannot_escape_the_prefix(storage):
    for bad in ("../../etc/passwd", "A" * 32, "", None, "ab/../cd" + "0" * 24):
        with pytest.raises(StoreError):
            storage.object_name(bad)


def test_stored_bytes_walks_folders_with_paging(storage, fake, monkeypatch):
    monkeypatch.setattr(supabase_storage, "LIST_PAGE", 2)
    sizes = []
    for i in range(5):
        data = JPEG + os.urandom(i)
        upload_via(storage, data)
        sizes.append(len(data))
    fake.objects[(BUCKET, "other/file.bin")] = {"data": b"x" * 999, "content_type": "application/octet-stream", "cache_control": None, "metadata": None}
    assert storage.stored_bytes() == sum(sizes)


def test_errors_never_contain_the_key(storage, fake):
    fake.outage = True
    for call in (lambda: storage.open_read("a" * 32), lambda: storage.exists("a" * 32), lambda: storage.delete("a" * 32), storage.stored_bytes):
        with pytest.raises(StoreError) as err:
            call()
        assert SERVICE_KEY not in str(err.value)


# ---------- Through the API (database: SQLite by default, PostgreSQL in the PostgreSQL test mode) ----------

def rows(db, sql):
    conn = dbutil.connect(db)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def staff_token(client):
    client.app.state.store.create_staff("asha", "Asha", auth.hash_password(PASSWORD))
    return client.post("/api/v1/staff/sessions", json={"username": "asha", "password": PASSWORD}).json()["token"]


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def new_complaint(client, content=JPEG, filename="e.jpg", ctype="image/jpeg", media="photo"):
    receipt = client.post("/api/v1/complaints", json=payload(mediaType=media)).json()
    response = client.post(f"/api/v1/complaints/{receipt['id']}/evidence", files={"file": (filename, content, ctype)},
                           headers=bearer(receipt["uploadToken"]))
    return receipt, response


@pytest.fixture
def supabase_api(supabase_env, monkeypatch, isolated_db_path, evidence_dir):
    monkeypatch.setenv("EVIDENCE_STORAGE_BACKEND", "supabase")
    with TestClient(create_app(db_path=isolated_db_path, evidence_dir=evidence_dir), client=("10.0.0.8", 1)) as client:
        yield client


def test_upload_view_and_audit_in_supabase_mode(supabase_api, fake, isolated_db_path, evidence_dir):
    assert supabase_api.app.state.evidence_storage.name == "supabase"
    receipt, response = new_complaint(supabase_api)
    assert response.status_code == 201, response.text
    [(key, backend, size)] = rows(isolated_db_path, "SELECT storage_key, storage_backend, byte_size FROM evidence")
    assert (backend, size) == ("supabase", len(JPEG)), "metadata in the database"
    assert fake.objects[(BUCKET, f"evidence/{key[:2]}/{key}")]["data"] == JPEG, "file in Supabase Storage"
    assert not any(p.is_file() for p in evidence_dir.rglob("*")), "nothing left on local disk"

    token = staff_token(supabase_api)
    viewed = supabase_api.get(f"/api/v1/staff/complaints/{receipt['id']}/evidence", headers=bearer(token))
    assert viewed.status_code == 200 and viewed.content == JPEG
    assert viewed.headers["cache-control"] == "no-store, private"
    assert rows(isolated_db_path, "SELECT action, client_ip FROM evidence_access_log") == [("view", "10.0.0.8")]

    anonymous = supabase_api.get(f"/api/v1/staff/complaints/{receipt['id']}/evidence")
    assert anonymous.status_code == 401 and len(rows(isolated_db_path, "SELECT pk FROM evidence_access_log")) == 1

    public = supabase_api.get(f"/api/v1/complaints/{receipt['id']}").text
    detail = supabase_api.get(f"/api/v1/staff/complaints/{receipt['id']}", headers=bearer(token)).text
    for leaked in (key, BUCKET, URL, SERVICE_KEY, "supabase", "evidence/"):
        assert leaked not in public, leaked
    for leaked in (key, BUCKET, URL, SERVICE_KEY, "storageKey", "storageBackend"):
        assert leaked not in detail, leaked


def test_video_upload_in_supabase_mode(supabase_api, fake):
    _, response = new_complaint(supabase_api, WEBM, "e.webm", "video/webm", "video")
    assert response.status_code == 201 and response.json()["contentType"] == "video/webm"
    [obj] = fake.objects.values()
    assert obj["data"] == WEBM and obj["content_type"] == "video/webm"


def test_retries_are_idempotent(supabase_api, fake):
    receipt, first = new_complaint(supabase_api)
    again = supabase_api.post(f"/api/v1/complaints/{receipt['id']}/evidence", files={"file": ("e.jpg", JPEG, "image/jpeg")}, headers=bearer(receipt["uploadToken"]))
    other = supabase_api.post(f"/api/v1/complaints/{receipt['id']}/evidence", files={"file": ("e.jpg", b"\xff\xd8\xff\xe1" + os.urandom(2000), "image/jpeg")}, headers=bearer(receipt["uploadToken"]))
    assert (first.status_code, again.status_code, other.status_code) == (201, 200, 409)
    assert len(fake.objects) == 1


def test_outage_during_upload_reports_failure_and_records_nothing(supabase_api, fake, isolated_db_path):
    receipt = supabase_api.post("/api/v1/complaints", json=payload()).json()
    fake.outage = True
    response = supabase_api.post(f"/api/v1/complaints/{receipt['id']}/evidence", files={"file": ("e.jpg", JPEG, "image/jpeg")}, headers=bearer(receipt["uploadToken"]))
    assert response.status_code == 503 and response.json()["error"]["code"] == "storage_unavailable"
    assert SERVICE_KEY not in response.text and URL not in response.text
    assert rows(isolated_db_path, "SELECT COUNT(*) FROM evidence") == [(0,)]
    assert supabase_api.app.state.evidence_quota.reserved == 0
    fake.outage = False
    retry = supabase_api.post(f"/api/v1/complaints/{receipt['id']}/evidence", files={"file": ("e.jpg", JPEG, "image/jpeg")}, headers=bearer(receipt["uploadToken"]))
    assert retry.status_code == 201, "a retry after the outage succeeds against the same complaint"


def test_database_failure_after_upload_removes_the_object(supabase_api, fake, monkeypatch):
    monkeypatch.setattr(supabase_api.app.state.store, "add_evidence", lambda *a: (_ for _ in ()).throw(StoreError("db down")))
    _, response = new_complaint(supabase_api)
    assert response.status_code == 503
    assert fake.objects == {}, "compensating delete: no object without a database record"


def test_database_failure_and_failed_cleanup_still_reports_failure(supabase_api, fake, monkeypatch, caplog):
    monkeypatch.setattr(supabase_api.app.state.store, "add_evidence", lambda *a: (_ for _ in ()).throw(StoreError("db down")))
    fake.fail["remove"] = 500
    _, response = new_complaint(supabase_api)
    assert response.status_code == 503
    assert len(fake.objects) == 1, "the unreferenced object remains (never served)"
    assert "Supabase evidence delete failed" in caplog.text and SERVICE_KEY not in caplog.text


def test_missing_object_returns_503_and_logs_nothing(supabase_api, fake, isolated_db_path):
    receipt, _ = new_complaint(supabase_api)
    fake.objects.clear()
    token = staff_token(supabase_api)
    response = supabase_api.get(f"/api/v1/staff/complaints/{receipt['id']}/evidence", headers=bearer(token))
    assert response.status_code == 503
    assert rows(isolated_db_path, "SELECT COUNT(*) FROM evidence_access_log") == [(0,)]


def test_local_evidence_stays_readable_after_switching(supabase_env, fake, monkeypatch, isolated_db_path, evidence_dir):
    monkeypatch.setenv("EVIDENCE_STORAGE_BACKEND", "local")
    with TestClient(create_app(db_path=isolated_db_path, evidence_dir=evidence_dir)) as local_app:
        old, response = new_complaint(local_app)
        assert response.status_code == 201
    monkeypatch.setenv("EVIDENCE_STORAGE_BACKEND", "supabase")
    with TestClient(create_app(db_path=isolated_db_path, evidence_dir=evidence_dir)) as app:
        token = staff_token(app)
        assert app.get(f"/api/v1/staff/complaints/{old['id']}/evidence", headers=bearer(token)).content == JPEG
        _, response = new_complaint(app)
        assert response.status_code == 201
        assert sorted(b for (b,) in rows(isolated_db_path, "SELECT storage_backend FROM evidence")) == ["local", "supabase"]
        assert app.app.state.evidence_quota.used == 2 * len(JPEG), "quota counts local and Supabase evidence"
        assert len(fake.objects) == 1


def test_supabase_evidence_needs_supabase_configured(supabase_env, fake, monkeypatch, isolated_db_path, evidence_dir):
    monkeypatch.setenv("EVIDENCE_STORAGE_BACKEND", "supabase")
    with TestClient(create_app(db_path=isolated_db_path, evidence_dir=evidence_dir)) as app:
        receipt, _ = new_complaint(app)
    monkeypatch.setenv("EVIDENCE_STORAGE_BACKEND", "local")
    with TestClient(create_app(db_path=isolated_db_path, evidence_dir=evidence_dir)) as local_app:
        token = staff_token(local_app)
        assert local_app.get(f"/api/v1/staff/complaints/{receipt['id']}/evidence", headers=bearer(token)).status_code == 503
        assert rows(isolated_db_path, "SELECT COUNT(*) FROM evidence_access_log") == [(0,)]


def test_startup_fails_when_the_bucket_is_missing(supabase_env, monkeypatch, isolated_db_path, evidence_dir):
    monkeypatch.setenv("EVIDENCE_STORAGE_BACKEND", "supabase")
    monkeypatch.setenv("SUPABASE_STORAGE_BUCKET", "missing-bucket")
    with pytest.raises(StoreError, match="does not exist"):
        with TestClient(create_app(db_path=isolated_db_path, evidence_dir=evidence_dir)):
            pass


def test_local_mode_never_contacts_supabase(fake, isolated_db_path, evidence_dir):
    with TestClient(create_app(db_path=isolated_db_path, evidence_dir=evidence_dir)) as client:
        assert set(client.app.state.evidence_storages) == {"local"}
        assert isinstance(client.app.state.evidence_storage, LocalEvidenceStorage)
        _, response = new_complaint(client)
        assert response.status_code == 201
    assert fake.requests == []
