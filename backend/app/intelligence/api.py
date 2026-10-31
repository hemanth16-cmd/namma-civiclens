"""Civic Intelligence API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from ..auth import current_staff
from . import fcm, hotspots, knowledge_graph, monte_carlo, predict, priority

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
def intelligence_summary(request: Request, _staff=Depends(current_staff)):
    store = getattr(request.app.state, "store", None)
    wards = predict.ward_elevated_activity(top_n=5)
    outlook = predict.workload_forecast(horizon_days=30)
    hotspot_rep = hotspots.get_hotspot_report(store, top_wards=5) if store else {}

    return {
        "status": "ok",
        "products": [
            "predictive_baselines",
            "knowledge_graph",
            "fcm",
            "monte_carlo",
            "verified_hotspots",
            "priority_scoring",
            "historical_aggregates",
        ],
        "top_elevated_wards": wards,
        "workload_outlook_30d": outlook,
        "hotspot_summary": hotspot_rep.get("summary", {}),
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


@router.get("/hotspots")
def get_hotspots(
    request: Request,
    radius: float = Query(150.0, ge=20.0, le=1000.0),
    top_wards: int = Query(15, ge=1, le=50),
    _staff=Depends(current_staff),
):
    store = request.app.state.store
    return hotspots.get_hotspot_report(store, radius_m=radius, top_wards=top_wards)


@router.get("/graph")
def get_graph(
    request: Request,
    complaint_id: str | None = Query(None),
    ward: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    _staff=Depends(current_staff),
):
    store = request.app.state.store
    return knowledge_graph.build_knowledge_graph(
        store,
        complaint_id=complaint_id,
        ward=ward,
        limit=limit,
    )


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


@router.get("/fcm/live")
def fcm_live(request: Request, _staff=Depends(current_staff)):
    store = request.app.state.store
    initial = fcm.derive_live_activations(store)
    result = fcm.run_fcm(initial)
    return {
        "initial_activations": initial,
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


@router.get("/simulations/defaults")
def simulation_defaults(request: Request, _staff=Depends(current_staff)):
    store = getattr(request.app.state, "store", None)
    return monte_carlo.suggest_simulation_defaults(store)


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


@router.get("/priority/queue")
def priority_queue(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    _staff=Depends(current_staff),
):
    store = request.app.state.store
    return priority.score_complaints(store, limit=limit)
