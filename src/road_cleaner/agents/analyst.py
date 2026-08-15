"""② Analyst — decides whether there is really something there.

Three stages, each cheaper than the one after it, so that the expensive question
is only ever asked about frames that have earned it:

1. **Prefilter.** A small model answers one question: is anything anomalous
   here? Most frames of a road are an empty road, and they die here.
2. **Vision.** Full structured analysis of whatever survived, producing a hazard
   type, position, severity and confidence.
3. **Gate.** The part that matters. Persistence across frames, a check against
   what the state already knows, and a severity-scaled confidence bar. See
   `domain/gating.py` -- it is deliberately conservative, and it is why this
   system can be trusted to contact a real agency.

Everything the gate decides gets written to the case trail, including the
decisions to stay quiet. "We saw this and said nothing, because Florida already
had it posted 210 metres up the road" is exactly the kind of reasoning that
should be visible rather than invisible.
"""

from __future__ import annotations

from datetime import timedelta

from road_cleaner.domain import narrative
from road_cleaner.domain.enums import CameraTier, CaseKind, EventTopic, GateDecision, Stage, Tone
from road_cleaner.domain.gating import evaluate
from road_cleaner.domain.lifecycle import (
    RECURRENCE_COOLDOWN_HOURS,
    correlation_key,
    should_open_case,
)
from road_cleaner.domain.models import Case, Detection, FrameRef, TrailEvent
from road_cleaner.logging import get_logger, trace

log = get_logger(__name__)


class Analyst:
    def __init__(self, container) -> None:
        self.c = container
        self.settings = container.settings
        self.analyzed = 0
        self.prefilter_kills = 0
        self.detections = 0

    async def handle_frame(self, payload: dict) -> None:
        """Bus handler for `frame.captured`."""
        camera = await self.c.repository.get_camera(payload["camera_id"])
        if camera is None:
            return

        frame = await self.c.repository.latest_frame(camera.id)
        if frame is None or frame.id != payload["frame_id"]:
            # A newer frame already arrived; analysing a stale one wastes money.
            return

        image = await self.c.blobs.get(payload["blob_key"])

        # --- stage 1: the cheap question ---
        if not await self.c.vision.prefilter(image, frame, camera):
            self.prefilter_kills += 1
            return

        # --- stage 2: the expensive question ---
        self.analyzed += 1
        detection = await self.c.vision.analyze(image, frame, camera)
        if detection is None:
            return

        self.detections += 1
        await self.c.repository.save_detection(detection)

        # --- stage 3: the decision ---
        await self._gate(detection, camera, frame)

    async def _gate(self, detection: Detection, camera, frame) -> None:
        now = self.c.clock.now()
        window = timedelta(seconds=self.settings.gate_max_frame_gap_seconds)
        priors = await self.c.repository.recent_detections(camera.id, now - window)
        events = await self.c.cameras.list_events(camera.state)

        result = evaluate(detection, priors, events, camera, self.c.gate_config)

        if not should_open_case(result):
            log.debug(
                "Detection did not earn a case",
                extra={"camera_id": camera.id, "reason": result.reason},
            )
            return

        case = await self._get_or_open_case(detection, camera, result, frame)

        trace(
            log, case.id, Stage.CONFIRM.value, result.reason,
            decision=result.decision.value,
            mean_confidence=round(result.mean_confidence, 2),
        )

        if result.decision is GateDecision.FILE:
            await self.c.bus.publish(
                EventTopic.HAZARD_CONFIRMED,
                {"case_id": case.id, "detection_id": detection.id, "camera_id": camera.id},
            )

    async def _get_or_open_case(self, detection: Detection, camera, result, frame) -> Case:
        """Find the case this belongs to, or start one.

        The same tire gets detected dozens of times. Without this, each sighting
        would become its own case and its own report -- which is the exact
        behaviour that would get this system blocked by every agency in the
        Southeast.
        """
        key = correlation_key(camera.id, detection.hazard_type)
        cooldown = self.c.clock.now() - timedelta(hours=RECURRENCE_COOLDOWN_HOURS)
        existing = await self.c.repository.find_recent_case(key, cooldown)

        if existing is not None:
            return await self._update_case(existing, detection, camera, result, frame)

        case_id = await self.c.repository.next_case_id(camera.state)
        now = self.c.clock.now()

        case = Case(
            id=case_id,
            camera_id=camera.id,
            state=camera.state,
            kind=(
                CaseKind.SUPPRESSED
                if result.decision is GateDecision.SUPPRESS
                else CaseKind.WATCHING
            ),
            hazard_type=detection.hazard_type,
            hazard_title=narrative.hazard_title(detection),
            location=narrative.location_text(camera),
            severity=detection.severity,
            confidence=result.mean_confidence or detection.confidence,
            opened_at=now,
            updated_at=now,
            gate_decision=result.decision,
            gate_reason=result.reason,
            sentence=narrative.sentence(detection, result, camera),
            explain=narrative.explain(detection, result, camera),
            detection_ids=[detection.id],
            raw_model_json=detection.raw_model_json,
            box=detection.box,
            box_label=detection.box_label,
            frame_refs=[
                FrameRef(
                    label="First sighting",
                    captured_at=frame.captured_at,
                    blob_key=frame.blob_key,
                    mark=True,
                )
            ],
        )
        if result.decision is GateDecision.SUPPRESS:
            case.ref_label = "NOTHING SENT"
            case.reference = "duplicate"
            case.closed_at = now

        await self.c.repository.save_case(case)
        await self.c.repository.set_correlation(case.id, key)

        await self._trail(
            case.id, Stage.DETECT,
            f"Spotted it. {detection.description} "
            f"Vision model called it {detection.hazard_type.value} at {detection.confidence:.2f}.",
            Tone.KEY,
        )

        # A camera with an open case gets watched harder.
        if case.kind is not CaseKind.SUPPRESSED and camera.tier is not CameraTier.ACTIVE:
            camera.tier = CameraTier.ACTIVE
            await self.c.repository.upsert_camera(camera)

        return case

    async def _update_case(self, case: Case, detection: Detection, camera, result, frame) -> Case:
        """Fold a new sighting into an existing case."""
        case.detection_ids = [*case.detection_ids, detection.id][-20:]
        case.confidence = result.mean_confidence or case.confidence
        case.updated_at = self.c.clock.now()
        case.severity = detection.severity

        # Only the gate's reasoning is refreshed while a case is still watching;
        # once filed, the original decision is the record.
        if case.kind is CaseKind.WATCHING:
            case.gate_decision = result.decision
            case.gate_reason = result.reason
            case.sentence = narrative.sentence(detection, result, camera)
            case.explain = narrative.explain(detection, result, camera)
            case.raw_model_json = detection.raw_model_json
            case.box = detection.box
            case.box_label = detection.box_label

        if len(case.frame_refs) < 2 and result.corroborating_ids:
            case.frame_refs = [
                *case.frame_refs,
                FrameRef(
                    label="Confirmation",
                    captured_at=frame.captured_at,
                    blob_key=frame.blob_key,
                ),
            ]

        if result.decision is GateDecision.SUPPRESS and case.kind is CaseKind.WATCHING:
            case.kind = CaseKind.SUPPRESSED
            case.ref_label = "NOTHING SENT"
            case.reference = "duplicate"
            case.closed_at = self.c.clock.now()
            await self._trail(case.id, Stage.CONFIRM, result.reason, Tone.WARN)

        await self.c.repository.save_case(case)
        return case

    async def _trail(
        self, case_id: str, stage: Stage, text: str, tone: Tone = Tone.ROUTINE
    ) -> None:
        await self.c.repository.append_trail(
            TrailEvent(
                case_id=case_id, at=self.c.clock.now(), stage=stage, text=text, tone=tone
            )
        )
