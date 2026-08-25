"""Wiring.

The one place in the system that knows which adapter is which. Everything else
takes ports and stays ignorant of whether it is talking to SQLite or Firestore,
a simulated camera or a state DOT.

Cloud adapters are imported lazily, inside the branch that needs them, so a
local run never has to have `google-cloud-*` installed at all -- and a missing
optional dependency produces a sentence explaining what to install rather than
an ImportError at startup.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from road_cleaner.adapters.blobs.local_store import LocalBlobStore
from road_cleaner.adapters.bus.memory_bus import InMemoryEventBus
from road_cleaner.adapters.camera.fixture import FixtureCameraSource
from road_cleaner.adapters.camera.scenarios import ScenarioBook
from road_cleaner.adapters.filing.dry_run import DryRunChannel
from road_cleaner.adapters.filing.email_channel import EmailChannel
from road_cleaner.adapters.filing.maintenance_form import MaintenanceFormChannel
from road_cleaner.adapters.filing.open311 import Open311Channel
from road_cleaner.adapters.reasoning.scripted_reasoner import ScriptedReasoner
from road_cleaner.adapters.repo.sqlite_repo import SqliteCaseRepository
from road_cleaner.adapters.vision.scripted_vision import ScriptedVisionAnalyzer
from road_cleaner.config import (
    SEEDS_DIR,
    BlobStoreKind,
    CameraSourceKind,
    EventBusKind,
    FilingChannelKind,
    MediaProviderKind,
    RepositoryKind,
    Settings,
    VisionProvider,
)
from road_cleaner.domain.enums import Channel
from road_cleaner.domain.gating import GateConfig
from road_cleaner.jurisdiction.registry import JurisdictionRegistry
from road_cleaner.logging import get_logger
from road_cleaner.ports.clock import Clock, FrozenClock, SystemClock

log = get_logger(__name__)

# Simulated runs start on a Thursday at 07:00 UTC, because the scenario timeline
# is written as minute offsets that land on the same weekdays and times as the
# cases in the design comps.
DEMO_WEEKDAY = 3  # Thursday
DEMO_HOUR = 7


def demo_start(now: datetime, days: float = 7.0) -> datetime:
    """When a simulated run should begin so it *ends* around now.

    Anchoring to a fixed date in the past means a demo generated today is dated
    weeks ago, and every SLA on the dashboard reads as wildly overdue against the
    real clock. So the start is walked back from now to the most recent Thursday
    07:00 UTC that leaves room for the whole run -- keeping the weekday alignment
    the scenarios assume while producing data that ends in the recent past.
    """
    target = now - timedelta(days=days)
    anchor = target.replace(hour=DEMO_HOUR, minute=0, second=0, microsecond=0)
    if anchor > target:
        anchor -= timedelta(days=1)
    anchor -= timedelta(days=(anchor.weekday() - DEMO_WEEKDAY) % 7)
    return anchor


class MissingDependencyError(RuntimeError):
    """A cloud adapter was selected but its library isn't installed."""


def _require_place_data() -> None:
    """Fail at boot if the place lookup's data files did not ship.

    They are read lazily, deep inside `locate()`, so their absence used to
    surface as an unhandled FileNotFoundError the first time somebody dropped a
    pin -- a bare `HTTP 500` under the map with nothing in it to debug, on a
    deployed image only. An anchorless `data/` in .gcloudignore had matched
    src/road_cleaner/adapters/geo/data and quietly left both files out of the
    build context.

    The ignore file is fixed, but the class of mistake is not: these are the
    only runtime assets that live under a directory name the ignore files
    exclude. Checking here turns a silent mid-session 500 into a container that
    refuses to start, which Cloud Run puts in the log where it can be read.
    """
    from road_cleaner.adapters.geo.places import PLACES_FILE, STATES_FILE

    missing = [p for p in (STATES_FILE, PLACES_FILE) if not p.is_file()]
    if missing:
        raise MissingDependencyError(
            "The place lookup's data files are missing:\n"
            + "\n".join(f"    {p}" for p in missing)
            + "\n\nWithout them no coordinate can be named, so dropping a pin fails."
            "\nIf this is a built image, check that .gcloudignore and .dockerignore"
            "\nstill anchor their `data/` rule as `/data/` -- an unanchored one"
            "\nexcludes this directory too."
        )


@dataclass
class Container:
    """Every wired dependency, built once and passed to the agents."""

    settings: Settings
    clock: Clock
    repository: object
    blobs: object
    bus: object
    cameras: object
    vision: object
    reasoner: object
    filing: object
    jurisdiction: JurisdictionRegistry
    scenarios: ScenarioBook | None = None
    # The simulation surface. `media_blobs` is a *second* blob store, rooted
    # somewhere other than the evidence frames, so generated clips and camera
    # stills cannot land in the same place. See `ports/media`.
    media_blobs: object = None
    video: object = None
    speech: object = None
    music: object = None
    # What signed-in people kept from their own dashcams. A separate store from
    # `repository` because it is owned by a uid and never counts toward the
    # fleet's statistics -- see `ports/incident_store`.
    incidents: object = None

    @property
    def gate_config(self) -> GateConfig:
        s = self.settings
        return GateConfig(
            min_confidence=s.gate_min_confidence,
            corroboration_min_confidence=s.gate_corroboration_min_confidence,
            min_frame_gap_seconds=s.gate_min_frame_gap_seconds,
            max_frame_gap_seconds=s.gate_max_frame_gap_seconds,
            duplicate_radius_meters=s.gate_duplicate_radius_meters,
            watch_margin=s.gate_watch_margin,
        )

    async def startup(self) -> None:
        _require_place_data()
        await self.repository.initialize()
        if self.incidents is not None:
            await self.incidents.initialize()
        await self.bus.start()
        # The agency registry lives in YAML but is mirrored into the store so
        # the dashboard can join against it without reparsing the file.
        for agency in self.jurisdiction.all_agencies():
            await self.repository.upsert_agency(agency)

    async def shutdown(self) -> None:
        for component in (self.bus, self.cameras, self.filing, self.repository, self.incidents):
            if component is None:
                continue
            close = getattr(component, "close", None)
            if close is not None:
                await close()

    def describe(self) -> dict[str, str]:
        """Which adapter is active for each port. Printed by `doctor`."""
        return {
            "clock": type(self.clock).__name__,
            "repository": type(self.repository).__name__,
            "incidents": type(self.incidents).__name__,
            "blobs": type(self.blobs).__name__,
            "bus": type(self.bus).__name__,
            "cameras": type(self.cameras).__name__,
            "vision": type(self.vision).__name__,
            "reasoner": type(self.reasoner).__name__,
            "filing": getattr(self.filing, "name", type(self.filing).__name__),
            "media": type(self.video).__name__,
        }


def build_container(
    settings: Settings,
    *,
    clock: Clock | None = None,
    simulated: bool | None = None,
    run_days: float = 7.0,
) -> Container:
    """Assemble the object graph from configuration.

    `simulated` forces a FrozenClock and the scenario book. Demos and tests use
    it so a week of road time can pass in a moment.
    """
    settings.ensure_directories()

    use_fixtures = settings.camera_source == CameraSourceKind.FIXTURE
    if simulated is None:
        simulated = use_fixtures

    start = demo_start(datetime.now(UTC), run_days)
    resolved_clock = clock or (FrozenClock(start) if simulated else SystemClock())

    scenarios: ScenarioBook | None = None
    if use_fixtures:
        # In simulated mode the book is anchored to the frozen clock's start. In
        # live mode there is no simulated timeline to replay, so it is anchored
        # to the same computed start purely so the fixture cameras have
        # something coherent to draw.
        scenarios = ScenarioBook.load(
            SEEDS_DIR / "scenarios.json",
            resolved_clock.now() if simulated else start,
        )

    media_blobs = LocalBlobStore(Path(settings.media_local_path))
    video, speech, music = _build_media(settings, media_blobs)

    return Container(
        settings=settings,
        clock=resolved_clock,
        repository=_build_repository(settings),
        incidents=_build_incidents(settings),
        blobs=_build_blobs(settings),
        bus=_build_bus(settings),
        cameras=_build_cameras(settings, scenarios, resolved_clock),
        vision=_build_vision(settings, scenarios),
        reasoner=_build_reasoner(settings),
        filing=_build_filing(settings),
        jurisdiction=JurisdictionRegistry.load(SEEDS_DIR / "agencies.yaml"),
        scenarios=scenarios,
        media_blobs=media_blobs,
        video=video,
        speech=speech,
        music=music,
    )


def _require(module: str, extra: str = "cloud"):
    try:
        return __import__(module, fromlist=["*"])
    except ImportError as exc:  # pragma: no cover - depends on install profile
        raise MissingDependencyError(
            f"{module} is needed for this configuration but isn't installed.\n"
            f"Install it with:  uv pip install -e '.[{extra}]'"
        ) from exc


def _build_repository(settings: Settings):
    if settings.repository == RepositoryKind.FIRESTORE:
        from road_cleaner.adapters.repo.firestore_repo import FirestoreCaseRepository

        return FirestoreCaseRepository(
            project=settings.google_cloud_project, database=settings.firestore_database
        )
    return SqliteCaseRepository(Path(settings.sqlite_path))


def _build_incidents(settings: Settings):
    """Where a signed-in person's saved dashcam findings go.

    Follows REPOSITORY, the same switch the case repository uses, so a
    deployment does not end up with its cases in Firestore and its incidents on
    a disk that Cloud Run throws away.
    """
    if settings.repository == RepositoryKind.FIRESTORE:
        from road_cleaner.adapters.incidents.firestore_incidents import FirestoreIncidentStore

        return FirestoreIncidentStore(
            project=settings.google_cloud_project, database=settings.firestore_database
        )
    from road_cleaner.adapters.incidents.local_incidents import LocalIncidentStore

    return LocalIncidentStore(Path(settings.data_dir) / "incidents")


def _build_blobs(settings: Settings):
    if settings.blob_store == BlobStoreKind.GCS:
        from road_cleaner.adapters.blobs.gcs_store import GcsBlobStore

        return GcsBlobStore(bucket=settings.gcs_bucket, project=settings.google_cloud_project)
    return LocalBlobStore(Path(settings.blob_local_path))


def _build_media(settings: Settings, store):
    """The three simulation adapters, or the cached-replay one for all three.

    Returns `(video, speech, music)`. Unlike every other builder here this does
    not follow ROAD_CLEANER_MODE -- see the note on `Settings.media_provider`.
    Video generation is billed per second, so switching it on is always explicit.
    """
    if settings.media_provider != MediaProviderKind.VERTEX:
        from road_cleaner.adapters.media.scripted_media import ScriptedMediaSynthesizer

        replay = ScriptedMediaSynthesizer(Path(settings.media_local_path))
        return replay, replay, replay

    from road_cleaner.adapters.media.chirp_speech import ChirpSpeechSynthesizer
    from road_cleaner.adapters.media.lyria_music import LyriaMusicSynthesizer
    from road_cleaner.adapters.media.veo_video import VeoVideoSynthesizer

    video = VeoVideoSynthesizer(
        model=settings.veo_model,
        store=store,
        project=settings.google_cloud_project,
        location=settings.vertex_media_location,
        use_vertex=settings.google_genai_use_vertexai,
        api_key=settings.google_api_key,
        timeout_seconds=settings.veo_timeout_seconds,
        resolution=settings.veo_resolution,
        enhance_prompt=settings.veo_enhance_prompt,
    )
    speech = ChirpSpeechSynthesizer(voice=settings.tts_voice, store=store)
    music = LyriaMusicSynthesizer(
        model=settings.lyria_model,
        store=store,
        project=settings.google_cloud_project,
        location=settings.vertex_media_location,
    )
    return video, speech, music


def _build_bus(settings: Settings):
    if settings.event_bus == EventBusKind.PUBSUB:
        from road_cleaner.adapters.bus.pubsub_bus import PubSubEventBus

        return PubSubEventBus(
            project=settings.google_cloud_project,
            topics={
                "frame.captured": settings.pubsub_topic_frames,
                "hazard.confirmed": settings.pubsub_topic_hazards,
            },
            dead_letter_topic=settings.pubsub_topic_dlq,
        )
    return InMemoryEventBus()


def _build_cameras(settings: Settings, scenarios: ScenarioBook | None, clock: Clock):
    if settings.camera_source == CameraSourceKind.VENDOR511:
        from road_cleaner.adapters.camera.vendor511 import Vendor511CameraSource

        return Vendor511CameraSource(
            base_urls=settings.state_base_urls,
            api_keys=settings.state_api_keys,
            rate_limit=settings.state_api_rate_limit,
            rate_window=settings.state_api_rate_window_seconds,
        )
    assert scenarios is not None  # fixture mode always builds a scenario book
    return FixtureCameraSource(SEEDS_DIR / "cameras.json", scenarios, clock)


def _build_vision(settings: Settings, scenarios: ScenarioBook | None):
    if settings.vision_provider == VisionProvider.GEMINI:
        from road_cleaner.adapters.vision.gemini_vision import GeminiVisionAnalyzer

        return GeminiVisionAnalyzer(
            model=settings.gemini_model,
            project=settings.google_cloud_project,
            location=settings.google_cloud_location,
            use_vertex=settings.google_genai_use_vertexai,
            api_key=settings.google_api_key,
            prefilter_model=settings.gemma_model if settings.gemma_prefilter_enabled else None,
            max_concurrency=settings.vision_max_concurrency,
            max_retries=settings.vision_max_retries,
        )
    assert scenarios is not None
    return ScriptedVisionAnalyzer(scenarios)


def _build_reasoner(settings: Settings):
    if settings.use_adk:
        from road_cleaner.adapters.reasoning.adk_reasoner import AdkReasoner

        return AdkReasoner(
            model=settings.gemini_model,
            project=settings.google_cloud_project,
            location=settings.google_cloud_location,
            use_vertex=settings.google_genai_use_vertexai,
            max_concurrency=2,
            max_retries=settings.vision_max_retries,
        )
    return ScriptedReasoner()


def _build_channel(kind: str, settings: Settings):
    """The transport that would carry a report if we were sending it."""
    if kind == FilingChannelKind.OPEN311:
        return Open311Channel(
            settings.open311_endpoint,
            settings.open311_api_key,
            from_address=settings.filing_from_address,
        )
    if kind == FilingChannelKind.EMAIL:
        return EmailChannel(
            host=settings.smtp_host,
            port=settings.smtp_port,
            user=settings.smtp_user,
            password=settings.smtp_password,
            from_address=settings.filing_from_address,
            attachment_root=Path(settings.blob_local_path),
        )
    return MaintenanceFormChannel(settings.filing_from_address)


def channel_for_agency(agency_channel: Channel, settings: Settings):
    """Pick the transport an agency actually accepts.

    Different agencies take different things -- Durham has Open311, GDOT has a
    web form, most FDOT districts take email -- so the channel is a property of
    the agency, not a global setting.
    """
    return _build_channel(agency_channel.value, settings)


def _build_filing(settings: Settings):
    """Build the default channel, wrapped in dry run unless explicitly disabled.

    Note the wrapping is on by default and independent of mode: a cloud
    deployment is still dry run until somebody says otherwise.
    """
    inner = _build_channel(settings.filing_channel, settings)
    if settings.dry_run or settings.filing_channel == FilingChannelKind.DRY_RUN:
        return DryRunChannel(inner, Path(settings.filing_outbox))
    log.warning(
        "DRY_RUN is off — reports will be transmitted to real agencies",
        extra={"channel": inner.name},
    )
    return inner
