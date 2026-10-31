"""Ward and zone rules: the e-Aasthi master list, GPS → ward by boundary polygon, and supervisor
geographic scope. Pure functions with no SQL or HTTP; store.py applies them inside its transactions.

Ward assignment is honest about what the data supports (docs/WARD_DATA_AND_GRIEVANCE_REPORT.md):
- the e-Aasthi list has ward numbers, names and zones but **no boundaries**;
- a complaint is assigned automatically ("auto") only when its GPS point falls inside exactly one
  active ward's `boundary_geojson` polygon, imported from an official boundary file;
- otherwise it stays "unverified" with no ward, until an authorised supervisor sets the ward by hand
  ("manual"). A ward is never guessed from the address, neighbourhood text or nearest-ward distance.

Scope: a supervisor acts on wards in their staff_geographic_scope rows (city-wide, whole zones or
single wards). An admin is always city-wide (admin outranks supervisor in lifecycle.ROLES). Scope is
granted only through the CLI (python -m app.staff scope ...), never through the API.
"""

import json
import math
from dataclasses import dataclass
from pathlib import Path

from . import geo
from .lifecycle import RuleViolation, has_role

SEED_PATH = Path(__file__).resolve().parent.parent / "seeds" / "wards_e_aasthi.json"
ASSIGNMENT_STATUSES = ("auto", "manual", "unverified")
PRIORITIES = ("low", "normal", "high", "urgent")
SCOPE_TYPES = ("city", "zone", "ward")
MAX_BOUNDARY_BYTES = 2_000_000  # one ward polygon; real ward boundaries are a few hundred KB at most


# ---------- Master list ----------

def load_seed(path: str | Path = SEED_PATH) -> list[dict]:
    """The e-Aasthi rows, checked: unique ward numbers, one name per zone number, integer keys."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data["wards"] if isinstance(data, dict) else data
    seen_wards, zone_names = set(), {}
    out = []
    for r in rows:
        ward, zone = int(r["ward_number"]), int(r["zone_number"])
        name, zone_name = str(r["ward_name"]).strip(), str(r["zone_name"]).strip()
        if not name or not zone_name:
            raise ValueError(f"Ward {ward}: empty ward or zone name")
        if ward in seen_wards:
            raise ValueError(f"Duplicate ward number {ward}")
        if zone_names.setdefault(zone, zone_name) != zone_name:
            raise ValueError(f"Zone {zone} has two names: {zone_names[zone]!r} and {zone_name!r}")
        seen_wards.add(ward)
        out.append({"ward_number": ward, "ward_name": name, "zone_number": zone, "zone_name": zone_name,
                    "testing": bool(r.get("testing"))})
    return out


# ---------- Boundaries and point-in-polygon ----------

def _ring_ok(ring) -> bool:
    return (isinstance(ring, list) and len(ring) >= 4 and ring[0] == ring[-1]
            and all(isinstance(p, list) and len(p) >= 2 and all(isinstance(v, (int, float)) and math.isfinite(v) for v in p[:2])
                    for p in ring))


def validate_boundary(geometry) -> dict:
    """A GeoJSON Polygon or MultiPolygon ([lng, lat] order, closed rings) lying in the Bengaluru service
    area. Returns the geometry; raises ValueError with a reason otherwise."""
    if not isinstance(geometry, dict) or geometry.get("type") not in ("Polygon", "MultiPolygon"):
        raise ValueError("Boundary must be a GeoJSON Polygon or MultiPolygon")
    polygons = [geometry.get("coordinates")] if geometry["type"] == "Polygon" else geometry.get("coordinates")
    if not isinstance(polygons, list) or not polygons:
        raise ValueError("Boundary has no coordinates")
    area = geo.SERVICE_AREA
    for polygon in polygons:
        if not isinstance(polygon, list) or not polygon or not all(_ring_ok(r) for r in polygon):
            raise ValueError("Each polygon needs closed rings of at least four [lng, lat] positions")
        for ring in polygon:
            for lng, lat, *_ in ring:
                if not (area["south"] <= lat <= area["north"] and area["west"] <= lng <= area["east"]):
                    raise ValueError("Boundary lies outside the Bengaluru service area (check [lng, lat] order)")
    if len(json.dumps(geometry)) > MAX_BOUNDARY_BYTES:
        raise ValueError("Boundary is too large")
    return {"type": geometry["type"], "coordinates": geometry["coordinates"]}


def _in_ring(lat: float, lng: float, ring: list) -> bool:
    """Ray casting (even-odd rule). Points exactly on an edge may fall either way; at GPS accuracy
    (metres) that is immaterial, and a point on a shared edge then matches 0 or 2 wards → unverified."""
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lng < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def point_in_boundary(lat: float, lng: float, geometry: dict) -> bool:
    polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    for polygon in polygons:
        outer, *holes = polygon
        if _in_ring(lat, lng, outer) and not any(_in_ring(lat, lng, h) for h in holes):
            return True
    return False


@dataclass(frozen=True)
class WardMatch:
    ward_pk: int | None
    status: str      # "auto" or "unverified"
    reason: str


def locate(lat, lng, boundaries: list[tuple[int, dict]]) -> WardMatch:
    """GPS → ward. `boundaries` holds (ward_pk, geometry) for active wards that have a boundary.
    Auto only for exactly one containing polygon; every other case is unverified, with the reason."""
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (lat, lng)):
        return WardMatch(None, "unverified", "no_gps")
    area = geo.SERVICE_AREA
    if not (area["south"] <= lat <= area["north"] and area["west"] <= lng <= area["east"]):
        return WardMatch(None, "unverified", "outside_service_area")
    if not boundaries:
        return WardMatch(None, "unverified", "no_boundaries")
    hits = [pk for pk, geometry in boundaries if point_in_boundary(lat, lng, geometry)]
    if len(hits) == 1:
        return WardMatch(hits[0], "auto", "boundary_match")
    return WardMatch(None, "unverified", "no_matching_boundary" if not hits else "overlapping_boundaries")


# ---------- Geographic scope ----------

def require_supervisor(staff: dict) -> None:
    if not has_role(staff, "supervisor"):
        raise RuleViolation(403, "forbidden", "Only a supervisor or above can use supervisor operations.")


def scope_wards(staff: dict, scope_rows: list[dict], wards: list[dict]) -> set[int] | None:
    """The ward pks this staff member may act on, or None for city-wide. `scope_rows` are their
    staff_geographic_scope rows ({scope_type, zone_pk, ward_pk}); `wards` are {pk, zone_pk} rows.
    An admin is city-wide. A supervisor with no scope rows has an empty scope (deny by default)."""
    require_supervisor(staff)
    if has_role(staff, "admin") or any(r["scope_type"] == "city" for r in scope_rows):
        return None
    zones = {r["zone_pk"] for r in scope_rows if r["scope_type"] == "zone"}
    allowed = {r["ward_pk"] for r in scope_rows if r["scope_type"] == "ward"}
    allowed |= {w["pk"] for w in wards if w["zone_pk"] in zones}
    return allowed


def require_ward_in_scope(ward_pk: int | None, allowed: set[int] | None, what: str = "act on this ward") -> None:
    if allowed is None:
        return
    if ward_pk is None or ward_pk not in allowed:
        raise RuleViolation(403, "out_of_scope", f"You can only {what} within your assigned wards and zones.")


def parse_boundary_file(path: str | Path, ward_property: str) -> dict[int, dict]:
    """An official GeoJSON FeatureCollection → {ward number: validated geometry}. Each feature must carry
    its ward number in `ward_property`; a missing or duplicate number stops the import (no name matching)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection" or not isinstance(data.get("features"), list):
        raise ValueError("Boundary file must be a GeoJSON FeatureCollection")
    out: dict[int, dict] = {}
    for i, feature in enumerate(data["features"], 1):
        raw = (feature.get("properties") or {}).get(ward_property) if isinstance(feature, dict) else None
        try:
            number = int(str(raw).strip())
        except ValueError:
            raise ValueError(f"Feature {i}: property {ward_property!r} is not a ward number ({raw!r})") from None
        if number in out:
            raise ValueError(f"Feature {i}: ward {number} appears more than once")
        try:
            out[number] = validate_boundary(feature.get("geometry"))
        except ValueError as exc:
            raise ValueError(f"Feature {i} (ward {number}): {exc}") from None
    if not out:
        raise ValueError("Boundary file has no features")
    return out
