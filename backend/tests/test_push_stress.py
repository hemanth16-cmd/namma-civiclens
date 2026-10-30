"""Web Push notification stress tests.

Covers Section 6: Web Push notification stress tests (error handling, 404/410 cleanup,
consecutive failure pruning, unread count concurrency, and push payload safety).
"""

import json
import requests
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.notifications import MAX_FAILURES, Notifier
from app.push import PushResult, PushSender, PushSettings
from app.store import ComplaintStore
from tests.test_complaints import payload
from tests.test_notifications import subscription


class ScriptablePushSender(PushSender):
    def __init__(self):
        self.script = {}  # endpoint -> list of (outcome, status)
        self.history = []  # list of (device, payload)

    def send(self, device: dict, payload: str) -> PushResult:
        self.history.append((device, payload))
        endpoint = device["endpoint"]
        if endpoint in self.script and self.script[endpoint]:
            outcome, status = self.script[endpoint].pop(0)
            return PushResult(outcome, status)
        return PushResult("sent", 201)


@pytest.fixture
def fake_sender():
    return ScriptablePushSender()


@pytest.fixture
def push_api(isolated_db_path, evidence_dir, fake_sender):
    push_settings = PushSettings(
        public_key="BDkARUW9ciyvRsEpUtv6vfLOw4htygvJMjdMSHf80Tfp0pUEuziKPy0oVATU6ZtQXiwksAWcQEcd1ipRSjpEFCo",
        private_key="hVFm82PuNcY-NcgyMl8Yo23dhYOvD0WeHdiuKzEa7HI",
        subject="mailto:test@example.org",
    )
    with TestClient(create_app(
        db_path=isolated_db_path,
        evidence_dir=evidence_dir,
        push_settings=push_settings,
        push_sender=fake_sender,
    )) as client:
        yield client


def test_push_service_404_410_deletes_device_immediately(push_api, fake_sender):
    """When a push service returns 404/410 Gone, the device subscription is deleted immediately."""
    store = push_api.app.state.store
    inbox = push_api.post("/api/v1/notifications/inboxes").json()["inboxToken"]
    headers = {"Authorization": f"Bearer {inbox}"}

    sub = subscription()
    dev = push_api.post("/api/v1/notifications/subscriptions", json=sub, headers=headers).json()
    assert dev["subscriptionId"].startswith("ps_")

    # Verify device exists
    devs = push_api.get("/api/v1/notifications/subscriptions", headers=headers).json()
    assert len(devs) == 1

    # Configure push sender to return 410 Gone
    fake_sender.script[sub["endpoint"]] = [("gone", 410)]

    # Trigger test notification
    res = push_api.post("/api/v1/notifications/test", headers=headers)
    assert res.status_code == 202

    # Device must be gone
    devs_after = push_api.get("/api/v1/notifications/subscriptions", headers=headers).json()
    assert len(devs_after) == 0


def test_consecutive_failures_pruning(push_api, fake_sender):
    """Devices are pruned after 5 (MAX_FAILURES) consecutive delivery failures."""
    store = push_api.app.state.store
    inbox = push_api.post("/api/v1/notifications/inboxes").json()["inboxToken"]
    headers = {"Authorization": f"Bearer {inbox}"}

    sub = subscription()
    push_api.post("/api/v1/notifications/subscriptions", json=sub, headers=headers)

    # 4 transient failures: device remains
    fake_sender.script[sub["endpoint"]] = [("retry", 500)] * 4
    for _ in range(4):
        push_api.post("/api/v1/notifications/test", headers=headers)

    devs = push_api.get("/api/v1/notifications/subscriptions", headers=headers).json()
    assert len(devs) == 1
    assert devs[0]["failureCount"] == 4

    # 5th failure: device is automatically pruned
    fake_sender.script[sub["endpoint"]] = [("retry", 500)]
    push_api.post("/api/v1/notifications/test", headers=headers)

    devs_after = push_api.get("/api/v1/notifications/subscriptions", headers=headers).json()
    assert len(devs_after) == 0


def test_push_payload_never_leaks_private_data(push_api, fake_sender):
    """Web Push payloads must never contain claim codes, upload tokens, precise coordinates or description."""
    store = push_api.app.state.store
    inbox = push_api.post("/api/v1/notifications/inboxes").json()["inboxToken"]
    headers = {"Authorization": f"Bearer {inbox}"}

    sub = subscription()
    push_api.post("/api/v1/notifications/subscriptions", json=sub, headers=headers)

    # Create complaint with sensitive description and GPS
    comp = push_api.post("/api/v1/complaints", json=payload(
        description="Top secret road collapse",
        capturedAt=None,
    )).json()

    # Follow as citizen
    push_api.post("/api/v1/notifications/follows", json={"complaintId": comp["id"], "claim": comp["claim"]}, headers=headers)

    # Inspect all push payloads received so far
    for dev, raw_payload in fake_sender.history:
        data = json.loads(raw_payload)
        assert comp["claim"] not in raw_payload
        assert comp["uploadToken"] not in raw_payload
        assert "Top secret" not in raw_payload
        assert "13.0358" not in raw_payload
        assert "77.597" not in raw_payload
        # Allowlisted fields only
        assert set(data.keys()).issubset({"id", "kind", "title", "body", "url", "audience", "createdAt"})


def test_unread_count_and_mark_all_read(push_api):
    """Test notification unread count tracking and mark-all-as-read functionality."""
    inbox = push_api.post("/api/v1/notifications/inboxes").json()["inboxToken"]
    headers = {"Authorization": f"Bearer {inbox}"}

    # Initially 0 unread
    assert push_api.get("/api/v1/notifications/unread-count", headers=headers).json()["unreadCount"] == 0

    # Send 3 test notifications
    for _ in range(3):
        push_api.post("/api/v1/notifications/test", headers=headers)

    unread_res = push_api.get("/api/v1/notifications/unread-count", headers=headers).json()
    assert unread_res["unreadCount"] == 3

    # Mark all read
    read_all_res = push_api.post("/api/v1/notifications/read-all", headers=headers).json()
    assert read_all_res["updated"] == 3
    assert read_all_res["unreadCount"] == 0

    # Unread count is now 0
    assert push_api.get("/api/v1/notifications/unread-count", headers=headers).json()["unreadCount"] == 0

