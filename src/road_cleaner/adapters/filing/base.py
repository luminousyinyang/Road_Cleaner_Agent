"""Shared shape for filing channels.

Splitting `compose` from `transmit` is the whole design here. Composing a report
is pure and safe; transmitting it is irreversible and lands on a real person's
desk. Keeping them apart means the dry-run wrapper can run the *real* compose
step and simply decline to transmit -- so what gets written to the outbox is
byte-identical to what would have gone out, rather than a separate "practice"
rendering that could quietly drift from the real one.
"""

from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from typing import Any

from road_cleaner.domain.models import Agency, Case, Filing
from road_cleaner.ports.filing_channel import FilingResult


class ComposedReport:
    """A report, fully rendered, not yet sent."""

    def __init__(
        self,
        *,
        destination: str,
        subject: str,
        body: str,
        payload: dict[str, Any] | None = None,
        attachments: list[str] | None = None,
    ) -> None:
        self.destination = destination
        self.subject = subject
        self.body = body
        self.payload = payload or {}
        self.attachments = attachments or []


class BaseFilingChannel(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def compose(self, filing: Filing, case: Case, agency: Agency) -> ComposedReport:
        """Render the report. Must have no side effects."""

    @abstractmethod
    async def transmit(self, report: ComposedReport, agency: Agency) -> FilingResult:
        """Actually send it. This is the irreversible part."""

    async def file(self, filing: Filing, case: Case, agency: Agency) -> FilingResult:
        return await self.transmit(self.compose(filing, case, agency), agency)

    async def close(self) -> None:
        return None


def synthesize_reference(agency: Agency, case: Case) -> str:
    """Build a plausible reference number in the agency's own format.

    Used in dry run, where no real agency hands one back. Deterministic on the
    case id so a reference stays stable across restarts, and so a demo re-run
    produces the same numbers.

    'TMC-#####' -> 'TMC-88213'
    """
    digest = hashlib.sha256(case.id.encode()).hexdigest()
    digits = re.sub(r"\D", "", digest) or "0"

    def fill(match: re.Match[str]) -> str:
        width = len(match.group(0))
        # Avoid a leading zero so the number looks like a real ticket.
        chunk = digits[:width].ljust(width, "7")
        return chunk if chunk[0] != "0" else "8" + chunk[1:]

    return re.sub(r"#+", fill, agency.ref_format)
