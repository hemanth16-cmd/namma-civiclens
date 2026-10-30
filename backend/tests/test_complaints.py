import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from itertools import chain, repeat

import pytest
from fastapi.testclient import TestClient

from app.claims import ID_ALPHABET, verify_claim_code
from app.main import create_app
from app.store import ComplaintStore, IdAllocationError, resolve_db_path

PUBLIC_KEYS = {"id", "category", "area", "status", "verification", "department", "updated", "timeline", "verificationLog"}
GENERATED_ID = re.compile(rf"^NCL-\d{{4}}-[{ID_ALPHABET}]{{6}}$")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def iso_now(delta: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


def payload(**overrides) -> dict:
    body = {
        "category": "Potholes",
        "mediaType": "photo",
        "description": "Deep pothole near the bus stop",
        "shareEvidence": False,
        "neighbourhood": "Yelahanka",
        "gps": {
            "lat": 13.100712,
            "lng": 77.596334,
            "accuracy": 12.5,
            "sourceType": "gps",
            "source": "Device GPS",
            "timestamp": int(time.time() * 1000) - 5000,
            "fixAgeSeconds": 5,
            "address": "12 Example Road, Yelahanka",
        },
        "capturedAt": iso_now(),
        "captureSource": "Live camera",
    }
    body.update(overrides)
    return body


def with_gps(**gps) -> dict:
    body = payload()
    body["gps"] = {**body["gps"], **gps}
    return body


@pytest.fixture
def db_path(isolated_db_path):
    return isolated_db_path


@pytest.fixture
def api(db_path):
    with TestClient(create_app(db_path)) as client:
        yield client


def rows(db_path, sql, params=()):
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute(sql, params).fetchall()


def count(db_path, table="complaints") -> int:
    return rows(db_path, f"SELECT COUNT(*) FROM {table}")[0][0]


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"} and body["error"]["code"] == code
    return body["error"]


def assert_422(response, field=None):
    error = assert_error(response, 422, "validation_error")
    if field:
        assert field in {d["field"] for d in error["details"]}, error["details"]


# Creation and receipt

def test_valid_complaint_returns_201_receipt_with_id_and_claim(api):
    response = api.post("/api/v1/complaints", json=payload())
    assert response.status_code == 201, response.text
    receipt = response.json()
    assert GENERATED_ID.fullmatch(receipt["id"])
    assert UUID.fullmatch(receipt["claim"])
    assert receipt["status"] == "Submitted"
    assert receipt["verification"] == "Pending Verification"
    assert receipt["area"] == "Yelahanka"
    assert receipt["gps"]["lat"] == 13.100712
    assert receipt["gps"]["timestamp"].endswith("Z")
    assert receipt["capturedAt"].endswith("Z") and receipt["uploadedAt"].endswith("Z")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["location"] == f"/api/v1/complaints/{receipt['id']}"


def test_capture_time_is_accepted_under_the_frontend_name_created(api):
    body = payload()
    body["created"] = body.pop("capturedAt")
    response = api.post("/api/v1/complaints", json=body)
    assert response.status_code == 201
    assert response.json()["capturedAt"] is not None


def test_missing_neighbourhood_uses_the_prototype_default_area(api):
    body = payload()
    del body["neighbourhood"]
    assert api.post("/api/v1/complaints", json=body).json()["area"] == "Bengaluru · location on file"


# Claim code storage

def test_claim_code_is_stored_only_as_a_salted_hash(api, db_path):
    claim = api.post("/api/v1/complaints", json=payload()).json()["claim"]
    (stored,) = rows(db_path, "SELECT claim_hash FROM complaints")[0]
    assert stored != claim and claim not in stored
    assert stored.startswith("scrypt$")
    assert verify_claim_code(claim, stored)
    assert not verify_claim_code(claim.upper(), stored)
    assert not verify_claim_code("not-the-code", stored)


def test_plaintext_claim_code_appears_nowhere_in_the_database_files(api, db_path):
    claim = api.post("/api/v1/complaints", json=payload()).json()["claim"]
    for table in ("complaints", "complaint_timeline", "verification_log"):
        for row in rows(db_path, f"SELECT * FROM {table}"):
            assert claim not in " ".join(map(str, row))
    for file in db_path.parent.glob(db_path.name + "*"):  # main file plus -wal / -shm
        assert claim.encode() not in file.read_bytes()


def test_same_claim_code_hashes_differently_each_time():
    from app.claims import hash_claim_code
    assert hash_claim_code("same-code") != hash_claim_code("same-code")


# Public tracking

def test_public_tracking_returns_the_created_complaint(api):
    receipt = api.post("/api/v1/complaints", json=payload()).json()
    response = api.get(f"/api/v1/complaints/{receipt['id']}")
    assert response.status_code == 200
    public = response.json()
    assert set(public) == PUBLIC_KEYS
    assert public["id"] == receipt["id"]
    assert public["category"] == "Potholes"
    assert public["area"] == "Yelahanka"
    assert public["status"] == "Submitted"
    assert public["verification"] == "Pending Verification"
    assert public["department"] == "Awaiting civic review"
    assert [t["status"] for t in public["timeline"]] == ["Submitted"]
    assert set(public["timeline"][0]) == {"status", "text", "date"}
    assert public["verificationLog"] == []


def test_public_response_has_no_claim_hash_gps_or_private_fields(api, db_path):
    receipt = api.post("/api/v1/complaints", json=payload()).json()
    raw = api.get(f"/api/v1/complaints/{receipt['id']}").text
    (stored_hash,) = rows(db_path, "SELECT claim_hash FROM complaints")[0]
    for secret in (receipt["claim"], stored_hash, "scrypt", "claim", "13.100712", "77.596334", "lat", "lng",
                   "gps", "12 Example Road", "Deep pothole", "description", "owner", "internal", "mediaType"):
        assert secret not in raw, secret


def test_unknown_complaint_returns_404_envelope(api):
    error = assert_error(api.get("/api/v1/complaints/NCL-2026-ZZZZZZ"), 404, "not_found")
    assert error["message"] == "No complaint was found with that ID."


@pytest.mark.parametrize("bad_id", ["ncl-2026-demo01", "NCL-2026-DEMO1", "NCL-26-DEMO01", "1 OR 1=1", "NCL-2026-DEMO01x"])
def test_malformed_ids_return_the_same_404(api, bad_id):
    assert_error(api.get(f"/api/v1/complaints/{bad_id}"), 404, "not_found")


def test_demo_style_ids_can_be_looked_up(db_path):
    store = ComplaintStore(db_path, id_factory=lambda: "NCL-2026-DEMO01")
    with TestClient(create_app(store=store)) as client:
        assert client.post("/api/v1/complaints", json=payload()).json()["id"] == "NCL-2026-DEMO01"
        response = client.get("/api/v1/complaints/NCL-2026-DEMO01")
    assert response.status_code == 200
    assert response.json()["id"] == "NCL-2026-DEMO01"


# Validation

def test_invalid_category_is_rejected(api, db_path):
    assert_422(api.post("/api/v1/complaints", json=payload(category="Noise")), "body.category")
    assert count(db_path) == 0


@pytest.mark.parametrize("gps, field", [
    ({"lat": 90.0001}, "body.gps.lat"),
    ({"lat": -91}, "body.gps.lat"),
    ({"lng": 180.5}, "body.gps.lng"),
    ({"lng": -181}, "body.gps.lng"),
])
def test_out_of_range_coordinates_are_rejected(api, gps, field):
    assert_422(api.post("/api/v1/complaints", json=with_gps(**gps)), field)


@pytest.mark.parametrize("body, field", [
    (with_gps(accuracy=-1), "body.gps.accuracy"),
    (with_gps(sourceType="wifi"), "body.gps.sourceType"),
    (with_gps(timestamp="yesterday"), "body.gps.timestamp"),
    (with_gps(timestamp=12345), "body.gps.timestamp"),
    (with_gps(timestamp=int((time.time() + 86_400) * 1000)), "body.gps.timestamp"),
    (payload(capturedAt="not-a-date"), "body.capturedAt"),
    (payload(capturedAt="2026-10-30T10:00:00"), "body.capturedAt"),  # no timezone
    (payload(capturedAt=iso_now(timedelta(days=1))), "body.capturedAt"),
    (payload(mediaType="audio"), "body.mediaType"),
    (payload(description="x" * 1001), "body.description"),
])
def test_invalid_accuracy_timestamps_and_values_are_rejected(api, body, field):
    assert_422(api.post("/api/v1/complaints", json=body), field)


def test_non_finite_numbers_are_rejected(api):
    raw = '{"category": "Potholes", "gps": {"lat": NaN, "lng": 77.5, "sourceType": "gps"}}'
    assert_422(api.post("/api/v1/complaints", content=raw, headers={"content-type": "application/json"}), "body.gps.lat")


def test_missing_gps_is_rejected(api):
    body = payload()
    del body["gps"]
    assert_422(api.post("/api/v1/complaints", json=body), "body.gps")


@pytest.mark.parametrize("body, field", [
    (payload(owner="resident@civiclens.demo"), "body.owner"),
    (payload(uploadedAt=iso_now()), "body.uploadedAt"),
    (payload(claim="my-own-code"), "body.claim"),
    (payload(status="Marked Resolved"), "body.status"),
    (with_gps(altitude=900), "body.gps.altitude"),
])
def test_unknown_fields_are_rejected(api, db_path, body, field):
    assert_422(api.post("/api/v1/complaints", json=body), field)
    assert count(db_path) == 0


# Persistence, collisions and transactions

def test_complaints_survive_an_application_restart(db_path):
    with TestClient(create_app(db_path)) as first:
        public_id = first.post("/api/v1/complaints", json=payload()).json()["id"]
    with TestClient(create_app(db_path)) as second:
        assert second.get(f"/api/v1/complaints/{public_id}").status_code == 200
    reopened = ComplaintStore(db_path)
    reopened.initialize()  # idempotent: must not wipe existing data
    assert reopened.get(public_id)["category"] == "Potholes"


def test_id_collision_retries_without_overwriting_the_existing_complaint(db_path):
    ids = iter(["NCL-2026-AAAAAA", "NCL-2026-AAAAAA", "NCL-2026-BBBBBB"])
    store = ComplaintStore(db_path, id_factory=lambda: next(ids))
    store.initialize()
    first, first_claim = store.create(payload(category="Potholes", capturedAt=None))
    second, _ = store.create(payload(category="Water leakage", capturedAt=None))
    assert (first["id"], second["id"]) == ("NCL-2026-AAAAAA", "NCL-2026-BBBBBB")
    kept = store.get("NCL-2026-AAAAAA")
    assert kept["category"] == "Potholes"
    assert verify_claim_code(first_claim, kept["claimHash"])
    assert count(db_path) == 2


def test_exhausted_id_retries_fail_generically_and_store_nothing_new(db_path):
    store = ComplaintStore(db_path, id_factory=lambda: "NCL-2026-AAAAAA")
    with TestClient(create_app(store=store)) as client:
        assert client.post("/api/v1/complaints", json=payload()).status_code == 201
        response = client.post("/api/v1/complaints", json=payload(category="Illegal dumping"))
    error = assert_error(response, 503, "storage_unavailable")
    assert "NCL-2026-AAAAAA" not in response.text and "attempt" not in error["message"]
    assert count(db_path) == 1
    with pytest.raises(IdAllocationError):
        store.create(payload(capturedAt=None))


def test_failed_transaction_leaves_no_partial_complaint(api, db_path):
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TRIGGER fail_timeline BEFORE INSERT ON complaint_timeline "
                     "BEGIN SELECT RAISE(ABORT, 'simulated timeline failure'); END")
        conn.commit()
    response = api.post("/api/v1/complaints", json=payload())
    assert_error(response, 503, "storage_unavailable")
    for leaked in ("simulated", "INSERT", "complaint_timeline", "Traceback", "sqlite"):
        assert leaked not in response.text
    assert count(db_path) == 0
    assert count(db_path, "complaint_timeline") == 0


def test_unreadable_database_returns_storage_error_without_details(tmp_path):
    not_a_db = tmp_path / "broken.sqlite3"
    not_a_db.write_bytes(b"this is not a sqlite database" * 100)
    store = ComplaintStore(not_a_db)
    app = create_app(store=store)
    client = TestClient(app)  # no lifespan, so initialise is skipped and the read hits the bad file
    response = client.get("/api/v1/complaints/NCL-2026-AAAAAA")
    assert_error(response, 503, "storage_unavailable")
    for leaked in ("not a database", "file is", "sqlite", "SELECT", "broken.sqlite3"):
        assert leaked not in response.text


# Isolation

def test_tests_use_a_temporary_database(isolated_db_path):
    assert resolve_db_path() == isolated_db_path
    assert "pytest" in str(isolated_db_path).lower() or "tmp" in str(isolated_db_path).lower()


def test_separate_database_files_do_not_share_complaints(tmp_path):
    with TestClient(create_app(tmp_path / "a.sqlite3")) as a, TestClient(create_app(tmp_path / "b.sqlite3")) as b:
        public_id = a.post("/api/v1/complaints", json=payload()).json()["id"]
        assert a.get(f"/api/v1/complaints/{public_id}").status_code == 200
        assert b.get(f"/api/v1/complaints/{public_id}").status_code == 404
