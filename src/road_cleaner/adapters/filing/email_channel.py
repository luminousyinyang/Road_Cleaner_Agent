"""Structured email, for the many agencies with no API at all.

Most DOT district maintenance desks take email and nothing else. That is not a
sophisticated integration, but it is the one that actually reaches a person, so
the message is built to be read by one: a clear subject line, the facts in a
fixed order, and evidence frames attached.

A real reply-to is set deliberately. An automated report nobody can respond to
is a worse citizen than no report at all.
"""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage
from pathlib import Path

from road_cleaner.adapters.filing.base import (
    BaseFilingChannel,
    ComposedReport,
    guard_live_send,
)
from road_cleaner.domain.models import Agency, Case, Filing
from road_cleaner.ports.filing_channel import FilingError, FilingResult


class EmailChannel(BaseFilingChannel):
    def __init__(
        self,
        *,
        host: str | None,
        port: int = 587,
        user: str | None = None,
        password: str | None = None,
        from_address: str = "road-cleaner@example.invalid",
        attachment_root: Path | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.from_address = from_address
        self.attachment_root = attachment_root

    @property
    def name(self) -> str:
        return "email"

    def compose(self, filing: Filing, case: Case, agency: Agency) -> ComposedReport:
        return ComposedReport(
            destination=agency.email or "",
            subject=filing.subject,
            body=filing.body,
            payload={
                "from": self.from_address,
                "to": agency.email,
                "subject": filing.subject,
                "case_id": case.id,
                "tier": filing.tier,
            },
            attachments=filing.attachments,
        )

    def _build_message(self, report: ComposedReport) -> EmailMessage:
        message = EmailMessage()
        message["From"] = self.from_address
        message["To"] = report.destination
        message["Subject"] = report.subject
        message["Reply-To"] = self.from_address
        # Marks this as machine-generated so the recipient's filters can see it
        # for what it is rather than guessing.
        message["Auto-Submitted"] = "auto-generated"
        message["X-Road-Cleaner"] = "automated-hazard-report"
        message.set_content(report.body)

        for key in report.attachments:
            if self.attachment_root is None:
                continue
            path = self.attachment_root / key
            if path.exists():
                message.add_attachment(
                    path.read_bytes(),
                    maintype="image",
                    subtype="jpeg",
                    filename=Path(key).name,
                )

        # Attachments the caller already holds, rather than ones to be read off
        # a disk relative to `attachment_root`. The dashcam needs this: its
        # stills go to a blob store that may be GCS, which has no filesystem
        # path for a root to be relative to.
        for filename, data in report.inline_attachments:
            message.add_attachment(
                data, maintype="image", subtype="jpeg", filename=filename
            )
        return message

    async def transmit(self, report: ComposedReport, agency: Agency) -> FilingResult:
        guard_live_send(report.destination, "email")
        if not self.host:
            raise FilingError("SMTP_HOST is not configured")
        if not report.destination:
            raise FilingError(f"{agency.name} has no email address on file")

        message = self._build_message(report)

        def send() -> None:
            with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
                smtp.starttls()
                if self.user and self.password:
                    smtp.login(self.user, self.password)
                smtp.send_message(message)

        try:
            await asyncio.to_thread(send)
        except (smtplib.SMTPException, OSError) as exc:
            raise FilingError(f"Email to {agency.name} failed: {exc}") from exc

        # Email gives back no reference, so the message id is the best handle
        # we have for correlating a reply later.
        return FilingResult(
            reference=None, raw_response=f"sent to {report.destination}", delivered=True
        )
