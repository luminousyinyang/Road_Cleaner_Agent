"""Synthetic traffic-camera stills.

Without DOT API keys there are no real camera images, and a pipeline that
analyses nothing proves nothing. So this draws them: a road in perspective, lane
markings, sky, a timestamp burn-in like real DOT cameras have, and -- when the
scenario calls for it -- a hazard sitting in a lane.

This matters more than it sounds. It means evidence frames are real JPEGs that
can be stored, hashed, served, re-fetched and compared. The perceptual-hash
diffing is exercised for real, the before/after clearance pair is genuinely two
different images, and the dashboard shows photographs instead of grey rectangles.

Rendering is deterministic: the same camera and timestamp always produce the
same image, so a re-run of a demo produces a byte-identical result.
"""

from __future__ import annotations

import io
import math
import random
from dataclasses import dataclass
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw

from road_cleaner.domain.enums import HazardType
from road_cleaner.domain.models import BoundingBox

WIDTH, HEIGHT = 640, 360
HORIZON_Y = 120

# Time-of-day palettes. Night frames are genuinely grainier and lower contrast,
# which is why the scripted analyzer is less confident about them -- the same
# way a real model would be.
PALETTES = {
    "day": {"sky": (150, 178, 202), "road": (86, 86, 90), "ground": (108, 116, 92)},
    "dusk": {"sky": (176, 142, 122), "road": (68, 66, 72), "ground": (84, 86, 72)},
    "night": {"sky": (28, 30, 40), "road": (44, 44, 50), "ground": (34, 38, 34)},
    "rain": {"sky": (128, 132, 138), "road": (74, 76, 84), "ground": (86, 94, 84)},
}

HAZARD_COLORS = {
    HazardType.DEBRIS: (34, 32, 30),
    HazardType.STALLED_VEHICLE: (168, 172, 178),
    HazardType.UNREPORTED_CLOSURE: (222, 108, 40),
    HazardType.FLOODING: (96, 118, 140),
    HazardType.INFRASTRUCTURE_DAMAGE: (150, 148, 140),
    HazardType.ANIMAL: (122, 96, 68),
    HazardType.PEDESTRIAN_ON_HIGHWAY: (200, 90, 70),
}

# Where in the frame each lane sits, as a fraction of width.
LANE_X = {
    "left_shoulder": 0.20,
    "lane_1": 0.34,
    "lane_2": 0.50,
    "lane_3": 0.66,
    "right_shoulder": 0.80,
    "median": 0.12,
    "median_barrier": 0.12,
    "intersection": 0.50,
    "all_lanes": 0.50,
    "unknown": 0.50,
}


@dataclass
class SceneSpec:
    """Everything needed to draw one frame."""

    camera_id: str
    label: str
    timestamp_text: str
    lighting: str = "day"
    traffic_density: int = 6
    hazard: HazardType | None = None
    hazard_lane: str = "lane_2"
    seed: int = 0


def _perspective_x(x_fraction: float, y: float) -> float:
    """Converge lanes toward a vanishing point as they recede."""
    depth = (y - HORIZON_Y) / (HEIGHT - HORIZON_Y)
    depth = max(0.0, min(1.0, depth))
    return WIDTH * (0.5 + (x_fraction - 0.5) * depth)


def _lane_width(y: float) -> float:
    depth = max(0.02, (y - HORIZON_Y) / (HEIGHT - HORIZON_Y))
    return 26 * depth


def render(spec: SceneSpec) -> tuple[bytes, BoundingBox | None]:
    """Draw the scene. Returns JPEG bytes and the hazard's box, if any."""
    rng = random.Random(f"{spec.camera_id}:{spec.seed}")
    palette = PALETTES.get(spec.lighting, PALETTES["day"])

    image = Image.new("RGB", (WIDTH, HEIGHT), palette["sky"])
    draw = ImageDraw.Draw(image)

    # Ground and road surface.
    draw.rectangle([0, HORIZON_Y, WIDTH, HEIGHT], fill=palette["ground"])
    draw.polygon(
        [
            (_perspective_x(0.12, HEIGHT), HEIGHT),
            (_perspective_x(0.88, HEIGHT), HEIGHT),
            (WIDTH * 0.53, HORIZON_Y),
            (WIDTH * 0.47, HORIZON_Y),
        ],
        fill=palette["road"],
    )

    _draw_lane_markings(draw, palette, spec)
    _draw_traffic(draw, rng, spec)

    box: BoundingBox | None = None
    if spec.hazard is not None:
        box = _draw_hazard(draw, spec)

    # Grain goes on before the burn-in overlay, so the timestamp stays crisp the
    # way it does on a real camera that draws text after capture.
    image = _apply_grain(image, spec)
    _draw_overlay(ImageDraw.Draw(image), spec)

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=82)
    return buffer.getvalue(), box


def _draw_lane_markings(draw: ImageDraw.ImageDraw, palette: dict, spec: SceneSpec) -> None:
    edge = (232, 228, 210) if spec.lighting != "night" else (170, 168, 156)
    for lane_fraction in (0.13, 0.87):
        draw.line(
            [
                (_perspective_x(lane_fraction, HEIGHT), HEIGHT),
                (_perspective_x(lane_fraction, HORIZON_Y + 2), HORIZON_Y + 2),
            ],
            fill=edge,
            width=2,
        )
    # Dashed centre lines, spaced by depth so they look like they recede.
    for divider in (0.42, 0.58):
        y = HEIGHT
        while y > HORIZON_Y + 6:
            depth = (y - HORIZON_Y) / (HEIGHT - HORIZON_Y)
            dash = max(2, 16 * depth)
            gap = max(3, 22 * depth)
            draw.line(
                [
                    (_perspective_x(divider, y), y),
                    (_perspective_x(divider, y - dash), y - dash),
                ],
                fill=(226, 208, 120) if spec.lighting != "night" else (150, 140, 90),
                width=max(1, int(3 * depth)),
            )
            y -= dash + gap


def _draw_traffic(draw: ImageDraw.ImageDraw, rng: random.Random, spec: SceneSpec) -> None:
    for _ in range(spec.traffic_density):
        lane = rng.choice([0.34, 0.50, 0.66])
        y = rng.uniform(HORIZON_Y + 30, HEIGHT - 20)
        depth = (y - HORIZON_Y) / (HEIGHT - HORIZON_Y)
        w = max(4, 40 * depth)
        h = max(3, 26 * depth)
        x = _perspective_x(lane, y)
        color = rng.choice([(180, 182, 186), (60, 62, 70), (140, 40, 40), (40, 70, 120)])
        draw.rectangle([x - w / 2, y - h, x + w / 2, y], fill=color)
        if spec.lighting in ("night", "dusk"):
            draw.ellipse([x - w / 2, y - h / 3, x - w / 4, y], fill=(240, 210, 140))
            draw.ellipse([x + w / 4, y - h / 3, x + w / 2, y], fill=(240, 210, 140))


def _draw_hazard(draw: ImageDraw.ImageDraw, spec: SceneSpec) -> BoundingBox:
    """Draw the hazard and return where it landed, in frame fractions."""
    lane_fraction = LANE_X.get(spec.hazard_lane, 0.5)
    y = HEIGHT * 0.72
    x = _perspective_x(lane_fraction, y)
    color = HAZARD_COLORS[spec.hazard]
    lane_w = _lane_width(y)

    if spec.hazard is HazardType.STALLED_VEHICLE:
        w, h = lane_w * 1.6, lane_w * 1.1
        draw.rectangle([x - w / 2, y - h, x + w / 2, y], fill=color)
        draw.rectangle([x - w / 2, y - h, x + w / 2, y - h * 0.55], fill=(90, 96, 104))
    elif spec.hazard is HazardType.FLOODING:
        w, h = lane_w * 3.2, lane_w * 0.9
        draw.ellipse([x - w / 2, y - h, x + w / 2, y], fill=color)
    elif spec.hazard is HazardType.UNREPORTED_CLOSURE:
        w, h = lane_w * 0.5, lane_w * 1.2
        for offset in (-lane_w, 0, lane_w):
            draw.polygon(
                [(x + offset, y - h), (x + offset - w / 2, y), (x + offset + w / 2, y)],
                fill=color,
            )
        w = lane_w * 2.6
    elif spec.hazard is HazardType.ANIMAL:
        w, h = lane_w * 0.7, lane_w * 0.9
        draw.ellipse([x - w / 2, y - h, x + w / 2, y - h * 0.3], fill=color)
        draw.rectangle([x - w / 6, y - h * 0.4, x + w / 6, y], fill=color)
    elif spec.hazard is HazardType.PEDESTRIAN_ON_HIGHWAY:
        w, h = lane_w * 0.35, lane_w * 1.3
        draw.ellipse([x - w / 2, y - h, x + w / 2, y - h * 0.72], fill=color)
        draw.rectangle([x - w / 3, y - h * 0.72, x + w / 3, y], fill=color)
    elif spec.hazard is HazardType.INFRASTRUCTURE_DAMAGE:
        w, h = lane_w * 2.0, lane_w * 0.5
        draw.line([(x - w / 2, y - h), (x + w / 2, y - h * 0.2)], fill=color, width=4)
        draw.line([(x, y - h * 0.6), (x + w / 3, y + h)], fill=color, width=3)
    else:  # debris
        w, h = lane_w * 0.9, lane_w * 0.55
        draw.ellipse([x - w / 2, y - h, x + w / 2, y], fill=color)
        draw.ellipse([x - w / 3, y - h * 0.8, x + w / 4, y - h * 0.2], fill=(58, 54, 50))

    pad = 6
    return BoundingBox(
        x=max(0.0, (x - w / 2 - pad) / WIDTH),
        y=max(0.0, (y - h - pad) / HEIGHT),
        width=min(1.0, (w + pad * 2) / WIDTH),
        height=min(1.0, (h + pad * 2) / HEIGHT),
    )


# How strong the sensor noise is, per lighting condition. Night frames are
# genuinely grainier, which is why the analyzer trusts them less.
GRAIN_AMPLITUDE = {"day": 4, "dusk": 10, "rain": 14, "night": 20}

# A small pool of pre-generated noise layers, picked by seed. Generating noise
# per frame in Python costs ~5ms and dominated the whole render; this makes it
# free after the first call while staying deterministic.
_NOISE_VARIANTS = 8


@lru_cache(maxsize=64)
def _noise_layer(amplitude: int, variant: int) -> Image.Image:
    """A reproducible noise layer centred on 128."""
    rng = random.Random(f"noise:{amplitude}:{variant}")
    raw = rng.randbytes(WIDTH * HEIGHT)
    layer = Image.frombytes("L", (WIDTH, HEIGHT), raw)
    # Compress the full 0..255 range down to 128 +/- amplitude. `point` builds a
    # 256-entry lookup table, so this runs at C speed rather than per pixel.
    return layer.point(lambda v: 128 + int((v - 128) * amplitude / 128)).convert("RGB")


def _apply_grain(image: Image.Image, spec: SceneSpec) -> Image.Image:
    amplitude = GRAIN_AMPLITUDE.get(spec.lighting, 4)
    if amplitude <= 0:
        return image
    layer = _noise_layer(amplitude, spec.seed % _NOISE_VARIANTS)
    # (image + layer) - 128, clamped: adds signed noise around zero.
    return ImageChops.add(image, layer, scale=1, offset=-128)


def _draw_overlay(draw: ImageDraw.ImageDraw, spec: SceneSpec) -> None:
    """The camera id and timestamp burn-in that real DOT cameras carry."""
    draw.rectangle([0, 0, WIDTH, 18], fill=(0, 0, 0))
    draw.text((6, 5), spec.label[:52], fill=(226, 226, 220))
    draw.rectangle([0, HEIGHT - 16, WIDTH, HEIGHT], fill=(0, 0, 0))
    draw.text((6, HEIGHT - 13), spec.timestamp_text, fill=(226, 226, 220))


def phash(image_bytes: bytes, hash_size: int = 16) -> str:
    """Perceptual hash, for spotting frames that are *identical* to the last one.

    Be precise about what this does, because it is tempting to expect far more
    of it than it can deliver. Measured on the fixture cameras:

        identical frame            -> distance 0
        traffic moved, no hazard   -> large
        hazard appeared, same cars -> ~0

    In other words: **frame differencing cannot see a hazard.** A shed tire is a
    few hundred pixels of a 640x360 frame, and ordinary traffic movement swamps
    it completely. Any scheme that skips "unchanged" frames on the strength of a
    hash and calls that hazard filtering is silently throwing away the exact
    frames the system exists to find.

    So this is used for one narrow, honest purpose: detecting a frame that is
    *identical* to the previous one, which means a frozen feed or a camera
    serving a cached image. Threshold 0. Nothing else.

    Even that needs a safety net, because a genuinely static scene would
    otherwise be skipped forever -- see `max_consecutive_skips` in the Watcher,
    which forces a look every so often regardless.

    The real cost reduction comes from the prefilter downstream. This just stops
    a dead camera from billing us.
    """
    with Image.open(io.BytesIO(image_bytes)) as img:
        small = img.convert("L").resize((hash_size, hash_size), Image.Resampling.LANCZOS)
        pixels = list(small.getdata())
    average = sum(pixels) / len(pixels)
    bits = "".join("1" if p > average else "0" for p in pixels)
    return f"{int(bits, 2):0{hash_size * hash_size // 4}x}"


def hamming_distance(a: str, b: str) -> int:
    """How many bits differ between two perceptual hashes."""
    if len(a) != len(b):
        return max(len(a), len(b)) * 4
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def lighting_for_hour(hour: int) -> str:
    """Pick a palette from the time of day, so a soak run looks like a day passing."""
    if 6 <= hour < 18:
        return "day"
    if 18 <= hour < 20 or 5 <= hour < 6:
        return "dusk"
    return "night"


def traffic_for_hour(hour: int) -> int:
    """Rush hours are busy, 3am is not."""
    return int(3 + 7 * max(0.0, math.sin((hour - 5) / 24 * 2 * math.pi)))
