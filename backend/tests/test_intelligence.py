"""Unit tests for the Civic Intelligence engines."""

from __future__ import annotations

from app.intelligence import fcm, monte_carlo, priority, predict


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


def test_predict_baselines_graceful_empty():
    elev = predict.ward_elevated_activity(aggregates={"by_ward": {"Ward A": 10, "Ward B": 5}})
    assert elev[0]["ward"] == "Ward A"
    assert elev[0]["label"] == "elevated_complaint_activity"

