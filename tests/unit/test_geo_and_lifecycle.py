from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from road_cleaner.domain.enums import CaseKind, Channel, GateDecision, HazardType
from road_cleaner.domain.geo import format_distance, haversine_meters, within_meters
from road_cleaner.domain.lifecycle import correlation_key, derive_kind, should_open_case
from road_cleaner.domain.models import Case, Filing, GateResult

T0 = datetime(2026, 8, 3, 14, 0, 0, tzinfo=UTC)


class TestHaversine:
    def test_same_point_is_zero(self):
        assert haversine_meters(33.64, -84.42, 33.64, -84.42) == 0

    def test_known_distance(self):
        """One degree of latitude is ~111km anywhere on Earth."""
        d = haversine_meters(33.0, -84.0, 34.0, -84.0)
        assert 110_000 < d < 112_000

    def test_symmetric(self):
        a = haversine_meters(33.64, -84.42, 33.65, -84.43)
        b = haversine_meters(33.65, -84.43, 33.64, -84.42)
        assert a == pytest.approx(b)

    def test_within_meters(self):
        assert within_meters(33.6407, -84.4277, 33.6417, -84.4277, 500) is True
        assert within_meters(33.6407, -84.4277, 33.7407, -84.4277, 500) is False

    @pytest.mark.parametrize(
        ("meters", "expected"),
        [(210, "210 metres"), (1400, "1.4 km"), (44, "40 metres")],
    )
    def test_format_distance(self, meters, expected):
        assert format_distance(meters) == expected


class TestCorrelationKey:
    def test_same_camera_and_hazard_correlate(self):
        """The same tire seen fifty times is one case, not fifty reports."""
        a = correlation_key("CAM-1", HazardType.DEBRIS)
        b = correlation_key("CAM-1", HazardType.DEBRIS)
        assert a == b

    def test_different_hazard_at_same_camera_is_a_separate_case(self):
        assert correlation_key("CAM-1", HazardType.DEBRIS) != correlation_key(
            "CAM-1", HazardType.FLOODING
        )

    def test_same_hazard_at_different_cameras_is_separate(self):
        assert correlation_key("CAM-1", HazardType.DEBRIS) != correlation_key(
            "CAM-2", HazardType.DEBRIS
        )


def make_case(**kwargs) -> Case:
    defaults = dict(
        id="GA-4471",
        camera_id="CAM-1",
        state="GA",
        hazard_type=HazardType.DEBRIS,
        hazard_title="Debris in lane 2",
        location="I-285 westbound",
        opened_at=T0,
    )
    return Case(**{**defaults, **kwargs})


def make_filing(at: datetime = T0, tier: int = 1) -> Filing:
    return Filing(
        case_id="GA-4471",
        agency_id="ga-dot-d7",
        channel=Channel.EMAIL,
        tier=tier,
        filed_at=at,
    )


class TestDeriveKind:
    def test_no_filing_is_watching(self):
        assert derive_kind(make_case(), [], T0) is CaseKind.WATCHING

    def test_filed_and_inside_window_is_filed(self):
        case = make_case(sla_deadline=T0 + timedelta(hours=24))
        assert derive_kind(case, [make_filing()], T0 + timedelta(hours=5)) is CaseKind.FILED

    def test_filed_and_past_deadline_is_escalated(self):
        case = make_case(sla_deadline=T0 + timedelta(hours=24))
        assert derive_kind(case, [make_filing()], T0 + timedelta(hours=26)) is CaseKind.ESCALATED

    def test_suppressed_never_enters_the_filing_lifecycle(self):
        case = make_case(gate_decision=GateDecision.SUPPRESS)
        assert derive_kind(case, [], T0) is CaseKind.SUPPRESSED

    def test_cleared_beats_everything(self):
        """A fixed road is fixed even if the paperwork ran late."""
        case = make_case(sla_deadline=T0 + timedelta(hours=24))
        kind = derive_kind(case, [make_filing()], T0 + timedelta(hours=99), cleared=True)
        assert kind is CaseKind.CLEARED

    def test_closed_at_implies_cleared(self):
        case = make_case(closed_at=T0 + timedelta(hours=19))
        assert derive_kind(case, [make_filing()], T0 + timedelta(hours=20)) is CaseKind.CLEARED

    def test_earliest_filing_starts_the_clock(self):
        """A tier-2 follow-up must not reset the deadline it breached."""
        case = make_case(sla_deadline=T0 + timedelta(hours=24))
        filings = [make_filing(at=T0), make_filing(at=T0 + timedelta(hours=25), tier=2)]
        assert derive_kind(case, filings, T0 + timedelta(hours=26)) is CaseKind.ESCALATED


class TestShouldOpenCase:
    @staticmethod
    def result(decision: GateDecision, corroborated: bool = True) -> GateResult:
        return GateResult(
            decision=decision,
            reason="",
            mean_confidence=0.9,
            corroborating_ids=["d0"] if corroborated else [],
        )

    def test_drop_leaves_no_case(self):
        assert should_open_case(self.result(GateDecision.DROP)) is False

    @pytest.mark.parametrize(
        "decision", [GateDecision.FILE, GateDecision.WATCH, GateDecision.SUPPRESS]
    )
    def test_corroborated_decisions_are_worth_a_record(self, decision):
        """'We saw this and said nothing' belongs on the record too."""
        assert should_open_case(self.result(decision)) is True

    @pytest.mark.parametrize(
        "decision", [GateDecision.FILE, GateDecision.WATCH, GateDecision.SUPPRESS]
    )
    def test_a_single_unconfirmed_frame_opens_nothing(self, decision):
        """Otherwise every false positive becomes a case and the log is noise."""
        assert should_open_case(self.result(decision, corroborated=False)) is False


class TestSuppressedVsCleared:
    def test_suppression_does_not_masquerade_as_a_fix(self):
        """Suppressing closes the case; it must not read as 'we got this fixed'."""
        case = make_case(
            gate_decision=GateDecision.SUPPRESS, closed_at=T0 + timedelta(minutes=1)
        )
        assert derive_kind(case, [], T0 + timedelta(hours=1)) is CaseKind.SUPPRESSED

    def test_an_explicit_clearance_still_wins(self):
        case = make_case(gate_decision=GateDecision.SUPPRESS)
        assert derive_kind(case, [], T0, cleared=True) is CaseKind.CLEARED
