"""Dynamic priority-based incident tracking engine.

Configurable policy scoring (not trained ML coefficients). Safety overrides
are explicit and audited.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


DEFAULT_WEIGHTS = {
    "severity": 30,
    "public_safety": 25,
    "recurrence": 20,
    "age_sla": 15,
    "corroboration": 10,
}

DEFAULT_THRESHOLDS = {
    "critical": 75,
    "high": 50,
    "medium": 25,
    "low": 0,
}

SAFETY_CRITICAL_KEYWORDS = (
    "exposed electrical", "live wire", "road collapse",
    "dangerous pothole", "open manhole", "gas leak", "structural collapse",
)


@dataclass
class PriorityResult:
    score: int
    level: str
    factors: dict[str, int]
    overrides: list[str] = field(default_factory=list)
    explanation: str = ""
    computed_at: str = ""


def _clamp(v: int, lo: int = 0, hi: int = 100) -> int:
    return max(lo, min(hi, v))


def score_incident(
    *,
    category: str,
    description: str = "",
    status: str = "",
    created_at: datetime | None = None,
    recurrence_count: int = 0,
    corroboration_count: int = 1,
    severity_hint: str | None = None,
    weights: dict[str, int] | None = None,
    thresholds: dict[str, int] | None = None,
    now: datetime | None = None,
) -> PriorityResult:
    w = weights or DEFAULT_WEIGHTS
    th = thresholds or DEFAULT_THRESHOLDS
    now = now or datetime.now(timezone.utc)
    factors: dict[str, int] = {}
    overrides: list[str] = []

    # Severity
    sev = 10
    cat_l = (category or "").lower()
    if any(k in cat_l for k in ("pothole", "road", "drain", "electrical", "street light")):
        sev = 18
    if any(k in cat_l for k in ("garbage", "waste", "dump")):
        sev = 12
    if severity_hint == "high":
        sev = 28
    elif severity_hint == "critical":
        sev = 30
    factors["severity"] = _clamp(sev, 0, w["severity"])

    # Public safety
    safety = 8
    desc_l = (description or "").lower()
    if any(k in desc_l or k in cat_l for k in ("pothole", "footpath", "drain", "waterlog", "street light")):
        safety = 15
    if any(k in desc_l for k in SAFETY_CRITICAL_KEYWORDS):
        safety = 25
        overrides.append("safety_critical_keyword")
    factors["public_safety"] = _clamp(safety, 0, w["public_safety"])

    # Recurrence
    rec = min(recurrence_count * 7, w["recurrence"])
    factors["recurrence"] = _clamp(rec, 0, w["recurrence"])

    # Age / SLA
    age_pts = 0
    if created_at is not None:
        age_days = max(0, (now - created_at).total_seconds() / 86400)
        if age_days > 30:
            age_pts = 15
        elif age_days > 14:
            age_pts = 10
        elif age_days > 7:
            age_pts = 6
        elif age_days > 3:
            age_pts = 3
    factors["age_sla"] = _clamp(age_pts, 0, w["age_sla"])

    # Corroboration
    corr = min(max(corroboration_count - 1, 0) * 3, w["corroboration"])
    factors["corroboration"] = _clamp(corr, 0, w["corroboration"])

    total = _clamp(sum(factors.values()))
    if overrides:
        total = max(total, th["critical"])

    level = "low"
    if total >= th["critical"]:
        level = "critical"
    elif total >= th["high"]:
        level = "high"
    elif total >= th["medium"]:
        level = "medium"

    explanation = (
        f"Score {total}/100 ({level}). "
        f"Factors: severity={factors['severity']}, safety={factors['public_safety']}, "
        f"recurrence={factors['recurrence']}, age/SLA={factors['age_sla']}, "
        f"corroboration={factors['corroboration']}."
    )
    if overrides:
        explanation += f" Overrides applied: {', '.join(overrides)}."

    return PriorityResult(
        score=total,
        level=level,
        factors=factors,
        overrides=overrides,
        explanation=explanation,
        computed_at=now.isoformat(),
    )


def score_complaints(store: Any, limit: int = 100) -> dict[str, Any]:
    """Score all live complaints from the store and return a prioritized queue."""
    try:
        raw_items, _total = store.list_complaints(limit=limit, offset=0)
    except Exception:
        raw_items = []

    scored_items = []
    now = datetime.now(timezone.utc)

    for c in raw_items:
        created_dt = None
        created_raw = c.get("created")
        if created_raw:
            try:
                created_dt = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            except Exception:
                pass

        recurrence = c.get("recurrence", 0)
        corroboration = c.get("corroboration", 1)

        result = score_incident(
            category=c.get("category", ""),
            description=c.get("description", "") or c.get("title", ""),
            status=c.get("status", ""),
            created_at=created_dt,
            recurrence_count=recurrence,
            corroboration_count=corroboration,
            now=now,
        )

        scored_items.append({
            "id": c.get("id"),
            "title": c.get("title") or f"{c.get('category')} reported by citizen",
            "category": c.get("category"),
            "area": c.get("area") or c.get("ward"),
            "status": c.get("status"),
            "created": c.get("created"),
            "priority": {
                "score": result.score,
                "level": result.level,
                "factors": result.factors,
                "overrides": result.overrides,
                "explanation": result.explanation,
                "computed_at": result.computed_at,
            },
        })

    # Sort descending by priority score
    scored_items.sort(key=lambda x: x["priority"]["score"], reverse=True)

    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for item in scored_items:
        lvl = item["priority"]["level"]
        if lvl in counts:
            counts[lvl] += 1

    return {
        "items": scored_items,
        "total": len(scored_items),
        "summary": counts,
        "policy": "configurable_weights_v1",
    }
