"""The confidence gate.

Everything upstream of here is a guess. This is where a guess becomes a decision
to contact a real public agency, so it is deliberately conservative: it would
rather miss a hazard than send a maintenance crew after a shadow. A system that
cries wolf at a DOT is worse than no system at all, because it gets switched off.

Four checks, in order, each able to stop the process:

1. **Floor** -- below a minimum confidence, discard outright.
2. **Persistence** -- one frame is a maybe, not a yes. A hazard has to still be
   there on a later look, far enough apart to be a genuinely separate glance.
3. **Duplicate** -- if the state's own feed already has something at this spot,
   stand down. Catching what they missed is the entire point; repeating what
   they already said is just noise.
4. **Severity x confidence** -- the bar to file scales with how bad it would be
   to be wrong. A pedestrian on an interstate clears at lower confidence than a
   bit of debris, because the cost of missing it is not symmetric.

Pure functions, no I/O, no clock of its own -- every input is passed in. That is
what makes it exhaustively testable, and this is the part of the system that most
needs to be.
"""

from __future__ import annotations

from dataclasses import dataclass

from road_cleaner.domain.enums import GateDecision, HazardType, Severity
from road_cleaner.domain.geo import format_distance, haversine_meters
from road_cleaner.domain.models import Camera, Detection, GateResult, OfficialEvent

# How confident we need to be before filing, by how bad it would be to be wrong.
# Critical hazards clear a lower bar on purpose: the asymmetry is the point.
SEVERITY_THRESHOLDS: dict[Severity, float] = {
    Severity.CRITICAL: 0.60,
    Severity.HIGH: 0.70,
    Severity.MEDIUM: 0.80,
    Severity.LOW: 0.88,
}

# Hazard types that describe the same underlying situation. Used when comparing
# our detection against an official event: the DOT calling something a "crash"
# while we call it a "stalled vehicle" is still the same thing on the same road.
HAZARD_FAMILIES: dict[HazardType, frozenset[str]] = {
    HazardType.DEBRIS: frozenset({"debris", "obstruction", "object", "roadwork", "hazard"}),
    HazardType.STALLED_VEHICLE: frozenset(
        {"stalled", "disabled", "vehicle", "crash", "accident", "incident", "breakdown"}
    ),
    HazardType.UNREPORTED_CLOSURE: frozenset(
        {"closure", "closed", "lane", "roadwork", "construction", "restriction"}
    ),
    HazardType.FLOODING: frozenset({"flood", "water", "weather", "high water", "standing water"}),
    HazardType.INFRASTRUCTURE_DAMAGE: frozenset(
        {"damage", "guardrail", "bridge", "sign", "signal", "maintenance", "pothole"}
    ),
    HazardType.ANIMAL: frozenset({"animal", "livestock", "deer", "hazard"}),
    HazardType.PEDESTRIAN_ON_HIGHWAY: frozenset({"pedestrian", "person", "hazard"}),
}


@dataclass(frozen=True)
class GateConfig:
    """Thresholds, passed in rather than read from global config.

    Keeps the gate a pure function and lets tests vary one knob at a time.
    """

    min_confidence: float = 0.55
    corroboration_min_confidence: float = 0.50
    min_frame_gap_seconds: int = 90
    max_frame_gap_seconds: int = 1800
    duplicate_radius_meters: float = 500.0
    watch_margin: float = 0.15


def hazard_families_overlap(hazard: HazardType, event_text: str) -> bool:
    """Does an official event plausibly describe the same thing we detected?

    Deliberately generous. A false match here means we stay quiet about
    something real, which is the safe direction to be wrong in -- the hazard
    still gets watched, it just does not get reported twice.
    """
    haystack = event_text.lower()
    return any(term in haystack for term in HAZARD_FAMILIES.get(hazard, frozenset()))


def find_corroboration(
    detection: Detection, priors: list[Detection], config: GateConfig
) -> list[Detection]:
    """Earlier sightings that independently confirm this one.

    A prior counts only if it is the same hazard type, confident enough to be
    worth anything, and separated in time by enough that it is a genuinely
    different look -- but not so long ago that it is probably a different event.
    """
    out: list[Detection] = []
    for prior in priors:
        if prior.id == detection.id:
            continue
        if prior.hazard_type != detection.hazard_type:
            continue
        if prior.confidence < config.corroboration_min_confidence:
            continue
        gap = abs((detection.analyzed_at - prior.analyzed_at).total_seconds())
        if config.min_frame_gap_seconds <= gap <= config.max_frame_gap_seconds:
            out.append(prior)
    return out


def find_duplicate_event(
    camera: Camera, detection: Detection, events: list[OfficialEvent], config: GateConfig
) -> tuple[OfficialEvent, float] | None:
    """The nearest active official event that already covers this hazard."""
    best: tuple[OfficialEvent, float] | None = None
    for event in events:
        if not event.active:
            continue
        if event.state and event.state != camera.state:
            continue
        distance = haversine_meters(camera.lat, camera.lng, event.lat, event.lng)
        if distance > config.duplicate_radius_meters:
            continue
        text = f"{event.event_type} {event.description}"
        if not hazard_families_overlap(detection.hazard_type, text):
            continue
        if best is None or distance < best[1]:
            best = (event, distance)
    return best


def evaluate(
    detection: Detection,
    priors: list[Detection],
    events: list[OfficialEvent],
    camera: Camera,
    config: GateConfig | None = None,
) -> GateResult:
    """Decide what to do about a detection.

    Returns the decision *and* the reasoning behind it. The reason is not
    decoration -- it becomes a line on the case trail, and it is what a human
    reads when they want to know why a report was sent.
    """
    cfg = config or GateConfig()

    # 1. Floor. Not confident enough to be worth anyone's attention.
    if detection.confidence < cfg.min_confidence:
        return GateResult(
            decision=GateDecision.DROP,
            reason=(
                f"Confidence {detection.confidence:.2f} is under the "
                f"{cfg.min_confidence:.2f} floor. Not worth anybody's time."
            ),
            mean_confidence=detection.confidence,
        )

    # 2. Persistence. One frame is a maybe, not a yes.
    corroborating = find_corroboration(detection, priors, cfg)
    if not corroborating:
        return GateResult(
            decision=GateDecision.WATCH,
            reason=(
                "Only one frame so far. One frame is a maybe, not a yes — "
                f"waiting for a second look at least {cfg.min_frame_gap_seconds}s apart."
            ),
            mean_confidence=detection.confidence,
        )

    confidences = [detection.confidence, *(p.confidence for p in corroborating)]
    mean_confidence = sum(confidences) / len(confidences)
    corroborating_ids = [p.id for p in corroborating]

    # 3. Duplicate. They already know; we have nothing to add.
    duplicate = find_duplicate_event(camera, detection, events, cfg)
    if duplicate is not None:
        event, distance = duplicate
        return GateResult(
            decision=GateDecision.SUPPRESS,
            reason=(
                f"{camera.state} already has an active '{event.event_type}' event "
                f"{format_distance(distance)} away. They know about this one."
            ),
            mean_confidence=mean_confidence,
            corroborating_ids=corroborating_ids,
            matched_event=event,
            matched_distance_m=distance,
        )

    # 4. Severity x confidence.
    threshold = SEVERITY_THRESHOLDS[detection.severity]
    gap_text = (
        f"{len(corroborating) + 1} frames agree at {mean_confidence:.2f}, "
        f"against a {threshold:.2f} bar for {detection.severity.value} severity"
    )

    if mean_confidence >= threshold:
        return GateResult(
            decision=GateDecision.FILE,
            reason=f"{gap_text}. Confirmed — this one is ours to report.",
            mean_confidence=mean_confidence,
            corroborating_ids=corroborating_ids,
        )

    if mean_confidence >= threshold - cfg.watch_margin:
        return GateResult(
            decision=GateDecision.WATCH,
            reason=f"{gap_text}. Close, but not enough to send anyone. Keeping an eye on it.",
            mean_confidence=mean_confidence,
            corroborating_ids=corroborating_ids,
        )

    return GateResult(
        decision=GateDecision.DROP,
        reason=f"{gap_text}. Too far under the bar to act on.",
        mean_confidence=mean_confidence,
        corroborating_ids=corroborating_ids,
    )
