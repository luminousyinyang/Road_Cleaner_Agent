"""SLA maths decides when we push an agency again, so off-by-one errors here
turn into either nagging or silence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from road_cleaner.domain.enums import HazardType
from road_cleaner.domain.sla import (
    deadline_for,
    due_tier,
    elapsed_fraction,
    format_duration,
    format_remaining,
    is_overdue,
    needs_escalation,
    next_check_delay_seconds,
    sla_hours,
)

T0 = datetime(2026, 8, 3, 14, 0, 0, tzinfo=UTC)


class TestSlaHours:
    def test_debris_gets_a_day(self):
        assert sla_hours(HazardType.DEBRIS) == 24

    def test_pedestrian_is_an_hour(self):
        """The most dangerous hazard gets the shortest fuse."""
        assert sla_hours(HazardType.PEDESTRIAN_ON_HIGHWAY) == 1

    def test_infrastructure_is_two_weeks(self):
        assert sla_hours(HazardType.INFRASTRUCTURE_DAMAGE) == 24 * 14

    def test_agency_override_wins(self):
        assert sla_hours(HazardType.DEBRIS, {"debris": 6}) == 6

    def test_unrelated_override_is_ignored(self):
        assert sla_hours(HazardType.DEBRIS, {"flooding": 2}) == 24

    def test_every_hazard_has_an_sla(self):
        for hazard in HazardType:
            assert sla_hours(hazard) > 0


class TestDeadline:
    def test_deadline_is_filed_plus_window(self):
        assert deadline_for(HazardType.DEBRIS, T0) == T0 + timedelta(hours=24)

    def test_not_overdue_before_deadline(self):
        deadline = deadline_for(HazardType.DEBRIS, T0)
        assert is_overdue(deadline, T0 + timedelta(hours=23)) is False

    def test_overdue_after_deadline(self):
        deadline = deadline_for(HazardType.DEBRIS, T0)
        assert is_overdue(deadline, T0 + timedelta(hours=25)) is True

    def test_no_deadline_is_never_overdue(self):
        """A case we never filed can't be late."""
        assert is_overdue(None, T0) is False


class TestElapsedFraction:
    @pytest.mark.parametrize(
        ("hours", "expected"),
        [(0, 0.0), (6, 0.25), (12, 0.5), (24, 1.0), (48, 1.0)],
    )
    def test_fraction(self, hours, expected):
        deadline = T0 + timedelta(hours=24)
        assert elapsed_fraction(T0, deadline, T0 + timedelta(hours=hours)) == pytest.approx(
            expected
        )

    def test_clamped_at_zero_for_clock_skew(self):
        deadline = T0 + timedelta(hours=24)
        assert elapsed_fraction(T0, deadline, T0 - timedelta(hours=1)) == 0.0

    def test_zero_length_window_is_full(self):
        assert elapsed_fraction(T0, T0, T0) == 1.0


class TestEscalationTiers:
    @pytest.mark.parametrize(
        ("hours", "tier"),
        [(1, 1), (23, 1), (24, 2), (30, 2), (48, 3), (100, 3)],
    )
    def test_due_tier(self, hours, tier):
        deadline = T0 + timedelta(hours=24)
        assert due_tier(T0, deadline, T0 + timedelta(hours=hours)) == tier

    def test_needs_escalation_when_tier_earned_exceeds_current(self):
        deadline = T0 + timedelta(hours=24)
        assert needs_escalation(T0, deadline, 1, T0 + timedelta(hours=25)) is True

    def test_no_escalation_while_inside_window(self):
        deadline = T0 + timedelta(hours=24)
        assert needs_escalation(T0, deadline, 1, T0 + timedelta(hours=10)) is False

    def test_no_repeat_escalation_at_same_tier(self):
        """Already pushed to tier 2 -- don't push again until tier 3 is earned."""
        deadline = T0 + timedelta(hours=24)
        assert needs_escalation(T0, deadline, 2, T0 + timedelta(hours=30)) is False
        assert needs_escalation(T0, deadline, 2, T0 + timedelta(hours=49)) is True


class TestFormatting:
    @pytest.mark.parametrize(
        ("delta", "expected"),
        [
            (timedelta(hours=10, minutes=22), "10h 22m left"),
            (timedelta(minutes=45), "45m left"),
            (timedelta(days=12, hours=3), "12d 3h left"),
            # Stays in hours below three days: "26h overdue" carries the urgency
            # that "1d 2h overdue" loses. This is the wording the design uses.
            (timedelta(hours=-26), "26h overdue"),
            (timedelta(hours=-26, minutes=-30), "26h 30m overdue"),
        ],
    )
    def test_format_remaining(self, delta, expected):
        assert format_remaining(T0 + delta, T0) == expected

    def test_no_deadline_reads_as_no_filing(self):
        assert format_remaining(None, T0) == "no filing"

    @pytest.mark.parametrize(
        ("delta", "expected"),
        [
            (timedelta(hours=19), "19h"),
            (timedelta(hours=2, minutes=51), "2h 51m"),
            (timedelta(minutes=40), "40m"),
            (timedelta(days=1, hours=5), "1d 5h"),
        ],
    )
    def test_format_duration(self, delta, expected):
        assert format_duration(T0, T0 + delta) == expected


class TestNextCheckDelay:
    def test_backs_off_as_checks_accumulate(self):
        """No point re-checking a two-week guardrail repair every minute."""
        delays = [next_check_delay_seconds(1, n, 60) for n in range(5)]
        assert delays == [60, 120, 240, 480, 960]

    def test_backoff_is_capped(self):
        assert next_check_delay_seconds(1, 99, 60) == 60 * 16

    def test_escalated_cases_get_watched_harder(self):
        """The case going wrong is the one to watch more, not less."""
        normal = next_check_delay_seconds(1, 4, 60)
        escalated = next_check_delay_seconds(2, 4, 60)
        assert escalated < normal
