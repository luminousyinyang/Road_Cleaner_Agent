"""The safety catch.

Wraps any real filing channel, runs its genuine compose step, and then simply
does not transmit. The fully-rendered report is written to `data/outbox/` where
it can be read, reviewed and diffed against what would have gone out.

This is a wrapper rather than a separate channel on purpose. If dry run had its
own rendering path, that path would drift, and the first time anybody flipped
`DRY_RUN=false` they would discover that what actually gets sent is not what
they had been reviewing for weeks. Here, the only difference between a dry run
and a live filing is whether `transmit()` is called.

It stays on by default, in both local and cloud mode. Turning it off is a
deliberate, per-case act -- see `cli.py file --confirm`.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from road_cleaner.adapters.filing.base import (
    BaseFilingChannel,
    ComposedReport,
    synthesize_reference,
)
from road_cleaner.domain.models import Agency, Case, Filing
from road_cleaner.logging import get_logger
from road_cleaner.ports.filing_channel import FilingResult

log = get_logger(__name__)


class DryRunChannel:
    """Composes for real, transmits never."""

    def __init__(self, inner: BaseFilingChannel, outbox: Path) -> None:
        self.inner = inner
        self.outbox = Path(outbox)
        self.outbox.mkdir(parents=True, exist_ok=True)

    @property
    def name(self) -> str:
        return f"dry_run({self.inner.name})"

    async def file(self, filing: Filing, case: Case, agency: Agency) -> FilingResult:
        # The real compose step. Anything wrong with the report -- a broken
        # template, a missing field -- surfaces here, in dry run, rather than
        # the first time we go live.
        report = self.inner.compose(filing, case, agency)
        reference = synthesize_reference(agency, case)

        self._write(report, filing, case, agency, reference)

        log.info(
            "Report composed but NOT sent (dry run)",
            extra={
                "case_id": case.id,
                "agency": agency.name,
                "channel": self.inner.name,
                "destination": report.destination,
                "reference": reference,
            },
        )
        return FilingResult(
            reference=reference,
            raw_response=json.dumps(
                {
                    "dry_run": True,
                    "would_have_sent_to": report.destination,
                    "channel": self.inner.name,
                    "synthetic_reference": reference,
                }
            ),
            delivered=False,
        )

    def _write(
        self,
        report: ComposedReport,
        filing: Filing,
        case: Case,
        agency: Agency,
        reference: str,
    ) -> None:
        """Write the report to the outbox, readable by a person.

        Two files per filing: the human-readable message, and the machine
        payload that would have been POSTed. Both are what would really go.
        """
        stamp = filing.filed_at.strftime("%Y%m%dT%H%M%S")
        stem = f"{stamp}_{case.id}_tier{filing.tier}"

        header = "\n".join(
            [
                "=" * 78,
                "NOT SENT — DRY RUN",
                "This is exactly what would have been transmitted. No agency was",
                "contacted. Set DRY_RUN=false and file with --confirm to send.",
                "=" * 78,
                f"Case:        {case.id}",
                f"Agency:      {agency.name}",
                f"Channel:     {self.inner.name}",
                f"Destination: {report.destination}",
                f"Tier:        {filing.tier}",
                f"Composed at: {datetime.now().astimezone().isoformat()}",
                f"Would-be ref: {reference}",
                f"Attachments: {', '.join(report.attachments) or 'none'}",
                "=" * 78,
                "",
                f"Subject: {report.subject}",
                "",
                report.body,
                "",
            ]
        )
        (self.outbox / f"{stem}.txt").write_text(header)
        if report.payload:
            (self.outbox / f"{stem}.json").write_text(json.dumps(report.payload, indent=2))

    async def close(self) -> None:
        await self.inner.close()
