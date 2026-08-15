"""Case identity and case state.

Two questions that look small and cause most of the bugs if you get them wrong:

1. **Is this the same hazard I already know about?** Cameras get polled every
   couple of minutes. The same truck tire will be detected dozens of times. If
   each detection opens a case, we file the same report thirty times, which is
   precisely the spam behaviour the whole design is trying to avoid.

2. **What state is this case in?** `kind` is derived from facts -- whether it
   was filed, whether it cleared, how overdue it is -- rather than set by hand
   at a dozen call sites that can each get it subtly wrong.
"""

from __future__ import annotations

from datetime import datetime

from road_cleaner.domain.enums import CaseKind, GateDecision, HazardType
from road_cleaner.domain.models import Case, Filing
from road_cleaner.domain.sla import due_tier


def correlation_key(camera_id: str, hazard_type: HazardType) -> str:
    """Identity of an ongoing situation, as opposed to a single sighting.

    A camera plus a hazard type. Debris and flooding at the same camera are two
    separate problems and get two separate cases; the same debris seen fifty
    times is one case with fifty detections attached.

    This is coarse on purpose. Two pieces of debris in different lanes of the
    same camera collapse into one case, and that is the right trade: one report
    saying "debris on this stretch" is useful, two reports ninety seconds apart
    is a nuisance.
    """
    return f"{camera_id}:{hazard_type.value}"


def derive_kind(
    case: Case,
    filings: list[Filing],
    now: datetime,
    *,
    cleared: bool = False,
) -> CaseKind:
    """Work out what state a case is in, from what has actually happened to it.

    Order matters. Cleared beats everything -- a fixed road is a fixed road even
    if the paperwork was late. Suppressed is next, because a case we decided not
    to report never enters the filed/overdue lifecycle at all.
    """
    if cleared or case.closed_at is not None:
        return CaseKind.CLEARED

    if case.gate_decision is GateDecision.SUPPRESS:
        return CaseKind.SUPPRESSED

    if not filings:
        return CaseKind.WATCHING

    first = min(filings, key=lambda f: f.filed_at)
    if case.sla_deadline is not None and due_tier(first.filed_at, case.sla_deadline, now) > 1:
        return CaseKind.ESCALATED
    return CaseKind.FILED


def should_open_case(decision: GateDecision) -> bool:
    """Is this decision worth a case at all?

    DROP means we looked and convinced ourselves there was nothing there; it
    leaves a log line but no case. Everything else is worth a record, including
    SUPPRESS -- "we saw this and deliberately said nothing" is exactly the kind
    of decision that should be on the record rather than invisible.
    """
    return decision is not GateDecision.DROP
