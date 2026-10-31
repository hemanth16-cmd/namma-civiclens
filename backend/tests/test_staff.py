import hashlib
import io
import sys
from contextlib import closing
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from tests import dbutil

from app import auth
from app.main import create_app
from app.ratelimit import InMemoryRateLimiter, RateLimitSettings, Rule
from app.staff import main as staff_cli
from app.store import ComplaintStore
from tests.test_complaints import payload
from tests.test_evidence import JPEG, WEBM

PASSWORD = "correct horse battery"


def run_cli(db, *args, stdin=None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin + "\n"))
    return staff_cli(["--db", str(db), *args])


@pytest.fixture
def staff_account(isolated_db_path, monkeypatch):
    assert run_cli(isolated_db_path, "create", "--username", "Asha", "--display-name", "Asha (ward office)", "--password-stdin",
                   stdin=PASSWORD, monkeypatch=monkeypatch) == 0
    return "asha"


@pytest.fixture
def make_api(isolated_db_path, evidence_dir):
    clients = []

    def make(**kwargs):
        client = TestClient(create_app(db_path=isolated_db_path, evidence_dir=evidence_dir, **kwargs), client=("10.0.0.7", 5000))
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def api(make_api):
    return make_api()


def sign_in(api, username="asha", password=PASSWORD):
    return api.post("/api/v1/staff/sessions", json={"username": username, "password": password})


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def token(api, staff_account):
    response = sign_in(api)
    assert response.status_code == 200, response.text
    return response.json()["token"]


def new_complaint(api, media_type="photo", content=JPEG, filename="e.jpg", ctype="image/jpeg", upload=True):
    receipt = api.post("/api/v1/complaints", json=payload(mediaType=media_type)).json()
    if upload:
        assert api.post(f"/api/v1/complaints/{receipt['id']}/evidence", files={"file": (filename, content, ctype)},
                        headers=bearer(receipt["uploadToken"])).status_code == 201
    return receipt


def rows(db, sql, params=()):
    with closing(dbutil.connect(db)) as conn:
        return conn.execute(sql, params).fetchall()


# ---------- CLI ----------

def test_cli_creates_accounts_with_hashed_passwords(staff_account, isolated_db_path, capsys):
    [(username, display, role, stored)] = rows(isolated_db_path, "SELECT username, display_name, role, password_hash FROM staff_accounts")
    assert (username, display, role) == ("asha", "Asha (ward office)", "official")
    assert stored.startswith("scrypt$") and PASSWORD not in stored
    assert auth.verify_password(PASSWORD, stored)
    assert PASSWORD not in capsys.readouterr().out


def test_cli_rejects_bad_input(isolated_db_path, staff_account, monkeypatch, capsys):
    assert run_cli(isolated_db_path, "create", "--username", "asha", "--display-name", "Again", "--password-stdin", stdin=PASSWORD, monkeypatch=monkeypatch) == 1
    assert run_cli(isolated_db_path, "create", "--username", "bo", "--display-name", "Short name", "--password-stdin", stdin=PASSWORD, monkeypatch=monkeypatch) == 2
    assert run_cli(isolated_db_path, "create", "--username", "ravi", "--display-name", "Ravi", "--password-stdin", stdin="short", monkeypatch=monkeypatch) == 2
    assert run_cli(isolated_db_path, "create", "--username", "../x", "--display-name", "X", "--password-stdin", stdin=PASSWORD, monkeypatch=monkeypatch) == 2
    assert len(rows(isolated_db_path, "SELECT * FROM staff_accounts")) == 1
    out = capsys.readouterr()
    assert "short" not in out.out + out.err.replace("Short", "")


def test_cli_list_never_prints_hashes(isolated_db_path, staff_account, capsys):
    capsys.readouterr()
    assert run_cli(isolated_db_path, "list") == 0
    out = capsys.readouterr().out
    assert "asha" in out and "active" in out and "scrypt" not in out


def test_no_default_account_exists(api):
    assert sign_in(api, "admin", "admin").status_code == 401
    assert sign_in(api, "official@civiclens.demo", "CivicDemo2026").status_code == 401


# ---------- Sign-in and sessions ----------

def test_sign_in_returns_a_bearer_session(api, staff_account, isolated_db_path):
    response = sign_in(api, "  ASHA ")
    assert response.status_code == 200
    body = response.json()
    assert body["staff"] == {"username": "asha", "displayName": "Asha (ward office)", "role": "official"}
    assert body["expiresAt"].endswith("Z") and len(body["token"]) >= 40
    assert response.headers["cache-control"] == "no-store"
    [(stored,)] = rows(isolated_db_path, "SELECT token_hash FROM staff_sessions")
    assert stored == hashlib.sha256(body["token"].encode()).hexdigest()
    for f in isolated_db_path.parent.glob(isolated_db_path.name + "*"):
        assert body["token"].encode() not in f.read_bytes()
    assert api.get("/api/v1/staff/me", headers=bearer(body["token"])).json()["username"] == "asha"


def test_wrong_password_and_unknown_user_get_the_same_answer(api, staff_account):
    wrong, unknown = sign_in(api, password="nope nope nope"), sign_in(api, username="nobody")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert wrong.json()["error"]["message"] == "The username or password is incorrect."


def test_unknown_users_still_run_one_password_hash(api, staff_account, monkeypatch):
    calls = []
    real = auth.verify_password
    monkeypatch.setattr(auth, "verify_password", lambda *a: calls.append(1) or real(*a))
    sign_in(api, username="nobody")
    sign_in(api, password="wrong wrong wrong")
    assert len(calls) == 2


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer short"}, {"Authorization": "Bearer " + "x" * 43}, {"Authorization": "Basic YXNoYTpwdw=="}])
def test_staff_routes_require_a_valid_session(api, headers):
    for path in ("/api/v1/staff/me", "/api/v1/staff/complaints", "/api/v1/staff/complaints/NCL-2026-ZZZZZZ/evidence"):
        response = api.get(path, headers=headers)
        assert response.status_code == 401 and response.json()["error"]["code"] == "unauthorized"
        assert response.headers["www-authenticate"] == "Bearer"


def test_upload_tokens_are_not_staff_sessions(api, staff_account):
    receipt = new_complaint(api, upload=False)
    assert api.get("/api/v1/staff/complaints", headers=bearer(receipt["uploadToken"])).status_code == 401


def test_staff_sessions_cannot_upload_evidence(api, token):
    receipt = new_complaint(api, upload=False)
    response = api.post(f"/api/v1/complaints/{receipt['id']}/evidence", files={"file": ("e.jpg", JPEG, "image/jpeg")}, headers=bearer(token))
    assert response.status_code == 401


def test_sign_out_revokes_the_session(api, token):
    assert api.delete("/api/v1/staff/sessions/current", headers=bearer(token)).status_code == 204
    assert api.get("/api/v1/staff/me", headers=bearer(token)).status_code == 401
    assert api.delete("/api/v1/staff/sessions/current", headers=bearer(token)).status_code == 401


def test_sessions_expire(make_api, staff_account):
    short = make_api(staff_session_ttl=timedelta(seconds=-1))
    expired = sign_in(short).json()["token"]
    assert short.get("/api/v1/staff/me", headers=bearer(expired)).status_code == 401


def test_disabling_an_account_ends_its_sessions_and_blocks_sign_in(api, token, isolated_db_path):
    assert run_cli(isolated_db_path, "disable", "--username", "asha") == 0
    assert api.get("/api/v1/staff/me", headers=bearer(token)).status_code == 401
    assert sign_in(api).status_code == 401
    assert run_cli(isolated_db_path, "enable", "--username", "asha") == 0
    assert api.get("/api/v1/staff/me", headers=bearer(token)).status_code == 401, "re-enabling does not revive old sessions"
    assert sign_in(api).status_code == 200


def test_a_disabled_account_is_refused_even_if_its_sessions_were_not_revoked(api, token, isolated_db_path):
    # Disabled by some other path (direct database edit, a future admin tool) without touching sessions.
    with closing(dbutil.connect(isolated_db_path)) as conn:
        conn.execute("UPDATE staff_accounts SET disabled_at = '2026-10-01T00:00:00.000Z' WHERE username = 'asha'")
        conn.commit()
    assert rows(isolated_db_path, "SELECT COUNT(*) FROM staff_sessions WHERE revoked_at IS NULL") == [(1,)]
    assert api.get("/api/v1/staff/me", headers=bearer(token)).status_code == 401


def test_changing_the_password_ends_sessions(api, token, isolated_db_path, monkeypatch):
    assert run_cli(isolated_db_path, "set-password", "--username", "asha", "--password-stdin", stdin="a brand new passphrase", monkeypatch=monkeypatch) == 0
    assert api.get("/api/v1/staff/me", headers=bearer(token)).status_code == 401
    assert sign_in(api).status_code == 401
    assert sign_in(api, password="a brand new passphrase").status_code == 200


def test_sign_in_is_rate_limited_before_password_checks(make_api, staff_account, monkeypatch):
    settings = replace(RateLimitSettings(enabled=True), staff_login_per_ip=Rule(3, 900), staff_login_per_account=Rule(100, 3600))
    limited = make_api(rate_limit_settings=settings, rate_limiter=InMemoryRateLimiter(clock=lambda: 0.0))
    calls = []
    real = auth.verify_password
    monkeypatch.setattr(auth, "verify_password", lambda *a: calls.append(1) or real(*a))
    for _ in range(3):
        assert sign_in(limited, password="wrong wrong wrong").status_code == 401
    response = sign_in(limited)
    assert response.status_code == 429 and response.headers["retry-after"].isdigit()
    assert len(calls) == 3, "the limited attempt did not reach the password check"


def test_sign_in_is_rate_limited_per_account_across_addresses(isolated_db_path, evidence_dir, staff_account):
    settings = replace(RateLimitSettings(enabled=True), staff_login_per_ip=Rule(100, 900), staff_login_per_account=Rule(2, 3600))
    app = create_app(db_path=isolated_db_path, evidence_dir=evidence_dir, rate_limit_settings=settings, rate_limiter=InMemoryRateLimiter(clock=lambda: 0.0))
    with TestClient(app, client=("10.0.0.1", 1)) as a, TestClient(app, client=("10.0.0.2", 1)) as b:
        assert sign_in(a, password="wrong wrong wrong").status_code == 401
        assert sign_in(b, password="wrong wrong wrong").status_code == 401
        assert sign_in(b).status_code == 429


# ---------- Complaint list and detail ----------

def test_list_is_newest_first_paginated_and_free_of_secrets(api, token):
    first = new_complaint(api)
    second = new_complaint(api, upload=False)
    response = api.get("/api/v1/staff/complaints?limit=1", headers=bearer(token))
    body = response.json()
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert body["total"] == 2 and body["limit"] == 1 and [i["id"] for i in body["items"]] == [second["id"]]
    assert body["items"][0]["hasEvidence"] is False
    page2 = api.get("/api/v1/staff/complaints?limit=1&offset=1", headers=bearer(token)).json()
    assert [i["id"] for i in page2["items"]] == [first["id"]] and page2["items"][0]["hasEvidence"] is True
    for secret in (first["claim"], first["uploadToken"], "scrypt", "claimHash", "storageKey"):
        assert secret not in response.text + str(page2)
    assert api.get("/api/v1/staff/complaints?limit=1000", headers=bearer(token)).status_code == 422


def test_list_carries_map_fields_and_costs_two_queries_whatever_its_length(api, token):
    """The list feeds the admin dashboard and map, and must not cost a database round trip per complaint
    (from Render to Supabase each query takes ~0.2 s, so per-complaint queries hit the 10 s browser timeout)."""
    for _ in range(6):
        new_complaint(api)
    store = api.app.state.store
    real_connect = store.db.connect
    executed = []

    class Counting:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=()):
            executed.append(sql)
            return self._conn.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    store.db.connect = lambda: Counting(real_connect())
    try:
        items, total = store.list_complaints(100, 0)
    finally:
        store.db.connect = real_connect
    assert total == 6 and len(items) == 6 and len(executed) == 2, executed
    body = api.get("/api/v1/staff/complaints?limit=3", headers=bearer(token)).json()
    first = body["items"][0]
    assert set(first) == {"id", "title", "category", "area", "status", "verification", "department", "created", "updated",
                          "mediaType", "hasEvidence", "gps", "mergedInto"}
    assert set(first["gps"]) == {"lat", "lng", "accuracy", "sourceType"} and first["hasEvidence"] is True
    assert first["gps"]["lat"] == payload()["gps"]["lat"] and first["gps"]["lng"] == payload()["gps"]["lng"]


def test_detail_has_operational_fields_but_no_secrets(api, token, isolated_db_path):
    receipt = new_complaint(api)
    response = api.get(f"/api/v1/staff/complaints/{receipt['id']}", headers=bearer(token))
    body = response.json()
    assert body["gps"]["lat"] == 13.100712 and body["description"] == "Deep pothole near the bus stop"
    assert body["evidence"]["contentType"] == "image/jpeg" and body["evidence"]["byteSize"] == len(JPEG)
    [(key, claim_hash, token_hash)] = rows(isolated_db_path, "SELECT e.storage_key, c.claim_hash, c.upload_token_hash FROM evidence e JOIN complaints c ON c.pk = e.complaint_pk")
    for secret in (receipt["claim"], receipt["uploadToken"], key, claim_hash, token_hash, "storageKey", "claim"):
        assert secret not in response.text
    assert api.get("/api/v1/staff/complaints/NCL-2026-ZZZZZZ", headers=bearer(token)).status_code == 404


# ---------- Evidence viewing and audit log ----------

def test_staff_can_stream_evidence_and_every_view_is_logged(api, token, isolated_db_path):
    receipt = new_complaint(api)
    response = api.get(f"/api/v1/staff/complaints/{receipt['id']}/evidence", headers=bearer(token))
    assert response.status_code == 200
    assert response.content == JPEG
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["content-length"] == str(len(JPEG))
    assert response.headers["cache-control"] == "no-store, private"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"] == f'inline; filename="{receipt["id"]}-evidence.jpg"'
    api.get(f"/api/v1/staff/complaints/{receipt['id']}/evidence", headers=bearer(token))
    logged = rows(isolated_db_path, "SELECT a.username, l.action, l.client_ip FROM evidence_access_log l JOIN staff_accounts a ON a.pk = l.staff_pk")
    assert logged == [("asha", "view", "10.0.0.7")] * 2
    access = api.get(f"/api/v1/staff/complaints/{receipt['id']}/evidence/access", headers=bearer(token)).json()
    assert [(e["username"], e["action"]) for e in access] == [("asha", "view")] * 2


def test_video_evidence_streams_with_its_type(api, token):
    receipt = new_complaint(api, "video", WEBM, "e.webm", "video/webm")
    response = api.get(f"/api/v1/staff/complaints/{receipt['id']}/evidence", headers=bearer(token))
    assert response.content == WEBM and response.headers["content-type"] == "video/webm"


def test_unauthorised_requests_get_no_bytes_and_no_log(api, staff_account, isolated_db_path):
    receipt = new_complaint(api)
    for headers in ({}, bearer(receipt["uploadToken"])):
        response = api.get(f"/api/v1/staff/complaints/{receipt['id']}/evidence", headers=headers)
        assert response.status_code == 401 and JPEG[:100] not in response.content
    assert rows(isolated_db_path, "SELECT COUNT(*) FROM evidence_access_log") == [(0,)]


def test_complaint_without_evidence(api, token):
    receipt = new_complaint(api, upload=False)
    response = api.get(f"/api/v1/staff/complaints/{receipt['id']}/evidence", headers=bearer(token))
    assert response.status_code == 404 and response.json()["error"]["code"] == "evidence_not_found"
    assert api.get(f"/api/v1/staff/complaints/{receipt['id']}/evidence/access", headers=bearer(token)).json() == []


def test_missing_file_is_reported_as_unavailable_and_logs_nothing(api, token, isolated_db_path, evidence_dir):
    """A file that is confirmed absent (e.g. uploaded to another machine's local disk) is a permanent
    condition: 404 evidence_unavailable with an explanation, not 503 'try again'."""
    receipt = new_complaint(api)
    [(key,)] = rows(isolated_db_path, "SELECT storage_key FROM evidence")
    (evidence_dir / "objects" / key[:2] / key).unlink()
    response = api.get(f"/api/v1/staff/complaints/{receipt['id']}/evidence", headers=bearer(token))
    assert response.status_code == 404 and response.json()["error"]["code"] == "evidence_unavailable"
    assert "not in this server's storage" in response.json()["error"]["message"] and key not in response.text
    assert rows(isolated_db_path, "SELECT COUNT(*) FROM evidence_access_log") == [(0,)]


def test_evidence_in_an_unconfigured_backend_is_reported_as_unavailable(api, token, isolated_db_path):
    receipt = new_complaint(api)
    with closing(dbutil.connect(isolated_db_path)) as conn:
        conn.execute("UPDATE evidence SET storage_backend = 'supabase'")  # this test server has no Supabase Storage
        conn.commit()
    response = api.get(f"/api/v1/staff/complaints/{receipt['id']}/evidence", headers=bearer(token))
    assert response.status_code == 404 and response.json()["error"]["code"] == "evidence_unavailable"
    assert "Supabase Storage" in response.json()["error"]["message"]
    assert rows(isolated_db_path, "SELECT COUNT(*) FROM evidence_access_log") == [(0,)]


def test_unreadable_storage_that_cannot_confirm_absence_stays_503(api, token, isolated_db_path, monkeypatch):
    """An outage (the storage cannot even say whether the file exists) is still 503 storage_unavailable."""
    from app.store import StoreError
    receipt = new_complaint(api)
    storage = api.app.state.evidence_storages["local"]
    monkeypatch.setattr(storage, "open_read", lambda key: (_ for _ in ()).throw(StoreError("disk offline")))
    monkeypatch.setattr(storage, "exists", lambda key: (_ for _ in ()).throw(StoreError("disk offline")))
    response = api.get(f"/api/v1/staff/complaints/{receipt['id']}/evidence", headers=bearer(token))
    assert response.status_code == 503 and response.json()["error"]["code"] == "storage_unavailable"
    assert rows(isolated_db_path, "SELECT COUNT(*) FROM evidence_access_log") == [(0,)]


def test_failed_audit_logging_sends_nothing(api, token, monkeypatch):
    from app.store import StoreError
    receipt = new_complaint(api)
    monkeypatch.setattr(api.app.state.store, "log_evidence_access", lambda *a, **k: (_ for _ in ()).throw(StoreError("log down")))
    response = api.get(f"/api/v1/staff/complaints/{receipt['id']}/evidence", headers=bearer(token))
    assert response.status_code == 503 and JPEG[:100] not in response.content


def test_public_tracking_still_exposes_no_evidence(api, token):
    receipt = new_complaint(api)
    api.get(f"/api/v1/staff/complaints/{receipt['id']}/evidence", headers=bearer(token))
    public = api.get(f"/api/v1/complaints/{receipt['id']}").text
    assert "evidence" not in public and "asha" not in public and "13.100712" not in public


def test_store_level_session_lookup_ignores_revoked_and_disabled(isolated_db_path):
    store = ComplaintStore(isolated_db_path)
    store.initialize()
    store.create_staff("ravi", "Ravi", auth.hash_password(PASSWORD))
    session = auth.sign_in(store, "ravi", PASSWORD, timedelta(hours=1))
    token_hash = hashlib.sha256(session["token"].encode()).hexdigest()
    assert store.get_staff_session(token_hash)["username"] == "ravi"
    store.revoke_staff_session(token_hash)
    assert store.get_staff_session(token_hash) is None
