"""The confidence gate is the part that decides whether a real agency gets
contacted, so every branch through it is tested explicitly."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from road_cleaner.domain.enums import GateDecision, HazardType, Severity
from road_cleaner.domain.gating import (
    GateConfig,
    evaluate,
    find_corroboration,
    hazard_families_overlap,
)
from road_cleaner.domain.models import Camera, Detection, OfficialEvent

T0 = datetime(2026, 8, 3, 14, 0, 0, tzinfo=UTC)


@pytest.fixture
def camera() -> Camera:
    return Camera(
        id="GDOT-CCTV-0447",
        state="GA",
        name="Camp Creek Pkwy",
        road="I-285",
        direction="westbound",
        lat=33.6407,
        lng=-84.4277,
        snapshot_url="https://example.invalid/cam.jpg",
    )


def make_detection(
    *,
    confidence: float = 0.94,
    severity: Severity = Severity.HIGH,
    hazard: HazardType = HazardType.DEBRIS,
    at: datetime = T0,
    det_id: str = "d1",
) -> Detection:
    return Detection(
        id=det_id,
        camera_id="GDOT-CCTV-0447",
        frame_id="f1",
        analyzed_at=at,
        hazard_type=hazard,
        lane_position="lane_2",
        severity=severity,
        confidence=confidence,
        description="Large dark object in the center travel lane.",
    )


class TestFloor:
    def test_below_floor_is_dropped(self, camera):
        result = evaluate(make_detection(confidence=0.30), [], [], camera)
        assert result.decision is GateDecision.DROP
        assert "floor" in result.reason.lower()

    def test_exactly_at_floor_is_not_dropped(self, camera):
        """0.55 is the floor, not the first value below it."""
        result = evaluate(make_detection(confidence=0.55), [], [], camera)
        assert result.decision is not GateDecision.DROP


class TestPersistence:
    def test_single_frame_only_watches(self, camera):
        """One frame is never enough to file, however confident."""
        result = evaluate(make_detection(confidence=0.99), [], [], camera)
        assert result.decision is GateDecision.WATCH
        assert "one frame" in result.reason.lower()

    def test_two_frames_far_enough_apart_can_file(self, camera):
        prior = make_detection(at=T0 - timedelta(seconds=103), det_id="d0")
        result = evaluate(make_detection(), [prior], [], camera)
        assert result.decision is GateDecision.FILE
        assert result.corroborating_ids == ["d0"]

    def test_frames_too_close_together_do_not_corroborate(self, camera):
        """Two looks 30s apart are one glance, not confirmation."""
        prior = make_detection(at=T0 - timedelta(seconds=30), det_id="d0")
        result = evaluate(make_detection(), [prior], [], camera)
        assert result.decision is GateDecision.WATCH

    def test_frames_too_far_apart_do_not_corroborate(self, camera):
        """An hour later is probably a different event entirely."""
        prior = make_detection(at=T0 - timedelta(hours=1), det_id="d0")
        result = evaluate(make_detection(), [prior], [], camera)
        assert result.decision is GateDecision.WATCH

    def test_different_hazard_type_does_not_corroborate(self, camera):
        prior = make_detection(
            hazard=HazardType.FLOODING, at=T0 - timedelta(seconds=120), det_id="d0"
        )
        result = evaluate(make_detection(), [prior], [], camera)
        assert result.decision is GateDecision.WATCH

    def test_low_confidence_prior_does_not_corroborate(self, camera):
        prior = make_detection(
            confidence=0.20, at=T0 - timedelta(seconds=120), det_id="d0"
        )
        result = evaluate(make_detection(), [prior], [], camera)
        assert result.decision is GateDecision.WATCH

    def test_detection_does_not_corroborate_itself(self, camera):
        det = make_detection()
        assert find_corroboration(det, [det], GateConfig()) == []


class TestDuplicateSuppression:
    def test_nearby_active_event_suppresses(self, camera):
        """The entire point is catching what they missed, not repeating them."""
        prior = make_detection(at=T0 - timedelta(seconds=120), det_id="d0")
        event = OfficialEvent(
            id="ga-1",
            state="GA",
            event_type="debris",
            lat=camera.lat + 0.001,  # ~110m away
            lng=camera.lng,
            description="Debris in roadway",
        )
        result = evaluate(make_detection(), [prior], [event], camera)
        assert result.decision is GateDecision.SUPPRESS
        assert result.matched_event is event
        assert result.matched_distance_m is not None
        assert result.matched_distance_m < 500

    def test_far_away_event_does_not_suppress(self, camera):
        prior = make_detection(at=T0 - timedelta(seconds=120), det_id="d0")
        event = OfficialEvent(
            id="ga-1",
            state="GA",
            event_type="debris",
            lat=camera.lat + 0.05,  # ~5.5km away
            lng=camera.lng,
            description="Debris in roadway",
        )
        result = evaluate(make_detection(), [prior], [event], camera)
        assert result.decision is GateDecision.FILE

    def test_inactive_event_does_not_suppress(self, camera):
        prior = make_detection(at=T0 - timedelta(seconds=120), det_id="d0")
        event = OfficialEvent(
            id="ga-1",
            state="GA",
            event_type="debris",
            lat=camera.lat,
            lng=camera.lng,
            description="Debris in roadway",
            active=False,
        )
        result = evaluate(make_detection(), [prior], [event], camera)
        assert result.decision is GateDecision.FILE

    def test_unrelated_event_type_does_not_suppress(self, camera):
        """A nearby flood warning says nothing about a tire in lane 2."""
        prior = make_detection(at=T0 - timedelta(seconds=120), det_id="d0")
        event = OfficialEvent(
            id="ga-1",
            state="GA",
            event_type="special event",
            lat=camera.lat,
            lng=camera.lng,
            description="Stadium traffic",
        )
        result = evaluate(make_detection(), [prior], [event], camera)
        assert result.decision is GateDecision.FILE

    def test_event_in_another_state_is_ignored(self, camera):
        prior = make_detection(at=T0 - timedelta(seconds=120), det_id="d0")
        event = OfficialEvent(
            id="fl-1",
            state="FL",
            event_type="debris",
            lat=camera.lat,
            lng=camera.lng,
            description="Debris in roadway",
        )
        result = evaluate(make_detection(), [prior], [event], camera)
        assert result.decision is GateDecision.FILE

    def test_closest_event_is_the_one_reported(self, camera):
        prior = make_detection(at=T0 - timedelta(seconds=120), det_id="d0")
        near = OfficialEvent(
            id="near", state="GA", event_type="debris",
            lat=camera.lat + 0.0005, lng=camera.lng, description="debris",
        )
        far = OfficialEvent(
            id="far", state="GA", event_type="debris",
            lat=camera.lat + 0.003, lng=camera.lng, description="debris",
        )
        result = evaluate(make_detection(), [prior], [far, near], camera)
        assert result.matched_event is not None
        assert result.matched_event.id == "near"


class TestSeverityMatrix:
    @pytest.mark.parametrize(
        ("severity", "confidence", "expected"),
        [
            # Critical clears a lower bar: being wrong the other way costs more.
            (Severity.CRITICAL, 0.62, GateDecision.FILE),
            (Severity.CRITICAL, 0.58, GateDecision.WATCH),
            (Severity.HIGH, 0.72, GateDecision.FILE),
            (Severity.HIGH, 0.62, GateDecision.WATCH),
            (Severity.MEDIUM, 0.82, GateDecision.FILE),
            (Severity.MEDIUM, 0.70, GateDecision.WATCH),
            # Low severity has to be near-certain before it's worth anyone's time.
            (Severity.LOW, 0.90, GateDecision.FILE),
            (Severity.LOW, 0.80, GateDecision.WATCH),
            (Severity.LOW, 0.60, GateDecision.DROP),
        ],
    )
    def test_matrix(self, camera, severity, confidence, expected):
        prior = make_detection(
            confidence=confidence, severity=severity,
            at=T0 - timedelta(seconds=120), det_id="d0",
        )
        det = make_detection(confidence=confidence, severity=severity)
        assert evaluate(det, [prior], [], camera).decision is expected

    def test_mean_confidence_is_averaged_across_frames(self, camera):
        """A weak second look drags the average down and holds the filing."""
        prior = make_detection(confidence=0.55, at=T0 - timedelta(seconds=120), det_id="d0")
        result = evaluate(make_detection(confidence=0.75), [prior], [], camera)
        assert result.mean_confidence == pytest.approx(0.65)
        assert result.decision is GateDecision.WATCH


class TestHazardFamilies:
    @pytest.mark.parametrize(
        ("hazard", "text", "expected"),
        [
            (HazardType.STALLED_VEHICLE, "Crash blocking right lane", True),
            (HazardType.STALLED_VEHICLE, "Disabled vehicle", True),
            (HazardType.FLOODING, "High water on SR-40", True),
            (HazardType.DEBRIS, "Object in roadway", True),
            (HazardType.INFRASTRUCTURE_DAMAGE, "Guardrail repair", True),
            (HazardType.DEBRIS, "Parade downtown", False),
            (HazardType.FLOODING, "Lane closure for paving", False),
        ],
    )
    def test_overlap(self, hazard, text, expected):
        assert hazard_families_overlap(hazard, text) is expected

    def test_matching_is_case_insensitive(self):
        assert hazard_families_overlap(HazardType.FLOODING, "HIGH WATER") is True


class TestConfigIsRespected:
    def test_raising_the_floor_drops_more(self, camera):
        cfg = GateConfig(min_confidence=0.95)
        result = evaluate(make_detection(confidence=0.94), [], [], camera, cfg)
        assert result.decision is GateDecision.DROP

    def test_widening_the_radius_suppresses_more(self, camera):
        prior = make_detection(at=T0 - timedelta(seconds=120), det_id="d0")
        event = OfficialEvent(
            id="ga-1", state="GA", event_type="debris",
            lat=camera.lat + 0.02, lng=camera.lng, description="debris",
        )
        assert evaluate(make_detection(), [prior], [event], camera).decision is GateDecision.FILE
        wide = GateConfig(duplicate_radius_meters=5000)
        assert (
            evaluate(make_detection(), [prior], [event], camera, wide).decision
            is GateDecision.SUPPRESS
        )


def test_disagreeing_frames_say_so_rather_than_claiming_there_was_one(camera):
    """Two looks that classified the hazard differently is not the same thing as
    having only looked once, and the reason text should not conflate them.

    Seen live: a drill staged a deer, the first vision call said `animal` and
    the second said `pedestrian_on_highway`. The gate correctly refused to
    corroborate them, then reported "Only one frame so far" -- which was false,
    and hid the more interesting fact that the two looks disagreed.
    """
    prior = make_detection(
        hazard=HazardType.ANIMAL, confidence=0.88,
        at=T0 - timedelta(seconds=240), det_id="d0",
    )
    result = evaluate(
        make_detection(hazard=HazardType.PEDESTRIAN_ON_HIGHWAY, confidence=0.92),
        [prior], [], camera,
    )

    assert result.decision is GateDecision.WATCH
    assert "Only one frame" not in result.reason
    assert "different things" in result.reason
    assert "animal" in result.reason
