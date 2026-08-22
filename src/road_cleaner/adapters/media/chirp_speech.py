"""Chirp 3 HD, via the Cloud Text-to-Speech API.

Reads the dispatch briefing aloud. The written report already exists -- this is
the same text spoken, so an operator can take a hazard briefing hands-free
instead of reading a form.

Note this is a different API from everything else in the media package: Cloud
TTS, not Vertex. It has no `location` and no publisher-model path, so the region
settings that govern Veo and Lyria do not apply.
"""

from __future__ import annotations

from datetime import datetime

from road_cleaner.adapters.media.manifest import write_manifest
from road_cleaner.logging import get_logger
from road_cleaner.ports.blob_store import BlobStore
from road_cleaner.ports.media import (
    SYNTHETIC_PREFIX,
    MediaUnavailableError,
    SyntheticClip,
)

log = get_logger(__name__)

# Cloud TTS rejects anything longer, and a briefing that runs past this is not a
# briefing any more. Truncating loudly beats a 400 from the API.
_MAX_CHARS = 4800


class ChirpSpeechSynthesizer:
    def __init__(
        self,
        *,
        voice: str = "en-US-Chirp3-HD-Achernar",
        store: BlobStore,
        language_code: str = "en-US",
    ) -> None:
        self.voice = voice
        self.store = store
        self.language_code = language_code
        self._client = None

    @property
    def model_name(self) -> str:
        return self.voice

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            from google.cloud import texttospeech
        except ImportError as exc:  # pragma: no cover - depends on install profile
            raise MediaUnavailableError(
                "google-cloud-texttospeech is not installed. Install it with:\n"
                "    uv pip install -e '.[cloud]'"
            ) from exc
        self._client = texttospeech.TextToSpeechAsyncClient()
        return self._client

    async def narrate(self, text: str, *, case_id: str | None = None) -> SyntheticClip:
        if not text.strip():
            raise MediaUnavailableError("Nothing to narrate: the briefing text is empty.")

        client = self._get_client()
        from google.cloud import texttospeech

        spoken = text.strip()
        if len(spoken) > _MAX_CHARS:
            log.warning(
                "Briefing truncated for narration",
                extra={"chars": len(spoken), "limit": _MAX_CHARS},
            )
            spoken = spoken[:_MAX_CHARS]

        try:
            response = await client.synthesize_speech(
                input=texttospeech.SynthesisInput(text=spoken),
                voice=texttospeech.VoiceSelectionParams(
                    language_code=self.language_code, name=self.voice
                ),
                audio_config=texttospeech.AudioConfig(
                    audio_encoding=texttospeech.AudioEncoding.MP3
                ),
            )
        except Exception as exc:  # noqa: BLE001
            raise MediaUnavailableError(f"Chirp call failed: {exc}") from exc

        data = response.audio_content
        if not data:
            raise MediaUnavailableError("Chirp returned no audio.")

        stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        slug = (case_id or "briefing").replace("/", "-")
        key = f"{SYNTHETIC_PREFIX}{slug}/{stamp}-briefing.mp3"
        await self.store.put(key, data, content_type="audio/mpeg")
        log.info("Narrated briefing", extra={"key": key, "bytes": len(data)})

        clip = SyntheticClip(
            key=key,
            mime_type="audio/mpeg",
            model_name=self.voice,
            prompt=spoken,
            seeded_from_case_id=case_id,
            size_bytes=len(data),
        )
        await write_manifest(self.store, clip)
        return clip
