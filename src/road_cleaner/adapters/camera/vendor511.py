"""The real state DOT APIs.

Georgia, Florida and North Carolina all run the same vendor platform, verified
directly against all three:

    https://511ga.org/api/v2/get/cameras?key=<KEY>&format=json
    https://fl511.com/api/v2/get/cameras?key=<KEY>&format=json
    https://www.drivenc.gov/api/v2/get/cameras?key=<KEY>&format=json

All three answer `<Error><Message>Invalid Key</Message></Error>` without a
developer key, which is why the whole system also ships a simulator. One client
parameterised by (base_url, key) therefore covers every launch state -- the
multi-state scalability story costs one adapter, not three.

Two things worth knowing, both of which shape the design:

* **The API is not the polling path.** The published throttle is ten calls per
  sixty seconds, which cannot poll thousands of cameras. So the API is used to
  fetch the camera registry (rarely -- metadata barely changes) and the incident
  feed, while snapshots come straight from the image CDN URLs the registry hands
  back. Those are ordinary public image URLs and are not behind the throttle.
* **SC and TN are not on this platform.** `511sc.org` 404s on this path and TDOT
  SmartWay is a separate application, so each needs its own adapter. The PRD's
  assumption that the whole Southeast shares one vendor holds only for GA/FL/NC.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from road_cleaner.adapters.camera.rate_limit import StateRateLimiters
from road_cleaner.domain.enums import CameraTier
from road_cleaner.domain.models import Camera, OfficialEvent
from road_cleaner.logging import get_logger
from road_cleaner.ports.camera_source import CameraFetchError

log = get_logger(__name__)

# Roads we treat as busy enough to deserve the fast polling tier.
BUSY_ROAD_PREFIXES = ("I-", "SR-", "US-")


class Vendor511CameraSource:
    def __init__(
        self,
        base_urls: dict[str, str],
        api_keys: dict[str, str | None],
        *,
        rate_limit: int = 10,
        rate_window: int = 60,
        timeout: float = 25.0,
    ) -> None:
        self.base_urls = base_urls
        self.api_keys = api_keys
        self.limiters = StateRateLimiters(rate_limit, rate_window)
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "RoadCleaner/0.1 (hazard reporting agent)"},
        )

    # ------------------------------------------------------------- helpers
    async def _get(self, state: str, resource: str) -> Any:
        """One throttled call to a state's API."""
        key = self.api_keys.get(state)
        if not key:
            raise CameraFetchError(f"No developer key configured for {state}")

        base = self.base_urls.get(state, "").rstrip("/")
        url = f"{base}/api/v2/get/{resource}"

        await self.limiters.acquire(state)
        try:
            response = await self._client.get(url, params={"key": key, "format": "json"})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise CameraFetchError(f"{state} {resource} request failed: {exc}") from exc

        # The platform answers errors as XML even when JSON was asked for --
        # an invalid key comes back as <Error><Message>Invalid Key</Message>.
        text = response.text.lstrip()
        if text.startswith("<"):
            raise CameraFetchError(f"{state} {resource} rejected the request: {text[:120]}")

        try:
            return response.json()
        except ValueError as exc:
            raise CameraFetchError(f"{state} {resource} returned non-JSON") from exc

    # ------------------------------------------------------------- cameras
    async def list_cameras(self, state: str) -> list[Camera]:
        payload = await self._get(state, "cameras")
        rows = payload if isinstance(payload, list) else payload.get("cameras", [])

        cameras: list[Camera] = []
        for row in rows:
            camera = self._parse_camera(state, row)
            if camera is not None:
                cameras.append(camera)
        log.info("Fetched camera registry", extra={"state": state, "count": len(cameras)})
        return cameras

    @staticmethod
    def _parse_camera(state: str, row: dict) -> Camera | None:
        """Map one API row onto our model.

        Field names vary a little between deployments of the same platform, so
        each is looked up under the several spellings seen in the wild rather
        than assuming one. A row without coordinates or an image URL is useless
        to us and is dropped rather than stored broken.
        """

        def pick(*names: str, default: Any = None) -> Any:
            for name in names:
                if row.get(name) not in (None, ""):
                    return row[name]
            return default

        lat = pick("Latitude", "latitude", "lat")
        lng = pick("Longitude", "longitude", "lng", "lon")
        url = pick("ImageUrl", "imageUrl", "Url", "url", "SnapshotUrl")
        camera_id = pick("Id", "id", "ID", "CameraId")

        if lat is None or lng is None or not url or camera_id is None:
            return None

        road = str(pick("RoadwayName", "roadway", "Roadway", "route", default="Unknown"))
        return Camera(
            id=str(camera_id),
            state=state,
            name=str(pick("Location", "location", "Description", "name", default=road)),
            road=road,
            direction=pick("DirectionOfTravel", "direction"),
            lat=float(lat),
            lng=float(lng),
            owner_agency_id=pick("Organization", "organization", "owner"),
            snapshot_url=str(url),
            stream_url=pick("VideoUrl", "videoUrl", "StreamUrl"),
            county=pick("County", "county"),
            tier=(
                CameraTier.BUSY
                if road.upper().startswith(BUSY_ROAD_PREFIXES)
                else CameraTier.QUIET
            ),
        )

    # ------------------------------------------------------------ snapshots
    async def fetch_snapshot(self, camera: Camera) -> bytes:
        """Pull a still straight from the image CDN.

        Not throttled and not counted against the API budget: these are plain
        public image URLs meant for exactly this.
        """
        try:
            response = await self._client.get(camera.snapshot_url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise CameraFetchError(f"{camera.id} snapshot failed: {exc}") from exc

        content_type = response.headers.get("content-type", "")
        if not content_type.startswith("image/"):
            # An offline camera often returns an HTML error page with a 200.
            raise CameraFetchError(f"{camera.id} returned {content_type or 'no'} content")
        if len(response.content) < 1024:
            raise CameraFetchError(f"{camera.id} returned a truncated image")
        return response.content

    # --------------------------------------------------------------- events
    async def list_events(self, state: str) -> list[OfficialEvent]:
        """What the state's own feed already knows about.

        Fetched so we can stay quiet about it. A failure here is deliberately
        non-fatal but *is* significant: without the feed we cannot tell whether
        the DOT already knows, so the caller gets an empty list and the gate
        loses its duplicate check for that pass.
        """
        try:
            payload = await self._get(state, "event")
        except CameraFetchError as exc:
            log.warning("Incident feed unavailable", extra={"state": state, "error": str(exc)})
            return []

        rows = payload if isinstance(payload, list) else payload.get("events", [])
        events: list[OfficialEvent] = []
        for row in rows:
            lat, lng = row.get("Latitude"), row.get("Longitude")
            if lat is None or lng is None:
                continue
            events.append(
                OfficialEvent(
                    id=str(row.get("ID") or row.get("Id") or row.get("id", "")),
                    state=state,
                    event_type=str(row.get("EventType") or row.get("Type") or "incident"),
                    lat=float(lat),
                    lng=float(lng),
                    description=str(row.get("Description") or row.get("Comment") or ""),
                    started_at=_parse_time(row.get("StartDate") or row.get("Reported")),
                    active=str(row.get("IsFullClosure", "")).lower() != "closed",
                    source=f"{state} 511 feed",
                )
            )
        return events

    async def get_video_url(self, state: str, camera_id: str) -> str | None:
        """Fetch a live stream URL.

        These expire quickly, so this is only worth calling for the handful of
        cameras with an active case -- calling it fleet-wide would burn the
        entire rate budget on URLs that are stale before they are used.
        """
        try:
            payload = await self._get(state, f"videourl?id={camera_id}")
        except CameraFetchError:
            return None
        if isinstance(payload, dict):
            return payload.get("VideoUrl") or payload.get("url")
        return None

    async def close(self) -> None:
        await self._client.aclose()


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
