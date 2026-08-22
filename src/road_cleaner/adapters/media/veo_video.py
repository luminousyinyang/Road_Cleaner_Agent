"""Veo on Vertex AI.

The one place in this codebase that deals with a long-running operation. Every
other model call is a single awaited request/response; `generate_videos` returns
an operation handle that has to be polled until it finishes, which for an 8s clip
is tens of seconds to a couple of minutes.

That cost shape is why generation is confined to `road-cleaner simulate` and
never runs inside the polling loop or on a web request. It is also why the
timeout is a setting rather than a constant -- an operation that never completes
must not hang a CLI command forever.

Region matters here. Veo is not available at `global`; it was verified in
us-central1, which is what `VERTEX_MEDIA_LOCATION` defaults to. That setting is
deliberately separate from `GOOGLE_CLOUD_LOCATION` so Gemini can stay on
`global`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from road_cleaner.adapters.media.manifest import prune_superseded, write_manifest
from road_cleaner.adapters.media.scenario_prompt import NEGATIVE_PROMPT
from road_cleaner.logging import get_logger
from road_cleaner.ports.blob_store import BlobStore
from road_cleaner.ports.media import (
    SYNTHETIC_PREFIX,
    MediaUnavailableError,
    SyntheticClip,
)

log = get_logger(__name__)

# How often to ask whether the operation is done. Veo takes tens of seconds at
# minimum, so polling faster than this just burns API calls for no benefit.
_POLL_INTERVAL_SECONDS = 10.0


class VeoVideoSynthesizer:
    def __init__(
        self,
        *,
        model: str,
        store: BlobStore,
        project: str | None = None,
        location: str = "us-central1",
        use_vertex: bool = True,
        api_key: str | None = None,
        timeout_seconds: int = 300,
        generate_audio: bool = True,
        resolution: str = "1080p",
        # Veo 3 rejects False outright; see the note in config.
        enhance_prompt: bool = True,
        prune_previous: bool = True,
    ) -> None:
        self.model = model
        self.store = store
        self.timeout_seconds = timeout_seconds
        self.resolution = resolution
        self.enhance_prompt = enhance_prompt
        # Keep only the newest clip per case. Superseded renders are pure cost:
        # they are regenerable by definition, and iterating on a prompt otherwise
        # leaves 20MB of dead video behind each time.
        self.prune_previous = prune_previous
        # Veo 3 renders diegetic audio with the clip -- engine noise, sirens,
        # tyre roar. That is what makes a generated dashcam clip usable; Lyria
        # writes music and cannot do this.
        self.generate_audio = generate_audio
        self._client = None
        self._config = {
            "project": project,
            "location": location,
            "use_vertex": use_vertex,
            "api_key": api_key,
        }

    @property
    def model_name(self) -> str:
        return self.model

    def _get_client(self):
        """Build the genai client on first use.

        Same construction as `GeminiVisionAnalyzer._get_client`, but pinned to the
        media region.
        """
        if self._client is not None:
            return self._client
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - depends on install profile
            raise MediaUnavailableError(
                "google-genai is not installed. Install it with:\n"
                "    uv pip install -e '.[cloud]'"
            ) from exc

        cfg = self._config
        if cfg["use_vertex"]:
            if not cfg["project"]:
                raise MediaUnavailableError(
                    "GOOGLE_CLOUD_PROJECT must be set to use Veo via Vertex AI."
                )
            self._client = genai.Client(
                vertexai=True, project=cfg["project"], location=cfg["location"]
            )
        else:
            if not cfg["api_key"]:
                raise MediaUnavailableError(
                    "GOOGLE_API_KEY must be set to use Veo without Vertex."
                )
            self._client = genai.Client(api_key=cfg["api_key"])
        return self._client

    async def render_scenario(
        self,
        *,
        prompt: str,
        seed_image: bytes | None = None,
        duration_seconds: int = 8,
        frame_id: str | None = None,
        case_id: str | None = None,
    ) -> SyntheticClip:
        client = self._get_client()
        from google.genai import types

        config = types.GenerateVideosConfig(
            number_of_videos=1,
            duration_seconds=duration_seconds,
            aspect_ratio="16:9",
            resolution=self.resolution,
            generate_audio=self.generate_audio,
            # The dedicated field, not the same words buried in the prompt. In the
            # prompt text "no collision" reads to the model as a mention of a
            # collision; here it is an actual exclusion.
            negative_prompt=NEGATIVE_PROMPT,
            # Not a knob we actually get: Veo 3 fails the request with "Veo 3
            # prompt enhancement cannot be disabled." Its rewrite is what turned
            # "a strip of tyre tread" into a wall of debris, so the size anchors
            # in the prompt and the negative_prompt above are what hold it back.
            enhance_prompt=self.enhance_prompt,
            # These are real road scenes; we want the hazard, not the bystanders.
            person_generation="dont_allow",
        )
        # `source=` rather than the prompt/image keywords, which the SDK
        # deprecated -- they still work but warn, and are slated for removal.
        source = types.GenerateVideosSource(
            prompt=prompt,
            image=(
                types.Image(image_bytes=seed_image, mime_type="image/jpeg")
                if seed_image is not None
                else None
            ),
        )

        try:
            operation = await client.aio.models.generate_videos(
                model=self.model, source=source, config=config
            )
        except Exception as exc:  # noqa: BLE001 - every failure is "no clip"
            # Veo's per-minute quota is small and generating a handful of clips
            # back to back will hit it. Saying so plainly beats a raw 429 dump,
            # because the fix is simply to wait rather than to change anything.
            if "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc):
                raise MediaUnavailableError(
                    "Veo is rate limited (429). The per-minute quota is small; "
                    "wait a minute and run this again."
                ) from exc
            raise MediaUnavailableError(f"Veo call failed: {exc}") from exc

        operation = await self._await_operation(client, operation)
        video = self._extract_video(operation)
        data = video.video_bytes
        if not data:
            # Set output_gcs_uri and the bytes come back as a URI instead. We do
            # not set it, so this means something changed underneath us.
            raise MediaUnavailableError(
                f"Veo returned no inline video bytes (uri={video.uri!r})."
            )

        key = _clip_key(case_id=case_id, model=self.model)
        await self.store.put(key, data, content_type="video/mp4")
        log.info(
            "Rendered synthetic scenario",
            extra={"key": key, "model": self.model, "bytes": len(data)},
        )
        clip = SyntheticClip(
            key=key,
            mime_type="video/mp4",
            model_name=self.model,
            prompt=prompt,
            seeded_from_frame_id=frame_id,
            seeded_from_case_id=case_id,
            duration_seconds=duration_seconds,
            size_bytes=len(data),
        )
        await write_manifest(self.store, clip)
        if self.prune_previous:
            # Only within this case's own folder, so rendering GA-4462 never
            # touches NC-1171's clip.
            await prune_superseded(self.store, key, _case_prefix(case_id))
        return clip

    async def _await_operation(self, client, operation):
        """Poll until the operation finishes, the deadline passes, or it errors."""
        waited = 0.0
        while not operation.done:
            if waited >= self.timeout_seconds:
                raise MediaUnavailableError(
                    f"Veo operation {operation.name} did not finish within "
                    f"{self.timeout_seconds}s. It may still complete server-side."
                )
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            waited += _POLL_INTERVAL_SECONDS
            try:
                operation = await client.aio.operations.get(operation)
            except Exception as exc:  # noqa: BLE001
                raise MediaUnavailableError(f"Polling Veo failed: {exc}") from exc

        if operation.error:
            raise MediaUnavailableError(f"Veo operation failed: {operation.error}")
        return operation

    @staticmethod
    def _extract_video(operation):
        """Pull the one video out, distinguishing "refused" from "broken".

        Safety filtering is the likely failure for this project specifically --
        crash and debris scenes sit near the line. It reports as an empty result
        set with a reason, so it needs saying out loud rather than surfacing as
        an IndexError.
        """
        response = operation.response or operation.result
        if response is None:
            raise MediaUnavailableError("Veo operation finished with no response.")

        filtered = getattr(response, "rai_media_filtered_count", 0) or 0
        videos = getattr(response, "generated_videos", None) or []
        if not videos:
            if filtered:
                reasons = getattr(response, "rai_media_filtered_reasons", None) or []
                raise MediaUnavailableError(
                    f"Veo filtered all {filtered} generated video(s): "
                    f"{'; '.join(str(r) for r in reasons) or 'no reason given'}. "
                    "Try describing the hazard more plainly and less dramatically."
                )
            raise MediaUnavailableError("Veo returned no videos and no filter reason.")

        video = videos[0].video
        if video is None:
            raise MediaUnavailableError("Veo returned an empty video object.")
        return video


def _case_prefix(case_id: str | None) -> str:
    """The case's own folder, with a trailing slash.

    The slash is load-bearing on GCS, where `list_blobs(prefix=...)` matches raw
    strings rather than paths: a bare "synthetic/GA-446" would sweep up GA-4462
    and GA-4460 alike, and pruning one case would delete another's clip.
    """
    return f"{SYNTHETIC_PREFIX}{(case_id or 'scenario').replace('/', '-')}/"


def _clip_key(*, case_id: str | None, model: str) -> str:
    """Build the blob key.

    Always under SYNTHETIC_PREFIX -- that prefix is what the web layer and the
    evidence checks key off to know this is generated, so it is not optional and
    not caller-supplied.
    """
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    slug = (case_id or "scenario").replace("/", "-")
    return f"{SYNTHETIC_PREFIX}{slug}/{stamp}-{model}.mp4"
