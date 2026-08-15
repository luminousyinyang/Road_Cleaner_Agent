"""State DOT maintenance request forms.

GDOT and NCDOT both publish web forms for reporting a maintenance issue. There
is no API behind them, so this posts the form fields directly rather than
driving a browser -- fewer moving parts, and nothing to break when a page's
markup changes.

Field names differ per district and are part of onboarding a new agency; the
defaults here follow the common pattern. Getting them wrong means the form
silently rejects the submission, which is exactly the kind of failure that dry
run is meant to surface before it matters.
"""

from __future__ import annotations

import re

import httpx

from road_cleaner.adapters.filing.base import BaseFilingChannel, ComposedReport
from road_cleaner.domain.models import Agency, Case, Filing
from road_cleaner.ports.filing_channel import FilingError, FilingResult


class MaintenanceFormChannel(BaseFilingChannel):
    def __init__(self, from_address: str, *, timeout: float = 30.0) -> None:
        self.from_address = from_address
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    @property
    def name(self) -> str:
        return "maintenance_form"

    def compose(self, filing: Filing, case: Case, agency: Agency) -> ComposedReport:
        payload = {
            "requestType": "roadway_maintenance",
            "issueCategory": case.hazard_type.value,
            "severity": case.severity.value,
            "route": case.location,
            "county": "",
            "description": filing.body,
            "contactEmail": self.from_address,
            "contactName": "Road Cleaner (automated)",
            "reportSource": "public traffic camera monitoring",
        }
        return ComposedReport(
            destination=agency.endpoint or "",
            subject=filing.subject,
            body=filing.body,
            payload=payload,
            attachments=filing.attachments,
        )

    async def transmit(self, report: ComposedReport, agency: Agency) -> FilingResult:
        if not report.destination:
            raise FilingError(f"{agency.name} has no maintenance form endpoint on file")
        try:
            response = await self._client.post(report.destination, data=report.payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FilingError(f"Maintenance form for {agency.name} failed: {exc}") from exc

        return FilingResult(
            reference=self._extract_reference(response.text),
            raw_response=response.text[:2000],
            delivered=True,
        )

    @staticmethod
    def _extract_reference(html: str) -> str | None:
        """Scrape the confirmation number out of the response page.

        Crude, and deliberately tolerant of not finding one: a filing that
        succeeded without a readable reference is still a filing, and the case
        stays trackable by its own id.
        """
        for pattern in (
            r"(?:reference|confirmation|request|ticket)\s*(?:number|no\.?|#|id)?\s*[:\-]?\s*"
            r"([A-Z]{2,4}-[0-9]{4,8})",
            r"\b([A-Z]{2,4}-[0-9]{4,8})\b",
        ):
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                return match.group(1).upper()
        return None

    async def close(self) -> None:
        await self._client.aclose()
