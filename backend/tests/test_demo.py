"""The demonstration-account command (app/demo.py): explicit, guarded, idempotent, and the accounts it
makes sign in through the normal resident and staff endpoints with their normal permissions."""

import pytest
from fastapi.testclient import TestClient

from app import demo
from tests.dbutil import POSTGRES_MODE
from tests.test_complaints import payload
from app.main import create_app
from app.store import ComplaintStore

API = "/api/v1"
# The suite's PostgreSQL mode uses an isolated test schema, which is what --allow-postgres is for.
FLAGS = ["--confirm-demo", *(["--allow-postgres"] if POSTGRES_MODE else [])]
CREDS = {"resident": {"email": "resident@civiclens.demo", "password": "CivicDemo2026!"},
         "admin": {"username": "demo_admin", "password": "CivicDemo2026!"}}


@pytest.fixture
def api(isolated_db_path, evidence_dir):
    with TestClient(create_app(db_path=isolated_db_path, evidence_dir=evidence_dir), client=("10.0.0.3", 1)) as client:
        yield client


def test_nothing_happens_without_confirmation(isolated_db_path, capsys):
    assert demo.main(["seed", "--db", str(isolated_db_path)]) == 2
    assert "--confirm-demo" in capsys.readouterr().err
    store = ComplaintStore(isolated_db_path)
    store.initialize()
    assert store.get_resident(demo.DEMO_RESIDENT_EMAIL) is None and store.get_staff(demo.DEMO_ADMIN_USERNAME) is None


@pytest.mark.parametrize("value", ["production", "PROD"])
def test_refuses_in_production(isolated_db_path, monkeypatch, value, capsys):
    monkeypatch.setenv("CIVICLENS_ENV", value)
    assert demo.main(["seed", "--db", str(isolated_db_path), "--confirm-demo"]) == 2
    assert "production" in capsys.readouterr().err


def test_postgres_needs_a_second_flag(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:secret-pw@db.example.supabase.co:5432/postgres")
    assert demo.main(["seed", "--confirm-demo"]) == 2
    out = capsys.readouterr()
    assert "--allow-postgres" in out.err and "secret-pw" not in out.out + out.err, "the URL password is never printed"


def test_seed_is_idempotent_and_the_accounts_work(isolated_db_path, api, capsys):
    assert demo.main(["seed", "--db", str(isolated_db_path), *FLAGS]) == 0
    out = capsys.readouterr().out
    assert "created" in out and "CivicDemo2026!" not in out, "the password is never printed"
    r = api.post(f"{API}/residents/sessions", json=CREDS["resident"])
    assert r.status_code == 200 and r.json()["resident"]["demo"] is True
    resident = {"Authorization": "Bearer " + r.json()["token"]}
    s = api.post(f"{API}/staff/sessions", json=CREDS["admin"])
    assert s.status_code == 200 and s.json()["staff"]["role"] == "admin"
    staff = {"Authorization": "Bearer " + s.json()["token"]}

    # A second run changes nothing and keeps the open sessions.
    assert demo.main(["seed", "--db", str(isolated_db_path), *FLAGS]) == 0
    assert "unchanged" in capsys.readouterr().out
    assert api.get(f"{API}/residents/me", headers=resident).status_code == 200
    assert api.get(f"{API}/staff/me", headers=staff).status_code == 200

    # The demo identities keep their separate domains.
    assert api.get(f"{API}/staff/me", headers=resident).status_code == 401
    assert api.get(f"{API}/residents/me", headers=staff).status_code == 401


def test_seed_repairs_a_changed_or_disabled_account(isolated_db_path, api):
    store = ComplaintStore(isolated_db_path)
    demo.seed(store)
    store.set_staff_role(demo.DEMO_ADMIN_USERNAME, "official")
    store.set_staff_disabled(demo.DEMO_ADMIN_USERNAME, True)
    store.update_resident(demo.DEMO_RESIDENT_EMAIL, disabled=True)
    assert api.post(f"{API}/residents/sessions", json=CREDS["resident"]).status_code == 401
    assert demo.seed(store) == {"resident": "updated", "admin": "updated"}
    assert store.get_staff(demo.DEMO_ADMIN_USERNAME)["role"] == "admin"
    assert api.post(f"{API}/residents/sessions", json=CREDS["resident"]).status_code == 200
    assert api.post(f"{API}/staff/sessions", json=CREDS["admin"]).status_code == 200


def test_remove_disables_and_signs_out(isolated_db_path, api):
    store = ComplaintStore(isolated_db_path)
    demo.seed(store)
    token = api.post(f"{API}/residents/sessions", json=CREDS["resident"]).json()["token"]
    assert demo.main(["remove", "--db", str(isolated_db_path), *FLAGS]) == 0
    assert api.get(f"{API}/residents/me", headers={"Authorization": "Bearer " + token}).status_code == 401
    assert api.post(f"{API}/staff/sessions", json=CREDS["admin"]).status_code == 401


def test_the_api_never_creates_demo_accounts_on_its_own(isolated_db_path, api):
    store = ComplaintStore(isolated_db_path)
    assert store.get_resident(demo.DEMO_RESIDENT_EMAIL) is None
    assert store.get_staff(demo.DEMO_ADMIN_USERNAME) is None
    assert api.post(f"{API}/residents/sessions", json=CREDS["resident"]).status_code == 401


def test_the_demo_admin_can_run_the_whole_admin_journey(isolated_db_path, api):
    """List, open, view photo and video evidence, read status and history, assign, and manage work orders,
    all through the existing staff API and role checks."""
    from tests.test_evidence import JPEG, WEBM

    store = ComplaintStore(isolated_db_path)
    demo.seed(store)
    store.create_department("roads", "Road maintenance")
    staff = {"Authorization": "Bearer " + api.post(f"{API}/staff/sessions", json=CREDS["admin"]).json()["token"]}
    photo = api.post(f"{API}/complaints", json=payload()).json()
    video = api.post(f"{API}/complaints", json=payload(mediaType="video")).json()
    for receipt, content, name, ctype in ((photo, JPEG, "e.jpg", "image/jpeg"), (video, WEBM, "e.webm", "video/webm")):
        up = api.post(f"{API}/complaints/{receipt['id']}/evidence", files={"file": (name, content, ctype)},
                      headers={"Authorization": "Bearer " + receipt["uploadToken"]})
        assert up.status_code == 201, up.text

    listed = api.get(f"{API}/staff/complaints", headers=staff).json()
    assert {photo["id"], video["id"]} <= {i["id"] for i in listed["items"]}
    case = api.get(f"{API}/staff/complaints/{photo['id']}", headers=staff)
    assert case.status_code == 200 and case.json()["status"] == "Submitted" and case.json()["timeline"]
    got_photo = api.get(f"{API}/staff/complaints/{photo['id']}/evidence", headers=staff)
    assert got_photo.status_code == 200 and got_photo.content == JPEG and got_photo.headers["content-type"].startswith("image/jpeg")
    got_video = api.get(f"{API}/staff/complaints/{video['id']}/evidence", headers=staff)
    assert got_video.status_code == 200 and got_video.content == WEBM and got_video.headers["content-type"].startswith("video/webm")
    assert api.get(f"{API}/staff/complaints/{photo['id']}/evidence").status_code == 401, "evidence needs a staff session"
    assert len(api.get(f"{API}/staff/complaints/{photo['id']}/evidence/access", headers=staff).json()) == 1, "views are logged"

    assigned = api.post(f"{API}/staff/complaints/{photo['id']}/assignment", headers=staff,
                        json={"department": "roads", "assignee": None, "reason": "", "expectedStatus": "Submitted"})
    assert assigned.status_code == 200 and assigned.json()["status"] == "Assigned", assigned.text
    order = api.post(f"{API}/staff/complaints/{photo['id']}/work-orders", headers=staff, json={"team": "Ward crew"})
    assert order.status_code == 201, order.text
    wo = order.json()["workOrders"][0]
    started = api.post(f"{API}/staff/work-orders/{wo['id']}/transitions", headers=staff, json={"to": "In Progress", "expectedStatus": wo["status"]})
    assert started.status_code == 200, started.text
    assert api.get(f"{API}/staff/work-orders", headers=staff).status_code == 200
    audit = api.get(f"{API}/staff/complaints/{photo['id']}/audit", headers=staff)
    assert audit.status_code == 200 and {"assigned", "work_order_created"} & {a["action"] for a in audit.json()}
