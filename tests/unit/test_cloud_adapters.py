"""Cloud adapters, exercised as far as they go without credentials.

These cannot be tested against real GCP here, and pretending otherwise with a
wall of SDK mocks would prove nothing. What *is* worth testing is the part that
bites in practice: that selecting a cloud adapter without the credentials it
needs produces a sentence a person can act on, rather than an ImportError at
startup or an AttributeError halfway through a run.

The response parsing in the 511 client is tested properly, against captured
payload shapes, because that is real logic.
"""

from __future__ import annotations

import pytest

from road_cleaner.adapters.camera.rate_limit import RateLimiter, StateRateLimiters
from road_cleaner.adapters.camera.vendor511 import Vendor511CameraSource, _parse_time
from road_cleaner.adapters.vision.gemini_vision import _box_for, _parse_json
from road_cleaner.config import Settings
from road_cleaner.container import build_container
from road_cleaner.domain.enums import CameraTier


class TestImportsAreLazy:
    """A local run must never need google-cloud-* installed."""

    @pytest.mark.parametrize(
        "module",
        [
            "road_cleaner.adapters.repo.firestore_repo",
            "road_cleaner.adapters.blobs.gcs_store",
            "road_cleaner.adapters.bus.pubsub_bus",
            "road_cleaner.adapters.vision.gemini_vision",
            "road_cleaner.adapters.reasoning.adk_reasoner",
            "road_cleaner.agents.coordinator",
        ],
    )
    def test_module_imports_without_cloud_extras(self, module):
        __import__(module)

    def test_the_adk_root_agent_can_be_imported_without_building(self):
        """`adk web` imports this; so does the test suite. Neither has creds."""
        from road_cleaner.agents.coordinator import root_agent

        assert root_agent is not None


class TestMisconfigurationIsExplained:
    def test_gcs_without_a_bucket_says_so(self):
        from road_cleaner.adapters.blobs.gcs_store import GcsBlobStore

        with pytest.raises(ValueError, match="GCS_BUCKET"):
            GcsBlobStore(bucket=None)

    def test_firestore_without_a_project_says_so(self):
        from road_cleaner.adapters.repo.firestore_repo import FirestoreCaseRepository

        with pytest.raises(ValueError, match="GOOGLE_CLOUD_PROJECT"):
            FirestoreCaseRepository(project=None)

    def test_pubsub_without_a_project_says_so(self):
        from road_cleaner.adapters.bus.pubsub_bus import PubSubEventBus

        with pytest.raises(ValueError, match="GOOGLE_CLOUD_PROJECT"):
            PubSubEventBus(project=None, topics={})

    async def test_gemini_without_a_project_says_so(self):
        """Whichever prerequisite is missing, the message names it.

        Depending on the install profile this is either "google-genai is not
        installed" or "GOOGLE_CLOUD_PROJECT must be set" -- both are things a
        person can act on, which is the actual requirement.
        """
        from road_cleaner.adapters.vision.gemini_vision import (
            GeminiVisionAnalyzer,
            VisionUnavailableError,
        )

        analyzer = GeminiVisionAnalyzer(model="gemini-2.5-flash", project=None, use_vertex=True)
        with pytest.raises(VisionUnavailableError) as exc:
            analyzer._get_client()

        message = str(exc.value)
        assert "GOOGLE_CLOUD_PROJECT" in message or "google-genai" in message
        assert "uv pip install" in message or "must be set" in message

    def test_doctor_lists_what_is_missing_for_cloud(self, tmp_path):
        settings = Settings(
            ROAD_CLEANER_MODE="cloud",
            DATA_DIR=str(tmp_path),
            GOOGLE_CLOUD_PROJECT=None,
        )
        missing = settings.missing_for_live()
        assert any("GOOGLE_CLOUD_PROJECT" in m for m in missing)
        assert any("511_API_KEY" in m for m in missing)

    def test_local_mode_needs_nothing(self, tmp_path):
        settings = Settings(ROAD_CLEANER_MODE="local", DATA_DIR=str(tmp_path))
        assert settings.missing_for_live() == []


class TestModeSelection:
    def test_local_mode_picks_local_adapters(self, tmp_path):
        settings = Settings(ROAD_CLEANER_MODE="local", DATA_DIR=str(tmp_path))
        wiring = build_container(settings).describe()
        assert wiring["repository"] == "SqliteCaseRepository"
        assert wiring["cameras"] == "FixtureCameraSource"
        assert wiring["vision"] == "ScriptedVisionAnalyzer"
        assert wiring["bus"] == "InMemoryEventBus"

    def test_dry_run_wraps_the_real_channel(self, tmp_path):
        """The artifact must come from the real compose path, not a stub."""
        settings = Settings(ROAD_CLEANER_MODE="local", DRY_RUN=True, DATA_DIR=str(tmp_path))
        assert build_container(settings).describe()["filing"].startswith("dry_run(")

    def test_dry_run_stays_on_by_default_even_in_cloud_mode(self, tmp_path):
        settings = Settings(
            ROAD_CLEANER_MODE="cloud",
            DATA_DIR=str(tmp_path),
            CAMERA_SOURCE="fixture",
            REPOSITORY="sqlite",
            BLOB_STORE="local",
            EVENT_BUS="memory",
            VISION_PROVIDER="scripted",
        )
        assert build_container(settings).describe()["filing"].startswith("dry_run(")


class TestVendor511Parsing:
    """Field names vary between deployments of the same platform."""

    def test_parses_a_standard_row(self):
        camera = Vendor511CameraSource._parse_camera(
            "GA",
            {
                "Id": "GDOT-CCTV-0447",
                "Latitude": 33.6407,
                "Longitude": -84.4277,
                "ImageUrl": "https://cdn.example/cam.jpg",
                "RoadwayName": "I-285",
                "DirectionOfTravel": "westbound",
                "Location": "Camp Creek Pkwy",
                "County": "Fulton",
                "Organization": "ga-dot-d7",
            },
        )
        assert camera is not None
        assert camera.id == "GDOT-CCTV-0447"
        assert camera.road == "I-285"
        assert camera.owner_agency_id == "ga-dot-d7"
        # Interstates get the fast polling tier.
        assert camera.tier is CameraTier.BUSY

    def test_parses_lowercase_field_names(self):
        camera = Vendor511CameraSource._parse_camera(
            "FL",
            {
                "id": "FL-1",
                "latitude": 28.3,
                "longitude": -81.4,
                "imageUrl": "https://cdn.example/a.jpg",
                "roadway": "Local Road",
            },
        )
        assert camera is not None
        assert camera.tier is CameraTier.QUIET

    @pytest.mark.parametrize(
        "row",
        [
            {"Id": "x", "Longitude": -84.0, "ImageUrl": "u"},   # no latitude
            {"Id": "x", "Latitude": 33.0, "ImageUrl": "u"},     # no longitude
            {"Id": "x", "Latitude": 33.0, "Longitude": -84.0},  # no image
            {"Latitude": 33.0, "Longitude": -84.0, "ImageUrl": "u"},  # no id
        ],
    )
    def test_unusable_rows_are_dropped_not_stored_broken(self, row):
        assert Vendor511CameraSource._parse_camera("GA", row) is None

    def test_parses_timestamps_and_tolerates_junk(self):
        assert _parse_time("2026-08-03T14:02:00Z") is not None
        assert _parse_time("not a date") is None
        assert _parse_time(None) is None


class TestRateLimiter:
    async def test_allows_calls_up_to_the_limit_without_waiting(self):
        limiter = RateLimiter(calls=5, window_seconds=60)
        for _ in range(5):
            await limiter.acquire()
        assert limiter.in_window == 5

    async def test_blocks_once_the_window_is_full(self):
        """The published throttle is 10/60s; exceeding it risks the key."""
        limiter = RateLimiter(calls=2, window_seconds=0.3)
        await limiter.acquire()
        await limiter.acquire()

        import time

        start = time.monotonic()
        await limiter.acquire()  # must wait for the window to roll
        assert time.monotonic() - start > 0.1

    async def test_states_are_limited_independently(self):
        """Each state issues its own key, so the budgets are separate."""
        limiters = StateRateLimiters(calls=1, window_seconds=60)
        await limiters.acquire("GA")
        await limiters.acquire("FL")
        assert limiters.for_state("GA").in_window == 1
        assert limiters.for_state("FL").in_window == 1


class TestGeminiResponseParsing:
    def test_parses_plain_json(self):
        assert _parse_json('{"hazard_present": true}') == {"hazard_present": True}

    def test_parses_json_in_a_markdown_fence(self):
        """Models fence their output regardless of what the prompt says."""
        assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_extracts_json_surrounded_by_prose(self):
        assert _parse_json('Sure!\n{"a": 1}\nHope that helps.') == {"a": 1}

    @pytest.mark.parametrize("junk", ["", "no json here", "[1, 2, 3]"])
    def test_unparseable_output_returns_none(self, junk):
        """Which the caller turns into a retry, never into 'no hazard'."""
        assert _parse_json(junk) is None

    def test_lane_maps_to_a_box_inside_the_frame(self):
        for lane in ("lane_1", "lane_2", "lane_3", "right_shoulder", "all_lanes", "nonsense"):
            box = _box_for(lane)
            assert box.x >= 0 and box.x + box.width <= 1.001
            assert box.y >= 0 and box.y + box.height <= 1.001
