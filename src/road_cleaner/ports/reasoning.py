"""The judgment calls.

Three places in this system genuinely need a language model, and no others:

1. **Jurisdiction**, when the rules run out. Most of the time a lookup table
   answers "whose road is this" perfectly well. Occasionally it doesn't -- a
   traffic signal on a state route inside city limits belongs to the city, not
   the DOT -- and that is a reasoning problem.
2. **Report prose**, to polish text that `domain/narrative.py` has already
   written deterministically.
3. **Clearance judgement**, handled through `VisionAnalyzer.verify_cleared`.

Everything else -- when to poll, whether two frames corroborate, what the SLA
is, whether to escalate -- is control flow, and control flow belongs in Python
where it can be tested and where it behaves the same way twice. A model that
decides whether to contact a government agency is a model that will eventually
decide to contact a government agency for a bad reason.

`ScriptedReasoner` implements all of this deterministically, so the absence of
an API key costs polish, not function.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from road_cleaner.domain.models import Agency, Camera, Detection


class JurisdictionVerdict:
    def __init__(self, agency_id: str, confidence: float, rationale: str) -> None:
        self.agency_id = agency_id
        self.confidence = confidence
        self.rationale = rationale


@runtime_checkable
class Reasoner(Protocol):
    @property
    def name(self) -> str:
        """Which reasoner produced this, recorded on the trail."""
        ...

    async def resolve_jurisdiction(
        self, camera: Camera, detection: Detection, candidates: list[Agency]
    ) -> JurisdictionVerdict | None:
        """Pick the responsible agency when the rules could not.

        Returning None means "still unsure" -- which is a legitimate answer, and
        results in the case being watched rather than misfiled.
        """
        ...

    async def polish_report(self, draft: str, detection: Detection, camera: Camera) -> str:
        """Improve already-correct report prose.

        Must never change the facts. If it fails, the caller keeps the draft --
        which is why the draft is generated deterministically first.
        """
        ...
