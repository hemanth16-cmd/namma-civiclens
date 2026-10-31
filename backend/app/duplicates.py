"""Duplicate detection: explainable scoring of candidate pairs (Workstream 2). Pure functions; the
store finds candidates (bounding box, category, time window) and this module scores and labels them.

Nothing here merges anything. Scores are suggestions for a supervisor, who decides.

Signals, each a value between 0 and 1 with a fixed weight (docs/CORE_PLATFORM_PLAN.md §6):
- location (0.45): distance relative to the combined GPS uncertainty of both reports. 30 m apart with
  ±40 m fixes is "same place"; 30 m apart with ±5 m fixes is not.
- category (0.20): same category 1.0, a related category 0.5 (e.g. potholes and road cracks).
- time (0.20): halves every 14 days between the two reports.
- description (0.15): character-trigram overlap. Left out (and the other weights rescaled) when either
  description is too short to compare, so a missing description neither helps nor hurts.
An identical evidence file (same SHA-256) is near-conclusive and lifts the score to at least 0.97.

Labels: likely_duplicate (≥ 0.75), possible_duplicate (≥ 0.5), nearby_separate, and recurrence (an
earlier report at the same spot was already marked resolved: the problem may have come back, which
is not the same as a duplicate report). Two reports too far apart for their GPS accuracy are always
nearby_separate, however similar the rest, which is how two potholes 40 m apart stay separate.
Low-accuracy locations (worse than ±100 m) cap the score at 0.6, because distance can no longer
tell neighbouring issues apart.
"""

import math
import re
from datetime import datetime

from .geo import haversine_m

DEFAULT_RADIUS_M = 250
MAX_RADIUS_M = 1000
DEFAULT_WINDOW_DAYS = 90
MAX_WINDOW_DAYS = 365
MAX_CANDIDATES = 10

WEIGHTS = {"location": 0.45, "category": 0.20, "time": 0.20, "description": 0.15}
GPS_FLOOR_M = 10.0          # added to every uncertainty: even a perfect fix marks a spot, not a point
UNKNOWN_ACCURACY_M = 50.0   # used when a report has no accuracy figure
LOW_ACCURACY_M = 100.0
LOW_ACCURACY_CAP = 0.6
TIME_HALF_LIFE_DAYS = 14.0
SAME_FILE_FLOOR = 0.97
LIKELY, POSSIBLE = 0.75, 0.5
SEPARATE_BELOW = 0.05       # location factor below this: clearly different spots

RELATED_CATEGORIES = {
    "Potholes": {"Road cracks and damage"},
    "Road cracks and damage": {"Potholes"},
    "Garbage accumulation": {"Illegal dumping"},
    "Illegal dumping": {"Garbage accumulation"},
    "Drainage or waterlogging": {"Water leakage"},
    "Water leakage": {"Drainage or waterlogging"},
}

LABELS = ("likely_duplicate", "possible_duplicate", "recurrence", "nearby_separate")


def comparable_categories(category: str) -> set[str]:
    return {category, *RELATED_CATEGORIES.get(category, set())}


def _trigrams(text: str) -> set[str]:
    words = re.findall(r"\w+", (text or "").lower())
    grams = set()
    for w in words:
        padded = f"  {w} "
        grams.update(padded[i:i + 3] for i in range(len(padded) - 2))
    return grams


def text_similarity(a: str, b: str) -> float | None:
    """Jaccard overlap of character trigrams, or None when either text has fewer than 3 letters or digits."""
    if len(re.sub(r"\W", "", a or "")) < 3 or len(re.sub(r"\W", "", b or "")) < 3:
        return None
    ga, gb = _trigrams(a), _trigrams(b)
    return len(ga & gb) / len(ga | gb) if ga and gb else None


def _accuracy(report: dict) -> float:
    acc = report.get("accuracy")
    return float(acc) if isinstance(acc, (int, float)) and math.isfinite(acc) and acc >= 0 else UNKNOWN_ACCURACY_M


def _when(report: dict) -> datetime:
    return datetime.fromisoformat(report["created"].replace("Z", "+00:00"))


def score_pair(target: dict, candidate: dict) -> dict:
    """Score how likely `candidate` is a duplicate of `target`. Both are dicts with id, category,
    created (ISO 8601), status, description, lat, lng, accuracy and sha256 (original evidence or None)."""
    distance = haversine_m(target["lat"], target["lng"], candidate["lat"], candidate["lng"])
    acc_t, acc_c = _accuracy(target), _accuracy(candidate)
    sigma = math.sqrt(acc_t ** 2 + acc_c ** 2 + GPS_FLOOR_M ** 2)
    location = math.exp(-0.5 * (distance / sigma) ** 2)

    if candidate["category"] == target["category"]:
        category, category_detail = 1.0, "Same category"
    elif candidate["category"] in RELATED_CATEGORIES.get(target["category"], set()):
        category, category_detail = 0.5, f"Related category ({candidate['category']})"
    else:
        category, category_detail = 0.0, f"Different category ({candidate['category']})"

    gap_days = abs((_when(candidate) - _when(target)).total_seconds()) / 86400
    time = 0.5 ** (gap_days / TIME_HALF_LIFE_DAYS)
    description = text_similarity(target.get("description", ""), candidate.get("description", ""))

    values = {"location": location, "category": category, "time": time, "description": description}
    used = {k: w for k, w in WEIGHTS.items() if values[k] is not None}
    total = sum(used.values())
    factors = []
    for name, weight in WEIGHTS.items():
        value = values[name]
        effective = used[name] / total if name in used else 0.0
        detail = {
            "location": f"{distance:.0f} m apart; combined GPS uncertainty about ±{sigma:.0f} m",
            "category": category_detail,
            "time": f"{gap_days:.1f} days apart",
            "description": "Not compared (a description is missing or too short)" if value is None
                           else f"{value:.0%} wording overlap",
        }[name]
        factors.append({"name": name, "value": None if value is None else round(value, 3), "weight": round(effective, 3),
                        "contribution": round((value or 0.0) * effective, 3), "detail": detail})
    score = sum(f["contribution"] for f in factors)

    reasons, caution = [], None
    same_file = bool(target.get("sha256")) and target.get("sha256") == candidate.get("sha256")
    if same_file:
        score = max(score, SAME_FILE_FLOOR)
        reasons.append("Both reports carry the identical evidence file.")
    worst = max(acc_t, acc_c)
    if worst > LOW_ACCURACY_M and not same_file:
        score = min(score, LOW_ACCURACY_CAP)
        caution = f"Location accuracy is low (±{worst:.0f} m), so distance cannot tell neighbouring issues apart."

    earlier = _when(candidate) < _when(target)
    if not same_file and location < SEPARATE_BELOW:
        label = "nearby_separate"
        reasons.append("Too far apart for their GPS accuracy to be the same spot; likely a separate issue nearby.")
    elif not same_file and earlier and candidate.get("status") == "Marked Resolved" and location >= 0.3 and category > 0:
        label = "recurrence"
        reasons.append("An earlier report at this spot was marked resolved; the problem may have returned (a recurrence, not a duplicate report).")
    elif score >= LIKELY:
        label = "likely_duplicate"
    elif score >= POSSIBLE:
        label = "possible_duplicate"
    else:
        label = "nearby_separate"
    if location >= 0.6:
        reasons.append("Within the GPS uncertainty of each other.")
    if description is not None and description >= 0.5:
        reasons.append("Similar description.")
    if gap_days <= 3:
        reasons.append("Reported within a few days of each other.")
    return {"score": round(score, 3), "label": label, "distanceM": round(distance, 1), "factors": factors,
            "reasons": reasons, "caution": caution}


def rank(target: dict, candidates: list[dict], limit: int = MAX_CANDIDATES) -> list[dict]:
    """Score every candidate and return the best `limit`, highest score first (ties: nearest first)."""
    scored = [{**c, **score_pair(target, c)} for c in candidates if c["id"] != target["id"]]
    scored.sort(key=lambda c: (-c["score"], c["distanceM"]))
    return scored[:limit]
