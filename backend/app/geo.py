"""Geographic helpers shared by duplicate detection and geospatial queries.

Coordinates are WGS84 latitude/longitude in decimal degrees, exactly as the browser's Geolocation
API and photo EXIF provide them and as `complaints.gps_lat` / `gps_lng` store them. Distances are
great-circle (haversine) distances in metres on a sphere of the mean Earth radius; at city scale the
error against the WGS84 ellipsoid is well under 0.5 %, far below phone GPS accuracy.

Queries prefilter with a bounding box (plain comparisons that both SQLite and PostgreSQL can answer
from a (gps_lat, gps_lng) index) and then compute the exact distance in Python. No PostGIS needed at
this scale; see docs/CORE_PLATFORM_PLAN.md §7.
"""

import math

EARTH_RADIUS_M = 6_371_008.8      # IUGG mean Earth radius
# Bengaluru service area: the same box as dist/exif.js SERVICE_AREA (south, north, west, east).
SERVICE_AREA = {"south": 12.75, "north": 13.3, "west": 77.35, "east": 77.9}
METRES_PER_DEGREE_LAT = math.pi * EARTH_RADIUS_M / 180
# Public map points are rounded to this many decimal places: 3 ≈ 110 m north-south (about a city block),
# so the public map shows where an issue is without publishing the exact spot a report was made from.
PUBLIC_POINT_DECIMALS = 3
PUBLIC_POINT_PRECISION = "approx-110m"


def public_map_point(lat, lng) -> dict | None:
    """An approximate, public map position for a complaint, or None when the stored coordinates are not
    usable (missing, non-finite or out of range). Precise coordinates are never returned."""
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (lat, lng)) or abs(lat) > 90 or abs(lng) > 180:
        return None
    return {"lat": round(lat, PUBLIC_POINT_DECIMALS), "lng": round(lng, PUBLIC_POINT_DECIMALS), "precision": PUBLIC_POINT_PRECISION}


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two points, in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def bounding_box(lat: float, lng: float, radius_m: float) -> tuple[float, float, float, float]:
    """(min_lat, max_lat, min_lng, max_lng) containing every point within radius_m of (lat, lng).
    Near a pole, or when the box would cross the antimeridian, longitude widens to the full range
    rather than wrapping, so the prefilter never misses a point (the exact distance check follows)."""
    dlat = radius_m / METRES_PER_DEGREE_LAT
    min_lat, max_lat = max(-90.0, lat - dlat), min(90.0, lat + dlat)
    cos_lat = math.cos(math.radians(lat))
    if max_lat >= 90.0 or min_lat <= -90.0 or cos_lat < 1e-6:
        return min_lat, max_lat, -180.0, 180.0
    dlng = dlat / cos_lat
    if lng - dlng < -180.0 or lng + dlng > 180.0:
        return min_lat, max_lat, -180.0, 180.0
    return min_lat, max_lat, lng - dlng, lng + dlng
