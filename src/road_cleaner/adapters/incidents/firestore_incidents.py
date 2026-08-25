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

The Firestore client is synchronous, so every call runs on a worker thread.
"""

from __future__ import annotations

import asyncio
from typing import Any

from road_cleaner.domain.models import Incident
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
