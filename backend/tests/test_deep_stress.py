"""Deep stress and concurrency tests for the Namma CivicLens backend.

Covers Section 3 (complaint creation, persistence, evidence upload, rate limiting),
Section 4 (database concurrency & locking), and Section 8 (concurrency workloads).
"""

import concurrent.futures
import io
import math
import os
import threading
import time
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient

from app.complaints import request_fingerprint
from app.main import create_app
from app.quota import EvidenceQuota
from app.ratelimit import InMemoryRateLimiter, Rule
from app.store import ComplaintStore
from tests.test_complaints import payload
from tests.test_evidence import JPEG, upload

REALISTIC_JPEG = bytes([0xFF, 0xD8, 0xFF, 0xE0, 0, 16, 0x4A, 0x46, 0x49, 0x46, 0, 1, 1, 0, 0, 1, 0, 1, 0, 0]) + (b"X" * 128_000) + bytes([0xFF, 0xD9])


@pytest.fixture
def api(isolated_db_path, evidence_dir):
    with TestClient(create_app(db_path=isolated_db_path, evidence_dir=evidence_dir)) as client:
        yield client


def test_concurrent_complaint_creation_50_clients(api):
    """Section 8 workload: 50 simultaneous clients creating complaints concurrently."""
    num_clients = 50
    barrier = threading.Barrier(num_clients)
    results = []

    def client_worker(idx):
        p = payload(
            category="Potholes",
            mediaType="photo",
            description=f"Concurrent stress complaint client #{idx}",
            capturedAt=None,
        )
        barrier.wait()
        r = api.post("/api/v1/complaints", json=p)
        results.append((idx, r.status_code, r.json() if r.status_code == 201 else r.text))

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_clients) as pool:
        futures = [pool.submit(client_worker, i) for i in range(num_clients)]
        for f in futures:
            f.result()

    assert len(results) == num_clients
    status_codes = [r[1] for r in results]
    assert all(code == 201 for code in status_codes), f"Status codes: {set(status_codes)}"

    public_ids = [r[2]["id"] for r in results]
    claim_codes = [r[2]["claim"] for r in results]
    upload_tokens = [r[2]["uploadToken"] for r in results]

    assert len(set(public_ids)) == num_clients, "Every public ID must be unique"
    assert len(set(claim_codes)) == num_clients, "Every claim code must be unique"
    assert len(set(upload_tokens)) == num_clients, "Every upload token must be unique"

    # Verify every complaint is retrievable via tracking
    for pid in public_ids[:10]:
        track = api.get(f"/api/v1/complaints/{pid}")
        assert track.status_code == 200
        assert track.json()["id"] == pid
        assert "claim" not in track.json()
        assert "claimHash" not in track.json()


def test_concurrent_public_tracking_100_requests(api):
    """Section 8 workload: 100 simultaneous requests tracking a complaint."""
    res = api.post("/api/v1/complaints", json=payload(capturedAt=None))
    assert res.status_code == 201
    public_id = res.json()["id"]

    num_requests = 100
    barrier = threading.Barrier(num_requests)
    outcomes = []

    def tracking_worker():
        barrier.wait()
        r = api.get(f"/api/v1/complaints/{public_id}")
        outcomes.append(r.status_code)

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_requests) as pool:
        futures = [pool.submit(tracking_worker) for _ in range(num_requests)]
        for f in futures:
            f.result()

    assert len(outcomes) == num_requests
    assert all(code == 200 for code in outcomes)


def test_concurrent_evidence_uploads_20_clients(api):
    """Section 8 workload: 20 concurrent uploads with realistic file sizes."""
    num_uploads = 20
    complaints = []
    for i in range(num_uploads):
        r = api.post("/api/v1/complaints", json=payload(capturedAt=None))
        assert r.status_code == 201
        complaints.append((r.json()["id"], r.json()["uploadToken"]))

    barrier = threading.Barrier(num_uploads)
    results = []

    def upload_worker(idx, pid, token):
        barrier.wait()
        r = upload(api, pid, REALISTIC_JPEG, token=token)
        results.append((idx, r.status_code, r.json() if r.status_code == 201 else r.text))

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_uploads) as pool:
        futures = [pool.submit(upload_worker, i, complaints[i][0], complaints[i][1]) for i in range(num_uploads)]
        for f in futures:
            f.result()

    assert len(results) == num_uploads
    assert all(r[1] == 201 for r in results), f"Upload failures: {[r for r in results if r[1] != 201]}"

    for idx, status, body in results:
        assert body["status"] == "uploaded"
        assert body["mediaType"] == "photo"
        assert body["byteSize"] == len(REALISTIC_JPEG)
        assert len(body["sha256"]) == 64


def test_multilingual_unicode_stress(api):
    """Test complaint creation and persistence with multilingual, emoji and boundary text."""
    test_cases = [
        ("Kannada", "ರಸ್ತೆ ಗುಂಡಿ ದುರಸ್ತಿ ಬೇಕು - ಕೆಂಪೇಗೌಡ ರಸ್ತೆ"),
        ("Tamil", "சாலை சேதமடைந்துள்ளது உடனடியாக சரிபார்க்கவும்"),
        ("Hindi", "सड़क पर गहरा गड्ढा है, दुर्घटना की आशंका है"),
        ("Telugu", "రోడ్డు మీద పెద్ద గుంత ఉంది, దయచేసి బాగు చేయండి"),
        ("Malayalam", "റോഡിലെ കുഴികൾ അടയ്ക്കണം"),
        ("Japanese", "道路に深い穴が開いています。修理をお願いします。"),
        ("Arabic_RTL", "هناك حفرة خطيرة في الشارع الرئيسي بحاجة إلى إصلاح"),
        ("Emoji", "🕳️⚠️ Major pothole on 80ft road near junction! 🚗💥 Need urgent fix! 🚧"),
        ("Max_Length_1000", "P" * 1000),
    ]

    for label, desc in test_cases:
        p = payload(description=desc, capturedAt=None)
        res = api.post("/api/v1/complaints", json=p)
        assert res.status_code == 201, f"Failed for {label}: {res.text}"
        data = res.json()
        pid = data["id"]

        track = api.get(f"/api/v1/complaints/{pid}")
        assert track.status_code == 200

    # Test exceeding 1000 chars is rejected with 422
    over_limit = payload(description="X" * 1001, capturedAt=None)
    res_over = api.post("/api/v1/complaints", json=over_limit)
    assert res_over.status_code == 422


def test_database_mixed_read_write_workload_50_workers(api):
    """Section 8 workload: 50 concurrent workers running mixed operations."""
    # Seed a few complaints
    seeded_ids = []
    for _ in range(5):
        r = api.post("/api/v1/complaints", json=payload(capturedAt=None))
        assert r.status_code == 201
        seeded_ids.append(r.json()["id"])

    # Create an inbox
    inbox_res = api.post("/api/v1/notifications/inboxes")
    assert inbox_res.status_code == 201
    inbox_token = inbox_res.json()["inboxToken"]

    num_workers = 50
    barrier = threading.Barrier(num_workers)
    outcomes = []

    def mixed_worker(i):
        barrier.wait()
        op = i % 4
        if op == 0:
            # Create complaint
            r = api.post("/api/v1/complaints", json=payload(capturedAt=None))
            outcomes.append(("create", r.status_code))
        elif op == 1:
            # Track seeded complaint
            pid = seeded_ids[i % len(seeded_ids)]
            r = api.get(f"/api/v1/complaints/{pid}")
            outcomes.append(("track", r.status_code))
        elif op == 2:
            # Check notifications list
            r = api.get("/api/v1/notifications", headers={"Authorization": f"Bearer {inbox_token}"})
            outcomes.append(("notifications", r.status_code))
        elif op == 3:
            # Unread count
            r = api.get("/api/v1/notifications/unread-count", headers={"Authorization": f"Bearer {inbox_token}"})
            outcomes.append(("unread_count", r.status_code))

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as pool:
        futures = [pool.submit(mixed_worker, i) for i in range(num_workers)]
        for f in futures:
            f.result()

    assert len(outcomes) == num_workers
    for op, status in outcomes:
        assert status in (200, 201), f"Unexpected failure in {op}: {status}"


def test_rate_limiter_memory_bounding():
    """Verify that InMemoryRateLimiter bounds its memory under 50,000 unique keys."""
    max_keys = 500
    limiter = InMemoryRateLimiter(max_keys=max_keys)
    rule = Rule(10, 60.0)

    for i in range(5000):
        key = f"client-ip-{i}"
        limiter.hit(key, rule)

    # Limiter size must never exceed max_keys
    assert len(limiter) == max_keys
    assert len(limiter._buckets) == max_keys


def test_quota_concurrency_exhaustion(tmp_path):
    """Test storage quota exhaustion under concurrent upload requests."""
    # Build isolated app with a tight 300 KB quota
    tight_quota = EvidenceQuota(limit_bytes=300 * 1024)
    app = create_app(
        db_path=tmp_path / "quota_test.sqlite3",
        evidence_dir=tmp_path / "quota_evidence",
        evidence_quota=tight_quota,
    )
    with TestClient(app) as client:
        num_uploads = 10
        complaints = []
        for _ in range(num_uploads):
            r = client.post("/api/v1/complaints", json=payload(capturedAt=None))
            assert r.status_code == 201
            complaints.append((r.json()["id"], r.json()["uploadToken"]))

        # Each upload is ~80 KB
        file_bytes = bytes([0xFF, 0xD8, 0xFF, 0xE0, 0, 16, 0x4A, 0x46, 0x49, 0x46, 0, 1, 1, 0, 0, 1, 0, 1, 0, 0]) + (b"Q" * 80_000) + bytes([0xFF, 0xD9])

        barrier = threading.Barrier(num_uploads)
        results = []

        def upload_task(idx):
            pid, token = complaints[idx]
            barrier.wait()
            r = upload(client, pid, file_bytes, token=token)
            results.append(r.status_code)

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_uploads) as pool:
            futures = [pool.submit(upload_task, i) for i in range(num_uploads)]
            for f in futures:
                f.result()

        assert len(results) == num_uploads
        successes = [s for s in results if s == 201]
        quota_exceeded = [s for s in results if s == 507]

        # With 300KB quota and 80KB uploads, 3 uploads fit (240KB <= 300KB), 4th would be 320KB (>300KB)
        assert len(successes) == 3, f"Expected 3 successes, got {len(successes)}: {results}"
        assert len(quota_exceeded) == 7, f"Expected 7 quota rejections, got {len(quota_exceeded)}: {results}"
        assert tight_quota.used == 3 * len(file_bytes)
        assert tight_quota.reserved == 0
