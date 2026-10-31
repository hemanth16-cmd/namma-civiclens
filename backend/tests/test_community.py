"""Public community list (GET /api/v1/complaints): every complaint in the Bengaluru service area,
filters by issue type and neighbourhood, facet counts, paging, and only public fields."""

import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.test_complaints import payload

API = "/api/v1/complaints"


@pytest.fixture
def client(isolated_db_path, evidence_dir):
    with TestClient(create_app(db_path=isolated_db_path, evidence_dir=evidence_dir), client=("10.0.0.9", 1)) as c:
        yield c


def file(client, lat=12.9716, lng=77.5946, **overrides):
    body = payload(**overrides)
    body["gps"] = body["gps"] | {"lat": lat, "lng": lng}
    r = client.post(API, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def listing(client, **params):
    r = client.get(API, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_lists_every_bengaluru_complaint_newest_first_with_public_fields_only(client):
    a = file(client, neighbourhood="Hebbal", description="SECRET description text")
    b = file(client, category="Illegal dumping", neighbourhood="Jayanagar", lat=12.925, lng=77.5938)
    file(client, lat=12.2958, lng=76.6394, neighbourhood="Mysuru")   # outside the Bengaluru service area
    data = listing(client)
    assert [i["id"] for i in data["items"]] == [b["id"], a["id"]] and data["total"] == 2
    assert set(data["items"][0]) == {"id", "title", "category", "area", "status", "verification", "department", "created", "updated",
                                     "mapPoint"}
    assert data["items"][1]["mapPoint"] == {"lat": 12.972, "lng": 77.595, "precision": "approx-110m"}, "rounded, not the exact fix"
    raw = json.dumps(data)
    for private in ("SECRET description", a["claim"], "12.9716", "77.5946", "gps", "evidence", "uploadToken"):
        assert private not in raw, private


def test_filters_by_issue_type_place_and_status(client):
    file(client, neighbourhood="Hebbal")
    file(client, neighbourhood="Hebbal", category="Illegal dumping")
    file(client, neighbourhood="Jayanagar")
    assert listing(client, category="Potholes")["total"] == 2
    assert listing(client, area="Hebbal")["total"] == 2
    both = listing(client, category="Potholes", area="Hebbal")
    assert both["total"] == 1 and both["items"][0]["area"] == "Hebbal" and both["items"][0]["category"] == "Potholes"
    assert listing(client, status="Marked Resolved")["total"] == 0
    assert listing(client, area="Nowhere")["items"] == []
    assert client.get(API, params={"category": "Unicorns"}).status_code == 422
    assert client.get(API, params={"status": "Closed"}).status_code == 422


def test_facets_count_everything_and_ignore_the_filters(client):
    file(client, neighbourhood="Hebbal")
    file(client, neighbourhood="Hebbal")
    file(client, neighbourhood="Jayanagar", category="Water leakage")
    facets = listing(client, area="Jayanagar")["facets"]
    assert facets["areas"] == [{"name": "Hebbal", "count": 2}, {"name": "Jayanagar", "count": 1}]
    assert facets["categories"] == [{"name": "Potholes", "count": 2}, {"name": "Water leakage", "count": 1}]


def test_paging(client):
    ids = [file(client)["id"] for _ in range(5)]
    first, second = listing(client, limit=2), listing(client, limit=2, offset=2)
    assert [i["id"] for i in first["items"] + second["items"]] == list(reversed(ids))[:4]
    assert first["total"] == 5 and client.get(API, params={"limit": 101}).status_code == 422


def test_merged_duplicates_are_not_listed_twice(client):
    store = client.app.state.store
    store.create_staff("ravi", "Ravi", "scrypt$x", role="supervisor")
    canonical, duplicate = file(client)["id"], file(client)["id"]
    store.merge_complaints(duplicate, canonical, store.get_staff("ravi")["pk"], "same pothole")
    assert [i["id"] for i in listing(client)["items"]] == [canonical]


def test_status_updates_show_in_the_list(client):
    store = client.app.state.store
    store.create_staff("ravi", "Ravi", "scrypt$x", role="supervisor")
    cid = file(client)["id"]
    store.transition(cid, store.get_staff("ravi")["pk"], "Under Review", "Submitted")
    assert listing(client)["items"][0]["status"] == "Under Review"
