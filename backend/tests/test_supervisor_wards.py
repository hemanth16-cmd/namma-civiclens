"""Supervisor + ward/zone operations (named feature ward_zone_operations), end to end through HTTP and the
CLI on the configured database (SQLite, or PostgreSQL with CIVICLENS_TEST_DATABASE=postgres): the
e-Aasthi seed, boundary-only ward assignment, geographic scope, manual ward changes, multi-ward
allocation with history, and dashboard numbers checked against direct database counts."""

import json
from contextlib import closing

import pytest

from app import wards as wd
from app.store import ComplaintStore
from tests.test_cases_api import API, act, cli, login, report  # noqa: F401  (fixtures below reuse org/make_api)
from tests.test_cases_api import api, make_api, org, staff  # noqa: F401
from tests.test_residents import bearer, resident_token

HOME = (13.100712, 77.596334)  # the GPS in tests.test_complaints.payload


def _gps(lat, lng):
    return {"lat": lat, "lng": lng, "accuracy": 10, "sourceType": "gps", "source": "Device GPS"}


def _q(sql, params=()):
    with closing(ComplaintStore().db.connect()) as conn:
        return [tuple(r) for r in conn.execute(sql, params).fetchall()]


@pytest.fixture
def seeded(org, monkeypatch):
    assert cli(org, monkeypatch, "wards", "seed") == 0
    return org


@pytest.fixture
def ops(seeded, api, staff, monkeypatch):
    """Seeded wards; ravi (supervisor) scoped to zone 1 (Byatarayanapura); meera (admin) city-wide by role."""
    assert cli(seeded, monkeypatch, "scope", "grant", "--username", "ravi", "--zone", "1") == 0
    return api, staff


def zone_wards(api, headers):
    scope = api.get(f"{API}/staff/supervisor/scope", headers=headers).json()
    return {z["number"]: [w["number"] for w in z["wards"]] for z in scope["zones"]}


def set_ward(api, headers, cid, ward, reason="Checked on the map against the GPS point", expect=200):
    return act(api, headers, f"/complaints/{cid}/ward", {"ward": ward, "reason": reason}, expect)


# ---------- Seed ----------

def test_seed_counts_testing_ward_and_idempotency(seeded, monkeypatch, capsys):
    rows = wd.load_seed()
    assert len(rows) == 199 and sum(r["testing"] for r in rows) == 1
    counts = ComplaintStore().ward_counts()
    assert counts == {"zones": 11, "operationalZones": 10, "wards": 199, "operationalWards": 198, "activeWards": 198, "withBoundary": 0}
    assert _q("SELECT w.ward_number, w.active, w.testing, z.zone_number FROM wards w JOIN zones z ON z.pk = w.zone_pk WHERE w.testing = 1") == [(298, 0, 1, 20)]
    assert _q("SELECT COUNT(*) FROM wards w JOIN zones z ON z.pk = w.zone_pk WHERE z.zone_number = 7") == [(40,)]
    capsys.readouterr()
    assert cli(seeded, monkeypatch, "wards", "seed") == 0
    assert "0 inserted, 0 updated, 199 unchanged" in capsys.readouterr().out
    assert ComplaintStore().ward_counts() == counts
    assert _q("SELECT COUNT(*) FROM audit_log WHERE action = 'wards_seeded'") == [(1,)], "a no-op seed writes no audit record"


def test_seed_rejects_a_bad_file(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"wards": [{"ward_number": 1, "ward_name": "A", "zone_number": 1, "zone_name": "Z"},
                                         {"ward_number": 1, "ward_name": "B", "zone_number": 1, "zone_name": "Z"}]}))
    with pytest.raises(ValueError, match="Duplicate ward"):
        wd.load_seed(bad)


# ---------- Point in polygon and the unverified rule ----------

SQUARE = {"type": "Polygon", "coordinates": [[[77.59, 13.09], [77.60, 13.09], [77.60, 13.11], [77.59, 13.11], [77.59, 13.09]]]}


def test_locate_is_boundary_only():
    holed = {"type": "Polygon", "coordinates": [SQUARE["coordinates"][0],
                                                [[77.595, 13.099], [77.598, 13.099], [77.598, 13.102], [77.595, 13.102], [77.595, 13.099]]]}
    multi = {"type": "MultiPolygon", "coordinates": [[[[77.5, 12.9], [77.51, 12.9], [77.51, 12.91], [77.5, 12.91], [77.5, 12.9]]], SQUARE["coordinates"]]}
    assert wd.locate(None, None, [(1, SQUARE)]) == wd.WardMatch(None, "unverified", "no_gps")
    assert wd.locate(19.07, 72.87, [(1, SQUARE)]).reason == "outside_service_area"   # Mumbai
    assert wd.locate(*HOME, []) == wd.WardMatch(None, "unverified", "no_boundaries")
    assert wd.locate(*HOME, [(1, SQUARE)]) == wd.WardMatch(1, "auto", "boundary_match")
    assert wd.locate(12.97, 77.59, [(1, SQUARE)]).reason == "no_matching_boundary"
    assert wd.locate(*HOME, [(1, SQUARE), (2, SQUARE)]).reason == "overlapping_boundaries"
    assert wd.locate(*HOME, [(1, holed)]).reason == "no_matching_boundary", "a point in a hole is outside"
    assert wd.locate(12.905, 77.505, [(3, multi)]).ward_pk == 3
    for bad in ({"type": "Point", "coordinates": [77.5, 13]},
                {"type": "Polygon", "coordinates": [[[13.09, 77.59], [13.09, 77.60], [13.11, 77.60], [13.09, 77.59]]]},  # lat/lng swapped
                {"type": "Polygon", "coordinates": [[[77.59, 13.09], [77.60, 13.09], [77.60, 13.11]]]}):                 # not closed
        with pytest.raises(ValueError):
            wd.validate_boundary(bad)


def test_new_complaints_stay_unverified_without_boundaries(ops):
    api, staff = ops
    inside = report(api)
    mumbai = report(api, gps=_gps(19.0760, 72.8777))
    for cid, reason in ((inside["id"], "no_boundaries"), (mumbai["id"], "outside_service_area")):
        case = api.get(f"{API}/staff/complaints/{cid}", headers=staff["meera"]).json()
        assert case["ward"] is None and case["wardAssignmentStatus"] == "unverified"
        assert [(h["method"], h["reason"], h["to"]) for h in case["wardHistory"]] == [("unverified", reason, None)]
    case = api.get(f"{API}/staff/complaints/{inside['id']}", headers=staff["meera"]).json()
    assert case["wardHistory"][0]["gps"] == {"lat": HOME[0], "lng": HOME[1]}
    assert _q("SELECT COUNT(*) FROM complaints WHERE ward_pk IS NOT NULL") == [(0,)], \
        "the neighbourhood text ('Yelahanka', also a ward name) is never used to pick a ward"


def test_imported_boundary_assigns_automatically(ops, seeded, monkeypatch, tmp_path):
    api, staff = ops
    # A synthetic square for this test database only; real boundaries must come from an official file.
    file = tmp_path / "wards.geojson"
    file.write_text(json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"ward_no": "5"}, "geometry": SQUARE}]}))
    assert cli(seeded, monkeypatch, "wards", "import-boundaries", "--file", str(file), "--ward-property", "ward_no", "--source", "test fixture") == 0
    auto = report(api)
    elsewhere = report(api, gps=_gps(12.9716, 77.5946))
    a = api.get(f"{API}/staff/complaints/{auto['id']}", headers=staff["ravi"]).json()
    assert a["ward"]["number"] == 5 and a["ward"]["zone"]["number"] == 1 and a["wardAssignmentStatus"] == "auto"
    assert a["wardHistory"][0]["reason"] == "boundary_match"
    e = api.get(f"{API}/staff/complaints/{elsewhere['id']}", headers=staff["ravi"]).json()
    assert e["ward"] is None and e["wardHistory"][0]["reason"] == "no_matching_boundary"
    unknown = tmp_path / "unknown.geojson"
    unknown.write_text(json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"ward_no": 9999}, "geometry": SQUARE}]}))
    assert cli(seeded, monkeypatch, "wards", "import-boundaries", "--file", str(unknown), "--ward-property", "ward_no", "--source", "x") == 1


# ---------- Access control and scope ----------

ENDPOINTS = ("/staff/supervisor/scope", "/staff/supervisor/dashboard", "/staff/supervisor/complaints?wards=5", "/staff/supervisor/staff")


def test_officials_residents_and_anonymous_are_refused(ops):
    api, staff = ops
    cid = report(api)["id"]
    resident = bearer(resident_token(api))
    for path in ENDPOINTS:
        assert api.get(API + path, headers=staff["asha"]).status_code == 403, path
        assert api.get(API + path, headers=resident).status_code == 401, path   # a resident token is not a staff session
        assert api.get(API + path).status_code == 401, path
    body = {"complaints": [cid], "assignee": "asha", "department": "roads", "reason": "r"}
    assert api.post(f"{API}/staff/supervisor/allocations", json=body, headers=staff["asha"]).status_code == 403
    assert api.post(f"{API}/staff/complaints/{cid}/ward", json={"ward": 5, "reason": "r"}, headers=staff["asha"]).status_code == 403
    assert api.post(f"{API}/staff/complaints/{cid}/ward", json={"ward": 5, "reason": "r"}, headers=resident).status_code == 401
    assert _q("SELECT COUNT(*) FROM ward_allocations") == [(0,)] and _q("SELECT COUNT(*) FROM ward_assignment_history WHERE method = 'manual'") == [(0,)]
    case = api.get(f"{API}/staff/complaints/{cid}", headers=staff["asha"]).json()
    assert "set_ward" not in [a["action"] for a in case["allowedActions"]]


def test_scope_admin_city_wide_supervisor_by_grant(ops, seeded, monkeypatch):
    api, staff = ops
    admin = api.get(f"{API}/staff/supervisor/scope", headers=staff["meera"]).json()
    assert admin["cityWide"] and admin["wardCount"] == 198 and len(admin["zones"]) == 10, "testing ward is inactive, so not offered"
    ravi = api.get(f"{API}/staff/supervisor/scope", headers=staff["ravi"]).json()
    assert not ravi["cityWide"] and [z["number"] for z in ravi["zones"]] == [1] and ravi["wardCount"] == 22
    # A supervisor without any scope sees nothing (deny by default) until the CLI grants it.
    assert cli(seeded, monkeypatch, "create", "--username", "kiran", "--display-name", "Kiran", "--role", "supervisor",
               "--password-stdin", stdin="correct horse battery") == 0
    kiran = login(api, "kiran")
    assert api.get(f"{API}/staff/supervisor/scope", headers=kiran).json()["wardCount"] == 0
    assert api.get(f"{API}/staff/supervisor/complaints?wards=5", headers=kiran).status_code == 403
    assert cli(seeded, monkeypatch, "scope", "grant", "--username", "kiran", "--ward", "153") == 0
    assert api.get(f"{API}/staff/supervisor/scope", headers=kiran).json()["wardCount"] == 1
    assert cli(seeded, monkeypatch, "scope", "grant", "--username", "kiran", "--city") == 0
    assert api.get(f"{API}/staff/supervisor/scope", headers=kiran).json()["cityWide"] is True
    assert cli(seeded, monkeypatch, "scope", "revoke", "--username", "kiran", "--city") == 0
    assert api.get(f"{API}/staff/supervisor/scope", headers=kiran).json()["wardCount"] == 1
    # No API route changes roles or scope.
    assert api.post(f"{API}/staff/supervisor/scope", json={"city": True}, headers=staff["ravi"]).status_code in (404, 405)


def test_manual_ward_is_scoped_audited_and_keeps_gps(ops):
    api, staff = ops
    cid = report(api)["id"]
    wards = zone_wards(api, staff["meera"])
    zone1, zone2 = wards[1][0], wards[2][0]
    assert set_ward(api, staff["ravi"], cid, zone2, expect=403)["error"]["code"] == "out_of_scope"
    assert set_ward(api, staff["meera"], cid, 298, expect=422)["error"]["code"] == "inactive_ward"
    assert set_ward(api, staff["meera"], cid, 4242, expect=422)["error"]["code"] == "unknown_ward"
    assert api.post(f"{API}/staff/complaints/{cid}/ward", json={"ward": zone1, "reason": ""}, headers=staff["ravi"]).status_code == 422
    case = set_ward(api, staff["ravi"], cid, zone1)
    assert case["ward"]["number"] == zone1 and case["wardAssignmentStatus"] == "manual"
    assert case["gps"]["lat"] == HOME[0] and case["gps"]["lng"] == HOME[1]
    assert set_ward(api, staff["ravi"], cid, zone1, expect=409)["error"]["code"] == "no_change"
    # Moving it to zone 2 needs scope over both wards: ravi cannot, the admin can.
    assert set_ward(api, staff["ravi"], cid, zone2, expect=403)["error"]["code"] == "out_of_scope"
    case = set_ward(api, staff["meera"], cid, zone2, reason="Ward office confirmed it is across the boundary road")
    assert [(h["method"], h["from"], h["to"], h["by"]) for h in case["wardHistory"]] == [
        ("unverified", None, None, None), ("manual", None, zone1, "ravi"), ("manual", zone1, zone2, "meera")]
    assert all(h["gps"] == {"lat": HOME[0], "lng": HOME[1]} for h in case["wardHistory"])
    audit = [e for e in api.get(f"{API}/staff/complaints/{cid}/audit", headers=staff["meera"]).json() if e["action"] == "ward_set"]
    assert [(e["actor"], e["details"]["from"], e["details"]["to"]) for e in sorted(audit, key=lambda e: e["at"])] == [("ravi", None, zone1), ("meera", zone1, zone2)]
    # ravi no longer covers this complaint's ward.
    assert api.get(f"{API}/staff/complaints/{cid}/allocations", headers=staff["ravi"]).status_code == 403
    with pytest.raises(Exception):
        with closing(ComplaintStore().db.connect()) as conn, conn.transaction():
            conn.execute("UPDATE ward_assignment_history SET reason = 'rewritten'")
    with pytest.raises(Exception):
        with closing(ComplaintStore().db.connect()) as conn, conn.transaction():
            conn.execute("DELETE FROM ward_assignment_history")


# ---------- Multi-ward allocation ----------

def test_multi_ward_list_filters_and_bulk_allocation_history(ops):
    api, staff = ops
    w1, w2 = zone_wards(api, staff["meera"])[1][:2]
    a, b = report(api)["id"], report(api, category="Garbage accumulation")["id"]
    c = report(api)["id"]   # stays unverified
    set_ward(api, staff["ravi"], a, w1)
    set_ward(api, staff["ravi"], b, w2)
    listed = api.get(f"{API}/staff/supervisor/complaints?wards={w1},{w2}", headers=staff["ravi"]).json()
    assert listed["total"] == 2 and {i["id"] for i in listed["items"]} == {a, b}
    assert api.get(f"{API}/staff/supervisor/complaints?wards={w1},{w2}&category=Potholes", headers=staff["ravi"]).json()["total"] == 1
    assert api.get(f"{API}/staff/supervisor/complaints?wards={w1}&minAgeDays=1", headers=staff["ravi"]).json()["total"] == 0
    assert api.get(f"{API}/staff/supervisor/complaints?wards={w1}&maxAgeDays=1", headers=staff["ravi"]).json()["total"] == 1
    assert api.get(f"{API}/staff/supervisor/complaints?wards={w1}&status=Assigned", headers=staff["ravi"]).json()["total"] == 0
    assert [i["id"] for i in api.get(f"{API}/staff/supervisor/complaints?unverifiedWard=true", headers=staff["ravi"]).json()["items"]] == [c]
    assert api.get(f"{API}/staff/supervisor/complaints?wards=abc", headers=staff["ravi"]).status_code == 422

    body = {"complaints": [a, b], "department": "roads", "assignee": "asha", "priority": "high", "dueAt": "2026-12-01",
            "reason": "Monsoon backlog drive", "note": "Bring the cold-mix crew"}
    # An unverified complaint in the batch refuses the whole batch; nothing is written.
    refused = api.post(f"{API}/staff/supervisor/allocations", json={**body, "complaints": [a, b, c]}, headers=staff["ravi"])
    assert refused.status_code == 409 and c in refused.json()["error"]["message"]
    assert _q("SELECT COUNT(*) FROM ward_allocations") == [(0,)] and _q("SELECT COUNT(*) FROM complaint_assignments") == [(0,)]

    done = act(api, staff["ravi"], "/supervisor/allocations", body)
    assert done["changed"] == 2 and {r["outcome"] for r in done["results"]} == {"allocated"}
    case = api.get(f"{API}/staff/complaints/{a}", headers=staff["ravi"]).json()
    assert case["assignment"]["assignee"]["username"] == "asha" and case["status"] == "Assigned"
    assert case["priority"] == "high" and case["dueAt"] == "2026-12-01"
    assert [n["body"] for n in case["notes"]] == ["Bring the cold-mix crew"]
    public = api.get(f"{API}/complaints/{a}").json()
    assert "asha" not in json.dumps(public).lower() and "cold-mix" not in json.dumps(public), "staff names and internal notes stay internal"

    # The same allocation again, without a note, changes nothing.
    again = act(api, staff["ravi"], "/supervisor/allocations", {**body, "note": ""})
    assert again["unchanged"] == 2 and again["changed"] == 0
    # Reassign one complaint to another department and official.
    moved = act(api, staff["ravi"], "/supervisor/allocations",
                {"complaints": [b], "department": "waste", "assignee": "bilal", "priority": "urgent", "reason": "Garbage, not roads"})
    assert moved["results"][0]["outcome"] == "reallocated"
    history = api.get(f"{API}/staff/complaints/{b}/allocations", headers=staff["ravi"]).json()
    assert [(h["kind"], h["from"]["assignee"], h["to"]["assignee"], h["from"]["priority"], h["to"]["priority"], h["by"]) for h in history] == [
        ("allocated", None, "asha", None, "high", "ravi"), ("reallocated", "asha", "bilal", "high", "urgent", "ravi")]
    assert history[0]["ward"] == w2 and history[0]["zone"] == 1
    assert history[1]["from"]["status"] == "Assigned" and history[1]["to"]["dueAt"] is None
    assert _q("SELECT COUNT(*) FROM complaint_assignments") == [(3,)]
    actions = [e["action"] for e in api.get(f"{API}/staff/complaints/{b}/audit", headers=staff["meera"]).json()]
    assert actions.count("allocated") == 1 and actions.count("reallocated") == 1 and "reassigned" in actions
    # An official in the wrong department cannot be the assignee (existing assignment rule).
    wrong = api.post(f"{API}/staff/supervisor/allocations", json={**body, "complaints": [a], "assignee": "bilal"}, headers=staff["ravi"])
    assert wrong.status_code == 422 and wrong.json()["error"]["code"] == "invalid_assignee"
    with pytest.raises(Exception):
        with closing(ComplaintStore().db.connect()) as conn, conn.transaction():
            conn.execute("UPDATE ward_allocations SET reason = 'rewritten'")


def test_allocation_is_limited_to_scope(ops):
    api, staff = ops
    w2 = zone_wards(api, staff["meera"])[2][0]
    cid = report(api)["id"]
    set_ward(api, staff["meera"], cid, w2)
    body = {"complaints": [cid], "department": "roads", "assignee": "asha", "reason": "r"}
    r = api.post(f"{API}/staff/supervisor/allocations", json=body, headers=staff["ravi"])
    assert r.status_code == 403 and r.json()["error"]["code"] == "out_of_scope"
    assert api.get(f"{API}/staff/supervisor/complaints?wards={w2}", headers=staff["ravi"]).status_code == 403
    assert api.post(f"{API}/staff/supervisor/allocations", json=body, headers=staff["meera"]).status_code == 200


# ---------- Dashboard ----------

def _db_counts(ward_pks=None):
    where = "merged_into_pk IS NULL" + (f" AND ward_pk IN ({', '.join(map(str, ward_pks))})" if ward_pks is not None else "")
    total, open_, resolved, pending = _q(
        f"SELECT COUNT(*), SUM(CASE WHEN status <> 'Marked Resolved' THEN 1 ELSE 0 END), SUM(CASE WHEN status = 'Marked Resolved' THEN 1 ELSE 0 END), "
        f"SUM(CASE WHEN status = 'Marked Resolved' AND verification = 'Pending Verification' THEN 1 ELSE 0 END) FROM complaints WHERE {where}")[0]
    overdue = _q(f"SELECT COUNT(*) FROM complaints WHERE {where} AND status <> 'Marked Resolved' AND due_at < '2026-01-01'")[0][0]
    return {"total": total, "open": open_ or 0, "resolved": resolved or 0, "pendingVerification": pending or 0, "overdue": overdue}


def test_dashboard_numbers_equal_database_counts(ops):
    api, staff = ops
    wards = zone_wards(api, staff["meera"])
    w1, w1b, w2 = wards[1][0], wards[1][1], wards[2][0]
    ids = [report(api, category=cat)["id"] for cat in ("Potholes", "Potholes", "Garbage accumulation", "Broken streetlights", "Potholes")]
    report(api)  # unverified ward
    for cid, ward in zip(ids, (w1, w1, w1b, w2, w2)):
        set_ward(api, staff["meera"], cid, ward)
    # ids[0]: resolved (pending verification); ids[1]: overdue allocation; ids[3]: merged duplicate of ids[4].
    act(api, staff["meera"], "/supervisor/allocations", {"complaints": [ids[0]], "department": "roads", "assignee": "asha", "reason": "r"})
    act(api, staff["meera"], f"/complaints/{ids[0]}/transitions", {"to": "In Progress", "expectedStatus": "Assigned"})
    act(api, staff["meera"], f"/complaints/{ids[0]}/transitions", {"to": "Marked Resolved", "expectedStatus": "In Progress", "explanation": "Patched"})
    act(api, staff["meera"], "/supervisor/allocations", {"complaints": [ids[1]], "department": "roads", "assignee": "asha", "dueAt": "2025-06-01", "reason": "r"})
    act(api, staff["meera"], f"/complaints/{ids[3]}/merge", {"canonical": ids[4], "reason": "Same streetlight pole"})

    city = api.get(f"{API}/staff/supervisor/dashboard", headers=staff["meera"]).json()
    assert city["level"] == "city" and city["cityWide"]
    assert city["totals"] == _db_counts() == {"total": 5, "open": 4, "resolved": 1, "pendingVerification": 1, "overdue": 1}
    assert city["unverifiedWard"]["total"] == 1
    zones = {z["number"]: z for z in city["byZone"]}
    assert len(zones) == 10 and zones[1]["total"] == 3 and zones[2]["total"] == 1 and zones[3]["total"] == 0
    pk = {n: p for n, p in _q("SELECT ward_number, pk FROM wards")}
    wards_out = {w["number"]: w for w in city["byWard"]}
    for ward in (w1, w1b, w2):
        assert {k: wards_out[ward][k] for k in ("total", "open", "resolved", "pendingVerification", "overdue")} == _db_counts([pk[ward]])
    assert {c["category"]: c["total"] for c in city["byCategory"]} == {"Potholes": 4, "Garbage accumulation": 1}
    assert sum(s["count"] for s in city["byStatus"]) == 5

    zone1 = api.get(f"{API}/staff/supervisor/dashboard?zone=1", headers=staff["meera"]).json()
    assert zone1["level"] == "zone" and zone1["totals"] == _db_counts([pk[n] for n in wards[1]]) and zone1["unverifiedWard"] is None
    ward = api.get(f"{API}/staff/supervisor/dashboard?ward={w1}", headers=staff["meera"]).json()
    assert ward["level"] == "ward" and ward["totals"] == _db_counts([pk[w1]]) and [w["number"] for w in ward["byWard"]] == [w1]
    # Drill-down to complaints in that ward: the unresolved one.
    assert [i["id"] for i in api.get(f"{API}/staff/supervisor/complaints?wards={w1}", headers=staff["meera"]).json()["items"]] == [ids[1]]

    # Scoped supervisor: only zone 1, no unverified bucket, other zones refused.
    ravi = api.get(f"{API}/staff/supervisor/dashboard", headers=staff["ravi"]).json()
    assert not ravi["cityWide"] and ravi["totals"] == _db_counts([pk[n] for n in wards[1]]) and [z["number"] for z in ravi["byZone"]] == [1]
    assert ravi["unverifiedWard"] is None
    assert api.get(f"{API}/staff/supervisor/dashboard?zone=2", headers=staff["ravi"]).status_code == 403
    assert api.get(f"{API}/staff/supervisor/dashboard?zone=99", headers=staff["ravi"]).status_code == 404


def test_supervisor_keeps_every_admin_workspace_action(ops):
    """Admin outranks supervisor; a supervisor already has every complaint-management action, plus set_ward."""
    api, staff = ops
    cid = report(api)["id"]
    for user in ("ravi", "meera"):
        actions = {a["action"] for a in api.get(f"{API}/staff/complaints/{cid}", headers=staff[user]).json()["allowedActions"]}
        assert {"assign", "merge", "review_duplicates", "add_note", "add_public_update", "set_ward"} <= actions
    names = {s["username"] for s in api.get(f"{API}/staff/supervisor/staff", headers=staff["ravi"]).json()}
    assert names == {"asha", "bilal", "ravi", "meera"}
    assert "password" not in json.dumps(api.get(f"{API}/staff/supervisor/staff", headers=staff["ravi"]).json()).lower()
