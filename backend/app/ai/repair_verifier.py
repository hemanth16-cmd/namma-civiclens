"""Before/after repair check: compares the AI results for a complaint's original photo and an
after-repair photo and suggests one of

    likely_repaired   the same place, the reported damage was clearly detected before and nothing is
                      detected now, in a usable photo
    damage_visible    the same place, and damage of the reported kind is still confidently detected
    inconclusive      anything else

The suggestion is for a human reviewer. It never changes the complaint. Rules (deliberately cautious):

- No detection in the after photo is NOT proof of repair. likely_repaired also needs a confirmed
  location, a confident detection in the original, a usable after photo, and no low-confidence
  ("possible") detection in the after photo.
- Location: the photos' content must match (OpenCV feature alignment). GPS supports a weak visual match
  but never establishes a location on its own; GPS far apart marks the photo as a likely wrong location.
- Any failed or missing AI result makes the outcome inconclusive.
- verification_confidence (how much the comparison supports its suggestion) is separate from the
  models' detection confidences, and is null for inconclusive outcomes.
"""

from .image_alignment import project_box

GPS_MATCH_M = 50.0        # phone GPS is often 10-30 m off in a city
GPS_MISMATCH_M = 200.0
STRONG_INLIERS, STRONG_RATIO = 25, 0.25
WEAK_INLIERS, WEAK_RATIO = 12, 0.15


def _relevant(result: dict, categories: set) -> list[dict]:
    return [d for d in result.get("detections", []) if d.get("category") in categories]


def _overlaps(a: dict, b: dict, margin: float = 0.25) -> bool:
    """True when box a intersects box b grown by `margin` of its size on each side."""
    bx, by = b["x"] - b["width"] * margin, b["y"] - b["height"] * margin
    bw, bh = b["width"] * (1 + 2 * margin), b["height"] * (1 + 2 * margin)
    return a["x"] < bx + bw and bx < a["x"] + a["width"] and a["y"] < by + bh and by < a["y"] + a["height"]


def _visual(alignment: dict) -> str:
    if not alignment.get("available"):
        return "unavailable"
    inliers, ratio = alignment.get("inliers", 0), alignment.get("inlierRatio", 0.0)
    if inliers >= STRONG_INLIERS and ratio >= STRONG_RATIO:
        return "strong"
    if inliers >= WEAK_INLIERS and ratio >= WEAK_RATIO:
        return "weak"
    return "none"


def assess(*, relevant_categories, original: list[dict], repair: list[dict], alignment: dict,
           complaint_gps: tuple | None, original_gps: tuple | None, repair_gps: tuple | None,
           quality_original: dict | None, quality_repair: dict | None,
           min_confidence: float, low_confidence: float, distance=None) -> dict:
    """`original` / `repair`: one entry per detector, {"detector", "state", "detections", "imageWidth", ...}
    (detections in the result_normalizer format). Returns outcome, verificationConfidence, locationCheck,
    gpsDistanceM and details (reasons and the measurements behind them)."""
    from .images import distance_m
    distance = distance or distance_m
    categories = set(relevant_categories)
    reasons = []

    # GPS: the repair photo's own EXIF position against the original's EXIF, else the complaint's GPS.
    reference = original_gps or complaint_gps
    gps = {"repairHasGps": repair_gps is not None, "reference": "original_photo" if original_gps else ("complaint" if complaint_gps else None)}
    dist = None
    if repair_gps and reference:
        dist = round(distance(reference, repair_gps), 1)
        gps["status"] = "match" if dist <= GPS_MATCH_M else ("far" if dist > GPS_MISMATCH_M else "uncertain")
    else:
        gps["status"] = "unknown"
    gps["distanceM"] = dist

    visual = _visual(alignment)
    if gps["status"] == "far":
        location = "mismatch"
    elif visual == "strong" or (visual == "weak" and gps["status"] == "match"):
        location = "match"
    else:
        location = "unverified"

    visual_score = 0.0
    if alignment.get("available"):
        visual_score = min(1.0, alignment.get("inlierRatio", 0) / 0.5) * min(1.0, alignment.get("inliers", 0) / 60)
    loc_score = max(visual_score, 0.5 if location == "match" else 0.0)
    if gps["status"] == "match":
        loc_score = min(1.0, loc_score + 0.1)

    by_detector = {r["detector"]: r for r in repair}
    comparison, incomplete = [], []
    orig_present, rep_present, rep_possible = [], [], []
    for o in original:
        r = by_detector.get(o["detector"])
        if o.get("state") != "completed" or r is None or r.get("state") != "completed":
            incomplete.append(o["detector"])
            continue
        o_rel, r_rel = _relevant(o, categories), _relevant(r, categories)
        o_p = [d for d in o_rel if d["confidence"] >= min_confidence]
        r_p = [d for d in r_rel if d["confidence"] >= min_confidence]
        r_low = [d for d in r_rel if low_confidence <= d["confidence"] < min_confidence]
        orig_present += o_p
        rep_present += r_p
        rep_possible += r_low
        comparison.append({"detector": o["detector"],
                           "originalPresent": len(o_p), "originalTopConfidence": max((d["confidence"] for d in o_rel), default=None),
                           "repairPresent": len(r_p), "repairPossible": len(r_low),
                           "repairTopConfidence": max((d["confidence"] for d in r_rel), default=None)})

    quality_ok = bool(quality_repair and quality_repair.get("acceptable"))
    overlaps = None
    H = alignment.get("homography")
    if rep_present and orig_present and H:
        projected = [p for p in (project_box(d["bbox"], H) for d in orig_present) if p]
        overlaps = any(_overlaps(r["bbox"], p) for r in rep_present for p in projected)

    outcome, confidence = "inconclusive", None
    if not original:
        reasons.append("No AI detector is configured for this complaint's category, so the photos were not compared.")
    elif incomplete:
        reasons.append(f"AI analysis is not complete for: {', '.join(incomplete)}.")
    elif location == "mismatch":
        reasons.append(f"The after photo was taken about {round(dist)} m from the reported location, so it may show a different place.")
    elif rep_present and location == "match":
        outcome = "damage_visible"
        top = max(d["confidence"] for d in rep_present)
        confidence = loc_score * top * (1.0 if overlaps is not False else 0.7)
        reasons.append(f"Damage of the reported kind is still detected in the after photo (model confidence up to {top:.0%}).")
        if overlaps is False:
            reasons.append("The damage detected now is not where the original damage was; it may be different damage nearby.")
    elif rep_present:
        reasons.append("Damage is detected in the after photo, but the photos could not be confirmed to show the same place.")
    elif location != "match":
        reasons.append("The photos could not be confirmed to show the same place"
                       + (" (GPS alone is not enough)." if gps["status"] == "match" else "."))
    elif not quality_ok:
        problems = ", ".join((quality_repair or {}).get("problems", [])) or "unreadable"
        reasons.append(f"The after photo is not good enough to rely on an absence of damage ({problems}).")
    elif not orig_present:
        reasons.append("The reported damage was not confidently detected in the original photo, so its absence now proves nothing.")
    elif rep_possible:
        reasons.append("Possible damage is detected in the after photo at low confidence.")
    else:
        outcome = "likely_repaired"
        before = max(d["confidence"] for d in orig_present)
        residual = max((d["confidence"] for d in _relevant_all(repair, categories)), default=0.0)
        confidence = loc_score * before * (1 - residual)
        reasons.append(f"The original damage (detected at {before:.0%}) is no longer detected, in a photo of the same place.")
    if outcome != "inconclusive":
        reasons.append("This is an AI suggestion; a reviewer must confirm it.")
    if location == "match":
        reasons.append(f"Same place: {alignment.get('inliers', 0)} matching image features"
                       + (f"; GPS {round(dist)} m apart." if dist is not None else "; no GPS in the after photo."))

    alignment_summary = {k: v for k, v in alignment.items() if k != "homography"}
    return {
        "outcome": outcome,
        "verificationConfidence": round(min(0.99, max(0.0, confidence)), 3) if confidence is not None else None,
        "locationCheck": location,
        "gpsDistanceM": dist,
        "details": {"reasons": reasons, "gps": gps, "visualMatch": visual, "alignment": alignment_summary,
                    "quality": {"original": quality_original, "repair": quality_repair},
                    "comparison": comparison, "overlapsOriginalDamage": overlaps,
                    "thresholds": {"present": min_confidence, "low": low_confidence, "gpsMatchM": GPS_MATCH_M,
                                   "gpsMismatchM": GPS_MISMATCH_M, "strongInliers": STRONG_INLIERS, "strongRatio": STRONG_RATIO}},
    }


def _relevant_all(results: list[dict], categories: set) -> list[dict]:
    return [d for r in results for d in _relevant(r, categories)]
