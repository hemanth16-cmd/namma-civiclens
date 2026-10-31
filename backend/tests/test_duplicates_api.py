"""Duplicate detection API (Workstream 2) on the configured database: ranked, explained suggestions,
human-approved merges with preserved records and history, reverts, dismissals, merge conflicts,
role checks and the public view of a merged report."""

import json
import math
import threading

import pytest

from app.geo import METRES_PER_DEGREE_LAT
from app.lifecycle import RuleViolation
from app.store import ComplaintStore
from tests.test_cases_api import API, act, api, assign, login, make_api, org, public, report, staff  # noqa: F401 (fixtures)
from tests.test_complaints import payload

LAT, LNG = 13.100712, 77.596334


def at(north=0.0, east=0.0, accuracy=8.0, **overrides):
    """A complaint payload `north`/`east` metres from the base point."""
    gps = payload()["gps"] | {"lat": LAT + north / METRES_PER_DEGREE_LAT,
                              "lng": LNG + east / (METRES_PER_DEGREE_LAT * math.cos(math.radians(LAT))), "accuracy": accuracy}
    return {"gps": gps, **overrides}


def file_report(api, **kw):
    """A complaint with the shared test photo uploaded (so every such pair also has an identical file)."""
    return report(api, **at(**kw))


def bare_report(api, **kw):
    """A complaint without evidence, so scores come from location, category, time and text alone."""
    r = api.post(f"{API}/complaints", json=payload(**at(**kw)))
    assert r.status_code == 201, r.text
    return r.json()


def candidates(api, headers, cid, **params):
    r = api.get(f"{API}/staff/complaints/{cid}/duplicates", params=params, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def merge(api, headers, dup, canonical, reason="Same pothole", expect=200):
    return act(api, headers, f"/complaints/{dup}/merge", {"canonical": canonical, "reason": reason}, expect)


def test_suggestions_are_ranked_and_explained(api, staff):
    target = bare_report(api)["id"]
    same = bare_report(api, north=5, description="Big pothole at the bus stop")["id"]
    separate = bare_report(api, east=60, accuracy=5)["id"]
    crack = bare_report(api, north=8, category="Road cracks and damage", description="Cracked road")["id"]
    bare_report(api, north=3, category="Broken streetlights")  # unrelated category: never a candidate
    bare_report(api, north=3000)                               # outside the search radius
    result = candidates(api, staff["ravi"], target)
    ids = [c["id"] for c in result["candidates"]]
    assert ids[0] == same and set(ids) == {same, separate, crack}
    top = result["candidates"][0]
    assert top["label"] == "likely_duplicate" and top["distanceM"] == pytest.approx(5, abs=0.5)
    assert {f["name"] for f in top["factors"]} == {"location", "category", "time", "description"}
    assert top["score"] == pytest.approx(sum(f["contribution"] for f in top["factors"]), abs=0.01)
    assert top["reasons"], "every suggestion says why"
    by_id = {c["id"]: c for c in result["candidates"]}
    assert by_id[separate]["label"] == "nearby_separate"
    assert result["radiusM"] == 250 and result["days"] == 90
    assert [c["id"] for c in candidates(api, staff["ravi"], target, radius=20)["candidates"]] == [same, crack]
    assert api.get(f"{API}/staff/complaints/{target}/duplicates?radius=5000", headers=staff["ravi"]).status_code == 422
    assert set(top) == {"id", "category", "area", "created", "status", "verification", "score", "label", "distanceM", "factors",
                        "reasons", "caution"}, "no description, coordinates or evidence in suggestions"


def test_identical_evidence_file_is_flagged(api, staff):
    # report() uploads the same test JPEG for every complaint, so these two share a SHA-256.
    a = file_report(api)["id"]
    b = file_report(api, east=200, accuracy=5, description="")["id"]
    top = candidates(api, staff["ravi"], a)["candidates"][0]
    assert top["id"] == b and top["score"] >= 0.97 and "identical evidence file" in " ".join(top["reasons"])


def test_merge_keeps_both_records_and_history(api, staff):
    canonical = file_report(api)
    dup = file_report(api, north=4)
    case = merge(api, staff["ravi"], dup["id"], canonical["id"])
    assert case["mergedInto"] == canonical["id"]
    assert case["allowedActions"] == [{"action": "add_note", "label": "Add internal note", "to": None, "needsExplanation": False},
                                      {"action": "revert_merge", "label": "Revert merge", "to": None, "needsExplanation": False}]
    m = case["merges"][0]
    assert (m["duplicate"], m["canonical"], m["by"], m["revertedAt"]) == (dup["id"], canonical["id"], "ravi", None)
    assert m["label"] == "likely_duplicate" and m["score"] >= 0.75, "the score at merge time is recorded"

    pub_dup, pub_canonical = public(api, dup["id"]), public(api, canonical["id"])
    assert pub_dup["mergedInto"] == canonical["id"] and pub_canonical["mergedInto"] is None
    assert f"Merged into {canonical['id']}" in pub_dup["timeline"][-1]["text"]
    assert dup["id"] in pub_canonical["timeline"][-1]["text"]
    assert "ravi" not in json.dumps(pub_dup) and "Same pothole" not in json.dumps(pub_dup), "reason and staff stay internal"
    # The duplicate's evidence is still there and still viewable by staff.
    assert api.get(f"{API}/staff/complaints/{dup['id']}/evidence", headers=staff["ravi"]).status_code == 200
    # Merged complaints drop out of suggestions; the canonical shows the link in its own history.
    assert dup["id"] not in [c["id"] for c in candidates(api, staff["ravi"], canonical["id"])["candidates"]]
    assert api.get(f"{API}/staff/complaints/{canonical['id']}", headers=staff["ravi"]).json()["merges"][0]["duplicate"] == dup["id"]
    assert candidates(api, staff["ravi"], dup["id"])["note"].startswith("This complaint is already merged")
    audit = [e["action"] for e in api.get(f"{API}/staff/complaints/{dup['id']}/audit", headers=staff["ravi"]).json()]
    assert audit[0] == "merged_as_duplicate"


def test_a_merged_duplicate_is_managed_through_its_canonical(api, staff):
    canonical, dup = file_report(api)["id"], file_report(api, north=4)["id"]
    merge(api, staff["ravi"], dup, canonical)
    act(api, staff["ravi"], f"/complaints/{dup}/transitions", {"to": "Under Review", "expectedStatus": "Submitted"}, 409)
    assign(api, staff["ravi"], dup, expect=409)
    act(api, staff["ravi"], f"/complaints/{dup}/notes", {"body": "still allowed"}, 201)
    assign(api, staff["ravi"], canonical)  # the canonical complaint carries on as normal


def test_merge_conflicts_are_refused(api, staff):
    a, b, c = (file_report(api, north=n)["id"] for n in (0, 3, 6))
    merge(api, staff["ravi"], a, a, expect=422)                       # self
    merge(api, staff["ravi"], b, a)
    merge(api, staff["ravi"], b, c, expect=409)                       # already merged
    merge(api, staff["ravi"], c, b, expect=409)                       # canonical is itself a duplicate (no chains)
    merge(api, staff["ravi"], a, c, expect=409)                       # a has duplicates of its own
    merge(api, staff["ravi"], c, a, reason=" ", expect=422)           # reason required (whitespace)
    assert api.post(f"{API}/staff/complaints/{c}/merge", json={"canonical": "nope", "reason": "x"}, headers=staff["ravi"]).status_code == 422
    merge(api, staff["ravi"], c, "NCL-2026-ZZZZZZ", expect=404)


def test_only_supervisors_merge_dismiss_or_revert(api, staff):
    a, b = file_report(api)["id"], file_report(api, north=3)["id"]
    merge(api, staff["asha"], b, a, expect=403)
    act(api, staff["asha"], f"/complaints/{a}/duplicates/{b}/dismiss", {"reason": "different"}, 403)
    merge_id = merge(api, staff["ravi"], b, a)["merges"][0]["id"]
    act(api, staff["asha"], f"/merges/{merge_id}/revert", {"reason": "oops"}, 403)
    assert candidates(api, staff["asha"], a)["candidates"] is not None, "officials may review suggestions"
    assert api.get(f"{API}/staff/complaints/{a}/duplicates").status_code == 401


def test_revert_restores_independence_and_keeps_the_record(api, staff):
    a, b = file_report(api)["id"], file_report(api, north=3)["id"]
    merge_id = merge(api, staff["ravi"], b, a)["merges"][0]["id"]
    act(api, staff["ravi"], f"/merges/{merge_id}/revert", {"reason": " "}, 422)
    case = act(api, staff["ravi"], f"/merges/{merge_id}/revert", {"reason": "Different potholes after all"})
    assert case["id"] == b and case["mergedInto"] is None
    assert case["merges"][0]["revertedBy"] == "ravi" and case["merges"][0]["revertReason"] == "Different potholes after all"
    act(api, staff["ravi"], f"/merges/{merge_id}/revert", {"reason": "again"}, 409)
    assert public(api, b)["mergedInto"] is None and "No longer merged" in public(api, b)["timeline"][-1]["text"]
    assert b in [c["id"] for c in candidates(api, staff["ravi"], a)["candidates"]], "suggested again after the revert"
    second = merge(api, staff["ravi"], b, a)["merges"]
    assert len(second) == 2 and second[0]["revertedAt"] and second[1]["revertedAt"] is None, "history keeps both merges"
    assert act(api, staff["ravi"], "/merges/mg_" + "x" * 16 + "/revert", {"reason": "x"}, 404)


def test_dismissed_pairs_are_no_longer_suggested(api, staff):
    a, b, c = file_report(api)["id"], file_report(api, north=3)["id"], file_report(api, north=6)["id"]
    act(api, staff["ravi"], f"/complaints/{a}/duplicates/{b}/dismiss", {"reason": "Two separate potholes"})
    act(api, staff["ravi"], f"/complaints/{b}/duplicates/{a}/dismiss", {"reason": "again"}, 409)
    act(api, staff["ravi"], f"/complaints/{a}/duplicates/{a}/dismiss", {"reason": "self"}, 422)
    assert [x["id"] for x in candidates(api, staff["ravi"], a)["candidates"]] == [c]
    assert a not in [x["id"] for x in candidates(api, staff["ravi"], b)["candidates"]], "in both directions"


def test_officials_see_only_candidates_in_their_scope(api, staff):
    target = file_report(api)["id"]
    other = file_report(api, north=3)["id"]
    assign(api, staff["ravi"], other, dept="waste")
    assert other not in [c["id"] for c in candidates(api, staff["asha"], target)["candidates"]]
    assert other in [c["id"] for c in candidates(api, staff["bilal"], target)["candidates"]]
    assert api.get(f"{API}/staff/complaints/{other}/duplicates", headers=staff["asha"]).status_code == 403


def test_concurrent_merges_of_the_same_duplicate_one_wins(isolated_db_path, org):
    store = ComplaintStore(isolated_db_path)
    store.initialize()
    ids = [store.create(payload(**at(north=n)))[0]["id"] for n in (0, 3, 6)]
    supervisor = store.get_staff("ravi")["pk"]
    results, barrier = [], threading.Barrier(2)

    def attempt(canonical):
        barrier.wait()
        try:
            store.merge_complaints(ids[0], canonical, supervisor, "same")
            results.append("ok")
        except RuleViolation as exc:
            results.append(exc.code)

    threads = [threading.Thread(target=attempt, args=(c,)) for c in ids[1:]]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["already_merged", "ok"]
    store.close()
