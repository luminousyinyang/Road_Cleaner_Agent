"""A simulated camera network.

Stands in for the state 511 APIs when no developer key is available -- which,
today, is always. Serves a real camera registry loaded from `seeds/cameras.json`
and renders a genuine JPEG for every snapshot request, so everything downstream
(hashing, storage, vision, evidence, the dashboard) does real work on real
bytes.

It also serves an official incident feed, because suppressing duplicates is a
behaviour worth exercising: some scenarios are deliberately marked as already
known to the state, and those should never produce a filing.
"""

from __future__ import annotations

import json
import random
from datetime import datetime
from pathlib import Path

from road_cleaner.adapters.camera.scenarios import ScenarioBook
from road_cleaner.adapters.camera.scene import (
    SceneSpec,
    lighting_for_hour,
    render,
    traffic_for_hour,
)
from road_cleaner.domain.enums import CameraTier
from road_cleaner.domain.models import Camera, OfficialEvent
from road_cleaner.logging import get_logger
from road_cleaner.ports.camera_source import CameraFetchError
from road_cleaner.ports.clock import Clock

log = get_logger(__name__)

# Roughly one camera in every 250 requests is down at any moment, which is
# optimistic compared with the real thing. Present so the pipeline has to cope
# with fetch failures rather than assuming every poll succeeds.
OUTAGE_RATE = 0.004


class FixtureCameraSource:
    def __init__(
        self,
        cameras_path: Path,
        scenarios: ScenarioBook,
        clock: Clock,
        *,
        outage_rate: float = OUTAGE_RATE,
    ) -> None:
        self.clock = clock
        self.scenarios = scenarios
        self.outage_rate = outage_rate
        self._cameras = self._load_cameras(Path(cameras_path))

    @staticmethod
    def _load_cameras(path: Path) -> list[Camera]:
        raw = json.loads(path.read_text())
        return [
            Camera(
                id=item["id"],
                state=item["state"],
                name=item["name"],
                road=item["road"],
                direction=item.get("direction"),
                lat=item["lat"],
                lng=item["lng"],
                owner_agency_id=item.get("owner_agency_id"),
                snapshot_url=item.get("snapshot_url", f"fixture://{item['id']}"),
                tier=CameraTier(item.get("tier", "quiet")),
                county=item.get("county"),
            )
            for item in raw["cameras"]
        ]

    async def list_cameras(self, state: str) -> list[Camera]:
        return [c for c in self._cameras if c.state == state]

    async def fetch_snapshot(self, camera: Camera) -> bytes:
        now = self.clock.now()
        # Seeded on camera and minute: the same minute always renders the same
        # frame, so two polls inside one minute are correctly detected as
        # unchanged and a re-run of a demo is reproducible.
        minute_key = int(now.timestamp() // 60)
        rng = random.Random(f"{camera.id}:{minute_key}")

        if rng.random() < self.outage_rate:
            raise CameraFetchError(f"{camera.id} did not respond")

        scenario = self.scenarios.active_for(camera.id, now)
        lighting = lighting_for_hour(now.hour)
        spec = SceneSpec(
            camera_id=camera.id,
            label=f"{camera.id}  {camera.road} {camera.direction or ''}".strip(),
            timestamp_text=now.strftime("%Y-%m-%d %H:%M:%S UTC"),
            lighting=lighting,
            traffic_density=traffic_for_hour(now.hour),
            hazard=scenario.hazard if scenario else None,
            hazard_lane=scenario.lane if scenario else "lane_2",
            seed=minute_key,
        )
        image, _box = render(spec)
        return image

    async def list_events(self, state: str) -> list[OfficialEvent]:
        """What the state's own feed already has posted.

        Only scenarios explicitly marked as known to the state appear here --
        that is what makes the suppression path testable.
        """
        now = self.clock.now()
        events: list[OfficialEvent] = []
        by_id = {c.id: c for c in self._cameras}
        for scenario in self.scenarios.official_events_at(now):
            camera = by_id.get(scenario.camera_id)
            if camera is None or camera.state != state:
                continue
            # Offset the event slightly so distance maths is genuinely exercised
            # rather than everything landing exactly on the camera.
            offset_deg = scenario.official_event_offset_m / 111_000
            events.append(
                OfficialEvent(
                    id=f"{state}-official-{scenario.camera_id}",
                    state=state,
                    event_type=scenario.official_event_type or "incident",
                    lat=camera.lat + offset_deg,
                    lng=camera.lng,
                    description=scenario.official_event_description,
                    started_at=now,
                    active=True,
                    source=f"{state} 511 feed",
                )
            )
        return events

    async def close(self) -> None:
        return None

    # Used by the Auditor's "check now": render a frame for an arbitrary moment.
    async def snapshot_at(self, camera: Camera, moment: datetime) -> bytes:
        scenario = self.scenarios.active_for(camera.id, moment)
        spec = SceneSpec(
            camera_id=camera.id,
            label=f"{camera.id}  {camera.road} {camera.direction or ''}".strip(),
            timestamp_text=moment.strftime("%Y-%m-%d %H:%M:%S UTC"),
            lighting=lighting_for_hour(moment.hour),
            traffic_density=traffic_for_hour(moment.hour),
            hazard=scenario.hazard if scenario else None,
            hazard_lane=scenario.lane if scenario else "lane_2",
            seed=int(moment.timestamp() // 60),
        )
        image, _ = render(spec)
        return image
