"""Lyria on Vertex AI.

Scores the demo reel. Be clear about what this is and is not: Lyria writes
instrumental music. It does not make sound effects -- no sirens, no tyre noise,
no impacts. Diegetic sound for a hazard clip comes from Veo's own
`generate_audio`, not from here. Lyria's job is the bed under the reel.

Unlike Veo and Gemini, Lyria has no method on the genai client, so this calls
the Vertex `:predict` endpoint directly over httpx (already a base dependency)
with an ADC bearer token. It is a plain request/response -- no long-running
operation to poll.
"""

from __future__ import annotations

import base64
from datetime import datetime

import httpx

from road_cleaner.adapters.media.manifest import write_manifest
from road_cleaner.logging import get_logger
from road_cleaner.ports.blob_store import BlobStore
from road_cleaner.ports.media import (
    SYNTHETIC_PREFIX,
    MediaUnavailableError,
    SyntheticClip,
)

log = get_logger(__name__)

_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_TIMEOUT_SECONDS = 120.0


class LyriaMusicSynthesizer:
    def __init__(
        self,
        *,
        model: str = "lyria-002",
        store: BlobStore,
        project: str | None = None,
        location: str = "us-central1",
    ) -> None:
        self.model = model
        self.store = store
        self.project = project
        self.location = location

    @property
    def model_name(self) -> str:
        return self.model

    def _endpoint(self) -> str:
        # Lyria is region-pinned and is not served from `global`; see
        # VERTEX_MEDIA_LOCATION.
        return (
            f"https://{self.location}-aiplatform.googleapis.com/v1/projects/"
            f"{self.project}/locations/{self.location}/publishers/google/models/"
            f"{self.model}:predict"
        )

    def _token(self) -> str:
        try:
            import google.auth
            from google.auth.transport.requests import Request
        except ImportError as exc:  # pragma: no cover - depends on install profile
            raise MediaUnavailableError(
                "google-auth is not installed. Install it with:\n"
                "    uv pip install -e '.[cloud]'"
            ) from exc
        try:
            credentials, _ = google.auth.default(scopes=[_SCOPE])
            credentials.refresh(Request())
        except Exception as exc:  # noqa: BLE001
            raise MediaUnavailableError(
                f"Could not get Google credentials for Lyria: {exc}"
            ) from exc
        return credentials.token

    async def score(self, prompt: str, *, name: str = "reel") -> SyntheticClip:
        if not self.project:
            raise MediaUnavailableError(
                "GOOGLE_CLOUD_PROJECT must be set to use Lyria via Vertex AI."
            )

        payload = {"instances": [{"prompt": prompt}], "parameters": {}}
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                response = await client.post(self._endpoint(), json=payload, headers=headers)
        except Exception as exc:  # noqa: BLE001
            raise MediaUnavailableError(f"Lyria call failed: {exc}") from exc

        if response.status_code != 200:
            raise MediaUnavailableError(
                f"Lyria returned HTTP {response.status_code}: {response.text[:300]}"
            )

        predictions = response.json().get("predictions") or []
        if not predictions:
            # Lyria filters prompts too, and reports it as an empty prediction
            # list rather than an error status.
            raise MediaUnavailableError(
                "Lyria returned no audio. The prompt was most likely filtered."
            )

        encoded = predictions[0].get("bytesBase64Encoded")
        if not encoded:
            raise MediaUnavailableError("Lyria returned a prediction with no audio bytes.")
        data = base64.b64decode(encoded)
        mime = predictions[0].get("mimeType") or "audio/wav"

        stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        suffix = "wav" if "wav" in mime else "mp3"
        key = f"{SYNTHETIC_PREFIX}score/{stamp}-{name}.{suffix}"
        await self.store.put(key, data, content_type=mime)
        log.info("Scored reel", extra={"key": key, "bytes": len(data)})

        clip = SyntheticClip(
            key=key,
            mime_type=mime,
            model_name=self.model,
            prompt=prompt,
            size_bytes=len(data),
        )
        await write_manifest(self.store, clip)
        return clip
