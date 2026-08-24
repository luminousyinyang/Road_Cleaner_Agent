"""Drawing the detection box onto a still.

The case page overlays boxes on the `<video>` with CSS while a run streams, and
that is the right way to do it there -- it costs nothing and it moves with the
footage. This module is for the other case: one saved picture, with the box
burned in, that survives being downloaded, screenshotted or pasted into a
report. A box that only exists as a `<div>` is not evidence of anything once it
leaves the page.

Deliberately Pillow-only. Pillow is already a hard dependency for frame
rendering and perceptual hashing, so this adds nothing to the image.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

from road_cleaner.domain.models import BoundingBox

# Same red the dashboard uses for an open detection.
STROKE = (226, 98, 43)
LABEL_TEXT = (255, 255, 255)

# Scaled from the image's own width so a box drawn on a 1920px still and one
# drawn on a 640px still look like the same annotation.
STROKE_FRACTION = 0.0035
MIN_STROKE = 2


def draw_box(jpeg: bytes, box: BoundingBox | None, label: str = "") -> bytes:
    """Return the image with `box` drawn on it, as JPEG bytes.

    A missing box returns the image untouched rather than raising: the caller
    is saving a picture, and a picture without an annotation is still worth
    more than an exception.
    """
    image = Image.open(io.BytesIO(jpeg)).convert("RGB")
    if box is None:
        return _encode(image)

    width, height = image.size
    stroke = max(MIN_STROKE, round(width * STROKE_FRACTION))
    left, top = box.x * width, box.y * height
    right, bottom = (box.x + box.width) * width, (box.y + box.height) * height

    draw = ImageDraw.Draw(image)
    draw.rectangle((left, top, right, bottom), outline=STROKE, width=stroke)

    if label:
        _draw_label(draw, label, left, top, stroke, width, height)
    return _encode(image)


def _draw_label(draw, label: str, left: float, top: float, stroke: int, width, height) -> None:
    """Caption the box, above it if there is room and inside it if there is not."""
    font = _font(width)
    text_box = draw.textbbox((0, 0), label, font=font)
    text_w, text_h = text_box[2] - text_box[0], text_box[3] - text_box[1]
    pad = max(3, stroke)

    box_h = text_h + 2 * pad
    # Above the box normally; a hazard detected at the very top of the frame
    # would otherwise have its caption drawn off the image entirely.
    y = top - box_h if top - box_h >= 0 else top
    x = min(max(0.0, left), max(0.0, width - text_w - 2 * pad))

    draw.rectangle((x, y, x + text_w + 2 * pad, y + box_h), fill=STROKE)
    draw.text((x + pad, y + pad - text_box[1]), label, fill=LABEL_TEXT, font=font)


def _font(image_width: int):
    """A legible font at whatever size the image is, falling back to the default.

    Pillow's bundled bitmap font is fixed at about 11px, which is unreadable on
    a 1920px frame, so a real TrueType face is tried first.
    """
    size = max(12, round(image_width * 0.018))
    for candidate in (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",  # macOS
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Debian, the image
    ):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _encode(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=88)
    return buffer.getvalue()
