"""The renderer and the frame-diffing hash.

The hash calibration here is load-bearing: if the threshold drifts so that a
newly-appeared hazard reads as "unchanged", the Watcher silently discards the
exact frames the whole system exists to notice. That failure would be invisible
in production -- no error, no crash, just a pipeline that never finds anything.
So it gets a test.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from road_cleaner.adapters.camera.scene import (
    HEIGHT,
    WIDTH,
    SceneSpec,
    hamming_distance,
    lighting_for_hour,
    phash,
    render,
    traffic_for_hour,
)
from road_cleaner.domain.enums import HazardType

PHASH_THRESHOLD = 1  # must match Settings.phash_identical_threshold


def spec(**kwargs) -> SceneSpec:
    defaults = dict(
        camera_id="GDOT-CCTV-0447",
        label="GDOT-CCTV-0447 I-285 westbound",
        timestamp_text="2026-08-03 14:02:00 UTC",
        lighting="day",
        traffic_density=6,
        seed=1,
    )
    return SceneSpec(**{**defaults, **kwargs})


class TestRendering:
    def test_produces_a_decodable_jpeg_of_the_right_size(self):
        data, _ = render(spec())
        with Image.open(io.BytesIO(data)) as img:
            assert img.format == "JPEG"
            assert img.size == (WIDTH, HEIGHT)

    def test_is_deterministic(self):
        """A re-run of a demo must produce byte-identical evidence."""
        assert render(spec())[0] == render(spec())[0]

    def test_clear_scene_has_no_box(self):
        _, box = render(spec())
        assert box is None

    @pytest.mark.parametrize("hazard", list(HazardType))
    def test_every_hazard_type_renders_and_returns_a_box(self, hazard):
        data, box = render(spec(hazard=hazard))
        assert len(data) > 1000
        assert box is not None
        # The box must be inside the frame, or the dashboard overlay drifts off.
        assert 0 <= box.x <= 1 and 0 <= box.y <= 1
        assert 0 < box.width <= 1 and 0 < box.height <= 1
        assert box.x + box.width <= 1.001
        assert box.y + box.height <= 1.001

    @pytest.mark.parametrize("lighting", ["day", "dusk", "night", "rain"])
    def test_every_lighting_condition_renders(self, lighting):
        data, _ = render(spec(lighting=lighting))
        assert len(data) > 1000

    def test_hazard_lands_in_the_lane_it_was_asked_for(self):
        """A box for lane 1 must sit left of a box for lane 3."""
        _, left = render(spec(hazard=HazardType.DEBRIS, hazard_lane="lane_1"))
        _, right = render(spec(hazard=HazardType.DEBRIS, hazard_lane="lane_3"))
        assert left.x < right.x


class TestPerceptualHash:
    """What the hash can and cannot do.

    These tests exist mostly to pin down a limitation, so nobody later "optimises"
    the Watcher by trusting the hash further than it deserves.
    """

    def test_identical_frames_hash_identically(self):
        a, _ = render(spec())
        b, _ = render(spec())
        assert hamming_distance(phash(a), phash(b)) == 0

    def test_a_repeat_frame_is_treated_as_unchanged(self):
        """The only saving this actually delivers: a frozen feed costs nothing."""
        a, _ = render(spec())
        b, _ = render(spec())
        assert hamming_distance(phash(a), phash(b)) <= PHASH_THRESHOLD

    def test_the_hash_cannot_see_a_hazard(self):
        """Documented limitation, asserted so it stays documented.

        A hazard appearing while traffic holds still is invisible to an average
        hash. This is why `PHASH_IDENTICAL_THRESHOLD` is 0 and why the Watcher
        caps consecutive skips -- if this were relied on as a hazard filter, the
        system would silently discard the frames it exists to analyse.
        """
        clear, _ = render(spec())
        hazard, _ = render(spec(hazard=HazardType.DEBRIS))
        assert hamming_distance(phash(clear), phash(hazard)) == 0

    def test_ordinary_traffic_movement_dominates_the_signal(self):
        """Traffic moving changes the hash far more than a hazard appearing does."""
        clear, _ = render(spec(seed=1))
        moved, _ = render(spec(seed=2))
        hazard, _ = render(spec(seed=1, hazard=HazardType.DEBRIS))
        traffic_delta = hamming_distance(phash(clear), phash(moved))
        hazard_delta = hamming_distance(phash(clear), phash(hazard))
        assert traffic_delta > hazard_delta

    def test_hash_length_is_stable(self):
        data, _ = render(spec())
        assert len(phash(data)) == len(phash(data, 16)) == 64

    def test_mismatched_hash_lengths_are_maximally_distant(self):
        """Guards against a silent comparison between two different hash sizes."""
        data, _ = render(spec())
        assert hamming_distance(phash(data, 8), phash(data, 16)) > 0


class TestTimeOfDay:
    @pytest.mark.parametrize(
        ("hour", "expected"),
        [(3, "night"), (5, "dusk"), (12, "day"), (18, "dusk"), (22, "night")],
    )
    def test_lighting_for_hour(self, hour, expected):
        assert lighting_for_hour(hour) == expected

    def test_traffic_is_heavier_by_day_than_at_night(self):
        assert traffic_for_hour(11) > traffic_for_hour(23)

    def test_traffic_is_never_negative(self):
        assert all(traffic_for_hour(h) >= 0 for h in range(24))
