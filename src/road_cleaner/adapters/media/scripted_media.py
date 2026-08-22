"""Replays media that was already generated, with no credentials and no spend.

The counterpart to `ScriptedVisionAnalyzer`: it lets the dashboard, the tests and
a fresh clone all run the simulation surface without calling Veo. This is the
default, because generation bills per second of video.

It invents nothing. If a clip has not been generated yet it says so rather than
substituting a real camera frame -- the whole separation between evidence and
synthetic media depends on those two never standing in for one another.
"""

from __future__ import annotations

from pathlib import Path

from road_cleaner.ports.media import (
    SYNTHETIC_PREFIX,
    MediaUnavailableError,
    SyntheticClip,
)

_SUFFIX_MIME = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
}


class ScriptedMediaSynthesizer:
    """Serves whatever is cached under the media root.

    Implements all three media protocols: with nothing to generate, rendering a
    scenario, narrating a briefing and scoring a reel are the same operation --
    find the newest cached file of the right kind.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @property
    def model_name(self) -> str:
        return "scripted(cached)"

    # ------------------------------------------------------------- lookup
    def _newest(self, suffixes: tuple[str, ...]) -> Path:
        base = self.root / SYNTHETIC_PREFIX
        candidates = [
            p
            for p in base.rglob("*")
            if p.is_file() and p.suffix.lower() in suffixes
        ] if base.exists() else []
        if not candidates:
            raise MediaUnavailableError(
                f"No cached media matching {', '.join(suffixes)} under {base}. "
                "Generate some first with:  road-cleaner simulate --provider vertex"
            )
        return max(candidates, key=lambda p: p.stat().st_mtime)

    def _clip(self, path: Path, prompt: str, **provenance) -> SyntheticClip:
        key = f"{SYNTHETIC_PREFIX}{path.relative_to(self.root / SYNTHETIC_PREFIX)}"
        return SyntheticClip(
            key=key,
            mime_type=_SUFFIX_MIME.get(path.suffix.lower(), "application/octet-stream"),
            # Says "replayed", not the name of a model it did not call.
            model_name=self.model_name,
            prompt=prompt,
            size_bytes=path.stat().st_size,
            **provenance,
        )

    # -------------------------------------------------------- protocols
    async def render_scenario(
        self,
        *,
        prompt: str,
        seed_image: bytes | None = None,
        duration_seconds: int = 8,
        frame_id: str | None = None,
        case_id: str | None = None,
    ) -> SyntheticClip:
        path = self._newest((".mp4", ".webm"))
        return self._clip(
            path, prompt, seeded_from_frame_id=frame_id, seeded_from_case_id=case_id
        )

    async def narrate(self, text: str, *, case_id: str | None = None) -> SyntheticClip:
        path = self._newest((".mp3", ".wav"))
        return self._clip(path, text, seeded_from_case_id=case_id)

    async def score(self, prompt: str, *, name: str = "reel") -> SyntheticClip:
        path = self._newest((".wav", ".mp3"))
        return self._clip(path, prompt)
