"""Where evidence frames live.

Frames are the evidence. A report that says "trust me" is worth nothing; a
report with two timestamped stills from a public camera is auditable by the
agency receiving it.

Retention is short on purpose -- see the house rules. Only hazard-positive
frames are kept at all, and they expire in a week.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class BlobNotFoundError(KeyError):
    pass


@runtime_checkable
class BlobStore(Protocol):
    async def put(self, key: str, data: bytes, content_type: str = "image/jpeg") -> str:
        """Store bytes under a key. Returns the key."""
        ...

    async def get(self, key: str) -> bytes:
        """Retrieve bytes. Raises BlobNotFoundError if missing."""
        ...

    async def exists(self, key: str) -> bool: ...

    async def delete(self, key: str) -> None: ...

    async def list_keys(self, prefix: str) -> list[str]:
        """Every key under a prefix, newest first.

        Used to prune superseded generated media -- re-rendering a scenario
        should replace the old clip rather than pile another 20MB beside it.
        """
        ...

    async def purge_older_than(self, days: int) -> int:
        """Delete frames past the retention window. Returns how many went.

        Called on a schedule. Keeping road stills forever would turn a hazard
        reporter into a surveillance archive, which is not what this is.
        """
        ...
