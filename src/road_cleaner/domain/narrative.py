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

from datetime import datetime

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
    HazardType.POTHOLE: "Pothole in a travel lane",
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
    HazardType.POTHOLE: "a cavity in the road surface itself",
}

STATE_POSSESSIVE: dict[str, str] = {
    State.GA: "Georgia",
    State.FL: "Florida",
    State.NC: "North Carolina",
    State.TN: "Tennessee",
    State.AL: "Alabama",
    State.SC: "South Carolina",
}

# Nothing here phrases a position any more.
#
# A camera pointed down a road cannot count the lanes to its left, so the lane
# number the model returned was a guess -- and it did not stay on screen. It went
# into case headlines and into the `Location:` line of reports addressed to a
# state DOT, which is a crew sent to the wrong part of the carriageway. The
# bounding box says where the hazard is, to the pixel, and says it honestly.
#
# `Detection.lane_position` still exists and still carries a coarse position for
# jurisdiction routing (see the `municipal-signal` rule). It is simply never
# narrated.


def hazard_title(detection: Detection) -> str:
    """The headline for a case.

    One title per hazard type, and no position appended. The append it replaces
    only knew how to strip `" in a travel lane"`, so every other hazard had a
    lane bolted onto a finished sentence: an animal detected in lane 2 was titled
    "Animal on the shoulder in lane 2", which is both wrong and self-contradictory.
    """
    return HAZARD_TITLES[detection.hazard_type]


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
    parts = [f"There's {observation} on {camera.road}."]

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


def observed_at(moment: datetime) -> str:
    """When the hazard was seen, phrased once.

    The three callers of `report_body` each formatted this themselves and each
    did it differently -- two appended "UTC" and one did not, and one of them was
    passing the wall clock at filing time rather than a moment anybody observed.
    A timestamp in a report to a road crew is a fact; it gets one rendering.
    """
    return moment.strftime("%a %b %-d, %-I:%M %p %Z").strip()


def report_body(
    detection: Detection,
    location: str,
    observed_at: str,
    *,
    attachment_count: int = 0,
    evidence_url: str | None = None,
    tier: int = 1,
) -> str:
    """The message that actually goes to the agency.

    Deliberately dry and factual -- the wry voice belongs on our dashboard, not
    in somebody's maintenance queue. States what was seen, where and when, then
    gets out of the way. Always says it was filed by a machine, and does not
    offer anything it cannot deliver.

    Four things this used to say and no longer does, all of them untrue:

    * **A camera id.** Fine for a fixed CCTV; meaningless for a phone on a
      windscreen, which is now where most of these come from.
    * **"Confirmed present at ..."**, which was the wall clock at the moment of
      filing rather than an observation. On a follow-up sent days later it
      asserted a confirmation that never happened.
    * **"N timestamped camera frames are attached"**, when nothing was attached.
      Two of the three channels post form fields and no files at all, and the
      email channel silently attaches nothing when the blob store is remote. The
      sentence now describes what is really enclosed, and links the evidence
      still when there is a link to give instead.
    * **"Reply to this thread and a person will see it."** Nobody is on the
      other end of it. A maintenance desk that took the invitation seriously
      would write back and hear nothing, which is a worse first contact than
      never having asked them to.

    `location` is passed in rather than rebuilt from a camera so that this line
    and the `route` field of the form carry the same string. They used to be
    built from different sources and disagree inside one submission.
    """
    opener = (
        "Reporting a road hazard seen from a vehicle dashcam."
        if tier == 1
        else "Following up on a previously reported road hazard that appears unresolved."
    )

    enclosure = []
    if attachment_count == 1:
        enclosure = ["A still from the footage is attached, with the hazard marked."]
    elif attachment_count > 1:
        enclosure = [f"{attachment_count} stills from the footage are attached."]
    if evidence_url:
        enclosure.append(f"The marked still is also here: {evidence_url}")

    return "\n".join(
        [
            opener,
            "",
            f"Location: {location}.",
            f"Observed: {observed_at}",
            "",
            detection.description.strip(),
            "",
            *([*enclosure, ""] if enclosure else []),
            # The machine disclosure stays; the promise that followed it does not.
            # "Reply to this thread and a person will see it" was an undertaking
            # nobody is on the other end of -- no inbox here is monitored, and a
            # maintenance desk that replied would be answered by silence. That is
            # worse than offering nothing, and it is the same class of untruth as
            # the attachments this report used to claim it carried.
            "Filed automatically by Road Cleaner.",
        ]
    )


def report_subject(detection: Detection, where: str, tier: int = 1) -> str:
    """`where` is a road name for a fixed camera, a place for a dropped pin.

    A road name takes "on" -- *debris on I-285*. A place already carries its own
    preposition, and gluing another in front produced *"debris on near Columbus,
    OH"*, which reads like a machine talking to itself.
    """
    prefix = "Road hazard" if tier == 1 else f"Follow-up ({_ordinal(tier)} notice) — road hazard"
    joined = where if where.startswith(("near ", "in ", "at ", "on ")) else f"on {where}"
    return f"{prefix}: {hazard_title(detection).lower()} {joined}"


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
