"""The vocabulary of the system.

Kept in one place because these strings cross every boundary: they are model
output, database columns, Pub/Sub payloads and CSS class names.
"""

from __future__ import annotations

from enum import StrEnum


class State(StrEnum):
    """States we watch. GA, FL and NC are live; the rest are architected."""

    GA = "GA"
    FL = "FL"
    NC = "NC"
    TN = "TN"
    AL = "AL"
    SC = "SC"


LAUNCH_STATES: tuple[State, ...] = (State.GA, State.FL, State.NC)

STATE_LABELS: dict[str, str] = {
    "GA": "Georgia",
    "FL": "Florida",
    "NC": "N. Carolina",
    "TN": "Tennessee",
    "AL": "Alabama",
    "SC": "S. Carolina",
}


class HazardType(StrEnum):
    DEBRIS = "debris"
    STALLED_VEHICLE = "stalled_vehicle"
    UNREPORTED_CLOSURE = "unreported_closure"
    FLOODING = "flooding"
    INFRASTRUCTURE_DAMAGE = "infrastructure_damage"
    ANIMAL = "animal"
    PEDESTRIAN_ON_HIGHWAY = "pedestrian_on_highway"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class CaseKind(StrEnum):
    """What state a case is in, from the reader's point of view.

    These are the five buckets the dashboard groups by, and they double as the
    lifecycle: a case is WATCHING until it either gets FILED or dropped, a filed
    case becomes ESCALATED if nobody comes, and ends CLEARED. SUPPRESSED means we
    saw it clearly and deliberately said nothing, because somebody already knew.
    """

    WATCHING = "watching"
    FILED = "filed"
    ESCALATED = "escalated"
    CLEARED = "cleared"
    SUPPRESSED = "suppressed"


CASE_KIND_LABELS: dict[str, str] = {
    "watching": "Watching",
    "filed": "Filed",
    "escalated": "Past due",
    "cleared": "Cleared",
    "suppressed": "Stood down",
}


class GateDecision(StrEnum):
    """What the confidence gate decided to do with a detection."""

    FILE = "file"
    WATCH = "watch"
    SUPPRESS = "suppress"
    DROP = "drop"


class Channel(StrEnum):
    """How a report physically reaches an agency."""

    OPEN311 = "open311"
    MAINTENANCE_FORM = "maintenance_form"
    EMAIL = "email"


CHANNEL_LABELS: dict[str, str] = {
    "open311": "Open311 service request",
    "maintenance_form": "Maintenance request form, filled automatically",
    "email": "Structured email to the district maintenance desk",
}


class AgencyLevel(StrEnum):
    STATE_DOT = "state_dot"
    COUNTY = "county"
    CITY = "city"
    TOLL_AUTHORITY = "toll_authority"


class Stage(StrEnum):
    """The seven stages of the loop, run per camera, continuously.

    Every trail entry is tagged with the stage that produced it, which is what
    lets the dashboard show the reasoning as a sequence rather than a pile.
    """

    WATCH = "watch"
    DETECT = "detect"
    CONFIRM = "confirm"
    RESOLVE = "resolve"
    REPORT = "report"
    CHECK_BACK = "check_back"
    PUSH = "push"


STAGE_LABELS: dict[str, str] = {
    "watch": "Watch",
    "detect": "Detect",
    "confirm": "Confirm",
    "resolve": "Resolve",
    "report": "Report",
    "check_back": "Check back",
    "push": "Push",
}


class Tone(StrEnum):
    """Visual weight of a trail entry. KEY is a milestone, WARN is a problem."""

    KEY = "key"
    WARN = "warn"
    ROUTINE = "routine"


class CameraTier(StrEnum):
    """How often a camera gets looked at."""

    BUSY = "busy"
    QUIET = "quiet"
    ACTIVE = "active"  # has an open case, so watch it closely


class EventTopic(StrEnum):
    """Logical topics on the bus. Map to Pub/Sub topics in cloud mode."""

    FRAME_CAPTURED = "frame.captured"
    HAZARD_CONFIRMED = "hazard.confirmed"
    CASE_FILED = "case.filed"
