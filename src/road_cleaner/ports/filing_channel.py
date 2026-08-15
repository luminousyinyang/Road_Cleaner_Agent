"""How a report physically reaches an agency.

This is the only port that touches the outside world in a way that cannot be
taken back. An HTTP GET to a camera is harmless; a maintenance request filed
with a county public works department is a real thing that a real person has to
read and act on.

So the shape here is deliberate: implementations *compose* the report and hand
back a `FilingResult`, and the decision about whether to actually transmit it
lives in one wrapper (`adapters/filing/dry_run.py`) rather than being scattered
through every channel. That way the dry-run artifact is byte-identical to what
would have been sent -- there is no separate "practice" code path that could
drift from the real one.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from road_cleaner.domain.models import Agency, Case, Filing


class FilingError(RuntimeError):
    """A report could not be delivered."""


class FilingResult:
    def __init__(
        self,
        reference: str | None,
        raw_response: str = "",
        delivered: bool = False,
    ) -> None:
        self.reference = reference
        self.raw_response = raw_response
        # False in dry run: the report was composed and stored, not transmitted.
        self.delivered = delivered


@runtime_checkable
class FilingChannel(Protocol):
    @property
    def name(self) -> str: ...

    async def file(self, filing: Filing, case: Case, agency: Agency) -> FilingResult:
        """Send the report and return whatever reference the agency gave back."""
        ...

    async def close(self) -> None: ...
