"""Civic Intelligence API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ..auth import current_staff
from . import fcm, monte_carlo, predict, priority

router = APIRouter(prefix="/intelligence", tags=["intelligence"])


class FCMScenarioIn(BaseModel):
    base: dict[str, float] = Field(default_factory=dict)
    deltas: dict[str, float] = Field(default_factory=dict)


class SimRunIn(BaseModel):
    base_rate: float = Field(ge=0)
    capacity: float | None = None
    n_runs: int = Field(default=2000, ge=100, le=20000)
    seed: int = 42


@router.get("/summary")
def intelligence_summary(_staff=Depends(current_staff)):
    wards = predict.ward_elevated_activity(top_n=5)
    outlook = predict.workload_forecast(horizon_days=30)
    return {
        "status": "ok",
        "products": ["predictive_baselines", "fcm", "monte_carlo", "priority_scoring", "historical_aggregates"],
        "top_elevated_wards": wards,
        "workload_outlook_30d": outlook,
        "notes": [
            "Historical dataset lacks GPS; hotspots for historical data are ward-level.",
            "FCM weights are expert assumptions.",
            "Monte Carlo uses Poisson/NB draws with explicit seeds.",
            "Priority scores are policy-based, not trained ML coefficients.",
        ],
    }


@router.get("/predictions")
def predictions(_staff=Depends(current_staff)):
    return {
        "elevated_activity_wards": predict.ward_elevated_activity(),
        "category_outlook": predict.category_outlook(),
        "workload_7d": predict.workload_forecast(horizon_days=7),
        "workload_30d": predict.workload_forecast(horizon_days=30),
    }


@router.get("/fcm/demo")
def fcm_demo(_staff=Depends(current_staff)):
    result = fcm.run_fcm()
    return {
        "activations": result.activations,
        "risk_score": result.risk_score,
        "iterations": result.iterations,
        "converged": result.converged,
        "contributing": result.contributing,
        "assumptions": result.assumptions,
    }


@router.post("/fcm/scenario")
def fcm_scenario(body: FCMScenarioIn, _staff=Depends(current_staff)):
    return fcm.scenario(body.base or {}, body.deltas or {})


@router.post("/simulations/run")
def run_simulation(body: SimRunIn, _staff=Depends(current_staff)):
    scenarios = monte_carlo.run_scenarios(
        body.base_rate, capacity=body.capacity, n_runs=body.n_runs, seed=body.seed
    )
    return {"scenarios": scenarios}


@router.get("/priority/score")
def priority_score(
    category: str = Query(...),
    description: str = "",
    recurrence_count: int = 0,
    corroboration_count: int = 1,
    _staff=Depends(current_staff),
):
    r = priority.score_incident(
        category=category,
        description=description,
        recurrence_count=recurrence_count,
        corroboration_count=corroboration_count,
    )
    return {
        "score": r.score,
        "level": r.level,
        "factors": r.factors,
        "overrides": r.overrides,
        "explanation": r.explanation,
        "computed_at": r.computed_at,
        "policy": "configurable_weights_v1",
    }

