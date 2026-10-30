import os
import threading

import pytest
from fastapi.testclient import TestClient

from app.errors import ApiProblem
from app.main import create_app
from app.quota import DEFAULT_QUOTA_BYTES, EvidenceQuota, quota_from_env
from tests.test_complaints import payload
from tests.test_evidence import JPEG, PNG, evidence_rows, objects, temp_files

OTHER_JPEG = b"\xff\xd8\xff\xe1" + os.urandom(4000)


def make(isolated_db_path, evidence_dir, limit):
    return TestClient(create_app(db_path=isolated_db_path, evidence_dir=evidence_dir, evidence_quota=EvidenceQuota(limit)))


def create(client):
    return client.post("/api/v1/complaints", json=payload()).json()


def upload(client, receipt, content=JPEG, filename="e.jpg", ctype="image/jpeg"):
    return client.post(f"/api/v1/complaints/{receipt['id']}/evidence", files={"file": (filename, content, ctype)},
                       headers={"Authorization": f"Bearer {receipt['uploadToken']}"})


def assert_507(response):
    assert response.status_code == 507, response.text
    assert response.json()["error"]["code"] == "storage_quota_exceeded"


def test_default_quota_is_2_gb(monkeypatch):
    assert DEFAULT_QUOTA_BYTES == 2 * 1024 ** 3 == quota_from_env()
    monkeypatch.setenv("CIVICLENS_EVIDENCE_QUOTA_BYTES", "12345")
    assert quota_from_env() == 12345


def test_reservation_accounting():
    quota = EvidenceQuota(1000)
    r = quota.reserve(600)
    assert (quota.used, quota.reserved) == (0, 600)
    with pytest.raises(ApiProblem) as err:
        quota.reserve(500)
    assert err.value.status_code == 507
    r.commit(550)
    assert (quota.used, quota.reserved) == (550, 0)
    r.release()  # no effect after commit
    assert (quota.used, quota.reserved) == (550, 0)
    with quota.reserve(400):
        assert quota.reserved == 400
    assert (quota.used, quota.reserved) == (550, 0), "leaving the block without commit releases the reservation"


def test_concurrent_reservations_cannot_over_allocate():
    quota = EvidenceQuota(1000)
    granted, refused = [], []
    barrier = threading.Barrier(20)

    def worker():
        barrier.wait()
        try:
            granted.append(quota.reserve(100))
        except ApiProblem:
            refused.append(1)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert (len(granted), len(refused)) == (10, 10)
    assert quota.reserved == 1000
    for r in granted:
        r.release()
    assert quota.reserved == 0


def test_upload_within_quota_succeeds_and_counts_real_size(isolated_db_path, evidence_dir):
    with make(isolated_db_path, evidence_dir, 1_000_000) as client:
        assert upload(client, create(client)).status_code == 201
        quota = client.app.state.evidence_quota
        assert (quota.used, quota.reserved) == (len(JPEG), 0)


def test_upload_beyond_quota_is_rejected_and_existing_files_stay(isolated_db_path, evidence_dir):
    with make(isolated_db_path, evidence_dir, 6000) as client:
        assert upload(client, create(client)).status_code == 201
        before = [(p, p.read_bytes()) for p in objects(evidence_dir)]
        response = upload(client, create(client), OTHER_JPEG)
        assert_507(response)
        assert "complaint itself is saved" in response.json()["error"]["message"]
        assert [(p, p.read_bytes()) for p in objects(evidence_dir)] == before
        assert len(evidence_rows(isolated_db_path)) == 1 and temp_files(evidence_dir) == []
        assert client.app.state.evidence_quota.reserved == 0


def test_failed_uploads_release_their_reservation(isolated_db_path, evidence_dir):
    with make(isolated_db_path, evidence_dir, 1_000_000) as client:
        receipt = create(client)
        quota = client.app.state.evidence_quota
        assert upload(client, receipt, PNG, "e.png", "image/png").status_code == 415
        assert upload(client, receipt, b"").status_code == 422
        assert (quota.used, quota.reserved) == (0, 0)
        assert upload(client, receipt).status_code == 201
        assert upload(client, receipt).status_code == 200  # duplicate retry: no extra usage
        assert (quota.used, quota.reserved) == (len(JPEG), 0)


def test_an_in_progress_upload_blocks_capacity_until_released(isolated_db_path, evidence_dir):
    with make(isolated_db_path, evidence_dir, 10_000) as client:
        quota = client.app.state.evidence_quota
        held = quota.reserve(8000)  # another upload is still streaming
        assert_507(upload(client, create(client)))
        held.release()
        assert upload(client, create(client)).status_code == 201


def test_storage_failure_after_reservation_releases_it(isolated_db_path, evidence_dir, monkeypatch):
    from app.store import StoreError
    with make(isolated_db_path, evidence_dir, 1_000_000) as client:
        monkeypatch.setattr(client.app.state.store, "add_evidence", lambda *a: (_ for _ in ()).throw(StoreError("x")))
        assert upload(client, create(client)).status_code == 503
        assert (client.app.state.evidence_quota.used, client.app.state.evidence_quota.reserved) == (0, 0)


def test_usage_survives_restart(isolated_db_path, evidence_dir):
    with make(isolated_db_path, evidence_dir, 1_000_000) as client:
        upload(client, create(client))
    with make(isolated_db_path, evidence_dir, 1_000_000) as again:
        assert again.app.state.evidence_quota.used == len(JPEG)
    with make(isolated_db_path, evidence_dir, len(JPEG) + 100) as tight:
        assert_507(upload(tight, create(tight), OTHER_JPEG))


def test_untracked_files_on_disk_count_conservatively(isolated_db_path, evidence_dir):
    with make(isolated_db_path, evidence_dir, 1_000_000) as client:
        upload(client, create(client))
    stray = evidence_dir / "objects" / "ab" / ("ab" + "0" * 30)
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"x" * 50_000)  # a file with no database record
    with make(isolated_db_path, evidence_dir, 1_000_000) as again:
        assert again.app.state.evidence_quota.used == len(JPEG) + 50_000
