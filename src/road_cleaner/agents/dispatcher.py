"""③ Dispatcher — works out whose problem it is, and tells them.

This is the part nobody else has built. Detection is a commodity; a dozen
products can put a dot on a map. What none of them do is take the next step:
identify the specific public body responsible for that specific stretch of road,
compose something a maintenance desk will actually act on, send it through the
channel that body accepts, and record what came back.

Order of operations:

1. **Resolve.** Rules first (`jurisdiction/registry.py`), a model only if they
   cannot decide, and if it still cannot decide, hold the case rather than guess.
2. **Compose.** Deterministic prose from `domain/narrative.py`, optionally
   polished by a model, with evidence frames attached.
3. **File.** Through whatever the agency accepts -- Open311, a web form, email --
   wrapped in dry run unless somebody has deliberately turned it off.
4. **Record.** Reference number, SLA deadline, and every step on the trail.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from road_cleaner.container import channel_for_agency
from road_cleaner.domain import narrative
from road_cleaner.domain.enums import CaseKind, Stage, Tone
from road_cleaner.domain.models import Case, Filing, TrailEvent
from road_cleaner.domain.sla import deadline_for, rationale_for
from road_cleaner.logging import get_logger, trace
from road_cleaner.ports.filing_channel import FilingError

log = get_logger(__name__)


class SyntheticCaseError(RuntimeError):
    """Someone tried to file a drill case with a real agency.

    Never expected in normal operation -- the drill composes its report and stops
    -- so this exists to make the boundary enforced rather than merely intended.
    """


class Dispatcher:
    def __init__(self, container) -> None:
        self.c = container
        self.settings = container.settings
        self.filed = 0
        self.unresolved = 0
        # One lock per case. The bus runs several workers, and a hazard that is
        # confirmed on two frames in quick succession produces two
        # `hazard.confirmed` events for the same case. Without this, both can
        # read the case as still WATCHING and both file -- which is the exact
        # duplicate-report behaviour this system is supposed to be incapable of.
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def handle_hazard(self, payload: dict) -> None:
        """Bus handler for `hazard.confirmed`."""
        case = await self.c.repository.get_case(payload["case_id"])
        if case is None or case.kind is not CaseKind.WATCHING:
            # Already filed, suppressed or closed. Filing again would be spam.
            return

        camera = await self.c.repository.get_camera(case.camera_id)
        detections = await self.c.repository.recent_detections(
            case.camera_id, case.opened_at, limit=20
        )
        detection = next(
            (d for d in detections if d.id == payload["detection_id"]),
            detections[0] if detections else None,
        )
        if camera is None or detection is None:
            return

        await self.file_case(case, camera, detection, tier=1)

    async def file_case(self, case: Case, camera, detection, *, tier: int = 1) -> Case | None:
        """Resolve jurisdiction, compose a report, and file it.

        Serialized per case and idempotent per tier: whatever happens upstream,
        one case gets at most one report per escalation tier.
        """
        async with self._locks[case.id]:
            # Re-read inside the lock. Another worker may have filed this case
            # while we were waiting for it.
            current = await self.c.repository.get_case(case.id)
            if current is not None:
                case = current

            existing = await self.c.repository.get_filings(case.id)
            if any(f.tier == tier for f in existing):
                log.debug(
                    "Already filed at this tier; not filing again",
                    extra={"case_id": case.id, "tier": tier},
                )
                return case

            return await self._file_locked(case, camera, detection, tier)

    async def _file_locked(self, case: Case, camera, detection, tier: int) -> Case | None:
        # --- 0. a drill is never filed ---
        #
        # The drill invents a location and generates its own footage. Sending
        # that to a real maintenance desk would be a fabricated report about a
        # road nobody looked at, which is the one failure this project cannot
        # afford. Raising rather than returning None is deliberate: a silent skip
        # here would look identical to "nothing to file" and could go unnoticed
        # for a long time.
        if case.synthetic:
            raise SyntheticCaseError(
                f"{case.id} is a drill case and can never be filed. Its location "
                "was invented and its footage generated."
            )

        # --- 1. whose road is it? ---
        verdict = await self.c.jurisdiction.resolve(camera, detection, self.c.reasoner)

        if not verdict.resolved:
            self.unresolved += 1
            await self._trail(
                case.id, Stage.RESOLVE,
                "Couldn't work out who owns this stretch, so I'm holding rather than "
                "sending it to the wrong desk. Flagged for a person.",
                Tone.WARN,
            )
            case.gate_reason = verdict.rationale
            await self.c.repository.save_case(case)
            return None

        agency = verdict.agency
        await self._trail(
            case.id, Stage.RESOLVE,
            f"Worked out whose road it is: {verdict.rationale}",
            Tone.KEY,
        )

        # --- 2. compose ---
        attachments = [f.blob_key for f in case.frame_refs if f.blob_key]

        body = narrative.report_body(
            detection,
            # The case's own location string, which is also what goes in the
            # form's `route` field. They were built from different sources and
            # could disagree inside a single submission.
            case.location,
            narrative.observed_at(case.opened_at),
            # The real count, not `or 1`. The floor meant a case with no stored
            # blobs still told the agency a frame was enclosed.
            attachment_count=len(attachments),
            tier=tier,
        )
        body = await self.c.reasoner.polish_report(body, detection, camera)
        subject = narrative.report_subject(detection, camera.road, tier=tier)

        filing = Filing(
            case_id=case.id,
            agency_id=agency.id,
            channel=agency.channel,
            tier=tier,
            filed_at=self.c.clock.now(),
            subject=subject,
            body=body,
            attachments=attachments,
            dry_run=self.settings.dry_run,
        )

        # --- 3. file ---
        channel = self._channel_for(agency)
        try:
            result = await channel.file(filing, case, agency)
        except FilingError as exc:
            await self._trail(
                case.id, Stage.REPORT,
                f"Tried to file with {agency.name} and couldn't: {exc}. Will retry.",
                Tone.WARN,
            )
            log.warning("Filing failed", extra={"case_id": case.id, "error": str(exc)})
            return None

        filing.reference = result.reference
        filing.response_raw = result.raw_response
        await self.c.repository.save_filing(filing)

        # --- 4. record ---
        case.agency_id = agency.id
        case.agency_name = agency.name
        case.channel = agency.channel
        case.reference = result.reference
        case.ref_label = agency.display_ref_label if tier == 1 else (
            f"{agency.display_ref_label} · {tier}{_ordinal_suffix(tier)} FILING"
        )
        case.kind = CaseKind.FILED
        case.escalation_tier = tier
        case.sla_deadline = deadline_for(
            case.hazard_type, filing.filed_at, agency.sla_overrides
        )
        case.sentence = narrative.sentence(detection, _gate_stub(case), camera, agency)
        case.updated_at = self.c.clock.now()
        await self.c.repository.save_case(case)

        sent = "Filed" if result.delivered else "Composed (dry run — not sent)"
        await self._trail(
            case.id, Stage.REPORT,
            f"{sent} with {agency.name} via {agency.channel.value}, "
            f"{len(attachments)} frame(s) attached. Reference {result.reference}. "
            f"{rationale_for(case.hazard_type)}",
            Tone.KEY,
        )
        trace(
            log, case.id, Stage.REPORT.value, "Report filed",
            agency=agency.name, reference=result.reference,
            dry_run=not result.delivered, tier=tier,
        )
        self.filed += 1
        return case

    def _channel_for(self, agency):
        """Use the agency's own channel, unless dry run is wrapping everything."""
        default = self.c.filing
        if self.settings.dry_run:
            # Keep the dry-run wrapper, but compose through the channel this
            # agency actually accepts -- otherwise the outbox would show the
            # wrong format for half the agencies.
            from pathlib import Path

            from road_cleaner.adapters.filing.dry_run import DryRunChannel

            return DryRunChannel(
                channel_for_agency(agency.channel, self.settings),
                Path(self.settings.filing_outbox),
            )
        return default

    async def _trail(
        self, case_id: str, stage: Stage, text: str, tone: Tone = Tone.ROUTINE
    ) -> None:
        await self.c.repository.append_trail(
            TrailEvent(case_id=case_id, at=self.c.clock.now(), stage=stage, text=text, tone=tone)
        )


def _gate_stub(case: Case):
    """A minimal GateResult so narrative.sentence can render a filed case."""
    from road_cleaner.domain.enums import GateDecision
    from road_cleaner.domain.models import GateResult

    return GateResult(
        decision=GateDecision.FILE,
        reason=case.gate_reason or "",
        mean_confidence=case.confidence,
        corroborating_ids=case.detection_ids[1:],
    )


def _ordinal_suffix(n: int) -> str:
    return {1: "ST", 2: "ND", 3: "RD"}.get(n, "TH")
