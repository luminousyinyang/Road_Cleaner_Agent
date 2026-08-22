"""The simulation surface.

The tests that matter most here are not about video. They are about the boundary
between generated media and camera evidence -- that generated artifacts are
always labelled, always stored apart, and can never be served as or attached to
the evidence behind a filed report. Everything else is presentation.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from road_cleaner.adapters.media.manifest import manifest_key, write_manifest
from road_cleaner.adapters.media.scenario_prompt import (
    UNSIMULATABLE,
    UnsimulatableHazardError,
    scenario_prompt,
)
from road_cleaner.adapters.media.scripted_media import ScriptedMediaSynthesizer
from road_cleaner.config import MediaProviderKind, Settings
from road_cleaner.domain.enums import CaseKind, HazardType, Severity
from road_cleaner.domain.models import Camera, Case
from road_cleaner.ports.media import (
    SYNTHETIC_PREFIX,
    MediaUnavailableError,
    SyntheticClip,
    is_synthetic_key,
)
from road_cleaner.web.serializers import media_for_case


def _case(hazard: HazardType = HazardType.DEBRIS, **kw) -> Case:
    return Case(
        id=kw.pop("id", "GA-0001"),
        state="GA",
        camera_id="GDOT-CCTV-0312",
        kind=CaseKind.FILED,
        hazard_type=hazard,
        hazard_title=kw.pop("title", "Debris in lane 1"),
        location="I-75 northbound",
        severity=Severity.MEDIUM,
        **kw,
    )


def _camera() -> Camera:
    return Camera(
        id="GDOT-CCTV-0312",
        state="GA",
        name="howell mill road",
        road="I-75",
        direction="northbound",
        lat=33.8,
        lng=-84.4,
        snapshot_url="https://example.invalid/x.jpg",
    )


# ------------------------------------------------------------------ labelling


def test_synthetic_clips_are_always_marked_synthetic():
    clip = SyntheticClip(
        key="synthetic/GA-0001/x.mp4", mime_type="video/mp4",
        model_name="veo-3.1-fast-generate-001", prompt="p",
    )
    assert clip.synthetic is True
    assert "SYNTHETIC" in clip.label
    assert "veo-3.1-fast-generate-001" in clip.label


def test_is_synthetic_key_separates_generated_from_evidence():
    assert is_synthetic_key("synthetic/GA-0001/clip.mp4")
    # Evidence keys look like STATE/CAMERA/TIMESTAMP.jpg and must never pass.
    assert not is_synthetic_key("GA/GDOT-CCTV-0312/2026-08-11T20-30-00.jpg")
    assert not is_synthetic_key("frames/synthetic/sneaky.mp4")


# --------------------------------------------------------------------- prompt


def test_prompt_is_built_from_the_detection_not_the_case_narrative():
    """`Case.sentence` narrates workflow progress and would poison the prompt."""
    case = _case(sentence="Reported to Georgia DOT, gone 1d 6h later.")
    prompt = scenario_prompt(
        case, _camera(), "lane_1", description="Shed tire tread in the left travel lane."
    )
    assert "Shed tire tread" in prompt
    assert "Georgia DOT" not in prompt
    assert "1d 6h" not in prompt


def test_prompt_describes_road_character_but_never_names_it():
    """Named roads made Veo paint gantry signs, and generated signage is garbled.

    One clip rendered a sign reading "Howell Mill Road Road".
    """
    prompt = scenario_prompt(_case(), _camera(), "lane_1")
    assert "interstate" in prompt.lower()
    assert "dashcam" in prompt.lower()
    assert "I-75" not in prompt
    assert "Howell Mill" not in prompt


def test_prompt_anchors_the_physical_size_of_the_hazard():
    """Without a size anchor Veo renders debris car-sized. This is the fix."""
    prompt = scenario_prompt(_case(), _camera(), "lane_1")
    assert "40 centimetres" in prompt  # the anchor for the rubber scrap
    assert "keeps exactly the same size and shape" in prompt
    assert "never rises higher than the kerb" in prompt


def test_scale_words_in_the_analyst_description_are_dropped():
    """"Large dark object" is exactly the instruction that oversized the debris."""
    inflating = scenario_prompt(
        _case(), _camera(), "lane_1", description="Large dark object in the lane."
    )
    assert "Large dark object" not in inflating

    safe = scenario_prompt(
        _case(), _camera(), "lane_1", description="Shed tire tread, partially shadowed."
    )
    assert "Shed tire tread" in safe


def test_negative_prompt_excludes_the_failure_modes_we_actually_hit():
    from road_cleaner.adapters.media.scenario_prompt import NEGATIVE_PROMPT

    for excluded in ("oversized", "morphing", "duplicated", "garbled text", "cinematic"):
        assert excluded in NEGATIVE_PROMPT


def test_prompt_refuses_hazards_depicting_a_person():
    """'Roads, not people' -- enforced by name, not left to the safety filter."""
    assert "pedestrian_on_highway" in UNSIMULATABLE
    with pytest.raises(UnsimulatableHazardError):
        scenario_prompt(_case(HazardType.PEDESTRIAN_ON_HIGHWAY, title="Person on I-75"))


def test_prompt_survives_an_unmapped_hazard_type():
    prompt = scenario_prompt(_case(HazardType.UNREPORTED_CLOSURE, title="Lanes closed"))
    assert prompt.strip()


# ---------------------------------------------------------------- provenance


async def test_manifest_records_the_real_model_name(tmp_path: Path):
    """Filenames were guessed from once, and produced 'generated by briefing'."""
    from road_cleaner.adapters.blobs.local_store import LocalBlobStore

    store = LocalBlobStore(tmp_path)
    key = f"{SYNTHETIC_PREFIX}GA-0001/2026-08-21T22-13-14-briefing.mp3"
    await store.put(key, b"audio", content_type="audio/mpeg")
    await write_manifest(
        store,
        SyntheticClip(
            key=key, mime_type="audio/mpeg",
            model_name="en-US-Chirp3-HD-Achernar", prompt="briefing text",
        ),
    )

    written = json.loads((tmp_path / manifest_key(key)).read_text())
    assert written["model_name"] == "en-US-Chirp3-HD-Achernar"

    shown = media_for_case(tmp_path, "GA-0001")
    assert len(shown) == 1
    assert shown[0]["badge"] == "SYNTHETIC — generated by en-US-Chirp3-HD-Achernar"
    assert shown[0]["kind"] == "audio"


def test_media_listing_is_empty_when_nothing_was_generated(tmp_path: Path):
    assert media_for_case(tmp_path, "GA-0001") == []
    assert media_for_case(None, "GA-0001") == []


# ------------------------------------------------------------------ scripted


async def test_scripted_synthesizer_refuses_rather_than_inventing(tmp_path: Path):
    """With no cached clip it must fail, never substitute a real frame."""
    replay = ScriptedMediaSynthesizer(tmp_path)
    with pytest.raises(MediaUnavailableError):
        await replay.render_scenario(prompt="anything")


async def test_scripted_synthesizer_replays_the_newest_clip(tmp_path: Path):
    folder = tmp_path / SYNTHETIC_PREFIX / "GA-0001"
    folder.mkdir(parents=True)
    (folder / "clip.mp4").write_bytes(b"video-bytes")

    clip = await ScriptedMediaSynthesizer(tmp_path).render_scenario(prompt="p")
    assert clip.key == f"{SYNTHETIC_PREFIX}GA-0001/clip.mp4"
    assert clip.synthetic is True
    # It must not claim to be Veo when it called nothing.
    assert "veo" not in clip.model_name.lower()


# --------------------------------------------------------------------- config


def test_media_generation_is_off_by_default_even_in_cloud_mode(tmp_path: Path):
    """Every other adapter follows ROAD_CLEANER_MODE. This one bills per second."""
    cloud = Settings(ROAD_CLEANER_MODE="cloud", DATA_DIR=str(tmp_path))
    assert cloud.media_provider == MediaProviderKind.SCRIPTED


def test_generated_media_defaults_beside_but_not_inside_the_evidence_store(tmp_path):
    s = Settings(ROAD_CLEANER_MODE="local", DATA_DIR=str(tmp_path))
    assert s.media_local_path != s.blob_local_path
    assert not str(s.media_local_path).startswith(str(s.blob_local_path))


def test_doctor_flags_gemma_enabled_without_a_deployed_endpoint(tmp_path: Path):
    """A bare Gemma name is not a Vertex publisher model and 404s on every frame."""
    bare = Settings(
        DATA_DIR=str(tmp_path), GEMMA_PREFILTER_ENABLED=True, GEMMA_MODEL="gemma-3-4b-it"
    )
    assert any("GEMMA_MODEL" in m for m in bare.missing_for_live())

    deployed = Settings(
        DATA_DIR=str(tmp_path),
        GEMMA_PREFILTER_ENABLED=True,
        GEMMA_MODEL="projects/p/locations/us-central1/endpoints/123",
    )
    assert not any("GEMMA_MODEL" in m for m in deployed.missing_for_live())


# ------------------------------------------------------------- render jobs


async def test_render_job_reports_honest_progress():
    """Veo gives no percentage, so the bar must not claim to know one."""
    from road_cleaner.web.jobs import RenderJob

    running = RenderJob(id="j1", case_id="GA-0001")
    snapshot = running.as_dict()
    assert snapshot["state"] == "running"
    # Never sits at 100% while still going.
    assert snapshot["estimated_fraction"] < 1.0
    assert "typical_seconds" in snapshot

    running.state = "done"
    running.finished_at = running.started_at + 30.0
    assert running.as_dict()["estimated_fraction"] == 1.0
    assert running.as_dict()["elapsed"] == 30.0


async def test_a_failed_render_reports_the_reason(tmp_path: Path):
    """A job must surface why, not just fail silently."""
    from road_cleaner.web.jobs import RenderJobs

    class Boom:
        async def render_scenario(self, **kw):
            raise MediaUnavailableError("Veo is rate limited (429).")

    class FakeContainer:
        video = Boom()

    jobs = RenderJobs()
    job = jobs.start(FakeContainer(), "GA-0001", "prompt", 8)
    for _ in range(50):
        if job.state != "running":
            break
        await asyncio.sleep(0.01)
    assert job.state == "failed"
    assert "429" in job.error


async def test_one_render_per_case_so_a_double_click_cannot_bill_twice():
    from road_cleaner.web.jobs import RenderJobs

    started = []

    class Slow:
        async def render_scenario(self, **kw):
            started.append(1)
            await asyncio.sleep(0.2)
            return SyntheticClip(
                key="synthetic/GA-0001/x.mp4", mime_type="video/mp4",
                model_name="veo", prompt="p",
            )

    class FakeContainer:
        video = Slow()

    jobs = RenderJobs()
    c = FakeContainer()
    first = jobs.start(c, "GA-0001", "p", 8)
    second = jobs.start(c, "GA-0001", "p", 8)
    assert first.id == second.id
    await asyncio.sleep(0.35)
    assert len(started) == 1


async def test_a_cancelled_render_does_not_leave_the_page_polling_forever():
    """CancelledError is a BaseException and would slip past `except Exception`."""
    from road_cleaner.web.jobs import RenderJobs

    class Hangs:
        async def render_scenario(self, **kw):
            await asyncio.sleep(30)

    class FakeContainer:
        video = Hangs()

    jobs = RenderJobs()
    job = jobs.start(FakeContainer(), "GA-0001", "p", 8)
    await asyncio.sleep(0.05)
    task = next(t for t in asyncio.all_tasks() if t is not asyncio.current_task())
    task.cancel()
    await asyncio.sleep(0.05)

    assert job.state == "failed"
    assert job.as_dict()["estimated_fraction"] == 1.0


def test_the_size_clamp_matches_the_hazard():
    """"Never larger than a car wheel" is nonsense for a stalled sedan."""
    debris = scenario_prompt(_case(HazardType.DEBRIS), _camera())
    assert "never rises higher than the kerb" in debris

    vehicle = scenario_prompt(_case(HazardType.STALLED_VEHICLE, title="Stalled car"), _camera())
    assert "kerb" not in vehicle
    assert "same size as the other cars" in vehicle

    water = scenario_prompt(_case(HazardType.FLOODING, title="Flooding"), _camera())
    assert "kerb" not in water
    assert "no depth to it" in water


# ---------------------------------------------------------------- pruning


async def test_a_new_clip_replaces_the_old_one(tmp_path: Path):
    from road_cleaner.adapters.blobs.local_store import LocalBlobStore
    from road_cleaner.adapters.media.manifest import prune_superseded

    store = LocalBlobStore(tmp_path)
    old = f"{SYNTHETIC_PREFIX}GA-0001/2026-08-21T10-00-00-veo.mp4"
    new = f"{SYNTHETIC_PREFIX}GA-0001/2026-08-22T10-00-00-veo.mp4"
    for key in (old, new):
        await store.put(key, b"video", content_type="video/mp4")
        await write_manifest(
            store,
            SyntheticClip(key=key, mime_type="video/mp4", model_name="veo", prompt="p"),
        )

    removed = await prune_superseded(store, new, f"{SYNTHETIC_PREFIX}GA-0001")
    assert removed == 2  # the old clip and its sidecar
    assert await store.exists(new)
    assert not await store.exists(old)
    assert not await store.exists(manifest_key(old))


async def test_pruning_a_clip_never_deletes_the_spoken_briefing(tmp_path: Path):
    """A case folder holds video and audio side by side. They are unrelated."""
    from road_cleaner.adapters.blobs.local_store import LocalBlobStore
    from road_cleaner.adapters.media.manifest import prune_superseded

    store = LocalBlobStore(tmp_path)
    briefing = f"{SYNTHETIC_PREFIX}GA-0001/2026-08-21T10-00-00-briefing.mp3"
    old_clip = f"{SYNTHETIC_PREFIX}GA-0001/2026-08-21T10-00-00-veo.mp4"
    new_clip = f"{SYNTHETIC_PREFIX}GA-0001/2026-08-22T10-00-00-veo.mp4"
    for key, mime in (
        (briefing, "audio/mpeg"), (old_clip, "video/mp4"), (new_clip, "video/mp4")
    ):
        await store.put(key, b"data", content_type=mime)

    await prune_superseded(store, new_clip, f"{SYNTHETIC_PREFIX}GA-0001")
    assert await store.exists(briefing)
    assert await store.exists(new_clip)
    assert not await store.exists(old_clip)


async def test_pruning_one_case_leaves_other_cases_alone(tmp_path: Path):
    from road_cleaner.adapters.blobs.local_store import LocalBlobStore
    from road_cleaner.adapters.media.manifest import prune_superseded

    store = LocalBlobStore(tmp_path)
    other = f"{SYNTHETIC_PREFIX}NC-1171/clip.mp4"
    new = f"{SYNTHETIC_PREFIX}GA-0001/new.mp4"
    for key in (other, new):
        await store.put(key, b"video", content_type="video/mp4")

    await prune_superseded(store, new, f"{SYNTHETIC_PREFIX}GA-0001")
    assert await store.exists(other)


async def test_list_keys_is_scoped_and_newest_first(tmp_path: Path):
    from road_cleaner.adapters.blobs.local_store import LocalBlobStore

    store = LocalBlobStore(tmp_path)
    await store.put("GA/CAM/a.jpg", b"1")
    await store.put(f"{SYNTHETIC_PREFIX}GA-0001/b.mp4", b"2")

    scoped = await store.list_keys(SYNTHETIC_PREFIX)
    assert scoped == [f"{SYNTHETIC_PREFIX}GA-0001/b.mp4"]
    # Evidence is not reachable from the synthetic prefix.
    assert not any("CAM" in k for k in scoped)


async def test_pruning_scope_does_not_bleed_between_similar_case_ids(tmp_path: Path):
    """GCS prefix matching is string-based: "GA-446" would catch GA-4460 too."""
    from road_cleaner.adapters.blobs.local_store import LocalBlobStore
    from road_cleaner.adapters.media.manifest import prune_superseded
    from road_cleaner.adapters.media.veo_video import _case_prefix

    assert _case_prefix("GA-4462").endswith("/")

    store = LocalBlobStore(tmp_path)
    sibling = f"{SYNTHETIC_PREFIX}GA-4460/clip.mp4"
    new = f"{SYNTHETIC_PREFIX}GA-4462/new.mp4"
    for key in (sibling, new):
        await store.put(key, b"video", content_type="video/mp4")

    await prune_superseded(store, new, _case_prefix("GA-4462"))
    assert await store.exists(sibling)
    assert await store.exists(new)


async def test_list_keys_works_when_the_store_root_is_relative(tmp_path, monkeypatch):
    """`_path` resolves but `self.root` may not -- relative_to raised on that.

    Missed first time because tmp_path is absolute, while the real configured
    root is the relative "data/media". Pruning silently failed in production.
    """
    from road_cleaner.adapters.blobs.local_store import LocalBlobStore

    monkeypatch.chdir(tmp_path)
    (tmp_path / "media").mkdir()
    store = LocalBlobStore(Path("media"))  # relative, as configured
    await store.put(f"{SYNTHETIC_PREFIX}GA-0001/clip.mp4", b"v", content_type="video/mp4")

    keys = await store.list_keys(SYNTHETIC_PREFIX)
    assert keys == [f"{SYNTHETIC_PREFIX}GA-0001/clip.mp4"]
