"""Firestore-backed incident store.

Documents live at ``users/{uid}/incidents/{id}`` -- a subcollection under the
owner rather than a top-level collection with a `uid` field. That makes
ownership structural: a query rooted at one user's document physically cannot
return another's, so reading somebody else's incidents is not a filter somebody
can forget, it is a path that does not exist.

It also means the newest-first listing needs no composite index. A top-level
`incidents` collection filtered by uid and ordered by created_at would, and
would fail its first query with FAILED_PRECONDITION on any deployment where
`deploy.sh --with-firestore` had not built it.

The 24h dedup check reads the other way, across every user, and pays for the
layout there: a collection group query needs `created_at` indexed at
COLLECTION_GROUP scope, which automatic single-field indexing does not give.
`deploy.sh --with-firestore` requests it. Without it that one query fails, and
`recent_sightings` is written to survive that -- see the comment on its except.

The Firestore client is synchronous, so every call runs on a worker thread.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from road_cleaner.domain.models import Incident, IncidentSighting
from road_cleaner.logging import get_logger

log = get_logger(__name__)

USERS = "users"
INCIDENTS = "incidents"


class FirestoreIncidentStore:
    def __init__(self, project: str | None, database: str = "(default)") -> None:
        if not project:
            raise ValueError("GOOGLE_CLOUD_PROJECT must be set to use Firestore")
        self.project = project
        self.database = database
        self._db = None

    # ------------------------------------------------------------ lifecycle
    def _client(self):
        if self._db is not None:
            return self._db
        try:
            from google.cloud import firestore
        except ImportError as exc:  # pragma: no cover - depends on install profile
            raise RuntimeError(
                "google-cloud-firestore is not installed. Install it with:\n"
                "    uv pip install -e '.[cloud]'"
            ) from exc
        self._db = firestore.Client(project=self.project, database=self.database)
        return self._db

    async def initialize(self) -> None:
        await asyncio.to_thread(self._client)

    async def close(self) -> None:
        self._db = None

    def _collection(self, uid: str):
        return self._client().collection(USERS).document(uid).collection(INCIDENTS)

    @staticmethod
    def _doc(incident: Incident) -> dict[str, Any]:
        return incident.model_dump(mode="json")

    # --------------------------------------------------------------- writes
    async def save(self, incident: Incident) -> None:
        await asyncio.to_thread(
            lambda: self._collection(incident.uid)
            .document(incident.id)
            .set(self._doc(incident))
        )

    # --------------------------------------------------------------- reads
    async def list_for_user(self, uid: str, limit: int = 100) -> list[Incident]:
        def query() -> list[Incident]:
            from google.cloud.firestore import Query

            snaps = (
                self._collection(uid)
                .order_by("created_at", direction=Query.DESCENDING)
                .limit(limit)
                .stream()
            )
            return [Incident(**s.to_dict()) for s in snaps]

        return await asyncio.to_thread(query)

    async def get(self, uid: str, incident_id: str) -> Incident | None:
        snap = await asyncio.to_thread(
            lambda: self._collection(uid).document(incident_id).get()
        )
        return Incident(**snap.to_dict()) if snap.exists else None

    async def recent_sightings(
        self, since: datetime, limit: int = 500
    ) -> list[IncidentSighting]:
        """A collection group query across every user's incidents subcollection.

        The one query in this class that is not rooted at a single user, which is
        the whole reason it returns `IncidentSighting` and not `Incident` -- see
        the port. `select()` makes that structural rather than a promise: the
        four projected fields are all Firestore is asked to send, so nobody
        else's photograph or correspondence crosses the wire at all.

        `created_at` is compared as a string because that is how `_doc` writes
        it. `model_dump(mode="json")` renders every timestamp as an ISO-8601
        instant at a fixed UTC offset, and those sort lexicographically in the
        same order they sort chronologically, so `>=` means what it says.
        """

        def query() -> list[IncidentSighting]:
            from google.cloud.firestore import Query

            snaps = (
                self._client()
                .collection_group(INCIDENTS)
                .where("created_at", ">=", since.isoformat())
                .order_by("created_at", direction=Query.DESCENDING)
                .limit(limit)
                .select(["hazard_type", "lat", "lng", "created_at"])
                .stream()
            )
            return [IncidentSighting(**s.to_dict()) for s in snaps]

        try:
            return await asyncio.to_thread(query)
        except Exception as exc:  # noqa: BLE001 - see below
            # Degrades towards *sending*. A collection group index that has not
            # finished building is the likely cause, and the alternative -- to
            # treat an unanswered question as "this is a duplicate" -- would
            # silently hold a report nobody had made before. Better a second
            # copy of one pothole than a hazard nobody is told about.
            log.warning(
                "Dedup lookup failed; treating this as a first report",
                extra={"error": str(exc)},
            )
            return []
