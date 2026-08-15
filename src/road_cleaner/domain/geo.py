"""Distance on the surface of the Earth.

Used for exactly one question, asked constantly: is this hazard close enough to
something the DOT already posted that we should keep quiet?
"""

from __future__ import annotations

from math import asin, cos, radians, sin, sqrt

EARTH_RADIUS_M = 6_371_008.8


def haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres between two WGS84 points."""
    phi1, phi2 = radians(lat1), radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = radians(lng2 - lng1)
    a = sin(d_phi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * asin(sqrt(a))


def within_meters(
    lat1: float, lng1: float, lat2: float, lng2: float, radius_m: float
) -> bool:
    return haversine_meters(lat1, lng1, lat2, lng2) <= radius_m


def format_distance(meters: float) -> str:
    """Human phrasing for a trail entry, e.g. '210 metres' or '1.4 km'."""
    if meters < 1000:
        return f"{round(meters / 10) * 10:.0f} metres"
    return f"{meters / 1000:.1f} km"
