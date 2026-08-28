"""Persistence for what individual people reported through their own dashcams.

Separate from `CaseRepository` rather than bolted onto it. The two answer
different questions -- "what has the fleet found and filed" versus "what did
*this person* keep" -- and every operation that returns an `Incident` is scoped
by `uid`, which is a concept the case repository does not have and should not
acquire.

Keeping the ownership check in the signature is the point. There is no
`get(incident_id)`; the uid is not an optional filter a caller can forget on the
way to reading somebody else's incident.

`recent_sightings` is the single read that crosses users, and it is shaped so
that it cannot become one of those. It returns `IncidentSighting` -- hazard
type, coordinates, timestamp -- and not `Incident`, so there is no uid, no
photograph, no prose and no email address in its result to begin with. The
narrower return type is the safeguard; a `uid` filter someone might forget is
not.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from road_cleaner.domain.models import Incident, IncidentSighting


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

    async def recent_sightings(
        self, since: datetime, limit: int = 500
    ) -> list[IncidentSighting]:
        """What everybody reported since `since`, stripped to the dedup facts.

        Crosses users on purpose: two strangers reporting one pothole an hour
        apart is precisely what the caller needs to see. `limit` is a ceiling on
        work, not a page -- a truncated answer undercounts and so can only ever
        fail towards sending a duplicate email, never towards holding a report
        that had no duplicate.
        """
        ...
