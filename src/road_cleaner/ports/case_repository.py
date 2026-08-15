"""Persistence for everything the system remembers.

The interface is deliberately domain-shaped rather than table-shaped -- there is
no `execute(sql)` here -- because the same operations have to work against both
SQLite and Firestore, which agree on almost nothing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

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


@runtime_checkable
class CaseRepository(Protocol):
    # ------------------------------------------------------------- lifecycle
    async def initialize(self) -> None:
        """Create schema / indexes. Idempotent."""
        ...

    async def close(self) -> None: ...

    # ---------------------------------------------------------------- cameras
    async def upsert_camera(self, camera: Camera) -> None: ...
    async def get_camera(self, camera_id: str) -> Camera | None: ...
    async def list_cameras(self, state: str | None = None) -> list[Camera]: ...
    async def cameras_due(self, now: datetime, limit: int = 50) -> list[Camera]:
        """Cameras whose polling tier says they are due for a look."""
        ...

    # ----------------------------------------------------------------- frames
    async def save_frame(self, frame: Frame) -> None: ...
    async def latest_frame(self, camera_id: str) -> Frame | None:
        """The last frame from this camera, for perceptual-hash comparison."""
        ...

    # ------------------------------------------------------------- detections
    async def save_detection(self, detection: Detection) -> None: ...
    async def recent_detections(
        self, camera_id: str, since: datetime, limit: int = 20
    ) -> list[Detection]:
        """Prior sightings, for the gate's persistence check."""
        ...

    # ------------------------------------------------------------------ cases
    async def save_case(self, case: Case) -> None: ...
    async def get_case(self, case_id: str) -> Case | None: ...
    async def get_case_detail(self, case_id: str) -> CaseWithDetail | None:
        """A case with its trail, filings, camera and agency attached."""
        ...

    async def find_open_case(self, correlation_key: str) -> Case | None:
        """An already-open case for this camera+hazard, so we don't file twice."""
        ...

    async def set_correlation(self, case_id: str, correlation_key: str) -> None: ...

    async def list_cases(
        self,
        state: str | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[Case]: ...

    async def open_cases(self) -> list[Case]:
        """Cases the Auditor still has work to do on."""
        ...

    async def next_case_id(self, state: str) -> str:
        """Allocate the next per-state case id, e.g. 'GA-4471'."""
        ...

    # ------------------------------------------------------------------ trail
    async def append_trail(self, event: TrailEvent) -> None: ...
    async def get_trail(self, case_id: str) -> list[TrailEvent]: ...

    # ---------------------------------------------------------------- filings
    async def save_filing(self, filing: Filing) -> None: ...
    async def get_filings(self, case_id: str) -> list[Filing]: ...

    # --------------------------------------------------------------- agencies
    async def upsert_agency(self, agency: Agency) -> None: ...
    async def get_agency(self, agency_id: str) -> Agency | None: ...
    async def list_agencies(self) -> list[Agency]: ...

    # ------------------------------------------------------------------ stats
    async def stats(self, now: datetime) -> dict[str, float | int]:
        """Headline numbers for the dashboard's stat band."""
        ...
