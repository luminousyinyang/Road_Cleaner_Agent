"""Persistence for what individual people reported through their own dashcams.

Separate from `CaseRepository` rather than bolted onto it. The two answer
different questions -- "what has the fleet found and filed" versus "what did
*this person* keep" -- and every operation here is scoped by `uid`, which is a
concept the case repository does not have and should not acquire.

Keeping the ownership check in the signature is the point. There is no
`get(incident_id)`; the uid is not an optional filter a caller can forget on the
way to reading somebody else's incident.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from road_cleaner.domain.models import Incident


@runtime_checkable
class IncidentStore(Protocol):
    async def initialize(self) -> None:
        """Create whatever the backing store needs. Idempotent."""
        ...

    async def close(self) -> None: ...

    async def save(self, incident: Incident) -> None:
        """Write it, overwriting any incident with the same id and owner."""
        ...

    async def list_for_user(self, uid: str, limit: int = 100) -> list[Incident]:
        """That user's incidents, newest first."""
        ...

    async def get(self, uid: str, incident_id: str) -> Incident | None:
        """One incident, or None if it does not exist *or is not theirs*."""
        ...
