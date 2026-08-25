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
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from road_cleaner.domain.models import Agency, Case, Filing
from road_cleaner.ports.filing_channel import FilingError, FilingResult


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
        inline_attachments: list[tuple[str, bytes]] | None = None,
    ) -> None:
        self.destination = destination
        self.subject = subject
        self.body = body
        self.payload = payload or {}
        # Keys, resolved against a channel's `attachment_root` on the way out.
        # Fine when the bytes are on the same disk as the process; pointed at
        # the wrong root the message sends with every attachment silently
        # missing, which is a failure mode worth knowing about.
        self.attachments = attachments or []
        # `(filename, bytes)` instead, for callers holding the image rather than
        # a path to it. The dashcam is the case that needs this: its stills live
        # in a blob store that may be GCS, where there is no filesystem path for
        # a root to be relative to.
        self.inline_attachments = inline_attachments or []


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


# The address the project ships with. It is an RFC 2606 reserved TLD, chosen so
# that a misconfigured deployment cannot mail anybody -- which is right for a
# `From:` header and wrong for a contact field somebody is meant to reply to.
UNSET_ADDRESS = "road-cleaner@example.invalid"

# What goes in a contact field when nobody has configured a real address.
# Deliberately a blank to fill rather than a plausible-looking fake: a form
# arriving at a DOT with an unreachable contact address is worse than one that
# visibly needs a name typed into it.
CONTACT_EMAIL_PLACEHOLDER = "<your email>"
CONTACT_NAME_PLACEHOLDER = "<Your name>"


def _configured(address: str | None) -> bool:
    """Whether somebody has set a real, routable reply address."""
    return bool(address) and address != UNSET_ADDRESS and not address.endswith(".invalid")


def contact_email(address: str | None) -> str:
    return address if _configured(address) else CONTACT_EMAIL_PLACEHOLDER


def contact_name(address: str | None) -> str:
    """A name for the contact field, derived from the address when there is one.

    `road-cleaner@example.invalid` used to be paired with the literal string
    "Road Cleaner (automated)". Both were hardcoded, neither could be replied to,
    and a maintenance desk reading them had no person to reach.
    """
    if not _configured(address):
        return CONTACT_NAME_PLACEHOLDER
    local = address.split("@", 1)[0]
    return local.replace(".", " ").replace("-", " ").replace("_", " ").title()


def guard_live_send(destination: str, channel: str) -> None:
    """Refuse to transmit unless somebody has said so twice.

    `seeds/agencies.yaml` holds the DOTs' real public reporting forms, so the
    dashboard can show where a report would actually go. The cost of that is that
    `DRY_RUN=false` alone would be enough to start POSTing to a government intake
    form, and one environment variable is too thin a wall for that.

    So this is the second switch. Every `transmit` calls it first -- checked here
    rather than at the call site, because a guard a caller can forget is not a
    guard. Composing is unaffected: rendering what would be sent has never been
    the dangerous half.
    """
    from road_cleaner.config import get_settings

    settings = get_settings()
    # Narrowest first: one address, for the duration of one send, put there by
    # code that verified it belongs to the person asking. See `allow_destination`.
    if destination and destination.strip().lower() in _permitted.get():
        return
    # Named explicitly, one address at a time. Checked before the global switch
    # so the demo path needs neither DRY_RUN=false nor ALLOW_LIVE_FILING=true --
    # which is the point: proving the last step is real should not also arm the
    # seventy-one agencies nobody meant to write to.
    if destination and destination.strip().lower() in settings.live_filing_allowed:
        return
    if settings.allow_live_filing:
        return
    raise FilingError(
        f"Refusing to transmit to {destination or 'an agency'} over {channel}. "
        "Real agency endpoints are configured, so sending needs ALLOW_LIVE_FILING=true "
        "as well as DRY_RUN=false, or the address named in LIVE_FILING_ALLOWLIST. "
        "Composing the report is unaffected."
    )


# The third way past `guard_live_send`, and the narrowest. LIVE_FILING_ALLOWLIST
# is static configuration and ALLOW_LIVE_FILING arms all seventy-two agencies;
# neither can express "this one person's own address, for this one request".
#
# A ContextVar rather than a `permitted=` parameter on `transmit()` because the
# existing guard's entire design is that a caller cannot forget it -- it is
# checked inside `transmit`, not at the call site. Threading a new argument
# through every channel would reintroduce exactly the omission the guard exists
# to make impossible. Async-task-local, so two concurrent requests cannot see
# each other's grant.
_permitted: ContextVar[frozenset[str]] = ContextVar("permitted_destinations", default=frozenset())


@contextmanager
def allow_destination(address: str) -> Iterator[None]:
    """Permit one address for the duration of this block, and no longer.

    The only legitimate argument is an address a *verified* identity provider
    vouched for -- in practice `AuthUser.mailable`, which is a Google-verified
    address off a signature-checked ID token. Never a string from a request
    body: that is a caller choosing who this application mails, which is the
    thing the guard exists to prevent.

    Deliberately does nothing for agency addresses. Mailing a real DOT stays
    behind LIVE_FILING_ALLOWLIST or ALLOW_LIVE_FILING, so no feature flag
    elsewhere can open that door by itself.
    """
    normalised = (address or "").strip().lower()
    if not normalised:
        raise ValueError("allow_destination needs an address.")
    token = _permitted.set(_permitted.get() | {normalised})
    try:
        yield
    finally:
        _permitted.reset(token)


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
