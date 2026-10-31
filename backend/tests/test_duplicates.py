"""Unit tests for app/geo.py and app/duplicates.py: known distances, bounding boxes, and the
duplicate scenarios the scoring must tell apart."""

import math

import pytest

from app import duplicates as dup
from app.geo import METRES_PER_DEGREE_LAT, bounding_box, haversine_m

BASE = (12.9716, 77.5946)  # Bengaluru


def offset(lat, lng, north_m=0.0, east_m=0.0):
    return lat + north_m / METRES_PER_DEGREE_LAT, lng + east_m / (METRES_PER_DEGREE_LAT * math.cos(math.radians(lat)))


def report(id_="NCL-2026-AAAAAA", north=0.0, east=0.0, category="Potholes", created="2026-10-30T10:00:00.000Z", accuracy=8.0,
           status="Submitted", description="Deep pothole near the bus stop", sha256=None):
    lat, lng = offset(*BASE, north, east)
    return {"id": id_, "lat": lat, "lng": lng, "category": category, "created": created, "accuracy": accuracy, "status": status,
            "description": description, "sha256": sha256}


# ---------- geo ----------

def test_known_distances():
    assert haversine_m(*BASE, *BASE) == 0
    assert haversine_m(0, 0, 1, 0) == pytest.approx(111_195, rel=1e-4)            # one degree of latitude
    assert haversine_m(*BASE, *offset(*BASE, north_m=1000)) == pytest.approx(1000, rel=1e-6)
    assert haversine_m(*BASE, *offset(*BASE, east_m=250)) == pytest.approx(250, rel=1e-4)
    # Bengaluru (MG Road) to Mysuru Palace: about 125 km by great circle.
    assert haversine_m(12.9756, 77.6066, 12.3052, 76.6552) == pytest.approx(126_000, rel=0.02)
    assert haversine_m(0, 179.9, 0, -179.9) == pytest.approx(22_239, rel=1e-3), "across the antimeridian"


def test_bounding_box_contains_the_circle():
    min_lat, max_lat, min_lng, max_lng = bounding_box(*BASE, 500)
    for north, east in ((500, 0), (-500, 0), (0, 500), (0, -500), (353, 353)):
        lat, lng = offset(*BASE, north * 0.999, east * 0.999)
        assert min_lat <= lat <= max_lat and min_lng <= lng <= max_lng
    assert bounding_box(89.999, 10, 1000)[2:] == (-180.0, 180.0), "near the pole the longitude range is full"
    assert bounding_box(0, 179.999, 1000)[2:] == (-180.0, 180.0), "no wrap across the antimeridian"


# ---------- scoring scenarios ----------

def test_same_pothole_reported_twice_is_a_likely_duplicate():
    a = report()
    b = report("NCL-2026-BBBBBB", north=6, created="2026-10-30T18:00:00.000Z", description="Big pothole at the bus stop")
    s = dup.score_pair(a, b)
    assert s["label"] == "likely_duplicate" and s["score"] >= 0.75
    assert "Within the GPS uncertainty of each other." in s["reasons"]
    assert sum(f["weight"] for f in s["factors"]) == pytest.approx(1.0, abs=1e-3)
    assert s["score"] == pytest.approx(sum(f["contribution"] for f in s["factors"]), abs=2e-3)


def test_two_potholes_40m_apart_with_precise_gps_stay_separate():
    a = report(accuracy=5)
    b = report("NCL-2026-BBBBBB", east=40, accuracy=5, description="Deep pothole near the bus stop")  # identical text, same day
    s = dup.score_pair(a, b)
    assert s["label"] == "nearby_separate"
    assert any("separate issue" in r for r in s["reasons"])


def test_the_same_distance_with_coarse_gps_is_only_possible():
    a = report(accuracy=35)
    b = report("NCL-2026-BBBBBB", east=40, accuracy=35)
    s = dup.score_pair(a, b)
    assert s["label"] in ("likely_duplicate", "possible_duplicate")
    assert dup.score_pair(report(accuracy=5), report("NCL-2026-BBBBBB", east=40, accuracy=5))["score"] < s["score"]


def test_a_new_report_where_an_old_one_was_resolved_is_a_recurrence():
    old = report("NCL-2026-OLD001", created="2026-09-01T10:00:00.000Z", status="Marked Resolved")
    new = report(north=3, created="2026-10-28T10:00:00.000Z")
    s = dup.score_pair(new, old)
    assert s["label"] == "recurrence"
    assert any("recurrence" in r for r in s["reasons"])


def test_identical_evidence_file_is_near_conclusive():
    a = report(sha256="a" * 64)
    b = report("NCL-2026-BBBBBB", east=300, created="2026-08-01T10:00:00.000Z", sha256="a" * 64, description="")
    s = dup.score_pair(a, b)
    assert s["score"] >= 0.97 and s["label"] == "likely_duplicate"
    assert "Both reports carry the identical evidence file." in s["reasons"]


def test_low_accuracy_caps_the_score_and_says_why():
    a = report(accuracy=250)
    b = report("NCL-2026-BBBBBB", north=5, accuracy=8)
    s = dup.score_pair(a, b)
    assert s["score"] <= 0.6 and s["label"] != "likely_duplicate"
    assert "±250 m" in s["caution"]


def test_missing_accuracy_uses_a_conservative_default():
    s = dup.score_pair(report(accuracy=None), report("NCL-2026-BBBBBB", north=20, accuracy=None))
    assert "±71 m" in next(f for f in s["factors"] if f["name"] == "location")["detail"]  # sqrt(50² + 50² + 10²) ≈ 71.4


def test_missing_descriptions_are_neutral():
    a, b = report(description=""), report("NCL-2026-BBBBBB", north=4, description="")
    s = dup.score_pair(a, b)
    text = next(f for f in s["factors"] if f["name"] == "description")
    assert text["value"] is None and text["weight"] == 0 and text["contribution"] == 0
    with_text = dup.score_pair(report(), report("NCL-2026-BBBBBB", north=4))
    assert s["label"] == with_text["label"] == "likely_duplicate"


def test_related_and_unrelated_categories():
    a = report()
    related = dup.score_pair(a, report("NCL-2026-BBBBBB", north=4, category="Road cracks and damage"))
    same = dup.score_pair(a, report("NCL-2026-CCCCCC", north=4))
    assert related["score"] < same["score"]
    assert "Related category" in next(f for f in related["factors"] if f["name"] == "category")["detail"]
    assert dup.comparable_categories("Potholes") == {"Potholes", "Road cracks and damage"}
    assert dup.comparable_categories("Broken streetlights") == {"Broken streetlights"}


def test_time_decay():
    a = report()
    fresh = dup.score_pair(a, report("NCL-2026-BBBBBB", north=4, created="2026-10-31T10:00:00.000Z"))
    stale = dup.score_pair(a, report("NCL-2026-CCCCCC", north=4, created="2026-07-01T10:00:00.000Z"))
    assert next(f for f in stale["factors"] if f["name"] == "time")["value"] < 0.02 < next(f for f in fresh["factors"] if f["name"] == "time")["value"]


def test_text_similarity():
    assert dup.text_similarity("Pothole near bus stop", "pothole near the bus-stop") > 0.6
    assert dup.text_similarity("Pothole near bus stop", "Streetlight flickering at night") < 0.15
    assert dup.text_similarity("", "anything") is None and dup.text_similarity("ok", "ok") is None


def test_rank_orders_by_score_then_distance_and_skips_itself():
    target = report()
    candidates = [report(), report("NCL-2026-FARAWA", east=200, accuracy=5), report("NCL-2026-NEAR01", north=3),
                  report("NCL-2026-NEAR02", north=3, created="2026-06-01T10:00:00.000Z")]
    ranked = dup.rank(target, candidates)
    assert [c["id"] for c in ranked] == ["NCL-2026-NEAR01", "NCL-2026-NEAR02", "NCL-2026-FARAWA"]
    assert ranked[0]["label"] == "likely_duplicate" and ranked[-1]["label"] == "nearby_separate"
    assert len(dup.rank(target, candidates, limit=1)) == 1
