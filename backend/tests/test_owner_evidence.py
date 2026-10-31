"""Owner evidence viewing through the shared retrieval service (app/evidence_access.py).

The same uploaded file is viewed by staff (staff session), by a passerby with the private claim code,
and by the resident who owns the complaint. All three get identical bytes and response headers from one
implementation; each path keeps its own authorisation and access record.
"""

import hashlib
from contextlib import closing
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from tests import dbutil

from app import auth
from app.main import create_app
from app.ratelimit import RateLimitSettings, Rule
from tests.test_complaints import payload
from tests.test_evidence import JPEG, WEBM
from tests.test_supabase_storage import new_complaint as supabase_complaint, supabase_api  # noqa: F401  (fixture)
from tests.test_supabase_storage import fake, supabase_env  # noqa: F401  (fixtures)

API = "/api/v1"
STAFF_PASSWORD = "correct horse battery"
SAME_HEADERS = ("content-type", "content-length", "content-disposition", "cache-control", "x-content-type-options", "content-security-policy")


@pytest.fixture
def make_api(isolated_db_path, evidence_dir):
    clients = []

    def make(**kwargs):
        client = TestClient(create_app(db_path=isolated_db_path, evidence_dir=evidence_dir, **kwargs), client=("10.0.0.4", 1))
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def api(make_api):
    return make_api()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def staff(api):
    api.app.state.store.create_staff("asha", "Asha", auth.hash_password(STAFF_PASSWORD), role="supervisor")
    return bearer(api.post(f"{API}/staff/sessions", json={"username": "asha", "password": STAFF_PASSWORD}).json()["token"])


def resident(api, email="asha@example.org"):
    return bearer(api.post(f"{API}/residents", json={"email": email, "password": "resident passphrase 1", "displayName": "R"}).json()["token"])


def report(api, content=JPEG, media="photo", ctype="image/jpeg", name="e.jpg", headers=None):
    receipt = api.post(f"{API}/complaints", json=payload(mediaType=media), headers=headers or {}).json()
    up = api.post(f"{API}/complaints/{receipt['id']}/evidence", files={"file": (name, content, ctype)}, headers=bearer(receipt["uploadToken"]))
    assert up.status_code == 201, up.text
    return receipt


def view(api, receipt, claim=None):
    return api.post(f"{API}/complaints/{receipt['id']}/evidence/view", json={"claim": claim or receipt["claim"]})


def audit(db, action="owner_evidence_view"):
    with closing(dbutil.connect(db)) as conn:
        return conn.execute("SELECT actor_type, details FROM audit_log WHERE action = ?", (action,)).fetchall()


def access_log_count(db):
    with closing(dbutil.connect(db)) as conn:
        return conn.execute("SELECT COUNT(*) FROM evidence_access_log").fetchone()[0]


@pytest.mark.parametrize("content,media,ctype,name", [(JPEG, "photo", "image/jpeg", "e.jpg"), (WEBM, "video", "video/webm", "e.webm")])
def test_staff_claim_code_and_resident_owner_get_the_same_file_from_one_implementation(api, isolated_db_path, content, media, ctype, name):
    st = staff(api)
    res = resident(api)
    receipt = report(api, content, media, ctype, name, headers=res)  # owned by the resident, claim code on the receipt
    as_staff = api.get(f"{API}/staff/complaints/{receipt['id']}/evidence", headers=st)
    as_claim = view(api, receipt)
    as_owner = api.get(f"{API}/residents/me/complaints/{receipt['id']}/evidence", headers=res)
    for r in (as_staff, as_claim, as_owner):
        assert r.status_code == 200, r.text
        assert r.content == content and hashlib.sha256(r.content).hexdigest() == hashlib.sha256(content).hexdigest()
    for h in SAME_HEADERS:
        assert as_staff.headers[h] == as_claim.headers[h] == as_owner.headers[h], h
    assert as_claim.headers["cache-control"] == "no-store, private"


def test_owner_views_are_audited_and_do_not_touch_the_staff_access_log_or_notify(api, isolated_db_path):
    st = staff(api)
    receipt = report(api)
    inbox = api.post(f"{API}/notifications/inboxes").json()["inboxToken"]
    api.post(f"{API}/notifications/follows", json={"complaintId": receipt["id"], "claim": receipt["claim"]}, headers=bearer(inbox))
    assert view(api, receipt).status_code == 200
    rows = audit(isolated_db_path)
    assert len(rows) == 1 and rows[0][0] == "citizen" and '"via":"claim_code"' in rows[0][1] and receipt["claim"] not in rows[0][1]
    assert access_log_count(isolated_db_path) == 0, "the staff access log holds staff views only"
    titles = [n["title"] for n in api.get(f"{API}/notifications", headers=bearer(inbox)).json()["items"]]
    assert not any("review" in t.lower() for t in titles), "an owner's own view is not reported as a staff review"
    assert api.get(f"{API}/staff/complaints/{receipt['id']}/evidence", headers=st).status_code == 200
    assert access_log_count(isolated_db_path) == 1, "staff views are still logged as before"
    titles = [n["title"] for n in api.get(f"{API}/notifications", headers=bearer(inbox)).json()["items"]]
    assert "Report under review" in titles, "a staff view still notifies followers, as before"
    accessed = api.get(f"{API}/staff/complaints/{receipt['id']}/evidence/access", headers=st).json()
    assert [a["username"] for a in accessed] == ["asha"]


def test_wrong_claim_code_or_unknown_id_is_refused_alike_and_nothing_is_streamed_or_logged(api, isolated_db_path):
    receipt = report(api)
    wrong = view(api, receipt, claim="00000000-0000-4000-8000-000000000000")
    unknown = api.post(f"{API}/complaints/NCL-2026-ZZZZZZ/evidence/view", json={"claim": receipt["claim"]})
    junk = api.post(f"{API}/complaints/../evidence/view", json={"claim": "x"})
    assert wrong.status_code == unknown.status_code == 403
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]
    assert junk.status_code in (403, 404)
    assert JPEG not in wrong.content and audit(isolated_db_path) == []


def test_claim_code_travels_only_in_the_body(api):
    receipt = report(api)
    assert api.get(f"{API}/complaints/{receipt['id']}/evidence/view?claim={receipt['claim']}").status_code == 405
    assert api.post(f"{API}/complaints/{receipt['id']}/evidence/view", json={"claim": receipt["claim"], "extra": 1}).status_code == 422


def test_no_evidence_yet(api):
    receipt = api.post(f"{API}/complaints", json=payload()).json()
    r = view(api, receipt)
    assert r.status_code == 404 and r.json()["error"]["code"] == "evidence_not_found"


def test_resident_route_needs_ownership(api):
    owner, other = resident(api), resident(api, "bina@example.org")
    owned = report(api, headers=owner)
    guest = report(api)
    assert api.get(f"{API}/residents/me/complaints/{owned['id']}/evidence", headers=other).status_code == 404
    assert api.get(f"{API}/residents/me/complaints/{guest['id']}/evidence", headers=owner).status_code == 404, "unclaimed: not theirs"
    assert api.post(f"{API}/residents/me/claims", json={"id": guest["id"], "claim": guest["claim"]}, headers=owner).status_code == 200
    assert api.get(f"{API}/residents/me/complaints/{guest['id']}/evidence", headers=owner).content == JPEG, "claimed: now theirs"
    assert api.get(f"{API}/residents/me/complaints/{owned['id']}/evidence").status_code == 401


def test_credentials_stay_in_their_own_domain(api):
    st, res = staff(api), resident(api)
    receipt = report(api, headers=res)
    assert api.get(f"{API}/residents/me/complaints/{receipt['id']}/evidence", headers=st).status_code == 401, "staff token is not a resident"
    assert api.get(f"{API}/staff/complaints/{receipt['id']}/evidence", headers=res).status_code == 401, "resident token is not staff"
    assert api.get(f"{API}/staff/complaints/{receipt['id']}/evidence").status_code == 401
    up_token = {"Authorization": "Bearer " + receipt["uploadToken"]}
    assert api.get(f"{API}/staff/complaints/{receipt['id']}/evidence", headers=up_token).status_code == 401
    assert api.get(f"{API}/residents/me/complaints/{receipt['id']}/evidence", headers=up_token).status_code == 401


def test_responses_never_reveal_storage_keys_or_urls(api, isolated_db_path):
    receipt = report(api)
    with closing(dbutil.connect(isolated_db_path)) as conn:
        key = conn.execute("SELECT storage_key FROM evidence").fetchone()[0]
    r = view(api, receipt)
    assert key not in str(dict(r.headers)) and "http" not in r.headers.get("content-disposition", "")
    assert r.headers["content-disposition"] == f'inline; filename="{receipt["id"]}-evidence.jpg"'


def test_owner_views_are_rate_limited(make_api):
    api = make_api(rate_limit_settings=replace(RateLimitSettings(), owner_evidence_per_ip=Rule(2, 900)))
    receipt = report(api)
    assert [view(api, receipt).status_code for _ in range(3)] == [200, 200, 429]


def test_owner_view_reads_private_supabase_storage_like_staff(supabase_api, fake):  # noqa: F811
    receipt, up = supabase_complaint(supabase_api)
    assert up.status_code == 201
    r = supabase_api.post(f"{API}/complaints/{receipt['id']}/evidence/view", json={"claim": receipt["claim"]})
    assert r.status_code == 200 and r.content == JPEG, "streamed from the private bucket through the API"
    assert "supabase" not in str(dict(r.headers)).lower()


def _evidence_id(db, public_id):
    with closing(dbutil.connect(db)) as conn:
        return conn.execute("SELECT e.evidence_id FROM evidence e JOIN complaints c ON c.pk = e.complaint_pk WHERE c.public_id = ?",
                            (public_id,)).fetchone()[0]


def test_my_complaints_per_file_route_is_the_shared_service_audited_and_owner_only(api, isolated_db_path):
    """GET /residents/me/complaints/{id}/evidence/{evidenceId} (My Complaints) streams through the same service,
    writes an owner audit entry and refuses anyone else; staff views are still the only access-log rows."""
    owner, other = resident(api), resident(api, "bina@example.org")
    receipt = report(api, headers=owner)
    ev = _evidence_id(isolated_db_path, receipt["id"])
    url = f"{API}/residents/me/complaints/{receipt['id']}/evidence/{ev}"
    got = api.get(url, headers=owner)
    assert got.status_code == 200 and got.content == JPEG
    assert got.headers["cache-control"] == "no-store, private" and got.headers["content-security-policy"] == "default-src 'none'; sandbox"
    rows = audit(isolated_db_path)
    assert len(rows) == 1 and '"via":"resident"' in rows[0][1] and ev in rows[0][1]
    assert access_log_count(isolated_db_path) == 0
    assert api.get(url, headers=other).status_code == 404
    assert api.get(url).status_code == 401
    assert api.get(f"{API}/residents/me/complaints/{receipt['id']}/evidence/ev_{'x' * 22}", headers=owner).status_code == 404
    assert len(audit(isolated_db_path)) == 1, "refused requests are not audited as views"


def test_my_complaints_per_file_route_is_rate_limited(make_api, isolated_db_path):
    api = make_api(rate_limit_settings=replace(RateLimitSettings(), owner_evidence_per_ip=Rule(2, 900)))
    owner = resident(api)
    receipt = report(api, headers=owner)
    url = f"{API}/residents/me/complaints/{receipt['id']}/evidence/{_evidence_id(isolated_db_path, receipt['id'])}"
    assert [api.get(url, headers=owner).status_code for _ in range(3)] == [200, 200, 429]
