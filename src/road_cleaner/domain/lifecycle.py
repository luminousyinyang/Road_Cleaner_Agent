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
from road_cleaner.domain.models import Case, Filing, GateResult
from road_cleaner.domain.sla import due_tier

# How long after a case closes the same camera+hazard is still considered the
# same situation rather than a new one. Without this, a suppressed or cleared
# case gets re-opened on the very next poll and the log fills with duplicates
# of one hazard.
RECURRENCE_COOLDOWN_HOURS = 6


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

    Order matters, and the middle branch is easy to get wrong. An explicit
    clearance from the Auditor beats everything -- a fixed road is fixed even if
    the paperwork ran late. But suppression has to be checked *before* the
    generic `closed_at`, because suppressing a case also closes it: without this
    ordering every "we stayed quiet, they already knew" case would misreport
    itself as "we got this fixed", which is close to the opposite of the truth.
    """
    if cleared:
        return CaseKind.CLEARED

    if case.gate_decision is GateDecision.SUPPRESS:
        return CaseKind.SUPPRESSED

    if case.closed_at is not None:
        return CaseKind.CLEARED

    if not filings:
        return CaseKind.WATCHING

    first = min(filings, key=lambda f: f.filed_at)
    if case.sla_deadline is not None and due_tier(first.filed_at, case.sla_deadline, now) > 1:
        return CaseKind.ESCALATED
    return CaseKind.FILED


def should_open_case(result: GateResult) -> bool:
    """Is this worth opening a case for?

    Two conditions, and the second one matters more than it looks:

    * not a DROP -- we looked and convinced ourselves there was nothing there
    * **corroborated by a second frame**

    Without the corroboration requirement, every single-frame false positive
    becomes a case. A vision model glancing at thousands of frames an hour will
    occasionally call a shadow debris, and if each of those opened a case, the
    road log would be mostly noise and the "watching" bucket would be
    meaningless. A single unconfirmed frame gets a log line and nothing more.

    Cases that do survive this are the honest ones: something was seen twice,
    at least ninety seconds apart. Whether it then gets filed is the confidence
    bar's business, not this function's.
    """
    return result.decision is not GateDecision.DROP and bool(result.corroborating_ids)
