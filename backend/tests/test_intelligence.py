"""Comprehensive unit and API tests for the Civic Intelligence engine."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from app.intelligence import fcm, hotspots, knowledge_graph, monte_carlo, predict, priority
from app.main import create_app
from tests.test_cases_api import PASSWORD, cli, login


@pytest.fixture
def org(isolated_db_path, monkeypatch):
    """Admin staff user for authenticated tests."""
    db = isolated_db_path
    assert cli(db, monkeypatch, "create", "--username", "admin_intel", "--display-name", "Admin Intel",
               "--role", "admin", "--password-stdin", stdin=PASSWORD) == 0
    return db


@pytest.fixture
def intel_client(isolated_db_path, evidence_dir, org):
    app = create_app(db_path=isolated_db_path, evidence_dir=evidence_dir)
    with TestClient(app, client=("10.0.0.7", 5000)) as client:
        yield client


@pytest.fixture
def auth_headers(intel_client):
    res = login(intel_client, "admin_intel")
    return res


def make_payload(**overrides):
    now_iso = datetime.now(timezone.utc).isoformat()
    body = {
        "category": "Potholes",
        "mediaType": "photo",
        "description": "Deep pothole near the bus stop",
        "shareEvidence": False,
        "neighbourhood": "Yelahanka",
        "gps": {
            "lat": 13.100712,
            "lng": 77.596334,
            "accuracy": 12.5,
            "sourceType": "gps",
            "source": "Device GPS",
            "timestamp": int(time.time() * 1000) - 5000,
            "fixAgeSeconds": 5,
            "address": "12 Example Road, Yelahanka",
        },
        "capturedAt": now_iso,
        "captureSource": "Live camera",
    }
    body.update(overrides)
    return body


# ============================================================================
# 1. Priority Incident Tracking Tests
# ============================================================================

def test_priority_basic():
    r = priority.score_incident(category="Potholes", description="large hole near school")
    assert 0 <= r.score <= 100
    assert r.level in ("low", "medium", "high", "critical")
    assert "severity" in r.factors


def test_priority_safety_override():
    r = priority.score_incident(
        category="Other civic issues",
        description="exposed electrical live wire on footpath",
    )
    assert r.level == "critical"
    assert "safety_critical_keyword" in r.overrides


def test_priority_recurrence_increases_score():
    r0 = priority.score_incident(category="Potholes", recurrence_count=0)
    r2 = priority.score_incident(category="Potholes", recurrence_count=3)
    assert r2.score >= r0.score


def test_priority_score_complaints(intel_client):
    store = intel_client.app.state.store
    res = priority.score_complaints(store)
    assert "items" in res
    assert "summary" in res
    assert res["policy"] == "configurable_weights_v1"


# ============================================================================
# 2. Fuzzy Cognitive Map Tests
# ============================================================================

def test_fcm_runs_and_converges():
    result = fcm.run_fcm({"complaint_frequency": 0.8, "operational_capacity": 0.3})
    assert 0.0 <= result.risk_score <= 1.0
    assert result.iterations >= 1
    assert "overall_civic_risk" in result.activations
    assert result.assumptions


def test_fcm_scenario_changes_risk():
    base = {"complaint_frequency": 0.4, "operational_capacity": 0.6}
    out = fcm.scenario(base, {"complaint_frequency": 0.4})
    assert "baseline_risk" in out
    assert "scenario_risk" in out
    assert out["delta_risk"] != 0.0


def test_fcm_derive_live_activations(intel_client):
    store = intel_client.app.state.store
    acts = fcm.derive_live_activations(store)
    assert "complaint_frequency" in acts
    assert "operational_capacity" in acts
    assert 0.0 <= acts["complaint_frequency"] <= 1.0


# ============================================================================
# 3. Monte Carlo Simulation Tests
# ============================================================================

def test_monte_carlo_reproducible():
    a = monte_carlo.simulate_demand(100.0, n_runs=500, seed=7)
    b = monte_carlo.simulate_demand(100.0, n_runs=500, seed=7)
    assert a.median == b.median
    assert a.mean == b.mean
    assert a.p10 <= a.median <= a.p90


def test_monte_carlo_scenarios():
    out = monte_carlo.run_scenarios(50.0, capacity=40.0, n_runs=300, seed=1)
    assert set(out) >= {"current", "increased_volume", "increased_capacity", "reduced_efficiency"}
    assert out["increased_volume"]["mean"] > out["current"]["mean"]


def test_monte_carlo_suggest_defaults(intel_client):
    store = intel_client.app.state.store
    defaults = monte_carlo.suggest_simulation_defaults(store)
    assert defaults["suggested_base_rate"] > 0
    assert defaults["suggested_capacity"] > 0
    assert defaults["recommended_runs"] >= 1000


# ============================================================================
# 4. Predictive Analytics Tests
# ============================================================================

def test_predict_baselines_graceful_empty():
    elev = predict.ward_elevated_activity(aggregates={"by_ward": {"Ward A": 10, "Ward B": 5}})
    assert elev[0]["ward"] == "Ward A"
    assert elev[0]["label"] == "elevated_complaint_activity"


def test_predict_category_outlook():
    cats = predict.category_outlook(aggregates={"by_category": {"Potholes": 150, "Waste": 50}})
    assert cats[0]["category"] == "Potholes"
    assert cats[0]["share"] == 0.75


def test_predict_workload_forecast():
    out = predict.workload_forecast(
        aggregates={"by_month": {"2024-01": 100, "2024-02": 120}},
        horizon_days=30,
    )
    assert out["point_forecast"] == 110.0
    assert out["approx_p10"] < out["point_forecast"] < out["approx_p90"]


# ============================================================================
# 5. Knowledge Graph Tests
# ============================================================================

def test_knowledge_graph_empty_store(intel_client):
    store = intel_client.app.state.store
    graph = knowledge_graph.build_knowledge_graph(store)
    assert "nodes" in graph
    assert "edges" in graph
    assert "summary" in graph
    assert graph["summary"]["node_count"] == 0


def test_knowledge_graph_with_complaint(intel_client, auth_headers):
    p = make_payload()
    r = intel_client.post("/api/v1/complaints", json=p)
    assert r.status_code == 201
    cid = r.json()["id"]

    store = intel_client.app.state.store
    graph = knowledge_graph.build_knowledge_graph(store, complaint_id=cid)
    assert graph["summary"]["node_count"] >= 1
    types = graph["summary"]["entity_types"]
    assert "Complaint" in types
    assert "Location" in types


# ============================================================================
# 6. Verified Recurring Hotspot Tests
# ============================================================================

def test_historical_hotspot_detection():
    mock_agg = {
        "by_ward": {"Ward X": 500, "Ward Y": 200},
        "ward_category_top": [{"ward": "Ward X", "category": "Potholes", "count": 300}],
    }
    hs = hotspots.detect_historical_hotspots(mock_agg)
    assert len(hs) == 2
    assert hs[0]["ward"] == "Ward X"
    assert hs[0]["scope"] == "ward_level_aggregate"
    assert "top_categories" in hs[0]


def test_live_hotspot_clustering_no_fabrication(intel_client):
    # Two complaints within 50m of each other
    p1 = make_payload(gps={
        "lat": 12.971600, "lng": 77.594600, "accuracy": 5.0, "sourceType": "gps",
        "source": "Device GPS", "timestamp": int(time.time() * 1000) - 5000,
        "fixAgeSeconds": 5, "address": "Spot 1",
    })
    p2 = make_payload(gps={
        "lat": 12.971650, "lng": 77.594650, "accuracy": 5.0, "sourceType": "gps",
        "source": "Device GPS", "timestamp": int(time.time() * 1000) - 4000,
        "fixAgeSeconds": 4, "address": "Spot 2",
    })
    r1 = intel_client.post("/api/v1/complaints", json=p1)
    r2 = intel_client.post("/api/v1/complaints", json=p2)
    assert r1.status_code == 201
    assert r2.status_code == 201

    store = intel_client.app.state.store
    clusters = hotspots.detect_live_hotspots(store, radius_m=100.0, min_complaints=2)
    assert len(clusters) == 1
    assert clusters[0]["incident_count"] == 2
    assert clusters[0]["radius_m"] == 100.0
    assert "Potholes" in clusters[0]["categories"]


# ============================================================================
# 7. Authenticated API Endpoint Routing Tests
# ============================================================================

def test_api_auth_required(intel_client):
    endpoints = [
        "/api/v1/intelligence/summary",
        "/api/v1/intelligence/predictions",
        "/api/v1/intelligence/hotspots",
        "/api/v1/intelligence/graph",
        "/api/v1/intelligence/fcm/demo",
        "/api/v1/intelligence/fcm/live",
        "/api/v1/intelligence/simulations/defaults",
        "/api/v1/intelligence/priority/score?category=Potholes",
        "/api/v1/intelligence/priority/queue",
    ]
    for ep in endpoints:
        res = intel_client.get(ep)
        assert res.status_code == 401, f"Endpoint {ep} must require staff authentication"


def test_api_summary_endpoint(intel_client, auth_headers):
    res = intel_client.get("/api/v1/intelligence/summary", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert "top_elevated_wards" in data
    assert "workload_outlook_30d" in data


def test_api_predictions_endpoint(intel_client, auth_headers):
    res = intel_client.get("/api/v1/intelligence/predictions", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert "elevated_activity_wards" in data
    assert "category_outlook" in data
    assert "workload_30d" in data


def test_api_hotspots_endpoint(intel_client, auth_headers):
    res = intel_client.get("/api/v1/intelligence/hotspots", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert "live_spatial_hotspots" in data
    assert "historical_ward_hotspots" in data
    assert "summary" in data


def test_api_graph_endpoint(intel_client, auth_headers):
    res = intel_client.get("/api/v1/intelligence/graph", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert "nodes" in data
    assert "edges" in data
    assert "summary" in data


def test_api_fcm_endpoints(intel_client, auth_headers):
    res_demo = intel_client.get("/api/v1/intelligence/fcm/demo", headers=auth_headers)
    assert res_demo.status_code == 200
    assert "activations" in res_demo.json()

    res_live = intel_client.get("/api/v1/intelligence/fcm/live", headers=auth_headers)
    assert res_live.status_code == 200
    assert "initial_activations" in res_live.json()

    scenario_payload = {
        "base": {"complaint_frequency": 0.5, "operational_capacity": 0.6},
        "deltas": {"complaint_frequency": 0.3},
    }
    res_sc = intel_client.post("/api/v1/intelligence/fcm/scenario", json=scenario_payload, headers=auth_headers)
    assert res_sc.status_code == 200
    assert "delta_risk" in res_sc.json()


def test_api_simulations_endpoints(intel_client, auth_headers):
    res_def = intel_client.get("/api/v1/intelligence/simulations/defaults", headers=auth_headers)
    assert res_def.status_code == 200
    assert "suggested_base_rate" in res_def.json()

    sim_payload = {"base_rate": 50.0, "capacity": 45.0, "n_runs": 500, "seed": 42}
    res_run = intel_client.post("/api/v1/intelligence/simulations/run", json=sim_payload, headers=auth_headers)
    assert res_run.status_code == 200
    assert "scenarios" in res_run.json()
    assert "current" in res_run.json()["scenarios"]


def test_api_priority_endpoints(intel_client, auth_headers):
    res_score = intel_client.get("/api/v1/intelligence/priority/score?category=Potholes&description=dangerous+hole", headers=auth_headers)
    assert res_score.status_code == 200
    assert "score" in res_score.json()
    assert "factors" in res_score.json()

    res_queue = intel_client.get("/api/v1/intelligence/priority/queue", headers=auth_headers)
    assert res_queue.status_code == 200
    assert "items" in res_queue.json()
    assert "summary" in res_queue.json()
