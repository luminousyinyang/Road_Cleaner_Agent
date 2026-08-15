"""Evidence frames in Cloud Storage.

Retention is enforced by a bucket lifecycle rule rather than by us walking the
bucket — see `deploy/deploy.sh`, which sets a seven-day delete. `purge_older_than`
exists so the port has one implementation everywhere, but on GCS the lifecycle
policy is the real mechanism and does not depend on our process running.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from road_cleaner.logging import get_logger
from road_cleaner.ports.blob_store import BlobNotFoundError

log = get_logger(__name__)


class GcsBlobStore:
    def __init__(self, bucket: str | None, project: str | None = None) -> None:
        if not bucket:
            raise ValueError("GCS_BUCKET must be set to use the Cloud Storage blob store")
        self.bucket_name = bucket
        self.project = project
        self._bucket = None

    def _get_bucket(self):
        if self._bucket is not None:
            return self._bucket
        try:
            from google.cloud import storage
        except ImportError as exc:  # pragma: no cover - depends on install profile
            raise RuntimeError(
                "google-cloud-storage is not installed. Install it with:\n"
                "    uv pip install -e '.[cloud]'"
            ) from exc
        client = storage.Client(project=self.project)
        self._bucket = client.bucket(self.bucket_name)
        return self._bucket

    async def put(self, key: str, data: bytes, content_type: str = "image/jpeg") -> str:
        def upload() -> None:
            blob = self._get_bucket().blob(key)
            blob.upload_from_string(data, content_type=content_type)

        await asyncio.to_thread(upload)
        return key

    async def get(self, key: str) -> bytes:
        def download() -> bytes:
            blob = self._get_bucket().blob(key)
            if not blob.exists():
                raise BlobNotFoundError(key)
            return blob.download_as_bytes()

        return await asyncio.to_thread(download)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(lambda: self._get_bucket().blob(key).exists())

    async def delete(self, key: str) -> None:
        def remove() -> None:
            blob = self._get_bucket().blob(key)
            if blob.exists():
                blob.delete()

        await asyncio.to_thread(remove)

    async def purge_older_than(self, days: int) -> int:
        """Fallback sweep. The bucket lifecycle rule is the primary mechanism."""
        cutoff = datetime.now(UTC) - timedelta(days=days)

        def purge() -> int:
            removed = 0
            for blob in self._get_bucket().list_blobs():
                if blob.time_created and blob.time_created < cutoff:
                    blob.delete()
                    removed += 1
            return removed

        return await asyncio.to_thread(purge)
