"""Deterministic reasoning, for when there is no model available.

The point of this class is that its absence changes nothing structural. The
pipeline calls the same methods, gets the same shapes back, and behaves the same
way -- what it loses is nuance on the handful of genuinely ambiguous calls, and
the prose stays exactly as `domain/narrative.py` wrote it.

That is the correct failure mode for a system that files paperwork: no key means
plainer wording, not wrong decisions.
"""

from __future__ import annotations

from road_cleaner.domain.enums import AgencyLevel
from road_cleaner.domain.models import Agency, Camera, Detection
from road_cleaner.ports.reasoning import JurisdictionVerdict


class ScriptedReasoner:
    @property
    def name(self) -> str:
        return "scripted"

    async def resolve_jurisdiction(
        self, camera: Camera, detection: Detection, candidates: list[Agency]
    ) -> JurisdictionVerdict | None:
        """Fall back to the least-surprising candidate.

        With no model to weigh the specifics, prefer the most specific authority
        that could plausibly own it -- a toll authority over a state district, a
        city over a state -- because a misrouted report to a smaller body is
        more likely to be forwarded on than one lost in a state-wide queue.

        Returns None if there is nothing to choose from, which leaves the case
        watched rather than misfiled.
        """
        if not candidates:
            return None

        priority = {
            AgencyLevel.TOLL_AUTHORITY: 0,
            AgencyLevel.CITY: 1,
            AgencyLevel.COUNTY: 2,
            AgencyLevel.STATE_DOT: 3,
        }
        best = min(candidates, key=lambda a: priority.get(a.level, 9))
        return JurisdictionVerdict(
            agency_id=best.id,
            # Deliberately modest. This is a fallback, and the confidence
            # recorded on the trail should say so.
            confidence=0.55,
            rationale=(
                f"No model available; picked {best.name} as the most specific "
                f"authority among {len(candidates)} candidates."
            ),
        )

    async def polish_report(self, draft: str, detection: Detection, camera: Camera) -> str:
        """Return the draft untouched.

        The draft is already correct and readable -- polishing is a luxury, not
        a requirement.
        """
        return draft
