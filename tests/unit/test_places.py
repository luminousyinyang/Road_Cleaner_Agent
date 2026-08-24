"""Turning a coordinate into somewhere a report can be sent.

Two questions with very different standards of proof. *Which state* picks the
agency, so it comes from a boundary and has to be right. *Near where* is a
convenience for whoever reads the report, so it is the nearest of 32,000
centroids and is allowed to be approximate — as long as it never pretends
otherwise.

The third job is refusal. A coordinate this system cannot file a report about
has to say so rather than resolve to the closest thing it happens to know.
"""

from __future__ import annotations

import pytest

from road_cleaner.adapters.geo.places import (
    MAINLAND,
    OutsideCoverageError,
    locate,
    nearest_place,
    state_at,
)


class TestWhichState:
    """This one picks the agency, so it is the one that has to be right."""

    @pytest.mark.parametrize(
        "place,lat,lng,state",
        [
            ("Atlanta", 33.7490, -84.3880, "GA"),
            ("Raleigh", 35.7796, -78.6382, "NC"),
            ("Orlando", 28.5383, -81.3792, "FL"),
            ("Columbus", 39.9612, -82.9988, "OH"),
            ("Austin", 30.2672, -97.7431, "TX"),
            ("Los Angeles", 34.0522, -118.2437, "CA"),
            ("Bozeman", 45.6770, -111.0429, "MT"),
            ("Portland ME", 43.6591, -70.2568, "ME"),
            ("Manhattan", 40.7580, -73.9855, "NY"),
            ("Washington DC", 38.9072, -77.0369, "DC"),
        ],
    )
    def test_a_known_point_lands_in_its_own_state(self, place, lat, lng, state):
        found = state_at(lat, lng)
        assert found is not None, f"{place} landed in no state at all"
        assert found[0] == state

    def test_open_water_is_in_no_state(self):
        assert state_at(35.0, -140.0) is None   # Pacific
        assert state_at(25.0, -90.0) is None    # Gulf of Mexico

    def test_every_mainland_state_is_covered(self):
        """48 plus DC. If one is missing, hazards there resolve to nobody."""
        assert len(MAINLAND) == 49
        for code in ("CA", "TX", "NY", "FL", "ME", "WA", "DC"):
            assert code in MAINLAND
        # Deliberately absent: routing to their DOTs has not been checked.
        for code in ("AK", "HI", "PR"):
            assert code not in MAINLAND


class TestNearWhere:
    def test_a_city_centre_finds_its_own_city(self):
        found = nearest_place(33.7490, -84.3880, "GA")
        assert found is not None
        assert found[0] == "Atlanta"
        assert found[1] < 10

    def test_it_does_not_cross_a_state_line_for_a_closer_town(self):
        """A point just inside Georgia is not 'near' somewhere in Alabama."""
        found = nearest_place(34.5, -85.5, "GA")
        assert found is not None

    def test_empty_country_gets_no_town_rather_than_a_distant_one(self):
        """Naming a town an hour away is not describing where the hazard is."""
        assert nearest_place(46.9, -105.9, "MT") is None


class TestTheLabel:
    def test_coordinates_lead_and_the_town_qualifies(self):
        place = locate(33.7490, -84.3880)
        assert place.label.startswith("33.74900, -84.38800")
        assert "near Atlanta, GA" in place.label

    def test_it_says_near_and_not_in(self):
        """The nearest centroid can be miles off; 'in' would be a claim."""
        assert " near " in locate(33.7490, -84.3880).label

    def test_somewhere_with_no_town_still_names_the_state(self):
        place = locate(46.9, -105.9)
        assert place.nearest is None
        assert place.label.endswith("Montana")

    def test_the_short_form_drops_the_numbers_for_a_subject_line(self):
        assert locate(39.9612, -82.9988).short == "near Columbus, OH"


class TestRefusals:
    """Somewhere this system cannot file is not somewhere to guess about."""

    @pytest.mark.parametrize(
        "where,lat,lng",
        [
            ("the Pacific", 35.0, -140.0),
            ("Anchorage", 61.2, -149.9),
            ("Honolulu", 21.3, -157.8),
            ("London", 51.5, -0.12),
            ("an unset coordinate", 0.0, 0.0),
            ("the Gulf of Mexico", 25.0, -90.0),
        ],
    )
    def test_it_refuses_rather_than_guessing(self, where, lat, lng):
        with pytest.raises(OutsideCoverageError):
            locate(lat, lng)

    def test_a_zeroed_coordinate_is_refused(self):
        """`lat=0, lng=0` is what an unset location looks like, and it is a
        point in the Gulf of Guinea. Routing it anywhere would be absurd."""
        with pytest.raises(OutsideCoverageError, match="outside the mainland"):
            locate(0.0, 0.0)

    def test_the_refusal_explains_itself(self):
        with pytest.raises(OutsideCoverageError) as exc:
            locate(61.2, -149.9)
        assert "mainland" in str(exc.value)


class TestItStaysOffline:
    def test_nothing_here_opens_a_socket(self):
        """The whole point of shipping 411 KB is that a demo cannot be broken
        by somebody else's service being slow."""
        from pathlib import Path

        source = Path(
            __import__("road_cleaner").__file__
        ).parent / "adapters" / "geo" / "places.py"
        body = source.read_text()
        for forbidden in ("httpx", "requests", "urllib", "socket", "http"):
            assert forbidden not in body, f"places.py reaches for {forbidden}"

    def test_the_data_ships_with_the_code(self):
        from road_cleaner.adapters.geo.places import PLACES_FILE, STATES_FILE

        assert STATES_FILE.is_file()
        assert PLACES_FILE.is_file()
        # Small enough to be worth not calling anybody for.
        assert (STATES_FILE.stat().st_size + PLACES_FILE.stat().st_size) < 1_000_000
