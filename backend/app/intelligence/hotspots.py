"""Verified Recurring Hotspot Detection engine.

Performs spatial clustering on live citizen complaints with genuine GPS coordinates
and aggregates historical BBMP grievances at the ward level.

Important constraints:
- Does NOT fabricate GPS coordinates: historical records lack lat/lng, so historical
  hotspot detection is strictly ward-level.
- Live spatial hotspots are computed from actual database records using Haversine
  distance clustering and explicit recurrence history.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from ..geo import haversine_m
from ..store import ComplaintStore
from .predict import load_aggregates

logger = logging.getLogger(__name__)

DEFAULT_CLUSTER_RADIUS_M = 150.0


def detect_historical_hotspots(
    aggregates: dict[str, Any] | None = None,
    top_n: int = 15,
) -> list[dict[str, Any]]:
    """Identifies wards with elevated historical grievance volume and recurrence density."""
    agg = aggregates or load_aggregates()
    by_ward = agg.get("by_ward") or {}
    ward_cat = agg.get("ward_category_top") or []
    if not by_ward:
        return []

    # Map top categories per ward
    ward_top_cats: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in ward_cat:
        w = entry.get("ward")
        c = entry.get("category")
        cnt = entry.get("count", 0)
        if w and c and len(ward_top_cats[w]) < 3:
            ward_top_cats[w].append({"category": c, "count": cnt})

    total = sum(by_ward.values()) or 1
    ranked = sorted(by_ward.items(), key=lambda x: x[1], reverse=True)[:top_n]

    hotspots = []
    for rank, (w, count) in enumerate(ranked, 1):
        share = round(count / total, 4)
        hotspots.append({
            "rank": rank,
            "ward": w,
            "historical_count": count,
            "share_of_total": share,
            "top_categories": ward_top_cats.get(w, []),
            "recurrence_indicator": "elevated_historical_activity" if share >= 0.015 else "moderate_activity",
            "scope": "ward_level_aggregate",
            "note": "Historical record has no lat/lng; hotspot detection is ward-level only.",
        })
    return hotspots


def detect_live_hotspots(
    store: ComplaintStore,
    *,
    radius_m: float = DEFAULT_CLUSTER_RADIUS_M,
    min_complaints: int = 2,
    category: str | None = None,
) -> list[dict[str, Any]]:
    """Detects recurring spatial clusters of complaints with genuine GPS coordinates."""
    try:
        items, _total = store.list_complaints(limit=500, offset=0)
        complaints = items
    except Exception as e:
        logger.warning("Could not list complaints for live hotspot detection: %s", e)
        return []

    # Filter complaints with valid GPS
    geo_complaints = []
    for c in complaints:
        gps = c.get("gps")
        if gps and isinstance(gps, dict):
            lat = gps.get("lat")
            lng = gps.get("lng")
            if lat is not None and lng is not None:
                if category and c.get("category") != category:
                    continue
                geo_complaints.append({
                    "id": c.get("id"),
                    "lat": float(lat),
                    "lng": float(lng),
                    "category": c.get("category", "Unknown"),
                    "status": c.get("status", "Unknown"),
                    "ward": c.get("area") or "Unknown",
                    "created": c.get("created", ""),
                })

    if not geo_complaints:
        return []

    # Simple greedy spatial clustering with real coordinates
    assigned = [False] * len(geo_complaints)
    clusters = []

    for i, c1 in enumerate(geo_complaints):
        if assigned[i]:
            continue

        cluster_members = [c1]
        assigned[i] = True

        for j, c2 in enumerate(geo_complaints):
            if i != j and not assigned[j]:
                d = haversine_m(c1["lat"], c1["lng"], c2["lat"], c2["lng"])
                if d <= radius_m:
                    cluster_members.append(c2)
                    assigned[j] = True

        if len(cluster_members) >= min_complaints:
            avg_lat = sum(m["lat"] for m in cluster_members) / len(cluster_members)
            avg_lng = sum(m["lng"] for m in cluster_members) / len(cluster_members)
            cats = list({m["category"] for m in cluster_members})
            statuses = [m["status"] for m in cluster_members]
            has_resolved = any(s in ("Marked Resolved", "Work Completed") for s in statuses)
            has_active = any(s not in ("Marked Resolved", "Work Completed") for s in statuses)
            is_recurrence = has_resolved and has_active

            clusters.append({
                "cluster_id": f"hotspot_{len(clusters) + 1}",
                "center": {"lat": round(avg_lat, 6), "lng": round(avg_lng, 6)},
                "radius_m": radius_m,
                "incident_count": len(cluster_members),
                "complaint_ids": [m["id"] for m in cluster_members],
                "categories": cats,
                "ward": cluster_members[0]["ward"],
                "is_verified_recurrence": is_recurrence,
                "status_summary": {s: statuses.count(s) for s in set(statuses)},
            })

    # Sort by incident count descending
    clusters.sort(key=lambda x: x["incident_count"], reverse=True)
    return clusters


def get_hotspot_report(
    store: ComplaintStore,
    *,
    radius_m: float = DEFAULT_CLUSTER_RADIUS_M,
    top_wards: int = 10,
) -> dict[str, Any]:
    """Combined verified recurring hotspot report."""
    historical = detect_historical_hotspots(top_n=top_wards)
    live = detect_live_hotspots(store, radius_m=radius_m)

    return {
        "status": "ok",
        "live_spatial_hotspots": live,
        "historical_ward_hotspots": historical,
        "summary": {
            "live_clusters_found": len(live),
            "live_verified_recurrences": sum(1 for c in live if c["is_verified_recurrence"]),
            "historical_top_wards_analyzed": len(historical),
        },
        "methodology": {
            "live_clustering": f"Haversine spatial distance (radius={radius_m}m, threshold=2+ incidents)",
            "historical_baseline": "Ward-level grievance frequency aggregation from 1.23M BBMP records",
            "gps_fabrication": "None: historical data has no GPS coordinates; live data uses reported GPS only",
        },
    }
