"""Evidence frames on the local filesystem."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from road_cleaner.ports.blob_store import BlobNotFoundError


class LocalBlobStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Keys look like "GA/GDOT-CCTV-0447/2026-08-03T14-02-19.jpg". Reject any
        # attempt to climb out of the store -- keys can be influenced by camera
        # ids that ultimately come from a third-party API.
        candidate = (self.root / key).resolve()
        if not candidate.is_relative_to(self.root.resolve()):
            raise ValueError(f"Blob key escapes the store root: {key!r}")
        return candidate

    async def put(self, key: str, data: bytes, content_type: str = "image/jpeg") -> str:
        path = self._path(key)

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        await asyncio.to_thread(write)
        return key

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise BlobNotFoundError(key)
        return await asyncio.to_thread(path.read_bytes)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._path(key).exists)

    async def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            await asyncio.to_thread(path.unlink)

    async def list_keys(self, prefix: str) -> list[str]:
        # Both sides of the relative_to have to be resolved. `self.root` is
        # whatever was configured -- typically the relative "data/media" -- while
        # `_path` always resolves, so comparing them directly raises ValueError.
        root = self.root.resolve()
        base = self._path(prefix) if prefix else root

        def scan() -> list[str]:
            if not base.exists():
                return []
            found = [p for p in base.rglob("*") if p.is_file()]
            found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return [str(p.relative_to(root)) for p in found]

        return await asyncio.to_thread(scan)

    async def purge_older_than(self, days: int) -> int:
        """Delete frames past the retention window.

        The seven-day default is a product decision, not a storage one: a system
        that keeps every frame of every public road forever is a surveillance
        archive, and this is supposed to be a hazard reporter.
        """
        cutoff = time.time() - days * 86_400

        def purge() -> int:
            removed = 0
            for path in self.root.rglob("*"):
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            # Tidy up whatever directories that emptied.
            for directory in sorted(self.root.rglob("*"), reverse=True):
                if directory.is_dir() and not any(directory.iterdir()):
                    directory.rmdir()
            return removed

        return await asyncio.to_thread(purge)
