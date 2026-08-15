"""How long a hazard is allowed to sit there, and what happens when it doesn't move.

The SLA is the difference between reporting something and actually caring
whether it got fixed. Filing a report and walking away is what the existing
tools do. Here, filing starts a clock.

Escalation tiers:

* **1** -- the initial report. The clock starts.
* **2** -- the deadline passed and it's still there, so file again, one level up.
* **3** -- twice the deadline. Stop trying; flag it for a person. An agent that
  politely re-sends the same message forever is just spam with extra steps.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from road_cleaner.domain.enums import HazardType

# How long each kind of hazard should reasonably take to clear. These are our
# expectations, not any agency's published commitment -- they set when we look
# again and when we push, nothing more. An agency's own SLA overrides them
# (see Agency.sla_overrides).
DEFAULT_SLA_HOURS: dict[HazardType, float] = {
    HazardType.PEDESTRIAN_ON_HIGHWAY: 1,
    HazardType.STALLED_VEHICLE: 4,
    HazardType.FLOODING: 6,
    HazardType.ANIMAL: 8,
    HazardType.UNREPORTED_CLOSURE: 12,
    HazardType.DEBRIS: 24,
    HazardType.INFRASTRUCTURE_DAMAGE: 24 * 14,
}

# Plain-English justification, shown on the case page so the deadline doesn't
# look arbitrary.
SLA_RATIONALE: dict[HazardType, str] = {
    HazardType.PEDESTRIAN_ON_HIGHWAY: (
        "A person walking on an interstate is an emergency, not a maintenance ticket."
    ),
    HazardType.STALLED_VEHICLE: (
        "A vehicle stopped on a shoulder should be recovered the same shift."
    ),
    HazardType.FLOODING: "Standing water on a travel lane is supposed to be dealt with quickly.",
    HazardType.ANIMAL: "Animals on the shoulder get cleared within the day.",
    HazardType.UNREPORTED_CLOSURE: (
        "An unmarked lane closure needs signage up long before the day is out."
    ),
    HazardType.DEBRIS: "Debris in a travel lane is supposed to be picked up within a day.",
    HazardType.INFRASTRUCTURE_DAMAGE: (
        "Damaged roadside hardware is routine maintenance, not an emergency — two weeks."
    ),
}

ESCALATION_TIER_2_MULTIPLIER = 1.0  # at the deadline
ESCALATION_TIER_3_MULTIPLIER = 2.0  # at twice the deadline, hand it to a human
MAX_ESCALATION_TIER = 3


def sla_hours(hazard: HazardType, overrides: dict[str, int] | None = None) -> float:
    """Hours allowed for this hazard, honouring an agency's own policy."""
    if overrides and hazard.value in overrides:
        return float(overrides[hazard.value])
    return DEFAULT_SLA_HOURS[hazard]


def deadline_for(
    hazard: HazardType, filed_at: datetime, overrides: dict[str, int] | None = None
) -> datetime:
    return filed_at + timedelta(hours=sla_hours(hazard, overrides))


def rationale_for(hazard: HazardType) -> str:
    return SLA_RATIONALE[hazard]


def is_overdue(deadline: datetime | None, now: datetime) -> bool:
    return deadline is not None and now > deadline


def elapsed_fraction(filed_at: datetime, deadline: datetime, now: datetime) -> float:
    """How far through the allowed window we are, clamped to 0..1.

    Drives the progress bar on the case page.
    """
    total = (deadline - filed_at).total_seconds()
    if total <= 0:
        return 1.0
    return max(0.0, min(1.0, (now - filed_at).total_seconds() / total))


def due_tier(filed_at: datetime, deadline: datetime, now: datetime) -> int:
    """The escalation tier this case has earned by now.

    1 while inside the window, 2 once the deadline passes, 3 at double.
    """
    total = (deadline - filed_at).total_seconds()
    if total <= 0:
        return MAX_ESCALATION_TIER
    ratio = (now - filed_at).total_seconds() / total
    if ratio >= ESCALATION_TIER_3_MULTIPLIER:
        return 3
    if ratio >= ESCALATION_TIER_2_MULTIPLIER:
        return 2
    return 1


def needs_escalation(
    filed_at: datetime, deadline: datetime, current_tier: int, now: datetime
) -> bool:
    """Has this case earned a tier it hasn't been pushed to yet?"""
    return due_tier(filed_at, deadline, now) > current_tier


def format_remaining(deadline: datetime | None, now: datetime) -> str:
    """The countdown text, e.g. '10h 22m left' or '26h overdue'."""
    if deadline is None:
        return "no filing"
    delta = deadline - now
    overdue = delta.total_seconds() < 0
    seconds = int(abs(delta.total_seconds()))
    days = seconds // 86_400
    total_hours, rem = divmod(seconds, 3_600)
    minutes = rem // 60

    # Stay in hours up to three days. "26h overdue" reads as urgent in a way
    # that "1d 2h overdue" does not, and urgency is the whole message here.
    if days >= 3:
        text = f"{days}d {total_hours - days * 24}h"
    elif total_hours:
        text = f"{total_hours}h {minutes}m" if minutes else f"{total_hours}h"
    else:
        text = f"{minutes}m"
    return f"{text} overdue" if overdue else f"{text} left"


def format_duration(start: datetime, end: datetime) -> str:
    """How long a closed case took, e.g. '19h' or '2h 51m'."""
    seconds = max(0, int((end - start).total_seconds()))
    days, rem = divmod(seconds, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def next_check_delay_seconds(escalation_tier: int, checks_done: int, base_seconds: int) -> int:
    """When to look again.

    The interval decays as a case ages -- there is no point re-checking a
    two-week guardrail repair every minute -- but an escalated case gets watched
    harder, not less, because it is the one going wrong.
    """
    decay = min(2**checks_done, 16)
    if escalation_tier >= 2:
        decay = max(1, decay // 4)
    return base_seconds * decay
