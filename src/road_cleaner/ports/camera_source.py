"""Where camera stills and official incident feeds come from."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from road_cleaner.domain.models import Camera, OfficialEvent


class CameraFetchError(RuntimeError):
    """A camera could not be read. Expected and routine -- public DOT cameras go
    offline constantly. Callers should log and move on, never crash a run."""


@runtime_checkable
class CameraSource(Protocol):
    """A supply of cameras, their images, and what the state already knows about.

    Implemented twice: against the real state 511 APIs, and by a simulator that
    renders synthetic road scenes so the whole pipeline runs with no keys.
    """

    async def list_cameras(self, state: str) -> list[Camera]:
        """The camera registry for one state.

        Metadata changes rarely, so this is refreshed daily rather than polled --
        it is also the call that costs us against the 10-per-60s throttle.
        """
        ...

    async def fetch_snapshot(self, camera: Camera) -> bytes:
        """A still image, as bytes. Raises CameraFetchError if unavailable.

        Goes straight to the image CDN rather than through the throttled API.
        """
        ...

    async def list_events(self, state: str) -> list[OfficialEvent]:
        """Incidents the state's own feed already knows about.

        Fetched so that we can stay quiet about them.
        """
        ...

    async def close(self) -> None:
        """Release connections."""
        ...
