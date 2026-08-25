"""The one path in this system that puts a real message in a real inbox.

Everything else composes and stops. That is the right default -- the registry
holds seventy-one agencies and two genuinely deliverable addresses, and an
automated message landing on a district maintenance desk is the single failure
here with a stranger on the other end of it. So `Drill` never calls
`transmit()`, `DryRunChannel` wraps the default channel, and `guard_live_send`
refuses on top of both.

But a system that has never once completed its last step has not been shown to
work, only described. So this module exists to complete it exactly once, at a
recipient named in advance:

* It runs `Drill`, which is the real pipeline -- real Veo footage, two separate
  real vision calls, the real confidence gate, the real jurisdiction rules and
  the real report text. Nothing about the detection is special-cased.
* Then it sends that report by SMTP, with the marked evidence still genuinely
  attached, to an address that appears in `LIVE_FILING_ALLOWLIST`.

**The recipient is the only thing that changes.** The report is not softened and
the jurisdiction lookup is not skipped: the drill still works out which agency
owns that stretch, and the message says so in as many words. What it does not do
is send it to them. A demonstration that quietly mails a real DOT to prove a
point is not a demonstration, it is the accident this codebase is built to make
difficult.

Why the allowlist rather than `ALLOW_LIVE_FILING=true`: that switch is
all-or-nothing and "all" includes `contact@dot.ga.gov`. Naming one recipient is
a smaller hole than opening every one of them, and `guard_live_send` checks the
allowlist *before* the global switch precisely so this path needs neither
`DRY_RUN=false` nor `ALLOW_LIVE_FILING=true` to work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from road_cleaner.adapters.filing.email_channel import EmailChannel
from road_cleaner.domain.enums import AgencyLevel, Channel, GateDecision
from road_cleaner.domain.models import Agency, Case, Filing
from road_cleaner.logging import get_logger
from road_cleaner.pipeline.drill import Drill, DrillError, StageReport
from road_cleaner.ports.filing_channel import FilingError

log = get_logger(__name__)

# Appended to the drill's six. The drill has no seventh stage on purpose; this
# run does, and it is the whole reason the run exists.
SEND_STAGE = ("send", "Send")

# How many stills a report encloses, however many the run looked at.
MAX_ENCLOSED = 2


class DemoSendError(RuntimeError):
    """The demo could not complete. Never a bare failure -- always says why."""


@dataclass
class DemoSendResult:
    """A drill result, plus what happened to the message afterwards."""

    stages: list[StageReport]
    drill: dict[str, Any] = field(default_factory=dict)
    sent: bool = False
    sent_to: str | None = None
    # The agency the jurisdiction rules actually picked. Kept separate from
    # `sent_to` so the UI can never imply the two were the same.
    would_have_gone_to: str | None = None
    attachments: int = 0
    # What the confidence gate concluded. Set only when it declined, so the page
    # can say the run was held rather than that the transport failed -- those
    # are different outcomes and only one of them is a fault.
    gate_decision: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items() if k != "stages"}
        data["stages"] = [s.as_dict() for s in self.stages]
        return data


class DemoSend:
    """Runs the real pipeline, then actually transmits the result."""

    def __init__(self, container) -> None:
        self.c = container
        self.settings = container.settings

    def _check_recipient(self, to: str) -> str:
        """Refuse anything not named in advance, before any work is done.

        `guard_live_send` would catch this anyway at `transmit()`. Checking here
        as well means a misconfigured demo fails in a second with a readable
        message, rather than after a Veo render and four vision calls.
        """
        address = (to or "").strip()
        if not address:
            raise DemoSendError("No recipient configured for the demo send.")
        if address.lower() not in self.settings.live_filing_allowed:
            raise DemoSendError(
                f"{address} is not in LIVE_FILING_ALLOWLIST, so this system will "
                "not transmit to it. Add it to .env and restart. This is the "
                "check that keeps a demo from reaching a real maintenance desk."
            )
        if not self.settings.smtp_host:
            raise DemoSendError(
                "SMTP_HOST is not configured, so there is nothing to send with. "
                "See .env.example for the four SMTP_* settings this needs."
            )
        return address

    async def run(
        self,
        text: str,
        *,
        to: str,
        full: bool = False,
        pin=None,
        on_progress=None,
    ) -> DemoSendResult:
        """Drive the drill, then send its report. `on_progress` streams stages."""
        address = self._check_recipient(to)

        result = DemoSendResult(stages=[])
        send_stage = StageReport(*SEND_STAGE)

        async def relay(drill_result) -> None:
            # The drill owns its six stages; the send stage is appended so the
            # page shows seven from the first paint and nothing appears to
            # materialise late.
            result.stages = [*drill_result.stages, send_stage]
            result.drill = drill_result.as_dict()
            result.would_have_gone_to = drill_result.agency
            if on_progress:
                await on_progress(result)

        try:
            outcome = await Drill(self.c).run(
                text, full=full, pin=pin, on_progress=relay,
                # The footage is the one part worth keeping between runs. The
                # demonstration shows the same scenario over and over, and a
                # fresh Veo render per click buys nothing but a minute of
                # waiting and a rate limit in front of whoever is watching.
                # Everything downstream -- the vision calls, the gate, the
                # jurisdiction lookup, the report -- still runs in full.
                reuse_clip=True,
            )
        except DrillError as exc:
            raise DemoSendError(f"The pipeline stopped before composing: {exc}") from exc

        result.stages = [*outcome.stages, send_stage]
        result.drill = outcome.as_dict()
        result.would_have_gone_to = outcome.agency

        if not outcome.report_body:
            send_stage.state = "blocked"
            send_stage.detail = "Nothing was composed, so there is nothing to send"
            result.error = send_stage.detail
            if on_progress:
                await on_progress(result)
            return result

        # The gate decides, here as everywhere else.
        #
        # The drill composes a report whatever the gate concluded, because
        # showing the draft it *would* have sent is the point of a drill. This
        # is not a drill: it transmits. A `watch` verdict means two looks
        # disagreed and the system is not confident enough to report -- and the
        # first live run produced exactly that ("pothole this time, debris
        # before") and mailed it regardless, which is this demo claiming to run
        # the real gate while overriding it. A demonstration that files what the
        # gate refused is demonstrating something the product does not do.
        if outcome.gate_decision != GateDecision.FILE.value:
            send_stage.state = "blocked"
            send_stage.detail = (
                f"Gate said {outcome.gate_decision} — not sent. "
                f"{outcome.gate_reason or ''}".strip()
            )
            result.error = send_stage.detail
            result.gate_decision = outcome.gate_decision
            if on_progress:
                await on_progress(result)
            return result

        send_stage.state = "running"
        if on_progress:
            await on_progress(result)

        try:
            sent_to, attached = await self._transmit(outcome, address)
        except (FilingError, DemoSendError) as exc:
            send_stage.state = "failed"
            send_stage.detail = str(exc)
            result.error = str(exc)
            log.warning("Demo send failed", extra={"error": str(exc)})
        else:
            result.sent, result.sent_to, result.attachments = True, sent_to, attached
            send_stage.state = "done"
            enclosure = (
                f"{attached} still{'s' if attached != 1 else ''} attached"
                if attached
                else "no stills attached"
            )
            send_stage.detail = f"Delivered to {sent_to} — {enclosure}"

        if on_progress:
            await on_progress(result)
        return result

    async def _transmit(self, outcome, address: str) -> tuple[str, int]:
        """Compose through the real email channel and actually send it."""
        # The frames the drill staged. `frame_urls` are what the page renders;
        # the channel needs keys relative to the media store, which is what is
        # left once the `/media/` prefix the route adds is taken back off.
        # Boxed stills where the run produced them, raw frames otherwise. A
        # maintenance desk opening a photograph of an ordinary-looking road
        # should not have to work out which part of it we meant; the rectangle
        # is what makes the picture evidence rather than scenery. The fallback
        # matters because boxing is deliberately non-fatal upstream -- a report
        # with unmarked stills beats a report with none.
        source = outcome.evidence_urls or outcome.frame_urls or []
        keys = [url.removeprefix("/media/") for url in source if url.startswith("/media/")]
        # Files first, then the cap -- in that order. Capping first picked the
        # first and last key and only then asked whether they existed, so a
        # single missing still silently cost the report an attachment while the
        # frames between them sat on disk unused.
        root = Path(self.settings.media_local_path)
        keys = [k for k in keys if (root / k).is_file()]
        # Two, not however many stills the run happened to look at. A report is
        # read by somebody deciding whether to send a crew, and five near
        # identical frames of the same approach is not five times the evidence --
        # it is the same evidence and a slower download. First sighting, and the
        # confirmation that it was still there.
        if len(keys) > MAX_ENCLOSED:
            keys = [keys[0], keys[-1]]

        agency = Agency(
            id="road-cleaner-demo",
            name="Road Cleaner demonstration inbox",
            level=AgencyLevel.STATE_DOT,
            state=str(outcome.spec.get("state") or "--"),
            channel=Channel.EMAIL,
            email=address,
        )
        case = Case(
            id=outcome.case_id or "DEMO",
            camera_id="DEMO",
            state=str(outcome.spec.get("state") or "--"),
            hazard_type=outcome.spec.get("hazard_type") or "pothole",
            hazard_title=outcome.report_subject or "Road hazard",
            location=str(outcome.spec.get("place") or ""),
        )
        filing = Filing(
            case_id=case.id,
            agency_id=agency.id,
            channel=Channel.EMAIL,
            tier=1,
            subject=outcome.report_subject or "Road hazard",
            # Exactly what the pipeline composed, with nothing appended. The
            # message is the report and the stills it was written about.
            body=outcome.report_body,
            attachments=keys,
            dry_run=False,
        )

        # Its own channel instance rather than the container's: that one is
        # rooted at the evidence store, and a drill's frames live in the media
        # store. Pointed at the wrong root the message sends with the report
        # intact and every attachment silently missing.
        channel = EmailChannel(
            host=self.settings.smtp_host,
            port=self.settings.smtp_port,
            user=self.settings.smtp_user,
            password=self.settings.smtp_password,
            from_address=self.settings.filing_from_address,
            attachment_root=Path(self.settings.media_local_path),
        )
        composed = channel.compose(filing, case, agency)
        await channel.transmit(composed, agency)

        # `keys` was filtered to existing files above, so this is what the
        # message really carries rather than what it was asked to carry.
        return address, len(keys)
