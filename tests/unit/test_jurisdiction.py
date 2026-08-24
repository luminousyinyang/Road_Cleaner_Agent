"""Jurisdiction rules.

The two cases that matter most are the ones a naive "look up the state DOT"
implementation gets wrong, and both are drawn from the design comps:

* a mattress on Florida's Turnpike belongs to the Turnpike Enterprise, not to
  the FDOT district the road runs through
* a rotated signal head on US-70 belongs to Durham, not to NCDOT, even though
  the route number says state highway
"""

from __future__ import annotations

from pathlib import Path

import pytest

from road_cleaner.domain.enums import AgencyLevel, HazardType, Severity
from road_cleaner.domain.models import Camera, Detection
from road_cleaner.jurisdiction.registry import JurisdictionRegistry

SEEDS = Path(__file__).resolve().parents[2] / "seeds" / "agencies.yaml"


@pytest.fixture(scope="module")
def registry() -> JurisdictionRegistry:
    return JurisdictionRegistry.load(SEEDS)


def camera(**kwargs) -> Camera:
    defaults = dict(
        id="CAM-1", state="GA", name="Somewhere", road="I-285",
        lat=33.6, lng=-84.4, snapshot_url="fixture://CAM-1",
    )
    return Camera(**{**defaults, **kwargs})


def detection(
    hazard: HazardType = HazardType.DEBRIS, lane: str = "lane_2"
) -> Detection:
    return Detection(
        camera_id="CAM-1", frame_id="F-1", hazard_type=hazard, lane_position=lane,
        severity=Severity.HIGH, confidence=0.9, description="",
    )


class TestRegistryLoading:
    def test_loads_agencies_and_rules(self, registry):
        assert len(registry.all_agencies()) > 10
        assert registry.rules

    def test_every_agency_has_a_reachable_channel(self, registry):
        """An agency with no endpoint and no email cannot be filed with at all."""
        for agency in registry.all_agencies():
            assert agency.endpoint or agency.email, f"{agency.id} has no destination"

    def test_candidates_are_scoped_to_the_state(self, registry):
        candidates = registry.candidates_for(camera(state="NC"))
        assert candidates
        assert all(a.state == "NC" for a in candidates)


class TestTollAuthority:
    def test_turnpike_beats_the_fdot_district(self, registry):
        """The most commonly-missed jurisdiction call in Florida."""
        cam = camera(
            state="FL", road="Florida's Turnpike", county="Orange",
            owner_agency_id="fl-dot-d5",  # even if the camera claims otherwise
        )
        result = registry.resolve_by_rules(cam, detection())
        assert result.resolved
        assert result.agency.id == "fl-turnpike"
        assert result.agency.level is AgencyLevel.TOLL_AUTHORITY

    def test_turnpike_rule_does_not_capture_ordinary_interstates(self, registry):
        cam = camera(state="FL", road="I-95", county="Broward", owner_agency_id="fl-dot-d4")
        result = registry.resolve_by_rules(cam, detection())
        assert result.agency.id == "fl-dot-d4"

    def test_turnpike_sla_is_shorter_than_the_state_default(self, registry):
        """They run their own crews and clear debris faster."""
        turnpike = registry.agencies["fl-turnpike"]
        assert turnpike.sla_overrides["debris"] < 24


class TestMunicipalSignals:
    def test_durham_signal_goes_to_the_city_not_the_state(self, registry):
        cam = camera(
            state="NC", road="US-70", county="Durham", owner_agency_id="nc-dot-d5"
        )
        result = registry.resolve_by_rules(
            cam, detection(HazardType.INFRASTRUCTURE_DAMAGE, "intersection")
        )
        assert result.agency.id == "durham-public-works"
        assert result.agency.level is AgencyLevel.CITY

    def test_debris_on_the_same_road_still_goes_to_the_state(self, registry):
        """Only the signal is the city's. The pavement is not."""
        cam = camera(
            state="NC", road="US-70", county="Durham", owner_agency_id="nc-dot-d5"
        )
        result = registry.resolve_by_rules(cam, detection(HazardType.DEBRIS, "lane_1"))
        assert result.agency.id == "nc-dot-d5"

    def test_signal_damage_outside_a_known_city_falls_back_to_the_state(self, registry):
        cam = camera(
            state="NC", road="US-70", county="Edgecombe", owner_agency_id="nc-dot-d4"
        )
        result = registry.resolve_by_rules(
            cam, detection(HazardType.INFRASTRUCTURE_DAMAGE, "intersection")
        )
        assert result.agency.id == "nc-dot-d4"


class TestCameraOwner:
    def test_camera_owner_is_believed_when_present(self, registry):
        cam = camera(state="GA", road="I-75", county="Fulton", owner_agency_id="ga-dot-d7")
        result = registry.resolve_by_rules(cam, detection())
        assert result.agency.id == "ga-dot-d7"

    def test_unknown_owner_id_does_not_resolve_to_a_wrong_agency(self, registry):
        """A stale owner id must fail closed, not fall through to something else."""
        cam = camera(state="GA", road="Local Road", county="Fulton", owner_agency_id="nope")
        result = registry.resolve_by_rules(cam, detection())
        assert not result.resolved


class TestUnresolved:
    def test_no_matching_rule_leaves_it_unresolved(self, registry):
        cam = camera(state="GA", road="Private Drive", county="Nowhere")
        result = registry.resolve_by_rules(cam, detection())
        assert not result.resolved
        assert result.confidence == 0.0

    async def test_without_a_reasoner_it_holds_rather_than_guessing(self, registry):
        """Filing with the wrong agency is worse than not filing."""
        cam = camera(state="GA", road="Private Drive", county="Nowhere")
        result = await registry.resolve(cam, detection(), reasoner=None)
        assert not result.resolved

    async def test_reasoner_is_consulted_only_when_rules_fail(self, registry):
        from road_cleaner.adapters.reasoning.scripted_reasoner import ScriptedReasoner

        cam = camera(state="NC", road="Private Drive", county="Nowhere")
        result = await registry.resolve(cam, detection(), reasoner=ScriptedReasoner())
        assert result.resolved
        assert result.rule_id.startswith("reasoner:")
        # A fallback should not claim to be certain.
        assert result.confidence < 1.0

    async def test_rules_win_over_the_reasoner_when_they_match(self, registry):
        from road_cleaner.adapters.reasoning.scripted_reasoner import ScriptedReasoner

        cam = camera(state="FL", road="Florida's Turnpike", county="Orange")
        result = await registry.resolve(cam, detection(), reasoner=ScriptedReasoner())
        assert result.rule_id == "toll-facility"


class TestADroppedPinFindsSomebody:
    """The case the whole nationwide registry exists for.

    Every rule above the fallback selects `use_camera_owner`, and a location that
    arrived as two numbers has no camera and therefore no owner. Before the
    fallback, a hazard reported from a phone in Ohio resolved to nobody.
    """

    def _pin(self, state):
        return camera(state=state, road="an unnamed road", county=None)

    @pytest.mark.parametrize("state", ["OH", "TX", "MT", "CA", "NY", "GA", "FL", "NC"])
    async def test_a_pin_resolves_in_every_state(self, registry, state):
        result = await registry.resolve(self._pin(state), detection(), reasoner=None)
        assert result.resolved, f"a pin in {state} had nowhere to go"
        assert result.agency.state == state

    async def test_a_named_road_that_matched_nothing_is_still_held(self, registry):
        """The fallback is not a licence to route anything anywhere.

        A private drive is not state-maintained, and handing it to the state DOT
        on the grounds that it is in that state is exactly the misfiling the
        rules exist to prevent.
        """
        cam = camera(state="GA", road="Private Drive", county="Nowhere")
        result = await registry.resolve(cam, detection(), reasoner=None)
        assert not result.resolved

    async def test_the_reasoner_is_still_asked_first(self, registry):
        """A rule that matches everything, placed before the model, means the
        model is never consulted. The fallback runs after it, not instead."""
        from road_cleaner.adapters.reasoning.scripted_reasoner import ScriptedReasoner

        cam = camera(state="NC", road="Private Drive", county="Nowhere")
        result = await registry.resolve(cam, detection(), reasoner=ScriptedReasoner())
        assert result.rule_id.startswith("reasoner:")

    async def test_the_district_rules_still_win_for_a_real_camera(self, registry):
        """Adding a state-level entry must not steal traffic from the districts."""
        cam = camera(state="NC", road="I-40", county="Wake", owner_agency_id="nc-dot-d5")
        result = await registry.resolve(cam, detection(), reasoner=None)
        assert result.agency.id == "nc-dot-d5"
        assert result.rule_id != "state-dot-fallback"
