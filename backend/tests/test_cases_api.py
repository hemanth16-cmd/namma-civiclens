"""Complaint management API (Workstream 1), end to end through HTTP on the configured database:
lifecycle transitions, assignment, notes and public updates, work orders with after-repair evidence,
citizen verification, reinspection, the audit trail, role and department checks, public-projection
leakage, concurrency and rate limits."""

import io
import json
import sys
import threading
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.lifecycle import RuleViolation
from app.main import create_app
from app.ratelimit import InMemoryRateLimiter, RateLimitSettings, Rule
from app.staff import main as staff_cli
from app.store import ComplaintStore
from tests.test_complaints import payload
from tests.test_evidence import JPEG, WEBM

PASSWORD = "correct horse battery"
API = "/api/v1"
SECRET_NOTE = "INTERNAL-ONLY contractor quote 48,000 rupees"


def cli(db, monkeypatch, *args, stdin=None):
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin + "\n"))
    return staff_cli(["--db", str(db), *args])


@pytest.fixture
def org(isolated_db_path, monkeypatch):
    """Two departments; an official in each, a supervisor and an admin."""
    db = isolated_db_path
    for code, name in (("roads", "Road maintenance"), ("waste", "Solid waste")):
        assert cli(db, monkeypatch, "department", "add", "--code", code, "--name", name) == 0
    for user, role, dept in (("asha", "official", "roads"), ("bilal", "official", "waste"), ("ravi", "supervisor", None), ("meera", "admin", None)):
        assert cli(db, monkeypatch, "create", "--username", user, "--display-name", user.title() + " Staffname", "--role", role,
                   "--password-stdin", stdin=PASSWORD) == 0
        if dept:
            assert cli(db, monkeypatch, "join", "--username", user, "--department", dept) == 0
    return db


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
def api(make_api, org):
    return make_api()


def login(api, user):
    r = api.post(f"{API}/staff/sessions", json={"username": user, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


@pytest.fixture
def staff(api):
    return {u: login(api, u) for u in ("asha", "bilal", "ravi", "meera")}


def report(api, **overrides):
    receipt = api.post(f"{API}/complaints", json=payload(**overrides)).json()
    up = api.post(f"{API}/complaints/{receipt['id']}/evidence", files={"file": ("e.jpg", JPEG, "image/jpeg")},
                  headers={"Authorization": "Bearer " + receipt["uploadToken"]})
    assert up.status_code == 201, up.text
    return receipt


def act(api, headers, path, body, expect=200):
    r = api.post(f"{API}/staff{path}", json=body, headers=headers)
    assert r.status_code == expect, r.text
    return r.json()


def assign(api, headers, cid, dept="roads", assignee=None, reason="", expected="Submitted", expect=200):
    return act(api, headers, f"/complaints/{cid}/assignment",
               {"department": dept, "assignee": assignee, "reason": reason, "expectedStatus": expected}, expect)


def move(api, headers, cid, to, expected, explanation="", expect=200):
    return act(api, headers, f"/complaints/{cid}/transitions", {"to": to, "expectedStatus": expected, "explanation": explanation}, expect)


def public(api, cid):
    r = api.get(f"{API}/complaints/{cid}")
    assert r.status_code == 200
    return r.json()


def respond(api, cid, claim, response, expect=200):
    r = api.post(f"{API}/complaints/{cid}/citizen-responses", json={"claim": claim, "response": response})
    assert r.status_code == expect, r.text
    return r.json()


def resolve_flow(api, staff, cid):
    """Supervisor assigns to roads/asha; asha creates, starts and completes a work order, then marks it resolved."""
    assign(api, staff["ravi"], cid, assignee="asha")
    case = act(api, staff["asha"], f"/complaints/{cid}/work-orders", {"team": "Ward 4 road crew", "description": "Fill pothole", "dueAt": "2099-01-31"}, 201)
    wo = case["workOrders"][0]["id"]
    act(api, staff["asha"], f"/work-orders/{wo}/transitions", {"to": "In Progress", "expectedStatus": "Open"})
    act(api, staff["asha"], f"/work-orders/{wo}/transitions", {"to": "Completed", "expectedStatus": "In Progress", "note": "Filled and compacted"})
    return move(api, staff["asha"], cid, "Marked Resolved", "In Progress", "Pothole filled on 30 October."), wo


# ---------- Journeys ----------

def test_full_lifecycle_journey_with_audit(api, staff):
    receipt = report(api)
    cid = receipt["id"]
    case = assign(api, staff["ravi"], cid, assignee="asha")
    assert case["status"] == "Assigned" and case["department"] == "Road maintenance"
    assert case["assignment"] == {"department": {"code": "roads", "name": "Road maintenance"},
                                  "assignee": {"username": "asha", "displayName": "Asha Staffname"}}

    case = act(api, staff["asha"], f"/complaints/{cid}/work-orders", {"team": "Ward 4 road crew", "dueAt": "2099-01-31"}, 201)
    order = case["workOrders"][0]
    assert order["status"] == "Open" and order["evidence"][0]["phase"] == "before", "the reported photo is the 'before' reference"
    case = act(api, staff["asha"], f"/work-orders/{order['id']}/transitions", {"to": "In Progress", "expectedStatus": "Open"})
    assert case["status"] == "In Progress", "starting the first work order moves the complaint into progress"

    up = api.post(f"{API}/staff/work-orders/{order['id']}/evidence", files={"file": ("after.jpg", JPEG, "image/jpeg")}, headers=staff["asha"])
    assert up.status_code == 201, up.text
    assert set(up.json()) == {"evidenceId", "mediaType", "contentType", "byteSize", "sha256", "capturedAt", "uploadedAt"}

    move(api, staff["asha"], cid, "Marked Resolved", "In Progress", "x", expect=409)  # work order still open
    act(api, staff["asha"], f"/work-orders/{order['id']}/transitions", {"to": "Completed", "expectedStatus": "In Progress", "note": "Done"})
    case = move(api, staff["asha"], cid, "Marked Resolved", "In Progress", "Pothole filled.")
    assert case["status"] == "Marked Resolved" and case["verification"] == "Pending Verification"

    assert respond(api, cid, receipt["claim"], "Appears Resolved") == {"verification": "Citizen Confirmed", "changed": True}
    pub = public(api, cid)
    assert pub["status"] == "Marked Resolved" and pub["verification"] == "Citizen Confirmed"
    assert [e["type"] for e in pub["verificationLog"]] == ["response"] and pub["verificationLog"][0]["actor"] == "Citizen"
    assert [(t["status"], t["text"]) for t in pub["timeline"]] == [
        ("Submitted", "Report received by the Namma CivicLens API."),
        ("Assigned", "Assigned to Road maintenance."),
        ("Assigned", "Work order created for Ward 4 road crew; target date 2099-01-31."),
        ("In Progress", "Work started by Ward 4 road crew."),
        ("In Progress", "After-repair evidence was attached for review."),
        ("In Progress", "Work completed by Ward 4 road crew."),
        ("Marked Resolved", "Pothole filled."),
    ], "one public entry per event, no duplicates when an action also moves the status"

    audit = api.get(f"{API}/staff/complaints/{cid}/audit", headers=staff["ravi"]).json()
    actions = [e["action"] for e in reversed(audit)]
    assert actions == ["assigned", "status_changed", "work_order_created", "work_order_started", "status_changed",
                       "repair_evidence_added", "work_order_completed", "status_changed", "citizen_response"]
    assert audit[0]["actorType"] == "citizen" and audit[0]["actor"] is None
    assert audit[-1]["actor"] == "ravi" and audit[-1]["details"]["to"] == {"department": "roads", "assignee": "asha"}


def test_after_repair_evidence_is_streamed_to_staff_and_logged(api, staff, evidence_dir):
    cid = report(api)["id"]
    resolve_flow(api, staff, cid)
    case = api.get(f"{API}/staff/complaints/{cid}", headers=staff["asha"]).json()
    order = case["workOrders"][0]
    before = order["evidence"][0]
    got = api.get(f"{API}/staff/work-orders/{order['id']}/evidence/{before['evidenceId']}", headers=staff["asha"])
    assert got.status_code == 200 and got.content == JPEG and got.headers["cache-control"] == "no-store, private"
    assert api.get(f"{API}/staff/work-orders/{order['id']}/evidence/{before['evidenceId']}", headers=staff["bilal"]).status_code == 403
    assert api.get(f"{API}/staff/work-orders/{order['id']}/evidence/{before['evidenceId']}").status_code == 401
    assert api.get(f"{API}/staff/work-orders/{order['id']}/evidence/ev_{'x' * 22}", headers=staff["asha"]).status_code == 404


# ---------- Transition rules ----------

def test_invalid_transitions_explanations_and_stale_state(api, staff):
    cid = report(api)["id"]
    move(api, staff["ravi"], cid, "Marked Resolved", "Submitted", "skip", expect=403)
    move(api, staff["ravi"], cid, "In Progress", "Submitted", expect=403)
    move(api, staff["ravi"], cid, "Under Review", "Under Review", expect=409)  # stale expectedStatus
    r = api.post(f"{API}/staff/complaints/{cid}/transitions", json={"to": "Closed", "expectedStatus": "Submitted"}, headers=staff["ravi"])
    assert r.status_code == 422
    r = api.post(f"{API}/staff/complaints/{cid}/transitions", json={"to": "Under Review", "expectedStatus": "Submitted", "status": "x"},
                 headers=staff["ravi"])
    assert r.status_code == 422, "unknown fields are refused"
    move(api, staff["asha"], cid, "Under Review", "Submitted")  # triage queue: any official
    move(api, staff["ravi"], cid, "Assigned", "Under Review", expect=409)  # no department yet
    assign(api, staff["ravi"], cid, expected="Under Review")
    move(api, staff["ravi"], cid, "Under Review", "Assigned", expect=422)  # explanation required
    assert public(api, cid)["status"] == "Assigned"


def test_allowed_actions_reflect_role_and_state(api, staff):
    cid = report(api)["id"]
    official = {a["action"] for a in api.get(f"{API}/staff/complaints/{cid}", headers=staff["asha"]).json()["allowedActions"]}
    supervisor = {a["action"] for a in api.get(f"{API}/staff/complaints/{cid}", headers=staff["ravi"]).json()["allowedActions"]}
    assert "assign" not in official and "assign" in supervisor
    assert {"add_note", "add_public_update", "transition"} <= official


# ---------- Roles, departments, authentication ----------

def test_role_checks_are_enforced_on_the_server(api, staff):
    receipt = report(api)
    cid = receipt["id"]
    assign(api, staff["asha"], cid, expect=403)
    resolve_flow(api, staff, cid)
    act(api, staff["asha"], f"/complaints/{cid}/reinspections", {"note": "x", "expectedStatus": "Marked Resolved"}, 403)
    move(api, staff["asha"], cid, "In Progress", "Marked Resolved", "reopen", expect=403)
    assert api.get(f"{API}/staff/complaints/{cid}/audit", headers=staff["asha"]).status_code == 403
    cid2 = report(api)["id"]
    assign(api, staff["ravi"], cid2, assignee="asha")
    wo = act(api, staff["asha"], f"/complaints/{cid2}/work-orders", {"team": "crew"}, 201)["workOrders"][0]["id"]
    act(api, staff["asha"], f"/work-orders/{wo}/transitions", {"to": "Cancelled", "expectedStatus": "Open", "note": "dup"}, 403)
    act(api, staff["ravi"], f"/work-orders/{wo}/transitions", {"to": "Cancelled", "expectedStatus": "Open", "note": "dup"})
    act(api, staff["meera"], f"/complaints/{cid}/reinspections", {"note": "Admin check", "expectedStatus": "Marked Resolved"})


def test_officials_cannot_reach_other_departments(api, staff):
    cid = report(api)["id"]
    assign(api, staff["ravi"], cid, dept="roads")
    bilal = staff["bilal"]
    assert api.get(f"{API}/staff/complaints/{cid}", headers=bilal).status_code == 403
    assert api.get(f"{API}/staff/complaints/{cid}/evidence", headers=bilal).status_code == 403
    assert api.get(f"{API}/staff/complaints/{cid}/evidence/access", headers=bilal).status_code == 403
    act(api, bilal, f"/complaints/{cid}/notes", {"body": "peek"}, 403)
    act(api, bilal, f"/complaints/{cid}/updates", {"text": "hi"}, 403)
    move(api, bilal, cid, "In Progress", "Assigned", expect=403)
    act(api, bilal, f"/complaints/{cid}/work-orders", {"team": "crew"}, 403)
    listed = {i["id"] for i in api.get(f"{API}/staff/complaints", headers=bilal).json()["items"]}
    assert cid not in listed
    assert cid in {i["id"] for i in api.get(f"{API}/staff/complaints", headers=staff["asha"]).json()["items"]}
    assert cid in {i["id"] for i in api.get(f"{API}/staff/complaints", headers=staff["ravi"]).json()["items"]}
    triage = report(api)["id"]
    assert triage in {i["id"] for i in api.get(f"{API}/staff/complaints", headers=bilal).json()["items"]}, "unassigned queue is shared"


def test_role_and_membership_changes_apply_immediately(api, staff, org, monkeypatch):
    cid = report(api)["id"]
    assert cli(org, monkeypatch, "set-role", "--username", "ravi", "--role", "official") == 0
    assign(api, staff["ravi"], cid, expect=403)  # same session, new role
    assert cli(org, monkeypatch, "set-role", "--username", "asha", "--role", "supervisor") == 0
    assign(api, staff["asha"], cid, dept="waste")
    assert cli(org, monkeypatch, "leave", "--username", "bilal", "--department", "waste") == 0
    assert api.get(f"{API}/staff/complaints/{cid}", headers=staff["bilal"]).status_code == 403
    assert cli(org, monkeypatch, "disable", "--username", "bilal") == 0
    assert api.get(f"{API}/staff/complaints", headers=staff["bilal"]).status_code == 401


def test_every_management_route_needs_a_session(api):
    cid = report(api)["id"]
    wo = "wo_" + "a" * 16
    for method, path in (("post", f"/staff/complaints/{cid}/transitions"), ("post", f"/staff/complaints/{cid}/assignment"),
                         ("post", f"/staff/complaints/{cid}/notes"), ("post", f"/staff/complaints/{cid}/updates"),
                         ("post", f"/staff/complaints/{cid}/reinspections"), ("post", f"/staff/complaints/{cid}/work-orders"),
                         ("post", f"/staff/work-orders/{wo}/transitions"), ("post", f"/staff/work-orders/{wo}/evidence"),
                         ("get", "/staff/work-orders"), ("get", f"/staff/complaints/{cid}/audit"), ("get", "/staff/departments")):
        for headers in ({}, {"Authorization": "Bearer " + "x" * 43}):
            r = getattr(api, method)(API + path, headers=headers, **({"json": {}} if method == "post" else {}))
            assert r.status_code == 401, (path, r.status_code)


# ---------- Assignment ----------

def test_assignment_and_reassignment_rules(api, staff):
    cid = report(api)["id"]
    assign(api, staff["ravi"], cid, assignee="bilal", expect=422)          # bilal is not in roads
    assign(api, staff["ravi"], cid, dept="parks", expect=422)               # unknown department
    assign(api, staff["ravi"], cid, assignee="ravi")                        # supervisors need no membership
    assign(api, staff["ravi"], cid, assignee="ravi", expected="Assigned", reason="again", expect=409)  # no change
    assign(api, staff["ravi"], cid, dept="waste", assignee="bilal", expected="Assigned", expect=422)  # reason required
    case = assign(api, staff["ravi"], cid, dept="waste", assignee="bilal", expected="Assigned", reason="Garbage, not road damage")
    assert case["department"] == "Solid waste" and case["status"] == "Assigned"
    assert [(h["department"]["code"], h["assignee"], h["reason"]) for h in case["assignmentHistory"]] == [
        ("roads", "ravi", ""), ("waste", "bilal", "Garbage, not road damage")]
    texts = [t["text"] for t in public(api, cid)["timeline"]]
    assert "Reassigned to Solid waste." in texts and not any("bilal" in t.lower() or "ravi" in t.lower() for t in texts)


def test_inactive_department_cannot_be_assigned(api, staff, org, monkeypatch):
    assert cli(org, monkeypatch, "department", "disable", "--code", "roads") == 0
    assign(api, staff["ravi"], report(api)["id"], expect=422)
    assert [d["code"] for d in api.get(f"{API}/staff/departments", headers=staff["asha"]).json()] == ["waste"]


# ---------- Visibility ----------

def test_internal_notes_and_staff_identities_never_reach_the_public(api, staff):
    receipt = report(api)
    cid = receipt["id"]
    act(api, staff["ravi"], f"/complaints/{cid}/notes", {"body": SECRET_NOTE}, 201)
    act(api, staff["ravi"], f"/complaints/{cid}/updates", {"text": "Inspection scheduled for Monday.", "kind": "department_response"}, 201)
    resolve_flow(api, staff, cid)
    respond(api, cid, receipt["claim"], "Still Present")
    act(api, staff["ravi"], f"/complaints/{cid}/reinspections", {"note": "Citizen dispute; send inspector", "expectedStatus": "Marked Resolved"})
    raw = json.dumps(public(api, cid))
    for secret in (SECRET_NOTE, "Asha", "asha", "Ravi", "ravi", "Staffname", "Filled and compacted", "wo_", "ev_", "sn_", "au_",
                   receipt["claim"], str(payload()["gps"]["lat"])):
        assert secret not in raw, secret
    pub = public(api, cid)
    assert set(pub) == {"id", "category", "area", "status", "verification", "department", "updated", "timeline", "verificationLog", "publicEvidence", "mergedInto"}
    assert "Inspection scheduled for Monday." in [t["text"] for t in pub["timeline"]]
    assert {e["actor"] for e in pub["verificationLog"]} <= {"Citizen", "Civic team"}
    staff_view = api.get(f"{API}/staff/complaints/{cid}", headers=staff["ravi"]).json()
    assert [n["body"] for n in staff_view["notes"]] == [SECRET_NOTE] and staff_view["notes"][0]["author"]["username"] == "ravi"
    assert "claimHash" not in json.dumps(staff_view) and receipt["claim"] not in json.dumps(staff_view)


# ---------- Citizen verification and reinspection ----------

def test_citizen_verification_dispute_reinspection_and_new_round(api, staff):
    receipt = report(api)
    cid, claim = receipt["id"], receipt["claim"]
    respond(api, cid, claim, "Appears Resolved", expect=409)          # not resolved yet
    assert respond(api, cid, claim, "Still Present") == {"verification": "Pending Verification", "changed": False}
    resolve_flow(api, staff, cid)
    assert respond(api, cid, claim, "Still Present") == {"verification": "Disputed", "changed": True}
    assert respond(api, cid, claim, "Cannot Confirm")["changed"] is False
    assert public(api, cid)["status"] == "Marked Resolved", "citizen responses never change the lifecycle"

    case = act(api, staff["ravi"], f"/complaints/{cid}/reinspections",
               {"note": "Pothole reopened after rain", "reopen": True, "expectedStatus": "Marked Resolved"})
    assert case["status"] == "In Progress" and case["verification"] == "Reinspection Required"
    act(api, staff["ravi"], f"/complaints/{cid}/reinspections", {"note": "again", "expectedStatus": "In Progress"}, 409)
    case = move(api, staff["asha"], cid, "Marked Resolved", "In Progress", "Re-laid the patch.")
    assert case["verification"] == "Pending Verification", "resolving again opens a new verification round"
    assert respond(api, cid, claim, "Appears Resolved")["verification"] == "Citizen Confirmed"
    log = public(api, cid)["verificationLog"]
    assert [(e["type"], e.get("response"), e["to"]) for e in log] == [
        ("response", "Still Present", None), ("response", "Still Present", "Disputed"), ("response", "Cannot Confirm", None),
        ("state", None, "Reinspection Required"), ("state", None, "Pending Verification"), ("response", "Appears Resolved", "Citizen Confirmed")]


def test_citizen_responses_need_the_claim_code(api, staff):
    receipt = report(api)
    respond(api, receipt["id"], "wrong-code", "Still Present", expect=401)
    assert respond(api, "NCL-2026-ZZZZZZ", receipt["claim"], "Still Present", expect=401)["error"]["message"] == \
        respond(api, receipt["id"], "wrong-code", "Still Present", expect=401)["error"]["message"], "same message for unknown IDs"
    r = api.post(f"{API}/complaints/{receipt['id']}/citizen-responses?claim={receipt['claim']}", json={"response": "Still Present"})
    assert r.status_code == 422, "the claim code is accepted only in the body"
    r = api.post(f"{API}/complaints/{receipt['id']}/citizen-responses", json={"claim": receipt["claim"], "response": "Fixed"})
    assert r.status_code == 422


def test_citizen_responses_are_rate_limited(make_api, org):
    settings = replace(RateLimitSettings(), citizen_response_per_ip=Rule(3, 900))
    client = make_api(rate_limit_settings=settings, rate_limiter=InMemoryRateLimiter())
    # The create limit is per IP too; use a separate client address for creation.
    receipt = TestClient(client.app, client=("10.0.0.8", 5000)).post(f"{API}/complaints", json=payload()).json()
    codes = [client.post(f"{API}/complaints/{receipt['id']}/citizen-responses", json={"claim": "nope", "response": "Cannot Confirm"}).status_code
             for _ in range(4)]
    assert codes == [401, 401, 401, 429]


# ---------- Work orders ----------

def test_work_order_rules_list_and_overdue(api, staff):
    cid = report(api)["id"]
    act(api, staff["asha"], f"/complaints/{cid}/work-orders", {"team": "crew"}, 409)  # not assigned yet
    assign(api, staff["ravi"], cid, assignee="asha")
    act(api, staff["asha"], f"/complaints/{cid}/work-orders", {"team": "crew", "dueAt": "2026-02-30"}, 422)
    act(api, staff["asha"], f"/complaints/{cid}/work-orders", {"team": "late crew", "dueAt": "2000-01-01"}, 201)
    case = act(api, staff["asha"], f"/complaints/{cid}/work-orders", {"team": "future crew", "dueAt": "2099-01-01", "assignee": "asha"}, 201)
    late, future = case["workOrders"]
    assert late["overdue"] and not future["overdue"] and future["assignee"] == "asha"
    act(api, staff["asha"], f"/work-orders/{late['id']}/transitions", {"to": "Completed", "expectedStatus": "Open", "note": "x"}, 409)
    act(api, staff["asha"], f"/work-orders/{late['id']}/transitions", {"to": "In Progress", "expectedStatus": "In Progress"}, 409)  # stale
    act(api, staff["asha"], f"/work-orders/{late['id']}/transitions", {"to": "In Progress", "expectedStatus": "Open"})
    act(api, staff["asha"], f"/work-orders/{late['id']}/transitions", {"to": "Completed", "expectedStatus": "In Progress", "note": " "}, 422)

    mine = api.get(f"{API}/staff/work-orders", headers=staff["asha"]).json()
    assert mine["total"] == 2 and [o["team"] for o in mine["items"]] == ["future crew", "late crew"]
    assert [o["team"] for o in api.get(f"{API}/staff/work-orders?overdue=true", headers=staff["asha"]).json()["items"]] == ["late crew"]
    assert api.get(f"{API}/staff/work-orders", headers=staff["bilal"]).json()["total"] == 0
    assert api.get(f"{API}/staff/work-orders?department=roads", headers=staff["ravi"]).json()["total"] == 2
    assert api.get(f"{API}/staff/work-orders?status=Bogus", headers=staff["ravi"]).status_code == 422
    history = [h["action"] for h in api.get(f"{API}/staff/complaints/{cid}", headers=staff["ravi"]).json()["workOrders"][0]["history"]]
    assert history == ["created", "started"]


def test_repair_evidence_rules(api, staff):
    cid = report(api)["id"]
    assign(api, staff["ravi"], cid, assignee="asha")
    wo = act(api, staff["asha"], f"/complaints/{cid}/work-orders", {"team": "crew"}, 201)["workOrders"][0]["id"]
    url = f"{API}/staff/work-orders/{wo}/evidence"
    assert api.post(url, files={"file": ("a.webm", WEBM, "video/webm")}, headers=staff["asha"]).status_code == 422
    assert api.post(url, files={"file": ("a.jpg", JPEG, "image/jpeg")}, headers=staff["bilal"]).status_code == 403
    assert api.post(url, files={"file": ("a.jpg", JPEG, "image/jpeg")}).status_code == 401
    assert api.post(f"{API}/staff/work-orders/wo_{'b' * 16}/evidence", files={"file": ("a.jpg", JPEG, "image/jpeg")},
                    headers=staff["asha"]).status_code == 404
    assert api.post(url, files={"file": ("a.jpg", JPEG, "image/jpeg")}, headers=staff["asha"]).status_code == 201
    assert api.post(url, files={"file": ("b.jpg", JPEG, "image/jpeg")}, headers=staff["asha"]).status_code == 201, "several after photos are fine"
    act(api, staff["ravi"], f"/work-orders/{wo}/transitions", {"to": "Cancelled", "expectedStatus": "Open", "note": "duplicate"})
    assert api.post(url, files={"file": ("c.jpg", JPEG, "image/jpeg")}, headers=staff["asha"]).status_code == 409
    phases = [e["phase"] for e in api.get(f"{API}/staff/complaints/{cid}", headers=staff["asha"]).json()["workOrders"][0]["evidence"]]
    assert phases == ["before", "after", "after"]
    # The citizen's original upload is unaffected: still exactly one original, still idempotent.
    assert api.get(f"{API}/staff/complaints/{cid}/evidence", headers=staff["asha"]).content == JPEG


# ---------- Concurrency ----------

def test_concurrent_transitions_one_wins_the_other_conflicts(isolated_db_path, org):
    store = ComplaintStore(isolated_db_path)
    store.initialize()
    record, _ = store.create(payload())
    supervisor = store.get_staff("ravi")["pk"]
    results, barrier = [], threading.Barrier(4)

    def attempt():
        barrier.wait()
        try:
            store.transition(record["id"], supervisor, "Under Review", "Submitted")
            results.append("ok")
        except RuleViolation as exc:
            results.append(exc.status)

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results, key=str) == [409, 409, 409, "ok"]
    assert [e["action"] for e in store.audit_trail(record["id"], supervisor)] == ["status_changed"]
    store.close()


def test_existing_submission_tracking_and_public_shape_are_unchanged(api):
    receipt = report(api)
    pub = public(api, receipt["id"])
    assert pub["status"] == "Submitted" and pub["verification"] == "Pending Verification" and pub["department"] == "Awaiting civic review"
    assert pub["timeline"] == [{"status": "Submitted", "text": "Report received by the Namma CivicLens API.", "date": pub["timeline"][0]["date"]}]


def test_direct_repair_upload_publication_and_reopening(api, staff):
    receipt = report(api, shareEvidence=True)
    cid = receipt['id']
    url = f'{API}/staff/complaints/{cid}/repair-evidence'
    assert api.post(url, files={'file': ('repair.jpg', JPEG, 'image/jpeg')}).status_code == 401
    upload = api.post(url, files={'file': ('repair.jpg', JPEG, 'image/jpeg')}, headers=staff['asha'])
    assert upload.status_code == 201, upload.text
    eid = upload.json()['evidenceId']
    case = api.get(f'{API}/staff/complaints/{cid}', headers=staff['asha']).json()
    assert case['workOrders'] == [], 'direct evidence must not manufacture a work order'
    assert case['repairEvidence'][0]['evidenceId'] == eid
    before = public(api, cid)['publicEvidence']
    assert [e['phase'] for e in before] == ['before']
    assert api.get(f'{API}/complaints/{cid}/public-evidence/{before[0]["evidenceId"]}').content == JPEG
    public_url = f'{API}/complaints/{cid}/public-evidence/{eid}'
    assert api.get(public_url).status_code == 404, 'unfinished repair is not public'
    private_url = f'{API}/staff/complaints/{cid}/repair-evidence/{eid}'
    assert api.get(private_url, headers=staff['asha']).content == JPEG
    # Admin can correct any operational stage, but must explain and use the latest version.
    move(api, staff['meera'], cid, 'Marked Resolved', 'Submitted', 'Repair inspected and completed.')
    media = public(api, cid)['publicEvidence']
    assert [e['phase'] for e in media] == ['before', 'after']
    assert set(media[1]) == {'evidenceId', 'phase', 'mediaType', 'uploadedAt'}
    got = api.get(public_url)
    assert got.status_code == 200 and got.content == JPEG
    assert got.headers['cache-control'] == 'no-store, private'
    move(api, staff['meera'], cid, 'Under Construction', 'Marked Resolved', 'Repair failed inspection; reopening.')
    assert api.get(public_url).status_code == 404, 'publication must be rechecked after reopening'
    assert [e['phase'] for e in public(api, cid)['publicEvidence']] == ['before']


def test_private_original_and_other_case_evidence_cannot_be_published(api, staff):
    cid = report(api, shareEvidence=False)['id']
    assign(api, staff['ravi'], cid)
    url = f'{API}/staff/complaints/{cid}/repair-evidence'
    assert api.post(url, files={'file': ('repair.jpg', JPEG, 'image/jpeg')}, headers=staff['bilal']).status_code == 403
    upload = api.post(url, files={'file': ('repair.jpg', JPEG, 'image/jpeg')}, headers=staff['asha'])
    assert upload.status_code == 201
    case = api.get(f'{API}/staff/complaints/{cid}', headers=staff['asha']).json()
    original = case['evidence']['evidenceId']
    move(api, staff['meera'], cid, 'Marked Resolved', 'Assigned', 'Inspected by administrator.')
    assert [e['phase'] for e in public(api, cid)['publicEvidence']] == ['after']
    assert api.get(f'{API}/complaints/{cid}/public-evidence/{original}').status_code == 404
    other = report(api)['id']
    eid = upload.json()['evidenceId']
    assert api.get(f'{API}/complaints/{other}/public-evidence/{eid}').status_code == 404
    assert api.post(url, files={'file': ('video.webm', WEBM, 'video/webm')}, headers=staff['asha']).status_code == 422


def test_extended_stages_persist_and_keep_citizen_verification_independent(api, staff):
    cid = report(api)['id']
    assign(api, staff['ravi'], cid)
    current = 'Assigned'
    for stage in ['Under Construction', 'On Hold', 'In Progress', 'Work Completed', 'Verification In Progress', 'Marked Resolved']:
        moved = move(api, staff['asha'], cid, stage, current, 'Progress checked on site.')
        assert moved['status'] == stage
        assert public(api, cid)['status'] == stage
        assert moved['verification'] == 'Pending Verification'
        current = stage
    move(api, staff['asha'], cid, 'Under Construction', current, 'Reopen', expect=403)
