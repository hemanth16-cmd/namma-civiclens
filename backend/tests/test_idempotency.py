import secrets
import sqlite3
import threading
from contextlib import closing
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.claims import verify_claim_code
from app.complaints import request_fingerprint
from app.main import create_app
from app.store import ComplaintStore, IdempotencyCompleted, IdempotencyConflict
from tests.test_complaints import payload
from tests.test_evidence import JPEG


def new_key():
    return secrets.token_urlsafe(32)


@pytest.fixture
def api(isolated_db_path, evidence_dir):
    with TestClient(create_app(db_path=isolated_db_path, evidence_dir=evidence_dir)) as client:
        yield client


def post(api, body, key):
    return api.post("/api/v1/complaints", json=body, headers={"Idempotency-Key": key} if key else {})


def count(db_path, table="complaints"):
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def claim_hash(db_path, public_id):
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute("SELECT claim_hash FROM complaints WHERE public_id = ?", (public_id,)).fetchone()[0]


def upload(api, receipt, token=None):
    return api.post(f"/api/v1/complaints/{receipt['id']}/evidence", files={"file": ("e.jpg", JPEG, "image/jpeg")},
                    headers={"Authorization": f"Bearer {token or receipt['uploadToken']}"})


def test_same_key_and_payload_creates_one_complaint_and_reissues_credentials(api, isolated_db_path):
    key, body = new_key(), payload()
    first = post(api, body, key)
    second = post(api, body, key)
    assert first.status_code == 201 and first.json()["replayed"] is False
    assert second.status_code == 200 and second.json()["replayed"] is True
    assert second.headers["idempotent-replayed"] == "true"
    assert second.headers["cache-control"] == "no-store"
    a, b = first.json(), second.json()
    assert a["id"] == b["id"] and count(isolated_db_path) == 1 and count(isolated_db_path, "complaint_timeline") == 1
    assert a["claim"] != b["claim"] and a["uploadToken"] != b["uploadToken"]
    stored = claim_hash(isolated_db_path, a["id"])
    assert verify_claim_code(b["claim"], stored) and not verify_claim_code(a["claim"], stored), "only the latest claim code works"
    assert upload(api, a).status_code == 401, "the earlier upload token no longer works"
    assert upload(api, b).status_code == 201


def test_json_field_order_does_not_change_the_fingerprint(api, isolated_db_path):
    key, body = new_key(), payload()
    post(api, body, key)
    reordered = dict(reversed(list(body.items())))
    assert post(api, reordered, key).status_code == 200
    assert count(isolated_db_path) == 1


def test_same_key_with_a_different_request_is_refused(api, isolated_db_path):
    key = new_key()
    created = post(api, payload(), key).json()
    response = post(api, payload(description="something else"), key)
    assert response.status_code == 409 and response.json()["error"]["code"] == "idempotency_key_conflict"
    assert created["id"] not in response.text and "claim" not in response.text.lower()
    assert count(isolated_db_path) == 1


def test_no_reissue_after_evidence_was_uploaded(api, isolated_db_path):
    key, body = new_key(), payload()
    created = post(api, body, key).json()
    assert upload(api, created).status_code == 201
    response = post(api, body, key)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "idempotency_key_used" and error["complaintId"] == created["id"]
    assert created["claim"] not in response.text and "uploadToken" not in response.text
    assert verify_claim_code(created["claim"], claim_hash(isolated_db_path, created["id"])), "credentials unchanged"


def test_no_reissue_after_the_token_was_renewed(api):
    key, body = new_key(), payload()
    created = post(api, body, key).json()
    assert api.post(f"/api/v1/complaints/{created['id']}/upload-token", json={"claim": created["claim"]}).status_code == 200
    assert post(api, body, key).json()["error"]["code"] == "idempotency_key_used"


def test_no_reissue_after_the_replay_window(isolated_db_path, evidence_dir):
    with TestClient(create_app(db_path=isolated_db_path, evidence_dir=evidence_dir, replay_window=timedelta(seconds=-1))) as api:
        key, body = new_key(), payload()
        created = post(api, body, key).json()
        response = post(api, body, key)
        assert response.status_code == 409 and response.json()["error"]["complaintId"] == created["id"]
        assert count(isolated_db_path) == 1


@pytest.mark.parametrize("bad", ["short", "a" * 21, "has space" * 4, "x" * 129, "abc+def/ghi=jklmnopqrstuvwxyz"])
def test_malformed_keys_are_rejected(api, isolated_db_path, bad):
    response = api.post("/api/v1/complaints", json=payload(), headers={"Idempotency-Key": bad})
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_idempotency_key"
    assert count(isolated_db_path) == 0


def test_without_a_key_each_request_creates_a_complaint(api, isolated_db_path):
    post(api, payload(), None)
    post(api, payload(), None)
    assert count(isolated_db_path) == 2


def test_different_keys_create_different_complaints(api, isolated_db_path):
    body = payload()
    assert post(api, body, new_key()).json()["id"] != post(api, body, new_key()).json()["id"]
    assert count(isolated_db_path) == 2


def test_failed_creation_leaves_no_idempotency_record(api, isolated_db_path):
    with closing(sqlite3.connect(isolated_db_path)) as conn:
        conn.execute("CREATE TRIGGER fail BEFORE INSERT ON complaint_timeline BEGIN SELECT RAISE(ABORT, 'boom'); END")
        conn.commit()
    key, body = new_key(), payload()
    assert post(api, body, key).status_code == 503
    assert count(isolated_db_path) == 0 and count(isolated_db_path, "idempotency_keys") == 0
    with closing(sqlite3.connect(isolated_db_path)) as conn:
        conn.execute("DROP TRIGGER fail")
        conn.commit()
    retry = post(api, body, key)
    assert retry.status_code == 201 and retry.json()["replayed"] is False, "the retry is a fresh creation"


def test_lost_response_recovery_end_to_end(api, isolated_db_path):
    """The client never saw the first response; the retry with the same key gives usable credentials."""
    key, body = new_key(), payload()
    post(api, body, key)  # response "lost"
    recovered = post(api, body, key).json()
    assert recovered["replayed"] is True
    renewed = api.post(f"/api/v1/complaints/{recovered['id']}/upload-token", json={"claim": recovered["claim"]})
    assert renewed.status_code == 200
    assert upload(api, recovered, renewed.json()["uploadToken"]).status_code == 201


def test_only_a_hash_of_the_key_is_stored(api, isolated_db_path):
    key = new_key()
    post(api, payload(), key)
    for f in isolated_db_path.parent.glob(isolated_db_path.name + "*"):
        assert key.encode() not in f.read_bytes()


def test_concurrent_requests_with_one_key_create_one_complaint(isolated_db_path):
    store = ComplaintStore(isolated_db_path)
    store.initialize()
    body = payload(capturedAt=None)
    fingerprint = request_fingerprint(body)
    outcomes, errors = [], []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        try:
            outcomes.append(store.create_idempotent(body, "a" * 64, fingerprint))
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sorted(o[0] for o in outcomes) == ["created"] + ["replayed"] * 7
    assert len({o[1]["id"] for o in outcomes}) == 1
    assert count(isolated_db_path) == 1 and count(isolated_db_path, "idempotency_keys") == 1
    latest_hash = claim_hash(isolated_db_path, outcomes[0][1]["id"])
    assert sum(verify_claim_code(o[2], latest_hash) for o in outcomes) == 1, "exactly the last issued claim code is valid"


def test_store_level_errors(isolated_db_path):
    store = ComplaintStore(isolated_db_path)
    store.initialize()
    body = payload(capturedAt=None)
    store.create_idempotent(body, "b" * 64, "f" * 64)
    with pytest.raises(IdempotencyConflict):
        store.create_idempotent(body, "b" * 64, "e" * 64)
    with pytest.raises(IdempotencyCompleted):
        store.create_idempotent(body, "b" * 64, "f" * 64, replay_window=timedelta(seconds=-1))
