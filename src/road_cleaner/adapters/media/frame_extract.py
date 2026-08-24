"""Pulling stills out of a generated clip so the analyst can look at them.

The vision model reads images, not video. To run the real detection pipeline
over a Veo clip we have to decode it into frames first, and the case page needs
each frame to carry the timestamp it came from -- clicking a result seeks the
`<video>` to that moment, so a frame whose time is approximate lands the viewer
somewhere other than the thing being pointed at.

ffmpeg comes from `imageio-ffmpeg`, which ships a static binary as part of the
wheel. That is the whole reason for the dependency: no `apt-get` in the
Dockerfile, and `make setup` on a clean clone still works.

Nothing here touches the evidence store. Clips live under `SYNTHETIC_PREFIX`
and the frames pulled from them are never written back as `Frame` rows -- they
are inputs to a demonstration, not records of anything that happened on a road.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path

# A frame that will not decode inside this long is a broken file, not a slow
# one. Without a ceiling a corrupt clip hangs the request that asked for it.
DECODE_TIMEOUT_SECONDS = 20.0

# Veo clips are 8s at 24fps. Sampling the very first and last frames is a waste
# of two of our five calls: the first is often still fading in, and the hazard
# has usually passed under the bumper by the last. So the window is inset.
EDGE_INSET_SECONDS = 0.4


class FrameExtractionError(RuntimeError):
    """The clip could not be decoded. Raised rather than returning no frames.

    An empty list is indistinguishable from "the model found nothing", and the
    difference between a broken file and a clean road matters enormously to
    someone reading the result.
    """


@dataclass(frozen=True)
class SampledFrame:
    """One still, and exactly where in the clip it came from."""

    index: int
    at_seconds: float
    jpeg: bytes

    @property
    def stamp(self) -> str:
        """How the time is shown in the UI, e.g. '3.2s'."""
        return f"{self.at_seconds:.1f}s"


def ffmpeg_path() -> str:
    """The ffmpeg binary to use: the bundled one, else whatever is on PATH."""
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
    except ImportError:  # pragma: no cover - the dependency is not optional
        found = shutil.which("ffmpeg")
        if found:
            return found
        raise FrameExtractionError(
            "ffmpeg is unavailable. Install it with: uv pip install imageio-ffmpeg"
        ) from None
    return get_ffmpeg_exe()


def sample_times(duration: float, count: int) -> list[float]:
    """`count` timestamps spread evenly across a clip, inset from both ends.

    Evenly spaced rather than clustered: a hazard that is only visible for part
    of the clip should be found by some of the samples and missed by others,
    because that disagreement is real information about how confident to be.
    """
    if count < 1:
        return []
    span = max(0.0, duration - 2 * EDGE_INSET_SECONDS)
    if span <= 0 or count == 1:
        return [max(0.0, duration / 2)]
    step = span / (count - 1)
    return [round(EDGE_INSET_SECONDS + i * step, 3) for i in range(count)]


async def probe_duration(path: Path) -> float:
    """How long the clip runs, in seconds, according to the file itself.

    Read from the container rather than from the generation sidecar: Veo is
    asked for 8 seconds and returns approximately 8 seconds, and seeking to a
    timestamp past the end yields no frame at all.
    """
    proc = await asyncio.create_subprocess_exec(
        ffmpeg_path(), "-i", str(path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await asyncio.wait_for(proc.communicate(), timeout=DECODE_TIMEOUT_SECONDS)

    # `ffmpeg -i` with no output file always exits non-zero; the duration is in
    # the banner it prints on the way out, as "Duration: 00:00:08.02,".
    for line in err.decode("utf-8", "replace").splitlines():
        stripped = line.strip()
        if not stripped.startswith("Duration:"):
            continue
        clock = stripped.split("Duration:", 1)[1].split(",", 1)[0].strip()
        try:
            hours, minutes, seconds = clock.split(":")
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        except ValueError:
            break
    # Reached when the file exists but is not a playable video -- a truncated
    # download, a placeholder, or a render that failed and left a stub behind.
    # Naming ffmpeg here would tell the reader which library was disappointed
    # rather than what is wrong with their clip.
    raise FrameExtractionError(
        f"{path.name} is not a readable video. It may be a truncated or failed "
        f"render — generating the clip again should replace it."
    )


async def extract_frame(path: Path, at_seconds: float) -> bytes:
    """One JPEG from one moment in the clip."""
    proc = await asyncio.create_subprocess_exec(
        ffmpeg_path(),
        # -ss before -i seeks by keyframe index instead of decoding up to the
        # timestamp: on an 8s clip the difference is a tenth of a second of
        # accuracy against several seconds of decoding, five times over.
        "-ss", f"{max(0.0, at_seconds):.3f}",
        "-i", str(path),
        "-frames:v", "1",
        "-q:v", "3",           # visibly lossless at this size; ~200KB a frame
        "-f", "image2",
        "-vcodec", "mjpeg",
        "-loglevel", "error",
        "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(), timeout=DECODE_TIMEOUT_SECONDS
        )
    except TimeoutError:
        proc.kill()
        raise FrameExtractionError(
            f"Decoding {path.name} at {at_seconds:.1f}s took longer than "
            f"{DECODE_TIMEOUT_SECONDS:.0f}s"
        ) from None

    if not out.startswith(b"\xff\xd8"):
        detail = err.decode("utf-8", "replace").strip().splitlines()
        raise FrameExtractionError(
            f"No frame at {at_seconds:.1f}s in {path.name}"
            + (f": {detail[-1]}" if detail else "")
        )
    return out


async def sample_clip(path: Path, count: int = 5) -> list[SampledFrame]:
    """`count` stills spread across the clip, in order.

    Extracted concurrently -- they are independent decodes of the same file, and
    doing them one at a time makes the page wait several seconds before the
    analysis it is meant to be streaming can even start.
    """
    path = Path(path)
    if not path.is_file():
        raise FrameExtractionError(f"No clip at {path}")

    duration = await probe_duration(path)
    times = sample_times(duration, count)
    jpegs = await asyncio.gather(*(extract_frame(path, t) for t in times))
    return [
        SampledFrame(index=i, at_seconds=t, jpeg=jpeg)
        for i, (t, jpeg) in enumerate(zip(times, jpegs, strict=True))
    ]
