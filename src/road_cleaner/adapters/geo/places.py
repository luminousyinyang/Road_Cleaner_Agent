"""Turning a coordinate into somewhere a person can be sent.

A dropped pin and a phone both give the same thing: two numbers. Everything
downstream needs more than that -- the jurisdiction rules match on a state, and
a report a crew reads needs a place name, not just a decimal pair.

Nothing here calls anything. Two files ship in the image:

* `us_states.json.gz` -- simplified state outlines, 22 KB, for point-in-polygon.
  This answers *which state*, and it is the answer that matters: it picks the
  agency the report goes to, so it has to be authoritative rather than nearest.
* `us_places.tsv.gz` -- 32,000 US places from the Census gazetteer, 389 KB. This
  answers *near where*, by straight nearest-neighbour.

A network lookup would have been less code. It would also mean a demo that fails
when somebody else's service is slow, a key to keep alive, and a rate limit to
respect on the one screen a judge is looking at. 411 KB in the image is a better
trade, and it is why `pyproject.toml` has no new dependency for any of this.

**The two answers are not equally strong, and the callers are told so.** The
state comes from a boundary and is right or the point is not in the US. The
place is the nearest of 32,000 centroids, which in rural Montana can be twenty
miles away -- so it is offered as "near X", never as "in X", and the coordinates
always lead.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from road_cleaner.domain.geo import haversine_meters

DATA = Path(__file__).parent / "data"
STATES_FILE = DATA / "us_states.json.gz"
PLACES_FILE = DATA / "us_places.tsv.gz"

# The lower 48 plus DC. Alaska and Hawaii are excluded deliberately: this system
# routes to a state DOT and files on that agency's own channel, and neither has
# been checked. A pin there is refused rather than routed on a guess.
MAINLAND: frozenset[str] = frozenset([
    "AL", "AR", "AZ", "CA", "CO", "CT", "DC", "DE", "FL", "GA", "IA", "ID",
    "IL", "IN", "KS", "KY", "LA", "MA", "MD", "ME", "MI", "MN", "MO", "MS",
    "MT", "NC", "ND", "NE", "NH", "NJ", "NM", "NV", "NY", "OH", "OK", "OR",
    "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VA", "VT", "WA", "WI", "WV",
    "WY",
])

# A rectangle around the lower 48, checked before the polygons because it is
# free and rejects the overwhelming majority of nonsense (a zeroed coordinate
# lands in the Gulf of Guinea, which is exactly what an unset lat/lng looks like).
MAINLAND_BOUNDS = (24.4, -125.1, 49.4, -66.9)  # south, west, north, east


class OutsideCoverageError(ValueError):
    """The point is not somewhere this system knows how to file a report.

    Raised rather than resolved to a nearest guess. Filing with the wrong agency
    wastes a stranger's time and leaves the hazard exactly where it was, which is
    the whole reason the jurisdiction rules exist.
    """


@dataclass(frozen=True)
class Place:
    """Where a coordinate is, as much as we can honestly say."""

    lat: float
    lng: float
    state: str                    # postal code, from the boundary it falls in
    state_name: str
    nearest: str | None = None    # the closest place name, if one is close enough
    nearest_km: float | None = None

    @property
    def short(self) -> str:
        """Just the place, for a subject line.

        `label` leads with coordinates because a crew needs them; a subject line
        reading "Road hazard: debris on 39.96120, -82.99880" does not, and reads
        like a machine talking to itself.
        """
        return f"near {self.nearest}, {self.state}" if self.nearest else self.state_name

    @property
    def label(self) -> str:
        """One line for a report, coordinates first.

        The numbers lead because they are exact and the name is not. "near" is
        doing real work: the nearest of 32,000 centroids can be a long way off
        in open country, and a crew told "in Ismay" would go to the wrong place.
        """
        where = f"{self.lat:.5f}, {self.lng:.5f}"
        if self.nearest:
            return f"{where} — near {self.nearest}, {self.state}"
        return f"{where} — {self.state_name}"


@lru_cache(maxsize=1)
def _states() -> list[dict]:
    with gzip.open(STATES_FILE, "rt", encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def _places() -> list[tuple[str, str, float, float]]:
    rows = []
    with gzip.open(PLACES_FILE, "rt", encoding="utf-8") as fh:
        for line in fh:
            name, state, lat, lng = line.rstrip("\n").split("\t")
            rows.append((name, state, float(lat), float(lng)))
    return rows


def _in_ring(lng: float, lat: float, ring: list[list[float]]) -> bool:
    """Ray casting. Deliberately not a dependency.

    `shapely` would do this and bring GEOS with it -- tens of megabytes of
    compiled library for one predicate over 52 outlines.
    """
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and lng < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def state_at(lat: float, lng: float) -> tuple[str, str] | None:
    """The state containing this point, as (postal code, name)."""
    for state in _states():
        for ring in state["rings"]:
            if _in_ring(lng, lat, ring):
                return state["code"], state["name"]
    return None


# Beyond this, naming a town is not describing where the hazard is. Rural
# Montana and the Nevada basins genuinely have nothing within 40 km, and
# "near Ely" for a point an hour away helps nobody.
NEAREST_LIMIT_KM = 40.0


def nearest_place(lat: float, lng: float, state: str | None = None) -> tuple[str, float] | None:
    """The closest populated place, and how far away it is in kilometres.

    Restricted to one state when we know it, which is both faster and stops a
    point just inside Georgia being described as near a town in Alabama.
    """
    best: tuple[str, float] | None = None
    for name, code, plat, plng in _places():
        if state and code != state:
            continue
        km = haversine_meters(lat, lng, plat, plng) / 1000.0
        if best is None or km < best[1]:
            best = (name, km)
    if best is None or best[1] > NEAREST_LIMIT_KM:
        return None
    return best


def locate(lat: float, lng: float) -> Place:
    """Everything we can say about a coordinate, or a refusal.

    Raises `OutsideCoverageError` rather than guessing. A pin in the Atlantic and
    a pin in Anchorage are both places this system cannot file a report about,
    and saying so is more useful than picking the closest agency it happens to
    have on file.
    """
    south, west, north, east = MAINLAND_BOUNDS
    if not (south <= lat <= north and west <= lng <= east):
        raise OutsideCoverageError(
            f"{lat:.4f}, {lng:.4f} is outside the mainland United States. "
            "Road Cleaner files with US state DOTs, so there is nobody to send this to."
        )

    found = state_at(lat, lng)
    if found is None:
        raise OutsideCoverageError(
            f"{lat:.4f}, {lng:.4f} is not on land in the mainland United States."
        )

    code, name = found
    if code not in MAINLAND:
        raise OutsideCoverageError(
            f"{name} is outside the area this system covers."
        )

    near = nearest_place(lat, lng, code)
    return Place(
        lat=lat,
        lng=lng,
        state=code,
        state_name=name,
        nearest=near[0] if near else None,
        nearest_km=round(near[1], 1) if near else None,
    )
