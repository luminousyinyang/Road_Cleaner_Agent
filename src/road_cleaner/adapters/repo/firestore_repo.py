"""Firestore-backed repository.

Same interface as the SQLite one, so nothing above this layer knows which is in
use. Two places where Firestore's shape genuinely differs are worth flagging,
because both are correctness issues rather than style:

* **Case id allocation is a transaction.** Several Cloud Run instances can be
  opening cases at once, and two cases with the same id would silently overwrite
  each other. SQLite gets away with a lock because it is one process.
* **Frames, detections and trail entries are subcollections**, not top-level
  collections with a foreign key, so reading a case's trail is one query instead
  of a scan.

The client is imported lazily and every blocking call runs on a worker thread —
the Firestore client is synchronous.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from road_cleaner.domain.models import (
    Agency,
    Camera,
    Case,
    CaseWithDetail,
    Detection,
    Filing,
    Frame,
    TrailEvent,
)
from road_cleaner.logging import get_logger

log = get_logger(__name__)

CAMERAS = "cameras"
CASES = "cases"
AGENCIES = "agencies"
FRAMES = "frames"
DETECTIONS = "detections"
TRAIL = "trail"
FILINGS = "filings"
COUNTERS = "counters"

SEQUENCE_START = {"GA": 4460, "FL": 2195, "NC": 1168}
OPEN_KINDS = ["watching", "filed", "escalated"]


class FirestoreCaseRepository:
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
        # Firestore is schemaless, but `list_cases` filters on state/kind and
        # orders by opened_at, which needs composite indexes. They are created by
        # `deploy/deploy.sh --with-firestore`; without them the first query fails
        # with FAILED_PRECONDITION and a console link to build the missing index.
        await asyncio.to_thread(self._client)

    async def close(self) -> None:
        self._db = None

    @staticmethod
    def _doc(model) -> dict[str, Any]:
        return model.model_dump(mode="json")

    async def _run(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    # -------------------------------------------------------------- cameras
    async def upsert_camera(self, camera: Camera) -> None:
        await self._run(
            lambda: self._client().collection(CAMERAS).document(camera.id).set(
                self._doc(camera)
            )
        )

    async def get_camera(self, camera_id: str) -> Camera | None:
        snap = await self._run(
            lambda: self._client().collection(CAMERAS).document(camera_id).get()
        )
        return Camera(**snap.to_dict()) if snap.exists else None

    async def list_cameras(self, state: str | None = None) -> list[Camera]:
        def query():
            col = self._client().collection(CAMERAS)
            if state:
                col = col.where("state", "==", state)
            return [Camera(**d.to_dict()) for d in col.stream()]

        return await self._run(query)

    async def cameras_due(self, now: datetime, limit: int = 50) -> list[Camera]:
        def query():
            docs = (
                self._client()
                .collection(CAMERAS)
                .where("active", "==", True)
                .order_by("last_polled_at")
                .limit(limit)
                .stream()
            )
            return [Camera(**d.to_dict()) for d in docs]

        return await self._run(query)

    # --------------------------------------------------------------- frames
    async def save_frame(self, frame: Frame) -> None:
        await self._run(
            lambda: self._client()
            .collection(CAMERAS)
            .document(frame.camera_id)
            .collection(FRAMES)
            .document(frame.id)
            .set(self._doc(frame))
        )

    async def latest_frame(self, camera_id: str) -> Frame | None:
        def query():
            docs = list(
                self._client()
                .collection(CAMERAS)
                .document(camera_id)
                .collection(FRAMES)
                .order_by("captured_at", direction="DESCENDING")
                .limit(1)
                .stream()
            )
            return Frame(**docs[0].to_dict()) if docs else None

        return await self._run(query)

    # ----------------------------------------------------------- detections
    async def save_detection(self, detection: Detection) -> None:
        await self._run(
            lambda: self._client()
            .collection(CAMERAS)
            .document(detection.camera_id)
            .collection(DETECTIONS)
            .document(detection.id)
            .set(self._doc(detection))
        )

    async def recent_detections(
        self, camera_id: str, since: datetime, limit: int = 20
    ) -> list[Detection]:
        def query():
            docs = (
                self._client()
                .collection(CAMERAS)
                .document(camera_id)
                .collection(DETECTIONS)
                .where("analyzed_at", ">=", since.isoformat())
                .order_by("analyzed_at", direction="DESCENDING")
                .limit(limit)
                .stream()
            )
            return [Detection(**d.to_dict()) for d in docs]

        return await self._run(query)

    # ---------------------------------------------------------------- cases
    async def save_case(self, case: Case) -> None:
        await self._run(
            lambda: self._client().collection(CASES).document(case.id).set(
                self._doc(case), merge=True
            )
        )

    async def get_case(self, case_id: str) -> Case | None:
        snap = await self._run(
            lambda: self._client().collection(CASES).document(case_id).get()
        )
        if not snap.exists:
            return None
        data = snap.to_dict()
        data.pop("correlation_key", None)
        return Case(**data)

    async def get_case_detail(self, case_id: str) -> CaseWithDetail | None:
        case = await self.get_case(case_id)
        if case is None:
            return None
        return CaseWithDetail(
            case=case,
            camera=await self.get_camera(case.camera_id),
            agency=await self.get_agency(case.agency_id) if case.agency_id else None,
            trail=await self.get_trail(case_id),
            filings=await self.get_filings(case_id),
            detections=[],
        )

    async def find_open_case(self, correlation_key: str) -> Case | None:
        def query():
            docs = list(
                self._client()
                .collection(CASES)
                .where("correlation_key", "==", correlation_key)
                .where("kind", "in", OPEN_KINDS)
                .limit(1)
                .stream()
            )
            if not docs:
                return None
            data = docs[0].to_dict()
            data.pop("correlation_key", None)
            return Case(**data)

        return await self._run(query)

    async def find_recent_case(self, correlation_key: str, since: datetime) -> Case | None:
        """Open, or closed within the cooldown. See the SQLite adapter for why."""
        open_case = await self.find_open_case(correlation_key)
        if open_case is not None:
            return open_case

        def query():
            docs = list(
                self._client()
                .collection(CASES)
                .where("correlation_key", "==", correlation_key)
                .where("updated_at", ">=", since.isoformat())
                .order_by("updated_at", direction="DESCENDING")
                .limit(1)
                .stream()
            )
            if not docs:
                return None
            data = docs[0].to_dict()
            data.pop("correlation_key", None)
            return Case(**data)

        return await self._run(query)

    async def set_correlation(self, case_id: str, correlation_key: str) -> None:
        await self._run(
            lambda: self._client().collection(CASES).document(case_id).update(
                {"correlation_key": correlation_key}
            )
        )

    async def list_cases(
        self,
        state: str | None = None,
        kind: str | None = None,
        limit: int = 100,
        include_synthetic: bool = False,
    ) -> list[Case]:
        """Cases, newest first. Drill cases excluded unless asked for.

        The synthetic filter is applied in Python rather than as a `where`
        clause. Firestore cannot match documents that lack a field at all, so a
        server-side `synthetic == False` would silently drop every case written
        before the field existed. Volumes here are a few hundred rows.
        """
        def query():
            col = self._client().collection(CASES)
            if state and state != "all":
                col = col.where("state", "==", state)
            if kind and kind != "all":
                col = col.where("kind", "==", kind)
            docs = col.order_by("opened_at", direction="DESCENDING").limit(limit).stream()
            out = []
            for d in docs:
                data = d.to_dict()
                data.pop("correlation_key", None)
                case = Case(**data)
                if include_synthetic or not case.synthetic:
                    out.append(case)
            return out

        return await self._run(query)

    async def open_cases(self) -> list[Case]:
        # Synthetic cases are never re-checked -- the Auditor would go looking
        # for a camera that does not exist.
        def query():
            docs = self._client().collection(CASES).where("kind", "in", OPEN_KINDS).stream()
            out = []
            for d in docs:
                data = d.to_dict()
                data.pop("correlation_key", None)
                case = Case(**data)
                if not case.synthetic:
                    out.append(case)
            return out

        return await self._run(query)

    async def next_case_id(self, state: str) -> str:
        """Allocate the next id inside a transaction.

        Several instances can be opening cases at the same moment. A read-then-
        write without a transaction hands the same id to two of them, and the
        second case silently overwrites the first.
        """
        from google.cloud import firestore

        def allocate() -> str:
            db = self._client()
            ref = db.collection(COUNTERS).document(state)

            @firestore.transactional
            def bump(transaction):
                snap = ref.get(transaction=transaction)
                nxt = (
                    snap.to_dict().get("next")
                    if snap.exists
                    else SEQUENCE_START.get(state, 1000)
                )
                transaction.set(ref, {"next": nxt + 1})
                return nxt

            return f"{state}-{bump(db.transaction())}"

        return await self._run(allocate)

    # ---------------------------------------------------------------- trail
    async def append_trail(self, event: TrailEvent) -> None:
        await self._run(
            lambda: self._client()
            .collection(CASES)
            .document(event.case_id)
            .collection(TRAIL)
            .document(event.id)
            .set(self._doc(event))
        )

    async def get_trail(self, case_id: str) -> list[TrailEvent]:
        def query():
            docs = (
                self._client()
                .collection(CASES)
                .document(case_id)
                .collection(TRAIL)
                .order_by("at")
                .stream()
            )
            return [TrailEvent(**d.to_dict()) for d in docs]

        return await self._run(query)

    # -------------------------------------------------------------- filings
    async def save_filing(self, filing: Filing) -> None:
        await self._run(
            lambda: self._client()
            .collection(CASES)
            .document(filing.case_id)
            .collection(FILINGS)
            .document(filing.id)
            .set(self._doc(filing))
        )

    async def get_filings(self, case_id: str) -> list[Filing]:
        def query():
            docs = (
                self._client()
                .collection(CASES)
                .document(case_id)
                .collection(FILINGS)
                .order_by("filed_at")
                .stream()
            )
            return [Filing(**d.to_dict()) for d in docs]

        return await self._run(query)

    # ------------------------------------------------------------- agencies
    async def upsert_agency(self, agency: Agency) -> None:
        await self._run(
            lambda: self._client().collection(AGENCIES).document(agency.id).set(
                self._doc(agency)
            )
        )

    async def get_agency(self, agency_id: str) -> Agency | None:
        snap = await self._run(
            lambda: self._client().collection(AGENCIES).document(agency_id).get()
        )
        return Agency(**snap.to_dict()) if snap.exists else None

    async def list_agencies(self) -> list[Agency]:
        return await self._run(
            lambda: [
                Agency(**d.to_dict())
                for d in self._client().collection(AGENCIES).stream()
            ]
        )

    # ---------------------------------------------------------------- stats
    async def stats(self, now: datetime) -> dict[str, float | int]:
        cases = await self.list_cases(limit=5000)
        by_kind: dict[str, int] = {}
        n_filed = n_suppressed = 0
        for case in cases:
            by_kind[case.kind.value] = by_kind.get(case.kind.value, 0) + 1
            if case.gate_decision.value == "file":
                n_filed += 1
            elif case.gate_decision.value == "suppress":
                n_suppressed += 1

        latencies: list[float] = []
        filed_week = 0
        week_ago = now - timedelta(days=7)
        for case in cases:
            for filing in await self.get_filings(case.id):
                if filing.tier == 1:
                    latencies.append((filing.filed_at - case.opened_at).total_seconds())
                if filing.filed_at >= week_ago:
                    filed_week += 1

        latencies.sort()
        total_confirmed = n_filed + n_suppressed
        return {
            "filed_this_week": filed_week,
            "missed_by_feed_pct": round(100 * n_filed / total_confirmed) if total_confirmed else 0,
            "median_detect_to_file_seconds": (
                int(latencies[len(latencies) // 2]) if latencies else 0
            ),
            "total_cases": len(cases),
            "open_cases": sum(by_kind.get(k, 0) for k in OPEN_KINDS),
            **{f"count_{k}": v for k, v in by_kind.items()},
        }
