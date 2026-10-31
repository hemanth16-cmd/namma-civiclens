"""Monte Carlo simulation for future civic incident demand uncertainty."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass
class SimulationResult:
    median: float
    mean: float
    p10: float
    p90: float
    exceedance_prob: float
    n_runs: int
    seed: int
    scenario: str
    assumptions: list[str]
    samples_summary: dict[str, float]


def simulate_demand(
    base_rate: float,
    *,
    n_runs: int = 5000,
    seed: int = 42,
    capacity: float | None = None,
    volume_multiplier: float = 1.0,
    efficiency: float = 1.0,
    distribution: str = "poisson",
) -> SimulationResult:
    rng = np.random.default_rng(seed)
    rate = max(0.0, base_rate * volume_multiplier)
    if distribution == "negbin":
        r = 5.0
        p = r / (r + rate) if rate > 0 else 1.0
        samples = rng.negative_binomial(r, p, size=n_runs).astype(float)
    else:
        samples = rng.poisson(rate, size=n_runs).astype(float)

    if capacity is not None and capacity > 0:
        effective_cap = capacity * efficiency
        backlog = np.maximum(samples - effective_cap, 0.0)
        exceed = float(np.mean(samples > effective_cap))
    else:
        backlog = samples * 0.0
        exceed = 0.0

    return SimulationResult(
        median=float(np.median(samples)),
        mean=float(np.mean(samples)),
        p10=float(np.percentile(samples, 10)),
        p90=float(np.percentile(samples, 90)),
        exceedance_prob=exceed,
        n_runs=n_runs,
        seed=seed,
        scenario="custom",
        assumptions=[
            f"Distribution: {distribution}",
            f"Base rate: {base_rate:.2f}",
            f"Volume multiplier: {volume_multiplier}",
            f"Efficiency: {efficiency}",
        ],
        samples_summary={
            "min": float(np.min(samples)),
            "max": float(np.max(samples)),
            "std": float(np.std(samples)),
            "mean_backlog": float(np.mean(backlog)),
        },
    )


def run_scenarios(
    base_rate: float,
    capacity: float | None = None,
    n_runs: int = 5000,
    seed: int = 42,
) -> dict[str, Any]:
    scenarios = {
        "current": simulate_demand(base_rate, n_runs=n_runs, seed=seed, capacity=capacity),
        "increased_volume": simulate_demand(
            base_rate, n_runs=n_runs, seed=seed+1, capacity=capacity, volume_multiplier=1.4),
        "increased_capacity": simulate_demand(
            base_rate, n_runs=n_runs, seed=seed+2, capacity=(capacity or base_rate)*1.3),
        "reduced_efficiency": simulate_demand(
            base_rate, n_runs=n_runs, seed=seed+3, capacity=capacity, efficiency=0.7),
    }
    return {
        name: {
            "median": s.median, "mean": s.mean, "p10": s.p10, "p90": s.p90,
            "exceedance_prob": s.exceedance_prob, "n_runs": s.n_runs, "seed": s.seed,
            "assumptions": s.assumptions, "summary": s.samples_summary,
        }
        for name, s in scenarios.items()
    }

