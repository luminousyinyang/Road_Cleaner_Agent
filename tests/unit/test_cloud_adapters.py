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
from road_cleaner.adapters.vision.gemini_vision import _box_from, _parse_json, _position
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


class TestPosition:
    """A coarse position, or nothing. Never a lane number.

    This value picks an agency -- `intersection` is what routes a damaged signal
    head to the city rather than the state DOT -- so a value we do not trust must
    not be able to reach the routing rules.
    """

    @pytest.mark.parametrize(
        "value", ["intersection", "left_shoulder", "right_shoulder", "median", "median_barrier"]
    )
    def test_a_position_we_accept_is_kept(self, value):
        assert _position({"position": value}) == value

    @pytest.mark.parametrize("value", ["lane_1", "lane_2", "lane_3", "all_lanes"])
    def test_a_lane_number_is_refused(self, value):
        """An old prompt, a cached reply or a model reaching for its training
        data can all still answer `lane_2`. None of them get to."""
        assert _position({"position": value}) == "unknown"

    def test_the_old_key_is_still_read(self):
        """Responses cached before the rename are still worth something."""
        assert _position({"lane_position": "median"}) == "median"

    @pytest.mark.parametrize(
        "payload", [{}, {"position": None}, {"position": ""}, {"position": "somewhere"}]
    )
    def test_anything_else_is_unknown(self, payload):
        assert _position(payload) == "unknown"

    def test_case_and_padding_are_tolerated(self):
        assert _position({"position": "  Right_Shoulder "}) == "right_shoulder"


class TestBoxFromTheModel:
    """`box_2d` is `[ymin, xmin, ymax, xmax]` on a 0-1000 grid, origin top-left.

    Two orderings to get wrong -- y before x, and 0-1000 rather than 0-1 -- and
    getting either wrong still yields a box that renders, just over the wrong
    part of the road. Hence the fixed reference case: [581, 227, 660, 452] was
    returned for a shredded tyre tread and verified by eye against the frame.
    """

    def test_the_verified_response_converts_to_the_tyre(self):
        box = _box_from({"box_2d": [581, 227, 660, 452]})
        assert (box.x, box.y) == pytest.approx((0.227, 0.581)), "x and y are not swapped"
        assert (box.width, box.height) == pytest.approx((0.225, 0.079))

    def test_reversed_corners_are_normalised_rather_than_dropped(self):
        """Models occasionally emit max before min. The box is still recoverable."""
        assert _box_from({"box_2d": [660, 452, 581, 227]}) == _box_from(
            {"box_2d": [581, 227, 660, 452]}
        )

    def test_a_box_running_off_frame_is_clamped_into_it(self):
        box = _box_from({"box_2d": [-40, 900, 500, 1400]})
        assert box.x >= 0 and box.y >= 0
        assert box.x + box.width <= 1.0 and box.y + box.height <= 1.0

    @pytest.mark.parametrize(
        "payload",
        [
            {},                                    # the model declined to place it
            {"box_2d": None},
            {"box_2d": [1, 2, 3]},                 # wrong arity
            {"box_2d": "581,227,660,452"},         # a string, not a list
            {"box_2d": ["a", "b", "c", "d"]},      # unparseable
            {"box_2d": [500, 300, 500, 300]},      # zero area
        ],
    )
    def test_nothing_usable_yields_no_box(self, payload):
        """So the caller falls back to the lane guess instead of drawing a lie."""
        assert _box_from(payload) is None


class TestVisionRetries:
    """Vertex throttles, and the analyzer has to cope rather than give up.

    Measured: a full-speed demo run with no ceiling produced 165 consecutive
    429 RESOURCE_EXHAUSTED responses and zero detections.
    """

    async def test_a_throttled_call_is_retried_and_succeeds(self, monkeypatch):
        from road_cleaner.adapters.vision import gemini_vision as gv

        calls = {"n": 0}

        class FakeModels:
            async def generate_content(self, **kw):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise RuntimeError("429 RESOURCE_EXHAUSTED")
                return type("R", (), {"text": "YES"})()

        class FakeClient:
            aio = type("A", (), {"models": FakeModels()})()

        analyzer = gv.GeminiVisionAnalyzer(model="m", project="p", max_retries=5)
        analyzer._client = FakeClient()
        monkeypatch.setattr(gv.asyncio, "sleep", lambda *_: _noop())

        assert await analyzer._generate("m", ["x"]) == "YES"
        assert calls["n"] == 3, "should have retried twice before succeeding"

    async def test_a_permanent_error_is_not_retried(self, monkeypatch):
        """Repeating a 404 just spends money on the same wrong request."""
        from road_cleaner.adapters.vision import gemini_vision as gv

        calls = {"n": 0}

        class FakeModels:
            async def generate_content(self, **kw):
                calls["n"] += 1
                raise RuntimeError("404 NOT_FOUND")

        class FakeClient:
            aio = type("A", (), {"models": FakeModels()})()

        analyzer = gv.GeminiVisionAnalyzer(model="m", project="p", max_retries=5)
        analyzer._client = FakeClient()
        monkeypatch.setattr(gv.asyncio, "sleep", lambda *_: _noop())

        with pytest.raises(gv.VisionUnavailableError):
            await analyzer._generate("m", ["x"])
        assert calls["n"] == 1, "a 404 must not be retried"

    async def test_concurrency_is_capped(self):
        """Without a ceiling the Analyst fires one call per frame, all at once."""
        import asyncio as aio

        from road_cleaner.adapters.vision import gemini_vision as gv

        peak = {"now": 0, "max": 0}

        class FakeModels:
            async def generate_content(self, **kw):
                peak["now"] += 1
                peak["max"] = max(peak["max"], peak["now"])
                await aio.sleep(0.01)
                peak["now"] -= 1
                return type("R", (), {"text": "ok"})()

        class FakeClient:
            aio = type("A", (), {"models": FakeModels()})()

        analyzer = gv.GeminiVisionAnalyzer(model="m", project="p", max_concurrency=3)
        analyzer._client = FakeClient()
        await aio.gather(*(analyzer._generate("m", ["x"]) for _ in range(20)))
        assert peak["max"] <= 3, f"ran {peak['max']} calls at once, cap was 3"


async def _noop():
    return None


class TestClearancePromptSubstitution:
    """The clearance prompt ends with a JSON example, and `str.format` read it
    as format syntax.

    `{"still_present": ...}` is a valid field name to `format`, so every
    clearance check raised `KeyError: '\\n  "still_present"'`. The Auditor's
    "is it still there?" call — the thing the whole product is built around —
    had never run successfully against a real model. The scripted analyzer does
    not implement this path, so the suite stayed green while it was broken.
    """

    def test_placeholders_are_filled(self):
        from road_cleaner.adapters.vision.gemini_vision import _fill

        out = _fill(
            "Type: {hazard_type}, lane {lane_position}: {description}",
            hazard_type="debris", lane_position="lane_1", description="tyre tread",
        )
        assert out == "Type: debris, lane lane_1: tyre tread"

    def test_json_braces_survive(self):
        from road_cleaner.adapters.vision.gemini_vision import _fill

        template = 'Hazard {hazard_type}\n\n{\n  "still_present": true\n}'
        out = _fill(template, hazard_type="debris")
        assert '"still_present": true' in out
        assert "debris" in out

    def test_the_real_prompt_renders(self):
        """Against the actual file, so a future edit that reintroduces a brace
        problem fails here rather than in production."""
        from pathlib import Path

        from road_cleaner.adapters.vision.gemini_vision import _fill

        template = (
            Path("src/road_cleaner/agents/prompts/clearance.md").read_text()
        )
        out = _fill(
            template, hazard_type="debris", lane_position="lane_1",
            description="shed tyre tread",
        )
        assert "{hazard_type}" not in out
        assert "shed tyre tread" in out
        assert '"still_present"' in out


class TestSharedRetryHelper:
    """Both model adapters back off the same way.

    ADK had no protection at all. Once the vision adapter stopped monopolising
    the quota, the jurisdiction agent started taking the 429s instead — and a
    throttled jurisdiction call means a case is held rather than filed.
    """

    def test_adk_wrapper_errors_are_recognised_as_transient(self):
        """ADK raises `_ResourceExhaustedError`, sometimes with no message at
        all, so the type name has to be inspected and not just the text."""
        from road_cleaner.adapters.retry import is_transient

        class _ResourceExhaustedError(Exception):
            pass

        assert is_transient(_ResourceExhaustedError(""))
        assert is_transient(RuntimeError("429 RESOURCE_EXHAUSTED"))
        assert is_transient(RuntimeError("503 UNAVAILABLE"))
        assert not is_transient(RuntimeError("404 NOT_FOUND"))
        assert not is_transient(ValueError("bad prompt"))

    async def test_cancellation_is_never_swallowed(self):
        """A retry loop that treats cancellation as a failure to retry will
        keep a shutting-down process alive."""
        import asyncio

        from road_cleaner.adapters.retry import with_retry

        async def cancelled():
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await with_retry(cancelled, attempts=3)

    async def test_it_gives_up_and_reports_the_last_error(self):
        from road_cleaner.adapters.retry import with_retry

        async def always_throttled():
            raise RuntimeError("429 RESOURCE_EXHAUSTED")

        with pytest.raises(RuntimeError, match="429"):
            await with_retry(always_throttled, attempts=2)
