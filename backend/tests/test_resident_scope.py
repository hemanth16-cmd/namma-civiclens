"""Two data scopes for residents, kept separate:

- My Complaints  (GET /residents/me/complaints): only complaints owned by the signed-in resident;
- Community page and neighbourhood map (GET /complaints): every public complaint across Bengaluru,
  whoever submitted it, with an approximate map point and no reporter or private data.
"""

import json

from app.geo import public_map_point
from tests.test_complaints import payload
from tests.test_residents import API, api, bearer, make_api, resident_token  # noqa: F401 (fixtures)


def submit(api, token=None, lat=12.9716, lng=77.5946, **overrides):
    body = payload(**overrides)
    body["gps"] = body["gps"] | {"lat": lat, "lng": lng}
    r = api.post(f"{API}/complaints", json=body, headers=bearer(token) if token else {})
    assert r.status_code == 201, r.text
    return r.json()


def mine(api, token, **params):
    r = api.get(f"{API}/residents/me/complaints", params=params, headers=bearer(token))
    assert r.status_code == 200, r.text
    return {i["id"] for i in r.json()["items"]}


def public(api, **params):
    r = api.get(f"{API}/complaints", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def scenario(api):
    a = resident_token(api, "asha@example.org", "Asha")
    b = resident_token(api, "bina@example.org", "Bina")
    return {"a": a, "b": b,
            "a1": submit(api, a, description="Asha private note about her street"),
            "b1": submit(api, b, lat=12.925, lng=77.5938, neighbourhood="Jayanagar", category="Illegal dumping"),
            "anon": submit(api, lat=13.0358, lng=77.597, neighbourhood="Hebbal")}


def test_my_complaints_hold_only_my_own(api):
    s = scenario(api)
    assert mine(api, s["a"]) == {s["a1"]["id"]}, "not Bina's, not the anonymous report"
    assert mine(api, s["b"]) == {s["b1"]["id"]}
    assert api.get(f"{API}/residents/me/complaints").status_code == 401
    # Ownership comes from the session only; a resident ID in the query is ignored.
    assert mine(api, s["a"], resident="res_anything", owner="bina@example.org") == {s["a1"]["id"]}


def test_claiming_moves_an_anonymous_report_into_my_complaints_only(api):
    s = scenario(api)
    r = api.post(f"{API}/residents/me/claims", json={"id": s["anon"]["id"], "claim": s["anon"]["claim"]}, headers=bearer(s["b"]))
    assert r.status_code == 200, r.text
    assert mine(api, s["b"]) == {s["b1"]["id"], s["anon"]["id"]}
    assert mine(api, s["a"]) == {s["a1"]["id"]}
    assert s["anon"]["id"] in {i["id"] for i in public(api)["items"]}, "still public after the claim"


def test_community_and_map_show_everyones_reports_for_any_viewer(api):
    s = scenario(api)
    everyone = {s["a1"]["id"], s["b1"]["id"], s["anon"]["id"]}
    listed = public(api)
    assert {i["id"] for i in listed["items"]} == everyone
    # Signed in as A or B, the public list is the same dataset (the endpoint ignores who is asking).
    for token in (s["a"], s["b"]):
        r = api.get(f"{API}/complaints", headers=bearer(token))
        assert {i["id"] for i in r.json()["items"]} == everyone
    # Every listed complaint has a usable approximate map point, so all of them can be plotted.
    points = {i["id"]: i["mapPoint"] for i in listed["items"]}
    assert points[s["anon"]["id"]] == {"lat": 13.036, "lng": 77.597, "precision": "approx-110m"}
    assert points[s["b1"]["id"]] == {"lat": 12.925, "lng": 77.594, "precision": "approx-110m"}
    assert all(p and len(str(p["lat"]).split(".")[1]) <= 3 for p in points.values())


def test_public_responses_carry_no_reporter_or_private_data(api):
    s = scenario(api)
    raw = json.dumps(public(api)).lower()
    for secret in ("asha", "bina", "example.org", "res_", "owner", "ownedvia", "private note", s["a1"]["claim"], s["anon"]["claim"],
                   "uploadtoken", "storage", "evidence", "12.9716", "77.5946", "13.0358", "description"):
        assert secret not in raw, secret


def test_filters_and_paging_cover_all_submitters(api):
    s = scenario(api)
    assert {i["id"] for i in public(api, category="Illegal dumping")["items"]} == {s["b1"]["id"]}
    assert {i["id"] for i in public(api, area="Hebbal")["items"]} == {s["anon"]["id"]}
    first, second = public(api, limit=2), public(api, limit=2, offset=2)
    assert first["total"] == 3 and len(first["items"]) == 2 and len(second["items"]) == 1
    assert {i["id"] for i in first["items"] + second["items"]} == {s["a1"]["id"], s["b1"]["id"], s["anon"]["id"]}


def test_unusable_coordinates_give_no_map_point():
    assert public_map_point(12.97164, 77.59456) == {"lat": 12.972, "lng": 77.595, "precision": "approx-110m"}
    for bad in ((None, 77.5), (12.9, None), (float("nan"), 77.5), (12.9, float("inf")), (91.0, 77.5), (12.9, 181.0), ("12.9", 77.5)):
        assert public_map_point(*bad) is None, bad
