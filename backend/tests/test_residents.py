"""Resident accounts and the three identities: anonymous passerby, signed-in resident, staff.

Covers registration, sign-in/out, sessions, profile, owned-complaint listing, claiming with the private
claim code, preferences, and the boundaries: passerby complaints stay unowned, a resident never sees
another resident's data, resident and staff tokens are not interchangeable, and public tracking never
reveals ownership.
"""

import json
from contextlib import closing
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from tests import dbutil

from app.main import create_app
from app.ratelimit import RateLimitSettings, Rule
from app.store import ComplaintStore
from tests.test_complaints import payload
from tests.test_staff import PASSWORD as STAFF_PASSWORD, run_cli, sign_in as staff_sign_in

PASSWORD = "resident passphrase 1"
API = "/api/v1"


@pytest.fixture
def make_api(isolated_db_path, evidence_dir):
    clients = []

    def make(**kwargs):
        client = TestClient(create_app(db_path=isolated_db_path, evidence_dir=evidence_dir, **kwargs), client=("10.0.0.9", 5000))
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def api(make_api):
    return make_api()


def register(api, email="asha@example.org", name="Asha", password=PASSWORD):
    return api.post(f"{API}/residents", json={"email": email, "password": password, "displayName": name})


def resident_token(api, email="asha@example.org", name="Asha") -> str:
    r = register(api, email, name)
    assert r.status_code == 201, r.text
    return r.json()["token"]


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def submit(api, token: str | None = None, **overrides):
    return api.post(f"{API}/complaints", json=payload(**overrides), headers=bearer(token) if token else {})


def owner_row(db_path, public_id):
    with closing(dbutil.connect(db_path)) as conn:
        return tuple(conn.execute("SELECT owner_resident_pk, owned_via, owned_at FROM complaints WHERE public_id = ?",
                                  (public_id,)).fetchone())


# ---------- 1-3: passerby and resident submission ----------

def test_a_passerby_reports_without_any_account(api, isolated_db_path):
    r = submit(api)
    assert r.status_code == 201
    body = r.json()
    assert body["id"].startswith("NCL-") and body["claim"], "public ID and private claim code on the receipt"
    assert owner_row(isolated_db_path, body["id"]) == (None, None, None), "a passerby complaint is never owned"
    assert api.get(f"{API}/complaints/{body['id']}").status_code == 200, "public tracking works"


def test_passerby_complaints_never_appear_in_any_resident_account(api):
    token = resident_token(api)
    guest = submit(api).json()
    mine = api.get(f"{API}/residents/me/complaints", headers=bearer(token)).json()
    assert mine["total"] == 0 and guest["id"] not in json.dumps(mine)
    assert api.get(f"{API}/residents/me/complaints/{guest['id']}", headers=bearer(token)).status_code == 404


def test_a_signed_in_resident_owns_what_they_submit(api, isolated_db_path):
    token = resident_token(api)
    body = submit(api, token).json()
    pk, via, at = owner_row(isolated_db_path, body["id"])
    assert pk is not None and via == "submitted" and at
    items = api.get(f"{API}/residents/me/complaints", headers=bearer(token)).json()["items"]
    assert [i["id"] for i in items] == [body["id"]]
    assert items[0]["ownedVia"] == "submitted" and items[0]["description"] == "Deep pothole near the bus stop"
    assert "claim" not in items[0] and "claimHash" not in json.dumps(items)


def test_owner_cannot_be_set_from_the_request_body(api, isolated_db_path):
    r = api.post(f"{API}/complaints", json={**payload(), "owner": "resident@civiclens.demo"})
    assert r.status_code == 422, "extra fields such as owner are refused"
    r = api.post(f"{API}/complaints", json={**payload(), "role": "resident"})
    assert r.status_code == 422


def test_a_stale_or_foreign_bearer_token_is_refused_not_treated_as_anonymous(api, isolated_db_path, monkeypatch):
    assert submit(api, "x" * 43).status_code == 401
    run_cli(isolated_db_path, "create", "--username", "asha", "--display-name", "Asha", "--password-stdin",
            stdin=STAFF_PASSWORD, monkeypatch=monkeypatch)
    staff = staff_sign_in(api).json()["token"]
    assert submit(api, staff).status_code == 401, "a staff token is not a resident session"
    with closing(dbutil.connect(isolated_db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM complaints").fetchone()[0] == 0


def test_idempotent_replay_keeps_the_owner_and_is_bound_to_it(api):
    token = resident_token(api)
    other = resident_token(api, "bina@example.org", "Bina")
    key, body = {"Idempotency-Key": "k" * 32}, payload()
    first = api.post(f"{API}/complaints", json=body, headers={**key, **bearer(token)})
    again = api.post(f"{API}/complaints", json=body, headers={**key, **bearer(token)})
    assert first.status_code == 201 and again.status_code == 200 and again.json()["id"] == first.json()["id"]
    assert api.post(f"{API}/complaints", json=body, headers={**key, **bearer(other)}).status_code == 409
    assert api.post(f"{API}/complaints", json=body, headers=key).status_code == 409, "nor can a passerby replay it"


# ---------- 4-5: sign-in ----------

def test_sign_in_sign_out_and_session_validation(api):
    resident_token(api)
    bad = api.post(f"{API}/residents/sessions", json={"email": "asha@example.org", "password": "wrong password!!"})
    assert bad.status_code == 401
    unknown = api.post(f"{API}/residents/sessions", json={"email": "nobody@example.org", "password": PASSWORD})
    assert unknown.status_code == 401 and unknown.json()["error"]["message"] == bad.json()["error"]["message"]
    ok = api.post(f"{API}/residents/sessions", json={"email": "  ASHA@example.org ", "password": PASSWORD})
    assert ok.status_code == 200 and ok.headers["cache-control"] == "no-store"
    token = ok.json()["token"]
    me = api.get(f"{API}/residents/me", headers=bearer(token)).json()
    assert me == {"residentId": me["residentId"], "email": "asha@example.org", "displayName": "Asha", "demo": False, "createdAt": me["createdAt"]}
    assert api.delete(f"{API}/residents/sessions/current", headers=bearer(token)).status_code == 204
    assert api.get(f"{API}/residents/me", headers=bearer(token)).status_code == 401, "sign-out ends the session"


def test_registration_rules(api):
    assert register(api, password="short").status_code == 422
    assert register(api, email="not-an-email").status_code == 422
    assert register(api).status_code == 201
    assert register(api, email="ASHA@example.org").status_code == 409, "one account per email, case-insensitive"


def test_expired_and_disabled_accounts_are_signed_out(make_api, isolated_db_path):
    from datetime import timedelta
    api = make_api(resident_session_ttl=timedelta(seconds=-1))
    token = resident_token(api)
    assert api.get(f"{API}/residents/me", headers=bearer(token)).status_code == 401, "an expired session is refused"
    api2 = make_api()
    token = api2.post(f"{API}/residents/sessions", json={"email": "asha@example.org", "password": PASSWORD}).json()["token"]
    ComplaintStore(db_path=isolated_db_path).update_resident("asha@example.org", disabled=True)
    assert api2.get(f"{API}/residents/me", headers=bearer(token)).status_code == 401


def test_profile_update(api):
    token = resident_token(api)
    r = api.patch(f"{API}/residents/me", json={"displayName": "Asha K"}, headers=bearer(token))
    assert r.status_code == 200 and r.json()["displayName"] == "Asha K"
    assert api.patch(f"{API}/residents/me", json={"email": "x@y.org", "displayName": "A"}, headers=bearer(token)).status_code == 422


# ---------- 6-9: claiming and isolation ----------

def test_claiming_a_guest_complaint_with_its_claim_code(api, isolated_db_path):
    guest = submit(api).json()
    with closing(dbutil.connect(isolated_db_path)) as conn:
        before = conn.execute("SELECT claim_hash, created_at, status FROM complaints WHERE public_id = ?", (guest["id"],)).fetchone()
        timeline_before = conn.execute("SELECT COUNT(*) FROM complaint_timeline").fetchone()[0]
    token = resident_token(api)
    r = api.post(f"{API}/residents/me/claims", json={"id": guest["id"].lower(), "claim": guest["claim"]}, headers=bearer(token))
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "claimed" and r.json()["complaint"]["ownedVia"] == "claimed"
    with closing(dbutil.connect(isolated_db_path)) as conn:
        after = conn.execute("SELECT claim_hash, created_at, status FROM complaints WHERE public_id = ?", (guest["id"],)).fetchone()
        assert tuple(after) == tuple(before), "claim hash, submission time and status are unchanged"
        assert conn.execute("SELECT COUNT(*) FROM complaint_timeline").fetchone()[0] == timeline_before, "public timeline unchanged"
        audit = conn.execute("SELECT actor_type, action FROM audit_log WHERE action = 'resident_claim'").fetchall()
        assert [tuple(a) for a in audit] == [("citizen", "resident_claim")]
    assert [i["id"] for i in api.get(f"{API}/residents/me/complaints", headers=bearer(token)).json()["items"]] == [guest["id"]]
    again = api.post(f"{API}/residents/me/claims", json={"id": guest["id"], "claim": guest["claim"]}, headers=bearer(token))
    assert again.json()["outcome"] == "already_yours"
    # The claim code keeps working for the existing claim-code features.
    assert api.post(f"{API}/complaints/{guest['id']}/upload-token", json={"claim": guest["claim"]}).status_code == 200


def test_a_wrong_claim_code_or_unknown_id_is_rejected_alike(api, isolated_db_path):
    guest = submit(api).json()
    token = resident_token(api)
    wrong = api.post(f"{API}/residents/me/claims", json={"id": guest["id"], "claim": "00000000-0000-4000-8000-000000000000"}, headers=bearer(token))
    unknown = api.post(f"{API}/residents/me/claims", json={"id": "NCL-2026-ZZZZZZ", "claim": guest["claim"]}, headers=bearer(token))
    junk = api.post(f"{API}/residents/me/claims", json={"id": "<script>", "claim": "x"}, headers=bearer(token))
    assert wrong.status_code == unknown.status_code == junk.status_code == 403
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]
    assert owner_row(isolated_db_path, guest["id"])[0] is None


def test_claiming_needs_a_resident_session(api):
    guest = submit(api).json()
    assert api.post(f"{API}/residents/me/claims", json={"id": guest["id"], "claim": guest["claim"]}).status_code == 401


def test_one_resident_cannot_take_or_see_another_residents_complaint(api):
    asha = resident_token(api)
    bina = resident_token(api, "bina@example.org", "Bina")
    owned = submit(api, asha).json()
    # Without the code: refused. With the code: still refused, because it is already owned.
    assert api.post(f"{API}/residents/me/claims", json={"id": owned["id"], "claim": "wrong"}, headers=bearer(bina)).status_code == 403
    taken = api.post(f"{API}/residents/me/claims", json={"id": owned["id"], "claim": owned["claim"]}, headers=bearer(bina))
    assert taken.status_code == 409 and taken.json()["error"]["code"] == "already_claimed"
    assert "asha" not in taken.text.lower(), "the refusal names nobody"
    assert api.get(f"{API}/residents/me/complaints/{owned['id']}", headers=bearer(bina)).status_code == 404
    assert api.get(f"{API}/residents/me/complaints", headers=bearer(bina)).json()["total"] == 0
    assert api.get(f"{API}/residents/me", headers=bearer(bina)).json()["email"] == "bina@example.org"


def test_public_tracking_never_reveals_the_owner(api):
    token = resident_token(api)
    owned = submit(api, token).json()
    public = api.get(f"{API}/complaints/{owned['id']}")
    text = public.text.lower()
    for secret in ("asha", "example.org", "res_", "owner", "ownedvia", owned["claim"], "13.100712", "deep pothole"):
        assert secret not in text, secret
    assert set(public.json()) == {"id", "category", "area", "status", "verification", "department", "updated", "timeline",
                                  "verificationLog", "mergedInto"}
    # The community list (all of Bengaluru) shows the same complaint without any owner details either.
    listed = api.get(f"{API}/complaints", params={"limit": 100})
    assert owned["id"] in listed.text
    for secret in ("asha", "example.org", "res_", "owner", "ownedvia", owned["claim"], "13.100712", "deep pothole"):
        assert secret not in listed.text.lower(), secret


# ---------- Preferences ----------

def test_preferences_persist_per_account(api):
    asha = resident_token(api)
    bina = resident_token(api, "bina@example.org", "Bina")
    assert api.get(f"{API}/residents/me/preferences", headers=bearer(asha)).json() == {"updates": True, "verification": True, "hotspots": False}
    r = api.put(f"{API}/residents/me/preferences", json={"updates": False, "verification": True, "hotspots": True}, headers=bearer(asha))
    assert r.status_code == 200
    assert api.get(f"{API}/residents/me/preferences", headers=bearer(asha)).json() == {"updates": False, "verification": True, "hotspots": True}
    assert api.get(f"{API}/residents/me/preferences", headers=bearer(bina)).json()["updates"] is True, "per account"
    assert api.put(f"{API}/residents/me/preferences", json={"role": "official"}, headers=bearer(asha)).status_code == 422


# ---------- 10-13: staff boundary ----------

def test_resident_and_staff_tokens_are_not_interchangeable(api, isolated_db_path, monkeypatch):
    run_cli(isolated_db_path, "create", "--username", "asha", "--display-name", "Asha", "--password-stdin",
            stdin=STAFF_PASSWORD, monkeypatch=monkeypatch)
    staff = staff_sign_in(api).json()["token"]
    resident = resident_token(api)
    for path in ("/staff/me", "/staff/complaints", "/staff/work-orders", "/staff/departments"):
        assert api.get(API + path).status_code == 401, f"anonymous {path}"
        assert api.get(API + path, headers=bearer(resident)).status_code == 401, f"resident token on {path}"
        assert api.get(API + path, headers=bearer(staff)).status_code == 200, f"staff token on {path}"
    for method, path in (("get", "/residents/me"), ("get", "/residents/me/complaints"), ("get", "/residents/me/preferences")):
        assert getattr(api, method)(API + path, headers=bearer(staff)).status_code == 401, f"staff token on {path}"
    assert api.post(f"{API}/residents/me/claims", json={"id": "NCL-2026-AAAAAA", "claim": "x"}, headers=bearer(staff)).status_code == 401
    assert api.delete(f"{API}/residents/sessions/current", headers=bearer(staff)).status_code == 401
    assert api.get(f"{API}/staff/me", headers=bearer(staff)).status_code == 200, "the staff session is untouched"


def test_staff_still_see_and_manage_resident_and_guest_complaints(api, isolated_db_path, monkeypatch):
    run_cli(isolated_db_path, "create", "--username", "asha", "--display-name", "Asha", "--password-stdin",
            stdin=STAFF_PASSWORD, monkeypatch=monkeypatch)
    ComplaintStore(db_path=isolated_db_path).initialize()
    with closing(dbutil.connect(isolated_db_path)) as conn:
        conn.execute("UPDATE staff_accounts SET role = 'admin'")
        conn.commit()
    staff = staff_sign_in(api).json()["token"]
    guest = submit(api).json()
    owned = submit(api, resident_token(api)).json()
    listed = api.get(f"{API}/staff/complaints", headers=bearer(staff)).json()
    assert {guest["id"], owned["id"]} <= {i["id"] for i in listed["items"]}
    case = api.get(f"{API}/staff/complaints/{owned['id']}", headers=bearer(staff))
    assert case.status_code == 200 and "example.org" not in case.text, "staff case view carries no resident email"


# ---------- Rate limits ----------

def test_sign_in_and_claim_attempts_are_rate_limited(make_api):
    settings = replace(RateLimitSettings(), resident_login_per_ip=Rule(2, 900), resident_claim_per_ip=Rule(2, 900))
    api = make_api(rate_limit_settings=settings)
    token = resident_token(api)
    for _ in range(2):
        api.post(f"{API}/residents/sessions", json={"email": "asha@example.org", "password": "wrong password!!"})
    assert api.post(f"{API}/residents/sessions", json={"email": "asha@example.org", "password": PASSWORD}).status_code == 429
    for _ in range(2):
        api.post(f"{API}/residents/me/claims", json={"id": "NCL-2026-AAAAAA", "claim": "x"}, headers=bearer(token))
    assert api.post(f"{API}/residents/me/claims", json={"id": "NCL-2026-AAAAAA", "claim": "x"}, headers=bearer(token)).status_code == 429
