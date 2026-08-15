"""What is actually happening on the simulated roads.

This is the ground truth for local runs. The fixture camera consults it to know
what to draw; the scripted vision analyzer consults it to know what it should
"see". Keeping both honest against one source is what makes the local pipeline a
real test of the logic rather than a puppet show -- the analyzer still has to
detect the hazard, the gate still has to confirm it across frames, and the
Auditor still has to notice when it goes away.

The analyzer does *not* get a free pass. It reads the truth and then degrades it
the way a real model would: confidence drops at night and in rain, marginal
hazards sometimes get missed entirely, and it occasionally reports something
that isn't there. Otherwise the confidence gate would never be exercised.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from road_cleaner.domain.enums import HazardType, Severity


@dataclass
class Scenario:
    """One hazard that appears on one camera for a stretch of time."""

    camera_id: str
    hazard: HazardType
    lane: str
    severity: Severity
    # What a perfect observer would report. The analyzer degrades this.
    truth_confidence: float
    description: str
    visual_evidence: list[str] = field(default_factory=list)
    # Minutes from the run's start instant.
    starts_at_minute: int = 0
    clears_at_minute: int | None = None
    # A matching entry in the state's own feed, which should make us stand down.
    official_event_type: str | None = None
    official_event_offset_m: float = 0.0
    official_event_description: str = ""
    note: str = ""

    def active_at(self, minutes: float) -> bool:
        if minutes < self.starts_at_minute:
            return False
        return self.clears_at_minute is None or minutes < self.clears_at_minute


@dataclass
class FakeAgencyBehaviour:
    """How quickly a simulated agency responds, so the Auditor has something to
    audit. Some cameras' hazards clear promptly; some never do, which is what
    produces the escalation path."""

    camera_id: str
    responds: bool = True


class ScenarioBook:
    def __init__(self, scenarios: list[Scenario], start: datetime) -> None:
        self.scenarios = scenarios
        self.start = start
        self._by_camera: dict[str, list[Scenario]] = {}
        for scenario in scenarios:
            self._by_camera.setdefault(scenario.camera_id, []).append(scenario)

    @classmethod
    def load(cls, path: Path, start: datetime) -> ScenarioBook:
        raw = json.loads(Path(path).read_text())
        scenarios = [
            Scenario(
                camera_id=item["camera_id"],
                hazard=HazardType(item["hazard"]),
                lane=item.get("lane", "lane_2"),
                severity=Severity(item.get("severity", "medium")),
                truth_confidence=float(item.get("truth_confidence", 0.9)),
                description=item.get("description", ""),
                visual_evidence=item.get("visual_evidence", []),
                starts_at_minute=int(item.get("starts_at_minute", 0)),
                clears_at_minute=item.get("clears_at_minute"),
                official_event_type=item.get("official_event_type"),
                official_event_offset_m=float(item.get("official_event_offset_m", 0)),
                official_event_description=item.get("official_event_description", ""),
                note=item.get("note", ""),
            )
            for item in raw["scenarios"]
        ]
        return cls(scenarios, start)

    def minutes_elapsed(self, now: datetime) -> float:
        return (now - self.start).total_seconds() / 60

    def active_for(self, camera_id: str, now: datetime) -> Scenario | None:
        """The hazard present at this camera right now, if any."""
        minutes = self.minutes_elapsed(now)
        for scenario in self._by_camera.get(camera_id, []):
            if scenario.active_at(minutes):
                return scenario
        return None

    def all_for(self, camera_id: str) -> list[Scenario]:
        return self._by_camera.get(camera_id, [])

    def official_events_at(self, now: datetime) -> list[Scenario]:
        """Scenarios that the state's own feed has also posted."""
        minutes = self.minutes_elapsed(now)
        return [
            s
            for s in self.scenarios
            if s.official_event_type and s.active_at(minutes)
        ]


def observed_confidence(
    scenario: Scenario, lighting: str, rng: random.Random
) -> float | None:
    """What the model actually reports, given conditions.

    Returns None when the hazard is missed entirely. Night and rain cost
    confidence, which is the whole reason the gate requires two frames -- a
    single grainy pre-dawn frame should not be enough to send anyone out.
    """
    penalty = {"day": 0.0, "dusk": 0.06, "rain": 0.10, "night": 0.18}.get(lighting, 0.0)
    jitter = rng.uniform(-0.04, 0.04)
    confidence = scenario.truth_confidence - penalty + jitter

    # A hazard the model is barely seeing is sometimes not seen at all.
    if confidence < 0.45 and rng.random() < 0.4:
        return None
    return max(0.05, min(0.99, confidence))


def spurious_detection(rng: random.Random, lighting: str) -> bool:
    """Occasionally the model sees something that isn't there.

    Rare in daylight, more common at night. These false positives should die at
    the gate -- they will not persist across two frames -- and that is precisely
    what we want the local pipeline to demonstrate.
    """
    rate = {"day": 0.01, "dusk": 0.02, "rain": 0.03, "night": 0.05}.get(lighting, 0.01)
    return rng.random() < rate


def build_history(scenario: Scenario, start: datetime) -> list[datetime]:
    """When this hazard was present, for seeding a demo with plausible history."""
    first = start + timedelta(minutes=scenario.starts_at_minute)
    if scenario.clears_at_minute is None:
        return [first]
    return [first, start + timedelta(minutes=scenario.clears_at_minute)]
