"""Open311 — the closest thing to a standard for civic service requests.

Several Florida and North Carolina municipalities run it, which is what makes
the Durham traffic-signal case fileable programmatically rather than by email.
Where a city supports it, this is the best channel available: structured,
acknowledged, and it returns a real service request id we can track.
"""

from __future__ import annotations

import httpx

from road_cleaner.adapters.filing.base import (
    BaseFilingChannel,
    ComposedReport,
    contact_email,
    contact_name,
    guard_live_send,
)
from road_cleaner.domain.enums import HazardType
from road_cleaner.domain.models import Agency, Case, Filing
from road_cleaner.ports.filing_channel import FilingError, FilingResult

# Open311 service codes vary per city; these are the common GeoReport v2
# conventions. A real deployment maps them per-agency during onboarding.
SERVICE_CODES: dict[HazardType, str] = {
    HazardType.DEBRIS: "road-debris",
    HazardType.FLOODING: "street-flooding",
    HazardType.INFRASTRUCTURE_DAMAGE: "traffic-signal",
    HazardType.UNREPORTED_CLOSURE: "traffic-hazard",
    HazardType.STALLED_VEHICLE: "abandoned-vehicle",
    HazardType.ANIMAL: "animal-control",
    HazardType.PEDESTRIAN_ON_HIGHWAY: "traffic-hazard",
}


class Open311Channel(BaseFilingChannel):
    def __init__(
        self,
        endpoint: str | None = None,
        api_key: str | None = None,
        *,
        from_address: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.from_address = from_address
        self.endpoint = endpoint
        self.api_key = api_key
        self._client = httpx.AsyncClient(timeout=timeout)

    @property
    def name(self) -> str:
        return "open311"

    def compose(self, filing: Filing, case: Case, agency: Agency) -> ComposedReport:
        endpoint = agency.endpoint or self.endpoint or ""
        payload = {
            "service_code": SERVICE_CODES.get(case.hazard_type, "traffic-hazard"),
            "description": filing.body,
            "address_string": case.location,
            "attribute[hazard_type]": case.hazard_type.value,
            "attribute[severity]": case.severity.value,
            # Worded exactly as `maintenance_form`'s `reportSource`. The two
            # channels described the same system to two cities in two different
            # ways -- one as a dashcam, one as camera monitoring -- and a report
            # should not change its account of where it came from depending on
            # which desk receives it.
            "attribute[source]": "dashcam footage, reviewed by an automated agent",
            "attribute[confidence]": f"{case.confidence:.2f}",
            "attribute[camera_id]": case.camera_id,
            # GeoReport v2 defines these and this channel sent none of them, so
            # every Open311 filing was anonymous while the report body promised
            # the city could reply to it.
            "email": contact_email(self.from_address),
            "first_name": contact_name(self.from_address),
        }
        if self.api_key:
            payload["api_key"] = self.api_key
        return ComposedReport(
            destination=f"{endpoint.rstrip('/')}/requests.json",
            subject=filing.subject,
            body=filing.body,
            payload=payload,
            attachments=filing.attachments,
        )

    async def transmit(self, report: ComposedReport, agency: Agency) -> FilingResult:
        guard_live_send(report.destination, "Open311")
        try:
            response = await self._client.post(report.destination, data=report.payload)
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            raise FilingError(f"Open311 submission to {agency.name} failed: {exc}") from exc
        except ValueError as exc:
            raise FilingError(f"Open311 returned non-JSON from {agency.name}: {exc}") from exc

        # GeoReport v2 returns a list with one object holding the request id.
        reference = None
        if isinstance(body, list) and body:
            reference = body[0].get("service_request_id") or body[0].get("token")
        return FilingResult(reference=reference, raw_response=str(body), delivered=True)

    async def close(self) -> None:
        await self._client.aclose()
