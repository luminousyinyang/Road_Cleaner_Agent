"""The 24h duplicate check for live dashcam finds.

`find_recent_similar` is a pure function over a list of sightings, so these
tests state the rule directly rather than through a route: same hazard family,
within the duplicate radius, inside the window -- all three, or it is not a
duplicate.

The bias under test is the opposite of the confidence gate's. The gate would
rather miss a hazard than report one that is not there. This would rather send a
second email than hold a report nobody had made, because a duplicate email is
noise and a held first report is a hazard nobody is told about. Every ambiguous
case below is asserted in that direction.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from road_cleaner.domain.enums import HazardType
from road_cleaner.domain.gating import (
    DEDUP_WINDOW_HOURS,
    find_recent_similar,
    same_hazard_family,
)
from road_cleaner.domain.models import IncidentSighting

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)

# Downtown Atlanta, and a point ~150m north of it -- inside the 500m radius.
LAT, LNG = 33.7490, -84.3880
NEARBY = (33.7503, -84.3880)
# Roughly 3km away: same city, comfortably outside the radius.
ACROSS_TOWN = (33.7760, -84.3880)


def sighting(
    hazard: HazardType = HazardType.DEBRIS,
    *,
    at: datetime | None = None,
    where: tuple[float, float] = (LAT, LNG),
) -> IncidentSighting:
    return IncidentSighting(
        hazard_type=hazard,
        lat=where[0],
        lng=where[1],
        created_at=at or (NOW - timedelta(hours=1)),
    )


def matches(*sightings: IncidentSighting, hazard: HazardType = HazardType.DEBRIS):
    return find_recent_similar(hazard, LAT, LNG, NOW, list(sightings))


class TestTheThreeConditions:
    def test_nothing_reported_is_not_a_duplicate(self):
        assert matches() == []

    def test_same_hazard_near_here_recently(self):
        assert len(matches(sighting())) == 1

    def test_a_different_hazard_is_not_a_duplicate(self):
        """Flooding does not silence the debris on the same stretch."""
        assert matches(sighting(HazardType.FLOODING)) == []

    def test_too_far_away_is_not_a_duplicate(self):
        """A pothole is a place, not a topic.

        Without this, one report of a pothole would silence every other pothole
        in the state for a day.
        """
        assert matches(sighting(where=ACROSS_TOWN)) == []

    def test_just_outside_the_window_is_not_a_duplicate(self):
        old = NOW - timedelta(hours=DEDUP_WINDOW_HOURS, minutes=1)
        assert matches(sighting(at=old)) == []

    def test_just_inside_the_window_is(self):
        recent = NOW - timedelta(hours=DEDUP_WINDOW_HOURS, minutes=-1)
        assert len(matches(sighting(at=recent))) == 1

    def test_a_sighting_from_the_future_is_ignored(self):
        """Clock skew between instances should not invent a duplicate."""
        assert matches(sighting(at=NOW + timedelta(minutes=5))) == []


class TestHazardFamilies:
    """Two drivers, one object, two words for it."""

    def test_a_carcass_is_debris_or_an_animal(self):
        assert same_hazard_family(HazardType.ANIMAL, HazardType.DEBRIS)
        assert same_hazard_family(HazardType.DEBRIS, HazardType.ANIMAL)

    def test_broken_pavement_is_a_pothole_or_infrastructure_damage(self):
        assert same_hazard_family(
            HazardType.POTHOLE, HazardType.INFRASTRUCTURE_DAMAGE
        )

    def test_the_relation_is_not_transitive(self):
        """The bug this shape exists to prevent.

        Animal matches debris and debris matches animal, but that must not make
        every hazard that touches debris equivalent to every other. Collapsing
        the groups by connected component would silence a real hazard on the
        strength of an unrelated one.
        """
        assert same_hazard_family(HazardType.ANIMAL, HazardType.DEBRIS)
        assert not same_hazard_family(HazardType.ANIMAL, HazardType.POTHOLE)

    @pytest.mark.parametrize("hazard", list(HazardType))
    def test_every_hazard_matches_itself(self, hazard: HazardType):
        assert same_hazard_family(hazard, hazard)

    def test_a_pothole_does_not_match_debris(self):
        """The lesson already recorded on HAZARD_FAMILIES, kept here too."""
        assert not same_hazard_family(HazardType.POTHOLE, HazardType.DEBRIS)


class TestCountingAndOrder:
    def test_it_counts_every_match(self):
        assert len(matches(sighting(), sighting(), sighting())) == 3

    def test_it_counts_only_the_matches(self):
        found = matches(
            sighting(),
            sighting(HazardType.FLOODING),
            sighting(where=ACROSS_TOWN),
            sighting(at=NOW - timedelta(days=3)),
        )
        assert len(found) == 1

    def test_nearest_first(self):
        """The caller quotes the closest one, so it has to be first."""
        far = sighting(where=NEARBY)
        near = sighting()
        assert find_recent_similar(
            HazardType.DEBRIS, LAT, LNG, NOW, [far, near]
        ) == [near, far]
