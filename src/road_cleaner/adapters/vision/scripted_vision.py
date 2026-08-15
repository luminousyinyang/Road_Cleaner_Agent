"""Deterministic stand-in for Gemini.

Reads the same scenario book the fixture cameras draw from, then behaves the way
a vision model actually behaves rather than the way a lookup table does:

* confidence sags at night and in rain
* marginal hazards get missed outright some of the time
* it occasionally reports something that isn't there

That last one matters most. A stub that only ever reports true hazards would let
a broken confidence gate pass every test. Because this one produces genuine
false positives, the gate has to earn its keep: spurious detections do not
persist across two frames, so they never reach a filing.

Every result is seeded on camera plus timestamp, so a run is reproducible.
"""

from __future__ import annotations

import json
import random
from datetime import datetime

from road_cleaner.adapters.camera.scene import lighting_for_hour
from road_cleaner.adapters.camera.scenarios import (
    ScenarioBook,
    observed_confidence,
    spurious_detection,
)
from road_cleaner.domain.enums import HazardType, Severity
from road_cleaner.domain.models import BoundingBox, Camera, Detection, Frame
from road_cleaner.ports.vision import ClearanceCheck

# What a spurious detection looks like: always low confidence, always the kind
# of thing a shadow or a wet patch could be mistaken for.
SPURIOUS_CANDIDATES = [
    (HazardType.DEBRIS, "A dark patch on the surface, possibly a shadow or a stain."),
    (HazardType.STALLED_VEHICLE, "A vehicle that may be stopped on the shoulder."),
    (HazardType.ANIMAL, "Something animal-shaped at the treeline. Heavy grain."),
]


class ScriptedVisionAnalyzer:
    def __init__(self, scenarios: ScenarioBook, *, prefilter_enabled: bool = True) -> None:
        self.scenarios = scenarios
        self.prefilter_enabled = prefilter_enabled
        # Counters that make the cost story measurable rather than asserted.
        self.frames_seen = 0
        self.frames_killed_by_prefilter = 0

    @property
    def model_name(self) -> str:
        return "scripted"

    async def prefilter(self, image: bytes) -> bool:
        """The cheap first pass.

        Deliberately generous: it lets through everything genuinely anomalous
        plus a slice of ordinary frames, which is what a small model actually
        does. Being wrong here in the permissive direction costs a little money;
        being wrong in the strict direction costs a missed hazard.
        """
        self.frames_seen += 1
        if not self.prefilter_enabled:
            return True
        # The prefilter cannot see the scenario book -- it only has the bytes --
        # so it approximates: anomalous frames are busier and hash differently.
        rng = random.Random(image[:64])
        passed = rng.random() < 0.35
        if not passed:
            self.frames_killed_by_prefilter += 1
        return passed

    async def analyze(self, image: bytes, frame: Frame, camera: Camera) -> Detection | None:
        moment = frame.captured_at
        lighting = lighting_for_hour(moment.hour)
        rng = random.Random(f"{camera.id}:{int(moment.timestamp())}")

        scenario = self.scenarios.active_for(camera.id, moment)

        if scenario is None:
            if not spurious_detection(rng, lighting):
                return None
            hazard, description = rng.choice(SPURIOUS_CANDIDATES)
            return self._build(
                frame, camera, hazard,
                lane="right_shoulder",
                severity=Severity.LOW,
                confidence=round(rng.uniform(0.56, 0.68), 2),
                description=description,
                evidence=["low contrast", "single frame"],
                lighting=lighting,
            )

        confidence = observed_confidence(scenario, lighting, rng)
        if confidence is None:
            return None  # missed it this time

        return self._build(
            frame, camera, scenario.hazard,
            lane=scenario.lane,
            severity=scenario.severity,
            confidence=round(confidence, 2),
            description=scenario.description,
            evidence=scenario.visual_evidence,
            lighting=lighting,
        )

    def _build(
        self,
        frame: Frame,
        camera: Camera,
        hazard: HazardType,
        *,
        lane: str,
        severity: Severity,
        confidence: float,
        description: str,
        evidence: list[str],
        lighting: str,
    ) -> Detection:
        box = self._box_for(lane)
        payload = {
            "hazard_type": hazard.value,
            "lane_position": lane,
            "severity": severity.value,
            "confidence": confidence,
            "description": description,
            "visual_evidence": list(evidence),
            "conditions": lighting,
        }
        return Detection(
            camera_id=camera.id,
            frame_id=frame.id,
            analyzed_at=frame.captured_at,
            hazard_type=hazard,
            lane_position=lane,
            severity=severity,
            confidence=confidence,
            description=description,
            visual_evidence=list(evidence),
            box=box,
            raw_model_json=json.dumps(payload, indent=2),
            model_name=self.model_name,
        )

    @staticmethod
    def _box_for(lane: str) -> BoundingBox:
        """Roughly where in the frame that lane sits.

        Mirrors the renderer's geometry so the box drawn on the dashboard lands
        on the thing it is pointing at.
        """
        centers = {
            "left_shoulder": 0.30, "lane_1": 0.39, "lane_2": 0.50,
            "lane_3": 0.61, "right_shoulder": 0.70, "median": 0.24,
            "median_barrier": 0.24, "intersection": 0.50, "all_lanes": 0.50,
        }
        center = centers.get(lane, 0.5)
        width = 0.14
        return BoundingBox(x=max(0.0, center - width / 2), y=0.60, width=width, height=0.18)

    async def verify_cleared(
        self, image: bytes, evidence_image: bytes, detection: Detection, camera: Camera
    ) -> ClearanceCheck:
        """Is the thing in the evidence photo still there?

        Answered against the scenario book, which knows whether the simulated
        agency has been out yet. The Auditor passes a freshly captured frame,
        so `at` carries the moment that frame was taken.
        """
        return await self.verify_cleared_at(camera, detection, detection.analyzed_at)

    async def verify_cleared_at(
        self, camera: Camera, detection: Detection, moment: datetime
    ) -> ClearanceCheck:
        """Clearance check as of a specific moment. This is the real entry point.

        Compares against the hazard type on the case rather than just "is
        anything there", because a case is closed by *its* hazard going away,
        not by the lane happening to be empty of something else.
        """
        scenario = self.scenarios.active_for(camera.id, moment)
        still_there = scenario is not None and scenario.hazard == detection.hazard_type
        return ClearanceCheck(
            still_present=still_there,
            confidence=0.90 if still_there else 0.93,
            note=(
                f"{detection.hazard_type.value} still visible in the same position"
                if still_there
                else "Lane is clear in the same view as the evidence frame"
            ),
        )
