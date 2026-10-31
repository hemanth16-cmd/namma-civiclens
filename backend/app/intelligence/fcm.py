"""Fuzzy Cognitive Map engine for civic risk analysis."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

CONCEPTS = [
    "complaint_frequency", "recent_growth", "issue_severity",
    "public_safety_impact", "historical_recurrence", "service_backlog",
    "resolution_delay_risk", "geographic_concentration",
    "infrastructure_vulnerability", "operational_capacity", "overall_civic_risk",
]

DEFAULT_W = np.array([
    [0.0, 0.4, 0.2, 0.1, 0.3, 0.2, 0.1, 0.3, 0.1, -0.1, 0.5],
    [0.2, 0.0, 0.1, 0.1, 0.2, 0.3, 0.2, 0.2, 0.1, -0.2, 0.4],
    [0.1, 0.0, 0.0, 0.5, 0.1, 0.1, 0.2, 0.0, 0.3, 0.0, 0.5],
    [0.0, 0.0, 0.2, 0.0, 0.1, 0.1, 0.1, 0.0, 0.2, 0.0, 0.6],
    [0.3, 0.2, 0.1, 0.2, 0.0, 0.2, 0.2, 0.3, 0.3, -0.1, 0.5],
    [0.1, 0.1, 0.0, 0.1, 0.1, 0.0, 0.4, 0.1, 0.1, -0.3, 0.4],
    [0.1, 0.1, 0.1, 0.2, 0.1, 0.3, 0.0, 0.0, 0.1, -0.2, 0.4],
    [0.2, 0.1, 0.0, 0.1, 0.2, 0.1, 0.0, 0.0, 0.2, 0.0, 0.3],
    [0.1, 0.0, 0.3, 0.3, 0.2, 0.1, 0.1, 0.1, 0.0, 0.0, 0.4],
    [-0.3, -0.2, 0.0, -0.1, -0.1, -0.4, -0.3, 0.0, 0.0, 0.0, -0.5],
    [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
], dtype=float)


def sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


@dataclass
class FCMResult:
    activations: dict[str, float]
    risk_score: float
    iterations: int
    converged: bool
    assumptions: list[str]
    contributing: list[dict[str, Any]]


def run_fcm(
    initial: dict[str, float] | None = None,
    W: np.ndarray | None = None,
    max_iter: int = 50,
    tol: float = 1e-4,
) -> FCMResult:
    W = DEFAULT_W if W is None else W
    n = len(CONCEPTS)
    A = np.zeros(n)
    if initial:
        for i, name in enumerate(CONCEPTS):
            if name in initial:
                A[i] = float(np.clip(initial[name], 0.0, 1.0))
    else:
        A[:] = 0.3
        A[CONCEPTS.index("operational_capacity")] = 0.6

    converged = False
    for it in range(1, max_iter + 1):
        A_new = sigmoid(A @ W)
        if np.max(np.abs(A_new - A)) < tol:
            A = A_new
            converged = True
            break
        A = A_new

    activations = {name: float(A[i]) for i, name in enumerate(CONCEPTS)}
    risk = activations["overall_civic_risk"]
    risk_col = W[:, CONCEPTS.index("overall_civic_risk")]
    contrib = sorted(
        [{"concept": CONCEPTS[i], "activation": activations[CONCEPTS[i]],
          "influence_on_risk": float(risk_col[i]),
          "weighted": float(risk_col[i] * activations[CONCEPTS[i]])}
         for i in range(n - 1)],
        key=lambda x: abs(x["weighted"]), reverse=True,
    )

    return FCMResult(
        activations=activations,
        risk_score=risk,
        iterations=it,
        converged=converged,
        assumptions=[
            "Influence weights are expert-defined assumptions, not fitted from data.",
            "Activation function is logistic sigmoid.",
            "Concepts are normalized to [0,1].",
        ],
        contributing=contrib[:6],
    )


def scenario(base: dict[str, float], deltas: dict[str, float]) -> dict[str, Any]:
    r0 = run_fcm(base)
    perturbed = dict(base)
    for k, d in deltas.items():
        perturbed[k] = float(np.clip(perturbed.get(k, 0) + d, 0.0, 1.0))
    r1 = run_fcm(perturbed)
    return {
        "baseline_risk": r0.risk_score,
        "scenario_risk": r1.risk_score,
        "delta_risk": r1.risk_score - r0.risk_score,
        "baseline_activations": r0.activations,
        "scenario_activations": r1.activations,
        "deltas_applied": deltas,
        "assumptions": r0.assumptions,
    }

