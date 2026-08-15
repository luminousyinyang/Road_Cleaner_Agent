"""① Watcher — looks at cameras.

The dullest agent and the one that decides the bill. It polls cameras on a tier
schedule, stores what it sees, and publishes a frame for analysis only when
looking is likely to be worth it.

Two economies, in order of how much they save:

1. **Identical-frame skip.** A camera serving a cached or frozen image costs
   nothing. This is narrow -- see `scene.phash`, frame differencing genuinely
   cannot see a hazard -- so it is capped by `max_consecutive_skips`, without
   which a static scene containing a hazard would be skipped forever.
2. **Tiering.** Busy corridors get looked at every couple of minutes, quiet
   rural cameras every ten, and any camera with an open case every minute,
   because that is the one we have promised somebody we are watching.
"""

from __future__ import annotations

from road_cleaner.adapters.camera.scene import hamming_distance, phash
from road_cleaner.domain.enums import CameraTier, EventTopic
from road_cleaner.domain.models import Camera, Frame
from road_cleaner.logging import get_logger
from road_cleaner.ports.camera_source import CameraFetchError

log = get_logger(__name__)


class Watcher:
    def __init__(self, container) -> None:
        self.c = container
        self.settings = container.settings
        # Per-camera count of consecutive identical frames, so the skip has a cap.
        self._skips: dict[str, int] = {}
        # Cost accounting, surfaced by `stats` and the demo summary.
        self.polls = 0
        self.skipped = 0
        self.published = 0
        self.failures = 0

    def poll_interval(self, camera: Camera) -> int:
        return {
            CameraTier.ACTIVE: self.settings.poll_seconds_active,
            CameraTier.BUSY: self.settings.poll_seconds_busy,
            CameraTier.QUIET: self.settings.poll_seconds_quiet,
        }[camera.tier]

    def is_due(self, camera: Camera) -> bool:
        if not camera.active:
            return False
        if camera.last_polled_at is None:
            return True
        elapsed = (self.c.clock.now() - camera.last_polled_at).total_seconds()
        return elapsed >= self.poll_interval(camera)

    async def seed_registry(self) -> int:
        """Load the camera registry into the store.

        Camera metadata changes rarely, so this runs daily rather than per poll.
        In cloud mode it is also the call that costs us against the 10-per-60s
        throttle, which is exactly why it isn't in the polling path.
        """
        total = 0
        for state in ("GA", "FL", "NC"):
            try:
                cameras = await self.c.cameras.list_cameras(state)
            except Exception as exc:  # noqa: BLE001 - one bad state shouldn't stop the rest
                log.warning(
                    "Camera registry unavailable",
                    extra={"state": state, "error": str(exc)},
                )
                continue
            for camera in cameras:
                existing = await self.c.repository.get_camera(camera.id)
                if existing is not None:
                    # Keep our own scheduling state; take everything else fresh.
                    camera.last_polled_at = existing.last_polled_at
                    camera.tier = existing.tier
                await self.c.repository.upsert_camera(camera)
                total += 1
            log.info("Camera registry refreshed", extra={"state": state, "cameras": len(cameras)})
        return total

    async def tick(self, limit: int = 100) -> int:
        """One pass: poll every camera that is due. Returns frames published."""
        now = self.c.clock.now()
        published = 0

        for camera in await self.c.repository.list_cameras():
            if not self.is_due(camera):
                continue
            if await self._poll(camera):
                published += 1
            if published >= limit:
                break

        del now
        return published

    async def _poll(self, camera: Camera) -> bool:
        """Fetch one camera. Returns True if a frame was published for analysis."""
        self.polls += 1
        now = self.c.clock.now()

        try:
            image = await self.c.cameras.fetch_snapshot(camera)
        except CameraFetchError as exc:
            # Public DOT cameras go offline constantly. Routine, not exceptional.
            self.failures += 1
            log.debug("Camera unavailable", extra={"camera_id": camera.id, "error": str(exc)})
            camera.last_polled_at = now
            await self.c.repository.upsert_camera(camera)
            return False

        digest = phash(image)
        previous = await self.c.repository.latest_frame(camera.id)

        if previous is not None and self._is_identical(digest, previous.phash, camera.id):
            self.skipped += 1
            camera.last_polled_at = now
            await self.c.repository.upsert_camera(camera)
            return False

        self._skips[camera.id] = 0

        blob_key = f"{camera.state}/{camera.id}/{now.strftime('%Y%m%dT%H%M%S')}.jpg"
        await self.c.blobs.put(blob_key, image)

        frame = Frame(
            camera_id=camera.id,
            captured_at=now,
            blob_key=blob_key,
            phash=digest,
            width=640,
            height=360,
        )
        await self.c.repository.save_frame(frame)

        camera.last_polled_at = now
        await self.c.repository.upsert_camera(camera)

        await self.c.bus.publish(
            EventTopic.FRAME_CAPTURED,
            {"frame_id": frame.id, "camera_id": camera.id, "blob_key": blob_key},
        )
        self.published += 1
        return True

    def _is_identical(self, digest: str, previous: str, camera_id: str) -> bool:
        """Is this frame a repeat, and have we skipped too many already?

        The cap is the important half. Without it, a camera pointed at a still
        scene would be skipped indefinitely -- including if a hazard were sitting
        in the middle of it, since the hash cannot see one.
        """
        distance = hamming_distance(digest, previous)
        if distance > self.settings.phash_identical_threshold:
            return False

        count = self._skips.get(camera_id, 0) + 1
        if count >= self.settings.max_consecutive_skips:
            log.debug(
                "Forcing a look despite an unchanged frame",
                extra={"camera_id": camera_id, "consecutive_skips": count},
            )
            self._skips[camera_id] = 0
            return False

        self._skips[camera_id] = count
        return True

    @property
    def skip_rate(self) -> float:
        return self.skipped / self.polls if self.polls else 0.0
