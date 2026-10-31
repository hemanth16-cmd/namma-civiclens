"""Predictive analytics baselines (honest historical-rate methods)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_aggregates(path: Path | None = None) -> dict[str, Any]:
    p = path or Path(__file__).resolve().parents[2] / "data" / "intelligence" / "historical_aggregates.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def ward_elevated_activity(aggregates: dict[str, Any] | None = None, top_n: int = 15) -> list[dict[str, Any]]:
    agg = aggregates or load_aggregates()
    by_ward = agg.get("by_ward") or {}
    if not by_ward:
        return []
    ranked = sorted(by_ward.items(), key=lambda x: x[1], reverse=True)[:top_n]
    total = sum(by_ward.values()) or 1
    return [
        {
            "ward": w,
            "historical_count": c,
            "share_of_sample": round(c / total, 4),
            "label": "elevated_complaint_activity",
            "method": "historical_volume_baseline",
            "note": "Not a trained recurrence model; uses sample aggregates only.",
        }
        for w, c in ranked
    ]


def category_outlook(aggregates: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    agg = aggregates or load_aggregates()
    by_cat = agg.get("by_category") or {}
    total = sum(by_cat.values()) or 1
    return [
        {"category": cat, "historical_count": cnt, "share": round(cnt / total, 4), "method": "historical_share"}
        for cat, cnt in sorted(by_cat.items(), key=lambda x: -x[1])[:12]
    ]


def workload_forecast(aggregates: dict[str, Any] | None = None, horizon_days: int = 30) -> dict[str, Any]:
    agg = aggregates or load_aggregates()
    monthly = agg.get("by_month") or {}
    if not monthly:
        return {"horizon_days": horizon_days, "point_forecast": None, "note": "No monthly aggregates available"}
    counts = list(monthly.values())
    avg_month = sum(counts) / len(counts)
    point = avg_month * (horizon_days / 30.0)
    import math
    sd = math.sqrt(max(point, 1.0))
    return {
        "horizon_days": horizon_days,
        "point_forecast": round(point, 1),
        "approx_p10": round(max(0, point - 1.28 * sd), 1),
        "approx_p90": round(point + 1.28 * sd, 1),
        "method": "historical_monthly_average_scaled",
        "months_observed": len(counts),
        "note": "Baseline only; not a validated time-series model.",
    }

