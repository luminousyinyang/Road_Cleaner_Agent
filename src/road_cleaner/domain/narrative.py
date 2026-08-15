"""Turning a decision into something a person would actually read.

The dashboard is public. Its readers are drivers and, hopefully, the people at a
Traffic Management Center who might one day take a report from this thing
seriously. Neither group wants `{"hazard_type": "debris", "confidence": 0.94}`.

So every case carries plain-English copy, written in a consistent voice: first
person, plainspoken, no adjectives it can't justify from the frame. The rule is
that the copy never claims more certainty than the gate actually had.

This is deterministic by design. An LLM may *polish* this text later (see
`ports/reasoning.py`), but it never produces it from nothing -- if the model is
unavailable, or hallucinates, the case still reads correctly. Copy generation is
not a place to introduce a dependency on a paid API being up.
"""

from __future__ import annotations

from road_cleaner.domain.enums import GateDecision, HazardType, Severity, State
from road_cleaner.domain.models import Agency, Camera, Detection, GateResult

# How to refer to each hazard in a headline. Concrete nouns, no jargon.
HAZARD_TITLES: dict[HazardType, str] = {
    HazardType.DEBRIS: "Debris in a travel lane",
    HazardType.STALLED_VEHICLE: "Stalled car on the shoulder",
    HazardType.UNREPORTED_CLOSURE: "Lane closure with no warning posted",
    HazardType.FLOODING: "Standing water across the road",
    HazardType.INFRASTRUCTURE_DAMAGE: "Damaged roadside hardware",
    HazardType.ANIMAL: "Animal on the shoulder",
    HazardType.PEDESTRIAN_ON_HIGHWAY: "Someone walking on the highway",
}

# What the hazard looks like in a frame -- the observable, not the conclusion.
HAZARD_OBSERVATIONS: dict[HazardType, str] = {
    HazardType.DEBRIS: "a dark object sitting in the roadway",
    HazardType.STALLED_VEHICLE: "a vehicle stopped where nobody parks on purpose",
    HazardType.UNREPORTED_CLOSURE: "cones and a closed lane with no advance warning",
    HazardType.FLOODING: "water standing across the surface",
    HazardType.INFRASTRUCTURE_DAMAGE: "roadside hardware bent out of shape",
    HazardType.ANIMAL: "something animal-shaped off the travel lanes",
    HazardType.PEDESTRIAN_ON_HIGHWAY: "a person on foot where no one should be",
}

STATE_POSSESSIVE: dict[str, str] = {
    State.GA: "Georgia",
    State.FL: "Florida",
    State.NC: "North Carolina",
    State.TN: "Tennessee",
    State.AL: "Alabama",
    State.SC: "South Carolina",
}

LANE_PHRASES: dict[str, str] = {
    "lane_1": "in lane 1",
    "lane_2": "in lane 2",
    "lane_3": "in lane 3",
    "left_shoulder": "on the left shoulder",
    "right_shoulder": "on the right shoulder",
    "median": "in the median",
    "median_barrier": "against the median barrier",
    "intersection": "in the intersection",
    "all_lanes": "across every lane",
    "unknown": "on the roadway",
}


def lane_phrase(lane_position: str) -> str:
    return LANE_PHRASES.get(lane_position, f"at {lane_position.replace('_', ' ')}")


def hazard_title(detection: Detection) -> str:
    """The headline for a case."""
    base = HAZARD_TITLES[detection.hazard_type]
    lane = detection.lane_position
    if lane in ("lane_1", "lane_2", "lane_3"):
        return f"{base.replace(' in a travel lane', '')} {lane_phrase(lane)}".strip()
    return base


def location_text(camera: Camera) -> str:
    """Where this is, phrased the way a person would say it aloud."""
    road = camera.road
    if camera.direction:
        road = f"{road} {camera.direction}"
    return f"{road} at {camera.name}"


def explain(detection: Detection, gate: GateResult, camera: Camera) -> str:
    """The 'What I saw' paragraph.

    Describes the evidence and then the reasoning, in that order, and is honest
    about what was inconclusive.
    """
    observation = HAZARD_OBSERVATIONS[detection.hazard_type]
    where = lane_phrase(detection.lane_position)
    parts = [f"There's {observation} {where} on {camera.road}."]

    if detection.description:
        parts.append(detection.description.strip().rstrip(".") + ".")

    if detection.visual_evidence:
        evidence = _join(list(detection.visual_evidence))
        parts.append(f"What convinced me: {evidence}.")

    corroborating = len(gate.corroborating_ids)
    if corroborating:
        frames = corroborating + 1
        parts.append(
            f"It was still there when I looked again — {frames} separate frames agree, "
            f"averaging {gate.mean_confidence:.2f} confidence."
        )
    else:
        parts.append(
            "I've only seen it once so far, which isn't enough to act on. "
            "Cars stop on shoulders for ordinary reasons and shadows look like objects."
        )

    if gate.decision is GateDecision.SUPPRESS and gate.matched_event is not None:
        state = STATE_POSSESSIVE.get(camera.state, camera.state)
        parts.append(
            f"{state}'s own feed already had this posted, so there was nothing for me to add."
        )
    elif gate.decision is GateDecision.FILE:
        state = STATE_POSSESSIVE.get(camera.state, camera.state)
        parts.append(
            f"{state}'s incident feed had nothing within "
            f"{int(gate.matched_distance_m or 500)} metres. So this one was mine to report."
        )

    return " ".join(parts)


def sentence(
    detection: Detection,
    gate: GateResult,
    camera: Camera,
    agency: Agency | None = None,
) -> str:
    """The one-line summary in the road log. One sentence, no hedging words."""
    state = STATE_POSSESSIVE.get(camera.state, camera.state)

    if gate.decision is GateDecision.SUPPRESS:
        distance = int(gate.matched_distance_m or 0)
        return (
            f"Detected cleanly, then thrown away — {state} had it posted "
            f"{distance} metres up the road already."
        )

    if gate.decision is GateDecision.WATCH:
        return (
            f"One frame is a maybe, not a yes. Nobody hears about this until "
            f"a second look agrees. Holding at {detection.confidence:.2f}."
        )

    if gate.decision is GateDecision.DROP:
        return "Looked at it twice and couldn't convince myself. Dropped without filing."

    who = agency.name if agency else f"{state} DOT"
    evidence = "both frames attached" if gate.corroborating_ids else "the frame attached"
    if detection.severity in (Severity.HIGH, Severity.CRITICAL):
        return f"{state} hadn't posted anything, so it went to {who} with {evidence}."
    return (
        f"Nobody's emergency, which is exactly why it would have sat. "
        f"Filed with {who} as routine maintenance."
    )


def cleared_sentence(agency: Agency | None, duration_text: str) -> str:
    who = agency.name if agency else "the agency"
    return (
        f"Reported to {who}, gone {duration_text} later — closed with a before "
        f"and after pulled from the same camera."
    )


def escalated_sentence(duration_text: str, tier: int) -> str:
    if tier >= 3:
        return (
            f"Still there {duration_text} after the first report and past two full "
            f"deadlines. Stopped re-sending and flagged it for a person to read."
        )
    return (
        f"Still there {duration_text} after the first report, so it filed again "
        f"one tier up and flagged the case for a person to read."
    )


def report_body(
    detection: Detection,
    camera: Camera,
    first_seen: str,
    confirmed_at: str,
    frame_count: int,
    tier: int = 1,
) -> str:
    """The message that actually goes to the agency.

    Deliberately dry and factual -- the wry voice belongs on our dashboard, not
    in somebody's maintenance queue. States what was seen, where, when, and how
    confident we are, then gets out of the way. Always says it was filed by a
    machine and always offers a human reply path.
    """
    lane = lane_phrase(detection.lane_position)
    opener = (
        "Reporting a road hazard observed on a public traffic camera."
        if tier == 1
        else "Following up on a previously reported road hazard that appears unresolved."
    )
    attachment = (
        f"{frame_count} timestamped camera frames are attached."
        if frame_count > 1
        else "A timestamped camera frame is attached."
    )
    return "\n".join(
        [
            opener,
            "",
            f"Location: {location_text(camera)}, {lane}.",
            f"Camera: {camera.id}",
            f"First observed: {first_seen}",
            f"Confirmed present at: {confirmed_at}",
            "",
            detection.description.strip(),
            "",
            attachment,
            "",
            "Filed automatically by Road Cleaner. Reply to this thread and a person will see it.",
        ]
    )


def report_subject(detection: Detection, camera: Camera, tier: int = 1) -> str:
    prefix = "Road hazard" if tier == 1 else f"Follow-up ({_ordinal(tier)} notice) — road hazard"
    return f"{prefix}: {hazard_title(detection).lower()} on {camera.road}"


def gate_trail_text(gate: GateResult) -> str:
    """The trail line recording what the gate decided. The reason *is* the text."""
    return gate.reason


def _join(items: list[str]) -> str:
    """Oxford-comma join: 'a', 'a and b', 'a, b and c'."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _ordinal(n: int) -> str:
    return {1: "first", 2: "second", 3: "third"}.get(n, f"{n}th")
