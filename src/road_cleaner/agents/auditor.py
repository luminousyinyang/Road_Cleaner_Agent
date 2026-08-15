"""④ Auditor — keeps watching after the report goes out.

This is the agent the whole product is actually about. Filing a report and
walking away is what every existing tool does, and it is why potholes sit for
months: nobody is counting. The Auditor goes back to the same camera, compares
against the evidence photo, and either closes the case with a before-and-after
pair or pushes harder.

Escalation is bounded on purpose. Tier 2 files once more, one level up. Tier 3
stops filing entirely and flags the case for a human. An agent that politely
re-sends the same message forever is just spam with better manners, and the
fastest way to get this system blocked would be to let it nag indefinitely.

Re-checks decay: a case gets looked at less often as it ages, except when it is
overdue, in which case it gets looked at more.
"""

from __future__ import annotations

from datetime import timedelta

from road_cleaner.domain import narrative
from road_cleaner.domain.enums import CameraTier, CaseKind, Stage, Tone
from road_cleaner.domain.models import Case, FrameRef, TrailEvent
from road_cleaner.domain.sla import (
    MAX_ESCALATION_TIER,
    due_tier,
    format_duration,
    format_remaining,
    needs_escalation,
    next_check_delay_seconds,
)
from road_cleaner.logging import get_logger, trace
from road_cleaner.ports.camera_source import CameraFetchError

log = get_logger(__name__)


class Auditor:
    def __init__(self, container, dispatcher) -> None:
        self.c = container
        self.dispatcher = dispatcher
        self.settings = container.settings
        self.checks = 0
        self.cleared = 0
        self.escalated = 0
        self.flagged_for_human = 0

    async def tick(self) -> int:
        """Re-check every open case once. Returns how many were checked."""
        now = self.c.clock.now()
        checked = 0
        for case in await self.c.repository.open_cases():
            if case.kind is CaseKind.SUPPRESSED:
                continue
            # Respect the decaying schedule. Without this, every open case gets
            # re-examined on every pass forever, which costs vision calls and
            # buries the trail in identical entries.
            if case.next_check_at is not None and now < case.next_check_at:
                continue
            try:
                await self.check_case(case)
            except Exception:  # noqa: BLE001 - one bad case must not stall the rest
                log.exception("Re-check failed", extra={"case_id": case.id})
                continue
            checked += 1
        return checked

    async def check_case(self, case: Case) -> Case:
        """Pull a fresh frame and decide: closed, still there, or overdue?"""
        self.checks += 1
        camera = await self.c.repository.get_camera(case.camera_id)
        if camera is None:
            return case

        detections = await self.c.repository.recent_detections(
            case.camera_id, case.opened_at, limit=20
        )
        detection = next((d for d in detections if d.id in case.detection_ids), None)
        if detection is None:
            return case

        now = self.c.clock.now()
        try:
            still_present = await self._still_there(camera, detection, case, now)
        except CameraFetchError as exc:
            # Public cameras drop out constantly. A camera we cannot see is not
            # evidence that the road is clear, so the case stays exactly as it
            # is and we try again on the next pass.
            log.debug(
                "Camera unavailable during re-check",
                extra={"case_id": case.id, "camera_id": camera.id, "error": str(exc)},
            )
            return case

        case.checks_done += 1
        case.last_checked_at = now
        case.next_check_at = now + timedelta(
            seconds=next_check_delay_seconds(
                case.escalation_tier, case.checks_done, self.settings.auditor_interval_seconds
            )
        )

        if not still_present:
            return await self._close(case, camera, now)

        # Still there. Is it overdue?
        if case.sla_deadline is not None and case.kind is not CaseKind.WATCHING:
            filings = await self.c.repository.get_filings(case.id)
            if filings:
                first = min(filings, key=lambda f: f.filed_at)
                if needs_escalation(first.filed_at, case.sla_deadline, case.escalation_tier, now):
                    return await self._escalate(case, camera, detection, first, now)

        # Only narrate a routine re-check the first time. After that the case
        # page shows "last look" from `last_checked_at`, and the trail stays a
        # record of what changed rather than a log of every glance.
        if case.checks_done == 1:
            await self._trail(case.id, Stage.CHECK_BACK, self._still_there_text(case, now))

        case.updated_at = now
        await self.c.repository.save_case(case)
        return case

    def _still_there_text(self, case: Case, now) -> str:
        """Say what is actually true, including when we have stopped filing."""
        if case.escalation_tier >= MAX_ESCALATION_TIER:
            return (
                "Checked back. Still there. I've already filed twice and flagged this "
                "for a person, so I'm not sending anything else — just watching."
            )
        remaining = format_remaining(case.sla_deadline, now)
        if case.sla_deadline is None:
            return (
                "Checked back. Still there, and still under the bar to report. "
                "Nobody hears about it from me until that changes."
            )
        if remaining.endswith("overdue"):
            return f"Checked back. Still there, {remaining}. Following up."
        return f"Checked back. Still there. {remaining.capitalize()}, so no follow-up yet."

    async def _still_there(self, camera, detection, case: Case, now) -> bool:
        """Ask the vision model whether the hazard from the evidence photo remains."""
        verify_at = getattr(self.c.vision, "verify_cleared_at", None)
        if verify_at is not None:
            check = await verify_at(camera, detection, now)
        else:
            image = await self.c.cameras.fetch_snapshot(camera)
            evidence_key = next((f.blob_key for f in case.frame_refs if f.mark), None)
            evidence = await self.c.blobs.get(evidence_key) if evidence_key else image
            check = await self.c.vision.verify_cleared(image, evidence, detection, camera)
        return check.still_present

    async def _close(self, case: Case, camera, now) -> Case:
        """Close the case, keeping the frame that proves it.

        The before/after pair is the whole point: it is the difference between
        claiming a hazard was fixed and showing it. If the camera happens to be
        down at this exact moment we still close the case -- the clearance check
        already succeeded -- but the "after" frame is recorded as missing rather
        than faked.
        """
        blob_key: str | None = None
        try:
            image = await self.c.cameras.fetch_snapshot(camera)
            blob_key = f"{camera.state}/{camera.id}/{now.strftime('%Y%m%dT%H%M%S')}_cleared.jpg"
            await self.c.blobs.put(blob_key, image)
        except CameraFetchError:
            log.debug(
                "Could not capture a clearance frame; closing without one",
                extra={"case_id": case.id, "camera_id": camera.id},
            )

        case.frame_refs = [
            *case.frame_refs,
            FrameRef(label="Cleared", captured_at=now, blob_key=blob_key, clear=True),
        ]
        case.kind = CaseKind.CLEARED
        case.closed_at = now
        case.updated_at = now

        duration = format_duration(case.opened_at, now)
        agency = (
            await self.c.repository.get_agency(case.agency_id) if case.agency_id else None
        )
        if case.was_filed:
            case.sentence = narrative.cleared_sentence(agency, duration)
            case.ref_label = f"CLOSED IN {duration.upper()}"
        else:
            case.sentence = (
                f"Watched it for {duration} and it resolved on its own. "
                f"Never rose to anything worth reporting."
            )
            case.ref_label = "RESOLVED ON ITS OWN"

        await self.c.repository.save_case(case)
        await self._trail(
            case.id, Stage.CHECK_BACK,
            f"Checked back and the road is clear. Closing with a before and after "
            f"from the same camera. Took {duration}.",
            Tone.KEY,
        )

        # The camera can go back to its normal rhythm.
        if camera.tier is CameraTier.ACTIVE:
            camera.tier = CameraTier.BUSY if camera.tier else CameraTier.QUIET
            await self.c.repository.upsert_camera(camera)

        self.cleared += 1
        trace(log, case.id, Stage.CHECK_BACK.value, "Case cleared", duration=duration)
        return case

    async def _escalate(self, case: Case, camera, detection, first_filing, now) -> Case:
        """Push harder, or stop and hand it to a person."""
        tier = due_tier(first_filing.filed_at, case.sla_deadline, now)
        overdue = format_remaining(case.sla_deadline, now)
        elapsed = format_duration(first_filing.filed_at, now)

        if tier >= MAX_ESCALATION_TIER:
            # Stop filing. Repeating ourselves forever would be spam.
            case.escalation_tier = MAX_ESCALATION_TIER
            case.kind = CaseKind.ESCALATED
            case.updated_at = now
            case.sentence = narrative.escalated_sentence(elapsed, MAX_ESCALATION_TIER)
            await self.c.repository.save_case(case)
            await self._trail(
                case.id, Stage.PUSH,
                f"Still there {elapsed} after the first report and {overdue}. "
                f"I've filed twice; filing a third time would just be noise. "
                f"Stopping and flagging this for a person to pick up.",
                Tone.WARN,
            )
            self.flagged_for_human += 1
            trace(log, case.id, Stage.PUSH.value, "Flagged for human review", tier=tier)
            return case

        # Tier 2: file once more, one level up.
        await self._trail(
            case.id, Stage.PUSH,
            f"{overdue.capitalize()} and it hasn't moved. Filing again, one tier up.",
            Tone.WARN,
        )
        filed = await self.dispatcher.file_case(case, camera, detection, tier=tier)
        refreshed = filed or case
        refreshed.kind = CaseKind.ESCALATED
        refreshed.escalation_tier = tier
        refreshed.sentence = narrative.escalated_sentence(elapsed, tier)
        refreshed.updated_at = now
        await self.c.repository.save_case(refreshed)

        self.escalated += 1
        trace(log, case.id, Stage.PUSH.value, "Escalated", tier=tier)
        return refreshed

    async def _trail(
        self, case_id: str, stage: Stage, text: str, tone: Tone = Tone.ROUTINE
    ) -> None:
        await self.c.repository.append_trail(
            TrailEvent(case_id=case_id, at=self.c.clock.now(), stage=stage, text=text, tone=tone)
        )
