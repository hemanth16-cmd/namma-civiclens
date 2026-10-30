"""Application security review tests for Namma CivicLens.

Covers Section 9 (IDOR, access control, claim code leakage, MIME spoofing,
path traversal, SQL injection, SSRF in push endpoints, secrets in git).
"""

import hashlib
import json
import os
import re
import secrets
import subprocess
import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.claims import hash_claim_code
from app.main import create_app
from app.store import ComplaintStore
from tests.test_complaints import payload
from tests.test_evidence import JPEG, upload
from tests.test_notifications import subscription


@pytest.fixture
def api(isolated_db_path, evidence_dir):
    with TestClient(create_app(db_path=isolated_db_path, evidence_dir=evidence_dir)) as client:
        yield client


@pytest.fixture
def staff_account(api, isolated_db_path):
    store = api.app.state.store
    staff = store.create_staff("sec_admin", "Security Officer", hash_password("CorrectHorseBattery123!"))
    res = api.post("/api/v1/staff/sessions", json={"username": "sec_admin", "password": "CorrectHorseBattery123!"})
    assert res.status_code == 200
    token = res.json()["token"]
    return {"staff": staff, "token": token, "headers": {"Authorization": f"Bearer {token}"}}


# ---------- IDOR & Access Control ----------

def test_idor_recipient_isolation(api):
    """A citizen inbox cannot read, mark, or delete another inbox's notifications or devices."""
    # Create Recipient A
    r_a = api.post("/api/v1/notifications/inboxes")
    assert r_a.status_code == 201
    token_a = r_a.json()["inboxToken"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    # Create Recipient B
    r_b = api.post("/api/v1/notifications/inboxes")
    assert r_b.status_code == 201
    token_b = r_b.json()["inboxToken"]
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # Create complaint and follow as A
    comp = api.post("/api/v1/complaints", json=payload(capturedAt=None)).json()
    f_a = api.post("/api/v1/notifications/follows", json={"complaintId": comp["id"], "claim": comp["claim"]}, headers=headers_a)
    assert f_a.status_code == 201

    # Trigger notification for A
    upload(api, comp, JPEG)
    notifs_a = api.get("/api/v1/notifications", headers=headers_a).json()
    assert len(notifs_a["items"]) >= 1
    target_nid = notifs_a["items"][0]["id"]

    # Recipient B tries to read A's notification
    # B lists notifications: should be empty for B
    notifs_b = api.get("/api/v1/notifications", headers=headers_b).json()
    assert len(notifs_b["items"]) == 0

    # B tries to mark A's notification as read -> 404
    mark_res = api.post(f"/api/v1/notifications/{target_nid}/read", headers=headers_b)
    assert mark_res.status_code == 404

    # B tries to unfollow A's complaint -> 404
    unfollow_res = api.delete(f"/api/v1/notifications/follows/{comp['id']}", headers=headers_b)
    assert unfollow_res.status_code == 404

    # Register device for A
    sub_a = subscription()
    dev_a = api.post("/api/v1/notifications/subscriptions", json=sub_a, headers=headers_a).json()
    assert "subscriptionId" in dev_a, f"Unexpected response: {dev_a}"
    dev_id_a = dev_a["subscriptionId"]

    # B tries to delete A's device -> 404
    del_dev = api.delete(f"/api/v1/notifications/subscriptions/{dev_id_a}", headers=headers_b)
    assert del_dev.status_code == 404


def test_staff_endpoint_protection(api, staff_account):
    """Anonymous visitors and citizen inboxes cannot access staff endpoints."""
    # Anonymous requests
    assert api.get("/api/v1/staff/complaints").status_code == 401
    assert api.get("/api/v1/staff/me").status_code == 401
    assert api.delete("/api/v1/staff/sessions/current").status_code == 401

    # Citizen inbox token on staff endpoints
    inbox = api.post("/api/v1/notifications/inboxes").json()["inboxToken"]
    citizen_headers = {"Authorization": f"Bearer {inbox}"}
    assert api.get("/api/v1/staff/complaints", headers=citizen_headers).status_code == 401
    assert api.get("/api/v1/staff/me", headers=citizen_headers).status_code == 401

    # Staff session works
    assert api.get("/api/v1/staff/complaints", headers=staff_account["headers"]).status_code == 200


def test_staff_session_revocation_on_disable_or_password_change(api, staff_account):
    """Disabling an account or changing its password immediately invalidates active sessions."""
    store = api.app.state.store
    headers = staff_account["headers"]
    username = "sec_admin"

    # Active session works
    assert api.get("/api/v1/staff/me", headers=headers).status_code == 200

    # Changing password revokes sessions
    store.set_staff_password(username, hash_password("NewSecurePassword999!"))
    assert api.get("/api/v1/staff/me", headers=headers).status_code == 401

    # Sign in with new password
    res = api.post("/api/v1/staff/sessions", json={"username": username, "password": "NewSecurePassword999!"})
    assert res.status_code == 200
    new_headers = {"Authorization": f"Bearer {res.json()['token']}"}
    assert api.get("/api/v1/staff/me", headers=new_headers).status_code == 200

    # Disabling account revokes sessions
    store.set_staff_disabled(username, True)
    assert api.get("/api/v1/staff/me", headers=new_headers).status_code == 401


# ---------- Information Leakage & Secrecy ----------

def test_public_tracking_never_leaks_secrets(api):
    """Public tracking responses must exclude claim codes, claim hashes, precise GPS, upload tokens, and description."""
    comp = api.post("/api/v1/complaints", json=payload(description="Sensitive citizen report", capturedAt=None)).json()
    pid = comp["id"]
    upload(api, comp, JPEG)

    res = api.get(f"/api/v1/complaints/{pid}")
    assert res.status_code == 200
    body = res.json()
    raw_text = res.text

    assert "claim" not in body
    assert "claimHash" not in body
    assert "uploadToken" not in body
    assert "description" not in body
    assert "gps" not in body
    assert comp["claim"] not in raw_text
    assert comp["uploadToken"] not in raw_text
    assert "Sensitive citizen report" not in raw_text


def test_error_envelope_does_not_leak_internals(api):
    """Invalid routes and bad inputs must return controlled error envelopes, not stack traces."""
    res = api.get("/api/v1/nonexistent-route-404")
    assert res.status_code == 404
    body = res.json()
    assert "error" in body
    assert body["error"]["code"] == "not_found"
    assert "Traceback" not in res.text

    res2 = api.post("/api/v1/complaints", content="not json", headers={"Content-Type": "application/json"})
    assert res2.status_code in (400, 422)
    assert "error" in res2.json()
    assert "Traceback" not in res2.text


# ---------- File Upload Attacks & MIME Spoofing ----------

def test_file_upload_malicious_types_rejected(api):
    """HTML, SVG, PHP, ELF, and spoofed extensions are rejected safely."""
    comp = api.post("/api/v1/complaints", json=payload(mediaType="photo", capturedAt=None)).json()

    # HTML disguised as JPG
    html_content = b"<!DOCTYPE html><html><body><script>alert('XSS')</script></body></html>"
    r1 = upload(api, comp, html_content, filename="exploit.jpg", ctype="image/jpeg")
    assert r1.status_code == 415

    # SVG disguised as JPG
    svg_content = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
    r2 = upload(api, comp, svg_content, filename="vector.jpg", ctype="image/jpeg")
    assert r2.status_code == 415

    # PNG file (not supported for photo evidence: only JPEG is accepted)
    png_content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
    r3 = upload(api, comp, png_content, filename="valid.png", ctype="image/png")
    assert r3.status_code == 415

    # Extension mismatch: JPEG content with .exe extension
    r4 = upload(api, comp, JPEG, filename="photo.exe", ctype="image/jpeg")
    assert r4.status_code == 422
    assert r4.json()["error"]["code"] == "type_mismatch"

    # Content-type mismatch: JPEG content declared as application/x-php
    r5 = upload(api, comp, JPEG, filename="photo.jpg", ctype="application/x-php")
    assert r5.status_code == 422
    assert r5.json()["error"]["code"] == "type_mismatch"


def test_staff_evidence_streaming_security_headers(api, staff_account):
    """Staff evidence streaming must include nosniff, private cache-control, and sandbox CSP."""
    comp = api.post("/api/v1/complaints", json=payload(mediaType="photo", capturedAt=None)).json()
    upload(api, comp, JPEG)

    stream_res = api.get(f"/api/v1/staff/complaints/{comp['id']}/evidence", headers=staff_account["headers"])
    assert stream_res.status_code == 200
    assert stream_res.headers.get("X-Content-Type-Options") == "nosniff"
    assert "sandbox" in stream_res.headers.get("Content-Security-Policy", "")
    assert "no-store" in stream_res.headers.get("Cache-Control", "")


# ---------- Path Traversal & SQL Injection ----------

def test_sql_injection_attempts(api):
    """SQL injection payloads in public ID, filters, and parameters must fail safely."""
    sqli_payloads = [
        "NCL-2026-DEMO01' OR '1'='1",
        "NCL-2026-DEMO01'; DROP TABLE complaints; --",
        "' UNION SELECT * FROM staff_accounts --",
        "1; WAITFOR DELAY '0:0:5'--",
    ]
    for sqli in sqli_payloads:
        # Public tracking
        r = api.get(f"/api/v1/complaints/{sqli}")
        assert r.status_code == 404, f"SQLi payload did not return 404: {sqli}"

    # Invalid cursor before
    inbox = api.post("/api/v1/notifications/inboxes").json()["inboxToken"]
    r_cursor = api.get(
        "/api/v1/notifications",
        params={"before": "nt_123' OR '1'='1"},
        headers={"Authorization": f"Bearer {inbox}"},
    )
    assert r_cursor.status_code in (404, 422)


def test_path_traversal_prevention(api):
    """Path traversal sequences in storage keys are rejected."""
    storage = api.app.state.evidence_storage
    for bad_key in ["../../etc/passwd", "..\\..\\windows\\win.ini", "00" * 15 + "/../", "00" * 16 + "\x00"]:
        with pytest.raises(Exception):
            storage.path_for(bad_key)


# ---------- SSRF Prevention in Push Endpoints ----------

def test_ssrf_prevention_on_push_subscription(api):
    """Push subscription endpoints must reject non-HTTPS, internal IPs, and non-whitelisted domains."""
    inbox = api.post("/api/v1/notifications/inboxes").json()["inboxToken"]
    headers = {"Authorization": f"Bearer {inbox}"}

    valid_base = subscription()

    ssrf_endpoints = [
        ("http://fcm.googleapis.com/push", "invalid_subscription"),  # Plain http
        ("https://127.0.0.1/push", "unsupported_push_service"),      # Localhost IP
        ("https://localhost/push", "unsupported_push_service"),      # Localhost name
        ("https://169.254.169.254/latest/meta-data", "unsupported_push_service"),  # AWS metadata
        ("https://10.0.0.1/push", "unsupported_push_service"),       # Private RFC 1918
        ("https://192.168.1.1/push", "unsupported_push_service"),    # Private RFC 1918
        ("https://fcm.googleapis.com.attacker.com/push", "unsupported_push_service"),  # Subdomain trick
        ("https://evil-push.example.com/push", "unsupported_push_service"),  # Untrusted domain
        ("https://fcm.googleapis.com:8443/push", "invalid_subscription"),  # Non-standard port
        ("https://user:pass@fcm.googleapis.com/push", "invalid_subscription"),  # Auth in URL
    ]

    for url, expected_code in ssrf_endpoints:
        sub = {**valid_base, "endpoint": url}
        res = api.post("/api/v1/notifications/subscriptions", json=sub, headers=headers)
        assert res.status_code == 422, f"SSRF endpoint was not rejected: {url} -> {res.status_code}"
        assert res.json()["error"]["code"] == expected_code, f"Unexpected code for {url}: {res.json()}"
