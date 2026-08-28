"""Incident store on local disk, one JSON file per incident.

Exists so that `make serve` works without a Google Cloud project. It is the
local counterpart of the Firestore store the same way `LocalBlobStore` is the
counterpart of `GcsStore`, and it is chosen by the same `REPOSITORY` setting.

JSON files rather than a table in `road_cleaner.db`, for two reasons. The
schema in `schema.sql` describes the traffic-camera pipeline and is migrated as
a unit; incidents are not part of that and should not be able to break it. And
a directory of readable files is the right affordance for the thing this is --
scratch storage for a laptop, inspectable with `cat`.

**Not for deployment.** Cloud Run's filesystem is ephemeral, so anything written
here dies with the instance. Deployed, `REPOSITORY=firestore` is what you want.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

from road_cleaner.domain.models import Incident, IncidentSighting
from road_cleaner.logging import get_logger

log = get_logger(__name__)


class LocalIncidentStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    async def initialize(self) -> None:
        await asyncio.to_thread(self.root.mkdir, parents=True, exist_ok=True)

    async def close(self) -> None:
        return None

    @staticmethod
    def _safe(value: str) -> str | None:
        """The identifier, or None if it is not one.

        Rejects rather than scrubs. Stripping the offending characters looks
        safer and is worse: `"../me"` would come back as `"me"`, quietly
        resolving one caller's request onto another user's directory. An
        identifier that needed editing to be usable was not that identifier, so
        the honest answer is "no such thing".

        Firebase uids and our own hex ids are both well inside this alphabet, so
        nothing legitimate is turned away.
        """
        if value and all(ch.isalnum() or ch in "-_" for ch in value):
            return value
        return None

    def _dir(self, uid: str) -> Path | None:
        # A filesystem path built from a value that arrived over the network.
        safe = self._safe(uid)
        return self.root / safe if safe else None

    # --------------------------------------------------------------- writes
    async def save(self, incident: Incident) -> None:
        def write() -> None:
            directory = self._dir(incident.uid)
            if directory is None:
                # Unlike the reads, this one raises: a uid that is not an
                # identifier has come from somewhere other than a verified
                # token, and silently dropping the write would lose data.
                raise ValueError(f"Not a usable uid: {incident.uid!r}")
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{incident.id}.json"
            # Written whole then moved, so a crash mid-write cannot leave a
            # half-file that fails to parse on the next listing.
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(incident.model_dump_json(indent=2), encoding="utf-8")
            tmp.replace(path)

        await asyncio.to_thread(write)

    # ---------------------------------------------------------------- reads
    async def list_for_user(self, uid: str, limit: int = 100) -> list[Incident]:
        def read() -> list[Incident]:
            directory = self._dir(uid)
            if directory is None or not directory.is_dir():
                return []
            found: list[Incident] = []
            for path in directory.glob("*.json"):
                incident = self._load(path)
                if incident is not None:
                    found.append(incident)
            found.sort(key=lambda i: i.created_at, reverse=True)
            return found[:limit]

        return await asyncio.to_thread(read)

    async def get(self, uid: str, incident_id: str) -> Incident | None:
        def read() -> Incident | None:
            # Both halves of the path arrived over the network, so both are
            # checked. Either one failing means there is no such incident.
            directory = self._dir(uid)
            safe_id = self._safe(incident_id)
            if directory is None or safe_id is None:
                return None
            return self._load(directory / f"{safe_id}.json")

        return await asyncio.to_thread(read)

    async def recent_sightings(
        self, since: datetime, limit: int = 500
    ) -> list[IncidentSighting]:
        """Every user's directory, newest first, projected down.

        A full walk of the tree. That is acceptable here and nowhere else: this
        store exists so `make serve` works on a laptop, where the tree is one
        developer's own test reports. The deployed answer is the Firestore
        store, which pushes the same filter into a query.
        """

        def read() -> list[IncidentSighting]:
            found: list[tuple[datetime, IncidentSighting]] = []
            for directory in self.root.glob("*"):
                if not directory.is_dir():
                    continue
                for path in directory.glob("*.json"):
                    incident = self._load(path)
                    if incident is None or incident.created_at < since:
                        continue
                    found.append(
                        (
                            incident.created_at,
                            IncidentSighting(
                                hazard_type=incident.hazard_type,
                                lat=incident.lat,
                                lng=incident.lng,
                                created_at=incident.created_at,
                            ),
                        )
                    )
            # Newest first, so a `limit` that bites drops the oldest -- the ones
            # about to leave the window anyway.
            found.sort(key=lambda pair: pair[0], reverse=True)
            return [sighting for _, sighting in found[:limit]]

        return await asyncio.to_thread(read)

    @staticmethod
    def _load(path: Path) -> Incident | None:
        try:
            return Incident(**json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            # The ordinary "no such incident" answer, which `get` asks on every
            # miss. Not worth a line in the log.
            return None
        except (OSError, ValueError) as exc:
            # This one is a real problem -- but one corrupt file should not blank
            # somebody's whole history, so it is skipped rather than raised.
            log.warning("Skipping unreadable incident %s: %s", path, exc)
            return None
